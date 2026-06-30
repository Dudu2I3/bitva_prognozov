"""
One-shot script: import group stage history from the Excel-exported CSV.

Usage (from wc26_bet_bot/ dir):
    python3 import_history.py

What it does:
1. Initialises DB tables (idempotent).
2. Creates all 72 group-stage matches from the Bitva CSV (stage='group').
3. Sets match results (status='finished', score_home/away).
4. Creates 10 user records with placeholder telegram_ids.
5. Imports all 72×10 predictions (is_doubled=False — no doubling info in CSV).
6. Runs the scoring engine for every prediction.

NOTE: Doublings are NOT imported (info not in the CSV).
Users' actual Telegram IDs can be updated with:
    UPDATE users SET telegram_id=<real_id> WHERE full_name='Имя';
Then restart the bot.
"""

import asyncio
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiosqlite
from bot.database.models import SCHEMA

BITVA_CSV = Path("/Users/daniyar/Downloads/Битва прогнозов.xlsx - ЧМ-2026-3.csv")
DB_PATH = Path("bot.db")

# name → (placeholder telegram_id, doublings_left from CSV)
PARTICIPANTS = [
    "Сергей", "Роман", "Рамиль", "Динар", "Данияр",
    "Альмар", "Макс", "Василий", "Ильшат", "Семен",
]

SCORE_RE = re.compile(r"^(\d+):(\d+)$")


