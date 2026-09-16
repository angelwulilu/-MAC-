# -*- coding: utf-8 -*-
"""
utils.py —— 跨模块共享的工具函数
"""

import os
import re
import subprocess
import sys
import time

from PySide6.QtCore import QObject, QEvent, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea


class PauseTracker:
    """统计「扣掉暂停时间」的实际耗时。

    用户关心的是「这批要跑多久」，所以暂停期间的时间不算进去。
    用法：
        t = PauseTracker(); t.start()
        ...
        t.pause() / t.resume()
        t.elapsed()   # 已跑时间（不含暂停）
    """

    def __init__(self):
        self._start = None
        self._paused = False
        self._pause_begin = 0.0
        self._paused_total = 0.0

    def start(self):
        self._start = time.monotonic()
        self._paused = False
        self._pause_begin = 0.0
        self._paused_total = 0.0

    def pause(self):
        if self._start is not None and not self._paused:
            self._paused = True
            self._pause_begin = time.monotonic()

    def resume(self):
        if self._paused:
            self._paused_total += time.monotonic() - self._pause_begin
            self._paused = False

    def elapsed(self):
        """已用时间（秒），不含暂停时长；正在暂停中也算得对。"""
        if self._start is None:
            return 0.0
        now = time.monotonic()
        total = now - self._start - self._paused_total
        if self._paused:
            total -= now - self._pause_begin
        return max(0.0, total)


def total_size_mb(file_paths):
    """一组文件的合计大小（MB）。读不到的文件跳过。"""
    total = 0
    for p in file_paths or []:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total / 1048576.0


def open_in_explorer(folder):
    """用系统文件管理器打开文件夹（失败返回 False）。

    - Windows：os.startfile（等于双击）
    - macOS：open（Finder）
    - Linux：xdg-open

    macOS 上必须走 subprocess，因为 os.startfile 是 Windows 独有的。
    ``open`` 是 macOS 自带的命令行工具，等价于在 Finder 里双击。
    """
    if not folder or not os.path.isdir(folder):
        return False
    try:
        if os.name == "nt":
            os.startfile(folder)                     # noqa: S606  Windows
        elif sys.platform == "darwin":
            # ⚠️ 不加 close_fds=False 之类的花活，保持最简单；
            #    stdout/stderr 丢 DEVNULL，避免万一 open 报错时污染运行日志。
            subprocess.Popen(["open", folder],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", folder],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# 常见视频文件的后缀
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".webm", ".ts"}


def is_video(path):
    """判断一个文件是不是视频（看后缀名）"""
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def list_videos(folder):
    """列出文件夹里的所有视频文件"""
    result = []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if os.path.isfile(full) and is_video(full):
            result.append(full)
    return result


