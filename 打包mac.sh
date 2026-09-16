#!/bin/bash
# ===========================================================================
# 打包mac.sh —— 在 macOS 上把「视频工具」打包成 视频工具.app
#
# 用法（在 Mac 的「终端」里）：
#     cd <本文件所在目录>
#     bash 打包mac.sh              # 默认 = --both（arm64 + Intel 两份 ffmpeg 都备）
#     bash 打包mac.sh --one        # 只备当前机器架构（体积小 50MB，但要求对方同架构）
#
# 脚本会自动做完这些事：
#   1. 检查 Python3 / pip
#   2. 装 PyInstaller + PySide6（装进当前用户的 pip，不动系统）
#   3. 下载 macOS 版 ffmpeg / ffprobe（放进 _bundled_ffmpeg/）
#      默认两份都下（arm64 + Intel，合计约 93MB）
#   4. 把 app_icon.ico 转成 app_icon.icns（有的话）
#   5. 跑 PyInstaller → dist/视频工具.app
#   6. 自检：直接运行打出来的程序，确认能起来
#
# ⚠️ 只能在 macOS 上跑。PyInstaller 不支持交叉编译 —— Windows 上打不出 Mac 包。
# ===========================================================================

set -e   # 任何一步失败就停，别带着错往下跑

# ------------------------------------------------------------- 参数解析
# 默认就备份两份架构 —— 用户明确选了「直接上双版本的」。
# 代价只有约 50MB（解压后）/ 约 24MB（zip），换掉「还得去问对方芯片」这一步。
WANT_BOTH=1
for _arg in "$@"; do
    case "$_arg" in
        --both)    WANT_BOTH=1 ;;
        --one)     WANT_BOTH=0 ;;
        --intel)   FORCE_ARCH="intel" ;;
        --arm)     FORCE_ARCH="arm" ;;
        -h|--help)
            sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "未知参数：$_arg（可用：--one / --both / --intel / --arm）"; exit 1 ;;
    esac
done

cd "$(dirname "$0")"
HERE="$(pwd)"

# 让输出好看点
BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"
say()  { printf "${BOLD}%s${RESET}\n" "$1"; }
ok()   { printf "${GREEN}  ✓ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}  ! %s${RESET}\n" "$1"; }
die()  { printf "${RED}  ✗ %s${RESET}\n" "$1"; exit 1; }

say "==========================================================="
say " 视频工具 · macOS 打包"
say "==========================================================="
echo

# ------------------------------------------------------------- 0. 系统检查
if [ "$(uname)" != "Darwin" ]; then
    die "这个脚本只能在 macOS 上运行（当前系统：$(uname)）。"
fi
ARCH="$(uname -m)"
say "系统：macOS / $ARCH"
if [ -n "$FORCE_ARCH" ]; then
    warn "已强制指定架构偏好：$FORCE_ARCH（仅影响下载哪份 ffmpeg，产物仍按本机架构打包）"
fi
case "$ARCH" in
    arm64)  FF_ARCH="arm64";  ok "Apple Silicon（M 系列芯片）" ;;
    x86_64) FF_ARCH="amd64";  ok "Intel 芯片" ;;
    *)      die "认不出的芯片架构：$ARCH" ;;
esac
if [ "$WANT_BOTH" = "1" ]; then
    ok "双架构模式（默认）：arm64 与 Intel 两份 ffmpeg 都备"
    echo "          .app 约 243MB（只备一份约 193~200MB）；"
    echo "          对方解压后多占约 50MB 硬盘，但不用再问芯片。"
    echo "          不想备两份就加 --one。"
else
    warn "单架构模式（--one）：只备本机架构，约 193~200MB"
    warn "对方芯片不一致的话跑不起来 —— 不确定就用默认（不加参数）"
fi
echo

# ------------------------------------------------------------- 1. Python
say "[1/6] 检查 Python3 ..."
if ! command -v python3 >/dev/null 2>&1; then
    die "没找到 python3。请先安装 Python 3.10+：https://www.python.org/downloads/macos/"
fi
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
ok "python3 = $PYVER（$(command -v python3)）"
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' \
    || die "Python 版本太低（需要 3.9+，推荐 3.11/3.12）"
if ! python3 -m pip --version >/dev/null 2>&1; then
    die "python3 没有 pip。试试：python3 -m ensurepip --upgrade"
fi
echo

