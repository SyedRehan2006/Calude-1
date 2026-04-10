"""
Video Downloader

Downloads YouTube videos to disk using yt-dlp.
Updates the Video database record throughout the process.

Two entry points:
  download_video(video)      — download an existing Video DB record
  download_from_url(url)     — create a new record from a URL and download it
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import yt_dlp
from loguru import logger

from config.settings import DOWNLOADS_DIR
from src.database.models import Video, VideoStatus, SessionLocal


class VideoDownloader:

    # ── yt-dlp options ────────────────────────────────────────────

    def _build_opts(self, output_path: Path) -> dict:
        """
        Build yt-dlp options.

        Quality priority:
          1. 1080p MP4 video + M4A audio  (best case)
          2. 1080p any-format + best audio (merged to MP4)
          3. Any 1080p+ stream available
          4. Best available quality below 1080p (fallback if channel
             never uploaded above 720p)

        format_sort ensures that when multiple streams match the format
        selector, yt-dlp picks the highest resolution >= 1080p first,
        then prefers mp4/m4a containers to avoid unnecessary re-encoding.
        """
        return {
            "format": (
                # 1st choice: 1080p+ MP4 video + M4A audio
                "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]"
                # 2nd choice: 1080p+ any video + M4A audio
                "/bestvideo[height>=1080]+bestaudio[ext=m4a]"
                # 3rd choice: 1080p+ any video + any audio
                "/bestvideo[height>=1080]+bestaudio"
                # Fallback: best single-file MP4 (may be below 1080p)
                "/best[ext=mp4]"
                # Last resort: whatever is available
                "/best"
            ),
            # Sort preference when multiple streams match
            "format_sort": ["res:1080", "ext:mp4:m4a", "fps"],
            # %(ext)s handled by yt-dlp; always remux to MP4
            "outtmpl": str(output_path.with_suffix("")) + ".%(ext)s",
            "merge_output_format": "mp4",
            # Keep output clean
            "quiet": True,
            "no_warnings": True,
            # Don't write extra files
            "writeinfojson":   False,
            "writethumbnail":  False,
            "writesubtitles":  False,
            # Retry on transient network errors
            "retries":         5,
            "fragment_retries": 5,
        }

    # ── Public API ────────────────────────────────────────────────

    def get_video_info(self, url: str) -> Optional[dict]:
        """
        Fetch video metadata from a URL without downloading.
        Returns a dict with: youtube_id, title, duration, url
        Returns None if the URL is invalid or inaccessible.
        """
        opts = {"quiet": True, "no_warnings": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return {
                    "youtube_id": info.get("id"),
                    "title":      info.get("title", "Untitled"),
                    "duration":   info.get("duration"),  # seconds (float)
                    "url":        url,
                }
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"Could not fetch info for {url}: {e}")
            return None

    def download_video(self, video: Video) -> bool:
        """
        Download a video that already exists in the database.
        Updates status: QUEUED → DOWNLOADING → DOWNLOADED (or FAILED).
        Returns True on success.
        """
        db = SessionLocal()
        try:
            record = db.query(Video).filter_by(id=video.id).first()
            if not record:
                logger.error(f"Video ID {video.id} not found in database.")
                return False

            # Skip if already downloaded
            if record.status == VideoStatus.DOWNLOADED and record.file_path:
                if Path(record.file_path).exists():
                    logger.info(f"Already downloaded: {record.title}")
                    return True

            record.status = VideoStatus.DOWNLOADING
            db.commit()
        finally:
            db.close()

        # ── Download ──────────────────────────────────────────────
        output_path = DOWNLOADS_DIR / f"{video.youtube_id}.mp4"
        opts = self._build_opts(output_path)

        logger.info(f"Downloading (1080p+ preferred): {video.title}")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video.url, download=True)
                duration   = info.get("duration", video.duration)
                height     = info.get("height") or info.get("requested_downloads", [{}])[0].get("height")
                resolution = f"{height}p" if height else "unknown res"
            logger.debug(f"Downloaded at: {resolution}")
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"Download failed for '{video.title}': {e}")
            self._set_status(video.id, VideoStatus.FAILED)
            return False

        # yt-dlp may add the extension; find the actual file
        actual_path = self._find_output_file(DOWNLOADS_DIR, video.youtube_id)

        if actual_path and actual_path.exists():
            db = SessionLocal()
            try:
                record = db.query(Video).filter_by(id=video.id).first()
                if record:
                    record.file_path   = str(actual_path)
                    record.duration    = duration
                    record.status      = VideoStatus.DOWNLOADED
                    record.processed_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()

            size_mb = actual_path.stat().st_size / (1024 * 1024)
            logger.success(
                f"Downloaded: '{video.title}' "
                f"({resolution}, {duration}s, {size_mb:.1f} MB) → {actual_path.name}"
            )
            return True

        logger.error(f"Download seemed to succeed but output file not found for: {video.title}")
        self._set_status(video.id, VideoStatus.FAILED)
        return False

    def download_from_url(self, url: str) -> Optional[Video]:
        """
        Download a video directly from a URL (user-submitted manual input).
        Creates a Video DB record if one doesn't exist, then downloads.
        Returns the Video record on success, None on failure.
        """
        logger.info(f"Fetching info for: {url}")
        info = self.get_video_info(url)
        if not info:
            return None

        db = SessionLocal()
        try:
            # Return existing record if already tracked
            existing = db.query(Video).filter_by(youtube_id=info["youtube_id"]).first()
            if existing:
                logger.info(f"Video already in database: {existing.title}")
                if existing.status == VideoStatus.DOWNLOADED and existing.file_path:
                    return existing
                # Re-download if it failed or was never downloaded
                video = existing
            else:
                video = Video(
                    channel_id=None,   # no channel — manually submitted
                    youtube_id=info["youtube_id"],
                    title=info["title"],
                    url=url,
                    duration=info["duration"],
                    status=VideoStatus.QUEUED,
                )
                db.add(video)
                db.commit()
                db.refresh(video)
                logger.info(f"Created video record: {video.title}")
        finally:
            db.close()

        success = self.download_video(video)
        return video if success else None

    # ── Helpers ───────────────────────────────────────────────────

    def _set_status(self, video_id: int, status: VideoStatus) -> None:
        db = SessionLocal()
        try:
            record = db.query(Video).filter_by(id=video_id).first()
            if record:
                record.status = status
                db.commit()
        finally:
            db.close()

    def _find_output_file(self, directory: Path, youtube_id: str) -> Optional[Path]:
        """
        yt-dlp sometimes appends .mp4 or other extensions.
        Find the actual output file by matching the youtube_id prefix.
        """
        for ext in ("mp4", "mkv", "webm"):
            candidate = directory / f"{youtube_id}.{ext}"
            if candidate.exists():
                return candidate

        # Glob fallback
        matches = list(directory.glob(f"{youtube_id}.*"))
        return matches[0] if matches else None
