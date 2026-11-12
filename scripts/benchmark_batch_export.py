"""Reproducible large-photo batch export benchmark.

Generates sixteen deterministic 6-megapixel PNG sources (24 MB budget each),
then exports the batch twice through the public service API and reports the
archive size plus peak resident memory during each export phase.

Run from the project root:

    python scripts/benchmark_batch_export.py --count 16 --size 3000x2000

Sources are saved under a temporary data directory and removed afterwards;
nothing is written to the real studio data folder.
"""
import argparse
import os
import resource
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(os.path.relpath(Path(__file__).parents[1]))
sys.path.insert(0, str(ROOT))


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def make_source(width, height, seed):
    """Deterministic photo-like content at full resolution, with real alpha."""
    channels = np.empty((height, width, 4), dtype=np.uint8)
    step = max(1, width // 900)
    columns = np.arange(width) // step
    rows = np.arange(height) // step
    gradient = (columns[None, :] + rows[:, None] + seed) % 256
    channels[..., 0] = gradient
    channels[..., 1] = (gradient // 2 + seed * 3) % 256
    channels[..., 2] = (np.arange(width)[None, :] + seed) % 256
    noise = np.random.default_rng(seed).integers(0, 32, (height, width, 1), dtype=np.uint8)
    channels[..., :3] = np.clip(channels[..., :3].astype(np.int16) + noise, 0, 255)
    channels[..., 3] = 255
    channels[: max(1, height // 15), :, 3] = 0
    return Image.fromarray(channels)


def measure_phase(service, asset_ids, options):
    baseline = rss_mb()
    peak = [baseline]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], rss_mb())
            time.sleep(0.02)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    task = service.archive_task(asset_ids, options)
    while True:
        record = service.task(task["id"])
        if record["status"] not in {"queued", "preparing", "cancelling"}:
            break
        time.sleep(0.1)
    elapsed = time.perf_counter() - started
    stop.set()
    sampler.join(timeout=1)
    return record, elapsed, peak[0] - baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=16, help="number of photos")
    parser.add_argument("--size", default="3000x2000", help="widthxheight, defaults to 6 MP")
    parser.add_argument("--format", default="PNG", choices=["PNG", "JPEG", "WEBP", "TIFF", "BMP", "GIF"])
    args = parser.parse_args()
    width, height = (int(value) for value in args.size.lower().split("x"))
    if width * height > 16_000_000 or max(width, height) > 8192:
        raise SystemExit("Size exceeds the studio's documented 16 MP / 8192 px limits")

    from backend.app import create_app

    root = Path(tempfile.mkdtemp(prefix="studio-export-bench-"))
    data = root / "data"
    try:
        app = create_app(data, testing=True)
        assets = app.extensions["studio"]["assets"]
        exports = app.extensions["studio"]["exports"]
        ids = []
        for index in range(args.count):
            record = assets.save(
                make_source(width, height, index * 13),
                name=f"benchmark-{index:02d}.png",
                kind="original",
            )
            ids.append(record["id"])
        print(f"{args.count} sources at {width}x{height} = {width * height / 1_000_000:.1f} MP each", flush=True)

        options = {"format": args.format, "quality": 90, "background": "#ffffff", "lossless": False, "dpi": 96}
        record, elapsed, delta = measure_phase(exports, ids[: args.count // 2], options)
        half = record["bytes"]
        print(
            f"{args.count // 2} images -> {half / 1_000_000:.2f} MB archive, "
            f"{elapsed:.1f}s, export-phase peak +{delta:.1f} MB",
            flush=True,
        )
        record, elapsed, delta = measure_phase(exports, ids, options)
        print(
            f"{args.count} images -> {record['bytes'] / 1_000_000:.2f} MB archive, "
            f"{elapsed:.1f}s, export-phase peak +{delta:.1f} MB",
            flush=True,
        )
        print(
            "Compare the two peaks: the archive doubled while memory stayed near one source image.",
            flush=True,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
