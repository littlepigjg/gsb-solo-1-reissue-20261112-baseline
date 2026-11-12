"""Compile the curated operation definitions into readable Python catalogs.

Generated files are committed application source: the server imports them
directly. This compiler is a maintenance tool, never run during app startup.
"""
from pathlib import Path
import os
import pprint


ROOT = Path(os.path.relpath(Path(__file__).parents[1]))
OPERATIONS = []
CATEGORIES = [
    {"id": "crop", "title": "裁剪构图", "icon": "crop", "subtitle": "留下画面的重点", "number": "01"},
    {"id": "resize", "title": "尺寸调整", "icon": "resize", "subtitle": "适配每一种画幅", "number": "02"},
    {"id": "transform", "title": "旋转翻转", "icon": "rotate", "subtitle": "换个角度看世界", "number": "03"},
    {"id": "color", "title": "色彩调整", "icon": "sun", "subtitle": "让色彩恰到好处", "number": "04"},
    {"id": "filter", "title": "艺术滤镜", "icon": "spark", "subtitle": "找到你的影像风格", "number": "05"},
    {"id": "enhance", "title": "降噪锐化", "icon": "focus", "subtitle": "照顾每一处细节", "number": "06"},
    {"id": "edge", "title": "边缘检测", "icon": "layers", "subtitle": "探索图像的轮廓", "number": "07"},
    {"id": "watermark", "title": "水印标注", "icon": "type", "subtitle": "为作品留下署名", "number": "08"},
    {"id": "format", "title": "格式转换", "icon": "file", "subtitle": "轻松分享与交付", "number": "09"},
    {"id": "batch", "title": "批量处理", "icon": "grid", "subtitle": "一次完成一组作品", "number": "10"},
]


def number(name, label, default, minimum, maximum, step=1, help="", unit="", group="基础参数", odd=False):
    field = {
        "name": name,
        "label": label,
        "type": "integer" if isinstance(default, int) and isinstance(step, int) else "number",
        "default": default,
        "min": minimum,
        "max": maximum,
        "step": step,
        "help": help or f"调整{label}，有效范围为 {minimum} 至 {maximum}。",
        "unit": unit,
        "group": group,
    }
    if odd:
        field["odd"] = True
    return field


def choice(name, label, default, options, help=""):
    return {
        "name": name,
        "label": label,
        "type": "select",
        "default": default,
        "options": [{"value": value, "label": title} for value, title in options],
        "help": help or f"选择{label}。",
        "group": "基础参数",
    }


def boolean(name, label, default, help):
    return {"name": name, "label": label, "type": "boolean", "default": default,
            "help": help, "group": "处理选项"}


def color(name, label, default, help=""):
    return {"name": name, "label": label, "type": "color", "default": default,
            "help": help or f"选择{label}，使用 RGB 十六进制颜色。", "group": "色彩"}


def text(name, label, default, maximum=120):
    return {"name": name, "label": label, "type": "text", "default": default,
            "max_length": maximum, "help": f"最多 {maximum} 个字符，支持中文。", "group": "文字"}


def op(key, title, category, module, description, algorithm, fields, dimensions=True, alpha=True):
    OPERATIONS.append({
        "id": key,
        "title": title,
        "category": category,
        "module": module,
        "description": description,
        "algorithm": algorithm,
        "input_modes": ["RGB", "RGBA", "L", "P"],
        "output_mode": "RGBA",
        "preserves_dimensions": dimensions,
        "preserves_alpha": alpha,
        "parameters": fields,
        "example": {"operation": key, "params": {field["name"]: field["default"] for field in fields}},
    })


def strength(default=1.0):
    return number("strength", "效果强度", default, 0, 1, 0.05, "0 为原图，1 为完整效果。")


def size_fields(width=1200, height=800):
    return [number("width", "目标宽度", width, 1, 8192, unit="px"),
            number("height", "目标高度", height, 1, 8192, unit="px")]


def sampling():
    return choice("resampling", "重采样方式", "lanczos", [
        ("lanczos", "Lanczos · 照片推荐"), ("bicubic", "双三次"),
        ("bilinear", "双线性"), ("nearest", "最近邻 · 像素画")])


def anchors():
    return [number("anchor_x", "水平锚点", 50, 0, 100, unit="%"),
            number("anchor_y", "垂直锚点", 50, 0, 100, unit="%")]