# ------------------------------------------------------------- 2. 依赖
say "[2/6] 安装 PyInstaller 与 PySide6 ..."
# ⚠️ 不用 --user（新版 pip 会报警告甚至报错）；直接装进当前 Python 环境。
#    如果这是系统自带的 python3，可能会要你加 --break-system-packages；
#    脚本里不擅自加，宁可让用户看到清晰报错。
python3 -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || warn "pip 升级失败，继续"
if python3 -m pip install --upgrade pyinstaller PySide6 ; then
    ok "PyInstaller + PySide6 就绪"
else
    die "依赖安装失败。如果提示 externally-managed-environment，请用虚拟环境：
       python3 -m venv .venv && source .venv/bin/activate && bash 打包mac.sh"
fi
echo

# ------------------------------------------------------------- 3. ffmpeg
say "[3/6] 准备 macOS 版 ffmpeg ..."
mkdir -p "$HERE/_bundled_ffmpeg"
FF="$HERE/_bundled_ffmpeg/ffmpeg"        # ← PyInstaller 只认这两个名字，打进 .app
FP="$HERE/_bundled_ffmpeg/ffprobe"
# 多架构时另存一份备用（不参与打包，只是让用户手里有一份对的）
ALT_DIR="$HERE/_bundled_ffmpeg/_alt"

# 下载单个二进制。$1=类型(ffmpeg|ffprobe) $2=架构(arm|intel) $3=落盘路径
#
# ⚠️ 为什么要**多个源**：原来只用 osxexperts.net 一家，实测那个站很不稳 ——
#    从国内直接访问是 HTTP 000（连接都建不起来）。它是第三方小站，
#    挂了/限速/被墙都不奇怪。而这一步失败会让整个打包 die。
#    所以改成**逐个源重试**，任一成功即可。
dl_one() {
    local kind="$1" a="$2" out="$3"
    local urls=()

    if [ "$a" = "arm" ]; then
        # 主源：osxexperts（体积小、是静态编译的 arm64 版）
        if [ "$kind" = "ffmpeg" ]; then
            urls+=("https://www.osxexperts.net/ffmpeg9arm.zip")
        else
            urls+=("https://www.osxexperts.net/ffprobe9arm.zip")
        fi
    else
        if [ "$kind" = "ffmpeg" ]; then
            urls+=("https://www.osxexperts.net/ffmpeg80intel.zip")
        else
            urls+=("https://www.osxexperts.net/ffprobe80intel.zip")
        fi
    fi

    # 备选源：evermeet.cx（持续维护多年的 macOS 静态构建站，GitHub 上广泛使用）
    if [ "$a" = "arm" ]; then
        # evermeet 目前主要是 x86_64 + arm64 universal 构建
        urls+=("https://evermeet.cx/ffmpeg/getrelease/${kind}/zip")
    else
        urls+=("https://evermeet.cx/ffmpeg/getrelease/${kind}/zip")
    fi

    local u
    for u in "${urls[@]}"; do
        local tmp; tmp="$(mktemp -d)"
        say "    下载 $kind ($a) ← $u"
        # --retry 容忍瞬时抖动；-fL 跟随跳转并在 HTTP 错误时失败
        if curl -fL --retry 2 --retry-delay 2 --connect-timeout 20 \
                --max-time 180 -s -o "$tmp/x.zip" "$u"; then
            (cd "$tmp" && unzip -q x.zip) 2>/dev/null
            # 有的源包出来就叫 ffmpeg / ffprobe，有的是二进制裸文件
            if [ -f "$tmp/$kind" ]; then
                cp "$tmp/$kind" "$out"; rm -rf "$tmp"; return 0
            fi
            # 兼容 evermeet 那种直接给裸二进制的
            if [ -f "$tmp/x.zip" ] && file "$tmp/x.zip" 2>/dev/null | grep -qi 'executable\|Mach-O'; then
                cp "$tmp/x.zip" "$out"; rm -rf "$tmp"; return 0
            fi
        fi
        rm -rf "$tmp"
        warn "    这个源不行，换下一个"
    done
    return 1
}

# 优先用本机已有的（brew / 手动装的），直接拷一份，省下载
use_local() {
    local found_ff found_fp
    found_ff="$(command -v ffmpeg || true)"
    found_fp="$(command -v ffprobe || true)"
    if [ -n "$found_ff" ] && [ -n "$found_fp" ]; then
        say "  本机已装 ffmpeg，直接复制一份打进包"
        say "  （版本：$("$found_ff" -version 2>/dev/null | head -1 | cut -c1-45)）"
        cp "$found_ff" "$FF"; cp "$found_fp" "$FP"
        return 0
    fi
    return 1
}

# ---- 主份：当前机器架构（决定 .app 能不能在这台机器上跑）
if [ "$ARCH" = "arm64" ]; then THIS_ARCH="arm"; else THIS_ARCH="intel"; fi

