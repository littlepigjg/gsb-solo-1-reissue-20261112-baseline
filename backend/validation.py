"""Strict JSON validation; strings and booleans are not silently numeric."""
import math
import re
from PIL import ImageColor
from .errors import StudioError


IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")


def object_value(value, label="请求"):
    if not isinstance(value, dict):
        raise StudioError(f"{label}必须是 JSON 对象")
    return value


def reject_unknown(value, allowed, label="请求"):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise StudioError(f"{label}包含未知字段", details={"fields": unknown})


def identifier(value, label="ID"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StudioError(f"{label}格式不正确")
    return value


def text_value(value, label, maximum=160, minimum=0):
    if not isinstance(value, str):
        raise StudioError(f"{label}必须是文本")
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise StudioError(f"{label}长度必须在 {minimum}–{maximum} 个字符之间")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise StudioError(f"{label}包含控制字符")
    return value


def number_value(value, label, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StudioError(f"{label}必须是数字")
    if not math.isfinite(value):
        raise StudioError(f"{label}必须是有限数字")
    if integer and value != int(value):
        raise StudioError(f"{label}必须是整数")
    if minimum is not None and value < minimum:
        raise StudioError(f"{label}不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise StudioError(f"{label}不能大于 {maximum}")
    return int(value) if integer else float(value)


def color_value(value, label="颜色"):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise StudioError(f"{label}必须是 #RRGGBB 颜色")
    ImageColor.getrgb(value)
    return value.lower()


def boolean_value(value, label):
    if type(value) is not bool:
        raise StudioError(f"{label}必须是布尔值")
    return value


def list_value(value, label, minimum=1, maximum=24):
    if not isinstance(value, list):
        raise StudioError(f"{label}必须是数组")
    if not minimum <= len(value) <= maximum:
        raise StudioError(f"{label}数量必须在 {minimum}–{maximum} 之间")
    return value


def choice_value(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise StudioError(f"{label}不在支持的选项中", details={"choices": choices})
    return value


def dimension_guard(width, height, settings):
    if min(width, height) < 1:
        raise StudioError("图片的宽度和高度必须大于零")
    if max(width, height) > settings.max_dimension:
        raise StudioError(f"图片单边不能超过 {settings.max_dimension} 像素")
    if width * height > settings.max_pixels:
        raise StudioError(f"图片不能超过 {settings.max_pixels:,} 像素")


def validate_parameter(spec, value):
    label = spec["label"]
    kind = spec["type"]
    if kind in ("integer", "number"):
        result = number_value(
            value,
            label,
            spec.get("min"),
            spec.get("max"),
            integer=kind == "integer",
        )
        if spec.get("odd") and result % 2 != 1:
            raise StudioError(f"{label}必须是奇数")
        return result
    if kind == "boolean":
        return boolean_value(value, label)
    if kind == "color":
        return color_value(value, label)
    if kind == "select":
        return choice_value(value, [item["value"] for item in spec["options"]], label)
    if kind == "text":
        return text_value(value, label, spec.get("max_length", 120))
    raise RuntimeError(f"Unknown parameter kind: {kind}")


def validate_parameters(spec, values):
    object_value(values, "参数")
    fields = spec["parameters"]
    reject_unknown(values, [field["name"] for field in fields], "参数")
    validated = {}
    for field in fields:
        name = field["name"]
        value = values.get(name, field["default"])
        validated[name] = validate_parameter(field, value)
    return validated

