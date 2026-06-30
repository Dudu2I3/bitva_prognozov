from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery

from bot.database.db import get_db, fetchone, fetchall
from bot.keyboards import (
    home_score_kb, away_score_kb, doubling_kb,
    playoff_team_kb, playoff_method_kb,
    matches_filter_kb,
)

router = Router()

MSK_TZ = timezone(timedelta(hours=3))

_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _fmt_date(dt: datetime) -> str:
    return f"{dt.day} {_MONTHS_RU[dt.month]}"


def _now_msk_str() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------- /start ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tg = message.from_user
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (tg.id, tg.username, tg.full_name),
        )
        await db.commit()
    await message.answer(
        f"Привет, {tg.full_name}! Ты зарегистрирован в «Битве прогнозов ЧМ-2026».\n"
        "Используй /matches чтобы увидеть ближайшие матчи и сделать прогноз."
    )


# ---------- /help ----------

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📋 <b>Команды бота:</b>\n\n"
        "/start — Регистрация\n"
        "/matches — Ближайшие матчи и прогнозы\n"
        "/my_predictions — Мои прогнозы\n"
        "/today_results — Результаты сегодняшних матчей\n"
        "/me — Мой профиль и статистика\n"
        "/standings — Общий рейтинг\n"
        "/help — Этот список команд"
    )


# ---------- /matches + filter callbacks ----------

async def _get_matches_with_preds(
    db, user_id: int, period: str
) -> tuple[list[dict], str]:
    now_msk = datetime.now(MSK_TZ)
    today = now_msk.strftime("%Y-%m-%d")

    match period:
        case "today":
            cond = "DATE(kickoff_msk) = ?"
            params: tuple = (today,)
            label = f"Сегодня, {_fmt_date(now_msk)}"
        case "tomorrow":
            tom = now_msk + timedelta(days=1)
            cond = "DATE(kickoff_msk) = ?"
            params = (tom.strftime("%Y-%m-%d"),)
            label = f"Завтра, {_fmt_date(tom)}"
        case "week":
            week_end = (now_msk + timedelta(days=7)).strftime("%Y-%m-%d")
            cond = "DATE(kickoff_msk) BETWEEN ? AND ?"
            params = (today, week_end)
            label = "Эта неделя"
        case _:
            cond = "1=1"
            params = ()
            label = "Все туры"

    rows = await fetchall(
        db,
        f"""
        SELECT m.id, m.team_home, m.team_away, m.kickoff_msk, m.stage,
               p.pred_home, p.pred_away, p.is_doubled
        FROM matches m
        LEFT JOIN predictions p ON p.match_id = m.id AND p.user_id = ?
        WHERE m.status = 'scheduled' AND {cond}
        ORDER BY m.kickoff_msk
        LIMIT 20
        """,
        (user_id, *params),
    )

    result: list[dict] = []
    for r in rows:
        m = {
            "id": r["id"],
            "team_home": r["team_home"],
            "team_away": r["team_away"],
            "kickoff_msk": r["kickoff_msk"],
            "stage": r["stage"],
            "pred": (
                {
                    "pred_home": r["pred_home"],
                    "pred_away": r["pred_away"],
                    "is_doubled": bool(r["is_doubled"]),
                }
                if r["pred_home"] is not None
                else None
            ),
        }
        result.append(m)
    return result, label


def _format_matches_msg(matches: list[dict], label: str) -> str:
    if not matches:
        return f"⚽ <b>Матчи — {label}:</b>\n\nНет предстоящих матчей."
    lines = [f"⚽ <b>Матчи — {label}:</b>\n"]
    for m in matches:
        time_str = str(m["kickoff_msk"])[11:16]
        lines.append(f"🕐 {time_str}  <b>{m['team_home']} — {m['team_away']}</b>")
        pred = m.get("pred")
        if pred:
            dbl = " (×2)" if pred["is_doubled"] else ""
            lines.append(f"   ✅ {pred['pred_home']}:{pred['pred_away']}{dbl}")
        else:
            lines.append("   📝 Прогноз не сделан")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _render_matches(
    target,
    user_telegram_id: int,
    period: str,
    edit: bool = False,
) -> None:
    async with get_db() as db:
        user = await fetchone(
            db, "SELECT id FROM users WHERE telegram_id = ?", (user_telegram_id,)
        )
        if not user:
            text = "Сначала напиши /start."
            if edit:
                await target.message.edit_text(text)
            else:
                await target.answer(text)
            return
        matches, label = await _get_matches_with_preds(db, user["id"], period)

    text = _format_matches_msg(matches, label)
    kb = matches_filter_kb(period, matches)
    if edit:
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("matches"))
async def cmd_matches(message: Message) -> None:
    await _render_matches(message, message.from_user.id, "all")


