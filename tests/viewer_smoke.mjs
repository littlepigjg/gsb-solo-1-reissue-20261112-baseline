// Logic smoke test for frontend/viewer.js under a minimal DOM mock.
// Run: node tests/viewer_smoke.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

function makeCtx() {
  const calls = { drawImage: [] };
  return {
    calls,
    setTransform() {}, clearRect() {}, save() {}, restore() {},
    beginPath() {}, rect() {}, clip() {}, translate() {}, fillRect() {},
    createPattern: () => ({}),
    drawImage(...args) { calls.drawImage.push(args); },
    set fillStyle(value) {}, get fillStyle() { return null; },
    set imageSmoothingEnabled(value) {}, set imageSmoothingQuality(value) {},
  };
}

class MockCanvas {
  constructor() {
    this.clientWidth = 1280;
    this.clientHeight = 800;
    this.width = 0;
    this.height = 0;
    this.listeners = {};
    this.classList = { add() {}, remove() {} };
    this.ctx = makeCtx();
  }
  getContext() { return this.ctx; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setPointerCapture() {}
  setAttribute() {}
  getBoundingClientRect() { return { left: 0, top: 0 }; }
}

const contexts = [];
global.window = global;
global.devicePixelRatio = 1;
global.document = { createElement: () => new MockCanvas() };
global.ResizeObserver = class { observe() {} };
global.requestAnimationFrame = callback => { callback(); return 1; };
global.AbortController = class {
  constructor() { this.signal = { aborted: false }; contexts.push(this); }
  abort() { this.signal.aborted = true; }
};
const fetchLog = [];
global.fetch = async (url, options = {}) => {
  fetchLog.push(url);
  if (url.includes("/tiles/")) return new Promise(() => {}); // hangs until aborted
  return { ok: true, blob: async () => ({ size: 1000 }) };
};
global.createImageBitmap = async blob => ({ width: 256, height: 256, closed: false, close() { this.closed = true; } });

eval(readFileSync(join(root, "frontend", "viewer.js"), "utf8"));

const pyramid = {
  asset_id: "a", width: 3000, height: 2000, tile_size: 256, full_bytes: 20_000_000,
  levels: [
    { level: 0, downsample: 1, width: 3000, height: 2000, cols: 12, rows: 8 },
    { level: 1, downsample: 2, width: 1500, height: 1000, cols: 6, rows: 4 },
    { level: 2, downsample: 4, width: 750, height: 500, cols: 3, rows: 2 },
    { level: 3, downsample: 8, width: 375, height: 250, cols: 2, rows: 1 },
    { level: 4, downsample: 16, width: 188, height: 125, cols: 1, rows: 1 },
  ],
  preview: { width: 1024, height: 683 },
};

let failures = 0;
function check(name, condition) {
  if (!condition) { failures += 1; console.error(`FAIL ${name}`); }
  else console.log(`ok   ${name}`);
}

const canvas = new MockCanvas();
const viewer = new TileViewer({}, canvas, {
  api: async () => pyramid,
  onMetrics() {},
  onError(message) { console.error("viewer error:", message); failures += 1; },
});

await viewer.setAsset({ id: "a", name: "测试", bytes: 20_000_000, previewUrl: "/api/assets/a/preview" });
check("fit uses the smaller dimension ratio", Math.abs(viewer.camera.scale - 0.4) < 1e-9);
check("fit centers the image", viewer.camera.x === 1500 && viewer.camera.y === 1000);
check("fit picks the level matching screen density", viewer.chooseLevel() === 1);

viewer.updateNeeded();
check("needed tiles belong to the current asset and level", [...viewer.needed].every(key => key.startsWith("a/1/")));
check("needed tiles cover the viewport", viewer.needed.size === 6 * 4);
check("tile fetches were issued only for visible tiles", fetchLog.filter(url => url.includes("/tiles/")).length === viewer.needed.size);
check("inflight requests are tracked", viewer.inflight.size === viewer.needed.size);

// Switching assets must abort every pending tile request of the previous one.
const pendingBeforeSwitch = contexts.length;
await viewer.setAsset({ id: "b", name: "下一张", bytes: 20_000_000, previewUrl: "/api/assets/b/preview" });
check("switching assets aborts all pending requests", contexts.slice(0, pendingBeforeSwitch).every(controller => controller.signal.aborted));
check("switching clears the inflight registry", viewer.inflight.size === 0);

// Zooming to 100% selects level 0 (real pixels, not an upscaled thumbnail).
viewer.camera.scale = 1;
check("100% zoom selects level 0", viewer.chooseLevel() === 0);
viewer.camera.scale = 0.1;
check("zooming out selects a coarser level", viewer.chooseLevel() === 3);

// LRU eviction respects the pixel budget and never drops visible tiles.
viewer.asset = { id: "b" };
viewer.maxCachePixels = 256 * 256 * 4;
viewer.needed = new Set(["b/0/0/0", "b/0/1/0"]);
for (const key of ["b/0/0/0", "b/0/1/0", "b/0/2/0", "b/0/3/0", "b/0/4/0", "b/0/5/0"]) {
  viewer.cacheSet(key, { width: 256, height: 256, closed: false, close() { this.closed = true; } });
}
check("cache is bounded by the pixel budget", viewer.cachePixels <= viewer.maxCachePixels);
check("visible tiles survive eviction", viewer.cache.has("b/0/0/0") && viewer.cache.has("b/0/1/0"));
check("evictions are counted for the metrics panel", viewer.metrics.evictedTiles >= 2);

// Rendering pulls only the current asset's tiles from the cache.
viewer.pyramid = pyramid;
viewer.fitted = true;
viewer.activeLevel = 1;
viewer.camera = { x: 1500, y: 1000, scale: 0.4 };
viewer.cache.clear();
viewer.cachePixels = 0;
const foreign = { width: 256, height: 256, tag: "foreign", close() {} };
const own = { width: 256, height: 256, tag: "own", close() {} };
viewer.cacheSet("a/1/0/0", foreign);
viewer.cacheSet("b/1/0/0", own);
canvas.ctx.calls.drawImage.length = 0;
viewer.render();
const painted = canvas.ctx.calls.drawImage.map(args => args[0].tag).filter(Boolean);
check("render never paints another asset's tiles", painted.includes("own") && !painted.includes("foreign"));

// Adjacent tiles share exact edges: range math must be gapless.
const range = viewer.visibleRange(1);
const spec = pyramid.levels[1];
check("visible range stays inside the level grid",
  range.x0 >= 0 && range.y0 >= 0 && range.x1 < spec.cols && range.y1 < spec.rows);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("viewer smoke test passed");
