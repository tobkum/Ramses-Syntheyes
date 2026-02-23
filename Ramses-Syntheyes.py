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

def run_app():
    if not _acquire_instance_lock():
        return

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

            # Cache for currentItem/currentStep — keyed by file path so we
            # only call the daemon when the open scene actually changes.
            self._context_cache = {"filePath": None, "item": None, "step": None}

            self.setWindowTitle("Ramses - SynthEyes")
            self.setStyleSheet(
                "QMainWindow { background-color: #1a1a1a; }"
                "QWidget { background-color: #1a1a1a; color: #cccccc; }"
            )

            self.setup_ui()
            self.refresh_context()

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

            # Group 1: Project & Scene  (blue — #2a3442)
            self.btn_switch = self.create_button("Browse Shots", "ramshot.png", self.on_switch_shot, "#2a3442")
            layout.addWidget(self.btn_switch)
            self.btn_import = self.create_button("Import Footage", "ramimport.png", self.on_import, "#2a3442")
            layout.addWidget(self.btn_import)
            self.btn_sync = self.create_button("Sync Settings", "ramsetupscene.png", self.on_sync, "#2a3442")
            layout.addWidget(self.btn_sync)

            layout.addSpacing(8)

            # Group 2: Working (teal — #2a423d)
            self.btn_save = self.create_button("Save", "ramsave.png", self.on_save, "#2a423d")
            layout.addWidget(self.btn_save)
            self.btn_incremental = self.create_button("Save New Version", "ramsaveincremental.png", self.on_incremental, "#2a423d")
            layout.addWidget(self.btn_incremental)
            self.btn_retrieve = self.create_button("Version History / Restore", "ramretrieve.png", self.on_retrieve, "#2a423d")
            layout.addWidget(self.btn_retrieve)
            self.btn_save_as = self.create_button("Save As / Create...", "ramsave.png", self.on_save_as, "#2a423d")
            layout.addWidget(self.btn_save_as)

            layout.addSpacing(8)

            # Group 3: Publish (green — #2a422a)
            self.btn_preview = self.create_button("Save Preview", "rampreview.png", self.on_preview, "#2a422a")
            layout.addWidget(self.btn_preview)
            self.btn_export = self.create_button("Export to Pipeline", "rampublishsettings.png", self.on_export, "#2a422a")
            layout.addWidget(self.btn_export)
            self.btn_status = self.create_button("Update Status", "ramstatus.png", self.on_status, "#2a422a")
            layout.addWidget(self.btn_status)

            layout.addSpacing(8)

            # Group 4: Settings (neutral — #333333)
            self.btn_update = self.create_button("Check for Update", "ramupdate.png", self.on_check_update, "#333333")
            layout.addWidget(self.btn_update)

            layout.addStretch()

            # Footer version label
            self.btn_about = qw.QPushButton("Ramses v" + self.host.version)
            self.btn_about.setFlat(True)
            self.btn_about.setStyleSheet("QPushButton { color: #555; font-size: 10px; background: transparent; border: none; }")
            self.btn_about.clicked.connect(self.on_about)
            layout.addWidget(self.btn_about)

        def create_button(self, text, icon_name, callback, accent_color=None):
            btn = qw.QPushButton(" " + text)
            btn.setMinimumHeight(30)
            btn.setMaximumHeight(30)
            icon_path = os.path.join(script_dir, "icons", icon_name)
            if os.path.exists(icon_path):
                btn.setIcon(qg.QIcon(icon_path))
                btn.setIconSize(qc.QSize(16, 16))
            btn.clicked.connect(callback)

            if accent_color:
                h = accent_color.lstrip("#")
                hr, hg, hb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                hover   = "#%02x%02x%02x" % (min(255, hr+15), min(255, hg+15), min(255, hb+15))
                pressed = "#%02x%02x%02x" % (max(0, hr-10),   max(0, hg-10),   max(0, hb-10))
                ss = (
                    f"QPushButton {{ text-align: left; padding-left: 12px;"
                    f" border: 1px solid #222; border-radius: 3px; background-color: {accent_color}; }}"
                    f"QPushButton:hover {{ background-color: {hover}; }}"
                    f"QPushButton:pressed {{ background-color: {pressed}; }}"
                    "QPushButton:disabled { background-color: #222; color: #555; border: 1px solid #1a1a1a; }"
                )
            else:
                ss = "QPushButton { text-align: left; padding-left: 12px; border: 1px solid #222; border-radius: 3px; }"
            btn.setStyleSheet(ss)
            return btn

        def refresh_context(self):
            """Updates the context label and button states based on current file."""
            # currentItem() and currentStep() may call the Ramses daemon.
            # Cache them by file path — only re-query when the scene changes.
            current_path = self.host.currentFilePath()
            if current_path != self._context_cache["filePath"]:
                self._context_cache["filePath"] = current_path
                self._context_cache["item"] = self.host.currentItem()
                self._context_cache["step"] = self.host.currentStep()

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
            for btn in (self.btn_save, self.btn_incremental, self.btn_retrieve,
                        self.btn_sync, self.btn_preview, self.btn_export, self.btn_status):
                btn.setEnabled(in_pipeline)

            # Save As / Create is always available (used to enter the pipeline)
            # Browse Shots, Check for Update, About are always available

        # --- Handlers ---

        def on_import(self):
            """Import published footage (image sequence or movie) from a previous step."""
            if self.host.importItem():
                self.refresh_context()

        def on_sync(self):
            """Manually sync scene settings."""
            if self.host.setupCurrentFile():
                qw.QMessageBox.information(self, "Ramses", "Scene settings synced with database.")
            else:
                qw.QMessageBox.warning(self, "Ramses", "Could not sync scene settings.\nMake sure a Ramses shot is active.")

        def on_preview(self):
            """Render and save a preview sequence (no .comp export)."""
            try:
                self.host.savePreview()
            except Exception as e:
                qw.QMessageBox.critical(self, "Preview Failed",
                    f"Could not save preview:\n{e}")
            self.refresh_context()

        def on_export(self):
            """Export tracking data via the Ramses publish lifecycle."""
            self.host.publish(forceShowPublishUI=True)
            self.refresh_context()

        def on_open(self):
            if self.host.open():
                self.refresh_context()

        def on_save(self):
            self.host.save()
            self.refresh_context()

        def on_incremental(self):
            self.host.save(incremental=True)
            self.refresh_context()

        def on_retrieve(self):
            if self.host.restoreVersion():
                self.refresh_context()

        def on_save_as(self):
            if self.host.saveAs():
                self.refresh_context()

        def on_switch_shot(self):
            if self.host.open():
                self.refresh_context()

        def on_status(self):
            if self.host.updateStatus():
                # Invalidate cached status data so the header shows the new state.
                # DAEMON.setData() doesn't flush the getData() 2-second cache,
                # so we clear it manually to force a fresh fetch.
                ram.RamDaemonInterface.instance()._cache.pop('data', None)
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
                if getattr(dialog, 'exec', None):
                    dialog.exec()
                else:
                    dialog.exec_()

        def on_about(self):
            dialog = RamAboutDialog()
            if getattr(dialog, 'exec', None):
                dialog.exec()
            else:
                dialog.exec_()

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

    if getattr(app, 'exec', None):
        app.exec()
    else:
        app.exec_()

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
