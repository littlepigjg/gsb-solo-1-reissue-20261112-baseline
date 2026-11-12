/* Annotation overlay: draw review markers that track image content through
 * crop, resize, rotate, perspective and canvas edits. Markers are persisted in
 * root image coordinates; the SVG overlay renders the server-projected view
 * for the currently selected asset, so continuous editing never requires
 * manual re-placement. */
"use strict";

const annotation = {
  active: false,
  tool: "rect",
  color: "#d9482b",
  lineWidth: 3,
  markers: [],          // projected markers in current canvas coordinates
  document: null,       // full server document (undo/redo flags)
  drawing: false,
  draft: null,
  selectedId: null,
  loadToken: 0,
};

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function annotationStage() {
  return {
    svg: $("#annotation-svg"),
    layer: $("#annotation-layer"),
    image: $("#preview-image"),
    stage: $("#image-stage"),
  };
}

/* Convert a pointer event to canvas pixel coordinates of the displayed asset.
 * The SVG overlay already matches the contain-fit image rectangle, so we map
 * through its own bounding box into the asset's natural pixel space. */
function eventToCanvasPoint(event) {
  const { svg, image } = annotationStage();
  const rect = svg.getBoundingClientRect();
  const naturalWidth = image.naturalWidth || state.current.width;
  const naturalHeight = image.naturalHeight || state.current.height;
  const scaleX = naturalWidth / rect.width;
  const scaleY = naturalHeight / rect.height;
  const x = (event.clientX - rect.left) * scaleX;
  const y = (event.clientY - rect.top) * scaleY;
  return { x, y, scale: scaleX };
}

