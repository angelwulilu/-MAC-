#!/bin/bash
# ===========================================================================
# fetch_ffmpeg_mac.sh —— 确保拿到一个「带 drawtext 滤镜」的 macOS ffmpeg
#
# 用法：
#     bash fetch_ffmpeg_mac.sh <输出目录>
#       → 会把 ffmpeg / ffprobe 两个二进制放到 <输出目录>/ 下
#
# 为什么需要它（2026-09-17 真实事故）：
#     brew 装的 ffmpeg 默认编译时**没开 --enable-libfreetype**，
#     于是根本没有 drawtext 滤镜，烧中文字幕会报 No such filter: 'drawtext'。
#     而 GitHub 的 macos runner 上 brew 装的就是这种残缺版，
#     直接拿来用 → 字幕烧录功能整个是坏的（打包那轮差点发出去）。
#
#     本脚本逻辑与打包脚本 打包mac.sh [3/6] 对齐：
#       ① 已存在且带 drawtext → 跳过下载
#       ② 本机已装且带 drawtext → 直接拷贝
#       ③ 都没有 → 从多个静态构建源下载，每个都验 drawtext，不合格换下一个
#       ④ 最终仍没有 → 明确报错退出
#
# 下载源（任一可达即可，逐个试）：
#     · osxexperts.net            —— 静态编译的 arm64 / intel 构建
#     · evermeet.cx              —— 多年维护的 macOS 静态构建
#     · eugeneware/ffmpeg-static（GitHub Release）—— GitHub 托管，最稳
# ===========================================================================

set -e

OUT="${1:-$PWD/ffmpeg-bin}"
mkdir -p "$OUT"
FF="$OUT/ffmpeg"
FP="$OUT/ffprobe"

# ---- 能力校验 ----
_has_filter() {
    local e="$1" f="$2"
    [ -f "$e" ] || return 1
    [ -x "$e" ] || chmod +x "$e" 2>/dev/null || true
    "$e" -hide_banner -filters 2>/dev/null | grep -qw "$f"
}
has_drawtext()  { _has_filter "$1" drawtext; }
has_subtitles() { _has_filter "$1" subtitles; }

# ---- ① 已有且合格 ----
if [ -x "$FF" ] && has_drawtext "$FF"; then
    echo "✅ $FF 已存在且带 drawtext，跳过下载"
    exit 0
fi

# ---- ② 本机已装的（brew/homebrew 等），带 drawtext 就拷贝 ----
SYS_FF="$(command -v ffmpeg || true)"
if [ -n "$SYS_FF" ] && has_drawtext "$SYS_FF"; then
    echo "本机 ffmpeg 已带 drawtext，拷贝到 $FF"
    cp "$SYS_FF" "$FF"
    SYS_FP="$(command -v ffprobe || true)"
    [ -n "$SYS_FP" ] && cp "$SYS_FP" "$FP" 2>/dev/null || true
    echo "✅ 就绪：$($FF -version 2>/dev/null | head -1)"
    exit 0
fi
if [ -n "$SYS_FF" ]; then
    echo "⚠️ 本机 ffmpeg 无 drawtext（$("$SYS_FF" -version 2>/dev/null | head -1)），改用下载的静态构建"
fi

# ---- ③ 下载（多源，按本机架构选 arm / intel）----
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then A=arm; else A=intel; fi

dl_one() {
    local kind="$1" out="$2"
    local urls=()
    if [ "$A" = "arm" ]; then
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
    urls+=("https://evermeet.cx/ffmpeg/getrelease/${kind}/zip")
    urls+=("https://github.com/eugeneware/ffmpeg-static/releases/latest/download/${kind}-darwin-${A}")

    local u
    for u in "${urls[@]}"; do
        local tmp; tmp="$(mktemp -d)"
        echo "  尝试下载 $kind ($A) ← $u"
        if curl -fL --retry 2 --retry-delay 2 --connect-timeout 20 --max-time 180 -s -o "$tmp/x.zip" "$u"; then
            (cd "$tmp" && unzip -q x.zip) 2>/dev/null || true
            if [ -f "$tmp/$kind" ]; then
                cp "$tmp/$kind" "$out" 2>/dev/null || { rm -rf "$tmp"; continue; }
                chmod +x "$out"
                if [ "$kind" = "ffmpeg" ] && ! has_drawtext "$out"; then
                    echo "    ⚠️ 该源 ffmpeg 无 drawtext，换下一个"; rm -f "$out"; rm -rf "$tmp"; continue
                fi
                rm -rf "$tmp"; return 0
            fi
            # 兼容 evermeet / ffmpeg-static 那种「直接给裸二进制」的源
            if [ -f "$tmp/x.zip" ] && file "$tmp/x.zip" 2>/dev/null | grep -qi 'executable\|Mach-O'; then
                cp "$tmp/x.zip" "$out" 2>/dev/null || { rm -rf "$tmp"; continue; }
                chmod +x "$out"
                if [ "$kind" = "ffmpeg" ] && ! has_drawtext "$out"; then
                    echo "    ⚠️ 该源 ffmpeg 无 drawtext，换下一个"; rm -f "$out"; rm -rf "$tmp"; continue
                fi
                rm -rf "$tmp"; return 0
            fi
        fi
        rm -rf "$tmp"
        echo "    ⚠️ 该源不可用，换下一个"
    done
    return 1
}

echo "下载带 drawtext 的 ffmpeg（$A）…"
dl_one ffmpeg "$FF" || { echo "::error::所有源都拿不到带 drawtext 的 ffmpeg，请手动准备后重试。"; exit 1; }
dl_one ffprobe "$FP" || echo "::warning::ffprobe 下载失败（烧录可能仍可用，ffmpeg 已就绪）"

chmod +x "$FF" "$FP" 2>/dev/null || true
xattr -dr com.apple.quarantine "$FF" "$FP" 2>/dev/null || true

# ---- ④ 最终硬校验：宁可失败也不交付残缺 ----
if ! has_drawtext "$FF"; then
    echo "::error::最终 ffmpeg 仍无 drawtext 滤镜（编译时缺 --enable-libfreetype）。"
    exit 1
fi
echo "✅ ffmpeg 就绪：$($FF -version 2>/dev/null | head -1)"
echo "   drawtext=$(has_drawtext "$FF" && echo 有 || echo 无)  subtitles=$(has_subtitles "$FF" && echo 有 || echo 无)"
