"""Validated non-destructive processing pipelines shared by UI and jobs."""
import threading
import time
from .registry import OperationContext
from .errors import StudioError
from .validation import text_value
from . import frames as frame_math
from .geometry_frames import frame_for_step, is_geometry


class ProcessingService:
    def __init__(self, settings, store, assets, registry, annotations=None):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.registry = registry
        self.annotations = annotations
        self.compute_lock = threading.Lock()
        self.context = OperationContext(settings=settings, assets=assets)

    def validate(self, steps):
        return self.registry.validate_pipeline(steps, maximum=self.settings.max_steps)

    def apply_steps(self, image, steps, progress=None, cancelled=None):
        timings = []
        frame = frame_math.IDENTITY
        warnings = []
        for index, step in enumerate(steps):
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            start = time.perf_counter()
            source = image
            image = self.registry.apply(image, step, self.context)
            if is_geometry(step["operation"]):
                contribution = frame_for_step(step, source, image)
                # Contribution maps the new canvas to the previous one; the
                # accumulated frame applies each new contribution first.
                frame = frame_math.multiply(frame, contribution)
                if step["operation"] == "transform_translate" and step["params"].get("wrap"):
                    warnings.append("循环平移不是单一投影关系，该步后的批注位置按近似处理")
            timings.append({
                "operation": step["operation"],
                "milliseconds": round((time.perf_counter() - start) * 1000, 2),
            })
            if progress:
                progress(index + 1, len(steps))
        return image, timings, {"matrix": frame_math.to_json(frame), "warnings": warnings}

    def process(self, asset_id, steps, name=None, progress=None, cancelled=None):
        steps = self.validate(steps)
        if name is not None:
            name = text_value(name, "结果名称", 180, 1)
        started = time.perf_counter()
        with self.compute_lock, self.assets.lock:
            image, parent = self.assets.load(asset_id)
            image, timings, frame_info = self.apply_steps(image, steps, progress, cancelled)
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            last = self.registry.spec(steps[-1]["operation"])
            if name is None:
                base = parent["name"].rsplit(".", 1)[0][:100]
                name = f"{base} · {last['title']}"
            parent_frame = parent.get("frame", {}).get("matrix", frame_math.IDENTITY)
            # Step frames map child canvas -> parent canvas; composing with the
            # parent's existing frame yields child canvas -> root canvas.
            composed = frame_math.multiply(parent_frame, frame_info["matrix"])
            result = self.assets.save(
                image,
                name=name,
                parent=parent,
                steps=steps,
                metadata={
                    "timings": timings,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "frame_warnings": frame_info["warnings"],
                },
                frame={"matrix": frame_math.to_json(composed),
                       "warnings": list(parent.get("frame", {}).get("warnings", [])) + frame_info["warnings"]},
            )
            if self.annotations is not None:
                self.annotations.fork(asset_id, result["id"])
        self.store.event("process", f"完成 {name}", {
            "source_id": asset_id,
            "result_id": result["id"],
            "operations": [step["operation"] for step in steps],
        })
        return result

    def preview(self, asset_id, steps):
        """Preview is ephemeral and never creates a JSON record."""
        from PIL import Image
        import io
        steps = self.validate(steps)
        with self.compute_lock:
            image, parent = self.assets.load(asset_id)
            image, timings, _frame = self.apply_steps(image, steps)
            image.thumbnail((1200, 1000), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        buffer.seek(0)
        return buffer