op("crop_rect", "自由裁剪", "crop", "geometry", "用像素坐标精确选取图像区域。", "Axis-aligned rectangle crop", [
    number("left", "左侧起点", 0, 0, 8191, unit="px"),
    number("top", "顶部起点", 0, 0, 8191, unit="px"),
    *size_fields(256, 256)], False)
op("crop_center", "中心裁剪", "crop", "geometry", "保留中心区域，按百分比收紧构图。", "Centered proportional crop", [
    number("percent", "保留比例", 80, 1, 100, unit="%")], False)
op("crop_ratio", "比例裁剪", "crop", "geometry", "按自定义宽高比裁剪，可移动构图中心。", "Aspect ratio crop with anchor", [
    number("ratio_width", "宽比例", 16, 1, 100), number("ratio_height", "高比例", 9, 1, 100), *anchors()], False)
op("crop_square", "方形构图", "crop", "geometry", "截取方形画幅，适合头像和社交媒体。", "Anchored square crop", [
    choice("anchor", "对齐位置", "center", [("start", "起始边"), ("center", "中心"), ("end", "末尾边")])], False)
op("crop_circle", "圆形头像", "crop", "geometry", "从中心生成圆形透明头像，支持柔和边缘。", "Elliptic alpha mask", [
    number("inset", "内缩比例", 0, 0, 45, unit="%"), number("feather", "边缘羽化", 0.0, 0, 20, 0.5, unit="px")], False, False)
op("crop_trim", "去除纯色边框", "crop", "geometry", "检测背景色差，自动裁掉空白边缘。", "RGB maximum difference bounding box", [
    color("background", "背景颜色", "#ffffff"), number("tolerance", "颜色容差", 16, 0, 200),
    number("padding", "保留边距", 0, 0, 500, unit="px")], False)
op("crop_alpha", "裁掉透明边缘", "crop", "geometry", "根据透明通道寻找有效内容范围。", "Alpha threshold bounding box", [
    number("threshold", "透明阈值", 0, 0, 254)], False)

op("resize_exact", "指定尺寸", "resize", "geometry", "精确设置宽高；宽高比不同时会拉伸。", "Selectable resampling", [*size_fields(), sampling()], False)
op("resize_percent", "等比缩放", "resize", "geometry", "以百分比放大或缩小，保持原始比例。", "Uniform scale", [
    number("percent", "缩放比例", 75, 1, 400, unit="%"), sampling()], False)
op("resize_fit", "适应边界", "resize", "geometry", "保持比例放入目标边界，输出不留白。", "Contain without padding", [
    *size_fields(), boolean("upscale", "允许放大", False, "关闭时小图保持原始尺寸。")], False)
op("resize_fill", "铺满画幅", "resize", "geometry", "等比填满目标尺寸，裁去溢出的部分。", "Cover resampling with anchor", [*size_fields(), *anchors()], False)
op("resize_pad", "留白适配", "resize", "geometry", "完整保留内容，在空余位置填充背景。", "Contain and alpha composite", [
    *size_fields(), color("background", "填充颜色", "#f4f0e8")], False, False)
op("resize_long_edge", "限制最长边", "resize", "geometry", "适合统一一组图片的最大尺寸。", "Longest-side constraint", [
    number("length", "最长边", 1600, 1, 8192, unit="px"), boolean("upscale", "允许放大", False, "关闭时仅缩小超出最长边的图片。")], False)
op("resize_canvas", "调整画布", "resize", "geometry", "内容居中，扩展或裁切画布而不缩放内容。", "Centered canvas composition", [
    *size_fields(), color("background", "画布背景", "#ffffff"),
    boolean("transparent", "透明背景", True, "扩展区域使用透明通道。")], False, False)

op("transform_rotate", "自由旋转", "transform", "geometry", "按任意角度逆时针旋转，选择透明留边。", "Bicubic rotation", [
    number("angle", "旋转角度", 15.0, -180, 180, 0.5, unit="°"),
    boolean("expand", "扩展画布", True, "保留旋转后的完整内容。"),
    boolean("transparent", "透明留边", True, "空白区域透明。"), color("background", "留边颜色", "#ffffff")], False, False)
op("transform_quarter", "直角旋转", "transform", "geometry", "无插值旋转，保持像素清晰。", "Lossless pixel transpose", [
    choice("angle", "逆时针角度", "90", [("90", "90°"), ("180", "180°"), ("270", "270°")])], False)
