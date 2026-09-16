# -*- coding: utf-8 -*-
"""
subtitle_tab.py —— 「批量添加字幕」标签页（左右布局版本）

布局：
  ┌──────────────┬───────────────────────────────┐
  │              │  文件列表 (1/3)   字幕预览 (2/3)  │
  │  左侧（设置）  │  ┌──────┬──────────────┐       │
  │              │  │ 文件 │  视频比例      │       │
  │  选择视频     │  │ 列表 │  预览效果      │       │
  │  选择字幕     │  │      │              │       │
  │  字幕样式     │  └──────┴──────────────┘       │
  │  批量添加字幕  │                               │
  └──────────────┴───────────────────────────────┘
"""

import os
import threading
import time

import paths

# ffmpeg 可执行路径（_burn_ass_to_mask 调 libass 烧录用）
# 自动查找，打包成 exe 后指向自带的 ffmpeg
FFMPEG_PATH = paths.find_ffmpeg()

# 「字幕位置」下拉里的自定义档：选它才解锁预览里的自由拖动定位。
# 排版语义等同「底部」（MarginV = 距画面底部的距离），但由拖动产生的
# MarginV / MarginL / MarginR 精确控制，所以上下左右都能放。
CUSTOM_POSITION = "自定义（可拖拽）"

# libass 把 .ass 里的 Fontsize 当「行高」用，实际字身只有 Fontsize / 1.324
# （实测 msyh：Fontsize=24 时 26 字行实烧 471px、=36 时 707px，比值都是 1.324）。
# Qt 的字体度量函数是按字身算宽度的，所以要除掉这个比值才是 libass 眼里的宽。
# 只用于「估算字幕块宽度以限制拖动范围」；能拿到实烧 mask 时优先用 mask（更准）。
LIBASS_FONT_RATIO = 1.324

from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QFileDialog, QVBoxLayout, QHBoxLayout,
    QSlider, QCheckBox, QMessageBox, QProgressBar, QGroupBox,
    QColorDialog, QListWidget, QListWidgetItem, QComboBox, QTextEdit,
    QSizePolicy, QApplication, QFormLayout, QScrollArea,
    QStackedWidget, QDialog, QPlainTextEdit,
    QGraphicsView, QGraphicsScene, QGraphicsItem, QFrame,
)
from PySide6.QtCore import (Qt, QThread, QSize, QSizeF, QUrl, QRectF, QObject,
                            Signal, QTimer)
from PySide6.QtGui import (QPainter, QColor, QFont, QFontMetrics, QPen, QBrush,
                           QRawFont, QImage, QPainterPath)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

from subtitle import (
    SubtitleWorker, list_presets, PRESET_DIR, ASPECT_RATIOS,
    available_fonts, parse_ass_for_preview, FONT_OPTIONS,
    get_preset_resolution,
)
from utils import (
    is_video, list_videos, default_subtitle_output_dir, _format_duration,
    format_elapsed_compact,
    _format_total_duration, _format_file_size,
    PauseTracker, total_size_mb, open_in_explorer,
    NoWheelMixin, disable_wheel_on, disable_wheel_recursive,
)


# 滚轮防误改：字幕 tab 的下拉框（预设字幕/位置/对齐/字体/颜色/画幅）
# 全部换成禁止滚轮改值的版本。
# ⚠️ mixin 必须写在 Qt 类**前面**（MRO 从左往右解析）。
class NoWheelCombo(NoWheelMixin, QComboBox):
    """下拉框：鼠标滚轮划过不会改值（没焦点时把滚轮让给页面滚动）。"""


class NoWheelSlider(NoWheelMixin, QSlider):
    """滑条：同上。

    ⚠️ 字幕 tab 的滑条（字幕大小/透明度/描边粗细）**必须也保护** ——
    用户的原话是「所有可以下拉调整参数的」，滑条同样是滚轮就能改值，
    而且改错了不弹确认（调完字号以为没动，跑完成片才发现字变大）。
    例外：**播放进度条 `seek_slider` 不保护** —— 滚轮在进度条上改播放位置
    是视频播放器的常规操作，禁掉反而别扭（它改的是播放位置，不是"参数"）。
    """
import config as app_config
from batch_dialog import show_batch_done
from stats import (
    StatsBridge, RatioBridge, total_stats, simplify_ratio,
    probe_dimensions, collect_ratios,
)


# 字号滑条范围（最常见的视频字幕字号）
FONTSIZE_MIN = 12
FONTSIZE_MAX = 100
FONTSIZE_DEFAULT = 28

# .ass 里 Fontname 存的是字体文件名（如 "msyh"，保存时用
# os.path.splitext(basename(font_file))[0]）→ 反查 UI 下拉框的中文显示名。
# 选预设时要把预设字体同步回下拉框，方便用户基于它小改。
#
# ⚠️ FONT_OPTIONS 的值是**候选路径列表**（跨平台），所以要把每个平台的路径名
#    都登记进来 —— 在 Mac 上打开的 .ass 写的是 "PingFang"，在 Windows 上
#    打开的写的是 "msyh"，两边都要能反查回中文显示名。
#
# 🔴 冲突消解：只登记**首个存在的候选项**（即这台机器上真正会用的那个）。
#    不能把整张候选表全塞进来 —— Mac 的几个老字体名（宋体/等线/仿宋）兜底
#    都指向 Songti.ttc，会把 "songti" 这个键抢来抢去（实测曾错成「等线」）。
#    只登记本机实际存在的路径，就不会有跨平台抢键的问题。
_ASS_FONT_TO_DISPLAY = {}
for _disp, _cands in FONT_OPTIONS:
    if isinstance(_cands, str):        # 兼容「单个路径」的老写法
        _cands = [_cands]
    # 优先登记本机真实存在的那个；一个都不存在时（比如预设来自别的平台）
    # 才退回登记第一个候选，保证反查表非空。
    _pick = next((p for p in _cands if os.path.isfile(p)), _cands[0] if _cands else None)
    if _pick:
        _ASS_FONT_TO_DISPLAY[
            os.path.splitext(os.path.basename(_pick))[0].lower()] = _disp

# 另外把「另一平台」的名字也补登记上（只补没被占用的键），
# 这样在 Mac 上打开一个 Windows 做的 .ass（Fontname="msyh"）也能认出显示名。
for _disp, _cands in FONT_OPTIONS:
    if isinstance(_cands, str):
        _cands = [_cands]
    for _fpath in _cands:
        _key = os.path.splitext(os.path.basename(_fpath))[0].lower()
        _ASS_FONT_TO_DISPLAY.setdefault(_key, _disp)


# ---------------- 字体度量（与 ffmpeg drawtext 对齐） ----------------
#
# ffmpeg 的 drawtext 用 FreeType 排版，规则是：
#   * 行距 pitch = (ascender + descender + lineGap) / unitsPerEm * fontsize
#   * y 锚点    = 第一行文字的「墨迹顶部」（不是 em 框顶部）
#   * text_h    = 整块文字的墨迹高度 = (行数-1)*pitch + 首行墨迹上伸 + 末行墨迹下伸
#
# Qt 的 QFontMetrics 在 Windows 上走 DirectWrite，行距/基线和 FreeType 不一样，
# 所以用 QRawFont 直接读字体文件拿 FreeType 级别的度量，保证预览 = 成片。
# 实测（微软雅黑）：pitch 在 28/36/40/100 号下 = 37/48/53/132，与 ffmpeg 完全一致。

# 显示名 → Qt 字体族名。
# 🔴 这是**唯一**一份（原来在 _load_fonts 和 _refresh_preview 里各抄了一份，
#    改一处漏一处）。Windows 名保持原样，Mac 补充对应族名。
_QT_FAMILY = {
    "微软雅黑": "Microsoft YaHei",
    "微软雅黑 Bold": "Microsoft YaHei",
    "宋体": "SimSun",
    "黑体": "SimHei",
    "楷体": "KaiTi",
    "仿宋": "FangSong",
    "等线": "DengXian",
    # macOS
    "苹方": "PingFang SC",
    "冬青黑体": "Hiragino Sans GB",
    "华文黑体": "Heiti SC",
}


def _qt_family(display_name):
    """显示名 → Qt 字体族名。不在表里就原样返回（别硬编码 Windows 字体当兜底）。"""
    return _QT_FAMILY.get(display_name, display_name)


# 默认字体显示名（按平台真实存在的那个；一个都没有时为 None）
_DEFAULT_FONT_NAME = paths.default_font()[0]

# Qt 字体族名 → 字体文件路径
_FONT_FILES = {}
for _name, _path in available_fonts():
    _FONT_FILES[_name] = _path                              # 中文名（.ass 里常见）
    _FONT_FILES[_qt_family(_name)] = _path                  # Qt 字体族名


def _default_font_file():
    """取一个**确实存在**的字体文件路径；找不到返回 None。

    🔴 原来各处写的是 `_FONT_FILES.get(x) or _FONT_FILES.get("Microsoft YaHei")`
    —— 在 macOS 上两个都拿不到（表里没有这个键），返回 None，
    而调用方又拿 None 去喂 QRawFont / freetype → 度量全错、预览与成片不符。
    """
    if _DEFAULT_FONT_NAME:
        p = _FONT_FILES.get(_DEFAULT_FONT_NAME)
        if p:
            return p
    for _p in _FONT_FILES.values():
        return _p
    return None

_RAWFONT_CACHE = {}
_METRIC_CACHE = {}


_OS2_CACHE = {}  # font_file -> {ascent, descent, line_gap}（OS/2 sTypo 字段，单位像素）
_FT_FACE_CACHE = {}  # font_file -> (Face, pixel_size)，freetype Face 按字号缓存


def _ass_line_metrics(font_file, pixel_size):
    """libass 行距（=OS/2 sTypoAsc + |sTypoDesc| + sTypoLineGap），
    这是 ffmpeg libass 选 .ass 预设时实际使用的行距，跟 drawtext 的 FreeType 行距不一样。

    返回 None 表示字体文件读不到（用不到时回退到 _raw_font 算的 hhea 行距）。
    """
    if not font_file:
        return None
    if font_file in _OS2_CACHE:
        v = _OS2_CACHE[font_file]
        if v is None:
            return None
        scale = pixel_size / v[3]
        return v[0] * scale + v[1] * scale + v[2] * scale
    try:
        from fontTools.ttLib import TTFont, TTCollection
    except Exception:
        _OS2_CACHE[font_file] = None
        return None
    try:
        if font_file.lower().endswith(".ttc"):
            ttf = TTCollection(font_file).fonts[0]
        else:
            ttf = TTFont(font_file, lazy=True)
        os2 = ttf["OS/2"]
        upem = ttf["head"].unitsPerEm
        asc = float(os2.sTypoAscender or 0)
        desc = float(os2.sTypoDescender or 0)
        lg = float(os2.sTypoLineGap or 0)
        try:
            ttf.close()
        except Exception:
            pass
        _OS2_CACHE[font_file] = (asc, abs(desc), lg, upem)
        scale = float(pixel_size) / upem
        return asc * scale + abs(desc) * scale + lg * scale
    except Exception:
        _OS2_CACHE[font_file] = None
        return None


def _os2_asc_desc(font_file):
    """读字体 OS/2 表的 sTypoAscender / |sTypoDescender| / unitsPerEm，返回像素参数
    必须在调用方按字号缩放。失败返回 (None, None, None)。
    """
    if not font_file:
        return (None, None, None)
    v = _OS2_CACHE.get(font_file)
    if v is None:
        # 触发 _ass_line_metrics 里的解析逻辑填充缓存
        _ass_line_metrics(font_file, 100)
        v = _OS2_CACHE.get(font_file)
    if v is None or len(v) < 4:
        return (None, None, None)
    return (v[0], v[1], v[3])


def _raw_font(font_file, pixel_size):
    """取指定字号的 QRawFont（带缓存：解析字体文件很慢，不能每次重绘都做）"""
    if not font_file:
        return None
    key = (font_file, int(pixel_size))
    if key in _RAWFONT_CACHE:
        return _RAWFONT_CACHE[key]
    try:
        rf = QRawFont(font_file, float(max(1, pixel_size)))
        if not rf.isValid():
            rf = None
    except Exception:
        rf = None
    if len(_RAWFONT_CACHE) > 64:
        _RAWFONT_CACHE.clear()
    _RAWFONT_CACHE[key] = rf
    return rf


_LIBASS_SWEEP_DONE = False


def _sweep_stale_libass_dirs():
    """删除当前目录下 30 分钟前的 libass_* 残留临时目录（后台线程跑）。"""
    import shutil
    import time as _time
    cutoff = _time.time() - 1800
    try:
        for name in os.listdir('.'):
            if name.startswith('libass_') and os.path.isdir(name):
                try:
                    if os.path.getmtime(name) < cutoff:
                        shutil.rmtree(name, ignore_errors=True)
                except OSError:
                    pass
    except OSError:
        pass


def _burn_ass_to_mask(ass_path, video_w, video_h):
    """烧字幕得到「形状 mask」（8 位灰度，值 = 该像素的字幕覆盖率 0~255）。

    ⚠️ 为什么不用「洋红背景 + colorkey」抠像（会被用户投诉变粉）：
      抠像的前提是背景色与字幕色不重叠，但半透明字幕（用户把「字体透明度」
      调低）会和洋红背景**物理混合**：白字 33% + 洋红 67% = 粉色。加滤镜只能
      减轻、不能消除（实测 colorkey 后仍有 15% 像素是洋红污染色）。
      抗锯齿边缘同样会混，所以任何「抠背景」思路都有这个问题。

    ✅ 现在的做法：只取形状，不取颜色。
      黑底上渲染纯白不透明字 → 转灰度（亮度即覆盖率）→ 得到一张 8 位灰度图
      （背景 0、字心 255、抗锯齿边缘渐变）。颜色和用户设的透明度由 Qt 在预览层
      上色，成片则交给 .ass 文件本身由 libass 正常渲染。形状与颜色彻底解耦，
      任何透明度下都不会串色。

    ⚠️ 别用 alphaextract：它抽的是「输入流的 alpha 通道」，而 color 源是不透明的
      （alpha 恒 255），libass 把字画上去并不产生 alpha → 输出全白。用 format=gray
      直接取亮度才是对的。

    返回 QImage(Format_Grayscale8)；失败返回 None（降级到 QPainter 渲染）。
    """
    import subprocess, os, shutil, tempfile
    tmp_dir = None
    tmp_ass_abs = None
    global _LIBASS_SWEEP_DONE
    if not _LIBASS_SWEEP_DONE:
        _LIBASS_SWEEP_DONE = True
        threading.Thread(target=_sweep_stale_libass_dirs, daemon=True).start()
    try:
        tmp_dir = tempfile.mkdtemp(prefix='libass_', dir='.')
        rel = os.path.basename(tmp_dir) + '/a.ass'
        tmp_ass_abs = os.path.abspath(rel)

        with open(ass_path, 'r', encoding='utf-8') as src, open(tmp_ass_abs, 'w', encoding='utf-8') as dst:
            dst.write(src.read())

        # 黑底 + 白字 → 转灰度：亮度就是字幕覆盖率（背景 0 / 字心 255 / 边缘渐变）
        cmd = [
            FFMPEG_PATH,
            '-y',
            '-f', 'lavfi',
            '-i', f'color=c=black:s={video_w}x{video_h}:d=1',
            '-vf', 'ass=' + rel + ',format=gray',
            '-frames:v', '1',
            '-f', 'rawvideo',
            '-pix_fmt', 'gray',
            '-',
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=10, cwd='.',
                             **paths.spawn_kwargs())
        if res.returncode != 0:
            return None
        buf = res.stdout
        if len(buf) != video_w * video_h:
            return None
        # 灰度原始字节 → QImage（QImage 有 8 位单通道格式，直接包住即可）
        img = QImage(buf, video_w, video_h, video_w, QImage.Format_Grayscale8)
        if img.isNull():
            return None
        return img.copy()   # copy 脱离 buf 生命周期
    except Exception:
        import traceback
        traceback.print_exc()
        return None
    finally:
        # 清理放后台线程：实测 Windows 上 rmtree 一个临时目录要 ~0.86s，
        # 同步删会把每次预览刷新白白拖慢近 1 秒。
        if tmp_dir:
            d = tmp_dir
            threading.Thread(
                target=shutil.rmtree, args=(d,),
                kwargs={"ignore_errors": True}, daemon=True,
            ).start()


class _AssBurnBridge(QObject):
    """后台烧录 .ass → mask 的线程桥：worker 线程跑完把结果发回主线程（Qt 自动排队）。

    done(seq, mask)：seq 用于丢弃过期结果（用户快速切换预设时只认最新一次）。
    """

    done = Signal(int, object)


def _burn_ass_async(bridge, seq, ass_path, w, h):
    """在后台线程执行 _burn_ass_to_mask，完成后通过 bridge.done 发回主线程。"""
    mask = None
    try:
        mask = _burn_ass_to_mask(ass_path, w, h)
    except Exception:
        mask = None
    bridge.done.emit(seq, mask)


def _glyph_tight_box(font_file, size, glyph_index):
    """单个字形的「实际像素 tight bounding box」（baseline 相对）。

    ffmpeg 的 drawtext / libass 用 FreeType 渲染后扫描字形 bitmap 算 tight ink，
    会排除 ascender/descender 中的空白像素。QRawFont 的 boundingRect 和 alphaMapForGlyph
    都给的是设计空间 ink（含空白），跟 FreeType 实际渲染像素不一致。

    实现：用 freetype-py 加载字形，扫描渲染后 bitmap 的非零像素，得到真实 ink 范围。

    返回 (ink_top, ink_bot)，均 ≥ 0，单位像素。
       ink_top = baseline 到 ink 顶部的距离（baseline 上方）
       ink_bot = baseline 到 ink 底部的距离（baseline 下方）
    """
    rf = _raw_font(font_file, size)
    if rf is None:
        return None
    # 用 freetype-py 拿真实渲染 ink
    try:
        import freetype
        face = _FT_FACE_CACHE.get(font_file)
        if face is None:
            face = freetype.Face(font_file)
            face.set_pixel_sizes(0, int(size))
            _FT_FACE_CACHE[font_file] = (face, int(size))
        else:
            face, cached_size = face
            if cached_size != int(size):
                face.set_pixel_sizes(0, int(size))
                _FT_FACE_CACHE[font_file] = (face, int(size))
        # 用 QRawFont 给的 cmap → FreeType 的 charcode 加载字形
        # QRawFont.glyphIndexesForString 返回 glyph index（不是 charcode），
        # FreeType 的 face.load_glyph 接受 glyph index。直接传 idx。
        face.load_glyph(glyph_index, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING)
        bm = face.glyph.bitmap
        rows = bm.rows
        width = bm.width
        pitch = bm.pitch
        if rows == 0 or width == 0:
            return None
        data = bytes(bm.buffer)
        # 扫描非零像素的 tight bounds
        minx, maxx, miny, maxy = width, -1, rows, -1
        found = False
        for y in range(rows):
            row_bytes = data[y * pitch: y * pitch + width]
            for x in range(width):
                if row_bytes[x] > 0:
                    if minx > x: minx = x
                    if maxx < x: maxx = x
                    if miny > y: miny = y
                    if maxy < y: maxy = y
                    found = True
        if not found:
            return None
        # bitmap 在 baseline 坐标系：
        #   bitmap_top（正数）= baseline 上方到 ink 顶部距离
        #   bitmap_top - rows（负数）= baseline 下方到 ink 底部距离的负数
        bm_top = face.glyph.bitmap_top
        ink_top = bm_top - miny                    # baseline 上方
        ink_bot = (maxy + 1) - bm_top             # baseline 下方
        return ink_top, ink_bot
    except Exception:
        return None


