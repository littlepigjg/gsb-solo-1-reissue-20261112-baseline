import io
import time

import numpy as np
import pytest
from PIL import Image

from backend.app import create_app
from backend.expressions import ExpressionError, compile_expression, evaluate
from backend.dynamic import step_context, orientation_of


def upload(client, width, height, name, color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    buffer.seek(0)
    return client.post("/api/assets", data={"file": (buffer, name)}).json["data"]


@pytest.fixture
def landscape(client):
    return upload(client, 400, 225, "海边的日落.png")


@pytest.fixture
def portrait(client):
    return upload(client, 225, 400, "街头肖像.png", "blue")


def wait_job(client, job_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


DYNAMIC_STEPS = [
    {
        "operation": "crop_ratio",
        "params": {"ratio_width": 16, "ratio_height": 9},
        "rules": [{
            "label": "竖图改 4:5",
            "when": 'source.orientation == "portrait"',
            "params": {"ratio_width": 4, "ratio_height": 5},
        }],
    },
    {
        "operation": "watermark_text",
        "params": {"text": "固定文字", "size": 30, "margin": 24},
        "rules": [{
            "label": "按宽度缩放水印",
            "when": "width >= 100",
            "params": {
                "size": {"$expr": "clamp(round(width / 20), 8, 80)"},
                "text": {"$expr": '"© " + source.stem'},
            },
        }],
    },
]


def test_expression_language_is_arithmetic_only():
    assert evaluate(compile_expression("min(3, 4) + round(2.5)"), {}) == 6
    for unsafe in [
        "__import__('os')",
        "open('/etc/passwd')",
        "source.__class__",
        "width.real",
        "[1,2][0]",
        "lambda: 1",
        "eval('1')",
    ]:
        with pytest.raises(ExpressionError):
            compile_expression(unsafe)


def test_expression_type_safety():
    with pytest.raises(ExpressionError):
        evaluate(compile_expression("width + source.name"), {"width": 10, "source.name": "x"})
    with pytest.raises(ExpressionError):
        evaluate(compile_expression("not width"), {"width": 10})
    with pytest.raises(ExpressionError):
        evaluate(compile_expression("width / 0"), {"width": 10})
    with pytest.raises(ExpressionError):
        evaluate(compile_expression("missing_variable"), {})


def test_orientation_context():
    assert orientation_of(400, 225) == "landscape"
    assert orientation_of(225, 400) == "portrait"
    assert orientation_of(300, 300) == "square"
    context = step_context(800, 600, {
        "asset_id": "a" * 32, "name": "名字.png", "kind": "original",
        "tags": ["t"], "width": 800, "height": 600,
    })
    assert context["source.orientation"] == "landscape"
    assert context["source.stem"] == "名字"
    assert context["source.tags"] == ["t"]


def test_rules_resolve_per_image(client, landscape, portrait):
    def resolve(asset):
        response = client.post("/api/resolve", json={"asset_id": asset["id"], "steps": DYNAMIC_STEPS})
        assert response.status_code == 200, response.json
        return response.json["data"]

    wide = resolve(landscape)
    tall = resolve(portrait)
    assert wide["steps"][0]["matched_rules"] == []
    assert tall["steps"][0]["matched_rules"][0]["label"] == "竖图改 4:5"
    assert (tall["steps"][0]["params"]["ratio_width"], tall["steps"][0]["params"]["ratio_height"]) == (4, 5)
    # Step 2 runs on the crop output, so its context is the post-crop size:
    # 400x225 -> clamp(round(400/20), 8, 80) = 20, and 225x281 -> clamp(round(225/20)) = 11.
    assert wide["steps"][1]["params"]["size"] == 20
    assert wide["steps"][1]["params"]["text"] == "© 海边的日落"
    assert tall["steps"][1]["params"]["size"] == 11
    assert wide["output_size"] == {"width": 400, "height": 225}
    assert tall["output_size"] == {"width": 225, "height": 281}


def test_rule_error_names_rule_and_image(client, landscape, portrait):
    steps = [{
        "operation": "resize_exact",
        "params": {"width": 256},
        "rules": [{
            "label": "除以零",
            "when": 'source.orientation == "portrait"',
            "params": {"height": {"$expr": "source.width / 0"}},
        }],
    }]
    response = client.post("/api/preview", json={"asset_id": portrait["id"], "steps": steps})
    assert response.status_code == 400
    error = response.json["error"]
    assert error["code"] == "rule_error"
    assert error["details"]["rule"] == "除以零"
    assert error["details"]["parameter"] == "height"
    assert error["details"]["step"] == 1
    # The same pipeline is valid for the landscape asset: nothing is poisoned.
    assert client.post("/api/preview", json={"asset_id": landscape["id"], "steps": steps}).status_code == 200


def test_computed_values_still_pass_strict_checks(client, landscape):
    steps = [{
        "operation": "resize_exact",
        "params": {"width": 100},
        "rules": [{
            "label": "越界公式",
            "when": "true",
            "params": {"width": {"$expr": "source.width * 40"}},
        }],
    }]
    response = client.post("/api/preview", json={"asset_id": landscape["id"], "steps": steps})
    assert response.status_code == 400
    assert response.json["error"]["details"]["expression"] == "source.width * 40"
    assert "不能大于 8192" in response.json["error"]["message"]


def test_bad_rule_syntax_rejected_at_validation(client):
    for rules in [
        [{"label": "x", "when": "width >", "params": {"width": 10}}],
        [{"label": "x", "when": "true", "params": {}}],
        [{"label": "x", "when": "true", "params": {"nope": 1}}],
        [{"label": "x", "when": "true", "params": {"width": {"$expr": "open('x')"}}}],
        [{"label": "x", "when": "true", "params": {"width": {"$expr": "width", "extra": 1}}}],
    ]:
        response = client.post("/api/presets", json={
            "name": "坏预设", "steps": [{"operation": "resize_exact", "params": {}, "rules": rules}],
        })
        assert response.status_code == 400, rules


def test_preview_process_and_job_agree(client, landscape, portrait, app):
    steps = DYNAMIC_STEPS
    preview = client.post("/api/preview", json={"asset_id": portrait["id"], "steps": steps})
    assert preview.status_code == 200
    assert "X-Studio-Dynamic" in preview.headers
    preview_pixels = np.asarray(Image.open(io.BytesIO(preview.data)).convert("RGB"))

    result = client.post("/api/process", json={"asset_id": portrait["id"], "steps": steps})
    assert result.status_code == 201
    processed = np.asarray(
        Image.open(io.BytesIO(client.get(result.json["data"]["url"]).data)).convert("RGB"),
    )
    assert result.json["data"]["metadata"]["dynamic"][0]["matched_rules"][0]["label"] == "竖图改 4:5"

    job = wait_job(client, client.post("/api/jobs", json={
        "asset_ids": [portrait["id"]], "steps": steps,
    }).json["data"]["id"])
    assert job["status"] == "completed"
    result_id = job["results"][0]["asset_id"]
    batched = np.asarray(
        Image.open(io.BytesIO(client.get(f"/api/assets/{result_id}/file").data)).convert("RGB"),
    )
    # Preview is downscaled to fit 1200x1000; here it already fits, so compare exactly.
    assert preview_pixels.shape == processed.shape == batched.shape
    assert np.array_equal(processed, batched)
    assert np.array_equal(preview_pixels, processed)


def test_job_snapshot_is_frozen_at_submit(client, landscape, app):
    steps = [{
        "operation": "watermark_text",
        "params": {"text": "x"},
        "rules": [{
            "label": "按名称",
            "when": '"提交时" in source.name',
            "params": {"size": 40},
        }],
    }]
    submitted = client.post("/api/jobs", json={"asset_ids": [landscape["id"], ], "steps": steps})
    assert submitted.status_code == 202
    job = wait_job(client, submitted.json["data"]["id"])
    snapshot = job["snapshots"][landscape["id"]]
    assert snapshot["name"] == "海边的日落.png"

    client.patch(f"/api/assets/{landscape['id']}", json={"name": "重新命名.png"})
    again = wait_job(client, client.post("/api/jobs", json={
        "asset_ids": [landscape["id"]], "steps": steps,
    }).json["data"]["id"])
    assert again["snapshots"][landscape["id"]]["name"] == "重新命名.png"
    # The first job's stored record is unchanged by the rename.
    stored = client.get(f"/api/jobs/{job['id']}").json["data"]
    assert stored["snapshots"][landscape["id"]]["name"] == "海边的日落.png"
    assert job["status"] == again["status"] == "completed"


def test_caller_supplied_snapshot_must_match_asset(client, landscape):
    bad = {"asset_id": landscape["id"], "name": "x.png", "kind": "original", "tags": [], "width": 999, "height": 999}
    response = client.post("/api/preview", json={"asset_id": landscape["id"], "steps": [], "snapshot": bad})
    assert response.status_code == 400
    response = client.post("/api/jobs", json={"asset_ids": [landscape["id"]], "steps": [{"operation": "color_invert"}], "snapshots": {landscape["id"]: bad}})
    assert response.status_code == 400


def test_preset_round_trip_keeps_rules(client):
    preset = client.post("/api/presets", json={
        "name": "动态预设",
        "steps": [{
            "operation": "resize_exact",
            "params": {},
            "rules": [{"label": "宽图", "when": 'source.orientation == "landscape"',
                      "params": {"width": {"$expr": "clamp(source.width, 1, 4096)"}}}],
        }],
    })
    assert preset.status_code == 201
    stored = client.get("/api/presets").json["data"]
    record = next(item for item in stored if item["id"] == preset.json["data"]["id"])
    assert record["steps"][0]["rules"][0]["params"]["width"]["$expr"] == "clamp(source.width, 1, 4096)"


def test_builtin_adaptive_preset_works_on_all_orientations(client):
    builtin = next(item for item in client.get("/api/presets").json["data"] if item.get("builtin_key") == "adaptive")
    for width, height, name in [(800, 400, "横.png"), (400, 800, "竖.png"), (500, 500, "方.png")]:
        asset = upload(client, width, height, name)
        response = client.post("/api/preview", json={"asset_id": asset["id"], "steps": builtin["steps"]})
        assert response.status_code == 200