op("transform_flip", "镜像翻转", "transform", "geometry", "水平、垂直或同时翻转画面。", "Horizontal / vertical transpose", [
    choice("axis", "翻转方向", "horizontal", [("horizontal", "水平"), ("vertical", "垂直"), ("both", "水平 + 垂直")])])
op("transform_transpose", "对角转置", "transform", "geometry", "沿对角线镜像图像并交换宽高。", "Diagonal transpose", [
    choice("diagonal", "对角线", "main", [("main", "主对角线"), ("anti", "副对角线")])], False)
op("transform_shear", "斜切变形", "transform", "geometry", "对图像施加水平或垂直剪切变换。", "Inverse affine shear", [
    number("horizontal", "水平斜切", 10, -80, 80, unit="%"),
    number("vertical", "垂直斜切", 0, -80, 80, unit="%"), color("background", "留边颜色", "#ffffff")], True, False)
op("transform_translate", "画面平移", "transform", "geometry", "移动图像内容，可将越界像素循环到另一侧。", "Pixel offset / alpha composition", [
    number("x", "水平位移", 10, -100, 100, unit="%"), number("y", "垂直位移", 0, -100, 100, unit="%"),
    boolean("wrap", "循环平移", False, "越界像素从另一侧进入画布。")], True, False)
op("transform_perspective", "梯形透视", "transform", "geometry", "模拟上下透视变形，适合创意构图。", "Quadrilateral warp", [
    number("amount", "透视程度", 25, -90, 90, unit="%")])
op("transform_mirror_tile", "四向镜像", "transform", "geometry", "拼接四个镜像画面，形成对称图案。", "Four-quadrant mirror tiling", [
    boolean("keep_size", "保持原尺寸", True, "将四格拼图缩回原图大小。")], False)

for key, title, description, default, maximum in [
    ("brightness", "亮度", "整体提亮或压暗图像。", 1.15, 3),
    ("contrast", "对比度", "增加明暗分离或降低画面反差。", 1.2, 3),
    ("saturation", "饱和度", "调整色彩浓度，0 可获得灰度图。", 1.2, 3),
]:
    op(f"color_{key}", title, "color", "color", description, f"Pillow {key} enhancement", [
        number("factor", "调整系数", default, 0, maximum, 0.05, "1 为原图，小于 1 减弱，大于 1 增强。")])
op("color_exposure", "曝光补偿", "color", "color", "在线性光空间中以 EV 调整曝光。", "sRGB transfer → linear exposure → sRGB", [
    number("stops", "曝光补偿", 0.5, -4, 4, 0.1, "每增加 1 EV，线性光强增加一倍。", "EV")])
op("color_gamma", "伽马校正", "color", "color", "调整中间调，保留纯黑和纯白。", "Power-law gamma transfer", [
    number("gamma", "Gamma", 1.2, 0.1, 5, 0.05)])
op("color_temperature", "色温与色调", "color", "color", "在冷暖和绿品红方向微调白平衡。", "RGB temperature and tint offsets", [
    number("temperature", "冷暖色温", 15, -100, 100), number("tint", "绿 / 品红", 0, -100, 100)])
op("color_hue", "色相偏移", "color", "color", "在 HSV 色环上旋转色相。", "HSV circular hue shift", [
    number("degrees", "色相角度", 30, -180, 180, unit="°")])
op("color_balance", "通道平衡", "color", "color", "独立控制 RGB 通道增益。", "Per-channel gain with optional luminance normalization", [
    number("red", "红色增益", 1.05, 0, 3, 0.05), number("green", "绿色增益", 1.0, 0, 3, 0.05),
    number("blue", "蓝色增益", 0.95, 0, 3, 0.05), boolean("preserve_luminance", "保持亮度", True, "按调整前的亮度归一化。")])
op("color_levels", "色阶", "color", "color", "调整输入黑白场和中间调。", "Clipped input range and gamma", [
    number("black", "输入黑场", 12, 0, 254), number("white", "输入白场", 243, 1, 255),
    number("gamma", "中间调", 1.0, 0.1, 5, 0.05)])
op("color_shadows", "阴影与高光", "color", "color", "提亮暗部、压低亮部，恢复画面层次。", "Smoothstep luminance masks", [
    number("shadows", "阴影提亮", 20, -100, 100), number("highlights", "高光压低", 15, -100, 100)])
