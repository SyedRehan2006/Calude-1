"""
Instagram Uploader

Uploads clips to Instagram as Reels using the Instagram Graph API
resumable upload flow. No public hosting URL required — video bytes
are sent directly to Instagram's upload endpoint.

Flow:
  1. POST /{user-id}/media  → create container, get upload_url + creation_id
  2. POST video bytes       → upload to the resumable upload_url
  3. Poll /{creation_id}    → wait until status_code == FINISHED
  4. POST /{user-id}/media_publish → publish and get the media ID

Requirements:
  - Instagram Professional account (Creator or Business)
  - Facebook Developer App with Instagram Graph API access
  - INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID in .env
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from config.settings import INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID
from src.database.models import (
    Clip, ClipStatus, Platform, SessionLocal, Upload, UploadStatus,
)

_GRAPH_BASE          = "https://graph.facebook.com/v19.0"
_MAX_CAPTION_LEN     = 2200   # Instagram caption character limit
_POLL_INTERVAL_SEC   = 5
_POLL_MAX_ATTEMPTS   = 24     # 2 minutes total wait


class InstagramUploader:

    def __init__(self) -> None:
        if not INSTAGRAM_ACCESS_TOKEN:
            raise RuntimeError("INSTAGRAM_ACCESS_TOKEN is not set in .env")
        if not INSTAGRAM_ACCOUNT_ID:
            raise RuntimeError("INSTAGRAM_ACCOUNT_ID is not set in .env")

        self._token   = INSTAGRAM_ACCESS_TOKEN
        self._user_id = INSTAGRAM_ACCOUNT_ID

    # ── Public API ────────────────────────────────────────────────

    def upload(self, clip: Clip) -> Optional[str]:
        """
        Upload a clip to Instagram as a Reel.
        Returns the Instagram post URL on success, None on failure.
        """
        if not clip.file_path or not Path(clip.file_path).exists():
            logger.error(f"Clip file not found: {clip.file_path}")
            self._record(clip.id, UploadStatus.FAILED, error="File not found")
            return None

        caption = self._build_caption(clip)
        logger.info(f"Uploading to Instagram: '{clip.title}'")

        # Step 1 — create container
        creation_id, upload_url = self._create_container(caption)
        if not creation_id or not upload_url:
            self._record(clip.id, UploadStatus.FAILED, error="Failed to create media container")
            return None

        # Step 2 — upload video
        if not self._upload_video(clip.file_path, upload_url):
            self._record(clip.id, UploadStatus.FAILED, error="Video upload to container failed")
            return None

        # Step 3 — wait for container to finish processing
        if not self._wait_for_ready(creation_id):
            self._record(clip.id, UploadStatus.FAILED, error="Container never reached FINISHED status")
            return None

        # Step 4 — publish
        media_id = self._publish(creation_id)
        if not media_id:
            self._record(clip.id, UploadStatus.FAILED, error="Publish call failed")
            return None

        url = f"https://www.instagram.com/reel/{media_id}/"
        logger.success(f"Instagram upload complete: {url}")
        self._record(clip.id, UploadStatus.SUCCESS, url=url)
        self._set_clip_uploaded(clip.id)
        return url

    # ── Upload steps ──────────────────────────────────────────────

    def _create_container(self, caption: str) -> tuple[Optional[str], Optional[str]]:
        """
        Create an Instagram media container for a Reel.
        Returns (creation_id, upload_url) or (None, None) on failure.
        """
        endpoint = f"{_GRAPH_BASE}/{self._user_id}/media"
        payload  = {
            "access_token": self._token,
            "media_type":   "REELS",
            "caption":      caption[:_MAX_CAPTION_LEN],
            "upload_type":  "resumable",
        }

        try:
            resp = requests.post(endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            creation_id = data.get("id")
            upload_url  = data.get("uri")

            if not creation_id or not upload_url:
                logger.error(f"Unexpected container response: {data}")
                return None, None

            logger.debug(f"Instagram container created: {creation_id}")
            return creation_id, upload_url

        except requests.HTTPError as e:
            logger.error(f"Container creation HTTP error: {e.response.text}")
            return None, None
        except Exception as e:
            logger.error(f"Container creation error: {e}")
            return None, None

    def _upload_video(self, file_path: str, upload_url: str) -> bool:
        """
        Upload the video file bytes to the resumable upload URL.
        Returns True on success.
        """
        file_size = Path(file_path).stat().st_size
        headers   = {
            "Authorization": f"OAuth {self._token}",
            "offset":        "0",
            "file_size":     str(file_size),
        }

        logger.debug(f"Uploading {file_size / (1024*1024):.1f} MB to Instagram...")

        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    upload_url,
                    headers=headers,
                    data=f,
                    timeout=300,   # 5 min timeout for large files
                )
            resp.raise_for_status()
            logger.debug("Instagram video bytes uploaded.")
            return True

        except requests.HTTPError as e:
            logger.error(f"Video upload HTTP error: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Video upload error: {e}")
            return False

    def _wait_for_ready(self, creation_id: str) -> bool:
        """
        Poll the container status until it is FINISHED (ready to publish).
        Instagram processes the video server-side — this usually takes 10–60s.
        """
        endpoint = f"{_GRAPH_BASE}/{creation_id}"
        params   = {
            "fields":       "status_code,status",
            "access_token": self._token,
        }

        for attempt in range(1, _POLL_MAX_ATTEMPTS + 1):
            try:
                resp = requests.get(endpoint, params=params, timeout=15)
                resp.raise_for_status()
                data        = resp.json()
                status_code = data.get("status_code", "")

                if status_code == "FINISHED":
                    logger.debug(f"Container ready after {attempt * _POLL_INTERVAL_SEC}s.")
                    return True

                if status_code == "ERROR":
                    logger.error(
                        f"Instagram container error: {data.get('status', 'unknown')}"
                    )
                    return False

                logger.debug(
                    f"Container status: {status_code} "
                    f"(attempt {attempt}/{_POLL_MAX_ATTEMPTS})"
                )

            except Exception as e:
                logger.warning(f"Status poll error (attempt {attempt}): {e}")

            time.sleep(_POLL_INTERVAL_SEC)

        logger.error(
            f"Container {creation_id} not ready after "
            f"{_POLL_MAX_ATTEMPTS * _POLL_INTERVAL_SEC}s."
        )
        return False

    def _publish(self, creation_id: str) -> Optional[str]:
        """
        Publish the ready container as a Reel.
        Returns the media ID on success, None on failure.
        """
        endpoint = f"{_GRAPH_BASE}/{self._user_id}/media_publish"
        payload  = {
            "creation_id":  creation_id,
            "access_token": self._token,
        }

        try:
            resp = requests.post(endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            media_id = resp.json().get("id")
            logger.debug(f"Instagram media published: {media_id}")
            return media_id

        except requests.HTTPError as e:
            logger.error(f"Publish HTTP error: {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Publish error: {e}")
            return None

    # ── Metadata ──────────────────────────────────────────────────

    def _build_caption(self, clip: Clip) -> str:
        """Build the Instagram caption from clip metadata."""
        base = clip.description or clip.title or ""
        # Add Reels hashtag if not present
        if "#reels" not in base.lower() and "#reel" not in base.lower():
            base = f"{base}\n\n#reels #shorts #clips".strip()
        return base[:_MAX_CAPTION_LEN]

    # ── DB helpers ────────────────────────────────────────────────

    def _record(
        self,
        clip_id: int,
        status:  UploadStatus,
        url:     Optional[str] = None,
        error:   Optional[str] = None,
    ) -> None:
        db = SessionLocal()
        try:
            db.add(Upload(
                clip_id=clip_id,
                platform=Platform.INSTAGRAM,
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
