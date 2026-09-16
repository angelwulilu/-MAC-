#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_push_mac.py 的离线验证脚本

为什么不用「假 git」：Windows 下用 .bat 造假可执行文件，子 bat 的 EOF 会终止
整个 cmd 会话（返回码还是 0），测不出真问题。所以用**真 git.exe**，但把
HOME 指到临时目录，仓库也建在临时目录里，绝不碰用户真实配置。

验证这些场景：
  1. git 未装        → 应给出安装指引并中止
  2. autocrlf 没配   → 体检应报警告
  3. autocrlf=true   → 体检应报警告（true 会改写本地文件）
  4. autocrlf=input  → 体检应全过
  5. Mac 脚本 CRLF   → 体检应提示但说明 autocrlf=input 会修
  6. 仓库地址非法    → 应拒绝并要求重输
  7. 完整推送流程    → 推到本地 bare 仓库，检查文件真的进去了
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))        # .../mac/_dev_tests
MAC = os.path.dirname(HERE)                              # .../mac
WT = os.path.dirname(MAC)                                # .../video-tool
TARGET = os.path.join(WT, "_push_mac.py")

REAL_GIT = r"C:\Program Files\Git\cmd\git.exe"
FALLBACK_GIT = None

PY = sys.executable

passed = []
failed = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    if cond:
        passed.append(label)
        print(f"  [PASS] {label}")
    else:
        failed.append((label, detail))
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))
    return cond


def find_real_git() -> str | None:
    global FALLBACK_GIT
    for p in [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
    ]:
        if os.path.isfile(p):
            return p
    # 退而求其次：WorkBuddy 自带的（仅用于测试，因为它确实是真 git）
    import glob
    hits = glob.glob(os.path.expanduser(
        r"~/.workbuddy/binaries/PortableGit/**/cmd/git.exe"), recursive=True)
    if hits:
        FALLBACK_GIT = hits[0]
        return hits[0]
    w = shutil.which("git")
    return w