op("color_grayscale", "黑白影像", "color", "color", "选择灰度算法，并与原图混合。", "Luminance / mean / desaturation grayscale", [
    choice("method", "灰度算法", "luminance", [("luminance", "感知亮度"), ("average", "通道平均"), ("desaturate", "去饱和")]), strength()])
op("color_invert", "色彩反相", "color", "color", "反转全部或单个颜色通道。", "255 minus channel", [
    choice("channel", "处理通道", "all", [("all", "所有通道"), ("red", "红色"), ("green", "绿色"), ("blue", "蓝色")])])
op("color_autocontrast", "自动对比度", "color", "color", "裁去极端亮度值后自动拉伸色阶。", "Histogram cutoff and stretch", [
    number("cutoff", "两端裁切", 1.0, 0, 20, 0.5, unit="%"), boolean("preserve_tone", "保持整体色调", True, "使用合并直方图保持色调。")])
op("color_equalize", "直方图均衡", "color", "color", "重新分配亮度以增强局部层次。", "Histogram equalization in YCbCr or RGB", [
    choice("channel", "均衡空间", "luminance", [("luminance", "仅亮度"), ("rgb", "RGB 独立")]), strength(0.6)])
op("color_duotone", "双色调", "color", "color", "用两种颜色重映射灰度影像。", "Two-color luminance interpolation", [
    color("shadow", "暗部颜色", "#203c3b"), color("highlight", "亮部颜色", "#eadab5"), strength()])
op("color_threshold", "二值化", "color", "color", "按亮度阈值分成明暗两种颜色。", "Fixed luminance threshold", [
    number("threshold", "亮度阈值", 128, 0, 255), color("dark", "暗部颜色", "#151b19"), color("light", "亮部颜色", "#ffffff")])
op("color_vibrance", "自然饱和度", "color", "color", "优先增强低饱和区域，减少过度饱和。", "Saturation-adaptive chroma gain", [
    number("amount", "自然饱和度", 35, -100, 100)])
op("color_channel_mixer", "通道重排", "color", "color", "交换 RGB 通道，创造特殊色彩。", "Channel permutation and blending", [
    choice("order", "通道顺序", "bgr", [(item, item.upper()) for item in ["rgb", "rbg", "grb", "gbr", "brg", "bgr"]]), strength()])

op("filter_sepia", "复古棕褐", "filter", "artistic", "将影像转换为温暖的老照片色调。", "Sepia color matrix", [strength(0.85)])
op("filter_vintage", "胶片时光", "filter", "artistic", "柔化黑白场，叠加暖调与暗角。", "Lifted blacks, warm gain and vignette", [
    strength(0.8), number("saturation", "色彩浓度", 0.8, 0, 2, 0.05), number("vignette", "暗角强度", 0.45, 0, 1, 0.05)])
op("filter_vignette", "柔和暗角", "filter", "artistic", "逐渐压暗画面四周，让视线聚焦中心。", "Smooth radial attenuation", [
    strength(0.7), number("radius", "起始半径", 0.45, 0, 0.95, 0.05)])
op("filter_grain", "胶片颗粒", "filter", "artistic", "添加可复现的单色或彩色高斯颗粒。", "Seeded Gaussian noise", [
    number("amount", "颗粒强度", 12.0, 0, 80, 0.5), number("seed", "随机种子", 42, 0, 999999),
    boolean("monochrome", "单色颗粒", True, "三个通道使用相同颗粒。")])
op("filter_pixelate", "像素风格", "filter", "artistic", "把画面变成清晰的像素色块。", "Box downsample and nearest upsample", [
    number("block", "像素块大小", 12, 2, 128, unit="px")])
op("filter_posterize", "海报色阶", "filter", "artistic", "减少每通道位深，形成大块分层色彩。", "Bit-depth posterization", [number("bits", "每通道位数", 4, 1, 8)])
op("filter_solarize", "曝光反转", "filter", "artistic", "反转超过阈值的像素，创造超现实效果。", "Threshold solarization", [number("threshold", "反转阈值", 128, 0, 255)])
op("filter_sketch", "铅笔素描", "filter", "artistic", "用反相模糊和颜色减淡模拟铅笔笔触。", "Gaussian color-dodge sketch", [
    number("radius", "笔触半径", 8.0, 0.5, 40, 0.5, unit="px"), boolean("color", "保留颜色", False, "将素描与原始颜色相乘。")])
