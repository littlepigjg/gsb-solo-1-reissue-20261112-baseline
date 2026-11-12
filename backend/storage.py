"""JSON repository with journaled saves and background compaction.

The local server owns one process. All HTTP and worker threads share this
repository. Each collection keeps its records in memory; every confirmed
mutation is appended to a per-collection journal and fsynced before the call
returns, so a save acknowledged to the page survives a sudden process exit.
The cost of a save is proportional to the change, not to the library size.

A background checkpoint rewrites the compact `{collection}.json` snapshot
(keeping the previous snapshot as `.json.bak`, as before) and rotates the
journal once it grows past the snapshot size. The checkpoint runs outside the
request path and writes in bounded slices, so sustained use does not
concentrate the saved writes into one long pause. Journal replay on startup
tolerates a torn tail left by a crash and truncates it.

Stored record dicts are never mutated in place: updates replace the dict, so a
checkpoint can serialize a shallow copy taken under the lock.
"""
import copy
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from .errors import MissingRecord, Conflict

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
JOURNAL_LIMIT = 512 * 1024
CHECKPOINT_SLICE = 256 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


class _Collection:
    """In-memory state and journal handle for one collection."""

    def __init__(self, name):
        self.name = name
        self.records = {}
        self.sequence = 0
        self.journal = None
        self.journal_bytes = 0
        self.snapshot_bytes = 0
        self.checkpointing = False