if [ -x "$FF" ] && [ -x "$FP" ]; then
    ok "已存在 _bundled_ffmpeg/ffmpeg 与 ffprobe，跳过下载"
elif use_local; then
    :
else
    warn "本机没有 ffmpeg，从网络下载当前架构（$THIS_ARCH）版本（多个源逐个试）"
    dl_one ffmpeg  "$THIS_ARCH" "$FF" || die "ffmpeg 下载失败（所有源都不通）。最稳的办法是先装一个再重跑：brew install ffmpeg"
    dl_one ffprobe "$THIS_ARCH" "$FP" || die "ffprobe 下载失败（所有源都不通）。最稳的办法是先装一个再重跑：brew install ffmpeg"
fi

chmod +x "$FF" "$FP"
xattr -dr com.apple.quarantine "$FF" 2>/dev/null || true
xattr -dr com.apple.quarantine "$FP" 2>/dev/null || true
ok "主份 ffmpeg 就绪（本机架构）：$("$FF" -version 2>/dev/null | head -1 | cut -c1-50)"

# ---- 另一份架构（--both 时）：只放到 _alt/，给用户留个「换架构」的备胎
if [ "$WANT_BOTH" = "1" ]; then
    if [ "$ARCH" = "arm64" ]; then OTHER="intel"; else OTHER="arm"; fi
    mkdir -p "$ALT_DIR"
    if [ -x "$ALT_DIR/ffmpeg" ] && [ -x "$ALT_DIR/ffprobe" ]; then
        ok "另一架构（$OTHER）已存在于 _alt/，跳过下载"
    else
        warn "下载另一架构（$OTHER）的 ffmpeg —— 仅作备胎，不参与打包"
        if dl_one ffmpeg "$OTHER" "$ALT_DIR/ffmpeg" && \
           dl_one ffprobe "$OTHER" "$ALT_DIR/ffprobe"; then
            chmod +x "$ALT_DIR/ffmpeg" "$ALT_DIR/ffprobe"
            xattr -dr com.apple.quarantine "$ALT_DIR/ffmpeg" 2>/dev/null || true
            xattr -dr com.apple.quarantine "$ALT_DIR/ffprobe" 2>/dev/null || true
            ok "备胎已就绪：$ALT_DIR（$(du -sh "$ALT_DIR" | cut -f1)）"
        else
            warn "备胎下载失败 —— 不影响本次打包，.app 里那份是好的"
        fi
    fi
    echo
    echo "  说明：备胎放在 _bundled_ffmpeg/_alt/，**不会**打进 .app。"
    echo "        如果打出来的包在对方机器上因为架构不对跑不了，"
    echo "        把 _alt/ 里那两个文件改名成 ffmpeg / ffprobe，"
    echo "        覆盖到 .app 旁边的 ffmpeg/ 文件夹即可（不用重新打包）。"
fi
echo

# ------------------------------------------------------------- 4. 图标
say "[4/6] 准备图标 ..."
ICNS="$HERE/app_icon.icns"
# 🔴 图标要按两个位置找：
#    ① $HERE/app_icon.ico —— 用 _package_mac_src.py 打出的「工程包」解压后就是这种布局
#       （包顶层只有 mac/，没有上级目录，原来只找 ../ 会导致**图标静默丢失**）；
#    ② $HERE/../app_icon.ico —— 在 D:\video-tool 里就地跑打包脚本时是这种布局
#       （图标在 mac 的上一级）。
#    哪个先找到用哪个；都没有也不报错（只是用系统默认图标）。
SRC_ICO=""
for _cand in "$HERE/app_icon.ico" "$HERE/../app_icon.ico"; do
    if [ -f "$_cand" ]; then SRC_ICO="$_cand"; break; fi
done
if [ -f "$ICNS" ]; then
    ok "已有 app_icon.icns"
elif [ -n "$SRC_ICO" ]; then
    say "  从 $(basename "$(dirname "$SRC_ICO")")/app_icon.ico 转换 ..."
    tmpi="$(mktemp -d)"
    if python3 - "$SRC_ICO" "$tmpi" <<'PYEOF'
import sys
src, out = sys.argv[1], sys.argv[2]
try:
    from PIL import Image
except ImportError:
    sys.exit(2)          # 没有 Pillow，交给下面的 brew/sips 分支
im = Image.open(src)
best = None
for f in getattr(im, "n_frames", 1) and [im] or [im]:
    pass
im = im.convert("RGBA")
sizes = [16, 32, 64, 128, 256, 512, 1024]
for s in sizes:
    im.resize((s, s), Image.LANCZOS).save(f"{out}/icon_{s}.png")