@router.callback_query(F.data.startswith("mf:"))
async def cb_matches_filter(callback: CallbackQuery) -> None:
    period = callback.data.split(":")[1]
    await _render_matches(callback, callback.from_user.id, period, edit=True)
    await callback.answer()


# ---------- /me ----------

@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    async with get_db() as db:
        user = await fetchone(
            db,
            "SELECT id, doublings_left FROM users WHERE telegram_id = ?",
            (message.from_user.id,),
        )
        if not user:
            await message.answer("Сначала напиши /start.")
            return
        uid = user["id"]
        stats = await fetchone(
            db,
            """
            SELECT
                COUNT(CASE WHEN m.team_home != '__adjustment__' THEN 1 END) AS total,
                SUM(CASE WHEN p.base_points = 3 AND m.team_home != '__adjustment__'
                         THEN 1 ELSE 0 END) AS exact,
                SUM(CASE WHEN p.base_points >= 1 AND m.team_home != '__adjustment__'
                         THEN 1 ELSE 0 END) AS correct_outcome,
                COALESCE(SUM(p.total_points), 0) AS points
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE p.user_id = ? AND p.total_points IS NOT NULL
            """,
            (uid,),
        )
        my_pts = stats["points"]
        rank_row = await fetchone(
            db,
            """
            SELECT COUNT(*) + 1 AS rank
            FROM (
                SELECT user_id, SUM(total_points) AS pts
                FROM predictions WHERE total_points IS NOT NULL GROUP BY user_id
            )
            WHERE pts > ?
            """,
            (my_pts,),
        )
        # Points of the player ranked one place above
        next_row = await fetchone(
            db,
            """
            SELECT MIN(pts) AS next_pts
            FROM (
                SELECT user_id, SUM(total_points) AS pts
                FROM predictions WHERE total_points IS NOT NULL GROUP BY user_id
                HAVING pts > ?
            )
            """,
            (my_pts,),
        )
    rank = rank_row["rank"] if rank_row else "—"
    s = stats
    gap_line = ""
    if next_row and next_row["next_pts"] is not None:
        gap = next_row["next_pts"] - my_pts
        gap_line = f"\n📈 До следующего места: {gap} очк."
    await message.answer(
        f"👤 <b>{message.from_user.full_name}</b>\n"
        f"🏅 Место: {rank}{gap_line}\n"
        f"⭐ Очки: {my_pts}\n"
        f"🎯 Точных счётов: {s['exact'] or 0}\n"
        f"✅ Угаданных исходов: {s['correct_outcome'] or 0} из {s['total'] or 0}\n"
        f"✌️ Удвоений осталось: {user['doublings_left']}/8"
    )


# ---------- /standings ----------

@router.message(Command("standings"))
async def cmd_standings(message: Message) -> None:
    async with get_db() as db:
        rows = await fetchall(
            db,
            """
            SELECT u.full_name,
                   COALESCE(SUM(p.total_points), 0) AS pts,
                   SUM(CASE WHEN p.base_points = 3 THEN 1 ELSE 0 END) AS exact,
                   SUM(CASE WHEN p.base_points >= 1 THEN 1 ELSE 0 END) AS outcomes
            FROM users u
            LEFT JOIN predictions p ON p.user_id = u.id AND p.total_points IS NOT NULL
            GROUP BY u.id
            ORDER BY pts DESC, exact DESC, outcomes DESC
            """,
        )
    if not rows:
        await message.answer("Рейтинг пуст.")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>Рейтинг:</b>\n"]
    for i, r in enumerate(rows, 1):
        medal = medals.get(i, f"{i}.")
        lines.append(f"{medal} {r['full_name']} — {r['pts']} очк. (точных: {r['exact'] or 0})")
    await message.answer("\n".join(lines))


# ---------- /today_results ----------

