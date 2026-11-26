"""批量解析和下载界面。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import auth, batch, formats
from ..pipeline import Options


class BatchWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str, str)
    prepared = Signal(object)
    downloaded = Signal(str, object)

    def __init__(self, source, opts, queue, preset, captions, verify):
        super().__init__()
        self.source, self.opts, self.queue = source, opts, queue
        self.preset, self.captions, self.verify = preset, captions, verify

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
                                                           caption_mode=self.captions, verify=self.verify, **args)
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

        preset_row = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItems(list(batch.PRESETS))
        self.parameters = QComboBox()
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.captions = QComboBox()
        self.captions.addItem("不下载字幕", "none")
        self.captions.addItem("AI 中文优先，缺失改用普通中文", "chinese")
        self.captions.addItem("全部可用字幕", "all")
        self.preset.setToolTip("首选音轨不可用时按预设降级。输出格式转换失败时保留原始音轨。")
        preset_row.addWidget(self.preset)
        preset_row.addWidget(QLabel("参数"))
        preset_row.addWidget(self.parameters)
        preset_row.addWidget(QLabel("字幕"))
        preset_row.addWidget(self.captions)
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
                       self.preset, self.captions):
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
        self.progress.setRange(0, 0)
        self.log.appendPlainText("—— 开始批量下载 ——" if download else "—— 开始批量解析 ——")
        self._set_busy(True)
        self.worker = BatchWorker(source, opts, self.queue if download else None,
                                  self.preset.currentText(), self.captions.currentData(), self.verify.isChecked())
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self._progress)
        self.worker.prepared.connect(self._prepared)
        self.worker.downloaded.connect(self._downloaded)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _progress(self, done, total, bvid, status):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        if bvid in self.rows:
            self.table.item(self.rows[bvid], 4).setText(status)

    def _prepared(self, queue):
        self.queue = queue
        self.table.setRowCount(len(queue.entries))
        for row, entry in enumerate(queue.entries):
            video = entry.video
            self.rows[video.bvid] = row
            cells = (video.bvid, video.title, f"{len(entry.pages)} / {len(video.pages)}", entry.preview, "待下载")
            for column, value in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.log.appendPlainText(f"队列：{len(queue.entries)} 个视频，"
                                 f"{sum(len(entry.pages) for entry in queue.entries)} 个分 P，"
                                 f"{len(queue.rejected)} 项未能解析")

    def _downloaded(self, folder, summary):
        self.folder = folder
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
