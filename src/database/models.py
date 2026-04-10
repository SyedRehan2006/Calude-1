"""
Database models and session management.
Uses SQLite via SQLAlchemy — no server required.

Tables:
  channels  — YouTube channels being monitored
  videos    — Videos downloaded or queued
  clips     — Clips generated from videos
  uploads   — Upload history per clip per platform
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from config.settings import DB_PATH


# ── Engine + Session ──────────────────────────────────────────────
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

# Enable WAL mode for better concurrent read performance
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """Context-managed database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Base ──────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────────────────
class VideoStatus(str, PyEnum):
    QUEUED      = "queued"       # Waiting to be downloaded
    DOWNLOADING = "downloading"  # Download in progress
    DOWNLOADED  = "downloaded"   # Downloaded, waiting for AI analysis
    PROCESSING  = "processing"   # AI is generating clip timestamps
    DONE        = "done"         # All clips generated
    FAILED      = "failed"       # Something went wrong


class ClipStatus(str, PyEnum):
    PENDING     = "pending"      # Just created, not sent for approval
    SENT        = "sent"         # Sent to Telegram for approval
    APPROVED    = "approved"     # User approved, ready to upload
    REJECTED    = "rejected"     # User rejected
    UPLOADING   = "uploading"    # Upload in progress
    UPLOADED    = "uploaded"     # Successfully uploaded


class ClipFormat(str, PyEnum):
    VERTICAL    = "vertical"     # 9:16 — Reels / Shorts
    HORIZONTAL  = "horizontal"   # 16:9 — Cinematic Shorts


class Platform(str, PyEnum):
    YOUTUBE     = "youtube"
    INSTAGRAM   = "instagram"


class UploadStatus(str, PyEnum):
    PENDING     = "pending"
    SUCCESS     = "success"
    FAILED      = "failed"


# ── Models ────────────────────────────────────────────────────────
class Channel(Base):
    """A YouTube channel being monitored for new uploads."""
    __tablename__ = "channels"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True)
    name:          Mapped[str]      = mapped_column(String(255))
    youtube_url:   Mapped[str]      = mapped_column(String(512), unique=True)
    channel_id:    Mapped[str]      = mapped_column(String(64), unique=True)  # UC...
    active:        Mapped[bool]     = mapped_column(Boolean, default=True)
    added_at:      Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_checked:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    videos: Mapped[list["Video"]] = relationship("Video", back_populates="channel")

    def __repr__(self) -> str:
        return f"<Channel name={self.name!r} active={self.active}>"


class Video(Base):
    """A video that has been detected, downloaded, or processed."""
    __tablename__ = "videos"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True)
    channel_id:    Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    youtube_id:    Mapped[str]      = mapped_column(String(64), unique=True)
    title:         Mapped[str]      = mapped_column(String(512))
    url:           Mapped[str]      = mapped_column(String(512))
    duration:      Mapped[float | None] = mapped_column(Float, nullable=True)   # seconds
    file_path:     Mapped[str | None]   = mapped_column(String(1024), nullable=True)
    transcript:    Mapped[str | None]   = mapped_column(Text, nullable=True)
    status:        Mapped[str]      = mapped_column(
        Enum(VideoStatus), default=VideoStatus.QUEUED
    )
    added_at:      Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    channel: Mapped["Channel | None"]  = relationship("Channel", back_populates="videos")
    clips:   Mapped[list["Clip"]]      = relationship("Clip", back_populates="video")

    def __repr__(self) -> str:
        return f"<Video title={self.title!r} status={self.status}>"


class Clip(Base):
    """A clip cut from a video."""
    __tablename__ = "clips"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True)
    video_id:      Mapped[int]      = mapped_column(ForeignKey("videos.id"))
    title:         Mapped[str]      = mapped_column(String(512))
    description:   Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time:    Mapped[float]    = mapped_column(Float)   # seconds into video
    end_time:      Mapped[float]    = mapped_column(Float)   # seconds into video
    format:        Mapped[str]      = mapped_column(
        Enum(ClipFormat), default=ClipFormat.VERTICAL
    )
    file_path:     Mapped[str | None] = mapped_column(String(1024), nullable=True)
    has_subtitles: Mapped[bool]     = mapped_column(Boolean, default=True)
    status:        Mapped[str]      = mapped_column(
        Enum(ClipStatus), default=ClipStatus.PENDING
    )
    ai_reason:     Mapped[str | None] = mapped_column(Text, nullable=True)  # why AI picked this
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video:   Mapped["Video"]          = relationship("Video", back_populates="clips")
    uploads: Mapped[list["Upload"]]   = relationship("Upload", back_populates="clip")

    @property
    def duration(self) -> float:
        return round(self.end_time - self.start_time, 2)

    def __repr__(self) -> str:
        return f"<Clip title={self.title!r} {self.start_time}s→{self.end_time}s status={self.status}>"


class Upload(Base):
    """Record of a clip being uploaded to a platform."""
    __tablename__ = "uploads"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True)
    clip_id:     Mapped[int]      = mapped_column(ForeignKey("clips.id"))
    platform:    Mapped[str]      = mapped_column(Enum(Platform))
    status:      Mapped[str]      = mapped_column(
        Enum(UploadStatus), default=UploadStatus.PENDING
    )
    upload_url:  Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_msg:   Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    clip: Mapped["Clip"] = relationship("Clip", back_populates="uploads")

    def __repr__(self) -> str:
        return f"<Upload platform={self.platform} status={self.status}>"


# ── Create all tables ─────────────────────────────────────────────
def init_db() -> None:
    """Create all tables if they don't exist yet."""
    Base.metadata.create_all(bind=engine)
