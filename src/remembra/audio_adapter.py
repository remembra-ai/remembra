"""Audio capture + transcription with simple energy-based speaker diarization.

Phase 1 module. Uses faster-whisper for local transcription. Produces transcript
segments that can be stored as memories with type="observation".
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single transcribed segment with speaker label and timestamps."""

    start: float
    end: float
    speaker: str
    text: str
    confidence: float = 0.0

    def to_memory(self, meeting_id: str | None = None) -> dict[str, Any]:
        """Convert to a memory dict suitable for MemoryService.store()."""
        metadata: dict[str, Any] = {
            "source": "audio_adapter",
            "speaker": self.speaker,
            "start": self.start,
            "end": self.end,
            "confidence": self.confidence,
        }
        if meeting_id:
            metadata["meeting_id"] = meeting_id
        return {
            "content": self.text,
            "type": "observation",
            "metadata": metadata,
        }


@dataclass
class AudioSession:
    """Tracks a single capture session."""

    session_id: str
    started_at: float
    stopped_at: float | None = None
    file_path: str | None = None
    meeting_id: str | None = None
    sample_rate: int = 16000
    channels: int = 1
    frames: list[bytes] = field(default_factory=list)
    _active: bool = True


class AudioAdapter:
    """Capture audio, transcribe it, and split into speaker-labeled segments.

    Designed to degrade gracefully: if sounddevice or faster-whisper are not
    installed, start/stop still return a valid session object but transcribe()
    will raise a clear error with install instructions.
    """

    def __init__(
        self,
        model_name: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        sample_rate: int = 16000,
        num_speakers_hint: int = 2,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.sample_rate = sample_rate
        self.num_speakers_hint = max(1, num_speakers_hint)
        self._sessions: dict[str, AudioSession] = {}
        self._model = None  # lazy-loaded faster-whisper model
        self._stream = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ capture

    def start(self, meeting_id: str | None = None) -> AudioSession:
        """Begin a capture session. Returns a session handle."""
        session = AudioSession(
            session_id=str(uuid.uuid4()),
            started_at=time.time(),
            meeting_id=meeting_id,
            sample_rate=self.sample_rate,
        )
        with self._lock:
            self._sessions[session.session_id] = session

        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:  # pragma: no cover - env-dependent
            log.warning("sounddevice unavailable (%s); session in file-only mode", exc)
            return session

        def _callback(indata: Any, frames: Any, time_info: Any, status: Any) -> None:
            if not session._active:
                return
            session.frames.append(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                callback=_callback,
            )
            self._stream.start()
        except Exception as exc:  # pragma: no cover - env-dependent
            log.warning("Failed to start audio stream: %s", exc)
            self._stream = None

        return session

    def stop(self, session_id: str) -> AudioSession:
        """Stop a capture session and write audio to a WAV file."""
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session_id: {session_id}")

        session._active = False
        session.stopped_at = time.time()

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # pragma: no cover
                log.warning("Stream close error: %s", exc)
            self._stream = None

        if session.frames:
            fd, path = tempfile.mkstemp(prefix=f"remembra_{session.session_id}_", suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(session.channels)
                wf.setsampwidth(2)  # int16
                wf.setframerate(session.sample_rate)
                wf.writeframes(b"".join(session.frames))
            session.file_path = path

        return session

    # ------------------------------------------------------------ transcription

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError("faster-whisper is required for transcription. Install: pip install faster-whisper") from exc
        self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(
        self,
        session_or_path: AudioSession | str,
        language: str | None = "en",
    ) -> list[TranscriptSegment]:
        """Transcribe audio and return speaker-labeled segments.

        Accepts either an AudioSession (with file_path set) or a raw file path.
        """
        if isinstance(session_or_path, AudioSession):
            path = session_or_path.file_path
            meeting_id = session_or_path.meeting_id
        else:
            path = session_or_path
            meeting_id = None

        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"Audio file not found: {path}")

        model = self._load_model()
        segments_iter, info = model.transcribe(
            path,
            language=language,
            vad_filter=True,
            word_timestamps=False,
        )

        raw_segments: list[dict[str, Any]] = []
        for seg in segments_iter:
            raw_segments.append(
                {
                    "start": float(seg.start or 0.0),
                    "end": float(seg.end or 0.0),
                    "text": (seg.text or "").strip(),
                    "avg_logprob": float(getattr(seg, "avg_logprob", 0.0) or 0.0),
                }
            )

        labeled = self._assign_speakers(path, raw_segments)
        log.info(
            "Transcribed %d segments (lang=%s, duration=%.1fs)",
            len(labeled),
            getattr(info, "language", language),
            getattr(info, "duration", 0.0),
        )
        _ = meeting_id  # retained for callers that want it
        return labeled

    # -------------------------------------------------- energy-based diarization

    def _assign_speakers(self, path: str, segments: list[dict[str, Any]]) -> list[TranscriptSegment]:
        """Very simple speaker attribution using per-segment average energy.

        This is intentionally lightweight: we bucket segments into N speakers
        by k-means-like clustering on RMS energy. Good enough to distinguish
        a loud speaker from a quiet one in a 1:1 or small-group meeting.
        """
        energies: list[float] = []
        try:
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate()
                sampwidth = wf.getsampwidth()
                n_channels = wf.getnchannels()
                raw = wf.readframes(wf.getnframes())
        except Exception as exc:  # pragma: no cover
            log.warning("Could not read WAV for diarization: %s", exc)
            return [
                TranscriptSegment(
                    start=s["start"],
                    end=s["end"],
                    speaker="speaker_1",
                    text=s["text"],
                    confidence=_logprob_to_conf(s["avg_logprob"]),
                )
                for s in segments
            ]

        # Compute RMS for each segment window.
        bytes_per_sample = sampwidth * n_channels
        for s in segments:
            start_byte = int(s["start"] * sr) * bytes_per_sample
            end_byte = int(s["end"] * sr) * bytes_per_sample
            chunk = raw[start_byte:end_byte]
            energies.append(_rms_int16(chunk) if chunk else 0.0)

        if not energies:
            return []

        k = min(self.num_speakers_hint, len(energies))
        centroids = _init_centroids(energies, k)
        labels = _one_dim_kmeans(energies, centroids, iterations=8)

        # Re-label so that the quietest cluster is speaker_1 (stable ordering).
        order = sorted(range(k), key=lambda i: centroids[i])
        remap = {old: f"speaker_{new + 1}" for new, old in enumerate(order)}

        out: list[TranscriptSegment] = []
        for seg, lbl in zip(segments, labels, strict=True):
            out.append(
                TranscriptSegment(
                    start=seg["start"],
                    end=seg["end"],
                    speaker=remap[lbl],
                    text=seg["text"],
                    confidence=_logprob_to_conf(seg["avg_logprob"]),
                )
            )
        return out

    # ---------------------------------------------------------------- utilities

    def segments_as_memories(self, segments: Iterable[TranscriptSegment], meeting_id: str | None = None) -> list[dict[str, Any]]:
        return [seg.to_memory(meeting_id=meeting_id) for seg in segments]

    def session_dict(self, session: AudioSession) -> dict[str, Any]:
        d = asdict(session)
        d.pop("frames", None)
        d.pop("_active", None)
        return d


# ------------------------------------------------------------------ helpers


def _rms_int16(chunk: bytes) -> float:
    if not chunk:
        return 0.0
    # int16 little-endian
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    total = 0
    # Sample every Nth frame to keep this cheap on long segments.
    step = max(1, n // 1024)
    count = 0
    for i in range(0, n, step):
        off = i * 2
        val = int.from_bytes(chunk[off : off + 2], "little", signed=True)
        total += val * val
        count += 1
    return math.sqrt(total / max(1, count))


def _logprob_to_conf(lp: float) -> float:
    # avg_logprob is typically in (-1.5, 0). Map to (0, 1).
    try:
        return max(0.0, min(1.0, math.exp(lp)))
    except OverflowError:
        return 0.0


def _init_centroids(values: list[float], k: int) -> list[float]:
    lo, hi = min(values), max(values)
    if k == 1 or hi == lo:
        return [sum(values) / len(values)] * k
    step = (hi - lo) / (k - 1)
    return [lo + step * i for i in range(k)]


def _one_dim_kmeans(values: list[float], centroids: list[float], iterations: int) -> list[int]:
    k = len(centroids)
    labels = [0] * len(values)
    for _ in range(iterations):
        # assignment
        for i, v in enumerate(values):
            best, best_d = 0, abs(v - centroids[0])
            for j in range(1, k):
                d = abs(v - centroids[j])
                if d < best_d:
                    best, best_d = j, d
            labels[i] = best
        # update
        sums = [0.0] * k
        counts = [0] * k
        for v, lbl in zip(values, labels, strict=True):
            sums[lbl] += v
            counts[lbl] += 1
        for j in range(k):
            if counts[j]:
                centroids[j] = sums[j] / counts[j]
    return labels
