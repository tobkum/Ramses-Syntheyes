import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# --- 1. Setup Environment Mocks ---
# SynthEyes specific mocks
sys.modules["PySide2"] = MagicMock()
sys.modules["PySide2.QtWidgets"] = MagicMock()
sys.modules["PySide2.QtCore"] = MagicMock()

# --- 2. Setup Path ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
lib_path = os.path.join(project_root, "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

# Mock Ramses API
import ramses
mock_daemon = MagicMock()
mock_daemon.online.return_value = True
sys.modules["ramses.daemon_interface"] = MagicMock(RamDaemonInterface=MagicMock(instance=lambda: mock_daemon))

# Import Code Under Test
from syntheyes_host import SynthEyesHost
from ramses import LogLevel

class TestSynthEyesHost(unittest.TestCase):

    def setUp(self):
        self.mock_hlev = MagicMock()
        self.host = SynthEyesHost(self.mock_hlev)

    def test_initialization(self):
        """Test if the host initializes correctly."""
        self.assertEqual(self.host.hostName, "SynthEyes")

    def test_normalize_path(self):
        """Verify path normalization."""
        path = r"C:\Data\Scene.sni"
        self.assertEqual(self.host.normalizePath(path), "C:/Data/Scene.sni")

    def test_current_file_path(self):
        """Verify current file path retrieval via SynthEyes API."""
        self.mock_hlev.SNIFileName.return_value = "D:/Work/track.sni"
        self.assertEqual(self.host.currentFilePath(), "D:/Work/track.sni")

    def test_save_as_logic(self):
        """Verify that _saveAs correctly calls SetSNIFileName and triggers Save."""
        target_path = "D:/Work/new_track.sni"
        
        success = self.host._saveAs(target_path, None, None, 1, "Initial", False)
        
        self.assertTrue(success)
        self.mock_hlev.SetSNIFileName.assert_called_with("D:/Work/new_track.sni")
        self.mock_hlev.ClickMainMenuAndWait.assert_called_with("Save")

    def test_open_logic(self):
        """Verify that _open correctly calls OpenSNI."""
        path = "D:/Work/track.sni"
        with patch("os.path.exists", return_value=True):
            success = self.host._open(path, None, None)
            self.assertTrue(success)
            self.mock_hlev.OpenSNI.assert_called_with(path)

if __name__ == "__main__":
    unittest.main()
