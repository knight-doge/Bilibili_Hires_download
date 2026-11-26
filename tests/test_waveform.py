import unittest

try:
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication
    from bili_audio.gui.waveform import LEFT, WaveformView
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class WaveformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["waveform-test", "-platform", "offscreen"])

    def test_paints_before_and_after_loading_audio(self):
        view = WaveformView()
        view.resize(480, 200)
        self.assertEqual(view._x(0), LEFT)
        messages = []
        previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
        try:
            self.assertFalse(view.grab().isNull())
            view.set_data([(-0.5, 0.5), (-0.2, 0.3)], 20, [10])
            self.assertGreater(view._x(10), LEFT)
            self.assertFalse(view.grab().isNull())
        finally:
            qInstallMessageHandler(previous)
        self.assertEqual([message for message in messages if "paint" in message.lower()], [])
