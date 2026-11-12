"""Rasterise review markers onto an exported image.

Markers are kept as vector-style overlays drawn in the exported asset's own
canvas coordinates. The annotation service projects root-space markers into
that canvas first, including an ``inside/partial/outside`` visibility flag so
content cropped out of the frame is rendered with a distinct dashed style
rather than silently disappearing.
"""
from PIL import Image, ImageDraw, ImageFont, ImageColor
from .operations.watermark import load_font


def _with_alpha(color, alpha=255):
    return ImageColor.getrgb(color) + (alpha,)


def _line_width(base, image):
    return max(1, min(base, round(max(image.size) / 220)))


def _label_box(draw, text, font, anchor, image_width):
    if not text:
        return None
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0] + 12
    height = box[3] - box[1] + 8
    x = min(max(anchor[0] + 8, 2), max(2, image_width - width - 2))
    y = max(anchor[1] + 8, 2)
    return (x, y, x + width, y + height), (x + 6 - box[0], y + 4 - box[1])


def render_annotations(image, view_markers):
    """Return a new RGBA image with markers composited on top."""
    canvas = image.convert("RGBA").copy()
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(max(12, round(max(canvas.size) / 42)))
    for marker in view_markers:
        geometry = marker["geometry"]
        color = marker.get("color", "#d9482b")
        width = _line_width(marker.get("line_width", 3), canvas)
        visibility = marker.get("visibility", "inside")
        dashed = visibility == "outside"
        alpha = 255 if visibility != "outside" else 150
        fill = _with_alpha(color, alpha)
        if marker["kind"] in {"rect", "ellipse"} and "corners" in geometry:
            points = geometry["corners"]
            if marker["kind"] == "rect":
                _polygon(draw, points, fill, width, dashed)
            else:
                draw.ellipse(_bounds(points), outline=fill, width=width)
        elif "points" in geometry:
            points = geometry["points"]
            if marker["kind"] == "arrow":
                _arrow(draw, points[0], points[-1], fill, width)
            else:
                _polyline(draw, points, fill, width, dashed)
        if "anchor" in geometry:
            anchor = geometry["anchor"]
            r = width + 3
            draw.ellipse((anchor[0] - r, anchor[1] - r, anchor[0] + r, anchor[1] + r),
                         outline=fill, width=width, fill=_with_alpha(color, 60))
        label = marker.get("label")
        anchor = geometry.get("anchor") or (geometry.get("points") or [[0, 0]])[0]
        if "x" in geometry:
            anchor = (geometry["x"], geometry["y"])
        label_geometry = _label_box(draw, label, font, anchor, canvas.width)
        if label_geometry:
            box, text_xy = label_geometry
            draw.rounded_rectangle(box, radius=5, fill=(255, 255, 255, 225), outline=fill, width=max(1, width - 1))
            draw.text(text_xy, label, font=font, fill=(40, 52, 42, 255))
    return Image.alpha_composite(canvas, overlay)


def _bounds(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _polyline(draw, points, fill, width, dashed):
    if not dashed:
        draw.line(points, fill=fill, width=width, joint="curve")
        return
    for start, end in zip(points, points[1:]):
        _dashed_line(draw, start, end, fill, width)


def _polygon(draw, points, fill, width, dashed):
    if not dashed:
        draw.line(points + [points[0]], fill=fill, width=width, joint="curve")
        return
    ring = points + [points[0]]
    for start, end in zip(ring, ring[1:]):
        _dashed_line(draw, start, end, fill, width)


def _dashed_line(draw, start, end, fill, width, dash=9, gap=6):
    import math
    sx, sy = start
    ex, ey = end
    distance = math.hypot(ex - sx, ey - sy)
    if distance == 0:
        return
    dx, dy = (ex - sx) / distance, (ey - sy) / distance
    travelled = 0
    while travelled < distance:
        segment_end = min(travelled + dash, distance)
        draw.line((sx + dx * travelled, sy + dy * travelled,
                   sx + dx * segment_end, sy + dy * segment_end), fill=fill, width=width)
        travelled += dash + gap


def _arrow(draw, start, end, fill, width):
    import math
    draw.line((start, end), fill=fill, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    head = max(8, width * 3)
    points = [
        end,
        (end[0] - head * math.cos(angle - 0.45), end[1] - head * math.sin(angle - 0.45)),
        (end[0] - head * math.cos(angle + 0.45), end[1] - head * math.sin(angle + 0.45)),
    ]
    draw.polygon(points, fill=fill)
