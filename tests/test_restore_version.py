# -*- coding: utf-8 -*-
"""Restoring an earlier version, and what the next save does with the copy.

`restoreVersion()` does not replace the working file. It copies the chosen
version up beside it as `<name>_+restored-vN+.sni` and opens that, and nothing
here ever deleted the copy again.

Third host to get this fix, after Ramses-Fusion and Ramses-Blender. It comes
from the shared SDK rather than from any one DCC, so the shape is the same; the
part worth knowing is that SynthEyes reports the *newest* version rather than
Fusion's obvious -1, because it uses the stock version lookup with no patch of
its own. A plausible wrong number is harder to notice than a broken one.

Real temp folders throughout: the behaviour under test is file naming and
placement, and mocking RamFileManager would mean asserting nothing.
"""

import filecmp
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# --- Environment mocks (must precede project imports) ---
sys.modules.setdefault("PySide2", MagicMock())
sys.modules.setdefault("PySide2.QtWidgets", MagicMock())
sys.modules.setdefault("PySide2.QtCore", MagicMock())

current_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(os.path.dirname(current_dir), "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

import ramses  # noqa: E402
import ramses.ramses  # noqa: E402

from ramses import RamFileInfo, RamHost, RamState  # noqa: E402

import syntheyes_host  # noqa: E402

BASE_NAME = "TEST_S_SH010_TRACK"


class RestoreOnDiskTestCase(unittest.TestCase):
    """A working file with three versions behind it, in a real temp folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ramses_se_restore_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.wip = os.path.join(self.tmp, "SHOT")
        self.versions = os.path.join(self.wip, "_versions")
        os.makedirs(self.versions)

        self.work = self.norm(os.path.join(self.wip, BASE_NAME + ".sni"))
        self.write(self.work, "v3-content")
        for v in (1, 2, 3):
            self.write(self.version_path(v), "v%d-content" % v)

        self._pin_the_naming_environment()

        self.scene_content = "v3-content"
        self.save_succeeds = True
        self._path = self.work

        self.hlev = MagicMock()
        self.hlev.SNIFileName.side_effect = lambda: self._path
        self.hlev.OpenSNI.side_effect = self._open_sni
        self.host = syntheyes_host.SynthEyesHost(self.hlev)

        self.patch(syntheyes_host.SynthEyesHost, "_markDirtyAndSave", self._write_scene)
        self.patch(syntheyes_host.SynthEyesHost, "_ensure_connected", return_value=True)
        self.patch(syntheyes_host.SynthEyesHost, "setupCurrentFile", return_value=True)
        self.patch(syntheyes_host.SynthEyesHost, "_store_ramses_metadata")
        # Identity comes from the daemon and is irrelevant here: every path
        # under test is derived from file names.
        for name in ("currentItem", "currentStep", "currentStatus"):
            self.patch(syntheyes_host.SynthEyesHost, name, return_value=None)

    # --- fake SynthEyes -----------------------------------------------------

    def _write_scene(self, filePath):
        """Stands in for _markDirtyAndSave: writes the scene and moves onto it."""
        if not self.save_succeeds:
            raise RuntimeError("SynthEyes refused to save")
        filePath = self.norm(filePath)
        os.makedirs(os.path.dirname(filePath), exist_ok=True)
        self.write(filePath, self.scene_content)
        self._path = filePath

    def _open_sni(self, filePath):
        filePath = self.norm(filePath)
        self.scene_content = self.read(filePath)
        self._path = filePath

    # --- helpers ------------------------------------------------------------

    def patch(self, target, attribute, *args, **kwargs):
        if args:
            p = patch.object(target, attribute, args[0], **kwargs)
        else:
            p = patch.object(target, attribute, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def _pin_the_naming_environment(self):
        """Makes `wip002` parse as a version, here and on any machine.

        RamFileInfo builds its filename regex from the state short names the
        *daemon* is offering, and caches it on the class for the process. With
        a Ramses client running the real states come back and "wip002" parses
        as version 2; without one only 'v' and 'pub' are known and the same
        name parses as a resource called "wip002" carrying no version at all.
        Same file, different meaning, no error either way.
        """
        wip = RamState(uuid="", data={"shortName": "wip", "name": "Work in progress"})
        self.patch(ramses.ramses.Ramses, "states", return_value=[wip])
        self.patch(RamFileInfo, "_RamFileInfo__nameRe", new=None)

    @staticmethod
    def norm(path):
        return os.path.abspath(path).replace("\\", "/")

    def write(self, path, content):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def read(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def version_path(self, version):
        return self.norm(
            os.path.join(self.versions, "%s_wip%03d.sni" % (BASE_NAME, version))
        )

    def restored_copy_path(self, version=2):
        return self.norm(
            os.path.join(self.wip, "%s_+restored-v%d+.sni" % (BASE_NAME, version))
        )

    def restore(self, version=2):
        self.host._restoreVersionUI = MagicMock(return_value=self.version_path(version))
        return self.host.restoreVersion()


class TestRestoreLeavesTheWorkingFileAlone(RestoreOnDiskTestCase):

    def test_restore_opens_a_marked_copy_beside_the_working_file(self):
        self.assertTrue(self.restore(2))

        copy_path = self.restored_copy_path(2)
        self.assertTrue(os.path.isfile(copy_path), "the restored copy is written")
        self.assertEqual("v2-content", self.read(copy_path))
        self.assertEqual(copy_path, self.host.currentFilePath())

    def test_restore_does_not_touch_the_working_file_or_the_versions(self):
        self.restore(2)

        self.assertEqual(
            "v3-content",
            self.read(self.work),
            "the working file keeps the newer work until the artist saves",
        )
        for v in (1, 2, 3):
            self.assertTrue(os.path.isfile(self.version_path(v)))


class TestTheRestoredVersionIsReported(RestoreOnDiskTestCase):
    """The comment dialog offered the newest version, not the one on screen."""

    def test_the_base_implementation_answers_with_the_newest_version(self):
        # The control for the override below, and the whole reason it exists.
        # RamHost.currentVersion() matches _versions entries against the current
        # file's name; a restored copy strips back to the plain name, so it
        # matches the newest version file and answers 3. If this ever stops
        # being wrong the override has become dead code and should go.
        self.restore(2)
        self.assertEqual(3, RamHost.currentVersion(self.host))

    def test_currentVersion_reports_the_restored_version(self):
        self.restore(2)
        self.assertEqual(2, self.host.currentVersion())
        self.assertEqual(2, self.host.currentRestoredVersion())

    def test_currentRestoredVersion_is_minus_one_for_an_ordinary_file(self):
        self.assertEqual(-1, self.host.currentRestoredVersion())
        self.assertEqual(3, self.host.currentVersion())

    def test_currentRestoredVersion_is_minus_one_for_an_unsaved_scene(self):
        self._path = ""
        self.assertEqual(-1, self.host.currentRestoredVersion())


class TestSavingARestoredScene(RestoreOnDiskTestCase):

    def test_the_save_lands_back_on_the_working_file(self):
        self.restore(2)
        self.assertTrue(self.host.save(setupFile=False))

        self.assertEqual(self.work, self.host.currentFilePath())
        self.assertEqual("v2-content", self.read(self.work))

    def test_the_version_is_incremented_rather_than_overwritten(self):
        self.restore(2)
        self.host.save(setupFile=False)

        self.assertTrue(
            os.path.isfile(self.version_path(4)),
            "restoring must not overwrite v3 with older content",
        )
        self.assertEqual("v2-content", self.read(self.version_path(4)))
        self.assertEqual("v3-content", self.read(self.version_path(3)))
        self.assertEqual(4, self.host.currentVersion())

    def test_the_restored_copy_is_removed(self):
        self.restore(2)
        self.assertTrue(os.path.isfile(self.restored_copy_path(2)))

        self.host.save(setupFile=False)

        self.assertFalse(
            os.path.isfile(self.restored_copy_path(2)),
            "the copy duplicates _versions/...wip002 once its content is saved",
        )

    def test_the_deleted_copy_is_dropped_from_the_metadata_sidecar(self):
        self.restore(2)
        sidecar = os.path.join(self.wip, "_ramses_data.json")
        with open(sidecar, "r", encoding="utf-8") as f:
            self.assertIn(os.path.basename(self.restored_copy_path(2)), json.load(f))

        self.host.save(setupFile=False)

        with open(sidecar, "r", encoding="utf-8") as f:
            self.assertNotIn(
                os.path.basename(self.restored_copy_path(2)), json.load(f)
            )

    def test_a_second_save_behaves_normally(self):
        self.restore(2)
        self.host.save(setupFile=False)
        self.scene_content = "more-work"

        self.host.save(setupFile=False)

        self.assertEqual(
            4,
            self.host.currentVersion(),
            "only the first save after a restore is forced to increment",
        )
        self.assertEqual("more-work", self.read(self.version_path(4)))

    def test_it_also_works_when_the_state_is_named(self):
        self.restore(2)
        state = RamState(uuid="", data={"shortName": "wip", "name": "wip"})

        self.assertTrue(self.host.save(setupFile=False, state=state))

        self.assertFalse(os.path.isfile(self.restored_copy_path(2)))
        self.assertTrue(os.path.isfile(self.version_path(4)))


class TestTheCopyIsKeptWhenDeletingWouldLoseSomething(RestoreOnDiskTestCase):

    def test_a_failed_save_keeps_the_copy(self):
        self.restore(2)
        self.save_succeeds = False

        self.assertFalse(self.host.save(setupFile=False))

        self.assertTrue(
            os.path.isfile(self.restored_copy_path(2)),
            "nothing was written, so the copy is still the only open work",
        )

    def test_a_copy_edited_since_the_restore_is_kept(self):
        # Anything that writes into the copy without going through save():
        # SynthEyes' own Save, a script calling hlev.SaveSNI(), an autosave.
        # After that the copy holds work that exists nowhere else.
        self.restore(2)
        self.write(self.restored_copy_path(2), "v2-content-plus-an-hour-of-work")

        self.host.save(setupFile=False)

        self.assertTrue(
            os.path.isfile(self.restored_copy_path(2)),
            "it no longer matches version 2, so it is not a duplicate",
        )

    def test_the_copy_survives_if_its_source_version_is_gone(self):
        self.restore(2)
        os.remove(self.version_path(2))

        self.host.save(setupFile=False)

        self.assertTrue(
            os.path.isfile(self.restored_copy_path(2)),
            "with _versions/...wip002 gone the copy is no longer a duplicate",
        )

    def test_a_save_that_lands_elsewhere_keeps_the_copy(self):
        # Save As moves the work to another item; the copy still belongs to
        # this one and nothing here supersedes it.
        self.restore(2)
        elsewhere = os.path.join(self.tmp, "OTHER")
        os.makedirs(elsewhere)
        self._path = self.norm(os.path.join(elsewhere, "TEST_S_SH020_TRACK.sni"))

        self.host._discardRestoredCopy(self.restored_copy_path(2))

        self.assertTrue(os.path.isfile(self.restored_copy_path(2)))

    def test_an_ordinary_save_deletes_nothing(self):
        self.host.save(setupFile=False)

        self.assertTrue(os.path.isfile(self.work))
        self.assertEqual(
            sorted(f for f in os.listdir(self.versions) if f.endswith(".sni")),
            ["%s_wip%03d.sni" % (BASE_NAME, v) for v in (1, 2, 3)],
        )


class TestAbandoningARestoredCopy(RestoreOnDiskTestCase):
    """Restore, then move on without saving: the copy must not be left behind."""

    def other_file(self):
        path = self.norm(os.path.join(self.wip, "TEST_S_SH010_ANIM.sni"))
        self.write(path, "other-content")
        return path

    def test_opening_another_file_removes_the_copy(self):
        self.restore(2)
        other = self.other_file()

        self.assertTrue(self.host._open(other, None, None))

        self.assertFalse(os.path.isfile(self.restored_copy_path(2)))
        self.assertEqual(other, self.host.currentFilePath())

    def test_restoring_twice_leaves_only_the_second_copy(self):
        self.restore(2)
        self.restore(1)

        self.assertFalse(os.path.isfile(self.restored_copy_path(2)))
        self.assertTrue(os.path.isfile(self.restored_copy_path(1)))

    def test_an_edited_copy_survives_being_abandoned(self):
        self.restore(2)
        self.write(self.restored_copy_path(2), "work-syntheyes-saved-on-its-own")

        self.host._open(self.other_file(), None, None)

        self.assertTrue(
            os.path.isfile(self.restored_copy_path(2)),
            "this is the only copy of that work",
        )

    def test_a_failed_open_keeps_the_copy(self):
        self.restore(2)

        self.assertFalse(
            self.host._open(os.path.join(self.wip, "nope.sni"), None, None)
        )

        self.assertTrue(os.path.isfile(self.restored_copy_path(2)))

    def test_an_open_that_raises_keeps_the_copy(self):
        self.restore(2)
        other = self.other_file()
        self.hlev.OpenSNI.side_effect = RuntimeError("SynthEyes refused")

        self.assertFalse(self.host._open(other, None, None))

        self.assertTrue(os.path.isfile(self.restored_copy_path(2)))

    def test_opening_from_an_ordinary_file_deletes_nothing(self):
        self.host._open(self.other_file(), None, None)

        self.assertTrue(os.path.isfile(self.work))

    def test_reopening_the_same_restored_copy_keeps_it(self):
        self.restore(2)
        copy_path = self.restored_copy_path(2)

        self.assertTrue(self.host._open(copy_path, None, None))

        self.assertTrue(os.path.isfile(copy_path), "it is still the open file")


class TestTheRedundancyCheckItself(RestoreOnDiskTestCase):
    """The single safety criterion, tested directly."""

    def test_it_names_the_version_a_copy_still_duplicates(self):
        self.restore(2)

        source = self.host._restoredCopySource(self.restored_copy_path(2))

        self.assertTrue(source)
        self.assertTrue(
            filecmp.cmp(source, self.version_path(2), shallow=False),
            "the source it names must be the version it was restored from",
        )

    def test_an_ordinary_file_is_never_a_redundant_copy(self):
        self.assertEqual("", self.host._restoredCopySource(self.work))

    def test_a_path_that_does_not_exist_is_not_a_redundant_copy(self):
        self.assertEqual(
            "", self.host._restoredCopySource(os.path.join(self.wip, "gone.sni"))
        )


if __name__ == "__main__":
    unittest.main()