@router.message(Command("today_results"))
async def cmd_today_results(message: Message) -> None:
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    async with get_db() as db:
        matches = await fetchall(
            db,
            """
            SELECT id, team_home, team_away, score_home, score_away,
                   went_to_extra_time, ot_pen_winner, ot_pen_method
            FROM matches
            WHERE status = 'finished' AND DATE(kickoff_msk) = ?
              AND team_home != '__adjustment__'
            ORDER BY kickoff_msk
            """,
            (today,),
        )
        if not matches:
            await message.answer(
                f"Сегодня ({_fmt_date(datetime.now(MSK_TZ))}) сыгранных матчей нет."
            )
            return

        lines = [f"📊 <b>Результаты — {_fmt_date(datetime.now(MSK_TZ))}:</b>\n"]
        for m in matches:
            ot_str = ""
            if m["went_to_extra_time"]:
                ot_str = f" ({m['ot_pen_winner']} через {m['ot_pen_method']})"
            lines.append(
                f"⚽ {m['team_home']} <b>{m['score_home']}:{m['score_away']}</b>"
                f" {m['team_away']}{ot_str}"
            )
            preds = await fetchall(
                db,
                """
                SELECT u.full_name, p.pred_home, p.pred_away, p.is_doubled,
                       p.total_points, p.base_points
                FROM predictions p
                JOIN users u ON u.id = p.user_id
                WHERE p.match_id = ? AND p.total_points IS NOT NULL
                ORDER BY p.total_points DESC
                """,
                (m["id"],),
            )
            if preds:
                for p in preds:
                    dbl = "×2 " if p["is_doubled"] else ""
                    icon = "🎯" if p["base_points"] == 3 else ("✅" if p["base_points"] >= 1 else "❌")
                    pts = p["total_points"]
                    lines.append(
                        f"  {icon} {p['full_name']}: {dbl}{p['pred_home']}:{p['pred_away']}"
                        f" → {pts:+d} очк."
                    )
            else:
                lines.append("  (прогнозов не было)")
            lines.append("")

    await message.answer("\n".join(lines).rstrip())


# ---------- /my_predictions ----------

@router.message(Command("my_predictions"))
async def cmd_my_predictions(message: Message) -> None:
    async with get_db() as db:
        user = await fetchone(
            db, "SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,)
        )
        if not user:
            await message.answer("Сначала напиши /start.")
            return
        rows = await fetchall(
            db,
            """
            SELECT m.team_home, m.team_away, m.kickoff_msk, m.status,
                   m.score_home, m.score_away,
                   p.pred_home, p.pred_away, p.is_doubled,
                   p.total_points, p.base_points
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE p.user_id = ? AND m.team_home != '__adjustment__'
            ORDER BY m.kickoff_msk
            """,
            (user["id"],),
        )
    if not rows:
        await message.answer("У тебя пока нет прогнозов.")
        return

    lines = ["📋 <b>Мои прогнозы:</b>\n"]
    for r in rows:
        dbl = "×2 " if r["is_doubled"] else ""
        pred_str = f"{dbl}{r['pred_home']}:{r['pred_away']}"
        if r["status"] == "finished" and r["total_points"] is not None:
            icon = "🎯" if r["base_points"] == 3 else ("✅" if r["base_points"] >= 1 else "❌")
            result_str = f"  ({r['score_home']}:{r['score_away']}) → {r['total_points']:+d} очк."
            lines.append(
                f"{icon} {r['team_home']} — {r['team_away']}: {pred_str}{result_str}"
            )
        else:
            date_str = str(r["kickoff_msk"])[:10]
            lines.append(f"⏳ {r['team_home']} — {r['team_away']} ({date_str}): {pred_str}")

    await message.answer("\n".join(lines))


# ---------- Prediction input callbacks ----------

