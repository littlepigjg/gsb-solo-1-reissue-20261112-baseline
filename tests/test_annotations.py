"""Annotation anchoring: frame math, version snapshots, undo and export."""
import io
import math
import numpy as np
import pytest
from PIL import Image, ImageDraw
from backend import frames as frame_math
from backend.geometry_frames import frame_for_step
from backend.annotation_render import render_annotations


def make_asset(client, width=120, height=90):
    image = Image.new("RGBA", (width, height), (200, 210, 190, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return client.post("/api/assets", data={"file": (buffer, "画布.png")}).json["data"]


def process(client, asset_id, steps):
    response = client.post("/api/process", json={"asset_id": asset_id, "steps": steps})
    assert response.status_code == 201, response.json
    return response.json["data"]


def add_rect(client, asset_id, x, y, w, h, label="问题区域"):
    response = client.post(f"/api/assets/{asset_id}/annotations", json={
        "kind": "rect", "label": label, "color": "#d9482b",
        "geometry": {"x": x, "y": y, "width": w, "height": h},
    })
    assert response.status_code == 201, response.json
    return response.json["data"]


# --------------------------------------------------------------------- frames
def test_identity_frame_for_original(client):
    asset = make_asset(client)
    doc = client.get(f"/api/assets/{asset['id']}/annotations").json["data"]
    assert doc["frame"]["matrix"] == [list(row) for row in frame_math.IDENTITY]


def test_crop_frame_moves_marker_with_content(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 40, 30, 20, 20)
    cropped = process(client, asset["id"], [
        {"operation": "crop_rect", "params": {"left": 30, "top": 20, "width": 60, "height": 50}}])
    view = client.get(f"/api/assets/{cropped['id']}/annotations/view").json["data"]["markers"]
    assert len(view) == 1
    geometry = view[0]["geometry"]
    assert geometry["x"] == pytest.approx(10, abs=0.01)
    assert geometry["y"] == pytest.approx(10, abs=0.01)
    assert geometry["width"] == pytest.approx(20, abs=0.01)
    assert view[0]["visibility"] == "inside"


def test_cropped_out_marker_is_distinguishable(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 90, 60, 25, 25)
    cropped = process(client, asset["id"], [
        {"operation": "crop_rect", "params": {"left": 0, "top": 0, "width": 40, "height": 40}}])
    view = client.get(f"/api/assets/{cropped['id']}/annotations/view").json["data"]["markers"]
    assert view[0]["visibility"] == "outside"


def test_partial_crop_visibility(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 30, 30, 40, 40)
    cropped = process(client, asset["id"], [
        {"operation": "crop_rect", "params": {"left": 0, "top": 0, "width": 50, "height": 50}}])
    view = client.get(f"/api/assets/{cropped['id']}/annotations/view").json["data"]["markers"]
    assert view[0]["visibility"] == "partial"


def test_resize_frame_scales_marker(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 20, 10, 40, 30)
    resized = process(client, asset["id"], [
        {"operation": "resize_exact", "params": {"width": 60, "height": 45, "resampling": "lanczos"}}])
    view = client.get(f"/api/assets/{resized['id']}/annotations/view").json["data"]["markers"][0]
    assert view["geometry"]["x"] == pytest.approx(10, abs=0.01)
    assert view["geometry"]["width"] == pytest.approx(20, abs=0.01)
    assert view["geometry"]["height"] == pytest.approx(15, abs=0.01)


def test_canvas_extension_keeps_content_position(client):
    asset = make_asset(client, 100, 80)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    expanded = process(client, asset["id"], [
        {"operation": "resize_canvas", "params": {
            "width": 140, "height": 120, "transparent": True, "background": "#ffffff"}}])
    view = client.get(f"/api/assets/{expanded['id']}/annotations/view").json["data"]["markers"][0]
    assert view["geometry"]["x"] == pytest.approx(30, abs=0.01)
    assert view["geometry"]["y"] == pytest.approx(30, abs=0.01)


def test_quarter_rotation_marker_round_trip(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 10, 20, 30, 15)
    rotated = process(client, asset["id"], [
        {"operation": "transform_quarter", "params": {"angle": "90"}}])
    view = client.get(f"/api/assets/{rotated['id']}/annotations/view").json["data"]["markers"][0]
    # Source TL (10,20) under CCW 90 -> (20, 120-1-10) = (20,109)
    assert view["geometry"]["corners"][0] == pytest.approx((20, 109), abs=0.5)


def test_flip_mirror_mapping(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    mirrored = process(client, asset["id"], [
        {"operation": "transform_flip", "params": {"axis": "horizontal"}}])
    view = client.get(f"/api/assets/{mirrored['id']}/annotations/view").json["data"]["markers"][0]
    assert view["geometry"]["corners"][0][0] == pytest.approx(119 - 10, abs=0.01)


def test_perspective_frame_tracks_empirical_pixels(client):
    width, height = 120, 90
    image = Image.new("RGBA", (width, height), (200, 210, 190, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 26, 26), fill=(255, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "透视.png")}).json["data"]
    add_rect(client, asset["id"], 20, 20, 6, 6)
    warped = process(client, asset["id"], [
        {"operation": "transform_perspective", "params": {"amount": 25}}])
    # Where the red block actually lands in the warped pixels.
    warped_pixels = np.array(Image.open(io.BytesIO(client.get(warped["url"]).data)).convert("RGBA"))
    ys, xs = np.where(np.all(warped_pixels == (255, 0, 0, 255), axis=-1))
    actual = (float(xs.mean()), float(ys.mean()))
    view = client.get(f"/api/assets/{warped['id']}/annotations/view").json["data"]["markers"][0]
    corners = view["geometry"]["corners"]
    centre = (sum(p[0] for p in corners) / 4, sum(p[1] for p in corners) / 4)
    assert centre == pytest.approx(actual, abs=2.5)


def test_free_rotation_frame_matches_empirical_pixels(client):
    width, height = 80, 60
    image = Image.new("RGBA", (width, height), (200, 210, 190, 255))
    ImageDraw.Draw(image).rectangle((10, 10, 15, 15), fill=(255, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "旋转.png")}).json["data"]
    add_rect(client, asset["id"], 10, 10, 5, 5)
    rotated = process(client, asset["id"], [
        {"operation": "transform_rotate", "params": {
            "angle": 35, "expand": True, "transparent": True, "background": "#ffffff"}}])
    rotated_pixels = np.array(Image.open(io.BytesIO(client.get(rotated["url"]).data)).convert("RGBA"))
    ys, xs = np.where(np.all(rotated_pixels == (255, 0, 0, 255), axis=-1))
    actual = (float(xs.mean()), float(ys.mean()))
    view = client.get(f"/api/assets/{rotated['id']}/annotations/view").json["data"]["markers"][0]
    corners = view["geometry"]["corners"]
    centre = (sum(p[0] for p in corners) / 4, sum(p[1] for p in corners) / 4)
    assert centre == pytest.approx(actual, abs=2.0)


def test_color_operation_keeps_identity_geometry(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    adjusted = process(client, asset["id"], [{"operation": "color_invert", "params": {}}])
    view = client.get(f"/api/assets/{adjusted['id']}/annotations/view").json["data"]["markers"][0]
    assert view["geometry"]["x"] == pytest.approx(10, abs=0.01)


def test_multi_step_composition(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 40, 30, 20, 20)
    result = process(client, asset["id"], [
        {"operation": "crop_rect", "params": {"left": 20, "top": 10, "width": 80, "height": 70}},
        {"operation": "resize_percent", "params": {"percent": 50, "resampling": "lanczos"}},
    ])
    view = client.get(f"/api/assets/{result['id']}/annotations/view").json["data"]["markers"][0]
    # Crop: root (40,30) -> child1 (20,20); 50% resize -> child2 (10,10).
    assert view["geometry"]["x"] == pytest.approx(10, abs=0.01)
    assert view["geometry"]["y"] == pytest.approx(10, abs=0.01)
    assert view["geometry"]["width"] == pytest.approx(10, abs=0.01)


# ------------------------------------------------------------------ branches
def test_branch_annotations_are_independent(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20, "主干批注")
    branch_a = process(client, asset["id"], [{"operation": "color_invert", "params": {}}])
    branch_b = process(client, asset["id"], [{"operation": "color_grayscale", "params": {}}])
    client.post(f"/api/assets/{branch_a['id']}/annotations", json={
        "kind": "note", "label": "分支 A 备注", "geometry": {"anchor": [5, 5]}})
    a_doc = client.get(f"/api/assets/{branch_a['id']}/annotations").json["data"]
    b_doc = client.get(f"/api/assets/{branch_b['id']}/annotations").json["data"]
    root_doc = client.get(f"/api/assets/{asset['id']}/annotations").json["data"]
    assert len(a_doc["markers"]) == 2
    assert len(b_doc["markers"]) == 1
    assert len(root_doc["markers"]) == 1
    assert b_doc["markers"][0]["label"] == "主干批注"


def test_old_version_keeps_its_own_markers(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    child = process(client, asset["id"], [{"operation": "color_invert", "params": {}}])
    client.patch(f"/api/assets/{child['id']}/annotations/{client.get(f'/api/assets/{child['id']}/annotations').json['data']['markers'][0]['id']}",
                 json={"label": "仅新版本修改"})
    old = client.get(f"/api/assets/{asset['id']}/annotations").json["data"]
    assert old["markers"][0]["label"] == "问题区域"


# ------------------------------------------------------------------ undo/redo
def test_undo_redo_lifecycle(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    doc = add_rect(client, asset["id"], 40, 40, 10, 10, "第二条")
    assert len(doc["markers"]) == 2
    undone = client.post(f"/api/assets/{asset['id']}/annotations/undo", json={}).json["data"]
    assert len(undone["markers"]) == 1
    assert undone["undo_available"] is True and undone["redo_available"] is True
    redone = client.post(f"/api/assets/{asset['id']}/annotations/redo", json={}).json["data"]
    assert len(redone["markers"]) == 2


def test_new_mutation_clears_redo(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    add_rect(client, asset["id"], 30, 30, 10, 10, "二")
    client.post(f"/api/assets/{asset['id']}/annotations/undo", json={})
    add_rect(client, asset["id"], 50, 50, 8, 8, "三")
    doc = client.get(f"/api/assets/{asset['id']}/annotations").json["data"]
    assert doc["redo_available"] is False
    assert len(doc["markers"]) == 2


def test_undo_persists_across_reload(app, client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    client.post(f"/api/assets/{asset['id']}/annotations/undo", json={})
    root = app.extensions["studio"]["settings"].root
    app.extensions["studio"]["jobs"].shutdown()
    second = __import__("backend.app", fromlist=["create_app"]).create_app(root, testing=True)
    try:
        doc = second.test_client().get(f"/api/assets/{asset['id']}/annotations").json["data"]
        assert len(doc["markers"]) == 0
        assert doc["redo_available"] is True
    finally:
        second.extensions["studio"]["jobs"].shutdown()


# ----------------------------------------------------------------- validation
def test_invalid_marker_rejected(client):
    asset = make_asset(client)
    response = client.post(f"/api/assets/{asset['id']}/annotations", json={
        "kind": "rect", "geometry": {"x": 0, "y": 0, "width": -5, "height": 10}})
    assert response.status_code == 400
    unknown = client.post(f"/api/assets/{asset['id']}/annotations", json={
        "kind": "rect", "geometry": {"x": 0, "y": 0, "width": 5, "height": 10}, "bogus": 1})
    assert unknown.status_code == 400


def test_marker_kinds(client):
    asset = make_asset(client, 120, 90)
    for kind, geometry in [
        ("ellipse", {"x": 5, "y": 5, "width": 10, "height": 10}),
        ("arrow", {"points": [[0, 0], [20, 20]]}),
        ("freehand", {"points": [[0, 0], [5, 6], [9, 12]]}),
        ("note", {"anchor": [30, 30]}),
    ]:
        response = client.post(f"/api/assets/{asset['id']}/annotations",
                               json={"kind": kind, "label": kind, "geometry": geometry})
        assert response.status_code == 201, (kind, response.json)


# --------------------------------------------------------------------- export
def test_export_with_and_without_annotations(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 20, 20, 40, 30, "导出可见")
    clean = client.post("/api/exports", json={"asset_id": asset["id"], "options": {"format": "PNG"}}).json["data"]
    marked = client.post("/api/exports", json={
        "asset_id": asset["id"], "options": {"format": "PNG", "include_annotations": True}}).json["data"]
    clean_bytes = client.get(clean["url"]).data
    marked_bytes = client.get(marked["url"]).data
    assert clean_bytes != marked_bytes
    with Image.open(io.BytesIO(marked_bytes)) as image:
        assert image.size == (120, 90)
        # The red marker stroke is present in the marked export only.
        pixels = image.convert("RGB").getcolors(maxcolors=1_000_000)
        colors = {color for count, color in pixels}
        assert any(r > 180 and g < 110 and b < 90 for r, g, b in colors)
    with Image.open(io.BytesIO(clean_bytes)) as image:
        colors = {color for count, color in image.convert("RGB").getcolors(maxcolors=1_000_000)}
        assert not any(r > 180 and g < 110 and b < 90 for r, g, b in colors)


def test_render_handles_outside_marker(client):
    asset = make_asset(client, 120, 90)
    add_rect(client, asset["id"], 90, 70, 40, 40)
    cropped = process(client, asset["id"], [
        {"operation": "crop_rect", "params": {"left": 0, "top": 0, "width": 50, "height": 50}}])
    markers = client.get(f"/api/assets/{cropped['id']}/annotations/view").json["data"]["markers"]
    # Renderer must not crash on off-canvas dashed geometry.
    output = render_annotations(Image.new("RGBA", (50, 50), (255, 255, 255, 255)), markers)
    assert output.size == (50, 50)


def test_archive_annotations_manifest(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    import zipfile, json
    record = client.post("/api/exports/archive", json={
        "asset_ids": [asset["id"]],
        "options": {"format": "PNG", "include_annotations": True}}).json["data"]
    content = client.get(record["url"]).data
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["annotations"][0]["markers"] == 1


def test_delete_asset_removes_annotations(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    assert client.delete(f"/api/assets/{asset['id']}").status_code == 200
    response = client.get(f"/api/assets/{asset['id']}/annotations")
    assert response.status_code == 404


def test_batch_job_inherits_annotation_snapshot(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20)
    response = client.post("/api/jobs", json={
        "asset_ids": [asset["id"]],
        "steps": [{"operation": "crop_rect", "params": {"left": 5, "top": 5, "width": 80, "height": 70}}],
    })
    job_id = response.json["data"]["id"]
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "completed"
    result_id = job["results"][0]["asset_id"]
    view = client.get(f"/api/assets/{result_id}/annotations/view").json["data"]["markers"]
    assert len(view) == 1
    # Marker shifted by the crop origin (5,5).
    assert view[0]["geometry"]["x"] == pytest.approx(5, abs=0.01)
    assert view[0]["geometry"]["y"] == pytest.approx(5, abs=0.01)


def test_marker_limit_enforced(client):
    asset = make_asset(client)
    from backend.annotations import MAX_MARKERS
    for index in range(MAX_MARKERS):
        response = client.post(f"/api/assets/{asset['id']}/annotations", json={
            "kind": "note", "label": str(index), "geometry": {"anchor": [index, index]}})
        assert response.status_code == 201
    overflow = client.post(f"/api/assets/{asset['id']}/annotations", json={
        "kind": "note", "label": "overflow", "geometry": {"anchor": [1, 1]}})
    assert overflow.status_code == 400


def test_nonexistent_asset_annotation_404(client):
    missing = "0" * 32
    assert client.get(f"/api/assets/{missing}/annotations").status_code == 404
    assert client.post(f"/api/assets/{missing}/annotations", json={
        "kind": "note", "geometry": {"anchor": [1, 1]}}).status_code == 404


def test_lineage_history_remains_traceable(client):
    asset = make_asset(client)
    add_rect(client, asset["id"], 10, 10, 20, 20, "v1 说明")
    child = process(client, asset["id"], [{"operation": "color_invert", "params": {}}])
    grandchild = process(client, child["id"], [{"operation": "color_grayscale", "params": {}}])
    history = client.get(f"/api/assets/{grandchild['id']}/history").json["data"]
    assert [item["id"] for item in history] == [asset["id"], child["id"], grandchild["id"]]
    # The root's annotation keeps its original wording untouched.
    root_doc = client.get(f"/api/assets/{asset['id']}/annotations").json["data"]
    assert root_doc["markers"][0]["label"] == "v1 说明"
    # Each version carries its own independent annotation document.
    child_doc = client.get(f"/api/assets/{child['id']}/annotations").json["data"]
    grand_doc = client.get(f"/api/assets/{grandchild['id']}/annotations").json["data"]
    assert len(child_doc["markers"]) == len(grand_doc["markers"]) == 1
    assert root_doc["markers"][0]["id"] == child_doc["markers"][0]["id"]
