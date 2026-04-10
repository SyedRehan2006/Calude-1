"""
Transcriber

Converts a downloaded video's audio into a timestamped transcript
using faster-whisper (runs 100% locally — no API cost).

Output per segment:
  { "start": 12.4, "end": 18.7, "text": "This is the spoken text." }

The transcript is saved to Video.transcript in the database as JSON,
and also returned as a list of dicts for immediate use by ClipDetector.
"""

import json
from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel
from loguru import logger

from config.settings import WHISPER_MODEL, WHISPER_DEVICE
from src.database.models import Video, SessionLocal


class Transcriber:
    """
    Lazy-loads the Whisper model on first use so startup is instant.
    The model stays in memory for the lifetime of the process — reused
    across all subsequent transcriptions.
    """

    _model: Optional[WhisperModel] = None

    # ── Model loading ─────────────────────────────────────────────

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            logger.info(
                f"Loading Whisper model '{WHISPER_MODEL}' on {WHISPER_DEVICE}. "
                f"This may take a moment on first run..."
            )
            # int8 quantisation — fast on CPU, minimal accuracy loss
            self._model = WhisperModel(
                WHISPER_MODEL,
                device=WHISPER_DEVICE,
                compute_type="int8",
            )
            logger.success(f"Whisper model '{WHISPER_MODEL}' loaded and ready.")
        return self._model

    # ── Public API ────────────────────────────────────────────────

    def transcribe(self, video: Video) -> Optional[list[dict]]:
        """
        Transcribe the audio of a downloaded video.

        Returns a list of segment dicts:
            [{"start": float, "end": float, "text": str}, ...]

        Also saves the JSON transcript to the Video DB record.
        Returns None if the file is missing or transcription fails.
        """
        if not video.file_path:
            logger.error(f"Cannot transcribe — no file path on video: {video.title}")
            return None

        file_path = Path(video.file_path)
        if not file_path.exists():
            logger.error(f"Video file not found: {file_path}")
            return None

        # Return cached transcript if already done
        if video.transcript:
            logger.info(f"Using cached transcript for: {video.title}")
            return json.loads(video.transcript)

        model = self._get_model()

        logger.info(f"Transcribing: '{video.title}' ({video.duration or '?'}s)")

        try:
            segments_iter, info = model.transcribe(
                str(file_path),
                language="en",
                beam_size=5,
                word_timestamps=False,   # segment-level is enough for clipping
                vad_filter=True,         # skip silent/music-only sections
                vad_parameters={
                    "min_silence_duration_ms": 500,
                },
            )

            segments = []
            for seg in segments_iter:
                text = seg.text.strip()
                if not text:
                    continue
                segments.append({
                    "start": round(seg.start, 2),
                    "end":   round(seg.end,   2),
                    "text":  text,
                })

        except Exception as e:
            logger.error(f"Transcription failed for '{video.title}': {e}")
            return None

        if not segments:
            logger.warning(f"No speech detected in: '{video.title}'")
            return []

        logger.success(
            f"Transcribed '{video.title}': "
            f"{len(segments)} segments, "
            f"~{sum(len(s['text'].split()) for s in segments)} words"
        )

        # Persist to DB
        self._save_transcript(video.id, segments)
        return segments

    def format_for_ai(self, segments: list[dict]) -> str:
        """
        Format segments as a readable timestamped block for the AI prompt.

        Example output line:
            [00:12 → 00:18] This is the spoken text here.
        """
        lines = []
        for seg in segments:
            start = _fmt_time(seg["start"])
            end   = _fmt_time(seg["end"])
            lines.append(f"[{start} → {end}] {seg['text']}")
        return "\n".join(lines)

    def get_full_text(self, segments: list[dict]) -> str:
        """Return all segment text joined into one string."""
        return " ".join(s["text"] for s in segments)

    # ── Subtitle format ───────────────────────────────────────────

    def to_srt(self, segments: list[dict]) -> str:
        """
        Convert segments to SRT subtitle format.
        Used by the subtitle engine to burn captions into clips.
        """
        lines = []
        for i, seg in enumerate(segments, start=1):
            start_srt = _seconds_to_srt_time(seg["start"])
            end_srt   = _seconds_to_srt_time(seg["end"])
            lines.append(f"{i}\n{start_srt} --> {end_srt}\n{seg['text']}\n")
        return "\n".join(lines)

    def segments_in_range(
        self, segments: list[dict], start: float, end: float
    ) -> list[dict]:
        """
        Return only the segments that fall within [start, end] seconds.
        Segments that partially overlap are included and their timestamps
        are clamped to the range.
        Used when building per-clip SRT files.
        """
        result = []
        for seg in segments:
            if seg["end"] <= start or seg["start"] >= end:
                continue
            result.append({
                "start": max(seg["start"], start) - start,
                "end":   min(seg["end"],   end)   - start,
                "text":  seg["text"],
            })
        return result

    # ── Internal ──────────────────────────────────────────────────

    def _save_transcript(self, video_id: int, segments: list[dict]) -> None:
        db = SessionLocal()
        try:
            record = db.query(Video).filter_by(id=video_id).first()
            if record:
                record.transcript = json.dumps(segments, ensure_ascii=False)
                db.commit()
        finally:
            db.close()


# ── Helpers ───────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS  e.g. 3:05 → '03:05'"""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _seconds_to_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
