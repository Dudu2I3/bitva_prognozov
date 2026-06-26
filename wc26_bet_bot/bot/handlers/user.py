import json
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery

from bot.database.db import get_db
from bot.keyboards import matches_list_kb, score_input_kb, doubling_kb, playoff_team_kb, playoff_method_kb

router = Router()

# ---------- /start ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tg = message.from_user
    async with await get_db() as db:
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


# ---------- /matches ----------

@router.message(Command("matches"))
async def cmd_matches(message: Message) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with await get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, team_home, team_away, kickoff_msk FROM matches "
            "WHERE status = 'scheduled' AND kickoff_msk > ? ORDER BY kickoff_msk LIMIT 20",
            (now,),
        )
    if not rows:
        await message.answer("Нет предстоящих матчей.")
        return
    matches = [dict(r) for r in rows]
    await message.answer("Выбери матч для прогноза:", reply_markup=matches_list_kb(matches))


# ---------- /me ----------

@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    async with await get_db() as db:
        user = await db.execute_fetchone(
            "SELECT id, doublings_left FROM users WHERE telegram_id = ?",
            (message.from_user.id,),
        )
        if not user:
            await message.answer("Сначала напиши /start.")
            return
        stats = await db.execute_fetchone(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN base_points = 3 THEN 1 ELSE 0 END) AS exact,
                SUM(CASE WHEN base_points >= 1 THEN 1 ELSE 0 END) AS correct_outcome,
                COALESCE(SUM(total_points), 0) AS points
            FROM predictions
            WHERE user_id = ? AND total_points IS NOT NULL
            """,
            (user["id"],),
        )
        rank_row = await db.execute_fetchone(
            """
            SELECT COUNT(*) + 1 AS rank
            FROM (
                SELECT user_id, SUM(total_points) AS pts
                FROM predictions WHERE total_points IS NOT NULL
                GROUP BY user_id
            )
            WHERE pts > (
                SELECT COALESCE(SUM(total_points), 0)
                FROM predictions WHERE user_id = ? AND total_points IS NOT NULL
            )
            """,
            (user["id"],),
        )
    rank = rank_row["rank"] if rank_row else "—"
    s = stats
    await message.answer(
        f"👤 {message.from_user.full_name}\n"
        f"🏅 Место: {rank}\n"
        f"⭐ Очки: {s['points']}\n"
        f"🎯 Точных счётов: {s['exact'] or 0}\n"
        f"✅ Угаданных исходов: {s['correct_outcome'] or 0} из {s['total']}\n"
        f"✌️ Удвоений осталось: {user['doublings_left']}/8"
    )


# ---------- /standings ----------

@router.message(Command("standings"))
async def cmd_standings(message: Message) -> None:
    async with await get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT u.full_name,
                   COALESCE(SUM(p.total_points), 0) AS pts,
                   SUM(CASE WHEN p.base_points = 3 THEN 1 ELSE 0 END) AS exact,
                   SUM(CASE WHEN p.base_points >= 1 THEN 1 ELSE 0 END) AS outcomes
            FROM users u
            LEFT JOIN predictions p ON p.user_id = u.id AND p.total_points IS NOT NULL
            GROUP BY u.id
            ORDER BY pts DESC, exact DESC, outcomes DESC
            """
        )
    if not rows:
        await message.answer("Рейтинг пуст.")
        return
    lines = ["🏆 Рейтинг:\n"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(rows, 1):
        medal = medals.get(i, f"{i}.")
        lines.append(f"{medal} {r['full_name']} — {r['pts']} очк. (точных: {r['exact'] or 0})")
    await message.answer("\n".join(lines))


# ---------- Callback: выбор матча → начало ввода прогноза ----------

# FSM-state хранится прямо в callback_data цепочке (без aiogram FSM)
# Формат ввода: predict → home_score → away_score → double → [playoff_team → playoff_method] → сохранение

@router.callback_query(F.data.startswith("predict:"))
async def cb_predict_start(callback: CallbackQuery) -> None:
    match_id = int(callback.data.split(":")[1])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with await get_db() as db:
        match = await db.execute_fetchone(
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
        f"Матч: {match['team_home']} — {match['team_away']}\n"
        f"Введи счёт хозяев:",
        reply_markup=score_input_kb(match_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("digit:"))
async def cb_digit(callback: CallbackQuery) -> None:
    _, match_id_str, digit_str = callback.data.split(":")
    match_id = int(match_id_str)
    digit = int(digit_str)
    text = callback.message.text or ""

    if "счёт хозяев" in text:
        await callback.message.edit_text(
            f"{text.splitlines()[0]}\nХозяева: {digit}\nВведи счёт гостей:",
            reply_markup=score_input_kb(match_id),
        )
    elif "счёт гостей" in text:
        home_score = int(text.split("Хозяева:")[1].split("\n")[0].strip())
        await callback.message.edit_text(
            f"{text.splitlines()[0]}\nПрогноз: {home_score}:{digit}\nУдвоить?",
            reply_markup=doubling_kb(match_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("double:"))
async def cb_double(callback: CallbackQuery) -> None:
    _, match_id_str, choice = callback.data.split(":")
    match_id = int(match_id_str)
    is_doubled = choice == "yes"
    text = callback.message.text or ""
    score_part = text.split("Прогноз:")[1].split("\n")[0].strip()
    home_str, away_str = score_part.split(":")
    pred_home, pred_away = int(home_str), int(away_str)

    # Check if prediction is a draw — offer playoff pick for playoff stage
    async with await get_db() as db:
        match = await db.execute_fetchone(
            "SELECT stage, team_home, team_away, kickoff_msk FROM matches WHERE id = ?",
            (match_id,),
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if match["kickoff_msk"] <= now:
        await callback.answer("Матч уже начался.", show_alert=True)
        return

    # Encode current state in next message text so we can retrieve it later
    state_line = f"[state:{match_id}:{pred_home}:{pred_away}:{int(is_doubled)}]"

    if match["stage"] == "playoff" and pred_home == pred_away:
        await callback.message.edit_text(
            f"{text.splitlines()[0]}\n{state_line}\n"
            "Прогноз = ничья в плей-офф. Кто пройдёт дальше?",
            reply_markup=playoff_team_kb(match_id, match["team_home"], match["team_away"]),
        )
    else:
        await _save_prediction(callback, match_id, pred_home, pred_away, is_doubled, None, None)
    await callback.answer()


@router.callback_query(F.data.startswith("playoff_team:"))
async def cb_playoff_team(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id = int(parts[1])
    team = parts[2]  # team name or 'skip'

    if team == "skip":
        state = _parse_state(callback.message.text or "")
        await _save_prediction(callback, match_id, state["pred_home"], state["pred_away"],
                               state["is_doubled"], None, None)
        return

    await callback.message.edit_text(
        f"{callback.message.text}\nКоманда: {team}\nКак пройдёт?",
        reply_markup=playoff_method_kb(match_id, team),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("playoff_method:"))
async def cb_playoff_method(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id = int(parts[1])
    team = parts[2]
    method_raw = parts[3]  # 'OT' | 'PEN' | 'skip'
    method = None if method_raw == "skip" else method_raw

    state = _parse_state(callback.message.text or "")
    await _save_prediction(callback, match_id, state["pred_home"], state["pred_away"],
                           state["is_doubled"], team, method)


def _parse_state(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("[state:"):
            _, mid, ph, pa, dbl = line.strip("[]").split(":")
            return {"match_id": int(mid), "pred_home": int(ph),
                    "pred_away": int(pa), "is_doubled": bool(int(dbl))}
    return {}


async def _save_prediction(
    callback: CallbackQuery,
    match_id: int,
    pred_home: int,
    pred_away: int,
    is_doubled: bool,
    playoff_team: str | None,
    playoff_method: str | None,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with await get_db() as db:
        match = await db.execute_fetchone(
            "SELECT kickoff_msk FROM matches WHERE id = ?", (match_id,)
        )
        if match["kickoff_msk"] <= now:
            await callback.answer("Матч уже начался — прогноз заблокирован.", show_alert=True)
            return

        user = await db.execute_fetchone(
            "SELECT id, doublings_left FROM users WHERE telegram_id = ?",
            (callback.from_user.id,),
        )
        if not user:
            await callback.answer("Сначала напиши /start.", show_alert=True)
            return

        if is_doubled:
            if user["doublings_left"] <= 0:
                await callback.answer("У тебя не осталось удвоений.", show_alert=True)
                return

        # Check for existing prediction (for doubling_left deduction logic)
        existing = await db.execute_fetchone(
            "SELECT is_doubled FROM predictions WHERE user_id = ? AND match_id = ?",
            (user["id"], match_id),
        )
        # Adjust doublings_left
        prev_doubled = existing["is_doubled"] if existing else False
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
            "UPDATE users SET doublings_left = ? WHERE id = ?",
            (new_left, user["id"]),
        )
        await db.commit()

    double_str = " (удвоение ✅)" if is_doubled else ""
    playoff_str = ""
    if playoff_team:
        playoff_str = f"\nПлей-офф: {playoff_team}" + (f" ({playoff_method})" if playoff_method else "")
    await callback.message.edit_text(
        f"✅ Прогноз сохранён: {pred_home}:{pred_away}{double_str}{playoff_str}"
    )
    await callback.answer()
