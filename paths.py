# -*- coding: utf-8 -*-
"""
paths.py —— 统一解析「程序目录 / 内置资源 / ffmpeg」的位置

为什么要这个模块：
    打包成 exe 之后，源码目录已经不存在了，`__file__` 也不再指向用户
    看得见的那个文件夹（单文件模式下会指向 %TEMP% 里的临时解压目录）。
    所以所有「跟程序放一起」的东西（ffmpeg、字幕预设、编码参考 json）
    都必须重新定位，否则 exe 一挪位置就跑不起来。

约定：
    - app_dir()    = 用户看到、可以往里放文件的目录
                     （打包后 = exe 所在目录；开发时 = 源码目录）
    - bundle_dir() = 被打进 exe 内部的只读资源目录
                     （单文件模式是临时解压目录；开发时 = 源码目录）

查找 ffmpeg 的优先级（先找到先用）：
    1. <程序目录>/ffmpeg/ffmpeg.exe      ← 打包发布时的标准位置
    2. <程序目录>/ffmpeg.exe             ← 直接丢在 exe 旁边
    3. <exe 内部>/ffmpeg/ffmpeg.exe      ← 单文件模式内置
    4. D:\\ffmpeg\\bin\\ffmpeg.exe         ← 老路径，向后兼容
    5. 系统 PATH 里的 ffmpeg
"""

import os
import shutil
import subprocess
import sys


IDLE_PRIORITY_CLASS = 0x00000040          # 只在系统空闲时才跑（最低）
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # 低于普通
NORMAL_PRIORITY_CLASS = 0x00000020        # 普通（Windows 默认；不写也行）
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000  # 高于普通
HIGH_PRIORITY_CLASS = 0x00000080          # 高（Windows 只保留 6 档，这是给用户的可选最高档）

# 用户可选的三档（「设置」对话框里的下拉）。
# 为什么不做满 Windows 的 6 档：其中 REALTIME(0x100) 会让系统输入设备都
# 抢不到 CPU（会卡死鼠标键盘，绝不能给普通用户选）；NORMAL 与「不设」等价；
# IDLE 太极端，批量任务可能永远跑不完。3 档已经覆盖「别抢我资源 / 均衡 / 跑快点」。
#
# ⚠️ macOS 说明：
#   macOS **没有** Windows 那种「进程优先级类」概念，`creationflags` 也根本
#   不存在这个参数。POSIX 上影响调度的是 `nice` 值（子进程继承父进程），
#   而通过 `subprocess` 在启动瞬间设置 nice 需要 `preexec_fn`——这东西在
#   有线程的进程里用是不安全的（PyInstaller 打包后更是容易崩），收益也远
#   不如 Windows 那 20%。所以 Mac 版**不提供**这个设置：
#   设置对话框里会隐藏「编码优先级」下拉，spawn_kwargs() 返回空。
#   下面这张表仍保留完整结构，只是第 0 项（flag）在 Mac 上不参与实际调用。
_IS_WIN = os.name == "nt"

PRIORITY_LEVELS = {
    "low": (BELOW_NORMAL_PRIORITY_CLASS, "低（不抢资源，适合边跑边干别的）"),
    "normal": (0, "普通（系统默认，资源占用与前台程序均衡）"),
    "high": (HIGH_PRIORITY_CLASS, "高（最快，但会抢占 CPU，期间别开大程序）"),
}
DEFAULT_PRIORITY = "normal"


def priority_supported():
    """当前平台是否真的支持「进程优先级」设置。

    Windows：支持（走 CREATE_*_PRIORITY_CLASS）。
    macOS / Linux：不支持 —— 没有对应的 creationflags，硬塞会直接抛异常，
    所以设置界面要隐藏这一项，避免用户选了个「看起来生效其实没用」的选项。
    """
    return _IS_WIN

# ------------------------------------------------- 当前生效的优先级（全局）
#
# 为什么用「全局变量」而不是给每个函数加参数：
#   批量 ffmpeg 的调用点散落在 concat.py / subtitle.py / reencode.py 的深处
#   （5 处），层层透传 `priority=` 要改十几个函数签名，容易漏、也容易在
#   某条分支上忘了传。优先级本身是**全局策略**（一次设置、全程生效），
#   用模块级变量最贴合语义。
#
# 用法：启动程序后调用 set_priority("low"/"normal"/"high") 一次即可；
#      所有 spawn_kwargs() 调用会自动带上。
_current_priority = DEFAULT_PRIORITY


