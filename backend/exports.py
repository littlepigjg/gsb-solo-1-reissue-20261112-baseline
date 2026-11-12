"""Real format encoding, bounded streaming ZIP preparation and tracked delivery.

A batch export never materialises the archive in RAM: every member is encoded
straight into a ZIP entry that lives in a hidden staging file, one source image
at a time. Only a fully written and structurally verified archive is promoted
to its final name and registered as an export record.
"""
import io
import json
import logging
import os
import threading
import zipfile
from PIL import Image
from .errors import StudioError, Conflict, MissingRecord
from .imaging import flatten
from .storage import new_id, utc_now
from .validation import (
    object_value,
    reject_unknown,
    choice_value,
    number_value,
    color_value,
    boolean_value,
    list_value,
    identifier,
)


LOGGER = logging.getLogger(__name__)

EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "BMP": "bmp", "TIFF": "tiff", "GIF": "gif"}
MIMETYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "BMP": "image/bmp", "TIFF": "image/tiff", "GIF": "image/gif"}
ARCHIVE_NAME = "光屿_批量导出.zip"
ACTIVE_TASK = {"queued", "preparing", "cancelling"}
TERMINAL_TASK = {"completed", "failed", "cancelled", "interrupted"}
DOWNLOAD_CHUNK = 64 * 1024


class _CancellableWrite:
    """File-like sink that stops Pillow between encoder chunks on cancel."""

    def __init__(self, target, cancelled):
        self.target = target
        self.cancelled = cancelled

    def write(self, data):
        if self.cancelled and self.cancelled():
            raise StudioError("导出已取消", code="cancelled", status=409)
        return self.target.write(data)

    def flush(self):
        flush = getattr(self.target, "flush", None)
        if flush:
            flush()


class DeliveryReader:
    """Tracked reader for an archive download.

    Werkzeug closes the file object when the WSGI response is finished or the
    client disconnects. If the full payload was not read, the export record and
    its file are removed instead of being left behind as a fake finished export.
    """

    def __init__(self, service, export_id, path, size, armed=True):
        self.service = service
        self.export_id = export_id
        self.size = size
        self.armed = armed
        self.stream = open(path, "rb")
        self.sent = 0
        self.read_started = False
        self.finished = False
        self.closed = False

    def readable(self):
        return True

    def seekable(self):
        return False

    def read(self, size=-1):
        self.read_started = True
        data = self.stream.read(size)
        if data:
            self.sent += len(data)
        else:
            self.finished = True
        return data

    def __iter__(self):
        return self

    def __next__(self):
        data = self.read(DOWNLOAD_CHUNK)
        if not data:
            raise StopIteration
        return data

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.stream.close()
        finally:
            complete = self.sent >= self.size
            if self.armed and self.read_started and not complete:
                self.service.abandon_delivery(self.export_id, self.sent, self.size)


