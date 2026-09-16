# -*- coding: utf-8 -*-
"""模拟 macOS 环境，验证 Mac 版的平台分支逻辑。

⚠️ 这个脚本在 Windows 上跑，但**不改真的 sys.platform**，
   而是直接对「纯函数」做断言 + 用假的 sys.executable 验证 app_dir()。
   真正能不能在 Mac 上跑起来，还得在 Mac 上实测 —— 本脚本只保证
   「逻辑分支没写错」，不能替代真机验证。

覆盖：
  1. paths.priority_supported() 在 Windows 上为 True
  2. spawn_kwargs() Windows 下带 creationflags（回归，别被改坏）
  3. app_dir() / bundle_dir() 的 .app 路径解析（用假 sys.executable）
  4. _find_tool() 的候选列表包含 macOS 的 Homebrew 路径
  5. procctl.py 在非 Windows 下**不再 raise**，且 suspend/resume 走 os.kill
  6. utils.open_in_explorer 有 macOS 分支
  7. app.py / subtitle_tab.py 里所有 priority 相关 UI 都有平台判断
  8. 没有任何地方在非 Windows 分支调用 os.startfile
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # mac/ 根目录
sys.path.insert(0, HERE)

fails = []
oks = []


def check(name, cond, extra=""):
    if cond:
        oks.append(name)
        print("  PASS %s" % name)
    else:
        fails.append(name + (" | " + extra if extra else ""))
        print("  FAIL %s  %s" % (name, extra))


print("=" * 64)
print("Mac 版平台分支验证（在 Windows 上做静态/逻辑检查）")
print("=" * 64)
print()

# ---------------------------------------------------------------- 1 & 2
print("[1] paths.py 在本机（Windows）行为不变")
import paths

check("priority_supported() 在 Windows 返回 True",
      paths.priority_supported() is True)
kw = paths.spawn_kwargs()
check("spawn_kwargs() 仍返回 creationflags",
      "creationflags" in kw, repr(kw))
check("spawn_kwargs() 含 CREATE_NO_WINDOW",
      bool(kw.get("creationflags", 0) & 0x08000000),
      hex(kw.get("creationflags", 0)))
paths.set_priority("high")
check("set_priority('high') 生效（HIGH 0x80）",
      bool(paths.spawn_kwargs().get("creationflags", 0) & 0x80))
paths.set_priority("low")
check("set_priority('low') 生效（BELOW_NORMAL 0x4000）",
      bool(paths.spawn_kwargs().get("creationflags", 0) & 0x4000))
paths.set_priority("normal")
print()

# ---------------------------------------------------------------- 3
print("[3] app_dir() 对 macOS .app 的路径解析")
src_paths = open(os.path.join(HERE, "paths.py"), encoding="utf-8").read()

# 把 app_dir 抠出来单独执行，喂不同的 (sys.platform, sys.executable, frozen)
src_tree = ast.parse(src_paths)
fn_src = None
for node in src_tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "app_dir":
        fn_src = ast.get_source_segment(src_paths, node)
        break
check("能从 paths.py 抠出 app_dir()", fn_src is not None)


class _FakeSys:
    def __init__(self, platform, executable, frozen=True):
        self.platform = platform
        self.executable = executable
        if frozen:
            self.frozen = True


def run_app_dir(platform, executable, frozen=True):
    """在受控命名空间里跑 app_dir()，不带真实 sys。

    ⚠️ 关键：必须给被测函数喂**对应平台**的 path 模块 ——
       posixpath 处理 "/" 路径（macOS），ntpath 处理 "\\" 路径（Windows）。
       一开始我图省事统一用 os.path（= 本机的 ntpath），结果 macOS 用例
       被 Windows 的分隔符规则干扰，误报 FAIL。
    """
    import ntpath
    import posixpath
    is_posix = platform != "win32"
    fake_os = type("FakeOS", (), {})()
    fake_os.path = posixpath if is_posix else ntpath
    fake_os.name = "posix" if is_posix else "nt"
    ns = {"os": fake_os, "sys": _FakeSys(platform, executable, frozen),
          "is_frozen": lambda: frozen}
    exec(fn_src, ns)
    return ns["app_dir"]()


mac_exe = "/Applications/视频工具.app/Contents/MacOS/视频工具"
got = run_app_dir("darwin", mac_exe)
check("macOS .app → app_dir 返回到 .app 同级",
      got == "/Applications", got)

mac_exe2 = "/Users/bob/Desktop/发布/视频工具.app/Contents/MacOS/视频工具"
got2 = run_app_dir("darwin", mac_exe2)
check("带中文的 .app 路径同样正确",
      got2 == "/Users/bob/Desktop/发布", got2)

win_exe = r"C:\tools\视频工具\视频工具.exe"
got3 = run_app_dir("win32", win_exe)
check("Windows 行为不变（返回 exe 所在目录）",
      got3 == r"C:\tools\视频工具", got3)
print()

# ---------------------------------------------------------------- 4
print("[4] _find_tool 的候选路径")
fn_src_ft = None
for node in src_tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "_find_tool":
        fn_src_ft = ast.get_source_segment(src_paths, node)
        break
check("能从 paths.py 抠出 _find_tool()", fn_src_ft is not None)
# macOS 分支必须包含 homebrew 两条
check("_find_tool 含 /opt/homebrew（Apple Silicon）",
      "/opt/homebrew/bin/" in fn_src_ft)
check("_find_tool 含 /usr/local/bin（Intel）",
      "/usr/local/bin/" in fn_src_ft)
check("_find_tool 仍含 Windows 的 D:\\ffmpeg\\bin",
      r"D:\ffmpeg\bin" in fn_src_ft)
check("_find_tool 不再硬编码 .exe 后缀",
      '.exe" if os.name == "nt" else ""' in fn_src_ft)
print()

# ---------------------------------------------------------------- 5
print("[5] procctl.py 非 Windows 分支")
pc = open(os.path.join(HERE, "procctl.py"), encoding="utf-8").read()
check("procctl 不再无条件 raise RuntimeError",
      'raise RuntimeError("procctl 仅支持 Windows")' not in pc)
check("procctl 有 _IS_WIN 判断", "_IS_WIN = sys.platform" in pc)
check("procctl 有 SIGSTOP 分支", "signal.SIGSTOP" in pc)
check("procctl 有 SIGCONT 分支", "signal.SIGCONT" in pc)
check("procctl 导入了 signal", re.search(r"^import signal", pc, re.M) is not None)
pc_tree = ast.parse(pc)
# suspend/resume 在 if/else 两个分支里各定义一次（各 2 个函数），
# 所以不在 module.body 顶层 → 递归搜所有 FunctionDef。
defs = [n.name for n in ast.walk(pc_tree) if isinstance(n, ast.FunctionDef)]
check("procctl 定义了 suspend（两个平台各一份）",
      defs.count("suspend") == 2, "找到 %d 个" % defs.count("suspend"))
check("procctl 定义了 resume（两个平台各一份）",
      defs.count("resume") == 2, "找到 %d 个" % defs.count("resume"))
check("procctl 保留 ProcGroup 类",
      any(isinstance(n, ast.ClassDef) and n.name == "ProcGroup"
          for n in ast.walk(pc_tree)))
print()

# ---------------------------------------------------------------- 6
print("[6] utils.open_in_explorer 的跨平台分支")
ut = open(os.path.join(HERE, "utils.py"), encoding="utf-8").read()
check("utils 导入了 subprocess", re.search(r"^import subprocess", ut, re.M) is not None)
check("utils 导入了 sys", re.search(r"^import sys", ut, re.M) is not None)
check("open_in_explorer 有 darwin 分支", 'darwin' in ut)
check("open_in_explorer 用 open 命令", '["open", folder]' in ut)
check("open_in_explorer 有 xdg-open（Linux）", 'xdg-open' in ut)
# os.startfile 只能在 os.name == 'nt' 分支里。
# ⚠️ 要排除 docstring 里的说明文字 —— 一开始我没排除，把
#    「Windows：os.startfile（等于双击）」这种注释也当成代码报错，
#    误报了 2 个 FAIL。用 AST 取真实的调用节点更准。
ut_tree = ast.parse(ut)
_calls = [n for n in ast.walk(ut_tree)
          if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Attribute)
          and n.func.attr == "startfile"]
check("utils 里 os.startfile 只有 1 处真实调用（其余在注释里）",
      len(_calls) == 1, "实数=%d" % len(_calls))
# 手写一个保守的「在 nt 分支里」检查：往上找最近的 if 判断
_lines = ut.split("\n")
for node in _calls:
    ln = node.lineno - 1
    seg = "\n".join(_lines[max(0, ln - 12):ln])
    check("os.startfile 调用处于 os.name == 'nt' 分支内",
          'os.name == "nt"' in seg, "第 %d 行" % (ln + 1))
print()

# ---------------------------------------------------------------- 7
print("[7] app.py / subtitle_tab.py 的优先级 UI 有平台判断")
app = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
st = open(os.path.join(HERE, "subtitle_tab.py"), encoding="utf-8").read()

check("app.py 设置对话框用 priority_supported() 包住优先级行",
      "if paths.priority_supported():" in app)
check("app.py 保存时对 prio_combo 判 None",
      "if prio_combo is not None:" in app)
check("app.py 对话框高度随平台变（210/150）",
      "210 if paths.priority_supported() else 150" in app)
check("app.py 状态栏文案随平台变",
      app.count("priority_supported") >= 3)
check("subtitle_tab 的 refresh_encode_hint 有平台分支",
      "if paths.priority_supported():" in st)
check("subtitle_tab 导入了 paths", re.search(r"^import paths", st, re.M) is not None)

# ⚠️ 关键：编码参考表的「优先级」列是**数据**，不该被平台判断藏掉
check("编码参考表仍保留优先级列（数据，不随平台隐藏）",
      "PRIORITY_CN_OPTIONS" in app and '"优先级"' in app)
check("编码参考表的列定义仍含 优先级",
      '"优先级", "预设字幕"' in app.replace("\n", " ").replace("  ", " ")
      or '"优先级",' in app)
print()

# ---------------------------------------------------------------- 8
print("[8] 非 Windows 路径不得出现 os.startfile / ntdll")
for fn in ("app.py", "subtitle_tab.py", "utils.py", "procctl.py", "paths.py"):
    t = open(os.path.join(HERE, fn), encoding="utf-8").read()
    # 找所有 os.startfile 出现处，检查是否在 nt 分支
    bad = []
    for m in re.finditer(r"os\.startfile\s*\(", t):
        seg = t[max(0, m.start() - 400):m.start()]
        if 'os.name == "nt"' not in seg and "Windows" not in seg.split("\n")[-1]:
            bad.append(m.start())
    check("%s: os.startfile 都有分支保护" % fn, not bad, str(bad))

# ntdll/windll 只能在 procctl 的 if _IS_WIN 分支里
pc_parts = pc.split("else:")
check("procctl: ntdll 只出现在 Windows 分支",
      "ntdll" not in pc_parts[1] if len(pc_parts) > 1 else True)
print()

# ---------------------------------------------------------------- 9
print("[9] .spec / 脚本基本完整性")
spec = os.path.join(HERE, "video_tool_macos.spec")
sh = os.path.join(HERE, "打包mac.sh")
check("video_tool_macos.spec 存在", os.path.isfile(spec))
check("打包mac.sh 存在", os.path.isfile(sh))
if os.path.isfile(spec):
    s = open(spec, encoding="utf-8").read()
    check("spec 用了 BUNDLE（.app 必需）", "BUNDLE(" in s)
    check("spec 指定 bundle_identifier", "bundle_identifier" in s)
    check("spec 开高分屏 NSHighResolutionCapable", "NSHighResolutionCapable" in s)
    check("spec 指向 _bundled_ffmpeg", "_bundled_ffmpeg" in s)
    check("spec 用 .icns 图标", ".icns" in s)
    check("spec 仍排除 QtWebEngine（省 126MB）", "QtWebEngineCore" in s)
if os.path.isfile(sh):
    s = open(sh, encoding="utf-8").read()
    check("脚本检查 Darwin 平台", 'if [ "$(uname)" != "Darwin" ]' in s)
    check("脚本区分 arm64/Intel", "arm64" in s and "x86_64" in s)
    check("脚本会去隔离属性（xattr）", "xattr -dr com.apple.quarantine" in s)
    check("脚本跑自检并找「全部正常」", "全部正常" in s)
    check("脚本删掉旧 build/dist", "rm -rf" in s)
print()

print("=" * 64)
print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
if fails:
    print()
    for f in fails:
        print("  FAIL:", f)
print("=== %s ===" % ("全部通过" if not fails else "存在失败"))
print("=" * 64)
sys.exit(0 if not fails else 1)
