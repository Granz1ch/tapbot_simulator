import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "data" / "tap_simulator.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                coins INTEGER NOT NULL DEFAULT 0,
                taps INTEGER NOT NULL DEFAULT 0,
                spins INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                qty INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS timed_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role_key TEXT NOT NULL,
                expires_at REAL,
                UNIQUE(user_id, role_key)
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                max_uses INTEGER NOT NULL DEFAULT 0,
                uses INTEGER NOT NULL DEFAULT 0,
                reward_type TEXT NOT NULL,
                reward_value TEXT NOT NULL,
                reward_qty INTEGER NOT NULL DEFAULT 1,
                duration_seconds INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (code, user_id)
            );

            CREATE TABLE IF NOT EXISTS boss_hits (
                user_id INTEGER PRIMARY KEY,
                damage INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS unique_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                serial INTEGER NOT NULL,
                inscribed_name TEXT NOT NULL,
                inscribed_user_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS top1_awarded (
                user_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS upgrades (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, key)
            );
            """
        )
        cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        if "daily_day" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN daily_day TEXT")
        if "daily_streak" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN daily_streak INTEGER NOT NULL DEFAULT 0")
        if "display_name" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        if "avatar_url" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS craft_custom (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                ings TEXT NOT NULL,
                coins INTEGER NOT NULL,
                result TEXT NOT NULL,
                target_user_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER
            )
            """
        )
    _ensure_upd3_tables()


def ensure_user(db: sqlite3.Connection, user_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def add_coins(user_id: int, amount: int) -> int:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
        row = db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["coins"])


def take_coins(user_id: int, amount: int) -> bool:
    if amount <= 0:
        return False
    with get_db() as db:
        ensure_user(db, user_id)
        row = db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if int(row["coins"]) < amount:
            return False
        db.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (amount, user_id))
        return True


def set_coins(user_id: int, amount: int) -> int:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute("UPDATE users SET coins = ? WHERE user_id = ?", (max(0, amount), user_id))
        return max(0, amount)


def get_user(user_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        inv = db.execute(
            "SELECT card_id, qty FROM inventory WHERE user_id = ? AND qty > 0",
            (user_id,),
        ).fetchall()
        inventory = {r["card_id"]: r["qty"] for r in inv}
        uniques = db.execute(
            "SELECT * FROM unique_cards WHERE owner_id = ? ORDER BY serial",
            (user_id,),
        ).fetchall()
        unique_list = [dict(r) for r in uniques]
        for u in unique_list:
            inventory[u["card_id"]] = inventory.get(u["card_id"], 0) + 1
        return {
            "user_id": row["user_id"],
            "coins": row["coins"],
            "taps": row["taps"],
            "spins": row["spins"],
            "inventory": inventory,
            "unique_cards": unique_list,
            "daily_streak": int(row["daily_streak"] or 0) if "daily_streak" in row.keys() else 0,
        }


def bump_taps(user_id: int) -> None:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute("UPDATE users SET taps = taps + 1 WHERE user_id = ?", (user_id,))


def bump_spins(user_id: int) -> None:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute("UPDATE users SET spins = spins + 1 WHERE user_id = ?", (user_id,))


def add_card(user_id: int, card_id: str, qty: int = 1) -> int:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute(
            """
            INSERT INTO inventory (user_id, card_id, qty) VALUES (?, ?, ?)
            ON CONFLICT(user_id, card_id) DO UPDATE SET qty = qty + excluded.qty
            """,
            (user_id, card_id, qty),
        )
        row = db.execute(
            "SELECT qty FROM inventory WHERE user_id = ? AND card_id = ?",
            (user_id, card_id),
        ).fetchone()
        return int(row["qty"])


def take_card(user_id: int, card_id: str, qty: int = 1) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT qty FROM inventory WHERE user_id = ? AND card_id = ?",
            (user_id, card_id),
        ).fetchone()
        if not row or row["qty"] < qty:
            return False
        db.execute(
            "UPDATE inventory SET qty = qty - ? WHERE user_id = ? AND card_id = ?",
            (qty, user_id, card_id),
        )
        return True


