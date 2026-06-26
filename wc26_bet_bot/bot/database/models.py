SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER UNIQUE NOT NULL,
    username        TEXT,
    full_name       TEXT,
    doublings_left  INTEGER NOT NULL DEFAULT 8,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_date          DATE NOT NULL,
    kickoff_msk         DATETIME NOT NULL,
    stage               TEXT NOT NULL,            -- 'group' | 'playoff'
    group_name          TEXT,
    team_home           TEXT NOT NULL,
    team_away           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'scheduled',  -- 'scheduled' | 'finished'
    score_home          INTEGER,
    score_away          INTEGER,
    went_to_extra_time  BOOLEAN NOT NULL DEFAULT FALSE,
    ot_pen_winner       TEXT,
    ot_pen_method       TEXT                      -- 'OT' | 'PEN' | NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    match_id        INTEGER NOT NULL REFERENCES matches(id),
    pred_home       INTEGER NOT NULL,
    pred_away       INTEGER NOT NULL,
    is_doubled      BOOLEAN NOT NULL DEFAULT FALSE,
    playoff_team    TEXT,
    playoff_method  TEXT,                         -- 'OT' | 'PEN' | NULL
    submitted_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked          BOOLEAN NOT NULL DEFAULT FALSE,
    base_points     INTEGER,
    base_final      INTEGER,
    bonus_points    INTEGER,
    total_points    INTEGER,
    UNIQUE(user_id, match_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    match_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    payload     TEXT,                             -- JSON
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
