"""
Admin-only handlers: /result (multi-step), /playoff_result, /recalc (with confirm),
/add_match, /admin_log.  All guarded by ADMIN_TELEGRAM_ID.
"""
import csv
import io
import json
from datetime import datetime, timezone, timedelta

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, Document, CallbackQuery

from bot.config import config
from bot.database.db import get_db, fetchone, fetchall
from bot.keyboards import (
    admin_match_list_kb, admin_home_score_kb, admin_away_score_kb,
    admin_playoff_team_kb, admin_playoff_method_kb, admin_confirm_kb,
    recalc_confirm_kb,
)
from bot.services.scoring import score_prediction, Prediction, Match

router = Router()
router.message.filter(F.from_user.id == config.admin_telegram_id)
router.callback_query.filter(F.from_user.id == config.admin_telegram_id)

MSK_TZ = timezone(timedelta(hours=3))


# ---------- helpers ----------

def _parse_score(text: str) -> tuple[int, int] | None:
    try:
        parts = text.strip().split(":")
        if len(parts) != 2:
            return None
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


async def _recalculate_match(db, match_id: int) -> int:
    """Recalculate scores for all predictions on a match. Returns count updated."""
    match_row = await fetchone(db, "SELECT * FROM matches WHERE id = ?", (match_id,))
    if not match_row or match_row["status"] != "finished":
        return 0

    match = Match(
        score_home=match_row["score_home"],
        score_away=match_row["score_away"],
        went_to_extra_time=bool(match_row["went_to_extra_time"]),
        ot_pen_winner=match_row["ot_pen_winner"],
        ot_pen_method=match_row["ot_pen_method"],
    )
    predictions = await fetchall(
        db, "SELECT * FROM predictions WHERE match_id = ?", (match_id,)
    )
    for row in predictions:
        pred = Prediction(
            pred_home=row["pred_home"],
            pred_away=row["pred_away"],
            is_doubled=bool(row["is_doubled"]),
            playoff_team=row["playoff_team"],
            playoff_method=row["playoff_method"],
        )
        result = score_prediction(pred, match)
        await db.execute(
            """
            UPDATE predictions
            SET base_points = ?, base_final = ?, bonus_points = ?, total_points = ?
            WHERE id = ?
            """,
            (result["base_points"], result["base_final"],
             result["bonus_points"], result["total_points"], row["id"]),
        )
    await db.commit()
    return len(predictions)


async def _log_admin_action(db, match_id: int | None, action: str, payload: dict) -> None:
    payload["admin_tg"] = config.admin_telegram_id
    await db.execute(
        "INSERT INTO audit_log (user_id, match_id, action, payload, created_at) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
        (config.admin_telegram_id, match_id, action, json.dumps(payload, ensure_ascii=False)),
    )
    await db.commit()


async def _publish_result(bot: Bot, match_id: int, db) -> None:
    """Post result summary to group chat if GROUP_CHAT_ID is configured."""
    if not config.group_chat_id:
        return
    match_row = await fetchone(db, "SELECT * FROM matches WHERE id = ?", (match_id,))
    if not match_row:
        return
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
        (match_id,),
    )
    ot_str = ""
    if match_row["went_to_extra_time"]:
        ot_str = f" ({match_row['ot_pen_winner']} через {match_row['ot_pen_method']})"
    lines = [
        f"⚽ <b>{match_row['team_home']} {match_row['score_home']}:{match_row['score_away']}"
        f" {match_row['team_away']}</b>{ot_str}\n"
    ]
    if preds:
        for p in preds:
            dbl = "×2 " if p["is_doubled"] else ""
            icon = "🎯" if p["base_points"] == 3 else ("✅" if p["base_points"] >= 1 else "❌")
            pts = p["total_points"]
            lines.append(
                f"{icon} {p['full_name']}: {dbl}{p['pred_home']}:{p['pred_away']}"
                f" → {pts:+d} очк."
            )
    else:
        lines.append("Прогнозов не было.")
    try:
        await bot.send_message(config.group_chat_id, "\n".join(lines))
    except Exception:
        pass


