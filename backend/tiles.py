"""On-demand image pyramid: small previews and addressable tiles for the viewer.

Tiles and previews are regenerable caches stored under ``data/cache`` and never
replace the canonical PNG in ``data/media``, which processing and exports use.
"""
import os
import threading
from collections import OrderedDict
import numpy as np
from PIL import Image
from .errors import StudioError

TILE_SIZE = 256
PREVIEW_EDGE = 1024
LEVEL_CACHE_PIXELS = 48_000_000


def level_dimensions(width, height, level):
    scale = 2 ** level
    return max(1, -(-width // scale)), max(1, -(-height // scale))


def fit_within(width, height, edge):
    factor = min(1.0, edge / max(width, height))
    return max(1, round(width * factor)), max(1, round(height * factor))


def downscale_rgba(image, size):
    """Resize with premultiplied alpha so transparent edges keep true colors."""
    if image.size == size:
        return image
    if image.getchannel("A").getextrema() == (255, 255):
        return image.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    array = np.asarray(image, dtype=np.float32)
    array[..., :3] *= (array[..., 3:4] / 255.0)
    premultiplied = Image.fromarray(array.astype(np.uint8), "RGBA")
    resized = premultiplied.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    result = np.asarray(resized, dtype=np.float32)
    alpha = result[..., 3:4] / 255.0
    solid = alpha >= (1.0 / 255.0)
    rgb = np.where(solid, result[..., :3] / np.maximum(alpha, 1.0 / 255.0), 0.0)
    output = np.concatenate([rgb, result[..., 3:4]], axis=2)
    return Image.fromarray(np.clip(output + 0.5, 0, 255).astype(np.uint8), "RGBA")


class TileService:
    def __init__(self, settings, assets):
        self.settings = settings
        self.assets = assets
        self.lock = threading.RLock()
        self.level_images = OrderedDict()
        self.level_pixels = 0
        self.passthrough = set()

    def pyramid(self, asset_id):
        return self._pyramid_meta(self.assets.get(asset_id))

    def preview_file(self, asset_id):
        path, record = self.assets.file(asset_id)
        if max(record["width"], record["height"]) <= PREVIEW_EDGE or asset_id in self.passthrough:
            return path, record
        cached = self.settings.cache / "previews" / f"{asset_id}.png"
        if not cached.is_file():
            with self.lock:
                if not cached.is_file():
                    image = self._level_image(asset_id, 0)
                    preview = downscale_rgba(image, fit_within(image.width, image.height, PREVIEW_EDGE))
                    self._write_png(preview, cached)
        if cached.stat().st_size >= path.stat().st_size:
            # Highly compressible sources beat their own preview; serve the file.
            self.passthrough.add(asset_id)
            cached.unlink(missing_ok=True)
            return path, record
        return cached, record

    def tile_file(self, asset_id, level, x, y):
        record = self.assets.get(asset_id)
        meta = self._pyramid_meta(record)
        if not 0 <= level < len(meta["levels"]):
            raise StudioError("缩放级别超出范围", details={"level": level})
        spec = meta["levels"][level]
        if not (0 <= x < spec["cols"] and 0 <= y < spec["rows"]):
            raise StudioError("瓦片坐标超出范围", details={"x": x, "y": y, "level": level})
        cached = self.settings.cache / "tiles" / asset_id / f"{level}_{x}_{y}.png"
        if not cached.is_file():
            with self.lock:
                if not cached.is_file():
                    image = self._level_image(asset_id, level)
                    box = (
                        x * TILE_SIZE,
                        y * TILE_SIZE,
                        min((x + 1) * TILE_SIZE, spec["width"]),
                        min((y + 1) * TILE_SIZE, spec["height"]),
                    )
                    tile = image.crop(box)
                    tile.load()
                    self._write_png(tile, cached)
        return cached, record

    def discard(self, asset_id):
        """Drop every cached artifact of an asset; called after deletion."""
        with self.lock:
            self.passthrough.discard(asset_id)
            for key in [key for key in self.level_images if key[0] == asset_id]:
                self._evict(key)
            folder = self.settings.cache / "tiles" / asset_id
            if folder.is_dir():
                for file in folder.glob("*.png"):
                    file.unlink(missing_ok=True)
                folder.rmdir()
            (self.settings.cache / "previews" / f"{asset_id}.png").unlink(missing_ok=True)

    def cache_stats(self):
        files = 0
        total = 0
        if self.settings.cache.is_dir():
            for file in self.settings.cache.rglob("*.png"):
                files += 1
                total += file.stat().st_size
        return {"files": files, "bytes": total}

    def _pyramid_meta(self, record):
        width, height = record["width"], record["height"]
        levels = []
        level = 0
        while True:
            level_width, level_height = level_dimensions(width, height, level)
            levels.append({
                "level": level,
                "downsample": 2 ** level,
                "width": level_width,
                "height": level_height,
                "cols": -(-level_width // TILE_SIZE),
                "rows": -(-level_height // TILE_SIZE),
            })
            if level_width <= TILE_SIZE and level_height <= TILE_SIZE:
                break
            level += 1
        preview_width, preview_height = fit_within(width, height, PREVIEW_EDGE)
        return {
            "asset_id": record["id"],
            "width": width,
            "height": height,
            "tile_size": TILE_SIZE,
            "levels": levels,
            "preview": {"width": preview_width, "height": preview_height},
            "full_bytes": record["bytes"],
        }

    def _level_image(self, asset_id, level):
        """Decoded RGBA level image; callers must hold ``self.lock`` while using it."""
        key = (asset_id, level)
        cached = self.level_images.get(key)
        if cached is not None:
            self.level_images.move_to_end(key)
            return cached[0]
        if level == 0:
            path, _record = self.assets.file(asset_id)
            with Image.open(path) as opened:
                opened.load()
                image = opened.convert("RGBA")
        else:
            base = self._level_image(asset_id, 0)
            image = downscale_rgba(base, level_dimensions(base.width, base.height, level))
        pixels = image.width * image.height
        self.level_images[key] = (image, pixels)
        self.level_pixels += pixels
        while self.level_pixels > LEVEL_CACHE_PIXELS and len(self.level_images) > 1:
            self._evict(next(iter(self.level_images)))
        return image

    def _evict(self, key):
        image, pixels = self.level_images.pop(key)
        image.close()
        self.level_pixels -= pixels

    @staticmethod
    def _write_png(image, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            image.save(temporary, format="PNG", compress_level=4)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
