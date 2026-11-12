import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from backend.storage import JsonStore
from backend.app import create_app


def test_atomic_concurrent_writes(tmp_path):
    store = JsonStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: store.insert("events", {"index": index}), range(40)))
    records = store.all("events")
    assert len(records) == 40
    assert {item["index"] for item in records} == set(range(40))
    assert not list(tmp_path.glob("*.tmp"))


def test_recovers_last_valid_backup(tmp_path):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "first"})
    store.insert("events", {"message": "second"})
    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")
    recovered = JsonStore(tmp_path)
    assert recovered.get("events", first["id"])["message"] == "first"
    assert len(recovered.all("events")) == 1


def test_restart_marks_job_interrupted(tmp_path):
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    store = app.extensions["studio"]["store"]
    job = store.insert("jobs", {"status": "running", "asset_ids": [], "results": []})
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_cancel_waiting_job(app, client, uploaded):
    processor = app.extensions["studio"]["processing"]
    processor.compute_lock.acquire()
    try:
        response = client.post("/api/jobs", json={"asset_ids": [uploaded["id"]], "steps": [{"operation": "color_invert", "params": {}}]})
        job_id = response.json["data"]["id"]
        cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={})
        assert cancelled.status_code == 200
    finally:
        processor.compute_lock.release()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert job["status"] == "cancelled"
    assert job["results"] == []


def test_source_count_over_5000():
    from scripts.count_backend import count_file, ROOT
    records = [count_file(path) for path in (ROOT / "backend").rglob("*.py")]
    assert sum(item["code_lines_excluding_docstrings"] for item in records) > 5000


# ---------------------------------------------------------------- recovery chain
def _job_client(tmp_path):
    app = create_app(tmp_path / "data", testing=True)
    return app, app.test_client()


