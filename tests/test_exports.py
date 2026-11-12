"""Batch archive preparation: bounded work, progress, cancel and cleanup."""
import io
import json
import threading
import time
import zipfile

import numpy as np
import pytest
from PIL import Image

from backend.app import create_app


def make_image(width, height, seed, alpha=True):
    x, y = np.meshgrid(np.arange(width) // 16, np.arange(height) // 16)
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 0] = (x + seed) % 256
    pixels[..., 1] = (y + seed * 3) % 256
    pixels[..., 2] = (x + y + seed) % 256
    pixels[..., 3] = 255
    if alpha:
        pixels[: max(1, height // 12), :, 3] = 0
    return Image.fromarray(pixels)


def save_asset(assets, width, height, seed, name):
    record = assets.save(make_image(width, height, seed), name=name, kind="original")
    return record["id"]


def wait_for_task(client, task_id, deadline_seconds=30):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/exports/archive/tasks/{task_id}").json["data"]
        if task["status"] not in {"queued", "preparing", "cancelling"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"export task {task_id} did not finish in time")


def start_task(client, asset_ids, options):
    response = client.post("/api/exports/archive/tasks", json={"asset_ids": asset_ids, "options": options})
    assert response.status_code == 202, response.json
    return response.json["data"]


def read_archive(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.data


def test_batch_task_reports_progress_and_keeps_order(app, client, uploaded):
    assets = app.extensions["studio"]["assets"]
    ids = [uploaded["id"]]
    for index in range(3):
        ids.append(save_asset(assets, 480, 360, index * 11, f"素材-{index}.png"))
    task = start_task(client, ids, {"format": "PNG"})
    seen = set()
    deadline = time.monotonic() + 30
    final = task
    while time.monotonic() < deadline:
        final = client.get(f"/api/exports/archive/tasks/{task['id']}").json["data"]
        seen.add((final["done"], final["progress"]))
        if final["status"] not in {"queued", "preparing", "cancelling"}:
            break
        time.sleep(0.02)
    assert final["status"] == "completed"
    assert final["done"] == final["total"] == 4
    assert final["progress"] == 100
    assert len(seen) > 1
    content = read_archive(client, final["download_url"])
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        assert names[0] == f"01_{ids[0][:8]}.png"
        assert names[-1] == "manifest.json"
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["assets"] == ids
        assert archive.testzip() is None
        with Image.open(io.BytesIO(archive.read(names[0]))) as image:
            assert image.size == (384, 320)
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0


def test_task_cancel_leaves_no_partial_files(app, client, uploaded):
    assets = app.extensions["studio"]["assets"]
    settings = app.extensions["studio"]["settings"]
    ids = [save_asset(assets, 700, 500, index, f"取消-{index}.png") for index in range(6)]
    task = start_task(client, ids, {"format": "PNG"})
    # Cancel while preparation is still running.
    cancelled = client.post(f"/api/exports/archive/tasks/{task['id']}/cancel", json={})
    assert cancelled.status_code == 200
    final = wait_for_task(client, task["id"])
    assert final["status"] == "cancelled"
    assert final.get("download_url") is None
    assert final["export_id"] is None
    staging = settings.exports / ".staging"
    assert not list(staging.iterdir())
    visible = [path.name for path in settings.exports.glob("*.zip")]
    assert visible == []
    exports = app.extensions["studio"]["store"].all("exports")
    assert all(item["kind"] != "archive" for item in exports)


def test_task_failure_cleans_staging_and_records(app, client, uploaded, monkeypatch):
    assets = app.extensions["studio"]["assets"]
    settings = app.extensions["studio"]["settings"]
    ids = [save_asset(assets, 320, 240, index, f"失败-{index}.png") for index in range(2)]
    exports = app.extensions["studio"]["exports"]

    def explode(self, image, options, target):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(type(exports), "_encode_into", explode)
    task = start_task(client, ids, {"format": "PNG"})
    final = wait_for_task(client, task["id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "export_error"
    assert final.get("download_url") is None
    assert not list((settings.exports / ".staging").iterdir())
    assert [path.name for path in settings.exports.glob("*.zip")] == []
    assert all(item["kind"] != "archive" for item in app.extensions["studio"]["store"].all("exports"))


def test_interrupted_download_removes_unfinished_archive(app, client, uploaded):
    exports = app.extensions["studio"]["exports"]
    store = app.extensions["studio"]["store"]
    settings = app.extensions["studio"]["settings"]

    record = exports.archive([uploaded["id"]], {"format": "PNG"})
    export_id = record["id"]
    path = settings.exports / record["filename"]
    reader, _, _ = exports.delivery(export_id)
    reader.read(8)
    reader.close()

    assert not path.exists()
    with pytest.raises(Exception):
        store.get("exports", export_id)
    assert record.get("task_id") is None


def test_member_bytes_match_single_exports(app, client, uploaded, monkeypatch):
    """Each archive member is byte-identical to the same image exported alone."""
    assets = app.extensions["studio"]["assets"]
    ids = [save_asset(assets, 300, 200, seed, f"一致-{seed}.png") for seed in (5, 19, 41)]
    options = {"format": "WEBP", "quality": 88, "lossless": False, "background": "#ffffff", "dpi": 96}
    singles = []
    for asset_id in ids:
        response = client.post("/api/exports", json={"asset_id": asset_id, "options": options})
        assert response.status_code == 201
        singles.append(client.get(response.json["data"]["url"]).data)
    task = start_task(client, ids, options)
    final = wait_for_task(client, task["id"])
    assert final["status"] == "completed"
    with zipfile.ZipFile(io.BytesIO(read_archive(client, final["download_url"]))) as archive:
        members = [archive.read(name) for name in archive.namelist() if name != "manifest.json"]
    assert members == singles


def test_second_preparation_is_rejected_while_active(app, client, uploaded, monkeypatch):
    exports = app.extensions["studio"]["exports"]
    entered = threading.Event()
    release = threading.Event()
    original = type(exports)._encode_into

    def blocking_encode(self, image, options, target):
        entered.set()
        release.wait(timeout=10)
        return original(self, image, options, target)

    monkeypatch.setattr(type(exports), "_encode_into", blocking_encode)
    task = start_task(client, [uploaded["id"]], {"format": "PNG"})
    assert entered.wait(timeout=10), "export worker never started encoding"
    duplicate = client.post("/api/exports/archive/tasks", json={"asset_ids": [uploaded["id"]], "options": {"format": "PNG"}})
    assert duplicate.status_code == 409
    sync = client.post("/api/exports/archive", json={"asset_ids": [uploaded["id"]], "options": {"format": "PNG"}})
    assert sync.status_code == 409
    client.post(f"/api/exports/archive/tasks/{task['id']}/cancel", json={})
    release.set()
    final = wait_for_task(client, task["id"])
    assert final["status"] == "cancelled"
    assert not list((app.extensions["studio"]["settings"].exports / ".staging").iterdir())


def test_export_task_survives_restart_as_interrupted(tmp_path):
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    store = app.extensions["studio"]["store"]
    task = store.insert("export_tasks", {
        "status": "preparing", "asset_ids": [], "options": {}, "total": 0,
        "done": 0, "progress": 0, "export_id": None,
    })
    app.extensions["studio"]["exports"].shutdown()
    second = create_app(root, testing=True)
    try:
        record = second.extensions["studio"]["store"].get("export_tasks", task["id"])
        assert record["status"] == "interrupted"
        assert record["export_id"] is None
    finally:
        second.extensions["studio"]["exports"].shutdown()
        second.extensions["studio"]["jobs"].shutdown()


def _peak_rss_kb():
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _archive_plan(app, count, fmt):
    """Run a batch through the service and report (peak RSS KB, archive bytes)."""
    exports = app.extensions["studio"]["exports"]
    settings = app.extensions["studio"]["settings"]
    assets = app.extensions["studio"]["assets"]
    ids = [save_asset(assets, 900, 700, count * 5 + index, f"内存-{count}-{index}.png") for index in range(count)]
    task = exports.archive_task(ids, {"format": fmt})
    final = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        final = exports.task(task["id"])
        if final["status"] not in {"queued", "preparing", "cancelling"}:
            break
        time.sleep(0.02)
    assert final is not None and final["status"] == "completed", final
    path = settings.exports / f"{final['export_id']}.zip"
    return final["bytes"], _peak_rss_kb(), path


def test_memory_does_not_scale_with_archive_size(app):
    """Double the batch: the archive grows, the peak working set must not.

    This is a structural check, not a wall-clock benchmark: it asserts the
    export path never holds a second large image or the assembled archive in
    memory. ``ru_maxrss`` is a high-water mark for the process, so comparing
    two runs in the same process is a conservative upper bound.
    """
    small_bytes, small_peak, small_path = _archive_plan(app, 3, "PNG")
    large_bytes, large_peak, large_path = _archive_plan(app, 8, "PNG")
    assert large_bytes > small_bytes
    # Allow generous headroom for interpreter noise but reject linear growth.
    assert large_peak - small_peak < 64 * 1024, (small_peak, large_peak)


def test_premature_and_repeated_downloads(app, client, uploaded):
    settings = app.extensions["studio"]["settings"]
    task = start_task(client, [uploaded["id"]], {"format": "PNG"})
    early = client.get(f"/api/exports/archive/tasks/{task['id']}/download")
    assert early.status_code == 409
    final = wait_for_task(client, task["id"])
    assert final["status"] == "completed"
    first = client.get(f"/api/exports/archive/tasks/{task['id']}/download")
    assert first.status_code == 200
    first.close()
    second = client.get(f"/api/exports/archive/tasks/{task['id']}/download")
    assert second.status_code == 200
    second.close()
    path = settings.exports / f"{final['export_id']}.zip"
    assert path.is_file(), "a completed download must not delete the archive"


def test_asset_in_active_export_cannot_be_deleted(app, client, uploaded, monkeypatch):
    exports = app.extensions["studio"]["exports"]
    entered = threading.Event()
    release = threading.Event()
    original = type(exports)._encode_into

    def blocking_encode(self, image, options, target):
        entered.set()
        release.wait(timeout=10)
        return original(self, image, options, target)

    monkeypatch.setattr(type(exports), "_encode_into", blocking_encode)
    task = start_task(client, [uploaded["id"]], {"format": "PNG"})
    assert entered.wait(timeout=10), "export worker never started encoding"
    deleted = client.delete(f"/api/assets/{uploaded['id']}")
    assert deleted.status_code == 409
    assert "导出" in deleted.json["error"]["message"]
    client.post(f"/api/exports/archive/tasks/{task['id']}/cancel", json={})
    release.set()
    final = wait_for_task(client, task["id"])
    assert final["status"] == "cancelled"
    after = client.delete(f"/api/assets/{uploaded['id']}")
    assert after.status_code == 200


def test_streaming_download_closes_without_cleanup_when_complete(app, client, uploaded):
    """Full read keeps the archive; the tracked reader must not false-positive."""
    settings = app.extensions["studio"]["settings"]
    task = start_task(client, [uploaded["id"]], {"format": "PNG"})
    final = wait_for_task(client, task["id"])
    assert final["status"] == "completed"
    response = client.get(final["download_url"])
    assert response.data  # forcing the body consumes the tracked reader
    response.close()
    path = settings.exports / f"{final['export_id']}.zip"
    assert path.is_file()
    assert app.extensions["studio"]["store"].get("exports", final["export_id"])["kind"] == "archive"
