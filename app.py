# -*- coding: utf-8 -*-
"""
app.py —— 视频批量处理工具（带图形界面）

主窗口包含两个 tab：
  1. 批量视频拼接（ConcatTab）
  2. 批量添加字幕（SubtitleTab，在 subtitle_tab.py 里）

运行方法：在命令行输入
    python app.py
"""

import os
import re
import io
import sys
import csv
import json
import threading
import time
import tempfile
# 🔴 必须模块级导入：_ff_capabilities / _selftest_burn 用了 subprocess，
#    而本文件原先只在 _selftest() 内部局部 import —— 于是那两个函数一调用就
#    NameError，又被 except Exception 吞掉，导致「滤镜缺失」永远是假警报，
#    而且 CI 的「烧中文字幕」校验会据此误判。
#    （2026-09-17 在 Windows 副本上实测定位，同源问题两边都有。）
import subprocess
import platform

import paths

# --- 关闭 QtMultimedia FFmpeg 后端的日志噪音 ---
# PySide6 6.11 的 QtMultimedia 走 FFmpeg 后端，加载/播放视频时会往控制台刷
# 大量 "Input #0 ..." / "MFT name ..." 日志（不是报错，但很碍眼）。
# 这里在创建任何 Qt 对象前把日志压掉。
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false;*.debug=false",
)

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QFileDialog, QVBoxLayout, QHBoxLayout, QHeaderView, QMessageBox,
    QProgressBar, QDialog, QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox,
    QTabWidget, QMenuBar, QComboBox, QGroupBox, QScrollArea, QMainWindow,
    QStyledItemDelegate, QCheckBox, QAbstractButton,
)
from PySide6.QtCore import QThread, Qt, QTimer, QObject, QEvent
from PySide6.QtGui import QColor

# 共享工具
from utils import (
    is_video, list_videos, get_desktop_path, default_output_dir,
    _natural_key, _safe_prefix, _avoid_collision, _format_duration,
    _format_total_duration, _format_file_size,
    PauseTracker, total_size_mb, open_in_explorer,
    NoWheelMixin, disable_wheel_on, disable_wheel_recursive,
)


# 滚轮防误改：继承 NoWheelMixin 的版本，全项目统一用它创建控件。
# ⚠️ mixin 必须写在 Qt 类**前面**（Python MRO 从左往右解析）。
class NoWheelCombo(NoWheelMixin, QComboBox):
    """下拉框：鼠标滚轮划过不会改值（没焦点时把滚轮让给页面滚动）。"""


class NoWheelSpin(NoWheelMixin, QSpinBox):
    """数字框：同上（编码参考的「总耗时 分/秒」用它）。"""

import config as app_config
from batch_dialog import show_batch_done

# 直接复用第 2 步写好的「读参数」功能（同一个文件夹里，直接 import）
from probe import (
    probe, probe_many, get_key_params, format_param, values_equal,
    CRITICAL_KEYS, WARN_KEYS, IGNORE_KEYS, IGNORE_REASONS,
)
from reencode import find_critical_diffs, reencode_head, ReencodeMatWorker
from concat import ConcatWorker
from stats import StatsBridge, CompareBridge, total_stats


def _quiet_ffmpeg_logs():
    """把 FFmpeg 库的 av_log 级别降到 ERROR，只留真正的错误。

    QT_LOGGING_RULES 只能关掉 Qt 自己的日志类别，FFmpeg 内部还有
    "[h264_mf ...] MFT name ..." 两行直接写 stderr，得用 av_log_set_level 压掉。

    ⚠️ 跨平台注意：库文件名不一样 ——
        Windows: avutil-<n>.dll      （PySide6 目录下）
        macOS:   libavutil.<n>.dylib （.app 内 Frameworks 或 PySide6 目录下）
        Linux:   libavutil.so.<n>
    所以这里三种后缀都扫一遍；扫不到就静默跳过（下面 except 兜着），
    失败只表现为「控制台日志多一点」，不影响任何功能。
    """
    try:
        import ctypes
        import glob as _glob
        import PySide6
        base = os.path.dirname(PySide6.__file__)
        candidates = []
        for pat in ("avutil-*.dll", "libavutil*.dylib", "libavutil.so*"):
            candidates += _glob.glob(os.path.join(base, pat))
        # macOS 打包后，Qt 的 ffmpeg 后端库可能在 .app 的 Frameworks 里
        if not candidates and sys.platform == "darwin":
            _fw = os.path.join(os.path.dirname(base), "Frameworks")
            for pat in ("libavutil*.dylib", "avutil*.dylib"):
                candidates += _glob.glob(os.path.join(_fw, pat))
        if candidates:
            _lib = ctypes.CDLL(candidates[0])
            _lib.av_log_set_level(16)  # AV_LOG_ERROR
    except Exception:
        pass  # 失败也不影响功能，只是日志多点


_quiet_ffmpeg_logs()

# 常见视频文件的后缀，用来判断"这个文件是不是视频"
VIDEO_FILTER = "视频文件 (*.mp4 *.mov *.mkv *.avi *.flv *.m4v *.webm *.ts)"

# 对比表显示素材数量的上限。
# None = 不限制，全部素材都会作为表格行显示（表格可以下拉滚动查看）。
MAX_SHOW = None