# ---------- /result — multi-step flow ----------

@router.message(Command("result"))
async def cmd_result(message: Message) -> None:
    args = (message.text or "").split(maxsplit=2)
    if len(args) >= 3:
        # Legacy: /result <match_id> <score>
        try:
            match_id = int(args[1])
        except ValueError:
            await message.answer("match_id должен быть числом.")
            return
        parsed = _parse_score(args[2])
        if not parsed:
            await message.answer("Неверный формат счёта. Пример: 2:1")
            return
        home, away = parsed
        async with get_db() as db:
            match = await fetchone(
                db, "SELECT id, team_home, team_away FROM matches WHERE id = ?", (match_id,)
            )
            if not match:
                await message.answer(f"Матч #{match_id} не найден.")
                return
            await db.execute(
                "UPDATE matches SET score_home=?, score_away=?, status='finished' WHERE id=?",
                (home, away, match_id),
            )
            await db.commit()
            count = await _recalculate_match(db, match_id)
            await _log_admin_action(db, match_id, "admin:result", {"score": f"{home}:{away}"})
            await _publish_result(message.bot, match_id, db)
        await message.answer(
            f"✅ Матч #{match_id} ({match['team_home']} — {match['team_away']}): "
            f"{home}:{away}\nПересчитано: {count}"
        )
        return

    # Interactive flow: show match list
    async with get_db() as db:
        rows = await fetchall(
            db,
            """SELECT id, team_home, team_away, kickoff_msk
               FROM matches WHERE status = 'scheduled'
               ORDER BY kickoff_msk LIMIT 20""",
        )
    if not rows:
        await message.answer("Нет матчей ожидающих результата.")
        return
    await message.answer("Выбери матч:", reply_markup=admin_match_list_kb(list(rows)))


