# REST API

地址：`http://127.0.0.1:8765/api`。同源 HTML 页面与 API 由同一个 Python 服务提供。

除图片、ZIP、元数据备份外，成功响应为 `{"ok": true, "data": ...}`；错误响应为 `{"ok": false, "error": {"code": "...", "message": "...", "details": {}}}`。`POST/PATCH` JSON 请求需要 `Content-Type: application/json`，上传图片使用 multipart 的 `file` 字段。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查、版本、功能数量 |
| GET | `/config` | 文件大小、尺寸、批量数量限制 |
| GET | `/catalog` | 全部功能分组、86 种操作与参数定义 |
| GET | `/stats` | 素材、结果、导出、批次统计 |
| GET | `/diagnostics` | 依赖版本、JSON 与文件完整性检查 |
| GET | `/assets` | 素材查询，支持 q/kind/favorite/limit/offset |
| POST | `/assets` | multipart 图片上传 |
| GET | `/assets/{id}` | 素材详细信息 |
| PATCH | `/assets/{id}` | 修改 name、favorite、tags |
| DELETE | `/assets/{id}` | 删除没有下游副本且未被活动任务引用的素材 |
| GET | `/assets/{id}/file` | 读取 PNG 编辑图 |
| GET | `/assets/{id}/thumbnail` | 读取缩略图 |
| GET | `/assets/{id}/history` | 从原图到当前结果的版本链 |
| GET | `/assets/{id}/analysis` | 图像直方图、色谱及亮度分析 |
| POST | `/compare` | 采样误差、PSNR 等比较指标 |
| POST | `/process` | 执行流程并保存新副本 |
| POST | `/preview` | 执行流程并返回临时 PNG，不写入素材库 |
| POST | `/samples` | 幂等生成三个示例素材，body 为 `{}` |
| GET | `/presets` | 列出内置与自定义预设 |
| POST | `/presets` | 创建预设 |
| PATCH | `/presets/{id}` | 修改自定义预设 |
| DELETE | `/presets/{id}` | 删除自定义预设 |
| GET | `/jobs` | 最近 100 条批量任务 |
| POST | `/jobs` | 提交后台批处理，返回 202 |
| GET | `/jobs/{id}` | 查询状态和进度 |
| POST | `/jobs/{id}/cancel` | 申请取消，body 为 `{}` |
| POST | `/exports` | 单图实际格式编码 |
| POST | `/exports/archive` | 多图编码并生成 ZIP |
| GET | `/exports/{id}/download` | 下载导出产物 |
| GET | `/assets/{id}/annotations` | 读取批注文档（根坐标、撤销/重做状态） |
| GET | `/assets/{id}/annotations/view` | 批注投影到当前画布后的渲染数据与可见性 |
| POST | `/assets/{id}/annotations` | 新增一条批注，返回 201 |
| PATCH | `/assets/{id}/annotations/{marker_id}` | 修改批注图形、颜色或说明 |
| DELETE | `/assets/{id}/annotations/{marker_id}` | 删除单条批注 |
| POST | `/assets/{id}/annotations/undo` | 撤销上一次批注变更 |
| POST | `/assets/{id}/annotations/redo` | 重做已撤销的批注变更 |
| GET | `/events` | 最近 100 条操作记录 |
| GET | `/backup` | 下载完整 JSON 元数据快照 |

## 处理请求

```json
{
  "asset_id": "上传响应中的32位id",
  "name": "我的胶片作品",
  "steps": [
    {"operation": "resize_long_edge", "params": {"length": 1600, "upscale": false}},
    {"operation": "filter_sepia", "params": {"strength": 0.8}},
    {"operation": "watermark_text", "params": {"text": "我的作品", "opacity": 0.6}}
  ]
}
```

`name` 可省略。未指定的参数采用 `/catalog` 中的默认值；未知参数、字符串形式数字、布尔值充当数字、非有限浮点数及越界数值都会被拒绝。相互依赖参数（如色阶黑场必须小于白场）在算法执行前继续验证。

成功返回的新素材包含 `id`、`parent_id`、`root_id`、`steps`、`url`、`thumbnail_url`、尺寸、字节数和处理耗时。原图文件不变。

## 批量请求

```json
{
  "title": "电商图统一处理",
  "asset_ids": ["图片1的id", "图片2的id"],
  "steps": [
    {"operation": "resize_pad", "params": {"width": 1000, "height": 1000, "background": "#ffffff"}},
    {"operation": "enhance_sharpness", "params": {"factor": 1.2}}
  ]
}
```

任务状态：`queued → running → completed / partial / failed`。取消使用 `cancelling → cancelled`。服务重启后，未完成任务变为 `interrupted`。批内每张图片独立保存，失败不会阻止后续图片处理。

`results` 保存成功素材的 id；`errors` 保存失败图片 id 和原因；`progress` 为 0–100；`done/total` 表示完成尝试的图片数量。取消时保留已完成结果。

## 导出请求

```json
{
  "asset_id": "结果图片id",
  "options": {
    "format": "WEBP",
    "quality": 88,
    "lossless": false,
    "background": "#ffffff",
    "dpi": 96
  }
}
```

支持 `PNG/JPEG/WEBP/BMP/TIFF/GIF`。响应的 `url` 可直接下载。ZIP 接口将 `asset_id` 改为 `asset_ids` 数组，压缩包中包含安全命名的图片和 `manifest.json`。

导出选项可带 `"include_annotations": true`，此时在最终像素上叠加当前版本可见的批注（含画面外标记的虚线提示）；默认 `false`，导出纯净画面，批注永不修改原图像素。

## 批注与坐标跟随

每条批注以**原片（root image）像素坐标**持久化，类型为 `rect`、`ellipse`、`arrow`、`freehand`、`note`，公共字段为 `kind`、`label`、`color`、`line_width` 与类型相关的 `geometry`。

每个素材记录携带 3×3 射影矩阵 `frame`，表示“当前画布坐标 → 原片坐标”。裁剪、缩放、旋转、翻转、斜切、平移、透视与扩展画布各自贡献一个矩阵并沿版本链复合；调色、滤镜等不移动像素的操作贡献恒等矩阵。`/annotations/view` 用矩阵的逆把根坐标批注投影回当前画布，并对每条标记给出 `inside/partial/outside` 可见性，从而区分仍在画面内、部分被裁出、完全在画面外（标记仍保留）的内容。

```json
{
  "kind": "rect",
  "label": "这里偏色",
  "color": "#d9482b",
  "line_width": 3,
  "geometry": {"x": 120, "y": 90, "width": 60, "height": 60}
}
```

批注文档按素材版本独立快照：处理生成新副本时复制父版本批注，但此后两个编辑分支互不可见、互不修改；旧版本与原片的批注保持可追溯。撤销/重做以整份标记快照实现并随 JSON 持久化，保存后重新打开仍可继续撤销。

## 状态码

- 200：读取、更新、删除或临时预览成功。
- 201：素材、处理结果、预设、导出文件创建成功。
- 202：批量任务已提交。
- 400：参数、格式、图片或尺寸无效。
- 403：跨站写入请求被拒绝。
- 404：记录或接口不存在。
- 409：资源正在使用、存在下游副本、队列已满或处理中取消。
- 410：JSON 记录存在但关联文件丢失。
- 413：上传体积超过限制。
- 415：请求不是合法 JSON Content-Type。
- 500：未预料的服务错误；响应含 request_id，可对照日志。
