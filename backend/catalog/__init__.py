"""The authoritative ten-feature catalog."""
from .crop import OPERATIONS as CROP
from .resize import OPERATIONS as RESIZE
from .transform import OPERATIONS as TRANSFORM
from .color import OPERATIONS as COLOR
from .filter import OPERATIONS as FILTER
from .enhance import OPERATIONS as ENHANCE
from .edge import OPERATIONS as EDGE
from .watermark import OPERATIONS as WATERMARK
from .format import OPERATIONS as FORMAT

CATEGORIES = [{'id': 'crop', 'title': '裁剪构图', 'icon': 'crop', 'subtitle': '留下画面的重点', 'number': '01'},
 {'id': 'resize', 'title': '尺寸调整', 'icon': 'resize', 'subtitle': '适配每一种画幅', 'number': '02'},
 {'id': 'transform',
  'title': '旋转翻转',
  'icon': 'rotate',
  'subtitle': '换个角度看世界',
  'number': '03'},
 {'id': 'color', 'title': '色彩调整', 'icon': 'sun', 'subtitle': '让色彩恰到好处', 'number': '04'},
 {'id': 'filter', 'title': '艺术滤镜', 'icon': 'spark', 'subtitle': '找到你的影像风格', 'number': '05'},
 {'id': 'enhance', 'title': '降噪锐化', 'icon': 'focus', 'subtitle': '照顾每一处细节', 'number': '06'},
 {'id': 'edge', 'title': '边缘检测', 'icon': 'layers', 'subtitle': '探索图像的轮廓', 'number': '07'},
 {'id': 'watermark',
  'title': '水印标注',
  'icon': 'type',
  'subtitle': '为作品留下署名',
  'number': '08'},
 {'id': 'format', 'title': '格式转换', 'icon': 'file', 'subtitle': '轻松分享与交付', 'number': '09'},
 {'id': 'batch', 'title': '批量处理', 'icon': 'grid', 'subtitle': '一次完成一组作品', 'number': '10'}]

OPERATIONS = CROP + RESIZE + TRANSFORM + COLOR + FILTER + ENHANCE + EDGE + WATERMARK + FORMAT
