# -*- coding: utf-8 -*-
import os
import time
import json
import re
from ramses import (
    RamHost,
    RamItem,
    RamStep,
    RamStatus,
    RamFileInfo,
    RamFileManager,
    LogLevel,
    ItemType,
    RAMSES,
    RAM_SETTINGS,
    RamMetaDataManager,
    RamState,
)

# =============================================================================
# APPLY RUNTIME PATCHES
# =============================================================================
# Fix the vendored SDK's data-loss / crash bugs at runtime (see ramses_patches).
# This module is imported early by the entry script, so patching here installs
# the fixes before any metadata or daemon call is made.
try:
    import ramses_patches
    ramses_patches.apply()
    from ramses_patches import DisableMakedirs
except ImportError:
    print(
        "[Ramses] Warning: ramses_patches module not found. Critical fixes may be missing."
    )
    # Read-only probes below use `with DisableMakedirs():` - degrade to a
    # no-op context manager rather than crashing if the patches are missing.
    from contextlib import nullcontext as DisableMakedirs

# Steps that hold ingested source plates. Same convention (and same user
# setting, "plateStepNames") as Ramses-Fusion.
DEFAULT_PLATE_STEP_NAMES = ("Plate", "Ingest", "Footage")

# File types SynthEyes can open as footage. Published version folders also
# contain sidecars (.ramses_complete, _ramses_data.json) which must never be
# handed to SynthEyes as a plate.
_FOOTAGE_EXTENSIONS = {
    ".exr", ".dpx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga",
    ".cin", ".sgi", ".jp2", ".iff", ".pic", ".bmp",
    ".mov", ".mp4", ".mxf", ".avi",
}