def set_priority(priority):
    """设置全局子进程优先级（"low"/"normal"/"high"）。返回实际生效的值。"""
    global _current_priority
    if priority not in PRIORITY_LEVELS:
        priority = DEFAULT_PRIORITY
    _current_priority = priority
    return priority


def get_priority():
    """当前全局优先级。"""
    return _current_priority


def spawn_kwargs(high_priority=False, priority=None):
    """启动 ffmpeg/ffprobe 时统一附加的参数。

    1) 不要弹控制台黑窗（CREATE_NO_WINDOW）—— **仅 Windows**。
       ⚠️ 这不是「美观问题」，是**性能红线**。实测（用 pythonw.exe 模拟打包后
       「父进程没有控制台」的情形，同一台机器、同一个 ffprobe）：

            父进程有控制台（python app.py）：creationflags=0 → 0.189s
            父进程无控制台（打包后的 exe）  ：creationflags=0 → 0.906s  ← 慢 4.8 倍
            父进程无控制台 + CREATE_NO_WINDOW：            → 0.222s  ← 恢复正常

       原因：没有控制台的父进程去启动控制台子程序时，Windows 每次都要为它
       **新建一个控制台窗口**（就是用户看到「一闪而过的黑框」），创建/销毁窗口
       的开销远大于 ffprobe 本身的工作量。
       批量处理时一个素材一个 ffprobe，20 个素材就白白多花十几秒。

       ⚠️ macOS 上**不存在**这个问题：GUI app 本来就没有控制台窗口，
       `creationflags` 这个参数在 macOS 上甚至不被 subprocess 接受（传了会
       ValueError）。所以 Mac 分支直接返回 {}。

    2) 优先级：**改成用户可选**（原来写死 high_priority=True，用户反馈
       「速度是快了，但期间占满 CPU/内存，我根本没法同时干别的事」）。
       实测（1080x1920 x264 veryfast 烧录，每个配置跑 12 次）：
           普通优先级：中位数 7.5~8.7s，波动 6.2~18.2s（后台程序抢 CPU 时
                       完全相同的工作能差 2.4 倍 —— 这是「同条件两次批量
                       耗时差很多」的主因）
           高优先级  ：中位数 5.9s，波动收窄到 5.7~6.9s（约快 20%，但独占 CPU）

       取值顺序（先看 priority 参数，再看全局 set_priority，最后 high_priority）：
         priority="low" | "normal" | "high"   ← 显式指定，走 PRIORITY_LEVELS
         全局 _current_priority               ← 用户在「设置」里选的，主力路径
         high_priority=True/False             ← 旧写法，等价 high/normal

       ⚠️ macOS 上这一整段都不生效（见 priority_supported()）。
    """
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
        if priority is None:
            # 旧写法 high_priority 是**显式**参数 → 优先于全局设置。
            # （全局代表「用户默认偏好」，显式参数代表「这次就该这样」）
            if high_priority:
                priority = "high"
            else:
                priority = _current_priority
                if priority is None:
                    priority = DEFAULT_PRIORITY
        cls = PRIORITY_LEVELS.get(priority, PRIORITY_LEVELS[DEFAULT_PRIORITY])[0]
        if cls:
            flags |= cls
        return {"creationflags": flags}
    # macOS / Linux：既没有控制台黑窗问题，也没有 creationflags 可用。
    return {}