op("filter_cartoon", "卡通插画", "filter", "artistic", "结合中值平滑、海报化与黑色轮廓。", "Median smoothing, quantization and edge mask", [
    number("smooth", "平滑窗口", 5, 3, 9, 2, odd=True), number("bits", "颜色位数", 4, 2, 7), number("threshold", "轮廓阈值", 45, 1, 255)])
op("filter_emboss", "浮雕质感", "filter", "artistic", "利用方向卷积突出立体表面纹理。", "Pillow emboss convolution", [strength(0.8)])
op("filter_halftone", "印刷网点", "filter", "artistic", "按灰度生成大小不同的圆形网点。", "Luminance-to-dot-area halftone", [
    number("cell", "网点间距", 12, 6, 100, unit="px"), color("paper", "纸张颜色", "#f3ead7"), color("ink", "油墨颜色", "#253a35")])
op("filter_gradient_map", "三色渐变映射", "filter", "artistic", "为暗部、中间调、亮部分别指定颜色。", "Three-stop luminance mapping", [
    color("shadow", "暗部颜色", "#182c3d"), color("midtone", "中间颜色", "#bd7867"), color("highlight", "亮部颜色", "#f8e7bc"),
    number("midpoint", "中间调位置", 128, 1, 254), strength(0.8)])
op("filter_light_leak", "暖光漏光", "filter", "artistic", "在画面一侧叠加柔和的彩色漏光。", "Gaussian field with screen blending", [
    choice("side", "光线方向", "left", [("left", "左侧"), ("right", "右侧")]), color("color", "光线颜色", "#ff864d"),
    number("spread", "扩散范围", 0.8, 0.1, 2, 0.05), strength(0.65)])
op("filter_scanlines", "复古扫描线", "filter", "artistic", "为画面叠加显示器扫描线纹理。", "Periodic row attenuation", [
    number("spacing", "扫描线间隔", 4, 2, 32, unit="px"), strength(0.3)])

for key, title, algorithm in [("gaussian", "高斯模糊", "Gaussian convolution"), ("box", "方框模糊", "Box convolution")]:
    op(f"enhance_{key}", title, "enhance", "enhance", "平滑画面细节，保留原始透明通道。", algorithm, [
        number("radius", "模糊半径", 3.0, 0, 40, 0.5, unit="px")])
op("enhance_median", "中值降噪", "enhance", "enhance", "去除孤立亮暗噪点，适合椒盐噪声。", "Median neighborhood filter", [number("size", "窗口大小", 3, 3, 9, 2, odd=True)])
op("enhance_unsharp", "精细锐化", "enhance", "enhance", "反差保留锐化，可控制半径、强度与阈值。", "Unsharp mask", [
    number("radius", "锐化半径", 2.0, 0.1, 20, 0.1, unit="px"), number("percent", "锐化强度", 150, 0, 500, unit="%"), number("threshold", "差异阈值", 3, 0, 255)])
op("enhance_sharpness", "快速清晰", "enhance", "enhance", "快速增强整体锐度。", "Pillow sharpness enhancement", [number("factor", "清晰系数", 1.5, 0, 4, 0.05)])
op("enhance_detail", "细节增强", "enhance", "enhance", "使用细节卷积核突出纹理。", "Detail convolution blended with source", [strength(0.8)])
op("enhance_bilateral", "保边降噪", "enhance", "enhance", "同时参考空间距离与颜色差异，平滑区域并保留边缘。", "Bilateral weighted neighborhood", [
    number("radius", "邻域半径", 2, 1, 4, unit="px"), number("sigma", "颜色标准差", 30.0, 1, 100, 1.0)])
op("enhance_motion", "运动模糊", "enhance", "enhance", "沿指定方向模拟运动拖影。", "Directional normalized kernel", [
    number("size", "拖影长度", 9, 3, 31, 2, odd=True), choice("direction", "运动方向", "horizontal", [("horizontal", "水平"), ("vertical", "垂直"), ("diagonal", "对角线")])])
op("enhance_clarity", "局部清晰度", "enhance", "enhance", "增强中频反差，使纹理更立体。", "Large-radius high-pass gain", [
    number("radius", "细节范围", 12.0, 1, 60, 0.5, unit="px"), number("amount", "增强幅度", 0.6, -1, 3, 0.05)])
op("enhance_soft_glow", "柔焦光晕", "enhance", "enhance", "以滤色模式叠加模糊图，营造柔和高光。", "Gaussian blur with screen blending", [
    number("radius", "光晕半径", 15.0, 1, 60, 0.5, unit="px"), strength(0.35)])
