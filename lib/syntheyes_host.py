# -*- coding: utf-8 -*-
import os
import re
import json
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

        try:
            # In SynthEyes, we set the filename and then trigger a save action.
            # SaveSNI() is the most robust SyPy method for this.
            self.hlev.SetSNIFileName(filePath)
            self.hlev.SaveSNI()
            
            # Store metadata in a Note object if item is provided
            if item:
                self._store_ramses_metadata(item, step)
                
            return True
        except Exception as e:
            self._log(f"Failed to save: {e}", LogLevel.Critical)
            return False

    def _open(self, filePath: str, item: RamItem, step: RamStep) -> bool:
        """Internal implementation to open an .sni file."""
        if os.path.exists(filePath):
            self.hlev.OpenSNI(self.normalizePath(filePath))
            return True
        return False

    def _setFileName(self, fileName: str) -> bool:
        """Sets the internal file name."""
        if not self.hlev:
            return False
        self.hlev.SetSNIFileName(self.normalizePath(fileName))
        return True

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

            # Frame range — SetAnimStart/End are UI controls, not shot
            # attributes, so they live outside the shot undo block.
            self.hlev.AcceptShotChanges(shot, "Ramses: Setup Scene")

            if "duration" in setupOptions and setupOptions["duration"] > 0:
                start = RAM_SETTINGS.userSettings.get("compStartFrame", 1001)
                duration = int(setupOptions["duration"])
                self.hlev.SetAnimStart(start)
                self.hlev.SetAnimEnd(start + duration - 1)

            return True
        except Exception as e:
            self.hlev.Cancel()
            self._log(f"Failed to setup scene: {e}", LogLevel.Warning)
            return False

    def exportScene(self, exportType: str = "Blackmagic Fusion Comp") -> str:
        """Exports the scene to the Ramses publish folder."""
        if not self.hlev:
            return ""
            
        item = self.currentItem()
        step = self.currentStep()
        if not item or not step:
            self._log("Cannot export: File not in Ramses pipeline.", LogLevel.Warning)
            return ""

        # Determine extension based on type
        ext = "comp" if "Fusion" in exportType else "txt"
        
        # Get publish path via Ramses API
        publish_info = self.publishInfo()
        publish_info.extension = ext
        # Add '-tracking' resource suffix
        if publish_info.resource:
            publish_info.resource += "-tracking"
        else:
            publish_info.resource = "tracking"
            
        export_path = self.normalizePath(publish_info.filePath())
        
        # Ensure directory
        os.makedirs(os.path.dirname(export_path), exist_ok=True)
        
        try:
            self.hlev.Export(exportType, export_path)
            self._log(f"Exported {exportType} to: {export_path}", LogLevel.Info)
            return export_path
        except Exception as e:
            self._log(f"Export failed: {e}", LogLevel.Critical)
            return ""

    def newShot(self, footagePath: str, item: RamItem, step: RamStep) -> bool:
        """Creates a new scene with the specified footage."""
        if not self.hlev:
            return False
        
        self.hlev.SaveIfChanged()
        
        # Get project settings
        project = item.project()
        aspect = project.aspectRatio() if project else 1.777
        
        # Create new scene
        # filnm can be first image of sequence or movie
        res = self.hlev.NewSceneAndShot(self.normalizePath(footagePath), aspect)
        
        if res:
            # Setup scene parameters
            setup_opts = {
                "width": project.width(),
                "height": project.height(),
                "framerate": project.framerate(),
                "pixelAspectRatio": project.pixelAspectRatio(),
                "aspectRatio": project.aspectRatio()
            }
            if item.itemType() == ItemType.SHOT:
                setup_opts["duration"] = item.duration()
                
            self._setupCurrentFile(item, step, setup_opts)
            self._store_ramses_metadata(item, step)
            return True
            
        return False

    def _store_ramses_metadata(self, item: RamItem, step: RamStep = None):
        """Stores Ramses identity in a hidden Note.

        Both the optional CreateNew and the Set must be inside a single
        Begin/Accept block — SyObj.Set() requires an active undo context.
        The Note attribute 'text' is the standard Sizzle content field;
        if the installed SynthEyes version uses a different name the except
        branch will log the failure without crashing the save workflow.
        """
        if not self.hlev:
            return

        note_name = "RamsesMetadata"
        note = self.hlev.FindByName("NOTE", note_name)

        meta = {
            "ItemUUID": str(item.uuid()),
            "ProjectUUID": str(item.project().uuid()) if item.project() else "",
        }
        if step:
            meta["StepUUID"] = str(step.uuid())

        self.hlev.Begin()
        try:
            if not note:
                note = self.hlev.CreateNew("NOTE")
                note.SetName(note_name)
            note.Set("text", json.dumps(meta))
            self.hlev.Accept("Ramses: Store Metadata")
        except Exception as e:
            self.hlev.Cancel()
            self._log(f"Failed to store metadata: {e}", LogLevel.Warning)

    def currentItem(self) -> RamItem:
        """Gets current item, recovery via Note metadata if needed."""
        item = super().currentItem()
        
        if (not item or item.virtual()) and self.hlev:
            note = self.hlev.FindByName("NOTE", "RamsesMetadata")
            if note:
                try:
                    meta = json.loads(note.Get("text"))
                    item_uuid = meta.get("ItemUUID")
                    if item_uuid:
                        from ramses import RamShot, RamAsset
                        # Try shot first for tracking
                        real_item = RamShot(item_uuid)
                        if real_item.shortName() == "Unknown":
                            real_item = RamAsset(item_uuid)
                        
                        if real_item.shortName() != "Unknown":
                            return real_item
                except Exception:
                    pass
        return item

    def currentStep(self) -> RamStep:
        """Gets current step, recovery via Note metadata if needed."""
        step = super().currentStep()
        
        if (not step or step.shortName() == "Unknown") and self.hlev:
            note = self.hlev.FindByName("NOTE", "RamsesMetadata")
            if note:
                try:
                    meta = json.loads(note.Get("text"))
                    step_uuid = meta.get("StepUUID")
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
                from PySide2 import QtWidgets as qw

                dialog = RamOpenDialog( ["sni"] )
                
                # --- Pre-set Defaults ---
                project = RAMSES.project()
                if project:
                    mamo_step = project.step("MaMo") or project.step("Matchmove")
                    if mamo_step:
                        dialog.setCurrentStep(mamo_step)

                dialog.setWindowFlags(dialog.windowFlags() | qc.Qt.WindowStaysOnTopHint)
                dialog.raise_()
                dialog.activateWindow()

                if getattr(dialog, 'exec', None):
                    res = dialog.exec()
                else:
                    res = dialog.exec_()
                    
                if not res:
                    return None

                path = dialog.filePath()
                item = dialog.currentItem()
                step = dialog.currentStep()

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
                        # Search for plate
                        plate_path = ""
                        for step_name in ["Plate", "Ingest", "Footage"]:
                            p_step = project.step(step_name)
                            if p_step:
                                pub_files = item.latestPublishedVersionFilePaths(step=p_step)
                                if pub_files:
                                    plate_path = pub_files[0]
                                    break
                        
                        if plate_path:
                            # Use existing newShot method to initialize
                            if self.newShot(plate_path, item, step):
                                # Return the newly created path so RamHost.open() finalizes
                                return {
                                    "filePath": self.currentFilePath(),
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
                    return {
                        "state": dialog.state(),
                        "comment": dialog.comment(),
                        "completionRatio": dialog.completionRatio()
                    }
                return None
            except ImportError:
                pass
        return None

    # --- Mandatory Ramses API Overrides ---

    def _import(self, filePaths: list, item: RamItem, step: RamStep, importOptions: list, forceShowImportUI: bool) -> bool:
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
        return []

    def _publish(self, publishInfo: RamFileInfo, publishOptions: dict) -> list:
        return []

    def _publishOptions(self, proposedOptions: dict, showPublishUI: bool = False) -> dict:
        return proposedOptions

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

    def _saveChangesUI(self) -> bool:
        """Asks the user whether to save unsaved changes before switching scenes.
        Returns False if the user cancels, True otherwise (saving if requested).
        """
        try:
            try:
                from PySide6.QtWidgets import QMessageBox
            except ImportError:
                from PySide2.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                None,
                "Unsaved Changes",
                "The current scene has unsaved changes.\nSave before continuing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                return False
            if reply == QMessageBox.Save:
                self.save()
            return True
        except Exception:
            return True  # Fallback: allow proceeding if Qt is unavailable

    def isSynthEyesStep(self, step: RamStep) -> bool:
        """Determines if a given Step is configured for SynthEyes."""
        if not step:
            return False

        # 1. Check Step Naming (including MaMo)
        short = step.shortName().upper()
        if any(x in short for x in ["TRACK", "MATCHMOVE", "MAMO", "SYPY", "SYNTHEYES"]):
            return True

        # 2. Check Linked Applications
        daemon = RAMSES.daemonInterface()
        s_data = step.data()
        apps = s_data.get("applications", [])
        if isinstance(apps, list):
            for app_uuid in apps:
                app_data = daemon.getData(str(app_uuid), "RamApplication")
                app_name = str(app_data.get("name", "")).upper()
                if "SYNTHEYES" in app_name:
                    return True

        return False
