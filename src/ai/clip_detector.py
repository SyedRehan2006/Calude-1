"""
Clip Detector

Sends the timestamped transcript to Google Gemini and asks it to identify
the best moments to clip. Gemini returns structured JSON with start/end
timestamps, a title, a caption, and the reason it picked each moment.

The detected clips are saved to the Clip table in the database.
"""

import json
import re
import time
from typing import Optional

import google.generativeai as genai
from loguru import logger

from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    CLIP_MIN_SECONDS,
    CLIP_MAX_SECONDS,
    CLIPS_PER_VIDEO,
)
from src.database.models import (
    Clip, ClipFormat, ClipStatus, Video, VideoStatus, SessionLocal,
)


# ── Gemini prompt template ────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You are an expert video editor and viral content strategist.
I will give you a timestamped transcript of a YouTube video.
Your task is to identify the {n} best moments to turn into short clips
for Instagram Reels and YouTube Shorts.

VIDEO TITLE: {title}
VIDEO DURATION: {duration_str}

━━━ TRANSCRIPT (format: [MM:SS → MM:SS] spoken text) ━━━
{transcript}
━━━ END OF TRANSCRIPT ━━━

SELECTION CRITERIA — pick moments that have at least one of:
  • A surprising fact, statistic, or revelation
  • A high-energy, emotional, or funny moment
  • A strong opinion or controversial take
  • A clear beginning and end (feels complete on its own)
  • Practical advice or a valuable insight
  • A story with a punchline or payoff

STRICT RULES:
  1. Each clip must be between {min_sec} and {max_sec} seconds long
  2. Clips must NOT overlap each other
  3. Start each clip at the beginning of a natural sentence
  4. End each clip at the end of a natural sentence
  5. Do not start a clip in the middle of a thought
  6. Timestamps must be in SECONDS (decimal), not MM:SS

Return ONLY a valid JSON object — no markdown, no explanation, no extra text.
Use exactly this structure:

