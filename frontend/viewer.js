"use strict";

/*
 * Canvas deep-zoom viewer. Paints a small preview immediately, then fetches
 * 256px pyramid tiles for the visible region only. Tile responses populate a
 * bounded LRU cache; rendering pulls from that cache keyed by asset and level,
 * so stale or aborted requests can never paint into the current frame.
 */
class TileViewer {
  constructor(stage, canvas, hooks) {
    this.stage = stage;
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.hooks = hooks;
    this.asset = null;
    this.pyramid = null;
    this.camera = { x: 0, y: 0, scale: 1 };
    this.minScale = 0.01;
    this.maxScale = 32;
    this.fitted = false;
    this.userInteracted = false;
    this.activeLevel = 0;
    this.cache = new Map();
    this.cachePixels = 0;
    this.maxCachePixels = 40 * 1000 * 1000;
    this.previews = new Map();
    this.maxPreviews = 8;
    this.inflight = new Map();
    this.failures = new Map();
    this.needed = new Set();
    this.generation = 0;
    this.pointers = new Map();
    this.pinch = null;
    this.rafId = 0;
    this.settleTimer = 0;
    this.metrics = { receivedBytes: 0, totalReceivedBytes: 0, evictedTiles: 0, fetchedTiles: 0 };
    const patternCanvas = document.createElement("canvas");
    patternCanvas.width = patternCanvas.height = 16;
    const patternCtx = patternCanvas.getContext("2d");
    patternCtx.fillStyle = "#f4f6f0";
    patternCtx.fillRect(0, 0, 16, 16);
    patternCtx.fillStyle = "#dde2d4";
    patternCtx.fillRect(0, 0, 8, 8);
    patternCtx.fillRect(8, 8, 8, 8);
    this.checkerPattern = this.ctx.createPattern(patternCanvas, "repeat");
    this.bindInput();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(stage);
  }

  async setAsset(info) {
    const generation = ++this.generation;
    this.abortInflight();
    this.needed.clear();
    this.asset = info;
    this.pyramid = null;
    this.fitted = false;
    this.userInteracted = false;
    this.metrics.receivedBytes = 0;
    this.canvas.setAttribute("aria-label", info.name || "图像查看器");
    this.requestRender();
    this.emitMetrics();
    this.loadPreview(info, generation);
    try {
      const pyramid = await this.hooks.api(`/assets/${info.id}/pyramid`);
      if (generation !== this.generation) return;
      this.pyramid = pyramid;
      this.fit();
    } catch (error) {
      if (generation === this.generation) this.hooks.onError(error.message);
    }
  }

  reset() {
    this.generation += 1;
    this.abortInflight();
    this.needed.clear();
    this.asset = null;
    this.pyramid = null;
    this.fitted = false;
    this.requestRender();
  }

  viewport() {
    return { width: this.canvas.clientWidth, height: this.canvas.clientHeight };
  }

  resize() {
    const { width, height } = this.viewport();
    if (!width || !height) return;
    const dpr = window.devicePixelRatio || 1;
    const pixelWidth = Math.round(width * dpr);
    const pixelHeight = Math.round(height * dpr);
    if (this.canvas.width !== pixelWidth || this.canvas.height !== pixelHeight) {
      this.canvas.width = pixelWidth;
      this.canvas.height = pixelHeight;
    }
    if (this.pyramid && (!this.fitted || !this.userInteracted)) this.fit();
    else this.requestRender();
  }

  fit() {
    if (!this.pyramid) return;
    const { width, height } = this.viewport();
    if (!width || !height) return;
    this.camera = {
      x: this.pyramid.width / 2,
      y: this.pyramid.height / 2,
      scale: Math.min(width / this.pyramid.width, height / this.pyramid.height),
    };
    this.minScale = this.camera.scale * 0.4;
    this.fitted = true;
    this.userInteracted = false;
    this.afterCameraChange();
  }

  zoomFit() {
    this.userInteracted = false;
    this.fit();
  }

  zoomActual() {
    if (!this.pyramid) return;
    this.camera.scale = 1;
    this.userInteracted = true;
    this.afterCameraChange();
  }

  zoomStep(factor) {
    const { width, height } = this.viewport();
    this.zoomAt(factor, width / 2, height / 2);
  }

