# Архитектура

## Стек
- Python 3.11+
- **aiogram 3.x** — Telegram Bot API
- **SQLite** (через `aiosqlite`) для локального запуска — схема пишется так, чтобы потом легко перенести на PostgreSQL
- **APScheduler** — напоминания о незаполненных прогнозах, авто-проверка дедлайнов
- **python-dotenv** — секреты (токен бота, telegram_id админа) только через `.env`

## Структура проекта

```
wc26_bet_bot/
├── bot/
│   ├── __init__.py
│   ├── config.py            # загрузка переменных из .env
│   ├── main.py               # точка входа, запуск polling
│   ├── database/
│   │   ├── db.py             # подключение, init_db()
│   │   └── models.py         # схемы таблиц
│   ├── handlers/
│   │   ├── user.py           # /start, /matches, /me, /standings
│   │   └── admin.py          # /result, /playoff_result, /recalc, /add_match
│   ├── services/
│   │   ├── scoring.py        # движок очков — см. 02_SCORING_RULES.md
│   │   └── scheduler.py      # напоминания, авто-лок прогнозов
│   └── keyboards.py          # inline-кнопки для ввода счёта и удвоения
├── data/
│   └── matches_seed.csv      # расписание для первичной загрузки в БД
├── .env                       # НЕ коммитить
├── .env.example
├── requirements.txt
└── README.md
```

## Схема БД

### `users`
| Поле | Тип | Примечание |
|---|---|---|
| id | INTEGER PK | |
| telegram_id | INTEGER UNIQUE | |
| username | TEXT | |
| full_name | TEXT | |
| doublings_left | INTEGER DEFAULT 8 | |
| created_at | DATETIME | |

### `matches`
| Поле | Тип | Примечание |
|---|---|---|
| id | INTEGER PK | |
| match_date | DATE | |
| kickoff_msk | DATETIME | время начала по МСК — все участники в одном поясе, конвертация не нужна |
| stage | TEXT | `group` \| `playoff` |
| group_name | TEXT NULL | только для группового этапа |
| team_home | TEXT | |
| team_away | TEXT | |
| status | TEXT | `scheduled` \| `finished` |
| score_home | INTEGER NULL | заполняется через `/result` |
| score_away | INTEGER NULL | |
| went_to_extra_time | BOOLEAN DEFAULT FALSE | |
| ot_pen_winner | TEXT NULL | команда, прошедшая дальше |
| ot_pen_method | TEXT NULL | `OT` \| `PEN` |

### `predictions`
| Поле | Тип | Примечание |
|---|---|---|
| id | INTEGER PK | |
| user_id | INTEGER FK → users | |
| match_id | INTEGER FK → matches | |
| pred_home | INTEGER | |
| pred_away | INTEGER | |
| is_doubled | BOOLEAN DEFAULT FALSE | |
| playoff_team | TEXT NULL | |
| playoff_method | TEXT NULL | `OT` \| `PEN` \| NULL |
| submitted_at | DATETIME | |
| locked | BOOLEAN DEFAULT FALSE | true сразу после kickoff |
| base_points | INTEGER NULL | заполняется после результата |
| base_final | INTEGER NULL | после удвоения/штрафа |
| bonus_points | INTEGER NULL | |
| total_points | INTEGER NULL | |

UNIQUE constraint на `(user_id, match_id)` — один прогноз на матч на пользователя.

### `audit_log` (для споров "до/после матча")
| Поле | Тип |
|---|---|
| id | INTEGER PK |
| user_id | INTEGER |
| match_id | INTEGER |
| action | TEXT (например `late_edit_attempt`) |
| payload | TEXT (JSON со старым/новым значением) |
| created_at | DATETIME |

## Ключевые механизмы

**Лок ставки.** При попытке создать/изменить прогноз сравнить `now()` с `match.kickoff_msk`. Если `now() >= kickoff_msk` — отклонить с понятным сообщением, записать попытку в `audit_log`, прогноз в БД не менять.

**Пересчёт очков.** Чистая функция `score_prediction(pred, match)` из `02_SCORING_RULES.md`. Вызывается для всех прогнозов на матч сразу после `/result` или `/playoff_result`. Идемпотентна — безопасно перезапускать через `/recalc`.

**Напоминания.** APScheduler job каждые ~30 минут: для матчей в ближайшие 2 часа без прогноза у части пользователей — пуш в личку.

**Авто-лок (доп. защита).** Отдельная задача APScheduler ровно в момент kickoff ставит `locked = true` на все прогнозы по матчу — резервный механизм поверх проверки при записи.
