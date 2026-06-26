from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def score_input_kb(match_id: int) -> InlineKeyboardMarkup:
    """Digit grid 0-9 for entering home/away score."""
    builder = InlineKeyboardBuilder()
    for digit in range(10):
        builder.button(
            text=str(digit),
            callback_data=f"digit:{match_id}:{digit}",
        )
    builder.adjust(5)
    return builder.as_markup()


def doubling_kb(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удвоить", callback_data=f"double:{match_id}:yes"),
        InlineKeyboardButton(text="➡️ Без удвоения", callback_data=f"double:{match_id}:no"),
    ]])


def playoff_team_kb(match_id: int, team_home: str, team_away: str) -> InlineKeyboardMarkup:
    """Ask which team advances (available only when prediction is a draw)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=team_home, callback_data=f"playoff_team:{match_id}:{team_home}"),
            InlineKeyboardButton(text=team_away, callback_data=f"playoff_team:{match_id}:{team_away}"),
        ],
        [InlineKeyboardButton(text="Не указывать", callback_data=f"playoff_team:{match_id}:skip")],
    ])


def playoff_method_kb(match_id: int, team: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ОТ", callback_data=f"playoff_method:{match_id}:{team}:OT"),
        InlineKeyboardButton(text="ПЕН", callback_data=f"playoff_method:{match_id}:{team}:PEN"),
        InlineKeyboardButton(text="Не указывать", callback_data=f"playoff_method:{match_id}:{team}:skip"),
    ]])


def matches_list_kb(matches: list[dict]) -> InlineKeyboardMarkup:
    """One button per upcoming match."""
    builder = InlineKeyboardBuilder()
    for m in matches:
        label = f"{m['team_home']} — {m['team_away']}  ({m['kickoff_msk'][:16]} МСК)"
        builder.button(text=label, callback_data=f"predict:{m['id']}")
    builder.adjust(1)
    return builder.as_markup()
