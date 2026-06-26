# CLAUDE.md — Битва прогнозов ЧМ-2026

## Документация проекта

@docx/01_PRODUCT_SPEC.md
@docx/02_SCORING_RULES.md
@docx/03_ARCHITECTURE.md

---

## Ограничения стека

- **Python 3.11+** — минимальная версия, использовать современный синтаксис (match/case, тайп-хинты через `|`)
- **aiogram 3.x** — только этот фреймворк для Telegram Bot API; не использовать python-telegram-bot или другие альтернативы
- **aiosqlite** — единственный драйвер БД для v1; схема должна быть совместима с PostgreSQL (типы, имена) для будущей миграции
- **APScheduler** — для напоминаний и авто-лока прогнозов; не городить self-written polling loops
- **python-dotenv** — все секреты (`BOT_TOKEN`, `ADMIN_TELEGRAM_ID`) только через `.env`; жёстко запрещено хардкодить токены и id в коде
- `.env` **никогда не коммитить** — он в `.gitignore`

## Структура проекта

Строго по `docx/03_ARCHITECTURE.md`. Новые модули класть только в уже определённые директории:

```
wc26_bet_bot/
├── bot/
│   ├── config.py
│   ├── main.py
│   ├── database/        # db.py, models.py
│   ├── handlers/        # user.py, admin.py
│   ├── services/        # scoring.py, scheduler.py
│   └── keyboards.py
├── data/
│   └── matches_seed.csv
├── .env                 # НЕ коммитить
├── .env.example
├── requirements.txt
└── README.md
```

Не создавать директорий и модулей вне этой структуры без явного согласования.

## Движок подсчёта очков

Реализовывать **строго по `docx/02_SCORING_RULES.md`** — там псевдокод и worked-примеры (они же тест-кейсы). Не интерпретировать правила по-своему.

---

## Git Workflow

- **Коммит после каждой логической задачи** — не накапливать изменения из нескольких задач в один коммит
- **Пуш после каждого коммита** — `git push` сразу после `git commit`
- **Conventional Commits** — формат сообщений:
  - `feat:` — новая функциональность
  - `fix:` — исправление бага
  - `refactor:` — рефакторинг без изменения поведения
  - `chore:` — зависимости, конфиги, CI
  - `docs:` — документация
  - `test:` — тесты
  - Пример: `feat: add /standings command with tiebreak logic`
- **Никогда `git push --force`** — история не переписывается; если нужно исправить — новый коммит
