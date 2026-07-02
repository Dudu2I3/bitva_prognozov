import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot.config import config
from bot.database.db import get_db, fetchall, fetchone
from bot.services.api_client import (
    fetch_games, ru_to_en, en_to_ru, local_to_msk, ROUND_ORDER,
)
from bot.services.scoring import score_prediction, Prediction, Match as MatchData
from bot.keyboards import api_result_kb, api_new_round_kb

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

MSK_TZ = timezone(timedelta(hours=3))


def _now_msk() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _pts_str(pts: int) -> str:
    """Russian plural form for points."""
    a = abs(pts)
    if 11 <= a % 100 <= 19:
        form = "очков"
    elif a % 10 == 1:
        form = "очко"
    elif a % 10 in (2, 3, 4):
        form = "очка"
    else:
        form = "очков"
    return f"{pts:+d} {form}"


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
    """Send one reminder per match ~30 min before kickoff, only to users without a prediction."""
    now_msk = datetime.now(MSK_TZ)
    window_start = (now_msk + timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
    window_end = (now_msk + timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        upcoming = await fetchall(
            db,
            """SELECT id, team_home, team_away, kickoff_msk
               FROM matches
               WHERE status = 'scheduled'
                 AND kickoff_msk > ? AND kickoff_msk <= ?
                 AND reminder_sent = FALSE""",
            (window_start, window_end),
        )
        if not upcoming:
            return
        for match in upcoming:
            no_pred_users = await fetchall(
                db,
                """SELECT u.telegram_id FROM users u
                   WHERE NOT EXISTS (
                       SELECT 1 FROM predictions p
                       WHERE p.match_id = ? AND p.user_id = u.id
                   )""",
                (match["id"],),
            )
            time_str = str(match["kickoff_msk"])[11:16]
            for u in no_pred_users:
                try:
                    await bot.send_message(
                        u["telegram_id"],
                        f"⏰ Через ~30 минут: <b>{match['team_home']} — {match['team_away']}</b>"
                        f" ({time_str} МСК)\nПрогноз не сделан! → /matches",
                    )
                except Exception:
                    pass
            await db.execute(
                "UPDATE matches SET reminder_sent = TRUE WHERE id = ?", (match["id"],)
            )
        await db.commit()


# ── Day summary ──────────────────────────────────────────────────────────────

_day_summaries_sent: set[str] = set()  # in-memory guard, resets on restart

_MONTHS_SHORT = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


async def _maybe_send_day_summary(bot: Bot, match_date: str) -> None:
    """After a result is saved, check if all matches of the day are finished.
    If so, broadcast a one-line-per-player day summary to all users (once per date)."""
    if match_date in _day_summaries_sent:
        return
    async with get_db() as db:
        remaining = await fetchone(
            db,
            """SELECT COUNT(*) AS cnt FROM matches
               WHERE DATE(kickoff_msk) = ? AND status = 'scheduled'
                 AND team_home != '__adjustment__'""",
            (match_date,),
        )
        if not remaining or remaining["cnt"] > 0:
            return
        finished = await fetchone(
            db,
            """SELECT COUNT(*) AS cnt FROM matches
               WHERE DATE(kickoff_msk) = ? AND status = 'finished'
                 AND team_home != '__adjustment__'""",
            (match_date,),
        )
        if not finished or finished["cnt"] == 0:
            return

        # Per-user points today (all users, 0 if no predictions)
        day_rows = await fetchall(
            db,
            """SELECT u.full_name, u.telegram_id,
                      COALESCE((
                          SELECT SUM(p.total_points) FROM predictions p
                          JOIN matches m ON m.id = p.match_id
                          WHERE p.user_id = u.id
                            AND DATE(m.kickoff_msk) = ?
                            AND m.team_home != '__adjustment__'
                            AND p.total_points IS NOT NULL
                      ), 0) AS day_pts
               FROM users u
               ORDER BY day_pts DESC, u.full_name""",
            (match_date,),
        )
        all_users = await fetchall(db, "SELECT telegram_id FROM users")

    if not day_rows:
        return

    _day_summaries_sent.add(match_date)

    dt = datetime.strptime(match_date, "%Y-%m-%d")
    date_label = f"{dt.day} {_MONTHS_SHORT[dt.month]}"
    lines = [f"📊 <b>Итог дня — {date_label}:</b>\n"]
    max_pts = day_rows[0]["day_pts"]
    for r in day_rows:
        pts = r["day_pts"]
        crown = "🏆 " if pts == max_pts and pts > 0 else "   "
        sign = "+" if pts > 0 else ""
        lines.append(f"{crown}{sign}{pts}  {r['full_name']}")
    lines.append("\n/standings — текущий рейтинг")
    text = "\n".join(lines)

    for u in all_users:
        try:
            await bot.send_message(u["telegram_id"], text)
        except Exception:
            pass


# ── Shared recalc + publish helper ───────────────────────────────────────────

async def _recalc_and_publish(bot: Bot, match_id: int) -> int:
    """Recalculate all prediction scores for a finished match, publish to group. Returns count."""
    async with get_db() as db:
        match_row = await fetchone(db, "SELECT * FROM matches WHERE id = ?", (match_id,))
        if not match_row or match_row["status"] != "finished":
            return 0
        match = MatchData(
            score_home=match_row["score_home"],
            score_away=match_row["score_away"],
            went_to_extra_time=bool(match_row["went_to_extra_time"]),
            ot_pen_winner=match_row["ot_pen_winner"],
            ot_pen_method=match_row["ot_pen_method"],
        )
        preds = await fetchall(db, "SELECT * FROM predictions WHERE match_id = ?", (match_id,))
        for row in preds:
            pred = Prediction(
                pred_home=row["pred_home"], pred_away=row["pred_away"],
                is_doubled=bool(row["is_doubled"]),
                playoff_team=row["playoff_team"], playoff_method=row["playoff_method"],
            )
            r = score_prediction(pred, match)
            await db.execute(
                "UPDATE predictions SET base_points=?, base_final=?, bonus_points=?, total_points=? WHERE id=?",
                (r["base_points"], r["base_final"], r["bonus_points"], r["total_points"], row["id"]),
            )
        await db.commit()

        ot_str = ""
        if match_row["went_to_extra_time"]:
            ot_str = f" ({match_row['ot_pen_winner']} через {match_row['ot_pen_method']})"
        header = (
            f"⚽ <b>{match_row['team_home']} "
            f"{match_row['score_home']}:{match_row['score_away']} "
            f"{match_row['team_away']}</b>{ot_str}"
        )

        # Group-chat summary
        if config.group_chat_id:
            scored = await fetchall(
                db,
                """SELECT u.full_name, p.pred_home, p.pred_away, p.is_doubled,
                          p.total_points, p.base_points
                   FROM predictions p JOIN users u ON u.id = p.user_id
                   WHERE p.match_id = ? AND p.total_points IS NOT NULL
                   ORDER BY p.total_points DESC""",
                (match_id,),
            )
            lines = [header + "\n"]
            for p in scored:
                dbl = "×2 " if p["is_doubled"] else ""
                icon = "🎯" if p["base_points"] == 3 else ("✅" if p["base_points"] >= 1 else "❌")
                lines.append(
                    f"{icon} {p['full_name']}: {dbl}{p['pred_home']}:{p['pred_away']}"
                    f" → {p['total_points']:+d} очк."
                )
            try:
                await bot.send_message(config.group_chat_id, "\n".join(lines))
            except Exception as exc:
                log.error("publish_to_group failed: %s", exc)

        # Individual notifications
        user_preds = await fetchall(
            db,
            """SELECT u.telegram_id, p.pred_home, p.pred_away, p.is_doubled,
                      p.playoff_team, p.playoff_method,
                      p.total_points, p.base_points
               FROM predictions p JOIN users u ON u.id = p.user_id
               WHERE p.match_id = ? AND p.total_points IS NOT NULL""",
            (match_id,),
        )
        for p in user_preds:
            try:
                dbl = "×2 " if p["is_doubled"] else ""
                icon = "🎯" if p["base_points"] == 3 else ("✅" if p["base_points"] >= 1 else "❌")
                pred_str = f"{dbl}{p['pred_home']}:{p['pred_away']}"
                if p["playoff_team"]:
                    m_str = f" {p['playoff_method']}" if p["playoff_method"] else ""
                    pred_str += f", {p['playoff_team']}{m_str}"
                await bot.send_message(
                    p["telegram_id"],
                    f"{header}\n\n"
                    f"Твой прогноз: {pred_str}\n"
                    f"{icon} {_pts_str(p['total_points'])}",
                )
            except Exception as exc:
                log.debug("Individual notify failed for %s: %s", p["telegram_id"], exc)

    match_date = str(match_row["kickoff_msk"])[:10]
    await _maybe_send_day_summary(bot, match_date)
    return len(preds)


def _detect_ot_pen(api_game: dict) -> tuple[str | None, str | None]:
    """
    Try to extract (winner_en, method) from an API game object.
    Returns (None, None) if not available.
    method is 'OT' or 'PEN'.
    """
    winner_en = (
        api_game.get("winner") or
        api_game.get("pen_winner") or
        api_game.get("extra_time_winner") or
        api_game.get("winner_en") or
        api_game.get("knockout_winner")
    )
    method: str | None = None
    if api_game.get("penalties") in ("TRUE", "true", True, 1, "1"):
        method = "PEN"
    elif api_game.get("extra_time") in ("TRUE", "true", True, 1, "1"):
        method = "OT"
    elif api_game.get("overtime") in ("TRUE", "true", True, 1, "1"):
        method = "OT"
    return (winner_en or None, method)


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

        # Playoff draw: try auto-detect OT/PEN winner
        if is_playoff and is_draw:
            winner_en, method = _detect_ot_pen(api_game)
            if winner_en and method:
                winner_ru = en_to_ru(winner_en)
                async with get_db() as db:
                    await db.execute(
                        """UPDATE matches
                           SET score_home=?, score_away=?, status='finished',
                               went_to_extra_time=TRUE, ot_pen_winner=?, ot_pen_method=?
                           WHERE id=?""",
                        (home_s, away_s, winner_ru, method, match_id),
                    )
                    await db.commit()
                count = await _recalc_and_publish(bot, match_id)
                try:
                    await bot.send_message(
                        config.admin_telegram_id,
                        f"✅ Авто (API): <b>{match['team_home']} {home_s}:{away_s}"
                        f" {match['team_away']}</b>\n"
                        f"➡️ {winner_ru} ({method})\n"
                        f"Пересчитано прогнозов: {count}",
                    )
                except Exception as exc:
                    log.error("check_results: admin notify failed for match %s: %s", match_id, exc)
                continue  # done for this match

            # API has no OT/PEN info yet — log for debug and fall through to manual
            log.info(
                "check_results: playoff draw match %s, OT/PEN not in API yet. Fields: %s",
                match_id,
                {k: v for k, v in api_game.items()
                 if k not in ("home_team_name_en", "away_team_name_en")},
            )

        text = (
            f"🔔 Результат из API:\n"
            f"<b>{match['team_home']} {home_s}:{away_s} {match['team_away']}</b>\n"
        )
        if is_playoff and is_draw:
            text += "\n⚠️ Ничья в плей-офф — API не дал победителя, выбери вручную."

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
        kickoff_msk = local_to_msk(g["local_date"], g.get("stadium_id", ""))
        msk_label = kickoff_msk[5:16].replace("-", ".").replace(" ", " ")  # "06.28 15:00"
        lines.append(
            f"• {en_to_ru(g['home_team_name_en'])} — {en_to_ru(g['away_team_name_en'])}"
            f"  {msk_label} МСК"
        )
        game_ids.append(str(g["id"]))

    lines.append("\nВремя будет автоматически конвертировано в МСК.")

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
    # Lock predictions every minute when match starts
    scheduler.add_job(_lock_started_matches, "interval", minutes=1, id="lock_matches")
    # Single reminder ~30 min before kickoff (runs every 5 min, uses reminder_sent flag)
    scheduler.add_job(_send_reminders, "interval", minutes=5, id="reminders", kwargs={"bot": bot})
    # Check API results every 45 min (results can arrive throughout the day)
    scheduler.add_job(_check_results, "interval", minutes=45, id="check_results", kwargs={"bot": bot})
    # Check for new playoff rounds once a day at 10:00 MSK
    scheduler.add_job(_check_new_round, "cron", hour=10, minute=0, id="check_new_round", kwargs={"bot": bot})
    scheduler.start()
