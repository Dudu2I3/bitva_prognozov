from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ── User prediction flow ───────────────────────────────────────────────────
# State is fully encoded in callback_data — no message-text parsing needed.
# Callback chain: predict:{mid} → hd:{mid}:{h} → ad:{mid}:{h}:{a}
#   → dbl:{mid}:{h}:{a}:{y|n}
#   → (playoff) pt:{mid}:{h}:{a}:{dbl}:{winner}
#   → pm:{mid}:{h}:{a}:{dbl}:{winner}:{method}

def home_score_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in range(10):
        builder.button(text=str(d), callback_data=f"hd:{match_id}:{d}")
    builder.adjust(5)
    return builder.as_markup()


def away_score_kb(match_id: int, home: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in range(10):
        builder.button(text=str(d), callback_data=f"ad:{match_id}:{home}:{d}")
    builder.adjust(5)
    return builder.as_markup()


def doubling_kb(match_id: int, home: int, away: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удвоить", callback_data=f"dbl:{match_id}:{home}:{away}:y"),
        InlineKeyboardButton(text="➡️ Без удвоения", callback_data=f"dbl:{match_id}:{home}:{away}:n"),
    ]])


def playoff_team_kb(
    match_id: int, home: int, away: int, doubled: int,
    team_home: str, team_away: str,
) -> InlineKeyboardMarkup:
    base = f"pt:{match_id}:{home}:{away}:{doubled}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=team_home, callback_data=f"{base}:h"),
            InlineKeyboardButton(text=team_away, callback_data=f"{base}:a"),
        ],
        [InlineKeyboardButton(text="Не указывать", callback_data=f"{base}:s")],
    ])


def playoff_method_kb(
    match_id: int, home: int, away: int, doubled: int, winner: str,
) -> InlineKeyboardMarkup:
    base = f"pm:{match_id}:{home}:{away}:{doubled}:{winner}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Доп. время (ОТ)", callback_data=f"{base}:O"),
        InlineKeyboardButton(text="Пенальти (ПЕН)", callback_data=f"{base}:P"),
        InlineKeyboardButton(text="Не указывать", callback_data=f"{base}:S"),
    ]])


# ── /matches — filter + per-match buttons ─────────────────────────────────

_PERIODS = [("today", "Сегодня"), ("tomorrow", "Завтра"),
            ("week", "Эта неделя"), ("all", "Все туры")]


def matches_filter_kb(period: str, matches: list[dict]) -> InlineKeyboardMarkup:
    """Filter row (4 buttons) followed by one predict/edit button per match."""
    builder = InlineKeyboardBuilder()
    for key, label in _PERIODS:
        prefix = "▶ " if key == period else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"mf:{key}")
    for m in matches:
        pred = m.get("pred")
        if pred:
            dbl = "×2 " if pred["is_doubled"] else ""
            label = f"✏️ {dbl}{pred['pred_home']}:{pred['pred_away']} — изменить"
        else:
            label = f"📝 {m['team_home']} — {m['team_away']}"
        builder.button(text=label, callback_data=f"predict:{m['id']}")
    builder.adjust(4, 1)
    return builder.as_markup()


# ── Admin /result multi-step flow ─────────────────────────────────────────
# Callbacks: arm:{mid} → ahd:{mid}:{h} → aad:{mid}:{h}:{a}
#   → (playoff) apt:{mid}:{h}:{a}:{winner} → apm:{mid}:{h}:{a}:{winner}:{method}
#   → acf:{mid}:{h}:{a}:{winner}:{method}

def admin_match_list_kb(matches: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for m in matches:
        label = f"{m['team_home']} — {m['team_away']}  {str(m['kickoff_msk'])[:16]}"
        builder.button(text=label, callback_data=f"arm:{m['id']}")
    builder.adjust(1)
    return builder.as_markup()


def admin_home_score_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in range(10):
        builder.button(text=str(d), callback_data=f"ahd:{match_id}:{d}")
    builder.adjust(5)
    return builder.as_markup()


def admin_away_score_kb(match_id: int, home: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in range(10):
        builder.button(text=str(d), callback_data=f"aad:{match_id}:{home}:{d}")
    builder.adjust(5)
    return builder.as_markup()


def admin_playoff_team_kb(
    match_id: int, home_s: int, away_s: int, team_home: str, team_away: str,
) -> InlineKeyboardMarkup:
    base = f"apt:{match_id}:{home_s}:{away_s}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=team_home, callback_data=f"{base}:h"),
        InlineKeyboardButton(text=team_away, callback_data=f"{base}:a"),
    ]])


def admin_playoff_method_kb(
    match_id: int, home_s: int, away_s: int, winner: str,
) -> InlineKeyboardMarkup:
    base = f"apm:{match_id}:{home_s}:{away_s}:{winner}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Доп. время (ОТ)", callback_data=f"{base}:OT"),
        InlineKeyboardButton(text="Пенальти (ПЕН)", callback_data=f"{base}:PEN"),
        InlineKeyboardButton(text="Не указывать", callback_data=f"{base}:n"),
    ]])


def admin_confirm_kb(
    match_id: int, home_s: int, away_s: int, winner: str = "n", method: str = "n",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"acf:{match_id}:{home_s}:{away_s}:{winner}:{method}",
        ),
        InlineKeyboardButton(text="❌ Отмена", callback_data="acancel"),
    ]])


def recalc_confirm_kb(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пересчитать", callback_data=f"rcf:{match_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="acancel"),
    ]])


def recalc_match_list_kb(matches: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for m in matches:
        score = f" {m['score_home']}:{m['score_away']}" if m.get("score_home") is not None else ""
        label = f"{m['team_home']} — {m['team_away']}{score}"
        builder.button(text=label, callback_data=f"rpm:{m['id']}")
    builder.adjust(1)
    return builder.as_markup()


def playoff_match_list_kb(matches: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for m in matches:
        label = f"{m['team_home']} {m['score_home']}:{m['score_away']} {m['team_away']}"
        builder.button(text=label, callback_data=f"pfm:{m['id']}")
    builder.adjust(1)
    return builder.as_markup()


def playoff_pick_team_kb(match_id: int, team_home: str, team_away: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=team_home, callback_data=f"ppt:{match_id}:h"),
        InlineKeyboardButton(text=team_away, callback_data=f"ppt:{match_id}:a"),
    ]])


def playoff_pick_method_kb(match_id: int, winner: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Доп. время (ОТ)", callback_data=f"ppm:{match_id}:{winner}:OT"),
        InlineKeyboardButton(text="Пенальти (ПЕН)", callback_data=f"ppm:{match_id}:{winner}:PEN"),
    ]])


def playoff_confirm_kb(match_id: int, winner: str, method: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"ppc:{match_id}:{winner}:{method}",
        ),
        InlineKeyboardButton(text="❌ Отмена", callback_data="acancel"),
    ]])
