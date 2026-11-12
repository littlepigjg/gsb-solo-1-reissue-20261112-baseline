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
    store.close()
    assert not list(tmp_path.glob("*.tmp"))


def test_confirmed_saves_survive_snapshot_corruption(tmp_path):
    store = JsonStore(tmp_path)
    store.insert("events", {"message": "first"})
    store.insert("events", {"message": "second"})
    store.close()
    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")
    recovered = JsonStore(tmp_path)
    try:
        assert {item["message"] for item in recovered.all("events")} == {"first", "second"}
    finally:
        recovered.close()


def test_recovers_last_valid_backup(tmp_path):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "first"})
    store.checkpoint("events")
    store.insert("events", {"message": "second"})
    store.checkpoint("events")
    store.close()
    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")
    (tmp_path / "events.journal").write_text("broken journal", encoding="utf-8")
    recovered = JsonStore(tmp_path)
    try:
        assert recovered.get("events", first["id"])["message"] == "first"
        assert len(recovered.all("events")) == 1
    finally:
        recovered.close()


def test_confirmed_changes_survive_restart_without_clean_shutdown(tmp_path):
    store = JsonStore(tmp_path)
    kept = store.insert("assets", {"name": "保留", "favorite": False})
    dropped = store.insert("assets", {"name": "删除"})
    store.update("assets", kept["id"], {"favorite": True, "tags": ["作品"]})
    store.delete("assets", dropped["id"])
    # No close(): a sudden exit only has the fsynced journal to rely on.
    recovered = JsonStore(tmp_path)
    try:
        record = recovered.get("assets", kept["id"])
        assert record["favorite"] is True
        assert record["tags"] == ["作品"]
        assert len(recovered.all("assets")) == 1
    finally:
        recovered.close()


def test_interleaved_field_updates_do_not_clobber(tmp_path):
    store = JsonStore(tmp_path)
    record = store.insert("assets", {"name": "原图", "favorite": False, "tags": []})
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for index in range(20):
            futures.append(pool.submit(store.update, "assets", record["id"], {"name": f"名称{index}"}))
            futures.append(pool.submit(store.update, "assets", record["id"], {"tags": [f"标签{index}"]}))
        for future in futures:
            future.result()
    final = store.get("assets", record["id"])
    assert final["name"].startswith("名称")
    assert len(final["tags"]) == 1 and final["tags"][0].startswith("标签")
    store.close()
    recovered = JsonStore(tmp_path)
    try:
        assert recovered.get("assets", record["id"]) == final
    finally:
        recovered.close()


def test_existing_json_files_load_without_journal(tmp_path):
    legacy = {"id": "abc123", "name": "旧资料", "created_at": "2024-01-01T00:00:00.000+00:00", "updated_at": "2024-01-01T00:00:00.000+00:00"}
    (tmp_path / "assets.json").write_text(
        json.dumps({"schema_version": 1, "records": {"abc123": legacy}}, ensure_ascii=False),
        encoding="utf-8",
    )
    store = JsonStore(tmp_path)
    assert store.get("assets", "abc123")["name"] == "旧资料"
    store.update("assets", "abc123", {"name": "新名字"})
    store.close()
    recovered = JsonStore(tmp_path)
    try:
        assert recovered.get("assets", "abc123")["name"] == "新名字"
    finally:
        recovered.close()


def test_checkpoint_compacts_journal_and_keeps_state(tmp_path):
    store = JsonStore(tmp_path, journal_limit=1024)
    for index in range(30):
        store.insert("assets", {"name": f"图{index}", "padding": "x" * 200})
    store.checkpoint()
    journal_size = (tmp_path / "assets.journal").stat().st_size
    snapshot_size = (tmp_path / "assets.json").stat().st_size
    assert journal_size < 1024 < snapshot_size
    assert (tmp_path / "assets.json.bak").exists()
    metrics = store.metrics()
    assert metrics["checkpoints"] >= 1
    assert metrics["checkpoint_bytes_written"] >= snapshot_size
    assert metrics["journal_bytes_pending"] < 5 * 1024
    store.close()
    recovered = JsonStore(tmp_path)
    try:
        assert len(recovered.all("assets")) == 30
    finally:
        recovered.close()
    assert not list(tmp_path.glob("*.tmp"))


def test_background_checkpoint_triggers_automatically(tmp_path):
    store = JsonStore(tmp_path, journal_limit=2048)
    for index in range(40):
        store.insert("events", {"message": "事件" + "x" * 100, "index": index})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if store.metrics()["checkpoints"] >= 1:
            break
        time.sleep(0.02)
    metrics = store.metrics()
    assert metrics["checkpoints"] >= 1
    assert metrics["journal_bytes_pending"] < metrics["journal_bytes_written"]
    store.close()
    assert not list(tmp_path.glob("*.tmp"))
    assert sorted(path.name for path in tmp_path.glob("events.*")) == ["events.journal", "events.json", "events.json.bak"]
    recovered = JsonStore(tmp_path)
    try:
        assert {item["index"] for item in recovered.all("events")} == set(range(40))
    finally:
        recovered.close()


def test_torn_journal_tail_is_truncated(tmp_path):
    store = JsonStore(tmp_path)
    store.insert("events", {"message": "完整"})
    store.close()
    with (tmp_path / "events.journal").open("ab") as stream:
        stream.write(b'{"seq": 2, "op": "upsert", "id": "unfinished"')
    recovered = JsonStore(tmp_path)
    try:
        assert len(recovered.all("events")) == 1
        assert recovered.metrics()["torn_tails_truncated"] == 1
    finally:
        recovered.close()


def test_metrics_track_save_and_checkpoint_costs(tmp_path):
    store = JsonStore(tmp_path)
    record = store.insert("events", {"message": "一"})
    store.update("events", record["id"], {"message": "二"})
    metrics = store.metrics()
    assert metrics["saves"] == 2
    assert metrics["journal_bytes_written"] > 0
    assert metrics["save_wait_ms"] >= 0
    assert metrics["journal_bytes_pending"] > 0
    store.checkpoint("events")
    compacted = store.metrics()
    assert compacted["checkpoints"] == 1
    assert compacted["journal_bytes_pending"] < metrics["journal_bytes_written"]
    store.close()


def test_restart_marks_job_interrupted(tmp_path):
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    store = app.extensions["studio"]["store"]
    job = store.insert("jobs", {"status": "running", "asset_ids": [], "results": []})
    app.extensions["studio"]["jobs"].shutdown()
    store.close()
    second = create_app(root, testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
    finally:
        second.extensions["studio"]["jobs"].shutdown()
        second.extensions["studio"]["store"].close()


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
