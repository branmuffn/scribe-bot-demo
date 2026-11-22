"""Speech engine: audio conversion (afconvert), Parakeet TDT transcription, speaker separation, voice embeddings.

Settings are the ones proven in M0 (docs/M0_RESULTS.md): no VAD (cut at quiet points), clustering threshold 1.1,
min-on 0.1 s, min-off 0.2 s, TitaNet-small embeddings. The engine is swappable: tests use a fake with the same
methods (see set_engine). sherpa_onnx and numpy are imported lazily so tests and CI don't need them.
"""
from __future__ import annotations

import re
import subprocess
import wave
from pathlib import Path

from scribe_bot.pipeline.paths import models

SAMPLE_RATE = 16000
THREADS = 4  # the M14 has 4 physical cores


def threads_now() -> int:
    """Cores for the next piece of work: 4, or 2 in Gentle / Auto while the Mac is in use (pipeline/pace.py)."""
    try:
        from scribe_bot.pipeline import pace
        return pace.threads()
    except Exception:
        return THREADS
THRESHOLD = 1.1
MIN_ON = 0.1
MIN_OFF = 0.2
EMBEDDING = "nemo_en_titanet_small.onnx"
PARAKEET = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
SEGMENTATION = "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
MIN_EMBED_TURN_S = 1.5


class Stopped(Exception):
    """stop() said to stop (Pause pressed); recording.py turns it into queue.Paused."""


