"""Measure save cost as the library grows: bytes written and wait per save."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.relpath(Path(__file__).parents[1]))
from backend.storage import JsonStore


def populate(store, count):
    ids = []
    for index in range(count):
        record = store.insert("assets", {
            "name": f"素材{index:05d}.png",
            "kind": "original",
            "width": 1920, "height": 1080, "sha256": "ab" * 32,
            "tags": ["样片", "未分类"], "favorite": False,
            "steps": [{"operation": "color_temperature", "params": {"temperature": 12}}],
            "metadata": {"source_format": "PNG", "note": "x" * 120},
        })
        ids.append(record["id"])
    return ids


def measure(store, ids, rounds):
    start = time.perf_counter()
    for index in range(rounds):
        asset_id = ids[index % len(ids)]
        store.update("assets", asset_id, {"name": f"改名{index}.png", "favorite": True, "tags": ["已整理"]})
    elapsed = (time.perf_counter() - start) * 1000
    metrics = store.metrics()
    return elapsed, metrics


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = JsonStore(Path(directory) / "json")
        ids = populate(store, 300)
        store.checkpoint()
        before = store.metrics()
        elapsed, metrics = measure(store, ids, 300)
        snapshot = os.path.getsize(Path(directory) / "json" / "assets.json")
        print(f"library snapshot: {snapshot / 1024:.0f} KiB, 300 assets")
        print(f"300 rename/tag/favorite saves: {elapsed:.1f} ms total, {elapsed / 300:.2f} ms/save")
        print(f"journal bytes for 300 saves: {metrics['journal_bytes_written'] - before['journal_bytes_written']}")
        print(f"save wait (fsync) total: {metrics['save_wait_ms'] - before['save_wait_ms']:.1f} ms")
        print(f"checkpoints during saves: {metrics['checkpoints'] - before['checkpoints']}")
        print(f"journal pending after: {metrics['journal_bytes_pending']} bytes")
        # A full-file rewrite design would have written 300 x snapshot (~= 300 x snapshot KiB).
        print(f"equivalent full-file rewrites would be: {300 * snapshot / 1024 / 1024:.1f} MiB")
        store.close()


if __name__ == "__main__":
    main()
