"""
YouTube Uploader

Uploads clips to YouTube using the YouTube Data API v3.
Requires OAuth 2.0 — on first run it opens a browser for authorization.
The token is cached in config/token.json for all subsequent runs.

Vertical clips automatically get #Shorts in the title and description.
All clips are uploaded as Private by default — the user can make them
public from YouTube Studio after reviewing.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from loguru import logger

from config.settings import BASE_DIR, YOUTUBE_CLIENT_SECRETS_FILE
from src.database.models import (
    Clip, ClipFormat, ClipStatus, Platform, SessionLocal, Upload, UploadStatus,
)

_SCOPES     = ["https://www.googleapis.com/auth/youtube.upload"]
_TOKEN_FILE = BASE_DIR / "config" / "token.json"

# YouTube API limits
_MAX_TITLE_LEN       = 100
_MAX_DESCRIPTION_LEN = 5000


class YouTubeUploader:

    def __init__(self) -> None:
        self._service = None

    # ── Public API ────────────────────────────────────────────────

    def upload(self, clip: Clip) -> Optional[str]:
        """
        Upload a clip to YouTube.

        Vertical clips are tagged as Shorts.
        Returns the YouTube video URL on success, None on failure.
        The Upload DB record is created regardless of outcome.
        """
        if not clip.file_path or not Path(clip.file_path).exists():
            logger.error(f"Clip file not found: {clip.file_path}")
            self._record(clip.id, UploadStatus.FAILED, error="File not found")
            return None

        title, description = self._build_metadata(clip)

        body = {
            "snippet": {
                "title":       title,
                "description": description,
                "tags":        ["shorts", "clips", "highlights"],
                "categoryId":  "22",   # People & Blogs — safe default
            },
            "status": {
                "privacyStatus":          "private",   # User promotes manually
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            clip.file_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=4 * 1024 * 1024,  # 4 MB chunks
        )

        logger.info(f"Uploading to YouTube: '{title}'")

        try:
            svc     = self._get_service()
            request = svc.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.debug(f"YouTube upload: {int(status.progress() * 100)}%")

            video_id = response["id"]
            url      = (
                f"https://youtube.com/shorts/{video_id}"
                if clip.format == ClipFormat.VERTICAL
                else f"https://youtube.com/watch?v={video_id}"
            )
            logger.success(f"YouTube upload complete: {url}")
            self._record(clip.id, UploadStatus.SUCCESS, url=url)
            self._set_clip_uploaded(clip.id)
            return url

        except HttpError as e:
            msg = f"HTTP {e.status_code}: {e.reason}"
            logger.error(f"YouTube upload failed: {msg}")
            self._record(clip.id, UploadStatus.FAILED, error=msg)
            return None

        except Exception as e:
            logger.error(f"YouTube upload error: {e}")
            self._record(clip.id, UploadStatus.FAILED, error=str(e))
            return None

    # ── OAuth ─────────────────────────────────────────────────────

    def _get_service(self):
        """Build and cache the YouTube service. Handles token refresh."""
        if self._service:
            return self._service

        creds: Optional[Credentials] = None

        if _TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing YouTube OAuth token...")
                creds.refresh(Request())
            else:
                logger.info(
                    "YouTube OAuth: opening browser for authorization.\n"
                    "If running headless, complete auth on a local machine first "
                    "and copy config/token.json to the server."
                )
                secrets = str(BASE_DIR / YOUTUBE_CLIENT_SECRETS_FILE)
                flow   = InstalledAppFlow.from_client_secrets_file(secrets, _SCOPES)
                creds  = flow.run_local_server(port=0)

            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            logger.info(f"YouTube token saved to {_TOKEN_FILE}")

        self._service = build("youtube", "v3", credentials=creds)
        return self._service

    # ── Metadata builder ──────────────────────────────────────────

    def _build_metadata(self, clip: Clip) -> tuple[str, str]:
        title       = (clip.title or "Clip")[:_MAX_TITLE_LEN]
        description = (clip.description or "")

        if clip.format == ClipFormat.VERTICAL:
            # YouTube classifies videos as Shorts when title/desc contains #Shorts
            if "#Shorts" not in title:
                # Only append if it fits
                candidate = f"{title} #Shorts"
                if len(candidate) <= _MAX_TITLE_LEN:
                    title = candidate
            if "#Shorts" not in description:
                description = f"{description}\n\n#Shorts".strip()

        description = description[:_MAX_DESCRIPTION_LEN]
        return title, description

    # ── DB helpers ────────────────────────────────────────────────

    def _record(
        self,
        clip_id:  int,
        status:   UploadStatus,
        url:      Optional[str] = None,
        error:    Optional[str] = None,
    ) -> None:
        db = SessionLocal()
        try:
            db.add(Upload(
                clip_id=clip_id,
                platform=Platform.YOUTUBE,
                status=status,
                upload_url=url,
                error_msg=error,
                uploaded_at=datetime.utcnow() if status == UploadStatus.SUCCESS else None,
            ))
            db.commit()
        finally:
            db.close()

    def _set_clip_uploaded(self, clip_id: int) -> None:
        db = SessionLocal()
        try:
            clip = db.query(Clip).filter_by(id=clip_id).first()
            if clip:
                clip.status = ClipStatus.UPLOADED
                db.commit()
        finally:
            db.close()
