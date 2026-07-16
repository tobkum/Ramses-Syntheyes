import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch, call

# --- Environment mocks (must precede project imports) ---
sys.modules["PySide2"] = MagicMock()
sys.modules["PySide2.QtWidgets"] = MagicMock()
sys.modules["PySide2.QtCore"] = MagicMock()

current_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(os.path.dirname(current_dir), "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

import ramses
from ramses import LogLevel

mock_daemon = MagicMock()
mock_daemon.online.return_value = True
sys.modules["ramses.daemon_interface"] = MagicMock(
    RamDaemonInterface=MagicMock(instance=lambda: mock_daemon)
)

from syntheyes_host import SynthEyesHost


def _make_host(hlev=None):
    if hlev is None:
        hlev = MagicMock()
    host = SynthEyesHost(hlev)
    return host, hlev


def _scene_with_notes(notes: str) -> MagicMock:
    scene = MagicMock()
    scene.notes = notes
    return scene


def _ramses_id_notes(item_uuid: str, step_uuid: str = None) -> str:
    meta = {"itemUUID": item_uuid, "projectUUID": "proj-uuid"}
    if step_uuid:
        meta["stepUUID"] = step_uuid
    return f"user notes\nRAMSES_ID:{json.dumps(meta)}\nmore notes"


# ---------------------------------------------------------------------------
# Connection guard
# ---------------------------------------------------------------------------

class TestEnsureConnected(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()

    def test_returns_true_when_version_succeeds(self):
        self.hlev.Version.return_value = "2026"
        self.assertTrue(self.host._ensure_connected())

    def test_reconnects_when_version_raises(self):
        self.hlev.Version.side_effect = Exception("socket closed")
        self.hlev.OpenExisting.return_value = True
        self.assertTrue(self.host._ensure_connected())
        self.hlev.OpenExisting.assert_called_once()

    def test_returns_false_when_reconnect_fails(self):
        self.hlev.Version.side_effect = Exception("socket closed")
        self.hlev.OpenExisting.return_value = False
        self.assertFalse(self.host._ensure_connected())

    def test_save_returns_false_on_dead_connection(self):
        """save() must bail before any SyPy3 call when the connection is dead."""
        self.hlev.Version.side_effect = Exception("socket closed")
        self.hlev.OpenExisting.return_value = False
        result = self.host.save()
        self.assertFalse(result)
        self.hlev.HasChanged.assert_not_called()


# ---------------------------------------------------------------------------
# Identity recovery: _parse_scene_identity and _sidecar_identity
# ---------------------------------------------------------------------------

class TestIdentityParsing(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()

    def test_parse_scene_identity_returns_item_and_step(self):
        self.hlev.Scene.return_value = _scene_with_notes(
            _ramses_id_notes("item-uuid-1", "step-uuid-1")
        )
        with patch("ramses.RamShot") as MockShot, \
             patch("ramses.RamAsset"), \
             patch("ramses.RamStep") as MockStep:
            mock_shot = MagicMock()
            mock_shot.shortName.return_value = "SH010"
            MockShot.return_value = mock_shot

            mock_step = MagicMock()
            mock_step.shortName.return_value = "MaMo"
            MockStep.return_value = mock_step

            item, step = self.host._parse_scene_identity()

        self.assertEqual(item, mock_shot)
        self.assertEqual(step, mock_step)
        MockShot.assert_called_once_with("item-uuid-1")
        MockStep.assert_called_once_with("step-uuid-1")

    def test_parse_scene_identity_returns_none_on_missing_tag(self):
        self.hlev.Scene.return_value = _scene_with_notes("no tag here")
        item, step = self.host._parse_scene_identity()
        self.assertIsNone(item)
        self.assertIsNone(step)

    def test_parse_scene_identity_returns_none_on_scene_error(self):
        self.hlev.Scene.side_effect = Exception("no scene")
        item, step = self.host._parse_scene_identity()
        self.assertIsNone(item)
        self.assertIsNone(step)

    def test_sidecar_identity_reads_both_values(self):
        with patch("syntheyes_host.RamMetaDataManager") as MockMDM, \
             patch("ramses.RamShot") as MockShot, \
             patch("ramses.RamAsset"), \
             patch("ramses.RamStep") as MockStep:
            MockMDM.getValue.return_value = {
                "itemUUID": "item-uuid-2",
                "stepUUID": "step-uuid-2",
            }
            mock_shot = MagicMock()
            mock_shot.shortName.return_value = "SH020"
            MockShot.return_value = mock_shot

            mock_step = MagicMock()
            mock_step.shortName.return_value = "MaMo"
            MockStep.return_value = mock_step

            item, step = self.host._sidecar_identity("/some/path.sni")

        self.assertEqual(item, mock_shot)
        self.assertEqual(step, mock_step)

    def test_sidecar_identity_returns_none_on_error(self):
        with patch("ramses.RamMetaDataManager") as MockMDM:
            MockMDM.getValue.side_effect = Exception("IO error")
            item, step = self.host._sidecar_identity("/some/path.sni")
        self.assertIsNone(item)
        self.assertIsNone(step)


# ---------------------------------------------------------------------------
# currentContext() — combined single-parse lookup
# ---------------------------------------------------------------------------

class TestCurrentContext(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()

    def _setup_named_scene(self, path="C:/pipeline/SH010.sni"):
        self.hlev.SNIFileName.return_value = path

    def test_returns_pending_for_unsaved_scene(self):
        self.hlev.SNIFileName.return_value = ""
        mock_item = MagicMock()
        mock_step = MagicMock()
        self.host._pending_new_shot_item = mock_item
        self.host._pending_new_shot_step = mock_step

        item, step = self.host.currentContext()

        self.assertEqual(item, mock_item)
        self.assertEqual(step, mock_step)

    def test_returns_both_from_scene_notes_in_one_parse(self):
        """Scene() must be called exactly once — not once per value."""
        self._setup_named_scene()
        self.hlev.Scene.return_value = _scene_with_notes(
            _ramses_id_notes("item-uuid", "step-uuid")
        )
        with patch("ramses.RamShot") as MockShot, \
             patch("ramses.RamAsset"), \
             patch("ramses.RamStep") as MockStep:
            shot = MagicMock(); shot.shortName.return_value = "SH010"
            MockShot.return_value = shot
            step_obj = MagicMock(); step_obj.shortName.return_value = "MaMo"
            MockStep.return_value = step_obj

            item, step = self.host.currentContext()

        self.assertEqual(item, shot)
        self.assertEqual(step, step_obj)
        self.assertEqual(self.hlev.Scene.call_count, 1)

    def test_falls_back_to_sidecar_when_notes_missing(self):
        self._setup_named_scene()
        self.hlev.Scene.return_value = _scene_with_notes("no tag")

        with patch("syntheyes_host.RamMetaDataManager") as MockMDM, \
             patch("ramses.RamShot") as MockShot, \
             patch("ramses.RamAsset"), \
             patch("ramses.RamStep") as MockStep:
            MockMDM.getValue.return_value = {
                "itemUUID": "sid-item",
                "stepUUID": "sid-step",
            }
            sid_shot = MagicMock(); sid_shot.shortName.return_value = "SH010"
            MockShot.return_value = sid_shot
            sid_step = MagicMock(); sid_step.shortName.return_value = "MaMo"
            MockStep.return_value = sid_step

            # base class returns virtual/unknown items
            with patch.object(self.host.__class__.__bases__[0], "currentItem",
                               return_value=MagicMock(virtual=lambda: True)), \
                 patch.object(self.host.__class__.__bases__[0], "currentStep",
                               return_value=MagicMock(shortName=lambda: "Unknown")):
                item, step = self.host.currentContext()

        self.assertEqual(item, sid_shot)
        self.assertEqual(step, sid_step)
        MockMDM.getValue.assert_called_once()  # sidecar read happened exactly once


# ---------------------------------------------------------------------------
# Scene notes recovery (currentItem / currentStep individually)
# ---------------------------------------------------------------------------

class TestEmbeddedIdentityRecovery(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()
        self.hlev.SNIFileName.return_value = "C:/Path/Scene.sni"

    def test_current_item_from_notes(self):
        self.hlev.Scene.return_value = _scene_with_notes(
            _ramses_id_notes("item-uuid-123")
        )
        with patch("ramses.RamShot") as MockShot, patch("ramses.RamAsset"):
            shot = MagicMock(); shot.shortName.return_value = "SH010"
            MockShot.return_value = shot
            item = self.host.currentItem()
        self.assertEqual(item, shot)
        MockShot.assert_called_with("item-uuid-123")

    def test_current_item_falls_back_to_asset(self):
        """When RamShot returns Unknown, try RamAsset."""
        self.hlev.Scene.return_value = _scene_with_notes(
            _ramses_id_notes("asset-uuid-999")
        )
        with patch("ramses.RamShot") as MockShot, \
             patch("ramses.RamAsset") as MockAsset:
            unknown = MagicMock(); unknown.shortName.return_value = "Unknown"
            MockShot.return_value = unknown
            asset = MagicMock(); asset.shortName.return_value = "HERO"
            MockAsset.return_value = asset

            item = self.host.currentItem()
        self.assertEqual(item, asset)

    def test_current_step_from_notes(self):
        self.hlev.Scene.return_value = _scene_with_notes(
            _ramses_id_notes("item-uuid", "step-uuid-456")
        )
        with patch("ramses.RamShot") as MockShot, \
             patch("ramses.RamAsset"), \
             patch("ramses.RamStep") as MockStep:
            shot = MagicMock(); shot.shortName.return_value = "SH010"
            MockShot.return_value = shot
            step = MagicMock(); step.shortName.return_value = "MaMo"
            MockStep.return_value = step

            result = self.host.currentStep()
        self.assertEqual(result, step)


# ---------------------------------------------------------------------------
# _setupCurrentFile: FPS + resolution sync
# ---------------------------------------------------------------------------

class TestSetupCurrentFile(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()
        self.shot = MagicMock()
        self.shot.rate = 24.0
        self.shot.Get.return_value = None   # default: no existing resolution
        self.hlev.Shots.return_value = [self.shot]

    def test_no_heavy_refresh_when_fps_unchanged(self):
        self.host._setupCurrentFile(MagicMock(), MagicMock(), {"framerate": 24.0})
        self.hlev.BeginShotChanges.assert_not_called()

    def test_heavy_refresh_when_fps_changes(self):
        self.host._setupCurrentFile(MagicMock(), MagicMock(), {"framerate": 30.0})
        self.hlev.BeginShotChanges.assert_called_with(self.shot)
        self.hlev.AcceptShotChanges.assert_called_once()

    def test_resolution_sync_uses_set_not_attr(self):
        """shot.Set() must be used for width/height/pixelAspect — not Python attr."""
        # shot.Get returns 0 for width → triggers the sync
        self.shot.Get.return_value = 0
        opts = {"framerate": 24.0, "width": 1920, "height": 1080, "pixelAspectRatio": 1.0}
        self.host._setupCurrentFile(MagicMock(), MagicMock(), opts)

        set_calls = [c[0][0] for c in self.shot.Set.call_args_list]
        self.assertIn("width", set_calls)
        self.assertIn("height", set_calls)
        self.assertIn("pixelAspect", set_calls)
        # Must NOT set via Python attribute assignment
        self.assertFalse(hasattr(self.shot, "__dict__") and
                         "width" in self.shot.__dict__)

    def test_resolution_no_sync_when_values_match(self):
        """No BeginShotChanges if resolution and FPS already match Ramses values."""
        self.shot.Get.side_effect = lambda key: {
            "width": 1920, "height": 1080, "pixelAspect": 1.0
        }.get(key)
        opts = {"framerate": 24.0, "width": 1920, "height": 1080, "pixelAspectRatio": 1.0}
        self.host._setupCurrentFile(MagicMock(), MagicMock(), opts)
        self.hlev.BeginShotChanges.assert_not_called()

    def test_smart_frame_range_trusts_syntheyes_over_ramses_movie(self):
        """If Ramses says 1 frame (movie placeholder) but SynthEyes has more, use SE."""
        self.shot.frames = 250
        self.host._setupCurrentFile(MagicMock(), MagicMock(),
                                    {"frames": 1, "framerate": 24.0})
        # AnimEnd = compStartFrame(1001) + 250 - 1 = 1250
        self.hlev.SetAnimEnd.assert_called_with(1250)


# ---------------------------------------------------------------------------
# _markDirtyAndSave: dirty-forcing and path pre-validation
# ---------------------------------------------------------------------------

class TestMarkDirtyAndSave(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()

    def _run(self, path, isdir=True, writable=True):
        with patch("os.path.isdir", return_value=isdir), \
             patch("os.access", return_value=writable), \
             patch("os.path.exists", return_value=True):
            self.host._markDirtyAndSave(path)

    def test_forces_dirty_via_shot_set_not_attr(self):
        """Dirty state must be set via shot.Set(), not Python attribute assignment."""
        self.hlev.HasChanged.return_value = False
        mock_shot = MagicMock()
        self.hlev.Shots.return_value = [mock_shot]
        self._run("C:/work/scene.sni")
        mock_shot.Set.assert_called()
        key = mock_shot.Set.call_args[0][0]
        self.assertEqual(key, "ramses_saved")

    def test_uses_begin_accept_not_shot_changes(self):
        """Dirty-forcing must use Begin/Accept to avoid triggering the RAM-cache flush."""
        self.hlev.HasChanged.return_value = False
        self.hlev.Shots.return_value = [MagicMock()]
        self._run("C:/work/scene.sni")
        self.hlev.Begin.assert_called()
        self.hlev.Accept.assert_called_with("Ramses: Force Save")
        self.hlev.BeginShotChanges.assert_not_called()

    def test_triggers_save_menu(self):
        self.hlev.HasChanged.return_value = True  # already dirty — skip the marking
        self._run("C:/work/scene.sni")
        self.hlev.ClickMainMenuAndWait.assert_called_with("Save")

    def test_raises_on_missing_directory(self):
        self.hlev.HasChanged.return_value = True
        with patch("os.path.isdir", return_value=False), \
             patch("os.path.exists", return_value=True):
            with self.assertRaises(OSError):
                self.host._markDirtyAndSave("/missing/dir/scene.sni")
        self.hlev.SetSNIFileName.assert_not_called()

    def test_raises_on_readonly_file(self):
        self.hlev.HasChanged.return_value = True
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("os.access", return_value=False):
            with self.assertRaises(OSError):
                self.host._markDirtyAndSave("C:/readonly/scene.sni")
        self.hlev.SetSNIFileName.assert_not_called()


# ---------------------------------------------------------------------------
# _import: two-lock design
# ---------------------------------------------------------------------------

class TestImportLocking(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()
        self.mock_item = MagicMock()
        self.mock_item.project().aspectRatio.return_value = 1.777

    def _existing_scene(self):
        """Configure hlev to look like a scene with loaded footage."""
        self.hlev.SNIFileName.return_value = "C:/existing.sni"

    def _empty_scene(self):
        self.hlev.SNIFileName.return_value = ""
        self.hlev.NumByType.return_value = 0

    def test_two_lock_cycles_for_add_shot_path(self):
        """is_empty check and AddShot each use their own lock — two Lock/Unlock pairs."""
        self._existing_scene()
        self.hlev.AddShot.return_value = MagicMock()
        with patch("os.path.exists", return_value=True):
            self.host._import(["C:/media.mov"], self.mock_item, MagicMock(), [], False)
        self.assertEqual(self.hlev.Lock.call_count, 2)
        self.assertEqual(self.hlev.Unlock.call_count, 2)

    def test_only_one_lock_for_new_shot_path(self):
        """For an empty scene, newShot() runs between the two lock windows —
        Lock is called once (is_empty check), then once more inside newShot()."""
        self._empty_scene()
        with patch("os.path.exists", return_value=True), \
             patch.object(self.host, "newShot", return_value=True) as mock_new:
            self.host._import(["C:/media.mov"], self.mock_item, MagicMock(), [], False)
        mock_new.assert_called_once()
        # Lock called once for is_empty, newShot is mocked so its internal lock
        # is not counted here.
        self.assertEqual(self.hlev.Lock.call_count, 1)
        self.assertEqual(self.hlev.Unlock.call_count, 1)

    def test_add_shot_failure_still_releases_lock(self):
        """Even if AddShot raises, the second lock must be released."""
        self._existing_scene()
        self.hlev.AddShot.side_effect = Exception("SynthEyes crashed")
        with patch("os.path.exists", return_value=True):
            result = self.host._import(["C:/media.mov"], self.mock_item, MagicMock(), [], False)
        self.assertFalse(result)
        self.assertEqual(self.hlev.Lock.call_count, 2)
        self.assertEqual(self.hlev.Unlock.call_count, 2)

    def test_new_shot_called_outside_lock(self):
        """newShot() must be called with no listener lock held — dialog safety."""
        self._empty_scene()
        lock_held_during_new_shot = []

        def fake_new_shot(*args, **kwargs):
            lock_held_during_new_shot.append(self.hlev.Lock.call_count ==
                                              self.hlev.Unlock.call_count)
            return True

        with patch("os.path.exists", return_value=True), \
             patch.object(self.host, "newShot", side_effect=fake_new_shot):
            self.host._import(["C:/media.mov"], self.mock_item, MagicMock(), [], False)

        self.assertTrue(lock_held_during_new_shot[0],
                        "Lock was still held when newShot() was called")


# ---------------------------------------------------------------------------
# newShot: lock scope around NewSceneAndShot
# ---------------------------------------------------------------------------

class TestNewShotLocking(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()

    def test_new_scene_and_shot_called_inside_lock(self):
        """NewSceneAndShot must be atomic w.r.t. the listener."""
        self.hlev.HasChanged.return_value = False
        mock_item = MagicMock()
        mock_item.project.return_value = None

        call_order = []
        self.hlev.Lock.side_effect = lambda: call_order.append("lock")
        self.hlev.Unlock.side_effect = lambda: call_order.append("unlock")
        self.hlev.NewSceneAndShot.side_effect = lambda *a, **k: call_order.append("new") or MagicMock()

        with patch("os.path.exists", return_value=True):
            self.host.newShot("C:/footage.mov", mock_item, MagicMock())

        self.assertEqual(call_order, ["lock", "new", "unlock"])


# ---------------------------------------------------------------------------
# _publish: multi-file detection
# ---------------------------------------------------------------------------

class TestPublishMultiFile(unittest.TestCase):

    def setUp(self):
        self.host, self.hlev = _make_host()

    def test_returns_all_new_files_after_export(self):
        mock_info = MagicMock()
        mock_info.resource = "tracking"
        mock_info.filePath.return_value = "/pub/SH010_tracking.comp"

        before = {"existing.comp"}
        after  = {"existing.comp", "SH010_tracking.comp", "SH010_tracking_cam2.comp"}

        with patch("os.path.dirname", return_value="/pub"), \
             patch("os.makedirs"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", side_effect=[before, after]):
            result = self.host._publish(mock_info, {})

        self.assertEqual(len(result), 2)
        basenames = {os.path.basename(f) for f in result}
        self.assertIn("SH010_tracking.comp", basenames)
        self.assertIn("SH010_tracking_cam2.comp", basenames)

    def test_returns_declared_path_when_no_new_files_detected(self):
        """If the directory diff finds nothing, fall back to the declared export path."""
        mock_info = MagicMock()
        mock_info.resource = "tracking"
        mock_info.filePath.return_value = "/pub/SH010_tracking.comp"

        with patch("os.path.dirname", return_value="/pub"), \
             patch("os.makedirs"), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=set()):
            result = self.host._publish(mock_info, {})

        # normalizePath is applied, so just check the basename
        self.assertEqual(len(result), 1)
        self.assertIn("SH010_tracking.comp", result[0])


# ---------------------------------------------------------------------------
# _log: debug mode toggle
# ---------------------------------------------------------------------------

class TestLogDebugMode(unittest.TestCase):

    def setUp(self):
        self.host, _ = _make_host()

    def test_debug_suppressed_by_default(self):
        with patch("builtins.print") as mock_print, \
             patch("ramses.RAM_SETTINGS") as mock_settings:
            mock_settings.userSettings = {}
            self.host._log("debug message", LogLevel.Debug)
            mock_print.assert_not_called()

    def test_debug_shown_when_flag_set(self):
        with patch("builtins.print") as mock_print, \
             patch("syntheyes_host.RAM_SETTINGS") as mock_settings:
            mock_settings.userSettings = {"debugLog": True}
            self.host._log("debug message", LogLevel.Debug)
            mock_print.assert_called_once()
            self.assertIn("Debug", mock_print.call_args[0][0])

    def test_info_always_shown(self):
        with patch("builtins.print") as mock_print, \
             patch("ramses.RAM_SETTINGS") as mock_settings:
            mock_settings.userSettings = {}
            self.host._log("info message", LogLevel.Info)
            mock_print.assert_called_once()


# ---------------------------------------------------------------------------
# _saveChangesUI: safe default on dialog failure
# ---------------------------------------------------------------------------

class TestSaveChangesUI(unittest.TestCase):

    def setUp(self):
        self.host, _ = _make_host()
        self.host.app = MagicMock()

    def test_returns_cancel_on_pyside_failure(self):
        """If the dialog cannot be shown, we must not silently discard work."""
        with patch.dict("sys.modules", {"PySide2.QtWidgets": None,
                                        "PySide6.QtWidgets": None}):
            result = self.host._saveChangesUI()
        self.assertEqual(result, "cancel")


# ---------------------------------------------------------------------------
# resolvePreviewPath: newest preview file, read-only
# ---------------------------------------------------------------------------

class TestResolvePreviewPath(unittest.TestCase):

    def setUp(self):
        self.host, _ = _make_host()

    def test_empty_when_no_folder(self):
        with patch.object(self.host, "previewPath", return_value=""):
            self.assertEqual(self.host.resolvePreviewPath(), "")

    def test_empty_when_folder_missing(self):
        with patch.object(self.host, "previewPath",
                          return_value="/no/such/preview/folder"):
            self.assertEqual(self.host.resolvePreviewPath(), "")

    def test_returns_newest_media_file(self):
        import tempfile
        folder = tempfile.mkdtemp()
        old = os.path.join(folder, "shot_preview.0001.jpg")
        new = os.path.join(folder, "shot_preview.0002.jpg")
        # A non-media sidecar that must be ignored even though it may be newest.
        sidecar = os.path.join(folder, "_ramses_data.json")
        for p in (old, new, sidecar):
            with open(p, "w") as f:
                f.write("x")
        # Force deterministic mtimes: old < new, sidecar newest of all.
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        os.utime(sidecar, (3000, 3000))
        with patch.object(self.host, "previewPath", return_value=folder):
            result = self.host.resolvePreviewPath()
        # Newest *media* file wins; the newer sidecar is filtered out.
        self.assertEqual(os.path.normpath(result), os.path.normpath(new))

    def test_empty_when_only_sidecars(self):
        import tempfile
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "_ramses_data.json"), "w") as f:
            f.write("{}")
        with patch.object(self.host, "previewPath", return_value=folder):
            self.assertEqual(self.host.resolvePreviewPath(), "")


if __name__ == "__main__":
    unittest.main()
