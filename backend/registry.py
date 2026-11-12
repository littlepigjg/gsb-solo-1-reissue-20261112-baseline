"""Single catalog powers parameter UI, API discovery and execution validation."""
import importlib
from dataclasses import dataclass
from .errors import StudioError
from .validation import object_value, reject_unknown, validate_parameters, list_value


@dataclass
class OperationContext:
    settings: object
    assets: object = None


class Registry:
    def __init__(self):
        from .catalog import CATEGORIES, OPERATIONS
        self.categories = CATEGORIES
        self.specs = {}
        self.functions = {}
        for spec in OPERATIONS:
            key = spec["id"]
            if key in self.specs:
                raise RuntimeError(f"Duplicate operation {key}")
            module = importlib.import_module(f"backend.operations.{spec['module']}")
            function = getattr(module, key)
            self.specs[key] = spec
            self.functions[key] = function
            validate_parameters(spec, {})

    def catalog(self):
        return {
            "categories": self.categories,
            "operations": list(self.specs.values()),
            "operation_count": len(self.specs),
        }

    def spec(self, operation):
        if not isinstance(operation, str) or operation not in self.specs:
            raise StudioError("未知图像处理操作", details={"operation": operation})
        return self.specs[operation]

    def validate_step(self, step):
        object_value(step, "处理步骤")
        reject_unknown(step, {"operation", "params"}, "处理步骤")
        spec = self.spec(step.get("operation"))
        params = validate_parameters(spec, step.get("params", {}))
        return {"operation": spec["id"], "params": params}

    def validate_pipeline(self, steps, maximum=12):
        list_value(steps, "处理步骤", maximum=maximum)
        return [self.validate_step(step) for step in steps]

    def apply(self, image, step, context):
        validated = self.validate_step(step)
        function = self.functions[validated["operation"]]
        result = function(image.copy(), validated["params"], context)
        from PIL import Image
        from .validation import dimension_guard
        if not isinstance(result, Image.Image):
            raise RuntimeError("Operation did not return an image")
        dimension_guard(*result.size, context.settings)
        result = result.convert("RGBA")
        result.info.clear()
        return result

