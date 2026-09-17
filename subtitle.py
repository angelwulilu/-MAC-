# -*- coding: utf-8 -*-
"""
subtitle.py —— 批量给视频添加固定字幕（广审等）

支持的字幕源：
  1. 手动输入文本：使用 ffmpeg drawtext 滤镜烧入
  2. 预设 .ass 文件：放在 subtitle_presets 文件夹里自动读取，使用 ass 滤镜烧入
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal

import paths
import procctl

# ffmpeg 的完整路径（自动查找，打包成 exe 后指向自带的 ffmpeg）
FFMPEG = paths.find_ffmpeg()

# 默认中文字体：**按平台真实存在的那个**（不要硬编码 msyh，
# 那是 Windows 路径，macOS 上不存在）。
# 这里保留 FONT_FILE 这个名字，因为外部（subtitle_tab 等）已经在用它。
_DEFAULT_FONT_NAME, FONT_FILE = paths.default_font()


def _font_options():
    """字体名 → 字体文件路径（用于 drawtext 滤镜）。

    🔴 原来这里写死了 7 个 `C:/Windows/Fonts/...` 路径 —— 在 macOS 上
    `available_fonts()` 会返回空列表，字体下拉框空白、度量全错。
    现在路径表统一放 `paths.font_candidates()`（按平台给候选），
    这里只做「过滤掉不存在的」。Windows 上的结果与原来完全一致。
    """
    return paths.font_candidates()


# 兼容旧名字（原代码里叫 FONT_OPTIONS，指的是同一份候选表）
FONT_OPTIONS = _font_options()


def available_fonts():
    """返回当前系统上真实存在的中文字体（自动剔除缺失项）"""
    return paths.resolve_fonts()

# 字幕预设文件夹（用户可往里放 .ass 文件，程序会自动读取）
# 打包成 exe 后指向「exe 同级的 subtitle_presets」，仍可自由增删
PRESET_DIR = paths.preset_dir()

# 视频比例 → 实际分辨率（用于 burn-in 时计算字幕位置）
ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1":  (1080, 1080),
    "4:3":  (1440, 1080),
}


def _install_builtin_font():
    """把内置中文字体复制到用户字体目录，让 libass / CoreText 能命中。

    这是为 .ass 预设字幕准备的：libass 在 macOS 上走 CoreText，只认
    「安装到系统字体目录」的字体；光把字体打进 .app 内部还不够。
    drawtext 路直接传 fontfile，不依赖字体安装。

    best-effort：找不到内置字体或没权限时静默跳过，绝不阻塞烧录。
    """
    try:
        name, src = paths.builtin_font()
        if not src or not os.path.isfile(src):
            return
        if sys.platform == "darwin":
            dest_dir = os.path.expanduser("~/Library/Fonts")
        elif os.name == "nt":
            dest_dir = os.path.join(
                os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
                "Microsoft", "Windows", "Fonts")
        else:
            return
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(src))
        if not os.path.isfile(dest):
            shutil.copy2(src, dest)
    except Exception:
        pass


# ---------------- drawtext 滤镜构造 ----------------

def _escape_drawtext_text(text):
    """转义 drawtext 文本里的特殊字符（顺序很重要：先转反斜杠）

    注意：换行必须保留「真实换行符」，不能写成 \\n。
    ffmpeg drawtext 只认真实换行；写成 \\n 会被滤镜解转义成字母 n，
    结果整段多行字幕被挤成一长行（预览多行、成片一行 —— 就是位置对不上的根因）。
    """
    text = text.replace("\\", "\\\\\\\\")  # 反斜杠
    text = text.replace(":", "\\:")        # 冒号（滤镜参数分隔符）
    text = text.replace("'", "\\'")        # 单引号
    text = text.replace("%", "\\%")        # 百分号（drawtext 文本扩展）
    text = text.replace(",", "\\,")        # 逗号（滤镜参数分隔符）
    text = text.replace(";", "\\;")        # 分号（滤镜链分隔符）
    # 换行保持原样（真实 \n），drawtext 会自动按多行排版
    return text


def _color_to_ffmpeg(hex_color, opacity_percent):
    """把 #RRGGBB + 不透明度(0-100) 转成 ffmpeg 0xAARRGGBB 格式"""
    h = hex_color.lstrip("#")
    alpha = int(round(opacity_percent / 100.0 * 255))
    return "0x{:02X}{}".format(alpha, h.upper())


