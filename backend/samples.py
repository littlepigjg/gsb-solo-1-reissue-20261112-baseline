"""Original procedural sample artwork; no external image download required."""
import math
import threading
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from .operations.watermark import load_font


def landscape(width=1440, height=1080):
    y = np.linspace(0, 1, height)[:, None, None]
    sky_top = np.array([163, 193, 182], dtype=np.float32)
    sky_bottom = np.array([233, 222, 181], dtype=np.float32)
    sky = sky_top * (1 - y) + sky_bottom * y
    array = np.broadcast_to(sky, (height, width, 3)).astype(np.uint8).copy()
    image = Image.fromarray(array).convert("RGBA")
    draw = ImageDraw.Draw(image)
    draw.ellipse((width * 0.62, height * 0.14, width * 0.77, height * 0.34), fill="#f5e5ac")
    layers = [
        (0.49, 0.10, "#8eaaa0", 0.6),
        (0.57, 0.15, "#668b7d", 1.8),
        (0.68, 0.13, "#3e6b5b", 3.2),
        (0.83, 0.12, "#264f41", 4.4),
    ]
    for base, amplitude, color, phase in layers:
        points = [(0, height)]
        for x in range(-10, width + 20, 8):
            t = x / width
            wave = math.sin(t * 5.3 + phase) * amplitude
            detail = math.sin(t * 14 + phase) * amplitude * 0.18
            points.append((x, round(height * (base + wave + detail))))
        points.append((width, height))
        draw.polygon(points, fill=color)
    rng = np.random.default_rng(19)
    for index in range(85):
        x = int(rng.integers(0, width))
        base = int(rng.integers(int(height * 0.87), height))
        size = int(rng.integers(15, 75))
        draw.line((x, base, x + size * 0.1, base - size), fill="#b1bd8d", width=2)
        draw.ellipse((x - 3, base - size - 4, x + 4, base - size + 3), fill="#dbd2a1")
    pixels = np.array(image, dtype=np.float32)
    grain = rng.normal(0, 1.1, (height, width, 1))
    pixels[..., :3] += grain
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def still_life(width=1200, height=1200):
    image = Image.new("RGBA", (width, height), "#e9dfcc")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height * 0.62, width, height), fill="#d4c1a5")
    shadow = Image.new("RGBA", image.size)
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.ellipse((width * 0.24, height * 0.65, width * 0.8, height * 0.79), fill=(58, 58, 32, 55))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    image = Image.alpha_composite(image, shadow)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((width * 0.37, height * 0.42, width * 0.64, height * 0.75), radius=65, fill="#c46c4c")
    draw.ellipse((width * 0.37, height * 0.39, width * 0.64, height * 0.48), fill="#a3513d")
    for x, y, tx, ty in [(600, 540, 420, 190), (610, 550, 740, 140), (590, 540, 560, 170), (620, 550, 820, 320)]:
        draw.line((x, y, tx, ty), fill="#576d45", width=8)
        for offset in range(0, 4):
            u = 0.25 + offset * 0.15
            cx = x + (tx - x) * u
            cy = y + (ty - y) * u
            side = 1 if offset % 2 else -1
            draw.ellipse((cx - 15 + side * 28, cy - 48, cx + 50 + side * 28, cy + 10), fill="#657e50")
    return image


def color_study(width=1440, height=960):
    image = Image.new("RGBA", (width, height), "#f2ead7")
    draw = ImageDraw.Draw(image)
    colors = ["#21483b", "#66856d", "#abc09d", "#dce5c0", "#e7bf72", "#c77451"]
    for index, color in enumerate(colors):
        x = 95 + index * 210
        draw.rounded_rectangle((x, 160, x + 170, 720), radius=85, fill=color)
        draw.text((x + 4, 758), color.upper(), font=load_font(24), fill="#344c3f")
    draw.text((96, 70), "COLOR STUDY / 03", font=load_font(35), fill="#21483b")
    draw.text((96, 850), "自然的颜色，留给日常的灵感。", font=load_font(28), fill="#66856d")
    return image


class SampleService:
    def __init__(self, assets, store):
        self.assets = assets
        self.store = store
        self.lock = threading.Lock()

    def ensure(self):
        with self.lock:
            existing = {item["metadata"].get("sample_key"): item for item in self.store.all("assets") if item["kind"] == "sample"}
            result = []
            definitions = [
                ("landscape", "山野来信.png", landscape),
                ("still-life", "窗边绿意.png", still_life),
                ("color-study", "自然色谱.png", color_study),
            ]
            for key, name, factory in definitions:
                if key in existing:
                    result.append(self.assets.public(existing[key]))
                else:
                    image = factory()
                    record = self.assets.save(image, name, kind="sample", metadata={"sample_key": key, "license": "Project original procedural sample"})
                    result.append(record)
            return result

