"""
Telegram Bot — Control Panel + Approval Interface

Commands:
  /start            — Welcome + help
  /add_channel <url>— Add a YouTube channel to monitor
  /channels         — List + remove monitored channels
  /process <url>    — Process a specific YouTube video now
  /clips            — Send all ready clips for approval
  /status           — System stats
  /help             — Command list

Free-text:
  Paste a YouTube URL → processed immediately
  Send timestamps after a re-cut request → "1:30 2:45" or "90 165"

Inline approval flow (per clip):
  ✅ Approve  → shows upload buttons (YouTube / Instagram / Both)
  ❌ Reject   → marks rejected, deletes file
  ✂️ Re-cut   → bot asks for new start/end timestamps
  ↔️ Horizontal → generates 16:9 version of same clip

Background scheduler:
  Every MONITOR_INTERVAL_MINUTES: check channels → queue new videos
  → process queued videos → send clips for approval automatically
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Optional

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config.settings import (
    MONITOR_INTERVAL_MINUTES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_USER_ID,
)
from src.database.models import (
    Channel,
    Clip,
    ClipFormat,
    ClipStatus,
    SessionLocal,
    Video,
)
from src.monitor.channel_monitor import ChannelMonitor
from src.pipeline import Pipeline
from src.uploader.youtube_uploader import YouTubeUploader
from src.uploader.instagram_uploader import InstagramUploader

# Telegram's bot API limit for sendVideo
_MAX_VIDEO_BYTES = 50 * 1024 * 1024  # 50 MB


class ClippingBot:
    """
    Wraps the python-telegram-bot Application.
    All state is in the database — this class is stateless except for the
    recut_pending dict which tracks who we're waiting for timestamps from.
    """

    def __init__(self) -> None:
        self.pipeline  = Pipeline()
        self.monitor   = ChannelMonitor()
        self.yt_upload = YouTubeUploader()
        self.ig_upload = InstagramUploader()
        # user_id → clip_id: tracks when we're waiting for re-cut timestamps
        self._recut_pending: dict[int, int] = {}

    # ═══════════════════════════════════════════════════════════════
    # Auth
    # ═══════════════════════════════════════════════════════════════

    def _auth(self, update: Update) -> bool:
        return update.effective_user.id == TELEGRAM_USER_ID

    async def _deny(self, update: Update) -> None:
        await update.message.reply_text("Unauthorized.")

    # ═══════════════════════════════════════════════════════════════
    # Commands
    # ═══════════════════════════════════════════════════════════════

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return await self._deny(update)

        await update.message.reply_text(
            "🎬 *Clipping Automation Bot*\n\n"
            "*Commands:*\n"
            "/add\\_channel `<url>` — Monitor a YouTube channel\n"
            "/channels — List + remove monitored channels\n"
            "/process `<url>` — Process a YouTube video now\n"
            "/clips — Send pending clips for approval\n"
            "/status — System stats\n\n"
            "*Quick actions:*\n"
            "• Paste any YouTube URL and I'll process it\n"
            "• After approving a clip, choose where to upload it\n"
            "• Use ✂️ Re\\-cut to specify exact timestamps\n"
            "• Use ↔️ Horizontal to get a 16:9 version",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.cmd_start(update, context)

    async def cmd_add_channel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return await self._deny(update)

        if not context.args:
            await update.message.reply_text(
                "Usage: `/add_channel <YouTube channel URL>`\n"
                "Example: `/add_channel https://youtube.com/@MrBeast`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        url = context.args[0]
        msg = await update.message.reply_text(f"⏳ Adding channel: `{url}`...", parse_mode=ParseMode.MARKDOWN)

        try:
            channel = self.monitor.add_channel(url)
            await msg.edit_text(
                f"✅ Added *{channel.name}*\n"
                f"Checking every {MONITOR_INTERVAL_MINUTES} min for new videos.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except ValueError as e:
            await msg.edit_text(f"❌ {e}")
        except Exception as e:
            logger.error(f"add_channel error: {e}")
            await msg.edit_text("❌ Failed to add channel. Check the URL and try again.")

    async def cmd_channels(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return await self._deny(update)

        channels = self.monitor.list_channels()
        if not channels:
            await update.message.reply_text(
                "No channels added yet.\nUse /add\\_channel to add one.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        lines = ["*Monitored Channels:*\n"]
        for ch in channels:
            last = ch.last_checked.strftime("%d %b %H:%M") if ch.last_checked else "never"
            lines.append(f"• *{ch.name}* — last checked: {last}")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"❌ Remove {ch.name[:25]}",
                callback_data=f"remove_ch:{ch.channel_id}",
            )]
            for ch in channels
        ])

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    async def cmd_process(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return await self._deny(update)

        if not context.args:
            await update.message.reply_text(
                "Usage: `/process <YouTube URL>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await self._run_pipeline_for_url(context.args[0], update, context)

    async def cmd_clips(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return await self._deny(update)

        sent = await self._send_pending_clips(context)
        if not sent:
            await context.bot.send_message(
                TELEGRAM_USER_ID, "No pending clips right now."
            )

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return await self._deny(update)

        db = SessionLocal()
        try:
            n_channels = db.query(Channel).filter_by(active=True).count()
            n_videos   = db.query(Video).count()
            n_clips    = db.query(Clip).count()
            n_pending  = db.query(Clip).filter_by(status=ClipStatus.PENDING).count()
            n_approved = db.query(Clip).filter_by(status=ClipStatus.APPROVED).count()
            n_uploaded = db.query(Clip).filter_by(status=ClipStatus.UPLOADED).count()
            n_rejected = db.query(Clip).filter_by(status=ClipStatus.REJECTED).count()
        finally:
            db.close()

        await update.message.reply_text(
            "📊 *System Status*\n\n"
            f"📺 Channels monitored: `{n_channels}`\n"
            f"🎞  Videos processed:   `{n_videos}`\n"
            f"✂️  Total clips:         `{n_clips}`\n\n"
            f"⏳ Awaiting approval:  `{n_pending}`\n"
            f"✅ Approved:           `{n_approved}`\n"
            f"☁️  Uploaded:           `{n_uploaded}`\n"
            f"❌ Rejected:           `{n_rejected}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ═══════════════════════════════════════════════════════════════
    # Callback handler (all inline button presses)
    # ═══════════════════════════════════════════════════════════════

    async def callback_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data

        if data.startswith("approve:"):
            await self._on_approve(int(data.split(":")[1]), query)

        elif data.startswith("reject:"):
            await self._on_reject(int(data.split(":")[1]), query)

        elif data.startswith("recut:"):
            await self._on_recut_request(int(data.split(":")[1]), query)

        elif data.startswith("horizontal:"):
            await self._on_make_horizontal(int(data.split(":")[1]), query, context)

        elif data.startswith("upload:"):
            _, clip_id_s, platform = data.split(":")
            await self._on_upload(int(clip_id_s), platform, query, context)

        elif data.startswith("remove_ch:"):
            await self._on_remove_channel(data.split(":")[1], query)

    # ── Approval actions ──────────────────────────────────────────

    async def _on_approve(self, clip_id: int, query) -> None:
        self._set_status(clip_id, ClipStatus.APPROVED)
        clip = self._get_clip(clip_id)
        duration = f"{clip.duration:.0f}s" if clip else ""
        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n✅ *Approved!* Where should I upload it?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📺 YouTube",   callback_data=f"upload:{clip_id}:youtube"),
                    InlineKeyboardButton("📷 Instagram", callback_data=f"upload:{clip_id}:instagram"),
                ],
                [InlineKeyboardButton("📺📷 Upload Both", callback_data=f"upload:{clip_id}:both")],
            ]),
        )

    async def _on_reject(self, clip_id: int, query) -> None:
        clip = self._get_clip(clip_id)
        if clip and clip.file_path:
            Path(clip.file_path).unlink(missing_ok=True)
        self._set_status(clip_id, ClipStatus.REJECTED)
        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n❌ *Rejected.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
        )

    async def _on_recut_request(self, clip_id: int, query) -> None:
        self._recut_pending[TELEGRAM_USER_ID] = clip_id
        await query.edit_message_caption(
            caption=(
                f"{query.message.caption}\n\n"
                "✂️ *Re\\-cut requested\\.* Send me the new timestamps:\n"
                "`start end` \\— e\\.g\\. `90 165` or `1:30 2:45`"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None,
        )

    async def _on_make_horizontal(
        self, clip_id: int, query, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        clip  = self._get_clip(clip_id)
        video = self._get_video(clip.video_id) if clip else None

        if not clip or not video:
            await query.edit_message_caption(caption="❌ Source not found.", reply_markup=None)
            return

        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n↔️ Generating horizontal version...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
        )

        async def _run() -> None:
            new_clip = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.pipeline.process_manual_range(
                    video, clip.start_time, clip.end_time, fmt=ClipFormat.HORIZONTAL
                ),
            )
            if new_clip:
                await self._send_clip_for_approval(new_clip, context)
            else:
                await context.bot.send_message(
                    TELEGRAM_USER_ID, "❌ Failed to generate horizontal clip."
                )

        asyncio.create_task(_run())

    async def _on_upload(
        self, clip_id: int, platform: str, query, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        platform_label = {
            "youtube":   "YouTube",
            "instagram": "Instagram",
            "both":      "YouTube & Instagram",
        }.get(platform, platform)

        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n⏳ Uploading to *{platform_label}*...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
        )

        clip = self._get_clip(clip_id)
        if not clip:
            await context.bot.send_message(TELEGRAM_USER_ID, "❌ Clip not found.")
            return

        async def _run() -> None:
            results = []

            loop = asyncio.get_running_loop()

            if platform in ("youtube", "both"):
                url = await loop.run_in_executor(
                    None, lambda: self.yt_upload.upload(clip)
                )
                if url:
                    results.append(f"📺 YouTube: {url}")
                else:
                    results.append("📺 YouTube: ❌ upload failed")

            if platform in ("instagram", "both"):
                url = await loop.run_in_executor(
                    None, lambda: self.ig_upload.upload(clip)
                )
                if url:
                    results.append(f"📷 Instagram: {url}")
                else:
                    results.append("📷 Instagram: ❌ upload failed")

            summary = "\n".join(results)
            await context.bot.send_message(
                TELEGRAM_USER_ID,
                f"✅ Upload complete:\n\n{summary}",
                disable_web_page_preview=True,
            )

        asyncio.create_task(_run())

    async def _on_remove_channel(self, channel_id: str, query) -> None:
        ok = self.monitor.remove_channel(channel_id)
        text = "✅ Channel removed." if ok else "❌ Channel not found."
        await query.edit_message_text(text, reply_markup=None)

    # ═══════════════════════════════════════════════════════════════
    # Free-text handler
    # ═══════════════════════════════════════════════════════════════

    async def text_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._auth(update):
            return

        text = update.message.text.strip()

        # ── Waiting for re-cut timestamps ─────────────────────────
        if TELEGRAM_USER_ID in self._recut_pending:
            clip_id = self._recut_pending.pop(TELEGRAM_USER_ID)
            await self._handle_recut_timestamps(clip_id, text, update, context)
            return

        # ── YouTube URL ───────────────────────────────────────────
        url_match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)\S+", text)
        if url_match:
            await self._run_pipeline_for_url(url_match.group(), update, context)
            return

        # ── Unknown ───────────────────────────────────────────────
        await update.message.reply_text(
            "I didn't understand that.\n"
            "Paste a YouTube URL to process it, or use /help for commands."
        )

    async def _handle_recut_timestamps(
        self,
        clip_id: int,
        text: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        start, end = _parse_timestamps(text)
        if start is None or end is None:
            await update.message.reply_text(
                "❌ Couldn't parse timestamps.\n"
                "Try: `90 165` or `1:30 2:45`",
                parse_mode=ParseMode.MARKDOWN,
            )
            self._recut_pending[TELEGRAM_USER_ID] = clip_id  # restore
            return

        clip  = self._get_clip(clip_id)
        video = self._get_video(clip.video_id) if clip else None
        if not video:
            await update.message.reply_text("❌ Source video not found.")
            return

        msg = await update.message.reply_text(
            f"✂️ Re-cutting `{text}`...", parse_mode=ParseMode.MARKDOWN
        )

        async def _run() -> None:
            new_clip = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.pipeline.process_manual_range(video, start, end),
            )
            if new_clip:
                await msg.edit_text("✅ Done! Sending clip...")
                await self._send_clip_for_approval(new_clip, context)
            else:
                await msg.edit_text("❌ Re-cut failed.")

        asyncio.create_task(_run())

    # ═══════════════════════════════════════════════════════════════
    # Clip sender
    # ═══════════════════════════════════════════════════════════════

    async def _send_pending_clips(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Send all PENDING clips that have a file ready. Returns count sent."""
        db = SessionLocal()
        try:
            clips = (
                db.query(Clip)
                .filter_by(status=ClipStatus.PENDING)
                .filter(Clip.file_path.isnot(None))
                .all()
            )
        finally:
            db.close()

        for clip in clips:
            await self._send_clip_for_approval(clip, context)

        return len(clips)

    async def _send_clip_for_approval(
        self, clip: Clip, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send a clip video to Telegram with the approval keyboard."""
        if not clip.file_path or not Path(clip.file_path).exists():
            logger.warning(f"Clip file missing, skipping send: clip_id={clip.id}")
            return

        video = self._get_video(clip.video_id)
        source = video.title if video else "Unknown source"

        caption = (
            f"🎬 *{clip.title}*\n\n"
            f"📹 {source}\n"
            f"⏱ {clip.duration:.0f}s  ({_fmt_ts(clip.start_time)} → {_fmt_ts(clip.end_time)})\n"
            f"📐 {clip.format.value.capitalize()}\n\n"
            f"💡 _{clip.ai_reason or 'AI selected'}_"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve",     callback_data=f"approve:{clip.id}"),
                InlineKeyboardButton("❌ Reject",      callback_data=f"reject:{clip.id}"),
            ],
            [
                InlineKeyboardButton("✂️ Re-cut",      callback_data=f"recut:{clip.id}"),
                InlineKeyboardButton("↔️ Horizontal",  callback_data=f"horizontal:{clip.id}"),
            ],
        ])

        file_size = Path(clip.file_path).stat().st_size
        try:
            with open(clip.file_path, "rb") as f:
                if file_size <= _MAX_VIDEO_BYTES:
                    await context.bot.send_video(
                        chat_id=TELEGRAM_USER_ID,
                        video=f,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=keyboard,
                        supports_streaming=True,
                    )
                else:
                    mb = file_size / (1024 * 1024)
                    await context.bot.send_document(
                        chat_id=TELEGRAM_USER_ID,
                        document=f,
                        caption=caption + f"\n\n⚠️ _{mb:.0f} MB — sent as file_",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=keyboard,
                    )

            self._set_status(clip.id, ClipStatus.SENT)

        except Exception as e:
            logger.error(f"Failed to send clip {clip.id} to Telegram: {e}")

    # ═══════════════════════════════════════════════════════════════
    # Background processing
    # ═══════════════════════════════════════════════════════════════

    async def _run_pipeline_for_url(
        self,
        url: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Kick off the pipeline for a URL in the background."""
        msg = await update.message.reply_text(
            f"⏳ Processing...\n`{url}`\n\nI'll send clips when ready.",
            parse_mode=ParseMode.MARKDOWN,
        )

        async def _run() -> None:
            try:
                clips = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.pipeline.process_url(url)
                )
                if clips:
                    await msg.edit_text(
                        f"✅ Generated *{len(clips)}* clip(s) — sending now...",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    for clip in clips:
                        await self._send_clip_for_approval(clip, context)
                else:
                    await msg.edit_text("❌ No clips could be generated for this video.")
            except Exception as e:
                logger.error(f"Pipeline error for {url}: {e}")
                await msg.edit_text(f"❌ Processing failed: {e}")

        asyncio.create_task(_run())

    async def _scheduled_monitor(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Runs on a schedule: checks channels, queues new videos,
        processes them, and sends clips for approval.
        """
        try:
            logger.info("Scheduled channel check running...")
            new_videos = await asyncio.get_running_loop().run_in_executor(
                None, self.monitor.check_all_channels
            )

            if not new_videos:
                return

            await context.bot.send_message(
                TELEGRAM_USER_ID,
                f"📥 Found *{new_videos}* new video(s). Processing...",
                parse_mode=ParseMode.MARKDOWN,
            )

            total_clips = await asyncio.get_running_loop().run_in_executor(
                None, self.pipeline.process_queued_videos
            )

            if total_clips:
                await context.bot.send_message(
                    TELEGRAM_USER_ID,
                    f"✂️ *{total_clips}* clip(s) ready — sending for approval...",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await self._send_pending_clips(context)

        except Exception as e:
            logger.error(f"Scheduled monitor error: {e}")

    # ═══════════════════════════════════════════════════════════════
    # DB helpers
    # ═══════════════════════════════════════════════════════════════

    def _set_status(self, clip_id: int, status: ClipStatus) -> None:
        db = SessionLocal()
        try:
            clip = db.query(Clip).filter_by(id=clip_id).first()
            if clip:
                clip.status = status
                db.commit()
        finally:
            db.close()

    def _get_clip(self, clip_id: int) -> Optional[Clip]:
        db = SessionLocal()
        try:
            return db.query(Clip).filter_by(id=clip_id).first()
        finally:
            db.close()

    def _get_video(self, video_id: int) -> Optional[Video]:
        db = SessionLocal()
        try:
            return db.query(Video).filter_by(id=video_id).first()
        finally:
            db.close()

    # ═══════════════════════════════════════════════════════════════
    # Startup
    # ═══════════════════════════════════════════════════════════════

    def run(self) -> None:
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Commands
        app.add_handler(CommandHandler("start",       self.cmd_start))
        app.add_handler(CommandHandler("help",        self.cmd_help))
        app.add_handler(CommandHandler("add_channel", self.cmd_add_channel))
        app.add_handler(CommandHandler("channels",    self.cmd_channels))
        app.add_handler(CommandHandler("process",     self.cmd_process))
        app.add_handler(CommandHandler("clips",       self.cmd_clips))
        app.add_handler(CommandHandler("status",      self.cmd_status))

        # Inline button presses
        app.add_handler(CallbackQueryHandler(self.callback_handler))

        # Free-text messages
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.text_handler
        ))

        # Scheduled channel monitor
        app.job_queue.run_repeating(
            self._scheduled_monitor,
            interval=MONITOR_INTERVAL_MINUTES * 60,
            first=30,  # wait 30s after startup before first check
        )

        logger.info(
            f"Bot started. Monitoring channels every {MONITOR_INTERVAL_MINUTES} min."
        )
        app.run_polling(drop_pending_updates=True)


# ══════════════════════════════════════════════════════════════════
# Module entry point (called from main.py)
# ══════════════════════════════════════════════════════════════════

def run_bot() -> None:
    bot = ClippingBot()
    bot.run()


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _parse_timestamps(text: str) -> tuple[Optional[float], Optional[float]]:
    """
    Parse two timestamps from a user message.

    Supported formats:
      "90 165"       → (90.0, 165.0)
      "1:30 2:45"    → (90.0, 165.0)
      "1:30 to 2:45" → (90.0, 165.0)
    """
    # Strip filler words
    text = re.sub(r"\bto\b", " ", text, flags=re.IGNORECASE).strip()
    tokens = text.split()
    if len(tokens) < 2:
        return None, None

    def _to_s(t: str) -> Optional[float]:
        if ":" in t:
            parts = t.split(":")
            try:
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except ValueError:
                return None
        else:
            try:
                return float(t)
            except ValueError:
                return None

    return _to_s(tokens[0]), _to_s(tokens[1])


def _fmt_ts(seconds: float) -> str:
    """Format seconds as MM:SS for captions."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"
