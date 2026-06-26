from dataclasses import dataclass
from os import getenv

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_telegram_id: int


def _load() -> Config:
    token = getenv("BOT_TOKEN")
    admin_id = getenv("ADMIN_TELEGRAM_ID")
    if not token or not admin_id:
        raise RuntimeError("BOT_TOKEN and ADMIN_TELEGRAM_ID must be set in .env")
    return Config(bot_token=token, admin_telegram_id=int(admin_id))


config: Config = _load()
