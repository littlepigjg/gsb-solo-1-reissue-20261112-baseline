"""Per-operation frame contributions for geometry steps.

A frame contribution is the homography F mapping the *output* canvas of one
processing step back to its *input* canvas (child coordinates -> parent
coordinates). The asset's root-relative frame is the composition of these
contributions along the version lineage. Non-geometric operations return the
identity because content coordinates do not move.

For each operation we first write the known forward mapping q = f(p) from
input point p to output point q, then F = f^{-1}. Crop and translation are
exact affine maps; Pillow AFFINE/QUAD already consume output->input maps, so
their coefficients are used directly; free rotation and perspective are
inverted homographies.
"""
import math

from . import frames


GEOMETRY_OPERATIONS = {
    "crop_rect", "crop_center", "crop_ratio", "crop_square", "crop_circle",
    "crop_trim", "crop_alpha",
    "resize_exact", "resize_percent", "resize_fit", "resize_fill",
    "resize_pad", "resize_long_edge", "resize_canvas",
    "transform_rotate", "transform_quarter", "transform_flip",
    "transform_transpose", "transform_shear", "transform_translate",
    "transform_perspective", "transform_mirror_tile",
}


def is_geometry(operation):
    return operation in GEOMETRY_OPERATIONS


def step_frame(step, source, result):
    """Return the output->input homography for one validated step."""
    operation = step["operation"]
    params = step["params"]
    width, height = source.size
    handler = HANDLERS.get(operation)
    if handler is None:
        return frames.IDENTITY
    return handler(params, width, height, result.size)


# --------------------------------------------------------------- crops
# Forward: q = p - (left, top); inverse F: p = q + (left, top).
def crop_rect(p, width, height, result_size):
    return frames.translation(p["left"], p["top"])


def crop_center(p, width, height, result_size):
    crop_width = max(1, round(width * p["percent"] / 100))
    crop_height = max(1, round(height * p["percent"] / 100))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    return frames.translation(left, top)


def crop_ratio(p, width, height, result_size):
    ratio = p["ratio_width"] / p["ratio_height"]
    if width / height > ratio:
        crop_width = max(1, round(height * ratio))
        crop_height = height
    else:
        crop_width = width
        crop_height = max(1, round(width / ratio))
    horizontal = p["anchor_x"] / 100
    vertical = p["anchor_y"] / 100
    left = round((width - crop_width) * horizontal)
    top = round((height - crop_height) * vertical)
    return frames.translation(left, top)


def crop_square(p, width, height, result_size):
    size = min(width, height)
    anchors = {"start": 0, "center": 0.5, "end": 1}
    offset = anchors[p["anchor"]]
    left = round((width - size) * offset)
    top = round((height - size) * offset)
    return frames.translation(left, top)


def crop_circle(p, width, height, result_size):
    return crop_square({"anchor": "center"}, width, height, result_size)


def crop_trim(p, width, height, result_size):
    # Content-driven box resolved in frame_for_step with live pixels.
    return frames.IDENTITY


def crop_alpha(p, width, height, result_size):
    # Content-driven box resolved in frame_for_step with live pixels.
    return frames.IDENTITY


# -------------------------------------------------------------- resizes
# Forward: q = s * p (+ offset); F scales by 1/s and undoes the offset.
def resize_exact(p, width, height, result_size):
    out_width, out_height = result_size
    return frames.scale_matrix(width / out_width, height / out_height)


def resize_percent(p, width, height, result_size):
    factor = p["percent"] / 100
    return frames.scale_matrix(1 / factor, 1 / factor)


def resize_fit(p, width, height, result_size):
    scale = min(p["width"] / width, p["height"] / height)
    if not p["upscale"]:
        scale = min(scale, 1)
    return frames.scale_matrix(1 / scale, 1 / scale)


