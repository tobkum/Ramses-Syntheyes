# -*- coding: utf-8 -*-
"""Read-only probe: how SynthEyes actually numbers shot frames and the play range.

Run this from inside SynthEyes (Script menu / Run Script File) with a scene
open that was set up by Ramses-Syntheyes -- ideally right after "New Shot from
Plate" or an Import, before touching Start/End by hand.

This confirms, against a live SynthEyes, the four facts the frame-range fix in
`SynthEyesHost._syncPlayRange` / `._syncShotLength` rests on:

  1. shot.start / shot.stop are the shot's real range, and the plugin's play
     range now matches them.
  2. There is no `frames` shot attribute -- the old code read one, which is
     why its "Smart Frame Range" fallback never fired.
  3. SynthEyes clamps an out-of-range playback range, and a fixed
     SetAnimStart-then-SetAnimEnd order loses one of the two writes.
  4. What the plate's own first frame number is (frameFirstOffset) and whether
     the UI is matching it (matchFrameNumbers / frameUIOffset).

Only the playback range is written, and it is restored to its original value
before exiting. Nothing else in the scene is touched. Do not save afterwards.

Results are printed and written to a report file next to this script.
"""

import os
import sys
import datetime

# --- Locate SyPy3 (same search order as Ramses-Syntheyes.py) ----------------
try:
    import SyPy3 as SyPy
except ImportError:
    _found = False
    for _p in (
        r"C:\Program Files\BorisFX\SynthEyes 2026",
        r"C:\Program Files\Andersson Technologies LLC\SynthEyes",
    ) + tuple(sys.path):
        if os.path.isdir(os.path.join(_p, "SyPy3")):
            sys.path.insert(0, _p)
            try:
                import SyPy3 as SyPy
                _found = True
                break
            except ImportError:
                sys.path.pop(0)
    if not _found:
        raise SystemExit("SyPy3 not found -- cannot connect to SynthEyes.")

LINES = []


def out(msg=""):
    LINES.append(str(msg))
    print(msg)


def probe(label, fn):
    """Reads one value, reporting the exception instead of dying on it."""
    try:
        v = fn()
    except Exception as e:
        out("  {:<22} !! {}: {}".format(label, type(e).__name__, e))
        return None
    out("  {:<22} {!r:<18} ({})".format(label, v, type(v).__name__))
    return v