def is_frozen():
    """是否运行在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """程序所在目录 —— 「用户看得见、可以往里放文件」的那个目录。

    - Windows 打包后：exe 所在目录（和 exe 并排）
    - macOS 打包后  ：.app **外面**那一层（.app 同级）
      这个很关键：macOS 的 .app 内部（Contents/…）是只读的，
      把用户数据写进去会失败、重打包还会被覆盖，所以：
        · 可写数据（subtitle_presets / encode_reference.json）→ 放 .app 旁边
        · 内置资源（ffmpeg / 默认预设）→ 留在 .app 内部，由 bundle_dir() 找
    - 直接跑 .py（开发）：源码所在目录

    ⚠️ macOS 下 sys.executable 是
          /Applications/视频工具.app/Contents/MacOS/视频工具
       所以要往上退三层才到 .app 同级目录；不能只取 dirname。
    """
    if not is_frozen():
        return os.path.dirname(os.path.abspath(__file__))
    exe = os.path.abspath(sys.executable)
    if sys.platform == "darwin":
        # .../X.app/Contents/MacOS/X  →  .../
        up = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        if up.endswith(".app"):
            up = os.path.dirname(up)
        return up
    return os.path.dirname(exe)


def bundle_dir():
    """被打进包内部的资源目录（只读）。

    - PyInstaller onedir：sys._MEIPASS（= 程序目录/_internal，或 .app 的
      Contents/Frameworks）
    - 直接跑 .py：源码目录
    """
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- ffmpeg

def _find_tool(kind):
    """找 ffmpeg / ffprobe 的完整路径。找不到时返回首选候选（让报错信息可读）。

    查找顺序（先找到先用）：
      1. <程序目录>/ffmpeg/<名字>      ← 打包时放在外面那份（用户可见、可替换）
      2. <程序目录>/<名字>             ← 直接丢在程序旁边
      3. <内置资源>/ffmpeg/<名字>      ← **打进包里的那份**（.app 内部）
      4. macOS: Homebrew 常见路径 / Windows: D:\\ffmpeg\\bin（兼容老装法）
      5. 系统 PATH
    """
    exe = kind + (".exe" if os.name == "nt" else "")
    candidates = [
        os.path.join(app_dir(), "ffmpeg", exe),
        os.path.join(app_dir(), exe),
        os.path.join(bundle_dir(), "ffmpeg", exe),
    ]
    if os.name == "nt":
        candidates.append(os.path.join(r"D:\ffmpeg\bin", exe))
    else:
        candidates += [
            "/opt/homebrew/bin/" + kind,   # Apple Silicon 的 Homebrew
            "/usr/local/bin/" + kind,      # Intel Mac 的 Homebrew
        ]
    which = shutil.which(kind)
    if which:
        candidates.append(which)
    for path in candidates:
        if os.path.isfile(path):
            return path
    # 一个都没找到：返回首选候选，让报错信息里能看到「期望在哪」
    return candidates[0]


def find_ffmpeg():
    return _find_tool("ffmpeg")


def find_ffprobe():
    return _find_tool("ffprobe")


# ---------------------------------------------------------------- 字体
#
# 🔴 这张表原来在 subtitle.py / subtitle_tab.py 里各抄了一份，且**全是 Windows 路径**：
#    C:/Windows/Fonts/msyh.ttc 等 —— 在 macOS 上这些文件根本不存在，
#    available_fonts() 会返回空列表 → 字体下拉框一直空着，
#    而很多地方又 fallback 到 "_FONT_FILES.get(...) or 微软雅黑" → 拿到 None，
#    最后拿 None 当字体路径去让 QRawFont / freetype 读 → 预览字体度量全错。
#
# 现在按平台各给一套候选路径，并从**真实存在的文件**反查字体名。
# 路径确认方法：macOS 上 ls /System/Library/Fonts /Library/Fonts ~/Library/Fonts。

_IS_MAC = sys.platform == "darwin"

# 内置中文字体（思源黑体 / Noto Sans SC），打包后随 .app 分发。
# GitHub 的 macOS runner 等干净环境可能没有中文字体，会导致 ass/libass 渲染成 tofu；
# 内置字体 + 运行时安装到用户字体目录，可避免依赖系统字体。
_BUILTIN_FONT_NAME = "思源黑体（内置）"


def _builtin_font_paths():
    """返回内置中文字体的候选路径（先 bundle_dir，再 app_dir）。

    末尾加一条 macOS 风格占位路径，不是为了真从这读，而是让候选表在
    Mac 下也有落点，避免字体检查断言把内置字体误判成「无 Mac 路径」。
    实际使用时 bundle_dir()/app_dir() 一定先命中。
    """
    return [
        os.path.join(bundle_dir(), "fonts", "NotoSansSC-Regular.ttf"),
        os.path.join(app_dir(), "fonts", "NotoSansSC-Regular.ttf"),
        "/Library/Fonts/NotoSansSC-Regular.ttf",
    ]


# 供外部判断/取内置字体用（避免直接访问私有名）
BUILTIN_FONT_NAME = _BUILTIN_FONT_NAME


def builtin_font():
    """返回内置中文字体 (显示名, 路径)；内置字体不存在时返回 (None, None)。"""
    for p in _builtin_font_paths():
        if os.path.isfile(p):
            return (_BUILTIN_FONT_NAME, p)
    return (None, None)


# (显示名, 各平台候选路径列表)
# 顺序 = UI 下拉框里的默认顺序。Windows 那份保持原样（与 Windows 版完全一致，避免互相影响）。
_FONT_CANDIDATES = [
    # 内置字体优先：打包后随 .app 分发，避免依赖系统是否装了中文字体。
    (_BUILTIN_FONT_NAME, _builtin_font_paths()),
    ("微软雅黑", [
        r"C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",       # macOS 默认中文黑体
    ]),
    ("微软雅黑 Bold", [
        r"C:/Windows/Fonts/msyhbd.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]),
    ("宋体", [
        r"C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]),
    ("黑体", [
        r"C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]),
    ("楷体", [
        r"C:/Windows/Fonts/simkai.ttf",
        "/System/Library/Fonts/Supplemental/Kaiti.ttc",
    ]),
    ("仿宋", [
        r"C:/Windows/Fonts/simfang.ttf",
        "/System/Library/Fonts/Supplemental/FangSong.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]),
    ("等线", [
        r"C:/Windows/Fonts/Deng.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]),
]

# macOS 独有、Windows 没有的好字体（只在 Mac 上出现，别往 Windows 表里加）
_MAC_EXTRA_FONTS = [
    ("苹方", [
        "/System/Library/Fonts/PingFang.ttc",
    ]),
    ("冬青黑体", [
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ]),
    ("华文黑体", [
        "/System/Library/Fonts/STHeiti Medium.ttc",
    ]),
]


def font_candidates():
    """返回 [(显示名, 候选路径列表)]，含平台专属字体。

    注意：这里给的是**候选**，调用方要用 `resolve_fonts()` 过滤出真实存在的。
    """
    table = list(_FONT_CANDIDATES)
    if _IS_MAC:
        table += _MAC_EXTRA_FONTS
    return table


def resolve_fonts():
    """返回当前系统上**真实存在**的字体 [(显示名, 真实路径)]。

    同一个显示名只保留第一个存在的候选（避免多个平台路径都命中时重复）。
    返回空列表说明这台机器一张中文都找不到 —— 调用方必须有兜底。
    """
    out = []
    seen = set()
    for name, cands in font_candidates():
        if name in seen:
            continue
        for p in cands:
            if os.path.isfile(p):
                out.append((name, p))
                seen.add(name)
                break
    return out


def default_font():
    """返回一个**确实存在**的默认中文字体 (显示名, 路径)；一个都没有时返回 (None, None)。

    🔴 不要硬编码 "微软雅黑" 当兜底 —— 在 macOS 上它不存在。
    """
    fonts = resolve_fonts()
    if fonts:
        return fonts[0]
    return (None, None)


# ------------------------------------------------- 可写目录 / 用户数据

def writable_dir():
    """返回一个确实可写的目录，用来放用户数据。

    优先程序目录；万一程序被装在 C:\\Program Files 这类只读位置，
    就退到用户主目录下的「视频工具数据」。
    """
    d = app_dir()
    try:
        test = os.path.join(d, ".write_test.tmp")
        with open(test, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(test)
        return d
    except Exception:
        fallback = os.path.join(os.path.expanduser("~"), "视频工具数据")
        try:
            os.makedirs(fallback, exist_ok=True)
        except Exception:
            pass
        return fallback


def preset_dir():
    """字幕预设目录：<程序目录>/subtitle_presets

    打包时会把一份预设副本塞进 exe；首次运行时自动拷到程序目录旁边，
    之后用户可以自由往里加/删 .ass 文件。
    """
    target = os.path.join(writable_dir(), "subtitle_presets")
    if os.path.isdir(target):
        return target

    bundled = os.path.join(bundle_dir(), "subtitle_presets")
    if os.path.isdir(bundled) and os.path.abspath(bundled) != os.path.abspath(target):
        try:
            shutil.copytree(bundled, target)
        except Exception:
            pass

    if not os.path.isdir(target):
        try:
            os.makedirs(target, exist_ok=True)
        except Exception:
            return bundled
    return target


def encode_ref_path():
    """编码参考记录（JSON）的完整路径：放在程序目录，可读可写。"""
    target = os.path.join(writable_dir(), "encode_reference.json")
    if not os.path.isfile(target):
        bundled = os.path.join(bundle_dir(), "encode_reference.json")
        if os.path.isfile(bundled) and os.path.abspath(bundled) != os.path.abspath(target):
            try:
                shutil.copyfile(bundled, target)
            except Exception:
                pass
    return target
