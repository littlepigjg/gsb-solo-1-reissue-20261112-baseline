"""Review annotations anchored to image content through projective frames.

Markers are stored once per asset in *root image coordinates* (the coordinate
space of the originally imported picture). Each asset's ``frame`` homography
maps its own canvas back to that root space, so crop, resize, rotate,
perspective and canvas-extension steps move the markers with the content
without any manual re-placement.

The annotation document of every processed asset starts as an independent
snapshot copied from its parent: editing one branch never touches a sibling
or an older saved version. Undo/redo keeps full document snapshots, giving a
per-asset review history that survives saving and reopening.
"""
import threading
from .errors import StudioError, MissingRecord
from .storage import new_id, utc_now
from .validation import identifier, text_value, number_value, color_value, choice_value, list_value
from . import frames as frame_math


MARKER_KINDS = {"rect", "ellipse", "arrow", "freehand", "note"}
MAX_MARKERS = 200
MAX_HISTORY = 50
MIN_COORD = -100000
MAX_COORD = 100000


class AnnotationService:
    def __init__(self, store, assets):
        self.store = store
        self.assets = assets
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ docs
    def _document(self, asset_id):
        identifier(asset_id)
        try:
            return self.store.get("annotations", asset_id)
        except MissingRecord:
            return {
                "id": asset_id,
                "asset_id": asset_id,
                "markers": [],
                "undo_stack": [],
                "redo_stack": [],
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }

    def _save_document(self, document):
        document["updated_at"] = utc_now()
        document_id = document["id"]
        try:
            self.store.get("annotations", document_id)
            exists = True
        except MissingRecord:
            exists = False
        if exists:
            changes = {key: value for key, value in document.items() if key != "id"}
            self.store.update("annotations", document_id, changes)
        else:
            self.store.insert("annotations", document)

    def get(self, asset_id):
        with self.lock:
            self.assets.get(asset_id)
            document = self._document(asset_id)
            return self._public(asset_id, document)

    def _public(self, asset_id, document):
        asset = self.assets.get(asset_id)
        frame = asset.get("frame", {}).get("matrix", frame_math.IDENTITY)
        return {
            "asset_id": asset_id,
            "root_id": asset["root_id"],
            "frame": asset.get("frame", {"matrix": frame_math.to_json(frame), "warnings": []}),
            "canvas": {"width": asset["width"], "height": asset["height"]},
            "markers": document["markers"],
            "undo_available": bool(document["undo_stack"]),
            "redo_available": bool(document["redo_stack"]),
        }

    # ------------------------------------------------------------- mutation
    def _push_history(self, document):
        document["undo_stack"].append({"markers": self._clone(document["markers"]), "at": utc_now()})
        if len(document["undo_stack"]) > MAX_HISTORY:
            document["undo_stack"].pop(0)
        document["redo_stack"] = []

    def add_marker(self, asset_id, payload):
        with self.lock:
            self.assets.get(asset_id)
            document = self._document(asset_id)
            if len(document["markers"]) >= MAX_MARKERS:
                raise StudioError(f"每个版本最多保留 {MAX_MARKERS} 条批注")
            marker = self._validate_marker(payload, self._root_size(asset_id))
            self._push_history(document)
            document["markers"].append(marker)
            self._save_document(document)
            self.store.event("annotation_add", f"为素材添加批注", {"asset_id": asset_id, "marker_id": marker["id"]})
            return self._public(asset_id, document)

    def update_marker(self, asset_id, marker_id, payload):
        with self.lock:
            document = self._document(asset_id)
            marker = self._find(document, marker_id)
            self._push_history(document)
            marker.update(self._validate_marker(payload, self._root_size(asset_id), partial=True, existing=marker))
            marker["id"] = marker_id
            marker["updated_at"] = utc_now()
            self._save_document(document)
            return self._public(asset_id, document)

    def delete_marker(self, asset_id, marker_id):
        with self.lock:
            document = self._document(asset_id)
            marker = self._find(document, marker_id)
            self._push_history(document)
            document["markers"] = [item for item in document["markers"] if item["id"] != marker_id]
            self._save_document(document)
            self.store.event("annotation_delete", "删除批注", {"asset_id": asset_id, "marker_id": marker_id})
            return self._public(asset_id, document)

    def undo(self, asset_id):
        with self.lock:
            document = self._document(asset_id)
            if not document["undo_stack"]:
                raise StudioError("没有可撤销的批注操作")
            snapshot = document["undo_stack"].pop()
            document["redo_stack"].append({"markers": self._clone(document["markers"]), "at": utc_now()})
            document["markers"] = snapshot["markers"]
            self._save_document(document)
            return self._public(asset_id, document)

    def redo(self, asset_id):
        with self.lock:
            document = self._document(asset_id)
            if not document["redo_stack"]:
                raise StudioError("没有可重做的批注操作")
            snapshot = document["redo_stack"].pop()
            document["undo_stack"].append({"markers": self._clone(document["markers"]), "at": utc_now()})
            document["markers"] = snapshot["markers"]
            self._save_document(document)
            return self._public(asset_id, document)

    # --------------------------------------------------------------- branch
    def fork(self, parent_id, child_id):
        """Give a new processed asset its own independent annotation snapshot."""
        with self.lock:
            parent_doc = self._document(parent_id)
            if not parent_doc["markers"]:
                return
            child_doc = {
                "id": child_id,
                "asset_id": child_id,
                "markers": self._clone(parent_doc["markers"]),
                "undo_stack": [],
                "redo_stack": [],
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            self._save_document(child_doc)

    # ------------------------------------------------------------ projection
    def markers_in_view(self, asset_id):
        """Markers projected from root space into the asset's own canvas."""
        with self.lock:
            asset = self.assets.get(asset_id)
            document = self._document(asset_id)
            frame = asset.get("frame", {}).get("matrix", frame_math.IDENTITY)
            inverse = frame_math.invert(frame)
            width, height = asset["width"], asset["height"]
            projected = []
            for marker in document["markers"]:
                view_marker = self._project_marker(marker, inverse)
                view_marker["visibility"] = self._visibility(view_marker, width, height)
                projected.append(view_marker)
            return projected

    def _root_size(self, asset_id):
        asset = self.assets.get(asset_id)
        root = self.assets.get(asset["root_id"])
        return root["width"], root["height"]

    def to_root(self, asset_id, points):
        asset = self.assets.get(asset_id)
        frame = asset.get("frame", {}).get("matrix", frame_math.IDENTITY)
        return frame_math.apply(frame, points)

    # ------------------------------------------------------------- validation
    def _validate_marker(self, payload, root_size, partial=False, existing=None):
        if not isinstance(payload, dict):
            raise StudioError("批注必须是 JSON 对象")
        allowed = {"kind", "label", "color", "geometry", "line_width"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise StudioError("批注包含未知字段", details={"fields": unknown})
        marker = dict(existing) if existing else {}
        if "kind" in payload or not partial:
            marker["kind"] = choice_value(payload.get("kind", marker.get("kind")), MARKER_KINDS, "批注类型")
        if "label" in payload or not partial:
            label = payload.get("label", marker.get("label", ""))
            marker["label"] = text_value(label or "", "批注说明", 500, 0)
        if "color" in payload or not partial:
            marker["color"] = color_value(payload.get("color", marker.get("color", "#d9482b")), "批注颜色")
        if "line_width" in payload or not partial:
            marker["line_width"] = number_value(payload.get("line_width", marker.get("line_width", 3)),
                                                "线宽", 1, 24, integer=True)
        if "geometry" in payload or not partial:
            marker["geometry"] = self._validate_geometry(marker["kind"], payload.get("geometry", marker.get("geometry")), root_size)
        if not partial:
            marker["id"] = new_id()
            marker["created_at"] = utc_now()
            marker["updated_at"] = utc_now()
        return marker

    def _validate_geometry(self, kind, geometry, root_size):
        if not isinstance(geometry, dict):
            raise StudioError("批注图形必须是对象")

        def point_value(value, label):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise StudioError(f"{label}必须是两个数字的坐标")
            x = number_value(value[0], f"{label} X", MIN_COORD, MAX_COORD)
            y = number_value(value[1], f"{label} Y", MIN_COORD, MAX_COORD)
            return [x, y]

        if kind in {"rect", "ellipse"}:
            allowed = {"x", "y", "width", "height"}
            if set(geometry) - allowed:
                raise StudioError(f"{kind} 图形字段不正确")
            x = number_value(geometry.get("x", 0), "图形起点 X", MIN_COORD, MAX_COORD)
            y = number_value(geometry.get("y", 0), "图形起点 Y", MIN_COORD, MAX_COORD)
            w = number_value(geometry.get("width", 10), "图形宽度", 1, MAX_COORD)
            h = number_value(geometry.get("height", 10), "图形高度", 1, MAX_COORD)
            return {"x": x, "y": y, "width": w, "height": h}
        if kind in {"arrow", "freehand"}:
            points = geometry.get("points")
            list_value(points, "批注点序列", minimum=2 if kind == "arrow" else 2, maximum=500)
            return {"points": [point_value(item, "批注坐标点") for item in points]}
        # note: an anchored point plus optional text already lives on marker.label
        return {"anchor": point_value(geometry.get("anchor", [0, 0]), "便签锚点")}

    def _project_marker(self, marker, inverse):
        view = {key: value for key, value in marker.items() if key != "geometry"}
        geometry = marker["geometry"]
        if "points" in geometry:
            view["geometry"] = {"points": frame_math.apply(inverse, geometry["points"])}
        elif "anchor" in geometry:
            view["geometry"] = {"anchor": frame_math.point(inverse, *geometry["anchor"])}
        else:
            x, y = geometry["x"], geometry["y"]
            w, h = geometry["width"], geometry["height"]
            corners = frame_math.apply(inverse, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
            xs = [point[0] for point in corners]
            ys = [point[1] for point in corners]
            view["geometry"] = {
                "x": min(xs), "y": min(ys),
                "width": max(xs) - min(xs), "height": max(ys) - min(ys),
                "corners": corners,
            }
        return view

    def _visibility(self, view_marker, width, height):
        geometry = view_marker["geometry"]
        if "points" in geometry:
            points = geometry["points"]
        elif "anchor" in geometry:
            points = [geometry["anchor"]]
        else:
            points = geometry["corners"]
        inside = [(-0.5 <= x <= width - 0.5 and -0.5 <= y <= height - 0.5) for x, y in points]
        if all(inside):
            return "inside"
        if any(inside):
            return "partial"
        return "outside"

    def _find(self, document, marker_id):
        identifier(marker_id, "批注 ID")
        for marker in document["markers"]:
            if marker["id"] == marker_id:
                return marker
        raise MissingRecord("批注", marker_id)

    @staticmethod
    def _clone(markers):
        import copy
        return copy.deepcopy(markers)