class RenameDialog(QDialog):
    """批量改名的输入框：固定开头（可留空）+ 数字范围"""

    def __init__(self, file_count, parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量改名")
        self.setModal(True)

        hint = QLabel("已选 {} 个文件，将按文件名顺序依次编号".format(file_count))

        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("可留空，例如：开场")

        self.start_spin = NoWheelSpin()
        self.start_spin.setRange(1, 999999)
        self.start_spin.setValue(1)

        self.end_spin = NoWheelSpin()
        self.end_spin.setRange(1, 999999)
        self.end_spin.setValue(max(file_count, 1))

        form = QFormLayout()
        form.addRow("固定开头（可留空）：", self.prefix_edit)
        form.addRow("起始数字：", self.start_spin)
        form.addRow("结束数字：", self.end_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self):
        """返回 (固定开头, 起始数字, 结束数字)"""
        return self.prefix_edit.text().strip(), self.start_spin.value(), self.end_spin.value()


class ReencodeMaterialsDialog(QDialog):
    """4b. 重新编码素材：选一套统一参数，把全部素材重编码成一致格式"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("重新编码素材（统一编码）")
        self.setModal(True)

        hint = QLabel(
            "把全部素材统一成下面这套参数（默认按常见竖屏短视频设置）。\n"
            "视频码率 / 音频比特率可直接改；留空则该项不强制指定（由编码器自定）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")

        self.res_edit = QLineEdit("1080x1920")
        self.fps_edit = QLineEdit("60")

        self.vcodec_combo = NoWheelCombo()
        self.vcodec_combo.addItems(["h264", "h265"])
        self.vcodec_combo.setCurrentText("h264")

        self.pix_combo = NoWheelCombo()
        self.pix_combo.addItems(["yuv420p", "yuv422p", "nv12", "yuv444p"])
        self.pix_combo.setCurrentText("yuv420p")

        self.acodec_combo = NoWheelCombo()
        self.acodec_combo.addItems(["aac", "libmp3lame"])
        self.acodec_combo.setCurrentText("aac")

        self.sr_combo = NoWheelCombo()
        self.sr_combo.addItems(["32000", "44100", "48000"])
        self.sr_combo.setCurrentText("32000")

        self.ch_combo = NoWheelCombo()
        self.ch_combo.addItems(["1", "2"])
        self.ch_combo.setCurrentText("2")

        self.vb_edit = QLineEdit("8000k")
        self.vb_edit.setPlaceholderText("如 8000k；留空=不限制")
        self.ab_edit = QLineEdit("128k")
        self.ab_edit.setPlaceholderText("如 128k；留空=不限制")

        form = QFormLayout()
        form.addRow("分辨率：", self.res_edit)
        form.addRow("帧率：", self.fps_edit)
        form.addRow("视频编码：", self.vcodec_combo)
        form.addRow("像素格式：", self.pix_combo)
        form.addRow("音频编码：", self.acodec_combo)
        form.addRow("采样率：", self.sr_combo)
        form.addRow("声道：", self.ch_combo)
        form.addRow("视频码率：", self.vb_edit)
        form.addRow("音频比特率：", self.ab_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self):
        """返回参数 dict（与 reencode.build_material_cmd 对应）"""
        return {
            "分辨率": self.res_edit.text().strip(),
            "帧率": self.fps_edit.text().strip(),
            "视频编码": self.vcodec_combo.currentText(),
            "像素格式": self.pix_combo.currentText(),
            "音频编码": self.acodec_combo.currentText(),
            "采样率": self.sr_combo.currentText(),
            "声道": self.ch_combo.currentText(),
            "视频码率": self.vb_edit.text().strip(),
            "音频比特率": self.ab_edit.text().strip(),
        }


class ConcatTab(QWidget):
    """「批量视频拼接」tab：选片头、选素材、参数对比、重编码、批量拼接"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.head_path = None   # 片头文件路径
        self.materials = []     # 素材文件路径列表（单个/多个/文件夹都汇总到这里）
        self.output_dir = default_output_dir()  # 成品默认保存到桌面「合成视频」文件夹
        # 计时器：算「扣掉暂停时间」的实际耗时（用户关心的是这批要跑多久）
        self._pause_tracker = PauseTracker()
        # 由主窗口注入：把本批数据写进「编码参考」（None = 不提供记录功能）
        # 由主窗口注入：把本批数据写进「编码参考」（None = 不提供记录功能）。
        # ⚠️ 拼接 tab 现在**不再注入**这两个回调 —— 编码参考只服务字幕烧录，
        # 拼接是几秒的流复制，记进去是噪音。属性保留是为了兼容外部调用。
        self.record_batch = None
        self.notify_batch = None
        self._last_batch = None

        # 素材总大小/总时长统计（后台线程探测，不卡界面）
        self._stats_gen = 0
        self._stats_bridge = StatsBridge()
        self._stats_bridge.done.connect(self._on_material_stats_done)

        # 参数对比：探测放后台线程 + 并发，探完通过信号回主线程填表
        self._compare_gen = 0
        self._compare_running = False
        self._compare_bridge = CompareBridge()
        self._compare_bridge.done.connect(self._on_compare_done)
        # 自动触发（如重编码片头后）的对比：记下是谁触发的 + 操作前的不一致项快照
        self._compare_auto = None
        self._compare_before = None

        self._build_ui()

    def _build_ui(self):
        """把界面上的各个控件拼起来（左右布局，跟字幕 tab 对齐）

        左半边：5 步操作流程（选片头 / 选素材 / 对比 / 重编码 / 批量合成）
        右半边：参数对比表，占满整右半屏
                  ┌──────────┬───────────────────────┐
                  │ 1.片头   │                       │
                  │ 2.素材   │   参数对比表           │
                  │ 3.对比   │   列 = 参数名（横）     │
                  │ 4.重编码 │   行 = 片头/素材（竖）  │
                  │ 5.批量   │                       │
                  └──────────┴───────────────────────┘
        """

        # ====== 第 1 步：选择片头 ======
        self.head_btn = QPushButton("1. 选择片头文件")
        self.head_btn.clicked.connect(self.choose_head)
        self.head_label = QLabel("（还没选片头）")
        self.head_label.setWordWrap(True)

        step1 = QGroupBox("1. 选择片头")
        sl1 = QVBoxLayout()
        sl1.addWidget(self.head_btn)
        sl1.addWidget(self.head_label)
        step1.setLayout(sl1)

        # ====== 第 2 步：选择素材 ======
        self.files_btn = QPushButton("2a. 选择素材文件（可多选）")
        self.files_btn.clicked.connect(self.choose_files)
        self.folder_btn = QPushButton("2b. 选择素材文件夹")
        self.folder_btn.clicked.connect(self.choose_folder)
        self.rename_btn = QPushButton("批量改名已选文件")
        self.rename_btn.clicked.connect(self.rename_materials)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.files_btn)
        btn_row.addWidget(self.folder_btn)

        self.material_label = QLabel("（还没选素材）")
        self.material_label.setWordWrap(True)
        self.material_summary = QLabel("总大小：-　总时长：-")
        self.material_summary.setStyleSheet("color: #555;")

        step2 = QGroupBox("2. 选择素材")
        sl2 = QVBoxLayout()
        sl2.addLayout(btn_row)
        sl2.addWidget(self.rename_btn)
        sl2.addWidget(self.material_label)
        sl2.addWidget(self.material_summary)
        step2.setLayout(sl2)

        # ====== 第 3 步：读取并对比参数 ======
        self.compare_btn = QPushButton("3. 读取并对比参数")
        self.compare_btn.setMinimumHeight(36)
        self.compare_btn.clicked.connect(self.compare)
        step3 = QGroupBox("3. 对比参数")
        sl3 = QVBoxLayout()
        sl3.addWidget(self.compare_btn)
        sl3.addWidget(QLabel("对比结果会在右侧表格显示；\n红色=必须重编码  黄色=码率可统一  白色=正常差异/一致"))
        step3.setLayout(sl3)

        # ====== 第 4 步：重编码（片头 / 素材） ======
        self.reencode_btn = QPushButton("4a. 重编码片头（对齐素材）")
        self.reencode_btn.clicked.connect(self.reencode)
        # 是否把片头做成「静音片头」：音频换成近静音占位音轨，避免拼接失败
        self.mute_intro_check = QCheckBox("改成静音片头")
        self.mute_intro_check.setToolTip(
            "勾选后，片头音频会被换成一条「几乎听不到」的占位音轨（aac / 97k / 32000 / 双声道）。\n"
            "作用：彻底静音的片头会被 ffmpeg 压成极低码率/无音轨，与素材音频参数对不上导致拼接失败；\n"
            "用这条占位音轨可保持音频参数一致，拼接更稳。参考脚本：1.编码片头视频+静音轨道.bat"
        )
        re4a_row = QHBoxLayout()
        re4a_row.addWidget(self.reencode_btn, 1)
        re4a_row.addWidget(self.mute_intro_check)

        self.reencode_mat_btn = QPushButton("4b. 重新编码素材（统一编码）")
        self.reencode_mat_btn.clicked.connect(self.reencode_materials)

        step4 = QGroupBox("4. 重编码")
        sl4 = QVBoxLayout()
        sl4.addLayout(re4a_row)
        sl4.addWidget(QLabel("红色参数出现时点 4a，把片头转成跟素材一致；\n静音片头仅替换片头音轨，不影响画面。"))
        sl4.addWidget(self.reencode_mat_btn)
        sl4.addWidget(QLabel("素材各自编码不一致时点 4b，把全部素材统一成同一套参数。"))
        step4.setLayout(sl4)

        # ====== 第 5 步：批量合成 + 保存位置 ======
        self.concat_btn = QPushButton("5. 一键批量合成")
        self.concat_btn.setMinimumHeight(40)
        self.concat_btn.clicked.connect(self.concat_all)
        self.out_dir_btn = QPushButton("选择保存位置")
        self.out_dir_btn.clicked.connect(self.choose_output_dir)
        concat_row = QHBoxLayout()
        concat_row.addWidget(self.concat_btn, 1)
        concat_row.addWidget(self.out_dir_btn)

        self.out_dir_label = QLabel("保存位置：" + self.output_dir)
        self.out_dir_label.setWordWrap(True)

        step5 = QGroupBox("5. 批量合成")
        sl5 = QVBoxLayout()
        sl5.addLayout(concat_row)
        sl5.addWidget(self.out_dir_label)
        step5.setLayout(sl5)

        # ====== 进度条 / 状态 ======
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("")
        self.progress_bar.hide()
        self.progress_label.hide()

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setToolTip("暂停/继续当前正在合成的视频")
        self.pause_btn.hide()
        self.pause_btn.clicked.connect(self._on_concat_pause)
        self.stop_btn = QPushButton("终止")
        self.stop_btn.setToolTip("立刻停止批量合成（已合成的成品保留）")
        self.stop_btn.hide()
        self.stop_btn.clicked.connect(self._on_concat_stop)
        self._concat_paused = False
        self._concat_cancelled = False

        self.status = QLabel("请先选片头和素材，再点「读取并对比参数」")

        # ====== 左侧：5 步操作流（放进可滚动区域） ======
        left_inner = QWidget()
        # 内容本身需要约 344px；让 Qt 布局按这个宽度预留空间，
        # 避免左侧被右侧表格挤扁导致按钮/下拉框截断或失效。
        left_inner.setMinimumWidth(344)
        ll = QVBoxLayout(left_inner)
        ll.setContentsMargins(6, 6, 6, 6)
        ll.setSpacing(8)
        ll.addWidget(step1)
        ll.addWidget(step2)
        ll.addWidget(step3)
        ll.addWidget(step4)
        ll.addWidget(step5)
        ll.addStretch(1)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_inner)
        # 左侧操作区最小 360px：内容需要约 344px；低于这个值 group box /
        # 按钮 / 下拉框会被右侧表格挤到文字截断、下拉失效。
        left_scroll.setMinimumWidth(360)
        # 存成属性：开始批量合成时要把左栏滚到底部，让用户看见进度条
        self.left_scroll = left_scroll

        # ====== 右侧：参数对比表 ======
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)  # 只读
        # 不要左右滚动条：列宽始终压缩到界面宽度内，放不下的文字用省略号（鼠标悬停看全值）
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideRight)
        # ⚠️ 必须关掉自动换行：默认开启时，列宽稍不够就把 "1080x1920" 折成两行
        # （表现为「分辨率」这类参数突然变高、表格参差不齐）。
        # 关掉后统一走「单行 + 省略号」，宽度分配见 _apply_column_widths()。
        self.table.setWordWrap(False)
        # 行高固定为「一行文字」的高度，禁止 Qt 因为折行而撑高某一行
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(
            self.table.fontMetrics().height() + 8)
        # 列头默认居中
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        # 行头也按内容自适应并居中
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignCenter)
        # 初始留空（0 行 0 列），点「读取并对比参数」后才会填充
        # 列 = 参数名（横排），行 = 片头 / 素材N（竖排）
        self.table.setColumnCount(0)
        self.table.setRowCount(0)
        # 记住最近一次的「自然列宽」和参数名，窗口缩放时按新宽度重新分配
        self._table_natural = []
        self._table_keys = []

        table_box = QGroupBox("参数对比表")
        tb = QVBoxLayout()
        tb.addWidget(self.table, 1)
        # 提示行
        hint = QLabel(
            "红色=必须重编码  黄色=码率可统一  白色=正常差异/一致\n"
            "鼠标悬停在参数名上看差异原因"
        )
        hint.setStyleSheet("color: #666; font-size: 11px;")
        hint.setWordWrap(True)
        tb.addWidget(hint)

        # 移除勾选的素材：参数表最左边一列是勾选框，勾掉要删的素材再点这里。
        # 典型场景：整个文件夹几百个素材，只有几个不合适 —— 勾那几个移除，
        # 不用先把要留的一个个挑出来。
        self.remove_checked_btn = QPushButton("移除勾选的素材")
        self.remove_checked_btn.setToolTip(
            "先在下面的参数表里，勾上左边第一列的框（一行一个素材），再点这个按钮：\n"
            "勾选的素材会被移出列表，没勾的保留。\n"
            "适合「文件夹里几百个素材，只有几个不要」的情况。\n"
            "（片头行没有勾选框——片头不能用这种方式移除）")
        self.remove_checked_btn.clicked.connect(self.remove_checked_materials)
        tb.addWidget(self.remove_checked_btn)

        # 清空片头/素材 + 重置参数表（释放已选列表占用的内存）
        self.clear_btn = QPushButton("清空片头与素材（重置参数表）")
        self.clear_btn.setToolTip("取消已选的片头和素材，并清空参数对照表，释放内存")
        self.clear_btn.clicked.connect(self.clear_selection)
        tb.addWidget(self.clear_btn)

        tb.addWidget(self.progress_bar)
        tb.addWidget(self.progress_label)
        tb.addWidget(self.pause_btn)
        tb.addWidget(self.stop_btn)
        tb.addWidget(self.status)
        table_box.setLayout(tb)

        # ====== 顶层：左右两栏（35:65），状态栏横跨底部 ======
        # 左侧只是操作按钮，不需要太宽；右侧参数表需要更多横向空间。
        body = QHBoxLayout()
        body.setSpacing(8)
        body.setContentsMargins(8, 8, 8, 4)
        body.addWidget(left_scroll)
        body.addWidget(table_box, 1)
        body.setStretchFactor(left_scroll, 35)
        body.setStretchFactor(table_box, 65)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addLayout(body, 1)

    def choose_head(self):
        """弹出文件选择框，选一个片头视频"""
        path, _ = QFileDialog.getOpenFileName(self, "选择片头文件", "", VIDEO_FILTER)
        if path:
            self.head_path = path
            self.head_label.setText(path)

    def choose_files(self):
        """选择一个或多个素材文件（可以按住 Ctrl 多选）"""
        paths, _ = QFileDialog.getOpenFileNames(self, "选择素材文件（可按住 Ctrl 多选）", "", VIDEO_FILTER)
        if paths:
            self.materials = list(paths)
            self.material_label.setText("已手动选择 {} 个素材文件".format(len(paths)))
            self._update_material_stats()

    def choose_folder(self):
        """选择整个素材文件夹，自动把里面的视频都加进来"""
        folder = QFileDialog.getExistingDirectory(self, "选择素材文件夹")
        if folder:
            self.materials = list_videos(folder)
            if self.materials:
                self.material_label.setText("文件夹：{}（共 {} 个视频）".format(folder, len(self.materials)))
                self._update_material_stats()
            else:
                self.material_label.setText("文件夹 {} 里没找到视频文件".format(folder))

    def choose_output_dir(self):
        """选择成品保存位置（默认桌面「合成视频」文件夹）"""
        folder = QFileDialog.getExistingDirectory(self, "选择保存位置", self.output_dir)
        if folder:
            self.set_output_dir(folder)
            self.status.setText("保存位置已改为：" + folder)

    def set_output_dir(self, folder, remember=True):
        """设置成品保存位置（并记住，下次打开程序还是这里）。"""
        self.output_dir = folder
        self.out_dir_label.setText("保存位置：" + folder)
        if remember:
            app_config.update(concat_output_dir=folder)

    def rename_materials(self):
        """批量改名：按文件名顺序，给已选素材依次编号"""
        if not self.materials:
            QMessageBox.warning(self, "提示", "请先选择素材（文件或文件夹）")
            return

        dlg = RenameDialog(len(self.materials), self)
        disable_wheel_recursive(dlg)      # 滚轮防误改（起始/结束编号是数字框）
        if dlg.exec() != QDialog.Accepted:
            return

        prefix, start, end = dlg.values()
        if end < start:
            QMessageBox.warning(self, "提示", "结束数字不能小于起始数字")
            return

        prefix = _safe_prefix(prefix)
        files = sorted(self.materials, key=_natural_key)
        count = end - start + 1

        if count != len(files):
            reply = QMessageBox.question(
                self, "数量不一致",
                "已选 {} 个文件，但数字范围 {}-{} 共 {} 个。\n\n"
                "文件比范围多时，多出的文件保持原名不动；\n"
                "文件比范围少时，多出的数字不会被使用。\n\n"
                "是否仍要继续？".format(len(files), start, end, count),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        width = 3   # 固定三位数补零（如 1→001，10→010，50→050）

        # 分两步改名，避免「A 改成 B、B 又被覆盖」的链式冲突
        # 第一步：先全部改成临时名，记下原路径
        staged = []   # (原路径, 临时路径, 扩展名, 所在目录)
        try:
            for i, path in enumerate(files):
                ext = os.path.splitext(path)[1]
                folder = os.path.dirname(path)
                tmp = os.path.join(folder, "__rb_tmp_{}_{}{}".format(os.getpid(), i, ext))
                os.rename(path, tmp)
                staged.append((path, tmp, ext, folder))
        except Exception as e:
            QMessageBox.critical(self, "改名失败", "第一步（临时改名）出错：{}\n\n可能已有部分文件被改名，请检查。".format(e))
            return

        # 第二步：从临时名改成最终编号名（超出范围的文件恢复原名）
        final_paths = []
        try:
            for i, (orig, tmp, ext, folder) in enumerate(staged):
                if i < count:
                    num = start + i
                    base = "{}_{:0{}d}".format(prefix, num, width) if prefix else "{:0{}d}".format(num, width)
                    final = _avoid_collision(os.path.join(folder, base + ext))
                    os.rename(tmp, final)
                    final_paths.append(final)
                else:
                    os.rename(tmp, orig)   # 超出范围，恢复原名
                    final_paths.append(orig)
        except Exception as e:
            QMessageBox.critical(self, "改名失败", "第二步（正式改名）出错：{}\n\n部分文件可能停留在临时名，请检查。".format(e))
            return

        renamed_count = min(count, len(files))
        self.materials = final_paths
        self.material_label.setText("已改名 {} 个文件".format(renamed_count))
        self._update_material_stats()

        preview = "、".join(os.path.basename(p) for p in final_paths[:5])
        if len(final_paths) > 5:
            preview += " …"
        self.status.setText("改名完成（{} 个）：{}".format(renamed_count, preview))
        QMessageBox.information(self, "完成", "成功改名 {} 个文件。".format(renamed_count))

    def _update_material_stats(self):
        """统计素材总大小（同步）和总时长（后台线程探测），更新汇总栏"""
        if not self.materials:
            self.material_summary.setText("总大小：-　总时长：-")
            return
        try:
            total_size = sum(os.path.getsize(p) for p in self.materials if os.path.isfile(p))
        except OSError:
            total_size = 0
        self.material_summary.setText("总大小：{}　总时长：计算中…".format(_format_file_size(total_size)))
        self._stats_gen += 1
        gen = self._stats_gen
        paths = list(self.materials)

        def worker():
            try:
                d, s = total_stats(paths)
            except Exception:
                d, s = 0.0, total_size
            self._stats_bridge.done.emit(gen, d, s)

        threading.Thread(target=worker, daemon=True).start()

    def _on_material_stats_done(self, gen, duration, size):
        """后台统计结果回来（主线程执行），忽略过期结果"""
        if gen != self._stats_gen:
            return
        self.material_summary.setText("总大小：{}　总时长：{}".format(
            _format_file_size(size), _format_total_duration(duration)))

    def compare(self, auto_after=None, before_bad=None):
        """读取片头和素材的参数，填进对比表，不一致的标红。

        新布局（与用户需求一致）：
          列 = 各参数名（横排）—— 列头参数，鼠标悬停看差异原因
          行 = 片头 / 素材1 / 素材2 / …（竖排）—— 行头是视频名

        ⚠️ 探测（每个文件一次 ffprobe）**放在后台线程 + 并发**里跑：
        20 个素材串行要 4.8 秒，之前在主线程里做，点下去整个窗口冻住，
        用户以为程序死了。现在界面全程可以拖动，探完再回主线程填表。

        auto_after: 非 None 表示这是**某个操作跑完后自动触发**的对比
                    （如 "重编码片头"）。填表完成后会在状态栏加一句
                    「重编码前 N 项不一致 → 现在 M 项」，让变化一眼可见。
        before_bad: 可选，调用方**事先算好**的「操作前不一致项集合」。
                    传了就用它，不传则自己现算（重编码场景必须传 —— 因为这时
                    self.head_path 已经换成新文件了，现算只能得到「新 vs 素材」，
                    前后都是同一个数，对照就没意义了）。
        """
        if not self.head_path and not self.materials:
            QMessageBox.warning(self, "提示", "请先选择片头或素材，再点「读取并对比参数」")
            return
        if getattr(self, "_compare_running", False):
            return                       # 上一次还没探完，忽略重复点击

        head = self.head_path
        mats = list(self.materials)
        self._compare_gen += 1
        gen = self._compare_gen
        self._compare_running = True
        self._compare_auto = auto_after     # 供 _on_compare_done 判断是不是自动触发

        # 前后对照的「操作前」快照：调用方给了就用（重编码场景），否则现算
        if before_bad is not None:
            self._compare_before = before_bad
        elif auto_after:
            self._compare_before = self._diff_keys(head, mats)
        else:
            self._compare_before = None

        # 立刻切成「计算中」状态：否则点下去像没反应
        self.compare_btn.setEnabled(False)
        self.compare_btn.setText("正在读取参数…")
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        # 探测期间别按旧列宽数据分配（表格已清空）
        self._table_natural = []
        self._table_keys = []
        if auto_after:
            self.status.setText("{}完成，正在重新读取参数，稍等…".format(auto_after))
        else:
            self.status.setText("正在读取 {} 个文件的参数（后台并发读取，界面可正常操作）…".format(
                (1 if head else 0) + len(mats)))

        def worker():
            failures = []
            head_info = None
            if head:
                try:
                    head_info = get_key_params(probe(head))
                except Exception as e:
                    failures.append((head, str(e)))
            mats_pairs, mat_failures = probe_many(mats) if mats else ([], [])
            failures.extend(mat_failures)
            self._compare_bridge.done.emit(gen, {
                "head_info": head_info,
                "mats": mats_pairs,
                "failures": failures,
            })

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _diff_keys(head_path, mats):
        """探测片头 + 素材，返回「关键参数不一致」的项名列表（不含码率等次要项）。

        重编码前后各算一次，得到「N 项不一致 → M 项」的直观对照。
        探测器在极少数情况下会失败（文件被占用等），此时返回 None 表示"不知道"，
        由调用方决定不显示对照（不要假装是 0 项，那会误导）。
        """
        if not head_path or not mats:
            return None
        try:
            head_info = get_key_params(probe(head_path))
            bad = set()
            for p in mats:
                mat_info = get_key_params(probe(p))
                bad.update(find_critical_diffs(head_info, mat_info))
            return bad
        except Exception:
            return None

    def _apply_column_widths(self):
        """按当前视口宽度分配参数表列宽（窗口缩放时也会调）。

        设计目标有三个，按优先级：
          1. **不出现横向滚动条** —— 列总宽始终等于视口可用宽度；
          2. **参数名与数值都只占一行** —— 所以列宽下限设为「表头文字能放下」
             （除非视口实在太窄，那时宁可让文字被省略号截断，也不折行）；
          3. **窗口放大后要用满宽度** —— 用 ResizeToContents 不好控制比例，
             所以自己算：先给每列它的自然宽度，多出来的余量再按比例摊给各列。

        数据来自 self._table_natural（填表时算好），所以缩放时不用重新探测。
        """
        natural = getattr(self, "_table_natural", None)
        keys = getattr(self, "_table_keys", None)
        if not natural or not keys or self.table.columnCount() < 2:
            return

        CHECK_COL_W = 34
        header = self.table.horizontalHeader()
        fm = self.table.fontMetrics()

        # 可用宽度 = 视口宽 - 勾选框列 - 右侧几条竖线/边距的余量
        avail = self.table.viewport().width() - CHECK_COL_W - 4
        if avail <= 0:
            return

        n = len(natural)
        total_nat = sum(natural)

        # 参数名「至少能看出是什么参数」的宽度下限。
        # 表头文字 + 一点边距；这是硬下限，宁可出横向滚动条也不低于它。
        # 边距取 6px（够放下省略号前的几个字），太大容易让常规窗口提前出滚动条。
        floor_w = [fm.horizontalAdvance(k) + 6 for k in keys]
        floor_total = sum(floor_w)

        if total_nat <= avail:
            # 放得下：每列先给自然宽度，剩下的余量按自然宽度比例摊下去。
            # 这样窗口放大时列会一起变宽，而不是把空白全留在右边。
            spare = avail - total_nat
            widths = [w + int(spare * w / total_nat) for w in natural]
            widths[-1] += avail - sum(widths)
        elif floor_total <= avail:
            # 放得下「参数名 + 部分数值」：以参数名宽度为地板，富余空间按各列
            # 「自然宽度 - 地板」的比例分配（数值长的列分得多）
            extra = avail - floor_total
            weights = [max(1, w - f) for w, f in zip(natural, floor_w)]
            wsum = sum(weights)
            widths = [f + int(extra * wt / wsum) for f, wt in zip(floor_w, weights)]
            widths[-1] += avail - sum(widths)
        else:
            # 窗口太窄，连参数名都放不下：**不硬挤**（挤成 18px 时表头只剩
            # 「分…」，比折行还难认）。给出参数名地板宽度，让表格出横向
            # 滚动条，用户滚动就能看全 —— 这比把所有列压扁更有用。
            widths = list(floor_w)

        # 内容比视口宽时（上面的最窄分支）要允许横向滚动，否则列会互相挤压
        need_scroll = sum(widths) > avail
        header.setStretchLastSection(False)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if need_scroll else Qt.ScrollBarAlwaysOff)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, CHECK_COL_W)
        for col in range(n):
            header.setSectionResizeMode(col + 1, QHeaderView.Fixed)
            header.resizeSection(col + 1, max(18, widths[col]))

    @staticmethod
    def _diff_keys_from_info(head_info, mats):
        """用**已经探好的**片头参数，去和每个素材比关键参数，返回不一致项集合。

        重编码场景专用：新片头还没探过、旧片头参数手头就有，用这个算 before 快照
        可以少探一次片头（也避免依赖"文件已经落盘"这种时序假设）。
        """
        if head_info is None or not mats:
            return None
        try:
            bad = set()
            for p in mats:
                mat_info = get_key_params(probe(p))
                bad.update(find_critical_diffs(head_info, mat_info))
            return bad
        except Exception:
            return None

    @staticmethod
    def _count_diff_columns(row_paths, row_infos, keys):
        """按「已填进表格的数据」统计还有哪些关键参数不一致。

        和 _on_compare_done 里的涂色判断用同一套 values_equal / CRITICAL_KEYS，
        所以状态栏说的项数跟表格里红色列的数量**必然一致**，不会两套口径打架。
        没有片头（row_infos[0] 是素材）时以第 1 个素材为基准，与表格逻辑相同。
        """
        if len(row_infos) < 2:
            return None
        bad = set()
        base = row_infos[0]
        for key in keys:
            if key not in CRITICAL_KEYS:
                continue
            for info in row_infos[1:]:
                if not values_equal(key, base.get(key), info.get(key)):
                    bad.add(key)
                    break
        return bad

    def _on_compare_done(self, gen, payload):
        """后台探测完毕（主线程执行）：填对比表。过期的结果直接丢弃。"""
        if gen != getattr(self, "_compare_gen", 0):
            return                       # 用户又点了一次 / 列表变了，这份结果作废
        self._compare_running = False
        self.compare_btn.setEnabled(True)
        self.compare_btn.setText("3. 读取并对比参数")

        head_info = payload.get("head_info")
        mats_pairs = payload.get("mats") or []
        failures = payload.get("failures") or []

        # 行 = [片头(若有)] + [素材1, 素材2, …]，列 = 参数
        # 素材行号沿用它在 self.materials 里的原始位置；探测失败的不占行
        info_by_path = {p: i for p, i in mats_pairs}
        row_paths = []
        row_names = []
        row_infos = []
        if head_info is not None:
            row_paths.append(self.head_path)
            row_names.append("片头")
            row_infos.append(head_info)
        for idx, p in enumerate(self.materials):
            mi = info_by_path.get(p)
            if mi is None:
                continue
            row_paths.append(p)
            row_names.append("素材{}".format(idx + 1))
            row_infos.append(mi)

        if not row_infos:
            QMessageBox.critical(
                self, "读取失败",
                "所有文件都没能读出参数：\n\n" + "\n".join(
                    "{}：{}".format(os.path.basename(p), m[:200])
                    for p, m in failures[:5]))
            self.status.setText("读取参数失败")
            return

        has_head = head_info is not None

        # 参数名（列）取自第 0 行（片头优先，否则第 1 个素材）
        keys = list(row_infos[0].keys())

        # 行表头：显示行名 + tooltip 完整路径（精确定位是哪个文件）
        self.table.setRowCount(len(row_paths))
        self.table.setVerticalHeaderLabels(row_names)
        for i, path in enumerate(row_paths):
            header_item = self.table.verticalHeaderItem(i)
            if header_item is not None:
                header_item.setToolTip(path)

        # 列表头：第 0 列是勾选框列（给素材用，勾上表示「要移除」），
        # 参数从第 1 列开始，tooltip 写差异原因（鼠标悬停看为啥红/黄/灰）
        CHECK_COL_W = 34
        self.table.setColumnCount(len(keys) + 1)
        self.table.setHorizontalHeaderLabels([""] + list(keys))

        # 勾选框列：片头行不给勾（片头不是用这种方式移除的）
        for row in range(len(row_paths)):
            cell = QTableWidgetItem("")
            cell.setFlags((cell.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            cell.setCheckState(Qt.Unchecked)
            if row == 0 and has_head:
                cell.setFlags(Qt.ItemIsEnabled)      # 片头行不给勾
                cell.setToolTip("片头不能用这种方式移除（要换片头请重新选一个）")
            else:
                cell.setToolTip("勾上这个框，再点上面的「移除勾选的素材」把它去掉")
            self.table.setItem(row, 0, cell)   # 第 0 列 = 勾选框列

        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignCenter)

        # 每行（每个文件）的每个参数值
        # 每个 (参数, 行) 单元格：填值；再统一判断整列是否有差异，决定涂色
        # 1) 先填值
        all_rows_values = row_infos  # [片头dict(若有), 素材1dict, ...]
        col_data = []  # col_data[col] = [format_param(head), format_param(mat1), ...]
        for col, key in enumerate(keys):
            col_vals = []
            for info in all_rows_values:
                v_raw = info.get(key)
                col_vals.append((format_param(key, v_raw), v_raw))
            col_data.append(col_vals)
            for row, (disp, _raw) in enumerate(col_vals):
                item = QTableWidgetItem(disp)
                item.setTextAlignment(Qt.AlignCenter)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)  # 只读
                self.table.setItem(row, col + 1, item)   # +1：第 0 列留给勾选框

        # 2) 对每列判断：是否和片头不一致 → 整列涂色 + tooltip
        for col, key in enumerate(keys):
            # 列是否差异：以第 0 行（片头优先，否则第 1 个素材）为基准
            hv_raw = row_infos[0].get(key)
            any_diff = False
            for row in range(1, len(row_paths)):
                mv_raw = all_rows_values[row].get(key)
                if not values_equal(key, hv_raw, mv_raw):
                    any_diff = True
                    break

            if not any_diff:
                # 整列一致：白底 + tooltip 写"全部一致"
                reason = "基准文件与所有其它文件该项参数一致"
                for row in range(len(row_paths)):
                    item = self.table.item(row, col + 1)
                    if item is not None:
                        item.setBackground(QColor(255, 255, 255))
                        # tooltip 前面拼上完整值（列窄时被省略号截掉的话，悬停还能看到）
                        item.setToolTip("{}\n{}".format(item.text(), reason))
                header = self.table.horizontalHeaderItem(col + 1)
                if header is not None:
                    header.setToolTip(reason)
                continue

            # 有差异 → 按严重程度选色
            if key in CRITICAL_KEYS:
                color = QColor(255, 160, 160)
                reason = ("关键参数不一致，必须重编码片头，否则直接拼接可能失败"
                          if has_head else
                          "素材间关键参数不一致，若与片头拼接需先统一编码")
            elif key in WARN_KEYS:
                color = QColor(255, 220, 150)
                reason = "码率不一致也能拼接成功，但输出视频的码率会不统一"
            elif key in IGNORE_KEYS:
                color = QColor(255, 255, 255)
                reason = IGNORE_REASONS.get(key, "该项属于正常差异，不影响拼接")
            else:
                color = QColor(220, 220, 220)
                reason = "该参数存在差异"

            # 给整列（片头+素材行）涂色，tooltip 写原因
            for row in range(len(row_paths)):
                item = self.table.item(row, col + 1)
                if item is not None:
                    item.setBackground(color)
                    # tooltip 前面拼上完整值（列窄时被省略号截掉的话，悬停还能看到）
                    item.setToolTip("{}\n{}".format(item.text(), reason))
            header = self.table.horizontalHeaderItem(col + 1)
            if header is not None:
                header.setToolTip("⚠️ {}\n\n{}".format(key, reason))

        # 列宽策略：第 0 列固定给勾选框，剩下宽度再分给参数列 ——
        # 数值比名字短的列压窄，其余列分剩余宽度，整体不出左右滚动条。
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignCenter)
        header.setMinimumSectionSize(0)

        # 先把「勾选框列」钉死，参数列只能用视口剩下的宽度
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, CHECK_COL_W)

        # 每列「一行放得下」所需的宽度（表头名字 与 最宽的值 取大者）。
        # 注意：不额外加很多 padding —— 加太多会把窗口逼向「压缩模式」，
        # 反而让参数名被省略号截断。
        fm = self.table.fontMetrics()
        natural = []
        for col, key in enumerate(keys):
            header_w = fm.horizontalAdvance(key) + 14
            max_cell_w = header_w
            for row in range(len(row_paths)):
                item = self.table.item(row, col + 1)
                if item is not None:
                    max_cell_w = max(max_cell_w, fm.horizontalAdvance(item.text()) + 12)
            natural.append(max_cell_w)

        # 记下来：窗口缩放时用同一份数据按新宽度重新分配，不用重新探测
        self._table_natural = list(natural)
        self._table_keys = list(keys)
        self._apply_column_widths()

        # 状态栏：提示当前显示模式（+ 读取失败的文件清单）
        if has_head and mats_pairs:
            self.status.setText("")
        elif has_head:
            self.status.setText("仅显示片头参数（未选素材）")
        else:
            self.status.setText("仅显示素材参数（未选片头，以第 1 个素材为基准标差异）")

        # 重编码后自动对比：把「之前 N 项不一致 → 现在 M 项」直接写进状态栏，
        # 让用户不用自己再去数红格子。before 为 None 表示探测失败，宁可不显示
        # 对照，也不能假装「原来 0 项」。
        auto_after = getattr(self, "_compare_auto", None)
        if auto_after:
            now_bad = self._count_diff_columns(row_paths, row_infos, keys)
            before_bad = getattr(self, "_compare_before", None)
            if before_bad is None or now_bad is None:
                self.status.setText(
                    "{}完成，参数已重新读取（对比表已刷新）".format(auto_after))
            elif not now_bad:
                self.status.setText(
                    "✅ {}完成：关键参数已全部对齐（重编码前有 {} 项不一致）".format(
                        auto_after, len(before_bad)))
            else:
                self.status.setText(
                    "{}完成：关键参数仍有 {} 项不一致（{}）—— 重编码前有 {} 项".format(
                        auto_after, len(now_bad), "、".join(sorted(now_bad)),
                        len(before_bad)))
            self._compare_auto = None

        if failures:
            names = "、".join(os.path.basename(p) for p, _ in failures[:3])
            if len(failures) > 3:
                names += " 等 {} 个".format(len(failures))
            self.status.setText(
                self.status.text() + "　⚠️ {} 个文件读取失败：{}".format(
                    len(failures), names))

    def remove_checked_materials(self):
        """把参数表里勾选的素材从已选列表移除（片头行没有勾选框）。"""
        if not self.materials:
            QMessageBox.information(self, "提示", "还没选素材")
            return
        if self.table.columnCount() == 0:
            QMessageBox.information(
                self, "提示",
                "请先点「3. 读取并对比参数」把表格读出来，再勾选要去掉的素材。")
            return

        # 勾选状态在第 0 列；「行 → 哪个素材」用行头的 tooltip（compare 时写进去的完整路径）。
        # 不能直接用行号推算：探测失败的素材不会占表行，行号和 self.materials 的下标会对不上。
        to_remove = []
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is None or cell.checkState() != Qt.Checked:
                continue
            hdr = self.table.verticalHeaderItem(row)
            path = hdr.toolTip() if hdr is not None else ""
            if path and path in self.materials:
                to_remove.append(path)
            elif hdr is not None and hdr.text() == "片头":
                continue                      # 片头行（本来就没勾选框，双保险）

        if not to_remove:
            QMessageBox.information(
                self, "提示",
                "还没有勾选任何素材。\n\n"
                "在下面参数表**最左边那一列**勾选要去掉的素材，再点这个按钮 ——\n"
                "勾选的会被移除，没勾的保留。")
            return

        gone = set(to_remove)
        self.materials = [p for p in self.materials if p not in gone]
        names = "、".join(os.path.basename(p) for p in to_remove[:3])
        if len(to_remove) > 3:
            names += " 等 {} 个".format(len(to_remove))
        self.material_label.setText("已移除 {} 个素材，还剩 {} 个".format(
            len(to_remove), len(self.materials)))
        self.status.setText("已移除 {} 个素材（{}）".format(len(to_remove), names))
        # 素材变了 → 参数表重读。compare() 现在是后台+缓存，剩下那些文件基本秒回。
        self.compare()

    def resizeEvent(self, event):
        """窗口/面板尺寸变化时，重新分配参数表列宽。

        没有这一步的话，列宽只在「填表那一刻」算一次：窗口放大后表格
        右边会留一大片空白，看起来像没适配。用 QTimer 延后 0ms 执行是为了
        等布局把新宽度真正应用到 viewport 上（resizeEvent 里读到的
        viewport().width() 还是旧值，会算错）。
        """
        super().resizeEvent(event)
        QTimer.singleShot(0, self._apply_column_widths)

    def clear_selection(self):
        """清空片头、素材与参数对照表（释放已选列表占用的内存）。"""
        self.head_path = None
        self.materials = []
        # 让还在后台跑的参数对比结果作废（否则探完会把旧数据填回空表）
        self._compare_gen += 1
        self._compare_running = False
        self.head_label.setText("（还没选片头）")
        self.material_label.setText("（还没选素材）")
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        # 清掉列宽缓存，避免空表时按旧数据分配宽度
        self._table_natural = []
        self._table_keys = []
        self.status.setText("已清空片头与素材，参数表已重置")

    def reencode(self):
        """检测片头与素材的关键差异，并重编码片头对齐素材"""
        if not self.head_path:
            QMessageBox.warning(self, "提示", "请先选择片头文件")
            return
        if not self.materials:
            QMessageBox.warning(self, "提示", "请先选择素材（文件或文件夹）")
            return

        head_info = get_key_params(probe(self.head_path))
        # 以第 1 个素材为基准
        mat_info = get_key_params(probe(self.materials[0]))

        mute = self.mute_intro_check.isChecked()

        diffs = find_critical_diffs(head_info, mat_info)
        if not diffs and not mute:
            QMessageBox.information(self, "结果", "片头和素材的关键参数一致，无需重编码，可以直接拼接。")
            return

        # 输出到项目里的 output 文件夹
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "head_converted.mp4")

        tail = "（含静音片头：音频将换成近静音占位音轨，码率 97k）" if mute else ""
        reply = QMessageBox.question(
            self,
            "确认重编码",
            "检测到关键参数不一致：{}\n\n将以「第 1 个素材」为基准重编码片头。{}\n输出到：{}\n\n是否开始？".format(
                "、".join(diffs) if diffs else "（无，仅应用静音片头）", tail, out_path),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            reencode_head(self.head_path, head_info, mat_info, out_path, mute=mute)
        except Exception as e:
            QMessageBox.critical(self, "重编码失败", str(e))
            return

        # 成功后，把片头切换为新文件，方便下一步批量合成直接用它
        self.head_path = out_path
        self.head_label.setText(out_path + "（已重编码）")
        self.status.setText("重编码完成：{}".format(out_path))

        # —— 自动追加一次「读取并对比参数」——
        # 重编码的目的就是对齐参数，跑完立刻重读一遍，表格里的红格子会当场消失，
        # 比让用户自己再点一次「3. 读取并对比参数」直观得多。
        #
        # ⚠️ 关键：before 快照必须用**重编码前的 head_info** 来算。
        # 此时 self.head_path 已经换成新文件了，若交给 compare() 现算，
        # 得到的会是「新片头 vs 素材」（= 0 项），前后对照就没意义。
        before_bad = self._diff_keys_from_info(head_info, self.materials)

        # 先启动后台对比，**再**弹「完成」框：
        # 用户读弹窗的这几秒后台已经在探测了，点掉确定就能看到结果，
        # 而不是点了确定才开始转圈。（compare() 本身是毫秒级返回的）
        self.compare(auto_after="重编码片头", before_bad=before_bad)

        tail = "已自动重新读取参数对比（正在后台读取，稍等即可看到结果）。"
        if before_bad:
            tail = ("已自动重新读取参数对比 —— 重编码前有 {} 项关键参数不一致（{}），"
                    "现在表格已按新参数刷新。").format(
                len(before_bad), "、".join(sorted(before_bad)))
        QMessageBox.information(
            self, "完成",
            "片头已重编码，参数已对齐素材。\n新文件：{}\n\n{}".format(out_path, tail))

    def reencode_materials(self):
        """4b. 重新编码素材：把全部素材统一成一套参数（后台线程跑）"""
        if not self.materials:
            QMessageBox.warning(self, "提示", "请先选择素材（文件或文件夹）")
            return

        dlg = ReencodeMaterialsDialog(self)
        disable_wheel_recursive(dlg)      # 滚轮防误改（参数是下拉框）
        if dlg.exec() != QDialog.Accepted:
            return
        params = dlg.values()

        # 校验必填：分辨率 / 帧率
        if not params["分辨率"] or not params["帧率"]:
            QMessageBox.warning(self, "提示", "分辨率和帧率不能为空")
            return

        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "output", "素材重编码")
        os.makedirs(out_dir, exist_ok=True)

        total = len(self.materials)
        # 用窗口内嵌进度条，避免弹出式对话框重绘冲突闪退
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在重编码素材 0/{} ...".format(total))
        self.progress_bar.show()
        self.progress_label.show()
        self._set_busy(True)
        # 把左侧栏滚到底：进度条在左栏最底部，小屏幕用户默认看不见
        self._scroll_left_to_bottom()

        self._mat_start = time.monotonic()

        self._mat_thread = QThread(self)
        self._mat_worker = ReencodeMatWorker(self.materials, params, out_dir)
        self._mat_worker.moveToThread(self._mat_thread)
        self._mat_worker.progress.connect(self._on_mat_progress)
        self._mat_worker.finished.connect(self._on_mat_finished)
        self._mat_thread.started.connect(self._mat_worker.run)
        self._mat_thread.start()

    def _on_mat_progress(self, current, total, filename, success, message):
        self.progress_bar.setValue(current)
        name = os.path.basename(filename)
        state = "成功" if success else "失败"
        self.progress_label.setText(
            "正在重编码素材 {}/{}：{} {}".format(current, total, name, state)
        )
        self.status.setText(
            "素材重编码中 {}/{}：{} {}".format(current, total, name, state)
        )

    def _on_mat_finished(self, success_count, fail_list, out_paths):
        elapsed = time.monotonic() - self._mat_start
        elapsed_str = _format_duration(elapsed)

        self._mat_thread.quit()
        self._mat_thread.wait()
        self._set_busy(False)
        self.progress_bar.hide()
        self.progress_label.hide()

        if success_count == -1:
            if fail_list and fail_list[0][0] == "内部错误":
                QMessageBox.critical(self, "错误", fail_list[0][1])
            self.status.setText("素材重编码出错")
            return

        # 成功后把素材列表切换成重编码后的文件，方便继续对比 / 拼接
        if out_paths:
            self.materials = out_paths
            self.material_label.setText(
                "已重编码 {} 个素材（统一编码），列表已更新为输出文件".format(len(out_paths))
            )

        if fail_list:
            detail = "\n".join(
                "{}：{}".format(os.path.basename(p), m[:200]) for p, m in fail_list
            )
            QMessageBox.warning(
                self, "素材重编码完成（部分失败）",
                "成功 {} 个，失败 {} 个，总耗时 {}。\n\n失败文件：\n{}".format(
                    success_count, len(fail_list), elapsed_str, detail),
            )
        else:
            QMessageBox.information(
                self, "素材重编码完成",
                "全部 {} 个素材已统一编码，总耗时 {}！\n输出文件夹：{}".format(
                    success_count, elapsed_str,
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "output", "素材重编码")),
            )
        self.status.setText(
            "素材重编码结束：{} 成功，{} 失败（耗时 {}）".format(
                success_count, len(fail_list), elapsed_str)
        )

    def _scroll_left_to_bottom(self):
        """把左侧操作栏滚到底部，让「进度条 / 暂停 / 终止」可见。

        小屏幕（或窗口拉得矮）时，这些控件在左栏最底部、默认看不见，
        批处理一开始用户会以为程序卡死了。这里在开始批量任务时自动滚下去；
        延迟两次是因为 show() 之后布局还没更新，滚动条最大值要等控件排好才准。
        """
        def _to_bottom():
            try:
                sb = self.left_scroll.verticalScrollBar()
                sb.setValue(sb.maximum())
            except Exception:
                pass
        QTimer.singleShot(0, _to_bottom)
        QTimer.singleShot(80, _to_bottom)

    def concat_all(self):
        """一键批量合成：片头 + 每个素材 → 输出成品"""
        if not self.head_path:
            QMessageBox.warning(self, "提示", "请先选择片头文件")
            return
        if not self.materials:
            QMessageBox.warning(self, "提示", "请先选择素材（文件或文件夹）")
            return

        # 先检查一下关键参数是否一致，不一致的话提醒用户先重编码
        head_info = get_key_params(probe(self.head_path))
        mat_info = get_key_params(probe(self.materials[0]))
        diffs = find_critical_diffs(head_info, mat_info)
        if diffs:
            reply = QMessageBox.question(
                self,
                "关键参数不一致",
                "检测到片头和素材存在以下关键差异：{}\n\n"
                "直接拼接可能会失败或花屏。建议先点「4. 重编码片头」对齐参数。\n\n"
                "是否仍要继续？".format("、".join(diffs)),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # 使用当前设置的保存位置（默认桌面「合成视频」文件夹），不存在则自动创建
        output_folder = self.output_dir
        os.makedirs(output_folder, exist_ok=True)

        total = len(self.materials)

        # 用窗口内嵌的进度条（不用弹出式对话框，避免重绘冲突导致闪退）
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在合成 0/{} ...".format(total))
        self.progress_bar.show()
        self.progress_label.show()

        # 合成期间显示「暂停 / 终止」按钮
        self.pause_btn.show()
        self.stop_btn.show()
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self._concat_paused = False
        self._concat_cancelled = False

        # 把左侧栏滚到底：屏幕小的用户看不到进度条，会以为软件死机了
        self._scroll_left_to_bottom()

        # 合成期间禁用按钮，防止重复点击
        self._set_busy(True)

        # 记录开始时间（暂停时长会被扣掉，见 PauseTracker）
        self._pause_tracker.start()
        self._concat_start = time.monotonic()

        # 在后台线程里跑 ffmpeg，界面不会卡死
        self._output_folder = output_folder
        self.thread = QThread(self)
        # 同时处理数从设置菜单读（1=串行；>1 并发，进阶用户用）
        encode = getattr(self, "encode_settings", None) or {}
        try:
            workers = max(1, int(encode.get("max_workers", "1")))
        except (TypeError, ValueError):
            workers = 1
        self.worker = ConcatWorker(self.head_path, self.materials, output_folder,
                                   max_workers=workers)
        self.worker.moveToThread(self.thread)

        # 两个信号都连接到 self 的方法（不是 lambda），Qt 会自动把它们切回主线程执行
        self.worker.progress.connect(self._on_concat_progress)
        self.worker.finished.connect(self._on_concat_finished)

        self.thread.started.connect(self.worker.run)
        self.thread.start()

    def _on_concat_progress(self, current, total, filename, success, message):
        """后台线程发来的进度，更新进度条和文字"""
        self.progress_bar.setValue(current)
        name = os.path.basename(filename)
        state = "成功" if success else "失败"
        self.progress_label.setText(
            "正在合成 {}/{}：{} {}".format(current, total, name, state)
        )
        self.status.setText(
            "合成中 {}/{}：{} {}".format(current, total, name, state)
        )

    def _on_concat_finished(self, success_count, fail_list):
        """合成结束：收尾清理 + 报告结果（此方法一定在主线程执行）"""
        output_folder = self._output_folder

        # 总耗时（不含暂停时长）
        elapsed = self._pause_tracker.elapsed()
        elapsed_str = _format_duration(elapsed)

        # 先停线程、恢复按钮。
        # 注意：这里只 quit+wait 让线程安全停止，不主动 deleteLater，
        # 因为跨线程 deleteLater 容易产生悬空引用；线程对象保留引用，
        # 下次合成时会自然被新的线程覆盖。
        self.thread.quit()
        self.thread.wait()
        self._set_busy(False)

        self.progress_bar.hide()
        self.progress_label.hide()
        self.pause_btn.hide()
        self.stop_btn.hide()

        if success_count == -1:
            if getattr(self, "_concat_cancelled", False):
                self._concat_cancelled = False
                QMessageBox.information(
                    self, "已终止",
                    "批量合成已终止。\n已完成的成品保留在：{}".format(output_folder))
                self.status.setText("合成已终止")
            elif fail_list and fail_list[0][0] == "内部错误":
                QMessageBox.critical(self, "错误", fail_list[0][1])
                self.status.setText("合成出错")
            return

        # 源文件 = 片头 + 全部素材；输出文件 = 本批成功的成品（worker 带回来的路径）
        src_mb = total_size_mb([p for p in [self.head_path] + list(self.materials) if p])
        out_mb = total_size_mb(getattr(self.worker, "outputs", []))

        # ⚠️ 拼接这一批**不进编码参考记录**（用户要求）：
        # 编码参考是给「字幕烧录」做参照用的（烧录是 CPU 密集、耗时长，
        # 记录不同设置下的耗时才有意义）；而片头拼接是 -c copy 流复制，
        # 几秒钟就完事，记进去只会污染参考表、拉低信噪比。
        # 所以这里既不传 on_record（结束弹窗不显示记录按钮），
        # 也不调 notify_batch（编码参考窗口的「记录最近一批」也拿不到这批数据）。
        show_batch_done(
            self,
            "合成完成" if not fail_list else "合成完成（部分失败）",
            success_count, len(fail_list), elapsed_str, output_folder,
            src_mb=src_mb, out_mb=out_mb,
            fail_list=fail_list or None,
        )
        self.status.setText(
            "合成结束：{} 成功，{} 失败（耗时 {}）".format(
                success_count, len(fail_list), elapsed_str
            )
        )

    def _record_last_batch(self):
        """把本批数据写进「编码参考」，返回记录总数（供弹窗显示）。

        ⚠️ 拼接 tab 目前**不会走到这里**（用户要求拼接批次不进编码参考）。
        方法保留是为了兼容外部调用 / 将来需要时能一键恢复。
        """
        if not self._last_batch or self.record_batch is None:
            return None
        return self.record_batch(self._last_batch)

    def _on_concat_pause(self):
        """暂停/继续：挂起或恢复当前正在跑的 ffmpeg 进程。"""
        if not hasattr(self, "worker") or self.worker is None:
            return
        if not self._concat_paused:
            self.worker.pause()
            self._concat_paused = True
            self._pause_tracker.pause()      # 暂停时长不计入耗时
            self.pause_btn.setText("继续")
            self.status.setText("已暂停（当前视频停在原地）")
        else:
            self.worker.resume()
            self._concat_paused = False
            self._pause_tracker.resume()
            self.pause_btn.setText("暂停")
            self.status.setText("继续合成")

    def _on_concat_stop(self):
        """终止：杀掉当前 ffmpeg 并停止后续文件。"""
        if not hasattr(self, "worker") or self.worker is None:
            return
        self._concat_cancelled = True
        self.worker.cancel()
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.status.setText("正在终止…")

    def _set_busy(self, busy):
        """合成/重编码素材期间禁用/启用所有按钮"""
        for btn in (self.head_btn, self.files_btn, self.folder_btn,
                    self.rename_btn, self.compare_btn, self.reencode_btn,
                    self.reencode_mat_btn, self.concat_btn, self.out_dir_btn,
                    self.clear_btn):
            btn.setEnabled(not busy)


def _selftest_skip_gui():
    """自检结束时的「自检完成」弹窗，要不要跳过。

    🔴 为什么必须有这个判断：`QMessageBox.information()` 是**模态**的 ——
       它会开自己的事件循环，**一直等人点「确定」**。CI / 无人值守环境里
       没有任何人可点，进程就永久卡死。特别注意：`QT_QPA_PLATFORM=offscreen`
       只是「渲染到一块假屏幕」，**并不解除模态阻塞**，指望它没用。

       2026-09-16 的实证：CI 两次都卡在这里，各烧满 45 分钟被超时取消，
       .app 其实早打好了却传不上去 —— 因为后面的上传步骤根本没机会跑。

    跳过它对结论毫无影响：报告本来就 print 到 stdout、也写进《自检报告.txt》，
    打包脚本判成败靠 grep 日志 —— 弹窗纯粹是给「本机手动跑」的人看的。

    返回 True = 跳过弹窗。
    """
    if os.environ.get("VIDEOTOOL_SELFTEST_NOGUI"):
        return True
    # Qt 明确要求离屏/最小平台插件 → 屏幕前一定没有真人
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in ("offscreen", "minimal"):
        return True
    # 输入输出都没接终端（CI 的典型情形）→ 一定是自动化流程
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return True
    except Exception:
        return True
    return False


def _ff_capabilities(ff):
    """探测 ffmpeg 有没有「烧字幕」必需的滤镜。

    🔴 为什么需要它：ffmpeg 的 `drawtext` 依赖编译时的 libfreetype，
    `subtitles` 依赖 libass。**有的 ffmpeg 编译时没开这两项，于是根本没有这些滤镜**
    —— 字幕烧录整个功能是坏的，但 `ffmpeg -version` 完全正常，
    自检如果只跑 -version 就会一路绿灯。2026-09-17 就是这样漏掉一个坏包。

    返回 {"drawtext": bool, "subtitles": bool}
    """
    caps = {"drawtext": False, "subtitles": False}
    try:
        r = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True,
                           timeout=30, **paths.spawn_kwargs())
        txt = (r.stdout or b"").decode("utf-8", "replace")
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] in caps:
                caps[parts[1]] = True
    except Exception as e:
        # ⚠️ 不静默：探测本身失败（路径不对 / 权限 / 变量没定义）必须留痕，
        #    否则会被读成「滤镜缺失」，把人引到完全错误的方向。
        caps["_error"] = "{}: {}".format(type(e).__name__, e)[:150]
    return caps


