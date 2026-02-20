# Ramses-Syntheyes

Ramses integration for Boris FX SynthEyes.

## Installation

1. Copy the `Ramses-Syntheyes` folder to your SynthEyes user scripts directory.
2. Ensure you have a Python 3 environment installed with `PySide2` or `PySide6`.
3. In SynthEyes, go to `Edit > Preferences > System` and ensure the `Python executable` points to your Python 3 install.

## Usage

1. Run the script from the SynthEyes `Scripts` menu.
2. The plugin UI will appear.
3. Use **Switch Shot / Task** to browse your Ramses project.
4. If no SynthEyes file exists for a shot, you can create one.

## Features

- **Context Awareness**: Automatically identifies the Shot/Step from the file path or embedded metadata.
- **Automated Saving**: Handles versioning (`_v001`, `_v002`) and standard naming conventions.
- **Plate Integration**: Automatically sets up new scenes with the correct resolution, FPS, and footage path.
- **Status Updates**: Update the Ramses database status directly from within SynthEyes.
- **Metadata Persistence**: Uses SynthEyes Notes to store project identity, allowing files to be recognized even if moved.
