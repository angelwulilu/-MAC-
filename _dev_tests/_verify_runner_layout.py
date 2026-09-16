# -*- coding: utf-8 -*-
"""在「精简布局 + macOS 平台」下复跑三个自检脚本。

🔴 为什么必须有这个脚本（bug 复盘，共两次）：

**第一次（run 35060031916）—— 布局问题**
    mac-package.yml 第 5 步在 macOS runner 上 **1 秒 exit 1**，本地 262 项全过。
    真凶：自检脚本里有一批断言指向**仓库外**的文件：

        _verify_mac_format.py  wt_root = os.path.dirname(HERE)
                               ├─ 配置git.bat            ← D:\\video-tool\\
                               ├─ _setup_git.py          ← D:\\video-tool\\
                               ├─ 推送mac到github.bat     ← D:\\video-tool\\
                               ├─ _push_mac.py           ← D:\\video-tool\\
                               └─ _package_mac_src.py    ← D:\\video-tool\\

    而仓库的根**就是 mac/** → runner 上一个都不存在 → 5 条 FAIL → exit 1。
    修法：这些断言改走 `check_win_side()`（非完整布局就跳过）。

**第二次（run 35061816016）—— 平台问题，且被本脚本漏掉**
    修完布局后仍然第 5 步失败。真凶是**平台**：
      · `_verify_mac_platform.py [1]` 的 5 条断言写的是「paths.py 在**Windows** 行为不变」，
        在 macOS 上必然全挂 —— 但本脚本当时**只模拟了目录布局，没模拟 sys.platform**，
        本地（Windows）跑照样全过 → **又一次「只在本地能过」**。
      · `_verify_mac_format.py [5]` 断言「mac/ 下不存在 __pycache__」，
        而 CI 刚跑过 `python3 _dev_tests/*.py` → 必然生成 → 必挂
        （本地过只是因为跑之前恰好没生成）。这是**口径错误**：该验的是
        「.gitignore 排掉了它」，不是「文件系统里没有」。

做法：
    ① 把 mac/ 整棵树复制到一个**干净的临时目录**（同级什么都不放）当仓库根；
    ② **同时把 sys.platform 伪装成 darwin**，再跑三个自检脚本。
    这样才同时复现了 runner 的「布局」和「平台」两个维度。

判据：
    三个脚本都要 exit 0。任何非 0 → 打印失败断言 → 本脚本 exit 1。
"""
import io
import os
import runpy
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))    # .../mac/_dev_tests
MAC = os.path.dirname(HERE)                          # .../mac

SKIP_DIRS = {"__pycache__", "build", "dist", ".git", "_bundled_ffmpeg"}
SKIP_EXTS = {".pyc"}

# 要复跑的三个脚本（不含 _verify_push_mac.py —— 它要连真远程，不适合放这里）
SCRIPTS = ["_verify_mac_fonts.py", "_verify_mac_format.py",
           "_verify_mac_platform.py"]


def copy_mac_to(dst):
    """把 mac/ 整棵树复制到 dst（跳过缓存与构建产物）。"""
    for dp, dns, fns in os.walk(MAC):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        rel = os.path.relpath(dp, MAC)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in fns:
            if os.path.splitext(f)[1].lower() in SKIP_EXTS:
                continue
            try:
                shutil.copy2(os.path.join(dp, f), os.path.join(target, f))
            except OSError:
                pass


def run_in(root, script, fake_platform=None):
    """在 root（= 假仓库根）里跑 root/_dev_tests/<script>。

    fake_platform: 非 None 时，跑的过程中把 sys.platform 改成这个值
                   （CI 上是真的 darwin，本机是 win32 —— 必须伪装才等价）。
    返回 (exit_code, fails, skips)。
    """
    path = os.path.join(root, "_dev_tests", script)
    captured = []

    class _Tee(io.TextIOBase):
        def __init__(self, real):
            self._real = real

        def write(self, s):
            captured.append(s)
            return self._real.write(s)

        def flush(self):
            return self._real.flush()

    orig_cwd = os.getcwd()
    orig_argv = sys.argv[:]
    orig_platform = sys.platform
    sys.argv = [path]
    os.chdir(root)
    sys.path.insert(0, os.path.join(root, "_dev_tests"))
    sys.path.insert(0, root)
    real_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout)
    if fake_platform:
        sys.platform = fake_platform
    code = 0
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        code = 99
    finally:
        sys.platform = orig_platform
        sys.stdout = real_stdout
        for p in (os.path.join(root, "_dev_tests"), root):
            if p in sys.path:
                sys.path.remove(p)
        os.chdir(orig_cwd)
        sys.argv = orig_argv

    text = "".join(captured)
    fails = [l.strip() for l in text.splitlines() if l.strip().startswith("FAIL")]
    skips = [l.strip() for l in text.splitlines() if l.strip().startswith("SKIP")]
    return code, fails, skips


def main():
    print("=" * 72)
    print("精简布局 + macOS 平台 复跑（模拟 GitHub runner 的两个维度）")
    print("=" * 72)
    print("为什么要跑这个：")
    print("  ① 布局：自检里有一批断言查**仓库外**的 Windows 侧文件，")
    print("     本地跑得到、runner 上跑不到。")
    print("  ② 平台：还有一批断言只在**本机是 Windows** 时成立")
    print("     （如「paths.py 的 Windows 行为不变」），在 macOS 上必然全挂。")
    print("  这里把 mac/ 拷到干净目录当仓库根，**并把 sys.platform 伪装成 darwin**，")
    print("  同时复现这两个维度。")
    print()

    tmp = tempfile.mkdtemp(prefix="runner_layout_")
    try:
        copy_mac_to(tmp)
        print("临时仓库根：%s" % tmp)
        print("伪装平台  ：darwin（本机实际 %s）" % sys.platform)
        top = sorted(os.listdir(tmp))
        print("顶层内容（%d 项）：%s" % (len(top), top[:8] + (["..."] if len(top) > 8 else [])))
        print()

        # 先单跑一次「假 darwin」下的平台脚本，方便单独看它的跳过情况
        results = {}
        for script in SCRIPTS:
            if not os.path.isfile(os.path.join(tmp, "_dev_tests", script)):
                print("!! 找不到 %s，跳过" % script)
                continue
            code, fails, skips = run_in(tmp, script, fake_platform="darwin")
            results[script] = (code, fails, skips)
            tag = "OK  " if code == 0 else "FAIL"
            print("[%s] %-26s exit=%s  失败 %d 条  跳过 %d 条"
                  % (tag, script, code, len(fails), len(skips)))
            for f in fails:
                print("        └─ %s" % f)

        print()
        print("=" * 72)
        bad = {k: v for k, v in results.items() if v[0] != 0}
        if bad:
            print("通过 %d 个脚本，失败 %d 个" % (len(results) - len(bad), len(bad)))
            print()
            print("🔴 这些脚本在 runner 上会 exit 1 —— CI 必然失败。")
            print("   修法（按根因选）：")
            print("   · 查仓库外文件的断言 → 改 check_win_side（找不到就跳过）")
            print("   · 只对 Windows 成立的断言 → 改 check_windows（非 Windows 跳过）")
            print("   · 口径写错的（如「磁盘上没有 __pycache__」）→ 改成查 .gitignore")
            print("=" * 72)
            return 1

        print("通过 %d 个脚本，失败 0 个" % len(results))
        total_skip = sum(len(v[2]) for v in results.values())
        if total_skip:
            print("（共跳过 %d 条：那是 Windows 侧检查，runner 上本就不适用）"
                  % total_skip)
        print("=== 全部通过 ===")
        print("=" * 72)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
