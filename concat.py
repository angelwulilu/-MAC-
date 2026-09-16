# -*- coding: utf-8 -*-
"""
concat.py —— 批量把「片头 + 素材」拼接成成品

第 5 步：把片头和素材拼成一段视频。

拼接方式：**先归一化时间戳，再 concat demuxer + -c copy 流复制**（秒级，画质无损）。
    ✅ 参考用户 bat2（2.统一添加片头.bat）的 -c copy 思路。
    ✅ 但直接 -c copy 会因「素材音频 DTS 起点低于片头结尾」报 Non-monotonic DTS，
       产出 video/audio 时长错乱的坏片。所以先对每个输入用
       `-c copy -fflags +genpts -avoid_negative_ts make_zero` 重封装一遍（重置时间戳、
       很快），再 -c copy 拼接 → 0 警告、时长精确。
    ❌ 不要退回「整段 filter_complex 重编码」——20 个要 8 分钟，用户明确否决。

用法（可单独在命令行测试）：
    python concat.py  "片头.mp4"  "素材1.mp4" "素材2.mp4" ...  "输出文件夹"
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QObject, Signal

import paths
import procctl
from probe import probe

# ffmpeg 的完整路径（自动查找，打包成 exe 后指向自带的 ffmpeg）
FFMPEG = paths.find_ffmpeg()


def _write_concat_list(paths, output_path):
    """把要拼接的文件写成 concat 列表文件（用绝对路径，配合 -safe 0 解析）。

    列表写在「输出文件夹」里，避免写到系统临时目录后 ffmpeg 把里面的相对路径
    当成相对临时目录解析 → 'Impossible to open' 的坑。
    """
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    fd, list_path = tempfile.mkstemp(prefix="concat_list_", suffix=".txt", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '{}'\n".format(os.path.abspath(p)))
    return list_path


def _normalize(in_path, tmp_path, ctrl=None):
    """快速重封装（不改编码）重置时间戳，让 -c copy 拼接不再 DTS 错乱。

    成功返回归一化后的临时文件；失败则返回原路径（让拼接继续尝试）。
    """
    cmd = [FFMPEG, "-y", "-i", in_path, "-c", "copy",
           "-fflags", "+genpts", "-avoid_negative_ts", "make_zero", tmp_path]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                **paths.spawn_kwargs())
        if ctrl is not None and hasattr(ctrl, "register"):
            ctrl.register(proc)
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return in_path
        if proc.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            return tmp_path
    except Exception:
        pass
    finally:
        if ctrl is not None and proc is not None and hasattr(ctrl, "unregister"):
            ctrl.unregister(proc)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return in_path


def concat_one(intro_path, material_path, output_path, ctrl=None):
    """把 intro 和 material 拼成一段视频（快速且可靠）。

    做法（参考用户 bat2 的 -c copy 思路，但加了时间戳归一化保证不出错）：
      1. 先把 intro、material 各用 -fflags +genpts 重封装一遍，重置时间戳；
      2. 再用 concat demuxer + -c copy 流复制拼接（秒级、画质无损）。

    ctrl: 可选 ProcGroup 实例，用于「暂停 / 终止」：
        - 每个 ffmpeg 子进程启动时 register、结束时 unregister（供外部挂起/恢复）
        - ctrl.cancelled 为 True 时立即杀掉当前进程并停止

    返回 (success: bool, message: str)
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."

    # 归一化两个输入（返回临时文件；失败则回退原路径）
    fd_i, intro_n = tempfile.mkstemp(prefix="intro_norm_", suffix=".mp4", dir=out_dir)
    os.close(fd_i)
    fd_m, mat_n = tempfile.mkstemp(prefix="mat_norm_", suffix=".mp4", dir=out_dir)
    os.close(fd_m)
    intro_norm = _normalize(intro_path, intro_n, ctrl)
    mat_norm = _normalize(material_path, mat_n, ctrl)

    list_path = _write_concat_list([intro_norm, mat_norm], output_path)
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", "-movflags", "+faststart", output_path,
    ]
    # 注意：stdout/stderr 不能走 PIPE 又不读 —— ffmpeg 持续写 stderr 会撑满
    # 64KB 管道缓冲区导致进程死锁（表现成“卡住/输出文件损坏”）。
    # 这里把 stderr 重定向到临时文件，失败时再读出来当报错信息。
    err_fd, err_path = tempfile.mkstemp(prefix="concat_err_", suffix=".log", dir=out_dir)
    proc = None
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=err_fd,
                                    **paths.spawn_kwargs())
            if ctrl is not None and hasattr(ctrl, "register"):
                ctrl.register(proc)
            # 轮询等待，便于及时响应「终止」
            while proc.poll() is None:
                if ctrl is not None and ctrl.cancelled:
                    proc.kill()
                    break
                time.sleep(0.1)
            proc.communicate()
            if ctrl is not None and ctrl.cancelled:
                return False, "已取消"
            if proc.returncode != 0:
                try:
                    with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                        return False, f.read()[-3000:]
                except Exception:
                    return False, "合成失败（ffmpeg 返回非 0）"
            return True, "完成"
        finally:
            if ctrl is not None and proc is not None and hasattr(ctrl, "unregister"):
                ctrl.unregister(proc)
            try:
                os.close(err_fd)
            except OSError:
                pass
            for p in (list_path, err_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            # 只删临时归一化文件（回退成原路径时不删原文件）
            for norm, orig in ((intro_norm, intro_path), (mat_norm, material_path)):
                if norm != orig:
                    try:
                        os.remove(norm)
                    except OSError:
                        pass
    except Exception as e:
        return False, str(e)


def _unique_output_path(output_folder, base_name):
    """生成一个「不覆盖已有文件」的输出路径。

    如果 `051_成品.mp4` 已存在，就返回 `051_成品(1).mp4`，依此类推。
    """
    out_path = os.path.join(output_folder, "{}_成品.mp4".format(base_name))
    index = 1
    while os.path.exists(out_path):
        out_path = os.path.join(output_folder, "{}_成品({}).mp4".format(base_name, index))
        index += 1
    return out_path


def concat_all(intro_path, materials, output_folder, progress_callback=None,
               ctrl=None, max_workers=1, outputs=None):
    """批量拼接所有素材。

    参数：
        intro_path: 片头路径
        materials: 素材路径列表
        output_folder: 输出文件夹
        progress_callback: 回调函数(current, total, filename, success, message)
        ctrl: 可选 ProcGroup，用于暂停/终止（并发时对多个 ffmpeg 同时生效）
        max_workers: 同时处理的数量。1=串行（默认，最稳）；
                     >1 时拼接是磁盘拷贝型，SSD 上提速明显，机械盘基本无效。
        outputs: 可选 list，成功的成品路径会被追加进去（供界面统计输出大小）

    返回：
        (success_count, fail_list)
    """
    os.makedirs(output_folder, exist_ok=True)
    total = len(materials)

    # 输出路径先串行算好：_unique_output_path 靠「查重 + 递增编号」防覆盖，
    # 并发里同时跑会有竞态（两个线程拿到同一个 (1) 编号），所以必须在这里做。
    jobs = []
    for mat_path in materials:
        base_name = os.path.splitext(os.path.basename(mat_path))[0]
        jobs.append((mat_path, _unique_output_path(output_folder, base_name)))

    lock = threading.Lock()
    state = {"done": 0, "success": 0}
    fail_list = []

    def _after(mat_path, out_path, ok, msg):
        with lock:
            state["done"] += 1
            if ok:
                state["success"] += 1
                if outputs is not None:
                    outputs.append(out_path)
            else:
                fail_list.append((mat_path, msg))
            cur = state["done"]
        if progress_callback:
            progress_callback(cur, total, mat_path, ok, msg)

    def _process_one(mat_path, out_path):
        if ctrl is not None and ctrl.cancelled:
            _after(mat_path, out_path, False, "已取消")
            return
        ok, msg = concat_one(intro_path, mat_path, out_path, ctrl=ctrl)
        _after(mat_path, out_path, ok, msg)

    if max_workers and max_workers > 1:
        with ThreadPoolExecutor(max_workers=int(max_workers)) as ex:
            futs = [ex.submit(_process_one, m, o) for m, o in jobs]
            for fut in futs:
                try:
                    fut.result()
                except Exception as e:
                    pass   # _process_one 内部已兜底，这里只防意外
    else:
        for mat_path, out_path in jobs:
            if ctrl is not None and ctrl.cancelled:
                break
            _process_one(mat_path, out_path)

    return state["success"], fail_list


class ConcatWorker(QObject):
    """在后台线程里跑批量合成，通过信号把进度传回界面。"""

    # current, total, filename, success, message
    progress = Signal(int, int, str, bool, str)
    # success_count, fail_list
    finished = Signal(int, list)

    def __init__(self, intro_path, materials, output_folder, max_workers=1):
        super().__init__()
        self.intro_path = intro_path
        self.materials = materials
        self.output_folder = output_folder
        self.max_workers = max_workers
        self.ctrl = procctl.ProcGroup()
        self.outputs = []          # 本批成功的成品路径（界面用来统计输出大小）

    def cancel(self):
        """终止：置取消标志，并立刻杀掉正在跑的 ffmpeg（并发时可能有多个）。"""
        self.ctrl.cancelled = True
        self.ctrl.kill_all()

    def pause(self):
        """暂停：挂起当前所有 ffmpeg 进程（Windows 下真正停住当前帧）。"""
        self.ctrl.suspend_all()

    def resume(self):
        """继续：恢复被挂起的 ffmpeg 进程。"""
        self.ctrl.resume_all()

    def run(self):
        def callback(current, total, filename, success, message):
            self.progress.emit(current, total, filename, success, message)

        try:
            success_count, fail_list = concat_all(
                self.intro_path, self.materials, self.output_folder,
                callback, self.ctrl, self.max_workers, self.outputs,
            )
            if self.ctrl.cancelled:
                self.finished.emit(-1, [])
            else:
                self.finished.emit(success_count, fail_list)
        except Exception as e:
            self.finished.emit(-1, [("内部错误", str(e))])


def main():
    if len(sys.argv) < 4:
        print("用法：python concat.py  \"片头.mp4\"  \"素材1.mp4\" [\"素材2.mp4\" ...]  \"输出文件夹\"")
        return

    intro_path = sys.argv[1]
    materials = sys.argv[2:-1]
    output_folder = sys.argv[-1]

    print("开始批量拼接，共 {} 个素材".format(len(materials)))
    print("输出到：{}".format(output_folder))
    print("-" * 40)

    def callback(current, total, filename, success, message):
        status = "成功" if success else "失败"
        print("[{}/{}] {} → {}".format(current, total, os.path.basename(filename), status))
        if not success:
            print(message)

    success_count, fail_list = concat_all(intro_path, materials, output_folder, callback)

    print("-" * 40)
    print("完成：{} 成功，{} 失败".format(success_count, len(fail_list)))
    if fail_list:
        print("失败的文件：")
        for path, msg in fail_list:
            print("  ", path)


if __name__ == "__main__":
    main()