def get_desktop_path():
    """获取桌面路径（Windows / macOS 通用，兼容 OneDrive 重定向、中文系统）

    - Windows: ~/Desktop、~/OneDrive/Desktop、~/桌面
    - macOS:   ~/Desktop（中文系统也仍是 Desktop，但中文目录名一并兜底）
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
        os.path.join(home, "桌面"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return os.path.join(home, "Desktop")


def default_output_dir():
    """视频拼接成品默认保存到桌面「合成视频」文件夹"""
    return os.path.join(get_desktop_path(), "合成视频")


def default_subtitle_output_dir():
    """加字幕后成品默认保存到桌面「合成视频-字幕」文件夹"""
    return os.path.join(get_desktop_path(), "合成视频-字幕")


def _natural_key(path):
    """自然排序键：让 '2.mp4' 排在 '10.mp4' 前面，而不是按字符串排"""
    name = os.path.basename(path)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _safe_prefix(text):
    """把开头里 Windows 不允许的文件名字符替换成下划线"""
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, "_")
    return text


def _avoid_collision(path):
    """若同名文件已存在，自动在扩展名前加 (1)(2)…，避免覆盖旧文件"""
    if not os.path.exists(path):
        return path
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    n = 1
    while True:
        candidate = os.path.join(folder, "{}({}){}".format(stem, n, ext))
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _format_duration(seconds):
    """把秒数格式化成「X 分 Y.XX 秒」这样的中文可读形式（保留 2 位小数）"""
    if seconds < 60:
        return "{:.2f} 秒".format(seconds)
    m = int(seconds // 60)
    s = seconds - m * 60
    if m < 60:
        return "{} 分 {:.2f} 秒".format(m, s)
    h = m // 60
    m = m - h * 60
    return "{} 小时 {} 分 {:.2f} 秒".format(h, m, s)


def _format_total_duration(seconds):
    """把「总时长」格式化成紧凑补零形式：XX小时XX分XX.XX秒（如 02小时08分10.28秒）"""
    total = max(0, int(round(float(seconds) * 100)))  # 精确到 0.01 秒
    cs = total % 100
    total_sec = total // 100
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return "{:02d}小时{:02d}分{:02d}.{:02d}秒".format(h, m, s, cs)


def format_elapsed_compact(seconds):
    """编码参考表用的紧凑耗时：XX分XX秒 / XX秒（取整，中间无空格）。

    和 _format_duration 的区别：
      · 不带小数（参考表里两位小数没意义，还占宽度）；
      · 不带空格（「4 分 32 秒」占宽更多，且看起来松散）；
      · 超过 1 小时也压成「XX分XX秒」形式（如 95分10秒），不引入「小时」，
        保证列宽口径统一、各条记录长度接近。
    秒数四舍五入；不足 1 分只写「XX秒」。
    """
    total = max(0, int(round(float(seconds))))
    m, s = divmod(total, 60)
    if m:
        return "{}分{}秒".format(m, s)
    return "{}秒".format(s)


def _format_file_size(num_bytes):
    """把字节数格式化成可读形式（B/KB/MB/GB）"""
    size = float(num_bytes)
    if size < 1024:
        return "{:.0f} B".format(size)
    size /= 1024
    if size < 1024:
        return "{:.2f} KB".format(size)
    size /= 1024
    if size < 1024:
        return "{:.2f} MB".format(size)
    size /= 1024
    return "{:.2f} GB".format(size)


# ---------------- 滚轮防误改（下拉框 / 数字框） ----------------

def _forward_wheel_to_scroll_parent(widget, event):
    """把滚轮事件转交给最近的父级滚动区（QAbstractScrollArea 的 viewport）。

    为什么必须手动转发：Qt 的 `event.ignore()` **不会**让滚轮事件自动
    冒泡到 QScrollArea —— 实测「滚在下拉框上时页面纹丝不动」（用户会
    觉得页面卡住）。原生 QScrollArea 只在滚轮打到它自己的 viewport 时
    才滚动，而事件一旦被投递到子控件（combo）就到此为止了。
    所以这里主动把事件重新投递给可见的祖先滚动区。

    返回 True 表示已找到并转交。
    """
    from PySide6.QtWidgets import QAbstractScrollArea

    p = widget.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            vp = p.viewport()
            if vp is not None and vp.isVisible():
                # 坐标换算到目标 viewport（只用于事件的 pos 字段）
                from PySide6.QtGui import QWheelEvent

                gpos = event.globalPosition()
                lpos = vp.mapFromGlobal(gpos.toPoint())
                ev2 = QWheelEvent(
                    QPointF(lpos), gpos,
                    event.pixelDelta(), event.angleDelta(),
                    event.buttons(), event.modifiers(),
                    event.phase(), event.inverted())
                QApplication.sendEvent(vp, ev2)
                return True
        p = p.parentWidget()
    return False


class NoWheelMixin:
    """给 QComboBox / QSpinBox 等「滚轮会改值」的控件彻底禁用滚轮改值。

    用户踩过的坑（复现过两次）：鼠标在页面上滚轮翻页时，光标恰好划过某个
    下拉框，参数就被静默改掉了（下拉框改值不弹确认，跑完一批才发现设置不对）。
    录屏证据：鼠标停在「字体格式」上滚轮，值从「居中」变成「居右」。

    🔴 **绝对不要用 `hasFocus()` 当放行条件**（第一版就是这么做，被用户打回）：
       QComboBox 的 focusPolicy 是 **StrongFocus(15)** —— 用户点过一次、
       甚至 Tab 切换经过，焦点就**留在**它身上。之后滚轮划过照样改值，
       表现就是「根本没禁掉」。实测：`hasFocus()==True` 时滚轮 → 值确实变了。

    正确行为：**任何情况下滚轮都不改值**，只把滚轮让给父级滚动区。
    想改值请用鼠标点击下拉 / 拖拽滑条（明确的意图表达）。

    用法（继承顺序：mixin 必须写在 Qt 类**前面**）：
        class NoWheelCombo(NoWheelMixin, QComboBox): pass
        class NoWheelSpin(NoWheelMixin, QSpinBox): pass
        class NoWheelSlider(NoWheelMixin, QSlider): pass
    """

    def wheelEvent(self, event):
        # 无条件拒绝滚轮改值；把滚动意图转交给父级滚动区，页面照常滚动
        _forward_wheel_to_scroll_parent(self, event)
        event.accept()


class _NoWheelFilter(QObject):
    """事件过滤器版（用于已经 new 好的实例，见 disable_wheel_on）。

    同样**不看焦点**：无条件拦下滚轮（理由见 NoWheelMixin 的说明）。
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            _forward_wheel_to_scroll_parent(obj, event)
            event.accept()
            return True
        return False


def disable_wheel_on(widget):
    """给已创建的控件（含 Qt Designer 出来的）禁用滚轮改值。

    用事件过滤器实现，不依赖类继承 —— 适合已经 new 好的实例
    （比如对话框里临时创建的 QComboBox / QSpinBox）。
    对任意 QWidget 调用都安全；重复调用不会重复安装。
    """
    if getattr(widget, "_no_wheel_filter", None) is not None:
        return widget                      # 已装过，幂等

    f = _NoWheelFilter(widget)
    widget.installEventFilter(f)
    widget._no_wheel_filter = f            # 防 GC（父对象挂了 filter 也会跟着挂）
    return widget


def disable_wheel_recursive(root):
    """把一棵控件树里所有「滚轮会改值」的控件都设成禁止滚轮改值。

    覆盖 QComboBox / QAbstractSpinBox（QSpinBox、QDoubleSpinBox、QDateTimeEdit…）。
    在对话框构造完、show 之前调一次即可（新建的子控件也要再调）。

    ⚠️ PySide6 的 `findChildren` **不接受 tuple**（只接受单个类型或正则），
       必须分两次调用（试过传 tuple → TypeError: wrong argument types）。
    """
    from PySide6.QtWidgets import QComboBox, QAbstractSpinBox

    targets = []
    if isinstance(root, (QComboBox, QAbstractSpinBox)):
        targets.append(root)
    targets.extend(root.findChildren(QComboBox))
    targets.extend(root.findChildren(QAbstractSpinBox))
    seen, uniq = set(), []
    for t in targets:
        if id(t) not in seen:
            seen.add(id(t))
            uniq.append(t)
    for t in uniq:
        disable_wheel_on(t)
    return len(uniq)