  zoomAt(factor, px, py) {
    if (!this.pyramid || !this.fitted) return;
    const { width, height } = this.viewport();
    const previous = this.camera.scale;
    const next = Math.min(this.maxScale, Math.max(this.minScale, previous * factor));
    if (next === previous) return;
    const imageX = (px - width / 2) / previous + this.camera.x;
    const imageY = (py - height / 2) / previous + this.camera.y;
    this.camera.scale = next;
    this.camera.x = imageX - (px - width / 2) / next;
    this.camera.y = imageY - (py - height / 2) / next;
    this.userInteracted = true;
    this.afterCameraChange();
  }

  panBy(dx, dy) {
    if (!this.pyramid || !this.fitted) return;
    this.camera.x -= dx / this.camera.scale;
    this.camera.y -= dy / this.camera.scale;
    this.userInteracted = true;
    this.afterCameraChange();
  }

  afterCameraChange() {
    const { width, height } = this.pyramid;
    this.camera.x = Math.min(width * 1.5, Math.max(-width * 0.5, this.camera.x));
    this.camera.y = Math.min(height * 1.5, Math.max(-height * 0.5, this.camera.y));
    this.requestRender();
    this.scheduleSettle();
    this.emitMetrics();
  }

  chooseLevel() {
    const levels = this.pyramid.levels;
    const dpr = window.devicePixelRatio || 1;
    const ratio = dpr / this.camera.scale;
    const level = Math.floor(Math.log2(Math.max(ratio, 1e-6)));
    return Math.max(0, Math.min(level, levels.length - 1));
  }

  visibleRange(level) {
    const spec = this.pyramid.levels[level];
    const tileSize = this.pyramid.tile_size;
    const { width, height } = this.viewport();
    const factorX = spec.width / this.pyramid.width;
    const factorY = spec.height / this.pyramid.height;
    const imageX0 = this.camera.x - width / 2 / this.camera.scale;
    const imageX1 = this.camera.x + width / 2 / this.camera.scale;
    const imageY0 = this.camera.y - height / 2 / this.camera.scale;
    const imageY1 = this.camera.y + height / 2 / this.camera.scale;
    return {
      x0: Math.max(0, Math.floor(imageX0 * factorX / tileSize)),
      y0: Math.max(0, Math.floor(imageY0 * factorY / tileSize)),
      x1: Math.min(spec.cols - 1, Math.ceil(imageX1 * factorX / tileSize) - 1),
      y1: Math.min(spec.rows - 1, Math.ceil(imageY1 * factorY / tileSize) - 1),
    };
  }