async function loadAnnotations() {
  if (!state.current) {
    annotation.markers = [];
    annotation.document = null;
    renderAnnotations();
    updateAnnotationChrome();
    return;
  }
  const token = ++annotation.loadToken;
  const assetId = state.current.id;
  try {
    const [doc, view] = await Promise.all([
      api(`/assets/${assetId}/annotations`),
      api(`/assets/${assetId}/annotations/view`),
    ]);
    if (token !== annotation.loadToken || state.current?.id !== assetId) return;
    annotation.document = doc;
    annotation.markers = view.markers;
    annotation.selectedId = null;
    renderAnnotations();
    updateAnnotationChrome();
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateAnnotationChrome() {
  const hasAsset = Boolean(state.current);
  $("#annotation-toggle").disabled = !hasAsset;
  $("#anno-count").textContent = `${annotation.markers.length} 条批注`;
  $("#annotation-undo").disabled = !annotation.document?.undo_available;
  $("#annotation-redo").disabled = !annotation.document?.redo_available;
  $("#anno-delete-selected").disabled = !annotation.selectedId;
}

function setAnnotationMode(enabled) {
  annotation.active = enabled && Boolean(state.current);
  $("#annotation-bar").hidden = !annotation.active;
  $("#annotation-legend").hidden = !annotation.active;
  $("#annotation-layer").hidden = !annotation.active;
  $("#annotation-toggle").setAttribute("aria-pressed", String(annotation.active));
  if (annotation.active) loadAnnotations();
  else clearDraft();
}

function renderAnnotations() {
  const { svg, image, stage } = annotationStage();
  if (!state.current) return;
  syncOverlayToImage(svg, image, stage);
  svg.setAttribute("viewBox", `0 0 ${state.current.width} ${state.current.height}`);
  svg.innerHTML = "";
  for (const marker of annotation.markers) {
    svg.appendChild(renderMarker(marker));
  }
  if (annotation.draft) svg.appendChild(renderMarker(annotation.draft, true));
}

/* Match the SVG box to the rendered image rectangle (object-fit: contain),
 * so viewBox pixel coordinates line up exactly with the picture content. */
function syncOverlayToImage(svg, image, stage) {
  const naturalWidth = image.naturalWidth || state.current.width;
  const naturalHeight = image.naturalHeight || state.current.height;
  const stageRect = stage.getBoundingClientRect();
  const scale = Math.min(stageRect.width / naturalWidth, stageRect.height / naturalHeight);
  const renderedWidth = naturalWidth * scale;
  const renderedHeight = naturalHeight * scale;
  const left = (stageRect.width - renderedWidth) / 2;
  const top = (stageRect.height - renderedHeight) / 2;
  svg.style.position = "absolute";
  svg.style.left = `${left}px`;
  svg.style.top = `${top}px`;
  svg.style.width = `${renderedWidth}px`;
  svg.style.height = `${renderedHeight}px`;
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
}

function visibilityClass(marker) {
  return `anno-shape anno-${marker.visibility || "inside"}`;
}

function renderMarker(marker, isDraft = false) {
  const group = svgEl("g", {
    class: `${visibilityClass(marker)}${isDraft ? " anno-draft" : ""}${marker.id === annotation.selectedId ? " anno-selected" : ""}`,
    "data-marker": marker.id || "draft",
  });
  const stroke = marker.color || annotation.color;
  const width = marker.line_width || annotation.lineWidth;
  const geometry = marker.geometry;
  const common = { fill: "none", stroke, "stroke-width": width, "stroke-linecap": "round", "stroke-linejoin": "round" };

  if (marker.kind === "rect" && geometry.corners) {
    const points = geometry.corners.map(p => p.join(",")).join(" ");
    group.appendChild(svgEl("polygon", { ...common, points }));
  } else if (marker.kind === "ellipse" && geometry.corners) {
    const xs = geometry.corners.map(p => p[0]);
    const ys = geometry.corners.map(p => p[1]);
    group.appendChild(svgEl("ellipse", {
      ...common,
      cx: (Math.min(...xs) + Math.max(...xs)) / 2,
      cy: (Math.min(...ys) + Math.max(...ys)) / 2,
      rx: Math.abs(Math.max(...xs) - Math.min(...xs)) / 2,
      ry: Math.abs(Math.max(...ys) - Math.min(...ys)) / 2,
    }));
  } else if (geometry.points) {
    const points = geometry.points.map(p => p.join(",")).join(" ");
    if (marker.kind === "arrow" && geometry.points.length >= 2) {
      const [start, end] = [geometry.points[0], geometry.points[geometry.points.length - 1]];
      group.appendChild(svgEl("line", { ...common, x1: start[0], y1: start[1], x2: end[0], y2: end[1] }));
      group.appendChild(arrowHead(start, end, stroke, width));
    } else {
      group.appendChild(svgEl("polyline", { ...common, points }));
    }
  } else if (geometry.anchor) {
    const [x, y] = geometry.anchor;
    group.appendChild(svgEl("circle", { ...common, cx: x, cy: y, r: width + 4, fill: stroke, "fill-opacity": 0.25 }));
  }

  if (marker.label) {
    const anchor = labelAnchor(geometry);
    const text = svgEl("text", {
      x: anchor[0] + 6, y: anchor[1] - 6, class: "anno-label-text",
      "stroke-width": 0, fill: "#2c3a2e",
    });
    text.textContent = marker.label.length > 40 ? marker.label.slice(0, 40) + "…" : marker.label;
    group.appendChild(text);
  }
  if (!isDraft) {
    group.style.cursor = annotation.tool === "select" ? "pointer" : "crosshair";
    group.addEventListener("click", event => {
      event.stopPropagation();
      if (annotation.tool === "select") selectMarker(marker.id);
    });
  }
  return group;
}

function arrowHead(start, end, color, width) {
  const angle = Math.atan2(end[1] - start[1], end[0] - start[0]);
  const size = width * 3 + 6;
  const points = [
    end,
    [end[0] - size * Math.cos(angle - 0.45), end[1] - size * Math.sin(angle - 0.45)],
    [end[0] - size * Math.cos(angle + 0.45), end[1] - size * Math.sin(angle + 0.45)],
  ].map(p => p.join(",")).join(" ");
  return svgEl("polygon", { points, fill: color, stroke: "none" });
}

function labelAnchor(geometry) {
  if (geometry.anchor) return geometry.anchor;
  if (geometry.points) return geometry.points[0];
  if (geometry.corners) return geometry.corners[0];
  return [geometry.x || 0, geometry.y || 0];
}

function selectMarker(id) {
  annotation.selectedId = id;
  const marker = annotation.markers.find(item => item.id === id);
  renderAnnotations();
  updateAnnotationChrome();
  if (marker) {
    $("#annotation-label-input").value = marker.label || "";
    openLabelPopover(marker, true);
  }
}

function clearDraft() {
  annotation.draft = null;
  annotation.drawing = false;
  renderAnnotations();
}

function startDraw(event) {
  if (!annotation.active || annotation.tool === "select") return;
  event.preventDefault();
  const point = eventToCanvasPoint(event);
  annotation.drawing = true;
  const base = {
    id: "draft", kind: annotation.tool, color: annotation.color,
    line_width: annotation.lineWidth, label: "", visibility: "inside",
  };
  if (annotation.tool === "rect" || annotation.tool === "ellipse") {
    annotation.draft = { ...base, geometry: { x: point.x, y: point.y, width: 0, height: 0, corners: rectCorners(point, point) } };
  } else if (annotation.tool === "note") {
    annotation.draft = { ...base, geometry: { anchor: [point.x, point.y] } };
    annotation.drawing = false;
    finishDraft();
    return;
  } else {
    annotation.draft = { ...base, geometry: { points: [[point.x, point.y]] } };
  }
  renderAnnotations();
}

function rectCorners(start, end) {
  return [
    [start.x, start.y], [end.x, start.y], [end.x, end.y], [start.x, end.y],
  ];
}

function moveDraw(event) {
  if (!annotation.drawing || !annotation.draft) return;
  const point = eventToCanvasPoint(event);
  const geometry = annotation.draft.geometry;
  if (annotation.draft.kind === "rect" || annotation.draft.kind === "ellipse") {
    const start = { x: geometry.x, y: geometry.y };
    geometry.width = point.x - start.x;
    geometry.height = point.y - start.y;
    geometry.corners = rectCorners(start, point);
  } else {
    const last = geometry.points[geometry.points.length - 1];
    if (Math.hypot(point.x - last[0], point.y - last[1]) > 3) {
      geometry.points.push([point.x, point.y]);
    }
  }
  renderAnnotations();
}

async function finishDraft() {
  if (!annotation.draft) return;
  const draft = annotation.draft;
  annotation.draft = null;
  annotation.drawing = false;
  const label = await promptLabel(draft);
  const payload = buildPayload(draft, label);
  if (!payload) { renderAnnotations(); return; }
  try {
    annotation.document = await api(`/assets/${state.current.id}/annotations`, {
      method: "POST", body: payload,
    });
    await refreshAfterMutation();
    toast("批注已保存，随画面内容一起移动", "info");
  } catch (error) {
    toast(error.message, "error");
    renderAnnotations();
  }
}

function buildPayload(draft, label) {
  const geometry = draft.geometry;
  if (draft.kind === "rect" || draft.kind === "ellipse") {
    const xs = geometry.corners.map(p => p[0]);
    const ys = geometry.corners.map(p => p[1]);
    const x = Math.min(...xs), y = Math.min(...ys);
    const width = Math.abs(Math.max(...xs) - x), height = Math.abs(Math.max(...ys) - y);
    if (width < 2 || height < 2) return null;
    return { kind: draft.kind, label, color: draft.color, line_width: draft.line_width,
             geometry: { x, y, width, height } };
  }
  if (draft.kind === "note") {
    return { kind: "note", label, color: draft.color, line_width: draft.line_width,
             geometry: { anchor: geometry.anchor } };
  }
  if (geometry.points.length < 2) return null;
  return { kind: draft.kind, label, color: draft.color, line_width: draft.line_width,
           geometry: { points: geometry.points } };
}

function promptLabel(draft) {
  return new Promise(resolve => {
    const pop = $("#annotation-label-pop");
    const input = $("#annotation-label-input");
    input.value = draft.label || "";
    positionPopover(pop, draft);
    pop.hidden = false;
    input.focus();
    const done = value => {
      pop.hidden = true;
      $("#annotation-label-ok").onclick = null;
      $("#annotation-label-skip").onclick = null;
      input.onkeydown = null;
      resolve(value.trim());
    };
    $("#annotation-label-ok").onclick = () => done(input.value);
    $("#annotation-label-skip").onclick = () => done("");
    input.onkeydown = event => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); done(input.value); }
      if (event.key === "Escape") done("");
    };
  });
}

