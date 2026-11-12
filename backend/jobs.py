"""Bounded background jobs with durable state, cancellation and recovery."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from .errors import StudioError, Conflict
from .validation import identifier, list_value, text_value, object_value, reject_unknown
from .dynamic import normalize_snapshot, verify_snapshot
from .storage import utc_now


LOGGER = logging.getLogger(__name__)
ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


class JobService:
    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="studio-job")
        self.cancellations = {}
        self.futures = {}
        for job in self.store.all("jobs"):
            if job["status"] in ACTIVE:
                self.store.update("jobs", job["id"], {
                    "status": "interrupted",
                    "finished_at": utc_now(),
                    "message": "服务重启中断了任务；已完成的结果仍然保留，可重新提交。",
                })

    def submit(self, asset_ids, steps, title="批量图像处理", snapshots=None):
        asset_ids = list_value(asset_ids, "图片列表", maximum=self.settings.max_batch)
        asset_ids = list(dict.fromkeys(identifier(value) for value in asset_ids))
        steps = self.processing.validate(steps)
        title = text_value(title, "任务名称", 100, 1)
        with self.assets.lock, self.lock:
            for asset_id in asset_ids:
                self.assets.get(asset_id)
            if snapshots is None:
                snapshots = self.processing.snapshots(asset_ids)
            else:
                object_value(snapshots, "素材快照")
                reject_unknown(snapshots, set(asset_ids), "素材快照")
                snapshots = {
                    asset_id: verify_snapshot(normalize_snapshot(snapshots[asset_id]), self.assets.get(asset_id))
                    for asset_id in asset_ids
                }
            active = [job for job in self.store.all("jobs") if job["status"] in ACTIVE]
            if len(active) >= 4:
                raise Conflict("最多允许 4 个等待或运行中的任务")
            record = self.store.insert("jobs", {
                "title": title,
                "asset_ids": asset_ids,
                "steps": steps,
                "snapshots": snapshots,
                "status": "queued",
                "total": len(asset_ids),
                "done": 0,
                "progress": 0,
                "results": [],
                "errors": [],
                "current_asset_id": None,
                "started_at": None,
                "finished_at": None,
                "message": "等待处理",
            })
            cancellation = threading.Event()
            self.cancellations[record["id"]] = cancellation
            self.futures[record["id"]] = self.executor.submit(self._run, record["id"], cancellation)
            return record

    def _run(self, job_id, cancellation):
        try:
            self._execute(job_id, cancellation)
        except Exception:
            LOGGER.exception("Batch job failed: %s", job_id)
            self.store.update("jobs", job_id, {
                "status": "failed",
                "finished_at": utc_now(),
                "message": "任务异常结束，请查看服务日志。",
            })
        finally:
            with self.lock:
                self.cancellations.pop(job_id, None)
                self.futures.pop(job_id, None)

    def _execute(self, job_id, cancellation):
        record = self.store.get("jobs", job_id)
        results = []
        errors = []
        self.store.update("jobs", job_id, {
            "status": "running", "started_at": utc_now(), "message": "正在处理",
        })
        for index, asset_id in enumerate(record["asset_ids"]):
            if cancellation.is_set():
                break
            self.store.update("jobs", job_id, {"current_asset_id": asset_id})

            def progress(completed, total):
                percentage = (index + completed / total) / record["total"] * 100
                self.store.update("jobs", job_id, {"progress": round(percentage, 1)})
            try:
                result = self.processing.process(
                    asset_id,
                    record["steps"],
                    snapshot=(record.get("snapshots") or {}).get(asset_id),
                    progress=progress,
                    cancelled=cancellation.is_set,
                )
                results.append({"source_id": asset_id, "asset_id": result["id"], "name": result["name"]})
            except StudioError as error:
                if error.code == "cancelled":
                    break
                errors.append({"asset_id": asset_id, **error.as_dict()})
            except Exception:
                LOGGER.exception("Batch item failed: %s", asset_id)
                errors.append({"asset_id": asset_id, "code": "processing_error", "message": "处理失败，请检查日志。"})
            self.store.update("jobs", job_id, {
                "results": results,
                "errors": errors,
                "done": index + 1,
                "progress": round((index + 1) / record["total"] * 100, 1),
            })
        if cancellation.is_set():
            status = "cancelled"
            message = "已取消；已完成的处理副本仍然保留。"
        elif errors:
            status = "partial" if results else "failed"
            message = f"成功 {len(results)} 张，失败 {len(errors)} 张"
        else:
            status = "completed"
            message = f"已完成 {len(results)} 张图片"
        self.store.update("jobs", job_id, {
            "status": status,
            "message": message,
            "results": results,
            "errors": errors,
            "finished_at": utc_now(),
            "current_asset_id": None,
        })
        self.store.event("batch", message, {"job_id": job_id})

    def get(self, job_id):
        identifier(job_id)
        return self.store.get("jobs", job_id)

    def list(self):
        records = self.store.all("jobs")
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return records[:100]

    def cancel(self, job_id):
        with self.lock:
            record = self.get(job_id)
            if record["status"] in TERMINAL:
                return record
            flag = self.cancellations.get(job_id)
            if flag:
                flag.set()
            return self.store.update("jobs", job_id, {
                "status": "cancelling", "message": "将在当前操作结束后取消",
            })

    def shutdown(self):
        with self.lock:
            for flag in self.cancellations.values():
                flag.set()
        self.executor.shutdown(wait=True, cancel_futures=False)

