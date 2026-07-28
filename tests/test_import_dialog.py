"""RamImportDialog: image-sequence grouping in the published-files list.

This guards a LOCAL PATCH to a vendored file
(`lib/ramses_ui_pyside/import_dialog.py`, vendored from Ramses-Py). A sync
from upstream would silently revert it and the dialog would go back to
listing one row per frame; these tests turn that into an immediate failure.
See UPSTREAM_SDK_FINDINGS.md.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# --- Environment setup (must precede project imports) ---
# Unlike the other test modules, this one needs the dialog to be a REAL class
# so its method can be called: with PySide2 mocked out the widget base classes
# are MagicMocks and RamImportDialog is not a class at all. Blocking PySide2
# (None in sys.modules raises ImportError) makes import_dialog fall through to
# its PySide6 branch and build a genuine QDialog subclass.
sys.modules["PySide2"] = None

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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

from PySide6 import QtWidgets as _qw

# One QApplication for the process; QListWidgetItem needs it to exist.
_app = _qw.QApplication.instance() or _qw.QApplication([])

import ramses_ui_pyside.import_dialog as import_dialog


class TestImportDialogSequenceGrouping(unittest.TestCase):
    """A published sequence is one file per frame; the list must show one row."""

    FOLDER = "X:/proj/05-SHOTS/0515/PLATE/_published/001"

    def _rows_for(self, fileNames):
        """Runs the real __update_published_files and returns [(label, path)]."""
        files = [self.FOLDER + "/" + n for n in fileNames]

        # Build the dialog without running its (heavy) __init__: this test is
        # about one method, not the whole widget tree.
        dlg = import_dialog.RamImportDialog.__new__(import_dialog.RamImportDialog)
        dlg.versionList = MagicMock()
        dlg.versionBox = MagicMock(currentData=lambda: self.FOLDER)
        dlg._RamImportDialog__openExtensions = ()
        dlg._RamImportDialog__hideExtensions = ()

        created = []

        def _makeItem(label):
            item = MagicMock()
            item.label = label
            item.path = None

            def _setData(_role, value):
                item.path = value

            item.setData.side_effect = _setData
            created.append(item)
            return item

        with patch.object(import_dialog.qw, "QListWidgetItem", side_effect=_makeItem), \
             patch.object(import_dialog.RamFileManager, "getRamsesFiles",
                          return_value=files):
            dlg._RamImportDialog__update_published_files()

        return [(i.label, os.path.basename(i.path)) for i in created]

    def test_sequence_collapses_to_one_row(self):
        rows = self._rows_for([
            "DNX_S_0515_PLATE.01599116.exr",
            "DNX_S_0515_PLATE.01599117.exr",
            "DNX_S_0515_PLATE.01599118.exr",
        ])
        self.assertEqual(len(rows), 1, "a sequence must produce a single row")
        label, path = rows[0]
        self.assertIn("[####].exr", label)
        self.assertIn("3 frames", label)
        self.assertEqual(path, "DNX_S_0515_PLATE.01599116.exr")

    def test_row_carries_the_lowest_frame_regardless_of_listing_order(self):
        """The hosts open the sequence from this path, so it must be frame one."""
        rows = self._rows_for([
            "DNX_S_0515_PLATE.01599118.exr",
            "DNX_S_0515_PLATE.01599116.exr",
            "DNX_S_0515_PLATE.01599117.exr",
        ])
        self.assertEqual(rows[0][1], "DNX_S_0515_PLATE.01599116.exr")

    def test_resources_are_not_merged_into_one_sequence(self):
        """A resource sequence is a different deliverable from the main one."""
        rows = self._rows_for([
            "DNX_S_0515_PLATE.0001.exr",
            "DNX_S_0515_PLATE.0002.exr",
            "DNX_S_0515_PLATE_overlay.0001.exr",
            "DNX_S_0515_PLATE_overlay.0002.exr",
        ])
        self.assertEqual(len(rows), 2)
        labels = [r[0] for r in rows]
        self.assertTrue(any(l.startswith("Main") for l in labels), labels)
        self.assertTrue(any(l.startswith("overlay") for l in labels), labels)

    def test_non_sequence_files_are_untouched(self):
        """Movies and scene files keep one row each, with the old label form."""
        rows = self._rows_for([
            "DNX_S_0515_PLATE.mov",
            "DNX_S_0515_TRACK.sni",
        ])
        self.assertEqual(
            sorted(rows),
            sorted([
                ("Main (mov)", "DNX_S_0515_PLATE.mov"),
                ("Main (sni)", "DNX_S_0515_TRACK.sni"),
            ]),
        )

    def test_mixed_folder(self):
        """The realistic case: a plate sequence plus other deliverables."""
        rows = self._rows_for([
            "DNX_S_0515_PLATE.01599116.exr",
            "DNX_S_0515_PLATE.01599117.exr",
            "DNX_S_0515_PLATE.mov",
            "DNX_S_0515_TRACK.sni",
        ])
        self.assertEqual(len(rows), 3, [r[0] for r in rows])

    def test_version_token_is_not_mistaken_for_a_frame(self):
        """"_v001" must not be read as a frame number and collapsed."""
        rows = self._rows_for([
            "DNX_S_0515_TRACK_v001.sni",
            "DNX_S_0515_TRACK_v002.sni",
        ])
        self.assertEqual(len(rows), 2, [r[0] for r in rows])


if __name__ == "__main__":
    unittest.main()
