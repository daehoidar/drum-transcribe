#!/usr/bin/env python
"""YouTube drum video → MIDI → MusicXML → score sheet (PDF + PNG).

Usage:
    python transcribe.py <youtube_url>            # single
    python transcribe.py --file urls.txt          # batch

Requires:
    - yt-dlp, ffmpeg                       (system, e.g. `brew install yt-dlp ffmpeg`)
    - MuseScore 4 with CLI `mscore`        (`brew install --cask musescore`)
    - Python deps in requirements.txt      (`pip install -r requirements.txt`)

Outputs in `output/<video_id>/`:
    audio.wav, transcription.mid, transcription.musicxml, score.pdf, score.png
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pretty_midi


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

ADTOF_THRESHOLDS = "0.22,0.24,0.32,0.22,0.30"
ADTOF_FPS = "100"

# Pre-rendering MIDI quantization. Snaps onsets to a grid so MuseScore's
# auto time-signature detection picks 4/4 instead of guessing (often wrongly).
# Side effect: kills 32nd-note clusters that arise from sustained cymbals/HH.
DEFAULT_BPM = 120.0
DEFAULT_GRID = 16  # 16th-note grid (set to 32 to preserve faster patterns)


def find_mscore() -> str:
    # 1. Explicit common paths
    for c in [
        "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
        "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
        shutil.which("mscore"),
        shutil.which("musescore"),
        shutil.which("MuseScore-4"),
        shutil.which("MuseScore-3"),
    ]:
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return c

    # 2. macOS: glob inside any MuseScore*.app bundle for the CLI binary
    for p in glob.glob("/Applications/MuseScore*.app/Contents/MacOS/*"):
        if not (Path(p).is_file() and os.access(p, os.X_OK)):
            continue
        name = Path(p).name.lower()
        if "score" in name or name == "mscore":
            return p

    return ""


def video_id_from_url(url: str) -> str:
    m = re.search(r"(?:youtu\.be/|v=|/embed/|/shorts/)([\w-]{11})", url)
    return m.group(1) if m else url.rstrip("/").split("/")[-1][:11]


def run(cmd, **kw):
    print(f"  $ {' '.join(map(str, cmd))}")
    subprocess.run(cmd, check=True, **kw)


def step_download(url: str, wav: Path) -> None:
    if wav.exists() and wav.stat().st_size > 0:
        print(f"  skip — exists: {wav.name}")
        return
    raw = wav.with_suffix(".raw.wav")
    run(["yt-dlp", "-x", "--audio-format", "wav", "--audio-quality", "0",
         "-o", str(wav.parent / "audio.%(ext)s"), url])
    # yt-dlp wrote to audio.wav with whatever sample rate. Re-encode to 44.1kHz mono for ADTOF.
    if wav.exists():
        wav.rename(raw)
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(raw), "-ar", "44100", "-ac", "1", str(wav)])
    raw.unlink(missing_ok=True)


def quantize_midi(mid: Path, bpm: float = DEFAULT_BPM, subdivision: int = DEFAULT_GRID) -> None:
    """Snap every onset to a rhythmic grid (default: 16th note at 120 BPM).

    Why: MuseScore's MIDI auto time-signature detection misfires (often picks
    3/4) on raw ADTOF onsets, which sit on a 10ms grid. Snapping to musical
    grid forces 4/4 detection and also collapses sustained-cymbal noise
    (the "콩나물" 32nd-note clusters) into clean 16th notes.
    """
    grid_sec = 60.0 / bpm / (subdivision / 4)
    pm = pretty_midi.PrettyMIDI(str(mid))
    for inst in pm.instruments:
        for n in inst.notes:
            snapped = round(n.start / grid_sec) * grid_sec
            n.start = snapped
            n.end = snapped + 0.05
    pm.write(str(mid))


def step_predict(wav: Path, mid: Path, thresholds: str = ADTOF_THRESHOLDS,
                 quantize: bool = True, grid: int = DEFAULT_GRID, bpm: float = DEFAULT_BPM) -> None:
    if mid.exists() and mid.stat().st_size > 0:
        print(f"  skip — exists: {mid.name}")
        return
    run(["adtof", "--audio", str(wav), "--out", str(mid),
         "--thresholds", thresholds, "--fps", ADTOF_FPS])
    # Mark each instrument as drum so MuseScore renders a percussion staff.
    pm = pretty_midi.PrettyMIDI(str(mid))
    for inst in pm.instruments:
        inst.is_drum = True
    pm.write(str(mid))
    if quantize:
        quantize_midi(mid, bpm=bpm, subdivision=grid)
        print(f"  + quantized onsets to 1/{grid} grid at {bpm} BPM")


def style_args(mscore_dir: Path) -> list[str]:
    style_path = ROOT / "style.mss"
    return ["--style", str(style_path)] if style_path.exists() else []


def add_system_breaks(musicxml: Path, measures_per_system: int = 4) -> None:
    """Force a predictable layout: strip every break MuseScore inserted on its
    own, then add <print new-system="yes"/> every N measures.

    Without the strip step, MuseScore's auto-inserted page/system breaks (often
    at odd measures like 78 or 83) leave sparse pages even after our breaks.
    """
    import xml.etree.ElementTree as ET

    ET.register_namespace("", "")
    tree = ET.parse(musicxml)
    root = tree.getroot()

    for part in root.findall("part"):
        measures = part.findall("measure")
        for i, m in enumerate(measures):
            # 1. Remove pre-existing system/page break attributes from any
            #    <print> element MuseScore inserted in this measure.
            for pr in m.findall("print"):
                for attr in ("new-system", "new-page"):
                    if attr in pr.attrib:
                        del pr.attrib[attr]

            # 2. Inject our own system break at every Nth measure (skip first).
            if i == 0 or i % measures_per_system != 0:
                continue
            existing = m.find("print")
            if existing is not None:
                existing.set("new-system", "yes")
            else:
                br = ET.Element("print", {"new-system": "yes"})
                m.insert(0, br)

    tree.write(musicxml, xml_declaration=True, encoding="UTF-8")


def step_musicxml(mscore: str, mid: Path, musicxml: Path, measures_per_system: int = 4) -> None:
    if musicxml.exists() and musicxml.stat().st_size > 0:
        print(f"  skip — exists: {musicxml.name}")
        return
    run([mscore, *style_args(ROOT), "-o", str(musicxml), str(mid)])
    add_system_breaks(musicxml, measures_per_system)
    print(f"  + inserted system breaks every {measures_per_system} measures")


def step_render(mscore: str, musicxml: Path, pdf: Path) -> None:
    if pdf.exists():
        print(f"  skip — exists: {pdf.name}")
        return
    run([mscore, *style_args(ROOT), "-o", str(pdf), str(musicxml)])


def transcribe(url: str, force: bool = False, thresholds: str = ADTOF_THRESHOLDS,
               quantize: bool = True, grid: int = DEFAULT_GRID, bpm: float = DEFAULT_BPM) -> None:
    vid = video_id_from_url(url)
    out = OUTPUT_DIR / vid
    out.mkdir(parents=True, exist_ok=True)

    wav = out / "audio.wav"
    mid = out / "transcription.mid"
    musicxml = out / "transcription.musicxml"
    pdf = out / "score.pdf"

    if force:
        for p in [wav, mid, musicxml, pdf]:
            p.unlink(missing_ok=True)
        # also clear stale per-page PNGs from previous runs
        for p in out.glob("score-*.png"):
            p.unlink(missing_ok=True)
        (out / "score.png").unlink(missing_ok=True)

    print(f"\n=== {vid} === ({url})")

    print("\n[1/4] Download → wav")
    step_download(url, wav)

    print(f"\n[2/4] Predict drum onsets (ADTOF-pytorch, thresholds={thresholds})")
    step_predict(wav, mid, thresholds=thresholds, quantize=quantize, grid=grid, bpm=bpm)

    mscore = find_mscore()
    if not mscore:
        print("\nERROR: MuseScore CLI not found.")
        print("  Install with:  brew install --cask musescore")
        sys.exit(1)

    print(f"\n[3/4] MIDI → MusicXML  ({mscore})")
    step_musicxml(mscore, mid, musicxml)

    print("\n[4/4] Render score (PDF)")
    step_render(mscore, musicxml, pdf)

    print(f"\n✓ Done — {out}")
    for f in [wav, mid, musicxml, pdf]:
        if f.exists():
            print(f"    {f.name:30s}  ({f.stat().st_size // 1024} KB)")


def main():
    ap = argparse.ArgumentParser(description="YouTube drum video → score sheet")
    ap.add_argument("url", nargs="?", help="single YouTube URL")
    ap.add_argument("--file", help="path to a text file with one URL per line (# for comments)")
    ap.add_argument("--force", action="store_true", help="re-run even if outputs exist")
    ap.add_argument(
        "--thresholds",
        default=ADTOF_THRESHOLDS,
        help=f"5 comma-separated peak-pick thresholds for [BD,SD,HH,TT,CY+RD]. "
             f"Lower → more recall (more notes). Default: {ADTOF_THRESHOLDS}",
    )
    ap.add_argument(
        "--no-quantize",
        action="store_true",
        help="skip MIDI grid quantization (raw onset times). May cause MuseScore "
             "to mis-detect time signature (e.g., 3/4 instead of 4/4).",
    )
    ap.add_argument("--grid", type=int, default=DEFAULT_GRID,
                    help=f"quantize subdivision (8/16/32). Default: {DEFAULT_GRID}")
    ap.add_argument("--bpm", type=float, default=DEFAULT_BPM,
                    help=f"BPM used for the quantize grid. Default: {DEFAULT_BPM}")
    args = ap.parse_args()

    urls = []
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if args.url:
        urls.append(args.url)

    if not urls:
        ap.print_help()
        sys.exit(1)

    for url in urls:
        try:
            transcribe(
                url,
                force=args.force,
                thresholds=args.thresholds,
                quantize=not args.no_quantize,
                grid=args.grid,
                bpm=args.bpm,
            )
        except subprocess.CalledProcessError as e:
            print(f"\n✗ FAILED ({url}): {e}")
        except Exception as e:
            print(f"\n✗ FAILED ({url}): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
