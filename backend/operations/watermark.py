"""Text, labels, borders and transparency-aware graphics."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageColor, ImageFilter, ImageChops
from ..errors import StudioError
from ..imaging import checked_size, fit_font
from ..settings import PROJECT_ROOT


def load_font(size):
    candidates = [
        str(PROJECT_ROOT / "assets/fonts/NotoSansSC-Regular.ttf"),
        "msyh.ttc",
        "simhei.ttf",
        "PingFang.ttc",
        "DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _text_layer(image, p):
    if not p["text"]:
        raise StudioError("请输入水印文字")
    font = fit_font(p["text"], p["size"], max(8, image.width - p["margin"] * 2), load_font)
    box = font.getbbox(p["text"])
    width = box[2] - box[0]
    height = box[3] - box[1]
    margin = min(p["margin"], max(0, (min(image.size) - 1) // 2))
    positions = {
        "top-left": (margin, margin),
        "top-right": (image.width - width - margin, margin),
        "bottom-left": (margin, image.height - height - margin),
        "bottom-right": (image.width - width - margin, image.height - height - margin),
        "center": ((image.width - width) // 2, (image.height - height) // 2),
    }
    x, y = positions[p["position"]]
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    color = ImageColor.getrgb(p["color"]) + (round(p["opacity"] * 255),)
    stroke = ImageColor.getrgb(p["stroke_color"]) + (round(p["opacity"] * 255),)
    draw.text((x - box[0], y - box[1]), p["text"], font=font, fill=color,
              stroke_width=p["stroke"], stroke_fill=stroke)
    return layer


def watermark_text(image, p, context):
    layer = _text_layer(image, p)
    return Image.alpha_composite(image, layer)


def watermark_repeat(image, p, context):
    if not p["text"]:
        raise StudioError("请输入水印文字")
    font = load_font(p["size"])
    box = font.getbbox(p["text"])
    tile_width = max(1, box[2] - box[0] + p["gap"])
    tile_height = max(1, box[3] - box[1] + p["gap"])
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    fill = ImageColor.getrgb(p["color"]) + (round(p["opacity"] * 255),)
    for row, y in enumerate(range(0, image.height, tile_height)):
        offset = -(tile_width // 2) if row % 2 else 0
        for x in range(offset, image.width, tile_width):
            draw.text((x - box[0], y - box[1]), p["text"], font=font, fill=fill)
    return Image.alpha_composite(image, layer)


def watermark_border(image, p, context):
    thickness = p["width"]
    if p["outside"]:
        size = checked_size(image.width + thickness * 2, image.height + thickness * 2, context)
        output = Image.new("RGBA", size, p["color"])
        output.alpha_composite(image, (thickness, thickness))
        return output
    output = image.copy()
    draw = ImageDraw.Draw(output)
    draw.rectangle((0, 0, image.width - 1, image.height - 1), outline=p["color"], width=thickness)
    return output


def watermark_round_corners(image, p, context):
    radius = round(min(image.size) * p["radius"] / 100)
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=radius, fill=255)
    if p["feather"]:
        mask = mask.filter(ImageFilter.GaussianBlur(p["feather"]))
    output = image.copy()
    output.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return output


def watermark_caption(image, p, context):
    band_height = p["height"]
    size = checked_size(image.width, image.height + band_height, context)
    output = Image.new("RGBA", size, p["background"])
    image_y = band_height if p["placement"] == "top" else 0
    output.alpha_composite(image, (0, image_y))
    band_y = 0 if p["placement"] == "top" else image.height
    font = fit_font(p["text"], p["size"], max(8, image.width - 32), load_font)
    box = font.getbbox(p["text"])
    x = (image.width - box[2] + box[0]) / 2
    y = band_y + (band_height - box[3] + box[1]) / 2
    draw = ImageDraw.Draw(output)
    draw.text((x - box[0], y - box[1]), p["text"], font=font, fill=p["color"])
    return output


def watermark_grid(image, p, context):
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    color = ImageColor.getrgb(p["color"]) + (round(p["opacity"] * 255),)
    for index in range(1, p["columns"]):
        x = round(image.width * index / p["columns"])
        draw.line((x, 0, x, image.height), fill=color, width=p["width"])
    for index in range(1, p["rows"]):
        y = round(image.height * index / p["rows"])
        draw.line((0, y, image.width, y), fill=color, width=p["width"])
    return Image.alpha_composite(image, layer)