class SynthEyesHost(RamHost):
    """
    Ramses Host implementation for Boris FX SynthEyes.
    """

    # Identity Properties
    name = "Ramses-Syntheyes"
    vendorName = "Overmind Studios"
    version = "1.0.0"
    helpURL = "https://www.overmind-studios.de"

    def __init__(self, hlev_obj: object) -> None:
        super().__init__()
        self.hlev = hlev_obj
        self.hostName = "SynthEyes"
        try:
            self.hostVersion = str(self.hlev.Version())
        except Exception:
            self.hostVersion = "Unknown"

    @staticmethod
    def _exec_dialog(dlg) -> int:
        """PySide2/PySide6-compatible dialog exec.

        PySide6 removed exec_(); PySide2 has both but exec_() is idiomatic.
        getattr(dlg, 'exec', None) is always truthy (QDialog always has exec),
        so the old pattern never fell through to exec_(). This picks exec_()
        when available and falls back to exec() for PySide6.
        """
        fn = getattr(dlg, 'exec_', None) or getattr(dlg, 'exec', None)
        return fn() if fn else 0

    def normalizePath(self, path: object) -> str:
        """Centralized path normalization for SynthEyes (forward slashes)."""
        if not path:
            return ""
        path_str = str(path)
        abs_path = os.path.abspath(path_str)
        return abs_path.replace("\\", "/")

    @staticmethod
    def _pick_footage_file(paths) -> str:
        """Picks the footage file to open from a published-version listing.

        ``latestPublishedVersionFilePaths()`` returns every file in the
        version folder, including sidecars written by Ramses-Ingest
        (.ramses_complete, _ramses_data.json) which sort first on disk.
        Filters to footage types and returns the lowest frame (published
        frames are zero-padded, so lexical order is frame order).

        Returns:
            str: A footage file path, or "" if the listing has none.
        """
        candidates = [
            str(p) for p in (paths or [])
            if os.path.splitext(str(p))[1].lower() in _FOOTAGE_EXTENSIONS
        ]
        if not candidates:
            return ""
        return sorted(candidates)[0]

    @staticmethod
    def findStepByShortName(project, *short_names):
        """Case-insensitive step lookup by short name.

        The upstream ``project.step()`` compares short names exactly, so a
        step named "MAMO" is invisible to a lookup for "MaMo". Matches any
        of *short_names* against the project's steps, ignoring case.
        """
        if not project:
            return None
        wanted = {str(n).lower() for n in short_names}
        try:
            for s in project.steps():
                if str(s.shortName()).lower() in wanted:
                    return s
        except Exception:
            return None
        return None

    def _find_plate_path(self, project, item) -> str:
        """Finds the first frame of the latest published plate for a shot.

        Plate steps are matched **case-insensitively** against the
        ``plateStepNames`` user setting (default: Plate, Ingest, Footage),
        so a step named "PLATE" or "plates" is found just as well.
        """
        names = {
            str(n).lower()
            for n in RAM_SETTINGS.userSettings.get(
                "plateStepNames", DEFAULT_PLATE_STEP_NAMES
            )
        }
        try:
            steps = project.steps()
        except Exception:
            return ""
        for p_step in steps:
            try:
                if str(p_step.shortName()).lower() not in names:
                    continue
                # Read-only probe: without DisableMakedirs this creates a
                # _published folder in every plate step just to look for the
                # latest plate (SDK path getters mkdir on read).
                with DisableMakedirs():
                    plate = self._pick_footage_file(
                        item.latestPublishedVersionFilePaths(step=p_step)
                    )
                if plate:
                    return plate
            except Exception:
                continue
        return ""

    def _ensure_connected(self) -> bool:
        """Verifies the SyPy listener is alive; attempts one reconnect if not.

        Returns True if the connection is (or becomes) usable.  Call this at
        the top of any action that would hard-fail on a dead connection.
        """
        try:
            self.hlev.Version()
            return True
        except Exception:
            pass
        try:
            if self.hlev.OpenExisting():
                self._log("Reconnected to SynthEyes listener.", LogLevel.Info)
                return True
        except Exception:
            pass
        self._log(
            "Lost connection to SynthEyes listener. Please restart the plugin.",
            LogLevel.Critical,
        )
        return False

    def currentFilePath(self) -> str:
        """Gets the file path of the current .sni file."""
        if not self.hlev:
            return ""
        try:
            path = self.hlev.SNIFileName()
            return self.normalizePath(path)
        except Exception:
            return ""

    def resolvePreviewPath(self) -> str:
        """Returns the most recent preview file for the current shot, or "".

        Used by the panel's 'Open Preview' button. Reads the shot's preview
        folder (read-only — no mkdir) and returns the newest media file in it,
        which is the preview the artist most recently rendered.
        """
        with DisableMakedirs():
            folder = self.previewPath()
        if not folder or not os.path.isdir(folder):
            return ""
        candidates = [
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in _FOOTAGE_EXTENSIONS
            and os.path.isfile(os.path.join(folder, f))
        ]
        if not candidates:
            return ""
        return self.normalizePath(max(candidates, key=os.path.getmtime))

    def _isDirty(self) -> bool:
        """Checks if the current scene has unsaved changes."""
        if not self.hlev:
            return False
        return bool(self.hlev.HasChanged())

    def _log(self, message: str, level: int) -> None:
        """Logs a message to the console.

        Debug messages are suppressed unless RAM_SETTINGS.userSettings["debugLog"]
        is set to True, allowing field diagnosis without a code deploy.
        """
        if level < LogLevel.Info:
            if not RAM_SETTINGS.userSettings.get("debugLog", False):
                return

        prefix = "Ramses"
        if level == LogLevel.Warning:
            prefix = "Ramses Warning"
        elif level == LogLevel.Critical:
            prefix = "Ramses Error"
        elif level < LogLevel.Info:
            prefix = "Ramses Debug"

        print(f"[{prefix}] {str(message)}")

    def _markDirtyAndSave(self, filePath: str) -> None:
        """Sets SNI filename and saves, guaranteeing the scene is written.

        Uses the 'Save' menu command after setting the filename to ensure
        a standard, silent save without triggering 'SaveIfChanged' dialogs.
        """
        if not self.hlev.HasChanged():
            marked = False
            try:
                shots = self.hlev.Shots()
                if shots:
                    shot = shots[0]
                    self.hlev.Begin()
                    # Use Set() — plain Python attribute assignment bypasses the
                    # C++ binding and does not trigger HasChanged().
                    shot.Set("ramses_saved", str(time.time()))
                    self.hlev.Accept("Ramses: Force Save")
                    marked = True
            except Exception:
                try:
                    self.hlev.Cancel()
                except Exception:
                    pass
            if not marked:
                self._log("Could not mark scene dirty before save (no shots).", LogLevel.Warning)
        
        # Pre-validate the path before handing control to SynthEyes — an invalid
        # directory or unwritable file can cause ClickMainMenuAndWait to block
        # indefinitely waiting for a dialog the user must dismiss.
        target_dir = os.path.dirname(filePath)
        if target_dir and not os.path.isdir(target_dir):
            raise OSError(f"Save target directory does not exist: {target_dir}")
        if os.path.exists(filePath) and not os.access(filePath, os.W_OK):
            raise OSError(f"Save target file is not writable: {filePath}")

        self.hlev.SetSNIFileName(filePath)
        try:
            # Trigger the standard Save menu command.
            # Since the filename is set, it will be silent.
            self.hlev.ClickMainMenuAndWait("Save")
            self._log(f"Successfully saved scene to: {filePath}", LogLevel.Info)
        except Exception as e:
            self._log(f"Error during save: {e}. Falling back to SaveIfChanged.", LogLevel.Warning)
            self.hlev.SaveIfChanged()
        
        # Ensure the viewport remains fresh after the save
        try:
            self.hlev.Redraw()
        except Exception:
            pass

    def _saveAs(
        self,
        filePath: str,
        item: RamItem,
        step: RamStep,
        version: int,
        comment: str,
        incremented: bool,
    ) -> bool:
        """Internal implementation to save the .sni file."""
        if not self.hlev:
            return False

        filePath = self.normalizePath(filePath)

        # Ensure target directory exists
        target_dir = os.path.dirname(filePath)
        if target_dir and not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)

        old_path = self.normalizePath(self.hlev.SNIFileName() or "")
        try:
            # Set the target path then save. _markDirtyAndSave() guarantees
            # SaveIfChanged() actually writes — brand-new scenes may have
            # HasChanged() == False even with loaded footage.
            self._markDirtyAndSave(filePath)

            # Write sidecar metadata AFTER the file exists on disk —
            # RamMetaDataManager requires the file to be present (it prunes
            # entries for missing files on every read).
            if item:
                self._store_ramses_metadata(item, step, filePath)

            return True
        except Exception as e:
            self._log(f"Failed to save: {e}", LogLevel.Critical)
            # Rollback: restore original filename so SynthEyes isn't left
            # pointing at an unwritten path.
            try:
                if old_path:
                    self.hlev.SetSNIFileName(old_path)
            except Exception:
                pass
            return False

    def _open(self, filePath: str, item: RamItem, step: RamStep) -> bool:
        """Internal implementation to open an .sni file."""
        filePath = self.normalizePath(filePath)
        if not os.path.exists(filePath):
            # File doesn't exist yet — this happens when _openUI returned a new
            # scene created via newShot() without a filePath key, causing the
            # base class to compute the pipeline path and call us with it.
            # Only consume the pending flags when the item UUID matches — clearing
            # them unconditionally before the check would lose identity data if
            # _open() is unexpectedly called with a different item.
            pending = getattr(self, "_pending_new_shot_item", None)
            pending_step = getattr(self, "_pending_new_shot_step", None)
            if pending and item and pending.uuid() == item.uuid():
                self._pending_new_shot_item = None
                self._pending_new_shot_step = None
                target_dir = os.path.dirname(filePath)
                if target_dir:
                    os.makedirs(target_dir, exist_ok=True)
                try:
                    self._markDirtyAndSave(filePath)
                    # Write sidecar now that the file is on disk
                    self._store_ramses_metadata(item, step or pending_step, filePath)
                    return True
                except Exception as e:
                    self._log(f"Failed to save new scene to pipeline path: {e}", LogLevel.Critical)
            return False
        try:
            self.hlev.OpenSNI(filePath)
            # Clear any stale pending identity from a previous newShot() call —
            # the newly opened file supplies its own identity via sidecar/notes.
            self._pending_new_shot_item = None
            self._pending_new_shot_step = None
            return True
        except Exception as e:
            self._log(f"Failed to open scene: {e}", LogLevel.Critical)
            return False

    def _setFileName(self, fileName: str) -> bool:
        """Sets the internal file name."""
        if not self.hlev:
            return False
        self.hlev.SetSNIFileName(self.normalizePath(fileName))
        return True

    def collectItemSettings(self, item: RamItem) -> dict:
        """Collects resolution and timing settings for the given item.

        Optimized version using API methods to handle overrides correctly.
        """
        if not item:
            return {}

        # item.project() is authoritative — RAMSES.project() is only the globally
        # active project and would pull the wrong settings in multi-project setups.
        project = item.project() or RAMSES.project()
        if not project:
            return {}

        settings = {
            "width": int(project.width() or 1920),
            "height": int(project.height() or 1080),
            "framerate": float(project.framerate() or 24.0),
            "duration": 0.0,
            "pixelAspectRatio": float(project.pixelAspectRatio() or 1.0),
        }

        if item and item.itemType() == ItemType.SHOT:
            from ramses import RamShot
            shot = item if isinstance(item, RamShot) else RamShot(item.uuid())
            settings["duration"] = float(shot.duration())
            settings["frames"] = shot.frames()

            seq = shot.sequence()
            if seq:
                seq_w = seq.width()
                seq_h = seq.height()
                seq_fps = seq.framerate()
                seq_par = seq.pixelAspectRatio()
                if seq_w:
                    settings["width"] = int(seq_w)
                if seq_h:
                    settings["height"] = int(seq_h)
                if seq_fps:
                    settings["framerate"] = float(seq_fps)
                    # Recompute frames using the sequence-overridden FPS so the
                    # frame count is consistent with the effective framerate.
                    settings["frames"] = int(round(settings["duration"] * float(seq_fps)))
                if seq_par:
                    settings["pixelAspectRatio"] = float(seq_par)

        return settings

    def setupCurrentFile(self, forceUI: bool = True, syncRange: bool = True) -> bool:
        """Applies Ramses settings to the current scene. Returns True on success.

        Args:
            syncRange: Re-align the playback range with the shot. True for the
                explicit "sync scene settings" action; save() passes False so
                that saving never discards a range the artist trimmed by hand.
        """
        if not self._ensure_connected():
            return False
        item = self.currentItem()
        if item:
            settings = self.collectItemSettings(item)
            return self._setupCurrentFile(
                item, self.currentStep(), settings,
                forceUI=forceUI, syncRange=syncRange,
            )
        return False

    def save(
        self,
        incremental: bool = False,
        comment: str = None,
        setupFile: bool = True,
        state: RamState = None,
    ) -> bool:
        """Saves the current file, optionally setting up the scene."""
        if not self._ensure_connected():
            return False
        if setupFile:
            # When saving, do not force disruptive UI switches, and do not
            # touch the playback range — the artist may have trimmed it to
            # work on a section and would lose that on every save.
            self.setupCurrentFile(forceUI=False, syncRange=False)
        else:
            item = self.currentItem()
            if item:
                self._store_ramses_metadata(item, self.currentStep())

        saveFilePath = self.saveFilePath()
        if saveFilePath == "":
            from ramses import Log
            self._log(Log.MalformedName, LogLevel.Critical)
            return self.saveAs()

        state_short = state.shortName() if state else None
        return self._RamHost__save(saveFilePath, incremental, comment, state_short)

    def _syncShotLength(self, shot: object) -> bool:
        """Makes the shot report the plate's real length.

        The Sizzle reference on ``Shot.frameCount``: "Be sure to set this,
        based on actualLength, after opening a new shot." SynthEyes' own
        ``rendercam.szl`` does exactly Flush -> frameCount -> Validate. Without
        it the shot can report a stale length, and the playback range derived
        from it below would inherit that.

        Guarded on inequality because frameCount can invalidate the RAM cache,
        so this must be a no-op once the shot already agrees.

        Returns:
            bool: True if the length is (or was made) correct.
        """
        try:
            actual = int(shot.Get("actualLength") or 0)
        except Exception as e:
            self._log(f"Could not read the shot length: {e}", LogLevel.Debug)
            return False
        if actual <= 0:
            return False
        try:
            if int(shot.Get("frameCount") or 0) == actual:
                return True
        except Exception:
            pass  # unreadable frameCount — fall through and set it

        # BeginShotChanges (not Begin): footage length is exactly the kind of
        # attribute that invalidates the RAM cache.
        self.hlev.BeginShotChanges(shot)
        try:
            shot.Flush()
            shot.Set("frameCount", actual)
            shot.Validate()
            self.hlev.AcceptShotChanges(shot, "Ramses: Sync Shot Length")
            return True
        except Exception as e:
            try: self.hlev.Cancel()
            except Exception: pass
            self._log(f"Failed to sync the shot length: {e}", LogLevel.Warning)
            return False

    def _setPlayRange(self, start: int, end: int) -> bool:
        """Sets the playback range, in an order SynthEyes will accept.

        ``SetAnimStart``/``SetAnimEnd`` write ``Scene.playStart``/``playEnd``,
        and the Sizzle reference requires playStart to stay *below* playEnd at
        every moment ("you will need to change Start or End first, depending on
        the situation"). A fixed order therefore silently drops one of the two
        writes whenever the range moves the wrong way.

        Reads the values back and warns if SynthEyes clamped them, because the
        old code failed at this completely silently.

        Returns:
            bool: True if the range reads back exactly as requested.
        """
        try:
            if start > self.hlev.AnimEnd():
                # Moving the range up: End first, or playStart would land
                # above playEnd and be rejected.
                self.hlev.SetAnimEnd(end)
                self.hlev.SetAnimStart(start)
            else:
                self.hlev.SetAnimStart(start)
                self.hlev.SetAnimEnd(end)

            got_start = self.hlev.AnimStart()
            got_end = self.hlev.AnimEnd()
        except Exception as e:
            self._log(f"Failed to set the playback range: {e}", LogLevel.Warning)
            return False

        if int(got_start) != int(start) or int(got_end) != int(end):
            self._log(
                f"Playback range {start}-{end} was clamped to "
                f"{got_start}-{got_end} by SynthEyes.",
                LogLevel.Warning,
            )
            return False
        return True

    def _syncPlayRange(self, shot: object, setupOptions: dict, forceUI: bool = False) -> bool:
        """Aligns the playback range with the shot's own frame range.

        SynthEyes numbers a shot's frames internally from ``shot.start``, and
        the playback range must stay inside the active shot or it gets clamped.
        This is what SynthEyes' own importers do (``abcimport.szl`` and
        friends)::

            Scene.playStart = shot.start
            Scene.playEnd   = shot.stop

        The Ramses duration is only a database estimate and never defines the
        range; it is a fallback for when the shot cannot answer, and even then
        it counts from the shot's own start rather than from an invented base.

        Note this is deliberately NOT offset by ``shot.frameFirstOffset``: the
        play range is in internal frame numbers. Whether the timebar *displays*
        plate numbers is the separate, per-shot ``matchFrameNumbers`` setting,
        which this plugin leaves alone.

        Returns:
            bool: True if the range was set and reads back as requested.
        """
        start = end = None
        try:
            start = int(shot.Get("start"))
            end = int(shot.Get("stop"))
        except Exception as e:
            self._log(f"Could not read the shot frame range: {e}", LogLevel.Debug)

        if start is None or end is None or end < start:
            # The shot cannot tell us. Fall back to the Ramses duration, but
            # keep the shot's start if we have it. Note actualLength is NOT
            # used here: the reference says it reads 10 for an unreadable
            # shot, so it cannot be distinguished from a real 10-frame plate.
            frames = int(setupOptions.get("frames", 0) or 0)
            if frames <= 0:
                dur = float(setupOptions.get("duration", 0) or 0)
                fps = float(setupOptions.get("framerate", 24.0) or 24.0)
                frames = int(round(dur * fps))
            if frames <= 0:
                self._log(
                    "No frame range available from the shot or from Ramses — "
                    "leaving the playback range alone.",
                    LogLevel.Debug,
                )
                return False
            if start is None:
                start = 0
            end = start + frames - 1
            self._log(
                f"Shot reported no frame range; using the Ramses duration "
                f"({frames} frames) instead.",
                LogLevel.Debug,
            )

        ok = self._setPlayRange(start, end)

        # Jump to the start only if we are forcing UI and the current frame is
        # outside the range we just set.
        if forceUI:
            try:
                current = self.hlev.Frame()
                if current < start or current > end:
                    self.hlev.SetFrame(start)
                    self.hlev.Redraw()
            except Exception:
                pass

        return ok

    def _setupCurrentFile(self, item: RamItem, step: RamStep, setupOptions: dict, shot_obj: object = None, forceUI: bool = False, syncRange: bool = False) -> bool:
        """Sets the current file parameters (resolution, FPS, aspect).

        Args:
            syncRange: Align the playback range with the shot. Only for a
                shot that was just created, imported or explicitly re-synced —
                a plain save must not overwrite a range the artist trimmed.
        """
        if not self.hlev:
            return False
        
        # Use provided shot or fallback to the primary one
        shot = shot_obj
        if not shot:
            shots = self.hlev.Shots()
            if shots:
                shot = shots[0]
        
        if not shot:
            return True

        # Ensure we are in Z-Up mode (standard for Ramses)
        try:
            self.hlev.SetSzlAxisMode(0)
        except Exception:
            pass

        # 1. Shot media attributes — resolution, PAR, FPS (BeginShotChanges because
        #    these can invalidate the RAM cache / PREVALIDATE / POSTVALIDATE chain).
        target_rate = float(setupOptions.get("framerate", 24.0))
        target_w    = int(setupOptions.get("width", 0))
        target_h    = int(setupOptions.get("height", 0))
        target_par  = float(setupOptions.get("pixelAspectRatio", 1.0))

        needs_heavy_refresh = False
        try:
            if abs(float(shot.rate or 0.0) - target_rate) > 0.001:
                needs_heavy_refresh = True
            if target_w and target_h:
                # Use shot.Get() — direct Python attribute access for non-SyPy3-mapped
                # names silently creates a Python-side attr with no C++ effect.
                if (int(shot.Get("width") or 0) != target_w or
                        int(shot.Get("height") or 0) != target_h or
                        abs(float(shot.Get("pixelAspect") or 1.0) - target_par) > 0.001):
                    needs_heavy_refresh = True
        except Exception:
            needs_heavy_refresh = True

        if needs_heavy_refresh:
            self.hlev.BeginShotChanges(shot)
            try:
                shot.rate = target_rate
                if target_w and target_h:
                    # Use Set() for the same reason — raises on unknown keys instead
                    # of silently no-oping via Python __setattr__.
                    shot.Set("width",       target_w)
                    shot.Set("height",      target_h)
                    shot.Set("pixelAspect", target_par)
                self.hlev.PostValidate(shot)
                self.hlev.AcceptShotChanges(shot, "Ramses: Sync Shot Settings")
            except Exception as e:
                try: self.hlev.Cancel()
                except Exception: pass
                self._log(f"Failed to sync shot settings: {e}", LogLevel.Warning)
        
        # 2. UI & Workspace ( Disrupted only if forceUI is True )
        if forceUI:
            try:
                # Force UI into a state where footage is visible
                self.hlev.SetRoom("Camera")
                self.hlev.SetView("Camera")
                image_flag = getattr(self.hlev, "VF_show_image", 512)
                self.hlev.ViewFlags(set=image_flag)
            except Exception:
                pass

            # Camera Activation
            try:
                cam = shot.cam
                if cam:
                    self.hlev.Begin()
                    self.hlev.SetActive(cam)
                    self.hlev.Accept("Ramses: Activate Camera")
            except Exception as e:
                try: self.hlev.Cancel()
                except Exception: pass
                self._log(f"Camera activation skipped: {e}", LogLevel.Debug)

        # 3. Frame range — driven by the PLATE, not by the Ramses duration.
        #
        # Only on syncRange: a scene that is merely being saved must keep the
        # range the artist set. Overwriting it on every save is what made this
        # look like "the range never sticks".
        if syncRange:
            self._syncShotLength(shot)
            self._syncPlayRange(shot, setupOptions, forceUI=forceUI)

        # Clear any temporary SyPy frame overrides
        try:
            self.hlev.ClearSzlFrame()
        except Exception:
            pass

        # 4. Identity Persistence
        self._store_ramses_metadata(item, step)
        return True

    def newShot(self, footagePath: str, item: RamItem, step: RamStep) -> bool:
        """Creates a new scene with the specified footage."""
        if not self.hlev:
            return False

        if self._isDirty():
            doSave = self._saveChangesUI()
            if doSave == "cancel":
                return False
            if doSave == "save":
                if not self.save():
                    return False

        # Get project settings
        project = item.project()
        aspect = 0.0
        if project:
            try:
                aspect = float(project.aspectRatio() or 0.0)
            except Exception:
                aspect = 0.0

        # Create new scene — lock only around the SyPy3 call, not the dialog above.
        self._log(f"Creating new scene with footage: {footagePath}", LogLevel.Info)
        try:
            self.hlev.Lock()
            try:
                res = self.hlev.NewSceneAndShot(self.normalizePath(footagePath), aspect)
            finally:
                self.hlev.Unlock()
            
            if res is not None:
                # Remember which item/step this scene is for so _open() can write
                # the sidecar once the pipeline file path is known and saved.
                self._pending_new_shot_item = item
                self._pending_new_shot_step = step
                
                # Sync scene settings (FPS, frame range) to the new shot
                # We force UI updates here as it's a new scene.
                self._setupCurrentFile(item, step, self.collectItemSettings(item),
                                       shot_obj=res, forceUI=True, syncRange=True)
                
                return True
            else:
                self._log("NewSceneAndShot failed (returned None).", LogLevel.Critical)
        except Exception as e:
            self._log(f"Error calling NewSceneAndShot: {e}", LogLevel.Critical)

        return False

    def _store_ramses_metadata(
        self, item: RamItem, step: RamStep = None, filePath: str = None
    ) -> bool:
        """Stores Ramses identity (item/step UUIDs) in the Ramses sidecar file and SNI notes.

        Uses RamMetaDataManager to write a JSON sidecar (_ramses_data.json) and
        also embeds the same info in the .sni 'notes' for robust self-healing.
        """
        meta = {
            "itemUUID": str(item.uuid()),
            "projectUUID": str(item.project().uuid()) if item.project() else "",
        }
        if step:
            meta["stepUUID"] = str(step.uuid())

        # 1. SNI Internal Storage (Optimal & Self-healing)
        try:
            scene = self.hlev.Scene()
            if scene:
                self.hlev.Begin()
                # SyPy3 allows direct property access
                tag = f"RAMSES_ID:{json.dumps(meta)}"
                current_notes = str(scene.notes or "")
                if "RAMSES_ID:" in current_notes:
                    # Update existing tag
                    new_notes = re.sub(r"RAMSES_ID:\{.*?\}", tag, current_notes)
                    scene.notes = new_notes
                else:
                    # Append new tag
                    scene.notes = current_notes + ("\n" if current_notes else "") + tag
                self.hlev.Accept("Ramses: Update Metadata")
        except Exception as e:
            try: self.hlev.Cancel()
            except Exception: pass
            self._log(f"Failed to embed identity in scene notes: {e}", LogLevel.Warning)

        # 2. Sidecar Storage
        path = filePath or self.currentFilePath()
        if not path or not os.path.isfile(path):
            # File not yet on disk — metadata will be written by _saveAs() or _open() logic.
            return True

        try:
            RamMetaDataManager.setValue(path, "ramses", meta)
            return True
        except Exception as e:
            self._log(f"Failed to store metadata sidecar: {e}", LogLevel.Warning)
            return False

    def _parse_scene_identity(self) -> tuple:
        """Parses RAMSES_ID from scene notes in one pass.

        Returns (RamItem_or_None, RamStep_or_None).  Both currentItem() and
        currentStep() call this so the regex + JSON parse runs once per
        refresh, not twice.
        """
        try:
            scene = self.hlev.Scene()
            notes = str(scene.notes or "")
            match = re.search(r"RAMSES_ID:({.*?})", notes, re.DOTALL)
            if not match:
                return None, None
            meta = json.loads(match.group(1))
            from ramses import RamShot, RamAsset, RamStep
            item = None
            item_uuid = meta.get("itemUUID")
            if item_uuid:
                candidate = RamShot(item_uuid)
                if candidate.shortName() == "Unknown":
                    candidate = RamAsset(item_uuid)
                if candidate.shortName() != "Unknown":
                    item = candidate
            step = None
            step_uuid = meta.get("stepUUID")
            if step_uuid:
                candidate = RamStep(step_uuid)
                if candidate.shortName() != "Unknown":
                    step = candidate
            return item, step
        except Exception:
            return None, None

    def _sidecar_identity(self, path: str) -> tuple:
        """Reads identity from the RamMetaDataManager sidecar. Returns (item, step)."""
        try:
            from ramses import RamShot, RamAsset, RamStep
            meta = RamMetaDataManager.getValue(path, "ramses") or {}
            item = None
            item_uuid = meta.get("itemUUID")
            if item_uuid:
                candidate = RamShot(item_uuid)
                if candidate.shortName() == "Unknown":
                    candidate = RamAsset(item_uuid)
                if candidate.shortName() != "Unknown":
                    item = candidate
            step = None
            step_uuid = meta.get("stepUUID")
            if step_uuid:
                candidate = RamStep(step_uuid)
                if candidate.shortName() != "Unknown":
                    step = candidate
            return item, step
        except Exception as e:
            self._log(f"Failed to recover identity from sidecar: {e}", LogLevel.Warning)
            return None, None

    def currentContext(self) -> tuple:
        """Returns (currentItem, currentStep) with one scene-notes parse and at
        most one sidecar read.

        Use this instead of calling currentItem() + currentStep() separately
        whenever you need both — it halves the scene-notes regex work.
        """
        # 1. Pending identity (unsaved new scenes)
        if not self.currentFilePath():
            pi = getattr(self, "_pending_new_shot_item", None)
            ps = getattr(self, "_pending_new_shot_step", None)
            if pi or ps:
                return pi, ps

        # 2. Scene-embedded identity — single regex + JSON parse for both
        item, step = self._parse_scene_identity()
        if item and step:
            return item, step

        # 3. Base-class file-path lookup (only for whichever is still missing)
        base_item = super().currentItem() if not item else None
        base_step = super().currentStep() if not step else None

        # 4. Sidecar — one read fills any remaining gaps
        sid_item, sid_step = None, None
        path = self.currentFilePath()
        needs_sidecar = (
            (base_item is None or base_item.virtual()) or
            (base_step is None or base_step.shortName() == "Unknown")
        )
        if path and needs_sidecar:
            sid_item, sid_step = self._sidecar_identity(path)

        # Resolve: notes > sidecar > base class (even virtual/unknown as last resort)
        if not item:
            item = (sid_item if (base_item is None or base_item.virtual()) else base_item) or base_item
        if not step:
            step = (sid_step if (base_step is None or base_step.shortName() == "Unknown") else base_step) or base_step

        return item, step

    def currentItem(self) -> RamItem:
        """Gets current item, recovery via scene-embedded metadata or sidecar."""
        if not self.currentFilePath():
            pending = getattr(self, "_pending_new_shot_item", None)
            if pending:
                return pending

        item, _ = self._parse_scene_identity()
        if item:
            return item

        item = super().currentItem()
        if not item or item.virtual():
            path = self.currentFilePath()
            if path:
                item, _ = self._sidecar_identity(path)
                if item:
                    return item
        return item

    def currentStep(self) -> RamStep:
        """Gets current step, recovery via scene-embedded metadata or sidecar."""
        if not self.currentFilePath():
            pending = getattr(self, "_pending_new_shot_step", None)
            if pending:
                return pending

        _, step = self._parse_scene_identity()
        if step:
            return step

        step = super().currentStep()
        if not step or step.shortName() == "Unknown":
            path = self.currentFilePath()
            if path:
                _, step = self._sidecar_identity(path)
                if step:
                    return step
        return step

    def saveFilePath(self) -> str:
        """Gets the path where the current file should be saved, handling pending identity."""
        path = super().saveFilePath()
        if not path:
            # Check if we have a pending identity for this unnamed scene
            item = getattr(self, "_pending_new_shot_item", None)
            step = getattr(self, "_pending_new_shot_step", None)
            if item and step:
                # Compute what the pipeline path WOULD be
                path = item.stepFilePath(step=step)
                if not path:
                    # Fallback: compute path from scratch if no file exists yet
                    step_folder = item.stepFolderPath(step)
                    if step_folder:
                        nm = RamFileInfo()
                        nm.project = item.projectShortName()
                        nm.ramType = item.itemType()
                        nm.shortName = item.shortName()
                        nm.step = step.shortName() if hasattr(step, "shortName") else str(step)
                        nm.extension = "sni"
                        path = os.path.join(step_folder, nm.fileName())
        return self.normalizePath(path) if path else ""

    def saveAsTemplate(self, name: str, step: RamStep = None) -> str:
        """Saves the current scene as a template in the step's templates folder.

        The scene must already be saved on disk: we flush the latest changes to
        the working file and copy it into the templates folder, so the working
        file keeps its own identity (mirrors Ramses-Fusion's template flow).

        Returns the template path on success, or "" on failure.
        """
        if not self._ensure_connected():
            return ""
        step = step or self.currentStep()
        if not step:
            self._log("Cannot save template: no current step.", LogLevel.Warning)
            return ""

        clean = re.sub(r"[^A-Za-z0-9_]", "", name.replace(" ", "_").replace("-", "_"))
        if not clean:
            self._log("Cannot save template: the name is empty after sanitising.", LogLevel.Warning)
            return ""

        src = self.currentFilePath()
        if not src or not os.path.isfile(src):
            self._log("Cannot save template: save the scene first.", LogLevel.Warning)
            return ""

        tpl_folder = step.templatesFolderPath()
        if not tpl_folder:
            self._log("Cannot save template: the step has no templates folder.", LogLevel.Warning)
            return ""

        # For GENERAL-type files RamFileInfo.fileName() omits shortName, so the
        # template name goes in the resource field (which IS included) to keep
        # each template uniquely named: PROJ_G_<step>_<name>.sni
        nm = RamFileInfo()
        nm.project = step.projectShortName()
        nm.ramType = ItemType.GENERAL
        nm.step = step.shortName()
        nm.resource = clean
        nm.extension = "sni"
        target = self.normalizePath(os.path.join(tpl_folder, nm.fileName()))

        try:
            # Flush the current scene, then copy it to the template path.
            self._markDirtyAndSave(src)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            RamFileManager.copy(src, target, separateThread=False)
            self._log(f"Template saved to: {target}", LogLevel.Info)
            return target
        except Exception as e:
            self._log(f"Failed to save template: {e}", LogLevel.Critical)
            return ""

    def _openUI(self, item: RamItem = None, step: RamStep = None) -> dict:
        """Shows the Ramses Open Dialog for opening or creating a scene."""
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.open_dialog import RamOpenDialog
                try:
                    from PySide2 import QtWidgets as qw
                except ImportError:
                    from PySide6 import QtWidgets as qw

                dialog = RamOpenDialog( ["sni"] )

                # --- Pre-set Defaults ---
                project = RAMSES.project()
                if project:
                    mamo_step = self.findStepByShortName(project, "MaMo", "Matchmove")
                    if mamo_step:
                        dialog.setCurrentStep(mamo_step)

                res = self._exec_dialog(dialog)
                    
                if not res:
                    return None

                path = dialog.filePath()
                item = dialog.currentItem()
                step = dialog.currentStep()
                # Re-fetch project from the dialog selection — the user may have
                # picked a shot from a different project than RAMSES.project().
                project = item.project() if item else project

                # 1. Existing File path found
                if path and os.path.exists(path):
                    return {
                        "filePath": path,
                        "item": item,
                        "step": step,
                    }

                # 2. No file on disk, but user selected a shot/step context
                if item and step:
                    res_new = qw.QMessageBox.question(None, "New SynthEyes Scene", 
                        f"No SynthEyes file found for {item.shortName()}.\nDo you want to create a new one from the plate?",
                        qw.QMessageBox.Yes | qw.QMessageBox.No)
                    
                    if res_new == qw.QMessageBox.Yes:
                        plate_path = self._find_plate_path(project, item)

                        if plate_path:
                            # Use existing newShot method to initialize
                            if self.newShot(plate_path, item, step):
                                # Do not return filePath — let RamHost.open() compute
                                # the pipeline path via item.stepFilePath() and save
                                # the new scene there via _saveAs().
                                return {
                                    "item": item,
                                    "step": step,
                                }
                        else:
                            qw.QMessageBox.warning(None, "Plate not found", 
                                "Could not find a plate for this shot in the pipeline.")
                
                return None
            except ImportError:
                pass
        return None

    def _saveAsUI(self) -> dict:
        """Shows the Ramses Save As Dialog."""
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.save_as_dialog import RamSaveAsDialog
                file_types = [{"extension": "sni", "name": "SynthEyes Scene"}]
                dialog = RamSaveAsDialog( file_types )
                dialog.raise_()
                dialog.activateWindow()
                
                project = RAMSES.project()
                if project and dialog:
                    dialog.setShot()
                    mamo_step = self.findStepByShortName(project, "MaMo", "Matchmove")
                    if mamo_step:
                        dialog.setStep(mamo_step)

                res = self._exec_dialog(dialog)
                    
                if res:
                    return {
                        "item": dialog.item(),
                        "step": dialog.step(),
                        "extension": dialog.extension(),
                        "resource": dialog.resource()
                    }
                return None
            except ImportError:
                pass
        return None

    def _statusUI(self, currentStatus: RamStatus = None) -> dict:
        """Status update UI."""
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.status_dialog import RamStatusDialog
                dialog = RamStatusDialog(currentStatus)
                dialog.raise_()
                dialog.activateWindow()
                res = self._exec_dialog(dialog)
                if res:
                    result = {
                        "state": dialog.state(),
                        "comment": dialog.comment(),
                        "completionRatio": dialog.completionRatio(),
                    }
                    if hasattr(dialog, 'publish'):
                        result["publish"] = dialog.publish()
                    if hasattr(dialog, 'savePreview'):
                        result["savePreview"] = dialog.savePreview()
                    if hasattr(dialog, 'showPublishUI'):
                        result["showPublishUI"] = dialog.showPublishUI()
                    return result
                return None
            except ImportError:
                pass
        return None

    # --- Mandatory Ramses API Overrides ---

    def _import(self, filePaths: list, item: RamItem, step: RamStep, importOptions: list, forceShowImportUI: bool) -> bool:
        """Loads published footage into the current SynthEyes scene as a new shot."""
        if not self.hlev or not filePaths:
            return False
        if not self._ensure_connected():
            return False
        
        # The upstream import flow may pass every file of a published version
        # folder (including sidecars) — pick actual footage from the list.
        raw_path = self._pick_footage_file([str(p) for p in filePaths]) or str(filePaths[0])
        if not os.path.exists(raw_path):
            self._log(f"Footage not found: {raw_path}", LogLevel.Critical)
            return False

        footage_path = self.normalizePath(raw_path)
        self._log(f"Importing footage: {footage_path}", LogLevel.Info)
        
        # Get project settings for aspect ratio
        project = item.project()
        aspect = 0.0
        if project:
            try:
                aspect = float(project.aspectRatio() or 0.0)
            except Exception:
                aspect = 0.0

        # 1. Determine if the scene is empty — keep the lock as brief as possible:
        #    only pure SyPy3 state reads, no UI, no file I/O.
        self.hlev.Lock()
        try:
            is_empty = True
            if self.currentFilePath():
                is_empty = False
            else:
                try:
                    num_shots = self.hlev.NumByType("SHOT")
                    if num_shots > 1:
                        is_empty = False
                    elif num_shots == 1:
                        shots = self.hlev.Shots()
                        if shots:
                            media_path = shots[0].Get("filenam") or ""
                            if media_path.strip():
                                is_empty = False
                    if is_empty:
                        if self.hlev.NumByType("TRK") > 0 or self.hlev.NumByType("MESH") > 0:
                            is_empty = False
                except Exception:
                    pass
        finally:
            self.hlev.Unlock()

        # newShot() may show a "save changes?" dialog and do file I/O — must not
        # hold the listener lock across that or SynthEyes cannot process messages.
        if is_empty:
            self._log("Fresh session detected. Initializing primary shot...", LogLevel.Info)
            return self.newShot(footage_path, item, step)

        # 2. Add to existing scene — re-acquire lock for the SyPy3 AddShot call
        self.hlev.Lock()
        try:
            self._log(f"Calling AddShot with aspect: {aspect}", LogLevel.Info)
            res = self.hlev.AddShot(footage_path, aspect)

            if res is not None:
                self._log(f"AddShot successful: {res.Name() if hasattr(res, 'Name') else 'New Shot'}", LogLevel.Info)
                if not self.currentFilePath():
                    self._pending_new_shot_item = item
                    self._pending_new_shot_step = step

                self._setupCurrentFile(item, step, self.collectItemSettings(item),
                                       shot_obj=res, forceUI=True, syncRange=True)

                try:
                    # actualLength, not "frames": SynthEyes shots have no
                    # `frames` attribute, so the old read always logged 0.
                    num_frames = int(res.Get("actualLength") or 0)
                    self._log(f"Import successful. Shot has {num_frames} frames.", LogLevel.Info)
                except Exception:
                    pass

                # NOTE: ReloadAll() sends RELOADALL which clears HasChanged.
                # Use Redraw() only so the dirty flag set by AddShot() survives.
                self.hlev.Redraw()

                if hasattr(self, "app") and self.app:
                    self.app.refresh_context()

                return True
            else:
                self._log("AddShot failed (returned None).", LogLevel.Critical)
        except Exception as e:
            self._log(f"Error calling AddShot: {e}", LogLevel.Critical)
        finally:
            self.hlev.Unlock()

        return False

    def _importUI(self, item: RamItem, step: RamStep) -> dict:
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.import_dialog import RamImportDialog
                dialog = RamImportDialog()
                dialog.raise_()
                dialog.activateWindow()
                res = self._exec_dialog(dialog)
                if res:
                    return {"filePaths": dialog.filePaths(), "item": dialog.currentItem(), "step": dialog.currentStep()}
            except ImportError:
                pass
        return None

    def _preview(self, previewFolderPath: str, previewFileBaseName: str, item: RamItem, step: RamStep) -> list:
        """Renders the tracking overlay sequence to disk via SynthEyes' Save Sequence.

        The output format is determined by the 'previewFormat' publish option
        (file extension).  Image sequences: jpg, png, exr, dpx, tif.
        Movies (platform-dependent): avi, mov, mp4.

        Advanced: 'previewRenderSettings' and 'previewRenderCompression' can
        override the opaque strings SynthEyes uses for channel selection and
        codec.  Leave them empty to use whatever is already configured in the
        scene.  To discover the correct values: configure the render manually
        in SynthEyes, then read back:
            hlev.Shots()[0].Get("renderSettings")
            hlev.Shots()[0].Get("renderCompression")
        """
        if not self.hlev:
            return []
        if not self._ensure_connected():
            return []

        shots = self.hlev.Shots()
        if not shots:
            return []
        shot = shots[0]

        # Read publish options (step settings merged with defaults)
        step_opts = {}
        if step:
            try:
                step_opts = step.publishSettings('yaml') or {}
                if not isinstance(step_opts, dict):
                    step_opts = {}
            except Exception:
                step_opts = {}
        options = self._publishOptions(step_opts, False)

        ext = options.get("previewFormat", "jpg").lstrip(".")
        render_settings_override = options.get("previewRenderSettings", "")
        render_compression_override = options.get("previewRenderCompression", "")

        # Build the output path — SynthEyes appends frame numbers for sequences
        os.makedirs(previewFolderPath, exist_ok=True)
        render_file = self.normalizePath(
            os.path.join(previewFolderPath, f"{previewFileBaseName}.{ext}")
        )

        # Capture current shot render settings so we can restore them afterward
        self._log("Preview: reading current render settings...", LogLevel.Info)
        try:
            old_render_file = shot.Get("renderFile") or ""
            old_render_settings = shot.Get("renderSettings") or ""
            old_render_compression = shot.Get("renderCompression") or ""
        except Exception:
            old_render_file = old_render_settings = old_render_compression = ""

        # Apply preview output path (and optional overrides).
        # Use Begin/Accept (not BeginShotChanges/AcceptShotChanges) because
        # renderFile is a pure output setting — it does not invalidate the RAM
        # cache, so PREVALIDATE / POSTVALIDATE / RELOADALL are not needed and
        # would cause unnecessary heavy work (and potential crashes).
        self._log(f"Preview: setting render output to: {render_file}", LogLevel.Info)
        self.hlev.Begin()
        try:
            shot.Set("renderFile", render_file)
            if render_settings_override:
                shot.Set("renderSettings", render_settings_override)
            if render_compression_override:
                shot.Set("renderCompression", render_compression_override)
            self.hlev.Accept("Ramses: Configure Preview Render")
        except Exception as e:
            try:
                self.hlev.Cancel()
            except Exception:
                pass
            self._log(f"Failed to configure preview render: {e}", LogLevel.Warning)
            return []

        # Snapshot directory before rendering
        before = set(os.listdir(previewFolderPath))

        # Render via menu — same pattern as hlev.Export() / ExportAgain().
        # hlev.RenderShot(shot) sends RENDERSHOT1 which crashes SynthEyes in
        # undo.cpp when renderSettings is unconfigured (scene-level format not
        # set up yet in Save Sequence).  ClickMainMenuAndWait drives the normal
        # "Save Sequence" menu action which handles format/codec internally and
        # does not interact with the undo stack.
        # If the scene's Save Sequence dialog appears, the user confirms once.
        self._log("Preview: triggering Save Sequence via menu...", LogLevel.Info)
        render_ok = False
        try:
            self.hlev.ClickMainMenuAndWait("Save Sequence")
            render_ok = True
            self._log("Preview: Save Sequence completed.", LogLevel.Info)
        except Exception as e:
            self._log(f"Preview render failed: {e}", LogLevel.Critical)

        # Restore original render settings regardless of render success.
        # Each setting gets its own undo block so a failure on one does not
        # roll back a successfully restored earlier setting via Cancel().
        for _key, _val, _cond in (
            ("renderFile",        old_render_file,        True),
            ("renderSettings",    old_render_settings,    bool(render_settings_override)),
            ("renderCompression", old_render_compression, bool(render_compression_override)),
        ):
            if not _cond:
                continue
            try:
                self.hlev.Begin()
                shot.Set(_key, _val)
                self.hlev.Accept("Ramses: Restore Render Settings")
            except Exception as e:
                self._log(f"Failed to restore {_key} after preview: {e}", LogLevel.Warning)
                try:
                    self.hlev.Cancel()
                except Exception:
                    pass

        if not render_ok:
            return []

        # Return the list of newly created files
        after = set(os.listdir(previewFolderPath))
        return sorted(
            os.path.join(previewFolderPath, f)
            for f in (after - before)
        )

    def savePreview(self) -> bool:
        """Renders the preview and reports whether a file was actually written.

        The vendored base returns False when the preview path cannot be
        resolved, but returns None on the success path *and* whenever
        _preview() produced no files at all. So a render that failed or was
        cancelled is indistinguishable from a successful one, and a caller
        checking `is False` only ever catches the unsaved-scene case.

        This override keeps the same behaviour and returns a real bool, so the
        UI can tell the artist the truth.

        Returns:
            bool: True when at least one preview file was produced.
        """
        path = self.previewPath()
        if not path:
            self.log(
                "The current scene is not saved, so there is nowhere to put "
                "the preview.",
                LogLevel.Critical,
            )
            return False

        fileInfo = RamFileInfo()
        fileInfo.setFilePath(self.currentFilePath())
        previewInfo = fileInfo.copy()
        previewInfo.version = -1
        previewInfo.extension = ""
        previewInfo.resource = ""
        previewInfo.state = ""

        previewFiles = self._preview(
            path, previewInfo.fileName(), self.currentItem(), self.currentStep()
        )
        if not previewFiles:
            return False

        for file in previewFiles:
            RamMetaDataManager.setVersion(file, self.currentVersion())
            RamMetaDataManager.setVersionFilePath(file, self.currentVersionFilePath())
        return True

    # Extensions SynthEyes writes for the export types we name, taken from the
    # SIZZLEX header of each shipped exporter (e.g. scripts/blender25.szl
    # declares "//SIZZLEX .py Blender (Python)"). Anything unrecognised keeps
    # the previous default rather than guessing.
    EXPORT_EXTENSIONS = (
        ("Fusion", "comp"),
        ("Blender", "py"),
    )

    @classmethod
    def _exportExtension(cls, exportType: str) -> str:
        """The file extension for a File > Export menu entry."""
        for needle, extension in cls.EXPORT_EXTENSIONS:
            if needle in exportType:
                return extension
        return "txt"

    def _publish(self, publishInfo: RamFileInfo, publishOptions: dict) -> list:
        """Exports tracking data to the publish folder using SynthEyes Export."""
        if not self.hlev:
            return []
        if not self._ensure_connected():
            return []

        options = publishOptions or {}
        export_type = options.get("exportType", "Fusion Composition")
        ext = self._exportExtension(export_type)

        publishInfo.extension = ext
        if not publishInfo.resource:
            publishInfo.resource = "tracking"

        export_path = self.normalizePath(publishInfo.filePath())
        target_dir = os.path.dirname(export_path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        before_export = set(os.listdir(target_dir)) if os.path.isdir(target_dir) else set()
        try:
            self.hlev.Export(export_type, export_path)
            self._log(f"Exported {export_type} to: {export_path}", LogLevel.Info)
            # Some export types (e.g. per-camera) write multiple files — collect all
            # new entries so the pipeline can register every published output.
            after_export = set(os.listdir(target_dir)) if os.path.isdir(target_dir) else set()
            new_files = sorted(
                os.path.join(target_dir, f) for f in (after_export - before_export)
            )
            return new_files if new_files else [export_path]
        except Exception as e:
            self._log(f"Export failed: {e}", LogLevel.Critical)
            self._log(f"TIP: Verify the name '{export_type}' exactly matches the entry in your SynthEyes File > Export menu.", LogLevel.Info)
            return []

    def _publishOptions(self, proposedOptions: dict, showPublishUI: bool = False) -> dict:
        """Shows a UI to edit the publish options (YAML) if requested."""
        defaults = {
            # Tracking data export ─────────────────────────────────────────────
            # Name from SynthEyes File › Export menu.
            "exportType": "Fusion Composition",

            # Preview / Save-Sequence render ───────────────────────────────────
            # File extension that controls the output format:
            #   Image sequences : jpg  png  exr  dpx  tif
            #   Movies (OS-dependent) : avi  mov  mp4
            "previewFormat": "jpg",

            # Advanced: override the opaque SynthEyes channel-selection and
            # codec strings.  Leave empty to use whatever is already
            # configured in the scene.
            # To find the values: set up the render in SynthEyes, then read:
            #   hlev.Shots()[0].Get("renderSettings")
            #   hlev.Shots()[0].Get("renderCompression")
            "previewRenderSettings": "",
            "previewRenderCompression": "",
        }
        options = {**defaults, **(proposedOptions or {})}
        if not showPublishUI:
            return options

        # A small form (replaces the old hand-edited-YAML editor). Editable
        # combo boxes give discoverability while still allowing any value.
        try:
            try:
                from PySide2 import QtWidgets as qw
                from PySide2 import QtCore as qc
            except ImportError:
                from PySide6 import QtWidgets as qw
                from PySide6 import QtCore as qc

            dialog = qw.QDialog()
            dialog.setWindowTitle("Export Settings")
            dialog.setMinimumWidth(460)
            layout = qw.QVBoxLayout(dialog)
            form = qw.QFormLayout()
            layout.addLayout(form)

            # Tracking-data export type — must match a File › Export menu entry.
            # Only names verified against the exporters SynthEyes ships, whose
            # SIZZLEX header line declares the menu name. The combo stays
            # editable, so an unlisted exporter can still be typed in.
            export_combo = qw.QComboBox()
            export_combo.setEditable(True)
            for name in ("Fusion Composition", "Blender (Python)"):
                export_combo.addItem(name)
            export_combo.setEditText(str(options.get("exportType", "Fusion Composition")))
            export_combo.setToolTip(
                "Must match an entry in the SynthEyes File › Export menu exactly.\n"
                "Other exporters can be typed in; check the name in the menu first.")
            form.addRow("Export type:", export_combo)

            # Preview output format (file extension).
            preview_combo = qw.QComboBox()
            preview_combo.setEditable(True)
            for ext in ("jpg", "png", "exr", "dpx", "tif", "avi", "mov", "mp4"):
                preview_combo.addItem(ext)
            preview_combo.setEditText(str(options.get("previewFormat", "jpg")).lstrip("."))
            preview_combo.setToolTip(
                "Image sequence: jpg png exr dpx tif   |   movie: avi mov mp4")
            form.addRow("Preview format:", preview_combo)

            # Advanced overrides — empty means "use the scene's current setting".
            settings_edit = qw.QLineEdit(str(options.get("previewRenderSettings", "")))
            settings_edit.setPlaceholderText("empty = use the scene's current channel setting")
            settings_edit.setToolTip(
                "Advanced: SynthEyes channel-selection string. Leave empty for the scene default.")
            form.addRow("Render settings:", settings_edit)

            compression_edit = qw.QLineEdit(str(options.get("previewRenderCompression", "")))
            compression_edit.setPlaceholderText("empty = use the scene's current codec")
            compression_edit.setToolTip(
                "Advanced: SynthEyes codec string. Leave empty for the scene default.")
            form.addRow("Render codec:", compression_edit)

            buttons = qw.QDialogButtonBox(
                qw.QDialogButtonBox.Ok | qw.QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            dialog.setWindowFlags(dialog.windowFlags() | qc.Qt.WindowStaysOnTopHint)
            dialog.raise_()
            dialog.activateWindow()
            if not self._exec_dialog(dialog):
                return None  # User cancelled

            # Start from the merged options so any extra keys a step carries are
            # preserved; override only the four fields the form exposes.
            result = dict(options)
            export_type = export_combo.currentText().strip()
            if export_type:
                result["exportType"] = export_type
            preview_fmt = preview_combo.currentText().strip().lstrip(".")
            if preview_fmt:
                result["previewFormat"] = preview_fmt
            result["previewRenderSettings"] = settings_edit.text().strip()
            result["previewRenderCompression"] = compression_edit.text().strip()
            return result

        except Exception as e:
            self._log(f"Could not show publish settings UI: {e}", LogLevel.Warning)
            return options  # Fall back to defaults rather than blocking publish

    def _prePublish(self, publishInfo: RamFileInfo, publishOptions: dict) -> dict:
        return publishOptions

    def _replace(self, filePaths: list, item: RamItem, step: RamStep, importOptions: list, forceShowImportUI: bool) -> bool:
        """Deliberately unimplemented — see note below.

        In Ramses-Fusion, Replace swaps a Loader's footage to a different
        published version in place. The SynthEyes analog would be re-pointing
        the current shot's footage to a different published plate version while
        keeping the existing solve.

        SynthEyes binds footage at shot-creation time (NewSceneAndShot /
        AddShot) and exposes NO supported SyPy or Sizzle call to change the
        image source of an existing shot; a blind write to the internal
        'filenam' attribute would risk silently desyncing the RAM cache and
        trackers from the new footage. Rather than ship a corrupting guess,
        Replace is deferred until a safe footage-repoint path is available
        (e.g. via Boris FX support). To move to a new plate today, create a new
        scene from it via 'Browse Shots'.
        """
        return False

    def _replaceUI(self, item: RamItem, step: RamStep) -> dict:
        """Unused — Replace is deferred (see _replace for the reason)."""
        return None

    def _restoreVersionUI(self, versionFiles: list) -> str:
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.versions_dialog import RamVersionDialog
                dialog = RamVersionDialog(versionFiles)
                dialog.raise_()
                dialog.activateWindow()
                res = self._exec_dialog(dialog)
                if res:
                    return dialog.currentFilePath()
            except ImportError:
                pass
        return ""

    def _saveChangesUI(self) -> str:
        """Asks the user whether to save unsaved changes before switching scenes.
        Returns 'cancel', 'save', or 'discard' as expected by RamHost.open().
        """
        try:
            try:
                from PySide2.QtWidgets import QMessageBox
            except ImportError:
                from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                None,
                "Unsaved Changes",
                "The current scene has unsaved changes.\nSave before continuing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                return 'cancel'
            if reply == QMessageBox.Save:
                return 'save'
            return 'discard'
        except Exception:
            return 'cancel'  # Safest fallback — never discard work silently on dialog failure