def main():
    hlev = SyPy.SyLevel()
    if not hlev.OpenExisting():
        raise SystemExit(
            "Could not connect to SynthEyes. Run this from the SynthEyes "
            "Script menu so it gets -port/-pin in argv."
        )

    out("Ramses-Syntheyes frame-range probe")
    out("Run at {}".format(datetime.datetime.now().isoformat(timespec="seconds")))
    out("SynthEyes version: {}".format(hlev.Version()))
    out("Scene file: {!r}".format(hlev.SNIFileName()))
    out()

    # ---------------------------------------------------------------- scene
    out("== Scene ==")
    anim_start = probe("AnimStart()", hlev.AnimStart)
    anim_end = probe("AnimEnd()", hlev.AnimEnd)
    probe("Frame()", hlev.Frame)
    scene = hlev.Scene()
    # Scene.startFrame is the "starting frame number" PREFERENCE that the
    # exporters bias by; it is NOT the timebar play range.
    probe("Scene.startFrame", lambda: scene.Get("startFrame"))
    out()

    # ---------------------------------------------------------------- shots
    shots = hlev.Shots() or []
    out("== Shots ({}) ==".format(len(shots)))

    first = None
    for i, shot in enumerate(shots):
        out()
        out("-- Shot[{}] ------------------------------------------".format(i))
        probe("nm", lambda: shot.Get("nm"))
        probe("imageName", lambda: shot.Get("imageName"))
        out()
        out("  range (internal, 0-based?):")
        s = probe("start", lambda: shot.Get("start"))
        e = probe("stop", lambda: shot.Get("stop"))
        probe("length", lambda: shot.Get("length"))
        probe("actualLength", lambda: shot.Get("actualLength"))
        # Should equal actualLength: _syncShotLength() sets it, per the
        # manual's "be sure to set this, based on actualLength".
        probe("frameCount", lambda: shot.Get("frameCount"))
        out()
        out("  plate numbering:")
        probe("frameFirstOffset", lambda: shot.Get("frameFirstOffset"))
        probe("matchFrameNumbers", lambda: shot.Get("matchFrameNumbers"))
        probe("frameMatchOffset", lambda: shot.Get("frameMatchOffset"))
        probe("frameUIOffset", lambda: shot.Get("frameUIOffset"))
        out()
        out("  media:")
        probe("rate", lambda: shot.Get("rate"))
        probe("width", lambda: shot.Get("width"))
        probe("height", lambda: shot.Get("height"))
        probe("pixasp", lambda: shot.Get("pixasp"))

        if first is None:
            first = (shot, s, e)

    # ------------------------------------------- clamp / ordering experiment
    out()
    out("== QUESTION 3: clamping and call order ==")
    if first is None or anim_start is None or anim_end is None:
        out("  Skipped: no shot, or the play range could not be read.")
    else:
        shot, s, e = first
        length = shot.Get("actualLength") or shot.Get("length") or 0
        try:
            length = int(length)
        except Exception:
            length = 0
        if length <= 0:
            out("  Skipped: could not determine the shot length.")
        else:
            # What _setupCurrentFile used to do: a 1001-based range, Start
            # first then End. Kept here to demonstrate the failure.
            target_start = 1001
            target_end = 1001 + length - 1
            out("  Shot occupies {}..{} internally.".format(s, e))
            out("  Play range before: {}..{}".format(anim_start, anim_end))
            out()
            out("  (a) plugin's current order -- SetAnimStart({}) then "
                "SetAnimEnd({}):".format(target_start, target_end))
            hlev.SetAnimStart(target_start)
            got_s = hlev.AnimStart()
            hlev.SetAnimEnd(target_end)
            out("      after SetAnimStart: {} (asked {})".format(got_s, target_start))
            out("      final range:        {}..{}".format(
                hlev.AnimStart(), hlev.AnimEnd()))
            out()
            out("  (b) reverse order -- SetAnimEnd first, then SetAnimStart:")
            hlev.SetAnimEnd(target_end)
            got_e = hlev.AnimEnd()
            hlev.SetAnimStart(target_start)
            out("      after SetAnimEnd:   {} (asked {})".format(got_e, target_end))
            out("      final range:        {}..{}".format(
                hlev.AnimStart(), hlev.AnimEnd()))
            out()
            out("  (c) the shot's own range -- what SynthEyes' own importers use:")
            hlev.SetAnimEnd(e)
            hlev.SetAnimStart(s)
            out("      final range:        {}..{}".format(
                hlev.AnimStart(), hlev.AnimEnd()))

            # ------------------------------------------------------- restore
            out()
            if anim_start <= anim_end:
                hlev.SetAnimEnd(anim_end)
                hlev.SetAnimStart(anim_start)
            else:
                hlev.SetAnimStart(anim_start)
                hlev.SetAnimEnd(anim_end)
            out("  Restored play range to {}..{} (now reads {}..{}).".format(
                anim_start, anim_end, hlev.AnimStart(), hlev.AnimEnd()))

    # --------------------------------------------------- unknown-attr probe
    # LAST on purpose: an unknown Sizzle attribute name may make the listener
    # emit an error, which can desync the request/response stream. Everything
    # that matters has already been read and restored by this point.
    out()
    out("== QUESTION 2: does the shot attribute `frames` exist? ==")
    out("  The old code read `shot.frames`. The Sizzle reference lists")
    out("  start/stop/length/actualLength/frameCount but no `frames`, so it")
    out("  should come back None -- confirming that read was always dead.")
    if first is not None:
        probe("frames  (expect None)", lambda: first[0].Get("frames"))
    else:
        out("  Skipped: no shot.")

    out()
    out("Done. Nothing but the playback range was written, and it was put "
        "back. Please do not save this scene.")

    report = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "probe_frame_range_report.txt")
    try:
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(LINES) + "\n")
        print("\nReport written to: " + report)
    except Exception as e:
        print("\nCould not write the report file: {}".format(e))


main()
