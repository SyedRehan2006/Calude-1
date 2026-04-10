"""
Advanced Clipping Automation — Entry Point

Run:
    python main.py

This starts:
  1. The database (creates tables if needed)
  2. Settings validation (warns about missing API keys)
  3. The Telegram bot (your control panel)
  4. The background channel monitor scheduler
"""

import sys
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from config.settings import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_USER_ID,
    GEMINI_API_KEY,
    YOUTUBE_API_KEY,
    WHISPER_MODEL,
    MONITOR_INTERVAL_MINUTES,
    LOGS_DIR,
    validate_settings,
)
from src.database.models import init_db

console = Console()


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level="INFO",
        colorize=True,
    )
    logger.add(
        LOGS_DIR / "clipping_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        level="DEBUG",
        encoding="utf-8",
    )


def print_banner() -> None:
    console.print(Panel.fit(
        "[bold cyan]Advanced Clipping Automation[/bold cyan]\n"
        "[dim]YouTube → AI Clips → Telegram Approval → Instagram / YouTube[/dim]",
        border_style="cyan",
        padding=(1, 4),
    ))


def print_config_table() -> None:
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Whisper Model",       WHISPER_MODEL)
    table.add_row("Gemini Model",        "gemini-1.5-flash (free)")
    table.add_row("Monitor Interval",    f"Every {MONITOR_INTERVAL_MINUTES} min")
    table.add_row("Telegram Bot",        "configured" if TELEGRAM_BOT_TOKEN else "[red]MISSING[/red]")
    table.add_row("Gemini API",          "configured" if GEMINI_API_KEY     else "[red]MISSING[/red]")
    table.add_row("YouTube API",         "configured" if YOUTUBE_API_KEY    else "[red]MISSING[/red]")
    table.add_row("Telegram User ID",    str(TELEGRAM_USER_ID) if TELEGRAM_USER_ID else "[red]MISSING[/red]")

    console.print(table)


def main() -> None:
    setup_logging()
    print_banner()
    print_config_table()

    # ── Validate config ───────────────────────────────────────────
    missing = validate_settings()
    if missing:
        console.print(
            f"\n[yellow]Warning:[/yellow] Missing settings: [red]{', '.join(missing)}[/red]\n"
            f"Copy [bold].env.example[/bold] to [bold].env[/bold] and fill in the values.\n"
        )
        if "TELEGRAM_BOT_TOKEN" in missing or "TELEGRAM_USER_ID" in missing:
            console.print("[red]Cannot start without Telegram credentials. Exiting.[/red]")
            sys.exit(1)

    # ── Initialise database ───────────────────────────────────────
    logger.info("Initialising database...")
    init_db()
    logger.info("Database ready.")

    # ── Start bot + scheduler ─────────────────────────────────────
    # Imported here so missing deps are caught after the config check
    logger.info("Starting Telegram bot...")
    console.print("\n[green]Bot is running. Open Telegram and message your bot![/green]\n")

    from src.bot.telegram_bot import run_bot
    run_bot()


if __name__ == "__main__":
    main()