def transfer_coins(from_id: int, to_id: int, amount: int) -> tuple[bool, str]:
    if amount <= 0:
        return False, "amount must be positive"
    with get_db() as db:
        ensure_user(db, from_id)
        ensure_user(db, to_id)
        src = db.execute("SELECT coins FROM users WHERE user_id = ?", (from_id,)).fetchone()
        if src["coins"] < amount:
            return False, "not enough coins"
        db.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (amount, from_id))
        db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, to_id))
        return True, "ok"


def transfer_card(from_id: int, to_id: int, card_id: str, qty: int = 1) -> tuple[bool, str]:
    if qty <= 0:
        return False, "qty must be positive"
    with get_db() as db:
        ensure_user(db, from_id)
        ensure_user(db, to_id)
        owned_u = db.execute(
            "SELECT id FROM unique_cards WHERE owner_id = ? AND card_id = ? ORDER BY serial LIMIT ?",
            (from_id, card_id, qty),
        ).fetchall()
        if owned_u:
            if len(owned_u) < qty:
                return False, "not enough unique cards"
            for r in owned_u:
                db.execute("UPDATE unique_cards SET owner_id = ? WHERE id = ?", (to_id, r["id"]))
            return True, "ok"
        row = db.execute(
            "SELECT qty FROM inventory WHERE user_id = ? AND card_id = ?",
            (from_id, card_id),
        ).fetchone()
        if not row or row["qty"] < qty:
            return False, "not enough cards"
        db.execute(
            "UPDATE inventory SET qty = qty - ? WHERE user_id = ? AND card_id = ?",
            (qty, from_id, card_id),
        )
        db.execute(
            """
            INSERT INTO inventory (user_id, card_id, qty) VALUES (?, ?, ?)
            ON CONFLICT(user_id, card_id) DO UPDATE SET qty = qty + excluded.qty
            """,
            (to_id, card_id, qty),
        )
        return True, "ok"


def all_user_ids() -> list[int]:
    with get_db() as db:
        rows = db.execute("SELECT user_id FROM users").fetchall()
        return [int(r["user_id"]) for r in rows]


def leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT user_id, coins, taps, display_name, avatar_url
            FROM users ORDER BY coins DESC, taps DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def cache_identity(user_id: int, display_name: str | None, avatar_url: str | None) -> None:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute(
            "UPDATE users SET display_name = COALESCE(?, display_name), avatar_url = COALESCE(?, avatar_url) WHERE user_id = ?",
            (display_name, avatar_url, user_id),
        )