  scheduleSettle() {
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.updateNeeded(), 90);
  }

  updateNeeded() {
    if (!this.pyramid || !this.fitted || !this.asset) return;
    const level = this.chooseLevel();
    this.activeLevel = level;
    const range = this.visibleRange(level);
    const needed = new Set();
    for (let y = range.y0; y <= range.y1; y++) {
      for (let x = range.x0; x <= range.x1; x++) {
        needed.add(`${this.asset.id}/${level}/${x}/${y}`);
      }
    }
    this.needed = needed;
    for (const [key, controller] of this.inflight) {
      if (!needed.has(key)) {
        controller.abort();
        this.inflight.delete(key);
      }
    }
    for (const key of needed) {
      const entry = this.cache.get(key);
      if (entry) {
        this.cache.delete(key);
        this.cache.set(key, entry);
      } else if (!this.inflight.has(key)) {
        this.fetchTile(key);
      }
    }
    this.requestRender();
    this.emitMetrics();
  }

  tileUrl(assetId, level, x, y) {
    return `/api/assets/${assetId}/tiles/${level}/${x}/${y}`;
  }

  async fetchTile(key) {
    const [assetId, level, x, y] = key.split("/");
    if (!this.asset || assetId !== this.asset.id) return;
    if ((this.failures.get(key) || 0) >= 3) return;
    const controller = new AbortController();
    this.inflight.set(key, controller);
    try {
      const response = await fetch(this.tileUrl(assetId, Number(level), x, y), { signal: controller.signal });
      if (!response.ok) throw new Error(`瓦片请求失败（${response.status}）`);
      const blob = await response.blob();
      const bitmap = await createImageBitmap(blob);
      this.inflight.delete(key);
      this.accountBytes(blob.size, assetId);
      this.metrics.fetchedTiles += 1;
      this.cacheSet(key, bitmap);
      if (this.asset && assetId === this.asset.id) this.requestRender();
    } catch (error) {
      this.inflight.delete(key);
      if (error.name === "AbortError") return;
      const attempts = (this.failures.get(key) || 0) + 1;
      this.failures.set(key, attempts);
      if (this.needed.has(key) && attempts < 3) {
        setTimeout(() => { if (this.needed.has(key)) this.fetchTile(key); }, 400 * attempts);
      }
    } finally {
      this.emitMetrics();
    }
  }

  async loadPreview(info, generation) {
    const cached = this.previews.get(info.id);
    if (cached) {
      this.previews.delete(info.id);
      this.previews.set(info.id, cached);
      this.requestRender();
      return;
    }
    try {
      const response = await fetch(info.previewUrl);
      if (!response.ok) throw new Error(`预览加载失败（${response.status}）`);
      const blob = await response.blob();
      const bitmap = await createImageBitmap(blob);
      this.accountBytes(blob.size, info.id);
      this.previews.set(info.id, { bitmap, pixels: bitmap.width * bitmap.height });
      while (this.previews.size > this.maxPreviews) {
        const [oldestKey, oldest] = this.previews.entries().next().value;
        oldest.bitmap.close();
        this.previews.delete(oldestKey);
      }
      if (generation === this.generation) this.requestRender();
    } catch (error) {
      if (generation === this.generation) this.hooks.onError(error.message);
    }
  }

  accountBytes(bytes, assetId) {
    this.metrics.totalReceivedBytes += bytes;
    if (this.asset && assetId === this.asset.id) this.metrics.receivedBytes += bytes;
  }

  cacheSet(key, bitmap) {
    const pixels = bitmap.width * bitmap.height;
    const existing = this.cache.get(key);
    if (existing) {
      this.cachePixels -= existing.pixels;
      existing.bitmap.close();
      this.cache.delete(key);
    }
    this.cache.set(key, { bitmap, pixels });
    this.cachePixels += pixels;
    for (const [oldKey, entry] of this.cache) {
      if (this.cachePixels <= this.maxCachePixels) break;
      if (this.needed.has(oldKey)) continue;
      this.cache.delete(oldKey);
      this.cachePixels -= entry.pixels;
      entry.bitmap.close();
      this.metrics.evictedTiles += 1;
    }
  }

  abortInflight() {
    for (const controller of this.inflight.values()) controller.abort();
    this.inflight.clear();
  }

  requestRender() {
    if (this.rafId) return;
    this.rafId = requestAnimationFrame(() => {
      this.rafId = 0;
      this.render();
    });
  }

  render() {
    const { ctx, canvas } = this;
    const dpr = window.devicePixelRatio || 1;
    const { width, height } = this.viewport();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!this.pyramid || !this.fitted || !this.asset) return;
    const imageWidth = this.pyramid.width;
    const imageHeight = this.pyramid.height;
    const sx = imageX => (imageX - this.camera.x) * this.camera.scale + width / 2;
    const sy = imageY => (imageY - this.camera.y) * this.camera.scale + height / 2;
    const left = sx(0);
    const top = sy(0);
    const right = sx(imageWidth);
    const bottom = sy(imageHeight);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, right - left, bottom - top);
    ctx.clip();
    ctx.translate(left, top);
    ctx.fillStyle = this.checkerPattern;
    ctx.fillRect(0, 0, right - left, bottom - top);
    ctx.restore();
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, right - left, bottom - top);
    ctx.clip();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    const preview = this.previews.get(this.asset.id);
    if (preview) ctx.drawImage(preview.bitmap, left, top, right - left, bottom - top);
    const target = this.activeLevel;
    const maxLevel = this.pyramid.levels.length - 1;
    for (let level = Math.min(maxLevel, target + 2); level > target; level--) {
      this.drawLevel(ctx, level, sx, sy);
    }
    this.drawLevel(ctx, target, sx, sy);
    ctx.restore();
  }

  drawLevel(ctx, level, sx, sy) {
    const spec = this.pyramid.levels[level];
    const tileSize = this.pyramid.tile_size;
    const factorX = spec.width / this.pyramid.width;
    const factorY = spec.height / this.pyramid.height;
    const range = this.visibleRange(level);
    for (let ty = range.y0; ty <= range.y1; ty++) {
      for (let tx = range.x0; tx <= range.x1; tx++) {
        const entry = this.cache.get(`${this.asset.id}/${level}/${tx}/${ty}`);
        if (!entry) continue;
        const tileLeft = tx * tileSize / factorX;
        const tileTop = ty * tileSize / factorY;
        const tileRight = Math.min((tx + 1) * tileSize, spec.width) / factorX;
        const tileBottom = Math.min((ty + 1) * tileSize, spec.height) / factorY;
        const dx = sx(tileLeft);
        const dy = sy(tileTop);
        ctx.drawImage(entry.bitmap, dx, dy, sx(tileRight) - dx, sy(tileBottom) - dy);
      }
    }
  }

  emitMetrics() {
    this.hooks.onMetrics({
      receivedBytes: this.metrics.receivedBytes,
      totalReceivedBytes: this.metrics.totalReceivedBytes,
      fullBytes: this.asset ? this.asset.bytes : 0,
      cachedTiles: this.cache.size,
      cachedPixels: this.cachePixels,
      evictedTiles: this.metrics.evictedTiles,
      fetchedTiles: this.metrics.fetchedTiles,
      inflight: this.inflight.size,
      needed: this.needed.size,
      level: this.activeLevel,
      zoom: this.camera.scale,
    });
  }

  bindInput() {
    this.canvas.tabIndex = 0;
    this.canvas.addEventListener("pointerdown", event => {
      if (!this.pyramid) return;
      this.canvas.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      this.canvas.classList.add("dragging");
    });
    this.canvas.addEventListener("pointermove", event => {
      if (!this.pointers.has(event.pointerId)) return;
      const previous = this.pointers.get(event.pointerId);
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (this.pointers.size === 1) {
        this.panBy(event.clientX - previous.x, event.clientY - previous.y);
      } else if (this.pointers.size === 2) {
        const points = [...this.pointers.values()];
        const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
        const centerX = (points[0].x + points[1].x) / 2;
        const centerY = (points[0].y + points[1].y) / 2;
        if (this.pinch && this.pinch.distance > 0) {
          const rect = this.canvas.getBoundingClientRect();
          this.panBy(centerX - this.pinch.centerX, centerY - this.pinch.centerY);
          this.zoomAt(distance / this.pinch.distance, centerX - rect.left, centerY - rect.top);
        }
        this.pinch = { distance, centerX, centerY };
      }
    });
    const release = event => {
      this.pointers.delete(event.pointerId);
      if (this.pointers.size < 2) this.pinch = null;
      if (!this.pointers.size) this.canvas.classList.remove("dragging");
    };
    this.canvas.addEventListener("pointerup", release);
    this.canvas.addEventListener("pointercancel", release);
    this.canvas.addEventListener("wheel", event => {
      if (!this.pyramid) return;
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * (event.deltaMode === 1 ? 0.05 : 0.0016));
      const rect = this.canvas.getBoundingClientRect();
      this.zoomAt(factor, event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });
    this.canvas.addEventListener("dblclick", event => {
      if (!this.pyramid) return;
      const rect = this.canvas.getBoundingClientRect();
      this.zoomAt(event.shiftKey ? 0.5 : 2, event.clientX - rect.left, event.clientY - rect.top);
    });
    this.canvas.addEventListener("keydown", event => {
      if (!this.pyramid) return;
      const actions = {
        "+": () => this.zoomStep(1.25),
        "=": () => this.zoomStep(1.25),
        "-": () => this.zoomStep(0.8),
        "0": () => this.zoomFit(),
        "1": () => this.zoomActual(),
        ArrowLeft: () => this.panBy(60, 0),
        ArrowRight: () => this.panBy(-60, 0),
        ArrowUp: () => this.panBy(0, 60),
        ArrowDown: () => this.panBy(0, -60),
      };
      const action = actions[event.key];
      if (action) {
        event.preventDefault();
        action();
      }
    });
  }
}

window.TileViewer = TileViewer;