class JsonStore:
    COLLECTIONS = {"assets", "jobs", "presets", "events", "exports"}

    def __init__(self, directory, journal_limit=JOURNAL_LIMIT):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_limit = journal_limit
        self.lock = threading.RLock()
        self._states = {name: _Collection(name) for name in self.COLLECTIONS}
        self._metrics = {
            "started_at": utc_now(),
            "saves": 0,
            "journal_bytes_written": 0,
            "save_wait_ms": 0.0,
            "save_wait_max_ms": 0.0,
            "checkpoints": 0,
            "checkpoint_bytes_written": 0,
            "checkpoint_wait_ms": 0.0,
            "last_checkpoint_ms": 0.0,
            "last_checkpoint_at": None,
            "torn_tails_truncated": 0,
            "temp_files_cleaned": 0,
        }
        self._clean_temp_files()
        for name in sorted(self.COLLECTIONS):
            with self.lock:
                self._load(name)

    # ------------------------------------------------------------------ paths

    def _path(self, collection):
        if collection not in self.COLLECTIONS:
            raise ValueError("Unknown collection")
        return self.directory / f"{collection}.json"

    def _journal_path(self, collection):
        if collection not in self.COLLECTIONS:
            raise ValueError("Unknown collection")
        return self.directory / f"{collection}.journal"

    def _clean_temp_files(self):
        for leftover in self.directory.glob("*.tmp"):
            try:
                leftover.unlink()
                self._metrics["temp_files_cleaned"] += 1
            except OSError:
                LOGGER.warning("无法清理临时文件：%s", leftover)

    # ------------------------------------------------------------------ load

    def _decode(self, path):
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("records"), dict):
            raise ValueError("Unsupported JSON schema")
        data.setdefault("sequence", 0)
        return data

    def _read_snapshot(self, collection):
        """Read the compacted snapshot, recovering the last valid backup."""
        path = self._path(collection)
        try:
            return self._decode(path)
        except (ValueError, OSError):
            backup = path.with_suffix(".json.bak")
            if backup.exists():
                try:
                    data = self._decode(backup)
                except (ValueError, OSError):
                    data = None
                if data is not None:
                    recovery = path.with_suffix(".json.recovery")
                    shutil.copy2(backup, recovery)
                    os.replace(recovery, path)
                    return data
            if not self._journal_path(collection).exists():
                raise RuntimeError(f"JSON 数据损坏且没有可用备份：{path}")
            return None

    def _load(self, collection):
        state = self._states[collection]
        snapshot_path = self._path(collection)
        journal_path = self._journal_path(collection)
        snapshot = self._read_snapshot(collection) if snapshot_path.exists() else None
        healed = snapshot is None
        if healed:
            snapshot = {"schema_version": SCHEMA_VERSION, "sequence": 0, "records": {}}
        state.records = snapshot["records"]
        state.sequence = snapshot["sequence"]
        self._replay(state, strict=healed)
        if healed:
            # Fresh directory or a snapshot rebuilt purely from the journal:
            # write the snapshot once so the files are complete and valid.
            self._write_snapshot_file(collection, state.sequence, state.records)
        state.snapshot_bytes = snapshot_path.stat().st_size
        state.journal = journal_path.open("ab")
        state.journal_bytes = journal_path.stat().st_size

    def _replay(self, state, strict=False):
        path = self._journal_path(state.name)
        if not path.exists():
            return
        raw = path.read_bytes()
        entries = []
        valid_upto = 0
        for line in raw.splitlines(keepends=True):
            stripped = line.strip()
            if stripped:
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    break
                if not isinstance(entry, dict) or not isinstance(entry.get("op"), str):
                    break
                entries.append(entry)
            valid_upto += len(line)
        if valid_upto < len(raw):
            if strict and not entries:
                raise RuntimeError(f"JSON 数据损坏且没有可用备份：{self._path(state.name)}")
            # A crash can leave a partially written tail; only complete lines
            # were ever acknowledged, so the tail is dropped and removed.
            with path.open("r+b") as stream:
                stream.truncate(valid_upto)
            self._metrics["torn_tails_truncated"] += 1
            LOGGER.warning("已截断 %s 末尾 %d 字节的不完整日志", path, len(raw) - valid_upto)
        base = 0
        for entry in entries:
            if entry["op"] == "base":
                base = max(base, entry.get("seq", 0))
        if strict and base > 0:
            # The journal continues a snapshot that no longer exists; without
            # a valid snapshot or backup the covered records cannot be known.
            raise RuntimeError(f"JSON 快照落后于日志且没有可用备份：{self._path(state.name)}")
        for entry in entries:
            op = entry["op"]
            if op == "base":
                continue
            seq = entry.get("seq", 0)
            if seq <= state.sequence:
                continue
            if op == "upsert" and isinstance(entry.get("record"), dict):
                state.records[entry["id"]] = entry["record"]
            elif op == "delete":
                state.records.pop(entry.get("id"), None)
            elif op == "delete_many" and isinstance(entry.get("ids"), list):
                for record_id in entry["ids"]:
                    state.records.pop(record_id, None)
            else:
                continue
            state.sequence = max(state.sequence, seq)

    # ----------------------------------------------------------------- journal

    def _journal(self, state, entry):
        """Append one confirmed mutation and fsync before acknowledging it."""
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        started = time.perf_counter()
        state.journal.write(line)
        state.journal.flush()
        os.fsync(state.journal.fileno())
        waited = (time.perf_counter() - started) * 1000
        state.journal_bytes += len(line)
        metrics = self._metrics
        metrics["saves"] += 1
        metrics["journal_bytes_written"] += len(line)
        metrics["save_wait_ms"] += waited
        metrics["save_wait_max_ms"] = max(metrics["save_wait_max_ms"], waited)
        if state.journal_bytes >= max(self.journal_limit, state.snapshot_bytes) and not state.checkpointing:
            state.checkpointing = True
            threading.Thread(
                target=self._checkpoint_guarded,
                args=(state,),
                daemon=True,
                name=f"studio-checkpoint-{state.name}",
            ).start()

    # -------------------------------------------------------------- checkpoint

    def _write_snapshot_file(self, name, sequence, records):
        """Write the compacted snapshot in bounded slices, keeping `.bak`."""
        path = self._path(name)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "records": records,
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        temporary = path.with_suffix(f".{new_id()}.tmp")
        try:
            with temporary.open("wb") as stream:
                for offset in range(0, len(data), CHECKPOINT_SLICE):
                    stream.write(data[offset:offset + CHECKPOINT_SLICE])
                    stream.flush()
                    time.sleep(0)
                os.fsync(stream.fileno())
            if path.exists():
                shutil.copy2(path, path.with_suffix(".json.bak"))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return len(data)

    def _rotate_journal(self, state, sequence):
        """Drop journaled mutations now covered by the snapshot."""
        journal_path = self._journal_path(state.name)
        state.journal.close()
        kept = [json.dumps({"op": "base", "seq": sequence}, separators=(",", ":")).encode("utf-8") + b"\n"]
        for line in journal_path.read_bytes().splitlines(keepends=True):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except ValueError:
                continue
            if entry.get("op") in {"upsert", "delete", "delete_many"} and entry.get("seq", 0) > sequence:
                kept.append(stripped + b"\n")
        temporary = journal_path.with_name(journal_path.name + ".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.writelines(kept)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, journal_path)
        finally:
            temporary.unlink(missing_ok=True)
        state.journal = journal_path.open("ab")
        state.journal_bytes = journal_path.stat().st_size

    def _checkpoint_guarded(self, state):
        try:
            self._checkpoint(state)
        except Exception:
            LOGGER.exception("后台整理失败：%s", state.name)
        finally:
            with self.lock:
                state.checkpointing = False

    def _checkpoint(self, state):
        started = time.perf_counter()
        with self.lock:
            view = dict(state.records)
            sequence = state.sequence
        written = self._write_snapshot_file(state.name, sequence, view)
        with self.lock:
            self._rotate_journal(state, sequence)
            state.snapshot_bytes = self._path(state.name).stat().st_size
            elapsed = (time.perf_counter() - started) * 1000
            metrics = self._metrics
            metrics["checkpoints"] += 1
            metrics["checkpoint_bytes_written"] += written
            metrics["checkpoint_wait_ms"] += elapsed
            metrics["last_checkpoint_ms"] = elapsed
            metrics["last_checkpoint_at"] = utc_now()

    def checkpoint(self, collection=None):
        """Compact now (synchronously); used by maintenance and tests."""
        names = [collection] if collection else sorted(self.COLLECTIONS)
        for name in names:
            state = self._states[name]
            while True:
                with self.lock:
                    if not state.checkpointing:
                        state.checkpointing = True
                        break
                time.sleep(0.005)
            try:
                self._checkpoint(state)
            finally:
                with self.lock:
                    state.checkpointing = False

    # ----------------------------------------------------------------- queries

    def all(self, collection):
        with self.lock:
            return copy.deepcopy(list(self._states[collection].records.values()))

    def get(self, collection, record_id):
        with self.lock:
            record = self._states[collection].records.get(record_id)
            if record is None:
                raise MissingRecord(collection, record_id)
            return copy.deepcopy(record)

    def insert(self, collection, record):
        with self.lock:
            state = self._states[collection]
            record = copy.deepcopy(record)
            record.setdefault("id", new_id())
            record.setdefault("created_at", utc_now())
            record["updated_at"] = utc_now()
            if record["id"] in state.records:
                raise Conflict("记录已存在")
            state.sequence += 1
            state.records[record["id"]] = record
            try:
                self._journal(state, {"seq": state.sequence, "op": "upsert", "id": record["id"], "record": record})
            except Exception:
                del state.records[record["id"]]
                state.sequence -= 1
                raise
            return copy.deepcopy(record)

    def update(self, collection, record_id, changes):
        with self.lock:
            state = self._states[collection]
            previous = state.records.get(record_id)
            if previous is None:
                raise MissingRecord(collection, record_id)
            changes = copy.deepcopy(changes)
            changes.pop("id", None)
            changes.pop("created_at", None)
            record = {**previous, **changes, "updated_at": utc_now()}
            state.sequence += 1
            state.records[record_id] = record
            try:
                self._journal(state, {"seq": state.sequence, "op": "upsert", "id": record_id, "record": record})
            except Exception:
                state.records[record_id] = previous
                state.sequence -= 1
                raise
            return copy.deepcopy(record)

    def delete(self, collection, record_id):
        with self.lock:
            state = self._states[collection]
            removed = state.records.pop(record_id, None)
            if removed is None:
                raise MissingRecord(collection, record_id)
            state.sequence += 1
            try:
                self._journal(state, {"seq": state.sequence, "op": "delete", "id": record_id})
            except Exception:
                state.records[record_id] = removed
                state.sequence -= 1
                raise
            return copy.deepcopy(removed)

    def prune(self, collection, maximum):
        with self.lock:
            state = self._states[collection]
            records = sorted(
                state.records.values(),
                key=lambda item: item["created_at"],
                reverse=True,
            )
            if len(records) <= maximum:
                return
            kept = records[:maximum]
            removed_ids = [item["id"] for item in records[maximum:]]
            previous = state.records
            state.records = {item["id"]: item for item in kept}
            state.sequence += 1
            try:
                self._journal(state, {"seq": state.sequence, "op": "delete_many", "ids": removed_ids})
            except Exception:
                state.records = previous
                state.sequence -= 1
                raise

    def event(self, action, message, details=None):
        self.insert("events", {
            "action": action,
            "message": message,
            "details": details or {},
        })
        self.prune("events", 500)

    def snapshot(self):
        with self.lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "created_at": utc_now(),
                "collections": {
                    name: {
                        "schema_version": SCHEMA_VERSION,
                        "records": copy.deepcopy(self._states[name].records),
                    }
                    for name in sorted(self.COLLECTIONS)
                },
            }

    # ----------------------------------------------------------------- metrics

    def metrics(self):
        with self.lock:
            collections = {}
            for name in sorted(self.COLLECTIONS):
                state = self._states[name]
                collections[name] = {
                    "records": len(state.records),
                    "sequence": state.sequence,
                    "journal_bytes": state.journal_bytes,
                    "snapshot_bytes": state.snapshot_bytes,
                    "checkpointing": state.checkpointing,
                }
            report = dict(self._metrics)
            report["save_wait_ms"] = round(report["save_wait_ms"], 2)
            report["save_wait_max_ms"] = round(report["save_wait_max_ms"], 2)
            report["checkpoint_wait_ms"] = round(report["checkpoint_wait_ms"], 2)
            report["last_checkpoint_ms"] = round(report["last_checkpoint_ms"], 2)
            report["journal_bytes_pending"] = sum(item["journal_bytes"] for item in collections.values())
            report["collections"] = collections
            return report

    def close(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.lock:
                busy = any(state.checkpointing for state in self._states.values())
            if not busy:
                break
            time.sleep(0.01)
        with self.lock:
            for state in self._states.values():
                if state.journal is not None and not state.journal.closed:
                    state.journal.close()
