from dataclasses import dataclass
from os import getenv

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_telegram_id: int          # первый/основной админ (для уведомлений планировщика)
    admin_ids: frozenset[int]       # все админы (включает admin_telegram_id)
    group_chat_id: int | None
    worldcup_api_email: str | None
    worldcup_api_password: str | None


def _load() -> Config:
    token = getenv("BOT_TOKEN")
    admin_id = getenv("ADMIN_TELEGRAM_ID")
    if not token or not admin_id:
        raise RuntimeError("BOT_TOKEN and ADMIN_TELEGRAM_ID must be set in .env")
    primary_id = int(admin_id)

    # ADMIN_TELEGRAM_IDS=111,222,333 — дополнительные админы (опционально)
    extra = getenv("ADMIN_TELEGRAM_IDS", "")
    extra_ids = {int(i.strip()) for i in extra.split(",") if i.strip()}
    admin_ids = frozenset({primary_id} | extra_ids)

    group_id = getenv("GROUP_CHAT_ID")
    return Config(
        bot_token=token,
        admin_telegram_id=primary_id,
        admin_ids=admin_ids,
        group_chat_id=int(group_id) if group_id else None,
        worldcup_api_email=getenv("WORLDCUP_API_EMAIL"),
        worldcup_api_password=getenv("WORLDCUP_API_PASSWORD"),
    )


config: Config = _load()
