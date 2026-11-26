"""Cross-group game storage. Every command is one SQLite write transaction."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock


MAINTENANCE_MESSAGE = "无尽电梯正在维护，探险进度已保存，计时已暂停。开放后可以继续本局。"


class GameMaintenance(sqlite3.OperationalError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS elevator_maintenance (
    id INTEGER PRIMARY KEY CHECK(id=1), started_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    user_id TEXT PRIMARY KEY, nickname TEXT NOT NULL,
    gold INTEGER NOT NULL DEFAULT 500 CHECK (gold >= 0),
    tickets INTEGER NOT NULL DEFAULT 20 CHECK (tickets >= 0),
    ticket_day TEXT NOT NULL DEFAULT '', bought_tickets INTEGER NOT NULL DEFAULT 0,
    daily_ticket_refill INTEGER NOT NULL DEFAULT 10,
    paid_tickets INTEGER NOT NULL DEFAULT 0,
    support_rewards INTEGER NOT NULL DEFAULT 0, safebox INTEGER NOT NULL DEFAULT 1,
    runs INTEGER NOT NULL DEFAULT 0, extracts INTEGER NOT NULL DEFAULT 0,
    loadout TEXT NOT NULL DEFAULT '{}', tutorial_flags INTEGER NOT NULL DEFAULT 0,
    name_confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inventory (
    stock_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES players(user_id),
    item_id TEXT NOT NULL, quality TEXT NOT NULL, value INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (user_id, item_id, quality, value)
);
CREATE TABLE IF NOT EXISTS rush_preferences (
    user_id TEXT PRIMARY KEY REFERENCES players(user_id),
    floors INTEGER NOT NULL CHECK(floors IN (3,5,10)),
    skip_valuable INTEGER NOT NULL CHECK(skip_valuable IN (0,1)),
    event_mode TEXT NOT NULL DEFAULT 'pause' CHECK(event_mode IN ('pause','random','ignore')),
    auto_elite INTEGER NOT NULL DEFAULT 0 CHECK(auto_elite IN (0,1))
);
CREATE TABLE IF NOT EXISTS heirloom_inventory (
    user_id TEXT NOT NULL REFERENCES players(user_id), piece TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity>0), PRIMARY KEY(user_id,piece)
);
CREATE TABLE IF NOT EXISTS heirloom_loadouts (
    user_id TEXT PRIMARY KEY REFERENCES players(user_id), focus TEXT NOT NULL, slots TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment (
    equip_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES players(user_id),
    base_id TEXT NOT NULL, rarity TEXT NOT NULL, stats TEXT NOT NULL,
    affixes TEXT NOT NULL, rerolls INTEGER NOT NULL DEFAULT 0, rules_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS equipment_owner ON equipment(user_id);
CREATE TABLE IF NOT EXISTS active_runs (
    user_id TEXT PRIMARY KEY REFERENCES players(user_id),
    group_id TEXT NOT NULL, updated_at INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_expiry ON active_runs(updated_at);
CREATE TABLE IF NOT EXISTS relics (
    box_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES players(user_id),
    owner_name TEXT NOT NULL, map_id TEXT NOT NULL, floor INTEGER NOT NULL,
    items TEXT NOT NULL, value INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    claimed INTEGER NOT NULL DEFAULT 0, reserved_by TEXT, reserved_until INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS relic_search ON relics(map_id, floor, claimed, expires_at);
CREATE TABLE IF NOT EXISTS signals (
    owner_id TEXT PRIMARY KEY REFERENCES active_runs(user_id) ON DELETE CASCADE,
    group_id TEXT NOT NULL, expires_at INTEGER NOT NULL, capacity INTEGER NOT NULL,
    helpers TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS signals_group ON signals(group_id, expires_at);
CREATE TABLE IF NOT EXISTS notices (
    notice_id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES players(user_id), body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notice_owner ON notices(user_id, notice_id);
CREATE TABLE IF NOT EXISTS collections (
    user_id TEXT NOT NULL REFERENCES players(user_id), item_id TEXT NOT NULL, count INTEGER NOT NULL,
    first_got_at INTEGER NOT NULL, PRIMARY KEY(user_id, item_id)
);
CREATE TABLE IF NOT EXISTS season_stats (
    month TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES players(user_id),
    max_floor INTEGER NOT NULL DEFAULT 0, floor_value INTEGER NOT NULL DEFAULT 0,
    best_extract_floor INTEGER NOT NULL DEFAULT 0, best_extract_value INTEGER NOT NULL DEFAULT 0,
    collection_count INTEGER NOT NULL DEFAULT 0, rob_value INTEGER NOT NULL DEFAULT 0,
    rescues INTEGER NOT NULL DEFAULT 0, death_value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(month, user_id)
);
CREATE TABLE IF NOT EXISTS season_map_stats (
    month TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES players(user_id), map_id TEXT NOT NULL,
    max_floor INTEGER NOT NULL, floor_value INTEGER NOT NULL,
    best_extract_floor INTEGER NOT NULL, best_extract_value INTEGER NOT NULL,
    PRIMARY KEY(month, user_id, map_id)
);
CREATE INDEX IF NOT EXISTS season_map_rank
ON season_map_stats(month, map_id, max_floor DESC, floor_value DESC, user_id);
CREATE TABLE IF NOT EXISTS group_active_users (
    group_id TEXT NOT NULL, month TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES players(user_id),
    PRIMARY KEY(group_id, month, user_id)
);
CREATE TABLE IF NOT EXISTS season_titles (
    month TEXT NOT NULL, category TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES players(user_id),
    title TEXT NOT NULL, PRIMARY KEY(month, category)
);
CREATE TABLE IF NOT EXISTS settled_months (month TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS rescue_pairs (
    day TEXT NOT NULL, first_id TEXT NOT NULL, second_id TEXT NOT NULL, rewards INTEGER NOT NULL,
    PRIMARY KEY(day, first_id, second_id)
);
CREATE TABLE IF NOT EXISTS group_buffs (
    group_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS command_cooldowns (
    user_id TEXT NOT NULL REFERENCES players(user_id), command TEXT NOT NULL, used_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, command)
);
CREATE TABLE IF NOT EXISTS ai_orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES players(user_id),
    group_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_orders_user ON ai_orders(user_id, status, expires_at);
CREATE TABLE IF NOT EXISTS daily_quests (
    day TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES players(user_id),
    payload TEXT NOT NULL, PRIMARY KEY(day, user_id)
);
CREATE TABLE IF NOT EXISTS exchange_listings (
    listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id TEXT NOT NULL REFERENCES players(user_id),
    seller_name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('public','direct')),
    target_user_id TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK(mode IN ('gold','barter','both')),
    asset_type TEXT NOT NULL CHECK(asset_type IN ('inventory','equipment','shining')),
    category TEXT NOT NULL,
    rarity TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_value INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    price_gold INTEGER NOT NULL DEFAULT 0,
    want_item_id TEXT NOT NULL DEFAULT '',
    want_item_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','cancelled','rejected','expired')),
    buyer_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    resolved_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS exchange_open ON exchange_listings(status, scope, expires_at);
CREATE INDEX IF NOT EXISTS exchange_seller ON exchange_listings(seller_id, status);
CREATE INDEX IF NOT EXISTS exchange_target ON exchange_listings(target_user_id, status);
CREATE TABLE IF NOT EXISTS prophecy_scores (
    week TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES players(user_id),
    nickname TEXT NOT NULL,
    max_floor INTEGER NOT NULL DEFAULT 1,
    cleared_floor INTEGER NOT NULL DEFAULT 0,
    best_value INTEGER NOT NULL DEFAULT 0,
    extracted INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(week, user_id)
);
CREATE INDEX IF NOT EXISTS prophecy_rank
ON prophecy_scores(week, cleared_floor DESC, max_floor DESC, extracted DESC, best_value DESC, updated_at ASC);
CREATE TABLE IF NOT EXISTS prophecy_settled (
    week TEXT PRIMARY KEY,
    settled_at INTEGER NOT NULL,
    summary TEXT NOT NULL
);
"""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path, catalog=None):
        self.path = path
        self.catalog = catalog
        self._ready = False
        self._lock = Lock()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = self._connect()
            try:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(SCHEMA)
                self._migrate_season_stats(db)
                self._migrate_players(db)
                self._migrate_equipment(db)
                self._migrate_rush_preferences(db)
                self._migrate_exchange_listings(db)
                if self.catalog is not None:
                    self._migrate_rainbow_stock(db)
                self._ready = True
            finally:
                db.close()

    def _migrate_rainbow_stock(self, db: sqlite3.Connection) -> None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS game_migrations(name TEXT PRIMARY KEY)")
            applied = db.execute("INSERT OR IGNORE INTO game_migrations VALUES ('rainbow_stock_v1')")
            if not applied.rowcount:
                return
            for key in self.catalog.rainbow:
                db.execute("""
                    INSERT INTO inventory(user_id,item_id,quality,value,quantity)
                    SELECT c.user_id,c.item_id,'',?,c.count-coalesce(sum(i.quantity),0)
                    FROM collections c LEFT JOIN inventory i
                      ON i.user_id=c.user_id AND i.item_id=c.item_id
                    WHERE c.item_id=? GROUP BY c.user_id,c.item_id,c.count
                    HAVING c.count>coalesce(sum(i.quantity),0)
                    ON CONFLICT(user_id,item_id,quality,value)
                    DO UPDATE SET quantity=quantity+excluded.quantity
                """, (self.catalog.items[key]["value"], key))

    def _migrate_equipment(self, db: sqlite3.Connection) -> None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(equipment)")}
            if "rules_version" not in columns:
                db.execute("ALTER TABLE equipment ADD COLUMN rules_version INTEGER NOT NULL DEFAULT 1")
            if self.catalog is None:
                return
            for row in db.execute("SELECT equip_id,base_id,rarity,stats FROM equipment WHERE rules_version<2").fetchall():
                stats = self.catalog.migrated_stats(row["base_id"],row["rarity"],json.loads(row["stats"]))
                db.execute("UPDATE equipment SET stats=?,rules_version=2 WHERE equip_id=?",
                           (encode(stats),row["equip_id"]))

    @staticmethod
    def _migrate_exchange_listings(db: sqlite3.Connection) -> None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='exchange_listings'"
            ).fetchone()
            if row is None or "'both'" in (row["sql"] or ""):
                return
            db.execute("""CREATE TABLE exchange_listings_v2 (
                listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id TEXT NOT NULL REFERENCES players(user_id),
                seller_name TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('public','direct')),
                target_user_id TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL CHECK(mode IN ('gold','barter','both')),
                asset_type TEXT NOT NULL CHECK(asset_type IN ('inventory','equipment','shining')),
                category TEXT NOT NULL,
                rarity TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                unit_value INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                price_gold INTEGER NOT NULL DEFAULT 0,
                want_item_id TEXT NOT NULL DEFAULT '',
                want_item_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','cancelled','rejected','expired')),
                buyer_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                resolved_at INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""INSERT INTO exchange_listings_v2(
                listing_id,seller_id,seller_name,scope,target_user_id,mode,asset_type,category,rarity,
                item_id,item_name,quantity,unit_value,payload,price_gold,want_item_id,want_item_name,
                status,buyer_id,created_at,expires_at,resolved_at
            ) SELECT
                listing_id,seller_id,seller_name,scope,target_user_id,mode,asset_type,category,rarity,
                item_id,item_name,quantity,unit_value,payload,price_gold,want_item_id,want_item_name,
                status,buyer_id,created_at,expires_at,resolved_at
            FROM exchange_listings""")
            db.execute("DROP TABLE exchange_listings")
            db.execute("ALTER TABLE exchange_listings_v2 RENAME TO exchange_listings")
            db.execute("CREATE INDEX IF NOT EXISTS exchange_open ON exchange_listings(status, scope, expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS exchange_seller ON exchange_listings(seller_id, status)")
            db.execute("CREATE INDEX IF NOT EXISTS exchange_target ON exchange_listings(target_user_id, status)")

    @staticmethod
    def _migrate_rush_preferences(db: sqlite3.Connection) -> None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(rush_preferences)")}
            if "event_mode" not in columns:
                db.execute("ALTER TABLE rush_preferences ADD COLUMN event_mode TEXT NOT NULL DEFAULT 'pause' "
                           "CHECK(event_mode IN ('pause','random','ignore'))")
            if "auto_elite" not in columns:
                db.execute("ALTER TABLE rush_preferences ADD COLUMN auto_elite INTEGER NOT NULL DEFAULT 0 "
                           "CHECK(auto_elite IN (0,1))")

    @staticmethod
    def _migrate_players(db: sqlite3.Connection) -> None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(players)")}
            if "name_confirmed" not in columns:
                db.execute("ALTER TABLE players ADD COLUMN name_confirmed INTEGER NOT NULL DEFAULT 0")
                # Existing players who have already played keep their current nickname without being interrupted.
                db.execute("UPDATE players SET name_confirmed=1 WHERE runs>0")
            if "daily_ticket_refill" not in columns:
                db.execute("ALTER TABLE players ADD COLUMN daily_ticket_refill INTEGER NOT NULL DEFAULT 10")
            if "paid_tickets" not in columns:
                # A negative value marks legacy balances that need splitting once.
                db.execute("ALTER TABLE players ADD COLUMN paid_tickets INTEGER NOT NULL DEFAULT -1")

    @staticmethod
    def _migrate_season_stats(db: sqlite3.Connection) -> None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(season_stats)")}
            missing = {"best_extract_floor", "best_extract_value"} - columns
            if not missing:
                return
            for name in sorted(missing):
                db.execute(f"ALTER TABLE season_stats ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            # In the previous schema, a positive value records an actual extraction at max_floor.
            db.execute(
                "UPDATE season_stats SET best_extract_floor=max_floor,best_extract_value=floor_value "
                "WHERE best_extract_floor=0 AND floor_value>0"
            )

    @contextmanager
    def read(self):
        self._initialize()
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def is_paused(self) -> bool:
        with self.read() as db:
            return db.execute("SELECT 1 FROM elevator_maintenance WHERE id=1").fetchone() is not None

    @contextmanager
    def transaction(self):
        self._initialize()
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM elevator_maintenance WHERE id=1").fetchone():
                    raise GameMaintenance(MAINTENANCE_MESSAGE)
                yield db
        finally:
            db.close()