def _line_metrics(font_file, size, line):
    """某一行文字的度量：(advance 宽度, 墨迹上伸, 墨迹下伸)，单位像素，均相对基线。

    ink_top / ink_bot 用 QRawFont alphaMapForGlyph 算出字形真实像素 tight box，
    跟 ffmpeg drawtext / libass 用的 FreeType ink box 一致。
    """
    key = (font_file, size, line)
    m = _METRIC_CACHE.get(key)
    if m is not None:
        return m

    rf = _raw_font(font_file, size)
    if rf is None or not line:
        m = (0.0, 0.0, 0.0)
    else:
        idx = rf.glyphIndexesForString(line)
        width = 0.0
        asc = 0.0
        desc = 0.0
        # 用 QRawFont.boundingRect（设计空间 ink），跟 drawtext 的 FreeType ascender+descender
        # 同源。手写字幕和 .ass 路径共用这一份 → 与 ffmpeg drawtext 行为对齐。
        for gi in idx:
            r = rf.boundingRect(gi)
            if -r.top() > asc:
                asc = -r.top()
            if r.bottom() > desc:
                desc = r.bottom()
        for a in rf.advancesForGlyphIndexes(idx):
            width += a.x()
        m = (width, asc, desc)

    if len(_METRIC_CACHE) > 4096:
        _METRIC_CACHE.clear()
    _METRIC_CACHE[key] = m
    return m


def _line_metrics_tight(font_file, size, line):
    """libass 路径专用：返回「字形实际像素 ink」tight box，跟 FreeType libass 的
    FT_Glyph_Get_CBox(FT_GLYPH_BBOX_PIXELS) 同源 —— 排除 ascender/descender 的空白。

    与 _line_metrics 的区别：_line_metrics 用 boundingRect（含设计空间 ascender/descender
    空白），用于和 drawtext 对齐；本函数用 alphaMapForGlyph 扫描真实字形像素，
    用于和 libass 对齐。

    返回 (width, ink_top, ink_bot)，单位像素，相对 baseline。
    """
    key = (font_file, size, line, "tight")
    m = _METRIC_CACHE.get(key)
    if m is not None:
        return m

    rf = _raw_font(font_file, size)
    if rf is None or not line:
        m = (0.0, 0.0, 0.0)
    else:
        idx = rf.glyphIndexesForString(line)
        width = 0.0
        asc = 0.0
        desc = 0.0
        for gi in idx:
            box = _glyph_tight_box(font_file, size, gi)
            if box is not None:
                t, b = box
                if -t > asc:
                    asc = -t
                if b > desc:
                    desc = b
        for a in rf.advancesForGlyphIndexes(idx):
            width += a.x()
        m = (width, asc, desc)

    if len(_METRIC_CACHE) > 4096:
        _METRIC_CACHE.clear()
    _METRIC_CACHE[key] = m
    return m


