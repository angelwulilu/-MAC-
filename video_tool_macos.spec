# -*- mode: python ; coding: utf-8 -*-
"""
video_tool_macos.spec —— 把「视频批量处理工具」打包成 macOS 的 .app

⚠️ 本 spec 只能在 **macOS 上**执行（PyInstaller 不支持交叉编译）。
   在 Mac 上：
       cd <本目录>
       python3 -m PyInstaller video_tool_macos.spec --noconfirm

产物：
    dist/视频工具.app                        ← 双击运行，整个拖给别人就行
    dist/视频工具.app/Contents/MacOS/视频工具  ← 真正的可执行文件
    dist/视频工具.app/Contents/Resources/…    ← 内置资源（ffmpeg / 预设 / json）

关于「自带 ffmpeg」：
    下面 FFMPEG_SRC / FFPROBE_SRC 指向 Mac 上的 ffmpeg 二进制。
    构建脚本(打包mac.sh)会先把它们下载/拷到 ./_bundled_ffmpeg/ 下，
    这里再打进 .app。运行时 paths.py 会优先找到 .app 旁边的那一份
    （方便将来替换），找不到才用 .app 内置的。
    → 收包的人**什么都不用装**。
"""

import os
import shutil

PROJ = SPECPATH  # noqa: F821  —— PyInstaller 注入的 spec 所在目录

APP_NAME = "视频工具"

# --------------------------------------------------------------- ffmpeg 来源
# 构建脚本会把 macOS 版 ffmpeg/ffprobe 放到这里（不是 .exe，是 Unix 可执行文件）
_FF_DIR = os.path.join(PROJ, "_bundled_ffmpeg")
FFMPEG_SRC = os.path.join(_FF_DIR, "ffmpeg")
FFPROBE_SRC = os.path.join(_FF_DIR, "ffprobe")

# 图标：Mac 要 .icns。构建脚本会把 app_icon.icns 放在本目录；
# 没有就不设图标（Dock 里显示默认图标，不影响运行）。
ICON = os.path.join(PROJ, "app_icon.icns")

# ---------------------------------------------------------------------------
# 用不到的 Qt 模块，全部排除
#
# 已实测确认（Windows 版同款结论）：QtMultimedia / QtMultimediaWidgets 只依赖
# Qt6Core / Qt6Gui / Qt6Network / Qt6Widgets，所以 Qml / Quick / WebEngine
# 这些大件可以安全砍掉。光 QtWebEngineCore 一个就 126MB。
# ---------------------------------------------------------------------------
EXCLUDES = [
    # QtWebEngine 全家（最大的一块）
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebView",
    # QML / Quick 全家
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickTest", "PySide6.QtQuickWidgets",
    # 3D / 图表 / 可视化
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    # 设计器 / 帮助 / 打印 / PDF
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    # 硬件 / 网络周边
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtSensors", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtNetworkAuth", "PySide6.QtHttpServer",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtRemoteObjects",
    "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
    # 其它用不到的
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtCanvasPainter",
    # 用不到的第三方 / 开发期东西
    "tkinter", "unittest", "doctest", "pydoc_data", "lib2to3",
    "numpy", "PIL", "matplotlib", "pandas", "scipy",
    "IPython", "pytest", "setuptools", "pip", "wheel",
    "PyInstaller", "Cython", "pydoc",
]

# --------------------------------------------------------------- 数据文件
datas = []
_presets = os.path.join(PROJ, "subtitle_presets")
if os.path.isdir(_presets):
    # 打进包里一份：首次运行会自动拷到程序旁边，之后用户可以自由增删
    datas.append((_presets, "subtitle_presets"))
_ref = os.path.join(PROJ, "encode_reference.json")
if os.path.isfile(_ref):
    datas.append((_ref, "."))

# ---------------------------------------------------------------- ffmpeg
# 放进 Contents/Resources/ffmpeg/ —— paths.py 的候选路径里有
#   os.path.join(bundle_dir(), "ffmpeg", exe)
# 在 onedir/.app 模式下 bundle_dir() == Contents/Resources（sys._MEIPASS），
# 所以这一条正好命中。
#
# _bundled_ffmpeg/ 里可能有多种架构（arm64 / intel_*），构建脚本会把
# 当前机器**真正要用**的那份放到 _bundled_ffmpeg/ 根下，就用它。
binaries = []
for src in (FFMPEG_SRC, FFPROBE_SRC):
    if os.path.isfile(src):
        binaries.append((src, "ffmpeg"))

# 🔴 ffmpeg / ffprobe 必须带可执行位。
#    PyInstaller 收二进制时不一定保留权限位；而 .app 里丢掉 x 位 =
#    「ffmpeg 找不到」或直接 Permission denied，且现象很隐蔽（自检可能仍通过）。
#    → 构建收尾时对 .app 内的副本显式 chmod 755（见文件末尾）。
_FF_NAMES = ("ffmpeg", "ffprobe")

# Qt Multimedia 的 FFmpeg 后端要靠这些编解码库。
# PyInstaller 的 PySide6 钩子也会收一份，但插件按 Qt 自己的搜索路径加载，
# 不一定找得到；这里再显式放一份到 Resources 根目录（在搜索路径上），双保险。
import glob as _glob  # noqa: E402
import PySide6 as _PySide6  # noqa: E402
_ps_dir = os.path.dirname(_PySide6.__file__)
for pat in ("libav*.dylib", "libsw*.dylib", "av*.dll", "sw*.dll"):
    for lib in _glob.glob(os.path.join(_ps_dir, pat)):
        binaries.append((lib, "."))

