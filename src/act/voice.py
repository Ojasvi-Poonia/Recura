"""Render a recovery message to speech (Track 03: "Hinglish voice recovery").

Voice matters in India for a reason that is easy to miss from a desk: a large share of
payers are more comfortable being spoken to than reading an SMS, and Hinglish is the
register they actually speak. An English-only text nudge simply does not land for them.

VOICE SELECTION - the non-obvious part
Hinglish is Hindi-English code-mixing written in LATIN script ("aapka payment complete
nahi hua"). Feeding that to a Devanagari Hindi voice produces nonsense, because the
voice expects Devanagari input. An INDIAN ENGLISH voice reads Latin-script Hinglish
correctly - it applies Indian phonology to Latin letters, which is exactly what is
wanted. Devanagari text gets the Hindi voice.

    en    -> Indian English voice
    hi    -> Indian English voice   (Hinglish in Latin script)
    deva  -> Hindi voice            (Devanagari)

Synthesis is a DEMO-ONLY step and an optional dependency. Nothing is ever dialled
(CLAUDE.md section 2): the agent logs the script, and this renders a handful of samples
so the recovery call can be heard rather than described.

    make voice
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.act.messaging import RenderedMessage, voice_script

AUDIO_DIR = Path(__file__).resolve().parents[2] / "demo" / "audio"

# macOS `say` voices. Indian English for Latin-script Hinglish; Hindi for Devanagari.
VOICE_BY_LANGUAGE = {"en": "Rishi", "hi": "Rishi", "deva": "Lekha"}
FALLBACK_VOICE = "Samantha"


class SynthesisUnavailable(Exception):
    """No TTS engine present. Scripts are still produced and logged."""


@dataclass(frozen=True)
class VoiceClip:
    path: Path | None
    script: str
    language: str
    voice: str
    synthesised: bool


def engine_available() -> bool:
    return shutil.which("say") is not None


def available_voices() -> set[str]:
    if not engine_available():
        return set()
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def _pick_voice(language: str) -> str:
    wanted = VOICE_BY_LANGUAGE.get(language, VOICE_BY_LANGUAGE["en"])
    voices = available_voices()
    return wanted if wanted in voices else FALLBACK_VOICE


def synthesise(message: RenderedMessage, out_dir: Path = AUDIO_DIR,
               name: str | None = None) -> VoiceClip:
    """Render one message to an audio file. Degrades to script-only, never raises."""
    script = voice_script(message)
    voice = _pick_voice(message.language)
    if not engine_available():
        return VoiceClip(None, script, message.language, voice, synthesised=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    # AAC rather than raw AIFF: five uncompressed samples came to 2.1 MB, which is a
    # silly thing to carry in a repository for clips a few seconds long.
    path = out_dir / f"{name or message.template_key.lower()}_{message.language}.m4a"
    try:
        subprocess.run(
            ["say", "-v", voice, "-o", str(path),
             "--data-format=aac", "--file-format=m4af", script],
            check=True, capture_output=True, timeout=60)
    except Exception:
        try:  # some `say` builds lack the AAC encoder; fall back to AIFF
            path = path.with_suffix(".aiff")
            subprocess.run(["say", "-v", voice, "-o", str(path), script],
                           check=True, capture_output=True, timeout=60)
        except Exception:
            return VoiceClip(None, script, message.language, voice, synthesised=False)
    return VoiceClip(path, script, message.language, voice, synthesised=True)


def main() -> None:
    from src.models import Channel, FailureClass

    from src.act.messaging import render

    slots = {"name": "Priya", "amount": "Rs 2,499", "merchant": "Acme Foods",
             "link": "https://rzp.io/i/demo", "rail": "UPI", "days": "45"}
    cases = [
        (FailureClass.FUNDS, "hi", "payment"),
        (FailureClass.AUTH_ABANDON, "hi", "payment"),
        (FailureClass.FUNDS, "en", "payment"),
        (FailureClass.FUNDS, "deva", "payment"),
        (FailureClass.UNKNOWN, "hi", "invoice"),
    ]

    print(f"\n{'=' * 78}\n  RECURA - Hinglish voice recovery samples\n{'=' * 78}")
    if not engine_available():
        print("no `say` binary found - printing scripts only (synthesis is optional "
              "and demo-only; the agent logs scripts either way)\n")

    for failure_class, language, source in cases:
        message = render(failure_class, language, Channel.VOICE, slots,
                         source_type=source)
        clip = synthesise(message, name=f"{message.template_key.lower()}")
        tag = f"{message.template_key}/{language}"
        print(f"\n  {tag:<28} voice={clip.voice}  dlt={message.dlt_id}")
        print(f"    script  {clip.script}")
        print(f"    audio   {clip.path if clip.synthesised else '(not synthesised)'}")

    print(f"\n{'=' * 78}")
    print("Every script above is a DLT-registered template with its slots filled.")
    print("The model chooses and fills; it never authors free-form copy. See")
    print("src/act/messaging.py::verify_compliance - that boundary is a test, not a claim.")


if __name__ == "__main__":
    main()
