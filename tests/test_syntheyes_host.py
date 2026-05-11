import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# --- Environment mocks (must precede project imports) ---
sys.modules["PySide2"] = MagicMock()
sys.modules["PySide2.QtWidgets"] = MagicMock()
sys.modules["PySide2.QtCore"] = MagicMock()

current_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(os.path.dirname(current_dir), "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

import ramses
mock_daemon = MagicMock()
mock_daemon.online.return_value = True
sys.modules["ramses.daemon_interface"] = MagicMock(
    RamDaemonInterface=MagicMock(instance=lambda: mock_daemon)
)

from syntheyes_host import SynthEyesHost
from ramses import LogLevel


class TestSynthEyesHost(unittest.TestCase):

    def setUp(self):
        self.mock_hlev = MagicMock()
        self.host = SynthEyesHost(self.mock_hlev)

    def test_initialization(self):
        self.assertEqual(self.host.hostName, "SynthEyes")

    def test_normalize_path(self):
        path = r"C:\Data\Scene.sni"
        self.assertEqual(self.host.normalizePath(path), "C:/Data/Scene.sni")

    def test_current_file_path(self):
        self.mock_hlev.SNIFileName.return_value = "D:/Work/track.sni"
        self.assertEqual(self.host.currentFilePath(), "D:/Work/track.sni")

    def test_save_as_calls_save_menu(self):
        """_saveAs sets the filename and triggers the Save menu command."""
        target_path = "D:/Work/new_track.sni"
        # Patch filesystem calls so the test is not machine-dependent.
        with patch("os.path.exists", return_value=False), \
             patch("os.makedirs"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.access", return_value=True):
            success = self.host._saveAs(target_path, None, None, 1, "Initial", False)

        self.assertTrue(success)
        self.mock_hlev.SetSNIFileName.assert_called_with("D:/Work/new_track.sni")
        self.mock_hlev.ClickMainMenuAndWait.assert_called_with("Save")

    def test_save_as_raises_on_missing_directory(self):
        """_saveAs propagates an OSError when the target directory is absent."""
        with patch("os.path.exists", return_value=False), \
             patch("os.makedirs"), \
             patch("os.path.isdir", return_value=False):
            success = self.host._saveAs("/nonexistent/dir/scene.sni", None, None, 1, "", False)
        self.assertFalse(success)

    def test_open_logic(self):
        """_open calls OpenSNI for an existing file."""
        path = "D:/Work/track.sni"
        with patch("os.path.exists", return_value=True):
            success = self.host._open(path, None, None)
        self.assertTrue(success)
        self.mock_hlev.OpenSNI.assert_called_with(path)


if __name__ == "__main__":
    unittest.main()