def resize_fill(p, width, height, result_size):
    out_width, out_height = result_size
    scale = max(out_width / width, out_height / height)
    offset_x = (width * scale - out_width) * (p["anchor_x"] / 100)
    offset_y = (height * scale - out_height) * (p["anchor_y"] / 100)
    # Forward q = s*p - o; inverse p = (q + o) / s.
    return frames.affine(
        a=1 / scale, b=0, c=offset_x / scale,
        d=0, e=1 / scale, f=offset_y / scale,
    )


def resize_pad(p, width, height, result_size):
    out_width, out_height = result_size
    scale = min(out_width / width, out_height / height)
    dx = (out_width - width * scale) / 2
    dy = (out_height - height * scale) / 2
    # Forward q = s*p + d; inverse p = (q - d) / s.
    return frames.affine(
        a=1 / scale, b=0, c=-dx / scale,
        d=0, e=1 / scale, f=-dy / scale,
    )


def resize_long_edge(p, width, height, result_size):
    factor = p["length"] / max(width, height)
    if not p["upscale"]:
        factor = min(factor, 1)
    return frames.scale_matrix(1 / factor, 1 / factor)


def resize_canvas(p, width, height, result_size):
    out_width, out_height = result_size
    dx = (out_width - width) / 2
    dy = (out_height - height) / 2
    # Forward q = p + d; inverse p = q - d.
    return frames.translation(-dx, -dy)


# ------------------------------------------------------------- transforms
def transform_rotate(p, width, height, result_size):
    out_width, out_height = result_size
    source_center = ((width - 1) / 2, (height - 1) / 2)
    if p["expand"]:
        output_center = ((out_width - 1) / 2, (out_height - 1) / 2)
        # Forward: translate to source centre, rotate, translate to output centre.
        forward = frames.translation(output_center[0], output_center[1]) @ \
            frames.rotation_about(0.0, 0.0, p["angle"]) @ \
            frames.translation(-source_center[0], -source_center[1])
    else:
        forward = frames.rotation_about(source_center[0], source_center[1], p["angle"])
    return frames.invert(forward)


def transform_quarter(p, width, height, result_size):
    angle = p["angle"]
    # Forward transposes (verified against Pillow); invert for the frame.
    if angle == "90":
        forward = frames.affine(a=0, b=1, c=0, d=-1, e=0, f=width - 1)
    elif angle == "180":
        forward = frames.affine(a=-1, b=0, c=width - 1, d=0, e=-1, f=height - 1)
    else:
        forward = frames.affine(a=0, b=-1, c=height - 1, d=1, e=0, f=0)
    return frames.invert(forward)


def transform_flip(p, width, height, result_size):
    # Reflections are self-inverse.
    if p["axis"] == "horizontal":
        return frames.affine(a=-1, b=0, c=width - 1, d=0, e=1, f=0)
    if p["axis"] == "vertical":
        return frames.affine(a=1, b=0, c=0, d=0, e=-1, f=height - 1)
    return frames.affine(a=-1, b=0, c=width - 1, d=0, e=-1, f=height - 1)


def transform_transpose(p, width, height, result_size):
    if p["diagonal"] == "main":
        # Main-diagonal swap is self-inverse.
        return frames.affine(a=0, b=1, c=0, d=1, e=0, f=0)
    forward = frames.affine(a=0, b=-1, c=height - 1, d=-1, e=0, f=width - 1)
    return frames.invert(forward)


def transform_shear(p, width, height, result_size):
    horizontal = p["horizontal"] / 100
    vertical = p["vertical"] / 100
    # Pillow's AFFINE tuple already maps output q -> input p, i.e. the frame.
    return frames.affine(
        1, horizontal, -horizontal * height / 2,
        vertical, 1, -vertical * width / 2,
    )


def transform_translate(p, width, height, result_size):
    dx = round(width * p["x"] / 100)
    dy = round(height * p["y"] / 100)
    if p["wrap"]:
        # Wrapped offsets have no single-valued projective mapping.
        return frames.IDENTITY
    # Forward q = p + (dx, dy); inverse shifts back.
    return frames.translation(-dx, -dy)


