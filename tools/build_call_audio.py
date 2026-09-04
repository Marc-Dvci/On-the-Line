"""Render the verification-call audio the console plays back.

Each turn in ``ontheline.workflow.CALL_SCRIPTS`` becomes one narrow-band MP3: the assistant in one
voice, the person answering at the organization in another, both filtered to the 300-3400 Hz
telephone band so a listener can hear that it is a call. The manifest records the text and the
measured duration of every clip, so the console reveals each transcript line exactly when it is
spoken.

    python tools/build_call_audio.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ontheline.workflow import CALL_SCRIPTS  # noqa: E402

OUTPUT = ROOT / "src" / "ontheline" / "web" / "call"
VOICES = {"assistant": "en-US-EmmaNeural", "provider": "en-US-BrianNeural"}
RATES = {"assistant": "+6%", "provider": "+2%"}
# 300-3400 Hz is the band a telephone line actually carries.
PHONE_FILTER = "highpass=f=300,lowpass=f=3400,acompressor=threshold=-18dB:ratio=3,volume=2.6"


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is required and is not on PATH")
    return path


def duration_seconds(path: Path) -> float:
    output = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return round(float(output.stdout.strip()), 3)


async def synthesize(text: str, role: str, destination: Path) -> None:
    raw = destination.with_suffix(".raw.mp3")
    await edge_tts.Communicate(text, VOICES[role], rate=RATES[role]).save(str(raw))
    subprocess.run(
        [_tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(raw),
         "-af", PHONE_FILTER, "-ac", "1", "-ar", "24000", "-b:a", "40k", str(destination)],
        check=True,
    )
    raw.unlink()


async def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for stale in OUTPUT.glob("*.mp3"):
        stale.unlink()

    manifest: dict[str, list[dict[str, object]]] = {}
    for scenario, turns in CALL_SCRIPTS.items():
        entries: list[dict[str, object]] = []
        for index, turn in enumerate(turns):
            role, text = turn
            name = f"{scenario}-{index}.mp3"
            await synthesize(text, role, OUTPUT / name)
            entries.append({
                "role": role,
                "text": text,
                "file": name,
                "seconds": duration_seconds(OUTPUT / name),
            })
            print(f"  {name:<28} {entries[-1]['seconds']:>6.2f}s  {role}")
        manifest[scenario] = entries
        total = sum(float(entry["seconds"]) for entry in entries)
        print(f"{scenario}: {len(entries)} turns, {total:.1f}s\n")

    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    weight = sum(path.stat().st_size for path in OUTPUT.iterdir())
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({weight / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
