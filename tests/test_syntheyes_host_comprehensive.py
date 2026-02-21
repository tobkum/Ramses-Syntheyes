import sys
import os
import unittest
import json
import time
from unittest.mock import MagicMock, patch, ANY

# --- 1. Setup Environment Mocks ---
sys.modules["PySide2"] = MagicMock()
sys.modules["PySide2.QtWidgets"] = MagicMock()
sys.modules["PySide2.QtCore"] = MagicMock()

# --- 2. Setup Path ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
lib_path = os.path.join(project_root, "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

# Mock Ramses API components
import ramses
from ramses import LogLevel

mock_daemon = MagicMock()
mock_daemon.online.return_value = True
sys.modules["ramses.daemon_interface"] = MagicMock(RamDaemonInterface=MagicMock(instance=lambda: mock_daemon))

# Import Code Under Test
from syntheyes_host import SynthEyesHost

class TestSynthEyesHostComprehensive(unittest.TestCase):

    def setUp(self):
        self.mock_hlev = MagicMock()
        self.host = SynthEyesHost(self.mock_hlev)
        # Mock RAMSES instance for project settings
        self.mock_project = MagicMock()
        self.mock_project.aspectRatio.return_value = 1.777
        self.mock_project.framerate.return_value = 24.0
        
        patcher = patch("ramses.RAMSES.project", return_value=self.mock_project)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_embedded_identity_recovery(self):
        """Verify that currentItem correctly extracts identity from scene notes."""
        meta = {"itemUUID": "test-uuid-123", "projectUUID": "proj-uuid-456"}
        mock_scene = MagicMock()
        mock_scene.notes = f"""Some user notes
RAMSES_ID:{json.dumps(meta)}
More notes"""
        self.mock_hlev.Scene.return_value = mock_scene
        self.mock_hlev.SNIFileName.return_value = "C:/Path/Scene.sni"

        with patch("ramses.RamShot") as mock_shot_cls:
            mock_shot = MagicMock()
            mock_shot.shortName.return_value = "SH010"
            mock_shot_cls.return_value = mock_shot
            
            item = self.host.currentItem()
            
            self.assertEqual(item, mock_shot)
            mock_shot_cls.assert_called_with("test-uuid-123")

    def test_import_atomic_locking(self):
        """Verify that import is wrapped in a global lock for thread safety."""
        mock_item = MagicMock()
        mock_item.project().aspectRatio.return_value = 1.777
        
        # Mock existing shots so it uses AddShot
        self.mock_hlev.NumByType.return_value = 1
        mock_shot = MagicMock()
        mock_shot.frames = 100
        self.mock_hlev.Shots.return_value = [mock_shot]
        self.mock_hlev.AddShot.return_value = MagicMock()

        with patch("os.path.exists", return_value=True):
            self.host._import(["C:/media.mov"], mock_item, MagicMock(), [], False)
            
            # Verify Lock/Unlock sequence
            self.mock_hlev.Lock.assert_called_once()
            self.mock_hlev.Unlock.assert_called_once()

    def test_differential_sync_logic(self):
        """Verify that heavy refresh (cache flush) only happens if framerate changes."""
        mock_shot = MagicMock()
        mock_shot.rate = 24.0  # Current scene rate
        self.mock_hlev.Shots.return_value = [mock_shot]
        
        # 1. No change case (24.0 -> 24.0)
        setup_opts = {"framerate": 24.0}
        self.host._setupCurrentFile(MagicMock(), MagicMock(), setup_opts)
        self.mock_hlev.BeginShotChanges.assert_not_called()

        # 2. Change case (24.0 -> 30.0)
        setup_opts = {"framerate": 30.0}
        self.host._setupCurrentFile(MagicMock(), MagicMock(), setup_opts)
        self.mock_hlev.BeginShotChanges.assert_called_with(mock_shot)
        self.mock_hlev.AcceptShotChanges.assert_called_once()

    def test_smart_frame_range_preservation(self):
        """Verify that we trust SynthEyes frames over Ramses '1 frame' movie registration."""
        mock_shot = MagicMock()
        mock_shot.frames = 250  # SynthEyes knows it's a long movie
        self.mock_hlev.Shots.return_value = [mock_shot]
        
        # Ramses registration for movies is often just '1'
        setup_opts = {"frames": 1, "framerate": 24.0}
        
        self.host._setupCurrentFile(MagicMock(), MagicMock(), setup_opts)
        
        # Verify that we used the 250 from SE, not the 1 from Ramses
        # AnimEnd = Start(1001) + Frames(250) - 1 = 1250
        self.mock_hlev.SetAnimEnd.assert_called_with(1250)

    def test_force_save_dirty_state(self):
        """Verify that _markDirtyAndSave forces a change without flushing cache."""
        self.mock_hlev.HasChanged.return_value = False
        mock_shot = MagicMock()
        self.mock_hlev.Shots.return_value = [mock_shot]
        
        self.host._markDirtyAndSave("C:/save.sni")
        
        # Should use plain Begin/Accept (not ShotChanges) to preserve cache
        self.mock_hlev.Begin.assert_called_once()
        self.mock_hlev.Accept.assert_called_with("Ramses: Force Save")
        # Should verify filename was set
        self.mock_hlev.SetSNIFileName.assert_called_with("C:/save.sni")
        # Should trigger standard Save menu
        self.mock_hlev.ClickMainMenuAndWait.assert_called_with("Save")

    def test_import_failure_graceful_unlock(self):
        """Verify that a failure during import still releases the global lock."""
        # Setup mock to fail during AddShot
        self.mock_hlev.AddShot.side_effect = Exception("SynthEyes Crashed")
        self.mock_hlev.NumByType.return_value = 1 # Force path through AddShot
        
        with patch("os.path.exists", return_value=True):
            # _import catches exceptions internally and returns False
            result = self.host._import(["C:/media.mov"], MagicMock(), MagicMock(), [], False)
            
            self.assertFalse(result)
            self.mock_hlev.Unlock.assert_called_once()

if __name__ == "__main__":
    unittest.main()
