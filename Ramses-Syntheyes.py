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
    """Returns True if this is the first instance, False if one is already running."""
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)   # signal 0: raises OSError if process is gone
            print(f"Ramses SynthEyes plugin is already running (PID {pid}).")
            return False
        except (OSError, ValueError):
            pass  # stale lock from a crashed previous run
    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True

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
            possible_paths = [
                r"C:\Program Files\BorisFX\SynthEyes 2026",
                r"C:\Program Files\Boris FX\SynthEyes 2026",
                r"C:\Program Files\SynthEyes 2026",
                r"C:\Program Files\SynthEyes",
            ]
            
            # Try to infer from the script path (AppData version)
            # Script: ...\AppData\Local\BorisFX\SynthEyes 2026\scripts\...
            # App: C:\Program Files\BorisFX\SynthEyes 2026
            if "BorisFX" in script_dir:
                parts = script_dir.split(os.sep)
                try:
                    idx = parts.index("BorisFX")
                    app_name = parts[idx+1] # "SynthEyes 2026"
                    possible_paths.insert(0, os.path.join(r"C:\Program Files\BorisFX", app_name))
                except: pass

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
            self.hlev = hlev
            
            self.setWindowTitle("Ramses - SynthEyes")
            
            self.setup_ui()
            self.refresh_context()

        def setup_ui(self):
            """Builds the vertical toolbar UI with icons."""
            central_widget = qw.QWidget()
            self.setCentralWidget(central_widget)
            layout = qw.QVBoxLayout(central_widget)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(4)

            # Context Label
            self.context_label = qw.QLabel("No Active Shot")
            self.context_label.setStyleSheet("font-weight: bold; color: #CCC; background-color: #1e2228; padding: 8px; border: 1px solid #3a4048; border-radius: 4px;")
            self.context_label.setAlignment(qc.Qt.AlignCenter)
            self.context_label.setWordWrap(True)
            layout.addWidget(self.context_label)

            layout.addSpacing(5)

            # Group 1: Scene Setup
            self.btn_switch = self.create_button("Browse Shots", "ramshot.png", self.on_switch_shot)
            layout.addWidget(self.btn_switch)
            
            self.btn_sync = self.create_button("Sync Settings", "ramsetupscene.png", self.on_sync)
            layout.addWidget(self.btn_sync)

            layout.addSpacing(10)

            # Group 2: Saving
            self.btn_save = self.create_button("Save", "ramsave.png", self.on_save)
            layout.addWidget(self.btn_save)

            self.btn_incremental = self.create_button("Save New Version", "ramsaveincremental.png", self.on_incremental)
            layout.addWidget(self.btn_incremental)

            self.btn_save_as = self.create_button("Save As / Create...", "ramsave.png", self.on_save_as)
            layout.addWidget(self.btn_save_as)

            layout.addSpacing(10)

            # Group 3: Publish
            self.btn_export = self.create_button("Export to Pipeline", "rampreview.png", self.on_export)
            layout.addWidget(self.btn_export)

            self.btn_status = self.create_button("Update Status", "ramstatus.png", self.on_status)
            layout.addWidget(self.btn_status)

            self.btn_update = self.create_button("Check for Update", "ramupdate.png", self.on_check_update)
            layout.addWidget(self.btn_update)

            layout.addStretch()

            # Footer
            self.btn_about = qw.QPushButton("Ramses v" + self.settings.version)
            self.btn_about.setFlat(True)
            self.btn_about.setStyleSheet("color: #555; font-size: 10px;")
            self.btn_about.clicked.connect(self.on_about)
            layout.addWidget(self.btn_about)

        def create_button(self, text, icon_name, callback):
            btn = qw.QPushButton(" " + text)
            btn.setMinimumHeight(32)
            icon_path = os.path.join(script_dir, "icons", icon_name)
            if os.path.exists(icon_path):
                btn.setIcon(qg.QIcon(icon_path))
            btn.clicked.connect(callback)
            btn.setStyleSheet("QPushButton { text-align: left; padding-left: 8px; }")
            return btn

        def refresh_header(self):
            """Unified refresh method."""
            self.refresh_context()

        def refresh_context(self):
            """Updates the context label based on current file."""
            item = self.host.currentItem()
            step = self.host.currentStep()
            
            if item and step:
                project = item.project()
                project_name = project.shortName() if project else "UNK"
                self.context_label.setText(f"<font color='#777' size='2'>{project_name}</font><br><font color='#FFF' size='4'>{item.shortName()}</font><br><font color='#aaa' size='2'>{step.shortName()}</font>")
            else:
                self.context_label.setText("<font color='#cc9900'>External Scene</font><br><font color='#777'>Not in Pipeline</font>")

        # --- Handlers ---

        def on_sync(self):
            """Manually sync scene settings."""
            self.host.setupCurrentFile()
            qw.QMessageBox.information(self, "Ramses", "Scene settings synced with database.")

        def on_export(self):
            """Export tracking data."""
            path = self.host.exportScene()
            if path:
                qw.QMessageBox.information(self, "Export Successful", f"Tracking data exported to:\n{path}")

        def on_open(self):
            if self.host.open():
                self.refresh_context()

        def on_save(self):
            self.host.save()
            self.refresh_context()

        def on_incremental(self):
            self.host.save(incremental=True)
            self.refresh_context()

        def on_save_as(self):
            if self.host.saveAs():
                self.refresh_context()

        def on_switch_shot(self):
            if self.host.open():
                self.refresh_context()
            self.refresh_context()

        def on_status(self):
            if self.host.updateStatus():
                self.refresh_header()
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

    main_win = RamsesSyntheyesApp(hlev)
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
