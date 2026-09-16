# -*- coding: utf-8 -*-
"""验证 Mac 版的**字体解析**是否真的跨平台。

为什么单独测：
    原来 subtitle.py 里写死了 7 个 `C:/Windows/Fonts/msyh.ttc` 之类的路径。
    在 macOS 上这些文件**根本不存在** → `available_fonts()` 返回 [] →
      ① 字体下拉框一直空着；
      ② 各处的 `or _FONT_FILES.get("Microsoft YaHei")` 兜底也拿不到（表里没这个键）
         → 返回 None → 拿 None 去喂 QRawFont / freetype → 字幕度量全错。
    这个 bug 在 Windows 上**永远测不出来**（路径都是对的），
    所以必须在测试里**模拟 macOS 的文件系统**来验证。

做法：不依赖真实 Mac —— 直接喂假的路径表 + 假的 os.path.isfile，
      断言「按平台选出的候选集」与「解析结果」是否符合预期。
"""
import os
import sys
import ast

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
print("Mac 版字体解析检查")
print("=" * 64)
print()

# ------------------------------------------------------------------ 源码级
print("[1] 源码里不该再有 Windows 专属字体路径")
src_sub = open(os.path.join(HERE, "subtitle.py"), encoding="utf-8").read()
src_tab = open(os.path.join(HERE, "subtitle_tab.py"), encoding="utf-8").read()
src_paths = open(os.path.join(HERE, "paths.py"), encoding="utf-8").read()

# 字体路径表必须集中在 paths.py；subtitle.py 里不该再写死盘符路径
tree_sub = ast.parse(src_sub)
sub_hard = []
for node in ast.walk(tree_sub):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        if (v.startswith("C:/") or v.startswith("C:\\")) and \
           ("Fonts" in v or v.lower().endswith((".ttc", ".ttf", ".otf"))):
            sub_hard.append((node.lineno, v))
check("subtitle.py 里没有写死的 C:/Windows/Fonts 字体路径",
      not sub_hard, str(sub_hard[:4]))

# subtitle_tab.py 同理
tree_tab = ast.parse(src_tab)
tab_hard = []
for node in ast.walk(tree_tab):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        if (v.startswith("C:/") or v.startswith("C:\\")) and \
           ("Fonts" in v or v.lower().endswith((".ttc", ".ttf", ".otf"))):
            tab_hard.append((node.lineno, v))
check("subtitle_tab.py 里没有写死的 C:/Windows/Fonts 字体路径",
      not tab_hard, str(tab_hard[:4]))

# 字体路径表应该只出现在 paths.py 一处
check("字体候选表集中在 paths.py（subtitle.py 不再自己维护）",
      "_FONT_CANDIDATES" in src_paths and "_FONT_CANDIDATES" not in src_sub)

# 不能再用「硬编码 Windows 字体名」当兜底。
# ⚠️ 必须查 AST 而不是查文本 —— 注释里会写到这些字符串（说明「原来错在哪」），
#    查文本会把注释误判成残留代码。
hard_defaults = []
for node in ast.walk(tree_tab):
    # 形如 X.get(k, "Microsoft YaHei") 的默认值
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get":
        for a in node.args[1:]:
            if isinstance(a, ast.Constant) and a.value == "Microsoft YaHei":
                hard_defaults.append(node.lineno)
    # 形如 `or _FONT_FILES.get("Microsoft YaHei")`
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" and node.args:
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and a0.value == "Microsoft YaHei":
            hard_defaults.append(node.lineno)
check("没有 `font_name_map.get(..., \"Microsoft YaHei\")` 硬编码兜底",
      not hard_defaults, "残留行号：%s" % hard_defaults)

# 局部重复的 font_name_map 字典必须清零（用 AST 找字面量 dict 赋值）
local_map = []
for node in ast.walk(tree_tab):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "font_name_map":
                local_map.append(node.lineno)
check("没有残留的局部 font_name_map（已统一到模块级 _qt_family）",
      not local_map, "残留行号：%s" % local_map)
print()

# ------------------------------------------------------------- paths 逻辑
print("[2] paths.py 的字体解析逻辑（模拟 macOS / Windows 两套文件系统）")
import paths

check("有 font_candidates()", hasattr(paths, "font_candidates"))
check("有 resolve_fonts()", hasattr(paths, "resolve_fonts"))
check("有 default_font()", hasattr(paths, "default_font"))

