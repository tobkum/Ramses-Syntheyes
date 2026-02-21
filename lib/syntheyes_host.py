# -*- coding: utf-8 -*-
import os
try:
    import ramses.yaml as yaml
except ImportError:
    import yaml
from ramses import (
    RamHost,
    RamItem,
    RamStep,
    RamStatus,
    RamFileInfo,
    LogLevel,
    ItemType,
    RAMSES,
    RAM_SETTINGS,
    RamMetaDataManager,
    RamState,
)

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

    def normalizePath(self, path: object) -> str:
        """Centralized path normalization for SynthEyes (forward slashes)."""
        if not path:
            return ""
        path_str = str(path)
        abs_path = os.path.abspath(path_str)
        return abs_path.replace("\\", "/")

    def currentFilePath(self) -> str:
        """Gets the file path of the current .sni file."""
        if not self.hlev:
            return ""
        try:
            path = self.hlev.SNIFileName()
            return self.normalizePath(path)
        except Exception:
            return ""

    def _isDirty(self) -> bool:
        """Checks if the current scene has unsaved changes."""
        if not self.hlev:
            return False
        return bool(self.hlev.HasChanged())

    def _log(self, message: str, level: int) -> None:
        """Logs a message to the console."""
        if level < LogLevel.Info:
            return
        
        prefix = "Ramses"
        if level == LogLevel.Warning:
            prefix = "Ramses Warning"
        elif level == LogLevel.Critical:
            prefix = "Ramses Error"
        
        print(f"[{prefix}] {str(message)}")

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
            # Set the target path then save. SaveIfChanged() is the correct
            # SyPy3 save method; after SetSNIFileName the scene is considered
            # modified (filename changed), so this always writes to the new path.
            self.hlev.SetSNIFileName(filePath)
            self.hlev.SaveIfChanged()

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
            # Always consume the pending flags here — they are only valid for
            # the single _open() call that immediately follows newShot().
            pending = getattr(self, "_pending_new_shot_item", None)
            pending_step = getattr(self, "_pending_new_shot_step", None)
            self._pending_new_shot_item = None
            self._pending_new_shot_step = None
            if pending and item and pending.uuid() == item.uuid():
                target_dir = os.path.dirname(filePath)
                if target_dir:
                    os.makedirs(target_dir, exist_ok=True)
                try:
                    self.hlev.SetSNIFileName(filePath)
                    self.hlev.SaveIfChanged()
                    # Write sidecar now that the file is on disk
                    self._store_ramses_metadata(item, step or pending_step, filePath)
                    return True
                except Exception as e:
                    self._log(f"Failed to save new scene to pipeline path: {e}", LogLevel.Critical)
            return False
        try:
            self.hlev.OpenSNI(filePath)
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

        project = RAMSES.project()
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
                    settings["frames"] = round(settings["duration"] * float(seq_fps))
                if seq_par:
                    settings["pixelAspectRatio"] = float(seq_par)

        return settings

    def setupCurrentFile(self) -> bool:
        """Applies Ramses settings to the current scene. Returns True on success."""
        item = self.currentItem()
        if item:
            settings = self.collectItemSettings(item)
            return self._setupCurrentFile(item, self.currentStep(), settings)
        return False

    def save(
        self,
        incremental: bool = False,
        comment: str = None,
        setupFile: bool = True,
        state: RamState = None,
    ) -> bool:
        """Saves the current file, optionally setting up the scene."""
        if setupFile:
            self.setupCurrentFile()
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

    def _setupCurrentFile(self, item: RamItem, step: RamStep, setupOptions: dict) -> bool:
        """Sets the current file parameters (resolution, FPS, aspect)."""
        if not self.hlev:
            return False
        
        # We need a shot to apply settings to
        shots = self.hlev.Shots()
        if not shots:
            return True
        
        shot = shots[0] # Assume primary shot

        # BeginShotChanges is required (not plain Begin) when modifying shot
        # settings so SynthEyes can correctly invalidate its RAM cache.
        self.hlev.BeginShotChanges(shot)
        try:
            # framerate / fps
            if "framerate" in setupOptions:
                shot.Set("rate", float(setupOptions["framerate"]))

            # NOTE: backPlateWidth is in millimetres (Sizzle doc).
            # project.width() returns pixels, so we do NOT set it here —
            # SynthEyes reads resolution directly from the loaded footage.

            self.hlev.AcceptShotChanges(shot, "Ramses: Setup Scene")
        except Exception as e:
            self.hlev.Cancel()
            self._log(f"Failed to setup scene: {e}", LogLevel.Warning)
            return False

        # Frame range — SetAnimStart/End are UI controls, not shot attributes,
        # so they live outside the BeginShotChanges/AcceptShotChanges block.
        frames = int(setupOptions.get("frames", 0))
        if not frames:
            # Fallback: derive frame count from duration (seconds) × framerate
            dur = float(setupOptions.get("duration", 0))
            fps = float(setupOptions.get("framerate", 24.0))
            frames = round(dur * fps)
        if frames > 0:
            start = RAM_SETTINGS.userSettings.get("compStartFrame", 1001)
            self.hlev.SetAnimStart(start)
            self.hlev.SetAnimEnd(start + frames - 1)

        # Persist identity
        self._store_ramses_metadata(item, step)
        return True

    def newShot(self, footagePath: str, item: RamItem, step: RamStep) -> bool:
        """Creates a new scene with the specified footage."""
        if not self.hlev:
            return False

        if self.currentFilePath():
            self.hlev.SaveIfChanged()

        # Get project settings
        project = item.project()
        aspect = project.aspectRatio() if project else (16.0 / 9.0)

        # Create new scene
        # filnm can be first image of sequence or movie
        res = self.hlev.NewSceneAndShot(self.normalizePath(footagePath), aspect)

        if res:
            # Remember which item/step this scene is for so _open() can write
            # the sidecar once the pipeline file path is known and saved.
            self._pending_new_shot_item = item
            self._pending_new_shot_step = step
            if not self._setupCurrentFile(item, step, self.collectItemSettings(item)):
                self._log(
                    "Scene created but settings (FPS, frame range) could not be applied.",
                    LogLevel.Warning,
                )
            return True

        return False

    def _store_ramses_metadata(
        self, item: RamItem, step: RamStep = None, filePath: str = None
    ) -> bool:
        """Stores Ramses identity (item/step UUIDs) in the Ramses sidecar file.

        Uses RamMetaDataManager to write a JSON sidecar (_ramses_data.json) in
        the same folder as the .sni file.  The sidecar must be written AFTER
        the .sni file exists on disk; when called before the file is saved
        (e.g. from _setupCurrentFile on a brand-new scene), the write is
        deferred and this method returns True without doing anything — the write
        will happen in _saveAs() once the file exists.

        Returns True on success (or deferred write), False on I/O failure.
        """
        path = filePath or self.currentFilePath()
        if not path or not os.path.isfile(path):
            # File not yet on disk — metadata will be written by _saveAs().
            return True

        meta = {
            "itemUUID": str(item.uuid()),
            "projectUUID": str(item.project().uuid()) if item.project() else "",
        }
        if step:
            meta["stepUUID"] = str(step.uuid())

        try:
            RamMetaDataManager.setValue(path, "ramses", meta)
            return True
        except Exception as e:
            self._log(f"Failed to store metadata: {e}", LogLevel.Warning)
            return False

    def currentItem(self) -> RamItem:
        """Gets current item, recovery via sidecar metadata if needed."""
        item = super().currentItem()

        if not item or item.virtual():
            path = self.currentFilePath()
            if path:
                try:
                    meta = RamMetaDataManager.getValue(path, "ramses") or {}
                    item_uuid = meta.get("itemUUID")
                    if item_uuid:
                        from ramses import RamShot, RamAsset
                        # Try shot first (most common for tracking)
                        real_item = RamShot(item_uuid)
                        if real_item.shortName() == "Unknown":
                            real_item = RamAsset(item_uuid)

                        if real_item.shortName() != "Unknown":
                            return real_item
                        else:
                            self._log(
                                f"Sidecar metadata found but item UUID '{item_uuid}' "
                                "not found in Ramses (shot or asset).",
                                LogLevel.Warning,
                            )
                except Exception as e:
                    self._log(f"Failed to recover item from sidecar metadata: {e}", LogLevel.Warning)
        return item

    def currentStep(self) -> RamStep:
        """Gets current step, recovery via sidecar metadata if needed."""
        step = super().currentStep()

        if not step or step.shortName() == "Unknown":
            path = self.currentFilePath()
            if path:
                try:
                    meta = RamMetaDataManager.getValue(path, "ramses") or {}
                    step_uuid = meta.get("stepUUID")
                    if step_uuid:
                        real_step = RamStep(step_uuid)
                        if real_step.shortName() != "Unknown":
                            return real_step
                except Exception:
                    pass
        return step

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
                    mamo_step = project.step("MaMo") or project.step("Matchmove")
                    if mamo_step:
                        dialog.setCurrentStep(mamo_step)

                if getattr(dialog, 'exec', None):
                    res = dialog.exec()
                else:
                    res = dialog.exec_()
                    
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
                        # Search for plate — step names are configurable via
                        # RAM_SETTINGS.userSettings["plateStepNames"]
                        plate_path = ""
                        default_plate_steps = ["Plate", "Ingest", "Footage"]
                        plate_step_names = RAM_SETTINGS.userSettings.get(
                            "plateStepNames", default_plate_steps
                        )
                        for step_name in plate_step_names:
                            p_step = project.step(step_name)
                            if p_step:
                                pub_files = item.latestPublishedVersionFilePaths(step=p_step)
                                if pub_files:
                                    plate_path = pub_files[0]
                                    break
                        
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
                    mamo_step = project.step("MaMo") or project.step("Matchmove")
                    if mamo_step:
                        dialog.setStep(mamo_step)

                if getattr(dialog, 'exec', None):
                    res = dialog.exec()
                else:
                    res = dialog.exec_()
                    
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
                if getattr(dialog, 'exec', None):
                    res = dialog.exec()
                else:
                    res = dialog.exec_()
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
        """Loads published footage into SynthEyes as a new scene/shot.

        For image sequences, filePaths contains all frame files — we pass the
        first frame and SynthEyes auto-detects the full sequence.
        For movie clips, filePaths contains the single movie file.
        """
        if not self.hlev or not filePaths:
            return False
        footage_path = self.normalizePath(filePaths[0])
        result = self.newShot(footage_path, item, step)
        # importItem() does not call _open(), so clear the pending flags that
        # newShot() set — they only make sense for the _openUI → _open() path.
        self._pending_new_shot_item = None
        self._pending_new_shot_step = None
        return result

    def _importUI(self, item: RamItem, step: RamStep) -> dict:
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.import_dialog import RamImportDialog
                dialog = RamImportDialog()
                dialog.raise_()
                dialog.activateWindow()
                if getattr(dialog, 'exec', None):
                    res = dialog.exec()
                else:
                    res = dialog.exec_()
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

        # Restore original render settings regardless of render success
        try:
            self.hlev.Begin()
            shot.Set("renderFile", old_render_file)
            if render_settings_override:
                shot.Set("renderSettings", old_render_settings)
            if render_compression_override:
                shot.Set("renderCompression", old_render_compression)
            self.hlev.Accept("Ramses: Restore Render Settings")
        except Exception as e:
            self._log(f"Failed to restore render settings after preview: {e}", LogLevel.Warning)
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

    def _publish(self, publishInfo: RamFileInfo, publishOptions: dict) -> list:
        """Exports tracking data to the publish folder using SynthEyes Export."""
        if not self.hlev:
            return []

        options = publishOptions or {}
        export_type = options.get("exportType", "Blackmagic Fusion Comp")
        ext = "comp" if "Fusion" in export_type else "txt"

        publishInfo.extension = ext
        if not publishInfo.resource:
            publishInfo.resource = "tracking"

        export_path = self.normalizePath(publishInfo.filePath())
        target_dir = os.path.dirname(export_path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        try:
            self.hlev.Export(export_type, export_path)
            self._log(f"Exported {export_type} to: {export_path}", LogLevel.Info)
            return [export_path]
        except Exception as e:
            self._log(f"Export failed: {e}", LogLevel.Critical)
            return []

    def _publishOptions(self, proposedOptions: dict, showPublishUI: bool = False) -> dict:
        """Shows a UI to edit the publish options (YAML) if requested."""
        defaults = {
            # Tracking data export ─────────────────────────────────────────────
            # Name from SynthEyes File › Export menu.
            "exportType": "Blackmagic Fusion Comp",

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

        # Convert dict to YAML for editing
        try:
            current_yaml = yaml.dump(options, default_flow_style=False)
        except Exception:
            current_yaml = ""

        yaml_label = (
            "exportType         — name from SynthEyes File › Export menu\n"
            "previewFormat      — image seq: jpg png exr dpx tif  |  movie: avi mov mp4\n"
            "previewRenderSettings    — advanced: SynthEyes channel string (empty = scene default)\n"
            "previewRenderCompression — advanced: SynthEyes codec string   (empty = scene default)"
        )

        try:
            try:
                from PySide2 import QtWidgets as qw, QtGui as qg
            except ImportError:
                from PySide6 import QtWidgets as qw, QtGui as qg

            dialog = qw.QDialog()
            dialog.setWindowTitle("Export Settings")
            dialog.setMinimumWidth(520)
            layout = qw.QVBoxLayout(dialog)

            layout.addWidget(qw.QLabel("Settings (YAML):"))
            layout.addWidget(qw.QLabel(yaml_label))

            editor = qw.QPlainTextEdit()
            editor.setFont(qg.QFont("Courier New", 9))
            editor.setPlainText(current_yaml)
            editor.setMinimumHeight(300)
            layout.addWidget(editor)

            buttons = qw.QDialogButtonBox(
                qw.QDialogButtonBox.Ok | qw.QDialogButtonBox.Cancel
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            dialog.raise_()
            dialog.activateWindow()
            accepted = dialog.exec() if getattr(dialog, 'exec', None) else dialog.exec_()

            if accepted:
                try:
                    new_options = yaml.safe_load(editor.toPlainText())
                    if isinstance(new_options, dict):
                        return new_options
                    self._log("Publish settings YAML must be a mapping.", LogLevel.Warning)
                    return self._publishOptions(options, True)
                except Exception as e:
                    self._log(f"Invalid YAML in publish settings: {e}", LogLevel.Warning)
                    return self._publishOptions(options, True)

            return None  # User cancelled

        except Exception as e:
            self._log(f"Could not show publish settings UI: {e}", LogLevel.Warning)
            return options  # Fall back to defaults rather than blocking publish

    def _prePublish(self, publishInfo: RamFileInfo, publishOptions: dict) -> dict:
        return publishOptions

    def _replace(self, filePaths: list, item: RamItem, step: RamStep, importOptions: list, forceShowImportUI: bool) -> bool:
        return False

    def _replaceUI(self, item: RamItem, step: RamStep) -> dict:
        return None

    def _restoreVersionUI(self, versionFiles: list) -> str:
        if hasattr(self, 'app') and self.app:
            try:
                from ramses_ui_pyside.versions_dialog import RamVersionDialog
                dialog = RamVersionDialog(versionFiles)
                dialog.raise_()
                dialog.activateWindow()
                if getattr(dialog, 'exec', None):
                    res = dialog.exec()
                else:
                    res = dialog.exec_()
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
            return 'discard'  # Fallback: allow proceeding without saving

