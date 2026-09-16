# -*- coding: utf-8 -*-
"""
batch_dialog.py —— 批处理结束的提示弹窗（拼接 / 烧字幕共用）

为什么不用 QMessageBox：
    1) 失败清单要能**滚动 + 复制 + 存 txt**（QMessageBox 内容长了被截断、还复制不了）；
    2) 跑完顺手能「打开输出文件夹」（不然还得自己去桌面翻）；
    3) 跑完顺手能「记录本批到编码参考」——文件数、源/输出大小、耗时这些
       程序全都知道，比手填省事，数据口径也统一。

两个 tab 共用这一处，保证行为一致。
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

import utils


def show_batch_done(parent, title, success_count, fail_count, elapsed_str,
                    output_folder, src_mb=None, out_mb=None,
                    fail_list=None, on_record=None):
    """显示批处理结束弹窗。

    参数：
        title: 窗口标题，如「合成完成」
        success_count / fail_count: 成功、失败数量
        elapsed_str: 已格式化好的耗时文字（**不含暂停时间**）
        output_folder: 成品所在文件夹
        src_mb / out_mb: 源文件、成品的合计大小（MB，两数都给才显示这一行）
        fail_list: [(路径, 原因), ...]，有失败时显示可复制的明细区
        on_record: 点「记录本批到编码参考」时调用的函数，返回记录总数或 None
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(720, 500 if fail_list else 260)
    v = QVBoxLayout(dlg)

    if fail_list:
        head_text = ("成功 <b>{}</b> 个，失败 <b>{}</b> 个，总耗时 {}。".format(
            success_count, fail_count, elapsed_str))
    else:
        head_text = "全部 <b>{}</b> 个完成，总耗时 {}。".format(success_count, elapsed_str)
    head = QLabel(head_text)
    head.setWordWrap(True)
    v.addWidget(head)

    info = QLabel("输出文件夹：{}".format(output_folder or "-"))
    info.setWordWrap(True)
    info.setStyleSheet("color: #444;")
    v.addWidget(info)

    if src_mb is not None and out_mb is not None:
        size_line = QLabel("源文件 {:.1f} MB → 输出 {:.1f} MB".format(src_mb, out_mb))
        size_line.setStyleSheet("color: #666;")
        v.addWidget(size_line)

    detail = ""
    if fail_list:
        v.addWidget(QLabel("失败的文件（文件名：原因）："))
        detail = "\n".join(
            "{}. {}：{}".format(i + 1, os.path.basename(path), (msg or "").strip()[:300])
            for i, (path, msg) in enumerate(fail_list))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(detail)
        view.setStyleSheet("font-family: Consolas, 'Microsoft YaHei', monospace;")
        v.addWidget(view, 1)

    row = QHBoxLayout()

    def _open_folder():
        if not utils.open_in_explorer(output_folder):
            QMessageBox.information(dlg, "提示", "打不开这个文件夹：\n{}".format(output_folder))

    open_btn = QPushButton("打开输出文件夹")
    open_btn.clicked.connect(_open_folder)

    record_btn = None
    if on_record is not None:
        record_btn = QPushButton("记录本批到编码参考")
        record_btn.setToolTip(
            "把这批的文件数、源/输出大小、编码设置、耗时自动记进「编码参考」，\n"
            "不用再手动填。备注里可以自己补机器信息。")

        def _record():
            try:
                n = on_record()
            except Exception as e:
                QMessageBox.critical(dlg, "记录失败", str(e))
                return
            if n is None:
                return
            record_btn.setText("已记录（共 {} 条）".format(n))
            record_btn.setEnabled(False)
            QMessageBox.information(
                dlg, "已记录",
                "本批数据已写入「编码参考」（共 {} 条）。\n\n"
                "备注建议自己在参考表里补一下机器信息（如显卡型号）。".format(n))

        record_btn.clicked.connect(_record)

    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dlg.accept)

    if fail_list:
        copy_btn = QPushButton("复制失败清单")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(detail))

        def save_txt():
            path, _ = QFileDialog.getSaveFileName(
                dlg, "保存失败清单",
                os.path.join(output_folder or os.path.expanduser("~"), "失败清单.txt"),
                "文本文件 (*.txt);;所有文件 (*)")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(head_text.replace("<b>", "").replace("</b>", "") + "\n")
                    f.write("输出文件夹：{}\n\n".format(output_folder))
                    f.write(detail)
                QMessageBox.information(dlg, "已保存", "失败清单已保存到：\n{}".format(path))
            except Exception as e:
                QMessageBox.critical(dlg, "保存失败", str(e))

        save_btn = QPushButton("保存为 txt")
        save_btn.clicked.connect(save_txt)
        row.addWidget(copy_btn)
        row.addWidget(save_btn)

    row.addStretch(1)
    row.addWidget(open_btn)
    if record_btn is not None:
        row.addWidget(record_btn)
    row.addWidget(close_btn)
    v.addLayout(row)

    dlg.exec()
    return dlg