def build_drawtext_filter(text, fontsize, color_hex, opacity, position, alignment, outline, frame_w, frame_h, font_file=None):
    """根据用户设置构造 drawtext 滤镜字符串。

    手动字幕：拆成「每行一个 drawtext 滤镜」叠加，每行 y 单独写死数值。
    行距 = 字盒（ascent + descent ≈ 1.32×fs） + 固定视觉间隙（≥ 18%×fs）。

    不用 freetype 测 msyh 中文字符 ink 高度：freetype 对 msyh.ttc 中文返回 bm.rows=0
    （不生成 bitmap），导致 ink=0、两行 y 重叠。改用字盒 + 固定间隙，不依赖 freetype
    也保证行距 ≥ 字盒 + gap、绝不会重叠。
    """
    color = _color_to_ffmpeg(color_hex, opacity)
    if not font_file:
        font_file = FONT_FILE

    # 边距（与 subtitle_tab.py 同公式）
    # **最后一行 baseline 锚定**在 frame_h - 100（离视频底 100px 处），
    # 字号变化时整块文字**只在视觉 descender 区小幅漂移**（15px 以内），
    # 不会再"整块飘到中部"（之前 margin 联动版 60+px 漂移）。
    margin_x = max(20, frame_w // 30)
    margin_y = max(40, frame_h // 20)

    # 字盒 = ascent + descent（不含 leading）。lineGap 在 msyh 是 0.22×fs，
    # 但 msyh fs=100 时字盒底=asc+desc+leading=154, 含 22px descender 空白；
    # 墨迹底=字盒底-22=1781，跟 ffmpeg drawtext 实测 1789 接近。
    # 用 1.32×fs 而非 1.54 是为了"墨迹底漂移最小"——fs=100 vs fs=34 墨迹底
    # 漂移 ~30px (1.5% 视频高，肉眼基本看不出)。
    box_h = int(round(fontsize * 1.32))
    # 行间视觉间隙：至少 8px 或 18% 字号
    line_gap_px = max(8, int(fontsize * 0.18))

    # 每行 advance 宽度估算（不用 freetype 测中文宽度，用经验公式）
    def _line_width(line):
        w = 0
        for ch in line:
            if '\u4e00' <= ch <= '\u9fff':  # CJK 统一汉字
                w += fontsize
            elif ch.isascii() and ch.isalnum():
                w += fontsize * 0.55
            elif ch in ' ,.!?:;':
                w += fontsize * 0.3
            else:
                w += fontsize * 0.55
        return w

    lines = text.split("\n") if text else [""]
    if not lines:
        lines = [""]

    n = len(lines)
    widths = [_line_width(ln) for ln in lines]
    max_w = max(widths) if widths else 0

    ascent_px = int(round(fontsize * 0.88))  # msyh ascent ≈ 0.88*fs
    descent_px = int(round(fontsize * 0.21))  # msyh descent ≈ 0.21*fs

    # 每行 baseline 排版（按 position 分支）
    if position == "底部":
        # 锚定最后一行 baseline = frame_h - 100（距视频底 100px），
        # 字号变化时整块文字**只在 descender 区小幅漂移**（15px 以内），
        # 不会再"整块飘到中部"。
        last_line_baseline = frame_h - 100
        baselines = [last_line_baseline] * n
        for i in range(n - 2, -1, -1):
            baselines[i] = baselines[i + 1] - (box_h + line_gap_px)
    elif position == "中部":
        # 整块垂直居中
        center_baseline = frame_h / 2.0
        half_offset = (n - 1) * (box_h + line_gap_px) / 2.0
        first_baseline = center_baseline - half_offset
        baselines = [first_baseline + i * (box_h + line_gap_px) for i in range(n)]
    else:  # 顶部
        # 第一行顶部 = margin_y
        first_baseline = margin_y + ascent_px
        baselines = [first_baseline + i * (box_h + line_gap_px) for i in range(n)]

    # 排版 x
    if alignment == "居中":
        x_x = (frame_w - max_w) / 2.0
    elif alignment == "居左":
        x_x = float(margin_x)
    else:  # "居右"
        x_x = frame_w - margin_x - max_w

    # 排版 x
    if alignment == "居中":
        x_x = (frame_w - max_w) / 2.0
    elif alignment == "居左":
        x_x = float(margin_x)
    else:  # 居右
        x_x = frame_w - margin_x - max_w

    # 每行 y（ffmpeg drawtext 的 y= 是文字顶部位置 = baseline - ascent）
    # top_y[i] = baseline[i] - ascent
    fontfile_esc = font_file.replace("\\", "/").replace(":", "\\:")
    parts = []
    for i, line in enumerate(lines):
        if not line:
            continue
        top_y = baselines[i] - ascent_px
        top_y_int = int(round(top_y))
        line_x = x_x + ((max_w - widths[i]) / 2.0) if alignment == "居中" else x_x
        line_x_int = int(round(line_x))
        sub_parts = [
            "text='{}'".format(_escape_drawtext_text(line)),
            "fontsize={}".format(fontsize),
            "fontcolor={}".format(color),
            "fontfile='{}'".format(fontfile_esc),
            "x={}".format(line_x_int),
            "y={}".format(top_y_int),
        ]
        if outline:
            sub_parts.append("borderw={}".format(max(1, fontsize // 12)))
            sub_parts.append("bordercolor=black@1.0")
        parts.append("drawtext=" + ":".join(sub_parts))

    return ",".join(parts)


def build_burn_command_manual(input_path, output_path, text, fontsize, color_hex, opacity,
                              position, alignment, outline, aspect, font_file=None,
                              preset="fast", crf="18"):
    """用手动文本烧字幕的 ffmpeg 命令"""
    frame_w, frame_h = ASPECT_RATIOS.get(aspect, ASPECT_RATIOS["9:16"])
    vf = build_drawtext_filter(text, fontsize, color_hex, opacity,
                                position, alignment, outline, frame_w, frame_h, font_file)
    return [
        FFMPEG, "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", str(crf), "-preset", str(preset),
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]


def build_burn_command_ass(input_path, output_path, ass_path, preset="fast", crf="18"):
    """用 .ass 预设字幕烧入的 ffmpeg 命令"""
    # ass 滤镜路径要转义 : 和 \
    escaped_path = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    vf = "ass='{}'".format(escaped_path)
    return [
        FFMPEG, "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", str(crf), "-preset", str(preset),
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]


# ---------------- 单文件烧录 ----------------

def _run_ffmpeg(cmd, output_path, ctrl=None):
    """通用 ffmpeg 运行：支持「暂停 / 终止」（通过 ctrl）。返回 (success, message)。

    ctrl: 可选 ProcGroup。
        - 当前子进程启动时 register、结束时 unregister（供外部挂起/恢复）
        - ctrl.cancelled 为 True 时立即杀掉当前进程

    注意：stdout/stderr 不能走 PIPE 又不读，否则 ffmpeg 写满 64KB 管道缓冲区后
    会死锁（表现成“卡住 / 输出损坏”）。这里把 stderr 重定向到临时文件，
    失败时再读出来当作报错信息。
    """
    import tempfile
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    err_fd, err_path = tempfile.mkstemp(prefix="burn_err_", suffix=".log", dir=out_dir)
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=err_fd,
                                    **paths.spawn_kwargs())
            if ctrl is not None and hasattr(ctrl, "register"):
                ctrl.register(proc)
            while proc.poll() is None:
                if ctrl is not None and ctrl.cancelled:
                    proc.kill()
                    break
                time.sleep(0.1)
            proc.communicate()
            if ctrl is not None and ctrl.cancelled:
                return False, "已取消"
            if proc.returncode != 0:
                try:
                    with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                        return False, f.read()[-3000:]
                except Exception:
                    return False, "烧字幕失败（ffmpeg 返回非 0）"
            return True, "完成"
        finally:
            if ctrl is not None and proc is not None and hasattr(ctrl, "unregister"):
                ctrl.unregister(proc)
            try:
                os.close(err_fd)
            except OSError:
                pass
            try:
                os.remove(err_path)
            except OSError:
                pass
    except Exception as e:
        return False, str(e)


def burn_subtitle_manual(input_path, output_path, text, fontsize, color_hex, opacity,
                          position, alignment, outline, aspect, font_file=None,
                          preset="fast", crf="18", ctrl=None):
    """用手动文本烧字幕，返回 (success, message)"""
    _install_builtin_font()  # best-effort：让 libass/CoreText 也能命中内置字体
    cmd = build_burn_command_manual(input_path, output_path, text, fontsize, color_hex,
                                     opacity, position, alignment, outline, aspect,
                                     font_file, preset, crf)
    return _run_ffmpeg(cmd, output_path, ctrl)


def burn_subtitle_ass(input_path, output_path, ass_path, preset="fast", crf="18", ctrl=None):
    """用 .ass 文件烧字幕，返回 (success, message)"""
    _install_builtin_font()  # .ass 路走 libass/CoreText，必须先把内置字体装进用户字体目录
    cmd = build_burn_command_ass(input_path, output_path, ass_path, preset, crf)
    return _run_ffmpeg(cmd, output_path, ctrl)


# ---------------- 批量 ----------------

def list_presets():
    """列出 subtitle_presets 文件夹里的所有 .ass 文件（仅文件名）"""
    if not os.path.isdir(PRESET_DIR):
        return []
    return sorted(f for f in os.listdir(PRESET_DIR) if f.lower().endswith(".ass"))


def get_preset_resolution(ass_path):
    """读取 .ass 的 [Script Info] PlayResX/PlayResY（预设的"设计分辨率"）。

    这是判断"预设跟当前视频比例对不对得上"的权威依据 —— 比从文件名猜准得多。
    读不到返回 (None, None)。
    """
    if not ass_path or not os.path.isfile(ass_path):
        return None, None
    px = py = None
    try:
        try:
            f = open(ass_path, "r", encoding="utf-8")
        except UnicodeDecodeError:
            f = open(ass_path, "r", encoding="gbk")
        with f:
            for line in f:
                if line.startswith("[") and line.strip().endswith("]"):
                    if line.strip().lower() != "[script info]":
                        continue
                    continue
                low = line.strip().lower()
                if low.startswith("playresx:"):
                    px = _safe_int(line.split(":", 1)[1].strip())
                elif low.startswith("playresy:"):
                    py = _safe_int(line.split(":", 1)[1].strip())
                if px and py:
                    break
    except Exception:
        return None, None
    return px, py


def _safe_int(s):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _unique_output_path(output_folder, base_name):
    """生成不覆盖已有文件的输出路径：name_加字幕.mp4 → name_加字幕(1).mp4"""
    out_path = os.path.join(output_folder, "{}_加字幕.mp4".format(base_name))
    index = 1
    while os.path.exists(out_path):
        out_path = os.path.join(output_folder, "{}_加字幕({}).mp4".format(base_name, index))
        index += 1
    return out_path


def burn_subtitle_batch(videos, output_folder, settings, progress_callback=None,
                        ctrl=None, outputs=None):
    """批量烧字幕。

    settings: dict, 包含 text, fontsize, color_hex, opacity, position, alignment, outline, aspect,
              以及 use_ass (bool), ass_path (str)
              可选 max_workers (int)：>1 时并行烧录（CPU 密集，多核机器才有提升）。
    progress_callback(current, total, filename, success, message)
    ctrl: 可选 ProcGroup，用于暂停/终止（并发时对多个 ffmpeg 同时生效）。
    outputs: 可选 list，成功的成品路径会被追加进去（供界面统计输出大小）。
    """
    os.makedirs(output_folder, exist_ok=True)
    total = len(videos)

    use_ass = settings.get("use_ass", False)
    ass_path = settings.get("ass_path")
    preset = settings.get("preset", "fast")
    crf = settings.get("crf", "18")
    try:
        max_workers = int(settings.get("max_workers", 1) or 1)
    except (TypeError, ValueError):
        max_workers = 1

    # 输出路径先串行算好：_unique_output_path 的防覆盖编号在并发里会有竞态
    jobs = []
    for video_path in videos:
        base = os.path.splitext(os.path.basename(video_path))[0]
        jobs.append((video_path, _unique_output_path(output_folder, base)))

    lock = threading.Lock()
    state = {"done": 0, "success": 0}
    fail_list = []

    def _after(video_path, out_path, ok, msg):
        with lock:
            state["done"] += 1
            if ok:
                state["success"] += 1
                if outputs is not None:
                    outputs.append(out_path)
            else:
                fail_list.append((video_path, msg))
            cur = state["done"]
        if progress_callback:
            progress_callback(cur, total, video_path, ok, msg)

    def _process_one(video_path, out_path):
        if ctrl is not None and ctrl.cancelled:
            _after(video_path, out_path, False, "已取消")
            return
        if use_ass and ass_path:
            ok, msg = burn_subtitle_ass(video_path, out_path, ass_path,
                                        preset, crf, ctrl=ctrl)
        else:
            ok, msg = burn_subtitle_manual(
                video_path, out_path,
                settings.get("text", ""),
                settings.get("fontsize", 28),
                settings.get("color_hex", "#FFFFFF"),
                settings.get("opacity", 100),
                settings.get("position", "底部"),
                settings.get("alignment", "居中"),
                settings.get("outline", True),
                settings.get("aspect", "9:16"),
                settings.get("font_file"),
                preset, crf, ctrl=ctrl,
            )
        _after(video_path, out_path, ok, msg)

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_process_one, v, o) for v, o in jobs]
            for fut in futs:
                try:
                    fut.result()
                except Exception:
                    pass   # _process_one 内部已兜底，这里只防意外
    else:
        for video_path, out_path in jobs:
            if ctrl is not None and ctrl.cancelled:
                break
            _process_one(video_path, out_path)

    return state["success"], fail_list


# ---------------- 预设 .ass 解析 ----------------

# ffmpeg 颜色 &HAABBGGRR → Qt #RRGGBB
def _ass_color_to_hex(ass_color):
    """把 .ass 的 &HAABBGGRR 转成 Qt 的 #RRGGBB（带透明度）"""
    s = ass_color.lstrip("&H").lstrip("&")
    # 长度 8 = AABBGGRR；长度 6 = BBGGRR（缺 alpha）
    if len(s) == 8:
        aa, bb, gg, rr = s[0:2], s[2:4], s[4:6], s[6:8]
    elif len(s) == 6:
        aa, bb, gg, rr = "00", s[0:2], s[2:4], s[4:6]
    else:
        return "#FFFFFF", 100
    try:
        alpha_val = int(aa, 16)   # ASS 透明度：00=不透明，FF=透明
        opacity = int(round((255 - alpha_val) / 255 * 100))
        return "#{}{}{}".format(rr.upper(), gg.upper(), bb.upper()), opacity
    except ValueError:
        return "#FFFFFF", 100


def parse_ass_for_preview(ass_path):
    """解析 .ass 文件，提取第一条字幕的文本、字体、字号、颜色、位置。
    返回 dict {text, fontsize, color_hex, opacity, position, alignment, outline, font_family}
    解析失败返回 None。
    """
    if not os.path.isfile(ass_path):
        return None
    try:
        with open(ass_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(ass_path, "r", encoding="gbk") as f:
            content = f.read()

    # 解析 Styles 段
    # Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,
    #         Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle,
    #         Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
    style_format_fields = []
    first_style = {}
    in_styles = False
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_styles = (line.lower() == "[v4+ styles]")
            continue
        if in_styles:
            if line.lower().startswith("format:"):
                style_format_fields = [f.strip() for f in line[7:].split(",")]
            elif line.lower().startswith("style:"):
                values = [v.strip() for v in line[6:].split(",")]
                # 长度不足时填充
                values += [""] * (len(style_format_fields) - len(values))
                first_style = dict(zip(style_format_fields, values))
                break

    # Fontsize 理论上整型，但也容错成 float（如 "24.0"）
    try:
        fontsize = int(float(first_style.get("Fontsize", "28") or 28))
    except (ValueError, TypeError):
        fontsize = 28
    font_name = first_style.get("Fontname", "Microsoft YaHei") or "Microsoft YaHei"
    color_hex, opacity = _ass_color_to_hex(first_style.get("PrimaryColour", "&H00FFFFFF") or "&H00FFFFFF")
    alignment = first_style.get("Alignment", "2") or "2"
    # ASS Alignment: 1=左下 2=中下 3=右下 4=左中 5=中中 6=右中 7=左上 8=中上 9=右上
    pos_map = {
        "1": ("底部", "居左"), "2": ("底部", "居中"), "3": ("底部", "居右"),
        "4": ("中部", "居左"), "5": ("中部", "居中"), "6": ("中部", "居右"),
        "7": ("顶部", "居左"), "8": ("顶部", "居中"), "9": ("顶部", "居右"),
    }
    position, alignment_h = pos_map.get(alignment, ("底部", "居中"))
    outline_w = first_style.get("Outline", "0") or "0"
    # ASS Style 里的 MarginV 是 libass 渲染时的下边距（数字越大字离底越远）。
    # 之前预览里完全忽略 MarginV，导致 .ass 预设的字幕位置预览跟实际偏差上百像素。
    try:
        margin_v = int(float(first_style.get("MarginV", "0") or 0))
    except ValueError:
        margin_v = 0
    try:
        margin_l = int(float(first_style.get("MarginL", "0") or 0))
        margin_r = int(float(first_style.get("MarginR", "0") or 0))
    except ValueError:
        margin_l = margin_r = 0
    # Outline 可能是小数（如 1.3），用 float 解析，避免 int('1.3') 抛 ValueError
    try:
        outline_width = float(outline_w)
    except (ValueError, TypeError):
        outline_width = 0.0
    # ASS 画描边要同时满足 BorderStyle=1 和 Outline>0：
    # BorderStyle=0 是"背景框"模式，就算 Outline>0 也不会画黑描边。
    # 之前只看 Outline，会把 BorderStyle=0 的预设误判成"有描边"，
    # 导致勾选框被错误打上、用户改了也没效果。
    try:
        border_style = int(float(first_style.get("BorderStyle", "1") or 1))
    except (ValueError, TypeError):
        border_style = 1
    outline = (outline_width > 0) and (border_style != 0)
    # libass 实际行距 ≈ FreeType ascent+descent（不加 leading）。
    # ffmpeg/libass 渲染多 Dialogue 时两个 baseline 间距走的是 FreeType 行内距，
    # 不含行间距（leading）。预览要在 _paint_subtitle_text 里走同一个规则才能算对总块高度。
    # 我们在 _paint_subtitle_text 里读 QRawFont 来拿这个值，所以这里只做信息备注。

    # 解析 Events 段所有 Dialogue（不只第一条）
    # libass 按所有事件的总行数排版第一条的事件，第一条 Dialogue 的 y 位置取决于
    # 整块文本高度。只取第一条会让预览严重偏下（libass 算总块，我的预览算单行）。
    in_events = False
    texts = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = (line.lower() == "[events]")
            continue
        if in_events and line.lower().startswith("dialogue:"):
            # Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
            parts = line[9:].split(",", 9)
            if parts:
                txt = parts[-1].strip()
                # 处理 \N 换行
                txt = txt.replace("\\N", "\n").replace("\\n", "\n")
                # 去掉花括号样式
                txt = re.sub(r"\{[^}]*\}", "", txt)
                if txt:
                    texts.append(txt)
    # 取前 8 条（足够常见，且限制超大文件）
    text = "\n".join(texts[:8]) or "（空字幕）"

    return {
        "text": text or "（空字幕）",
        "fontsize": fontsize,
        "color_hex": color_hex,
        "opacity": opacity,
        "position": position,
        "alignment": alignment_h,
        "outline": outline,
        "outline_width": outline_width,
        "font_family": font_name,
        "alignment_num": alignment,  # 保留原始值
        # 三个 Margin：选 .ass 预设时用，预览要尊重 libass 的 MarginV 才能跟成片对齐
        "margin_v": margin_v,
        "margin_l": margin_l,
        "margin_r": margin_r,
    }


# ---------------- 后台 Worker ----------------

class SubtitleWorker(QObject):
    """在后台线程里跑批量烧字幕，通过信号把进度传回界面。"""

    progress = Signal(int, int, str, bool, str)   # current, total, filename, success, message
    finished = Signal(int, list)                  # success_count, fail_list

    def __init__(self, videos, output_folder, settings):
        super().__init__()
        self.videos = videos
        self.output_folder = output_folder
        self.settings = settings
        self.ctrl = procctl.ProcGroup()
        self.outputs = []          # 本批成功的成品路径（界面用来统计输出大小）

    def cancel(self):
        """终止：置取消标志并立刻杀掉正在跑的 ffmpeg（并发时可能有多个）。"""
        self.ctrl.cancelled = True
        self.ctrl.kill_all()

    def pause(self):
        """暂停：挂起当前所有 ffmpeg 进程（真正停住当前帧）。"""
        self.ctrl.suspend_all()

    def resume(self):
        """继续：恢复被挂起的 ffmpeg 进程。"""
        self.ctrl.resume_all()

    def run(self):
        def callback(current, total, filename, success, message):
            self.progress.emit(current, total, filename, success, message)

        try:
            success_count, fail_list = burn_subtitle_batch(
                self.videos, self.output_folder, self.settings, callback,
                self.ctrl, self.outputs,
            )
            if self.ctrl.cancelled:
                self.finished.emit(-1, [])
            else:
                self.finished.emit(success_count, fail_list)
        except Exception as e:
            self.finished.emit(-1, [("内部错误", str(e))])
