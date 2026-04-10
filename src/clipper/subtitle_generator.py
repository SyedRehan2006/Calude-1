"""
Subtitle Generator

Generates SRT subtitle files from the full video transcript for a specific
clip time range. Timestamps are shifted so the clip starts at 0.

Also provides ffmpeg force_style strings for burning subtitles into clips.
The SRT files are written to temp files, consumed by ClipExtractor during
encoding, then deleted automatically.
"""

import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger


# ── Subtitle style presets ────────────────────────────────────────
# ffmpeg force_style format (ASS/SSA style overrides)
# Colours use BGR hex with alpha prefix: &HAABBGGRR
#   &H00FFFFFF = fully opaque white
#   &H00000000 = fully opaque black
#   &H80000000 = 50% transparent black (background box)

STYLE_VERTICAL = (
    "FontName=Arial,"
    "Bold=1,"
    "FontSize=22,"
    "PrimaryColour=&H00FFFFFF,"    # white text
    "OutlineColour=&H00000000,"    # black hard outline
    "BackColour=&H80000000,"       # semi-transparent black bg box
    "Outline=2,"
    "Shadow=0,"
    "MarginV=120,"                 # distance from bottom (portrait needs more)
    "Alignment=2"                  # bottom-center
)

STYLE_HORIZONTAL = (
    "FontName=Arial,"
    "Bold=1,"
    "FontSize=18,"
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BackColour=&H80000000,"
    "Outline=2,"
    "Shadow=0,"
    "MarginV=60,"
    "Alignment=2"
)


class SubtitleGenerator:

    # ── Public API ────────────────────────────────────────────────

    def write_srt_for_clip(
        self,
        segments: list[dict],
        clip_start: float,
        clip_end: float,
        output_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """
        Write an SRT file for a clip's time range.

        Args:
            segments:    Full video transcript from Transcriber.
            clip_start:  Clip start in seconds (absolute video time).
            clip_end:    Clip end in seconds (absolute video time).
            output_path: Where to write the SRT. Uses a temp file if None.

        Returns:
            Path to the SRT file, or None if no speech exists in the range.
            Caller is responsible for deleting the file after use.
        """
        clip_segs = self._segments_in_range(segments, clip_start, clip_end)

        if not clip_segs:
            logger.debug(
                f"No subtitle segments found in range "
                f"[{clip_start:.1f}s → {clip_end:.1f}s]"
            )
            return None

        srt_content = self._to_srt(clip_segs)

        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".srt", delete=False, encoding="utf-8"
            )
            output_path = Path(tmp.name)
            tmp.write(srt_content)
            tmp.close()
        else:
            output_path.write_text(srt_content, encoding="utf-8")

        logger.debug(
            f"Wrote {len(clip_segs)} subtitle entries → {output_path.name}"
        )
        return output_path

    def get_style(self, fmt: str) -> str:
        """
        Return the ffmpeg force_style string for the given clip format.
        fmt: 'vertical' or 'horizontal'
        """
        return STYLE_VERTICAL if fmt == "vertical" else STYLE_HORIZONTAL

    # ── Internal ──────────────────────────────────────────────────

    def _segments_in_range(
        self, segments: list[dict], start: float, end: float
    ) -> list[dict]:
        """
        Extract segments that overlap with [start, end].
        Shifts timestamps so the clip begins at t=0.
        Partial overlaps are clamped to the clip boundary.
        """
        result = []
        for seg in segments:
            if seg["end"] <= start or seg["start"] >= end:
                continue
            result.append({
                "start": round(max(seg["start"], start) - start, 3),
                "end":   round(min(seg["end"],   end)   - start, 3),
                "text":  seg["text"],
            })
        return result

    def _to_srt(self, segments: list[dict]) -> str:
        """Convert 0-based segments to SRT format string."""
        blocks = []
        for i, seg in enumerate(segments, start=1):
            s = _to_srt_time(seg["start"])
            e = _to_srt_time(seg["end"])
            blocks.append(f"{i}\n{s} --> {e}\n{seg['text']}")
        return "\n\n".join(blocks) + "\n"


# ── Helpers ───────────────────────────────────────────────────────

def _to_srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp: HH:MM:SS,mmm"""
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