@router.callback_query(F.data.startswith("predict:"))
async def cb_predict_start(callback: CallbackQuery) -> None:
    match_id = int(callback.data.split(":")[1])
    now = _now_msk_str()
    async with get_db() as db:
        match = await fetchone(
            db,
            "SELECT id, team_home, team_away, kickoff_msk, stage FROM matches WHERE id = ?",
            (match_id,),
        )
    if not match:
        await callback.answer("Матч не найден.", show_alert=True)
        return
    if match["kickoff_msk"] <= now:
        await callback.answer("Матч уже начался — прогноз заблокирован.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
        f"Введи счёт {match['team_home']}:",
        reply_markup=home_score_kb(match_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hd:"))
async def cb_home_digit(callback: CallbackQuery) -> None:
    _, match_id_str, digit_str = callback.data.split(":")
    match_id, digit = int(match_id_str), int(digit_str)
    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    await callback.message.edit_text(
        f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
        f"{match['team_home']}: {digit}\n"
        f"Введи счёт {match['team_away']}:",
        reply_markup=away_score_kb(match_id, digit),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ad:"))
async def cb_away_digit(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id, home_score, digit = int(parts[1]), int(parts[2]), int(parts[3])
    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    await callback.message.edit_text(
        f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
        f"Прогноз: {home_score}:{digit}\n"
        f"Удвоить?",
        reply_markup=doubling_kb(match_id, home_score, digit),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dbl:"))
async def cb_double_choice(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id, pred_home, pred_away = int(parts[1]), int(parts[2]), int(parts[3])
    is_doubled = parts[4] == "y"
    now = _now_msk_str()
    async with get_db() as db:
        match = await fetchone(
            db,
            "SELECT stage, team_home, team_away, kickoff_msk FROM matches WHERE id = ?",
            (match_id,),
        )
    if match["kickoff_msk"] <= now:
        await callback.answer("Матч уже начался.", show_alert=True)
        return
    if match["stage"] == "playoff" and pred_home == pred_away:
        await callback.message.edit_text(
            f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
            f"Прогноз: {pred_home}:{pred_away}{'  (×2)' if is_doubled else ''}\n"
            f"Ничья в плей-офф. Кто пройдёт дальше?",
            reply_markup=playoff_team_kb(
                match_id, pred_home, pred_away, int(is_doubled),
                match["team_home"], match["team_away"],
            ),
        )
    else:
        await _save_prediction(
            callback, match_id, pred_home, pred_away, is_doubled, None, None
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pt:"))
async def cb_playoff_team(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id = int(parts[1])
    pred_home, pred_away, doubled_int = int(parts[2]), int(parts[3]), int(parts[4])
    winner_code = parts[5]  # h / a / s
    is_doubled = bool(doubled_int)

    if winner_code == "s":
        await _save_prediction(
            callback, match_id, pred_home, pred_away, is_doubled, None, None
        )
        return

    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    team = match["team_home"] if winner_code == "h" else match["team_away"]
    await callback.message.edit_text(
        f"{callback.message.text}\nКоманда: {team}\nКак пройдёт?",
        reply_markup=playoff_method_kb(match_id, pred_home, pred_away, doubled_int, winner_code),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pm:"))
async def cb_playoff_method(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id = int(parts[1])
    pred_home, pred_away, doubled_int = int(parts[2]), int(parts[3]), int(parts[4])
    winner_code = parts[5]
    method_code = parts[6]  # O / P / S

    is_doubled = bool(doubled_int)
    method = {"O": "OT", "P": "PEN", "S": None}[method_code]

    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    team = match["team_home"] if winner_code == "h" else match["team_away"]
    await _save_prediction(
        callback, match_id, pred_home, pred_away, is_doubled, team, method
    )


async def _save_prediction(
    callback: CallbackQuery,
    match_id: int,
    pred_home: int,
    pred_away: int,
    is_doubled: bool,
    playoff_team: str | None,
    playoff_method: str | None,
) -> None:
    now = _now_msk_str()
    async with get_db() as db:
        match = await fetchone(
            db,
            "SELECT kickoff_msk, team_home, team_away FROM matches WHERE id = ?",
            (match_id,),
        )
        if match["kickoff_msk"] <= now:
            await callback.answer("Матч уже начался — прогноз заблокирован.", show_alert=True)
            return

        user = await fetchone(
            db,
            "SELECT id, doublings_left FROM users WHERE telegram_id = ?",
            (callback.from_user.id,),
        )
        if not user:
            await callback.answer("Сначала напиши /start.", show_alert=True)
            return

        existing = await fetchone(
            db,
            "SELECT is_doubled FROM predictions WHERE user_id = ? AND match_id = ?",
            (user["id"], match_id),
        )
        prev_doubled = bool(existing["is_doubled"]) if existing else False
        delta = int(is_doubled) - int(prev_doubled)
        new_left = user["doublings_left"] - delta
        if new_left < 0:
            await callback.answer("У тебя не осталось удвоений.", show_alert=True)
            return

        await db.execute(
            """
            INSERT INTO predictions (user_id, match_id, pred_home, pred_away,
                                     is_doubled, playoff_team, playoff_method)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, match_id) DO UPDATE SET
                pred_home = excluded.pred_home,
                pred_away = excluded.pred_away,
                is_doubled = excluded.is_doubled,
                playoff_team = excluded.playoff_team,
                playoff_method = excluded.playoff_method,
                submitted_at = CURRENT_TIMESTAMP
            """,
            (user["id"], match_id, pred_home, pred_away,
             is_doubled, playoff_team, playoff_method),
        )
        await db.execute(
            "UPDATE users SET doublings_left = ? WHERE id = ?", (new_left, user["id"])
        )
        await db.commit()

    double_str = "  (×2 удвоение)" if is_doubled else ""
    playoff_str = ""
    if playoff_team:
        playoff_str = f"\n🏆 Плей-офф: {playoff_team}"
        if playoff_method:
            playoff_str += f" ({playoff_method})"

    kickoff = str(match["kickoff_msk"])[:16]
    await callback.message.edit_text(
        f"✅ <b>Принято:</b> {match['team_home']} {pred_home}:{pred_away}"
        f" {match['team_away']}{double_str}{playoff_str}\n"
        f"⏰ Дедлайн: {kickoff} МСК"
    )
    await callback.answer()
