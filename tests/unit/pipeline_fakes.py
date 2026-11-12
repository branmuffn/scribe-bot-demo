"""Fake speech engine and synthetic data for pipeline tests (no models, no real audio)."""
import os
import time
from pathlib import Path

BRIAN_VOICE = [1.0, 0.0, 0.0]
GUEST_VOICE = [0.0, 1.0, 0.0]
THIRD_VOICE = [0.0, 0.0, 1.0]


def segment(words: list[tuple[float, str]]) -> dict:
    return {"start": words[0][0], "end": words[-1][0] + 0.5, "text": " ".join(w for _, w in words),
            "tokens": [" " + w for _, w in words], "token_times": [t for t, _ in words]}


class FakeEngine:
    """Same methods as speech.SherpaEngine. Configure turns per speaker count and a voice per cluster."""

    def __init__(self, segments, auto_turns, voices, two_turns=None, two_voices=None):
        self.segments = segments
        self.auto_turns = auto_turns
        self.two_turns = two_turns
        self.voices = voices
        self.two_voices = two_voices or voices
        self.diarize_calls = []

    def duration(self, path):
        return 120.0

    def to_wav(self, src, dst):
        assert Path(src).exists(), "the vault copy must exist"
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"RIFF fake wav")

    def to_archive(self, src, dst):
        Path(dst).write_bytes(b"fake opus")

    def load(self, wav):
        assert Path(wav).exists()
        return "samples"

    def transcribe(self, samples):
        return self.segments

    def diarize(self, samples, speakers=None):
        self.diarize_calls.append(speakers)
        return self.two_turns if speakers == 2 and self.two_turns else self.auto_turns

    def embeddings(self, samples, turns):
        voices = self.two_voices if turns is self.two_turns else self.voices
        return {t["speaker"]: voices[t["speaker"]] for t in turns if t["speaker"] in voices}

    def write_clip(self, samples, start_s, end_s, dst):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"fake clip")


def drop_file(folder: Path, name: str, content: bytes = b"synthetic audio", age_s: float = 120) -> Path:
    """Put a synthetic 'recording' in the inbound folder, aged so it counts as fully synced."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(content)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p
