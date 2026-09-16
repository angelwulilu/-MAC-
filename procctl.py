# -*- coding: utf-8 -*-
"""
procctl.py —— 对子进程（ffmpeg）做「挂起 / 恢复 / 终止」。

ffmpeg 本身没有交互式暂停指令，只能从外部用 OS 级挂起来实现。

支持两个平台：
  - Windows：ctypes 调 ntdll 的 NtSuspendProcess / NtResumeProcess
             （把整个进程含其所有线程挂起）
  - macOS / Linux：os.kill(pid, SIGSTOP) / SIGCONT

两者语义一致：都是「冻结整个进程，之后能原样恢复」。
SIGSTOP 不能被进程忽略（不像 SIGTSTP），所以对 ffmpeg 一定生效。
"""

import os
import signal
import sys
import threading

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll

    PROCESS_SUSPEND_RESUME = 0x0800

    def _open_handle(pid, access):
        h = kernel32.OpenProcess(access, False, pid)
        if not h:
            raise ctypes.WinError()
        return h

    def suspend(pid):
        """挂起整个进程（所有线程暂停），ffmpeg 当前帧会停在原地。"""
        h = _open_handle(pid, PROCESS_SUSPEND_RESUME)
        try:
            status = ntdll.NtSuspendProcess(h)
            if status != 0:
                raise ctypes.WinError()
        finally:
            kernel32.CloseHandle(h)

    def resume(pid):
        """恢复被挂起的进程。"""
        h = _open_handle(pid, PROCESS_SUSPEND_RESUME)
        try:
            status = ntdll.NtResumeProcess(h)
            if status != 0:
                raise ctypes.WinError()
        finally:
            kernel32.CloseHandle(h)

else:
    # ------------------------------------------------------------ POSIX
    # macOS 和 Linux 都用 SIGSTOP / SIGCONT。
    #
    # ⚠️ 用 SIGSTOP 而不是 SIGTSTP：
    #    SIGTSTP 可以被进程忽略或捕获（终端里 Ctrl+Z 发的就是它），
    #    ffmpeg 完全可能不理会；SIGSTOP 由内核直接处理，**不可忽略、不可捕获**，
    #    所以一定能停下来。这也是 macOS 上「停止进程」的标准做法。
    #
    # ⚠️ 权限：只能操作自己启动的子进程（同一用户）。本工具就是自己 Popen 出来的
    #    ffmpeg，所以不会遇到 PermissionError。真遇到说明 pid 已不属于我们，
    #    直接抛给调用方（ProcGroup 里会吞掉，不影响其它进程）。

    def suspend(pid):
        """挂起整个进程（SIGSTOP），ffmpeg 当前帧会停在原地。"""
        os.kill(pid, signal.SIGSTOP)

    def resume(pid):
        """恢复被挂起的进程（SIGCONT）。"""
        os.kill(pid, signal.SIGCONT)


class ProcGroup:
    """管理一批同时在跑的 ffmpeg 子进程，暂停/继续/终止对所有进程生效。

    串行时只有一个进程；并发（设置里的「同时处理数」>1）时可能有多个，
    所以暂停/终止必须对一组进程生效，而不是单个 ctrl.proc。

    - register/unregister：各任务在启动/结束时登记自己的 Popen；
    - cancelled：置 True 后，各任务循环会尽快收尾；kill_all() 立刻杀掉在跑的进程。
    """

    def __init__(self):
        self._procs = set()
        self._lock = threading.Lock()
        self.cancelled = False

    def register(self, proc):
        with self._lock:
            self._procs.add(proc)

    def unregister(self, proc):
        with self._lock:
            self._procs.discard(proc)

    def _live(self):
        with self._lock:
            procs = list(self._procs)
        return [p for p in procs if p.poll() is None]

    def suspend_all(self):
        """暂停：挂起当前所有在跑的 ffmpeg 进程。"""
        for p in self._live():
            try:
                suspend(p.pid)
            except Exception:
                pass

    def resume_all(self):
        """继续：恢复所有被挂起的 ffmpeg 进程。"""
        for p in self._live():
            try:
                resume(p.pid)
            except Exception:
                pass

    def kill_all(self):
        """终止：杀掉所有在跑的 ffmpeg 进程。"""
        for p in self._live():
            try:
                p.kill()
            except Exception:
                pass