def parse_score(s: str) -> tuple[int, int] | None:
    m = SCORE_RE.match(s.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def csv_date_to_iso(d: str) -> str:
    """'28.06.2026' → '2026-06-28'"""
    day, mon, year = d.split(".")
    return f"{year}-{mon}-{day}"


def csv_time_to_kickoff(d: str, t: str) -> str:
    """'28.06.2026', '2:30' → '2026-06-28 02:30:00'"""
    iso_date = csv_date_to_iso(d)
    h, m = t.split(":")
    return f"{iso_date} {int(h):02d}:{m}:00"


def outcome(h: int, a: int) -> str:
    return "home_win" if h > a else ("away_win" if h < a else "draw")


def score_prediction(ph: int, pa: int, sh: int, sa: int) -> dict:
    exact = ph == sh and pa == sa
    same = outcome(ph, pa) == outcome(sh, sa)
    base = 3 if exact else (1 if same else 0)
    return {"base_points": base, "base_final": base, "bonus_points": 0, "total_points": base}


async def main() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── 1. Init tables ────────────────────────────────────────────────────
        for stmt in SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                await db.execute(stmt)
        try:
            await db.execute("ALTER TABLE matches ADD COLUMN api_game_id TEXT")
        except Exception:
            pass
        await db.commit()
        print("✅ Tables ready")

        # ── 2. Parse Bitva CSV ────────────────────────────────────────────────
        with open(BITVA_CSV, encoding="utf-8") as f:
            rows = list(csv.reader(f))

        # Row 1 (index 1): doublings remaining for each participant
        doublings_row = rows[1]
        doublings_left = [int(doublings_row[4 + i]) for i in range(10)]

        match_data: list[dict] = []
        for r in rows:
            if not r or not re.match(r"\d{2}\.\d{2}\.\d{4}", r[0] or ""):
                continue
            score = parse_score(r[3]) if len(r) > 3 else None
            if score is None:
                continue
            parts = r[2].split(" – ")
            if len(parts) != 2:
                print(f"⚠️  Skip unparseable: {r[2]!r}")
                continue
            home, away = parts[0].strip(), parts[1].strip()
            kickoff = csv_time_to_kickoff(r[0], r[1])
            preds = [parse_score(r[4 + i].strip()) if len(r) > 4 + i else None for i in range(10)]
            match_data.append({
                "home": home, "away": away, "kickoff": kickoff,
                "date": csv_date_to_iso(r[0]), "score": score, "preds": preds,
            })

        print(f"✅ Parsed {len(match_data)} matches from CSV")

        # ── 3. Insert/update matches ──────────────────────────────────────────
        match_id_map: dict[tuple[str, str, str], int] = {}  # (home, away, kickoff) → id
        for md in match_data:
            cur = await db.execute(
                "SELECT id FROM matches WHERE kickoff_msk=? AND team_home=? AND team_away=?",
                (md["kickoff"], md["home"], md["away"]),
            )
            row = await cur.fetchone()
            sh, sa = md["score"]
            if row:
                mid = row["id"]
                await db.execute(
                    "UPDATE matches SET status='finished', score_home=?, score_away=? WHERE id=?",
                    (sh, sa, mid),
                )
            else:
                cur = await db.execute(
                    """INSERT INTO matches
                       (match_date, kickoff_msk, stage, group_name, team_home, team_away,
                        status, score_home, score_away)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (md["date"], md["kickoff"], "group", None,
                     md["home"], md["away"], "finished", sh, sa),
                )
                mid = cur.lastrowid
            match_id_map[(md["home"], md["away"], md["kickoff"])] = mid

        await db.commit()
        print(f"✅ {len(match_id_map)} matches upserted as finished")

        # ── 4. Create users ───────────────────────────────────────────────────
        user_ids: list[int] = []
        for i, name in enumerate(PARTICIPANTS):
            tg_id = 1000001 + i
            dl = doublings_left[i]
            cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (tg_id,))
            row = await cur.fetchone()
            if row:
                uid = row["id"]
                await db.execute("UPDATE users SET doublings_left=? WHERE id=?", (dl, uid))
            else:
                cur = await db.execute(
                    "INSERT INTO users (telegram_id, username, full_name, doublings_left) VALUES (?,?,?,?)",
                    (tg_id, name.lower(), name, dl),
                )
                uid = cur.lastrowid
            user_ids.append(uid)
        await db.commit()
        print(f"✅ {len(user_ids)} users ready")

        # ── 5. Import predictions + scoring ──────────────────────────────────
        pred_new = pred_upd = 0
        for md in match_data:
            mid = match_id_map[(md["home"], md["away"], md["kickoff"])]
            sh, sa = md["score"]
            for i, uid in enumerate(user_ids):
                pred = md["preds"][i]
                if pred is None:
                    continue
                ph, pa = pred
                pts = score_prediction(ph, pa, sh, sa)
                cur = await db.execute(
                    "SELECT id FROM predictions WHERE user_id=? AND match_id=?", (uid, mid)
                )
                existing = await cur.fetchone()
                if existing:
                    await db.execute(
                        """UPDATE predictions
                           SET pred_home=?, pred_away=?,
                               base_points=?, base_final=?, bonus_points=?, total_points=?
                           WHERE id=?""",
                        (ph, pa, pts["base_points"], pts["base_final"],
                         pts["bonus_points"], pts["total_points"], existing["id"]),
                    )
                    pred_upd += 1
                else:
                    await db.execute(
                        """INSERT INTO predictions
                           (user_id, match_id, pred_home, pred_away, is_doubled, locked,
                            base_points, base_final, bonus_points, total_points)
                           VALUES (?,?,?,?,0,1,?,?,?,?)""",
                        (uid, mid, ph, pa,
                         pts["base_points"], pts["base_final"],
                         pts["bonus_points"], pts["total_points"]),
                    )
                    pred_new += 1
        await db.commit()
        print(f"✅ Predictions: {pred_new} new, {pred_upd} updated, all scored")

        # ── 6. Compute base totals (excluding any existing adjustment row) ────
        computed: dict[int, int] = {}
        for uid in user_ids:
            cur = await db.execute(
                """SELECT COALESCE(SUM(p.total_points),0) AS s
                   FROM predictions p
                   JOIN matches m ON m.id = p.match_id
                   WHERE p.user_id = ? AND m.team_home != '__adjustment__'""",
                (uid,),
            )
            row = await cur.fetchone()
            computed[uid] = row["s"]

        # ── 7. Read CSV group-stage totals ────────────────────────────────────
        with open(BITVA_CSV, encoding="utf-8") as f:
            all_rows = list(csv.reader(f))
        csv_totals: list[int] = []
        for r in all_rows:
            if r and "Итог группового" in (r[0] or ""):
                csv_totals = [int(r[4 + i]) for i in range(10)]
                break

        if not csv_totals:
            print("⚠️  Строка «Итог группового этапа» не найдена — корректировка пропущена")
        else:
            # Insert virtual match for doubling adjustments
            cur = await db.execute(
                "SELECT id FROM matches WHERE team_home='__adjustment__' AND team_away='__adjustment__'"
            )
            adj_row = await cur.fetchone()
            if adj_row:
                adj_mid = adj_row["id"]
            else:
                cur = await db.execute(
                    """INSERT INTO matches
                       (match_date, kickoff_msk, stage, team_home, team_away, status)
                       VALUES ('2026-06-28','2026-06-28 23:59:00','group',
                               '__adjustment__','__adjustment__','finished')""",
                )
                adj_mid = cur.lastrowid
            await db.commit()

            for i, uid in enumerate(user_ids):
                delta = csv_totals[i] - computed[uid]
                cur2 = await db.execute(
                    "SELECT id FROM predictions WHERE user_id=? AND match_id=?",
                    (uid, adj_mid),
                )
                existing = await cur2.fetchone()
                if existing:
                    await db.execute(
                        "UPDATE predictions SET total_points=?, base_final=? WHERE id=?",
                        (delta, delta, existing["id"]),
                    )
                else:
                    await db.execute(
                        """INSERT INTO predictions
                           (user_id, match_id, pred_home, pred_away, is_doubled, locked,
                            base_points, base_final, bonus_points, total_points)
                           VALUES (?,?,0,0,0,1,0,?,0,?)""",
                        (uid, adj_mid, delta, delta),
                    )
            await db.commit()
            print(f"✅ Поправки за удвоения применены (матч id={adj_mid})")

        # ── 8. Final standings ────────────────────────────────────────────────
        print("\n📊 Итоговый рейтинг группового этапа:")
        cur = await db.execute(
            """SELECT u.full_name, u.doublings_left,
                      COALESCE(SUM(p.total_points),0) AS pts,
                      COUNT(CASE WHEN p.base_points=3 THEN 1 END) AS exact,
                      COUNT(CASE WHEN p.base_points>=1 AND p.match_id != (
                          SELECT id FROM matches WHERE team_home='__adjustment__' LIMIT 1
                      ) THEN 1 END) AS outcomes
               FROM users u
               LEFT JOIN predictions p ON p.user_id=u.id
               GROUP BY u.id
               ORDER BY pts DESC, exact DESC, outcomes DESC"""
        )
        for rank, r in enumerate(await cur.fetchall(), 1):
            print(
                f"  {rank:2}. {r['full_name']:<12} {r['pts']:>3} очков  "
                f"(точных: {r['exact']:2}, исходов: {r['outcomes']:2}, "
                f"удвоений осталось: {r['doublings_left']})"
            )

        print("\n✅ Участники: placeholder telegram_id 1000001–1000010.")
        print("   Чтобы привязать реальный аккаунт:")
        print("   UPDATE users SET telegram_id=<real_id> WHERE full_name='Имя';")


if __name__ == "__main__":
    asyncio.run(main())