cands = paths.font_candidates()
names = [n for n, _ in cands]
check("候选表非空", len(cands) > 0, "%d 项" % len(cands))
check("含 Windows 老字体名（保证 Windows 版行为不变）",
      "微软雅黑" in names and "宋体" in names and "黑体" in names)

# 每个字体都有候选路径，且路径都是绝对路径
bad = [(n, ps) for n, ps in cands if not ps or not all(
    (p.startswith("/") or (len(p) > 1 and p[1] == ":")) for p in ps)]
check("每个字体都有绝对路径候选", not bad, str(bad[:3]))

# macOS 专属字体只在 Mac 上出现（Windows 上不该冒出来）
mac_only = ["苹方", "冬青黑体", "华文黑体"]
if sys.platform == "darwin":
    check("macOS 上出现苹方等 Mac 专属字体",
          all(n in names for n in mac_only))
else:
    check("非 macOS 上**不**出现 Mac 专属字体（不污染 Windows 版）",
          not any(n in names for n in mac_only),
          "混进来了：%s" % [n for n in mac_only if n in names])

# Windows 那份路径必须保持原样（这是与 Windows 版一致性的关键）
win_expect = {
    "微软雅黑": "C:/Windows/Fonts/msyh.ttc",
    "宋体": "C:/Windows/Fonts/simsun.ttc",
    "黑体": "C:/Windows/Fonts/simhei.ttf",
    "楷体": "C:/Windows/Fonts/simkai.ttf",
    "仿宋": "C:/Windows/Fonts/simfang.ttf",
    "等线": "C:/Windows/Fonts/Deng.ttf",
}
mismatch = []
for n, expect in win_expect.items():
    got = next((ps[0] for nm, ps in cands if nm == n), None)
    if got != expect:
        mismatch.append((n, got))
check("Windows 老路径仍排在各自候选表第一位（Windows 行为不变）",
      not mismatch, str(mismatch))

# macOS 每个老字体都要有 Mac 落点，否则 Mac 上又是空下拉框
mac_missing = []
for n, ps in cands:
    macp = [p for p in ps if p.startswith("/System/Library") or p.startswith("/Library")]
    if not macp:
        mac_missing.append(n)
check("每个字体都有 macOS 落点（Mac 上不会空下拉框）",
      not mac_missing, "缺 Mac 路径：%s" % mac_missing)

# 苹果字体路径必须带 .ttc/.ttf/.otf 后缀（别写出目录名当文件）
bad_ext = []
for n, ps in cands:
    for p in ps:
        if p.startswith("/") and not p.lower().endswith((".ttc", ".ttf", ".otf", ".ttc")):
            bad_ext.append((n, p))
check("macOS 候选路径都指向字体文件（非目录）", not bad_ext, str(bad_ext[:3]))
print()

# ------------------------------------ 模拟 macOS 文件系统，验证真能解析出来
# 这是本文件**最关键**的一组：Windows 上永远测不出「Mac 上字体表是空的」这个 bug，
# 所以必须把 isfile 换掉来模拟 macOS。
print("[2b] 模拟 macOS 文件系统（Windows 上唯一的验证办法）")
_MAC_FAKE_FILES = {
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Kaiti.ttc",
    "/System/Library/Fonts/Supplemental/FangSong.ttf",
}

