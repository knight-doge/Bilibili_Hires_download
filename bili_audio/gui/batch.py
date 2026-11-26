"""批量解析和下载界面。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import auth, batch, formats
from ..pipeline import Options


class BatchWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str, str)
    prepared = Signal(object)
    downloaded = Signal(str, object)
    captions_progress = Signal(object)

    def __init__(self, source, opts, queue, preset, captions, caption_types, verify):
        super().__init__()
        self.source, self.opts, self.queue = source, opts, queue
        self.preset, self.captions, self.verify = preset, captions, verify
        self.caption_types = caption_types

    def run(self):
        try:
            session, _ = auth.current_session()
            with session:
                args = dict(log=self.log.emit, progress=self.progress.emit, stopped=self.isInterruptionRequested)
                if self.queue is None:
                    queue = batch.prepare_queue(session, self.source, self.opts, **args)
                    if self.isInterruptionRequested():
                        self.log.emit("解析已停止，请重新解析后下载")
                    else:
                        self.prepared.emit(queue)
                else:
                    folder, summary = batch.download_queue(session, self.queue, self.opts, preset=self.preset,
                                                           caption_mode=self.captions, caption_types=self.caption_types,
                                                           captions_progress=self.captions_progress.emit,
                                                           verify=self.verify, **args)
                    self.downloaded.emit(str(folder), summary)
        except Exception as error:
            self.log.emit(f"✗ 批量任务失败：{error}")


class BatchPage(QWidget):
    def __init__(self, output: Path):
        super().__init__()
        self.queue = None
        self.rows = {}
        self.worker = None
        self.folder = ""
        self.caption_order = []
        self.caption_boxes = {}
        layout = QVBoxLayout(self)

        self.sources = QPlainTextEdit()
        self.sources.setPlaceholderText("每行一个 BV 号、视频链接或 B 站合集链接，也可以导入 TXT 清单")
        self.sources.setMaximumHeight(110)
        self.sources.textChanged.connect(self._invalidate)
        layout.addWidget(self.sources)

        row = QHBoxLayout()
        self.import_button = QPushButton("导入 TXT")
        self.import_button.clicked.connect(self._import)
        self.parse_button = QPushButton("批量解析")
        self.parse_button.clicked.connect(lambda: self._start(False))
        row.addWidget(self.import_button)
        row.addStretch(1)
        row.addWidget(self.parse_button)
        layout.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["BV 号", "视频标题", "分 P", "首个所选分 P 音轨", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 2)

        settings = QFormLayout()
        paths = QHBoxLayout()
        self.output = QLineEdit(str(output))
        self.pick_button = QPushButton("选择…")
        self.pick_button.clicked.connect(self._pick)
        paths.addWidget(self.output, 1)
        paths.addWidget(self.pick_button)
        settings.addRow("输出目录", paths)

        options = QHBoxLayout()
        self.pages = QLineEdit("all")
        self.pages.setMaximumWidth(110)
        self.pages.textChanged.connect(self._invalidate)
        self.threads = QComboBox()
        self.threads.addItems(["1", "2", "4", "8"])
        self.threads.setCurrentText("2")
        self.verify = QCheckBox("完整解码检查")
        self.verify.setChecked(True)
        self.tags = QCheckBox("写入标签/封面")
        self.tags.setChecked(True)
        self.clean_filenames = QCheckBox("清理文件名")
        self.clean_filenames.setChecked(True)
        self.clean_filenames.setToolTip("清除音频文件名中的空格、标点和表情，保留分 P 编号和扩展名。")
        for widget in (QLabel("分 P"), self.pages, QLabel("线程"), self.threads,
                       self.verify, self.tags, self.clean_filenames):
            options.addWidget(widget)
        options.addStretch(1)
        settings.addRow("下载设置", options)

        subtitle_row = QHBoxLayout()
        self.captions = QComboBox()
        self.captions.addItem("全部下载", "all")
        self.captions.addItem("按优先级下载", "priority")
        self.captions.setToolTip("全部下载会保存所有已勾选的可用字幕。按优先级下载每首只保存第一条成功导出的字幕。")
        self.captions.currentIndexChanged.connect(lambda: self._caption_coverage())
        self.caption_scroll = QScrollArea()
        self.caption_scroll.setWidgetResizable(True)
        self.caption_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        subtitle_options = QWidget()
        self.caption_options = QHBoxLayout(subtitle_options)
        self.caption_options.setContentsMargins(4, 0, 4, 0)
        self.caption_hint = QLabel()
        self.caption_options.addWidget(self.caption_hint)
        self.caption_options.addStretch()
        self.caption_scroll.setWidget(subtitle_options)
        self.caption_scroll.setFixedHeight(
            self.captions.sizeHint().height() + self.caption_scroll.horizontalScrollBar().sizeHint().height()
        )
        subtitle_row.addWidget(self.captions)
        subtitle_row.addWidget(self.caption_scroll, 1)
        settings.addRow("字幕设置", subtitle_row)
        self.caption_status = QLabel()
        self.caption_status.setWordWrap(True)
        settings.addRow("", self.caption_status)

        preset_row = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItems(list(batch.PRESETS))
        self.parameters = QComboBox()
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.preset.setToolTip("首选音轨不可用时按预设降级。输出格式转换失败时保留原始音轨。")
        preset_row.addWidget(self.preset)
        preset_row.addWidget(QLabel("参数"))
        preset_row.addWidget(self.parameters)
        preset_row.addStretch(1)
        settings.addRow("下载预设", preset_row)
        layout.addLayout(settings)

        buttons = QHBoxLayout()
        self.download_button = QPushButton("开始批量下载")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(lambda: self._start(True))
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.open_button = QPushButton("打开结果文件夹")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder)))
        buttons.addWidget(self.download_button, 1)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log, 1)
        self._preset_changed()

    def _invalidate(self):
        self.queue = None
        self.rows.clear()
        if hasattr(self, "download_button"):
            self.table.setRowCount(0)
            self.download_button.setEnabled(False)
            self._render_captions()

    def _render_captions(self):
        for box in self.caption_boxes.values():
            self.caption_options.removeWidget(box)
            box.hide()
            box.deleteLater()
        self.caption_boxes.clear()
        queue = self.queue
        if queue is None:
            self.caption_hint.setText("批量解析后显示可用字幕")
        else:
            self.caption_order = [kind for kind in self.caption_order if kind in queue.caption_labels]
            for kind in queue.caption_labels:
                box = QCheckBox()
                box.setChecked(kind in self.caption_order)
                box.toggled.connect(lambda checked, kind=kind: self._toggle_caption(kind, checked))
                self.caption_options.insertWidget(self.caption_options.count() - 1, box)
                self.caption_boxes[kind] = box
            self.caption_hint.setText("未发现可用字幕")
            self._number_captions()
        self.caption_hint.setVisible(not self.caption_boxes)
        self._caption_coverage()

    def _toggle_caption(self, kind, checked):
        if checked:
            self.caption_order.append(kind)
        else:
            self.caption_order.remove(kind)
        self._number_captions()
        self._caption_coverage()

    def _number_captions(self):
        counts = self.queue.caption_counts()
        order = {kind: number for number, kind in enumerate(self.caption_order, 1)}
        for kind, box in self.caption_boxes.items():
            prefix = f"{order[kind]}. " if kind in order else ""
            box.setText(f"{prefix}{self.queue.caption_labels[kind]} {counts[kind]}/{self.queue.part_count}")

    def _caption_coverage(self, actual=None):
        queue = self.queue
        if queue is None:
            self.caption_status.clear()
            return
        if not self.caption_order:
            message = "未勾选字幕"
        elif actual is None:
            kinds = set(self.caption_order)
            available = sum(bool(kinds & tracks) for tracks in queue.captions_by_part.values())
            message = f"预计有歌词 {available}/{queue.part_count} 首"
        else:
            message = f"实际有歌词 {actual['parts_with_lyrics']}/{queue.part_count} 首"
            if actual["complete"]:
                message += " · 已完全覆盖"
            else:
                missing = "、".join(queue.caption_labels[kind] for kind in actual["missing_types"])
                message += f" · 尚未保存：{missing}"
        if queue.unknown_captions:
            message += f" · 解析时字幕未确认 {queue.unknown_captions} 首"
        self.caption_status.setText(message)

    def _preset_changed(self):
        fmt, _ = batch.PRESETS[self.preset.currentText()]
        self.parameters.clear()
        choices = formats.preset_choices(fmt)
        for choice in choices:
            self.parameters.addItem(choice.label, choice.id)
        if not choices:
            self.parameters.addItem("跟随源")
        medium = self.parameters.findData("medium")
        if medium >= 0:
            self.parameters.setCurrentIndex(medium)
        self.parameters.setEnabled(len(choices) > 1)
        self._invalidate()

    def _pick(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if folder:
            self.output.setText(folder)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入清单", "", "文本文件 (*.txt)")
        if path:
            try:
                self.sources.setPlainText(Path(path).read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError) as error:
                self.log.appendPlainText(f"✗ 无法读取 UTF-8 清单：{error}")

    def is_busy(self):
        return self.worker is not None and self.worker.isRunning()

    def stop(self):
        if self.is_busy():
            self.worker.requestInterruption()
            self.stop_button.setEnabled(False)
            self.log.appendPlainText("停止已请求，当前分 P 或请求结束后停止")

    def _set_busy(self, active):
        for widget in (self.sources, self.import_button, self.parse_button, self.output,
                       self.pick_button, self.pages, self.threads, self.verify, self.tags, self.clean_filenames,
                       self.preset, self.captions, self.caption_scroll):
            widget.setEnabled(not active)
        self.parameters.setEnabled(not active and self.parameters.count() > 1)
        self.stop_button.setEnabled(active)
        self.download_button.setEnabled(not active and self.queue is not None and bool(self.queue.entries))

    def _start(self, download):
        source = self.sources.toPlainText().strip()
        if self.is_busy() or not source or (download and (self.queue is None or not self.queue.entries)):
            return
        path = self.output.text().strip()
        if download and not path:
            self.log.appendPlainText("请选择输出目录")
            return
        fmt, priority = batch.PRESETS[self.preset.currentText()]
        opts = Options(out=Path(path), fmt=fmt, preset=self.parameters.currentData() or "medium",
                       audio_priority=priority, fallback_to_source=True,
                       pages=self.pages.text().strip() or "all", threads=int(self.threads.currentText()),
                       verify=False, tag=self.tags.isChecked(), clean_filenames=self.clean_filenames.isChecked())
        if not download:
            self._invalidate()
        self._caption_coverage()
        self.progress.setRange(0, 0)
        self.log.appendPlainText("—— 开始批量下载 ——" if download else "—— 开始批量解析 ——")
        self._set_busy(True)
        self.worker = BatchWorker(source, opts, self.queue if download else None,
                                  self.preset.currentText(), self.captions.currentData(),
                                  tuple(self.caption_order), self.verify.isChecked())
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self._progress)
        self.worker.prepared.connect(self._prepared)
        self.worker.downloaded.connect(self._downloaded)
        self.worker.captions_progress.connect(self._caption_coverage)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _progress(self, done, total, bvid, status):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        if status == "读取字幕":
            self.caption_status.setText(f"正在读取字幕 {done}/{total}")
        if bvid in self.rows:
            self.table.item(self.rows[bvid], 4).setText(status)

    def _prepared(self, queue):
        self.queue = queue
        self._render_captions()
        self.table.setRowCount(len(queue.entries))
        for row, entry in enumerate(queue.entries):
            video = entry.video
            self.rows[video.bvid] = row
            cells = (video.bvid, video.title, f"{len(entry.pages)} / {len(video.pages)}", entry.preview, "待下载")
            for column, value in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.log.appendPlainText(f"队列：{len(queue.entries)} 个视频，"
                                 f"{queue.part_count} 个分 P，"
                                 f"{len(queue.rejected)} 项未能解析")

    def _downloaded(self, folder, summary):
        self.folder = folder
        self._caption_coverage(summary["subtitle_coverage"])
        self.open_button.setEnabled(True)
        for video in summary["videos"]:
            self.table.item(self.rows[video["bvid"]], 4).setText("完成" if video["ok"] else "未完成")
        self.log.appendPlainText(f"{'已停止' if summary['stopped'] else '批量结束'}："
                                 f"{summary['videos_complete']} / {summary['videos_requested']} 个视频完成，"
                                 f"保存 {summary['tracks_saved']} 个音频。详情见 summary.json。")

    def _finished(self):
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self._set_busy(False)
