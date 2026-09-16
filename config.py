# -*- coding: utf-8 -*-
"""
config.py —— 记住用户上次的设置

为什么要它：
    保存位置、编码速度、画质、并发数这些每次打开程序都要重设一遍太烦
    （尤其是「并发」和「保存位置」，默认值通常不是用户想要的）。

存在哪：
    <程序目录>/config.json（打包后 = exe 同级），和 encode_reference.json 同一位置，
    走 paths.writable_dir()（万一把程序装在 Program Files 这类只读目录里也能退到用户目录）。

设计原则：
    读失败 / 文件损坏 → 返回空 dict，绝不让配置问题影响程序启动；
    写失败 → 静默忽略（配置存不上不该打断用户操作）。
"""

import json
import os

import paths

CONFIG_NAME = "config.json"

# 默认值（第一次运行、或配置里缺字段时用）
#
# ⚠️ 2026-09-15 按用户要求改过（原 fast / 18 / 1 → 现 veryfast / 23 / 2）：
#    veryfast + crf23 = 出片快、体积可控，是「边干活边跑」的更好起点；
#    并发 2 在多数机器上比串行明显快，又不会像 4 那样把 CPU 吃满。
# 注意：**改这里只影响「新用户 / 删了 config.json 的人」**。
#      老用户的 config.json 里已存了旧值，load() 会优先用文件里的值 ——
#      这是有意的（不能把用户自己调过的设置悄悄改回去）。
DEFAULTS = {
    "preset": "veryfast",
    "crf": "23",
    "max_workers": "2",
    # 子进程优先级："low" / "normal" / "high"（见 paths.PRIORITY_LEVELS）
    "priority": "normal",
    "concat_output_dir": "",
    "burn_output_dir": "",
}


def config_path():
    """配置文件完整路径。"""
    return os.path.join(paths.writable_dir(), CONFIG_NAME)


def load():
    """读取配置，返回 dict（失败返回 {}）。"""
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def get(key):
    """取一个配置项，缺省时返回 DEFAULTS 里的值。"""
    val = load().get(key)
    if val in (None, ""):
        return DEFAULTS.get(key, "")
    return val


def update(**kwargs):
    """更新若干配置项并落盘（保留其它已有字段）。"""
    data = load()
    for k, v in kwargs.items():
        data[k] = v
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
