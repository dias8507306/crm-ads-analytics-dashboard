"""SQLite: обращения, сообщения (для дедупа) и названия объявлений из Meta.

Одно соединение под замком — трафик вебхука небольшой, так проще и надёжнее.
Новые колонки добавлять миграцией через ALTER TABLE в MIGRATIONS (схема версионируется
через PRAGMA user_version), а не правкой SCHEMA — иначе на сервере со старой базой их не будет.
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inquiries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id        TEXT    NOT NULL,               -- номер клиента в формате WhatsApp: 7707...
    profile_name TEXT    NOT NULL DEFAULT '',
    started_at   INTEGER NOT NULL,               -- unix-время первого сообщения обращения
    last_at      INTEGER NOT NULL,
    messages     INTEGER NOT NULL DEFAULT 1,
    first_wamid  TEXT    NOT NULL UNIQUE,
    first_text   TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL CHECK (source IN ('ad', 'organic')),
    -- поля referral из вебхука; у органики пустые
    ad_id        TEXT    NOT NULL DEFAULT '',    -- referral.source_id
    source_type  TEXT    NOT NULL DEFAULT '',    -- referral.source_type: ad | post
    source_url   TEXT    NOT NULL DEFAULT '',
    headline     TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',
    media_type   TEXT    NOT NULL DEFAULT '',
    ctwa_clid    TEXT    NOT NULL DEFAULT ''     -- бывает пустым и у рекламных обращений
);
CREATE INDEX IF NOT EXISTS idx_inquiries_started ON inquiries(started_at);
CREATE INDEX IF NOT EXISTS idx_inquiries_contact ON inquiries(wa_id, started_at);
CREATE INDEX IF NOT EXISTS idx_inquiries_ad ON inquiries(ad_id);

CREATE TABLE IF NOT EXISTS messages (
    wamid        TEXT    PRIMARY KEY,            -- Meta повторяет доставку вебхука; второй раз не считаем
    wa_id        TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    type         TEXT    NOT NULL DEFAULT '',
    has_referral INTEGER NOT NULL DEFAULT 0,
    inquiry_id   INTEGER NOT NULL REFERENCES inquiries(id),
    received_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(wa_id, ts);

CREATE TABLE IF NOT EXISTS ad_meta (
    ad_id         TEXT    PRIMARY KEY,
    ad_name       TEXT    NOT NULL DEFAULT '',
    adset_name    TEXT    NOT NULL DEFAULT '',
    campaign_name TEXT    NOT NULL DEFAULT '',
    fetched_at    INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,    -- неудачных запросов подряд
    last_error    TEXT    NOT NULL DEFAULT ''
);
"""

# (версия, [SQL...]) — применяются по порядку к базам с меньшей user_version.
MIGRATIONS = [
    (1, []),  # исходная схема
    (2, [  # amoCRM: сделки из вебхука и кэш телефонов контактов
        """CREATE TABLE IF NOT EXISTS amo_leads (
            lead_id            INTEGER PRIMARY KEY,
            pipeline_id        INTEGER NOT NULL DEFAULT 0,
            created_at         INTEGER NOT NULL DEFAULT 0,    -- время создания сделки в amoCRM
            received_at        INTEGER NOT NULL,              -- когда пришёл вебхук
            contact_id         INTEGER NOT NULL DEFAULT 0,
            phones             TEXT    NOT NULL DEFAULT '',   -- только цифры, через запятую
            source_before      TEXT    NOT NULL DEFAULT '',   -- что стояло в «Қайдан келді» до нас
            source_before_enum INTEGER NOT NULL DEFAULT 0,
            -- new → fetched → written | dry_run | already | organic | no_match | skipped | gone | error
            state              TEXT    NOT NULL DEFAULT 'new',
            inquiry_id         INTEGER REFERENCES inquiries(id),
            action             TEXT    NOT NULL DEFAULT '',   -- что поставили (или поставили бы)
            attempts           INTEGER NOT NULL DEFAULT 0,
            next_try_at        INTEGER NOT NULL DEFAULT 0,
            last_error         TEXT    NOT NULL DEFAULT '',
            updated_at         INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_amo_leads_due ON amo_leads(state, next_try_at)",
        "CREATE INDEX IF NOT EXISTS idx_amo_leads_inquiry ON amo_leads(inquiry_id)",
        """CREATE TABLE IF NOT EXISTS amo_contacts (
            contact_id INTEGER PRIMARY KEY,
            phones     TEXT    NOT NULL DEFAULT '',
            fetched_at INTEGER NOT NULL
        )""",
    ]),
]


class Database:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            with self.transaction() as c:
                for sql in statements:
                    c.execute(sql)
                c.execute(f"PRAGMA user_version = {int(version)}")

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self._lock:
            self._conn.execute(sql, params)

    def close(self):
        with self._lock:
            self._conn.close()