function positionPopover(pop, draft) {
  const { svg } = annotationStage();
  const anchor = labelAnchor(draft.geometry);
  const rect = svg.getBoundingClientRect();
  const layerRect = $("#annotation-layer").getBoundingClientRect();
  const scaleX = rect.width / (state.current?.width || 1);
  const scaleY = rect.height / (state.current?.height || 1);
  // The popover is positioned inside #annotation-layer, whose box matches the
  // contain-fit image rectangle exactly like the SVG element.
  const left = rect.left - layerRect.left + anchor[0] * scaleX;
  const top = rect.top - layerRect.top + anchor[1] * scaleY;
  pop.style.left = `${Math.max(4, Math.min(left, layerRect.width - 240))}px`;
  pop.style.top = `${Math.max(4, Math.min(top, layerRect.height - 120))}px`;
}

function openLabelPopover(marker, isEdit) {
  const pop = $("#annotation-label-pop");
  const input = $("#annotation-label-input");
  positionPopover(pop, { geometry: marker.geometry });
  pop.hidden = false;
  input.focus();
  const close = () => {
    pop.hidden = true;
    $("#annotation-label-ok").onclick = null;
    $("#annotation-label-skip").onclick = null;
    input.onkeydown = null;
  };
  $("#annotation-label-ok").onclick = async () => {
    try {
      annotation.document = await api(
        `/assets/${state.current.id}/annotations/${marker.id}`,
        { method: "PATCH", body: { label: input.value.trim() } });
      close();
      await refreshAfterMutation();
    } catch (error) { toast(error.message, "error"); }
  };
  $("#annotation-label-skip").onclick = close;
  input.onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#annotation-label-ok").click(); }
    if (event.key === "Escape") close();
  };
}

