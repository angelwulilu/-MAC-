# -*- coding: utf-8 -*-
"""在「精简布局」（= GitHub runner 的真实布局）下复跑三个自检脚本。

🔴 为什么必须有这个脚本（bug 复盘）：
    mac-package.yml 第 5 步「字体解析 + 交付物格式检查」在 macOS runner 上
    **1 秒就 exit 1**，但本地（Windows，完整 video-tool 目录）262 项全过。
    真凶是自检脚本里有一批断言指向**仓库外**的文件：

        _verify_mac_format.py  wt_root = os.path.dirname(HERE)
                               ├─ 配置git.bat            ← D:\\video-tool\\
                               ├─ _setup_git.py          ← D:\\video-tool\\
                               ├─ 推送mac到github.bat     ← D:\\video-tool\\
                               ├─ _push_mac.py           ← D:\\video-tool\\
                               └─ _package_mac_src.py    ← D:\\video-tool\\

    而 GitHub 仓库的根**就是 mac/** —— runner 上这些文件一个都不存在
    → 5 条 check 失败 → sys.exit(1) → CI 挂。
    本地永远测不出来（本地它们都在）。这就是典型的「只在本地能过」。

做法：
    把 mac/ 整棵树复制到一个**干净的临时目录**（同级什么都不放），
    让那份副本充当「仓库根」，在里面跑三个自检脚本。
    这份副本的布局 == runner 上的真实布局。

判据：
    三个脚本都要 exit 0。任何非 0 → 打印失败断言 → 本脚本 exit 1。
    本地跑（完整布局）时不该出现「跳过」，出现「跳过」本身也说明
    本机布局不完整 —— 但不判失败，只提示。
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


def run_in(root, script):
    """在 root（= 假仓库根）里跑 root/_dev_tests/<script>，返回 (exit_code, fails, skips)。"""
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
    sys.argv = [path]
    os.chdir(root)
    sys.path.insert(0, os.path.join(root, "_dev_tests"))
    sys.path.insert(0, root)
    real_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout)
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
    print("精简布局复跑（模拟 GitHub runner 的目录结构）")
    print("=" * 72)
    print("为什么要跑这个：")
    print("  自检脚本里有一批断言查的是**仓库外**的 Windows 侧文件，")
    print("  本地跑得到、runner 上跑不到 → CI 会「1 秒失败」。")
    print("  这里把 mac/ 单独拷到一个干净目录当仓库根，复现 runner 的布局。")
    print()

    tmp = tempfile.mkdtemp(prefix="runner_layout_")
    try:
        copy_mac_to(tmp)
        print("临时仓库根：%s" % tmp)
        top = sorted(os.listdir(tmp))
        print("顶层内容（%d 项）：%s" % (len(top), top[:8] + (["..."] if len(top) > 8 else [])))
        print()

        results = {}
        for script in SCRIPTS:
            if not os.path.isfile(os.path.join(tmp, "_dev_tests", script)):
                print("!! 找不到 %s，跳过" % script)
                continue
            code, fails, skips = run_in(tmp, script)
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
            print("🔴 这些脚本在 runner 布局下会 exit 1 —— CI 必然失败。")
            print("   修法：把查仓库外文件的断言改成 check_win_side（找不到就跳过），")
            print("   或把判据换成 mac/ 内部的文件。")
            print("=" * 72)
            return 1

        print("通过 %d 个脚本，失败 0 个" % len(results))
        total_skip = sum(len(v[2]) for v in results.values())
        if total_skip:
            print("（共跳过 %d 条：那是 Windows 侧文件检查，runner 上本就没有）"
                  % total_skip)
        print("=== 全部通过 ===")
        print("=" * 72)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
