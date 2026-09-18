# -*- coding: utf-8 -*-
"""
reencode.py —— 重编码片头，使其关键参数对齐素材

第 4 步：当片头和素材的关键参数（编码/分辨率/帧率/像素格式/采样率/声道等）
        不一致时，用 ffmpeg 把片头重编码，让它们保持一致。
        这样后面用 concat 硬拼就不会失败了。

用法（可单独在命令行测试）：
    python reencode.py  "片头.mp4"  "素材.mp4"  "输出.mp4"
"""

import os
import subprocess
import sys

from PySide6.QtCore import QObject, Signal

import paths
from probe import (
    probe, get_key_params, frame_rate_float, values_equal, CRITICAL_KEYS,
)

# ffmpeg 的完整路径（自动查找，打包成 exe 后指向自带的 ffmpeg）
FFMPEG = paths.find_ffmpeg()


def find_critical_diffs(head_info, mat_info):
    """找出片头和素材之间「关键参数」不一致的项，返回列表。

    返回空列表 = 关键参数都一致，可以直接拼接，无需重编码。
    """
    diffs = []
    for key in CRITICAL_KEYS:
        if not values_equal(key, head_info.get(key), mat_info.get(key)):
            diffs.append(key)
    return diffs


def _parse_resolution(res_str):
    """把 '1080x1920' 转成 (1080, 1920)"""
    try:
        w, h = str(res_str).split("x")
        return int(w), int(h)
    except Exception:
        return None, None


def build_ffmpeg_cmd(head_path, head_info, mat_info, output_path, mute=False):
    """根据素材参数，构造一条「把片头重编码」的 ffmpeg 命令。

    mute=True 时：片头音频不保留原声，而是换成一条「近静音」音轨
    （amplitude=0.001 的白噪声 + 固定 97k 码率）。这样片头虽然听不到声音，
    但音频参数（aac / 32000 / 2ch / 97k）是“真实存在”的，跟素材拼接时
    不会因为“静音被 ffmpeg 压成极低码率/无音轨”而参数对不上、导致 concat 失败。
    参考用户脚本：1.编码片头视频+静音轨道.bat
    """
    # ---- 输入段：所有 -i 必须排在一起，输出选项全部放到后面 ----
    inputs = [FFMPEG, "-y", "-i", head_path]
    sr = str(mat_info.get("采样率") or "32000")
    ch = str(mat_info.get("声道") or 2)
    if mute:
        # 近静音占位音轨（采样率优先对齐素材，没有就退回 32000）
        inputs += ["-f", "lavfi",
                   "-i", "anoisesrc=color=white:amplitude=0.001:sample_rate={}".format(sr)]

    # ---- 输出段：滤镜 / 编码 / 码率 / 映射 / 封装 ----
    out = []
    vf_parts = []
    w, h = _parse_resolution(mat_info.get("分辨率"))
    if w and h:
        vf_parts.append("scale={}:{}".format(w, h))
    fps = frame_rate_float(str(mat_info.get("帧率")))
    if fps:
        # 四舍五入到整数帧率（59.88 -> 60），更通用、兼容性更好
        vf_parts.append("fps={}".format(int(round(fps))))
    if vf_parts:
        out += ["-vf", ",".join(vf_parts)]

    # 视频编码器 + 像素格式（统一一套参数，保证 -c copy 拼接时两端兼容）
    pix_fmt = mat_info.get("像素格式") or "yuv420p"
    out += ["-c:v", "libx264", "-preset", "fast",
            "-profile:v", "high", "-level", "4.2",
            "-pix_fmt", str(pix_fmt),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]

    if mute:
        out += ["-map", "0:v", "-map", "1:a"]
        # 关键：把静音音轨的时间戳重新从 0 排起，并 avoid_negative_ts，
        # 否则后面 concat demuxer + -c copy 拼素材时第二段音频 DTS 比第一段结尾
        # 还小，报 Non-monotonic DTS 并在接缝处产生坏包。参考用户 bat1。
        out += ["-af", "asetpts=PTS-STARTPTS"]
        out += ["-c:a", "aac", "-ar", sr, "-ac", ch, "-b:a", "97k"]
        # anoisesrc 是无限音源，必须 -shortest 才能按视频时长收尾，否则会一直编码
        out += ["-shortest"]
    else:
        # 音频：仅当片头本身带音频时才处理（保留并转成跟素材一致）
        if head_info.get("音频编码"):
            out += ["-c:a", "aac"]
            if mat_info.get("采样率"):
                out += ["-ar", str(mat_info["采样率"])]
            if mat_info.get("声道") is not None:
                out += ["-ac", str(mat_info["声道"])]

    # crf 18 = 高质量；avoid_negative_ts 让拼接端时间戳干净；
    # +faststart = 让 mp4 支持边下边播（适合上传）
    out += ["-crf", "18", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", output_path]
    return inputs + out


def reencode_head(head_path, head_info, mat_info, output_path, mute=False):
    """执行重编码，返回输出文件路径"""
    cmd = build_ffmpeg_cmd(head_path, head_info, mat_info, output_path, mute=mute)
    print("执行命令：", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace",
                            **paths.spawn_kwargs())
    if result.returncode != 0:
        raise RuntimeError("重编码失败：\n" + result.stderr)
    return output_path


def build_material_cmd(in_path, params, out_path):
    """按用户选的参数，构造一条「重编码单个素材」的 ffmpeg 命令。

    params 关键：分辨率 / 帧率 / 视频编码(h264|h265) / 像素格式 /
    音频编码 / 采样率 / 声道 / 视频码率 / 音频比特率
    """
    cmd = [FFMPEG, "-y", "-i", in_path]

    vf_parts = []
    w, h = _parse_resolution(params.get("分辨率", ""))
    if w and h:
        vf_parts.append("scale={}:{}".format(w, h))
    fps = frame_rate_float(str(params.get("帧率", "")))
    if fps:
        vf_parts.append("fps={}".format(int(round(fps))))
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]

    vcodec = "libx265" if str(params.get("视频编码", "h264")).lower().startswith("h265") else "libx264"
    cmd += ["-c:v", vcodec, "-preset", "fast"]
    if params.get("像素格式"):
        cmd += ["-pix_fmt", str(params["像素格式"])]
    if params.get("视频码率"):
        cmd += ["-b:v", str(params["视频码率"])]

    acodec = str(params.get("音频编码", "aac"))
    cmd += ["-c:a", acodec]
    if params.get("采样率"):
        cmd += ["-ar", str(params["采样率"])]
    if params.get("声道"):
        cmd += ["-ac", str(params["声道"])]
    if params.get("音频比特率"):
        cmd += ["-b:a", str(params["音频比特率"])]

    cmd += ["-movflags", "+faststart", out_path]
    return cmd