def claim_daily(user_id: int, today: str) -> tuple[bool, int, int]:
    """Returns (ok, streak, payout). ok=False if already claimed today."""
    with get_db() as db:
        ensure_user(db, user_id)
        row = db.execute(
            "SELECT daily_day, daily_streak FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        last, streak = row["daily_day"], int(row["daily_streak"] or 0)
        if last == today:
            return False, streak, 0
        from datetime import date, timedelta

        yday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        streak = streak + 1 if last == yday else 1
        payout = 25 + min(streak, 7) * 5
        db.execute(
            "UPDATE users SET daily_day = ?, daily_streak = ?, coins = coins + ? WHERE user_id = ?",
            (today, streak, payout, user_id),
        )
        return True, streak, payout


def set_meta(key: str, value: Any) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def get_meta(key: str, default: Any = None) -> Any:
    with get_db() as db:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        return json.loads(row["value"])


def upsert_timed_role(user_id: int, role_key: str, expires_at: float | None) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO timed_roles (user_id, role_key, expires_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id, role_key) DO UPDATE SET expires_at = excluded.expires_at
            """,
            (user_id, role_key, expires_at),
        )


def delete_timed_role(user_id: int, role_key: str) -> None:
    with get_db() as db:
        db.execute(
            "DELETE FROM timed_roles WHERE user_id = ? AND role_key = ?",
            (user_id, role_key),
        )


def due_timed_roles(now: float) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT user_id, role_key FROM timed_roles WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", code).upper()


def create_promo(
    code: str,
    *,
    reward_type: str,
    reward_value: str,
    reward_qty: int = 1,
    max_uses: int = 0,
    duration_seconds: int | None = None,
    created_by: int | None = None,
) -> tuple[bool, str]:
    code = normalize_code(code)
    if not code or len(code) > 32:
        return False, "код 1–32 символа без пробелов"
    if reward_type not in ("coins", "card", "role"):
        return False, "reward_type: coins | card | role"
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM promos WHERE code = ?", (code,)).fetchone()
        if exists:
            return False, "такой промокод уже есть"
        db.execute(
            """
            INSERT INTO promos (code, max_uses, reward_type, reward_value, reward_qty, duration_seconds, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (code, int(max_uses or 0), reward_type, str(reward_value), int(reward_qty or 1), duration_seconds, created_by),
        )
    return True, code


def update_promo(code: str, **fields: Any) -> tuple[bool, str]:
    code = normalize_code(code)
    allowed = {
        "max_uses",
        "reward_type",
        "reward_value",
        "reward_qty",
        "duration_seconds",
        "active",
    }
    sets = []
    vals = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return False, "нечего менять"
    with get_db() as db:
        row = db.execute("SELECT 1 FROM promos WHERE code = ?", (code,)).fetchone()
        if not row:
            return False, "промокод не найден"
        db.execute(f"UPDATE promos SET {', '.join(sets)} WHERE code = ?", (*vals, code))
    return True, code


def get_promo(code: str) -> dict[str, Any] | None:
    code = normalize_code(code)
    with get_db() as db:
        row = db.execute("SELECT * FROM promos WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None


def list_promos() -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM promos ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def redeem_promo(code: str, user_id: int) -> tuple[bool, str, dict[str, Any] | None]:
    code = normalize_code(code)
    with get_db() as db:
        promo = db.execute("SELECT * FROM promos WHERE code = ?", (code,)).fetchone()
        if not promo or not promo["active"]:
            return False, "not_found", None
        used = db.execute(
            "SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?",
            (code, user_id),
        ).fetchone()
        if used:
            return False, "already", None
        if promo["max_uses"] and promo["uses"] >= promo["max_uses"]:
            return False, "exhausted", None
        db.execute("UPDATE promos SET uses = uses + 1 WHERE code = ?", (code,))
        db.execute(
            "INSERT INTO promo_redemptions (code, user_id) VALUES (?, ?)",
            (code, user_id),
        )
        return True, "ok", dict(promo)


def boss_clear_hits() -> None:
    with get_db() as db:
        db.execute("DELETE FROM boss_hits")


def boss_add_hit(user_id: int, dmg: int) -> int:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO boss_hits (user_id, damage) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET damage = damage + excluded.damage
            """,
            (user_id, dmg),
        )
        row = db.execute("SELECT damage FROM boss_hits WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["damage"])


def boss_hits() -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT user_id, damage FROM boss_hits ORDER BY damage DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def unique_of(user_id: int, card_id: str | None = None) -> list[dict[str, Any]]:
    with get_db() as db:
        if card_id:
            rows = db.execute(
                "SELECT * FROM unique_cards WHERE owner_id = ? AND card_id = ? ORDER BY serial",
                (user_id, card_id),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM unique_cards WHERE owner_id = ? ORDER BY serial",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def owns_unique(user_id: int, card_id: str) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM unique_cards WHERE owner_id = ? AND card_id = ? LIMIT 1",
            (user_id, card_id),
        ).fetchone()
        return bool(row)


def top1_already_awarded(user_id: int) -> bool:
    with get_db() as db:
        return bool(db.execute("SELECT 1 FROM top1_awarded WHERE user_id = ?", (user_id,)).fetchone())


def award_top1_card(user_id: int, inscribed_name: str, card_id: str = "crown_tap") -> dict[str, Any] | None:
    with get_db() as db:
        ensure_user(db, user_id)
        if db.execute("SELECT 1 FROM top1_awarded WHERE user_id = ?", (user_id,)).fetchone():
            return None
        row = db.execute("SELECT COALESCE(MAX(serial), 0) AS m FROM unique_cards WHERE card_id = ?", (card_id,)).fetchone()
        serial = int(row["m"]) + 1
        db.execute("INSERT INTO top1_awarded (user_id) VALUES (?)", (user_id,))
        cur = db.execute(
            """
            INSERT INTO unique_cards (card_id, serial, inscribed_name, inscribed_user_id, owner_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (card_id, serial, inscribed_name[:80], user_id, user_id),
        )
        return {
            "id": cur.lastrowid,
            "card_id": card_id,
            "serial": serial,
            "inscribed_name": inscribed_name[:80],
            "inscribed_user_id": user_id,
            "owner_id": user_id,
        }


def upgrade_level(user_id: int, key: str) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT level FROM upgrades WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return int(row["level"]) if row else 0


def all_upgrades(user_id: int) -> dict[str, int]:
    with get_db() as db:
        rows = db.execute("SELECT key, level FROM upgrades WHERE user_id = ?", (user_id,)).fetchall()
        return {r["key"]: int(r["level"]) for r in rows}


def set_upgrade_level(user_id: int, key: str, level: int) -> int:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute(
            """
            INSERT INTO upgrades (user_id, key, level) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET level = excluded.level
            """,
            (user_id, key, level),
        )
        return level


def craft_custom_add(
    *,
    tag: str,
    ings: list[str],
    coins: int,
    result: str,
    target_user_id: int | None,
    created_by: int | None,
) -> int:
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO craft_custom (tag, ings, coins, result, target_user_id, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tag[:40], json.dumps(ings), int(coins), result, target_user_id, created_by),
        )
        return int(cur.lastrowid)


def craft_custom_list(include_off: bool = False) -> list[dict[str, Any]]:
    with get_db() as db:
        q = "SELECT * FROM craft_custom"
        if not include_off:
            q += " WHERE active = 1"
        q += " ORDER BY id DESC"
        return [dict(r) for r in db.execute(q).fetchall()]


def craft_custom_for(user_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM craft_custom
            WHERE active = 1 AND (target_user_id IS NULL OR target_user_id = ?)
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            raw = item.get("ings")
            item["ings"] = json.loads(raw) if isinstance(raw, str) else (raw or [])
            out.append(item)
        return out


def craft_custom_off(recipe_id: int) -> bool:
    with get_db() as db:
        cur = db.execute("UPDATE craft_custom SET active = 0 WHERE id = ?", (recipe_id,))
        return cur.rowcount > 0


def _ensure_upd3_tables() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS enchanted (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                tier INTEGER NOT NULL,
                ready_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS shard_users (
                user_id INTEGER PRIMARY KEY,
                shards INTEGER NOT NULL DEFAULT 0,
                slot_coins REAL NOT NULL DEFAULT 1,
                slot_luck REAL NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS referrals (
                user_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                invited INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS referred (
                user_id INTEGER PRIMARY KEY,
                by_user INTEGER NOT NULL,
                paid INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        if "shards" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN shards INTEGER NOT NULL DEFAULT 0")


def shards_of(user_id: int) -> int:
    with get_db() as db:
        ensure_user(db, user_id)
        row = db.execute("SELECT shards FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["shards"] or 0) if row else 0


def add_shards(user_id: int, n: int) -> int:
    with get_db() as db:
        ensure_user(db, user_id)
        db.execute("UPDATE users SET shards = COALESCE(shards,0) + ? WHERE user_id = ?", (n, user_id))
        return int(db.execute("SELECT shards FROM users WHERE user_id = ?", (user_id,)).fetchone()["shards"])


def take_shards(user_id: int, n: int) -> bool:
    if n <= 0:
        return False
    with get_db() as db:
        ensure_user(db, user_id)
        row = db.execute("SELECT COALESCE(shards,0) AS s FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if int(row["s"]) < n:
            return False
        db.execute("UPDATE users SET shards = shards - ? WHERE user_id = ?", (n, user_id))
        return True


def shard_minted() -> int:
    return int(get_meta("shard_minted") or 0)


def try_mint_shards(n: int) -> bool:
    cap = 10000
    with get_db() as db:
        row = db.execute("SELECT value FROM meta WHERE key = ?", ("shard_minted",)).fetchone()
        cur = int(json.loads(row["value"])) if row else 0
        if cur + n > cap:
            return False
        db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("shard_minted", json.dumps(cur + n)),
        )
        return True


def shard_slots(user_id: int) -> dict[str, float]:
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO shard_users (user_id) VALUES (?)", (user_id,))
        row = db.execute("SELECT slot_coins, slot_luck FROM shard_users WHERE user_id = ?", (user_id,)).fetchone()
        return {"coins": float(row["slot_coins"]), "luck": float(row["slot_luck"])}


def set_shard_slot(user_id: int, kind: str, value: float) -> None:
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO shard_users (user_id) VALUES (?)", (user_id,))
        col = "slot_coins" if kind == "coins" else "slot_luck"
        db.execute(f"UPDATE shard_users SET {col} = ? WHERE user_id = ?", (value, user_id))


def steal_coins(victim_id: int, attacker_id: int, want: int) -> int:
    if want <= 0:
        return 0
    with get_db() as db:
        ensure_user(db, victim_id)
        ensure_user(db, attacker_id)
        v = int(db.execute("SELECT coins FROM users WHERE user_id = ?", (victim_id,)).fetchone()["coins"])
        take = min(v, want)
        if take <= 0:
            return 0
        db.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (take, victim_id))
        db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (take, attacker_id))
        return take


def enchant_list(user_id: int, *, ready_only: bool = False) -> list[dict[str, Any]]:
    with get_db() as db:
        q = "SELECT * FROM enchanted WHERE owner_id = ?"
        if ready_only:
            q += " AND ready_at = 0"
        rows = db.execute(q + " ORDER BY id", (user_id,)).fetchall()
        return [dict(r) for r in rows]


def enchant_busy(user_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM enchanted WHERE owner_id = ? AND ready_at > 0",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def enchant_start(user_id: int, card_id: str, kind: str, tier: int, ready_at: float) -> int:
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO enchanted (owner_id, card_id, kind, tier, ready_at) VALUES (?,?,?,?,?)",
            (user_id, card_id, kind, tier, ready_at),
        )
        return int(cur.lastrowid)


def enchant_finish_due(now: float) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM enchanted WHERE ready_at > 0 AND ready_at <= ?",
            (now,),
        ).fetchall()
        db.execute("UPDATE enchanted SET ready_at = 0 WHERE ready_at > 0 AND ready_at <= ?", (now,))
        return [dict(r) for r in rows]


def enchant_cancel(user_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM enchanted WHERE owner_id = ? AND ready_at > 0",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        db.execute("DELETE FROM enchanted WHERE id = ?", (row["id"],))
        return dict(row)


def enchant_best(user_id: int, kind: str) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT MAX(tier) AS t FROM enchanted WHERE owner_id = ? AND kind = ? AND ready_at = 0",
            (user_id, kind),
        ).fetchone()
        return int(row["t"] or 0)


def transfer_enchanted(from_id: int, to_id: int, card_id: str, qty: int) -> int:
    moved = 0
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM enchanted WHERE owner_id = ? AND card_id = ? AND ready_at = 0 ORDER BY id LIMIT ?",
            (from_id, card_id, qty),
        ).fetchall()
        for r in rows:
            db.execute("UPDATE enchanted SET owner_id = ? WHERE id = ?", (to_id, r["id"]))
            moved += 1
    return moved


def ref_code_for(user_id: int) -> str:
    with get_db() as db:
        row = db.execute("SELECT code FROM referrals WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row["code"]
        code = f"R{user_id % 10**8:08d}"
        db.execute("INSERT OR IGNORE INTO referrals (user_id, code) VALUES (?, ?)", (user_id, code))
        row = db.execute("SELECT code FROM referrals WHERE user_id = ?", (user_id,)).fetchone()
        return row["code"]


def ref_by_code(code: str) -> int | None:
    code = code.strip().upper()
    with get_db() as db:
        row = db.execute("SELECT user_id FROM referrals WHERE code = ?", (code,)).fetchone()
        return int(row["user_id"]) if row else None


def ref_bind(user_id: int, by_user: int) -> bool:
    if user_id == by_user:
        return False
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM referred WHERE user_id = ?", (user_id,)).fetchone()
        if exists:
            return False
        db.execute("INSERT INTO referred (user_id, by_user, paid) VALUES (?, ?, 0)", (user_id, by_user))
        return True


def ref_pay_if_needed(user_id: int) -> int | None:
    with get_db() as db:
        row = db.execute("SELECT by_user, paid FROM referred WHERE user_id = ?", (user_id,)).fetchone()
        if not row or int(row["paid"]):
            return None
        by_user = int(row["by_user"])
        db.execute("UPDATE referred SET paid = 1 WHERE user_id = ?", (user_id,))
        db.execute("UPDATE referrals SET invited = invited + 1 WHERE user_id = ?", (by_user,))
        return by_user


def ref_stats(user_id: int) -> int:
    with get_db() as db:
        row = db.execute("SELECT invited FROM referrals WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["invited"]) if row else 0


_ensure_upd3_tables()
