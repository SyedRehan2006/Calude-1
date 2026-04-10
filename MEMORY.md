# Advanced Clipping Automation — Design Memory
> Living document. Updated after every phase. Use this as the single source of truth for all design decisions, user preferences, and progress.

---

## Project Goal
Build a fully automated video clipping pipeline that:
- Monitors YouTube channels for new uploads
- Downloads videos automatically
- Uses AI to understand context and clip the best moments
- Sends clips to the user via Telegram for approval
- Auto-uploads approved clips to Instagram Reels and YouTube Shorts
- Supports a full chat interface for manual control

---

## User Preferences (Confirmed in conversation)

| Preference | Decision |
|---|---|
| Approval interface | Telegram Bot |
| Clip formats | Vertical 9:16 (Reels/Shorts) AND Horizontal 16:9 (Cinematic Shorts) |
| Clip length | 30–90 seconds (no hard limit required, this is a default range) |
| Download quality | Minimum 1080p resolution + audio. Falls back to best available if channel has no 1080p content. |
| Video language | English |
| API keys setup | None yet — all need to be created |
| Hosting (now) | Local laptop |
| Hosting (future) | Oracle Cloud Free Tier (decided later in conversation) |
| Cost preference | As free as possible |

---

## Tech Stack Decisions

### Why each tool was chosen

| Tool | Role | Why | Cost |
|---|---|---|---|
| `yt-dlp` | Download YouTube videos | Best maintained downloader, handles all formats | Free |
| `ffmpeg` | Cut clips, encode, add subtitles | Industry standard, handles all video ops | Free |
| `faster-whisper` | Speech-to-text transcription | Runs **locally**, same accuracy as OpenAI Whisper API | Free |
| `Google Gemini API` | AI context understanding + clip detection | Replaced Claude API — generous free tier (1M tokens/day) | Free tier |
| `Telegram Bot API` | Chat interface + clip approval | Mobile-friendly, instant notifications, button UI | Free |
| `YouTube Data API v3` | Channel monitoring + Shorts upload | Official API, 10,000 units/day free | Free tier |
| `Instagram Graph API` | Reels upload | Official API, requires Creator account | Free |
| `SQLAlchemy + SQLite` | Local database | No server needed, simple, reliable | Free |
| `APScheduler` | Background channel polling | Lightweight Python scheduler | Free |
| `python-telegram-bot` | Telegram SDK | Best Python Telegram library | Free |
| `loguru` | Logging | Clean, simple logging with file rotation | Free |
| `rich` | Terminal output | Pretty startup banner and config table | Free |

### What was ruled out and why

| Rejected Tool | Replaced By | Reason |
|---|---|---|
| OpenAI Whisper API | `faster-whisper` (local) | Paid per minute of audio |
| Claude API | Google Gemini API | Paid per token |
| PostgreSQL | SQLite | Overkill for single-user local app |
| Web dashboard | Telegram Bot | More accessible, works on phone |

---

## Architecture

```
YouTube Channels (monitored)
        │
        ▼
  Channel Monitor ──── detects new uploads ────┐
                                                │
  Manual Video Input (URL from user) ───────────┤
                                                ▼
                                       Video Downloader
                                       (yt-dlp → MP4)
                                                │
                                                ▼
                                      Transcriber
                                      (faster-whisper → timestamped text)
                                                │
                                                ▼
                                      AI Clip Detector
                                      (Gemini reads transcript → clip timestamps + titles)
                                                │
                                                ▼
                                       Clip Engine
                                       (ffmpeg → cuts, encodes, adds subtitles)
                                       Outputs: vertical 9:16 + horizontal 16:9
                                                │
                                                ▼
                                     Telegram Bot (Approval)
                                       - Sends clip preview
                                       - User: Approve / Reject / Edit / Re-cut
                                                │
                                       ┌────────┴────────┐
                                       ▼                 ▼
                              YouTube Shorts      Instagram Reels
                              (YouTube Data API)  (Instagram Graph API)
```

---

## Module Map

```
clipping-automation/
├── main.py                        Entry point — starts bot + scheduler
├── requirements.txt               All Python dependencies
├── .env.example                   Template for API keys (copy to .env)
├── .gitignore                     Protects secrets + output files
├── MEMORY.md                      This file — design reference
│
├── config/
│   └── settings.py                Loads .env → typed constants + validate_settings()
│
├── src/
│   ├── monitor/
│   │   └── channel_monitor.py     YouTube API → detect new videos → queue in DB
│   │
│   ├── downloader/
│   │   └── video_downloader.py    yt-dlp → download MP4 → update DB record
│   │
│   ├── ai/
│   │   ├── transcriber.py         faster-whisper → timestamped transcript
│   │   └── clip_detector.py       Gemini API → reads transcript → clip timestamps
│   │
│   ├── clipper/
│   │   ├── clip_extractor.py      ffmpeg → cuts clips from video
│   │   └── subtitle_generator.py  ffmpeg → burns subtitles into clip
│   │
│   ├── uploader/
│   │   ├── youtube_uploader.py    YouTube Data API → upload Shorts
│   │   └── instagram_uploader.py  Instagram Graph API → upload Reels
│   │
│   ├── bot/
│   │   └── telegram_bot.py        Telegram bot — chat + approval UI
│   │
│   └── database/
│       └── models.py              SQLAlchemy models: Channel, Video, Clip, Upload
│
└── output/
    ├── downloads/                 Full downloaded videos (gitignored)
    ├── clips/                     Generated clips (gitignored)
    └── logs/                      Daily rotating log files (gitignored)
```

