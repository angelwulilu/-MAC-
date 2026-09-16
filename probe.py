# -*- coding: utf-8 -*-
"""
probe.py —— 读取视频参数（整个工具的地基）

作用：输入一个视频文件，用 ffprobe 读出它的关键参数并打印出来。
后面做"参数对比"、"重编码"、"批量合成"全靠它先读参数。

用法：在命令行里输入
    python probe.py  "你的视频文件路径.mp4"
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import paths

# ffprobe 的完整路径。自动查找（程序目录/ffmpeg → 程序目录 → D:\ffmpeg\bin → 系统 PATH），
# 打包成 exe 后也能找到自带的 ffprobe。
FFPROBE = paths.find_ffprobe()

# 对 concat -c copy 拼接起决定性作用的参数：不同则必须重编码，否则大概率失败
# （容器格式已从对比表移除：输出容器由输出文件决定，输入容器不同不影响拼接，
#   且其字符串很长会挤占表格宽度）
CRITICAL_KEYS = {
    "视频编码", "分辨率", "帧率", "像素格式",
    "音频编码", "采样率", "声道",
}

# 这些不同也能拼成功，但输出会码率忽高忽低，建议统一
WARN_KEYS = {"视频码率", "音频码率"}

# 这些不同不影响拼接，属于正常差异（不算问题）
# 注：容器格式原本也在这里，但它不影响拼接且字符串很长，
#     会让对比表占大量宽度，已按用户要求从对比表中移除。
IGNORE_KEYS = {"时长(秒)"}

# 参数在对比表里的显示顺序：越靠上越重要（对合成影响越大）
# 注：容器格式已移除（不影响拼接，且字符串很长会挤占表格宽度）
PARAM_ORDER = [
    "分辨率",
    "帧率",
    "视频编码",
    "像素格式",
    "音频编码",
    "采样率",
    "声道",
    "视频码率",
    "音频码率",
    "时长(秒)",
]

# 「正常差异」类参数的具体说明（鼠标悬停时显示）
IGNORE_REASONS = {
    "时长(秒)": "时长不同是正常的（片头通常比素材短）",
}


# ffprobe 超时（秒）。没超时的话，文件在网络盘上掉线、或被别的程序独占时
# 会**永久卡住**：单线程调用会冻住界面，并发调用会白占一个工作线程。
PROBE_TIMEOUT = 60

# ffprobe 原始输出的缓存：同一个文件（绝对路径 + 修改时间 + 大小都没变）
# 不重复探测。批量流程里同一个文件会被反复探（选素材探一次、对比参数探一次、
# 重编码/合成前还要探），缓存能省掉大半。
#
# ⚠️ 缓存的是**原始 JSON 文本**，不是解析后的 dict：每次调用重新 json.loads，
#    调用方拿到的永远是全新对象，不会出现「有人改了 dict 污染缓存」的隐患。
#    重新解析的开销只有几十微秒，相对 0.2 秒的 ffprobe 可以忽略。
_PROBE_CACHE = {}
_PROBE_CACHE_MAX = 64


def probe(video_path, timeout=PROBE_TIMEOUT, use_cache=True):
    """调用 ffprobe，把视频信息读成一个 Python 字典"""
    key = sig = None
    try:
        key = os.path.abspath(video_path)
        st = os.stat(key)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = sig = None

    if use_cache and key is not None:
        hit = _PROBE_CACHE.get(key)
        if hit and hit[0] == sig:
            return json.loads(hit[1])

    cmd = [
        FFPROBE,
        "-v", "error",              # 只输出错误，不输出杂项
        "-print_format", "json",    # 用 JSON 格式输出，方便程序解析
        "-show_format",             # 读取容器信息（总码率、时长等）
        "-show_streams",            # 读取流信息（视频流、音频流）
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout, **paths.spawn_kwargs())
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "读取超时（{} 秒）：文件可能在网络盘上、或正被别的程序占用\n{}".format(
                timeout, video_path))
    if result.returncode != 0:
        raise RuntimeError("读取失败，可能路径不对或不是视频文件：\n" + result.stderr)

    if use_cache and key is not None:
        if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[key] = (sig, result.stdout)
    return json.loads(result.stdout)


def probe_many(paths, max_workers=None):
    """并发探测多个文件，返回 (结果列表, 失败列表)。

    结果列表 = [(路径, 参数字典), ...]，**顺序和输入一致**，探测失败的会被跳过；
    失败列表 = [(路径, 错误信息), ...]。

    max_workers=None 时**自适应**：
      先串行探前 2 个给磁盘测速。单个平均耗时 > 0.5 秒是机械硬盘 / 网络盘的
      典型特征（SSD 上只要 0.2 秒左右），这时退回 2 并发；否则用 4 并发
      （实测 SSD 上 21 个文件 4.79s → 2.01s，快 2.4 倍）。
      机械盘上多开会让磁头来回寻道、几乎没收益，所以不硬开 4 路。
    """
    paths = list(paths)
    if not paths:
        return [], []

    results = {}
    failures = []

    def _one(p):
        return get_key_params(probe(p))

    # ---- 自适应：先串行探 2 个给磁盘测速 ----
    if max_workers is None:
        sample = paths[:2]
        t0 = time.perf_counter()
        for p in sample:
            try:
                results[p] = _one(p)
            except Exception as e:
                failures.append((p, str(e)))
        each = (time.perf_counter() - t0) / max(1, len(sample))
        max_workers = 2 if each > 0.5 else 4

    rest = [p for p in paths if p not in results]
    if max_workers > 1 and len(rest) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(rest))) as ex:
            futs = {ex.submit(_one, p): p for p in rest}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    results[p] = fut.result()
                except Exception as e:
                    failures.append((p, str(e)))
    else:
        for p in rest:
            try:
                results[p] = _one(p)
            except Exception as e:
                failures.append((p, str(e)))

    return [(p, results[p]) for p in paths if p in results], failures


def parse_frame_rate(rate_str):
    """把 ffprobe 给的帧率（可能是 '4970/83' 这种分数）算成小数，方便看。

    返回一个简洁字符串，例如：'60'、'59.88'、'30'
    """
    if not rate_str:
        return "未知"
    val = frame_rate_float(rate_str)
    if val is None:
        return str(rate_str)
    # 整数的帧率直接显示整数，否则保留两位小数
    if abs(val - round(val)) < 0.005:
        return str(int(round(val)))
    return "{:.2f}".format(val)


def frame_rate_float(rate_str):
    """把 '4970/83' 或 '60' 转成小数（float），重编码时算帧率用"""
    if not rate_str:
        return None
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        try:
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(rate_str)
    except ValueError:
        return None


def _to_kbps(bit_rate):
    """把 bps 码率转成 kbps 数值，失败返回 None"""
    try:
        return float(bit_rate) / 1000.0
    except (TypeError, ValueError):
        return None


def format_param(key, value):
    """把 get_key_params 拿到的「原始值」，格式化成好看的中文显示"""
    if value is None:
        return ""
    if key == "帧率":
        return parse_frame_rate(str(value))
    if key == "分辨率":
        return str(value).replace("x", " x ")
    if key in ("视频码率", "音频码率"):
        kbps = _to_kbps(value)
        if kbps is None:
            return ""
        return "{:.0f} kbps".format(kbps)
    if key == "时长(秒)":
        try:
            return "{:.1f} 秒".format(float(value))
        except (ValueError, TypeError):
            return str(value)
    return str(value)


def values_equal(key, a, b):
    """判断两个参数值是否「实质一致」。

    帧率做容差比较（可变帧率的视频，帧率本就有微小波动），
    其余参数做精确比较。
    """
    if key == "帧率":
        fa = frame_rate_float(str(a)) if a not in (None, "") else None
        fb = frame_rate_float(str(b)) if b not in (None, "") else None
        if fa is None or fb is None:
            return str(a) == str(b)
        return abs(fa - fb) < 0.5
    return str(a) == str(b)


def get_key_params(data):
    """从 ffprobe 的结果里，挑出「合成视频最关心的那些参数」"""
    info = {}

    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            info["视频编码"] = s.get("codec_name")
            info["分辨率"] = "{}x{}".format(s.get("width"), s.get("height"))
            info["帧率"] = s.get("avg_frame_rate") or s.get("r_frame_rate")
            info["像素格式"] = s.get("pix_fmt")
        elif s.get("codec_type") == "audio":
            info["音频编码"] = s.get("codec_name")
            info["采样率"] = s.get("sample_rate")
            info["声道"] = s.get("channels")
            info["音频码率"] = s.get("bit_rate")

    fmt = data.get("format", {})
    info["视频码率"] = fmt.get("bit_rate")
    # 容器格式（format_name）不再采集：不影响拼接，且字符串很长会挤占对比表宽度
    info["时长(秒)"] = fmt.get("duration")

    # 按重要程度排序返回（PARAM_ORDER 定义的顺序，越靠上越关键）
    ordered = {}
    for key in PARAM_ORDER:
        if key in info:
            ordered[key] = info[key]
    for key in info:  # 兜底：PARAM_ORDER 没列到的 key 追加在后面
        if key not in ordered:
            ordered[key] = info[key]
    return ordered


def main():
    if len(sys.argv) < 2:
        print("用法：python probe.py  \"视频文件路径\"")
        return

    video_path = sys.argv[1]
    print("正在读取：", video_path)
    print("-" * 40)

    data = probe(video_path)
    for key, value in get_key_params(data).items():
        print("{}：{}".format(key, format_param(key, value)))


if __name__ == "__main__":
    main()
