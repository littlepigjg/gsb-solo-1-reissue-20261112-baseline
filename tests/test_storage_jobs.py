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
