"""
Admin-only handlers: /result, /playoff_result, /recalc, /add_match.
All commands are gated by ADMIN_TELEGRAM_ID from config.
"""
import csv
import io

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, Document

from bot.config import config
from bot.database.db import get_db
from bot.services.scoring import score_prediction, Prediction, Match

router = Router()
router.message.filter(F.from_user.id == config.admin_telegram_id)


# ---------- helpers ----------

def _parse_score(text: str) -> tuple[int, int] | None:
    """Parse '2:1' → (2, 1). Returns None on bad input."""
    try:
        parts = text.strip().split(":")
        if len(parts) != 2:
            return None
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


async def _recalculate_match(db, match_id: int) -> int:
    """Recalculate scores for all predictions on a match. Returns count updated."""
    match_row = await db.execute_fetchone("SELECT * FROM matches WHERE id = ?", (match_id,))
    if not match_row or match_row["status"] != "finished":
        return 0

    match = Match(
        score_home=match_row["score_home"],
        score_away=match_row["score_away"],
        went_to_extra_time=bool(match_row["went_to_extra_time"]),
        ot_pen_winner=match_row["ot_pen_winner"],
        ot_pen_method=match_row["ot_pen_method"],
    )

    predictions = await db.execute_fetchall(
        "SELECT * FROM predictions WHERE match_id = ?", (match_id,)
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


# ---------- /result <match_id> <score> ----------

@router.message(Command("result"))
async def cmd_result(message: Message) -> None:
    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await message.answer("Использование: /result <match_id> <счёт, напр. 2:1>")
        return

    match_id_str, score_str = args[1], args[2]
    try:
        match_id = int(match_id_str)
    except ValueError:
        await message.answer("match_id должен быть числом.")
        return

    parsed = _parse_score(score_str)
    if parsed is None:
        await message.answer("Неверный формат счёта. Пример: 2:1")
        return

    home, away = parsed
    async with await get_db() as db:
        match = await db.execute_fetchone(
            "SELECT id, team_home, team_away FROM matches WHERE id = ?", (match_id,)
        )
        if not match:
            await message.answer(f"Матч #{match_id} не найден.")
            return

        await db.execute(
            """
            UPDATE matches
            SET score_home = ?, score_away = ?, status = 'finished'
            WHERE id = ?
            """,
            (home, away, match_id),
        )
        await db.commit()

        count = await _recalculate_match(db, match_id)

    await message.answer(
        f"✅ Результат матча #{match_id} ({match['team_home']} — {match['team_away']}): "
        f"{home}:{away}\nПересчитано прогнозов: {count}"
    )


# ---------- /playoff_result <match_id> <team> <OT|PEN> ----------

@router.message(Command("playoff_result"))
async def cmd_playoff_result(message: Message) -> None:
    args = (message.text or "").split(maxsplit=3)
    if len(args) < 4:
        await message.answer("Использование: /playoff_result <match_id> <команда> <OT|PEN>")
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

    async with await get_db() as db:
        row = await db.execute_fetchone(
            "SELECT id FROM matches WHERE id = ? AND status = 'finished'", (match_id,)
        )
        if not row:
            await message.answer(f"Матч #{match_id} не найден или ещё не завершён (сначала /result).")
            return

        await db.execute(
            """
            UPDATE matches
            SET went_to_extra_time = TRUE, ot_pen_winner = ?, ot_pen_method = ?
            WHERE id = ?
            """,
            (team, method, match_id),
        )
        await db.commit()
        count = await _recalculate_match(db, match_id)

    await message.answer(
        f"✅ Плей-офф результат матча #{match_id}: {team} прошла через {method}.\n"
        f"Пересчитано прогнозов: {count}"
    )


# ---------- /recalc <match_id> ----------

@router.message(Command("recalc"))
async def cmd_recalc(message: Message) -> None:
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /recalc <match_id>")
        return
    try:
        match_id = int(args[1])
    except ValueError:
        await message.answer("match_id должен быть числом.")
        return

    async with await get_db() as db:
        count = await _recalculate_match(db, match_id)

    if count == 0:
        await message.answer(f"Матч #{match_id} не найден или ещё не завершён.")
    else:
        await message.answer(f"✅ Пересчитано прогнозов: {count}")


# ---------- /add_match (или CSV-файл) ----------

@router.message(Command("add_match"))
async def cmd_add_match(message: Message) -> None:
    await message.answer(
        "Отправь CSV-файл с матчами (формат как в matches_seed_template.csv).\n"
        "Колонки: match_date, kickoff_msk, stage, group_name, team_home, team_away"
    )


@router.message(F.document)
async def handle_csv_upload(message: Message) -> None:
    doc: Document = message.document
    if not doc.file_name.endswith(".csv"):
        return  # ignore non-CSV files

    file = await message.bot.get_file(doc.file_id)
    data = await message.bot.download_file(file.file_path)
    text = data.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(text))
    required = {"match_date", "kickoff_msk", "stage", "team_home", "team_away"}
    if not required.issubset(set(reader.fieldnames or [])):
        await message.answer(f"CSV должен содержать колонки: {', '.join(sorted(required))}")
        return

    inserted = 0
    async with await get_db() as db:
        for row in reader:
            await db.execute(
                """
                INSERT INTO matches (match_date, kickoff_msk, stage, group_name, team_home, team_away)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["match_date"],
                    row["kickoff_msk"],
                    row["stage"],
                    row.get("group_name") or None,
                    row["team_home"],
                    row["team_away"],
                ),
            )
            inserted += 1
        await db.commit()

    await message.answer(f"✅ Загружено матчей: {inserted}")