def in_child(fn, *args, stop=None):
    """Run fn(*args) in a fresh Python process and return its result (its exceptions are raised here). With stop (a
    no-argument check), the child is ended within ~2 s once stop() is true and Stopped is raised (Pause, 2026-11-21)."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutTimeout

    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    try:
        fut = pool.submit(fn, *args)
        while True:
            try:
                return fut.result(timeout=2 if stop else None)
            except FutTimeout:
                if stop():
                    for p in list((getattr(pool, "_processes", None) or {}).values()):
                        p.terminate()
                    raise Stopped()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _diarize(samples, speakers: int | None, threads: int = THREADS) -> list[dict]:
    """Speaker separation itself (runs in a child process: see SherpaEngine.diarize)."""
    import sherpa_onnx

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(models() / SEGMENTATION)),
            num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(models() / EMBEDDING), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=speakers or -1, threshold=THRESHOLD),
        min_duration_on=MIN_ON, min_duration_off=MIN_OFF,
    )
    if not config.validate():
        raise RuntimeError("invalid diarization config (check the model files in ~/scribe-bot-models)")
    result = sherpa_onnx.OfflineSpeakerDiarization(config).process(samples).sort_by_start_time()
    return [{"start": round(r.start, 2), "end": round(r.end, 2), "speaker": r.speaker} for r in result]


class SherpaEngine:
    """The real engine (runs on the M14)."""

    def duration(self, path: Path) -> float:
        try:
            out = subprocess.run(["afinfo", str(path)], capture_output=True, text=True).stdout
        except FileNotFoundError:  # afinfo is macOS-only (CI runs on Linux); length unknown
            return 0.0
        m = re.search(r"estimated duration: ([\d.]+)", out)
        return float(m.group(1)) if m else 0.0

    def to_wav(self, src: Path, dst: Path) -> None:
        tmp = dst.with_name(dst.stem + ".tmp.wav")
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(src), str(tmp)], check=True)
        tmp.replace(dst)

    def to_archive(self, src: Path, dst: Path) -> None:
        """Archive copy kept forever (D8, format changed 2026-10-19): HE-AAC, mono, 24 kbit/s in .m4a. It plays in every
        browser and on Android (Opus-in-CAF didn't), at ~11 MB per audio hour vs ~14 for the old Opus file."""
        tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aach", "-c", "1", "-b", "24000", str(src), str(tmp)], check=True)
        tmp.replace(dst)

    def load(self, wav: Path):
        import numpy as np

        with wave.open(str(wav)) as w:
            if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
                raise ValueError("expected 16 kHz mono WAV")
            data = w.readframes(w.getnframes())
        return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

    pausable = True

    def transcribe(self, samples, done: list[dict] | None = None, stop=None, save=None) -> list[dict]:
        """done = pieces already transcribed before a Pause (skipped); after each new ~28 s piece save(out) keeps
        progress and stop() is checked, raising Stopped (Brian, 2026-11-21: pause a 2-hour talk, resume tonight)."""
        import sherpa_onnx

        p = models() / PARAKEET

        def recognizer(n):
            return sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(p / "encoder.int8.onnx"), decoder=str(p / "decoder.int8.onnx"),
                joiner=str(p / "joiner.int8.onnx"), tokens=str(p / "tokens.txt"),
                num_threads=n, model_type="nemo_transducer",
            )
        n = threads_now()
        rec = recognizer(n)
        out = list(done or [])
        have = {round(x["start"], 2) for x in out}
        for s0, s1 in quiet_cuts(samples):
            if round(s0 / SAMPLE_RATE, 2) in have:
                continue
            if stop and stop():
                raise Stopped()
            if threads_now() != n:   # Auto switched between full speed and Gentle: same words, different speed
                n = threads_now()
                rec = recognizer(n)
            stream = rec.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples[s0:s1])
            rec.decode_stream(stream)
            start = s0 / SAMPLE_RATE
            r = stream.result
            out.append({"start": round(start, 2), "end": round(s1 / SAMPLE_RATE, 2), "text": r.text.strip(),
                        "tokens": list(r.tokens), "token_times": [round(start + t, 2) for t in r.timestamps]})
            if save:
                save(out)
        return sorted(out, key=lambda x: x["start"])

    def diarize(self, samples, speakers: int | None = None, stop=None) -> list[dict]:
        """In a child process: sherpa-onnx holds Python's lock (the GIL) for the whole separation, which froze the
        worker's heartbeat thread (8 min for a 23-min recording: false "isn't responding" alerts, 2026-10-20).
        Measured on the M14: same speed, and the worker's other threads keep running."""
        return in_child(_diarize, samples, speakers, threads_now(), stop=stop)

    def embeddings(self, samples, turns: list[dict]) -> dict[int, list[float]]:
        """Average, normalised voice embedding per cluster, from turns of at least 1.5 s."""
        import numpy as np
        import sherpa_onnx

        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(models() / EMBEDDING), num_threads=THREADS))
        acc: dict[int, list] = {}
        for t in turns:
            if t["end"] - t["start"] < MIN_EMBED_TURN_S:
                continue
            s = ex.create_stream()
            s.accept_waveform(SAMPLE_RATE, samples[int(t["start"] * SAMPLE_RATE):int(t["end"] * SAMPLE_RATE)])
            s.input_finished()
            e = np.array(ex.compute(s))
            acc.setdefault(t["speaker"], []).append(e / np.linalg.norm(e))
        out = {}
        for c, v in acc.items():
            m = np.mean(v, axis=0)
            out[c] = [float(x) for x in m / np.linalg.norm(m)]
        return out

    def write_clip(self, samples, start_s: float, end_s: float, dst: Path) -> None:
        import numpy as np

        dst.parent.mkdir(parents=True, exist_ok=True)
        part = samples[int(start_s * SAMPLE_RATE):int(end_s * SAMPLE_RATE)]
        tmp = dst.with_name(dst.stem + ".tmp.wav")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(part, -1, 1) * 32767).astype(np.int16).tobytes())
        tmp.replace(dst)


def quiet_cuts(samples, target_s: float = 28, earliest_s: float = 20) -> list[tuple[int, int]]:
    """Split audio into ~target_s chunks, cutting at the quietest 0.2 s frame (no VAD: VAD missed soft speech)."""
    import numpy as np

    frame = int(0.2 * SAMPLE_RATE)
    chunks, start = [], 0
    while len(samples) - start > target_s * SAMPLE_RATE:
        lo, hi = start + int(earliest_s * SAMPLE_RATE), start + int(target_s * SAMPLE_RATE)
        energy = [float(np.mean(samples[i:i + frame] ** 2)) for i in range(lo, hi, frame)]
        cut = lo + int(np.argmin(energy)) * frame + frame // 2
        chunks.append((start, cut))
        start = cut
    chunks.append((start, len(samples)))
    return chunks


_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = SherpaEngine()
    return _engine


def set_engine(e) -> None:
    """Tests swap in a fake engine with the same methods."""
    global _engine
    _engine = e
