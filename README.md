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
- **Automated Saving**: Handles versioning and standard naming conventions (one unversioned working file, state-named archives in `_versions/`).
- **Plate Integration**: "New Shot from Plate" finds the latest published plate and sets up the scene with the correct resolution, FPS, and footage path. Plate steps are looked up by short name via the `plateStepNames` setting (default: `Plate`, `Ingest`, `Footage` — shared with Ramses-Fusion).
- **Publishing**: Exports tracking data to the step's `_published` folder — by default as a **Fusion Composition** (`.comp`) that Ramses-Fusion merges directly into the artist's flow. The export type is configurable per step.
- **Previews**: Renders the tracking overlay via SynthEyes' *Save Sequence* into the shot's `_preview` folder (format configurable via step publish settings).
- **Status Updates**: Update the Ramses database status directly from within SynthEyes.
- **Metadata Persistence**: Uses SynthEyes Notes to store project identity, allowing files to be recognized even if moved.
