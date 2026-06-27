"""
Client for worldcup26.ir REST API.
Handles auth (register / authenticate), token lifecycle (84-day TTL),
and game data fetching with automatic 401-retry.
"""
import logging
from datetime import datetime, timedelta

import aiohttp

from bot.config import config
from bot.database.db import get_db, fetchone

log = logging.getLogger(__name__)

_API_BASE = "https://worldcup26.ir"
_TOKEN_TTL_DAYS = 84
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Playoff round hierarchy for check_new_round ordering
ROUND_ORDER: dict[str, int] = {"r32": 0, "r16": 1, "qf": 2, "sf": 3, "final": 4}

# Russian → English team name mapping (all 48 WC2026 participants)
_RU_TO_EN: dict[str, str] = {
    "Австралия": "Australia",
    "Австрия": "Austria",
    "Алжир": "Algeria",
    "Англия": "England",
    "Аргентина": "Argentina",
    "Бельгия": "Belgium",
    "Босния и Герцеговина": "Bosnia and Herzegovina",
    "Бразилия": "Brazil",
    "Германия": "Germany",
    "Гана": "Ghana",
    "Гаити": "Haiti",
    "ДР Конго": "Democratic Republic of the Congo",
    "Египет": "Egypt",
    "Иордания": "Jordan",
    "Иран": "Iran",
    "Ирак": "Iraq",
    "Испания": "Spain",
    "Кабо-Верде": "Cape Verde",
    "Канада": "Canada",
    "Катар": "Qatar",
    "Колумбия": "Colombia",
    "Кот-д'Ивуар": "Ivory Coast",
    "Кюрасао": "Curaçao",
    "Марокко": "Morocco",
    "Мексика": "Mexico",
    "Нидерланды": "Netherlands",
    "Новая Зеландия": "New Zealand",
    "Норвегия": "Norway",
    "Панама": "Panama",
    "Парагвай": "Paraguay",
    "Португалия": "Portugal",
    "Саудовская Аравия": "Saudi Arabia",
    "Сенегал": "Senegal",
    "США": "United States",
    "Турция": "Turkey",
    "Тунис": "Tunisia",
    "Уругвай": "Uruguay",
    "Узбекистан": "Uzbekistan",
    "Франция": "France",
    "Хорватия": "Croatia",
    "Чехия": "Czech Republic",
    "Швейцария": "Switzerland",
    "Швеция": "Sweden",
    "Шотландия": "Scotland",
    "Южная Африка": "South Africa",
    "Южная Корея": "South Korea",
    "Япония": "Japan",
    "Эквадор": "Ecuador",
}

_EN_TO_RU: dict[str, str] = {v: k for k, v in _RU_TO_EN.items()}


def ru_to_en(name: str) -> str | None:
    return _RU_TO_EN.get(name)


def en_to_ru(name: str) -> str:
    return _EN_TO_RU.get(name, name)


# ── Token storage ─────────────────────────────────────────────────────────────

async def _save_token(token: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM api_tokens")
        await db.execute(
            "INSERT INTO api_tokens (token, obtained_at) VALUES (?, CURRENT_TIMESTAMP)",
            (token,),
        )
        await db.commit()


async def _get_stored_token() -> str | None:
    async with get_db() as db:
        row = await fetchone(db, "SELECT token, obtained_at FROM api_tokens LIMIT 1")
    if not row:
        return None
    obtained = datetime.fromisoformat(str(row["obtained_at"]))
    if datetime.utcnow() - obtained > timedelta(days=_TOKEN_TTL_DAYS):
        return None
    return str(row["token"])


# ── Auth ──────────────────────────────────────────────────────────────────────

async def authenticate() -> str:
    """POST /auth/authenticate, persist token. Returns new token."""
    if not config.worldcup_api_email or not config.worldcup_api_password:
        raise RuntimeError("WORLDCUP_API_EMAIL / WORLDCUP_API_PASSWORD not set in .env")
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(
            f"{_API_BASE}/auth/authenticate",
            json={"email": config.worldcup_api_email, "password": config.worldcup_api_password},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    token: str = data["token"]
    await _save_token(token)
    return token


async def setup_api() -> str:
    """Register (first time) or re-authenticate. Returns token."""
    if not config.worldcup_api_email or not config.worldcup_api_password:
        raise RuntimeError("WORLDCUP_API_EMAIL / WORLDCUP_API_PASSWORD not set in .env")
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(
            f"{_API_BASE}/auth/register",
            json={
                "name": "WC26Bot",
                "email": config.worldcup_api_email,
                "password": config.worldcup_api_password,
            },
        ) as resp:
            data = await resp.json()
            if resp.status == 200 and "token" in data:
                token: str = data["token"]
                await _save_token(token)
                return token
    # Already registered — fall back to authenticate
    return await authenticate()


# ── Game data ─────────────────────────────────────────────────────────────────

async def fetch_games() -> list[dict]:
    """
    GET /get/games with bearer token.
    On 401: re-authenticates once and retries.
    Raises on any other HTTP error or network failure.
    """
    token = await _get_stored_token() or await authenticate()
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for attempt in range(2):
            async with session.get(
                f"{_API_BASE}/get/games",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    token = await authenticate()
                    continue
                resp.raise_for_status()
                data = await resp.json()
                return data.get("games", [])
    return []


# UTC offsets for each WC2026 stadium during summer 2026 (DST in effect for US/Canada).
# Mexico City & Guadalajara no longer observe DST (abolished 2023) → UTC-6 year-round.
# Monterrey is a border zone that still observes CDT → UTC-5.
_STADIUM_UTC_OFFSET: dict[str, int] = {
    "1": -6,   # Mexico City (Estadio Azteca) — permanent UTC-6
    "2": -6,   # Guadalajara (Estadio Akron) — permanent UTC-6
    "3": -5,   # Monterrey (Estadio BBVA) — CDT (border zone)
    "4": -5,   # Dallas (AT&T Stadium) — CDT
    "5": -5,   # Houston (NRG Stadium) — CDT
    "6": -5,   # Kansas City — CDT
    "7": -4,   # Atlanta — EDT
    "8": -4,   # Miami — EDT
    "9": -4,   # Boston — EDT
    "10": -4,  # Philadelphia — EDT
    "11": -4,  # New York/New Jersey — EDT
    "12": -4,  # Toronto — EDT
    "13": -7,  # Vancouver — PDT
    "14": -7,  # Seattle — PDT
    "15": -7,  # San Francisco — PDT
    "16": -7,  # Los Angeles — PDT
}
_MSK_OFFSET = 3  # UTC+3


def local_to_msk(local_date: str, stadium_id: str) -> str:
    """
    Convert API local_date "MM/DD/YYYY HH:MM" (stadium local time) → "YYYY-MM-DD HH:MM:00" MSK.
    Falls back to adding 7h (Eastern→MSK) if stadium_id is unknown.
    """
    dt = datetime.strptime(local_date, "%m/%d/%Y %H:%M")
    utc_offset = _STADIUM_UTC_OFFSET.get(str(stadium_id), -4)  # default: Eastern
    msk_dt = dt + timedelta(hours=(_MSK_OFFSET - utc_offset))
    return msk_dt.strftime("%Y-%m-%d %H:%M:00")
