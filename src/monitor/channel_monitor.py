"""
YouTube Channel Monitor

Polls configured YouTube channels at a regular interval and queues
any new videos it finds into the database for download + processing.

Supports channel URLs in all common formats:
  - https://www.youtube.com/@channelname
  - https://www.youtube.com/channel/UCxxxxxxxx
  - https://www.youtube.com/c/customname
"""

import re
from datetime import datetime
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from config.settings import YOUTUBE_API_KEY
from src.database.models import Channel, Video, VideoStatus, SessionLocal


class ChannelMonitor:

    def __init__(self):
        if not YOUTUBE_API_KEY:
            raise RuntimeError("YOUTUBE_API_KEY is not set in .env")
        self._youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

    # ── Public API ────────────────────────────────────────────────

    def add_channel(self, url: str) -> Channel:
        """
        Add a YouTube channel URL to the monitor list.
        If the channel already exists, reactivates it.
        Raises ValueError if the URL cannot be resolved.
        """
        channel_id = self._resolve_channel_id(url)
        if not channel_id:
            raise ValueError(
                f"Could not resolve a YouTube channel ID from: {url}\n"
                "Make sure the URL is a valid YouTube channel link."
            )

        info = self._fetch_channel_info(channel_id)

        db = SessionLocal()
        try:
            existing = db.query(Channel).filter_by(channel_id=channel_id).first()
            if existing:
                existing.active = True
                db.commit()
                logger.info(f"Reactivated channel: {existing.name}")
                return existing

            channel = Channel(
                name=info["name"],
                youtube_url=url,
                channel_id=channel_id,
                active=True,
            )
            db.add(channel)
            db.commit()
            db.refresh(channel)
            logger.info(f"Added channel to monitor: {channel.name}")
            return channel
        finally:
            db.close()

    def remove_channel(self, channel_id: str) -> bool:
        """Deactivate a channel so it's no longer monitored. Returns True if found."""
        db = SessionLocal()
        try:
            channel = db.query(Channel).filter_by(channel_id=channel_id).first()
            if channel:
                channel.active = False
                db.commit()
                logger.info(f"Removed channel from monitor: {channel.name}")
                return True
            return False
        finally:
            db.close()

    def list_channels(self) -> list[Channel]:
        """Return all active monitored channels."""
        db = SessionLocal()
        try:
            return db.query(Channel).filter_by(active=True).all()
        finally:
            db.close()

    def check_all_channels(self) -> int:
        """
        Check every active channel for new videos.
        New videos are queued in the database.
        Returns total number of new videos found.
        """
        db = SessionLocal()
        try:
            channels = db.query(Channel).filter_by(active=True).all()
        finally:
            db.close()

        if not channels:
            logger.debug("No channels configured yet.")
            return 0

        logger.info(f"Checking {len(channels)} channel(s) for new videos...")
        total_new = 0

        for channel in channels:
            try:
                new_count = self._check_channel(channel)
                total_new += new_count
            except Exception as e:
                logger.error(f"Error checking channel '{channel.name}': {e}")

        if total_new:
            logger.info(f"Found {total_new} new video(s) across all channels.")
        else:
            logger.info("No new videos found.")

        return total_new

    # ── Internal helpers ──────────────────────────────────────────

    def _check_channel(self, channel: Channel) -> int:
        """Check a single channel. Returns count of new videos queued."""
        logger.debug(f"Checking: {channel.name}")

        playlist_id = self._get_uploads_playlist_id(channel.channel_id)
        if not playlist_id:
            logger.warning(f"Could not get uploads playlist for: {channel.name}")
            return 0

        recent = self._fetch_recent_videos(playlist_id, max_results=10)

        db = SessionLocal()
        new_count = 0
        try:
            for item in recent:
                exists = db.query(Video).filter_by(youtube_id=item["id"]).first()
                if not exists:
                    video = Video(
                        channel_id=channel.id,
                        youtube_id=item["id"],
                        title=item["title"],
                        url=f"https://www.youtube.com/watch?v={item['id']}",
                        status=VideoStatus.QUEUED,
                    )
                    db.add(video)
                    new_count += 1
                    logger.info(f"Queued new video: [{channel.name}] {item['title']}")

            # Stamp last_checked on the channel record
            ch = db.query(Channel).filter_by(id=channel.id).first()
            if ch:
                ch.last_checked = datetime.utcnow()

            db.commit()
        finally:
            db.close()

        return new_count

    def _resolve_channel_id(self, url: str) -> Optional[str]:
        """
        Turn any YouTube channel URL into a raw channel ID (UCxxxxxxxx).
        Handles @handle, /channel/UC..., and /c/name formats.
        """
        # Direct UC... ID in URL
        match = re.search(r"youtube\.com/channel/(UC[\w-]{20,})", url)
        if match:
            return match.group(1)

        # @handle format
        match = re.search(r"youtube\.com/@([\w.-]+)", url)
        if match:
            return self._lookup_by_handle(match.group(1))

        # /c/customname format
        match = re.search(r"youtube\.com/c/([\w.-]+)", url)
        if match:
            return self._lookup_by_handle(match.group(1))

        # /user/username format (legacy)
        match = re.search(r"youtube\.com/user/([\w.-]+)", url)
        if match:
            return self._lookup_by_username(match.group(1))

        # Plain string that looks like a handle
        if url.startswith("@"):
            return self._lookup_by_handle(url[1:])

        return None

    def _lookup_by_handle(self, handle: str) -> Optional[str]:
        try:
            resp = self._youtube.channels().list(
                part="id",
                forHandle=handle,
            ).execute()
            items = resp.get("items", [])
            return items[0]["id"] if items else None
        except HttpError as e:
            logger.error(f"YouTube API error looking up handle @{handle}: {e}")
            return None

    def _lookup_by_username(self, username: str) -> Optional[str]:
        try:
            resp = self._youtube.channels().list(
                part="id",
                forUsername=username,
            ).execute()
            items = resp.get("items", [])
            return items[0]["id"] if items else None
        except HttpError as e:
            logger.error(f"YouTube API error looking up username {username}: {e}")
            return None

    def _fetch_channel_info(self, channel_id: str) -> dict:
        """Fetch the channel display name."""
        try:
            resp = self._youtube.channels().list(
                part="snippet",
                id=channel_id,
            ).execute()
            items = resp.get("items", [])
            if not items:
                return {"name": channel_id}
            return {"name": items[0]["snippet"]["title"]}
        except HttpError as e:
            logger.error(f"Failed to fetch channel info for {channel_id}: {e}")
            return {"name": channel_id}

    def _get_uploads_playlist_id(self, channel_id: str) -> Optional[str]:
        """
        Every YouTube channel has a hidden 'uploads' playlist.
        Its ID is the channel ID with the second character changed from 'C' to 'U'.
        We fetch it via the API to be safe.
        """
        try:
            resp = self._youtube.channels().list(
                part="contentDetails",
                id=channel_id,
            ).execute()
            items = resp.get("items", [])
            if not items:
                return None
            return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except HttpError as e:
            logger.error(f"Failed to get uploads playlist for {channel_id}: {e}")
            return None

    def _fetch_recent_videos(self, playlist_id: str, max_results: int = 10) -> list[dict]:
        """Fetch the most recent videos from an uploads playlist."""
        try:
            resp = self._youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=max_results,
            ).execute()

            results = []
            for item in resp.get("items", []):
                video_id = item["contentDetails"]["videoId"]
                title    = item["snippet"]["title"]
                # Skip private/deleted videos
                if title in ("Private video", "Deleted video"):
                    continue
                results.append({"id": video_id, "title": title})

            return results
        except HttpError as e:
            logger.error(f"Failed to fetch videos from playlist {playlist_id}: {e}")
            return []