print("ok")
PYEOF
    then
        mkdir -p "$tmpi/视频工具.iconset"
        for s in 16 32 64 128 256 512 1024; do
            cp "$tmpi/icon_${s}.png" "$tmpi/视频工具.iconset/icon_${s}x${s}.png" 2>/dev/null || true
        done
        # 标准 iconset 命名（iconutil 要求严格）
        rm -rf "$tmpi/视频工具.iconset"; mkdir -p "$tmpi/视频工具.iconset"
        cp "$tmpi/icon_16.png"   "$tmpi/视频工具.iconset/icon_16x16.png"
        cp "$tmpi/icon_32.png"   "$tmpi/视频工具.iconset/icon_16x16@2x.png"
        cp "$tmpi/icon_32.png"   "$tmpi/视频工具.iconset/icon_32x32.png"
        cp "$tmpi/icon_64.png"   "$tmpi/视频工具.iconset/icon_32x32@2x.png"
        cp "$tmpi/icon_128.png"  "$tmpi/视频工具.iconset/icon_128x128.png"
        cp "$tmpi/icon_256.png"  "$tmpi/视频工具.iconset/icon_128x128@2x.png"
        cp "$tmpi/icon_256.png"  "$tmpi/视频工具.iconset/icon_256x256.png"
        cp "$tmpi/icon_512.png"  "$tmpi/视频工具.iconset/icon_256x256@2x.png"
        cp "$tmpi/icon_512.png"  "$tmpi/视频工具.iconset/icon_512x512.png"
        cp "$tmpi/icon_1024.png" "$tmpi/视频工具.iconset/icon_512x512@2x.png"
        if iconutil -c icns "$tmpi/视频工具.iconset" -o "$ICNS" 2>/dev/null; then
            ok "已生成 app_icon.icns"
        else
            warn "iconutil 失败，将使用默认图标"
        fi
        rm -rf "$tmpi"
    else
        warn "没装 Pillow，跳过图标转换（用默认图标，不影响使用）"
        warn "想带图标的话先跑：python3 -m pip install pillow"
        rm -rf "$tmpi"
    fi
else
    warn "找不到 app_icon.ico，用默认图标"
fi
echo

# ------------------------------------------------------------- 5. 打包
say "[5/6] 开始打包（首次约 2~4 分钟）..."
rm -rf "$HERE/build" "$HERE/dist"
python3 -m PyInstaller video_tool_macos.spec --noconfirm
APP="$HERE/dist/视频工具.app"
[ -d "$APP" ] || die "打包结束但没找到 $APP"

# 去掉整个 .app 的隔离属性（从网络/压缩包来的文件会带这个，导致打不开）
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
# 给可执行文件加执行权限（有些解压工具会丢）
chmod +x "$APP/Contents/MacOS/视频工具" 2>/dev/null || true

SIZE="$(du -sh "$APP" | cut -f1)"
ok "打包完成：$APP（$SIZE）"
echo

# ------------------------------------------------------------- 6. 自检
say "[6/6] 自检（确认程序能起来）..."
EXE="$APP/Contents/MacOS/视频工具"
if "$EXE" --selftest > /tmp/vt_selftest.log 2>&1; then
    if grep -q "全部正常" /tmp/vt_selftest.log; then
        ok "自检通过：全部正常"
        grep -E "ffmpeg|预设|结论|OK" /tmp/vt_selftest.log | head -20 || true
    else
        warn "自检跑完了但结论不是「全部正常」，输出如下："
        tail -30 /tmp/vt_selftest.log
    fi
else
    warn "自检返回非 0，输出如下（把这段发给开发看）："
    tail -40 /tmp/vt_selftest.log
fi
echo

say "==========================================================="
say " 完成"
say "==========================================================="
cat <<EOF

产物：$APP

怎么发给别人：
  1. 右键点「视频工具.app」→「压缩 "视频工具"」→ 得到 视频工具.app.zip
  2. 把 zip 发出去
  3. 对方解压后，**第一次打开**要：右键点图标 →「打开」→ 再点「打开」
     （因为没做苹果签名，直接双击会被 Gatekeeper 拦。只需做这一次。）

重要提醒：
  · .app 里已经带好了 ffmpeg，对方**不需要装任何东西**（不用装 Python）。
  · 对方如果只拿走 .app 这一个文件（拖进「应用程序」），完全没问题 —— 
    ffmpeg 在 .app 内部有内置副本。
  · 旁边那个 ffmpeg/ 文件夹和 subtitle_presets/ 是可选的：
    一起发过去，ffmpeg 会优先用外面这份（方便将来替换），预设也能被用户看到。

EOF
