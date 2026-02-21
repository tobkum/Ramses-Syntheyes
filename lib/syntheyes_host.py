# -*- coding: utf-8 -*-
import os
import time
import json
import re
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
                    # Force a change with a timestamp to guarantee dirty state.
                    # Use plain Begin/Accept to avoid cache flush.
                    shot.ramses_saved = str(time.time())
                    self.hlev.Accept("Ramses: Force Save")
                    marked = True
            except Exception:
                try:
                    self.hlev.Cancel()
                except Exception:
                    pass
            if not marked:
                self._log("Could not mark scene dirty before save (no shots).", LogLevel.Warning)
        
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
                    self._markDirtyAndSave(filePath)
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

    def setupCurrentFile(self, forceUI: bool = True) -> bool:
        """Applies Ramses settings to the current scene. Returns True on success."""
        item = self.currentItem()
        if item:
            settings = self.collectItemSettings(item)
            return self._setupCurrentFile(item, self.currentStep(), settings, forceUI=forceUI)
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
            # When saving, do not force disruptive UI switches
            self.setupCurrentFile(forceUI=False)
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

    def _setupCurrentFile(self, item: RamItem, step: RamStep, setupOptions: dict, shot_obj: object = None, forceUI: bool = False) -> bool:
        """Sets the current file parameters (resolution, FPS, aspect)."""
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

        # 1. Shot Attributes (Heavy Refresh - Only if changed)
        needs_heavy_refresh = False
        target_rate = float(setupOptions.get("framerate", 24.0))
        try:
            # SyPy3 direct attribute access
            if abs(float(shot.rate or 0.0) - target_rate) > 0.001:
                needs_heavy_refresh = True
        except Exception:
            needs_heavy_refresh = True

        if needs_heavy_refresh:
            self.hlev.BeginShotChanges(shot)
            try:
                shot.rate = target_rate
                self.hlev.PostValidate(shot)
                # Use AcceptShotChanges only when media parameters changed
                self.hlev.AcceptShotChanges(shot, "Ramses: Sync Shot Settings")
            except Exception as e:
                try: self.hlev.Cancel()
                except: pass
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
            except Exception:
                try: self.hlev.Cancel()
                except: pass

        # 3. Frame range — UI controls, not shot attributes (Safe)
        frames = int(setupOptions.get("frames", 0))
        
        # Smart Frame Range: If Ramses says 1 (movie), trust SynthEyes if it has more.
        try:
            se_frames = int(shot.frames or 0)
            if se_frames > 1 and frames <= 1:
                frames = se_frames
        except Exception:
            pass

        if not frames:
            dur = float(setupOptions.get("duration", 0))
            fps = float(setupOptions.get("framerate", 24.0))
            frames = round(dur * fps)
        
        if frames > 0:
            start = RAM_SETTINGS.userSettings.get("compStartFrame", 1001)
            self.hlev.SetAnimStart(start)
            self.hlev.SetAnimEnd(start + frames - 1)
            
            # Jump to start only if we are forcing UI and currently out of range
            if forceUI:
                try:
                    if abs(self.hlev.Frame() - start) > frames:
                        self.hlev.SetFrame(start)
                        self.hlev.Redraw()
                except Exception:
                    pass

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

        # Create new scene
        self._log(f"Creating new scene with footage: {footagePath}", LogLevel.Info)
        try:
            res = self.hlev.NewSceneAndShot(self.normalizePath(footagePath), aspect)
            
            if res is not None:
                # Remember which item/step this scene is for so _open() can write
                # the sidecar once the pipeline file path is known and saved.
                self._pending_new_shot_item = item
                self._pending_new_shot_step = step
                
                # Sync scene settings (FPS, frame range) to the new shot
                # We force UI updates here as it's a new scene.
                self._setupCurrentFile(item, step, self.collectItemSettings(item), shot_obj=res, forceUI=True)
                
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
            except: pass
            self._log(f"Failed to embed identity in scene notes: {e}", LogLevel.Debug)

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

    def currentItem(self) -> RamItem:
        """Gets current item, recovery via scene-embedded metadata or sidecar."""
        # 1. Unnamed scene / New shot case
        if not self.currentFilePath():
            pending = getattr(self, "_pending_new_shot_item", None)
            if pending:
                return pending

        # 2. Scene-embedded identity (High performance & Self-healing)
        try:
            scene = self.hlev.Scene()
            notes = str(scene.notes or "")
            match = re.search(r"RAMSES_ID:({.*?})", notes)
            if match:
                meta = json.loads(match.group(1))
                item_uuid = meta.get("itemUUID")
                if item_uuid:
                    from ramses import RamShot, RamAsset
                    # Try shot first (most common for tracking)
                    real_item = RamShot(item_uuid)
                    if real_item.shortName() == "Unknown":
                        real_item = RamAsset(item_uuid)
                    if real_item.shortName() != "Unknown":
                        return real_item
        except Exception:
            pass

        # 3. Sidecar fallback
        item = super().currentItem()
        if not item or item.virtual():
            path = self.currentFilePath()
            if path:
                try:
                    meta = RamMetaDataManager.getValue(path, "ramses") or {}
                    item_uuid = meta.get("itemUUID")
                    if item_uuid:
                        from ramses import RamShot, RamAsset
                        real_item = RamShot(item_uuid)
                        if real_item.shortName() == "Unknown":
                            real_item = RamAsset(item_uuid)
                        if real_item.shortName() != "Unknown":
                            return real_item
                except Exception as e:
                    self._log(f"Failed to recover item from metadata: {e}", LogLevel.Warning)
        return item

    def currentStep(self) -> RamStep:
        """Gets current step, recovery via scene-embedded metadata or sidecar."""
        # 1. Unnamed scene / New shot case
        if not self.currentFilePath():
            pending = getattr(self, "_pending_new_shot_step", None)
            if pending:
                return pending

        # 2. Scene-embedded identity (High performance & Self-healing)
        try:
            scene = self.hlev.Scene()
            notes = str(scene.notes or "")
            match = re.search(r"RAMSES_ID:({.*?})", notes)
            if match:
                meta = json.loads(match.group(1))
                step_uuid = meta.get("stepUUID")
                if step_uuid:
                    from ramses import RamStep
                    real_step = RamStep(step_uuid)
                    if real_step.shortName() != "Unknown":
                        return real_step
        except Exception:
            pass

        # 3. Sidecar fallback
        step = super().currentStep()
        if not step or step.shortName() == "Unknown":
            path = self.currentFilePath()
            if path:
                try:
                    meta = RamMetaDataManager.getValue(path, "ramses") or {}
                    step_uuid = meta.get("stepUUID")
                    if step_uuid:
                        from ramses import RamStep
                        real_step = RamStep(step_uuid)
                        if real_step.shortName() != "Unknown":
                            return real_step
                except Exception:
                    pass
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
        """Loads published footage into the current SynthEyes scene as a new shot."""
        if not self.hlev or not filePaths:
            return False
        
        raw_path = str(filePaths[0])
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

        # Acquire global listener lock for atomic import & setup (API Perfection)
        self.hlev.Lock()
        try:
            # 1. Determine if we should initialize a new scene or add to the current one
            is_empty = True
            
            # RULE 1: If the scene has a name on disk, it's NOT empty.
            if self.currentFilePath():
                is_empty = False
            else:
                try:
                    # RULE 2: If there's already media loaded, it's NOT empty.
                    num_shots = self.hlev.NumByType("SHOT")
                    if num_shots > 1:
                        is_empty = False
                    elif num_shots == 1:
                        shots = self.hlev.Shots()
                        if shots:
                            # Check if the shot actually has a footage file linked
                            media_path = shots[0].Get("filenam") or ""
                            if media_path.strip():
                                is_empty = False
                    
                    # RULE 3: If the user has started working (trackers/meshes), it's NOT empty.
                    if is_empty:
                        if self.hlev.NumByType("TRK") > 0 or self.hlev.NumByType("MESH") > 0:
                            is_empty = False
                except Exception:
                    pass

            if is_empty:
                self._log("Fresh session detected. Initializing primary shot...", LogLevel.Info)
                res = self.newShot(footage_path, item, step)
                return res

            # 2. Add to existing scene
            try:
                self._log(f"Calling AddShot with aspect: {aspect}", LogLevel.Info)
                res = self.hlev.AddShot(footage_path, aspect)
                
                if res is not None:
                    self._log(f"AddShot successful: {res.Name() if hasattr(res, 'Name') else 'New Shot'}", LogLevel.Info)
                    # If we are in an unnamed/new scene, track this as the pending identity
                    if not self.currentFilePath():
                        self._pending_new_shot_item = item
                        self._pending_new_shot_step = step

                    # Sync settings and refresh UI
                    self._setupCurrentFile(item, step, self.collectItemSettings(item), shot_obj=res, forceUI=True)
                    
                    try:
                        num_frames = int(res.frames or 0)
                        self._log(f"Import successful. Shot has {num_frames} frames.", LogLevel.Info)
                    except Exception:
                        pass

                    self.hlev.ReloadAll()
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
        export_type = options.get("exportType", "Fusion Composition")
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