_orig_isfile = os.path.isfile
_orig_isdir = os.path.isdir
try:
    os.path.isfile = lambda p: p in _MAC_FAKE_FILES
    os.path.isdir = lambda p: False
    # _IS_MAC 影响 font_candidates() 是否带上 Mac 专属字体
    _orig_is_mac = paths._IS_MAC
    paths._IS_MAC = True

    mac_fonts = paths.resolve_fonts()
    mac_names = [n for n, _ in mac_fonts]
    print("     Mac 上解析到 %d 个字体：%s" % (len(mac_fonts), mac_names))

    check("模拟 Mac 上字体列表**非空**（原 bug 的根因）", len(mac_fonts) > 0,
          "空列表 = 字体下拉框会一直空着")
    check("模拟 Mac 上每个路径都是 /System 或 /Library 下的",
          all(p.startswith("/System/") or p.startswith("/Library/") for _, p in mac_fonts))
    check("模拟 Mac 上解析结果不包含任何 C:/ 路径",
          not any(p.startswith("C:") for _, p in mac_fonts))
    check("模拟 Mac 上出现平台专属字体（苹方）", "苹方" in mac_names)

    mdn, mdp = paths.default_font()
    check("模拟 Mac 上 default_font() 有值（不再返回 None）",
          bool(mdn) and bool(mdp), "返回 %s / %s" % (mdn, mdp))
    check("模拟 Mac 上 default_font() 是 Mac 路径",
          mdp.startswith("/System/") or mdp.startswith("/Library/"))
    check("模拟 Mac 上老字体名（微软雅黑）也能落到 Mac 字体",
          any(n == "微软雅黑" and p.startswith("/") for n, p in mac_fonts))

    # 候选表在 Mac 下应含 Mac 专属字体
    mcands = paths.font_candidates()
    check("模拟 Mac 下 font_candidates() 含 Mac 专属字体",
          any(n in ("苹方", "冬青黑体", "华文黑体") for n, _ in mcands))
finally:
    os.path.isfile = _orig_isfile
    os.path.isdir = _orig_isdir
    paths._IS_MAC = _orig_is_mac
print()

# --------------------------------------------- resolve_fonts 真实行为
print("[3] resolve_fonts() 在当前系统上的真实返回")
real = paths.resolve_fonts()
print("     实际解析到 %d 个字体：%s" % (len(real), [n for n, _ in real]))
check("每个返回项的路径都真实存在",
      all(os.path.isfile(p) for _, p in real))
check("返回项无重名",
      len({n for n, _ in real}) == len(real))
check("调用两次结果一致（无副作用）", paths.resolve_fonts() == real)

dn, dp = paths.default_font()
print("     默认字体：%s -> %s" % (dn, dp))
if real:
    check("default_font() 返回真实存在的文件", bool(dp) and os.path.isfile(dp))
    check("default_font() 取的是第一个可用字体", dn == real[0][0])
else:
    # 当前 Windows 机器上应该有微软雅黑
    check("当前系统（Windows）应至少有一个可用中文字体", False,
          "resolve_fonts() 返回空 —— 检查候选路径")
print()

# -------------------------------------- subtitle.py 的对外接口
print("[4] subtitle.py 对外接口")
# subtitle.py 导入时会 import PySide6，所以只做 AST 检查，不真 import
fn_defs = {n.name for n in ast.walk(tree_sub) if isinstance(n, ast.FunctionDef)}
check("available_fonts() 仍存在（外部依赖它）", "available_fonts" in fn_defs)
check("FONT_OPTIONS 仍是模块级名字（兼容旧引用）",
      "FONT_OPTIONS" in src_sub)
check("FONT_FILE 仍是模块级名字（兼容旧引用）", "FONT_FILE" in src_sub)
check("available_fonts 委托给 paths.resolve_fonts",
      "paths.resolve_fonts()" in src_sub)
check("FONT_FILE 来自 paths.default_font（不再硬编码 msyh）",
      "paths.default_font()" in src_sub)
print()

# -------------------------------------- subtitle_tab.py 的映射表
print("[5] subtitle_tab.py 的字体名映射")
check("_QT_FAMILY 只有一份（不再局部重复）",
      src_tab.count("_QT_FAMILY = {") == 1,
      "出现 %d 次" % src_tab.count("_QT_FAMILY = {"))
check("有统一的 _qt_family() 取值函数", "def _qt_family(" in src_tab)
check("_qt_family 对未知字体原样返回（不硬编码兜底）",
      "_QT_FAMILY.get(display_name, display_name)" in src_tab)
check("有 _default_font_file() 兜底函数", "def _default_font_file(" in src_tab)
check("Mac 字体族名已加入映射（苹方 → PingFang SC）",
      '"PingFang SC"' in src_tab)
check("_load_fonts 默认选中平台字体（不再硬编码 微软雅黑）",
      'self.font_combo.setCurrentText(_DEFAULT_FONT_NAME' in src_tab)
check("写 .ass 的 Fontname 不再硬编码 Microsoft YaHei",
      'ass_fontname = "Microsoft YaHei"' not in src_tab)
check("占位符画笔字体不再硬编码",
      'QFont("Microsoft YaHei", 12)' not in src_tab)
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