for key, title, algorithm in [("minimum", "暗部扩张", "Minimum neighborhood erosion"), ("maximum", "亮部扩张", "Maximum neighborhood dilation")]:
    op(f"enhance_{key}", title, "enhance", "enhance", "用邻域极值改变明暗区域边界。", algorithm, [number("size", "窗口大小", 3, 3, 9, 2, odd=True)])

for key, title, algorithm in [("sobel", "Sobel 边缘", "Sobel 3×3 gradient magnitude"),
                              ("prewitt", "Prewitt 边缘", "Prewitt 3×3 gradient magnitude"),
                              ("scharr", "Scharr 边缘", "Scharr 3×3 gradient magnitude")]:
    op(f"edge_{key}", title, "edge", "edges", "计算水平和垂直方向的灰度梯度。", algorithm, [
        choice("direction", "梯度方向", "both", [("both", "合成梯度"), ("horizontal", "水平方向"), ("vertical", "垂直方向")]),
        number("gain", "边缘增益", 1.0, 0.1, 5, 0.1), boolean("invert", "反转黑白", False, "开启后使用白底黑边。")])
op("edge_laplacian", "Laplacian 边缘", "edge", "edges", "用二阶微分提取快速变化的亮度区域。", "4-neighbor Laplacian", [
    number("blur", "预先平滑", 1.0, 0, 10, 0.5), number("gain", "边缘增益", 3.0, 0.1, 10, 0.1), boolean("invert", "反转黑白", False, "开启后使用白底黑边。")])
op("edge_roberts", "Roberts 边缘", "edge", "edges", "沿对角方向计算相邻像素差分。", "Roberts cross gradient", [
    number("gain", "边缘增益", 1.0, 0.1, 5, 0.1), boolean("invert", "反转黑白", False, "开启后使用白底黑边。")])
op("edge_contour", "线稿轮廓", "edge", "edges", "提取适合插画描摹的轮廓线。", "Pillow contour convolution", [
    number("blur", "预先平滑", 1.0, 0, 10, 0.5), boolean("invert", "反转黑白", False, "反转轮廓和背景颜色。")])
op("edge_binary", "二值边缘", "edge", "edges", "平滑、Sobel 梯度和阈值组合，输出纯黑白边缘。", "Gaussian → Sobel → binary threshold", [
    number("blur", "预先平滑", 1.0, 0, 10, 0.5), number("threshold", "边缘阈值", 60, 1, 254), boolean("invert", "反转黑白", False, "开启后使用白底黑边。")])
op("edge_overlay", "彩色轮廓叠加", "edge", "edges", "把检测到的边缘以自定义颜色叠加在原图上。", "Sobel-weighted color overlay", [color("color", "轮廓颜色", "#d7f67d"), strength(0.8)])

op("watermark_text", "文字水印", "watermark", "watermark", "添加支持中文、描边与透明度的署名。", "Font rasterization and alpha composition", [
    text("text", "水印文字", "光屿 · IMAGE STUDIO"), number("size", "字体大小", 32, 8, 256, unit="px"),
    choice("position", "水印位置", "bottom-right", [("top-left", "左上"), ("top-right", "右上"), ("center", "居中"), ("bottom-left", "左下"), ("bottom-right", "右下")]),
    color("color", "文字颜色", "#ffffff"), number("opacity", "不透明度", 0.75, 0, 1, 0.05),
    number("margin", "边距", 24, 0, 300, unit="px"), number("stroke", "描边宽度", 1, 0, 8, unit="px"), color("stroke_color", "描边颜色", "#182b26")], True, False)
op("watermark_repeat", "平铺水印", "watermark", "watermark", "错行重复文字水印，覆盖整张图片。", "Staggered text tiling", [
    text("text", "水印文字", "光屿工作室"), number("size", "字体大小", 24, 8, 128, unit="px"),
    number("gap", "水印间距", 80, 16, 400, unit="px"), color("color", "文字颜色", "#ffffff"), number("opacity", "不透明度", 0.25, 0, 1, 0.05)], True, False)
op("watermark_border", "画框边线", "watermark", "watermark", "添加内边框或向外扩展画框。", "Raster border or expanded canvas", [
    number("width", "边框宽度", 24, 1, 500, unit="px"), color("color", "边框颜色", "#f7f4ea"), boolean("outside", "向外扩展", True, "不遮挡原始画面，输出尺寸会变大。")], False, False)