{{
  "clips": [
    {{
      "title": "Short punchy title (max 60 chars)",
      "description": "1-2 sentence caption for Instagram/YouTube (max 150 chars)",
      "start_seconds": 45.2,
      "end_seconds": 112.8,
      "reason": "Brief explanation of why this moment was selected"
    }}
  ]
}}
"""


class ClipDetector:

    def __init__(self):
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        genai.configure(api_key=GEMINI_API_KEY)
        self._model = genai.GenerativeModel(
            GEMINI_MODEL,
            generation_config=genai.GenerationConfig(
                temperature=0.4,       # low temp = more consistent, structured output
                response_mime_type="application/json",
            ),
        )

    # ── Public API ────────────────────────────────────────────────

    def detect_clips(
        self,
        video: Video,
        segments: list[dict],
        n_clips: Optional[int] = None,
        fmt: ClipFormat = ClipFormat.VERTICAL,
    ) -> list[Clip]:
        """
        Run AI clip detection on a transcript.

        Args:
            video:    The Video DB record (needs title + duration).
            segments: Timestamped transcript from Transcriber.
            n_clips:  Override number of clips (defaults to CLIPS_PER_VIDEO).
            fmt:      Output format — ClipFormat.VERTICAL (default) or
                      ClipFormat.HORIZONTAL. Pass HORIZONTAL only when the
                      user explicitly requests it via Telegram.

        Returns:
            List of saved Clip DB records with status=PENDING.
        """
        if not segments:
            logger.warning(f"No transcript segments for '{video.title}' — skipping detection.")
            return []

        n = n_clips or CLIPS_PER_VIDEO
        duration_str = _fmt_duration(video.duration or 0)
        transcript_text = _segments_to_prompt_text(segments)

        prompt = _PROMPT_TEMPLATE.format(
            n=n,
            title=video.title,
            duration_str=duration_str,
            transcript=transcript_text,
            min_sec=CLIP_MIN_SECONDS,
            max_sec=CLIP_MAX_SECONDS,
        )

        logger.info(
            f"Sending transcript to Gemini for '{video.title}' "
            f"({n} clips, {fmt.value} format)..."
        )

        raw_response = self._call_gemini(prompt)
        if raw_response is None:
            return []

        suggestions = self._parse_response(raw_response)
        if not suggestions:
            logger.error("Gemini returned no valid clip suggestions.")
            return []

        # Validate + clamp timestamps against actual video duration
        suggestions = self._validate_suggestions(suggestions, video.duration)

        clips = self._save_clips(video, suggestions, fmt)
        logger.success(
            f"Detected {len(clips)} clip(s) for '{video.title}' [{fmt.value}]"
        )
        return clips

    def detect_clips_from_range(
        self,
        video: Video,
        segments: list[dict],
        start_seconds: float,
        end_seconds: float,
        fmt: ClipFormat = ClipFormat.VERTICAL,
    ) -> list[Clip]:
        """
        Force a specific time range into a clip (user-specified via Telegram).
        Skips AI detection — directly creates a Clip record.

        fmt defaults to VERTICAL. Pass ClipFormat.HORIZONTAL when the user
        explicitly asks for a horizontal cut of this range.
        """
        duration = end_seconds - start_seconds
        if duration < 1:
            logger.warning("Requested clip range is too short (<1s). Skipping.")
            return []

        # Pull the text for this range to generate a title
        range_segments = [
            s for s in segments
            if s["end"] > start_seconds and s["start"] < end_seconds
        ]
        snippet = " ".join(s["text"] for s in range_segments)[:200]

        title = f"Clip {_fmt_duration(start_seconds)} – {_fmt_duration(end_seconds)}"
        description = snippet if snippet else "User-defined clip."

        suggestion = {
            "title":         title,
            "description":   description,
            "start_seconds": start_seconds,
            "end_seconds":   end_seconds,
            "reason":        "Manually specified time range by user.",
        }

        clips = self._save_clips(video, [suggestion], fmt)
        logger.info(f"Created manual clip: {title} [{fmt.value}]")
        return clips

    # ── Gemini call ───────────────────────────────────────────────

    def _call_gemini(self, prompt: str, retries: int = 3) -> Optional[str]:
        """Call Gemini with exponential backoff on rate-limit errors."""
        for attempt in range(1, retries + 1):
            try:
                response = self._model.generate_content(prompt)
                return response.text
            except Exception as e:
                err = str(e).lower()
                if "quota" in err or "rate" in err or "429" in err:
                    wait = 2 ** attempt
                    logger.warning(
                        f"Gemini rate limit hit (attempt {attempt}/{retries}). "
                        f"Retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"Gemini API error: {e}")
                    return None

        logger.error("Gemini call failed after all retries.")
        return None

    # ── Response parsing ──────────────────────────────────────────

    def _parse_response(self, raw: str) -> list[dict]:
        """
        Parse Gemini's JSON response into a list of clip dicts.
        Handles cases where the model wraps JSON in markdown code fences.
        """
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()

        # Extract the first JSON object
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            logger.error(f"No JSON object found in Gemini response:\n{raw[:500]}")
            return []

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini JSON: {e}\nRaw: {raw[:500]}")
            return []

        clips = data.get("clips", [])
        if not isinstance(clips, list):
            logger.error("Gemini JSON 'clips' field is not a list.")
            return []

        valid = []
        required_keys = {"title", "description", "start_seconds", "end_seconds"}
        for i, clip in enumerate(clips):
            missing = required_keys - clip.keys()
            if missing:
                logger.warning(f"Clip {i} missing keys {missing} — skipping.")
                continue
            try:
                clip["start_seconds"] = float(clip["start_seconds"])
                clip["end_seconds"]   = float(clip["end_seconds"])
            except (TypeError, ValueError):
                logger.warning(f"Clip {i} has non-numeric timestamps — skipping.")
                continue
            valid.append(clip)

        return valid

    def _validate_suggestions(
        self, suggestions: list[dict], video_duration: Optional[float]
    ) -> list[dict]:
        """
        Filter out clips that violate length constraints or fall outside
        the video duration. Clamp end_seconds to video length if needed.
        """
        valid = []
        for clip in suggestions:
            start = clip["start_seconds"]
            end   = clip["end_seconds"]

            # Clamp to video length
            if video_duration and end > video_duration:
                end = video_duration
                clip["end_seconds"] = end

            duration = end - start
            if start < 0:
                logger.warning(f"Clip '{clip['title']}' has negative start — skipping.")
                continue
            if end <= start:
                logger.warning(f"Clip '{clip['title']}' end <= start — skipping.")
                continue
            if duration < CLIP_MIN_SECONDS:
                logger.warning(
                    f"Clip '{clip['title']}' is {duration:.1f}s "
                    f"(min {CLIP_MIN_SECONDS}s) — skipping."
                )
                continue
            if duration > CLIP_MAX_SECONDS:
                # Trim to max rather than discard — keep the best part
                clip["end_seconds"] = start + CLIP_MAX_SECONDS
                logger.debug(
                    f"Clip '{clip['title']}' trimmed to {CLIP_MAX_SECONDS}s."
                )

            valid.append(clip)

        return valid

    # ── DB persistence ────────────────────────────────────────────

    def _save_clips(
        self,
        video: Video,
        suggestions: list[dict],
        fmt: ClipFormat = ClipFormat.VERTICAL,
    ) -> list[Clip]:
        """Save validated clip suggestions to the Clip table as a single format."""
        db = SessionLocal()
        saved = []
        try:
            for suggestion in suggestions:
                clip = Clip(
                    video_id=video.id,
                    title=suggestion["title"][:512],
                    description=suggestion.get("description", "")[:1000],
                    start_time=suggestion["start_seconds"],
                    end_time=suggestion["end_seconds"],
                    format=fmt,
                    has_subtitles=True,
                    status=ClipStatus.PENDING,
                    ai_reason=suggestion.get("reason", "")[:1000],
                )
                db.add(clip)
                saved.append(clip)

            # Mark video as fully processed
            vid = db.query(Video).filter_by(id=video.id).first()
            if vid:
                vid.status = VideoStatus.DONE

            db.commit()
            for clip in saved:
                db.refresh(clip)
        finally:
            db.close()

        return saved


# ── Helpers ───────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Format seconds as human-readable  e.g. 3723 → '1h 02m 03s'"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _segments_to_prompt_text(segments: list[dict]) -> str:
    """Format segments as MM:SS → MM:SS lines for the prompt."""
    lines = []
    for seg in segments:
        start = _fmt_mm_ss(seg["start"])
        end   = _fmt_mm_ss(seg["end"])
        lines.append(f"[{start} → {end}] {seg['text']}")
    return "\n".join(lines)


def _fmt_mm_ss(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"
