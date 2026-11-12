"""Procedural photographic and illustration effects; no remote services."""
import numpy as np
from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageDraw, ImageColor
from ..imaging import rgb_array, from_array, with_alpha, normalized_grid, blend_effect


def filter_sepia(image, p, context):
    array = rgb_array(image)
    matrix = np.array([[0.393, 0.769, 0.189],
                       [0.349, 0.686, 0.168],
                       [0.272, 0.534, 0.131]], dtype=np.float32)
    sepia = array @ matrix.T
    mixed = array * (1 - p["strength"]) + sepia * p["strength"]
    return from_array(mixed, image)


def filter_vintage(image, p, context):
    array = rgb_array(image) / 255
    faded = 0.07 + array * 0.86
    faded[..., 0] *= 1.08
    faded[..., 2] *= 0.88
    effect = from_array(faded * 255, image)
    effect = ImageEnhance.Color(effect).enhance(p["saturation"])
    effect = filter_vignette(effect, {"strength": p["vignette"], "radius": 0.65}, context)
    return blend_effect(image, effect, p["strength"])


def filter_vignette(image, p, context):
    x, y = normalized_grid(image)
    distance = np.sqrt(x * x + y * y) / np.sqrt(2)
    mask = np.clip((distance - p["radius"]) / max(1 - p["radius"], 0.01), 0, 1)
    mask = mask * mask * (3 - 2 * mask)
    factor = 1 - mask * p["strength"]
    return from_array(rgb_array(image) * factor[..., None], image)


def filter_grain(image, p, context):
    rng = np.random.default_rng(p["seed"])
    shape = (image.height, image.width, 1 if p["monochrome"] else 3)
    noise = rng.normal(0, p["amount"], shape).astype(np.float32)
    return from_array(rgb_array(image) + noise, image)


def filter_pixelate(image, p, context):
    size = (max(1, image.width // p["block"]), max(1, image.height // p["block"]))
    miniature = image.resize(size, Image.Resampling.BOX)
    return miniature.resize(image.size, Image.Resampling.NEAREST)


def filter_posterize(image, p, context):
    result = ImageOps.posterize(image.convert("RGB"), p["bits"])
    return with_alpha(result, image)


def filter_solarize(image, p, context):
    result = ImageOps.solarize(image.convert("RGB"), p["threshold"])
    return with_alpha(result, image)


def filter_sketch(image, p, context):
    gray = image.convert("L")
    inverted = ImageOps.invert(gray)
    blurred = inverted.filter(ImageFilter.GaussianBlur(p["radius"]))
    base = np.asarray(gray, dtype=np.float32)
    denominator = 255 - np.asarray(blurred, dtype=np.float32)
    sketch = np.clip(base * 255 / np.maximum(denominator, 1), 0, 255)
    result = from_array(sketch, image)
    if p["color"]:
        gray_rgb = np.asarray(result.convert("RGB"), dtype=np.float32) / 255
        result = from_array(rgb_array(image) * gray_rgb, image)
    return result


def filter_cartoon(image, p, context):
    smooth = image.convert("RGB").filter(ImageFilter.MedianFilter(p["smooth"]))
    poster = ImageOps.posterize(smooth, p["bits"])
    gray = image.convert("L").filter(ImageFilter.GaussianBlur(1))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_array = np.asarray(edges)
    mask = (edge_array < p["threshold"]).astype(np.float32)
    mask[0, :] = 1
    mask[-1, :] = 1
    mask[:, 0] = 1
    mask[:, -1] = 1
    result = np.asarray(poster, dtype=np.float32) * mask[..., None]
    return from_array(result, image)


def filter_emboss(image, p, context):
    effect = image.convert("RGB").filter(ImageFilter.EMBOSS)
    effect = with_alpha(effect, image)
    return blend_effect(image, effect, p["strength"])


def filter_halftone(image, p, context):
    block = p["cell"]
    columns = max(1, image.width // block)
    rows = max(1, image.height // block)
    gray = image.convert("L").resize((columns, rows), Image.Resampling.BOX)
    values = np.asarray(gray)
    output = Image.new("RGBA", image.size, p["paper"])
    draw = ImageDraw.Draw(output)
    cell_width = image.width / columns
    cell_height = image.height / rows
    for row in range(rows):
        for column in range(columns):
            darkness = 1 - float(values[row, column]) / 255
            radius = np.sqrt(darkness) * min(cell_width, cell_height) * 0.48
            x = (column + 0.5) * cell_width
            y = (row + 0.5) * cell_height
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=p["ink"])
    output.putalpha(image.getchannel("A"))
    return output


def filter_gradient_map(image, p, context):
    gray = image.convert("L")
    gradient = ImageOps.colorize(gray, p["shadow"], p["highlight"], mid=p["midtone"], midpoint=p["midpoint"])
    effect = with_alpha(gradient, image)
    return blend_effect(image, effect, p["strength"])


def filter_light_leak(image, p, context):
    x, y = normalized_grid(image)
    center_x = -1 if p["side"] == "left" else 1
    distance = ((x - center_x) / p["spread"]) ** 2 + ((y + 0.3) / 1.4) ** 2
    mask = np.exp(-distance * 2) * p["strength"]
    color = np.asarray(ImageColor.getrgb(p["color"]), dtype=np.float32) / 255
    base = rgb_array(image) / 255
    screen = 1 - (1 - base) * (1 - mask[..., None] * color)
    return from_array(screen * 255, image)


def filter_scanlines(image, p, context):
    array = rgb_array(image)
    rows = np.arange(image.height)
    factor = np.where(rows % p["spacing"] == 0, 1 - p["strength"], 1)
    array *= factor[:, None, None]
    return from_array(array, image)

