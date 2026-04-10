"""
Pipeline Orchestrator

Chains Phase 2 → 3 → 4 into a single call:
  download → transcribe → detect clips → extract clips

Used by:
  - The Telegram bot when a user submits a URL manually
  - The channel monitor when a new video is detected
  - Any future automation trigger

All steps update the Video/Clip DB records with their progress,
so the Telegram bot can query pending clips at any time.
"""

import json
from typing import Optional

from loguru import logger

from src.downloader.video_downloader import VideoDownloader
from src.ai.transcriber import Transcriber
from src.ai.clip_detector import ClipDetector
from src.clipper.clip_extractor import ClipExtractor
from src.database.models import (
    Clip, ClipFormat, Video, VideoStatus, SessionLocal,
)


class Pipeline:
    """
    Single entry point for the full video → clips workflow.
    All heavy objects (models, API clients) are instantiated once
    and reused across calls.
    """

    def __init__(self):
        self.downloader  = VideoDownloader()
        self.transcriber = Transcriber()
        self.detector    = ClipDetector()
        self.extractor   = ClipExtractor()

    # ── Main entry points ─────────────────────────────────────────

    def process_url(
        self,
        url: str,
        n_clips: Optional[int] = None,
        fmt: ClipFormat = ClipFormat.VERTICAL,
    ) -> list[Clip]:
        """
        Full pipeline for a user-submitted YouTube URL.

        Steps:
          1. Download the video (1080p)
          2. Transcribe audio → timestamped segments
          3. AI detects the best clip moments
          4. ffmpeg cuts + formats + burns subtitles

        Returns list of ready Clip records (file_path set, status=PENDING).
        """
        logger.info(f"Pipeline started for URL: {url}")

        # Step 1 — Download
        video = self.downloader.download_from_url(url)
        if not video:
            logger.error(f"Pipeline aborted — download failed for: {url}")
            return []

        return self._process_video(video, n_clips=n_clips, fmt=fmt)

    def process_video(
        self,
        video: Video,
        n_clips: Optional[int] = None,
        fmt: ClipFormat = ClipFormat.VERTICAL,
    ) -> list[Clip]:
        """
        Full pipeline for an existing Video DB record
        (e.g. auto-detected by the channel monitor).
        Skips download if the file already exists.
        """
        if not video.file_path:
            logger.info(f"Downloading video: {video.title}")
            ok = self.downloader.download_video(video)
            if not ok:
                logger.error(f"Pipeline aborted — download failed for: {video.title}")
                return []
            # Refresh record from DB to get updated file_path
            video = self._reload_video(video.id)
            if not video:
                return []

        return self._process_video(video, n_clips=n_clips, fmt=fmt)

    def process_queued_videos(self) -> int:
        """
        Pick up all QUEUED videos from the database and run the full pipeline.
        Called by the scheduler after channel monitor queues new videos.
        Returns total number of clips generated.
        """
        db = SessionLocal()
        try:
            queued = db.query(Video).filter_by(status=VideoStatus.QUEUED).all()
        finally:
            db.close()

        if not queued:
            return 0

        logger.info(f"Processing {len(queued)} queued video(s)...")
        total_clips = 0

        for video in queued:
            try:
                clips = self.process_video(video)
                total_clips += len(clips)
            except Exception as e:
                logger.error(f"Pipeline failed for '{video.title}': {e}")

        return total_clips

    def process_manual_range(
        self,
        video: Video,
        start_seconds: float,
        end_seconds: float,
        fmt: ClipFormat = ClipFormat.VERTICAL,
    ) -> Optional[Clip]:
        """
        Create a clip for a user-specified time range (Telegram command).
        Skips AI detection — uses the exact timestamps provided.
        Returns the Clip record on success, None on failure.
        """
        segments = self._get_segments(video)

        clips = self.detector.detect_clips_from_range(
            video, segments or [],
            start_seconds, end_seconds,
            fmt=fmt,
        )
        if not clips:
            return None

        clip = clips[0]
        ok = self.extractor.extract(clip, video, segments)
        return clip if ok else None

    # ── Internal ──────────────────────────────────────────────────

    def _process_video(
        self,
        video: Video,
        n_clips: Optional[int],
        fmt: ClipFormat,
    ) -> list[Clip]:
        """Internal: transcribe → detect → extract for a downloaded video."""

        # Step 2 — Transcribe
        logger.info(f"Transcribing: {video.title}")
        segments = self.transcriber.transcribe(video)

        if segments is None:
            logger.error(f"Transcription failed for: {video.title}")
            return []

        if not segments:
            logger.warning(f"No speech detected in: {video.title}")
            return []

        # Step 3 — Detect clips
        logger.info(f"Detecting clips for: {video.title}")
        clips = self.detector.detect_clips(
            video, segments, n_clips=n_clips, fmt=fmt
        )

        if not clips:
            logger.warning(f"No clips detected for: {video.title}")
            return []

        # Step 4 — Extract clips
        logger.info(f"Extracting {len(clips)} clip(s) for: {video.title}")
        ready = []
        for clip in clips:
            ok = self.extractor.extract(clip, video, segments)
            if ok:
                ready.append(clip)

        logger.success(
            f"Pipeline complete for '{video.title}': "
            f"{len(ready)}/{len(clips)} clip(s) ready"
        )
        return ready

    def _get_segments(self, video: Video) -> Optional[list[dict]]:
        """Load transcript segments from DB or run transcription."""
        if video.transcript:
            try:
                return json.loads(video.transcript)
            except Exception:
                pass

        logger.info(f"Transcribing for manual clip: {video.title}")
        return self.transcriber.transcribe(video)

    def _reload_video(self, video_id: int) -> Optional[Video]:
        db = SessionLocal()
        try:
            return db.query(Video).filter_by(id=video_id).first()
        finally:
            db.close()
