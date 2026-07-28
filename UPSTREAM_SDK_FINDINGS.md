# Vendored ramses-py code — local patches and known defects

`lib/ramses/` and `lib/ramses_ui_pyside/` are vendored from
[Ramses-Py](https://codeberg.org/Ramses/Ramses-Py). This file records where
this repo deliberately diverges, so a future sync from upstream does not
silently revert a fix.

Two different conventions apply, and they are not interchangeable:

- **`lib/ramses/` (the SDK) is never edited.** Defects are corrected at
  runtime in `lib/ramses_patches.py`, so the tree stays byte-identical to
  upstream and can be replaced wholesale.
- **`lib/ramses_ui_pyside/` (the Qt dialogs) is edited in place** where
  needed, marked with a `LOCAL PATCH (not upstream)` comment. Ramses-Fusion
  does the same with its copy (see its commit `43bdd6a`). Runtime-patching
  these is impractical: the methods worth changing are name-mangled private
  ones, so a patch would have to assign to
  `Class._Class__method` and carry a full copy of the body.

**Neither convention protects itself.** A direct edit is reverted by a sync;
a runtime patch silently no-ops if upstream renames its target. The thing
that actually catches both is a test asserting the *behaviour*. Every entry
below names the test that guards it.

---

## Local patches in `lib/ramses_ui_pyside/`

### 1. `import_dialog.py` — image sequences are collapsed to one row

**Guarded by:** `tests/test_import_dialog.py`
(6 tests; verified by mutation that reverting the patch fails 4 of them)

`__update_published_files()` added one `QListWidgetItem` per file returned by
`RamFileManager.getRamsesFiles()`. A published image sequence is one file per
frame, so importing a plate showed hundreds of near-identical rows — one per
frame — instead of one row for the sequence.

The patch groups files that share a resource, stem and extension but differ
only in a trailing frame token, into a single row labelled
`Main ([####].exr) - 43 frames`. The row carries the **lowest** frame as its
path.

This is presentation only. `SynthEyesHost._pick_footage_file()` already did
`sorted(candidates)[0]`, and both SynthEyes and Fusion expand a whole sequence
from any single frame, so the import result is unchanged.

**Why the detection uses the file name and not `RamFileInfo`:** it cannot use
`RamFileInfo`. The Ramses naming regex ends in `(?:\.([a-z0-9.]+))?$` and that
character class deliberately allows dots, so `PLATE.0001.exr` parses with
`extension == "0001.exr"` and `resource == ""`. That is by design — Ramses
filenames have no frame component, so everything after the first dot is the
extension.

**Why this is not upstreamed:** the behaviour above is intentional, and other
consumers may rely on `nm.extension` being the full dotted tail. Changing
`RamFileInfo`, or changing how this dialog filters on extension, would be a
behavioural change for every Ramses tool. The patch is therefore scoped to
presentation and kept local.

---

## Known upstream defects, NOT patched here

### `import_dialog.py` — the extension filter excludes image sequences

Same root cause as above: `nm.extension` for a sequence frame is
`"01599116.exr"`, so

```python
if len(self.__openExtensions) > 0 and not nm.extension in self.__openExtensions:
    continue
```

drops every frame when a caller passes `openExtensions=["exr", ...]`.

Not an issue in this repo — `SynthEyesHost._importUI()` constructs
`RamImportDialog()` with no extension arguments, so the filter is inactive.
It would bite any caller that passes them (Ramses-Fusion's `_importUI` does,
though Fusion reaches its own UIManager browser instead and never shows this
dialog). Left alone deliberately: fixing it changes filtering semantics for
all consumers, which is an upstream decision rather than a local one.

---

## Runtime patches in `lib/ramses_patches.py`

See that module's docstring for the full list. Summary: the metadata sidecar
is never pruned on read and is never rewritten from an unreadable merge base,
`getValue`/`setValue` tolerate a falsy path, and `RamDaemonInterface.online()`
returns False instead of raising. `apply()` is idempotent (guarded by a
`_ramses_patched` sentinel on each patched class).
