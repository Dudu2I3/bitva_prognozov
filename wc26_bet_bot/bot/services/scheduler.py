import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot.config import config
from bot.database.db import get_db, fetchall, fetchone
from bot.services.api_client import (
    fetch_games, ru_to_en, en_to_ru, parse_local_date, ROUND_ORDER,
)
from bot.keyboards import api_result_kb, api_new_round_kb

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

MSK_TZ = timezone(timedelta(hours=3))


def _now_msk() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ── Lock & reminders (unchanged logic) ───────────────────────────────────────

async def _lock_started_matches() -> None:
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
    now = _now_msk()
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
            pass


# ── API: check finished results ───────────────────────────────────────────────

async def _check_results(bot: Bot) -> None:
    """
    Fetch games from worldcup26.ir. For each finished API game that matches
    a still-scheduled DB match, send admin a confirmation message.
    Silently skips on any API error.
    """
    try:
        games = await fetch_games()
    except Exception as exc:
        log.warning("check_results: API unavailable (%s), skipping", exc)
        return

    # Build lookup: (en_home_lower, en_away_lower) → api_game
    finished_by_teams: dict[tuple[str, str], dict] = {}
    for g in games:
        if g.get("finished") != "TRUE":
            continue
        h = g.get("home_team_name_en", "")
        a = g.get("away_team_name_en", "")
        if h and a:
            finished_by_teams[(h.lower(), a.lower())] = g

    if not finished_by_teams:
        return

    now = _now_msk()
    async with get_db() as db:
        pending = await fetchall(
            db,
            "SELECT id, team_home, team_away, stage FROM matches "
            "WHERE status = 'scheduled' AND kickoff_msk <= ?",
            (now,),
        )

    for match in pending:
        en_home = ru_to_en(match["team_home"])
        en_away = ru_to_en(match["team_away"])
        if not en_home or not en_away:
            log.warning(
                "check_results: no EN mapping for %s / %s",
                match["team_home"], match["team_away"],
            )
            continue

        api_game = finished_by_teams.get((en_home.lower(), en_away.lower()))
        if not api_game:
            continue

        home_s = int(api_game["home_score"])
        away_s = int(api_game["away_score"])
        match_id: int = match["id"]
        is_playoff = match["stage"] == "playoff"
        is_draw = home_s == away_s

        text = (
            f"🔔 Результат из API:\n"
            f"<b>{match['team_home']} {home_s}:{away_s} {match['team_away']}</b>\n"
        )
        if is_playoff and is_draw:
            text += "\n⚠️ Ничья в плей-офф — после подтверждения нужно выбрать победителя."

        try:
            await bot.send_message(
                config.admin_telegram_id,
                text,
                reply_markup=api_result_kb(match_id, home_s, away_s, is_playoff and is_draw),
            )
        except Exception as exc:
            log.error("check_results: failed to notify admin for match %s: %s", match_id, exc)


# ── API: check for new playoff rounds ────────────────────────────────────────

async def _check_new_round(bot: Bot) -> None:
    """
    If API has knockout games with known teams not yet in our DB,
    send admin a proposal to add them (grouped by round type).
    Silently skips on API error.
    """
    try:
        games = await fetch_games()
    except Exception as exc:
        log.warning("check_new_round: API unavailable (%s), skipping", exc)
        return

    # Collect API knockout games with known team names
    knockout_with_teams = [
        g for g in games
        if g.get("type") in ROUND_ORDER
        and g.get("home_team_name_en")
        and g.get("away_team_name_en")
    ]
    if not knockout_with_teams:
        return

    # Build set of DB matches already tracked (by api_game_id or team names)
    async with get_db() as db:
        db_matches = await fetchall(
            db,
            "SELECT api_game_id, team_home, team_away FROM matches WHERE stage = 'playoff'",
        )

    known_api_ids: set[str] = {
        str(r["api_game_id"]) for r in db_matches if r["api_game_id"]
    }
    known_pairs: set[tuple[str, str]] = set()
    for r in db_matches:
        en_h = ru_to_en(r["team_home"])
        en_a = ru_to_en(r["team_away"])
        if en_h and en_a:
            known_pairs.add((en_h.lower(), en_a.lower()))

    # Find new games not yet in DB
    new_games: list[dict] = []
    for g in knockout_with_teams:
        if str(g["id"]) in known_api_ids:
            continue
        pair = (g["home_team_name_en"].lower(), g["away_team_name_en"].lower())
        if pair in known_pairs:
            continue
        new_games.append(g)

    if not new_games:
        return

    # Group by round type, propose earliest new round
    by_round: dict[str, list[dict]] = {}
    for g in new_games:
        by_round.setdefault(g["type"], []).append(g)

    earliest_type = min(by_round, key=lambda t: ROUND_ORDER.get(t, 99))
    round_games = by_round[earliest_type]
    round_label = {
        "r32": "1/16 финала", "r16": "1/8 финала",
        "qf": "Четвертьфинал", "sf": "Полуфинал", "final": "Финал",
    }.get(earliest_type, earliest_type.upper())

    lines = [f"🆕 Новые матчи <b>{round_label}</b> в API:\n"]
    game_ids: list[str] = []
    for g in sorted(round_games, key=lambda x: x.get("local_date", "")):
        lines.append(
            f"• {en_to_ru(g['home_team_name_en'])} — {en_to_ru(g['away_team_name_en'])}"
            f"  ({g['local_date']} local)"
        )
        game_ids.append(str(g["id"]))

    lines.append("\n⚠️ Время указано по местному (стадиону). После добавления уточни MSK вручную.")

    game_ids_str = ",".join(game_ids)
    # Limit callback_data length: if too many IDs, truncate (shouldn't happen for ≤8 games/round)
    if len(f"apinr:{earliest_type}:{game_ids_str}") > 60:
        game_ids_str = game_ids_str[:60]

    try:
        await bot.send_message(
            config.admin_telegram_id,
            "\n".join(lines),
            reply_markup=api_new_round_kb(earliest_type, game_ids_str),
        )
    except Exception as exc:
        log.error("check_new_round: failed to notify admin: %s", exc)


# ── Combined daily job ────────────────────────────────────────────────────────

async def _daily_api_check(bot: Bot) -> None:
    await _check_results(bot)
    await _check_new_round(bot)


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_scheduler(bot: Bot) -> None:
    scheduler.add_job(_lock_started_matches, "interval", minutes=1, id="lock_matches")
    scheduler.add_job(
        _send_reminders, "interval", minutes=30, id="reminders", kwargs={"bot": bot}
    )
    scheduler.add_job(
        _daily_api_check,
        "cron",
        hour=10,
        minute=0,
        id="daily_api_check",
        kwargs={"bot": bot},
    )
    scheduler.start()
