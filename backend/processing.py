"""Validated non-destructive processing pipelines shared by UI and jobs."""
import threading
import time
from .registry import OperationContext
from .errors import StudioError
from .validation import text_value, object_value, reject_unknown
from .dynamic import snapshot_from_record, step_context, normalize_snapshot, verify_snapshot


class ProcessingService:
    def __init__(self, settings, store, assets, registry):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.registry = registry
        self.compute_lock = threading.Lock()
        self.context = OperationContext(settings=settings, assets=assets)

    def validate(self, steps):
        return self.registry.validate_pipeline(steps, maximum=self.settings.max_steps)

    def snapshots(self, asset_ids):
        """Capture the rule-relevant metadata of each source at submit time."""
        result = {}
        with self.assets.lock:
            for asset_id in asset_ids:
                result[asset_id] = snapshot_from_record(self.assets.get(asset_id))
        return result

    def resolve_pipeline(self, steps, width, height, snapshot=None):
        """Resolve every dynamic step against the size entering that step."""
        resolved = []
        reports = []
        for index, step in enumerate(steps):
            step, report = self.registry.resolved_step(
                step, step_context(width, height, snapshot), index,
            )
            resolved.append(step)
            reports.append(report)
            # The next step sees the size this step will produce. Size-changing
            # operations are modelled exactly for the operations that define
            # their output size; other steps keep the same dimensions and the
            # authoritative check still happens after execution.
            width, height = predict_size(step, width, height)
        return resolved, reports

    def apply_steps(self, image, steps, snapshot=None, progress=None, cancelled=None):
        timings = []
        reports = []
        for index, step in enumerate(steps):
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            resolved, report = self.registry.resolved_step(
                step, step_context(image.width, image.height, snapshot), index,
            )
            reports.append(report)
            start = time.perf_counter()
            image = self.registry.apply(image, resolved, self.context)
            timings.append({
                "operation": step["operation"],
                "milliseconds": round((time.perf_counter() - start) * 1000, 2),
            })
            if progress:
                progress(index + 1, len(steps))
        return image, timings, reports

    def process(self, asset_id, steps, name=None, progress=None, cancelled=None, snapshot=None):
        steps = self.validate(steps)
        if name is not None:
            name = text_value(name, "结果名称", 180, 1)
        started = time.perf_counter()
        with self.compute_lock, self.assets.lock:
            image, parent = self.assets.load(asset_id)
            if snapshot is not None:
                snapshot = verify_snapshot(normalize_snapshot(snapshot), parent)
            else:
                snapshot = snapshot_from_record(parent)
            image, timings, reports = self.apply_steps(image, steps, snapshot, progress, cancelled)
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            last = self.registry.spec(steps[-1]["operation"])
            if name is None:
                base = parent["name"].rsplit(".", 1)[0][:100]
                name = f"{base} · {last['title']}"
            result = self.assets.save(
                image,
                name=name,
                parent=parent,
                steps=steps,
                metadata={
                    "timings": timings,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "dynamic": dynamic_summary(reports),
                    "source_snapshot": snapshot,
                },
            )
        self.store.event("process", f"完成 {name}", {
            "source_id": asset_id,
            "result_id": result["id"],
            "operations": [step["operation"] for step in steps],
            "dynamic_steps": sum(1 for item in reports if item["matched_rules"]),
        })
        return result

    def preview(self, asset_id, steps, snapshot=None):
        """Preview is ephemeral and never creates a JSON record."""
        from PIL import Image
        import io
        steps = self.validate(steps)
        with self.compute_lock:
            image, parent = self.assets.load(asset_id)
            if snapshot is not None:
                snapshot = verify_snapshot(normalize_snapshot(snapshot), parent)
            else:
                snapshot = snapshot_from_record(parent)
            image, timings, reports = self.apply_steps(image, steps, snapshot)
            image.thumbnail((1200, 1000), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        buffer.seek(0)
        return buffer, dynamic_summary(reports)

    def resolve_report(self, asset_id, steps, snapshot=None):
        """Return the size plan and which rules would fire, without rendering."""
        steps = self.validate(steps)
        image, parent = self.assets.load(asset_id)
        if snapshot is not None:
            snapshot = verify_snapshot(normalize_snapshot(snapshot), parent)
        else:
            snapshot = snapshot_from_record(parent)
        resolved, reports = self.resolve_pipeline(steps, image.width, image.height, snapshot)
        width, height = image.width, image.height
        for step in resolved:
            width, height = predict_size(step, width, height)
        return {
            "asset_id": asset_id,
            "source": snapshot,
            "steps": reports,
            "output_size": {"width": width, "height": height},
        }


def predict_size(step, width, height):
    """Best-effort output size for the next step's context.

    Only exact, well-understood cases are modelled; anything else is left
    unchanged and the real size is re-read after execution.
    """
    operation = step["operation"]
    params = step["params"]
    if operation in {"resize_exact", "resize_fill", "resize_pad", "resize_canvas"}:
        return int(params["width"]), int(params["height"])
    if operation == "resize_percent":
        factor = params["percent"] / 100
        return max(1, round(width * factor)), max(1, round(height * factor))
    if operation == "resize_fit":
        scale = min(params["width"] / width, params["height"] / height)
        if not params["upscale"]:
            scale = min(scale, 1)
        return max(1, round(width * scale)), max(1, round(height * scale))
    if operation == "resize_long_edge":
        factor = params["length"] / max(width, height)
        if not params["upscale"]:
            factor = min(factor, 1)
        return max(1, round(width * factor)), max(1, round(height * factor))
    if operation == "crop_rect":
        left = int(params["left"])
        top = int(params["top"])
        right = min(width, left + int(params["width"]))
        bottom = min(height, top + int(params["height"]))
        return max(1, right - left), max(1, bottom - top)
    if operation == "crop_center":
        return (
            max(1, round(width * params["percent"] / 100)),
            max(1, round(height * params["percent"] / 100)),
        )
    if operation == "crop_square":
        size = min(width, height)
        return size, size
    if operation == "crop_ratio":
        return _ratio_size(params, width, height)
    if operation == "transform_quarter":
        return (height, width) if str(params["angle"]) in {"90", "270"} else (width, height)
    if operation == "transform_transpose":
        return height, width
    if operation == "watermark_border" and params["outside"]:
        thickness = int(params["width"])
        return width + thickness * 2, height + thickness * 2
    if operation == "watermark_caption":
        return width, height + int(params["height"])
    return width, height


def _ratio_size(params, width, height):
    ratio = params["ratio_width"] / params["ratio_height"]
    if width / height > ratio:
        return max(1, round(height * ratio)), height
    return width, max(1, round(width / ratio))


def dynamic_summary(reports):
    if not reports:
        return []
    return [
        {
            "step": item["index"],
            "operation": item["operation"],
            "matched_rules": item["matched_rules"],
        }
        for item in reports
        if item["matched_rules"]
    ]
