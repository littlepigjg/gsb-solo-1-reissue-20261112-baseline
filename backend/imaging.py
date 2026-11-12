"""Image decoding, bounded allocation and alpha-preserving primitives."""
import io
import warnings
import numpy as np
from PIL import Image, ImageOps, ImageColor, UnidentifiedImageError
from .errors import StudioError
from .validation import dimension_guard


FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF"}
RESAMPLING = {
    "nearest": Image.Resampling.NEAREST,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}


def decode_image(payload, settings):
    if not payload:
        raise StudioError("上传文件为空")
    if len(payload) > settings.max_upload_bytes:
        raise StudioError("图片文件过大", status=413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as source:
                if source.format not in FORMATS:
                    raise StudioError("不支持此图片格式")
                dimension_guard(*source.size, settings)
                original_format = source.format
                frames = getattr(source, "n_frames", 1)
                source.seek(0)
                source.load()
                result = ImageOps.exif_transpose(source).convert("RGBA")
                result.info.clear()
                dimension_guard(*result.size, settings)
                return result, original_format, frames
    except StudioError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise StudioError("文件不是有效图片，或图片尺寸超出限制") from error


def rgb_array(image):
    return np.asarray(image.convert("RGB"), dtype=np.float32)


def gray_array(image):
    return np.asarray(image.convert("L"), dtype=np.float32)


def from_array(array, source=None):
    output = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8)).convert("RGBA")
    if source is not None and source.size == output.size:
        output.putalpha(source.convert("RGBA").getchannel("A"))
    return output


def with_alpha(rgb, original):
    output = rgb.convert("RGBA")
    output.putalpha(original.convert("RGBA").getchannel("A"))
    return output


def blend_effect(source, effect, strength):
    if effect.size != source.size:
        raise ValueError("Effect dimensions differ")
    output = Image.blend(source.convert("RGBA"), effect.convert("RGBA"), strength)
    output.putalpha(source.convert("RGBA").getchannel("A"))
    return output


def flatten(image, background="#ffffff"):
    output = Image.new("RGBA", image.size, ImageColor.getrgb(background) + (255,))
    output.alpha_composite(image.convert("RGBA"))
    return output.convert("RGB")


def checked_size(width, height, context):
    width = int(round(width))
    height = int(round(height))
    dimension_guard(width, height, context.settings)
    return width, height


def convolution(array, kernel):
    kernel = np.asarray(kernel, dtype=np.float32)
    radius = kernel.shape[0] // 2
    if array.ndim == 3:
        padding = ((radius, radius), (radius, radius), (0, 0))
    else:
        padding = ((radius, radius), (radius, radius))
    padded = np.pad(array, padding, mode="edge")
    height, width = array.shape[:2]
    output = np.zeros_like(array, dtype=np.float32)
    for y in range(kernel.shape[0]):
        for x in range(kernel.shape[1]):
            coefficient = kernel[y, x]
            if coefficient:
                output += padded[y:y + height, x:x + width] * coefficient
    return output


def percentile_stretch(array, low=1, high=99):
    minimum, maximum = np.percentile(array, [low, high])
    if maximum <= minimum:
        return np.zeros_like(array)
    return np.clip((array - minimum) * 255.0 / (maximum - minimum), 0, 255)


def luminance(rgb):
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def smoothstep(edge0, edge1, values):
    t = np.clip((values - edge0) / max(edge1 - edge0, 1e-6), 0, 1)
    return t * t * (3 - 2 * t)


def normalized_grid(image):
    x = np.linspace(-1, 1, image.width, dtype=np.float32)
    y = np.linspace(-1, 1, image.height, dtype=np.float32)
    return np.meshgrid(x, y)


def fit_font(text, font_size, max_width, loader):
    font = loader(font_size)
    while font_size > 8:
        box = font.getbbox(text)
        if box[2] - box[0] <= max_width:
            return font
        font_size -= 1
        font = loader(font_size)
    return font