---

## Database Schema

### Channel
Stores YouTube channels being monitored.
- `id`, `name`, `youtube_url`, `channel_id` (UCxxxx), `active`, `added_at`, `last_checked`

### Video
Stores every video detected or manually added.
- `id`, `channel_id` (FK), `youtube_id`, `title`, `url`, `duration`, `file_path`, `transcript`, `status`, `added_at`, `processed_at`
- **Status flow:** `queued → downloading → downloaded → processing → done | failed`

### Clip
Stores clips generated from a video.
- `id`, `video_id` (FK), `title`, `description`, `start_time`, `end_time`, `format` (vertical/horizontal), `file_path`, `has_subtitles`, `status`, `ai_reason`, `created_at`
- **Status flow:** `pending → sent → approved → uploading → uploaded | rejected`

### Upload
Tracks upload history per clip per platform.
- `id`, `clip_id` (FK), `platform` (youtube/instagram), `status`, `upload_url`, `error_msg`, `uploaded_at`

---

## Video Formats

| Format | Dimensions | Target Platform |
|---|---|---|
| `vertical` | 1080 × 1920 (9:16) | Instagram Reels, YouTube Shorts |
| `horizontal` | 1920 × 1080 (16:9) | Cinematic YouTube Shorts |

---

## Telegram Bot Commands (Planned)

| Command | What it does |
|---|---|
| `/start` | Welcome message + status |
| `/add_channel <url>` | Add a YouTube channel to monitor |
| `/remove_channel` | Stop monitoring a channel |
| `/channels` | List all monitored channels |
| `/process <url>` | Manually submit a YouTube video |
| `/clips` | View pending clips awaiting approval |
| `/status` | Show system status (queue, DB counts) |
| `/setformat vertical\|horizontal\|both` | Change default clip format |
| Free text | Chat with the bot for custom commands |

### Approval Flow (per clip)
```
Bot sends clip preview →
  [Approve] [Reject] [Re-cut] [Change Format]
     │                │           │
  Queued for       Deleted    Bot asks for
  upload                      new timestamps
```

---

## API Keys Required (Setup Guide)

1. **Telegram Bot Token** — Message `@BotFather` on Telegram → `/newbot`
2. **Telegram User ID** — Message `@userinfobot` on Telegram
3. **Gemini API Key** — `https://aistudio.google.com/app/apikey` (free)
4. **YouTube Data API v3** — Google Cloud Console → Enable API → Create API Key
5. **YouTube OAuth** (for uploading) — Google Cloud Console → OAuth 2.0 Client ID → Download JSON
6. **Instagram Graph API** — Facebook Developer account → Create App → Instagram Basic Display

---

## Build Phases

| Phase | Description | Status |
|---|---|---|
| Phase 1 | Project foundation: structure, config, database | ✅ Done |
| Phase 2 | Download pipeline: channel monitor + yt-dlp downloader | ✅ Done |
| Phase 3 | AI brain: Whisper transcription + Gemini clip detection | Pending |
| Phase 4 | Clip engine: ffmpeg cutting + subtitle burning + format handling | Pending |
| Phase 5 | Telegram bot: chat interface + approval flow | Pending |
| Phase 6 | Uploaders: YouTube Shorts + Instagram Reels | Pending |
| Phase 7 | Polish: end-to-end testing, error handling, logging | Pending |

---

## Hosting Plan

- **Development / Testing:** Local laptop
- **Production (future):** Oracle Cloud Free Tier
  - ARM instance: 4 CPU, 24 GB RAM — always free, never expires
  - Deploy: `git clone` + `pip install -r requirements.txt` + `python main.py`
  - Runs 24/7 even when laptop is off

---

## Key Design Decisions

1. **Local Whisper over API** — Saves money, no rate limits, works offline. Trade-off: slower on CPU machines. User can set `WHISPER_MODEL=tiny` for speed or `large-v2` for accuracy.

2. **Gemini over Claude** — Free tier is generous enough for this use case (1M tokens/day). Claude API is better but costs money.

3. **Telegram over Web UI** — More accessible on mobile, works anywhere, approval is one tap. Web UI adds unnecessary complexity for a single-user tool.

4. **SQLite over PostgreSQL** — Single-user tool running locally. SQLite is sufficient and requires zero setup.

5. **Per-format clip files** — Vertical and horizontal are generated as separate files. This avoids re-processing at upload time.

6. **Subtitles burned in** — Subtitles are burned (hardcoded) into the video, not as a separate track. This ensures they show on all platforms including Instagram which doesn't support soft subtitles.