def _selftest_burn(ff):
    """真的烧一帧来验证 drawtext 端到端可用。

    用 `-f null -` 当输出，**不需要编码器**，所以很快（约 1 秒），
    只测「滤镜图能不能建起来」这一件事 —— 那正是 drawtext 缺失会挂的地方。

    返回 (ok, msg)。
    """
    font = paths.default_font()[1]
    if not font:
        return True, "（这台机器没有中文字体，跳过试烧）"
    vf = ("drawtext=fontfile='{}':text='广审':fontsize=24:"
          "x=10:y=10:fontcolor=white".format(
              font.replace("\\", "/").replace(":", "\\:")))
    try:
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
             "-vf", vf, "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=60, **paths.spawn_kwargs())
        if r.returncode != 0:
            tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            return False, (tail[-1][:120] if tail else "ffmpeg 返回 %d" % r.returncode)
        return True, ""
    except Exception as e:
        return False, repr(e)[:120]


def _selftest():
    """诊断模式：命令行加 `--selftest` 时，检查关键路径和依赖，写出报告。

    用途：打包成 exe 之后没有控制台，出问题是「双击没反应」。
    跑一次 `视频工具.exe --selftest`，程序目录下会生成《自检报告.txt》，
    里面写清楚 ffmpeg 找到没有、预设读到几个、Qt 模块是否齐全。
    """
    import subprocess

    lines = ["视频工具 自检报告",
             "时间              : " + time.strftime("%Y-%m-%d %H:%M:%S"),
             "平台              : {} / {}".format(sys.platform, platform.machine()),
             "frozen(是否打包)  : " + str(paths.is_frozen()),
             "程序目录          : " + paths.app_dir(),
             "内置资源目录      : " + paths.bundle_dir(),
             "优先级设置        : " + ("支持（Windows）" if paths.priority_supported()
                                       else "不支持（本平台无此项，界面已隐藏）"),
             "-" * 58]

    # 🔴 bad 必须在这里先定义：下面「ffmpeg 滤镜能力」那一段就会往它里面 append，
    #    原来定义在模块检查之前 → 滤镜缺失时直接 UnboundLocalError，整个自检崩掉，
    #    而 CI 里「.app 自检」排在「上传 .app 成品」之前且不容错 → 会挡住 .app 上传。
    #    （2026-09-17 实测）
    bad = []

    ff = paths.find_ffmpeg()
    fp = paths.find_ffprobe()
    lines.append("ffmpeg            : {} [{}]".format(
        ff, "存在" if os.path.isfile(ff) else "**缺失**"))
    lines.append("ffprobe           : {} [{}]".format(
        fp, "存在" if os.path.isfile(fp) else "**缺失**"))
    for name, exe in (("ffmpeg", ff), ("ffprobe", fp)):
        if os.path.isfile(exe):
            try:
                out = subprocess.run([exe, "-version"], capture_output=True,
                                     timeout=15, **paths.spawn_kwargs())
                first = (out.stdout or b"").decode("utf-8", "replace").splitlines()
                lines.append("  {} 版本        : {}".format(
                    name, first[0][:70] if first else "(无输出)"))
            except Exception as e:
                lines.append("  {} 无法运行    : {}".format(name, e))
    # ── ffmpeg 的「滤镜能力」+ 真烧一帧 ────────────────────────────────
    # 🔴 只跑 `-version` **测不出**这个缺陷：缺 drawtext 的 ffmpeg 版本号一切正常，
    #    但烧字幕必失败。必须查滤镜表，并真烧一帧。（2026-09-17 的真实漏网。）
    if os.path.isfile(ff):
        _caps = _ff_capabilities(ff)
        if _caps.get("_error"):
            lines.append("滤镜探测出错      : " + str(_caps["_error"]))
        for _nm in ("drawtext", "subtitles"):
            lines.append("滤镜 {:<13}: {}".format(_nm, "OK" if _caps.get(_nm) else "**缺失**"))
            if not _caps.get(_nm):
                bad.append("ffmpeg 缺 %s 滤镜" % _nm)
        if _caps.get("drawtext"):
            # 🔴 这一步出任何意外都不许让整个自检崩掉 ——
            #    CI 上 .app 自检失败会挡住「上传 .app 成品」（自检在步骤 11，上传在 14）。
            try:
                _bok, _bmsg = _selftest_burn(ff)
            except Exception as _e:
                _bok, _bmsg = False, "试烧过程异常 {}: {}".format(
                    type(_e).__name__, _e)
            lines.append("试烧一帧(drawtext): " + ("OK" if _bok else "**失败** " + _bmsg))
            if not _bok:
                bad.append("drawtext 试烧失败")

    pd = paths.preset_dir()
    lines.append("字幕预设目录      : {} [{}]".format(
        pd, "存在" if os.path.isdir(pd) else "**缺失**"))
    try:
        from subtitle import list_presets
        lines.append("读到预设数量      : {}".format(len(list_presets())))
    except Exception as e:
        lines.append("读取预设失败      : {}".format(e))

    lines.append("编码参考 json     : " + paths.encode_ref_path())
    lines.append("可写目录          : " + paths.writable_dir())
    lines.append("-" * 58)

    modules = [
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
        "PySide6.QtNetwork", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "fontTools.ttLib", "freetype", "ctypes",
    ]
    for mod in modules:
        try:
            __import__(mod)
            lines.append("模块 {:<28} OK".format(mod))
        except Exception as e:
            lines.append("模块 {:<28} **失败** {}".format(mod, e))
            bad.append(mod)

    # 试着真正建一个播放器（预览功能的核心）
    try:
        from PySide6.QtMultimedia import QMediaPlayer
        p = QMediaPlayer()
        lines.append("QMediaPlayer 实例化 : OK")
        del p
    except Exception as e:
        lines.append("QMediaPlayer 实例化 : **失败** {}".format(e))
        bad.append("QMediaPlayer")

    # 如果带了一个视频文件做参数，真的加载一次——验证 Qt 的多媒体插件
    # （ffmpegmediaplugin.dll）和它依赖的编解码 DLL 在打包后是否可用。
    video = None
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        if os.path.isfile(arg):
            video = arg
            break
    if video:
        lines.append("-" * 58)
        lines.append("试加载视频        : " + video)
        try:
            from PySide6.QtCore import QEventLoop, QTimer, QUrl
            from PySide6.QtMultimedia import QMediaPlayer
            from PySide6.QtWidgets import QApplication
            _app = QApplication.instance() or QApplication(sys.argv)
            _p = QMediaPlayer()
            _loop = QEventLoop()

            def _on_status(st):
                if st in (QMediaPlayer.MediaStatus.LoadedMedia,
                          QMediaPlayer.MediaStatus.BufferedMedia,
                          QMediaPlayer.MediaStatus.InvalidMedia):
                    _loop.quit()

            _p.mediaStatusChanged.connect(_on_status)
            _p.setSource(QUrl.fromLocalFile(video))
            QTimer.singleShot(15000, _loop.quit)   # 最多等 15 秒
            _loop.exec()
            st = _p.mediaStatus()
            name = {QMediaPlayer.MediaStatus.NoMedia: "NoMedia",
                    QMediaPlayer.MediaStatus.LoadingMedia: "LoadingMedia",
                    QMediaPlayer.MediaStatus.LoadedMedia: "LoadedMedia",
                    QMediaPlayer.MediaStatus.BufferedMedia: "BufferedMedia",
                    QMediaPlayer.MediaStatus.InvalidMedia: "InvalidMedia"}.get(st, str(st))
            lines.append("媒体状态          : {}".format(name))
            if st in (QMediaPlayer.MediaStatus.LoadedMedia,
                      QMediaPlayer.MediaStatus.BufferedMedia):
                lines.append("视频加载          : OK（多媒体插件工作正常）")
            else:
                lines.append("视频加载          : **异常**（预览可能不可用）")
                bad.append("视频加载")
        except Exception as e:
            lines.append("视频加载          : **失败** {}".format(e))
            bad.append("视频加载")

    lines.append("-" * 58)
    lines.append("结论              : " + ("全部正常" if not bad
                                          else "有问题 -> " + ", ".join(bad)))

    report = "\n".join(lines)
    out_path = os.path.join(paths.writable_dir(), "自检报告.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        out_path = "(写入失败)"
    try:
        print(report)
        # 🔴 立刻刷出去：万一后面卡住或被杀，日志里也一定能看到报告
        sys.stdout.flush()
    except Exception:
        pass
    # 需要看弹窗就正常跑；自动化测试可设 VIDEOTOOL_SELFTEST_NOGUI=1 跳过弹窗。
    # 🔴 但更要紧的是「非交互环境**自动**跳过」—— 见 _selftest_skip_gui() 的说明。
    if _selftest_skip_gui():
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        _app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(None, "自检完成", report + "\n\n报告已保存到：\n" + out_path)
    except Exception:
        pass


def _install_crash_handler():
    """兜底：未捕获的异常写进「崩溃日志.txt」并弹窗提示。

    打包成 exe 后是图形程序、没有控制台，一旦启动阶段就崩，用户只会看到
    「双击没反应」。这里把错误落到文件里，至少能查。
    """
    def hook(exc_type, exc, tb):
        import traceback
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log_path = os.path.join(paths.writable_dir(), "崩溃日志.txt")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                f.write(text)
        except Exception:
            log_path = "（日志写入失败）"
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None, "程序出错了",
                "程序遇到了一个错误，已经记到「崩溃日志.txt」。\n\n"
                + text[-1200:] + "\n\n日志位置：" + str(log_path))
        except Exception:
            pass

    sys.excepthook = hook


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    _install_crash_handler()
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


