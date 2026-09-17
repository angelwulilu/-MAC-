# -*- coding: utf-8 -*-
"""检查 Mac 交付物的「格式正确性」—— 这些在 Windows 上很容易被悄悄改坏。

bash 脚本对格式极其挑剔，Windows 上的编辑器/压缩工具经常搞坏：
  · CRLF 行尾 → macOS 报 `'\r': command not found`（整脚本跑不起来）
  · UTF-8 BOM → `#!/bin/bash` 前面多俩字节，shebang 失效
  · 文件权限     → 丢了可执行位就得用 `bash 文件` 而不是 `./文件`

这些坑我在 Windows 上看不出来（本地 bash 是坏的没法试），所以写个脚本卡住。
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 🔴 上游目录到底是「完整的 video-tool」（Windows 侧）还是「只有 mac/」（GitHub runner）？
#
# 为什么要判这个：
#   本脚本有一批断言是查 **Windows 侧工具**的（配置git.bat / _setup_git.py /
#   推送mac到github.bat / _push_mac.py）——这四个文件在 D:\video-tool\ 下，
#   **不在仓库里**。GitHub 仓库的根就是 mac/，runner 上 HERE 的父目录什么都没有。
#   实测：runner 布局下这 4 条必然失败 → 整个脚本 exit 1 → CI 第 5 步
#   「字体解析 + 交付物格式检查」1 秒就挂（run 35060031916 的真凶）。
#
# 判据：看父目录里有没有 Windows 主工程的特征文件（app.py / subtitle_presets/）。
#   有  → 完整布局，Windows 侧断言**照常严格检查**（本地自检不能被放水）
#   没有 → 精简布局（runner），这些断言**跳过**，不判失败
UPSTREAM = os.path.dirname(HERE)
HAS_WIN_SIDE = (
    os.path.isfile(os.path.join(UPSTREAM, "app.py"))
    or os.path.isdir(os.path.join(UPSTREAM, "subtitle_presets"))
)

fails = []
oks = []
skips = []


def check(name, cond, extra=""):
    if cond:
        oks.append(name)
        print("  PASS %s" % name)
    else:
        fails.append(name + (" | " + extra if extra else ""))
        print("  FAIL %s  %s" % (name, extra))


def check_win_side(name, cond, extra=""):
    """只对「完整 video-tool 布局」成立的断言。

    在精简布局（GitHub runner，上游只有 mac/）下**跳过** —— 不是失败，
    因为那些文件本来就不在仓库里，查不到是正常现象。
    """
    if not HAS_WIN_SIDE:
        skips.append(name)
        print("  SKIP %s  （上游只有 mac/，Windows 侧文件不在仓库里）" % name)
        return
    check(name, cond, extra)


print("=" * 62)
print("Mac 交付物格式检查")
print("=" * 62)
print()

# ---------------------------------------------------------------- sh 脚本
print("[1] 打包mac.sh 的格式")
sh = os.path.join(HERE, "打包mac.sh")
check("文件存在", os.path.isfile(sh))
raw = open(sh, "rb").read()
check("无 CRLF（必须是纯 LF 行尾）", raw.count(b"\r\n") == 0,
      "发现 %d 个 CRLF" % raw.count(b"\r\n"))
check("无 UTF-8 BOM", not raw.startswith(b"\xef\xbb\xbf"))
check("以 #!/ 开头（shebang 干净）", raw.startswith(b"#!/"),
      repr(raw[:12]))
check("shebang 是 bash", raw.startswith(b"#!/bin/bash") or
      raw.startswith(b"#!/usr/bin/env bash"), repr(raw[:20]))
check("有 set -e（出错即停）", b"set -e" in raw)
check("以换行结尾", raw.endswith(b"\n"))
text = raw.decode("utf-8")
check("含 Darwin 平台检查", 'if [ "$(uname)" != "Darwin" ]' in text)
print()

# ------------------------------------------------- 双架构（--both）支持
print("[1b] 打包mac.sh 的 --both 双架构支持")
check("解析 --both 参数", "WANT_BOTH=1" in text)
check("有 --help 说明", "--help" in text)
check("arm64 下载 arm 版", "ffmpeg9arm.zip" in text)
check("Intel 下载 intel 版", "ffmpeg80intel.zip" in text)
check("ffprobe 也有两份", "ffprobe9arm.zip" in text and "ffprobe80intel.zip" in text)
check("另一架构放 _alt/（不打进 .app）", "_alt" in text)
# 关键：两个 ffmpeg 不能同名塞进 .app（会撞车）→ 主份必须只有一对
check("主份路径唯一（不重复塞进 .app）",
      text.count('FF="$HERE/_bundled_ffmpeg/ffmpeg"') == 1)
check("含 chmod +x（可执行位）", "chmod +x" in text)
# 旧版写死的 7.1 地址已失效，不该再出现
check("无过期的 7.1 下载地址", "ffmpeg71arm.zip" not in text)
# 🔴 图标要按 $HERE/ 和 $HERE/../ 两个位置找。
#    原来只找 ../ —— 用工程包（顶层只有 mac/）解压出来跑时，图标会**静默丢失**。
check("图标按两个位置找（$HERE 与 $HERE/..）",
      '"$HERE/app_icon.ico"' in text and '"$HERE/../app_icon.ico"' in text)
check("找不到图标只是 warn（不中断打包）", "找不到 app_icon.ico" in text)

# 🔴 ffmpeg 下载必须**多个源**。原来只用 osxexperts.net 一家，实测那站从国内
#    访问是 HTTP 000（连接都建不起来）。它是第三方小站，挂了很正常，
#    而这一步失败会让整个打包 die。所以改成逐个源重试。
check("ffmpeg 下载有备选源（不只 osxexperts）", "evermeet.cx" in text)
check("下载失败会换下一个源（不是直接放弃）", "换下一个" in text)
check("下载带 --retry 容忍瞬时抖动", "--retry" in text)
check("下载带 --max-time（别无限挂住）", "--max-time" in text)
# 🔴 2026-09-17 改：这条原来要求「提示里给 brew 兜底」。
#    但实测 runner 上 brew 装的 ffmpeg **没有 drawtext**（编译时缺 --enable-libfreetype），
#    再写「brew install 一下就行」是**错建议** —— 会把人带进同一个坑。
#    正确指引 = 手动放一个带 drawtext 的 ffmpeg 进 _bundled_ffmpeg/。
check("下载失败提示里给了正确兜底方案（手动放带 drawtext 的 ffmpeg）",
      "_bundled_ffmpeg/" in text and "drawtext" in text)
print()

# ------------------------------------------------- 一键打包.command（双击版）
print("[1c] 一键打包.command（给双击用）")
cmd = os.path.join(HERE, "一键打包.command")
check("文件存在", os.path.isfile(cmd))
if os.path.isfile(cmd):
    craw = open(cmd, "rb").read()
    # ⚠️ .command 和 .sh 一样怕 CRLF/BOM —— 有 CRLF 就是 `'\r': command not found`，
    #    有 BOM 就是 shebang 失效，双击直接闪退。
    check("无 CRLF（纯 LF 行尾）", craw.count(b"\r\n") == 0,
          "发现 %d 个 CRLF" % craw.count(b"\r\n"))
    check("无 UTF-8 BOM", not craw.startswith(b"\xef\xbb\xbf"))
    check("以 #!/bin/bash 开头", craw.startswith(b"#!/bin/bash"),
          repr(craw[:20]))
    check("以换行结尾", craw.endswith(b"\n"))
    ctext = craw.decode("utf-8")
    # 双击时工作目录是用户主目录，不 cd 就会找不到 打包mac.sh
    check("先 cd 到脚本所在目录", 'cd "$(dirname "$0")"' in ctext)
    # 必须用 `bash 文件` 调，不能 `./文件` —— 从 Windows 压缩包解压过来会丢 x 位
    check("用 bash 调用打包脚本（不依赖 x 位）", "bash 打包mac.sh" in ctext)
    check("调的是 打包mac.sh", "打包mac.sh" in ctext)
    # set -e 在子 shell 内部生效，父脚本不受影响；但显式 set +e 更保险
    check("调用前显式 set +e（防提前退出）", "set +e" in ctext)
    check("拿到并判断退出码", "RC=$?" in ctext)
    check("失败时有提示（窗口不会白闪）", "退出码" in ctext or "出错" in ctext)
    # 最后要停住，否则窗口一闪而过看不到结果
    check("结尾有 read 停住窗口", "read -r _" in ctext)
    check("提到被 Gatekeeper 拦时怎么打开", "右键" in ctext)
print()

# ------------------------------------------------- 工程分包脚本（Windows 侧）
print("[1d] _package_mac_src.py（在 Windows 上打工程包）")
# ⚠️ 这个脚本也在仓库外（D:\video-tool\），runner 上没有 → 用 check_win_side
if not HAS_WIN_SIDE:
    print("  （上游只有 mac/ —— 这是 Windows 侧打包脚本，跳过）")
pk = os.path.join(UPSTREAM, "_package_mac_src.py")
check_win_side("文件存在", os.path.isfile(pk))
if os.path.isfile(pk):
    praw = open(pk, "rb").read()
    check("无 BOM", not praw.startswith(b"\xef\xbb\xbf"))
    try:
        compile(praw.decode("utf-8"), pk, "exec")
        check("语法可编译", True)
    except Exception as e:
        check("语法可编译", False, repr(e))
    ptext = praw.decode("utf-8")
    # 🔴 __pycache__ 必须排掉：Windows 上跑过 py_compile 就会生成，
    #    里面是 Windows 的 .pyc，拷到 Mac 上没用还可能被误加载。
    check("排除 __pycache__", "__pycache__" in ptext)
    check("排除 .pyc/.pyo", '".pyc"' in ptext or ".pyc" in ptext)
    check("排除 build/dist", '"build"' in ptext and '"dist"' in ptext)
    # 🔴 zip 内路径必须转成正斜杠，否则 Mac 解出怪文件名
    check("zip 内路径用正斜杠", 'replace("\\\\", "/")' in ptext or
          "replace('\\\\', '/')" in ptext)
    check("顶层套一个文件夹（解压不散落）", "arc = " in ptext)
    check("检查必备文件", "REQUIRED" in ptext)
    check("有 --dry-run", "--dry-run" in ptext)
print()

# ---------------------------------------------------------------- 文本文件
print("[2] 文本文件编码（说明书要给收包人看）")
for fn in ("给收包人看-使用说明.txt", "README-打包指南.md"):
    p = os.path.join(HERE, fn)
    check("%s 存在" % fn, os.path.isfile(p))
    if os.path.isfile(p):
        r = open(p, "rb").read()
        # 纯 ASCII 或合法 UTF-8 都行；关键是不能有 BOM（记事本会显示乱码）
        check("%s 无 BOM" % fn, not r.startswith(b"\xef\xbb\xbf"))
        try:
            r.decode("utf-8")
            check("%s 是合法 UTF-8" % fn, True)
        except Exception as e:
            check("%s 是合法 UTF-8" % fn, False, repr(e))

# 收包人最可能卡住的「架构不对」必须写进说明书里
_guide = os.path.join(HERE, "给收包人看-使用说明.txt")
if os.path.isfile(_guide):
    g = open(_guide, encoding="utf-8").read()
    check("说明书含「芯片架构不匹配」的排查", "架构" in g and "osxexperts" in g)
    check("说明书含「不用重新打包」的换法", "不用重新打包" in g)

# 🔴 所有 .py / .sh / .command 必须纯 LF 无 BOM。
#    .py 虽然在 Mac 上带 CRLF 也能跑，但：
#      ① 和仓库里其他文件不一致（git diff 会出现整文件重写）；
#      ② .sh/.command 带 CRLF 会直接 `bad interpreter: /bin/bash^M` 跑不起来。
#    这类问题在 Windows 上用 Edit 工具改文件时**很容易悄悄引入**（踩过一次：
#    _verify_mac_format.py 被写成 771 个 CRLF），所以纳入自检。
for _fn in sorted(os.listdir(HERE)):
    if not _fn.lower().endswith((".py", ".sh", ".command")):
        continue
    _p = os.path.join(HERE, _fn)
    if not os.path.isfile(_p):
        continue
    _r = open(_p, "rb").read()
    check("%s 纯 LF 无 BOM" % _fn,
          b"\r\n" not in _r and not _r.startswith(b"\xef\xbb\xbf"),
          "CRLF x%d%s" % (_r.count(b"\r\n"),
                          "，有 BOM" if _r.startswith(b"\xef\xbb\xbf") else ""))
for _fn in ("_verify_mac_fonts.py", "_verify_mac_format.py",
            "_verify_mac_platform.py", "_verify_runner_layout.py"):
    _p = os.path.join(HERE, "_dev_tests", _fn)
    if not os.path.isfile(_p):
        continue
    _r = open(_p, "rb").read()
    check("_dev_tests/%s 纯 LF 无 BOM" % _fn,
          b"\r\n" not in _r and not _r.startswith(b"\xef\xbb\xbf"),
          "CRLF x%d" % _r.count(b"\r\n"))
print()

# ---------------------------------------------------------------- spec 文件
print("[3] spec 文件")
spec = os.path.join(HERE, "video_tool_macos.spec")
check("文件存在", os.path.isfile(spec))
if os.path.isfile(spec):
    r = open(spec, "rb").read()
    check("无 BOM", not r.startswith(b"\xef\xbb\xbf"))
    try:
        compile(r.decode("utf-8"), spec, "exec")
        check("语法可编译（纯 Python）", True)
    except Exception as e:
        check("语法可编译（纯 Python）", False, repr(e))
print()

# ------------------------------------------------- CI / 免费 Mac 环境
print("[3b] GitHub Actions（免费 Mac 真机）")
wf_dir = os.path.join(HERE, ".github", "workflows")
pk_wf = os.path.join(wf_dir, "mac-package.yml")
check("mac-package.yml 存在", os.path.isfile(pk_wf))
check("mac-smoke-test.yml 存在",
      os.path.isfile(os.path.join(wf_dir, "mac-smoke-test.yml")))
if os.path.isfile(pk_wf):
    wraw = open(pk_wf, "rb").read()
    check("无 BOM", not wraw.startswith(b"\xef\xbb\xbf"))
    wtext = wraw.decode("utf-8")
    # 🔴 额度相关的硬要求：绝不能 push 就自动跑
    #    macOS runner 在私有仓库按 10 倍折算，push 自动跑会把额度烧光。
    check("只手动触发（不含 push: 自动触发）",
          "workflow_dispatch" in wtext and "\n  push:" not in wtext)
    check("用 macos-latest runner", "macos-latest" in wtext)
    # 🔴 必须设超时，否则卡住会一直烧额度
    check("设了 timeout-minutes", "timeout-minutes" in wtext)
    # 🔴🔴 CI 里必须**先装 ffmpeg** 再打包 —— 这是第一次运行失败的根因：
    #    打包mac.sh 的 [3/6] 要找一份 macOS ffmpeg 打进 .app，顺序是
    #    「本地已有 → use_local(command -v ffmpeg) → 从 osxexperts.net 下载」。
    #    runner 没预装 ffmpeg → 前两条落空 → 走第三方小站下载 → 那个站实测
    #    从国内访问 HTTP 000 根本连不上 → 下载失败 → die → 整个打包挂掉。
    #    brew install 之后 use_local 立刻命中，绕开那个不靠谱的源。
    _ff_idx = -1
    _build_idx = -1
    for _i, _ln in enumerate(wtext.splitlines()):
        if "装 ffmpeg" in _ln and "name:" in _ln:
            _ff_idx = _i
        if "打包mac.sh --one" in _ln:
            _build_idx = _i
    check("workflow 里装了 ffmpeg（打包前必做）",
          "brew install ffmpeg" in wtext)
    check("装 ffmpeg 的步骤在打包之前",
          _ff_idx >= 0 and _build_idx >= 0 and _ff_idx < _build_idx,
          "ffmpeg@%d build@%d" % (_ff_idx, _build_idx))
    check("装 ffmpeg 那步解释了为什么必需",
          "use_local" in wtext or "osxexperts" in wtext)
    # 🔴 调的是仓库里那份脚本（不是抄一份逻辑进 workflow）——
    #    这样「CI 跑的」和「本地跑的」才是同一份，不会互相掩盖问题
    check("调用 打包mac.sh（而非抄一份逻辑）", "bash 打包mac.sh" in wtext)
    # CI 的 .app 只装得下本机架构 → 用 --one 省 1~2 分钟
    check("打包用 --one（CI 里省时间）", "--one" in wtext)
    # .app 是文件夹，必须 ditto --keepParent 才保住资源属性
    check("用 ditto 压 .app（保住资源属性）", "ditto -c -k" in wtext and
          "--keepParent" in wtext)
    check("上传 .app 成品 artifact", "upload-artifact" in wtext)
    check("设了 artifact 保留天数", "retention-days" in wtext)
    # 界面相关的能力 CI 验不了，得说清楚
    check("说明 CI 验不了图形界面", "图形界面" in wtext or "没有显示器" in wtext)
    check("顺带跑中文字幕烧录", "burn_subtitle_manual" in wtext)
    # 仓库布局兼容（mac/ 当根 或 video-tool/ 当根）
    check("兼容两种仓库布局", "GITHUB_WORKSPACE/mac/subtitle.py" in wtext)

    # 🔴 两个 workflow 名字必须能一眼区分 —— 用户曾点错，
    #    在「Mac 冒烟测试」里找 run_tests（那是 mac-package.yml 的输入项）。
    check("mac-package.yml 名字点明「产出 .app」",
          "产出 .app" in wtext, wtext.splitlines()[0] if wtext else "")
    check("mac-package.yml 说明别点错",
          "别点错" in wtext or "点错" in wtext)
    # 两个输入项必须都在（用户就是要找 run_tests）
    check("有 run_tests 输入项", "run_tests" in wtext)
    check("有 make_dmg 输入项", "make_dmg" in wtext)
    check("输入项声明为 boolean 类型", "type: boolean" in wtext)
    check("run_tests 默认勾选", "default: true" in wtext)

sm_wf = os.path.join(wf_dir, "mac-smoke-test.yml")
if os.path.isfile(sm_wf):
    stext_wf = open(sm_wf, encoding="utf-8").read()
    # 名字也要点明「不管打包」，否则和上面那个分不清
    check("mac-smoke-test.yml 名字点明「不管打包」",
          "不管打包" in stext_wf, stext_wf.splitlines()[0] if stext_wf else "")
    # 它原本 workflow_dispatch 后面是空的 → 点 Run workflow 没表单，
    # 用户会以为「没有 run_tests」。补一个输入项当「有表单」的信号。
    check("冒烟测试也有输入项（给用户『有表单』的信号）",
          "burn_sample" in stext_wf)
    # 🔴 冒烟测试绝不能带 run_tests，否则两个 workflow 就真分不清了
    check("冒烟测试不含 run_tests（避免混淆）", "run_tests" not in stext_wf)
    check("冒烟测试不打包 .app",
          "PyInstaller" not in stext_wf or "不打包" in stext_wf)
    check("冒烟测试只手动触发", "workflow_dispatch" in stext_wf
          and "\n  push:" not in stext_wf)
    # 两个 workflow 名字不能一样
    pk_name = ""
    if os.path.isfile(pk_wf):
        for ln in open(pk_wf, encoding="utf-8"):
            if ln.startswith("name:"):
                pk_name = ln.strip()
                break
    sm_name = stext_wf.splitlines()[0].strip() if stext_wf else ""
    check("两个 workflow 名字不同", pk_name != sm_name,
          "pk=%r sm=%r" % (pk_name, sm_name))

# 🔴 两个 workflow 都必须跑「精简布局复跑」——这是防「只在本地能过」的硬防线。
#    背景：自检脚本里查仓库外文件的断言曾让 CI 1 秒挂掉（run 35060031916）。
#    _verify_runner_layout.py 把 mac/ 拷到临时目录当仓库根，复现 runner 布局再跑一遍，
#    能抓出同类问题。少接一个 workflow，那条流水线就少了这道保险。
_runner_verifier = "_verify_runner_layout.py"
for _wf_name, _wf_path in (("mac-package.yml", pk_wf),
                           ("mac-smoke-test.yml", sm_wf)):
    _wt = open(_wf_path, encoding="utf-8").read() if os.path.isfile(_wf_path) else ""
    check("%s 跑了精简布局复跑（防『只在本地能过』）" % _wf_name,
          _runner_verifier in _wt, "缺 %s" % _runner_verifier)
check("精简布局复跑脚本存在",
      os.path.isfile(os.path.join(HERE, "_dev_tests", _runner_verifier)))

# .gitignore 必须挡住会出问题的那几类
gi = os.path.join(HERE, ".gitignore")
check(".gitignore 存在", os.path.isfile(gi))
if os.path.isfile(gi):
    g = open(gi, encoding="utf-8").read()
    for pat, why in [("__pycache__", "Windows 字节码"), ("build/", "构建产物"),
                     ("dist/", "成品几百MB"), ("_bundled_ffmpeg", "临时的ffmpeg")]:
        check(".gitignore 挡住 %s" % pat, pat in g, why)
    check(".gitignore 挡住 .DS_Store", ".DS_Store" in g)

check("测试说明文档存在",
      os.path.isfile(os.path.join(HERE, "README-在免费Mac上测试.md")))
print()

# ------------------------------------------------- 配置 git（Windows 侧）
print("[3c] git 配置向导（Windows 侧，推代码前必做）")
if not HAS_WIN_SIDE:
    print("  （上游只有 mac/ —— 这组是 Windows 侧工具，跳过）")
wt_root = UPSTREAM                       # video-tool/
setup_bat = os.path.join(wt_root, "配置git.bat")
setup_py = os.path.join(wt_root, "_setup_git.py")

check_win_side("配置git.bat 存在（Windows 双击入口）", os.path.isfile(setup_bat))
check_win_side("_setup_git.py 存在（实际逻辑）", os.path.isfile(setup_py))

if os.path.isfile(setup_bat):
    braw = open(setup_bat, "rb").read()
    bcnt_crlf = braw.count(b"\r\n")
    bcnt_lf = braw.count(b"\n") - bcnt_crlf
    btext = braw.decode("utf-8", "replace")
    # 🔴 Windows .bat 必须 CRLF：LF 的 bat 在某些 cmd 版本下会出怪问题
    check("配置git.bat 是 CRLF 行尾（Windows 要求）",
          bcnt_crlf > 0 and bcnt_lf == 0, "CRLF=%d LF=%d" % (bcnt_crlf, bcnt_lf))
    check("配置git.bat 无 BOM", not braw.startswith(b"\xef\xbb\xbf"))
    check("配置git.bat 声明 chcp 65001", "chcp 65001" in btext)
    # 🔴 中文提示必须交给 Python 输出，不能在 bat 里 echo 中文：
    #    cmd 在 65001 下会静默吞掉部分中文 echo 行（实测多种写法都不可靠）
    check("配置git.bat 调用 _setup_git.py（中文不交给 bat）",
          "_setup_git.py" in btext)
    # 不该有的危险操作
    for op in ("reg add", "reg delete", "regedit", "--system"):
        check("配置git.bat 不含危险操作 %s" % op, op.lower() not in btext.lower())

if os.path.isfile(setup_py):
    sraw = open(setup_py, "rb").read()
    stext = sraw.decode("utf-8", "replace")
    try:
        import ast as _ast
        _ast.parse(stext)
        check("_setup_git.py 语法可编译", True)
    except Exception as e:
        check("_setup_git.py 语法可编译", False, repr(e))

    # 四条配置必须齐全，且 autocrlf 必须 input
    check("写入 core.quotepath=false", '"core.quotepath"' in stext)
    check("写入 core.autocrlf=input", '"core.autocrlf"' in stext)
    check("autocrlf 用 input（不是 true/false）",
          '"input"' in stext and '"true"' not in stext.split("autocrlf")[1][:80])
    check("声明了 user.name / user.email", '"user.name"' in stext and '"user.email"' in stext)
    # 必须限定 --global，绝不碰系统级
    check("所有写入都带 --global", '"--global"' in stext)
    check("没有 --system 调用", '"--system"' not in stext)
    # 🔴 必须排除 WorkBuddy 自带的 PortableGit —— 那不是用户的 git，
    #    不在系统 PATH，用它配出来的东西用户看不到、也用不上。
    check("排除 WorkBuddy 内置 PortableGit", ".workbuddy" in stext)
    # 找不到 git 时要给出安装指引，不能干巴巴报错
    check("找不到 git 时给安装指引",
          "git-scm.com/download/win" in stext and "winget install Git.Git" in stext)
    # 写之前要确认、写之后要自检
    check("写入前有确认步骤", "确认写入" in stext)
    check("写入后有自检", "自检" in stext)
    check("自检会核对 4 项", stext.count("[OK]") >= 1 and "REQUIRED_CONFIG" in stext)
    # 中文输出走 Python stdout，不能出现 cmd 那套 echo
    check("中文提示在 Python 里（非 bat echo）", "sys.stdout.reconfigure" in stext)

# 文档里必须提到这一步，且写明装 git 是前提
rmac = os.path.join(HERE, "README-在免费Mac上测试.md")
if os.path.isfile(rmac):
    rd = open(rmac, encoding="utf-8").read()
    check("README 提到 autocrlf", "autocrlf" in rd)
    # 🔴 必须说清「先装 git」——用户机器上根本没装 git
    check("README 说明需要先装 Git", "Git for Windows" in rd or "git-scm.com" in rd)
    check("README 提到配置向导", "配置git" in rd or "_setup_git" in rd)
print()

# ------------------------------------------------- 推送 github（Windows 侧）
print("[3d] 推送向导（Windows 侧，把 mac/ 推上去）")
if not HAS_WIN_SIDE:
    print("  （上游只有 mac/ —— 这组是 Windows 侧工具，跳过）")
push_bat = os.path.join(wt_root, "推送mac到github.bat")
push_py = os.path.join(wt_root, "_push_mac.py")

check_win_side("推送mac到github.bat 存在（Windows 双击入口）",
               os.path.isfile(push_bat))
check_win_side("_push_mac.py 存在（实际逻辑）", os.path.isfile(push_py))

if os.path.isfile(push_bat):
    praw = open(push_bat, "rb").read()
    pcnt_crlf = praw.count(b"\r\n")
    pcnt_lf = praw.count(b"\n") - pcnt_crlf
    ptext = praw.decode("utf-8", "replace")
    check("推送 bat 是 CRLF 行尾（Windows 要求）",
          pcnt_crlf > 0 and pcnt_lf == 0,
          "CRLF=%d LF=%d" % (pcnt_crlf, pcnt_lf))
    check("推送 bat 无 BOM", not praw.startswith(b"\xef\xbb\xbf"))
    check("推送 bat 声明 chcp 65001", "chcp 65001" in ptext)
    # 同 配置git.bat：中文一律交给 Python，bat 只当入口
    check("推送 bat 调用 _push_mac.py（中文不交给 bat）",
          "_push_mac.py" in ptext)
    for op in ("reg add", "reg delete", "regedit", "push --force", "-f origin",
               "reset --hard", "clean -fd"):
        check("推送 bat 不含危险操作 %s" % op, op.lower() not in ptext.lower())

if os.path.isfile(push_py):
    praw2 = open(push_py, "rb").read()
    ptext2 = praw2.decode("utf-8", "replace")
    try:
        import ast as _ast2
        _ast2.parse(ptext2)
        check("_push_mac.py 语法可编译", True)
    except Exception as e:
        check("_push_mac.py 语法可编译", False, repr(e))

    # 🔴 中文输出走 Python stdout（bat 里 echo 中文会被 cmd 吞）
    check("推送向导中文在 Python 里", "sys.stdout.reconfigure" in ptext2)
    # 🔴 必须排除 WorkBuddy 自带 PortableGit
    check("推送向导排除 WorkBuddy 内置 PortableGit", ".workbuddy" in ptext2)
    # 找不到 git 时给安装指引
    check("推送向导找不到 git 时给安装指引",
          "git-scm.com/download/win" in ptext2
          and "winget install Git.Git" in ptext2)
    # 推送前必须体检：身份 / 行尾 / gitignore / 体积
    check("推送前体检 git 身份", "user.name" in ptext2 and "user.email" in ptext2)
    check("推送前体检 autocrlf", "core.autocrlf" in ptext2)
    check("推送前体检 .sh 行尾", "打包mac.sh" not in ptext2 and ".command" in ptext2)
    check("推送前体检 .gitignore", ".gitignore" in ptext2)
    check("推送前预览待推内容体积", "总体积" in ptext2)
    # 仓库地址要校验，不能闭眼推
    check("仓库地址做格式校验", "RE_URL_HTTPS" in ptext2 and "RE_URL_SSH" in ptext2)
    check("地址非法时提示重填", "不像仓库地址" in ptext2)
    # 🔴 关键：Mac 脚本必须是 LF —— 向导要能识别 CRLF 并解释 autocrlf 会修
    check("识别 Mac 脚本 CRLF 并解释会自动转 LF",
          "bad interpreter" in ptext2 or "CR（CRLF）" in ptext2)
    # 只推 mac/，绝不推整个 video-tool
    check("默认只推 mac/ 目录", 'os.path.join(HERE, "mac")' in ptext2
          or '"mac"' in ptext2)
    # 每一步都要有确认，不能默默推
    check("推送前有确认步骤", "确认推送" in ptext2)
    check("体检有警告时二次确认", "还是要继续推送吗" in ptext2)
    # 🔴 绝不允许强推（会毁掉远程历史）
    for bad in ("push\", \"--force", "push\", \"-f", "--force-with-lease"):
        check("不会强推 %s" % bad, bad not in ptext2)
    # 失败时要给可操作的排查提示
    check("失败时给排查提示",
          "authentication failed" in ptext2 and "repository not found" in ptext2)
    # 提交信息要固定，避免空提交报错
    check("有改动才 commit（避免空提交报错）",
          "status" in ptext2 and "没有新改动" in ptext2)

if os.path.isfile(rmac):
    rd2 = open(rmac, encoding="utf-8").read()
    check("README 提到推送向导", "推送mac到github" in rd2 or "_push_mac" in rd2)
    check("README 说明「只推 mac/ 不推整个目录」", "只推 `mac/`" in rd2
          or "只推 mac/" in rd2)
print()

need_py = ["app.py", "paths.py", "procctl.py", "utils.py", "concat.py",
           "subtitle.py", "reencode.py", "probe.py", "config.py", "stats.py",
           "subtitle_tab.py", "batch_dialog.py"]
missing = [n for n in need_py if not os.path.isfile(os.path.join(HERE, n))]
check("12 个源文件齐全", not missing, "缺: %s" % missing)
check("subtitle_presets/ 存在", os.path.isdir(os.path.join(HERE, "subtitle_presets")))
ps = os.path.join(HERE, "subtitle_presets")
if os.path.isdir(ps):
    ass = sorted(f for f in os.listdir(ps) if f.lower().endswith(".ass"))
    # 不该写死数量（预设会增删）。真正的判据是「和 Windows 源码目录完全一致」，
    # 否则 Mac 版会悄悄少预设 / 多预设，用户在 Mac 上才发现。
    win_ps = os.path.join(UPSTREAM, "subtitle_presets")
    if os.path.isdir(win_ps):
        win_ass = sorted(f for f in os.listdir(win_ps) if f.lower().endswith(".ass"))
        check("预设与 Windows 版完全一致（%d 个）" % len(win_ass),
              ass == win_ass,
              "Mac=%s / Win=%s" % (ass, win_ass))
    else:
        check("预设非空（找不到 Windows 目录，退化为非空检查）", len(ass) > 0,
              "实际 %d 个: %s" % (len(ass), ass))
check("encode_reference.json 存在",
      os.path.isfile(os.path.join(HERE, "encode_reference.json")))
check("打包mac.sh 存在", os.path.isfile(os.path.join(HERE, "打包mac.sh")))
check("一键打包.command 存在", os.path.isfile(cmd))
check("video_tool_macos.spec 存在", os.path.isfile(spec))
# 🔴 图标必须**在 mac/ 里**有一份：工程包顶层只有 mac/，
#    打包脚本要能就地找到它，否则打出来的 .app 没图标。
check("mac/app_icon.ico 存在", os.path.isfile(os.path.join(HERE, "app_icon.ico")))
print()

# ---------------------------------------------------------------- 不该有的
print("[5] 不该被打包进去的东西")
# 🔴 口径修正（bug 复盘 run 35061816016）：
#    原来写的是「mac/ 目录下不许存在 build/dist/__pycache__」——**这是错的口径**。
#    ① `__pycache__` 是 Python 跑任何一个 .py 就会生成的**运行时产物**，
#       CI 里我们刚跑过 `python3 _dev_tests/*.py` → 必然存在 → 断言必挂；
#    ② 这些东西本来就由 `.gitignore` 挡住，**根本不会进仓库**，
#       所以真正要验的是「.gitignore 排掉了」，而不是「文件系统里没有」。
#    本地之所以一直过，只是因为跑测试前恰好没生成（或已清）—— 又一个「只在本地能过」。
_gitignore = ""
_gi_path = os.path.join(HERE, ".gitignore")
if os.path.isfile(_gi_path):
    _gitignore = open(_gi_path, encoding="utf-8").read()

for _junk, _why in [("build", "构建产物"), ("dist", "构建产物"),
                    ("__pycache__", "Python 字节码"),
                    (".venv", "虚拟环境"),
                    ("_bundled_ffmpeg", "打包脚本自己生成的 ffmpeg")]:
    check(".gitignore 排除了 %s（%s）" % (_junk, _why),
          _junk in _gitignore)

# 真正的「不该出现」= 会被提交进仓库、又不该在的东西。
# __pycache__ 只提示、不判失败（运行时必然产生）。
_suspicious = []
for _junk in ("build", "dist", ".venv"):
    if os.path.exists(os.path.join(HERE, _junk)):
        _suspicious.append(_junk)
check("工作区没有 build/dist/.venv（真被打包会出问题）",
      not _suspicious, str(_suspicious))

_has_pycache = os.path.exists(os.path.join(HERE, "__pycache__"))
print("  ℹ️  __pycache__ %s（.gitignore 已排除，不影响仓库；本地跑过测试就会生成）"
      % ("存在" if _has_pycache else "不存在"))
# 确认没有把 Windows 的 .exe 混进来
exes = []
for dp, dns, fns in os.walk(HERE):
    dns[:] = [d for d in dns if d not in ("__pycache__",)]
    exes += [os.path.join(dp, f) for f in fns if f.lower().endswith(".exe")]
check("没有混入 Windows .exe", not exes, str(exes[:3]))
print()

# ------------------------------------------------- 元检查：不许再引用仓库外文件
#
# 🔴 这一组是给「未来的自己」上的保险。
#    bug 复盘（run 35060031916）：本脚本原来有 5 条断言指向 D:\video-tool\ 下的
#    Windows 侧文件（配置git.bat / _setup_git.py / 推送mac到github.bat /
#    _push_mac.py / _package_mac_src.py），而 GitHub 仓库的根就是 mac/ ——
#    runner 上这些文件不存在 → 5 条断言失败 → exit 1 → CI 第 5 步 1 秒挂。
#    本地永远测不出来（本地它们都在）。
#
#    所以现在加一条元检查：扫描本目录三个自检脚本的源码，凡是出现
#    os.path.dirname(HERE) / 父目录拼接的地方，**必须**配套 check_win_side
#    或显式的 HAS_WIN_SIDE 判断。否则直接判失败。
print("[6] runner 布局安全性（别再有「只在本地能过」的断言）")
import ast as _ast_meta

_self_dir = os.path.join(HERE, "_dev_tests")
# ⚠️ _verify_runner_layout.py 也扫 —— 它自己同样跑在 runner 上，
#    要是它内部有「查仓库外文件」的逻辑，一样会挂。
#    _verify_push_mac.py 不扫：它要连真远程，不适合在 CI 里跑。
_meta_files = ["_verify_mac_format.py", "_verify_mac_fonts.py",
               "_verify_mac_platform.py", "_verify_runner_layout.py"]

for _fn in _meta_files:
    _p = os.path.join(_self_dir, _fn)
    if not os.path.isfile(_p):
        check("%s 存在（元检查）" % _fn, False)

_own = os.path.join(_self_dir, "_verify_mac_format.py")
_own_src = open(_own, encoding="utf-8").read()
_own_tree = _ast_meta.parse(_own_src)

# ── 用 AST 精确判定，别用文本窗口（第一版用 6 行窗口，误报了 3 处：
#    HAS_WIN_SIDE 的定义本身、win_ps 的降级分支、print 里的说明文字）──

# 🔴 关键：真正的用法是**先赋给变量再查**，不是内联：
#       setup_bat = os.path.join(UPSTREAM, "配置git.bat")
#       check_win_side("配置git.bat 存在", os.path.isfile(setup_bat))
#   所以第一步要收集「哪些变量名是 UPSTREAM 拼出来的」，第二步再看它们被谁用。
_upstream_vars = set()
for _node in _ast_meta.walk(_own_tree):
    if isinstance(_node, _ast_meta.Assign):
        # 右值里有没有 join(UPSTREAM|wt_root, ...)
        _has = False
        for _sub in _ast_meta.walk(_node.value):
            if isinstance(_sub, _ast_meta.Call) and isinstance(_sub.func, _ast_meta.Attribute) \
                    and _sub.func.attr == "join" and _sub.args:
                _a0 = _sub.args[0]
                if isinstance(_a0, _ast_meta.Name) and _a0.id in ("UPSTREAM", "wt_root"):
                    _has = True
        if _has:
            for _t in _node.targets:
                if isinstance(_t, _ast_meta.Name):
                    _upstream_vars.add(_t.id)
                # wt_root = UPSTREAM 这种链式别名
                elif isinstance(_t, _ast_meta.Tuple):
                    for _e in _t.elts:
                        if isinstance(_e, _ast_meta.Name):
                            _upstream_vars.add(_e.id)
# 别名：wt_root = UPSTREAM
for _node in _own_tree.body:
    if isinstance(_node, _ast_meta.Assign) and len(_node.targets) == 1:
        _t = _node.targets[0]
        if isinstance(_t, _ast_meta.Name) and isinstance(_node.value, _ast_meta.Name) \
                and _node.value.id in ("UPSTREAM", "wt_root"):
            _upstream_vars.add(_t.id)

# HAS_WIN_SIDE 是 bool，不是路径变量；留着会让检测变松
# （`check("x", HAS_WIN_SIDE)` 也会被算成「受保护」）
_upstream_vars.discard("HAS_WIN_SIDE")

# 再看每个 check / check_win_side 调用，实参里有没有这些变量
_upstream_file_checks = []
for _node in _ast_meta.walk(_own_tree):
    if not isinstance(_node, _ast_meta.Call):
        continue
    _fname = None
    if isinstance(_node.func, _ast_meta.Name):
        _fname = _node.func.id
    if _fname not in ("check", "check_win_side"):
        continue
    _uses = False
    for _sub in _ast_meta.walk(_node):
        if isinstance(_sub, _ast_meta.Name) and _sub.id in _upstream_vars:
            _uses = True
        # 内联写法也要覆盖
        if isinstance(_sub, _ast_meta.Call) and isinstance(_sub.func, _ast_meta.Attribute) \
                and _sub.func.attr == "join" and _sub.args:
            _a0 = _sub.args[0]
            if isinstance(_a0, _ast_meta.Name) and _a0.id in ("UPSTREAM", "wt_root"):
                _uses = True
    if _uses:
        _upstream_file_checks.append((_node.lineno, _fname == "check_win_side"))

_unprotected = [ln for ln, protected in _upstream_file_checks if not protected]
check("查上游目录的 check 都用了 check_win_side（共 %d 处，认到变量 %s）"
      % (len(_upstream_file_checks), sorted(_upstream_vars)),
      not _unprotected and len(_upstream_file_checks) > 0,
      "未保护行号：%s" % _unprotected)

# ── 三个自检脚本里，AST 层面不许出现「用父目录拼出的路径」直接喂给 check ──
#   （即：不允许 os.path.isfile(os.path.join(os.path.dirname(HERE), ...)) 这种）
#   HAS_WIN_SIDE 的定义、UPSTREAM 的赋值本身不算。
_bad = []
for _fn in _meta_files:
    _fp = os.path.join(_self_dir, _fn)
    if not os.path.isfile(_fp):
        continue
    _tree = _ast_meta.parse(open(_fp, encoding="utf-8").read())
    for _node in _ast_meta.walk(_tree):
        if isinstance(_node, _ast_meta.Call) and isinstance(_node.func, _ast_meta.Attribute) \
                and _node.func.attr == "join":
            # 直接内联 dirname(HERE) 当第一个参数 → 危险
            if _node.args and isinstance(_node.args[0], _ast_meta.Call) \
                    and isinstance(_node.args[0].func, _ast_meta.Attribute) \
                    and _node.args[0].func.attr == "dirname":
                _bad.append((_fn, _node.lineno))
check("没有脚本内联 dirname(HERE) 拼路径（统一走 UPSTREAM 变量）",
      not _bad, str(_bad[:3]))

# ── 真的碰了上游目录的脚本，必须显式声明 HAS_WIN_SIDE（让「跳过」意图可读）──
#    ⚠️ 判据不能只看文本里有没有 "UPSTREAM" 字样 —— 注释里提到也算，
#    会误报（_verify_runner_layout.py 只在注释里解释了这件事）。
#    改成用 AST 看：有没有真的 `os.path.join(UPSTREAM|wt_root, ...)` 这种调用。
for _fn in _meta_files:
    _fp = os.path.join(_self_dir, _fn)
    if not os.path.isfile(_fp):
        continue
    _txt = open(_fp, encoding="utf-8").read()
    _t = _ast_meta.parse(_txt)
    _really_touches = False
    for _n in _ast_meta.walk(_t):
        if isinstance(_n, _ast_meta.Call) and isinstance(_n.func, _ast_meta.Attribute) \
                and _n.func.attr == "join" and _n.args:
            _a0 = _n.args[0]
            # 直接 UPSTREAM/wt_root，或别名字符串赋值（如 os.path.join(UPSTREAM, ...)）
            if isinstance(_a0, _ast_meta.Name) and _a0.id in ("UPSTREAM", "wt_root"):
                _really_touches = True
    if _really_touches:
        _has_decl = ("HAS_WIN_SIDE" in _txt) or ("check_win_side" in _txt)
        check("%s 碰了上游目录并声明了跳过逻辑" % _fn, _has_decl,
              "用了 UPSTREAM 拼接但没有 HAS_WIN_SIDE/check_win_side")
print()

# ------------------------------------------------- 元检查：平台维度也不能「只在本地能过」
#
# 🔴 第二次踩坑（run 35061816016）：修完「布局」问题后第 5 步仍然挂。
#    真凶换成了「平台」——
#      · _verify_mac_platform.py [1] 有 5 条裸 check 断言「paths.py 在 Windows 行为不变」，
#        在 macOS runner 上必然全 FAIL → exit 1。
#      · 而 _verify_runner_layout.py 当时**只模拟了目录布局、没模拟 sys.platform**，
#        本机（Windows）跑照样全过 → 又一次「只在本地能过」。
#
#    现在上两条保险：
#      ① 平台脚本里凡是断言「Windows 行为」的，必须走 check_windows（非 Windows 跳过）；
#      ② 复跑脚本必须伪装 sys.platform（见下一条 check）。
print("[7] 平台维度安全性（别再有「只在 Windows 成立」的裸断言）")

# ① 在 _verify_mac_platform.py 里找「**运行时**断言 Windows 行为、却用裸 check 包着」的
#
#    ⚠️ 判据要排除「静态源码检查」—— 那类断言虽然文本里有 Windows 字样，
#    但验的是「源码里还有没有这个字符串/这个分支」，**平台无关**，
#    在 macOS 上也该正常跑（如「Windows 行为不变」是喂假 sys.executable 的纯函数测试、
#    「_find_tool 仍含 D:\ffmpeg\bin」是查文本、「ntdll 只出现在 Windows 分支」是查 AST/文本）。
#    真正危险的是**调用 paths.*() 的运行时断言** —— 那类必须走 check_windows。
_plat = os.path.join(_self_dir, "_verify_mac_platform.py")
_plat_bad = []
if os.path.isfile(_plat):
    _plat_src = open(_plat, encoding="utf-8").read()
    _pt = _ast_meta.parse(_plat_src)
    # 「运行时」的判据：断言调用里出现了对 paths 模块的属性调用
    # （paths.priority_supported() / paths.spawn_kwargs() / paths.set_priority()）
    for _n in _ast_meta.walk(_pt):
        if not isinstance(_n, _ast_meta.Call):
            continue
        if not (isinstance(_n.func, _ast_meta.Name) and _n.func.id == "check"):
            continue
        _runtime = False
        for _sub in _ast_meta.walk(_n):
            if isinstance(_sub, _ast_meta.Call) \
                    and isinstance(_sub.func, _ast_meta.Attribute) \
                    and isinstance(_sub.func.value, _ast_meta.Name) \
                    and _sub.func.value.id == "paths":
                _runtime = True
        if _runtime:
            _nm = ""
            if _n.args and isinstance(_n.args[0], _ast_meta.Constant):
                _nm = str(_n.args[0].value)
            _plat_bad.append((_n.lineno, _nm[:44]))
check("平台脚本里「运行时」的 paths 断言都走了 check_windows（裸的 %d 条）"
      % len(_plat_bad), not _plat_bad, str(_plat_bad[:4]))

# ② 复跑脚本必须真的伪装平台（否则又只能抓一半）
_rl = os.path.join(_self_dir, "_verify_runner_layout.py")
_rl_txt = open(_rl, encoding="utf-8").read() if os.path.isfile(_rl) else ""
check("复跑脚本伪装了 sys.platform（否则只能抓「布局」抓不到「平台」）",
      'sys.platform = fake_platform' in _rl_txt and 'fake_platform="darwin"' in _rl_txt)

# ③ 两个 workflow 都必须跑这个复跑脚本
#    ⚠️ HERE 就是 mac/（= 仓库根），.github 在它**里面**，不是外面。
_wf_dir = os.path.join(HERE, ".github", "workflows")
for _wf_name in ("mac-package.yml", "mac-smoke-test.yml"):
    _wf_path = os.path.join(_wf_dir, _wf_name)
    _wtxt = open(_wf_path, encoding="utf-8").read() if os.path.isfile(_wf_path) else ""
    check("%s 跑了平台+布局复跑" % _wf_name,
          "_verify_runner_layout.py" in _wtxt)
print()

# ---------------------------------------------------------------- CI 不能卡死
print("[8] CI 不能卡死（模态弹窗 / 缺超时兜底）")
# 🔴 2026-09-16 的教训：app.py 的 --selftest 结束时弹**模态** QMessageBox 等人点「确定」，
#    CI 上没人点 → 永久阻塞 → 撞 timeout-minutes 被判 cancelled，两次各烧 45 分钟。
#    ⚠️ QT_QPA_PLATFORM=offscreen 只解决「显示」，**不解决「阻塞」**。
#    下面这几条就是把这个坑钉住，别拆。
_sh_path = os.path.join(HERE, "打包mac.sh")
_sh_txt = open(_sh_path, encoding="utf-8").read() if os.path.isfile(_sh_path) else ""
_app_path = os.path.join(HERE, "app.py")
_app_txt = open(_app_path, encoding="utf-8").read() if os.path.isfile(_app_path) else ""
_pkg_path = os.path.join(_wf_dir, "mac-package.yml")
_pkg_txt = open(_pkg_path, encoding="utf-8").read() if os.path.isfile(_pkg_path) else ""

# ① 打包脚本调 --selftest 时必须屏蔽弹窗
check("打包mac.sh 调 --selftest 时设了 VIDEOTOOL_SELFTEST_NOGUI（否则 CI 必卡死）",
      "--selftest" in _sh_txt and "VIDEOTOOL_SELFTEST_NOGUI=1" in _sh_txt)

# ② 必须有超时兜底，不能无限等
check("打包mac.sh 的自检有超时看门狗（不会无限期干等）",
      "SELFTEST_TIMEOUT" in _sh_txt and "kill -TERM" in _sh_txt)

# ③ app.py 自己也要能判「非交互」，不能只靠外部传环境变量
check("app.py 自检会自己判断非交互环境（不只依赖外部环境变量）",
      "_selftest_skip_gui" in _app_txt and "isatty" in _app_txt
      and "offscreen" in _app_txt)

# ④ workflow 把两道保险都设上
check("mac-package.yml 设了 VIDEOTOOL_SELFTEST_NOGUI",
      "VIDEOTOOL_SELFTEST_NOGUI" in _pkg_txt)
check("mac-package.yml 设了 PYTHONUNBUFFERED（卡住时也看得到跑到哪）",
      "PYTHONUNBUFFERED" in _pkg_txt)

# ⑤ timeout 不能过紧（45 分钟实测不够，一卡就整轮报废）
_tmo = 0
for _ln in _pkg_txt.splitlines():
    _ls = _ln.strip()
    if _ls.startswith("timeout-minutes:"):
        try:
            _tmo = max(_tmo, int(_ls.split(":", 1)[1].strip().split()[0]))
        except ValueError:
            pass
check("mac-package.yml 的 timeout-minutes >= 60（实测 45 太紧）",
      _tmo >= 60, "实际 %d" % _tmo)

# ⑥ 自检输出必须能进 CI 日志（卡住时才知道跑到哪）
check("打包mac.sh 会把自检输出 cat 出来（不是只写文件）",
      "cat /tmp/vt_selftest.log" in _sh_txt)

# ⑦ 🔴 交付物必须**排在验证之前**。
#    2026-09-17 run #7：「烧中文字幕」验证挂了，把后面的「打包 zip / 上传 .app」
#    全带成 skipped —— .app 明明打好了却下不到，白等一轮。
#    所以顺序钉死：先 zip + 上传，再跑各种验证。
_i_up = _pkg_txt.find("- name: 上传 .app 成品")
_i_burn = _pkg_txt.find("- name: 烧中文字幕")
check("「上传 .app 成品」排在「烧中文字幕」之前（验证挂了也能拿到 .app）",
      0 <= _i_up < _i_burn,
      "上传@%d 烧字幕@%d" % (_i_up, _i_burn))

check("「烧中文字幕」设了 continue-on-error（它是验证，不该挡住产物）",
      "continue-on-error: true" in _pkg_txt[_i_burn:_i_burn + 400])

# ⑧ 字体度量依赖：漏装 → .app 自检报「模块 fontTools.ttLib / freetype 失败」，
#    而且**不报错**，只是排版精度静默降级，最容易一直发现不了。
check("打包mac.sh 的 pip 清单含 fontTools 与 freetype-py",
      "fontTools freetype-py" in _sh_txt)
check("workflow 装依赖也含 fontTools 与 freetype-py",
      "fontTools freetype-py" in _pkg_txt)
check("打包日志 artifact 带上 /tmp/burn.log（烧录验证失败时才有线索）",
      "/tmp/burn.log" in _pkg_txt)

# ⑨ 🔴 ffmpeg 必须带 drawtext —— 否则 .app 的「批量添加字幕」整个功能是坏的。
#    2026-09-17：runner 上 brew 装的 ffmpeg 编译时没开 --enable-libfreetype，
#    没有 drawtext 滤镜，却被 use_local 原样打进 .app，而自检只跑 -version、一路绿灯。
check("打包mac.sh 有 drawtext 能力校验函数（_ff_has_drawtext）",
      "_ff_has_drawtext" in _sh_txt)

# ⚠️ 切函数体不能用「从 find() 起固定长度」—— 那样窗口会越过函数末尾，
#    把后面主流程里的同名调用也算进来，于是断言永远通过。
#    （2026-09-17 钓鱼测试抓到：抽掉 use_local 里的门槛，断言竟然还是绿的。）
def _sh_func(txt, name):
    """粗略切出一个 shell 函数体：从 `name() {` 到行首的 `}`。"""
    i = txt.find(name + "() {")
    if i < 0:
        return ""
    j = txt.find("\n}", i)
    return txt[i:j + 2] if j > 0 else txt[i:]

_ul = _sh_func(_sh_txt, "use_local")
_dl = _sh_func(_sh_txt, "dl_one")
check("能切出 use_local / dl_one 的函数体（切不出来说明改名了，得同步改断言）",
      len(_ul) > 200 and len(_dl) > 200, "use_local=%d dl_one=%d" % (len(_ul), len(_dl)))
check("use_local 会拒绝「没有 drawtext」的本机 ffmpeg（不许直接复制进包）",
      "_ff_has_drawtext" in _ul and "return 1" in _ul, "函数体 %d 字符" % len(_ul))
check("下载来的 ffmpeg 也要过 drawtext 校验（不合格就换下一个源）",
      "_ff_has_drawtext" in _dl and "continue" in _dl, "函数体 %d 字符" % len(_dl))

check("打包前有硬门槛：内置 ffmpeg 缺 drawtext 就 die（宁可不出包）",
      "仍然没有 drawtext 滤镜" in _sh_txt and "已阻止打包" in _sh_txt)

check("已有的 _bundled_ffmpeg/ 缓存也要先验 drawtext（残缺版不许反复复用）",
      "旧版本残留" in _sh_txt)

check("mac-package.yml 打包后有 drawtext/subtitles 硬校验（缺了就 ::error + exit 1）",
      "硬校验：内置 ffmpeg 能烧字幕" in _pkg_txt
      and "::error title=内置 ffmpeg 缺滤镜" in _pkg_txt)

check("装 ffmpeg 那步会明确报告「本机没有 drawtext」（不再静默通过）",
      "本机 ffmpeg 没有 drawtext" in _pkg_txt)

check("app.py 自检会探测 ffmpeg 滤镜能力 + 真烧一帧（不再只看 -version）",
      "_ff_capabilities" in _app_txt and "_selftest_burn" in _app_txt)
print()

print("[9] 自检本身不许「静默失效」（2026-09-17 实测踩过的三个坑）")
# 🔴 背景：这三个 bug 让 `--selftest` 要么直接崩、要么**假装正常**，而且都很隐蔽 ——
#    真正致命的是第二个：一个 `except: pass` 把 NameError 吞了，于是「滤镜缺失」
#    永远是假警报，连人都被带到错误方向（去查 ffmpeg，其实写错了变量）。
#    而 CI 里「.app 自检」排在「上传 .app 成品」之前、且没有 continue-on-error
#    → 自检一崩，.app 就传不上来。所以这几条必须钉住。
_app_lines = _app_txt.splitlines()
_paths_path2 = os.path.join(HERE, "paths.py")
_paths_txt = open(_paths_path2, encoding="utf-8").read() if os.path.isfile(_paths_path2) else ""


def _lines_starting(pat):
    """返回所有「去掉缩进后以 pat 开头」的行号（1 起）。"""
    return [i for i, l in enumerate(_app_lines, 1) if l.strip().startswith(pat)]


# 模块级导入 = 顶格那一行（函数体内那个是缩进的，不算）
_i_sub = next((i for i, l in enumerate(_app_lines, 1)
               if l.startswith("import subprocess")), -1)
check("app.py 在模块级 import subprocess（顶格那行；函数里局部导入不算）",
      _i_sub > 0, "顶格 import subprocess @行 %s" % _i_sub)

_i_bad_def = _lines_starting("bad = []")
_i_bad_use = _lines_starting('bad.append("ffmpeg 缺')
check("app.py 的 bad = [] 定义在首次使用之前（否则滤镜缺失时 UnboundLocalError 崩自检）",
      bool(_i_bad_def) and bool(_i_bad_use) and min(_i_bad_def) < min(_i_bad_use),
      "bad=[] @%s，首次使用 @%s" % (_i_bad_def or "未找到", _i_bad_use or "未找到"))

check("_ff_capabilities 探测失败会留痕（不能 except: pass —— 那会把故障读成「滤镜缺失」）",
      # ⚠️ 必须连等号一起查：_selftest 里那句 str(_caps["_error"]) 会
      #    意外「包含」caps["_error"] 这个子串，只查子串会漏判（钓鱼测试抓出来的）。
      'caps["_error"] =' in _app_txt or "caps['_error'] =" in _app_txt)

check("自检里试烧一帧的调用有 try/except 兜底（不许因它崩掉整个自检）",
      "试烧过程异常" in _app_txt)

check("_selftest_burn 用的字体 API 确实存在（用错模块/函数名 = AttributeError 崩自检）",
      ("def default_font" in _paths_txt) or ("available_fonts" in _app_txt))
print()

print("=" * 62)
if skips:
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(oks), len(fails), len(skips)))
    print("（跳过的是 Windows 侧文件检查 —— 上游只有 mac/ 时本就不该有它们）")
else:
    print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
if fails:
    print()
    for f in fails:
        print("  FAIL:", f)
print("=== %s ===" % ("全部通过" if not fails else "存在失败"))
print("=" * 62)
sys.exit(0 if not fails else 1)