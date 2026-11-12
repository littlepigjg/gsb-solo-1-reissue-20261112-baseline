"""Pixel-format preparation; final encoding is handled by ExportService."""
import io
import numpy as np
from PIL import Image, ImageOps, ImageColor
from ..imaging import flatten, from_array, rgb_array


def format_flatten(image, p, context):
    return flatten(image, p["background"]).convert("RGBA")


def format_palette(image, p, context):
    dither = Image.Dither.FLOYDSTEINBERG if p["dither"] else Image.Dither.NONE
    rgb = flatten(image, p["background"])
    quantized = rgb.quantize(colors=p["colors"], method=Image.Quantize.MEDIANCUT, dither=dither)
    return quantized.convert("RGBA")


def format_monochrome(image, p, context):
    gray = image.convert("L")
    dither = Image.Dither.FLOYDSTEINBERG if p["dither"] else Image.Dither.NONE
    output = gray.convert("1", dither=dither).convert("RGBA")
    output.putalpha(image.getchannel("A"))
    return output


def format_jpeg_preview(image, p, context):
    buffer = io.BytesIO()
    rgb = flatten(image, p["background"])
    rgb.save(buffer, "JPEG", quality=p["quality"], subsampling=p["subsampling"])
    buffer.seek(0)
    with Image.open(buffer) as encoded:
        encoded.load()
        return encoded.convert("RGBA")


def format_opacity(image, p, context):
    output = image.copy()
    alpha = np.asarray(image.getchannel("A"), dtype=np.float32)
    output.putalpha(Image.fromarray((alpha * p["opacity"]).astype(np.uint8)))
    return output


def format_color_key(image, p, context):
    rgb = rgb_array(image)
    key = np.asarray(ImageColor.getrgb(p["color"]), dtype=np.float32)
    distance = np.sqrt(np.mean((rgb - key) ** 2, axis=2))
    softness = max(p["softness"], 1)
    mask = np.clip((distance - p["tolerance"]) / softness, 0, 1)
    alpha = np.asarray(image.getchannel("A"), dtype=np.float32) * mask
    output = image.copy()
    output.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return output