def _paint_subtitle_text(painter, fx, fy, fw, fh, text, fontsize, color,
                         opacity, position, alignment, outline, font_family, ref_height,
                         auto_fit=False, margin_v=None, margin_l=None, margin_r=None,
                         line_height=None):
    """在给定的帧矩形里画字幕文本（纯字幕预览 + 视频软字幕层共用）。

    不负责 painter.begin/end，只负责绘制。字号按 ref_height（视频实际高度）缩放，
    保证预览和最终烧录（drawtext 按实际分辨率算）的字号比例一致。

    auto_fit:
      - True  （默认用于无视频的纯黑框预览）→ 如果文本超框，自动缩小字号让最长行不溢出
      - False （默认，用于视频软字幕层）   → 不自动缩字号，保留用户在 UI 上设的字号比例，
                                            位置和大小与 ffmpeg drawtext 实际渲染 1:1 对齐

    margin_v / margin_l / margin_r：选 .ass 预设时由 parse_ass_for_preview 解析出来。
    libass 渲染时用这三值做边距，预览必须尊重才能跟成片对齐。手动字幕模式下不传 →
    沿用 drawtext 的默认边距 max(40, h//20) 等，与 subtitle.py 一致。

    line_height：选 .ass 预设时由 parse_ass_for_preview 解析（=Fontsize*(1+Spacing/100)）。
    不传则用 FreeType 的 ascender+descender+leading（drawtext 默认规则）。
    """
    if not text.strip():
        return

    lines = text.split("\n") or [""]

    # 预览框相对「视频真实分辨率」的缩放比
    scale = fh / float(ref_height or 1920) if ref_height else 1.0
    if scale <= 0:
        scale = 1.0

    # ===== 先在视频真实分辨率坐标系里排版（和 ffmpeg drawtext 用同一套数字），
    #      最后整体乘 scale 映射到预览框 —— 保证预览就是成片的等比缩小。
    RW = max(1, int(round(fw / scale)))
    RH = max(1, int(round(fh / scale)))

    font_file = _FONT_FILES.get(font_family) or _default_font_file()
    fs = max(1, int(fontsize))          # ffmpeg drawtext 用的就是 UI 上这个字号

    # 边距：保留 ffmpeg drawtext 默认（max(40, h//20)），但**锚定最后一行 baseline**
    # 在 frame_h - 100（距视频底 100px）处。字号变化时最后一行 baseline 几乎不动，
    # 文字只在 descender 区小幅漂移（15px 以内）——之前 margin 联动版 60+px 漂移
    # 导致用户感觉"飘到中部"，现在修正。
    if margin_v is None:
        margin_y = max(40, max(RW, RH) // 20)
    else:
        margin_y = max(0, int(margin_v))
    if margin_l is None:
        ml = max(20, max(RW, RH) // 30)
    else:
        ml = max(0, int(margin_l))
    if margin_r is None:
        mr = max(20, max(RW, RH) // 30)
    else:
        mr = max(0, int(margin_r))
    # 居中时的水平 margin 取左右较大那个，对齐 libass 的可配置行为
    if alignment == "居中":
        margin_x = max(ml, mr)
    elif alignment == "居左":
        margin_x = ml
    else:  # 居右
        margin_x = mr

    # auto_fit（仅无视频的纯黑框预览用）：超框就自动缩字号
    if auto_fit:
        avail_w = max(20, RW - margin_x * 2)
        while fs >= 6:
            widest = max(_line_metrics(font_file, fs, ln)[0] for ln in lines)
            if widest <= avail_w:
                break
            fs -= 1

    # ===== 手动字幕：行间距 = 字盒（ascent+descender+lineGap） + 固定视觉间隙 =====
    # 用 QRawFont.ascent()+descent()+leading() 作字盒（Qt 走 DirectWrite，对中文稳定）。
    # 不用 freetype 测 msyh 中文 ink 高度（freetype 中文返回 bm.rows=0 导致重叠 bug）。
    # 行间视觉间隙 = max(8, int(fs * 0.18))（保证大字号下行间也有视觉空隙）
    # - .ass 路径用 line_height 透传（必须 100% 对齐 libass 实烧）
    # - 手动字幕路径：每行 y 顶部 = base_y + i*(box_h + line_gap)
    rf = _raw_font(font_file, fs)
    if line_height is not None:
        # .ass 路径（必须 100% 对齐 libass）
        pitch = max(1, int(line_height))
    elif rf:
        # 字盒 = ascent + descent（与 ffmpeg drawtext 实烧对齐好的值）。
        # 不含 leading（leading 是字体建议的额外行间距，drawtext 默认不开 line_spacing
        # 时也不加 leading；预览与成片用同一公式保证像素级一致）
        pitch = int(rf.ascent() + rf.descent())
    else:
        pitch = fs * 1.2

    # ink 测量源：libass 路径用 tight ink（实际像素），手动字幕路径用设计空间 ink
    if line_height is not None:
        mets = [_line_metrics_tight(font_file, fs, ln) for ln in lines]
    else:
        mets = [_line_metrics(font_file, fs, ln) for ln in lines]
    n = len(lines)

    # 手动字幕路径：行间距 = 字盒 + 固定视觉间隙
    line_gap_px = max(8, int(fs * 0.18)) if line_height is None else 0
    ink_heights = [mets[i][1] + mets[i][2] for i in range(n)]
    if line_height is not None:
        # libass 路径：保持原 pitch 排版
        text_h = (n - 1) * pitch + mets[0][1] + mets[-1][2]
    else:
        # 手动字幕：字盒 + 间隙（每行 y 间距固定 = box_h + gap）
        text_h = n * pitch + line_gap_px * max(0, n - 1)

    # 位置 y：底部对齐时锚定最后一行 baseline = RH - 100（距视频底 100px）
    # 字号变化时最后一行 baseline 几乎不动，文字只在 descender 区小幅漂移
    if position == "底部":
        last_line_baseline = RH - 100
        # 从下到上累加 box_h + line_gap 算每行 baseline
        baselines = [last_line_baseline] * n
        for i in range(n - 2, -1, -1):
            baselines[i] = baselines[i + 1] - (pitch + line_gap_px)
        baseline1 = baselines[0] if n > 0 else last_line_baseline
    elif position == "中部":
        base_y = (RH - text_h) / 2.0
        baseline1 = base_y + mets[0][1]
    else:  # 顶部
        base_y = float(margin_y)
        baseline1 = base_y + mets[0][1]

    # 手动字幕：每行 baseline 已算好（baselines[]）
    # .ass 路径：单行用 baseline1 + i*pitch
    if line_height is None:
        # 手动字幕：baselines 已在 position == "底部" 分支算好；
        # 中部/顶部也用同样的"最后一行 baseline 锚定"算法（保持视觉稳定）
        if position == "中部":
            center_baseline = RH / 2.0
            half_offset = (n - 1) * (pitch + line_gap_px) / 2.0
            first_baseline = center_baseline - half_offset
            baselines = [first_baseline + i * (pitch + line_gap_px) for i in range(n)]
        elif position == "顶部":
            first_baseline = margin_y + mets[0][1]
            baselines = [first_baseline + i * (pitch + line_gap_px) for i in range(n)]
        # else: baselines 已在底部分支算好
        baseline1 = baselines[0] if baselines else 0
    else:
        # .ass 路径：用 libass 自己的 pitch 排版
        if position == "底部":
            base_y = RH - margin_y - text_h
        elif position == "中部":
            base_y = (RH - text_h) / 2.0
        else:  # 顶部
            base_y = float(margin_y)
        baseline1 = base_y + mets[0][1]
        baselines = [baseline1 + i * pitch for i in range(n)]

    # 描边宽度：ffmpeg 是 max(1, fontsize // 12)
    bw_real = max(1, fs // 12) if outline else 0

    # ===== 映射到预览坐标系 =====
    pfs = max(1, int(round(fs * scale)))
    font = QFont(font_family)
    font.setPixelSize(pfs)
    font.setBold(False)
    painter.setFont(font)

    bw = max(1, int(round(bw_real * scale))) if outline else 0

    for i, line in enumerate(lines):
        lw = mets[i][0]
        if alignment == "居中":
            # libass 的居中语义：在 [MarginL, 画面宽 - MarginR] 这段区域里居中。
            # 左右边距相等时 = 画面正中（所有预设都是 15/15 这种对称值，所以
            # 原来的 (RW - lw)/2 结果一样）；不等时才会整体左右偏移
            # ——「自定义（可拖拽）」就是靠这个来左右挪字的。
            tx = (ml + (RW - mr) - lw) / 2.0
        elif alignment == "居左":
            tx = float(ml)
        else:  # 居右
            tx = float(RW - mr - lw)

        px = fx + tx * scale
        py = fy + baselines[i] * scale

        # 描边
        if outline:
            outline_color = QColor(0, 0, 0, int(opacity * 255))
            painter.setPen(QPen(outline_color, bw * 2 + 1, Qt.SolidLine, Qt.RoundCap))
            offsets = [(-bw, 0), (bw, 0), (0, -bw), (0, bw),
                       (-bw, -bw), (bw, -bw), (-bw, bw), (bw, bw)]
            for dx, dy in offsets:
                painter.drawText(px + dx, py + dy, line)

        # 文本本体
        text_color = QColor(color)
        text_color.setAlpha(int(opacity * 255))
        painter.setPen(text_color)
        painter.drawText(px, py, line)


class PreviewWidget(QWidget):
    """实时预览：根据设置画一个示意帧 + 字幕"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.aspect = (1080, 1920)   # 视频实际分辨率（宽, 高），用于算预览框比例
        self.ref_height = 1920       # 字号缩放基准高度：加载视频后设为实际高度
        self.text = "本视频由AI生成\n请谨慎辨别"
        self.fontsize = FONTSIZE_DEFAULT
        self.color = QColor("#FFFFFF")
        self.opacity = 1.0
        self.position = "底部"
        self.alignment = "居中"
        self.outline = True
        self.use_ass = False
        self.ass_name = ""
        self.font_family = _qt_family(_DEFAULT_FONT_NAME) if _DEFAULT_FONT_NAME else ""   # 给 QPainter 用
        # .ass 预设专用：libass 渲染时的 MarginV/L/R（手动字幕用 drawtext 默认值，不传）
        self.margin_v = None
        self.margin_l = None
        self.margin_r = None
        # 不设最小尺寸，让它跟着 layout 自由缩放
        self.setMinimumSize(0, 0)
        self.setStyleSheet("background-color: #1a1a1a; border: 1px solid #444;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def update_all(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.update()

    def _compute_frame(self, margin=12):
        """按视频比例在当前容器里算出「尽量大、居中」的帧矩形 (fx, fy, fw, fh)。

        现在主要用于无视频时的纯字幕预览（margin>0，黑色框四周留边）。
        有视频时预览由 QGraphicsVideoItem + SubtitleItem 软字幕叠加负责。
        """
        aw, ah = self.aspect
        if not aw or not ah:
            aw, ah = 1080, 1920
        avail_w = max(50, self.width() - margin * 2)
        avail_h = max(50, self.height() - margin * 2)

        if aw >= ah:  # 横向（如 16:9）：宽先到顶
            fw = avail_w
            fh = int(fw * ah / aw)
            if fh > avail_h:
                fh = avail_h
                fw = int(fh * aw / ah)
        else:  # 竖向（如 9:16）：高先到顶
            fh = avail_h
            fw = int(fh * aw / ah)
            if fw > avail_w:
                fw = avail_w
                fh = int(fw * ah / aw)

        fx = (self.width() - fw) // 2
        fy = (self.height() - fh) // 2
        return fx, fy, fw, fh

    def draw_subtitle(self, painter, fx, fy, fw, fh):
        """在给定的帧矩形里画字幕（纯字幕预览 + 视频软字幕层共用）。

        不负责 painter.begin/end，只负责绘制。
        """
        # 如果用预设字幕，画占位提示
        if self.use_ass:
            painter.setPen(QColor(180, 180, 180))
            painter.setFont(QFont(_qt_family(_DEFAULT_FONT_NAME) if _DEFAULT_FONT_NAME else "", 12))
            msg = "使用预设字幕：\n{}".format(self.ass_name) if self.ass_name else "使用预设字幕"
            painter.drawText(fx, fy + fh // 2 - 10, fw, 60, Qt.AlignHCenter | Qt.TextWordWrap, msg)
            return

        _paint_subtitle_text(
            painter, fx, fy, fw, fh, self.text, self.fontsize, self.color,
            self.opacity, self.position, self.alignment, self.outline,
            self.font_family, self.ref_height,
            auto_fit=True,   # 纯黑框预览：超框自动缩字号，避免溢出
            margin_v=getattr(self, "margin_v", None),
            margin_l=getattr(self, "margin_l", None),
            margin_r=getattr(self, "margin_r", None),
            line_height=getattr(self, "line_height", None),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        fx, fy, fw, fh = self._compute_frame(12)

        # 帧背景
        painter.fillRect(fx, fy, fw, fh, QColor(35, 35, 35))
        painter.setPen(QColor(100, 100, 100))
        painter.drawRect(fx, fy, fw, fh)

        # 比例标签
        painter.setPen(QColor(150, 150, 150))
        painter.setFont(QFont("Arial", 9))
        ratio_text = simplify_ratio(self.aspect[0], self.aspect[1]) or "{}:{}".format(self.aspect[0], self.aspect[1])
        painter.drawText(fx + 6, fy + 14, ratio_text)

        self.draw_subtitle(painter, fx, fy, fw, fh)
        painter.end()


class SubtitleItem(QGraphicsItem):
    """软字幕图层：作为 QGraphicsVideoItem 的子项叠加在视频上。

    为什么用 QGraphicsItem 而不是 QWidget 覆盖层：
    QVideoWidget 在 Windows 上会用独立的原生窗口渲染视频，普通 QWidget
    叠在它上面会被视频盖住（所以之前字幕预览"消失"了）。QGraphicsVideoItem
    把视频渲染进 QGraphicsScene，字幕 item 作为它的子项，能稳定地画在视频上层。

    坐标空间 = 视频原生分辨率（如 1080x1920），字号直接用像素值，和
    ffmpeg drawtext 的实际渲染比例一致。

    .ass 路径：直接显示 ffmpeg 烧录的 mask 图（libass 实际渲染），不走 QPainter 渲染。
    这样能 100% 对齐 libass 的位置和字宽（实测 QPainter 渲染字宽比 libass 大 25%）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.fontsize = FONTSIZE_DEFAULT
        self.color = QColor("#FFFFFF")
        self.opacity = 1.0
        self.position = "底部"
        self.alignment = "居中"
        self.outline = True
        self.font_family = (_qt_family(_DEFAULT_FONT_NAME)
                            if _DEFAULT_FONT_NAME else "")
        self.use_ass = False
        self.ass_name = ""
        # .ass 预设专用：libass 渲染时的 MarginV/L/R（手动字幕用 drawtext 默认值，不传）
        self.margin_v = None
        self.margin_l = None
        self.margin_r = None
        # .ass 预设专用：libass 行距 = Fontsize * (1 + Spacing/100)
        self.line_height = None
        # .ass 预设专用：libass 行距 = Fontsize * (1 + Spacing/100)
        self.line_height = None
        self._video_w = 1080
        self._video_h = 1920
        # .ass 烧录的「形状 mask」图（8 位灰度，只含形状不含颜色）
        # ⚠️ 不存 libass 渲染结果的颜色：半透明字与背景混合会串色（变粉），
        #    颜色必须由 Qt 侧用 _mask_color 现场着色。
        self.burned_mask = None
        self._mask_rect = None
        self._mask_source = None  # (ass_path, video_w, video_h) 缓存来源，避免重复烧录
        self._mask_color = QColor("#FFFFFF")   # 字幕颜色（预览着色用）
        self._mask_opacity = 1.0               # 字幕透明度（预览着色用）
        self._mask_outline = True              # 是否画黑描边（预览着色用）
        self._painted_mask = None              # 着色后的成品图（缓存，避免每帧重算）

        # ---- 拖动定位字幕（二维）----
        # 只在「位置 = 自定义（可拖拽）」时开放（由 SubtitleTab 设置 _drag_enabled）。
        # 拖动量按视频原生像素坐标算，松手后换算成 MarginV / MarginL / MarginR
        # 回写设置 —— 那三个才是 libass 真正用来摆放文字的参数，
        # 所以预览和成片落在同一个位置。
        self._drag_enabled = False             # 由 SubtitleTab 在合适时机打开
        self._dragging = False
        self._drag_start_scene_x = 0.0
        self._drag_start_scene_y = 0.0
        self._drag_offset_x = 0
        self._drag_offset_y = 0
        self.drag_selected = False             # 是否高亮（显示可拖动的选中框）
        self.on_drag_commit = None             # 回调：拖动结束时把 (dx, dy) 交给主界面

    def can_drag(self):
        """只有在「位置 = 自定义（可拖拽）」且确实画着内容时才允许拖。"""
        return bool(self._drag_enabled and self._painted_mask is not None)

    def _hit_mask(self, scene_pos):
        """判断场景坐标是否落在字幕图上（用于决定能否起拖）。"""
        if self._painted_mask is None or self._mask_rect is None:
            return False
        x, y, w, h = self._mask_rect
        # 加 8px 容差，方便点中
        return (x - 8 <= scene_pos.x() <= x + w + 8 and
                y - 8 <= scene_pos.y() <= y + h + 8)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.can_drag() \
                and self._hit_mask(event.scenePos()):
            self._dragging = True
            self.drag_selected = True
            self._drag_start_scene_x = event.scenePos().x()
            self._drag_start_scene_y = event.scenePos().y()
            self._drag_offset_x = 0
            self._drag_offset_y = 0
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            # 拖动量按视频原生坐标算（scene 坐标就是视频像素坐标）
            dx = int(round(event.scenePos().x() - self._drag_start_scene_x))
            dy = int(round(event.scenePos().y() - self._drag_start_scene_y))
            if dx != self._drag_offset_x or dy != self._drag_offset_y:
                self._drag_offset_x = dx
                self._drag_offset_y = dy
                self.prepareGeometryChange()
                self.update()          # 拖动过程中只重画（画的时候整体平移）
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor if self._drag_enabled else Qt.ArrowCursor)
            dx = self._drag_offset_x
            dy = self._drag_offset_y
            self._drag_offset_x = 0
            self._drag_offset_y = 0
            self.update()
            if (dx or dy) and callable(self.on_drag_commit):
                self.on_drag_commit(dx, dy)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_video_size(self, w, h):
        """设置视频原生分辨率（宽, 高），更新字幕坐标空间。"""
        w, h = int(w), int(h)
        if (w, h) == (self._video_w, self._video_h):
            return
        self.prepareGeometryChange()
        self._video_w, self._video_h = w, h
        self.update()

    def set_style(self, **kwargs):
        """批量更新字幕样式（与 _refresh_preview 的 kwargs 对齐）。"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, self._video_w, self._video_h)

    def set_burned_mask(self, mask, color=None, opacity=1.0, outline=None):
        """设置 .ass 烧录的「形状 mask」（8 位灰度，值=字幕覆盖率）。None 表示清除。

        mask 只含形状不含颜色，画的时候用 color + opacity 现场着色，
        所以用户把「字体透明度」调低时字会正常变淡，而不是变成粉色。

        outline：是否画黑描边（形状版 mask 里 128 那档）。None = 沿用上次的值。
        """
        # 颜色/透明度每次都要更新（透明度和颜色改了但形状没变时，mask 缓存命中
        # 走的是同一个 QImage，必须重新上色）
        if color is not None:
            self._mask_color = QColor(color)
        self._mask_opacity = max(0.0, min(1.0, float(opacity)))
        if outline is not None:
            self._mask_outline = bool(outline)
        if mask is None:
            self.burned_mask = None
            self._mask_rect = None
            self._mask_source = None
            self._painted_mask = None
            self.update()
            return
        if mask.width() != self._video_w or mask.height() != self._video_h:
            # 视频尺寸变了，重新烧录后再调用此方法
            self.burned_mask = None
            self._mask_rect = None
            self._mask_source = None
            self._painted_mask = None
            self.update()
            return
        if mask.format() != QImage.Format_Grayscale8:
            mask = mask.convertToFormat(QImage.Format_Grayscale8)
        # 预先算出 mask 中真正有字幕像素的 bounding rect，画的时候只贴这块，
        # 避免每帧把整张 1080x1920 大图合成到视频上（会拖慢播放/黑屏）。
        # ⚠️ 用原始字节扫描（C 级速度），别改回逐像素 pixel()——52 万次调用要 0.4 秒。
        w_, h_ = mask.width(), mask.height()
        stride = mask.bytesPerLine()                # ⚠️ 每行按 4 字节对齐，可能 > w_
        try:
            bits = bytes(mask.constBits())
        except Exception:
            bits = b""
        if stride != w_:                            # 去掉行尾填充，拼成紧凑 w*h
            bits = b"".join(bits[r * stride:r * stride + w_] for r in range(h_))
        if len(bits) != w_ * h_:
            self.burned_mask = None
            self._mask_rect = None
            self._mask_source = None
            self._painted_mask = None
            self.update()
            return
        flags = bits.translate(b"\x00" + b"\x01" * 255)   # 非零 → 1
        first = flags.find(1)
        if first < 0:
            self.burned_mask = None
            self._mask_rect = None
            self._mask_source = None
            self._painted_mask = None
            self.update()
            return
        y0 = first // w_
        y1 = flags.rfind(1) // w_
        x0, x1 = w_, -1
        for yy in range(y0, y1 + 1):                # 只有字幕那几十行，很快
            row = flags[yy * w_:(yy + 1) * w_]
            a = row.find(1)
            if a >= 0:
                if a < x0: x0 = a
                b_ = row.rfind(1)
                if b_ > x1: x1 = b_
        # 扩展 2px 边缘防截断（必须夹回图像内，否则裁剪尺寸对不上 → 着色越界）
        x0 = max(0, x0 - 2); y0 = max(0, y0 - 2)
        x1 = min(w_ - 1, x1 + 2); y1 = min(h_ - 1, y1 + 2)
        # 只裁出字幕那一小块再着色（整幅 1080x1920 着色每帧都做受不了）
        self._mask_rect = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
        self.burned_mask = mask
        self._painted_mask = self._colorize_mask(mask, self._mask_rect)
        if self._painted_mask is None:
            # 着色失败（尺寸异常）：退回不画，避免预览崩掉
            self._mask_rect = None
        self.update()

    def _colorize_mask(self, mask, rect):
        """把灰度形状 mask 着色成实际字幕图。

        形状版 mask 是三段亮度（见 _build_inherited_ass 的 shape_only 分支）：
          0   = 背景
          128 = 描边（\bord）
          255 = 字心
        Qt 侧分别上色：字心用字色、描边用黑色，透明度统一乘进去。
        - 描边只在勾选了「黑色描边」时才画（_mask_outline），跟成片一致。
        - 全程走字节操作（不逐像素 setPixel，200 万像素逐点要 1.6 秒）。
        """
        x, y, w, h = rect
        sub = mask.copy(x, y, w, h)
        w, h = sub.width(), sub.height()            # copy 后以实际尺寸为准（防越界裁剪）
        # ⚠️ QImage 每行按 4 字节对齐（stride），灰度图宽度不是 4 的倍数时
        #    constBits() 会带行尾填充字节，长度 > w*h。必须显式按 stride 取。
        stride = sub.bytesPerLine()
        alpha = bytes(sub.constBits())
        if stride != w:
            rows = [alpha[r * stride:r * stride + w] for r in range(h)]
            alpha = b"".join(rows)
        if len(alpha) != w * h:
            return None
        op = self._mask_opacity
        c = self._mask_color
        core_px = bytes((c.blue(), c.green(), c.red(), 0))   # 字心（BGRA 内存序）
        out_px = bytes((0, 0, 0, 0))                         # 描边固定黑色
        draw_outline = bool(getattr(self, "_mask_outline", True))

        # ⚠️ 顺序至关重要：**必须先分档、再乘透明度**。
        # 反过来（先乘透明度再分档）会把字心的 255 压进描边档：
        #   42% → 255×0.42 ≈ 107，落进 64~191 的描边区间 → 字心被涂成黑色；
        #   75% → 255×0.75 = 191，刚好跌破 192 阈值 → 整个字幕判空消失。
        # 这两条正是"75% 直接消失 / 42% 变黑块"的根因。
        core = alpha.translate(bytes(255 if v >= 192 else 0 for v in range(256)))
        if draw_outline:
            edge0 = alpha.translate(bytes(255 if 64 <= v < 192 else 0 for v in range(256)))
        else:
            edge0 = None

        # 透明度在分档完成后统一乘到 alpha 上（不影响档位判定）
        if op < 1.0:
            tbl = bytes(min(255, int(round(v * op))) for v in range(256))
            core = core.translate(tbl)
            if edge0 is not None:
                edge0 = edge0.translate(tbl)
        edge = edge0

        arr = bytearray(core_px * (w * h))
        arr[3::4] = core                                     # 字心：字色 + 覆盖率 alpha
        if edge is not None:
            # 描边叠加：只填「描边有、字心没有」的像素（字心优先，避免描边吃字）
            ea = bytes(edge)
            r_, g_, b_, _ = out_px
            for i in range(w * h):
                if ea[i] and not core[i]:
                    j = i * 4
                    arr[j] = b_; arr[j + 1] = g_; arr[j + 2] = r_
                    arr[j + 3] = ea[i]
        out = QImage(bytes(arr), w, h, w * 4, QImage.Format_ARGB32)
        return out.copy()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        # .ass 路径：画 libass 烧录的形状 mask（位置/字宽 100% 对齐 libass），
        # 颜色/透明度由 _painted_mask 现场着色，不会因透明度变粉。
        # 必须用 SourceOver（不是 Source）：mask 背景 alpha=0，SourceOver 会跳过透明像素，
        # 视频画面正常显示；Source 会把整屏替换成透明黑 → 画面全黑。
        # 只贴有字幕的 bounding rect，避免每帧全屏合成拖慢视频。
        if self._painted_mask is not None and self._mask_rect is not None:
            x, y, w, h = self._mask_rect
            # 拖动中整体平移（不重新烧 mask，纯位移，零额外开销）
            x += self._drag_offset_x
            y += self._drag_offset_y
            painter.drawImage(x, y, self._painted_mask, 0, 0, w, h)
            # 拖动状态下画一个虚框，提示"这块可以拖"
            if self._dragging or (self.can_drag() and self.drag_selected):
                pen = QPen(QColor(0, 160, 255), max(1.0, self._video_w / 540.0),
                           Qt.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(x - 4, y - 4, w + 8, h + 8))
            return

        if self.use_ass:
            painter.setPen(QColor(180, 180, 180))
            painter.setFont(QFont(_qt_family(_DEFAULT_FONT_NAME) if _DEFAULT_FONT_NAME else "", 12))
            msg = "使用预设字幕：\n{}".format(self.ass_name) if self.ass_name else "使用预设字幕"
            painter.drawText(self.boundingRect(), Qt.AlignCenter | Qt.TextWordWrap, msg)
            return

        _paint_subtitle_text(
            painter, 0, 0, self._video_w, self._video_h,
            self.text, self.fontsize, self.color, self.opacity,
            self.position, self.alignment, self.outline,
            self.font_family, self._video_h,
            auto_fit=False,  # 视频软字幕层：1:1 对齐 ffmpeg drawtext，不自动缩字号
            margin_v=self.margin_v,
            margin_l=self.margin_l,
            margin_r=self.margin_r,
            line_height=getattr(self, "line_height", None),
        )


def _fmt_ms(ms):
    """把毫秒格式化成 MM:SS"""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = 0
    if ms < 0:
        ms = 0
    total_s = ms // 1000
    return "{:02d}:{:02d}".format(total_s // 60, total_s % 60)


class VideoView(QGraphicsView):
    """承载视频 + 软字幕的图形视图：窗口大小变化时自动 fit 视频比例。"""

    def __init__(self, scene, video_item):
        super().__init__(scene)
        self._video_item = video_item
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QBrush(QColor("#1a1a1a")))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        if self._video_item is None:
            return
        rect = self._video_item.boundingRect()
        if rect.width() > 1 and rect.height() > 1:
            self.fitInView(rect, Qt.KeepAspectRatio)


class VideoPreviewWidget(QWidget):
    """视频播放器预览：QtMultimedia 播放 + 软字幕叠加 + 拖拽进度条。

    结构：
      inner_stack(QStackedWidget)
        ├── 0: 播放器页（QGraphicsVideoItem + SubtitleItem 软字幕 + 控制条）
        └── 1: 提示页（批量合成中，预览已暂停）

    软字幕实现：用 QGraphicsVideoItem 把视频渲染进 QGraphicsScene，
    SubtitleItem 作为它的子项画在视频上层（等同"暂时叠加软字幕看效果"）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # 播放器
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)

        # 图形场景：视频 item + 软字幕 item
        self.video_item = QGraphicsVideoItem()
        self.video_item.setAspectRatioMode(Qt.KeepAspectRatio)
        # 给视频 item 一个初始尺寸，避免 nativeSize 到位前 boundingRect 为 0
        self.video_item.setSize(QSizeF(1080, 1920))
        self.player.setVideoOutput(self.video_item)

        self.subtitle_item = SubtitleItem(self.video_item)   # 作为视频的子项
        self.subtitle_item.setPos(0, 0)
        self.subtitle_item.set_video_size(1080, 1920)

        self.scene = QGraphicsScene()
        self.scene.addItem(self.video_item)
        self.view = VideoView(self.scene, self.video_item)

        # 控制条
        self.play_btn = QPushButton("播放")
        self.play_btn.setFixedWidth(64)
        self.play_btn.clicked.connect(self._toggle_play)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)

        self.time_label = QLabel("00:00 / 00:00")

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 4, 0, 0)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.seek_slider, 1)
        controls.addWidget(self.time_label)

        player_page = QWidget()
        pp = QVBoxLayout(player_page)
        pp.setContentsMargins(0, 0, 0, 0)
        pp.setSpacing(4)
        pp.addWidget(self.view, 1)
        pp.addLayout(controls)

        # 提示页
        self.busy_page = QLabel("批量合成中，预览已暂停\n完成后自动恢复")
        self.busy_page.setAlignment(Qt.AlignCenter)
        self.busy_page.setStyleSheet("color: #888; font-size: 14px;")

        self.inner_stack = QStackedWidget()
        self.inner_stack.addWidget(player_page)   # index 0
        self.inner_stack.addWidget(self.busy_page)  # index 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.inner_stack)

        # 状态
        self.current_path = None
        self.aspect = (1080, 1920)
        self._seeking = False
        self._saved_position = 0
        self._load_error = False
        # 每次加载/恢复后，只做一次「定位到首帧」；用一次性标志防止
        # setPosition 再次触发 mediaStatusChanged(LoadedMedia) 造成无限递归
        self._need_first_frame = False

        # 信号
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.video_item.nativeSizeChanged.connect(self._on_native_size)

    # ---- 对外接口 ----

    def load_video(self, path, dim=None):
        """加载视频到播放器（停在首帧当封面，**不自动播放**）。

        dim=(w,h) 是视频实际分辨率，用于软字幕坐标。
        加载完成后由 _on_media_status 把画面定位到第 0 帧，让用户看到首帧图，
        知道这个功能可用；想播放时手动点「播放」按钮。
        """
        self.current_path = path
        if dim and dim[0] and dim[1]:
            self.aspect = (int(dim[0]), int(dim[1]))
            w, h = int(dim[0]), int(dim[1])
            self.video_item.setSize(QSizeF(w, h))
            self.subtitle_item.set_video_size(w, h)
        self._load_error = False
        self._saved_position = 0
        self.time_label.setText("加载中…")
        self._need_first_frame = True      # 加载完成后定位到首帧（只做一次）
        self.player.setSource(QUrl.fromLocalFile(path))
        # 这里不调用 play()：加载后停在首帧即可
        self.play_btn.setText("播放")

    def set_subtitle(self, **kwargs):
        """把字幕样式同步到软字幕层"""
        self.subtitle_item.set_style(**kwargs)

    def set_aspect(self, w, h):
        """手动设置预览比例（用户在下拉栏手动选比例时用）"""
        self.aspect = (int(w), int(h))
        self.subtitle_item.set_video_size(int(w), int(h))

    def release_for_batch(self):
        """批量合成前释放播放器资源（pause + 清空媒体），切到提示页"""
        self._saved_position = self.player.position()
        self.player.pause()
        self.player.setSource(QUrl())   # 释放视频缓冲，回收内存
        self.inner_stack.setCurrentIndex(1)

    def resume_after_batch(self):
        """批量合成后回到预览页，只停在首帧当封面（**不自动播放**）"""
        self.inner_stack.setCurrentIndex(0)
        self._saved_position = 0
        if self.current_path and os.path.isfile(self.current_path):
            self._need_first_frame = True
            self.player.setSource(QUrl.fromLocalFile(self.current_path))
        self.play_btn.setText("播放")

    # ---- 播放器信号 ----

    def _on_native_size(self, size):
        """视频原生尺寸确定后，把视频 item 和软字幕坐标空间都锁到原生分辨率。"""
        if size.isValid() and size.width() > 1 and size.height() > 1:
            # 优先用外部传入的 dim；没传时用播放器自己报告的 native size
            self.aspect = (int(size.width()), int(size.height()))
            self.video_item.setSize(QSizeF(size.width(), size.height()))
            self.subtitle_item.set_video_size(int(size.width()), int(size.height()))
            self.view._fit()

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_moved(self, ms):
        self._saved_position = ms
        self.player.setPosition(ms)

    def _on_seek_released(self):
        self.player.setPosition(self._saved_position)
        self._seeking = False

    def _on_position(self, ms):
        if not self._seeking:
            self.seek_slider.setValue(ms)
        self.time_label.setText("{}/{}".format(_fmt_ms(ms), _fmt_ms(self.player.duration())))

    def _on_duration(self, ms):
        self.seek_slider.setRange(0, ms)

    def _on_state(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_btn.setText("暂停")
        else:
            self.play_btn.setText("播放")

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._load_error = False
            # 加载完成 → 停在首帧当封面（只做一次）。
            # 必须先清标志再 setPosition：否则 setPosition 会再次触发
            # mediaStatusChanged(LoadedMedia)，又进本函数 → 无限递归
            # （RecursionError: maximum recursion depth exceeded）。
            if self._need_first_frame:
                self._need_first_frame = False
                self.player.pause()
                self.player.setPosition(0)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._load_error = True

    def _on_error(self, error, error_string):
        self._load_error = True
        self.time_label.setText("无法播放：{}".format(error_string or "格式不支持"))


class SubtitleTab(QWidget):
    """「批量添加字幕」tab（左右布局）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.videos = []                 # 选中的视频路径列表
        self.output_dir = default_subtitle_output_dir()  # 默认保存到桌面「合成视频-字幕」
        # 计时器：算「扣掉暂停时间」的实际耗时
        self._pause_tracker = PauseTracker()
        # 由主窗口注入：写进「编码参考」/ 告知批处理完成
        self.record_batch = None
        self.notify_batch = None
        self._last_batch = None
        self._last_burn_settings = None   # 本批实际用的设置（记录预设名要用）

        # 默认设置
        self._color = QColor("#FFFFFF")
        # 总大小/总时长统计（后台线程探测）
        self._stats_gen = 0
        self._stats_bridge = StatsBridge()
        self._stats_bridge.done.connect(self._on_stats_done)
        # 比例扫描（后台线程探测各视频宽高比）
        self._ratio_gen = 0
        self._ratio_bridge = RatioBridge()
        self._ratio_bridge.done.connect(self._on_ratios_done)
        self._current_dim = None   # 当前加载视频的实际分辨率 (w, h)

        # .ass 后台烧录（避免切预设时 ffmpeg 阻塞 UI 数秒）
        self._burn_seq = 0                     # 递增序号，只认最新一次烧录结果
        self._pending_ass_key = None           # 本次烧录对应的 (ass_path, w, h)
        self._ass_mask_cache = {}              # (ass_path, w, h) -> QImage mask
        self._ass_burn_bridge = _AssBurnBridge()
        self._ass_burn_bridge.done.connect(self._on_ass_burn_done)
        self._ass_burn_thread = None           # 引用防 GC

        # 继承自预设的布局参数（取消预设后预览/成片继续按预设模型渲染），
        # 选预设时在 _sync_preset_into_controls 里更新；None = 无继承（纯手动）
        self._inherited_layout = None
        self._inherited_ass_source = None      # 继承来源 .ass 的路径
        # 取消预设后，用户改文本/字号/颜色时防抖重烧预览（避免每敲一个字烧一次）
        self._pending_burn = None              # (ass_path, w, h) 等待防抖烧录
        self._burn_timer = QTimer(self)
        self._burn_timer.setSingleShot(True)
        self._burn_timer.setInterval(350)
        self._burn_timer.timeout.connect(self._flush_pending_burn)

        self._build_ui()
        self._load_presets()
        self._load_fonts()
        self._update_ratio_label()
        self._refresh_preview()
        # 启动时不再自动读取/播放桌面「合成视频」里的文件（一打开就自动加载+播放很打扰）。
        # 需要时手动点左上「自动加载桌面文件夹」按钮即可加载。
        self._update_file_list("（还没选视频，点上方按钮选择或自动加载）")
        self.refresh_encode_hint()       # 初始化编码提示显示

    # ---------------- UI 构建 ----------------

    def _build_ui(self):
        # ====== 左侧（设置，可滚动） ======
        # 用 QScrollArea 包住 4 个 GroupBox：内容超出可视区域时，
        # 右侧会自动出现滚动条，往下拖就能看到（类似浏览器的右边栏）。

        # --- 选择视频 ---
        self.files_btn = QPushButton("选择文件")
        self.files_btn.clicked.connect(self.choose_files)
        self.folder_btn = QPushButton("选择文件夹")
        self.folder_btn.clicked.connect(self.choose_folder)
        self.refresh_btn = QPushButton("自动加载桌面文件夹")
        self.refresh_btn.clicked.connect(self._auto_load_default)

        self.video_label = QLabel("（还没选视频）")
        self.video_label.setWordWrap(True)

        video_box = QGroupBox("选择视频")
        vb = QVBoxLayout()
        vb.addWidget(self.files_btn)
        vb.addWidget(self.folder_btn)
        vb.addWidget(self.refresh_btn)
        vb.addWidget(self.video_label)
        video_box.setLayout(vb)

        # --- 选择字幕（预设） ---
        self.preset_combo = NoWheelCombo()
        # 「取消预设」：唯一作用就是解锁手动输入字幕（换预设本身就会自动刷新）
        self.cancel_preset_btn = QPushButton("取消预设")
        self.cancel_preset_btn.setToolTip("取消当前选中的预设字幕，恢复手动输入字幕")
        self.cancel_preset_btn.clicked.connect(self._cancel_preset)
        self.open_preset_dir_btn = QPushButton("打开预设文件夹")
        self.open_preset_dir_btn.clicked.connect(self._open_preset_dir)

        source_box = QGroupBox("选择字幕")
        sb = QVBoxLayout()
        sb.addWidget(QLabel("预设字幕："))
        sb.addWidget(self.preset_combo)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.cancel_preset_btn)
        btn_row.addWidget(self.open_preset_dir_btn)
        sb.addLayout(btn_row)
        source_box.setLayout(sb)

        # --- 字幕样式 ---
        # 手动输入
        self.text_edit = QTextEdit()
        # 默认字幕文本（未选预设时的初始内容）
        self._default_manual_text = "本视频由AI生成\n请谨慎辨别"
        self.text_edit.setPlainText(self._default_manual_text)
        self.text_edit.setMaximumHeight(70)
        self.text_edit.textChanged.connect(self._on_text_changed)

        # 字号
        self.fontsize_label = QLabel("调整字幕大小 {}".format(FONTSIZE_DEFAULT))
        self.fontsize_slider = NoWheelSlider(Qt.Horizontal)
        self.fontsize_slider.setRange(FONTSIZE_MIN, FONTSIZE_MAX)
        self.fontsize_slider.setValue(FONTSIZE_DEFAULT)
        self.fontsize_slider.valueChanged.connect(self._on_fontsize_changed)

        # 位置
        self.position_combo = NoWheelCombo()
        # 「自定义（可拖拽）」= 解锁预览里的自由拖动定位（上下左右都能拖）。
        # 其它三档是预设/细排位置，拖动会写出「改了又弹回」的错觉，所以锁着。
        self.position_combo.addItems(["底部", "中部", "顶部", CUSTOM_POSITION])
        self.position_combo.setToolTip(
            "「自定义（可拖拽）」：选它之后可以在右边预览里直接拖动字幕，"
            "上下左右任意位置（需要先点「取消预设」解锁样式控件）。")
        self.position_combo.currentTextChanged.connect(self._refresh_preview)

        # 字体格式（左右中）
        self.alignment_combo = NoWheelCombo()
        self.alignment_combo.addItems(["居中", "居左", "居右"])
        self.alignment_combo.currentTextChanged.connect(self._refresh_preview)

        # 字体
        self.font_combo = NoWheelCombo()
        self.font_combo.currentTextChanged.connect(self._on_font_changed)

        # 颜色：下拉栏（常见色 + 自定义）
        self.color_combo = NoWheelCombo()
        # 常见颜色 + 自定义
        # 数据存为 (显示文字, QColor)：custom 时弹出颜色对话框
        self._color_choices = [
            ("白色",   QColor("#FFFFFF")),
            ("黑色",   QColor("#000000")),
            ("红色",   QColor("#FF0000")),
            ("黄色",   QColor("#FFFF00")),
            ("蓝色",   QColor("#0066FF")),
            ("绿色",   QColor("#00CC00")),
        ]
        for name, _ in self._color_choices:
            self.color_combo.addItem(name)
        self.color_combo.addItem("自定义...")
        self.color_combo.currentIndexChanged.connect(self._on_color_combo_changed)
        # 颜色预览方块（方便看出当前选了什么自定义色）
        self.color_swatch = QLabel()
        self.color_swatch.setFixedSize(28, 22)
        self.color_swatch.setStyleSheet("border: 1px solid #888; background-color: #FFFFFF;")
        # 横排：下拉 + 色块
        color_row = QHBoxLayout()
        color_row.addWidget(self.color_combo, 1)
        color_row.addWidget(self.color_swatch)
        self._color = QColor("#FFFFFF")

        # 不透明度
        self.opacity_label = QLabel("字体透明度 100%")
        self.opacity_slider = NoWheelSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)

        # 描边
        self.outline_check = QCheckBox("黑色描边")
        self.outline_check.setChecked(True)
        self.outline_check.stateChanged.connect(self._on_outline_toggled)

        # 描边粗细（Outline 字段，单位=像素，可小数）。
        # 选预设时带出预设里的真实值（如 4.0 / 2 / 1.3），手动调整后写回临时 .ass。
        # 做成 0.5~6.0 的滑块，步进 0.1 → 内部用 5~60 的整数滑块表示。
        self.outline_width_label = QLabel("描边粗细 4.0")
        self.outline_width_slider = NoWheelSlider(Qt.Horizontal)
        self.outline_width_slider.setRange(5, 60)      # 5~60 → 0.5~6.0
        self.outline_width_slider.setValue(40)         # 默认 4.0
        self.outline_width_slider.valueChanged.connect(self._on_outline_width_changed)
        self._outline_width = 4.0                      # 当前描边粗细（浮点）

        # 把当前手动输入的字幕（含所有样式）保存为 .ass 字幕文件，
        # 下次可以直接在"预设字幕"下拉框选它复用
        self.save_preset_btn = QPushButton("保存为字幕文件")
        self.save_preset_btn.setToolTip("把当前手动输入的字幕（含字体大小/颜色/位置/透明度/描边）保存为 .ass 文件，下次可直接调用")
        self.save_preset_btn.clicked.connect(self._save_manual_as_ass)

        # 选了预设字幕后，手动输入被锁住 —— 这里提示怎么解锁
        self.preset_lock_hint = QLabel("（点击“取消预设”解锁手动输入字幕功能）")
        self.preset_lock_hint.setWordWrap(True)
        self.preset_lock_hint.setStyleSheet("color: #C86A00; font-size: 11px;")
        self.preset_lock_hint.setVisible(False)   # 只在选中预设时显示

        style_box = QGroupBox("字幕样式")
        stl = QVBoxLayout()
        stl.addWidget(QLabel("手动输入字幕："))
        stl.addWidget(self.preset_lock_hint)
        stl.addWidget(self.text_edit)
        stl.addWidget(self.fontsize_label)
        stl.addWidget(self.fontsize_slider)

        # 位置、字体格式、字体、颜色 用 form layout 让标签对齐
        form = QFormLayout()
        form.addRow("字幕位置：", self.position_combo)
        form.addRow("字体格式：", self.alignment_combo)
        form.addRow("字体：", self.font_combo)
        form.addRow("字体颜色：", color_row)
        stl.addLayout(form)

        stl.addWidget(self.opacity_label)
        stl.addWidget(self.opacity_slider)
        # 描边：勾选框 + 保存按钮并排
        outline_row = QHBoxLayout()
        outline_row.addWidget(self.outline_check)
        outline_row.addWidget(self.save_preset_btn)
        outline_row.addStretch(1)
        stl.addLayout(outline_row)
        # 描边粗细（只在勾了描边时有意义，取消勾选时禁用）
        stl.addWidget(self.outline_width_label)
        stl.addWidget(self.outline_width_slider)
        style_box.setLayout(stl)

        # --- 批量添加字幕 ---
        self.out_dir_btn = QPushButton("选择保存位置")
        self.out_dir_btn.clicked.connect(self.choose_output_dir)
        self.out_dir_label = QLabel("保存位置：" + self.output_dir)
        self.out_dir_label.setWordWrap(True)

        self.burn_btn = QPushButton("批量添加字幕")
        self.burn_btn.setMinimumHeight(40)
        self.burn_btn.clicked.connect(self.burn_all)

        # 编码提示：按钮下第二行提示，再往下显示当前编码速度和画质。
        # 用户要求「改成橙色、显眼点」—— 原来 #888 灰字太不显眼，
        # 很多人没注意到编码速度/画质要去「设置」里调。
        self.encode_tip_label = QLabel("（生成字幕视频的编码速度和画质请在设置里调整）")
        self.encode_tip_label.setWordWrap(True)
        self.encode_tip_label.setStyleSheet(
            "color: #E67E22; font-weight: bold; font-size: 12px;")
        self.encode_value_label = QLabel("")
        self.encode_value_label.setWordWrap(True)   # 加「并发」后文字变长，窄栏里要能换行
        self.encode_value_label.setStyleSheet("color: #333;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        self.progress_label = QLabel("")
        self.progress_label.hide()

        self.burn_pause_btn = QPushButton("暂停")
        self.burn_pause_btn.setToolTip("暂停/继续当前正在烧字幕的视频")
        self.burn_pause_btn.hide()
        self.burn_pause_btn.clicked.connect(self._on_burn_pause)
        self.burn_stop_btn = QPushButton("终止")
        self.burn_stop_btn.setToolTip("立刻停止批量烧字幕（已烧好的保留）")
        self.burn_stop_btn.hide()
        self.burn_stop_btn.clicked.connect(self._on_burn_stop)
        self._burn_paused = False
        self._burn_cancelled = False

        burn_box = QGroupBox("批量添加字幕")
        bb = QVBoxLayout()
        bb.addWidget(self.out_dir_btn)
        bb.addWidget(self.out_dir_label)
        bb.addWidget(self.burn_btn)
        bb.addWidget(self.encode_tip_label)
        bb.addWidget(self.encode_value_label)
        bb.addWidget(self.progress_bar)
        bb.addWidget(self.progress_label)
        bb_btns = QHBoxLayout()
        bb_btns.addWidget(self.burn_pause_btn)
        bb_btns.addWidget(self.burn_stop_btn)
        bb.addLayout(bb_btns)
        burn_box.setLayout(bb)

        # === 关键改动：用 QScrollArea 包住 4 个左侧区块 ===
        # 跟浏览器右边栏一样：内容超出可视区域就出滚动条，往下拖滚动条就能看到
        left_inner = QWidget()
        left_inner_layout = QVBoxLayout(left_inner)
        left_inner_layout.setContentsMargins(6, 6, 6, 6)
        left_inner_layout.setSpacing(10)
        # 顺序排列，宽度自适应滚动容器
        left_inner_layout.addWidget(video_box)        # 选择视频
        left_inner_layout.addWidget(source_box)       # 选择字幕
        left_inner_layout.addWidget(style_box)        # 字幕样式（最长的一块）
        left_inner_layout.addWidget(burn_box)         # 批量添加字幕
        left_inner_layout.addStretch(1)               # 底部留白

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)   # 内部控件宽度跟滚动条一样宽
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # 不要横滚动条（会撑破布局）
        left_scroll.setWidget(left_inner)
        # 让滚动条只在需要时出现，宽度限制避免撑得太宽
        # 设下限为 0：主窗口已设最小宽度 820，留给三栏自己分配，
        # 否则加起来会逼用户拖到全屏。
        left_scroll.setMinimumWidth(0)
        # 存成属性：开始批量烧字幕时要把左栏滚到底部，让用户看见进度条
        self.left_scroll = left_scroll

        # ====== 文件列表 ======
        self.file_list = QListWidget()
        # 下限设为 0：让拉伸因子 30:20:50 在主窗口 ≥820 时主导宽度分配，
        # 否则三个 minimumWidth 加起来 ≈ 360 + 220 + 视频预览的最低需要，
        # 在左半屏（≈ 960×1040）下也能正常伸展，不会被挤成竖条。
        self.file_list.setMinimumWidth(0)
        self.file_list.setStyleSheet("QListWidget { background-color: #fafafa; }")
        # 点击列表里的文件 → 切换播放器预览到该视频
        self.file_list.itemClicked.connect(self._on_file_clicked)
        # 勾选框变化 → 更新「移除勾选的视频」按钮上的计数
        self._filling_list = False
        self.file_list.itemChanged.connect(self._on_file_item_changed)
        list_box = QGroupBox("文件列表")
        lb = QVBoxLayout()
        self.file_count_label = QLabel("（无）")
        self.summary_label = QLabel("总大小：-　总时长：-")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: #555;")
        self.ratio_hint_label = QLabel("")
        self.ratio_hint_label.setWordWrap(True)
        self.ratio_hint_label.setStyleSheet("color: #b05000; font-size: 11px;")
        lb.addWidget(self.file_list)
        lb.addWidget(self.file_count_label)
        lb.addWidget(self.summary_label)
        lb.addWidget(self.ratio_hint_label)
        # 移除已勾选的视频：列表里每个文件左边有个勾选框，勾去掉要删的再点这里。
        # 典型场景：整个文件夹几百个视频，只有几个不想要 —— 勾那几个移除，
        # 不用一个个手动挑出要留下的。
        self.remove_checked_btn = QPushButton("移除勾选的视频")
        self.remove_checked_btn.setToolTip(
            "在下面的列表里勾选要去掉的视频（可按住 Ctrl 多选），再点这个按钮：\n"
            "勾选的会被移出列表，没勾的保留。\n"
            "适合「文件夹里几百个，只有几个不要」的情况。")
        self.remove_checked_btn.clicked.connect(self._remove_checked_videos)
        lb.addWidget(self.remove_checked_btn)
        # 清空视频列表：取消已选的全部视频，释放预览/缓存占用的内存
        self.clear_list_btn = QPushButton("清空视频列表")
        self.clear_list_btn.setToolTip("取消已选的全部视频，并释放预览/缓存占用的内存")
        self.clear_list_btn.clicked.connect(self._clear_video_list)
        lb.addWidget(self.clear_list_btn)
        list_box.setLayout(lb)

        # --- 字幕预览（右侧 83%，视频播放器 + 字幕叠加） ---
        self.aspect_combo = NoWheelCombo()
        self.aspect_combo.addItem("自动（跟随视频）")
        self.aspect_combo.addItems(list(ASPECT_RATIOS.keys()))
        self.aspect_combo.setCurrentText("自动（跟随视频）")
        self.aspect_combo.currentTextChanged.connect(self._on_aspect_changed)

        # 当前视频比例的"人话"说明，如「9:16（竖屏）1080×1920」，方便新手理解
        self.ratio_label = QLabel("未加载视频")
        self.ratio_label.setStyleSheet("color: #666; font-size: 11px;")
        # 预设分辨率与视频比例不匹配时的提醒（黄色醒目，但不阻断操作）
        self.ratio_warn_label = QLabel("")
        self.ratio_warn_label.setWordWrap(True)
        self.ratio_warn_label.setStyleSheet("color: #C86A00; font-size: 11px;")
        self.ratio_warn_label.setVisible(False)

        self.preview = PreviewWidget()              # 无视频时：纯字幕预览（黑色框）
        self.preview.setMinimumSize(0, 0)
        self.video_preview = VideoPreviewWidget()   # 有视频时：播放器 + 字幕叠加
        # 预览里直接拖字幕：把位移 (dx, dy) 换算成 MarginV/L/R 变化并回写
        self.video_preview.subtitle_item.on_drag_commit = self._on_subtitle_dragged

        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.preview)         # index 0
        self.preview_stack.addWidget(self.video_preview)   # index 1

        preview_box = QGroupBox("字幕预览")
        pb = QVBoxLayout()
        aspect_row = QHBoxLayout()
        aspect_row.addWidget(QLabel("视频比例："))
        aspect_row.addWidget(self.aspect_combo)
        aspect_row.addWidget(self.ratio_label)
        aspect_row.addStretch(1)
        pb.addLayout(aspect_row)
        pb.addWidget(self.ratio_warn_label)
        # preview_stack 占满 stretch=1，让预览框尽可能大
        pb.addWidget(self.preview_stack, 1)
        preview_box.setLayout(pb)

        # === 三栏扁平布局：左侧设置区 30 / 文件列表 20 / 字幕预览 50 ===
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(left_scroll)     # 选择视频/字幕/样式/批量添加字幕
        body.addWidget(list_box)        # 文件列表
        body.addWidget(preview_box)     # 字幕预览
        body.setStretchFactor(left_scroll, 30)
        body.setStretchFactor(list_box, 20)
        body.setStretchFactor(preview_box, 50)
        main_layout.addLayout(body, 1)
        # 状态行
        self.status = QLabel("提示：未选视频时自动从桌面「合成视频」文件夹读取")
        main_layout.addWidget(self.status)

    # ---------------- 视频选择 ----------------

    def _auto_load_default(self):
        """自动加载桌面「合成视频」文件夹（拼接 tab 输出的位置）"""
        from utils import default_output_dir
        default = default_output_dir()
        if os.path.isdir(default):
            vids = list_videos(default)
            if vids:
                self.videos = vids
                self._update_file_list("已自动加载桌面「合成视频」：{} 个视频".format(len(vids)))
                self.status.setText("自动加载：{}".format(default))
                return
        self._update_file_list("（桌面「合成视频」文件夹为空，请手动选择）")
        self.status.setText("未选择视频，请点击上方按钮选择文件或文件夹")

    def _update_file_list(self, label_text=None):
        """刷新右侧文件列表（每个文件左边带勾选框，供「移除勾选的视频」用）"""
        self._filling_list = True          # 填充期间不响应 itemChanged
        try:
            self.file_list.clear()
            for path in self.videos:
                item = QListWidgetItem(os.path.basename(path))
                item.setToolTip(path)      # 鼠标悬停看完整路径
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.file_list.addItem(item)
        finally:
            self._filling_list = False
        self._update_checked_count()
        if label_text is not None:
            self.video_label.setText(label_text)
        self.file_count_label.setText("共 {} 个".format(len(self.videos)))
        self._update_stats()
        self._load_first_video()   # 加载第一个视频到播放器预览
        self._scan_ratios()        # 后台扫描比例种类，用于不一致提示

    def _on_file_item_changed(self, _item):
        """勾选框被点了一下：更新按钮计数（填充列表时忽略）"""
        if getattr(self, "_filling_list", False):
            return
        self._update_checked_count()

    def _checked_video_indices(self):
        """当前被勾选的视频下标列表。"""
        out = []
        for i in range(self.file_list.count()):
            it = self.file_list.item(i)
            if it is not None and it.checkState() == Qt.Checked:
                out.append(i)
        return out

    def _update_checked_count(self):
        """把勾选数量显示在按钮文字上，避免误删。"""
        if not hasattr(self, "remove_checked_btn"):
            return
        n = len(self._checked_video_indices())
        if n:
            self.remove_checked_btn.setText("移除勾选的视频（已勾 {} 个）".format(n))
        else:
            self.remove_checked_btn.setText("移除勾选的视频")

    def _remove_checked_videos(self):
        """把列表里勾选的视频从已选列表移除，没勾的保留。"""
        if not self.videos:
            QMessageBox.information(self, "提示", "视频列表已经是空的")
            return
        idx = set(self._checked_video_indices())
        if not idx:
            QMessageBox.information(
                self, "提示",
                "还没有勾选任何视频。\n\n"
                "在下面的文件列表里勾选要去掉的视频（想勾多个可以按住 Ctrl 点），\n"
                "再点这个按钮 —— 勾选的会被移除，没勾的保留。")
            return
        removed = [p for i, p in enumerate(self.videos) if i in idx]
        self.videos = [p for i, p in enumerate(self.videos) if i not in idx]
        # 被移除的里面如果有正在预览的那个，释放播放器缓冲
        try:
            cur = getattr(self.video_preview, "current_path", None)
            if cur in removed:
                self.video_preview.player.pause()
                self.video_preview.player.setSource(QUrl())
                self.video_preview.current_path = None
        except Exception:
            pass
        names = "、".join(os.path.basename(p) for p in removed[:3])
        if len(removed) > 3:
            names += " 等 {} 个".format(len(removed))
        self._update_file_list("已移除 {} 个视频，还剩 {} 个".format(
            len(removed), len(self.videos)))
        self.status.setText("已移除 {} 个视频（{}）".format(len(removed), names))

    def _clear_video_list(self):
        """清空已选视频列表，并释放播放器缓冲 / mask 缓存占用的内存。"""
        if not self.videos:
            QMessageBox.information(self, "提示", "视频列表已经是空的")
            return
        n = len(self.videos)
        self.videos = []
        # 释放预览播放器的视频缓冲（内存占用大头）
        try:
            self.video_preview.player.pause()
            self.video_preview.player.setSource(QUrl())
            self.video_preview.current_path = None
        except Exception:
            pass
        # 清空字幕 mask 缓存（每张 1080x1920 约 8MB）
        try:
            self._ass_mask_cache.clear()
        except Exception:
            pass
        # 让仍在后台跑的统计/比例扫描结果作废（gen 号自增）
        self._stats_gen += 1
        self._ratio_gen += 1
        # 刷新界面
        self.file_list.clear()
        self._update_checked_count()
        self.video_label.setText("（已清空视频列表）")
        self.file_count_label.setText("共 0 个")
        self.summary_label.setText("总大小：-　总时长：-")
        self.ratio_hint_label.setText("")
        self._load_video_at(0)      # 无视频 → 回纯字幕预览
        self.status.setText("已清空视频列表（释放了 {} 个视频的预览占用）".format(n))

    def _update_stats(self):
        """统计视频总大小（同步）和总时长（后台线程探测），更新汇总栏"""
        if not self.videos:
            self.summary_label.setText("总大小：-　总时长：-")
            return
        try:
            total_size = sum(os.path.getsize(p) for p in self.videos if os.path.isfile(p))
        except OSError:
            total_size = 0
        self.summary_label.setText("总大小：{}　总时长：计算中…".format(_format_file_size(total_size)))
        self._stats_gen += 1
        gen = self._stats_gen
        paths = list(self.videos)

        def worker():
            try:
                d, s = total_stats(paths)
            except Exception:
                d, s = 0.0, total_size
            self._stats_bridge.done.emit(gen, d, s)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stats_done(self, gen, duration, size):
        """后台统计结果回来（主线程执行），忽略过期结果"""
        if gen != self._stats_gen:
            return
        self.summary_label.setText("总大小：{}　总时长：{}".format(
            _format_file_size(size), _format_total_duration(duration)))

    def _load_first_video(self):
        """加载列表里第一个视频到播放器预览"""
        self._load_video_at(0)

    def _on_file_clicked(self, item):
        """点击文件列表里的某个视频 → 切换播放器预览到该视频"""
        row = self.file_list.row(item)
        if row < 0 or row >= len(self.videos):
            return
        self._load_video_at(row)

    def _load_video_at(self, index):
        """加载列表里第 index 个视频到播放器预览（同步探测其宽高）。

        index 越界或文件不存在时回退到纯字幕预览（黑色框）。
        """
        self._current_dim = None
        if not self.videos or index < 0 or index >= len(self.videos):
            self.preview_stack.setCurrentIndex(0)   # 没视频，切回纯字幕预览
            self._set_aspect_locked(False)
            return
        path = self.videos[index]
        if not os.path.isfile(path):
            self.preview_stack.setCurrentIndex(0)
            self._set_aspect_locked(False)
            return
        # 同步探测该视频的宽高（约 50-200ms，一次性，可接受）
        dim = probe_dimensions(path)
        if dim and dim[0] and dim[1]:
            self._current_dim = dim
            self.video_preview.load_video(path, dim)
            # 同步更新纯字幕预览的 aspect/ref_height
            self.preview.update_all(aspect=dim, ref_height=dim[1])
        else:
            # 探测失败（可能损坏），仍尝试让播放器加载，nativeSizeChanged 会兜底
            self.video_preview.load_video(path, None)
        self.preview_stack.setCurrentIndex(1)   # 切到播放器
        self._set_aspect_locked(True)           # 有视频 → 比例锁定为视频实际比例
        self._refresh_preview()                  # 把当前字幕同步到视频软字幕层

    def _set_aspect_locked(self, locked):
        """视频已加载时锁定「视频比例」下拉：视频比例由实际分辨率决定，不可改。

        无视频时解锁，允许手动选比例做纯字幕预览（黑色框）。
        """
        if locked:
            self.aspect_combo.blockSignals(True)
            self.aspect_combo.setCurrentText("自动（跟随视频）")
            self.aspect_combo.blockSignals(False)
            self.aspect_combo.setEnabled(False)
            self.aspect_combo.setToolTip("视频比例由视频实际分辨率决定，加载视频后不可手动更改")
        else:
            self.aspect_combo.setEnabled(True)
            self.aspect_combo.setToolTip("未加载视频时可手动选择预览比例")
        self._update_ratio_label()

    def _scan_ratios(self):
        """后台扫描所有视频的比例种类，用于「比例不一致」提示"""
        if not self.videos:
            self.ratio_hint_label.setText("")
            return
        self._ratio_gen += 1
        gen = self._ratio_gen
        paths = list(self.videos)

        def worker():
            try:
                ratios, first_dim = collect_ratios(paths)
            except Exception:
                ratios, first_dim = {}, None
            self._ratio_bridge.done.emit(gen, ratios, first_dim)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ratios_done(self, gen, ratios, first_dim):
        """比例扫描结果回来（主线程），忽略过期结果"""
        if gen != self._ratio_gen:
            return
        if len(ratios) > 1:
            ratio_list = "、".join(list(ratios.keys()))
            self.ratio_hint_label.setText(
                "⚠️ 项目里有 {} 种比例（{}），预览仅显示第一个视频".format(len(ratios), ratio_list)
            )
        else:
            self.ratio_hint_label.setText("")

    def refresh_encode_hint(self):
        """刷新「当前编码速度和画质」显示（读取主窗口传入的 encode_settings）"""
        encode = getattr(self, "encode_settings", None) or {}
        # ⚠️ 这里的兜底值要和 config.DEFAULTS 保持一致（原来写的是 fast/18/1，
        #    改了默认值后不同步就会出现「设置里是 veryfast，这里显示 fast」）
        preset = encode.get("preset") or "veryfast"
        crf = encode.get("crf") or "23"
        workers = str(encode.get("max_workers") or "2")
        # ⚠️ macOS 上没有「编码优先级」设置 → 不显示这一项，
        #    免得用户找不到去哪儿改（见 paths.priority_supported()）。
        if paths.priority_supported():
            prio = encode.get("priority") or "normal"
            # 英文 key → 中文（与参考表下拉同口径）
            prio_cn = {"low": "低", "normal": "普通", "high": "高"}.get(prio, prio)
            self.encode_value_label.setText(
                "当前编码速度：{}　　当前画质：crf {}　　并发：{}　　优先级：{}".format(
                    preset, crf, workers, prio_cn))
        else:
            self.encode_value_label.setText(
                "当前编码速度：{}　　当前画质：crf {}　　并发：{}".format(
                    preset, crf, workers))

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择视频文件（可按住 Ctrl 多选）", "",
            "视频文件 (*.mp4 *.mov *.mkv *.avi *.flv *.m4v *.webm *.ts)"
        )
        if paths:
            self.videos = list(paths)
            self._update_file_list("已手动选择 {} 个视频文件".format(len(paths)))

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择视频文件夹")
        if folder:
            vids = list_videos(folder)
            if vids:
                self.videos = vids
                self._update_file_list("文件夹：{}（{} 个视频）".format(folder, len(vids)))
            else:
                QMessageBox.warning(self, "提示", "该文件夹里没有视频文件")

    def choose_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择保存位置", self.output_dir)
        if folder:
            self.set_output_dir(folder)

    def set_output_dir(self, folder, remember=True):
        """设置成品保存位置（并记住，下次打开程序还是这里）。"""
        self.output_dir = folder
        self.out_dir_label.setText("保存位置：" + folder)
        if remember:
            app_config.update(burn_output_dir=folder)

    # ---------------- 字幕源 / 样式 ----------------

    def _load_presets(self):
        """从 subtitle_presets 文件夹加载 .ass 预设（并把选择重置为「不使用预设」）"""
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("（不使用预设）")
        for name in list_presets():
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)
        # 信号只连一次：重复 connect 会导致一次选择触发多次刷新
        if not getattr(self, "_preset_signal_connected", False):
            self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
            self._preset_signal_connected = True
        self._apply_preset_state()

    def _cancel_preset(self):
        """取消预设字幕 → 回到「不使用预设」，解锁手动输入字幕。

        顺带重扫一遍 subtitle_presets 文件夹（新放进去的 .ass 也能出现在列表里）。
        """
        self._load_presets()
        self._apply_preset_state()
        self._refresh_preview()

    def _load_fonts(self):
        """从系统中加载真实存在的中文字体"""
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        self._font_paths = {}  # 显示名 → 文件路径
        for name, path in available_fonts():
            self.font_combo.addItem(name)
            self._font_paths[name] = path
        if self.font_combo.count() > 0:
            # 默认选中平台默认字体（原来硬编码 "微软雅黑"，macOS 上没有 → 选不中）
            self.font_combo.setCurrentText(_DEFAULT_FONT_NAME
                                           or self.font_combo.itemText(0))
        self.font_combo.blockSignals(False)

    def _open_preset_dir(self):
        """在文件管理器里打开预设文件夹"""
        if not os.path.isdir(PRESET_DIR):
            os.makedirs(PRESET_DIR, exist_ok=True)
        # 走 open_in_explorer（跨平台：Windows 用 startfile，macOS 用 open）
        open_in_explorer(PRESET_DIR)

    def _on_preset_changed(self, _text):
        self._apply_preset_state()
        self._update_ratio_label()
        self._refresh_preview()

    def _get_preset_info(self):
        """读取当前选中预设的解析结果（文本 + 全部样式）。无预设/解析失败返回 None。"""
        if self.preset_combo.currentIndex() <= 0:
            self._ass_source_path = None
            return None
        ass_name = self.preset_combo.currentText()
        if not ass_name:
            self._ass_source_path = None
            return None
        ass_path = os.path.join(PRESET_DIR, ass_name)
        self._ass_source_path = ass_path
        try:
            return parse_ass_for_preview(ass_path)
        except Exception:
            return None

    def _sync_preset_into_controls(self, info):
        """把预设的文本 + 样式写进手动输入区的各个控件。

        目的：选了预设后，手动输入框里显示的就是预设内容（禁用状态），
        用户点「取消预设」解锁后可以直接在这套内容/样式上做小幅度修改，
        而不用从零重设字号、颜色、位置等。

        写控件时统一 blockSignals，避免每写一个控件就触发一次预览刷新。
        """
        if not info:
            return
        widgets = [
            self.text_edit, self.fontsize_slider, self.opacity_slider,
            self.position_combo, self.alignment_combo, self.outline_check,
            self.font_combo, self.color_combo, self.outline_width_slider,
        ]
        for w in widgets:
            w.blockSignals(True)
        try:
            # 文本（\N 已在解析时转成真实换行）
            self.text_edit.setPlainText(info.get("text", ""))
            # 字号
            try:
                fs = int(info.get("fontsize", FONTSIZE_DEFAULT))
            except (TypeError, ValueError):
                fs = FONTSIZE_DEFAULT
            self.fontsize_slider.setValue(max(FONTSIZE_MIN, min(FONTSIZE_MAX, fs)))
            # 透明度
            try:
                op = int(round(float(info.get("opacity", 100))))
            except (TypeError, ValueError):
                op = 100
            self.opacity_slider.setValue(max(0, min(100, op)))
            # 位置 / 对齐
            pos = info.get("position") or "底部"
            if self.position_combo.findText(pos) >= 0:
                self.position_combo.setCurrentText(pos)
            align = info.get("alignment") or "居中"
            if self.alignment_combo.findText(align) >= 0:
                self.alignment_combo.setCurrentText(align)
            # 描边
            self.outline_check.setChecked(bool(info.get("outline", False)))
            # 描边粗细：带出预设里的真实值（Outline 字段，可能带小数）
            self._set_outline_width(info.get("outline_width") or 4.0)
            # 字体（.ass 里存的是字体名，如 "msyh"，映射回中文显示名）
            fam = info.get("font_family") or ""
            disp = _ASS_FONT_TO_DISPLAY.get(fam.lower())
            if not disp:
                # 退一步：用「显示名 → 字体文件路径」反查
                for _dname, _dpath in getattr(self, "_font_paths", {}).items():
                    if os.path.splitext(os.path.basename(_dpath))[0].lower() == fam.lower():
                        disp = _dname
                        break
            if disp and self.font_combo.findText(disp) >= 0:
                self.font_combo.setCurrentText(disp)
            # 颜色（下拉里没有的颜色自动落回「自定义...」，并把色块更新）
            hex_color = (info.get("color_hex") or "#FFFFFF").upper()
            matched = False
            for i, (_name, c) in enumerate(self._color_choices):
                if c.name().upper() == hex_color:
                    self.color_combo.setCurrentIndex(i)
                    matched = True
                    break
            if not matched:
                self.color_combo.setCurrentIndex(len(self._color_choices))  # 自定义...
            self._color = QColor(hex_color)
            if hasattr(self, "color_swatch"):
                self.color_swatch.setStyleSheet(
                    "border: 1px solid #888; background-color: {};".format(hex_color))
        finally:
            for w in widgets:
                w.blockSignals(False)
        # 字号标签跟着更新
        if hasattr(self, "fontsize_label"):
            self.fontsize_label.setText("调整字幕大小 {}".format(self.fontsize_slider.value()))
        if hasattr(self, "opacity_label"):
            self.opacity_label.setText("字体透明度 {}%".format(self.opacity_slider.value()))

        # ===== 记录「继承自预设的布局参数」=====
        # 取消预设后，预览/成片继续用预设的 MarginV/L/R 和 libass 行距渲染，
        # 否则手动模型（margin=96、字盒行距）和预设模型（MarginV=8、OS/2 行距）
        # 差异巨大 —— 用户看到的「取消预设后效果明显不一样」就是这个。
        if info:
            ff = _FONT_FILES.get(info.get("font_family")) or _default_font_file()
            line_h = None
            if ff:
                try:
                    line_h = int(round(_ass_line_metrics(ff, int(info.get("fontsize", 28)))))
                except Exception:
                    line_h = None
            self._inherited_layout = {
                "margin_v": info.get("margin_v", 0),
                "margin_l": info.get("margin_l", 0),
                "margin_r": info.get("margin_r", 0),
                "line_height": line_h,
                "fontsize": int(info.get("fontsize", 28)),
                # 样式快照：判断用户取消预设后改了哪些字段（改了的才写进临时 .ass）
                "snapshot": {
                    "fontsize": int(info.get("fontsize", 28)),
                    "color": (info.get("color_hex") or "#FFFFFF").upper(),
                    "opacity": int(info.get("opacity", 100)),
                    "position": info.get("position") or "底部",
                    "alignment": info.get("alignment") or "居中",
                    "outline": bool(info.get("outline", False)),
                    # 预设原本的描边粗细：用户取消勾选后再勾回来时沿用这个值，
                    # 而不是按字号重算（重算会跟原预设的观感不一致）。
                    "outline_width": info.get("outline_width"),
                    "font_display": self.font_combo.currentText(),
                },
            }
            self._inherited_ass_source = self._ass_source_path
        else:
            self._inherited_layout = None
            self._inherited_ass_source = None

    def _apply_preset_state(self):
        """根据预设是否被选中，启用/禁用手动字幕样式控件"""
        use_preset = self.preset_combo.currentIndex() > 0
        # 选了预设 → 把预设的字幕内容/样式同步到手动输入区（仍禁用），
        # 这样点「取消预设」解锁后，用户可以直接基于预设内容做小幅度修改。
        if use_preset:
            self._sync_preset_into_controls(self._get_preset_info())
        # 手动输入 + 字幕样式整体在选预设时禁用
        self.text_edit.setEnabled(not use_preset)
        # 选了预设时给出「怎么解锁」的提示
        if hasattr(self, "preset_lock_hint"):
            self.preset_lock_hint.setVisible(use_preset)
        if hasattr(self, "cancel_preset_btn"):
            self.cancel_preset_btn.setEnabled(use_preset)
        for w in (self.fontsize_slider, self.position_combo, self.alignment_combo,
                  self.font_combo, self.color_combo, self.opacity_slider,
                  self.outline_check, self.outline_width_slider):
            w.setEnabled(not use_preset)
        # 描边粗细还要额外跟随「黑色描边」勾选状态（没勾描边时调粗细没意义）
        if not use_preset:
            on = self.outline_check.isChecked()
            self.outline_width_slider.setEnabled(on)
            self.outline_width_label.setEnabled(on)

    def _on_font_changed(self, _text):
        self._refresh_preview()

    def _on_text_changed(self):
        self._refresh_preview()

    # ---------------- 继承预设：临时 .ass 生成 ----------------

    def _build_inherited_ass(self, shape_only=False):
        """取消预设后基于原预设生成临时 .ass（成片用 libass 烧，保证跟预设同一渲染模型）。

        - Style：保留原预设的全部字段（Bold/Spacing/MarginV/ScaledBorderAndShadow 等），
          仅替换用户在 UI 上改过的字段（字号/颜色/透明度/位置/对齐/描边/字体）
        - Dialogue：全部去掉，换成一条（时间轴沿用原第一条），文本用输入框当前内容
        返回临时文件路径；无继承来源或读取失败返回 None。

        shape_only=True（预览专用）：输出「纯白不透明形状版」—— 颜色强制不透明白、
        透明度强制 100%。预览只拿它当形状（颜色/透明度由 Qt 上色），这样用户把
        「字体透明度」调低时字是正常变淡，而不是与背景混成粉色。
        成片始终用 shape_only=False（真实样式），由 libass 正常渲染。
        """
        src = getattr(self, "_inherited_ass_source", None)
        inh = getattr(self, "_inherited_layout", None)
        if not src or not inh or not os.path.isfile(src):
            return None
        try:
            with open(src, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            try:
                with open(src, "r", encoding="gbk") as f:
                    content = f.read()
            except Exception:
                return None

        text = self.text_edit.toPlainText().strip()
        if not text:
            return None

        # ---- 解析并替换 Style 行 / 重建 Dialogue ----
        # 单遍扫描：按 [Events] Format 的实际字段解析原 Dialogue，再统一按标准
        # 10 字段输出。之前保留原 Format 行 + 硬塞 10 字段 Dialogue，遇到只有
        # 5 个字段的预设（Layer,Start,End,Style,Text）会整行错位 —— 表现为
        # 「取消预设后第一行字幕位置/内容不对」。
        STD_EVENTS_FIELDS = ["Layer", "Start", "End", "Style", "Name",
                             "MarginL", "MarginR", "MarginV", "Effect", "Text"]
        snap = inh.get("snapshot", {})
        out_lines = []
        section = None
        style_format_fields = []
        events_format_fields = list(STD_EVENTS_FIELDS)   # Format 行缺失时的默认
        first_dialogue_fields = None                     # 原第一条 Dialogue 按字段名存
        font_display_changed = (self.font_combo.currentText() != snap.get("font_display"))
        new_fontname = None
        if font_display_changed:
            fp = self._font_paths.get(self.font_combo.currentText())
            if fp:
                new_fontname = os.path.splitext(os.path.basename(fp))[0]

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.lower()
                out_lines.append(line)
                continue
            low = stripped.lower()
            if section == "[v4+ styles]" and low.startswith("format:"):
                style_format_fields = [x.strip() for x in stripped[7:].split(",")]
                out_lines.append(line)
                continue
            if section == "[v4+ styles]" and low.startswith("style:") and style_format_fields:
                values = [v.strip() for v in stripped[6:].split(",")]
                values += [""] * (len(style_format_fields) - len(values))
                field = dict(zip(style_format_fields, values))

                # 字号
                if self.fontsize_slider.value() != snap.get("fontsize"):
                    field["Fontsize"] = str(self.fontsize_slider.value())
                # 颜色 + 透明度（PrimaryColour = &HAABBGGRR）
                cur_color = self._color.name().upper()
                cur_op = self.opacity_slider.value()
                if shape_only:
                    # 形状版：预览只取「字形轮廓」，颜色/透明度交给 Qt 侧上色
                    # （半透明字与背景物理混合会变粉，所以绝不能在 ffmpeg 里出颜色）。
                    #
                    # ⚠️ 分层取形状（关键）：描边和本体必须是**两个不同的亮度**，
                    # 否则没法在 Qt 侧分别上色：
                    #   - 本体（PrimaryColour）= 白 255 → mask 里 255
                    #   - 描边（OutlineColour）= 中灰 128 → mask 里 128
                    #   - 背景（BackColour）  = 全透明 → mask 里 0
                    # 黑底烧录取灰度时：0=背景、128=描边、255=字心。
                    # 之前把 OutlineColour 也刷成黑色，描边跟背景同为 0 →
                    # 描边在 mask 里直接消失，表现为"预设有黑描边但预览没有"。
                    field["PrimaryColour"] = "&H00FFFFFF"      # 本体 → 255
                    field["SecondaryColour"] = "&H00FFFFFF"
                    field["OutlineColour"] = "&H00808080"      # 描边 → 128
                    field["BackColour"] = "&HFF000000"         # 背景框 → 透明
                elif cur_color != snap.get("color") or cur_op != snap.get("opacity"):
                    h = cur_color.lstrip("#")
                    alpha = "{:02X}".format(255 - int(round(cur_op * 2.55)))
                    field["PrimaryColour"] = "&H{a}{b}{g}{r}".format(
                        a=alpha, b=h[4:6], g=h[2:4], r=h[0:2])
                # 位置/对齐（ASS Alignment 1-9）
                pos_t = self.position_combo.currentText()
                align_t = self.alignment_combo.currentText()
                if pos_t != snap.get("position") or align_t != snap.get("alignment"):
                    align_map = {
                        ("底部", "居中"): "2", ("底部", "居左"): "1", ("底部", "居右"): "3",
                        ("中部", "居中"): "5", ("中部", "居左"): "4", ("中部", "居右"): "6",
                        ("顶部", "居中"): "8", ("顶部", "居左"): "7", ("顶部", "居右"): "9",
                    }
                    field["Alignment"] = align_map.get((pos_t, align_t), field.get("Alignment", "2"))
                # 描边
                # ⚠️ 这里不能用「跟快照比有没有变」来决定写不写：
                # 取消预设后用户把描边勾回原样（或勾选状态恰好 == 预设原值），
                # 判定为"没改"→ 一个字段都不写 → 用户看到"改了没任何变化"。
                # 描边必须按当前勾选状态**无条件写死**，保证 UI 所见即所得。
                cur_outline = self.outline_check.isChecked()
                fs_now = self.fontsize_slider.value()
                if cur_outline:
                    # 粗细用滑块当前值（选预设时已带出预设真实值，用户也能手动调）。
                    w_now = getattr(self, "_outline_width", 0) or 0
                    if w_now <= 0:
                        w_now = max(1, fs_now // 12)
                    field["BorderStyle"] = "1"
                    field["Outline"] = "{:.1f}".format(w_now)
                    field["Shadow"] = field.get("Shadow") or "0"
                else:
                    # 关描边：BorderStyle=0 时 libass 不画描边，但 Outline≠0 且
                    # 颜色不透明时会退化成"背景框"，所以 Outline/Shadow 一起清零。
                    field["BorderStyle"] = "0"
                    field["Outline"] = "0"
                    field["Shadow"] = "0"
                # 字体
                if new_fontname:
                    field["Fontname"] = new_fontname

                # 位置边距：无条件写进 Style。
                # 「自定义（可拖拽）」改的就是这三个值；另外 ASS 里 Dialogue 的
                # margin 写 0 表示「用 Style 的值」，所以两边写同一个数最不容易出歧义。
                field["MarginL"] = str(int(inh.get("margin_l") or 0))
                field["MarginR"] = str(int(inh.get("margin_r") or 0))
                field["MarginV"] = str(int(inh.get("margin_v") or 0))

                out_lines.append("Style: " + ",".join(
                    field.get(f, "") for f in style_format_fields))
                continue
            if section == "[events]" and low.startswith("format:"):
                # 只记录字段名，输出时统一改成标准 10 字段
                events_format_fields = [x.strip() for x in stripped[7:].split(",")]
                out_lines.append("Format: " + ",".join(STD_EVENTS_FIELDS))
                continue
            if section == "[events]" and low.startswith("dialogue:") and events_format_fields:
                # 只留第一条（文本已由 UI 接管，其余丢弃）
                if first_dialogue_fields is None:
                    vals = [v.strip() for v in
                            stripped[9:].split(",", len(events_format_fields) - 1)]
                    vals += [""] * (len(events_format_fields) - len(vals))
                    first_dialogue_fields = dict(zip(events_format_fields, vals))
                continue
            out_lines.append(line)

        # ---- 组装新 Dialogue：沿用原第一条的时间轴/层级/Style，仅换文本 + 边距 ----
        f = first_dialogue_fields or {}
        ass_text = text.replace("\n", "\\N").replace("{", "\\{").replace("}", "\\}")
        # ⚠️ Dialogue 自己的 MarginL/MarginR/MarginV **优先于** Style 里的同名字段。
        # 用户拖动字幕改的是 _inherited_layout 里的 margin，如果只改 Style，
        # Dialogue 里沿用的旧值（预设里常见是 0）会把它盖掉 → 看起来"拖了没用"。
        # 所以三个 margin 都无条件写进 Dialogue（Style 那边也写了同一份值）。
        ml_now = int(inh.get("margin_l") or 0)
        mr_now = int(inh.get("margin_r") or 0)
        mv_now = inh.get("margin_v")
        if mv_now is None:
            mv_now = f.get("MarginV") or "0"
        new_dialogue = "Dialogue: {layer},{start},{end},{style},{name},{ml},{mr},{mv},{effect},{text}".format(
            layer=f.get("Layer") or "0",
            start=f.get("Start") or "0:00:00.00",
            end=f.get("End") or "9:59:59.00",
            style=f.get("Style") or "Default",
            name=f.get("Name") or "",
            ml=ml_now,
            mr=mr_now,
            mv=int(mv_now),
            effect=f.get("Effect") or "",
            text=ass_text,
        )
        content = "\n".join(out_lines) + "\n" + new_dialogue + "\n"

        # ---- 写临时文件 ----
        # 文件名带内容哈希：内容变了 → 路径变 → 预览 mask 缓存自动失效，
        # 不会出现「改了字/颜色但预览还是旧图」。
        try:
            import tempfile
            import hashlib
            tmp_dir = os.path.join(tempfile.gettempdir(), "video_tool_ass")
            os.makedirs(tmp_dir, exist_ok=True)
            digest = hashlib.md5(content.encode("utf-8")).hexdigest()[:10]
            tmp_path = os.path.join(tmp_dir, "inherited_{}.ass".format(digest))
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            # 顺手清理旧文件（只留最近 8 份），避免临时目录无限增长
            try:
                olds = [os.path.join(tmp_dir, n) for n in os.listdir(tmp_dir)
                        if n.startswith("inherited_") and n.endswith(".ass")]
                for p in sorted(olds, key=os.path.getmtime, reverse=True)[8:]:
                    os.remove(p)
            except Exception:
                pass
            return tmp_path
        except Exception:
            return None

    def _save_manual_as_ass(self):
        """把当前手动输入的字幕（含所有样式）保存为 .ass 文件，下次可直接调用。

        生成的 .ass 符合 libass 规范：Style 含 Fontname/Fontsize/PrimaryColour/
        Outline/Alignment/MarginL/MarginR/MarginV，Dialogue 含 Text。
        存到 subtitle_presets/ 子目录后，用户在"预设字幕"下拉框直接选它复用。
        """
        text = self.text_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请先输入字幕文本")
            return

        # 弹出保存对话框，默认目录 subtitle_presets/
        default_name = "我的字幕.ass"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存为字幕文件",
            os.path.join(PRESET_DIR, default_name),
            "ASS 字幕文件 (*.ass);;所有文件 (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".ass"):
            path = path + ".ass"

        # 取当前样式
        fontsize = self.fontsize_slider.value()
        color = self._color.name()                # #RRGGBB
        opacity = self.opacity_slider.value()     # 0-100
        position_text = self.position_combo.currentText()  # "底部" / "中部" / "顶部"
        alignment_text = self.alignment_combo.currentText()  # "居中" / "居左" / "居右"
        outline = self.outline_check.isChecked()
        font_name = self.font_combo.currentText()  # 显示名

        # ASS 颜色 = &H + alpha + BGR（一个 &H 前缀，alpha=00 不透明、FF 全透明）
        hex_color = color.lstrip("#")
        rr, gg, bb = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        # opacity 0-100 → ASS alpha 00-FF（00 不透明，FF 全透明）
        # 用 round 避开 100*2.55=254.999... 这种浮点误差（int 截断会少 1）
        alpha_hex = "{:02X}".format(255 - int(round(opacity * 2.55)))
        ass_color = "&H{alpha}{bb}{gg}{rr}".format(
            alpha=alpha_hex, bb=bb.upper(), gg=gg.upper(), rr=rr.upper()
        )

        # ASS Alignment: 1=左下 2=中下 3=右下 4=左中 5=正中 6=右中 7=左上 8=中上 9=右上
        align_map = {
            ("底部", "居中"): 2, ("底部", "居左"): 1, ("底部", "居右"): 3,
            ("中部", "居中"): 5, ("中部", "居左"): 4, ("中部", "居右"): 6,
            ("顶部", "居中"): 8, ("顶部", "居左"): 7, ("顶部", "居右"): 9,
        }
        ass_alignment = align_map.get((position_text, alignment_text), 2)

        # ASS 字体名（要写进 Style 的 Fontname）
        # ⚠️ FONT_OPTIONS 的值是「候选路径列表」，要**按平台挑第一个存在的**；
        #    写进 .ass 的应该是本机真实可用的字体名，否则 libass 找不到会退默认字体。
        from subtitle import FONT_OPTIONS
        font_file = None
        for _n, _cands in FONT_OPTIONS:
            if _n != font_name:
                continue
            if isinstance(_cands, str):
                _cands = [_cands]
            font_file = next((p for p in _cands if os.path.isfile(p)), None)
            break
        if font_file:
            ass_fontname = os.path.splitext(os.path.basename(font_file))[0]
        else:
            # 🔴 别硬编码 "Microsoft YaHei"（macOS 没这个字体）。
            #    libass 找不到 Fontname 会退回默认字体，字形会不对。
            _df = _default_font_file()
            ass_fontname = (os.path.splitext(os.path.basename(_df))[0]
                            if _df else "Arial")

        # 描边：ASS 用 BorderStyle=1 + Outline 字段（粗细用滑块值，可小数）
        border_style = 1 if outline else 0
        outline_w = (getattr(self, "_outline_width", 0) or max(1, fontsize // 12)) if outline else 0
        # Shadow=1 给文字下方一点阴影，跟 ffmpeg drawtext 的描边视觉接近
        shadow = 1 if outline else 0

        # ASS 文件内容
        ass_content = (
            "[Script Info]\n"
            "Title: {name}\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1080\n"
            "PlayResY: 1920\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,{fontname},{fontsize},{color},&H00FFFFFF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},{ass_alignment},15,15,8,134\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        ).format(
            name=os.path.splitext(os.path.basename(path))[0],
            fontname=ass_fontname, fontsize=fontsize,
            color=ass_color,
            border_style=border_style, outline_w=outline_w, shadow=shadow,
            ass_alignment=ass_alignment,
        )

        # 把多行合并到一个 Dialogue 内，用 \N 分隔（ffmpeg/libass 规范的硬换行）。
        # 不能拆多个 Dialogue，否则每一行会按自己的 baseline 锚定烧录，破坏多行布局。
        ass_text = text.replace("\n", "\\N")
        # ASS 文本需要 escape { } （虽然我们没用，但稳妥）+ 保留 \N
        ass_text = ass_text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        ass_text = ass_text.replace("\\\\N", "\\N")  # 防止 \\N 被双重 escape 成 \\\N
        ass_content += (
            "Dialogue: 0,0:00:00.00,9:59:59.00,Default,,0,0,0,,{text}\n"
        ).format(text=ass_text)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(ass_content)
            QMessageBox.information(self, "已保存",
                "字幕文件已保存到：\n{}\n\n下次在「预设字幕」下拉框选「{}」即可直接使用。".format(
                    path, os.path.splitext(os.path.basename(path))[0]))
            # 刷新预设下拉框列表，让用户能立刻选它
            self._load_presets()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _on_fontsize_changed(self, v):
        self.fontsize_label.setText("调整字幕大小 {}".format(v))
        # 继承模式：libass 行距与字号成正比，字号改了行距等比缩放（margin 不变，
        # libass 的 MarginV 也不随字号变）。这样改字号后预览/成片仍是预设的排版模型。
        inh = getattr(self, "_inherited_layout", None)
        if inh and inh.get("line_height") and inh.get("fontsize"):
            try:
                old_fs = int(inh["fontsize"])
                if old_fs > 0 and v != old_fs:
                    inh["line_height"] = max(1, int(round(inh["line_height"] * v / old_fs)))
                    inh["fontsize"] = int(v)
            except Exception:
                pass
        self._refresh_preview()

    def _on_opacity_changed(self, v):
        self.opacity_label.setText("字体透明度 {}%".format(v))
        self._refresh_preview()

    def _on_subtitle_dragged(self, dx, dy):
        """预览里拖动字幕结束：把位移换算成 MarginV / MarginL / MarginR 再刷新。

        为什么用 margin 而不是别的：libass 摆放文字的规则是「按 Alignment 锚定
        一条边，再用 MarginL/R/V 做偏移」，margin 是它唯一的定位手段。
        用 margin 的另一个好处是预览和成片共用同一套数字，不需要额外对齐。

        实测确认的 libass 行为（1080x1920、Align=2 底部居中）：
            文字块中心 x = (MarginL + (画面宽 - MarginR)) / 2
            文字块底边 y = 画面高 - MarginV
        所以：
            - 上下拖：MarginV -= dy      （MarginV 越大越靠上）
            - 居中时左右拖：要「中心 +dx」需要 MarginL - MarginR 增加 2*dx
              为不让 MarginR 变成负数，dx>0 时只加 MarginL，dx<0 时只加 MarginR
            - 居左时左边界 = MarginL，居右时右边界 = 宽 - MarginR
        """
        if not dx and not dy:
            return
        inh = getattr(self, "_inherited_layout", None)
        if not inh:
            # 没有「继承自预设」的布局：没有可写的 margin，也给不出可烧的临时 .ass
            self.status.setText("请先在上方选一个字幕预设，再来拖动定位")
            return

        align = self.alignment_combo.currentText()

        if dy:
            inh["margin_v"] = int(inh.get("margin_v") or 0) - int(dy)

        if dx:
            ml = int(inh.get("margin_l") or 0)
            mr = int(inh.get("margin_r") or 0)
            if align == "居左":
                inh["margin_l"] = ml + int(dx)
            elif align == "居右":
                inh["margin_r"] = mr - int(dx)
            else:                       # 居中
                if dx > 0:
                    inh["margin_l"] = ml + 2 * int(dx)
                else:
                    inh["margin_r"] = mr - 2 * int(dx)

        self._clamp_custom_margins(align)
        self.status.setText("自定义位置：距底部 {} · 左边距 {} · 右边距 {}".format(
            inh["margin_v"], inh["margin_l"], inh["margin_r"]))
        self._refresh_preview()

    def _custom_block_size(self):
        """当前字幕块在**视频像素坐标**下的 (宽, 高)，用来限制拖动范围。

        首选：上一次 libass 实烧出来的 mask 尺寸 —— 那就是 libass 眼里的真实
              块大小，最准，而且字号/文本一变、重新烧录后会自动跟着更新。
        兜底：按字体度量估算（宽度要除以 LIBASS_FONT_RATIO，见常量注释）。
        """
        try:
            si = getattr(getattr(self, "video_preview", None), "subtitle_item", None)
            rect = getattr(si, "_mask_rect", None)
            if rect and rect[2] > 4 and rect[3] > 4:
                # _mask_rect 自带了上下左右各 2px 外扩，减掉才是墨迹本身
                return float(rect[2] - 4), float(rect[3] - 4)
        except Exception:
            pass
        try:
            lines = [ln for ln in self.text_edit.toPlainText().split("\n") if ln.strip()]
            if not lines:
                return None, None
            fs = int(self.fontsize_slider.value())
            fpath = self._font_paths.get(self.font_combo.currentText())
            if not fpath:
                return None, None
            widest = max(_line_metrics_tight(fpath, fs, ln)[0] for ln in lines)
            pitch = _ass_line_metrics(fpath, fs) or (fs * 1.32)
            return widest / LIBASS_FONT_RATIO, pitch * len(lines)
        except Exception:
            return None, None

    def _custom_block_width(self):
        return self._custom_block_size()[0]

    def _clamp_custom_margins(self, align):
        """把 margin 夹到「字幕块整块留在画面内」，并化简成最简形式。

        为什么要夹：文字一旦越过画面边缘，libass 的处理（贴边 + 挤压缩排）
        不好精确预测，预览就容易和成片对不上。夹在画面内可保证预览=成片
        （这个范围实测过，误差 1~2px）。

        为什么要化简（居中时）：居中的视觉效果**只取决于「中心 x」**
            center = (MarginL + 画面宽 - MarginR) / 2
        也就是只取决于 MarginL - MarginR，两个数同时加一个大常数结果不变。
        所以统一化成「较小的那个 = 0」：
          - 数字始终很小，不会拖几次就滚到几千
          - 中心位置一个像素都不变
        """
        inh = getattr(self, "_inherited_layout", None)
        if not inh:
            return
        W, H = self._video_size()
        if not W or not H:
            return
        lw, th = self._custom_block_size()
        lw = lw or 0
        th = th or 0
        ml = int(inh.get("margin_l") or 0)
        mr = int(inh.get("margin_r") or 0)
        mv = int(inh.get("margin_v") or 0)

        if align == "居左":
            ml = max(0, min(ml, max(0, W - lw)))
        elif align == "居右":
            mr = max(0, min(mr, max(0, W - lw)))
        else:
            center = (ml + W - mr) / 2.0
            if lw and lw <= W:
                center = max(lw / 2.0, min(center, W - lw / 2.0))
            d = int(round(2 * center - W))
            ml, mr = (d, 0) if d >= 0 else (0, -d)

        # 上下：MarginV = 文字块底边距画面底部的距离（0 = 贴底）。
        # 上限要扣掉文字块自身高度，否则整块会被推出画面顶部（实烧出来是空帧）。
        mv = max(0, min(mv, max(0, H - th)))

        inh["margin_l"], inh["margin_r"], inh["margin_v"] = ml, mr, mv

    def _update_ratio_label(self):
        """更新「视频比例」说明 + 预设分辨率不匹配提醒。"""
        if not hasattr(self, "ratio_label"):
            return
        dim = getattr(self, "_current_dim", None)
        if dim and dim[0] and dim[1]:
            w, h = int(dim[0]), int(dim[1])
            r = simplify_ratio(w, h) or "{}:{}".format(w, h)
            orient = "竖屏" if h > w else ("横屏" if w > h else "方形")
            self.ratio_label.setText("{}（{}）{}×{}".format(r, orient, w, h))
        else:
            # 没加载视频时，用下拉框当前选的预览比例
            text = self.aspect_combo.currentText()
            if text == "自动（跟随视频）":
                self.ratio_label.setText("未加载视频")
            else:
                w, h = ASPECT_RATIOS.get(text, (0, 0))
                if w and h:
                    r = simplify_ratio(w, h) or text
                    orient = "竖屏" if h > w else ("横屏" if w > h else "方形")
                    self.ratio_label.setText("{}（{}）{}×{}".format(r, orient, w, h))

        # ---- 预设分辨率 vs 当前视频比例 校验 ----
        warn = ""
        name = self.preset_combo.currentText() if hasattr(self, "preset_combo") else ""
        if name and self.preset_combo.currentIndex() > 0 and dim and dim[0] and dim[1]:
            pw, ph = get_preset_resolution(os.path.join(PRESET_DIR, name))
            if pw and ph:
                cur_r = simplify_ratio(int(dim[0]), int(dim[1]))
                pre_r = simplify_ratio(pw, ph)
                if cur_r and pre_r and cur_r != pre_r:
                    warn = ("⚠️ 预设「{}」是按 {}×{}（{}）做的，当前视频是 {}×{}（{}）。"
                            "比例不一致时字幕的字号和位置可能看着不对，建议换用同比例的预设。"
                            .format(name, pw, ph, pre_r, int(dim[0]), int(dim[1]), cur_r))
        elif name and self.preset_combo.currentIndex() > 0 and not (dim and dim[0]):
            # 还没加载视频：只提示这个预设的设计分辨率，方便先判断合不合适
            pw, ph = get_preset_resolution(os.path.join(PRESET_DIR, name))
            if pw and ph:
                pre_r = simplify_ratio(pw, ph)
                warn = "该预设按 {}×{}（{}）设计。请确认视频比例与之一致。".format(
                    pw, ph, pre_r)
        if hasattr(self, "ratio_warn_label"):
            self.ratio_warn_label.setText(warn)
            self.ratio_warn_label.setVisible(bool(warn))
            # 没加载视频时是"提示"而非"警告"，用中性色
            if warn and not (dim and dim[0]):
                self.ratio_warn_label.setStyleSheet("color: #666; font-size: 11px;")
            else:
                self.ratio_warn_label.setStyleSheet("color: #C86A00; font-size: 11px;")

    def _on_aspect_changed_extra(self):
        self._update_ratio_label()

    def _video_height(self):
        """当前视频的原生高度（无视频时退回预览比例高度）。"""
        dim = getattr(self, "_current_dim", None)
        if dim and dim[1]:
            return int(dim[1])
        return int(self.preview.aspect[1] or 1920)

    def _video_size(self):
        """当前视频的原生分辨率 (宽, 高)；无视频时退回预览比例。"""
        dim = getattr(self, "_current_dim", None)
        if dim and dim[0] and dim[1]:
            return int(dim[0]), int(dim[1])
        a = getattr(self.preview, "aspect", None) or (1080, 1920)
        return int(a[0] or 1080), int(a[1] or 1920)

    def _is_custom_position(self):
        """当前位置是不是「自定义（可拖拽）」。"""
        return self.position_combo.currentText() == CUSTOM_POSITION

    def _effective_position(self):
        """排版用的位置名：自定义档按「底部」语义（MarginV = 距底距离）。

        自定义档只改 margin，不改对齐锚点；用「底部」语义可以让文字从画面
        最下方一直到最上方自由摆放（MarginV 越大越靠上）。
        """
        return "底部" if self._is_custom_position() else self.position_combo.currentText()

    def _on_outline_toggled(self, _state):
        """勾/取消「黑色描边」：联动描边粗细滑块的可用状态，再刷新预览。"""
        on = self.outline_check.isChecked()
        if hasattr(self, "outline_width_slider"):
            self.outline_width_slider.setEnabled(on)
            self.outline_width_label.setEnabled(on)
        self._refresh_preview()

    def _on_outline_width_changed(self, v):
        """描边粗细滑块（5~60）→ 浮点值（0.5~6.0）。"""
        self._outline_width = round(v / 10.0, 1)
        if hasattr(self, "outline_width_label"):
            self.outline_width_label.setText("描边粗细 {:.1f}".format(self._outline_width))
        self._refresh_preview()

    def _set_outline_width(self, width):
        """把描边粗细写进滑块（选预设时带出预设的真实值）。"""
        try:
            width = float(width)
        except (TypeError, ValueError):
            width = 4.0
        if width <= 0:
            width = 4.0
        width = max(0.5, min(6.0, width))
        self._outline_width = round(width, 1)
        if hasattr(self, "outline_width_slider"):
            self.outline_width_slider.blockSignals(True)
            self.outline_width_slider.setValue(int(round(width * 10)))
            self.outline_width_slider.blockSignals(False)
        if hasattr(self, "outline_width_label"):
            self.outline_width_label.setText("描边粗细 {:.1f}".format(self._outline_width))

    def _on_aspect_changed(self, text):
        if text == "自动（跟随视频）":
            dim = getattr(self, "_current_dim", None)
            w, h = dim if (dim and dim[0] and dim[1]) else (1080, 1920)
        else:
            w, h = ASPECT_RATIOS.get(text, (1080, 1920))
        self.preview.update_all(aspect=(w, h), ref_height=h)
        if hasattr(self, "video_preview"):
            self.video_preview.set_aspect(w, h)
        self._update_ratio_label()

    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, "选择字体颜色")
        if color.isValid():
            self._color = color
            self._update_color_swatch()
            self._refresh_preview()

    def _on_color_combo_changed(self, idx):
        """颜色下拉变化：预设色直接用；自定义才弹对话框"""
        # 0~5 是预设色，最后一项是「自定义...」
        if idx < len(self._color_choices):
            self._color = self._color_choices[idx][1]
            self._update_color_swatch()
            self._refresh_preview()
        else:
            # 自定义：弹颜色对话框
            color = QColorDialog.getColor(self._color, self, "选择字体颜色")
            if color.isValid():
                self._color = color
            else:
                # 用户取消，保持原色，但下拉回到「自定义...」（让用户能再次打开）
                pass
            # 自定义颜色不在列表里，所以下拉保持「自定义...」显示
            self._update_color_swatch()
            self._refresh_preview()

    def _update_color_swatch(self):
        """右侧小色块反映当前颜色"""
        self.color_swatch.setStyleSheet(
            "border: 1px solid #888; background-color: {};".format(self._color.name())
        )

    def _refresh_preview(self):
        """刷新预览内容（同时更新纯字幕预览 + 视频覆盖层）。

        关键逻辑：
          - 选预设字幕时 → 解析 .ass 文件，把里面的文字/字号/颜色/位置直接画出来
          - 手动模式时 → 用 UI 上当前选的参数画
        """
        use_ass = self.preset_combo.currentIndex() > 0
        # 视频预览要用哪份 .ass 的 libass 渲染结果（None = 用 QPainter 画）
        mask_ass = None

        if use_ass:
            # 解析 .ass 预设，按它里面的样式渲染
            ass_name = self.preset_combo.currentText()
            ass_path = os.path.join(PRESET_DIR, ass_name) if ass_name else None
            info = parse_ass_for_preview(ass_path) if ass_path else None
            if info:
                # .ass 行距 = libass 实际使用的行距（= OS/2 sTypoAsc + |sTypoDesc| + sTypoLineGap，
                # 跟 drawtext 的 hhea 行距不一样）。直接从字体文件读 OS/2，跟 libass 用同一个数字。
                ff = _FONT_FILES.get(info["font_family"]) or _default_font_file()
                line_h = _ass_line_metrics(ff, info["fontsize"]) if ff else None
                kwargs = dict(
                    text=info["text"],
                    fontsize=info["fontsize"],
                    color=QColor(info["color_hex"]),
                    opacity=info["opacity"] / 100.0,
                    position=info["position"],
                    alignment=info["alignment"],
                    outline=info["outline"],
                    use_ass=False,           # 不再画「使用预设字幕」占位符
                    ass_name="",            # 也不显示文件名
                    font_family=info["font_family"],
                    # .ass 的 MarginV/L/R + 行距透传给画函数，跟 libass 实际渲染对齐
                    margin_v=info.get("margin_v", 0),
                    margin_l=info.get("margin_l", 0),
                    margin_r=info.get("margin_r", 0),
                    line_height=int(round(line_h)) if line_h else None,
                )
                # 统一管线：预设模式也用归一化临时 .ass（Style 原样、Dialogue 合并）。
                # 有的预设把每行拆成多条同时显示的 Dialogue，libass 碰撞检测会把
                # 后一行顶高几像素，跟取消预设后的一条 Dialogue + \N 排版对不上；
                # 两种模式共用同一份归一化文件，保证「选预设 ↔ 取消预设」像素级一致。
                # shape_only=True：只取字形轮廓，颜色/透明度由 Qt 上色（防透明度变粉）。
                mask_ass = self._build_inherited_ass(shape_only=True) or ass_path
            else:
                # 解析失败时退回占位符
                kwargs = dict(use_ass=True, ass_name=ass_name)
        else:
            # 手动模式：按 UI 当前参数画
            # 🔴 原来在这里抄了第二份 font_name_map（与 _load_fonts 里那份重复），
            #    现在统一走模块级 `_qt_family()`，改一处即全局生效。
            font_qt = _qt_family(self.font_combo.currentText())
            kwargs = dict(
                text=self.text_edit.toPlainText(),
                fontsize=self.fontsize_slider.value(),
                color=QColor(self._color),
                opacity=self.opacity_slider.value() / 100.0,
                position=self._effective_position(),
                alignment=self.alignment_combo.currentText(),
                outline=self.outline_check.isChecked(),
                use_ass=False,
                ass_name="",
                font_family=font_qt,
            )
            # 取消预设后的「继承模式」：继续用预设的 MarginV/L/R + libass 行距渲染，
            # 保证「选预设 → 取消预设」预览效果无缝衔接（否则手动模型 margin=96、
            # 字盒行距 vs 预设 MarginV=8、OS/2 行距，视觉差异巨大）。
            inh = getattr(self, "_inherited_layout", None)
            if inh:
                kwargs["margin_v"] = inh["margin_v"]
                kwargs["margin_l"] = inh["margin_l"]
                kwargs["margin_r"] = inh["margin_r"]
                if inh.get("line_height"):
                    kwargs["line_height"] = inh["line_height"]
                # 关键：取消预设后，视频预览也交给 libass 渲染。
                # 之前这里退回 QPainter，而选预设时用的是 libass 实烧 mask，
                # 两套渲染的字宽/描边/字重都不一样（QPainter 字宽大 ~25%），
                # 用户看到的就是「预设和取消预设效果明显不一样」。
                # 现在直接复用基于原预设生成的临时 .ass，参数（字号/行距/
                # MarginV/描边/字体）全部来自字幕文件本身 → 两条路径完全一致。
                # shape_only=True：只取字形轮廓，颜色/透明度由 Qt 上色。
                mask_ass = self._build_inherited_ass(shape_only=True)

        self.preview.update_all(**kwargs)
        # 同步到视频覆盖层（播放器存在时才更新）
        if hasattr(self, "video_preview") and self.video_preview is not None:
            self.video_preview.set_subtitle(**kwargs)
            si = getattr(self.video_preview, "subtitle_item", None)
            if si is not None:
                # 拖动定位只在「位置 = 自定义（可拖拽）」时开放。
                # 其它三档的位置由预设/枚举决定，能拖反而会产生"改了又弹回"的错觉；
                # 另外没有继承布局（从没选过预设）时也没有可写的 margin，同样不给拖。
                can_drag = (self._is_custom_position()
                            and bool(getattr(self, "_inherited_layout", None)))
                si._drag_enabled = can_drag
                si.setCursor(Qt.OpenHandCursor if can_drag else Qt.ArrowCursor)
            if mask_ass and si is not None:
                # 已是「预设 / 继承预设」→ 挂 libass 实烧 mask。
                # 继承模式（用户正在改文本）用防抖，避免每敲一个字烧一次。
                self._ensure_ass_mask(mask_ass, si, debounce=(not use_ass))
            elif si is not None:
                self._pending_burn = None
                self._burn_timer.stop()
                si.set_burned_mask(None)

    # ---------------- 视频预览的 libass mask 调度 ----------------

    def _opacity_value(self):
        """当前 UI 的字体透明度（0.0~1.0），给预览着色用。"""
        try:
            return max(0.0, min(1.0, self.opacity_slider.value() / 100.0))
        except Exception:
            return 1.0

    def _ensure_ass_mask(self, ass_path, si, debounce=False):
        """确保 SubtitleItem 显示 ass_path 用 libass 烧出来的 mask。

        缓存命中/已挂同一来源 → 立即用；否则后台线程烧（1-4 秒，不能阻塞 UI）。
        debounce=True 时先挂起，等用户停止输入 350ms 再真正烧（继承模式下边打字
        边刷新会一秒钟烧好几次 ffmpeg）。
        """
        if si is None or not ass_path:
            return
        w, h = si._video_w, si._video_h
        key = (ass_path, w, h)
        if not debounce:
            # 立即渲染的请求（选预设）优先级最高：撤掉挂起的防抖请求，
            # 否则它稍后触发会顶掉这次结果（seq 递增会让本次烧录作废）。
            self._pending_burn = None
            self._burn_timer.stop()
        if key in self._ass_mask_cache:                 # 缓存命中
            si.set_burned_mask(self._ass_mask_cache[key], self._color, self._opacity_value(), self.outline_check.isChecked())
            si._mask_source = key
            return
        if si.burned_mask is not None and getattr(si, "_mask_source", None) == key:
            # 形状没变，但颜色/透明度可能改了 → 只重新着色，不重烧
            si.set_burned_mask(si.burned_mask, self._color, self._opacity_value(), self.outline_check.isChecked())
            return
        if debounce:
            self._pending_burn = (ass_path, w, h)
            self._burn_timer.start()
            return
        self._start_ass_burn(key)

    def _flush_pending_burn(self):
        """防抖计时结束：真正启动后台烧录。"""
        pending = self._pending_burn
        self._pending_burn = None
        if not pending:
            return
        ass_path, w, h = pending
        vp = getattr(self, "video_preview", None)
        si = getattr(vp, "subtitle_item", None) if vp is not None else None
        if si is None or (si._video_w, si._video_h) != (w, h):
            return                                      # 视频换了，这次请求作废
        key = (ass_path, w, h)
        if key in self._ass_mask_cache:
            si.set_burned_mask(self._ass_mask_cache[key], self._color, self._opacity_value(), self.outline_check.isChecked())
            si._mask_source = key
            return
        self._start_ass_burn(key)

    def _start_ass_burn(self, key):
        """后台启动一次 .ass → mask 烧录（只认最新一次结果）。"""
        ass_path, w, h = key
        self._burn_seq += 1
        seq = self._burn_seq
        self._pending_ass_key = key
        self._ass_burn_thread = threading.Thread(
            target=_burn_ass_async,
            args=(self._ass_burn_bridge, seq, ass_path, w, h),
            daemon=True,
        )
        self._ass_burn_thread.start()

    def _on_ass_burn_done(self, seq, mask):
        """后台烧录完成回到主线程：只认最新一次请求，设置/缓存 mask。"""
        if seq != self._burn_seq:
            return  # 过期结果（用户又切了别的预设/模式），丢弃
        key = self._pending_ass_key
        si = None
        if self.video_preview is not None:
            si = getattr(self.video_preview, "subtitle_item", None)
        if si is None:
            return
        if mask is not None:
            self._ass_mask_cache[key] = mask
            # 控制缓存上限（>20 个时删最早一个；每张 1080x1920 mask ≈ 8.3MB 内存，
            # 20 张 ≈ 166MB，纯内存开销，关程序即释放，不影响磁盘/程序体积）
            if len(self._ass_mask_cache) > 20:
                self._ass_mask_cache.pop(next(iter(self._ass_mask_cache)))
            si.set_burned_mask(mask, self._color, self._opacity_value(),
                               self.outline_check.isChecked())
            si._mask_source = key
        else:
            # 烧录失败：清掉旧 mask（防残留上一个预设画面），回到 QPainter 占位
            si.set_burned_mask(None)
            if key in self._ass_mask_cache:
                del self._ass_mask_cache[key]

    # ---------------- 执行 ----------------

    def _get_settings(self):
        """把当前 UI 上的设置收集成 settings dict"""
        use_ass = self.preset_combo.currentIndex() > 0
        ass_name = self.preset_combo.currentText()
        ass_path = os.path.join(PRESET_DIR, ass_name) if (use_ass and ass_name) else None
        # 给「编码参考」记录用的**人类可读预设名**。
        # ass_path 后面会被换成归一化临时文件（inherited_xxx.ass），
        # 所以必须在这里先把选中预设的名字单独存一份，否则自动记录会
        # 写出一串哈希文件名（截图第 7 行的 bug）。
        preset_name = ass_name.strip() if use_ass else ""
        # 「取消预设后的继承模式」：UI 参数来自预设（可能只小改了文本/字号等）。
        # 成片继续用 libass 烧（基于原预设生成临时 .ass），跟预设成片同一渲染模型，
        # 否则 drawtext 手动模型（margin=96、字盒行距）烧出来跟预设效果差很多。
        inherited_ass = None
        if not use_ass and getattr(self, "_inherited_layout", None):
            inherited_ass = self._build_inherited_ass()
        elif use_ass and ass_path:
            # 统一管线：选预设也用归一化临时 .ass（Style 原样、Dialogue 合并），
            # 成片跟预览、跟取消预设后完全同一条渲染路径。
            inherited_ass = self._build_inherited_ass() or ass_path
        aspect_text = self.aspect_combo.currentText()
        # 当前选中的字体文件路径（用于 drawtext）
        current_font_name = self.font_combo.currentText()
        font_file = self._font_paths.get(current_font_name)
        # 从主窗口读编码设置（preset/crf + 同时处理数）
        encode = getattr(self, "encode_settings", {}) or {}
        if inherited_ass:
            use_ass = True
            ass_path = inherited_ass
            # 走到这里说明 ass_path 已经是临时归一化文件了。若原本没选预设
            # （纯「取消预设后的继承模式」），preset_name 为空 → 记「继承预设」。
            if not preset_name:
                preset_name = "继承预设 (来自 {})".format(os.path.basename(
                    getattr(self, "_inherited_ass_source", "") or "预设")) \
                    if getattr(self, "_inherited_ass_source", None) else "继承预设"
        try:
            workers = max(1, int(encode.get("max_workers", "1")))
        except (TypeError, ValueError):
            workers = 1
        return {
            "text": self.text_edit.toPlainText(),
            "fontsize": self.fontsize_slider.value(),
            "color_hex": self._color.name(),
            "opacity": self.opacity_slider.value(),
            "position": self._effective_position(),
            "alignment": self.alignment_combo.currentText(),
            "outline": self.outline_check.isChecked(),
            "aspect": aspect_text,
            "use_ass": use_ass,
            "ass_path": ass_path,
            "preset_name": preset_name,   # 人类可读预设名（编码参考记录用）
            "font_file": font_file,
            "preset": encode.get("preset") or "veryfast",
            "crf": encode.get("crf") or "23",
            "max_workers": workers,
        }

    def burn_all(self):
        if not self.videos:
            QMessageBox.warning(self, "提示", "请先选择视频（文件或文件夹）")
            return

        settings = self._get_settings()
        if not settings["use_ass"] and not settings["text"].strip():
            QMessageBox.warning(self, "提示", "请输入字幕文本，或选择预设字幕")
            return
        self._last_burn_settings = settings   # 记录本批设置（编码参考要写预设名）

        os.makedirs(self.output_dir, exist_ok=True)
        total = len(self.videos)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在烧字幕 0/{} ...".format(total))
        self.progress_bar.show()
        self.progress_label.show()

        self._set_busy(True)
        self._pause_tracker.start()          # 暂停时长不计入耗时
        self._burn_start = time.monotonic()
        self._output_folder = self.output_dir

        # 释放预览播放器资源，把性能让给批量编码
        self.video_preview.release_for_batch()

        self.thread = QThread(self)
        self.worker = SubtitleWorker(self.videos, self.output_dir, settings)
        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self._on_burn_progress)
        self.worker.finished.connect(self._on_burn_finished)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

        # 烧字幕期间显示 暂停 / 终止 按钮
        self.burn_pause_btn.show()
        self.burn_stop_btn.show()
        self.burn_pause_btn.setEnabled(True)
        self.burn_stop_btn.setEnabled(True)
        self._burn_paused = False
        self._burn_cancelled = False
        self.burn_pause_btn.setText("暂停")

        # 把左侧栏滚到底：屏幕小的用户看不到进度条，会以为软件死机了
        self._scroll_left_to_bottom()

    def _scroll_left_to_bottom(self):
        """把左侧操作栏滚到底部，让「进度条 / 暂停 / 终止」可见。

        小屏幕（或窗口拉得矮）时这些控件在左栏最底部、默认看不见，
        批处理一开始用户会以为程序卡死了。延迟两次滚动是因为 show() 之后
        布局还没更新，滚动条最大值要等控件排好才准。
        """
        def _to_bottom():
            try:
                sb = self.left_scroll.verticalScrollBar()
                sb.setValue(sb.maximum())
            except Exception:
                pass
        QTimer.singleShot(0, _to_bottom)
        QTimer.singleShot(80, _to_bottom)

    def _on_burn_progress(self, current, total, filename, success, message):
        self.progress_bar.setValue(current)
        name = os.path.basename(filename)
        state = "成功" if success else "失败"
        self.progress_label.setText("正在烧字幕 {}/{}：{} {}".format(current, total, name, state))
        self.status.setText("烧字幕中 {}/{}：{} {}".format(current, total, name, state))

    def _on_burn_pause(self):
        """暂停/继续：挂起或恢复当前正在跑的 ffmpeg 进程。"""
        if not hasattr(self, "worker") or self.worker is None:
            return
        if not self._burn_paused:
            self.worker.pause()
            self._burn_paused = True
            self._pause_tracker.pause()      # 暂停时长不计入耗时
            self.burn_pause_btn.setText("继续")
            self.status.setText("已暂停（当前视频停在原地）")
        else:
            self.worker.resume()
            self._burn_paused = False
            self._pause_tracker.resume()
            self.burn_pause_btn.setText("暂停")
            self.status.setText("继续烧字幕")

    def _on_burn_stop(self):
        """终止：杀掉当前 ffmpeg 并停止后续文件。"""
        if not hasattr(self, "worker") or self.worker is None:
            return
        self._burn_cancelled = True
        self.worker.cancel()
        self.burn_pause_btn.setEnabled(False)
        self.burn_stop_btn.setEnabled(False)
        self.status.setText("正在终止…")

    def _on_burn_finished(self, success_count, fail_list):
        elapsed = self._pause_tracker.elapsed()   # 不含暂停时长
        elapsed_str = _format_duration(elapsed)
        output_folder = self._output_folder

        self.thread.quit()
        self.thread.wait()
        self._set_busy(False)
        self.progress_bar.hide()
        self.progress_label.hide()
        self.burn_pause_btn.hide()
        self.burn_stop_btn.hide()

        # 恢复预览播放（从暂停处继续）
        self.video_preview.resume_after_batch()

        if success_count == -1:
            if getattr(self, "_burn_cancelled", False):
                self._burn_cancelled = False
                QMessageBox.information(
                    self, "已终止",
                    "批量烧字幕已终止。\n已完成的视频保留在：{}".format(output_folder))
                self.status.setText("烧字幕已终止")
            elif fail_list and fail_list[0][0] == "内部错误":
                QMessageBox.critical(self, "错误", fail_list[0][1])
                self.status.setText("烧字幕出错")
            return

        # 源文件 = 本批视频；输出 = 本批成品（worker 带回的路径）
        src_mb = total_size_mb(self.videos)
        out_mb = total_size_mb(getattr(self.worker, "outputs", []))
        burn_settings = self._last_burn_settings or {}
        # ⚠️ 注意：选了预设时 `ass_path` 是**归一化临时文件**
        # （%TEMP%\video_tool_ass\inherited_xxx.ass，见 _get_settings / _build_inherited_ass
        # 的「统一管线」），直接取 basename 会记成「inherited_523714c」——
        # 用户完全认不出是哪个预设（截图第 7 行的 bug）。这里一律记**预设名**
        # （preset_name 由 _get_settings 带出；取消预设的继承模式记「继承预设」）。
        sub_name = (burn_settings.get("preset_name") or "").strip()
        if not sub_name:
            if burn_settings.get("use_ass"):
                sub_name = "预设"
            else:
                sub_name = "手动输入"

        self._last_batch = {
            "date": time.strftime("%Y-%m-%d"),
            "files": success_count,
            "src_mb": "{:.1f}".format(src_mb),
            "preset": (self.encode_settings or {}).get("preset", ""),
            "crf": (self.encode_settings or {}).get("crf", ""),
            "workers": str((self.encode_settings or {}).get("max_workers", "1")),
            # 优先级：存中文（与参考表的下拉列同口径）。
            # ⚠️ 这里翻译一次；MainWindow.record_encode_ref_batch 落盘时还会
            #    再归一一次（防御性），两层都认英文 key 和中文值。
            "priority": {"low": "低", "normal": "普通",
                         "high": "高"}.get(
                             str((self.encode_settings or {}).get(
                                 "priority", "normal")), "普通"),
            "subtitle": sub_name,
            # 参考表里用紧凑格式（XX分XX秒，无空格无小数）；
            # 弹窗/状态栏仍用 elapsed_str（「4 分 32.15 秒」，更好读）
            "elapsed": format_elapsed_compact(elapsed),
            "out_mb": "{:.1f}".format(out_mb),
            "note": ("自动记录" if not fail_list
                     else "自动记录（{} 个失败）".format(len(fail_list))),
        }
        if self.notify_batch is not None:
            self.notify_batch(self._last_batch)

        show_batch_done(
            self,
            "完成" if not fail_list else "完成（部分失败）",
            success_count, len(fail_list), elapsed_str, output_folder,
            src_mb=src_mb, out_mb=out_mb,
            fail_list=fail_list or None,
            on_record=(self._record_last_batch if self.record_batch else None),
        )
        self.status.setText(
            "烧字幕结束：{} 成功，{} 失败（耗时 {}）".format(
                success_count, len(fail_list), elapsed_str
            )
        )

    def _record_last_batch(self):
        """把本批数据写进「编码参考」，返回记录总数（供弹窗显示）。"""
        if not self._last_batch or self.record_batch is None:
            return None
        return self.record_batch(self._last_batch)

    def _set_busy(self, busy):
        for btn in (self.files_btn, self.folder_btn, self.refresh_btn,
                    self.burn_btn, self.out_dir_btn,
                    self.cancel_preset_btn, self.open_preset_dir_btn,
                    self.clear_list_btn, self.remove_checked_btn):
            btn.setEnabled(not busy)
        # 文件列表在批量合成时禁用（避免编码中途点击切换视频）
        self.file_list.setEnabled(not busy)
        if busy:
            # 烧录期间：所有设置控件都禁用（避免中途修改）
            for w in (self.preset_combo, self.text_edit, self.fontsize_slider,
                      self.position_combo, self.alignment_combo, self.font_combo,
                      self.color_combo, self.opacity_slider, self.outline_check,
                      self.outline_width_slider):
                w.setEnabled(False)
        else:
            # 结束后：预设下拉框必须恢复可用，手动字幕控件状态由 _apply_preset_state
            # 统一决定（选预设时禁用手动、未选时启用）
            self.preset_combo.setEnabled(True)
            self._apply_preset_state()
