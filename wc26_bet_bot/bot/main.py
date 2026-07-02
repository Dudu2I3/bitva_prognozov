import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

from bot.config import config
from bot.database.db import init_db
from bot.handlers import user, admin
from bot.services.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO)

_USER_COMMANDS = [
    BotCommand(command="start", description="Регистрация"),
    BotCommand(command="matches", description="Ближайшие матчи и прогнозы"),
    BotCommand(command="my_predictions", description="Мои прогнозы"),
    BotCommand(command="today_results", description="Результаты сегодняшних матчей"),
    BotCommand(command="me", description="Мой профиль и статистика"),
    BotCommand(command="standings", description="Общий рейтинг"),
    BotCommand(command="export", description="Таблица прогнозов в Excel"),
    BotCommand(command="help", description="Список команд"),
]

_ADMIN_EXTRA = [
    BotCommand(command="result", description="Ввод результата матча"),
    BotCommand(command="playoff_result", description="Результат ОТ/ПЕН"),
    BotCommand(command="recalc", description="Пересчёт очков"),
    BotCommand(command="add_match", description="Добавить матчи (CSV)"),
    BotCommand(command="admin_log", description="Лог действий"),
    BotCommand(command="setup_api", description="Подключить worldcup26.ir API"),
    BotCommand(command="check_now", description="Проверить результаты API прямо сейчас"),
    BotCommand(command="check_predictions", description="Кто не сделал прогноз"),
]


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(_USER_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        _USER_COMMANDS + _ADMIN_EXTRA,
        scope=BotCommandScopeChat(chat_id=config.admin_telegram_id),
    )


async def main() -> None:
    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(admin.router)
    dp.include_router(user.router)

    setup_scheduler(bot)

    await bot.delete_webhook(drop_pending_updates=True)
    await _set_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
