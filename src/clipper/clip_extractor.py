"""
Clip Extractor

Uses ffmpeg to cut, format-encode, and burn subtitles into clips
in a single pass (no double-encode quality loss).

Vertical  (9:16):  center-crops the 16:9 source → scales to 1080×1920
Horizontal (16:9): scales to 1920×1080, pads with black if needed

Output files are saved to CLIPS_DIR with a descriptive filename.
The Clip DB record is updated with the file path on success.
"""

import re
from pathlib import Path
from typing import Optional

import ffmpeg
from loguru import logger

from config.settings import CLIPS_DIR
from src.clipper.subtitle_generator import SubtitleGenerator
from src.database.models import Clip, ClipFormat, ClipStatus, Video, SessionLocal


class ClipExtractor:

    def __init__(self):
        self._sub_gen = SubtitleGenerator()

    # ── Public API ────────────────────────────────────────────────

    def extract(
        self,
        clip: Clip,
        video: Video,
        segments: Optional[list[dict]] = None,
    ) -> bool:
        """
        Cut, format, and encode a single clip from the source video.

        Args:
            clip:     Clip DB record with start_time, end_time, format.
            video:    Video DB record pointing to the downloaded MP4.
            segments: Full transcript segments for subtitle burning.
                      If None or clip.has_subtitles is False, no subs added.

        Returns:
            True on success. Updates clip.file_path in the database.
        """
        if not video.file_path or not Path(video.file_path).exists():
            logger.error(
                f"Source video file missing for '{video.title}': {video.file_path}"
            )
            return False

        output_path = self._build_output_path(clip)
        duration    = round(clip.end_time - clip.start_time, 3)

        # ── Generate SRT ─────────────────────────────────────────
        srt_path: Optional[Path] = None
        if clip.has_subtitles and segments:
            srt_path = self._sub_gen.write_srt_for_clip(
                segments, clip.start_time, clip.end_time
            )

        fmt_label = clip.format.value  # 'vertical' or 'horizontal'
        logger.info(
            f"Extracting: '{clip.title}' "
            f"[{clip.start_time:.1f}s → {clip.end_time:.1f}s | "
            f"{duration:.1f}s | {fmt_label}]"
        )

        # ── ffmpeg encode ─────────────────────────────────────────
        try:
            if clip.format == ClipFormat.VERTICAL:
                self._encode_vertical(
                    video.file_path, str(output_path),
                    clip.start_time, duration, srt_path,
                )
            else:
                self._encode_horizontal(
                    video.file_path, str(output_path),
                    clip.start_time, duration, srt_path,
                )
        except ffmpeg.Error as e:
            stderr = e.stderr.decode(errors="replace") if e.stderr else "no stderr"
            logger.error(f"ffmpeg failed for '{clip.title}':\n{stderr[-1000:]}")
            return False
        except Exception as e:
            logger.error(f"Clip extraction error for '{clip.title}': {e}")
            return False
        finally:
            # Always clean up temp SRT file
            if srt_path and srt_path.exists():
                srt_path.unlink(missing_ok=True)

        # ── Verify output ─────────────────────────────────────────
        if not output_path.exists():
            logger.error(f"ffmpeg finished but output not found: {output_path}")
            return False

        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.success(
            f"Clip ready: '{clip.title}' "
            f"({fmt_label}, {duration:.1f}s, {size_mb:.1f} MB) "
            f"→ {output_path.name}"
        )
        self._save_file_path(clip.id, str(output_path))
        return True

    def extract_all_pending(
        self,
        video: Video,
        segments: Optional[list[dict]] = None,
    ) -> tuple[int, int]:
        """
        Extract all PENDING clips for a video that don't yet have a file.
        Returns (success_count, failure_count).
        """
        db = SessionLocal()
        try:
            clips = (
                db.query(Clip)
                .filter_by(video_id=video.id, status=ClipStatus.PENDING)
                .filter(Clip.file_path.is_(None))
                .all()
            )
        finally:
            db.close()

        if not clips:
            logger.debug(f"No pending clips to extract for: {video.title}")
            return 0, 0

        success = failure = 0
        for clip in clips:
            ok = self.extract(clip, video, segments)
            if ok:
                success += 1
            else:
                failure += 1

        logger.info(
            f"Extraction complete for '{video.title}': "
            f"{success} ok, {failure} failed"
        )
        return success, failure

    # ── ffmpeg encode helpers ─────────────────────────────────────

    def _encode_vertical(
        self,
        input_path: str,
        output_path: str,
        start: float,
        duration: float,
        srt_path: Optional[Path],
    ) -> None:
        """
        9:16 vertical encoding.

        Filter chain:
          1. crop  — take the center 9:16 strip from the 16:9 source
                     formula: width = ih * 9/16, height = ih
                     x offset centres it horizontally
          2. scale — upscale/downscale the crop to exactly 1080×1920
          3. subtitles (optional) — burn in captions
        """
        inp   = ffmpeg.input(input_path, ss=start, t=duration)
        video = (
            inp.video
            .filter("crop", "ih*9/16", "ih", "(iw-ih*9/16)/2", 0)
            .filter("scale", 1080, 1920)
        )
        if srt_path:
            video = video.filter(
                "subtitles", _escape_path(str(srt_path)),
                force_style=self._sub_gen.get_style("vertical"),
            )
        self._run(video, inp.audio, output_path)

    def _encode_horizontal(
        self,
        input_path: str,
        output_path: str,
        start: float,
        duration: float,
        srt_path: Optional[Path],
    ) -> None:
        """
        16:9 horizontal encoding.

        Filter chain:
          1. scale — fit within 1920×1080, preserving aspect ratio
          2. pad   — add black bars to reach exactly 1920×1080
          3. subtitles (optional) — burn in captions
        """
        inp   = ffmpeg.input(input_path, ss=start, t=duration)
        video = (
            inp.video
            .filter("scale", 1920, 1080, force_original_aspect_ratio="decrease")
            .filter("pad",   1920, 1080, "(ow-iw)/2", "(oh-ih)/2", color="black")
            .filter("setsar", 1)
        )
        if srt_path:
            video = video.filter(
                "subtitles", _escape_path(str(srt_path)),
                force_style=self._sub_gen.get_style("horizontal"),
            )
        self._run(video, inp.audio, output_path)

    def _run(self, video, audio, output_path: str) -> None:
        """Run the ffmpeg encode with consistent output settings."""
        (
            ffmpeg
            .output(
                video, audio, output_path,
                vcodec="libx264",
                acodec="aac",
                audio_bitrate="192k",
                crf=23,           # quality: 0 (best) – 51 (worst), 23 is a good default
                preset="fast",    # encoding speed vs compression trade-off
                pix_fmt="yuv420p",  # required for Instagram/YouTube compatibility
                movflags="+faststart",  # web-optimised: metadata at start of file
            )
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

    # ── DB helpers ────────────────────────────────────────────────

    def _build_output_path(self, clip: Clip) -> Path:
        fmt       = "v" if clip.format == ClipFormat.VERTICAL else "h"
        safe_name = _safe_filename(clip.title, max_len=40)
        return CLIPS_DIR / f"clip_{clip.id}_{fmt}_{safe_name}.mp4"

    def _save_file_path(self, clip_id: int, file_path: str) -> None:
        db = SessionLocal()
        try:
            record = db.query(Clip).filter_by(id=clip_id).first()
            if record:
                record.file_path = file_path
                # Status stays PENDING — Telegram bot will set it to SENT
                db.commit()
        finally:
            db.close()


# ── Helpers ───────────────────────────────────────────────────────

def _safe_filename(title: str, max_len: int = 40) -> str:
    """Strip unsafe characters and truncate for use in a filename."""
    safe = re.sub(r"[^\w\s-]", "", title)
    safe = re.sub(r"\s+", "_", safe).strip("_")
    return safe[:max_len] or "clip"


def _escape_path(path: str) -> str:
    """
    Escape a file path for use in an ffmpeg filter string.
    ffmpeg filter values use ':' as delimiter so colons and backslashes
    in paths must be escaped.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:")
