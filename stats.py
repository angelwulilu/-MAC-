# -*- coding: utf-8 -*-
"""
stats.py —— 统计视频文件的总时长和总大小（供两个 tab 的汇总栏使用）

为什么单独放这里：统计「总时长」需要逐个文件调 ffprobe，文件多时会耗时几秒到几十秒，
所以放在后台线程跑，通过 StatsBridge 的信号安全地把结果送回主线程。
"""

import math
import os
from collections import OrderedDict

from PySide6.QtCore import QObject, Signal

from probe import probe


def probe_duration(path):
    """探测单个视频的时长（秒），失败返回 0.0"""
    try:
        data = probe(path)
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def total_stats(paths):
    """同步计算 (总时长秒, 总大小字节)。

    时长用 ffprobe 探测（重活，放后台线程）；
    大小用 os.path.getsize 读文件系统元数据（零成本）。
    """
    total_duration = 0.0
    total_size = 0
    for p in paths:
        try:
            total_size += os.path.getsize(p)
        except OSError:
            pass
        total_duration += probe_duration(p)
    return total_duration, total_size


class StatsBridge(QObject):
    """线程桥：后台线程通过它把统计结果安全发回主线程（Qt 自动排队）。"""
    done = Signal(int, float, int)   # generation, 总时长秒, 总大小字节


class RatioBridge(QObject):
    """比例扫描的线程桥：后台扫完把比例种类和第一个视频分辨率发回主线程。"""
    done = Signal(int, object, object)   # generation, ratios(OrderedDict), first_dim(tuple|None)


class CompareBridge(QObject):
    """参数对比的线程桥：后台把片头/素材参数探完，发回主线程填表。

    payload 是 dict：
        {"head_info": dict|None, "mats": [(路径, dict), ...], "failures": [(路径, 错误)]}
    """
    done = Signal(int, object)   # generation, payload


def probe_dimensions(path):
    """探测单个视频的实际宽高 (width, height)，失败返回 (None, None)。

    供「按视频实际比例预览」使用：预览框的宽高比和字幕字号缩放都要
    跟视频真实分辨率对齐，而不是写死 1080x1920。
    """
    try:
        data = probe(path)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w = s.get("width")
                h = s.get("height")
                if w and h:
                    return int(w), int(h)
        return None, None
    except Exception:
        return None, None


def simplify_ratio(w, h):
    """把宽高简化成最简比例字符串，例如 1920x1080 -> '16:9'。"""
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    g = math.gcd(w, h)
    return "{}:{}".format(w // g, h // g)


def collect_ratios(paths):
    """扫描多个视频，统计它们的比例种类。

    返回 (ratios, first_dim)：
      - ratios: OrderedDict，形如 {'16:9': 3, '9:16': 1}（按首次出现顺序）
      - first_dim: (w, h) 第一个成功探测到宽高的视频的实际分辨率；没有则 None
    """
    ratios = OrderedDict()
    first_dim = None
    for p in paths:
        w, h = probe_dimensions(p)
        if w and h:
            if first_dim is None:
                first_dim = (w, h)
            key = simplify_ratio(w, h)
            if key:
                ratios[key] = ratios.get(key, 0) + 1
    return ratios, first_dim