class MainWindow(QMainWindow):
    """主窗口：包含两个 tab（批量视频拼接、批量添加字幕）+ 顶部菜单

    用 QMainWindow 而不是 QWidget，确保 Windows 下标题栏、系统菜单、
    最大化/最小化/关闭按钮都正常显示，避免最大化后标题栏消失无法还原。"""

    # ffmpeg preset 选项（速度从快到慢）
    PRESET_OPTIONS = [
        "ultrafast", "superfast", "veryfast", "faster", "fast",
        "medium", "slow", "slower", "veryslow",
    ]
    # crf 常用选项（数值越小画质越高，文件越大）
    CRF_OPTIONS = [str(c) for c in (18, 20, 23, 25, 28)]
    # 优先级 3 档的中文名（与「设置 → 编码优先级」同一套 key：low/normal/high）。
    # 表里 / 下拉里一律显示中文，便于读表；导入时中英文都认（见导入别名表）。
    PRIORITY_CN_OPTIONS = ["低", "普通", "高"]
    # 英文 key → 中文（落盘 / 导入 / 下拉默认值 都走这一张表，保证口径唯一）
    PRIORITY_CN_MAP = {"low": "低", "normal": "普通", "high": "高"}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("视频批量处理工具")

        # 按屏幕可用区域设初始尺寸——避免初始窗口底部被任务栏挡住。
        # availableGeometry() 已扣掉任务栏/Dock，vs screenGeometry 是全屏。
        # 注意：**不能**用 setMaximumSize 来限制（哪怕设成屏幕尺寸也会让
        # Windows 把「最大化」按钮置灰）。改成在 resizeEvent / moveEvent 里
        # 动态把窗口夹回屏幕可用区域，这样最大化按钮始终可用。
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail:
            # 留 40px 边距，避免窗口贴边
            init_w = min(1100, avail.width() - 40)
            init_h = min(750, avail.height() - 40)
        else:
            init_w, init_h = 1100, 750
        self.resize(init_w, init_h)
        # 最小尺寸：避免拖到左半屏时布局塌陷成一条竖线、操作不了
        # （3 栏布局下宽 < 820 时 left_scroll / file_list / preview 会争抢空间，
        #  整个 UI 看起来像「卡死」。这里给出安全下限。）
        # 主窗口最小尺寸：左侧操作区最小 360 + 间距 + 右侧表格至少 400 才够用
        self.setMinimumSize(900, 520)

        # 全局编码设置（被字幕 tab 读取）
        # max_workers = 同时处理的数量：1=串行（默认，最稳）；>1 给进阶用户提速用
        # 上次用过的值存在 config.json 里，下次打开程序直接沿用
        saved = app_config.load()
        self.encode_settings = {
            "preset": saved.get("preset") or app_config.DEFAULTS["preset"],
            "crf": saved.get("crf") or app_config.DEFAULTS["crf"],
            "max_workers": str(saved.get("max_workers") or app_config.DEFAULTS["max_workers"]),
            "priority": (saved.get("priority") or app_config.DEFAULTS["priority"]),
        }
        # 把优先级设进 paths 的全局变量 —— 之后所有路径上的 ffmpeg 调用
        # （concat / subtitle / reencode）都会自动带上，不用层层传参。
        # ⚠️ 必须在任何批量任务启动之前调用，这里是最早的时机。
        paths.set_priority(self.encode_settings["priority"])
        # 最近一批的编码参考数据（弹窗关掉后，编码参考窗口里还能补记）
        self._last_batch = None
        self._pending_batch = None
        # 最近一批实际用的预设名：自动记录的「预设字幕」列用它，
        # 避免落盘 inherited_xxxx.ass 这种临时哈希名（见 _clean_subtitle_name）
        self._last_burn_preset_name = ""

        self.tabs = QTabWidget()
        self.concat_tab = ConcatTab()
        self.subtitle_tab = None  # 延迟导入以避免循环依赖

        self.tabs.addTab(self.concat_tab, "批量视频拼接")
        # ⚠️ 拼接 tab **不注入**编码参考记录回调（用户要求）：
        # 编码参考是给字幕烧录做参照的，拼接是 -c copy 流复制、几秒就完，
        # 记进去只是噪音。所以不设 record_batch / notify_batch（保持默认 None），
        # 结束弹窗就不会出现「记录本批到编码参考」按钮，
        # 编码参考窗口的「记录最近一批」也拿不到拼接数据。
        concat_out = saved.get("concat_output_dir")
        if concat_out and os.path.isdir(concat_out):
            self.concat_tab.set_output_dir(concat_out, remember=False)

        # 延迟加载字幕 tab（避免循环引用）
        try:
            from subtitle_tab import SubtitleTab
            self.subtitle_tab = SubtitleTab()
            self.tabs.addTab(self.subtitle_tab, "批量添加字幕")
            # 把全局设置传过去（同一个引用，菜单改 → tab 读到的也变）
            self.subtitle_tab.encode_settings = self.encode_settings
            self.subtitle_tab.refresh_encode_hint()
            # 注入「写进编码参考」的回调 + 恢复上次的保存位置
            self.subtitle_tab.record_batch = self.record_encode_ref_batch
            self.subtitle_tab.notify_batch = self._on_batch_finished
            burn_out = saved.get("burn_output_dir")
            if burn_out and os.path.isdir(burn_out):
                self.subtitle_tab.set_output_dir(burn_out, remember=False)
        except Exception as e:
            # 加载失败时给个提示 tab
            err_widget = QLabel("字幕 tab 加载失败：{}".format(e))
            err_widget.setWordWrap(True)
            self.tabs.addTab(err_widget, "批量添加字幕（不可用）")

        # 拼接 tab 也读全局设置（同时处理数）
        self.concat_tab.encode_settings = self.encode_settings

        # QMainWindow 的标准布局：顶部菜单栏 + 中央区域放 tab
        self.setMenuBar(self._build_menu_bar())
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(self.tabs)
        self.setCentralWidget(central)

        # 默认窗口大小：给左右两栏都留足空间，避免启动时左侧被压缩。
        # 用前面按屏幕算出的 init_w/init_h（已保证不超出任务栏上方）。
        self.resize(init_w, init_h)

        # 全窗口兜底：把所有下拉框/数字框设成禁止滚轮改值。
        # 主控件已用 NoWheelCombo/NoWheelSpin 创建，这里是保险 ——
        # 将来谁新加一个普通 QComboBox 也不会出现「滚轮翻页改参数」。
        disable_wheel_recursive(self)

    # ---------- 窗口尺寸夹取（不超出屏幕可用区域） ----------
    def _clamp_to_available_screen(self):
        """把窗口夹回当前屏幕的可用区域（已扣任务栏）。

        - 最大化/全屏时不干预（Windows 自己会避开任务栏）
        - 手动拉伸过大 / 拖到屏幕下方时，把尺寸和位置夹回来，
          避免底部被任务栏挡住、看不到控件
        - 不用 setMaximumSize 是因为那会让「最大化」按钮被置灰
        """
        if self.isMaximized() or self.isFullScreen():
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        geo = self.geometry()
        new_w = min(geo.width(), avail.width())
        new_h = min(geo.height(), avail.height())
        # 位置：保证整窗都在可用区域内（右下不越界，左上不越界）
        new_x = max(avail.left(), min(geo.left(), avail.right() - new_w + 1))
        new_y = max(avail.top(), min(geo.top(), avail.bottom() - new_h + 1))
        if (new_w, new_h, new_x, new_y) != (geo.width(), geo.height(), geo.left(), geo.top()):
            self.setGeometry(new_x, new_y, new_w, new_h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_to_available_screen()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._clamp_to_available_screen()

    def _build_menu_bar(self):
        """顶部菜单栏：设置（preset + crf）"""
        menubar = QMenuBar()
        settings_menu = menubar.addMenu("设置")
        action = settings_menu.addAction("编码设置...")
        action.triggered.connect(self._open_settings_dialog)
        # 右侧辅助显示当前设置。
        #
        # ⚠️ 这里踩过坑：以前是写死一串 "preset=veryfast, crf=23, 并发=2"，
        #    结果 preset 选到 `veryfast`（8 字符，比 `fast` 多 4 个）时，
        #    整串文字变长，末尾的「并发=N」被窗口右边缘裁掉 —— 用户看到
        #    的是「并发」后面什么都没有，看起来像功能坏了。
        #    窗口标题栏上的 label 是 setCornerWidget 放上去的，**不会自己
        #    换行、也不会省略**，超出去就直接看不见。
        #
        # 解决：把「显示什么」交给 _update_menu_hint() 按**真实字体宽度**算，
        #      窗口变窄 / preset 名变长时自动降级，**永远优先保住并发值**。
        self._menu_hint = QLabel("")
        self._menu_hint.setStyleSheet("color: #888; padding-right: 12px;")
        menubar.setCornerWidget(self._menu_hint, Qt.TopRightCorner)
        # 初次填充（此时窗口还没显示，宽度可能是默认值，显示后 resizeEvent 会再算一次）
        QTimer.singleShot(0, self._update_menu_hint)
        return menubar

    def _menu_hint_text(self, avail):
        """按可用宽度决定右上角提示显示什么，返回最合适的一档。

        档位（从详细到简略），**核心原则：并发值最后才牺牲**：
            1. "  preset=veryfast, crf=23, 并发=2"   完整
            2. "  veryfast · crf23 · 并发2"          去掉冗长前缀
            3. "  并发 2"                            只留并发
            4. "  x2"                                极窄时的兜底
        """
        p = str(self.encode_settings.get("preset", ""))
        c = str(self.encode_settings.get("crf", ""))
        w = str(self.encode_settings.get("max_workers", "1"))
        cands = [
            "  preset={}, crf={}, 并发={}".format(p, c, w),
            "  {} · crf{} · 并发{}".format(p, c, w),
            "  并发 {}".format(w),
            "  x{}".format(w),
        ]
        fm = self._menu_hint.fontMetrics()
        for t in cands:
            if fm.horizontalAdvance(t) + 16 <= avail:      # 16 = padding 余量
                return t
        return cands[-1]                                   # 再窄也得让用户看见并发

    def _update_menu_hint(self):
        """重算右上角提示文字（窗口缩放 / 设置变更时调用）。"""
        if not hasattr(self, "_menu_hint") or self._menu_hint is None:
            return
        # 可用宽度 = 窗口宽 − 左侧菜单部分占的宽度 − 一点安全余量。
        # 菜单栏 cornerWidget 能用多少，只有在窗口布局完成后才知道，
        # 所以这里保守地按「窗口宽的一半」为上限，避免把菜单项挤没。
        avail = max(84, int(self.width() * 0.5) - 40)
        self._menu_hint.setText(self._menu_hint_text(avail))

    def resizeEvent(self, event):
        """窗口尺寸变化 → 重算右上角提示（避免长 preset 名把并发值挤出窗口）。"""
        super().resizeEvent(event)
        self._update_menu_hint()

    def _open_settings_dialog(self):
        """打开设置对话框：preset + crf"""
        dlg = QDialog(self)
        dlg.setWindowTitle("编码设置")
        dlg.resize(360, 210 if paths.priority_supported() else 150)
        #                     ↑ 有「编码优先级」一行时 210；macOS 没这行，回到 150
        form = QFormLayout(dlg)

        preset_combo = NoWheelCombo()
        preset_combo.addItems(self.PRESET_OPTIONS)
        preset_combo.setCurrentText(self.encode_settings["preset"])
        # 旁边提示
        preset_hint = QLabel("（越慢画质越好、文件越小，编码越耗时）")
        preset_hint.setStyleSheet("color: #888; font-size: 11px;")
        preset_row = QVBoxLayout()
        preset_row.addWidget(preset_combo)
        preset_row.addWidget(preset_hint)

        crf_combo = NoWheelCombo()
        crf_combo.addItems(self.CRF_OPTIONS)
        crf_combo.setCurrentText(self.encode_settings["crf"])
        crf_hint = QLabel("（越小画质越好、文件越大；18 接近无损，23 默认，28 较差）")
        crf_hint.setStyleSheet("color: #888; font-size: 11px;")
        crf_row = QVBoxLayout()
        crf_row.addWidget(crf_combo)
        crf_row.addWidget(crf_hint)

        form.addRow("编码速度 (preset)：", preset_row)
        form.addRow("画质 (crf)：", crf_row)

        # —— 同时处理数量（并发）：普通用户保持 1 即可；这是给进阶用户的提速选项 ——
        worker_combo = NoWheelCombo()
        worker_combo.addItem("1（串行 · 默认，最稳）", "1")
        worker_combo.addItem("2 个同时处理", "2")
        worker_combo.addItem("3 个同时处理", "3")
        worker_combo.addItem("4 个同时处理", "4")
        cur_wk = str(self.encode_settings.get("max_workers", "1"))
        idx = worker_combo.findData(cur_wk)
        worker_combo.setCurrentIndex(idx if idx >= 0 else 0)
        worker_hint = QLabel(
            "（进阶选项，一般不用动：批量拼接是磁盘拷贝型，固态硬盘 SSD 开 2~3 明显提速；\n"
            "烧字幕是 CPU 密集型，核多的电脑开 2~3 才可能有提升。开太大反而互相抢资源变慢。）")
        worker_hint.setWordWrap(True)
        worker_hint.setStyleSheet("color: #888; font-size: 11px;")
        worker_row = QVBoxLayout()
        worker_row.addWidget(worker_combo)
        worker_row.addWidget(worker_hint)
        form.addRow("同时处理数：", worker_row)

        # —— 子进程优先级：用户反馈「高优先级虽然快，但期间占满资源，没法同时干别的」——
        # 所以给出 3 档让别人自己权衡：赶片子的选「高」，边干活边跑的选「低」。
        #
        # ⚠️ macOS 上**整行隐藏**：macOS 没有 Windows 的「进程优先级类」，
        #    subprocess 也不接受 creationflags（传了直接抛异常）。与其摆一个
        #    「选了其实没用」的下拉，不如干脆不显示 —— 免得用户以为设了「高」
        #    就该更快，结果毫无变化还来问是不是坏了。
        #    判断走 paths.priority_supported()（单一事实来源）。
        if paths.priority_supported():
            prio_combo = NoWheelCombo()
            for _key in ("low", "normal", "high"):
                _cls, _desc = paths.PRIORITY_LEVELS[_key]
                prio_combo.addItem(_desc, _key)
            _cur_p = str(self.encode_settings.get("priority") or paths.DEFAULT_PRIORITY)
            _pi = prio_combo.findData(_cur_p)
            prio_combo.setCurrentIndex(_pi if _pi >= 0 else 1)
            prio_hint = QLabel(
                "（给 ffmpeg 分配多少 CPU 优先权。选「高」出片最快，但期间电脑会明显变卡、\n"
                "别的大程序也可能卡住；选「低」对前台几乎无感，代价是批量耗时变长。）")
            prio_hint.setWordWrap(True)
            prio_hint.setStyleSheet("color: #888; font-size: 11px;")
            prio_row = QVBoxLayout()
            prio_row.addWidget(prio_combo)
            prio_row.addWidget(prio_hint)
            form.addRow("编码优先级：", prio_row)
        else:
            # 非 Windows：没有这个设置项，保存时也不去动 priority 字段。
            prio_combo = None

        # —— 编码参考：手动记录每次批量编码的实际数据（文件数/大小/设置/耗时），
        #    方便把自己的真实数据留给别人当参考 ——
        ref_btn = QPushButton("查看 / 添加编码参考记录...")
        ref_btn.clicked.connect(lambda: self._open_encode_reference_dialog(dlg))
        ref_hint = QLabel("（手动记录：烧了多少个文件、多大、用的什么设置、耗时多久，供他人参考）")
        ref_hint.setStyleSheet("color: #888; font-size: 11px;")
        ref_row = QVBoxLayout()
        ref_row.addWidget(ref_btn)
        ref_row.addWidget(ref_hint)
        form.addRow("编码参考：", ref_row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        disable_wheel_recursive(dlg)      # 滚轮防误改（编码速度/画质/并发）
        if dlg.exec() == QDialog.Accepted:
            self.encode_settings["preset"] = preset_combo.currentText()
            self.encode_settings["crf"] = crf_combo.currentText()
            self.encode_settings["max_workers"] = worker_combo.currentData() or "1"
            # ⚠️ macOS 上没有优先级下拉（prio_combo = None）→ 保留原值，别写空。
            if prio_combo is not None:
                self.encode_settings["priority"] = (
                    prio_combo.currentData() or paths.DEFAULT_PRIORITY)
                # 立即生效（paths 的全局变量，下次启动 ffmpeg 就用新优先级）
                paths.set_priority(self.encode_settings["priority"])
            # 记住这次的编码设置（下次打开程序直接沿用）
            app_config.update(preset=self.encode_settings["preset"],
                              crf=self.encode_settings["crf"],
                              max_workers=self.encode_settings["max_workers"],
                              priority=self.encode_settings["priority"])
            self._update_menu_hint()
            if self.subtitle_tab is not None:
                # 状态栏文案：macOS 上没有优先级设置，就不提这一项
                if paths.priority_supported():
                    _msg = "编码设置已更新：preset={}, crf={}, 并发={}, 优先级={}".format(
                        self.encode_settings["preset"], self.encode_settings["crf"],
                        self.encode_settings["max_workers"],
                        self.PRIORITY_CN_MAP.get(
                            self.encode_settings["priority"],
                            self.encode_settings["priority"]))
                else:
                    _msg = "编码设置已更新：preset={}, crf={}, 并发={}".format(
                        self.encode_settings["preset"], self.encode_settings["crf"],
                        self.encode_settings["max_workers"])
                self.subtitle_tab.status.setText(_msg)
                self.subtitle_tab.refresh_encode_hint()

    # ---------------- 编码参考（手动记录，存 JSON，程序体积不受影响） ----------------

    # 编码参考记录 json：放在「程序目录」（打包后 = exe 同级），可读可写
    ENCODE_REF_PATH = paths.encode_ref_path()
    # 归一化临时 .ass 的存放目录（subtitle_tab._build_inherited_ass 生成）。
    # 自动记录时要把落在这个目录里的临时路径**还原成预设名**，
    # 否则「预设字幕」列会出现 inherited_523714c 这种没人看得懂的名字。
    _INHERITED_TMP_DIR = os.path.join(tempfile.gettempdir(), "video_tool_ass")
    ENCODE_REF_HINT = ("⚠️ 编码速度受电脑性能影响，以下数据仅供参考。\n"
                       "记录方式：手动添加，写你自己一次真实批量编码的数据（可以写多条对比）。")

    def _clean_subtitle_name(self, raw, batch=None):
        """把「预设字幕」值归一成**人能看懂的名字**。

        ⚠️ 这是个真 bug 的修复（截图里第 7 行）：
          选了预设之后，`subtitle_tab._get_settings()` 会走「统一管线」把
          `ass_path` 换成归一化的**临时文件**
          （`%TEMP%\\video_tool_ass\\inherited_xxxx.ass`）。自动记录若直接取
          `os.path.basename(ass_path)`，表里就出现「inherited_523714c」——
          用户根本认不出这是哪个预设。这里按顺序还原：
            0) 批快照里显式带了 `preset_name`/`preset_display` → 直接用（最可靠）
            1) 路径在预设目录里 → 用文件名（正常预设，原样保留）
            2) 路径是临时归一化文件（或名以 inherited_ 开头）→ 回退到
               **本次实际选中的预设名**；取不到就写「继承预设」
            3) 其余（手动输入 / 无）原样返回
        batch: 可选，本批快照 dict（自动记录时传，用来拿 preset_name）。
        """
        raw = (raw or "").strip()
        # 0) 快照里已有明确预设名 → 优先（不依赖临时路径推断）
        if batch:
            for k in ("preset_name", "preset_display"):
                v = (batch.get(k) or "").strip()
                if v:
                    return v
        if not raw:
            return "手动输入"
        base = os.path.basename(raw)
        # 1) 临时归一化文件 → 还原成预设名
        if base.lower().startswith("inherited_") or \
                os.path.normcase(os.path.dirname(raw)) == \
                os.path.normcase(self._INHERITED_TMP_DIR):
            name = (getattr(self, "_last_burn_preset_name", "") or "").strip()
            if not name and self.subtitle_tab is not None:
                # 兼容：从「继承来源 .ass」路径取预设名，再退到当前下拉框
                src = getattr(self.subtitle_tab, "_inherited_ass_source", None)
                if src:
                    name = os.path.basename(src)
                else:
                    try:
                        cb = self.subtitle_tab.preset_combo
                        if cb.currentIndex() > 0:
                            name = cb.currentText().strip()
                    except Exception:
                        name = ""
            return name or "继承预设"
        # 2) 真·预设文件 → 保留文件名
        return base or "手动输入"

    def _load_encode_ref(self):
        """读取编码参考记录（JSON 列表）。文件不存在/损坏时返回空列表。"""
        try:
            with open(self.ENCODE_REF_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_encode_ref(self, records):
        try:
            with open(self.ENCODE_REF_PATH, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", "编码参考记录写入失败：{}".format(e))

    def _on_batch_finished(self, rec):
        """某个 tab 刚跑完一批：记下快照，供「编码参考」窗口补记。"""
        self._pending_batch = rec

    def record_encode_ref_batch(self, rec):
        """把一批自动统计的数据写进「编码参考」，返回记录总条数。"""
        records = self._load_encode_ref()
        rec = dict(rec)
        rec.setdefault("note", "自动记录")
        # 归一「预设字幕」：subtitle_tab 传来的是预设名（preset_name），
        # 但为兼容旧快照 / 极端情况，还是过一遍 _clean_subtitle_name，
        # 保证落盘的值永远不是 inherited_xxxx.ass 这类临时文件名。
        _pn = (rec.get("preset_name") or "").strip()
        if _pn:
            self._last_burn_preset_name = _pn
        rec["subtitle"] = self._clean_subtitle_name(rec.get("subtitle", ""), rec)
        # 归一「优先级」→ 中文。**在落盘这一层做**（而不是只改 subtitle_tab）：
        # 手动添加、subtitle_tab 自动记录、以及以后别的调用方，传进来的可能是
        # 英文 key（low/normal/high）或已经是中文，统一在这里收敛成中文，
        # 参考表里就不会出现「低 / low / 」三种口径混排。
        # 老快照没这个字段 → 留空（不硬塞一个「普通」，那是编数据）。
        _pv = str(rec.get("priority") or "").strip()
        if _pv:
            rec["priority"] = self.PRIORITY_CN_MAP.get(
                _pv.lower(), _pv if _pv in self.PRIORITY_CN_OPTIONS else "")
        records.append(rec)
        self._save_encode_ref(records)
        self._pending_batch = None
        return len(records)

    def _open_encode_reference_dialog(self, parent=None):
        """编码参考窗口：表格展示历史记录 + 底部手动添加 / 删除选中。"""
        dlg = QDialog(parent or self)
        dlg.setWindowTitle("编码参考（手动记录）")
        # 默认宽 1080：10 列里「编码速度」要放得下 veryfast（138px）、
        # 「预设字幕」要放得下常见文件名，再给备注留点地方。
        dlg.resize(1080, 560)
        # 最小宽度：10 列要「参数名全部显示 + 数据不被截」约需 1020px 表格宽度，
        # 加上左右边距。给 1000px 保底，避免用户把窗口拖得很窄之后列名被
        # 省略号挡住（那会被误认为功能坏了）。拖到更窄时会退化到
        # 「保住列名，宁可出横向滚动条」（见 apply_widths()）。
        dlg.setMinimumWidth(1000)
        v = QVBoxLayout(dlg)

        hint = QLabel(self.ENCODE_REF_HINT)
        hint.setStyleSheet("color: #B8860B; font-weight: bold;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        cols = ["日期", "文件数", "源文件大小(MB)", "编码速度",
                "画质(crf)", "并发", "优先级", "预设字幕", "总耗时", "输出大小(MB)", "备注"]
        # 最左侧行号栏（vertical header）的参数名与宽度。
        # 用户要求：给它一个明确的参数名（原来是一片空白），宽度拉到能容纳 3 位数。
        # ⚠️ 宽度必须在这里（建表之前）算好：QHeaderView.setFixedWidth 一旦设过，
        #    后面再改需要重设，不如一次算准。
        from PySide6.QtGui import QFontMetrics as _QFM
        fm_vertical = _QFM(dlg.font())
        _VH_TITLE = "序号"
        _VH_W = max(48, fm_vertical.horizontalAdvance("000") + 16)  # 3 位数 + 呼吸空间
        # 这 5 列**各缩窄相同的量**，把宽度让给最左侧的行号栏 + 新增的「优先级」列：
        # 文件数(1) / 源文件大小(2) / 总耗时(8) / 输出大小(9) / 备注(10)。
        # 「相同的量」在 apply_widths() 里统一扣（DONOR_STRIP），且保证扣完之后
        # 这几列仍放得下列名和数据（不低于各自 floor）。
        DONOR_COLS = (1, 2, 8, 9, 10)
        # 行号栏原默认宽约 40px；新增的宽度就是需要让位的总量，各列均摊。
        # 「优先级」列（下拉，值只有「低/普通/高」）也在这里分一份 —— 它自己是
        # 新来的，不该让别的列多让，所以按它的实际所需宽度一并计入被让位的总量。
        _PRIO_NEED = fm_vertical.horizontalAdvance("普通") + 24 + 8   # 值 + 下拉chrome + 内边距
        _VH_GROW = max(0, _VH_W - 40) + _PRIO_NEED
        DONOR_STRIP = int(round(_VH_GROW / len(DONOR_COLS)))
        records = self._load_encode_ref()
        # 列索引 → 记录字典键（cellChanged / combo 改动时用来回写）
        COL_KEYS = ["date", "files", "src_mb", "preset", "crf",
                    "workers", "priority", "subtitle", "elapsed", "out_mb", "note"]
        # 这几列用下拉可选项：编码速度 / 画质 / 并发 / 优先级 / 预设字幕
        # （3=编码速度 4=画质 5=并发 6=优先级 7=预设字幕）
        COMBO_COLS = (3, 4, 5, 6, 7)

        # 用 _loading 标志跳过「初始填充 / 重建」阶段触发的 cellChanged / combo 信号，
        # 避免无谓保存或死循环。
        _loading = [True]

        # 可拖拽排序的表格（拖动行即可调整记录顺序）
        class EncodeRefTable(QTableWidget):
            def __init__(self, recs, on_reorder, parent=None):
                super().__init__(0, len(cols), parent)
                self._recs = recs
                self._on_reorder = on_reorder
                self.setDragEnabled(True)
                self.setDragDropMode(QTableWidget.InternalMove)
                self.setDropIndicatorShown(True)
                self.setSelectionBehavior(QTableWidget.SelectRows)
                self.setSelectionMode(QTableWidget.ExtendedSelection)
                self.setEditTriggers(QTableWidget.DoubleClicked
                                      | QTableWidget.EditKeyPressed)
                # ===== 最左侧的行号栏（vertical header）=====
                # 用户要求：① 给它一个**参数名**（不再是空白）；② 宽度加到
                # 能容纳 **3 位数**（如 120）；③ 宽度从 文件数/源文件大小/
                # 输出大小/备注/总耗时 这 5 列各缩窄**相同的量**腾出来。
                vh = self.verticalHeader()
                vh.setSectionResizeMode(QHeaderView.Fixed)
                vh.setDefaultAlignment(Qt.AlignCenter)
                vh.setFixedWidth(_VH_W)          # 按 3 位数定宽，见上方计算
                # 参数名：Qt 的 vertical header 没有标题区，标准做法是用左上角
                # 的「角落按钮」（corner button，横竖表头的交叉处）来承载。
                #
                # ⚠️ 这里踩过坑：角落按钮**默认是空的且带 3D 边框**，看起来就是
                #    「表格左上角有一块空白」——用户直接截图圈出来说「不应该存在」。
                #    必须 ① 写上参数名；② **去掉所有边框**（flat + 透明背景），
                #    否则它会像一块凸起的空白补丁。
                cb = self.findChild(QAbstractButton)
                if cb is not None:
                    cb.setText(_VH_TITLE)
                    cb.setToolTip("记录序号（按当前显示顺序）")
                    # findChild(QAbstractButton) 拿到的具体类型随 Qt 版本变
                    # （实测是 QAbstractButton 本身，没有 setFlat）→ 逐个探测。
                    if hasattr(cb, "setFlat"):
                        cb.setFlat(True)             # 去掉 3D 边框
                    cb.setAutoFillBackground(False)  # 不要额外底色，融入表头
                    cb.setStyleSheet(
                        "QAbstractButton { border: none; background: transparent;"
                        " font-weight: bold; color: palette(text); }")
                    cb.show()

            def dropEvent(self, event):
                sel = self.selectedIndexes()
                if not sel:
                    event.ignore()
                    return
                src = sel[0].row()
                target = self.rowAt(event.pos().y())
                if target < 0:
                    target = self.rowCount()   # 拖到表格下方 → 追加到末尾
                if target == src:
                    event.acceptProposedAction()
                    return
                rec = self._recs.pop(src)
                self._recs.insert(target, rec)
                event.acceptProposedAction()
                self._on_reorder()

        def _on_combo(row, col, text):
            """编码速度 / 画质 / 预设字幕 下拉改动 → 回写并保存。"""
            if _loading[0]:
                return
            if 0 <= row < len(records):
                records[row][COL_KEYS[col]] = text
                self._save_encode_ref(records)

        def _combo_opts(c):
            """某下拉列的可选项列表。"""
            if c == 3:
                return self.PRESET_OPTIONS          # 编码速度
            if c == 4:
                return self.CRF_OPTIONS             # 画质(crf)
            if c == 5:
                return ["1", "2", "3", "4"]         # 并发（同时处理数）
            if c == 6:
                # 优先级：与「设置 → 编码优先级」同一套 3 档（低 / 普通 / 高）。
                # 列里存中文，便于直接读表；导入时中英文都认（见导入别名表）。
                return self.PRIORITY_CN_OPTIONS
            if c == 7:
                opts = ["手动输入", "无"]
                try:
                    from subtitle import list_presets
                    opts += list(list_presets())
                except Exception:
                    pass
                return opts                        # 预设字幕
            return []

        def fill_row(r, row):
            for c, key in enumerate(COL_KEYS):
                if c in COMBO_COLS:
                    # 下拉可选项：只允许从列表里选，不允许手输不存在的参数
                    combo = NoWheelCombo()
                    opts = _combo_opts(c)
                    combo.addItems(opts)
                    cur = str(r.get(key, ""))
                    if cur and cur not in opts:
                        combo.addItem(cur)   # 兼容历史里万一存在的旧值
                    combo.setCurrentText(cur)
                    combo.setEditable(True)
                    le = combo.lineEdit()
                    le.setReadOnly(True)                       # 只能选，不能敲
                    if c == 7:
                        # 预设字幕的值往往很长，左对齐才能看清开头是哪个预设。
                        # 但 setCurrentText 会把光标放到末尾，窄的 QLineEdit 会自动
                        # 滚到光标处 → 反而只看到结尾（"幕1080P.ass"）。所以初始化
                        # 和每次改文本后都把光标拉回开头，稳定显示开头（"定轴高光…"）。
                        le.setAlignment(Qt.AlignLeft)
                        le.setStyleSheet("padding-left: 4px; padding-right: 4px;")
                        le.setCursorPosition(0)
                        le.deselect()
                    else:
                        le.setAlignment(Qt.AlignCenter)        # 文字居中
                        # 下拉箭头在右侧会吃掉一部分宽度，导致居中文字看起来偏左；
                        # 给编辑框加一点左内边距，把“视觉中心”往右挪，整体回到正中。
                        le.setStyleSheet("padding-left: 10px; padding-right: 4px;")
                    combo.currentTextChanged.connect(
                        lambda text, row=row, c=c: _on_combo(row, c, text))
                    if c == 7:
                        # 选别的预设后光标又跑到末尾，这里再拉回开头
                        combo.currentTextChanged.connect(
                            lambda _t, cb=combo: cb.lineEdit().setCursorPosition(0))
                    table.setCellWidget(row, c, combo)
                else:
                    item = QTableWidgetItem(str(r.get(key, "")))
                    item.setTextAlignment(Qt.AlignCenter)   # 数据居中
                    table.setItem(row, c, item)

        # 双击编辑普通文本列时，让编辑框里的文字也保持居中
        # （默认 QLineEdit 编辑框是左对齐的，会导致“改完变居左”）。
        class CenteredEditDelegate(QStyledItemDelegate):
            def createEditor(self, parent, option, index):
                editor = super().createEditor(parent, option, index)
                if isinstance(editor, QLineEdit):
                    editor.setAlignment(Qt.AlignCenter)
                return editor

        # 拖拽排序后的回调：这里只传一个「转调」的 lambda，
        # 真正的 rebuild/_reorder 定义在后面（它们依赖 apply_widths，
        # 而 apply_widths 又依赖 table 和 fm 都已就绪）。
        # 用 lambda 延迟查找名字，避免「函数还没定义就被当参数传进去」的
        # UnboundLocalError。lambda 只会在用户真拖拽时才被调用。
        table = EncodeRefTable(records, lambda: (_reorder(), self._save_encode_ref(records)))
        table.setHorizontalHeaderLabels(cols)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)  # 表头居中
        table.setItemDelegate(CenteredEditDelegate())                 # 编辑时居中
        # 列宽策略：先用「正好放下参数名」的宽度设置各固定列，固定列加起来
        # 超出视口时按优先级逐级收窄（源/输出 → 日期/总耗时 → 文件数），
        # 预设字幕 / 备注保持可拉伸（跟随剩余宽度）。
        fm = table.fontMetrics()

        def _name_fit(c, extra=26):
            """「刚好显示参数名」的宽度 = 表头文字宽 + 余量。"""
            t = table.horizontalHeaderItem(c).text()
            return max(48, fm.horizontalAdvance(t) + extra)

        def _date_min():
            """日期列的绝对下限：放得下「YYYY-MM-DD」（如 2026-09-11）。

            日期列被压到 75px 时会把「2026-09-11」截成「2026-09…」—— 这是真实
            数据被切掉，比列名截断严重得多。所以下限按「最宽的实际日期」算，
            再留 8px 给单元格内边距；记录为空时退回一个稳妥的固定值。

            NOTE: offscreen 测试环境没有中文字体，日期会量到 120px（虚高）；
            真实环境（微软雅黑）约 80px。这里是按实际被测字体算的，所以
            真实环境不会白白占 132px。
            """
            widest = 0
            for r in range(table.rowCount()):
                it = table.item(r, 0)
                if it is not None and it.text():
                    widest = max(widest, fm.horizontalAdvance(it.text()))
            return max(fm.horizontalAdvance("2026-00-00") + 8, widest + 8, 84)

        def _data_fit(c, extra=24, at_least=54):
            """按该列**实际数据**的最宽值算宽度（不看列名）。

            用于「源文件大小 / 输出大小 / 总耗时」这类列：它们的数据比列名窄，
            按列名给宽度会浪费几十像素，挤得别的列显示不全。
            列名放不下就走省略号（悬停有完整列名），优先保证数据看得见。
            取 max(兜底宽度, 最宽数据 + 余量)。
            """
            widest = 0
            for r in range(table.rowCount()):
                it = table.item(r, c)
                if it is not None and it.text():
                    widest = max(widest, fm.horizontalAdvance(it.text()))
            return max(at_least, widest + extra)

        def apply_widths():
            """按当前表格内容分配各列宽度（数据变了 / rebuild 后调用）。

            必须在 rebuild() **之后**调用：靠表格里的实际数据算宽的列，表还空时
            只有兜底值。

            ⚠️ 每列的「需要宽度」按列的性质定（用户要求：参数名全部显示 + 值能看见）：
              · 普通文本列：max(列名宽, 数据宽) + 内边距
              · **下拉列（编码速度 3 / 画质 4 / 并发 5 / 预设字幕 6）**：
                可编辑 QComboBox 里文字区 ≈ 列宽 - 42（箭头+内边距+边框），
                所以列宽要 = **值文字宽 + 42**，否则会出现「只看得到箭头、
                值被裁掉」——用户反馈的「编码速度 / 并发看不全」就是这个。
              · **文件数（列 1）**：内容是 1~3 位数字，用户要求这列宽一点，给足余量。
              · 日期：留得下 `YYYY-MM-DD` 即可，不为它多占地方。

            算法：**一次算清，不做迭代收窄**（迭代容易自相矛盾，踩过坑）。
              1. 每列算出「理想宽」与「紧身下限」（= 列名完整显示所需，参数名硬指标）；
              2. 固定列理想宽之和 <= 可用宽 → 直接给理想宽，剩下的全给 Stretch 列；
              3. 否则从「最不重要」的列开始砍到紧身下限，直到放得下；
              4. 还是放不下 → 全部取紧身下限，**让表格出横向滚动条**
                 （保证参数名完整，用户可横拉看全，比列名变「源文件…」强）。
            """
            PAD = 8
            # 可编辑 QComboBox 的「箭头 + 内边距 + 边框」占 **24px**（实测：
            # lineEdit 宽 = 列宽 - 24）。之前我按 42 估，每个下拉列多占 18px、
            # 四列共浪费 ~72px —— 正是「编码速度 / 画质 / 并发 看不全」的原因。
            COMBO_CHROME = 24
            # 再留 6px 给 lineEdit 内部左内边距，避免文字贴着箭头
            COMBO_PAD = 6
            COMBO_COLS = (3, 4, 5, 6, 7)

            header = table.horizontalHeader()

            def _name_w(c):
                return fm.horizontalAdvance(table.horizontalHeaderItem(c).text())

            # 各下拉列的「值」字符集：用来算列宽，保证这些值一定看得见。
            # ⚠️ 直接用本类真实的下拉项常量，别手抄 —— 我曾手抄漏了
            # `ultrafast` / `superfast`，结果「编码速度」列宽算了 126px
            # 而实际需要 138px，下拉值还是被裁（用户反馈「看不全」）。
            COMBO_SHORT = {
                3: self.PRESET_OPTIONS,          # 编码速度
                4: self.CRF_OPTIONS,             # 画质(crf)
                5: ["1", "2", "3", "4"],         # 并发
                6: self.PRIORITY_CN_OPTIONS,     # 优先级（低/普通/高，都很短）
                7: ["无", "手动输入"],            # 预设字幕（文件名可能很长，见下）
            }

            def _combo_short_values(c):
                v = 0
                for s in COMBO_SHORT.get(c, ()):
                    v = max(v, fm.horizontalAdvance(s))
                return v

            def _combo_values(c):
                """该下拉列所有下拉项 / 当前值里最宽的文字。"""
                widest = 0
                for r in range(table.rowCount()):
                    wdg = table.cellWidget(r, c)
                    if wdg is not None:
                        try:
                            for i in range(wdg.count()):
                                widest = max(widest,
                                             fm.horizontalAdvance(wdg.itemText(i)))
                            cur = wdg.currentText()
                            if cur:
                                widest = max(widest, fm.horizontalAdvance(cur))
                        except Exception:
                            pass
                return widest

            def _combo_text_need(c, use_all_items=False):
                """下拉列所需的列宽。

                use_all_items=False → 只按「常见短值」算（下限用，不许长文件名撑宽）
                use_all_items=True  → 按该列**全部**下拉项的显示值算（理想宽用，
                                      保证 ultrafast / 最长文件名都看得见）

                chrome（箭头+内边距+边框）不手写常数，用 sizeHint 自校准：
                chrome = sizeHint − 当前显示值文字宽。
                ⚠️ 必须取所有行的**最小值** —— 只要有一行当前值短（或为空），
                   单行算出来的 chrome 就会虚高（实测编码速度 cur='fast' 时
                   算出 100，真值只有 30），列会被撑到 198px。
                """
                # 自校准 chrome：取所有行里的最小值（真 chrome 是所有行的下界）
                chrome = None
                for r in range(table.rowCount()):
                    wdg = table.cellWidget(r, c)
                    if wdg is None or not hasattr(wdg, "count"):
                        continue
                    try:
                        cur = wdg.currentText()
                        if not cur:
                            continue
                        cur_w = fm.horizontalAdvance(cur)
                        if cur_w <= 0:
                            continue
                        v = max(0, wdg.sizeHint().width() - cur_w)
                        chrome = v if chrome is None else min(chrome, v)
                    except Exception:
                        pass
                if chrome is None:
                    chrome = COMBO_CHROME

                if use_all_items:
                    vals = list(COMBO_SHORT.get(c, ()))
                    for r in range(table.rowCount()):
                        wdg = table.cellWidget(r, c)
                        if wdg is None or not hasattr(wdg, "count"):
                            continue
                        try:
                            vals.extend(wdg.itemText(i) for i in range(wdg.count()))
                        except Exception:
                            pass
                    widest = max((fm.horizontalAdvance(v) for v in vals), default=0)
                else:
                    widest = _combo_short_values(c)
                return widest + chrome

            # 紧身下限（任何列都不能低于它 —— 参数名必须完整显示，硬指标）
            def _floor(c):
                f = _name_w(c) + PAD
                if c in COMBO_COLS:
                    # 下限只保证「常见短值」看得见（编码速度 fast/medium、
                    # 画质 18~28、并发 1~4）；字幕文件名太长，不按它要宽度。
                    # chrome 由 sizeHint 自校准，不手写常数。
                    f = max(f, _combo_text_need(c))
                if c == 1:
                    # 文件数：用户要求「刚好容纳 3 位数」（如 120）。
                    # 取 max(3位数字宽, 列名「文件数」宽) + 内边距，
                    # 保证 999 这种三位数一定显示完整、列名也不被截。
                    f = max(f, FILES_MIN)
                if c == 0:
                    # 日期：至少要放得下 YYYY-MM-DD（别为省像素把真实日期截了）
                    f = max(f, _date_min())
                if c == 8:
                    # 总耗时：至少要放得下 XX分XX秒
                    # （⚠️ 加了「优先级」列后，总耗时从 7 挪到 8 —— 这行没跟着改
                    #   会把下限加在**预设字幕**上，而总耗时列没了兜底，
                    #   窄窗口下「2分14秒」会被压到看不见。）
                    f = max(f, _data_fit(c, extra=PAD, at_least=84))
                return f

            def _ideal(c):
                """理想宽：列名 / 数据 / 下拉值 三者取大，再给些余量。"""
                v = max(_name_w(c) + PAD, _data_fit(c, extra=PAD, at_least=0))
                if c in COMBO_COLS:
                    # 理想宽按该列**全部**下拉项算（编码速度要放得下 ultrafast）。
                    # 不用 sizeHint：它受当前显示值/布局影响，同一列不同行会
                    # 给出 138 / 198 这种漂移值，反而把列撑宽。
                    v = max(v, _combo_text_need(c, use_all_items=True))
                if c == 1:
                    # 文件数：理想宽 = 3 位数再多给一位的余量（视口富余时更宽松）
                    v = max(v, FILES_3DIGIT + fm.horizontalAdvance("0"))
                if c == 0:
                    # 日期：只要放得下 YYYY-MM-DD，不按列名宽去铺
                    v = max(_name_w(c) + PAD, _data_fit(c, extra=PAD, at_least=0) or 84)
                if c == SUB_COL:
                    # 预设字幕的值是字幕文件名（可能很长，如
                    # 定轴高光广审字幕1080P.ass ≈ 288px）。**不能按最长文件名给宽度**
                    # —— 那一列就能吃掉 330px，把别的列全挤出视口（踩过）。
                    # 给一个上限，超出部分显示省略号（悬停有完整文件名）。
                    v = min(v, 190)
                return v

            FIXED = (0, 1, 2, 3, 4, 5, 6, 8, 9)
            SUB_COL, NOTE_COL = 7, 10
            # 文件数（列 1）：用户要求「刚好容纳 3 位数」（如 120），
            # 且明确说「可以把文件数和备注的宽度拉低提供给最左侧的记录数量」。
            # 不写死常数，按字体实测 3 位数字宽 + 单元格左右内边距算，
            # 换字体/换 DPI 也不会退化。
            # ⚠️ 光算数字宽不够：单元格是**居中**的，左右各要留白，
            #    否则 3 位数字会贴着边框看起来很挤（用户要的是「刚好容纳」= 舒服地放下）。
            FILES_PAD = 16                      # 居中单元格左右各 8px 呼吸空间
            FILES_3DIGIT = fm.horizontalAdvance("000") + FILES_PAD
            # 下限：列名「文件数」也要完整显示，取两者最大。
            FILES_MIN = max(FILES_3DIGIT, _name_w(1) + PAD)
            # 预设字幕的下限：列名 + 常见短值（**不按最长文件名**，文件名太长
            # 会把它自己撑到 230+px 并把别的列挤出视口）。超长文件名显示省略号。
            SUB_MIN = max(_name_w(SUB_COL) + PAD,
                          _combo_short_values(SUB_COL) + COMBO_CHROME + COMBO_PAD,
                          110)
            # 预设字幕的上限：再宽就是浪费。实测「字文件名」要 234px，
            # 但那是极端值（定轴高光广审字幕1080P.ass）；截断后悬停可看全文。
            SUB_MAX = 168
            # 备注：用户要求窄，且明确说「可以把备注的宽度拉低提供给最左侧的记录数量」。
            # 所以下限/上限都收——它的空间优先让给「文件数」列。
            NOTE_MIN = 90
            NOTE_MAX = 130

            try:
                viewport_w = table.viewport().width()
            except Exception:
                viewport_w = 0

            # —— 按比例铺满整张表 ——
            # 目标（用户要求）：① 不浪费右侧空白；② 缩放窗口时各列**等比**变化。
            #
            # 做法：
            #   1. 每列先算 floor（列名/值都放得下的最小宽度，硬底线）；
            #   2. 若 Σfloor <= 可用宽 → 把剩下的宽度（gap）按各列「权重」
            #      等比分配下去，使 Σ 恰好 = 可用宽（右侧不再留白）；
            #   3. 若 Σfloor > 可用宽 → 只能保 floor，让表格出横向滚动条
            #      （保参数名完整，这是硬指标）。
            #
            # 权重 = 该列理想宽 / Σ理想宽。用理想宽当权重是因为它天然反映了
            # 各列的「内容体量」（源文件大小列要 116px，并发列只要 42px），
            # 等比放大后各列仍按内容比例增长，不会出现「并发列被撑成巨宽」。
            ALL_COLS = list(range(len(cols)))
            floors = {c: _floor(c) for c in ALL_COLS}
            ideals = {c: _ideal(c) for c in ALL_COLS}


            # 预设字幕 / 备注虽不在 FIXED 里，也要按 floor/ideal 参与分配，
            # 否则它们会被排除在「铺满」之外、留下右侧空白。
            floors[SUB_COL] = SUB_MIN
            floors[NOTE_COL] = NOTE_MIN
            # 上限定死：等比放大时也不许超过（备注必须保持窄 —— 用户明确要求）。
            # 它们被「封顶」后，多出来的份额会分摊给别人，不会浪费。
            caps = {SUB_COL: SUB_MAX, NOTE_COL: NOTE_MAX}

            # ===== 把宽度让给最左侧的行号栏 =====
            # 用户要求：文件数 / 源文件大小 / 输出大小 / 备注 / 总耗时 这 5 列
            # **各缩窄相同的量**，正好空出最左侧行号栏容纳 3 位数所需的宽度。
            #
            # 🔴 两个硬约束：
            #   ① **各列缩窄量必须完全相等**（用户明确要求「所有的这些变窄的量
            #      是一样的」）。所以不能用「每列扣 DONOR_STRIP、扣不动的就夹住」
            #      —— 那样会出现 2/0/2/0/2 这种不均匀结果（踩过）。
            #      正确做法：先求「5 列都能承受的最大统一缩窄量」再统一扣。
            #   ② 缩窄后这 5 列**仍要完整显示列名和数据**，不能被遮挡。
            #      所以每列的硬底线 = max(列名宽, 数据宽) + PAD。
            if DONOR_STRIP > 0:
                # 每列最多能扣多少（留够自己的硬底线）
                capacity = []
                for c in DONOR_COLS:
                    hard = max(_name_w(c) + PAD,
                               _data_fit(c, extra=PAD, at_least=0))
                    capacity.append(max(0, floors[c] - hard))
                # 统一缩窄量 = min(期望值, 所有列都承受得起的上限)
                uniform = min([DONOR_STRIP] + capacity) if capacity else 0
                for c in DONOR_COLS:
                    floors[c] -= uniform
                    ideals[c] = max(floors[c], ideals[c] - uniform)

            # ⚠️ `viewport().width()` 返回的是**已经扣掉垂直滚动条**的可用宽度。
            #    所以**不能**再减一次——重复扣除会让右侧永远差 20px、填不满
            #    （用户明确要求「不要有留白」）。实测：视口 994、列宽合计 974，
            #    差的 20px 正是被重复扣掉的。
            sb_w = 0

            if viewport_w > 0:
                avail = viewport_w - sb_w
                total_floor = sum(floors.values())
                if total_floor <= avail:
                    # 有余量 → 按权重等比放大铺满，但受 caps 封顶；
                    # 被 cap 掉的部分再分给未封顶的列（迭代到收敛，最多几轮）。
                    widths = dict(floors)
                    gap = avail - total_floor
                    pool = set(ALL_COLS)
                    for _ in range(len(ALL_COLS)):
                        if gap <= 0 or not pool:
                            break
                        base = sum(ideals[c] for c in pool) or 1
                        given = 0
                        full = set()
                        for c in list(pool):
                            share = int(round(gap * (ideals[c] / base)))
                            cap = caps.get(c)
                            if cap is not None and widths[c] + share >= cap:
                                share = max(0, cap - widths[c])
                                full.add(c)
                            given += share
                            widths[c] += share
                        if given == 0:
                            break
                        gap -= given
                        pool -= full
                    # 取整误差补到「未封顶且最宽」的列，保证 Σ 正好 == avail
                    if gap != 0:
                        # 优先补给未封顶的固定列；找不到再补给 Stretch 列
                        # （否则 gap 会留在右侧变成一条白边 —— 用户要求「不要有留白」）。
                        done = False
                        for c in sorted(ALL_COLS, key=lambda x: -ideals[x]):
                            if c in caps:
                                continue
                            widths[c] += gap
                            done = True
                            break
                        if not done:
                            for c in sorted(ALL_COLS, key=lambda x: -widths[x]):
                                widths[c] += gap
                                break
                else:
                    # 放不下 → 全部取下限，出横向滚动条
                    widths = dict(floors)
            else:
                widths = dict(ideals)

            for c in ALL_COLS:
                header.setSectionResizeMode(c, QHeaderView.Fixed)
                table.setColumnWidth(c, widths[c])

        def rebuild():
            """从 records 全量重建表格（初始填充 / 增删 / 拖拽排序后调用）。"""
            _loading[0] = True
            table.setRowCount(0)
            for row, r in enumerate(records):
                table.insertRow(row)
                fill_row(r, row)
            # 最左侧行号栏显示 1、2、3…（拖拽排序后也跟着重排，是「当前顺序」）
            table.setVerticalHeaderLabels(
                [str(i + 1) for i in range(len(records))])
            _loading[0] = False
            # 数据变了 → 按新数据重算列宽（源/输出/总耗时是按数据宽度定的）。
            # 必须放在填完数据之后：表还空的时候 _data_fit 只能拿到兜底值。
            apply_widths()

        def _reorder():
            """拖拽排序后：重建表格（持久化由创建表格时传入的 lambda 负责）。"""
            rebuild()

        v.addWidget(table, 1)

        # ===== 窗口缩放时按比例重算列宽 =====
        # 用户要求：「手动缩放窗口大小的时候也让宽度按比例更改」。
        # 表格自身没有 resize 信号，用它 viewport 的事件过滤器最直接。
        # ⚠️ resizeEvent 里读 viewport().width() 可能是**旧值**，所以用
        #   QTimer.singleShot(0, ...) 延到本轮布局完成之后再算（踩过坑：
        #   直接读会导致列宽永远慢一拍 / 算出 0 走兜底值）。
        class _ViewportResizeFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Resize:
                    QTimer.singleShot(0, apply_widths)
                return False

        _vp_filter = _ViewportResizeFilter(table)
        table.viewport().installEventFilter(_vp_filter)
        # 防止被 GC（父对象挂了 filter 也会跟着挂，但显式持有一份更稳）
        table._vp_resize_filter = _vp_filter

        def _on_cell_changed(row, col):
            """普通文本列（非下拉列）被双击编辑时回写并保存。"""
            if _loading[0]:
                return
            if 0 <= row < len(records) and col not in COMBO_COLS:
                item = table.item(row, col)
                new_val = item.text().strip() if item else ""
                records[row][COL_KEYS[col]] = new_val
                if item:
                    item.setTextAlignment(Qt.AlignCenter)
                self._save_encode_ref(records)

        table.cellChanged.connect(_on_cell_changed)

        rebuild()   # 初始填充

        # ===== 手动添加区 =====
        form = QFormLayout()
        files_edit = QLineEdit()
        files_edit.setPlaceholderText("本次烧了多少个文件，如 12")
        src_edit = QLineEdit()
        src_edit.setPlaceholderText("源文件一共多少 MB，如 850.3")
        preset_combo = NoWheelCombo()
        preset_combo.addItems(self.PRESET_OPTIONS)
        preset_combo.setCurrentText(self.encode_settings["preset"])
        crf_combo = NoWheelCombo()
        crf_combo.addItems(self.CRF_OPTIONS)
        crf_combo.setCurrentText(self.encode_settings["crf"])
        # 并发（同时处理数）：默认读设置菜单里的当前值
        workers_combo = NoWheelCombo()
        workers_combo.addItems(["1", "2", "3", "4"])
        workers_combo.setCurrentText(str(self.encode_settings.get("max_workers", "1")))
        # 优先级：新增的手动录入项，放在「并发」右边（与表格列顺序一致）。
        # 默认读设置里的当前优先级，存中文（与表格列同口径）。
        prio_combo = NoWheelCombo()
        prio_combo.addItems(self.PRIORITY_CN_OPTIONS)
        _prio_key = str(self.encode_settings.get("priority") or "normal")
        prio_combo.setCurrentText(
            self.PRIORITY_CN_MAP.get(_prio_key, "普通"))
        enc_row = QHBoxLayout()
        enc_row.addWidget(preset_combo)
        enc_row.addWidget(QLabel("画质："))
        enc_row.addWidget(crf_combo)
        enc_row.addWidget(QLabel("并发："))
        enc_row.addWidget(workers_combo)
        enc_row.addWidget(QLabel("优先级："))
        enc_row.addWidget(prio_combo)
        enc_row.addStretch(1)

        sub_combo = NoWheelCombo()
        sub_combo.setEditable(True)
        sub_combo.addItem("手动输入")
        sub_combo.addItem("无")
        try:
            from subtitle import list_presets
            for name in list_presets():
                sub_combo.addItem(name)
        except Exception:
            pass
        sub_hint = QLabel("（本次烧字幕用的预设；手动输入的选「手动输入」）")
        sub_hint.setStyleSheet("color: #888; font-size: 11px;")
        sub_row = QVBoxLayout()
        sub_row.addWidget(sub_combo)
        sub_row.addWidget(sub_hint)

        # 总耗时：拆成「分 + 秒」两个数字框，只填数字不用打单位字
        # （原来是一个文本框要用户自己敲「4分32秒」，容易格式不统一：
        #  有人写 4分32秒、有人写 4 分 32 秒、有人写 4:32 → 表里口径就乱了）
        elapsed_min_spin = NoWheelSpin()
        elapsed_min_spin.setRange(0, 9999)
        elapsed_min_spin.setSuffix(" 分")
        elapsed_min_spin.setAlignment(Qt.AlignCenter)
        elapsed_min_spin.setFixedWidth(90)
        elapsed_sec_spin = NoWheelSpin()
        elapsed_sec_spin.setRange(0, 59)
        elapsed_sec_spin.setSuffix(" 秒")
        elapsed_sec_spin.setAlignment(Qt.AlignCenter)
        elapsed_sec_spin.setFixedWidth(90)
        elapsed_row = QHBoxLayout()
        elapsed_row.addWidget(elapsed_min_spin)
        elapsed_row.addWidget(elapsed_sec_spin)
        elapsed_row.addStretch(1)

        out_edit = QLineEdit()
        out_edit.setPlaceholderText("输出文件一共多少 MB，如 720.5")
        note_edit = QLineEdit()
        note_edit.setPlaceholderText("备注（可选），如：显卡型号 / 4K 素材等")

        form.addRow("文件数：", files_edit)
        form.addRow("源文件大小(MB)：", src_edit)
        form.addRow("编码设置：", enc_row)
        form.addRow("预设字幕：", sub_row)
        form.addRow("总耗时：", elapsed_row)
        form.addRow("输出大小(MB)：", out_edit)
        form.addRow("备注：", note_edit)

        def _clean_subtitle_name(raw):
            """归一「预设字幕」值（逻辑在 MainWindow._clean_subtitle_name，
            手动添加区这里是纯文本输入，只需处理 inherited_ 临时文件名）。"""
            return self._clean_subtitle_name(raw)

        def add_record():
            """校验 + 追加一条记录（日期自动填今天）。"""
            try:
                n = int(files_edit.text().strip())
            except ValueError:
                n = 0
            if n <= 0:
                QMessageBox.warning(dlg, "提示", "「文件数」要填一个正整数")
                return
            # 总耗时：两个数字框拼成「XX分XX秒」（与自动记录同口径）。
            # 只填秒（分为 0）时写「XX秒」，和自动记录保持一致。
            _m = elapsed_min_spin.value()
            _s = elapsed_sec_spin.value()
            elapsed_text = ("{}分{}秒".format(_m, _s) if _m else "{}秒".format(_s))
            rec = {
                "date": time.strftime("%Y-%m-%d"),
                "files": n,
                "src_mb": src_edit.text().strip(),
                "preset": preset_combo.currentText(),
                "crf": crf_combo.currentText(),
                "workers": workers_combo.currentText().strip(),
                "priority": prio_combo.currentText(),
                "subtitle": _clean_subtitle_name(sub_combo.currentText()),
                "elapsed": elapsed_text,
                "out_mb": out_edit.text().strip(),
                "note": note_edit.text().strip(),
            }
            records.append(rec)
            self._save_encode_ref(records)
            rebuild()                 # 全量重建（保证下拉列的信号回调行号正确）
            table.scrollToBottom()
            # 清空输入（编码设置/并发保留，方便连着记同设置的几批）
            for e in (files_edit, src_edit, out_edit):
                e.clear()
            elapsed_min_spin.setValue(0)
            elapsed_sec_spin.setValue(0)
            note_edit.clear()

        add_btn = QPushButton("添加记录")
        add_btn.clicked.connect(add_record)

        def record_pending():
            """把最近跑完那一批的数据补记一条（弹窗里没记的时候用）。"""
            if not self._pending_batch:
                QMessageBox.information(
                    dlg, "提示",
                    "现在没有可记录的批次数据。\n\n"
                    "跑完一批合成 / 烧字幕后，结束弹窗里点「记录本批到编码参考」；\n"
                    "要是当时没点，回到这个窗口点这个按钮也能补记。")
                return
            rec = dict(self._pending_batch)
            # 同样要归一「预设字幕」（走这个按钮补记时别写进 inherited_xxx.ass）
            _pn = (rec.get("preset_name") or "").strip()
            if _pn:
                self._last_burn_preset_name = _pn
            rec["subtitle"] = self._clean_subtitle_name(rec.get("subtitle", ""), rec)
            records.append(rec)
            self._save_encode_ref(records)
            self._pending_batch = None
            rebuild()
            table.scrollToBottom()
            QMessageBox.information(dlg, "已记录", "最近一批的数据已写入编码参考。")

        pending_btn = QPushButton("记录最近一批")
        pending_btn.setToolTip("把刚跑完那批（拼接 / 烧字幕）的文件数、大小、设置、耗时自动记一条")
        pending_btn.clicked.connect(record_pending)

        del_btn = QPushButton("删除选中")

        def del_selected():
            rows = sorted({i.row() for i in table.selectedIndexes()}, reverse=True)
            if not rows:
                QMessageBox.information(dlg, "提示", "先在表格里选中要删除的行")
                return
            ans = QMessageBox.question(
                dlg, "确认删除",
                "确定要删除选中的 {} 条记录吗？\n此操作不可撤销。".format(len(rows)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
            for r in rows:
                if r < len(records):
                    del records[r]
            self._save_encode_ref(records)
            rebuild()                 # 重建以刷新下拉列的信号回调行号

        del_btn.clicked.connect(del_selected)

        # ---- 导出：CSV 文件 / 复制到剪贴板（方便把参考数据发给别人）----
        def _cell_text(r, c):
            """读某格文本：下拉列从 cellWidget 取，普通列从 item 取。"""
            if c in COMBO_COLS:
                w = table.cellWidget(r, c)
                return w.currentText() if w else ""
            it = table.item(r, c)
            return it.text() if it else ""

        def _table_rows():
            """把表格当前内容读成二维列表（含表头）。"""
            headers = [table.horizontalHeaderItem(c).text()
                       for c in range(table.columnCount())]
            rows = [headers]
            for r in range(table.rowCount()):
                rows.append([_cell_text(r, c) for c in range(table.columnCount())])
            return rows

        def export_csv():
            if table.rowCount() == 0:
                QMessageBox.information(dlg, "提示", "还没有任何记录，先添加几条再导出")
                return
            default = os.path.join(os.path.dirname(self.ENCODE_REF_PATH),
                                   "编码参考_{}.csv".format(time.strftime("%Y%m%d")))
            path, _ = QFileDialog.getSaveFileName(
                dlg, "导出为 CSV", default, "CSV 文件 (*.csv);;所有文件 (*)")
            if not path:
                return
            if not path.lower().endswith(".csv"):
                path += ".csv"
            try:
                # utf-8-sig 带 BOM：Excel 直接双击打开不乱码
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerows(_table_rows())
                QMessageBox.information(
                    dlg, "已导出",
                    "编码参考已导出到：\n{}\n\n（utf-8-sig 编码，Excel 可直接打开）".format(path))
            except Exception as e:
                QMessageBox.critical(dlg, "导出失败", str(e))

        def copy_tsv():
            """复制成 TSV：粘到 Excel / 腾讯文档里会自动分列。"""
            if table.rowCount() == 0:
                QMessageBox.information(dlg, "提示", "还没有任何记录")
                return
            text = "\n".join("\t".join(r) for r in _table_rows())
            QApplication.clipboard().setText(text)
            QMessageBox.information(
                dlg, "已复制",
                "已复制 {} 条记录到剪贴板，可直接粘贴到 Excel / 表格里。".format(table.rowCount()))

        # ---- 导入：CSV（含别人导出的）或 JSON（程序内部格式）----
        # 场景：别人跑了一批，把「导出 CSV」的文件发过来 → 点这里导入进自己的参考表。
        # 两种格式都认，靠**表头名字**对齐列（不靠列顺序），列顺序变了也不会错位。
        def _norm_header(h):
            """把表头归一化后做匹配：去空白 + 小写 + 去括号内容。

            因为不同人导出的表头可能写成「源文件大小(MB)」「源文件大小MB」
            「输出大小(MB)」等，直接字符串相等会漏匹配。
            """
            h = (h or "").strip().lower()
            h = re.sub(r"[（(].*?[)）]", "", h)     # 去掉 (MB) （crf） 这类后缀
            h = h.replace(" ", "").replace("_", "").replace("-", "")
            return h

        # 表头别名 → 内部键。左边是归一化后的写法，右边是 COL_KEYS 里的键。
        HEADER_ALIAS = {
            "日期": "date", "date": "date",
            "文件数": "files", "个数": "files", "数量": "files", "files": "files",
            "源文件大小": "src_mb", "源大小": "src_mb", "源文件": "src_mb",
            "srcmb": "src_mb",
            "编码速度": "preset", "速度": "preset", "预设": "preset",
            "preset": "preset", "编码": "preset",
            "画质": "crf", "crf": "crf", "画质crf": "crf",
            "并发": "workers", "同时处理": "workers", "workers": "workers",
            # 优先级：兼容老表里没有这一列（缺失时留空），也兼容英文导出。
            "优先级": "priority", "编码优先级": "priority", "priority": "priority",
            "预设字幕": "subtitle", "字幕": "subtitle", "subtitle": "subtitle",
            "总耗时": "elapsed", "耗时": "elapsed", "elapsed": "elapsed",
            "输出大小": "out_mb", "输出": "out_mb", "outmb": "out_mb",
            "备注": "note", "note": "note", "remark": "note",
        }

        def _map_headers(headers):
            """表头列表 → 列索引到内部键的映射 {col_index: key}。"""
            mapping = {}
            for i, h in enumerate(headers):
                key = HEADER_ALIAS.get(_norm_header(h))
                if key and key not in mapping.values():
                    mapping[i] = key
            return mapping

        def _parse_import(path):
            """读入文件 → 记录列表。失败时抛异常（调用方弹框）。"""
            low = path.lower()
            raw = None
            for enc in ("utf-8-sig", "utf-8", "gbk"):
                try:
                    with open(path, "r", encoding=enc, newline="") as f:
                        raw = f.read()
                    break
                except UnicodeDecodeError:
                    continue
            if raw is None:
                raise ValueError("文件编码无法识别（试过 utf-8 / gbk）")

            if low.endswith(".json"):
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise ValueError("JSON 内容不是一个记录列表")
                out = []
                for r in data:
                    if isinstance(r, dict):
                        # 只保留认识的键，其余忽略
                        _row = {k: str(r.get(k, "")) for k in COL_KEYS}
                        # 优先级归一成中文（英文 JSON 也能直接导）
                        _pv = _row.get("priority", "").strip()
                        if _pv:
                            _row["priority"] = self.PRIORITY_CN_MAP.get(
                                _pv.lower(), _pv)
                        out.append(_row)
                return out

            # 当作 CSV / TSV：自动判断分隔符
            head_line = raw.splitlines()[0] if raw.splitlines() else ""
            delim = "\t" if head_line.count("\t") > head_line.count(",") else ","
            rows = list(csv.reader(io.StringIO(raw), delimiter=delim))
            if not rows:
                raise ValueError("文件是空的")
            headers = rows[0]
            mapping = _map_headers(headers)
            if not mapping:
                raise ValueError(
                    "表头不认识。第一行需要是列名，例如：\n"
                    "日期,文件数,源文件大小(MB),编码速度,画质(crf),并发,"
                    "预设字幕,总耗时,输出大小(MB),备注")
            out = []
            for row in rows[1:]:
                if not any((c or "").strip() for c in row):
                    continue                      # 跳过空行
                rec = {k: "" for k in COL_KEYS}
                for ci, key in mapping.items():
                    if ci < len(row):
                        rec[key] = (row[ci] or "").strip()
                # 优先级归一成中文（别人导出/老表可能是 low/normal/high）。
                # 老表根本没有这一列 → 保持空，显示为空白，不猜值。
                _pv = rec.get("priority", "").strip()
                if _pv:
                    rec["priority"] = self.PRIORITY_CN_MAP.get(_pv.lower(), _pv)
                # 至少得有「文件数」或「输出大小」，否则视为无效行
                if rec.get("files") or rec.get("out_mb"):
                    out.append(rec)
            return out

        def import_data():
            path, _ = QFileDialog.getOpenFileName(
                dlg, "导入编码参考数据",
                os.path.dirname(self.ENCODE_REF_PATH),
                "参考数据 (*.csv *.json);;CSV 文件 (*.csv);;JSON 文件 (*.json);;所有文件 (*)")
            if not path:
                return
            try:
                incoming = _parse_import(path)
            except Exception as e:
                QMessageBox.critical(dlg, "导入失败", "读不了这个文件：\n{}".format(e))
                return
            if not incoming:
                QMessageBox.information(dlg, "提示", "文件里没有可导入的记录")
                return

            # 问：追加还是替换
            box = QMessageBox(dlg)
            box.setWindowTitle("导入方式")
            box.setIcon(QMessageBox.Question)
            box.setText("从文件里读到 {} 条记录。\n\n要追加到现有记录后面吗？".format(len(incoming)))
            box.setInformativeText(
                "「追加」= 保留你现在的 {} 条，把新记录接在后面\n"
                "「替换」= 清空现在这 {} 条，只用文件里的\n"
                "「取消」= 什么都不做".format(len(records), len(records)))
            add_btn2 = box.addButton("追加", QMessageBox.AcceptRole)
            rep_btn = box.addButton("替换", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.setDefaultButton(add_btn2)
            box.exec()
            clicked = box.clickedButton()
            if clicked is not add_btn2 and clicked is not rep_btn:
                return

            if clicked is rep_btn:
                records[:] = incoming
            else:
                records.extend(incoming)
            try:
                self._save_encode_ref(records)
            except Exception as e:
                QMessageBox.critical(dlg, "保存失败", str(e))
                return
            rebuild()
            table.scrollToBottom()
            QMessageBox.information(
                dlg, "已导入",
                "成功导入 {} 条记录（{}）。\n现在共 {} 条。".format(
                    len(incoming), "替换原记录" if clicked is rep_btn else "追加在原记录后面",
                    len(records)))

        import_btn = QPushButton("导入")
        import_btn.setToolTip(
            "导入别人给你的参考数据（.csv 或 .json）。\n"
            "按列名自动对齐，列顺序不一样也能认。")
        import_btn.clicked.connect(import_data)

        export_btn = QPushButton("导出 CSV")
        export_btn.setToolTip("把表格里的所有记录导出成 .csv 文件（Excel 可直接打开）")
        export_btn.clicked.connect(export_csv)
        copy_btn = QPushButton("复制到剪贴板")
        copy_btn.setToolTip("复制成制表符分隔文本，粘贴到 Excel / 腾讯文档会自动分列")
        copy_btn.clicked.connect(copy_tsv)

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(pending_btn)
        btn_row.addWidget(import_btn)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        v.addLayout(form)
        v.addLayout(btn_row)

        # 兜底：表格里的下拉框是动态 rebuild 出来的，逐个 new 时已用
        # NoWheelCombo；这里再递归扫一遍，保证将来新加的控件也不会滚轮误改。
        disable_wheel_recursive(dlg)

        dlg.exec()


if __name__ == "__main__":
    main()