def _upload(client, name="原图.png", color="red", size=(64, 48)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    buffer.seek(0)
    return client.post("/api/assets", data={"file": (buffer, name)}).json["data"]


def _wait(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.02)
    return job


def _linked_results(store, job_id):
    return [
        asset for asset in store.all("assets")
        if (asset.get("metadata", {}).get("job") or {}).get("id") == job_id
    ]


def _drop_job_results(app, job_id):
    """Remove the files and records an earlier run saved, to mimic a lost write."""
    store = app.extensions["studio"]["store"]
    settings = app.extensions["studio"]["settings"]
    for asset in _linked_results(store, job_id):
        store.delete("assets", asset["id"])
        (settings.media / f"{asset['id']}.png").unlink(missing_ok=True)
        (settings.media / f"{asset['id']}.thumb.png").unlink(missing_ok=True)


def test_restart_adopts_result_saved_before_crash(tmp_path):
    """The asset is saved, but progress was never updated before the exit."""
    app, client = _job_client(tmp_path)
    asset = _upload(client)
    steps = [{"operation": "color_invert", "params": {}}]
    job = client.post("/api/jobs", json={"asset_ids": [asset["id"]], "steps": steps}).json["data"]
    _wait(client, job["id"])
    store = app.extensions["studio"]["store"]
    # Simulate: asset committed, JSON job state still says nothing happened.
    record = store.get("jobs", job["id"])
    store.update("jobs", job["id"], {
        "status": "running", "items": [
            {"source_id": asset["id"], "index": 0, "status": "running", "result_id": None,
             "result_name": None, "error": None, "attempts": 1}
        ], "results": [], "errors": [], "done": 0, "progress": 0, "finished_at": None,
    })
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(tmp_path / "data", testing=True)
    try:
        recovered = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert recovered["status"] == "completed"
        assert [item["status"] for item in recovered["items"]] == ["success"]
        assert recovered["results"][0]["asset_id"] == record["results"][0]["asset_id"]
        assert len(_linked_results(second.extensions["studio"]["store"], job["id"])) == 1
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_restart_marks_unsaved_item_pending_and_resumes_without_duplicates(tmp_path):
    """Job state was written, output never was: item must be pending and resumable."""
    app, client = _job_client(tmp_path)
    first = _upload(client, "甲.png", "blue")
    second = _upload(client, "乙.png", "green")
    steps = [{"operation": "color_grayscale", "params": {}}]
    job = client.post("/api/jobs", json={"asset_ids": [first["id"], second["id"]], "steps": steps}).json["data"]
    _wait(client, job["id"])
    _drop_job_results(app, job["id"])
    store = app.extensions["studio"]["store"]
    store.update("jobs", job["id"], {
        "status": "running", "finished_at": None, "progress": 0, "done": 0,
        "results": [], "errors": [], "items": [
            {"source_id": first["id"], "index": 0, "status": "pending", "result_id": None,
             "result_name": None, "error": None, "attempts": 0},
            {"source_id": second["id"], "index": 1, "status": "pending", "result_id": None,
             "result_name": None, "error": None, "attempts": 0},
        ],
    })
    app.extensions["studio"]["jobs"].shutdown()
    second_app = create_app(tmp_path / "data", testing=True)
    try:
        client2 = second_app.test_client()
        recovered = client2.get(f"/api/jobs/{job['id']}").json["data"]
        assert recovered["status"] == "interrupted"
        assert recovered["pending"] == 2 and recovered["resumable"] is True
        before = len(_linked_results(second_app.extensions["studio"]["store"], job["id"]))
        resumed = client2.post(f"/api/jobs/{job['id']}/resume", json={})
        assert resumed.status_code == 202
        final = _wait(client2, job["id"])
        assert final["status"] == "completed"
        assert final["succeeded"] == 2
        after = len(_linked_results(second_app.extensions["studio"]["store"], job["id"]))
        assert after == 2  # exactly one output per source, no duplicate republication
    finally:
        second_app.extensions["studio"]["jobs"].shutdown()


def test_repeated_resume_after_completion_is_idempotent(tmp_path):
    app, client = _job_client(tmp_path)
    asset = _upload(client)
    job = client.post("/api/jobs", json={"asset_ids": [asset["id"]], "steps": [{"operation": "color_invert", "params": {}}]}).json["data"]
    final = _wait(client, job["id"])
    assert final["status"] == "completed"
    assert client.post(f"/api/jobs/{job['id']}/resume", json={}).status_code == 202
    assert len(_linked_results(app.extensions["studio"]["store"], job["id"])) == 1


def test_cancelled_job_is_not_revived(tmp_path):
    app, client = _job_client(tmp_path)
    asset = _upload(client)
    steps = [{"operation": "color_invert", "params": {}}]
    job = client.post("/api/jobs", json={"asset_ids": [asset["id"]], "steps": steps}).json["data"]
    _wait(client, job["id"])
    store = app.extensions["studio"]["store"]
    # A cancellation was requested but the process exited before it took effect.
    store.update("jobs", job["id"], {"status": "cancelling", "finished_at": None})
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(tmp_path / "data", testing=True)
    try:
        restored = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert restored["status"] == "cancelled"
        assert restored["resumable"] is False
        assert second.test_client().post(f"/api/jobs/{job['id']}/resume", json={}).status_code == 409
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_legacy_job_without_items_is_explicitly_unverified(tmp_path):
    app = create_app(tmp_path / "data", testing=True)
    store = app.extensions["studio"]["store"]
    job = store.insert("jobs", {"status": "running", "asset_ids": [], "results": [], "total": 3,
                                "done": 2, "progress": 66.7, "errors": [], "steps": []})
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(tmp_path / "data", testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
        assert record["recovery_state"] == "unverified"
        assert record["resumable"] is False
        assert "无法确认" in record["message"]
        response = second.test_client().post(f"/api/jobs/{job['id']}/resume", json={})
        assert response.status_code == 409
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_recovery_does_not_claim_other_job_result(tmp_path):
    app, client = _job_client(tmp_path)
    asset = _upload(client)
    steps = [{"operation": "color_invert", "params": {}}]
    job_a = _wait(client, client.post("/api/jobs", json={"asset_ids": [asset["id"]], "steps": steps}).json["data"]["id"])
    job_b = _wait(client, client.post("/api/jobs", json={"asset_ids": [asset["id"]], "steps": steps}).json["data"]["id"])
    store = app.extensions["studio"]["store"]
    # Both jobs produced a result from the same source with identical steps and names.
    store.update("jobs", job_b["id"], {
        "status": "running", "finished_at": None, "results": [], "done": 0, "items": [
            {"source_id": asset["id"], "index": 0, "status": "running", "result_id": None,
             "result_name": None, "error": None, "attempts": 1}
        ],
    })
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(tmp_path / "data", testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job_b["id"])
        linked = [
            item for item in record["items"] if item["status"] == "success"
        ]
        # Only job B's own linkage may be adopted; job A's output is untouched.
        own = {a["id"] for a in _linked_results(second.extensions["studio"]["store"], job_b["id"])}
        assert len(record["results"]) == 1
        assert record["results"][0]["asset_id"] in own
        assert record["results"][0]["asset_id"] != job_a["results"][0]["asset_id"]
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_restart_while_progress_update_was_lost_keeps_one_output(tmp_path):
    """Result saved and progress advanced, then the job JSON write is lost."""
    app, client = _job_client(tmp_path)
    asset = _upload(client)
    steps = [{"operation": "color_invert", "params": {}}]
    job = _wait(client, client.post("/api/jobs", json={"asset_ids": [asset["id"], asset["id"]], "steps": steps}).json["data"]["id"])
    # Drop all per-item knowledge, keeping only the old-style partial results list.
    store = app.extensions["studio"]["store"]
    linked = _linked_results(store, job["id"])
    store.update("jobs", job["id"], {
        "status": "running", "items": [], "results": [], "errors": [], "done": 0,
        "progress": 0, "finished_at": None,
    })
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(tmp_path / "data", testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["recovery_state"] == "unverified"
        assert record["resumable"] is False
        # Even repeated startup recovery leaves existing outputs untouched.
        second.extensions["studio"]["jobs"].shutdown()
        third = create_app(tmp_path / "data", testing=True)
        try:
            again = third.extensions["studio"]["store"].get("jobs", job["id"])
            assert again["recovery_state"] == "unverified"
            assert len(_linked_results(third.extensions["studio"]["store"], job["id"])) == len(linked)
        finally:
            third.extensions["studio"]["jobs"].shutdown()
    finally:
        try:
            second.extensions["studio"]["jobs"].shutdown()
        except Exception:
            pass


def test_resume_twice_saves_no_duplicate_output(tmp_path):
    app, client = _job_client(tmp_path)
    first = _upload(client, "甲.png", "blue")
    second = _upload(client, "乙.png", "green")
    steps = [{"operation": "color_grayscale", "params": {}}]
    job = _wait(client, client.post("/api/jobs", json={"asset_ids": [first["id"], second["id"]], "steps": steps}).json["data"]["id"])
    _drop_job_results(app, job["id"])
    store = app.extensions["studio"]["store"]
    store.update("jobs", job["id"], {
        "status": "interrupted", "finished_at": None, "results": [], "errors": [], "done": 0,
        "progress": 0, "resumable": True, "items": [
            {"source_id": first["id"], "index": 0, "status": "pending", "result_id": None,
             "result_name": None, "error": None, "attempts": 1},
            {"source_id": second["id"], "index": 1, "status": "pending", "result_id": None,
             "result_name": None, "error": None, "attempts": 1},
        ],
    })
    for _ in range(3):
        response = client.post(f"/api/jobs/{job['id']}/resume", json={})
        assert response.status_code == 202
        final = _wait(client, job["id"])
        assert final["status"] == "completed"
    assert len(_linked_results(store, job["id"])) == 2


def test_old_interrupted_record_adopts_saved_results_and_flags_rest(tmp_path):
    """A record written by the previous version: results saved, page said not done."""
    app, client = _job_client(tmp_path)
    first = _upload(client, "旧一.png", "blue")
    second = _upload(client, "旧二.png", "green")
    steps = [{"operation": "color_invert", "params": {}}]
    done = _wait(client, client.post("/api/jobs", json={"asset_ids": [first["id"]], "steps": steps}).json["data"]["id"])
    result_asset = done["results"][0]["asset_id"]
    store = app.extensions["studio"]["store"]
    # Old shape: no items, only results; the second image was never processed.
    job = store.insert("jobs", {
        "title": "旧批次", "asset_ids": [first["id"], second["id"]], "steps": steps,
        "status": "interrupted", "total": 2, "done": 1, "progress": 50,
        "results": [{"source_id": first["id"], "asset_id": result_asset, "name": "旧一"}],
        "errors": [], "message": "服务重启中断了任务。",
    })
    app.extensions["studio"]["jobs"].shutdown()
    second_app = create_app(tmp_path / "data", testing=True)
    try:
        record = second_app.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["recovery_state"] == "unverified"
        assert record["succeeded"] == 1
        assert [i["status"] for i in record["items"]] == ["success", "unknown"]
        assert record["results"][0]["asset_id"] == result_asset
        assert record["done"] == 1
        assert record["resumable"] is False
        assert "无法确认" in record["message"]
        assert len(_linked_results(second_app.extensions["studio"]["store"], job["id"])) == 0
    finally:
        second_app.extensions["studio"]["jobs"].shutdown()


def test_cancelled_job_reports_saved_output_but_stays_cancelled(tmp_path):
    """Cancel lands after one output is committed; recovery must not republish or revive."""
    app, client = _job_client(tmp_path)
    first = _upload(client, "甲.png", "blue")
    second = _upload(client, "乙.png", "green")
    steps = [{"operation": "color_grayscale", "params": {}}]
    job = _wait(client, client.post("/api/jobs", json={"asset_ids": [first["id"], second["id"]], "steps": steps}).json["data"]["id"])
    store = app.extensions["studio"]["store"]
    linked = _linked_results(store, job["id"])
    first_result = [a for a in linked if a["parent_id"] == first["id"]][0]
    # Only the first output exists; the second failed to save before cancellation.
    for asset in linked:
        if asset["parent_id"] == second["id"]:
            settings = app.extensions["studio"]["settings"]
            store.delete("assets", asset["id"])
            (settings.media / f"{asset['id']}.png").unlink(missing_ok=True)
            (settings.media / f"{asset['id']}.thumb.png").unlink(missing_ok=True)
    store.update("jobs", job["id"], {
        "status": "cancelling", "finished_at": None, "resumable": False,
        "items": [
            {"source_id": first["id"], "index": 0, "status": "running", "result_id": None,
             "result_name": None, "error": None, "attempts": 1},
            {"source_id": second["id"], "index": 1, "status": "pending", "result_id": None,
             "result_name": None, "error": None, "attempts": 0},
        ],
    })
    app.extensions["studio"]["jobs"].shutdown()
    second_app = create_app(tmp_path / "data", testing=True)
    try:
        record = second_app.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "cancelled"
        assert record["recovery_state"] == "cancelled"
        assert record["resumable"] is False
        assert [item["status"] for item in record["items"]] == ["success", "pending"]
        assert record["results"][0]["asset_id"] == first_result["id"]
        assert len(_linked_results(second_app.extensions["studio"]["store"], job["id"])) == 1
        client2 = second_app.test_client()
        assert client2.post(f"/api/jobs/{job['id']}/resume", json={}).status_code == 409
        # No new output appears after a refused resume attempt.
        assert len(_linked_results(second_app.extensions["studio"]["store"], job["id"])) == 1
    finally:
        second_app.extensions["studio"]["jobs"].shutdown()


def test_progress_ahead_of_output_is_not_reported_complete(tmp_path):
    """Progress showed 100%, but the output was never saved: item must be pending."""
    app, client = _job_client(tmp_path)
    first = _upload(client, "甲.png", "blue")
    steps = [{"operation": "color_invert", "params": {}}]
    job = _wait(client, client.post("/api/jobs", json={"asset_ids": [first["id"]], "steps": steps}).json["data"]["id"])
    _drop_job_results(app, job["id"])
    store = app.extensions["studio"]["store"]
    store.update("jobs", job["id"], {
        "status": "running", "finished_at": None, "progress": 100, "done": 1, "results": [],
        "items": [
            {"source_id": first["id"], "index": 0, "status": "running", "result_id": None,
             "result_name": None, "error": None, "attempts": 1},
        ],
    })
    app.extensions["studio"]["jobs"].shutdown()
    second_app = create_app(tmp_path / "data", testing=True)
    try:
        record = second_app.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
        assert record["pending"] == 1
        assert record["results"] == []
        assert record["done"] == 0
        assert record["resumable"] is True
    finally:
        second_app.extensions["studio"]["jobs"].shutdown()


def test_duplicate_candidates_never_get_republished(tmp_path):
    """Two provable outputs for one source: mark unknown, resume must not add a third."""
    app, client = _job_client(tmp_path)
    asset = _upload(client)
    steps = [{"operation": "color_invert", "params": {}}]
    job = _wait(client, client.post("/api/jobs", json={"asset_ids": [asset["id"]], "steps": steps}).json["data"]["id"])
    store = app.extensions["studio"]["store"]
    original = _linked_results(store, job["id"])[0]
    # Simulate a second legitimate output for the same batch/source (e.g. a retry
    # that committed but whose job update was lost).
    clone_meta = json.loads(json.dumps(original["metadata"]))
    second_asset = store.insert("assets", {**{k: v for k, v in original.items() if k not in {"id", "created_at", "updated_at"}},
                                            "id": "b" * 32, "metadata": clone_meta})
    settings = app.extensions["studio"]["settings"]
    (settings.media / f"{second_asset['id']}.png").write_bytes((settings.media / f"{original['id']}.png").read_bytes())
    store.update("jobs", job["id"], {
        "status": "running", "finished_at": None, "results": [], "done": 0, "progress": 0,
        "items": [{"source_id": asset["id"], "index": 0, "status": "running", "result_id": None,
                   "result_name": None, "error": None, "attempts": 1}],
    })
    app.extensions["studio"]["jobs"].shutdown()
    second_app = create_app(tmp_path / "data", testing=True)
    try:
        record = second_app.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
        assert record["items"][0]["status"] == "unknown"
        assert record["recovery_state"] == "conflict"
        assert record["resumable"] is False
        assert record["results"] == []
        assert len(_linked_results(second_app.extensions["studio"]["store"], job["id"])) == 2
        client2 = second_app.test_client()
        assert client2.post(f"/api/jobs/{job['id']}/resume", json={}).status_code == 409
        assert len(_linked_results(second_app.extensions["studio"]["store"], job["id"])) == 2
        # A second restart must not re-claim either candidate.
        second_app.extensions["studio"]["jobs"].shutdown()
        third = create_app(tmp_path / "data", testing=True)
        try:
            again = third.extensions["studio"]["store"].get("jobs", job["id"])
            assert again["items"][0]["status"] == "unknown"
            assert len(_linked_results(third.extensions["studio"]["store"], job["id"])) == 2
        finally:
            third.extensions["studio"]["jobs"].shutdown()
    finally:
        try: second_app.extensions["studio"]["jobs"].shutdown()
        except Exception: pass
