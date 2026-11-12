"""Reusable JSON processing recipes with built-in examples."""
from .errors import StudioError
from .validation import object_value, reject_unknown, text_value, identifier


BUILTINS = [
    {
        "key": "film",
        "name": "温暖胶片",
        "description": "微暖色调、胶片颗粒与柔和暗角。",
        "steps": [
            {"operation": "color_temperature", "params": {"temperature": 12, "tint": 3}},
            {"operation": "filter_vintage", "params": {"strength": 0.7, "saturation": 0.85, "vignette": 0.4}},
            {"operation": "filter_grain", "params": {"amount": 5, "seed": 42, "monochrome": True}},
        ],
    },
    {
        "key": "social",
        "name": "社交方图",
        "description": "中心方形裁剪、1080 像素输出与轻度锐化。",
        "steps": [
            {"operation": "crop_square", "params": {"anchor": "center"}},
            {"operation": "resize_exact", "params": {"width": 1080, "height": 1080, "resampling": "lanczos"}},
            {"operation": "enhance_unsharp", "params": {"radius": 1.2, "percent": 110, "threshold": 3}},
        ],
    },
    {
        "key": "mono",
        "name": "黑白叙事",
        "description": "感知灰度、适度反差和细腻颗粒。",
        "steps": [
            {"operation": "color_grayscale", "params": {"method": "luminance", "strength": 1}},
            {"operation": "color_contrast", "params": {"factor": 1.25}},
            {"operation": "filter_grain", "params": {"amount": 7, "seed": 12, "monochrome": True}},
        ],
    },
    {
        "key": "web",
        "name": "网页轻量图",
        "description": "限制最长边，轻度锐化，适合后续 WebP 导出。",
        "steps": [
            {"operation": "resize_long_edge", "params": {"length": 1600, "upscale": False}},
            {"operation": "enhance_sharpness", "params": {"factor": 1.2}},
        ],
    },
]


class PresetService:
    def __init__(self, store, registry):
        self.store = store
        self.registry = registry
        existing = {item.get("builtin_key") for item in store.all("presets")}
        for preset in BUILTINS:
            if preset["key"] not in existing:
                steps = registry.validate_pipeline(preset["steps"])
                store.insert("presets", {
                    "name": preset["name"],
                    "description": preset["description"],
                    "steps": steps,
                    "builtin_key": preset["key"],
                    "readonly": True,
                })

    def list(self):
        records = self.store.all("presets")
        records.sort(key=lambda item: (not item.get("readonly", False), item["created_at"]))
        return records

    def create(self, payload):
        object_value(payload)
        reject_unknown(payload, {"name", "description", "steps"})
        record = {
            "name": text_value(payload.get("name"), "预设名称", 80, 1),
            "description": text_value(payload.get("description", ""), "预设说明", 300),
            "steps": self.registry.validate_pipeline(payload.get("steps")),
            "readonly": False,
            "builtin_key": None,
        }
        if len(self.store.all("presets")) >= 100:
            raise StudioError("预设数量已达 100 个，请先删除不用的预设")
        return self.store.insert("presets", record)

    def update(self, preset_id, payload):
        identifier(preset_id)
        record = self.store.get("presets", preset_id)
        if record.get("readonly"):
            raise StudioError("内置预设不能修改，请保存为新预设")
        reject_unknown(payload, {"name", "description", "steps"})
        changes = {}
        if "name" in payload:
            changes["name"] = text_value(payload["name"], "名称", 80, 1)
        if "description" in payload:
            changes["description"] = text_value(payload["description"], "说明", 300)
        if "steps" in payload:
            changes["steps"] = self.registry.validate_pipeline(payload["steps"])
        return self.store.update("presets", preset_id, changes)

    def delete(self, preset_id):
        identifier(preset_id)
        record = self.store.get("presets", preset_id)
        if record.get("readonly"):
            raise StudioError("内置预设不能删除")
        self.store.delete("presets", preset_id)