# ------------------------------------------------------------------ 构建
a = Analysis(
    [os.path.join(PROJ, "app.py")],
    pathex=[PROJ],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # onedir：依赖由 COLLECT 收集进 _internal
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,              # 图形界面程序
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,           # None = 跟随当前机器架构（Apple Silicon / Intel）
    codesign_identity=None,     # 不做签名（收包的人右键→打开即可）
    entitlements_file=None,
    icon=ICON if os.path.isfile(ICON) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# ---------------------------------------------------------------------------
# macOS 专属：把 COLLECT 出来的文件夹包成 .app
#
# bundle_identifier 用反域名格式（只是个标识，不需要真的有域名）。
# ⚠️ 不要用中文当 identifier —— 某些 macOS 版本会因此拒绝加载。
# ---------------------------------------------------------------------------
app = BUNDLE(
    coll,
    name=APP_NAME + ".app",
    icon=ICON if os.path.isfile(ICON) else None,
    bundle_identifier="com.videotool.mac",
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        # 允许高分屏（不加的话在 Retina 上字会糊）
        "NSHighResolutionCapable": True,
        # 声明「这是纯 GUI 程序，不要在 Dock 里显示成终端应用」
        "LSApplicationCategoryType": "public.app-category.video",
        # 告诉 macOS：本程序只在本机运行即可（不做沙箱/签名）
        "LSMinimumSystemVersion": "10.15",
        # 允许读写用户选择的文件（批量视频处理需要）
        "NSDocumentsFolderUsageDescription": "需要读取你选择的视频文件并输出成品",
        "NSDesktopFolderUsageDescription": "需要读取桌面上的视频素材并输出成品",
        "NSDownloadsFolderUsageDescription": "需要读取下载文件夹里的视频素材",
    },
)

# ---------------------------------------------------------------------------
# 收尾：把预置数据和 ffmpeg 也从 .app 内部复制一份到 .app 旁边
#
#   - ffmpeg 放外面：用户**看得见、能替换**（paths.py 优先用这一份）
#   - 预设/编码参考放外面：重打包 .app 不会覆盖用户数据
#
# ⚠️ 为什么「里面也要留一份」：别人只拿走 .app 一个文件（拖到 Applications），
#    外面那份就没了，这时必须靠 .app 内置的那份兜底 —— 这就是「自带」的含义。
# ---------------------------------------------------------------------------
DIST_DIR = os.path.join(DISTPATH, APP_NAME + ".app")  # noqa: F821
_APP_INNER = os.path.join(DIST_DIR, "Contents", "MacOS")
_RES = os.path.join(DIST_DIR, "Contents", "Resources")

# ffmpeg：复制到 .app 旁边（外面那份方便替换；内置那份已由 binaries 收好）
_side_ff = os.path.join(DISTPATH, "ffmpeg")
if os.path.isdir(_FF_DIR) and not os.path.isdir(_side_ff):
    try:
        shutil.copytree(_FF_DIR, _side_ff)
        print("[spec] ffmpeg 也放了一份在 .app 旁边:", _side_ff)
    except Exception as _e:
        print("[spec] ffmpeg 外置副本失败（不影响运行，.app 内已有）:", _e)

# 预设 / 编码参考：复制到 .app 旁边
for _sub in ("subtitle_presets", "encode_reference.json"):
    _src = os.path.join(_RES, _sub)
    _dst = os.path.join(DISTPATH, APP_NAME, _sub)
    if os.path.exists(_src) and not os.path.exists(_dst):
        try:
            if os.path.isdir(_src):
                shutil.copytree(_src, _dst)
            else:
                os.makedirs(os.path.dirname(_dst), exist_ok=True)
                shutil.copyfile(_src, _dst)
            print("[spec] 已复制到外置目录:", _dst)
        except Exception as _e:
            print("[spec] 复制失败（.app 内已有，不影响）:", _e)

print("[spec] 完成。产物:", DIST_DIR)

# ---------------------------------------------------------------------------
# 收尾 1：给 .app 内置的 ffmpeg / ffprobe 补可执行位
# ---------------------------------------------------------------------------
for _name in _FF_NAMES:
    for _p in (os.path.join(_RES, "ffmpeg", _name),
               os.path.join(_APP_INNER, _name)):
        if os.path.isfile(_p):
            try:
                os.chmod(_p, 0o755)
                print("[spec] 已设可执行位:", _p)
            except Exception as _e:
                print("[spec] chmod 失败（请手动确认）:", _p, _e)

# ---------------------------------------------------------------------------
# 收尾 2：⌘ 双架构说明
#
# ⚠️ 打出来的包**只能在同一架构的 Mac 上跑**。这是打包机决定的，不是配置项。
#    （target_arch 留 None = 跟随本机；想通用得打 universal2，体积翻倍。）
#
# 在 .app **旁边**多放一份 ffmpeg 的意义就在这里：
#    如果打出来的是 arm64 包、对方却是 Intel 机器，
#    对方可以自己下载 Intel 版 ffmpeg 覆盖到「ffmpeg/」文件夹里，
#    程序会优先用外面这份（paths.py 的查找顺序），**不用重新打包**。
# ---------------------------------------------------------------------------
print("[spec] 提示：产物架构 = 本机架构（%s）。换架构需要重打包，"
      "或用外面的 ffmpeg/ 文件夹替换。" % (os.uname().machine
                                            if hasattr(os, "uname") else "?"))
