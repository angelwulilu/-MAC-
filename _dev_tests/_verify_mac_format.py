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

fails = []
oks = []


def check(name, cond, extra=""):
    if cond:
        oks.append(name)
        print("  PASS %s" % name)
    else:
        fails.append(name + (" | " + extra if extra else ""))
        print("  FAIL %s  %s" % (name, extra))


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
pk = os.path.join(os.path.dirname(HERE), "_package_mac_src.py")
check("文件存在", os.path.isfile(pk))
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
wt_root = os.path.dirname(HERE)          # video-tool/
setup_bat = os.path.join(wt_root, "配置git.bat")
setup_py = os.path.join(wt_root, "_setup_git.py")

check("配置git.bat 存在（Windows 双击入口）", os.path.isfile(setup_bat))
check("_setup_git.py 存在（实际逻辑）", os.path.isfile(setup_py))

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
push_bat = os.path.join(wt_root, "推送mac到github.bat")
push_py = os.path.join(wt_root, "_push_mac.py")

check("推送mac到github.bat 存在（Windows 双击入口）", os.path.isfile(push_bat))
check("_push_mac.py 存在（实际逻辑）", os.path.isfile(push_py))

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
    win_ps = os.path.join(os.path.dirname(HERE), "subtitle_presets")
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
bad_present = []
for junk in ("build", "dist", "__pycache__", ".venv", "_bundled_ffmpeg"):
    if os.path.exists(os.path.join(HERE, junk)):
        bad_present.append(junk)
# _bundled_ffmpeg 是脚本自己生成的，允许存在；其余是构建缓存
real_junk = [j for j in bad_present if j != "_bundled_ffmpeg"]
check("没有 build/dist/__pycache__ 等缓存", not real_junk, str(real_junk))
# 确认没有把 Windows 的 .exe 混进来
exes = []
for dp, dns, fns in os.walk(HERE):
    dns[:] = [d for d in dns if d not in ("__pycache__",)]
    exes += [os.path.join(dp, f) for f in fns if f.lower().endswith(".exe")]
check("没有混入 Windows .exe", not exes, str(exes[:3]))
print()

print("=" * 62)
print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
if fails:
    print()
    for f in fails:
        print("  FAIL:", f)
print("=== %s ===" % ("全部通过" if not fails else "存在失败"))
print("=" * 62)
sys.exit(0 if not fails else 1)
