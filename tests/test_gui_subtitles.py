import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None
else:
    from bili_audio.gui import app as gui
    from bili_audio.models import Page


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class GuiSubtitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["subtitle-test", "-platform", "offscreen"])

    def wait_for_worker(self, window):
        self.assertTrue(window.download_worker.wait(3000))
        self.app.processEvents()

    def test_loads_subtitles_and_downloads_only_checked_tracks_for_selected_page(self):
        video = SimpleNamespace(bvid="BV1GJ4m1g7Sk", title="测试", owner="测试", pages=[Page(1, 7, "一"), Page(2, 8, "二")])
        track = {"lan": "zh-CN", "lan_doc": "中文"}
        first = dict(track, id_str="1", subtitle_url="//aisubtitle.hdslb.com/first")
        second = dict(track, id_str="2", subtitle_url="//aisubtitle.hdslb.com/second")
        unavailable = dict(track, id_str="3", subtitle_url="")
        page_tracks = {7: [first, unavailable], 8: [first, second]}
        dash = {"dash": {"audio": [{"id": 30280, "bandwidth": 192000, "baseUrl": "https://example.org/audio"}]}}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(gui.auth, "current_session", return_value=(object(), None)), \
                patch.object(gui.api, "get_video", return_value=video), \
                patch.object(gui.api, "get_playurl", return_value=dash), \
                patch.object(gui.subtitles, "get_tracks", side_effect=lambda _s, _bv, cid: page_tracks[cid]), \
                patch.object(gui.subtitles, "fetch_cues", return_value=[gui.subtitles.Cue(1000, 2000, "测试")]) as fetch, \
                patch.object(gui, "load_config", return_value={}), patch.object(gui, "save_config"), \
                patch.object(gui, "process_url") as download:
            window = gui.MainWindow()
            window._done_dialog = lambda *args: None
            window.out.setText(temp)
            window.url.setText(video.bvid)
            window.parse()
            self.wait_for_worker(window)
            choices = [box for _, _, box in window._subtitle_checks]
            self.assertEqual([box.text().split()[0] for box in choices], ["P1", "P2", "P2"])
            self.assertFalse(any(box.isChecked() for box in choices))

            window.pages.setText("2")
            self.assertFalse(window.btn_download.isEnabled())
            window.pages.editingFinished.emit()
            self.wait_for_worker(window)
            self.assertEqual(len(window._subtitle_checks), 2)
            window._subtitle_checks[1][2].click()

            newer = dict(second, subtitle_url=second["subtitle_url"] + "?fresh=1")
            page_tracks[8] = [newer, first]
            recording = SimpleNamespace(video=video, page=video.pages[1], path=str(Path(temp) / "音频.m4a"))
            download.return_value = [recording, recording]
            window.download()
            self.wait_for_worker(window)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[1], newer["subtitle_url"])
            self.assertEqual(len(list(Path(temp).glob("*.srt"))), 1)

            window.url.setText("BV1S1421d74g")
            self.assertEqual(window._subtitle_checks, [])
            self.assertFalse(window.btn_download.isEnabled())
            page_tracks[8] = []
            window.parse()
            self.wait_for_worker(window)
            self.assertEqual(window.subtitle_hint.text(), "无可用字幕")
            self.assertTrue(window.btn_download.isEnabled())
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
