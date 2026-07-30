"""Startup console hiding: the decision logic in Ramses-Syntheyes.py.

SynthEyes launches the plugin with python.exe, which allocates a console the
artist had to minimise on every start. It is hidden unless debugLog is on.

Only the decision is tested here; _set_console_visible() is a thin ctypes
wrapper around GetConsoleWindow/ShowWindow with nothing to assert in-process.

The entry script has a hyphen in its name so it cannot be imported normally;
it is loaded by path. Its module level is only sys.path setup and it guards
the actual startup behind `if __name__ == "__main__"`, so importing is safe.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.modules.setdefault("PySide2", None)   # unused here; keeps import cheap

_ENTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Ramses-Syntheyes.py",
)


def _load_entry():
    spec = importlib.util.spec_from_file_location("ramses_syntheyes_entry", _ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = _load_entry()


class TestShouldHideConsole(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write(self, payload):
        with open(self.path, "w", encoding="utf8") as f:
            if isinstance(payload, str):
                f.write(payload)
            else:
                json.dump(payload, f)

    def test_hidden_when_debug_logging_is_off(self):
        self._write({"userSettings": {"debugLog": False}})
        self.assertTrue(entry._should_hide_console(self.path))

    def test_visible_when_debug_logging_is_on(self):
        """The console is the only place the debug output goes."""
        self._write({"userSettings": {"debugLog": True}})
        self.assertFalse(entry._should_hide_console(self.path))

    def test_hidden_on_a_first_run_with_no_settings_file(self):
        os.remove(self.path)
        self.assertTrue(entry._should_hide_console(self.path))

    def test_hidden_when_the_key_is_absent(self):
        """An absent debugLog means off, same as a fresh install."""
        self._write({"userSettings": {"plateStepNames": ["Plate"]}})
        self.assertTrue(entry._should_hide_console(self.path))

    def test_hidden_when_the_user_settings_block_is_absent(self):
        """Also not corruption — just settings the plugin has never written."""
        self._write({"somethingElse": 1})
        self.assertTrue(entry._should_hide_console(self.path))

    def test_visible_when_the_settings_file_is_corrupt(self):
        """Fails open: a broken settings file is when output matters most."""
        self._write("{not json at all")
        self.assertFalse(entry._should_hide_console(self.path))

    def test_visible_when_the_json_is_not_an_object(self):
        self._write([1, 2, 3])
        self.assertFalse(entry._should_hide_console(self.path))

    def test_visible_when_user_settings_is_not_an_object(self):
        self._write({"userSettings": "nonsense"})
        self.assertFalse(entry._should_hide_console(self.path))

    def test_no_settings_path_at_all_leaves_the_console_alone(self):
        """Unsupported OS: _addon_settings_path() returns ""."""
        self.assertFalse(entry._should_hide_console(""))


class TestSetConsoleVisible(unittest.TestCase):

    def test_never_raises(self):
        """Cosmetic only — it must never stop the plugin from starting."""
        entry._set_console_visible(False)
        entry._set_console_visible(True)


if __name__ == "__main__":
    unittest.main()