def run(args, env, cwd=None, stdin_text=None, timeout=180):
    return subprocess.run(
        [PY, TARGET, *args],
        env=env, cwd=cwd or WT,
        input=stdin_text,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def _run_script(script: str, args, env, cwd=None, stdin_text=None, timeout=180):
    """跑指定脚本（用于跑打过补丁的副本，不污染 TARGET）。"""
    return subprocess.run(
        [PY, script, *args],
        env=env, cwd=cwd or WT,
        input=stdin_text,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def make_env(home: str, git: str | None, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["VIDEO_TOOL_NO_PAUSE"] = "1"
    env["USERPROFILE"] = home
    env["HOME"] = home
    env["HOMEDRIVE"], env["HOMEPATH"] = os.path.splitdrive(home)
    # 屏蔽掉系统 PATH 里的 git，逼脚本走 FORCE_GIT
    env["PUSH_MAC_ALLOW_ANY"] = ""
    if git:
        env["PUSH_MAC_FORCE_GIT"] = git
    else:
        env.pop("PUSH_MAC_FORCE_GIT", None)
    if extra:
        env.update(extra)
    return env


def git_global(home: str, git: str, *args: str):
    env = dict(os.environ)
    env["USERPROFILE"] = home
    env["HOME"] = home
    env["HOMEDRIVE"], env["HOMEPATH"] = os.path.splitdrive(home)
    return subprocess.run([git, "config", "--global", *args],
                          env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)


def main() -> int:
    print("=" * 64)
    print("  _push_mac.py 离线验证")
    print("=" * 64)

    git = find_real_git()
    if not git:
        print("  !! 找不到任何 git.exe，无法验证")
        return 1
    print(f"  用这个真 git: {git}")
    if FALLBACK_GIT:
        print("  （注意：用的是 WorkBuddy 自带版，仅测试用）")
    print()

    tmp_root = tempfile.mkdtemp(prefix="pushmac_")
    try:
        # ── 场景 1：git 未装 ────────────────────────────────────────
        print("[1] git 未装（不设 FORCE_GIT，且 PATH 里也找不到）")
        home1 = os.path.join(tmp_root, "h1")
        os.makedirs(home1, exist_ok=True)
        env = make_env(home1, None)
        env["PUSH_MAC_NO_GIT_AT_ALL"] = "1"
        r = run([], env)
        check("退出码非 0", r.returncode != 0, f"rc={r.returncode}")
        check("提示去装 Git for Windows",
              "git-scm.com" in r.stdout or "winget install" in r.stdout,
              r.stdout[-400:])
        check("明确说「没有可用的 git」", "没有可用的 git" in r.stdout,
              r.stdout[-400:])
        print()

        # ── 场景 2：git 装了但什么都没配 ───────────────────────────
        print("[2] git 装了但 user.name / email / autocrlf 全没配")
        home2 = os.path.join(tmp_root, "h2")
        os.makedirs(home2, exist_ok=True)
        env = make_env(home2, git, {"PUSH_MAC_FORCE_CONFIRM": "N"})
        r = run([], env)
        out = r.stdout
        check("报 user.name 没配", "user.name" in out and "没配" in out)
        check("报 core.autocrlf 没配", "autocrlf" in out and "没配" in out)
        check("列出体检警告条数", "体检发现" in out, out[:200])
        check("用户选 N 时取消", "已取消" in out)
        print()

        # ── 场景 3：autocrlf = true（网上教程的坑）─────────────────
        print("[3] core.autocrlf = true（不该推荐的那个）")
        home3 = os.path.join(tmp_root, "h3")
        os.makedirs(home3, exist_ok=True)
        git_global(home3, git, "user.name", "tester")
        git_global(home3, git, "user.email", "t@example.com")
        git_global(home3, git, "core.autocrlf", "true")
        env = make_env(home3, git, {"PUSH_MAC_FORCE_CONFIRM": "N"})
        r = run([], env)
        out = r.stdout
        check("警告 true 会改写本地文件",
              "true" in out and "改写" in out, out[:300])
        check("建议改成 input", "建议改成 input" in out)
        print()

        # ── 场景 4：autocrlf = false ────────────────────────────────
        print("[4] core.autocrlf = false")
        home4 = os.path.join(tmp_root, "h4")
        os.makedirs(home4, exist_ok=True)
        git_global(home4, git, "user.name", "tester")
        git_global(home4, git, "user.email", "t@example.com")
        git_global(home4, git, "core.autocrlf", "false")
        env = make_env(home4, git, {"PUSH_MAC_FORCE_CONFIRM": "N"})
        r = run([], env)
        out = r.stdout
        check("警告 false 会让 CRLF 原样传上去",
              "false" in out and "原样传" in out, out[:300])
        print()

        # ── 场景 5：配置齐全 + 行尾检查 ─────────────────────────────
        print("[5] 配置齐全（autocrlf=input）→ 体检应全过")
        home5 = os.path.join(tmp_root, "h5")
        os.makedirs(home5, exist_ok=True)
        git_global(home5, git, "user.name", "tester")
        git_global(home5, git, "user.email", "t@example.com")
        git_global(home5, git, "core.autocrlf", "input")
        git_global(home5, git, "core.quotepath", "false")
        env = make_env(home5, git, {"PUSH_MAC_FORCE_CONFIRM": "N"})
        r = run([], env)
        out = r.stdout
        check("体检全部通过提示", "体检全部通过" in out, out[:400])
        check("autocrlf=input 被认成 OK", "core.autocrlf  = input" in out)
        check("行尾检查跑到了", "行尾检查" in out)
        check("检测到 Mac 脚本 CRLF 并说明会自动转换",
              "不用手动改" in out or "都是 LF" in out)
        check(".gitignore 检查跑到了", ".gitignore 是否挡住" in out)
        check("待推送内容预览有文件数和体积", "文件数" in out and "总体积" in out)
        check("git 身份显示正确", "t@example.com" in out)
        print()

        # ── 场景 6：非法仓库地址 ────────────────────────────────────
        print("[6] 仓库地址非法 → 应拒绝")
        home6 = os.path.join(tmp_root, "h6")
        os.makedirs(home6, exist_ok=True)
        git_global(home6, git, "user.name", "tester")
        git_global(home6, git, "user.email", "t@example.com")
        git_global(home6, git, "core.autocrlf", "input")
        env = make_env(home6, git, {"PUSH_MAC_FORCE_URL": "这不是网址"})
        r = run([], env, stdin_text="\n\n\n")
        out = r.stdout
        check("提示不像仓库地址", "不像仓库地址" in out, out[:400])
        check("最终中止", "已取消" in out or "无效" in out)
        print()

        # ── 场景 7：完整推送（推到本地 bare 仓库）──────────────────
        print("[7] 完整推送流程 → 推到本地 bare 仓库")
        home7 = os.path.join(tmp_root, "h7")
        os.makedirs(home7, exist_ok=True)
        git_global(home7, git, "user.name", "tester")
        git_global(home7, git, "user.email", "t@example.com")
        git_global(home7, git, "core.autocrlf", "input")
        git_global(home7, git, "init.defaultBranch", "main")

        bare = os.path.join(tmp_root, "remote.git")
        env7 = make_env(home7, git)
        subprocess.run([git, "init", "--bare", bare], env=env7,
                       capture_output=True, text=True, timeout=60)

        # 工作副本：把 mac/ 拷一份出来推（绝不碰用户真实的 mac/）
        work_mac = os.path.join(tmp_root, "work_mac")
        shutil.copytree(MAC, work_mac,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))

        env7["PUSH_MAC_DIR"] = work_mac
        env7["PUSH_MAC_FORCE_URL"] = bare.replace("\\", "/")
        env7["PUSH_MAC_FORCE_CONFIRM"] = "Y"
        env7["PUSH_MAC_ALLOW_LOCAL_REMOTE"] = "1"

        r = run([], env7, timeout=240)
        out = r.stdout
        check("推送流程退出码 0", r.returncode == 0,
              f"rc={r.returncode}\n{out[-1500:]}")
        check("打印了推送成功", "推送成功" in out, out[-800:])

        # 检查 bare 仓库里真有东西
        env_ls = dict(os.environ)
        env_ls["USERPROFILE"] = home7
        env_ls["HOME"] = home7
        env_ls["HOMEDRIVE"], env_ls["HOMEPATH"] = os.path.splitdrive(home7)
        ls = subprocess.run(
            [git, "--git-dir", bare, "ls-tree", "-r", "--name-only", "main"],
            env=env_ls, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        files = [x for x in ls.stdout.splitlines() if x.strip()]
        check("远程仓库收到了文件", len(files) > 20,
              f"只有 {len(files)} 个")
        check("subtitle.py 推上去了",
              "subtitle.py" in files, f"清单前几个 {files[:5]}")
        check("workflow 推上去了",
              any("workflows/mac-package.yml" in x for x in files),
              str(files[:8]))
        check("没有推 __pycache__",
              not any("__pycache__" in x for x in files))
        check("没有推 build/dist",
              not any(x.startswith("build/") or x.startswith("dist/")
                      for x in files))

        # 🔴 关键：Mac 脚本在远程必须已经变成 LF
        for script in ("打包mac.sh", "一键打包.command"):
            r2 = subprocess.run(
                [git, "--git-dir", bare, "show", f"main:{script}"],
                env=env_ls, capture_output=True, timeout=60)
            blob = r2.stdout
            check(f"远程的 {script} 已是纯 LF（无 CR）",
                  b"\r" not in blob,
                  f"含 {blob.count(b'CR')} 个 CR")

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        for label, detail in failed:
            print(f"    FAIL: {label}")
            if detail:
                print(f"          {detail[:200]}")
    print("=" * 64)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