class ExportService:
    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing
        self.lock = threading.RLock()
        self.prepare_lock = threading.Lock()
        self.cancellations = {}
        self.threads = {}
        staging = self.settings.exports / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        for leftover in staging.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
        self._recover()

    # -- validation and encoding ------------------------------------------------

    def validate(self, options):
        object_value(options, "导出参数")
        reject_unknown(options, {"format", "quality", "background", "lossless", "dpi"}, "导出参数")
        return {
            "format": choice_value(options.get("format", "PNG"), list(EXTENSIONS), "导出格式"),
            "quality": number_value(options.get("quality", 90), "质量", 1, 100, integer=True),
            "background": color_value(options.get("background", "#ffffff")),
            "lossless": boolean_value(options.get("lossless", False), "无损模式"),
            "dpi": number_value(options.get("dpi", 96), "DPI", 36, 1200, integer=True),
        }

    def _encode_options(self, image, options):
        format_name = options["format"]
        kwargs = {}
        output = image
        if format_name in {"JPEG", "BMP", "GIF"}:
            output = flatten(image, options["background"])
        if format_name == "JPEG":
            kwargs.update(quality=options["quality"], optimize=True, progressive=True)
        elif format_name == "WEBP":
            kwargs.update(quality=options["quality"], lossless=options["lossless"], method=4)
        elif format_name == "PNG":
            kwargs.update(optimize=True, compress_level=6)
        elif format_name == "TIFF":
            kwargs.update(compression="tiff_lzw")
        elif format_name == "GIF":
            output = output.quantize(colors=256)
        if format_name in {"PNG", "JPEG", "TIFF", "BMP"}:
            kwargs["dpi"] = (options["dpi"], options["dpi"])
        output.info.clear()
        return output, kwargs

    def _encode_into(self, image, options, target):
        output, kwargs = self._encode_options(image, options)
        try:
            output.save(target, format=options["format"], **kwargs)
        finally:
            if output is not image:
                output.close()

    def encode(self, image, options):
        buffer = io.BytesIO()
        self._encode_into(image, options, buffer)
        return buffer.getvalue()

    # -- single export ----------------------------------------------------------

    def _save_file(self, payload, extension, name, asset_ids, options, kind):
        export_id = new_id()
        filename = f"{export_id}.{extension}"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            record = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": name,
                "bytes": len(payload),
                "asset_ids": asset_ids,
                "options": options,
                "kind": kind,
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("export", f"导出 {name}", {"export_id": export_id})
        return record

    def create(self, asset_id, options):
        options = self.validate(options)
        with self.processing.compute_lock:
            image, asset = self.assets.load(asset_id)
            try:
                payload = self.encode(image, options)
            finally:
                image.close()
        extension = EXTENSIONS[options["format"]]
        name = asset["name"].rsplit(".", 1)[0][:100] + "." + extension
        return self._save_file(payload, extension, name, [asset_id], options, "image")

    # -- shared archive preparation --------------------------------------------

    def _staging_path(self, export_id):
        return self.settings.exports / ".staging" / f"{export_id}.zip.partial"

    def _archive_filename(self, index, asset_id, options):
        return f"{index:02d}_{asset_id[:8]}.{EXTENSIONS[options['format']]}"

    def _manifest(self, asset_ids, options):
        return json.dumps(
            {"options": options, "assets": asset_ids, "count": len(asset_ids)},
            ensure_ascii=False,
            indent=2,
        )

    def _raise_if_cancelled(self, cancellation):
        if cancellation and cancellation.is_set():
            raise StudioError("导出已取消", code="cancelled", status=409)

    def _build_archive(self, asset_ids, options, staging, cancellation=None, progress=None):
        """Write each encoded member directly into the staging ZIP, one at a time.

        The compute lock is taken per member and released between members so
        queued edits and previews can interleave with a long export without
        ever holding two large images at once.
        """
        with open(staging, "xb", buffering=256 * 1024) as sink:
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for index, asset_id in enumerate(asset_ids, start=1):
                    self._raise_if_cancelled(cancellation)
                    with self.processing.compute_lock:
                        image, asset = self.assets.load(asset_id)
                        try:
                            self._raise_if_cancelled(cancellation)
                            filename = self._archive_filename(index, asset_id, options)
                            with archive.open(filename, "w", force_zip64=True) as entry:
                                self._encode_into(
                                    image,
                                    options,
                                    _CancellableWrite(entry, cancellation.is_set if cancellation else None),
                                )
                        finally:
                            image.close()
                    prepared = staging.stat().st_size
                    if progress:
                        progress(index, len(asset_ids), asset["name"], prepared)
                self._raise_if_cancelled(cancellation)
                archive.writestr("manifest.json", self._manifest(asset_ids, options))

    def _expected_members(self, asset_ids, options):
        return [self._archive_filename(index, asset_id, options)
                for index, asset_id in enumerate(asset_ids, start=1)] + ["manifest.json"]

    def _verify_archive(self, staging, expected_names, asset_ids=None):
        """Validate the central directory and manifest without loading members."""
        with zipfile.ZipFile(staging, "r") as archive:
            names = archive.namelist()
            if names != expected_names:
                raise StudioError("导出压缩包成员不完整，请重新导出", status=500)
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("count") != len(expected_names) - 1:
                raise StudioError("导出清单与成员数量不一致，请重新导出", status=500)
            if asset_ids is not None and manifest.get("assets") != list(asset_ids):
                raise StudioError("导出清单中的图片顺序不一致，请重新导出", status=500)

    def _register_archive(self, export_id, filename, size, asset_ids, options, task_id=None):
        record = self.store.insert("exports", {
            "id": export_id,
            "filename": filename,
            "download_name": ARCHIVE_NAME,
            "bytes": size,
            "asset_ids": asset_ids,
            "options": options,
            "kind": "archive",
            "task_id": task_id,
        })
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("export", f"打包导出 {len(asset_ids)} 张图片", {
            "export_id": export_id,
            "task_id": task_id,
        })
        return record

    def _cleanup_archive(self, staging, final):
        staging.unlink(missing_ok=True)
        final.unlink(missing_ok=True)

    # -- synchronous archive ----------------------------------------------------

    def archive(self, asset_ids, options):
        asset_ids = list_value(asset_ids, "导出图片", maximum=self.settings.max_batch)
        asset_ids = list(dict.fromkeys(asset_ids))
        options = self.validate(options)
        export_id = new_id()
        staging = self._staging_path(export_id)
        final = self.settings.exports / f"{export_id}.zip"
        expected = self._expected_members(asset_ids, options)
        try:
            if not self.prepare_lock.acquire(blocking=False):
                raise Conflict("已有一个批量导出正在准备，请使用进度接口或稍后重试")
            try:
                self._build_archive(asset_ids, options, staging)
                self._verify_archive(staging, expected, asset_ids)
                os.replace(staging, final)
                size = final.stat().st_size
                return self._register_archive(export_id, final.name, size, asset_ids, options)
            finally:
                self.prepare_lock.release()
        except Exception:
            self._cleanup_archive(staging, final)
            raise
        finally:
            staging.unlink(missing_ok=True)

    # -- durable preparation tasks ---------------------------------------------

    def task(self, task_id):
        identifier(task_id)
        return self.public_task(self.store.get("export_tasks", task_id))

    def tasks(self):
        records = self.store.all("export_tasks")
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return [self.public_task(item) for item in records[:20]]

    def public_task(self, record):
        result = dict(record)
        result["status_url"] = f"/api/exports/archive/tasks/{record['id']}"
        result["cancel_url"] = f"/api/exports/archive/tasks/{record['id']}/cancel"
        if record.get("status") == "completed" and record.get("export_id"):
            result["url"] = f"/api/exports/{record['export_id']}/download"
            result["download_url"] = f"/api/exports/archive/tasks/{record['id']}/download"
            result["download_name"] = ARCHIVE_NAME
        return result

    def archive_task(self, asset_ids, options):
        asset_ids = list_value(asset_ids, "导出图片", maximum=self.settings.max_batch)
        asset_ids = list(dict.fromkeys(asset_ids))
        options = self.validate(options)
        with self.lock:
            active = [item for item in self.store.all("export_tasks") if item["status"] in ACTIVE_TASK]
            if active:
                raise Conflict("已有一个批量导出正在准备，请等待完成或取消后再试")
            for asset_id in asset_ids:
                identifier(asset_id)
                self.assets.get(asset_id)
            record = self.store.insert("export_tasks", {
                "asset_ids": asset_ids,
                "options": options,
                "status": "queued",
                "total": len(asset_ids),
                "done": 0,
                "progress": 0,
                "bytes_prepared": 0,
                "current_asset_id": None,
                "current_name": "",
                "message": "等待准备导出",
                "error": None,
                "export_id": None,
                "bytes": 0,
                "started_at": None,
                "finished_at": None,
            })
            cancellation = threading.Event()
            self.cancellations[record["id"]] = cancellation
            thread = threading.Thread(
                target=self._run_task,
                args=(record["id"], cancellation),
                name=f"studio-export-{record['id'][:6]}",
                daemon=True,
            )
            self.threads[record["id"]] = thread
            thread.start()
            return self.public_task(self.store.get("export_tasks", record["id"]))

    def _update_task(self, task_id, changes):
        return self.store.update("export_tasks", task_id, changes)

    def _run_task(self, task_id, cancellation):
        export_id = None
        try:
            export_id = self._execute_task(task_id, cancellation)
        except StudioError as error:
            if error.code == "cancelled":
                self._update_task(task_id, {
                    "status": "cancelled",
                    "message": "已取消；没有生成可下载的压缩包。",
                    "current_asset_id": None,
                    "current_name": "",
                    "finished_at": utc_now(),
                    "error": None,
                })
                self.store.event("export_cancelled", "批量导出已取消", {"task_id": task_id})
            else:
                LOGGER.exception("Export preparation failed: %s", task_id)
                self._update_task(task_id, {
                    "status": "failed",
                    "message": f"准备失败：{error.message}",
                    "current_asset_id": None,
                    "current_name": "",
                    "finished_at": utc_now(),
                    "error": error.as_dict(),
                })
                self.store.event("export_failed", "批量导出准备失败", {"task_id": task_id})
        except Exception as error:
            LOGGER.exception("Export preparation failed: %s", task_id)
            self._update_task(task_id, {
                "status": "failed",
                "message": "准备导出时发生异常，请查看服务日志。",
                "current_asset_id": None,
                "current_name": "",
                "finished_at": utc_now(),
                "error": {"code": "export_error", "message": str(error), "details": {}},
            })
            self.store.event("export_failed", "批量导出准备失败", {"task_id": task_id})
        finally:
            if export_id:
                self._staging_path(export_id).unlink(missing_ok=True)
            with self.lock:
                self.cancellations.pop(task_id, None)
                self.threads.pop(task_id, None)

    def _discard_partial(self, export_id):
        """Remove staging data and any record/file that was not fully delivered."""
        if not export_id:
            return
        self._staging_path(export_id).unlink(missing_ok=True)
        final = self.settings.exports / f"{export_id}.zip"
        final.unlink(missing_ok=True)
        try:
            self.store.get("exports", export_id)
        except MissingRecord:
            return
        try:
            self.store.delete("exports", export_id)
        except MissingRecord:
            pass
    def _execute_task(self, task_id, cancellation):
        record = self.store.get("export_tasks", task_id)
        export_id = new_id()
        final = self.settings.exports / f"{export_id}.zip"
        asset_ids = record["asset_ids"]
        options = record["options"]
        expected = self._expected_members(asset_ids, options)
        self._update_task(task_id, {
            "status": "preparing",
            "started_at": utc_now(),
            "message": "正在准备导出",
        })

        def progress(done, total, name, prepared):
            self._update_task(task_id, {
                "done": done,
                "progress": round(done / total * 100, 1),
                "bytes_prepared": prepared,
                "current_asset_id": None,
                "current_name": name,
                "message": f"正在编码 {done}/{total} · {name}",
            })

        staging = self._staging_path(export_id)
        if not self.prepare_lock.acquire(blocking=False):
            raise Conflict("已有另一个导出正在准备，当前任务未能开始")
        try:
            self._raise_if_cancelled(cancellation)
            self._build_archive(asset_ids, options, staging, cancellation, progress)
            self._raise_if_cancelled(cancellation)
            self._verify_archive(staging, expected, asset_ids)
            os.replace(staging, final)
            size = final.stat().st_size
            record = self._register_archive(export_id, final.name, size, asset_ids, options, task_id)
            # Publish completion while still holding the lock so a download
            # cannot race ahead of the task record that points at the file.
            self._update_task(task_id, {
                "status": "completed",
                "done": len(asset_ids),
                "progress": 100,
                "bytes_prepared": size,
                "bytes": size,
                "current_asset_id": None,
                "current_name": "",
                "message": f"已准备 {len(asset_ids)} 张图片，可以下载",
                "export_id": export_id,
                "finished_at": utc_now(),
                "error": None,
            })
        except Exception:
            self._discard_partial(export_id)
            raise
        finally:
            self.prepare_lock.release()
        return export_id

    def cancel_task(self, task_id):
        identifier(task_id)
        with self.lock:
            record = self.store.get("export_tasks", task_id)
            if record["status"] in TERMINAL_TASK:
                return self.public_task(record)
            cancellation = self.cancellations.get(task_id)
            if cancellation:
                cancellation.set()
            record = self._update_task(task_id, {
                "status": "cancelling",
                "message": "正在取消；当前图片编码结束后停止",
            })
            return self.public_task(record)

    def _recover(self):
        for record in self.store.all("export_tasks"):
            if record["status"] in ACTIVE_TASK:
                self.store.update("export_tasks", record["id"], {
                    "status": "interrupted",
                    "message": "服务重启中断了导出准备；没有生成可下载文件。",
                    "finished_at": utc_now(),
                    "current_asset_id": None,
                    "current_name": "",
                })
            elif record["status"] == "completed" and record.get("export_id"):
                try:
                    export = self.store.get("exports", record["export_id"])
                    present = (self.settings.exports / export["filename"]).is_file()
                except MissingRecord:
                    present = False
                if not present:
                    self.store.update("export_tasks", record["id"], {
                        "status": "failed",
                        "message": "导出文件已丢失，请重新导出。",
                        "export_id": None,
                        "error": {"code": "missing_export", "message": "导出文件已丢失", "details": {}},
                        "finished_at": utc_now(),
                    })

    # -- delivery ---------------------------------------------------------------

    def file(self, export_id):
        identifier(export_id)
        record = self.store.get("exports", export_id)
        path = self.settings.exports / record["filename"]
        if not path.exists():
            raise StudioError("导出文件丢失，请重新导出", status=410)
        mime = "application/zip" if record["kind"] == "archive" else MIMETYPES[record["options"]["format"]]
        return path, record, mime

    def delivery(self, export_id, armed=True):
        path, record, mime = self.file(export_id)
        reader = DeliveryReader(self, export_id, path, path.stat().st_size, armed)
        return reader, record, mime

    def task_delivery(self, task_id):
        identifier(task_id)
        record = self.store.get("export_tasks", task_id)
        if record["status"] != "completed" or not record.get("export_id"):
            raise Conflict("导出尚未准备完成，暂时不能下载")
        return self.delivery(record["export_id"])

    def abandon_delivery(self, export_id, sent, size):
        """A download ended before the complete archive reached the client."""
        with self.lock:
            try:
                record = self.store.get("exports", export_id)
            except MissingRecord:
                return
            if record.get("kind") != "archive":
                return
            (self.settings.exports / record["filename"]).unlink(missing_ok=True)
            try:
                self.store.delete("exports", export_id)
            except MissingRecord:
                pass
            task_id = record.get("task_id")
            if task_id:
                try:
                    task = self.store.get("export_tasks", task_id)
                except MissingRecord:
                    task = None
                if task and task.get("export_id") == export_id:
                    self.store.update("export_tasks", task_id, {
                        "status": "failed",
                        "message": "下载连接中断，未完整交付的压缩包已清理；请重新导出。",
                        "export_id": None,
                        "bytes": 0,
                        "error": {
                            "code": "download_interrupted",
                            "message": "下载连接中断",
                            "details": {"received_bytes": sent, "total_bytes": size},
                        },
                        "finished_at": utc_now(),
                    })
            self.store.event("export_download_interrupted", "导出下载中断，文件已清理", {
                "export_id": export_id,
                "received_bytes": sent,
                "total_bytes": size,
            })

    def shutdown(self):
        with self.lock:
            for cancellation in self.cancellations.values():
                cancellation.set()
        for thread in list(self.threads.values()):
            thread.join(timeout=10)
