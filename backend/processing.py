"""Validated non-destructive processing pipelines shared by UI and jobs."""
import hashlib
import json
import threading
import time
from .registry import OperationContext
from .errors import StudioError
from .validation import text_value


def pipeline_sha256(steps):
    payload = json.dumps(steps, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

    def apply_steps(self, image, steps, progress=None, cancelled=None):
        timings = []
        for index, step in enumerate(steps):
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            start = time.perf_counter()
            image = self.registry.apply(image, step, self.context)
            timings.append({
                "operation": step["operation"],
                "milliseconds": round((time.perf_counter() - start) * 1000, 2),
            })
            if progress:
                progress(index + 1, len(steps))
        return image, timings

    def process(self, asset_id, steps, name=None, progress=None, cancelled=None, job=None):
        steps = self.validate(steps)
        if name is not None:
            name = text_value(name, "结果名称", 180, 1)
        started = time.perf_counter()
        job_metadata = None
        if job:
            job_metadata = {
                "id": job["job_id"],
                "source_id": job["source_id"],
                "steps_sha256": pipeline_sha256(steps),
                "index": job.get("item_index"),
            }
        with self.compute_lock, self.assets.lock:
            if job_metadata:
                existing = self.assets.linked_result(
                    job_metadata["id"],
                    job_metadata["source_id"],
                    job_metadata["steps_sha256"],
                )
                if existing is not None:
                    return existing
            image, parent = self.assets.load(asset_id)
            image, timings = self.apply_steps(image, steps, progress, cancelled)
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            last = self.registry.spec(steps[-1]["operation"])
            if name is None:
                base = parent["name"].rsplit(".", 1)[0][:100]
                name = f"{base} · {last['title']}"
            metadata = {
                "timings": timings,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
            if job_metadata:
                metadata["job"] = job_metadata
            result = self.assets.save(
                image,
                name=name,
                parent=parent,
                steps=steps,
                metadata=metadata,
            )
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
            image, timings = self.apply_steps(image, steps)
            image.thumbnail((1200, 1000), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        buffer.seek(0)
        return buffer

