"""JSON repository with serialized read/modify/write and atomic replacement.

The local server owns one process. All HTTP and worker threads share this
repository. A backup is retained before each successful replacement. Invalid
JSON is never replaced silently: a valid backup is recovered or startup fails.
"""
import copy
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from .errors import MissingRecord, Conflict


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


class JsonStore:
    COLLECTIONS = {"assets", "jobs", "presets", "events", "exports", "annotations"}

    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for collection in self.COLLECTIONS:
            with self.lock:
                if not self._path(collection).exists():
                    self._write(collection, {"schema_version": 1, "records": {}})
                else:
                    self._read(collection)

    def _path(self, collection):
        if collection not in self.COLLECTIONS:
            raise ValueError("Unknown collection")
        return self.directory / f"{collection}.json"

    def _decode(self, path):
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != 1 or not isinstance(data.get("records"), dict):
            raise ValueError("Unsupported JSON schema")
        return data

    def _read(self, collection):
        path = self._path(collection)
        try:
            return self._decode(path)
        except (ValueError, OSError):
            backup = path.with_suffix(".json.bak")
            if not backup.exists():
                raise RuntimeError(f"JSON 数据损坏且没有可用备份：{path}")
            data = self._decode(backup)
            recovery = path.with_suffix(".json.recovery")
            shutil.copy2(backup, recovery)
            os.replace(recovery, path)
            return data

    def _write(self, collection, data):
        path = self._path(collection)
        temporary = path.with_suffix(f".{new_id()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                shutil.copy2(path, path.with_suffix(".json.bak"))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def all(self, collection):
        with self.lock:
            data = self._read(collection)
            return copy.deepcopy(list(data["records"].values()))

    def get(self, collection, record_id):
        with self.lock:
            data = self._read(collection)
            record = data["records"].get(record_id)
            if record is None:
                raise MissingRecord(collection, record_id)
            return copy.deepcopy(record)

    def insert(self, collection, record):
        with self.lock:
            data = self._read(collection)
            record = copy.deepcopy(record)
            record.setdefault("id", new_id())
            record.setdefault("created_at", utc_now())
            record["updated_at"] = utc_now()
            if record["id"] in data["records"]:
                raise Conflict("记录已存在")
            data["records"][record["id"]] = record
            self._write(collection, data)
            return copy.deepcopy(record)

    def update(self, collection, record_id, changes):
        with self.lock:
            data = self._read(collection)
            if record_id not in data["records"]:
                raise MissingRecord(collection, record_id)
            record = data["records"][record_id]
            changes = copy.deepcopy(changes)
            changes.pop("id", None)
            changes.pop("created_at", None)
            record.update(changes)
            record["updated_at"] = utc_now()
            self._write(collection, data)
            return copy.deepcopy(record)

    def delete(self, collection, record_id):
        with self.lock:
            data = self._read(collection)
            if record_id not in data["records"]:
                raise MissingRecord(collection, record_id)
            removed = data["records"].pop(record_id)
            self._write(collection, data)
            return copy.deepcopy(removed)

    def prune(self, collection, maximum):
        with self.lock:
            data = self._read(collection)
            records = sorted(
                data["records"].values(),
                key=lambda item: item["created_at"],
                reverse=True,
            )
            if len(records) > maximum:
                data["records"] = {item["id"]: item for item in records[:maximum]}
                self._write(collection, data)

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
                "schema_version": 1,
                "created_at": utc_now(),
                "collections": {
                    name: self._read(name)
                    for name in sorted(self.COLLECTIONS)
                },
            }

