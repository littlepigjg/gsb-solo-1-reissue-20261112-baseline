"""Color transforms in RGB, HSV and luminance spaces."""
import numpy as np
from PIL import Image, ImageEnhance, ImageOps, ImageColor
from ..errors import StudioError
from ..imaging import rgb_array, from_array, with_alpha, luminance, smoothstep


def color_brightness(image, p, context):
    rgb = ImageEnhance.Brightness(image.convert("RGB")).enhance(p["factor"])
    return with_alpha(rgb, image)


def color_contrast(image, p, context):
    rgb = ImageEnhance.Contrast(image.convert("RGB")).enhance(p["factor"])
    return with_alpha(rgb, image)


def color_saturation(image, p, context):
    rgb = ImageEnhance.Color(image.convert("RGB")).enhance(p["factor"])
    return with_alpha(rgb, image)


def color_exposure(image, p, context):
    array = rgb_array(image) / 255
    linear = np.where(array <= 0.04045, array / 12.92, ((array + 0.055) / 1.055) ** 2.4)
    linear = np.clip(linear * (2 ** p["stops"]), 0, 1)
    encoded = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)
    return from_array(encoded * 255, image)


def color_gamma(image, p, context):
    array = rgb_array(image) / 255
    output = np.power(array, 1 / p["gamma"])
    return from_array(output * 255, image)


def color_temperature(image, p, context):
    array = rgb_array(image)
    warm = p["temperature"] * 0.45
    tint = p["tint"] * 0.35
    array[..., 0] += warm + tint / 2
    array[..., 1] -= tint
    array[..., 2] -= warm - tint / 2
    return from_array(array, image)


def color_hue(image, p, context):
    hsv = np.array(image.convert("RGB").convert("HSV"), dtype=np.uint8)
    hue = hsv[..., 0].astype(np.int16)
    hsv[..., 0] = ((hue + round(p["degrees"] * 255 / 360)) % 256).astype(np.uint8)
    rgb = Image.frombytes("HSV", image.size, hsv.tobytes()).convert("RGB")
    return with_alpha(rgb, image)


def color_balance(image, p, context):
    array = rgb_array(image)
    channels = (p["red"], p["green"], p["blue"])
    if p["preserve_luminance"]:
        before = luminance(array)
    for index, value in enumerate(channels):
        array[..., index] *= value
    if p["preserve_luminance"]:
        after = luminance(array)
        ratio = before / np.maximum(after, 1)
        array *= ratio[..., None]
    return from_array(array, image)


def color_levels(image, p, context):
    if p["black"] >= p["white"]:
        raise StudioError("黑场必须小于白场")
    array = rgb_array(image)
    normalized = np.clip((array - p["black"]) / (p["white"] - p["black"]), 0, 1)
    corrected = normalized ** (1 / p["gamma"])
    return from_array(corrected * 255, image)


def color_shadows(image, p, context):
    array = rgb_array(image)
    light = luminance(array) / 255
    shadows = 1 - smoothstep(0, 0.6, light)
    highlights = smoothstep(0.4, 1, light)
    change = shadows * p["shadows"] - highlights * p["highlights"]
    result = array + change[..., None] * 1.2
    return from_array(result, image)


def color_grayscale(image, p, context):
    array = rgb_array(image)
    if p["method"] == "luminance":
        gray = luminance(array)
    elif p["method"] == "average":
        gray = array.mean(axis=2)
    else:
        gray = (array.min(axis=2) + array.max(axis=2)) / 2
    output = np.repeat(gray[..., None], 3, axis=2)
    mixed = array * (1 - p["strength"]) + output * p["strength"]
    return from_array(mixed, image)


def color_invert(image, p, context):
    array = rgb_array(image)
    channel_indices = {"all": [0, 1, 2], "red": [0], "green": [1], "blue": [2]}
    for index in channel_indices[p["channel"]]:
        array[..., index] = 255 - array[..., index]
    return from_array(array, image)


def color_autocontrast(image, p, context):
    rgb = ImageOps.autocontrast(image.convert("RGB"), cutoff=p["cutoff"], preserve_tone=p["preserve_tone"])
    return with_alpha(rgb, image)


def color_equalize(image, p, context):
    rgb = image.convert("RGB")
    if p["channel"] == "luminance":
        y, cb, cr = rgb.convert("YCbCr").split()
        y = ImageOps.equalize(y)
        result = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
    else:
        result = ImageOps.equalize(rgb)
    result = Image.blend(rgb, result, p["strength"])
    return with_alpha(result, image)


def color_duotone(image, p, context):
    gray = image.convert("L")
    result = ImageOps.colorize(gray, p["shadow"], p["highlight"])
    rgb = Image.blend(image.convert("RGB"), result, p["strength"])
    return with_alpha(rgb, image)


def color_threshold(image, p, context):
    gray = np.asarray(image.convert("L"))
    dark = np.array(ImageColor.getrgb(p["dark"]), dtype=np.uint8)
    light = np.array(ImageColor.getrgb(p["light"]), dtype=np.uint8)
    result = np.where((gray >= p["threshold"])[..., None], light, dark)
    return from_array(result, image)


def color_vibrance(image, p, context):
    array = rgb_array(image)
    maximum = array.max(axis=2)
    minimum = array.min(axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1)
    light = luminance(array)[..., None]
    factor = 1 + (1 - saturation)[..., None] * p["amount"] / 100
    result = light + (array - light) * factor
    return from_array(result, image)


def color_channel_mixer(image, p, context):
    array = rgb_array(image)
    order = {"rgb": (0, 1, 2), "rbg": (0, 2, 1), "grb": (1, 0, 2),
             "gbr": (1, 2, 0), "brg": (2, 0, 1), "bgr": (2, 1, 0)}
    remapped = array[..., list(order[p["order"]])]
    result = array * (1 - p["strength"]) + remapped * p["strength"]
    return from_array(result, image)

