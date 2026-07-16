# ; Ramses
# -*- coding: utf-8 -*-
import sys
import os
import json
import time
import tempfile
import traceback

# --- Path Setup ---
script_dir = os.path.dirname(os.path.realpath(__file__))
plugin_lib_path = os.path.join(script_dir, "lib")
if plugin_lib_path not in sys.path:
    sys.path.append(plugin_lib_path)
_LOCK_FILE = os.path.join(tempfile.gettempdir(), "ramses_syntheyes.lock")

def _acquire_instance_lock() -> bool:
    """Returns True if this is the first instance, False if one is already running.

    Uses O_CREAT | O_EXCL for an atomic create-or-fail, eliminating the
    TOCTOU window that existed between the old existence-check and the write.
    """
    my_pid = str(os.getpid())

    def _try_atomic_create() -> bool:
        """Attempt a single atomic lock-file creation. Returns True on success."""
        try:
            fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, my_pid.encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False

    # First attempt: create atomically
    if _try_atomic_create():
        return True

    # Lock file already exists — read the PID and check liveness
    pid = None
    try:
        with open(_LOCK_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0: no-op, raises OSError if process is gone
        print(f"Ramses SynthEyes plugin is already running (PID {pid}).")
        return False
    except PermissionError:
        # On Windows, PermissionError means the process IS still running.
        print(f"Ramses SynthEyes plugin is already running (PID {pid}).")
        return False
    except (OSError, ValueError):
        pass  # ProcessLookupError (process gone) or unreadable/bad pid — stale lock

    # Stale lock: remove it, then retry the atomic create once
    try:
        os.remove(_LOCK_FILE)
    except OSError:
        pass

    if _try_atomic_create():
        return True

    # Another process slipped in during the narrow removal window
    print("Ramses SynthEyes: could not acquire instance lock — another instance started simultaneously.")
    return False

def _release_instance_lock():
    try:
        os.remove(_LOCK_FILE)
    except OSError:
        pass

def _quarantine_corrupt_addon_settings():
    """Moves a corrupt add-on settings file aside so startup can't hard-crash.

    RamSettings loads ${APPDATA}/Ramses/Config/ramses_addons_settings.json on
    the first `import ramses`; a truncated or malformed JSON there would raise
    deep in the SDK before the UI ever appears. Rename it to *.corrupt and let
    the SDK fall back to defaults. Only the platforms the SDK itself handles
    (Windows, Linux) are covered — it doesn't define a Darwin config path.
    """
    import platform as _platform
    system = _platform.system()
    if system == "Windows":
        folder = os.path.expandvars("${APPDATA}/Ramses/Config")
    elif system == "Linux":
        folder = os.path.expanduser("~/.config/Ramses/Config")
    else:
        return
    settings_file = os.path.join(folder, "ramses_addons_settings.json")
    if not os.path.isfile(settings_file):
        return
    try:
        with open(settings_file, "r", encoding="utf8") as f:
            json.load(f)
    except (ValueError, OSError):
        quarantined = settings_file + ".corrupt"
        try:
            os.replace(settings_file, quarantined)
            print(
                "[Ramses] Warning: the add-on settings file was corrupt and "
                "has been moved to " + quarantined + ". Default settings "
                "will be used; re-configure the add-on if needed."
            )
        except OSError:
            pass

def run_app():
    if not _acquire_instance_lock():
        return

    # Move a corrupt settings file aside before the first `import ramses`
    # (below, via syntheyes_host) triggers RamSettings to read it.
    _quarantine_corrupt_addon_settings()

    # --- SyPy Setup ---
    print("Searching for SyPy3...")
    try:
        import SyPy3 as SyPy
        print("SyPy3 found in system site-packages.")
    except ImportError:
        try:
            import SyPy
            print("SyPy found in system site-packages.")
        except ImportError:
            # Expanded search paths
            possible_paths = []
            for _year in [2026, 2025, 2024, 2023]:
                possible_paths += [
                    rf"C:\Program Files\BorisFX\SynthEyes {_year}",
                    rf"C:\Program Files\Boris FX\SynthEyes {_year}",
                    rf"C:\Program Files\SynthEyes {_year}",
                ]
            possible_paths.append(r"C:\Program Files\SynthEyes")
            
            # Try to infer from the script path (AppData version)
            # Script: ...\AppData\Local\BorisFX\SynthEyes 2026\scripts\...
            # App: C:\Program Files\BorisFX\SynthEyes 2026
            if "BorisFX" in script_dir:
                parts = script_dir.split(os.sep)
                try:
                    idx = parts.index("BorisFX")
                    app_name = parts[idx+1] # "SynthEyes 2026"
                    possible_paths.insert(0, os.path.join(r"C:\Program Files\BorisFX", app_name))
                except Exception: pass

            found = False
            print(f"Checking {len(possible_paths)} possible SynthEyes locations...")
            for p in possible_paths:
                check_path = os.path.join(p, "SyPy3")
                print(f" - checking: {check_path}")
                if os.path.exists(check_path):
                    sys.path.append(p)
                    try:
                        import SyPy3 as SyPy
                        print(f"FOUND SyPy3 at: {p}")
                        found = True
                        break
                    except ImportError as e:
                        print(f"Found SyPy3 folder but import failed: {e}")
                        continue
            
            if not found:
                print("\nERROR: SyPy3 not found.")
                print("Your SynthEyes seems to be installed in a non-standard location.")
                print("Please tell me the FULL PATH to your 'SynthEyes.exe' file.")
                return

    from syntheyes_host import SynthEyesHost

    # --- PySide Setup ---
    # _exec_compat: PySide2 has exec_() (idiomatic) and exec() as alias.
    # PySide6 removed exec_(). Prefer exec_() so PySide2 stays idiomatic;
    # fall back to exec() for PySide6.  getattr(dlg, 'exec', None) is always
    # truthy and cannot distinguish the two — this helper does it correctly.
    def _exec_compat(obj) -> int:
        fn = getattr(obj, 'exec_', None) or getattr(obj, 'exec', None)
        return fn() if fn else 0
    try:
        from PySide2 import QtWidgets as qw
        from PySide2 import QtCore as qc
        from PySide2 import QtGui as qg
    except ImportError:
        try:
            from PySide6 import QtWidgets as qw
            from PySide6 import QtCore as qc
            from PySide6 import QtGui as qg
        except ImportError:
            print("ERROR: PySide2 or PySide6 is required for the Ramses UI.")
            print("Please run: pip install PySide2")
            return

    import ramses as ram
    from ramses_ui_pyside.open_dialog import RamOpenDialog
    from ramses_ui_pyside.save_as_dialog import RamSaveAsDialog
    from ramses_ui_pyside.status_dialog import RamStatusDialog
    from ramses_ui_pyside.about_dialog import RamAboutDialog
    from ramses_ui_pyside.import_dialog import RamImportDialog
    from ramses_ui_pyside.update_dialog import RamUpdateDialog
    from ramses_ui_pyside.comment_dialog import RamCommentDialog

    class RamsesSyntheyesApp(qw.QMainWindow):
        """The main application window for the Ramses SynthEyes integration."""

        def __init__(self, hlev: object) -> None:
            super(RamsesSyntheyesApp, self).__init__()
            
            self.ramses = ram.Ramses.instance()
            self.settings = ram.RamSettings.instance()
            
            # Initialize Host
            self.host = SynthEyesHost(hlev)
            # Assign to the host attribute directly as Ramses object has no setHost method
            self.ramses.host = self.host
            self.host.app = self  # Required: dialog methods check hasattr(self, 'app')
            self.hlev = hlev

            # Cache for currentItem/currentStep — keyed by (filePath, pending_uuid)
            # so we also refresh when a new shot is loaded into an unsaved scene.
            self._context_cache = {"filePath": None, "_pending_uuid": None, "item": None, "step": None}

            self.setWindowTitle("Ramses - SynthEyes")
            self.setStyleSheet(
                "QMainWindow { background-color: #1a1a1a; }"
                "QWidget { background-color: #1a1a1a; color: #cccccc; }"
            )

            self.setup_ui()
            self.refresh_context()

            # The panel is a separate process from SynthEyes, so nothing tells it
            # when the artist opens/switches a scene directly in SynthEyes. Poll
            # the current .sni path on a light timer and refresh only when it
            # actually changes (see _poll_refresh). Focus-in does a full refresh
            # too (changeEvent) to also pick up status edits made in the Client.
            # refresh_context() above already seeded self._last_poll_path.
            self._poll_timer = qc.QTimer(self)
            self._poll_timer.setInterval(1500)
            self._poll_timer.timeout.connect(self._poll_refresh)
            self._poll_timer.start()

        def setup_ui(self):
            """Builds the vertical toolbar UI with icons."""
            central_widget = qw.QWidget()
            self.setCentralWidget(central_widget)
            layout = qw.QVBoxLayout(central_widget)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(4)

            # Context Label — matches Fusion's ContextFrame style
            self.context_label = qw.QLabel("No Active Shot")
            self.context_label.setStyleSheet(
                "QLabel { border: 1px solid #3a4048; background-color: #1e2228;"
                " border-radius: 4px; padding: 8px; }"
            )
            self.context_label.setAlignment(qc.Qt.AlignCenter)
            self.context_label.setWordWrap(True)
            self.context_label.setMinimumHeight(90)
            layout.addWidget(self.context_label)

            layout.addSpacing(5)

            # Section palette matches Ramses-Fusion so an artist using both
            # tools reads the same colour language:
            #   project=blue  working=teal  publish=green  settings=neutral
            PROJECT_HUE = "#2c4468"
            WORKING_HUE = "#2b5a4c"
            PUBLISH_HUE = "#2f5a32"
            NEUTRAL_HUE = "#333333"
            PUBLISH_ACCENT = "#6e4a12"  # amber — the heavy publish action

            # Group 1: Project & Scene
            self.btn_switch = self.create_button(
                "Browse Shots", "ramshot.png", self.on_switch_shot, PROJECT_HUE,
                tooltip="Jump to another shot in this project, or create a new scene from its plate.")
            layout.addWidget(self.btn_switch)
            self.btn_import = self.create_button(
                "Import Footage", "ramimport.png", self.on_import, PROJECT_HUE,
                tooltip="Load a published plate (image sequence or movie) into the scene as a new shot.")
            layout.addWidget(self.btn_import)
            self.btn_sync = self.create_button(
                "Sync Project Settings", "ramsetupscene.png", self.on_sync, PROJECT_HUE,
                tooltip="Set the scene resolution, FPS, pixel aspect and frame range from the Ramses project / shot.")
            layout.addWidget(self.btn_sync)

            layout.addSpacing(8)

            # Group 2: Working
            self.btn_save = self.create_button(
                "Save", "ramsave.png", self.on_save, WORKING_HUE,
                tooltip="Save the current working file (overwrites the unversioned working .sni).",
                prominent=True)  # highest-frequency action
            layout.addWidget(self.btn_save)
            self.btn_incremental = self.create_button(
                "Save New Version", "ramsaveincremental.png", self.on_incremental, WORKING_HUE,
                tooltip="Archive a new numbered version into _versions (e.g. v001 -> v002).")
            layout.addWidget(self.btn_incremental)
            self.btn_comment = self.create_button(
                "Save with Note", "ramcomment.png", self.on_comment, WORKING_HUE,
                tooltip="Save the scene and attach a descriptive note to the version in the Ramses database.")
            layout.addWidget(self.btn_comment)
            self.btn_retrieve = self.create_button(
                "Version History / Restore", "ramretrieve.png", self.on_retrieve, WORKING_HUE,
                tooltip="Browse and restore a previous version of this scene.")
            layout.addWidget(self.btn_retrieve)
            self.btn_save_as = self.create_button(
                "Save As / Create...", "ramsave.png", self.on_save_as, WORKING_HUE,
                tooltip="Save as a new item or step in the pipeline, or create a new scene.")
            layout.addWidget(self.btn_save_as)

            layout.addSpacing(8)

            # Group 3: Publish
            self.btn_preview = self.create_button(
                "Save Preview", "rampreview.png", self.on_preview, PUBLISH_HUE,
                tooltip="Render the tracking overlay to the shot's _preview folder for supervisor review.")
            layout.addWidget(self.btn_preview)
            self.btn_open_preview = self.create_button(
                "Open Preview", "ramopen.png", self.on_open_preview, PUBLISH_HUE,
                tooltip="Open the most recent preview for this shot in your default viewer.")
            layout.addWidget(self.btn_open_preview)
            self.btn_export = self.create_button(
                "Export to Pipeline", "rampublishsettings.png", self.on_export, PUBLISH_ACCENT,
                tooltip="Export the tracking data (Fusion comp by default) to the step's _published folder, where Ramses-Fusion picks it up.",
                prominent=True)  # heaviest action
            layout.addWidget(self.btn_export)
            self.btn_status = self.create_button(
                "Update Status", "ramstatus.png", self.on_status, PUBLISH_HUE,
                tooltip="Set the shot's state, completion ratio and a comment in the Ramses database.")
            layout.addWidget(self.btn_status)

            layout.addSpacing(8)

            # Group 4: Settings
            self.btn_update = self.create_button(
                "Check for Update", "ramupdate.png", self.on_check_update, NEUTRAL_HUE,
                tooltip="Check whether a newer version of the Ramses SynthEyes plugin is available.")
            layout.addWidget(self.btn_update)

            layout.addStretch()

            # Inline status line — non-blocking feedback that replaces the modal
            # popups. Coloured by kind via _set_status (ok/warn/error/info).
            self.status_line = qw.QLabel("")
            self.status_line.setWordWrap(True)
            self.status_line.setStyleSheet(
                "QLabel { color: #888888; font-size: 11px; padding: 2px 4px; }"
            )
            layout.addWidget(self.status_line)

            # Footer version label
            self.btn_about = qw.QPushButton("Ramses v" + self.host.version)
            self.btn_about.setFlat(True)
            self.btn_about.setStyleSheet("QPushButton { color: #555; font-size: 10px; background: transparent; border: none; }")
            self.btn_about.clicked.connect(self.on_about)
            layout.addWidget(self.btn_about)

        def create_button(self, text, icon_name, callback, accent_color=None,
                           tooltip="", prominent=False):
            """Builds a left-aligned icon+text button.

            prominent makes the button taller (36px) and semibold — used to give
            the highest-frequency action (Save) and the heaviest one (Export to
            Pipeline) more visual weight than the routine buttons around them.
            """
            btn = qw.QPushButton(" " + text)
            height = 36 if prominent else 30
            btn.setMinimumHeight(height)
            btn.setMaximumHeight(height)
            if tooltip:
                btn.setToolTip(tooltip)
            icon_path = os.path.join(script_dir, "icons", icon_name)
            if os.path.exists(icon_path):
                btn.setIcon(qg.QIcon(icon_path))
                btn.setIconSize(qc.QSize(16, 16))
            btn.clicked.connect(callback)

            weight_css = "font-weight: 600;" if prominent else ""
            if accent_color:
                h = accent_color.lstrip("#")
                hr, hg, hb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                hover   = "#%02x%02x%02x" % (min(255, hr+15), min(255, hg+15), min(255, hb+15))
                pressed = "#%02x%02x%02x" % (max(0, hr-10),   max(0, hg-10),   max(0, hb-10))
                ss = (
                    f"QPushButton {{ text-align: left; padding-left: 12px; {weight_css}"
                    f" border: 1px solid #222; border-radius: 3px; background-color: {accent_color}; }}"
                    f"QPushButton:hover {{ background-color: {hover}; }}"
                    f"QPushButton:pressed {{ background-color: {pressed}; }}"
                    "QPushButton:disabled { background-color: #222; color: #555; border: 1px solid #1a1a1a; }"
                )
            else:
                ss = (
                    f"QPushButton {{ text-align: left; padding-left: 12px; {weight_css}"
                    " border: 1px solid #222; border-radius: 3px; }"
                )
            btn.setStyleSheet(ss)
            return btn

        _STATUS_COLORS = {
            "ok": "#6ab04c",
            "warn": "#e1b12c",
            "error": "#eb4d4b",
            "info": "#888888",
        }

        def _set_status(self, text: str, kind: str = "info") -> None:
            """Shows a one-line, non-blocking status message under the buttons.

            kind is one of ok / warn / error / info and selects the colour.
            """
            line = getattr(self, "status_line", None)
            if line is None:
                return
            color = self._STATUS_COLORS.get(kind, self._STATUS_COLORS["info"])
            line.setStyleSheet(
                f"QLabel {{ color: {color}; font-size: 11px; padding: 2px 4px; }}"
            )
            line.setText(text)

        def _poll_refresh(self):
            """Timer tick: refresh the header only when the .sni path changed.

            Kept deliberately cheap — a single SNIFileName() read per tick — so
            it can run continuously without hammering the daemon. A full refresh
            (which re-reads status) happens on focus-in via changeEvent.
            """
            try:
                path = self.host.currentFilePath()
            except Exception:
                return  # listener hiccup — try again next tick
            if path != self._last_poll_path:
                self._last_poll_path = path
                self.refresh_context()

        def changeEvent(self, event):
            """Refresh when the panel regains focus (picks up external edits)."""
            try:
                if event.type() == qc.QEvent.ActivationChange and self.isActiveWindow():
                    # refresh_context() re-reads status and re-seeds the poll baseline.
                    self.refresh_context()
            except Exception:
                pass
            super(RamsesSyntheyesApp, self).changeEvent(event)

        def refresh_context(self):
            """Updates the context label and button states based on current file."""
            # Cache by (filePath, pending_uuid) — file path alone misses the case
            # where a new shot is loaded into an unsaved (path == "") scene, because
            # the path never changes even though the pending identity has.
            current_path = self.host.currentFilePath()
            # Keep the poll baseline in sync so a refresh triggered here (by a
            # handler or focus-in) doesn't make the next timer tick re-fire.
            self._last_poll_path = current_path
            pending_uuid = None
            if not current_path:
                pending = getattr(self.host, "_pending_new_shot_item", None)
                if pending:
                    try:
                        pending_uuid = str(pending.uuid())
                    except Exception:
                        pass

            if (current_path != self._context_cache["filePath"] or
                    pending_uuid != self._context_cache["_pending_uuid"]):
                self._context_cache["filePath"] = current_path
                self._context_cache["_pending_uuid"] = pending_uuid
                # currentContext() does one scene-notes parse for both values
                # instead of two separate parses from currentItem() + currentStep().
                item, step = self.host.currentContext()
                self._context_cache["item"] = item
                self._context_cache["step"] = step

            item = self._context_cache["item"]
            step = self._context_cache["step"]
            in_pipeline = bool(item and item.uuid() and step)

            if in_pipeline:
                project = item.project()
                project_name = (project.name() if project else item.projectShortName()).upper()
                item_name = item.shortName()

                # Sequence prefix (for shots only)
                seq_prefix = ""
                try:
                    if item.itemType() == ram.ItemType.SHOT:
                        seq = item.sequence()
                        if seq:
                            seq_prefix = f"<font color='#666'><b>{seq.shortName()}</b> | </font>"
                except Exception:
                    pass

                # Step with color
                step_name = "No Step"
                if step:
                    step_color = step.colorName()
                    step_name = f"<font color='{step_color}'>{step.name()}</font>"

                # State with color + Priority suffix
                state_label = ""
                priority_suffix = ""
                try:
                    status = self.host.currentStatus()
                    if status:
                        prio = int(status.get("priority", 0))
                        if prio == 1:
                            priority_suffix = " <font color='#ffcc00'>!</font>"
                        elif prio == 2:
                            priority_suffix = " <font color='#ff8800'>!!</font>"
                        elif prio >= 3:
                            priority_suffix = " <font color='#ff0000'>!!!</font>"
                        if status.state():
                            state = status.state()
                            state_color = state.colorName()
                            state_label = f" <font color='#555'>|</font> <font color='{state_color}'><b>{state.shortName()}</b></font>"
                except Exception:
                    pass

                html = (
                    f"<font color='#777' size='3'>{project_name}</font><br>"
                    f"{seq_prefix}<font color='#FFF' size='5'><b>{item_name}</b>{priority_suffix}</font><br>"
                    f"<font size='3'>{step_name}{state_label}</font>"
                )
                self.context_label.setText(html)
            else:
                path = self.host.currentFilePath()
                if path:
                    self.context_label.setText("<font color='#cc9900'>External Scene</font><br><font color='#777'>Not in a Ramses Project</font>")
                else:
                    self.context_label.setText("<font color='#cc9900'>No Active Scene</font>")

            # Buttons that require a pipeline context (known item + step)
            for btn in (self.btn_save, self.btn_incremental, self.btn_comment,
                        self.btn_retrieve, self.btn_sync, self.btn_preview,
                        self.btn_open_preview, self.btn_export, self.btn_status):
                btn.setEnabled(in_pipeline)

            # Save As / Create is always available (used to enter the pipeline)
            # Browse Shots, Check for Update, About are always available

        # --- Handlers ---

        def on_import(self):
            """Import published footage (image sequence or movie) from a previous step."""
            if self.host.importItem():
                self.refresh_context()
                self._set_status("✓ Imported footage into the scene.", "ok")

        def on_sync(self):
            """Manually sync scene settings."""
            if self.host.setupCurrentFile():
                self._set_status("✓ Scene settings synced (resolution, FPS, range).", "ok")
            else:
                self._set_status("Could not sync — make sure a Ramses shot is active.", "warn")

        def on_preview(self):
            """Render and save a preview sequence (no .comp export)."""
            try:
                # savePreview() returns False only when the scene isn't saved yet;
                # None (falsy) is the normal success return.
                if self.host.savePreview() is False:
                    self._set_status("Save the scene before creating a preview.", "warn")
                else:
                    self._set_status(f"✓ Preview saved · {time.strftime('%H:%M')}", "ok")
            except Exception as e:
                self.host._log(f"Preview failed: {e}", ram.LogLevel.Critical)
                self._set_status("Preview failed — see the SynthEyes console.", "error")
            self.refresh_context()

        def on_open_preview(self):
            """Open the most recent preview for this shot in the default viewer."""
            try:
                preview_path = self.host.resolvePreviewPath()
            except Exception as e:
                self.host._log(f"Could not resolve preview path: {e}", ram.LogLevel.Critical)
                self._set_status("Could not resolve the preview path.", "error")
                return
            if not preview_path or not os.path.exists(preview_path):
                self._set_status("No preview yet — use Save Preview first.", "warn")
                return
            # QDesktopServices picks the OS default handler for the file type.
            qg.QDesktopServices.openUrl(qc.QUrl.fromLocalFile(preview_path))
            self._set_status("Opened preview in the default viewer.", "ok")

        def on_export(self):
            """Export tracking data via the Ramses publish lifecycle."""
            if self.host.publish(forceShowPublishUI=True):
                self._set_status(f"✓ Exported to pipeline · {time.strftime('%H:%M')}", "ok")
            else:
                self._set_status("Export did not complete — see the SynthEyes console.", "warn")
            self.refresh_context()

        def on_open(self):
            if self.host.open():
                self.refresh_context()

        def on_save(self):
            if self.host.save():
                self._set_status(f"✓ Saved · {time.strftime('%H:%M')}", "ok")
            else:
                self._set_status("Save failed — see the SynthEyes console.", "error")
            self.refresh_context()

        def on_incremental(self):
            if self.host.save(incremental=True):
                self._set_status(f"✓ Saved new version · {time.strftime('%H:%M')}", "ok")
            else:
                self._set_status("Save failed — see the SynthEyes console.", "error")
            self.refresh_context()

        def on_comment(self):
            """Save the scene and attach a note to the current version."""
            host = self.host
            status = host.currentStatus()
            current_note = status.comment() if status else ""
            state = status.state() if status else None
            current_version = host.currentVersion()

            dialog = RamCommentDialog(current_version, current_note)
            dialog.setWindowFlags(dialog.windowFlags() | qc.Qt.WindowStaysOnTopHint)
            dialog.raise_()
            dialog.activateWindow()
            if not _exec_compat(dialog):
                return

            new_note = dialog.comment()
            if new_note == current_note:
                self._set_status("Note unchanged — nothing saved.", "info")
                return

            # Non-incremental save-over that records the note against the current
            # version, tagged with the existing state (mirrors Ramses-Fusion).
            if host.save(comment=new_note, state=state):
                if status:
                    status.setComment(new_note)
                self.refresh_context()
                self._set_status(f"✓ Saved with note · {time.strftime('%H:%M')}", "ok")
            else:
                self._set_status("Save failed — see the SynthEyes console.", "error")

        def on_retrieve(self):
            if self.host.restoreVersion():
                self.refresh_context()
                self._set_status("✓ Version restored.", "ok")

        def on_save_as(self):
            if self.host.saveAs():
                self.refresh_context()
                self._set_status("✓ Saved into the pipeline.", "ok")

        def on_switch_shot(self):
            if self.host.open():
                self.refresh_context()
                self._set_status("✓ Shot loaded.", "ok")

        def on_status(self):
            if self.host.updateStatus():
                # Invalidate cached status data so the header shows the new state.
                # DAEMON.setData() doesn't flush the getData() 2-second cache,
                # so we clear it manually to force a fresh fetch.
                try:
                    ram.RamDaemonInterface.instance()._cache.pop('data', None)
                except AttributeError:
                    pass  # SDK version without this internal cache — no-op
                self._set_status("✓ Status updated.", "ok")
            self.refresh_context()

        def on_check_update(self):
            """Handler for 'Check for Update' button."""
            update_info = self.host.checkAddOnUpdate()
            if update_info:
                # Force foreground
                dialog = RamUpdateDialog(update_info, self.host.name, self.host.version)
                dialog.setWindowFlags(dialog.windowFlags() | qc.Qt.WindowStaysOnTopHint)
                dialog.raise_()
                dialog.activateWindow()
                _exec_compat(dialog)

        def on_about(self):
            dialog = RamAboutDialog()
            _exec_compat(dialog)

    # --- SyPy connection ---
    print("Connecting to SynthEyes Listener...")
    hlev = SyPy.SyLevel()
    if not hlev.OpenExisting():
        print("FAILED to connect to SynthEyes.")
        print("Please ensure 'Activate Listener' is checked in SynthEyes Preferences > System.")
        return

    print("Success. Launching UI...")
    app = qw.QApplication.instance()
    if not app:
        app = qw.QApplication(sys.argv)

    # Warn if the Ramses daemon is unreachable — do this before creating the
    # window so the dialog appears before the panel, not buried behind it.
    try:
        daemon = ram.RamDaemonInterface.instance()
        if not daemon.online():
            qw.QMessageBox.warning(
                None,
                "Ramses Not Connected",
                "Could not connect to the Ramses daemon.\n\n"
                "Pipeline features (item tracking, status updates, publish) "
                "will not be available until the Ramses application is running.\n\n"
                "Start the Ramses application, then click 'Browse Shots' to reconnect.",
            )
    except Exception:
        pass  # Best-effort check; don't block startup

    main_win = RamsesSyntheyesApp(hlev)
    # Keep the panel visible when SynthEyes is focused — without this the
    # window disappears behind SynthEyes since it runs in a separate process.
    main_win.setWindowFlags(main_win.windowFlags() | qc.Qt.WindowStaysOnTopHint)
    main_win.show()

    _exec_compat(app)

    _release_instance_lock()

if __name__ == "__main__":
    try:
        run_app()
    except Exception:
        print("\n" + "!"*60)
        print("RAMSES PLUGIN FATAL ERROR:")
        traceback.print_exc()
        print("!"*60)
        input("\nPress ENTER to close this console...")
