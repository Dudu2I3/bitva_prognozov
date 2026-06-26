from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot.database.db import get_db, fetchall

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

MSK_TZ = timezone(timedelta(hours=3))


def _now_msk() -> str:
    """Current time in Moscow timezone as a string matching the kickoff_msk column format."""
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


async def _lock_started_matches() -> None:
    """Set locked=TRUE on all predictions for matches whose kickoff has passed."""
    now = _now_msk()
    async with get_db() as db:
        await db.execute(
            """
            UPDATE predictions
            SET locked = TRUE
            WHERE match_id IN (
                SELECT id FROM matches
                WHERE status = 'scheduled' AND kickoff_msk <= ?
            )
            """,
            (now,),
        )
        await db.commit()


async def _send_reminders(bot: Bot) -> None:
    """Push reminder to users who have no prediction for matches starting in <2 hours."""
    now = _now_msk()
    # datetime arithmetic for +2h in SQLite uses its own datetime(), which is UTC-aware.
    # Since kickoff_msk is stored as MSK time, we compute the window ceiling in Python.
    ceiling = (datetime.now(MSK_TZ) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        rows = await fetchall(
            db,
            """
            SELECT m.id, m.team_home, m.team_away, m.kickoff_msk,
                   u.telegram_id
            FROM matches m
            CROSS JOIN users u
            WHERE m.status = 'scheduled'
              AND m.kickoff_msk > ?
              AND m.kickoff_msk <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM predictions p
                  WHERE p.match_id = m.id AND p.user_id = u.id
              )
            """,
            (now, ceiling),
        )
    for row in rows:
        try:
            await bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    f"⏰ Напоминание: матч {row['team_home']} — {row['team_away']} "
                    f"начнётся в {str(row['kickoff_msk'])[:16]} МСК.\n"
                    "Ты ещё не сделал прогноз! /matches"
                ),
            )
        except Exception:
            pass  # user may have blocked the bot


def setup_scheduler(bot: Bot) -> None:
    scheduler.add_job(_lock_started_matches, "interval", minutes=1, id="lock_matches")
    scheduler.add_job(
        _send_reminders, "interval", minutes=30, id="reminders", kwargs={"bot": bot}
    )
    scheduler.start()