async function refreshAfterMutation() {
  if (!state.current) return;
  const view = await api(`/assets/${state.current.id}/annotations/view`);
  annotation.markers = view.markers;
  annotation.selectedId = null;
  renderAnnotations();
  updateAnnotationChrome();
}

async function undoAnnotation() {
  if (!state.current) return;
  try {
    annotation.document = await api(`/assets/${state.current.id}/annotations/undo`, { method: "POST", body: {} });
    await refreshAfterMutation();
  } catch (error) { toast(error.message, "error"); }
}

async function redoAnnotation() {
  if (!state.current) return;
  try {
    annotation.document = await api(`/assets/${state.current.id}/annotations/redo`, { method: "POST", body: {} });
    await refreshAfterMutation();
  } catch (error) { toast(error.message, "error"); }
}

async function deleteSelected() {
  if (!annotation.selectedId || !state.current) return;
  try {
    annotation.document = await api(
      `/assets/${state.current.id}/annotations/${annotation.selectedId}`, { method: "DELETE" });
    await refreshAfterMutation();
    toast("批注已删除");
  } catch (error) { toast(error.message, "error"); }
}

function bindAnnotationEvents() {
  $("#annotation-toggle").addEventListener("click", () => setAnnotationMode(!annotation.active));
  $$(".anno-tool").forEach(button => button.addEventListener("click", () => {
    annotation.tool = button.dataset.annoTool;
    $$(".anno-tool").forEach(item => item.classList.toggle("active", item === button));
    annotation.selectedId = null;
    updateAnnotationChrome();
  }));
  $("#anno-color").addEventListener("input", event => { annotation.color = event.target.value; });
  $("#anno-width").addEventListener("input", event => { annotation.lineWidth = Number(event.target.value); });
  $("#annotation-undo").addEventListener("click", undoAnnotation);
  $("#annotation-redo").addEventListener("click", redoAnnotation);
  $("#anno-delete-selected").addEventListener("click", deleteSelected);

  const svg = $("#annotation-svg");
  svg.addEventListener("pointerdown", startDraw);
  svg.addEventListener("pointermove", moveDraw);
  svg.addEventListener("pointerup", () => { if (annotation.drawing) finishDraft(); });
  svg.addEventListener("pointerleave", () => { if (annotation.drawing && annotation.tool !== "note") finishDraft(); });
  window.addEventListener("resize", () => { if (annotation.active) renderAnnotations(); });
}

window.annotationController = {
  bind: bindAnnotationEvents,
  onAssetChanged: loadAnnotations,
  setMode: setAnnotationMode,
  isActive: () => annotation.active,
};