def reencode_material(in_path, params, out_path):
    """执行单个素材的重编码，返回输出路径"""
    cmd = build_material_cmd(in_path, params, out_path)
    print("执行命令：", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace",
                            **paths.spawn_kwargs())
    if result.returncode != 0:
        raise RuntimeError("重编码失败：\n" + result.stderr)
    return out_path


def _unique_mat_path(out_dir, base):
    """素材重编码输出路径：<原素材名>_重新编码.mp4，同名自动加 (1)(2)…避免覆盖"""
    out = os.path.join(out_dir, "{}_重新编码.mp4".format(base))
    i = 1
    while os.path.exists(out):
        out = os.path.join(out_dir, "{}_重新编码({}).mp4".format(base, i))
        i += 1
    return out


class ReencodeMatWorker(QObject):
    """后台线程重编码全部素材，通过信号回报进度

    out_dir：
        给了目录 → 所有成品都放这个目录；
        给 None  → 每个成品放到**它自己那份素材**所在目录（默认，见 app.py 的对话框）。
    """
    progress = Signal(int, int, str, bool, str)
    # success_count, fail_list, out_paths
    finished = Signal(int, list, list)

    def __init__(self, materials, params, out_dir=None):
        super().__init__()
        self.materials = materials
        self.params = params
        self.out_dir = out_dir
        self.out_paths = []
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        total = len(self.materials)
        success = 0
        fail = []
        self.out_paths = []
        try:
            for i, path in enumerate(self.materials, 1):
                if self._cancelled:
                    raise InterruptedError("用户取消")
                base = os.path.splitext(os.path.basename(path))[0]
                # out_dir 为 None = 就地输出到原素材所在目录
                out_dir = self.out_dir or os.path.dirname(os.path.abspath(path))
                try:
                    os.makedirs(out_dir, exist_ok=True)
                except OSError:
                    pass      # 建不出来就让下面的 reencode 去报错，错误信息更具体
                out = _unique_mat_path(out_dir, base)
                try:
                    reencode_material(path, self.params, out)
                    success += 1
                    self.out_paths.append(out)
                    self.progress.emit(i, total, path, True, "完成")
                except Exception as e:
                    fail.append((path, str(e)))
                    self.progress.emit(i, total, path, False, str(e))
            self.finished.emit(success, fail, self.out_paths)
        except InterruptedError:
            self.finished.emit(-1, [], self.out_paths)
        except Exception as e:
            self.finished.emit(-1, [("内部错误", str(e))], self.out_paths)


def main():
    if len(sys.argv) < 4:
        print("用法：python reencode.py  \"片头.mp4\"  \"素材.mp4\"  \"输出.mp4\"")
        return

    head_path, mat_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    head_info = get_key_params(probe(head_path))
    mat_info = get_key_params(probe(mat_path))

    diffs = find_critical_diffs(head_info, mat_info)
    if not diffs:
        print("片头和素材的关键参数完全一致，无需重编码！")
        return

    print("发现关键参数不一致，需要重编码片头：", "、".join(diffs))
    reencode_head(head_path, head_info, mat_info, out_path)
    print("重编码完成：", out_path)


if __name__ == "__main__":
    main()
