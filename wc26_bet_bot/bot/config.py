from dataclasses import dataclass
from os import getenv

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_telegram_id: int
    group_chat_id: int | None  # optional: auto-publish match results here


def _load() -> Config:
    token = getenv("BOT_TOKEN")
    admin_id = getenv("ADMIN_TELEGRAM_ID")
    if not token or not admin_id:
        raise RuntimeError("BOT_TOKEN and ADMIN_TELEGRAM_ID must be set in .env")
    group_id = getenv("GROUP_CHAT_ID")
    return Config(
        bot_token=token,
        admin_telegram_id=int(admin_id),
        group_chat_id=int(group_id) if group_id else None,
    )


config: Config = _load()