@router.callback_query(F.data.startswith("arm:"))
async def cb_admin_match_select(callback: CallbackQuery) -> None:
    match_id = int(callback.data.split(":")[1])
    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    if not match:
        await callback.answer("Матч не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
        f"Введи счёт {match['team_home']}:",
        reply_markup=admin_home_score_kb(match_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ahd:"))
async def cb_admin_home_digit(callback: CallbackQuery) -> None:
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
        reply_markup=admin_away_score_kb(match_id, digit),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("aad:"))
async def cb_admin_away_digit(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id, home_s, away_s = int(parts[1]), int(parts[2]), int(parts[3])
    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away, stage FROM matches WHERE id = ?", (match_id,)
        )
    # If playoff match and draw → ask for OT/PEN winner
    if match["stage"] == "playoff" and home_s == away_s:
        await callback.message.edit_text(
            f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
            f"Счёт: {home_s}:{away_s} (ничья)\nКто прошёл дальше?",
            reply_markup=admin_playoff_team_kb(
                match_id, home_s, away_s, match["team_home"], match["team_away"]
            ),
        )
    else:
        # Go straight to confirmation
        await _show_admin_confirm(
            callback, match_id, home_s, away_s,
            match["team_home"], match["team_away"],
            winner="n", method="n",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("apt:"))
async def cb_admin_playoff_team(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id, home_s, away_s, winner = int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    team_label = match["team_home"] if winner == "h" else match["team_away"]
    await callback.message.edit_text(
        f"Матч: <b>{match['team_home']} — {match['team_away']}</b>\n"
        f"Счёт: {home_s}:{away_s}  Победитель: {team_label}\nКак прошёл?",
        reply_markup=admin_playoff_method_kb(match_id, home_s, away_s, winner),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("apm:"))
async def cb_admin_playoff_method(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id, home_s, away_s = int(parts[1]), int(parts[2]), int(parts[3])
    winner, method = parts[4], parts[5]  # h/a, OT/PEN/n
    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
    await _show_admin_confirm(
        callback, match_id, home_s, away_s,
        match["team_home"], match["team_away"],
        winner=winner, method=method,
    )
    await callback.answer()


async def _show_admin_confirm(
    callback: CallbackQuery,
    match_id: int, home_s: int, away_s: int,
    team_home: str, team_away: str,
    winner: str, method: str,
) -> None:
    extra = ""
    if winner != "n":
        team_label = team_home if winner == "h" else team_away
        meth_label = "" if method == "n" else f" через {method}"
        extra = f"\nПобедитель: {team_label}{meth_label}"
    await callback.message.edit_text(
        f"Подтвердить результат?\n\n"
        f"Матч: <b>{team_home} — {team_away}</b>\n"
        f"Счёт: <b>{home_s}:{away_s}</b>{extra}",
        reply_markup=admin_confirm_kb(match_id, home_s, away_s, winner, method),
    )


@router.callback_query(F.data.startswith("acf:"))
async def cb_admin_confirm(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    match_id, home_s, away_s = int(parts[1]), int(parts[2]), int(parts[3])
    winner, method = parts[4], parts[5]

    async with get_db() as db:
        match = await fetchone(
            db, "SELECT team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден.", show_alert=True)
            return

        went_extra = winner != "n"
        ot_winner: str | None = None
        ot_method: str | None = None
        if went_extra:
            ot_winner = match["team_home"] if winner == "h" else match["team_away"]
            ot_method = None if method == "n" else method

        await db.execute(
            """UPDATE matches SET score_home=?, score_away=?, status='finished',
               went_to_extra_time=?, ot_pen_winner=?, ot_pen_method=? WHERE id=?""",
            (home_s, away_s, went_extra, ot_winner, ot_method, match_id),
        )
        await db.commit()
        count = await _recalculate_match(db, match_id)
        await _log_admin_action(
            db, match_id, "admin:result",
            {"score": f"{home_s}:{away_s}", "winner": ot_winner, "method": ot_method},
        )
        await _publish_result(callback.bot, match_id, db)

    extra = ""
    if ot_winner:
        meth_label = f" ({ot_method})" if ot_method else ""
        extra = f"\n🏆 Победитель: {ot_winner}{meth_label}"
    await callback.message.edit_text(
        f"✅ Результат сохранён\n"
        f"{match['team_home']} <b>{home_s}:{away_s}</b> {match['team_away']}{extra}\n"
        f"Пересчитано прогнозов: {count}"
    )
    await callback.answer()


@router.callback_query(F.data == "acancel")
async def cb_admin_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ---------- /playoff_result <match_id> <team> <OT|PEN> (kept for backward compat) ----------

@router.message(Command("playoff_result"))
async def cmd_playoff_result(message: Message) -> None:
    args = (message.text or "").split(maxsplit=3)
    if len(args) < 4:
        await message.answer("Использование: /playoff_result &lt;match_id&gt; &lt;команда&gt; &lt;OT|PEN&gt;")
        return
    match_id_str, team, method = args[1], args[2], args[3].upper()
    if method not in ("OT", "PEN"):
        await message.answer("Метод должен быть OT или PEN.")
        return
    try:
        match_id = int(match_id_str)
    except ValueError:
        await message.answer("match_id должен быть числом.")
        return

    async with get_db() as db:
        row = await fetchone(
            db,
            "SELECT id FROM matches WHERE id = ? AND status = 'finished'",
            (match_id,),
        )
        if not row:
            await message.answer(f"Матч #{match_id} не найден или ещё не завершён.")
            return
        await db.execute(
            "UPDATE matches SET went_to_extra_time=TRUE, ot_pen_winner=?, ot_pen_method=? WHERE id=?",
            (team, method, match_id),
        )
        await db.commit()
        count = await _recalculate_match(db, match_id)
        await _log_admin_action(
            db, match_id, "admin:playoff_result", {"winner": team, "method": method}
        )
        await _publish_result(message.bot, match_id, db)

    await message.answer(
        f"✅ Плей-офф: {team} через {method}\nПересчитано прогнозов: {count}"
    )


# ---------- /recalc <match_id> — with confirmation ----------

@router.message(Command("recalc"))
async def cmd_recalc(message: Message) -> None:
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /recalc &lt;match_id&gt;")
        return
    try:
        match_id = int(args[1])
    except ValueError:
        await message.answer("match_id должен быть числом.")
        return

    async with get_db() as db:
        match = await fetchone(
            db,
            "SELECT team_home, team_away, score_home, score_away, status FROM matches WHERE id = ?",
            (match_id,),
        )
        if not match or match["status"] != "finished":
            await message.answer(f"Матч #{match_id} не найден или ещё не завершён.")
            return
        pred_count = await fetchone(
            db,
            "SELECT COUNT(*) AS cnt FROM predictions WHERE match_id = ?",
            (match_id,),
        )
    count = pred_count["cnt"] if pred_count else 0
    await message.answer(
        f"Пересчитать матч #{match_id}?\n\n"
        f"{match['team_home']} <b>{match['score_home']}:{match['score_away']}</b> {match['team_away']}\n"
        f"Прогнозов: {count}",
        reply_markup=recalc_confirm_kb(match_id),
    )


@router.callback_query(F.data.startswith("rcf:"))
async def cb_recalc_confirm(callback: CallbackQuery) -> None:
    match_id = int(callback.data.split(":")[1])
    async with get_db() as db:
        count = await _recalculate_match(db, match_id)
        await _log_admin_action(db, match_id, "admin:recalc", {})
    if count == 0:
        await callback.message.edit_text(f"Матч #{match_id} не найден или не завершён.")
    else:
        await callback.message.edit_text(f"✅ Пересчитано прогнозов: {count}")
    await callback.answer()


# ---------- /add_match + CSV upload ----------

@router.message(Command("add_match"))
async def cmd_add_match(message: Message) -> None:
    await message.answer(
        "Отправь CSV-файл с матчами.\n"
        "Колонки: match_date, kickoff_msk, stage, group_name, team_home, team_away"
    )


@router.message(F.document)
async def handle_csv_upload(message: Message) -> None:
    doc: Document = message.document
    if not doc.file_name.endswith(".csv"):
        return
    file = await message.bot.get_file(doc.file_id)
    data = await message.bot.download_file(file.file_path)
    text = data.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    required = {"match_date", "kickoff_msk", "stage", "team_home", "team_away"}
    if not required.issubset(set(reader.fieldnames or [])):
        await message.answer(f"CSV должен содержать колонки: {', '.join(sorted(required))}")
        return
    inserted = 0
    async with get_db() as db:
        for row in reader:
            await db.execute(
                """INSERT INTO matches (match_date, kickoff_msk, stage, group_name, team_home, team_away)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (row["match_date"], row["kickoff_msk"], row["stage"],
                 row.get("group_name") or None, row["team_home"], row["team_away"]),
            )
            inserted += 1
        await db.commit()
        await _log_admin_action(db, None, "admin:add_match", {"count": inserted})
    await message.answer(f"✅ Загружено матчей: {inserted}")


# ---------- /admin_log ----------

@router.message(Command("admin_log"))
async def cmd_admin_log(message: Message) -> None:
    async with get_db() as db:
        rows = await fetchall(
            db,
            """SELECT a.action, a.match_id, a.payload, a.created_at,
                      m.team_home, m.team_away
               FROM audit_log a
               LEFT JOIN matches m ON m.id = a.match_id
               WHERE a.action LIKE 'admin:%'
               ORDER BY a.id DESC LIMIT 10""",
        )
    if not rows:
        await message.answer("Лог действий пуст.")
        return
    lines = ["📋 <b>Последние действия:</b>\n"]
    for r in rows:
        ts = str(r["created_at"])[:16]
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        action = r["action"].replace("admin:", "")
        match_label = (
            f" [{r['team_home']} — {r['team_away']}]"
            if r["team_home"] else ""
        )
        detail = ""
        if action == "result":
            detail = f" → {payload.get('score', '')}"
        elif action == "playoff_result":
            detail = f" → {payload.get('winner', '')} ({payload.get('method', '')})"
        elif action == "add_match":
            detail = f" ({payload.get('count', '')} матчей)"
        lines.append(f"[{ts}] {action}{match_label}{detail}")
    await message.answer("\n".join(lines))
