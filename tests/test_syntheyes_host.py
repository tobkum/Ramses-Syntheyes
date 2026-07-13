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


class TestPlateDiscovery(unittest.TestCase):
    """Plate lookup for 'New Shot from Plate' and footage imports."""

    def setUp(self):
        self.mock_hlev = MagicMock()
        self.host = SynthEyesHost(self.mock_hlev)

    # --- _pick_footage_file --------------------------------------------------

    def test_pick_footage_skips_ingest_sidecars(self):
        """.ramses_complete sorts first on disk but must never be the plate."""
        files = [
            "D:/pub/001/.ramses_complete",
            "D:/pub/001/_ramses_data.json",
            "D:/pub/001/TEST_S_SH010_PLATE.1002.exr",
            "D:/pub/001/TEST_S_SH010_PLATE.1001.exr",
        ]
        self.assertEqual(
            SynthEyesHost._pick_footage_file(files),
            "D:/pub/001/TEST_S_SH010_PLATE.1001.exr",
        )

    def test_pick_footage_accepts_movies(self):
        files = ["D:/pub/001/_ramses_data.json", "D:/pub/001/plate.mov"]
        self.assertEqual(
            SynthEyesHost._pick_footage_file(files), "D:/pub/001/plate.mov"
        )

    def test_pick_footage_empty_when_only_sidecars(self):
        files = ["D:/pub/001/.ramses_complete", "D:/pub/001/_ramses_data.json"]
        self.assertEqual(SynthEyesHost._pick_footage_file(files), "")
        self.assertEqual(SynthEyesHost._pick_footage_file([]), "")
        self.assertEqual(SynthEyesHost._pick_footage_file(None), "")

    # --- _find_plate_path ----------------------------------------------------

    def _project_with_step(self, short_name):
        step = MagicMock()
        step.shortName.return_value = short_name
        project = MagicMock()
        project.steps.return_value = [step]
        return project, step

    def _item_with_files(self, files):
        item = MagicMock()
        item.latestPublishedVersionFilePaths.return_value = files
        return item

    def test_find_plate_is_case_insensitive(self):
        """A step named PLATE matches the default 'Plate' entry."""
        project, step = self._project_with_step("PLATE")
        item = self._item_with_files(
            ["D:/pub/001/.ramses_complete", "D:/pub/001/SH010.1001.exr"]
        )
        import syntheyes_host
        settings = MagicMock()
        settings.userSettings = {}
        with patch.object(syntheyes_host, "RAM_SETTINGS", settings):
            result = self.host._find_plate_path(project, item)
        self.assertEqual(result, "D:/pub/001/SH010.1001.exr")
        item.latestPublishedVersionFilePaths.assert_called_with(step=step)

    def test_find_plate_ignores_non_plate_steps(self):
        project, _ = self._project_with_step("Comp")
        item = self._item_with_files(["D:/pub/001/SH010.1001.exr"])
        import syntheyes_host
        settings = MagicMock()
        settings.userSettings = {}
        with patch.object(syntheyes_host, "RAM_SETTINGS", settings):
            self.assertEqual(self.host._find_plate_path(project, item), "")

    def test_find_step_by_short_name_case_insensitive(self):
        """A step named MAMO satisfies the 'MaMo' dialog preselection."""
        project, step = self._project_with_step("MAMO")
        found = SynthEyesHost.findStepByShortName(project, "MaMo", "Matchmove")
        self.assertIs(found, step)
        self.assertIsNone(
            SynthEyesHost.findStepByShortName(project, "Comp")
        )
        self.assertIsNone(SynthEyesHost.findStepByShortName(None, "MaMo"))

    def test_find_plate_respects_custom_setting(self):
        project, _ = self._project_with_step("FOOTAGE_RAW")
        item = self._item_with_files(["D:/pub/001/SH010.1001.exr"])
        import syntheyes_host
        settings = MagicMock()
        settings.userSettings = {"plateStepNames": ["footage_raw"]}
        with patch.object(syntheyes_host, "RAM_SETTINGS", settings):
            result = self.host._find_plate_path(project, item)
        self.assertEqual(result, "D:/pub/001/SH010.1001.exr")

    def test_import_prefers_footage_over_sidecars(self):
        """_import handed a full version-folder listing loads the footage."""
        files = [
            "D:/pub/001/.ramses_complete",
            "D:/pub/001/SH010.1001.exr",
        ]
        item = MagicMock()
        # Empty session: no scene file, no shots/trackers/meshes
        self.mock_hlev.SNIFileName.return_value = ""
        self.mock_hlev.NumByType.return_value = 0
        with patch("os.path.exists", return_value=True), \
             patch.object(self.host, "_ensure_connected", return_value=True), \
             patch.object(self.host, "newShot", return_value=True) as mock_new:
            result = self.host._import(files, item, None, [], False)
        self.assertTrue(result)
        mock_new.assert_called_once()
        self.assertIn("SH010.1001.exr", mock_new.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