op("watermark_round_corners", "圆角画幅", "watermark", "watermark", "为图片添加透明圆角，可设置柔和边缘。", "Rounded rectangle alpha mask", [
    number("radius", "圆角比例", 8, 0, 50, unit="%"), number("feather", "边缘羽化", 0.0, 0, 10, 0.5, unit="px")], True, False)
op("watermark_caption", "底片标题", "watermark", "watermark", "添加独立标题条，让作品成为一张明信片。", "Expanded caption band with fitted text", [
    text("text", "标题文字", "A MOMENT IN LIGHT · 光屿"), number("height", "标题条高度", 90, 24, 500, unit="px"),
    number("size", "字体大小", 28, 8, 128, unit="px"), color("background", "标题条颜色", "#f4f0e8"), color("color", "文字颜色", "#213c35"),
    choice("placement", "标题条位置", "bottom", [("bottom", "底部"), ("top", "顶部")])], False, False)
op("watermark_grid", "构图网格", "watermark", "watermark", "把规则网格绘制到图像中，用于构图教学。", "Uniform grid rasterization", [
    number("columns", "列数", 3, 2, 20), number("rows", "行数", 3, 2, 20), number("width", "线宽", 2, 1, 20, unit="px"),
    color("color", "线条颜色", "#ffffff"), number("opacity", "不透明度", 0.6, 0, 1, 0.05)], True, False)

op("format_flatten", "透明背景合成", "format", "formatting", "将透明区域合成到纯色背景，适合 JPEG 导出。", "RGBA over opaque RGB", [color("background", "背景颜色", "#ffffff")], True, False)
op("format_palette", "调色板压缩", "format", "formatting", "量化为有限颜色，适合图标和插画。", "Median-cut palette quantization", [
    number("colors", "颜色数量", 64, 2, 256), boolean("dither", "误差扩散抖动", True, "用邻近色模拟渐变。"), color("background", "透明填充色", "#ffffff")], True, False)
op("format_monochrome", "黑白位图", "format", "formatting", "转换为黑白两色，可用抖动保留层次。", "1-bit Floyd–Steinberg or threshold", [
    boolean("dither", "误差扩散抖动", True, "开启后用黑白网点保留灰度层次。")])
op("format_jpeg_preview", "JPEG 压缩预览", "format", "formatting", "通过真实编解码预览 JPEG 压缩损失，结果仍保存为编辑副本。", "JPEG encode/decode round-trip", [
    number("quality", "JPEG 质量", 80, 10, 100), number("subsampling", "色度采样级别", 2, 0, 2), color("background", "透明填充色", "#ffffff")], True, False)
op("format_opacity", "整体透明度", "format", "formatting", "缩放透明通道，原有透明区域保持透明。", "Alpha channel multiplication", [number("opacity", "不透明度", 0.6, 0, 1, 0.05)], True, False)
op("format_color_key", "纯色背景抠除", "format", "formatting", "按与指定颜色的距离生成透明遮罩，适合绿幕和纯色背景。", "RGB distance color key with feather", [
    color("color", "抠除颜色", "#00ff00"), number("tolerance", "颜色容差", 35, 0, 200), number("softness", "边缘过渡", 20, 1, 100)], True, False)


def main():
    destination = ROOT / "backend" / "catalog"
    destination.mkdir(exist_ok=True)
    modules = []
    for category in CATEGORIES:
        records = [item for item in OPERATIONS if item["category"] == category["id"]]
        if not records:
            continue
        module_name = category["id"]
        modules.append(module_name)
        source = '"""' + category["title"] + '：服务端参数协议与界面元数据。"""\n\n'
        source += "OPERATIONS = " + pprint.pformat(records, width=92, sort_dicts=False) + "\n"
        (destination / f"{module_name}.py").write_text(source, encoding="utf-8")
    index = '"""The authoritative ten-feature catalog."""\n'
    for name in modules:
        index += f"from .{name} import OPERATIONS as {name.upper()}\n"
    index += "\nCATEGORIES = " + pprint.pformat(CATEGORIES, width=92, sort_dicts=False)
    index += "\n\nOPERATIONS = " + " + ".join(name.upper() for name in modules) + "\n"
    (destination / "__init__.py").write_text(index, encoding="utf-8")
    print(f"Compiled {len(OPERATIONS)} operations across {len(CATEGORIES)} features.")


if __name__ == "__main__":
    main()
