"""Bounded background jobs with durable, verifiable per-item state.

Each batch keeps one record per source image. A result asset carries in
`metadata.job` the batch id, source id and pipeline fingerprint that produced
it, so recovery never guesses from a file name, a timestamp, or a neighbouring
batch. Startup reconciliation adopts outputs that were saved just before an
abrupt exit, leaves genuinely missing items pending, and never republishes an
output that is already in the library.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from .errors import StudioError, Conflict
from .processing import pipeline_sha256
from .validation import identifier, list_value, text_value
from .storage import utc_now


LOGGER = logging.getLogger(__name__)
ACTIVE = {"queued", "running", "cancelling"}
RESUMABLE = {"interrupted", "partial", "failed"}
ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_SUCCESS = "success"
ITEM_FAILED = "failed"
ITEM_UNKNOWN = "unknown"


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
        self.shutdown_event = threading.Event()
        self._recover_after_restart()

    # ------------------------------------------------------------------ submit
    def _new_items(self, asset_ids):
        return [
            {
                "source_id": asset_id,
                "index": index,
                "status": ITEM_PENDING,
                "result_id": None,
                "result_name": None,
                "error": None,
                "attempts": 0,
            }
            for index, asset_id in enumerate(asset_ids)
        ]

    def submit(self, asset_ids, steps, title="批量图像处理"):
        asset_ids = list_value(asset_ids, "图片列表", maximum=self.settings.max_batch)
        asset_ids = list(dict.fromkeys(identifier(value) for value in asset_ids))
        steps = self.processing.validate(steps)
        title = text_value(title, "任务名称", 100, 1)
        with self.assets.lock, self.lock:
            for asset_id in asset_ids:
                self.assets.get(asset_id)
            active = [job for job in self.store.all("jobs") if job["status"] in ACTIVE]
            if len(active) >= 4:
                raise Conflict("最多允许 4 个等待或运行中的任务")
            record = self.store.insert("jobs", {
                "title": title,
                "asset_ids": asset_ids,
                "steps": steps,
                "status": "queued",
                "total": len(asset_ids),
                "done": 0,
                "progress": 0,
                "results": [],
                "errors": [],
                "items": self._new_items(asset_ids),
                "succeeded": 0,
                "failed": 0,
                "pending": len(asset_ids),
                "remaining": len(asset_ids),
                "resumable": False,
                "recovery_state": "clean",
                "recovery_message": "",
                "recovery": {"state": "clean", "message": "", "checked_at": None},
                "resume_count": 0,
                "current_asset_id": None,
                "started_at": None,
                "finished_at": None,
                "message": "等待处理",
            })
            cancellation = threading.Event()
            self.cancellations[record["id"]] = cancellation
            self.futures[record["id"]] = self.executor.submit(self._run, record["id"], cancellation)
            return record

    # ------------------------------------------------------------------- worker
    def _run(self, job_id, cancellation):
        try:
            self._execute(job_id, cancellation)
        except Exception:
            LOGGER.exception("Batch job failed: %s", job_id)
            try:
                current = self.store.get("jobs", job_id)
                if self._stopping_unless_user_cancelled(job_id):
                    self._finish_interrupted(job_id, current, current.get("items") or [])
                elif cancellation.is_set() or current.get("status") == "cancelling":
                    self._finish_cancelled(job_id)
                else:
                    self._reconcile(current, "exception")
            except Exception:
                LOGGER.exception("Batch job recovery failed: %s", job_id)
        finally:
            with self.lock:
                self.cancellations.pop(job_id, None)
                self.futures.pop(job_id, None)

    def _execute(self, job_id, cancellation):
        record = self.store.get("jobs", job_id)
        items = record.get("items")
        if not isinstance(items, list) or not items:
            items = self._new_items(record.get("asset_ids") or [])
        total = record.get("total") or len(items)
        self.store.update("jobs", job_id, {
            "status": "cancelling" if record.get("status") == "cancelling" else "running",
            "started_at": record.get("started_at") or utc_now(),
            "current_asset_id": None,
            "message": "正在取消" if record.get("status") == "cancelling" else "正在处理",
            "finished_at": None,
            "resumable": False,
        })
        for index, item in enumerate(items):
            if cancellation.is_set():
                break
            if item.get("status") in {ITEM_SUCCESS, ITEM_UNKNOWN}:
                continue
            item["status"] = ITEM_RUNNING
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["error"] = None
            self.store.update("jobs", job_id, {
                "items": items,
                "current_asset_id": item["source_id"],
                "message": f"正在处理第 {index + 1}/{total} 张",
            })

            def progress(completed, steps_total, item_index=index):
                if not steps_total:
                    return
                percentage = (item_index + completed / steps_total) / total * 100 if total else 0
                self.store.update("jobs", job_id, {
                    "progress": round(min(percentage, 99.9), 1),
                })

            try:
                result = self.processing.process(
                    item["source_id"],
                    record["steps"],
                    progress=progress,
                    cancelled=cancellation.is_set,
                    job={"job_id": job_id, "source_id": item["source_id"], "item_index": index},
                )
            except StudioError as error:
                if error.code == "cancelled":
                    item["status"] = ITEM_PENDING
                    item["error"] = None
                    self.store.update("jobs", job_id, {"items": items})
                    break
                if error.code == "conflict":
                    # Duplicate outputs already exist for this source; do not pick
                    # one and do not publish another. Leave it for a human.
                    item["status"] = ITEM_UNKNOWN
                    item["error"] = error.as_dict()
                    self.store.update("jobs", job_id, self._snapshot(items, record))
                    break
                item["status"] = ITEM_FAILED
                item["error"] = error.as_dict()
            except Exception:
                LOGGER.exception("Batch item failed: %s", item["source_id"])
                item["status"] = ITEM_FAILED
                item["error"] = {
                    "code": "processing_error",
                    "message": "处理失败，请检查日志。",
                    "details": {},
                }
            else:
                item["status"] = ITEM_SUCCESS
                item["result_id"] = result["id"]
                item["result_name"] = result.get("name")

            self.store.update("jobs", job_id, self._snapshot(items, record))

        if cancellation.is_set():
            if self._stopping_unless_user_cancelled(job_id):
                self._finish_interrupted(job_id, record, items)
            else:
                self._finish_cancelled(job_id)
        else:
            self._finalize(job_id, record, items)

    def _stopping_unless_user_cancelled(self, job_id):
        """True when the stop came from service shutdown, not a user cancel."""
        if not self.shutdown_event.is_set():
            return False
        current = self.store.get("jobs", job_id)
        return current.get("status") != "cancelling"

    def _finish_interrupted(self, job_id, record, items):
        changes = self._snapshot(items, record)
        pending = changes["pending"]
        unknown = changes["unknown"]
        if pending:
            message = (
                f"服务停止中断了任务：成功 {changes['succeeded']} 张，失败 {changes['failed']} 张，"
                f"未完成 {pending} 张；重启后可继续处理剩余图片。"
            )
        elif unknown:
            message = (
                f"服务停止中断了任务：成功 {changes['succeeded']} 张，失败 {changes['failed']} 张，"
                f"无法确认 {unknown} 张。无法确认的图片不会自动重做，请先核对素材库。"
            )
        else:
            message = (
                f"服务停止中断了任务：成功 {changes['succeeded']} 张，"
                f"失败 {changes['failed']} 张；重启后可重新提交失败项。"
            )
        state = "conflict" if unknown else "reconciled"
        changes.update({
            "status": "interrupted",
            "message": message,
            "resumable": bool(pending) and not unknown,
            "recovery_state": state,
            "recovery_message": message if unknown else "",
            "recovery": {"state": state, "message": message if unknown else "", "checked_at": utc_now()},
            "finished_at": utc_now(),
            "current_asset_id": None,
        })
        self.store.update("jobs", job_id, changes)
        self.store.event("batch", message, {"job_id": job_id})

    def _finish_cancelled(self, job_id):
        record = self.store.get("jobs", job_id)
        items = record.get("items") or []
        changes = self._snapshot(items, record)
        changes.update({
            "status": "cancelled",
            "message": "已取消；已完成的处理副本仍然保留，未完成图片不会自动继续。",
            "resumable": False,
            "recovery_state": "clean",
            "recovery_message": "",
            "recovery": {"state": "clean", "message": "", "checked_at": utc_now()},
            "finished_at": utc_now(),
            "current_asset_id": None,
        })
        self.store.update("jobs", job_id, changes)
        self.store.event("batch", changes["message"], {"job_id": job_id})

    def _finalize(self, job_id, record, items):
        changes = self._snapshot(items, record)
        pending = changes["pending"]
        failures = changes["failed"]
        unknown = changes["unknown"]
        successes = changes["succeeded"]
        if pending:
            status = "interrupted"
            message = f"成功 {successes} 张，失败 {failures} 张，未完成 {pending} 张；可继续处理剩余图片。"
        elif unknown:
            status = "interrupted"
            message = (
                f"成功 {successes} 张，失败 {failures} 张，无法确认 {unknown} 张。"
                "无法确认的图片不会自动重做，请先核对素材库。"
            )
        elif failures:
            status = "partial" if successes else "failed"
            message = f"成功 {successes} 张，失败 {failures} 张；可重试失败项。"
        else:
            status = "completed"
            message = f"已完成 {successes} 张图片"
        state = "conflict" if unknown else "clean"
        changes.update({
            "status": status,
            "message": message,
            "resumable": status in RESUMABLE and not unknown,
            "recovery_state": state,
            "recovery_message": message if unknown else "",
            "recovery": {"state": state, "message": message if unknown else "", "checked_at": utc_now()},
            "finished_at": utc_now(),
            "current_asset_id": None,
        })
        self.store.update("jobs", job_id, changes)
        self.store.event("batch", message, {"job_id": job_id})

    # --------------------------------------------------------------- accounting
    def _snapshot(self, items, record):
        total = record.get("total") or len(record.get("asset_ids") or []) or len(items)
        results = []
        errors = []
        succeeded = failed = unknown = pending = 0
        for item in items:
            status = item.get("status")
            if status == ITEM_SUCCESS and item.get("result_id"):
                succeeded += 1
                results.append({
                    "source_id": item["source_id"],
                    "asset_id": item["result_id"],
                    "name": item.get("result_name"),
                })
            elif status == ITEM_FAILED:
                failed += 1
                error = item.get("error") or {
                    "code": "processing_error",
                    "message": "处理失败，请检查日志。",
                    "details": {},
                }
                errors.append({"asset_id": item["source_id"], **error})
            elif status == ITEM_UNKNOWN:
                unknown += 1
                error = item.get("error") or {
                    "code": "unverified",
                    "message": "无法确认这张图片的处理结果。",
                    "details": {},
                }
                errors.append({"asset_id": item["source_id"], **error})
            else:
                pending += 1
        done = succeeded + failed + unknown
        percentage = round(done / total * 100, 1) if total else 0
        return {
            "items": items,
            "results": results,
            "errors": errors,
            "succeeded": succeeded,
            "failed": failed,
            "unknown": unknown,
            "pending": pending,
            "remaining": pending,
            "done": done,
            "progress": percentage,
        }

    # ---------------------------------------------------------------- recovery
    def _linked_assets(self, job_id, digest=None):
        """Assets this exact batch can prove it produced, keyed by source id."""
        linked = {}
        for asset in self.store.all("assets"):
            metadata = asset.get("metadata") or {}
            nested = metadata.get("job") if isinstance(metadata.get("job"), dict) else {}
            if nested.get("id") != job_id:
                continue
            source = nested.get("source_id")
            if not source:
                continue
            linked_digest = nested.get("steps_sha256")
            if digest and linked_digest and linked_digest != digest:
                continue
            if asset.get("parent_id") != source:
                continue
            linked.setdefault(source, []).append(asset)
        return linked

    def _link_matches(self, asset, job_id, source_id, digest):
        job = (asset.get("metadata") or {}).get("job")
        if not isinstance(job, dict):
            return False
        return (
            job.get("id") == job_id
            and job.get("source_id") == source_id
            and (not digest or not job.get("steps_sha256") or job.get("steps_sha256") == digest)
            and asset.get("parent_id") == source_id
        )

    def _unverified(self, record):
        """Old records without per-item state: adopt only provable results.

        The old `results` list was written by the same batch, so each entry is
        verifiable when the referenced asset still exists and its parent is the
        recorded source. Everything else stays explicitly unknown instead of
        being re-run and republished.
        """
        previous = {
            "results": record.get("results"),
            "errors": record.get("errors"),
            "done": record.get("done"),
            "progress": record.get("progress"),
        }
        asset_map = {asset["id"]: asset for asset in self.store.all("assets")}
        asset_ids = [value for value in (record.get("asset_ids") or []) if isinstance(value, str)]
        if not asset_ids:
            asset_ids = [
                entry["source_id"] for entry in (record.get("results") or [])
                if isinstance(entry, dict) and isinstance(entry.get("source_id"), str)
            ]
        proven = {}
        for entry in record.get("results") or []:
            if not isinstance(entry, dict):
                continue
            result_id = entry.get("asset_id")
            source_id = entry.get("source_id")
            asset = asset_map.get(result_id)
            if asset is None or not source_id or asset.get("parent_id") != source_id:
                continue
            if source_id in proven:
                proven[source_id] = None  # ambiguous duplicate, refuse to claim
            else:
                proven[source_id] = asset
        legacy_errors = {}
        for entry in record.get("errors") or []:
            if isinstance(entry, dict) and isinstance(entry.get("asset_id"), str):
                legacy_errors[entry["asset_id"]] = {
                    "code": entry.get("code") or "processing_error",
                    "message": entry.get("message") or "处理失败，请检查日志。",
                    "details": entry.get("details") or {},
                }
        items = []
        for index, asset_id in enumerate(asset_ids):
            asset = proven.get(asset_id)
            if asset is not None:
                items.append({
                    "source_id": asset_id,
                    "index": index,
                    "status": ITEM_SUCCESS,
                    "result_id": asset["id"],
                    "result_name": asset.get("name"),
                    "error": None,
                    "attempts": 0,
                })
            elif asset_id in legacy_errors:
                items.append({
                    "source_id": asset_id,
                    "index": index,
                    "status": ITEM_FAILED,
                    "result_id": None,
                    "result_name": None,
                    "error": legacy_errors[asset_id],
                    "attempts": 0,
                })
            else:
                items.append({
                    "source_id": asset_id,
                    "index": index,
                    "status": ITEM_UNKNOWN,
                    "result_id": None,
                    "result_name": None,
                    "error": {
                        "code": "unverified_legacy_record",
                        "message": "旧记录没有逐张保存状态，无法确认这张图片是否已经保存。",
                        "details": {},
                    },
                    "attempts": 0,
                })

        success_ids = {item["source_id"] for item in items if item["status"] == ITEM_SUCCESS}
        failed = [item for item in items if item["status"] == ITEM_FAILED]
        unknown = [item for item in items if item["status"] == ITEM_UNKNOWN]
        if not items and (record.get("done") or record.get("progress")):
            message = (
                "服务重启中断了任务，但这条旧记录既没有逐张状态，也没有可核对的结果列表，"
                "无法确认哪些图片已经保存。为避免重复发布，已停止自动继续；"
                "请先在素材库核对已有结果，再重新提交未完成的图片。"
            )
        elif unknown:
            message = (
                "服务重启中断了任务，但这条旧记录没有逐张保存处理状态。"
                f"已确认并保留 {len(success_ids)} 张已保存的结果，"
                f"其余 {len(unknown)} 张无法确认是否已经保存。"
                "为避免重复发布，未自动继续；请先在素材库核对，再重新提交不确定的图片。"
            )
        else:
            message = (
                "服务重启中断了任务，但这条旧记录没有逐张保存处理状态。"
                f"已确认并保留全部 {len(success_ids)} 张已保存的结果"
                + (f"，并有 {len(failed)} 张明确失败。" if failed else "。")
            )
        results = [
            {
                "source_id": item["source_id"],
                "asset_id": item["result_id"],
                "name": item["result_name"],
            }
            for item in items if item["status"] == ITEM_SUCCESS
        ]
        changes = {
            "status": "interrupted",
            "message": message,
            "items": items,
            "results": results,
            "errors": [
                {"asset_id": item["source_id"], **item["error"]}
                for item in items if item["status"] != ITEM_SUCCESS
            ],
            "succeeded": len(success_ids),
            "failed": len(failed),
            "unknown": len(unknown),
            "pending": 0,
            "remaining": 0,
            "done": len(success_ids) + len(failed),
            "progress": round((len(success_ids) + len(failed)) / len(items) * 100, 1) if items else 0,
            "resumable": False,
            "recovery_state": "unverified",
            "recovery_message": message,
            "recovery": {"state": "unverified", "message": message, "checked_at": utc_now()},
            "legacy": previous,
            "current_asset_id": None,
            "finished_at": record.get("finished_at") or utc_now(),
        }
        self.store.update("jobs", record["id"], changes)
        return self.store.get("jobs", record["id"])

    def _reconcile(self, record, cause="restart"):
        job_id = record["id"]
        items = record.get("items")
        if not isinstance(items, list) or not items:
            return self._unverified(record)

        total = record.get("total") or len(items)
        digest = pipeline_sha256(record.get("steps") or [])
        linked = self._linked_assets(job_id, digest)
        asset_map = {asset["id"]: asset for asset in self.store.all("assets")}
        conflict = False

        for item in items:
            source_id = item.get("source_id")
            status = item.get("status")
            recorded_id = item.get("result_id")

            if status == ITEM_UNKNOWN:
                # An earlier pass already refused to claim this output. Keep the
                # explicit "cannot confirm" state and never auto-republish it.
                conflict = True
                continue

            if status == ITEM_FAILED:
                # A failure was already observed and recorded; keep its reason so
                # a restart does not silently downgrade it to "unfinished".
                if not item.get("error"):
                    item["error"] = {
                        "code": "processing_error",
                        "message": "处理失败，请检查日志。",
                        "details": {},
                    }
                continue

            if status == ITEM_SUCCESS and recorded_id:
                asset = asset_map.get(recorded_id)
                if asset is None:
                    item["status"] = ITEM_PENDING
                    item["result_id"] = None
                    item["result_name"] = None
                    item["error"] = None
                elif not self._link_matches(asset, job_id, source_id, digest):
                    item["status"] = ITEM_UNKNOWN
                    item["error"] = {
                        "code": "unverified_result",
                        "message": "结果记录与这张图片的批次信息不一致，无法确认来源。",
                        "details": {},
                    }
                    conflict = True
                else:
                    item["result_name"] = asset.get("name")
                continue

            candidates = linked.get(source_id, [])
            if len(candidates) == 1:
                asset = candidates[0]
                item["status"] = ITEM_SUCCESS
                item["result_id"] = asset["id"]
                item["result_name"] = asset.get("name")
                item["error"] = None
            elif len(candidates) > 1:
                item["status"] = ITEM_UNKNOWN
                item["error"] = {
                    "code": "duplicate_result",
                    "message": "发现同一批次同一张图片有多个结果副本，无法确认应保留哪一个。",
                    "details": {"asset_ids": [asset["id"] for asset in candidates]},
                }
                conflict = True
            else:
                item["status"] = ITEM_PENDING
                item["result_id"] = None
                item["result_name"] = None
                item["error"] = None

        changes = self._snapshot(items, record)
        pending = changes["pending"]
        failures = changes["failed"]
        unknown = changes["unknown"]
        successes = changes["succeeded"]
        if pending:
            status = "interrupted"
            prefix = "服务重启中断了任务：" if cause == "restart" else ""
            if conflict:
                message = (
                    "检测到无法确认的处理记录；为避免重复发布，已停止自动继续，请先核对素材库。"
                    + (f"成功 {successes} 张，未完成 {pending} 张。" if successes or pending else "")
                )
            else:
                message = (
                    f"{prefix}成功 {successes} 张，失败 {failures} 张，未完成 {pending} 张"
                    "；可继续处理剩余图片。"
                )
        elif unknown:
            status = "interrupted"
            message = (
                f"成功 {successes} 张，失败 {failures} 张，无法确认 {unknown} 张。"
                "无法确认的图片不会自动重做，请先核对素材库。"
            )
        elif failures:
            status = "partial" if successes else "failed"
            message = f"成功 {successes} 张，失败 {failures} 张；可重试失败项。"
        else:
            status = "completed"
            message = f"已完成 {successes} 张图片"

        state = "conflict" if conflict or unknown else "reconciled"
        changes.update({
            "status": status,
            "message": message,
            "resumable": status in RESUMABLE and not conflict,
            "recovery_state": state,
            "recovery_message": message if state == "conflict" else "",
            "recovery": {
                "state": state,
                "message": message if state == "conflict" else "",
                "checked_at": utc_now(),
            },
            "finished_at": record.get("finished_at") or utc_now(),
            "current_asset_id": None,
        })
        self.store.update("jobs", job_id, changes)
        return self.store.get("jobs", job_id)

    def _recover_after_restart(self):
        for job in self.store.all("jobs"):
            status = job.get("status")
            state = job.get("recovery_state")
            if state in {"unverified", "conflict", "cancelled"}:
                # Already resolved explicitly; repeating must not relabel it.
                continue
            if status in ACTIVE:
                if status == "cancelling":
                    # Reconcile first so saved outputs are reported, then keep it
                    # cancelled. A cancellation is never revived by a restart.
                    record = self._reconcile(job, "restart")
                    items = record.get("items") or []
                    changes = self._snapshot(items, record)
                    message = "已取消的任务在服务重启后保持取消，不会自动继续。"
                    changes.update({
                        "status": "cancelled",
                        "message": message,
                        "resumable": False,
                        "recovery_state": "cancelled",
                        "recovery_message": message,
                        "recovery": {"state": "cancelled", "message": message, "checked_at": utc_now()},
                        "finished_at": utc_now(),
                        "current_asset_id": None,
                    })
                    self.store.update("jobs", job["id"], changes)
                else:
                    self._reconcile(job, "restart")
            elif status == "interrupted":
                # Reconciliation is idempotent; run it again after every restart.
                self._reconcile(job, "restart")

    # ------------------------------------------------------------------- reads
    def get(self, job_id):
        identifier(job_id)
        return self.store.get("jobs", job_id)

    def list(self):
        records = self.store.all("jobs")
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return records[:100]

    # ------------------------------------------------------------------ cancel
    def cancel(self, job_id):
        with self.lock:
            record = self.get(job_id)
            if record["status"] not in ACTIVE:
                return record
            flag = self.cancellations.get(job_id)
            if flag:
                flag.set()
            return self.store.update("jobs", job_id, {
                "status": "cancelling", "message": "将在当前操作结束后取消",
            })

    # ------------------------------------------------------------------ resume
    def resume(self, job_id):
        with self.assets.lock, self.lock:
            record = self.get(job_id)
            status = record.get("status")
            if status in ACTIVE:
                return record
            if status == "cancelled":
                raise Conflict("已取消的任务不会自动恢复；请重新选择图片提交新任务。")
            if status == "completed":
                return record
            if not isinstance(record.get("items"), list) or not record.get("items"):
                raise Conflict(
                    "这条旧任务记录没有逐张保存处理状态，无法确认哪些图片已经保存。"
                    "为避免重复发布，不能自动继续；请先在素材库核对已有结果，再重新提交未完成的图片。"
                )
            record = self._reconcile(record, "resume")
            if record.get("recovery_state") in {"unverified", "conflict"}:
                raise Conflict(record.get("recovery_message") or record.get("message") or "无法确认已有结果，不能自动继续。")
            retry = [
                item for item in record["items"]
                if item.get("status") in {ITEM_PENDING, ITEM_RUNNING, ITEM_FAILED}
            ]
            if not retry:
                return record
            active = [job for job in self.store.all("jobs") if job["status"] in ACTIVE]
            if len(active) >= 4:
                raise Conflict("最多允许 4 个等待或运行中的任务，请等待其他任务结束后再继续")
            for item in retry:
                item["status"] = ITEM_PENDING
                item["error"] = None
            changes = self._snapshot(record["items"], record)
            changes.update({
                "status": "queued",
                "message": f"继续处理剩余 {len(retry)} 张图片",
                "resumable": False,
                "recovery_state": "reconciled",
                "recovery_message": "",
                "recovery": {"state": "reconciled", "message": "", "checked_at": utc_now()},
                "resume_count": int(record.get("resume_count") or 0) + 1,
                "current_asset_id": None,
                "finished_at": None,
            })
            record = self.store.update("jobs", job_id, changes)
            cancellation = threading.Event()
            self.cancellations[job_id] = cancellation
            self.futures[job_id] = self.executor.submit(self._run, job_id, cancellation)
            self.store.event("batch", f"继续批次：{record['title']}", {"job_id": job_id})
            return record

    def shutdown(self):
        with self.lock:
            self.shutdown_event.set()
            for flag in self.cancellations.values():
                flag.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