def transform_perspective(p, width, height, result_size):
    amount = p["amount"] / 100
    delta = abs(amount) * width * 0.4
    if amount >= 0:
        quad = (delta, 0, 0, height, width, height, width - delta, 0)
    else:
        quad = (0, 0, delta, height, width - delta, height, width, 0)
    return _quad_frame(quad, (width, height))


def _quad_frame(quad, size):
    """Output->source homography consistent with Pillow's own QUAD sampling.

    Pillow implements QUAD through an internal mesh rather than a textbook
    homography, so deriving coefficients analytically drifts from the real
    pixels. Instead we transform coordinate maps (every pixel stores its own
    x/y index) with the identical QUAD call and fit the projective map to the
    sampled correspondences; the residual is below the annotation stroke width.
    """
    import numpy as np
    from PIL import Image
    from . import frames as frame_math
    width, height = size
    x_grid = np.tile(np.arange(width, dtype=np.float32), (height, 1))
    y_grid = np.tile(np.arange(height, dtype=np.float32)[:, None], (1, width))
    map_x = Image.fromarray(x_grid, mode="F").transform(
        size, Image.Transform.QUAD, quad, resample=Image.Resampling.NEAREST)
    map_y = Image.fromarray(y_grid, mode="F").transform(
        size, Image.Transform.QUAD, quad, resample=Image.Resampling.NEAREST)
    sampled_x = np.asarray(map_x)
    sampled_y = np.asarray(map_y)
    step_x = max(1, width // 16)
    step_y = max(1, height // 16)
    output_points = []
    source_points = []
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            output_points.append((float(x), float(y)))
            source_points.append((float(sampled_x[y, x]), float(sampled_y[y, x])))
    return frame_math._fit_homography(
        np.asarray(output_points, dtype=np.float64),
        np.asarray(source_points, dtype=np.float64),
    )


def transform_mirror_tile(p, width, height, result_size):
    if p["keep_size"]:
        # Forward q = p / 2 after the tile is shrunk back; inverse doubles.
        return frames.scale_matrix(2, 2)
    return frames.IDENTITY


HANDLERS = {
    "crop_rect": crop_rect,
    "crop_center": crop_center,
    "crop_ratio": crop_ratio,
    "crop_square": crop_square,
    "crop_circle": crop_circle,
    "crop_trim": crop_trim,
    "crop_alpha": crop_alpha,
    "resize_exact": resize_exact,
    "resize_percent": resize_percent,
    "resize_fit": resize_fit,
    "resize_fill": resize_fill,
    "resize_pad": resize_pad,
    "resize_long_edge": resize_long_edge,
    "resize_canvas": resize_canvas,
    "transform_rotate": transform_rotate,
    "transform_quarter": transform_quarter,
    "transform_flip": transform_flip,
    "transform_transpose": transform_transpose,
    "transform_shear": transform_shear,
    "transform_translate": transform_translate,
    "transform_perspective": transform_perspective,
    "transform_mirror_tile": transform_mirror_tile,
}


def frame_for_step(step, source_image, result_image):
    """Resolve a step's frame, using live pixels for content-driven crops."""
    operation = step["operation"]
    if operation in {"crop_trim", "crop_alpha"}:
        return _content_crop_frame(operation, step["params"], source_image)
    return step_frame(step, source_image, result_image)


def _content_crop_frame(operation, params, source):
    from PIL import Image, ImageChops
    if operation == "crop_alpha":
        mask = source.getchannel("A").point(lambda value: 255 if value > params["threshold"] else 0)
        box = mask.getbbox()
        left, top = box[0], box[1]
    else:
        background = Image.new("RGB", source.size, params["background"])
        difference = ImageChops.difference(source.convert("RGB"), background)
        channels = difference.split()
        difference = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        mask = difference.point(lambda value: 255 if value > params["tolerance"] else 0)
        box = mask.getbbox()
        padding = params["padding"]
        left = max(0, box[0] - padding)
        top = max(0, box[1] - padding)
    return frames.translation(left, top)
