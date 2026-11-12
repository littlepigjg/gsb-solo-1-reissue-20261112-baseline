"""Read-only operational checks surfaced in the local system panel."""
import importlib.metadata
import platform
import shutil
from pathlib import Path
from .storage import utc_now


class DiagnosticsService:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    def dependencies(self):
        versions = {}
        for package in ("Flask", "Pillow", "numpy", "waitress"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = "missing"
        return versions

    def inspect(self):
        issues = []
        collections = {}
        records = {}
        storage = self.store.metrics()
        for name in sorted(self.store.COLLECTIONS):
            try:
                entries = self.store.all(name)
                records[name] = entries
                usage = storage["collections"][name]
                path = self.settings.database / f"{name}.json"
                collections[name] = {
                    "records": len(entries),
                    "bytes": usage["snapshot_bytes"] + usage["journal_bytes"],
                    "journal_bytes": usage["journal_bytes"],
                    "backup_exists": path.with_suffix(".json.bak").exists(),
                    "readable": True,
                }
            except (RuntimeError, ValueError, OSError) as error:
                collections[name] = {"readable": False}
                records[name] = []
                issues.append({"code": "json_unreadable", "collection": name, "message": str(error)})
        asset_ids = {item["id"] for item in records["assets"]}
        tracked_files = set()
        missing_files = []
        for asset in records["assets"]:
            for suffix in (".png", ".thumb.png"):
                filename = asset["id"] + suffix
                tracked_files.add(filename)
                if not (self.settings.media / filename).is_file():
                    missing_files.append(filename)
            parent_id = asset.get("parent_id")
            if parent_id and parent_id not in asset_ids:
                issues.append({
                    "code": "missing_parent",
                    "asset_id": asset["id"],
                    "parent_id": parent_id,
                })
            root_id = asset.get("root_id")
            if root_id and root_id not in asset_ids:
                issues.append({
                    "code": "missing_root",
                    "asset_id": asset["id"],
                    "root_id": root_id,
                })
        for filename in missing_files:
            issues.append({"code": "missing_media", "filename": filename})
        disk_files = {path.name for path in self.settings.media.glob("*.png")}
        orphans = sorted(disk_files - tracked_files)
        for filename in orphans:
            issues.append({"code": "untracked_media", "filename": filename})
        for record in records["exports"]:
            if not (self.settings.exports / record["filename"]).is_file():
                issues.append({"code": "missing_export", "export_id": record["id"]})
        usage = shutil.disk_usage(self.settings.root)
        if usage.free < 512 * 1024 * 1024:
            issues.append({"code": "low_disk_space", "message": "空闲磁盘空间低于 512 MB"})
        active_jobs = [
            item["id"]
            for item in records["jobs"]
            if item["status"] in {"queued", "running", "cancelling"}
        ]
        return {
            "checked_at": utc_now(),
            "python": platform.python_version(),
            "platform": platform.system(),
            "dependencies": self.dependencies(),
            "collections": collections,
            "storage": storage,
            "media_file_count": len(disk_files),
            "tracked_media_count": len(tracked_files),
            "missing_media_count": len(missing_files),
            "orphan_media_count": len(orphans),
            "disk_total_bytes": usage.total,
            "disk_free_bytes": usage.free,
            "active_jobs": active_jobs,
            "healthy": not issues,
            "issues": issues,
            "limits": self.settings.public(),
        }

