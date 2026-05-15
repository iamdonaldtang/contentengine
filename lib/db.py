"""SQLite state machine wrapper · single source of truth for all engine modules.

This module is the ONLY allowed entry point to the engine's SQLite database.
Other modules MUST import ``db`` from here; direct ``sqlite3.connect()`` calls
are forbidden (see Prompt_AI系统化编程_v1.md §7).

Design:
  * Per-thread connection pool (sqlite3 connections are not threadsafe).
  * WAL journal mode for concurrent readers + serialized writers.
  * Automatic DDL bootstrap on first connect (idempotent ``CREATE TABLE IF
    NOT EXISTS``).
  * Migration runner reads ``lib/migrations/NNN_*.sql`` in order and tracks
    applied versions in ``schema_migrations`` table.
  * Thin domain-object adapters (``db.pieces``, ``db.state_events`` ...) that
    callers use instead of raw SQL where convenient.  Raw SQL via
    ``db.execute()`` is fully supported for ad-hoc analytics.

Threading note: callers obtain a connection via ``db._conn()``; cursors are
created per-call and closed automatically. Long-running transactions should
use the ``with db.transaction():`` context manager.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# DDL — 15 tables (14 from PRD §3 + schema_migrations meta)
# --------------------------------------------------------------------------- #

DDL_STATEMENTS: list[str] = [
    # ---- meta ----
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        filename   TEXT
    )
    """,
    # ---- content pipeline ----
    """
    CREATE TABLE IF NOT EXISTS pieces (
        id                  TEXT PRIMARY KEY,
        selection_card_yaml TEXT NOT NULL,
        state               TEXT NOT NULL CHECK(state IN (
                                'candidate','selected','drafted',
                                'reviewed','scheduled','published','measured'
                            )),
        created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_actor          TEXT,
        notes               TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        piece_id   TEXT NOT NULL,
        from_state TEXT,
        to_state   TEXT NOT NULL,
        actor      TEXT,
        timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP,
        notes      TEXT,
        FOREIGN KEY(piece_id) REFERENCES pieces(id)
    )
    """,
    # ---- publishings + metrics ----
    """
    CREATE TABLE IF NOT EXISTS publishings (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        piece_id         TEXT NOT NULL,
        platform         TEXT NOT NULL,
        external_post_id TEXT,
        postiz_post_id   TEXT,
        published_at     DATETIME,
        utm_campaign     TEXT,
        utm_content      TEXT,
        utm_term         TEXT,
        FOREIGN KEY(piece_id) REFERENCES pieces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics_daily (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        publishing_id         INTEGER,
        snapshot_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
        snapshot_type         TEXT CHECK(snapshot_type IN ('30m','24h','7d')),
        impressions           INTEGER,
        likes                 INTEGER,
        replies               INTEGER,
        quotes                INTEGER,
        retweets              INTEGER,
        shares                INTEGER,
        bookmarks             INTEGER,
        profile_clicks        INTEGER,
        link_clicks           INTEGER,
        impressions_30m       INTEGER,
        engagement_count_30m  INTEGER,
        fetched_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
        raw_json              TEXT,
        FOREIGN KEY(publishing_id) REFERENCES publishings(id)
    )
    """,
    # ---- landing + leads ----
    """
    CREATE TABLE IF NOT EXISTS landing_metrics (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date        DATE NOT NULL,
        page_path            TEXT NOT NULL,
        utm_source           TEXT,
        utm_medium           TEXT,
        utm_campaign         TEXT,
        utm_content          TEXT,
        utm_term             TEXT,
        sessions             INTEGER,
        users                INTEGER,
        pageviews            INTEGER,
        avg_duration_seconds INTEGER,
        conversions          INTEGER,
        fetched_at           DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leads (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        email                       TEXT UNIQUE,
        email_hash                  TEXT,
        first_seen_at               DATETIME,
        first_landing_page          TEXT,
        first_utm_campaign          TEXT,
        first_utm_content           TEXT,
        first_utm_term              TEXT,
        last_seen_at                DATETIME,
        qualified                   BOOLEAN DEFAULT FALSE,
        sql_status                  TEXT,
        sql_verified_at             DATETIME,
        sql_verified_by             TEXT,
        bd_attribution_content_id   TEXT,
        bounced                     BOOLEAN DEFAULT FALSE,
        notes                       TEXT
    )
    """,
    # ---- newsletter ----
    """
    CREATE TABLE IF NOT EXISTS newsletter_campaigns (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id     TEXT UNIQUE,
        send_time       DATETIME,
        subject_a       TEXT,
        subject_b       TEXT,
        emails_sent     INTEGER,
        opens           INTEGER,
        open_rate       REAL,
        clicks          INTEGER,
        click_rate      REAL,
        hard_bounces    INTEGER,
        soft_bounces    INTEGER,
        unsubscribed    INTEGER,
        fetched_at      DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS newsletter_link_clicks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id   TEXT,
        url           TEXT,
        clicks        INTEGER,
        unique_clicks INTEGER
    )
    """,
    # ---- touchpoints inside TaskOn platform ----
    """
    CREATE TABLE IF NOT EXISTS btouch_daily (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date   DATE,
        touchpoint_id   INTEGER,
        touchpoint_name TEXT,
        impressions     INTEGER,
        clicks          INTEGER,
        ctr             REAL,
        fetched_at      DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ---- attribution ----
    """
    CREATE TABLE IF NOT EXISTS user_journey (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id        TEXT NOT NULL,
        touchpoint_seq INTEGER,
        action         TEXT,
        utm_source     TEXT,
        utm_medium     TEXT,
        utm_campaign   TEXT,
        utm_content    TEXT,
        utm_term       TEXT,
        referrer       TEXT,
        page_path      TEXT,
        piece_id       TEXT,
        timestamp      DATETIME,
        raw_data       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS weekly_aggregates (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        week                    TEXT,
        dimension               TEXT,
        value                   TEXT,
        publish_count           INTEGER,
        avg_impressions         REAL,
        avg_engagement_rate     REAL,
        avg_click_through_rate  REAL,
        leads_attributed        INTEGER,
        sql_attributed          INTEGER,
        weight_for_next_week    REAL
    )
    """,
    # ---- health ----
    """
    CREATE TABLE IF NOT EXISTS heartbeat (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name         TEXT,
        last_run_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        status           TEXT CHECK(status IN ('ok','warning','failed')),
        duration_seconds INTEGER,
        error_message    TEXT,
        rows_written     INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publish_failures (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        severity        TEXT CHECK(severity IN ('P0','P1','P2')),
        source          TEXT,
        failure_type    TEXT,
        failure_detail  TEXT,
        recovered       BOOLEAN DEFAULT FALSE,
        recovery_at     DATETIME,
        recovery_actor  TEXT
    )
    """,
    # ---- KOL watch ----
    """
    CREATE TABLE IF NOT EXISTS kol_watchlist (
        handle              TEXT PRIMARY KEY,
        tier                TEXT CHECK(tier IN ('A','B','C')),
        relationship_status TEXT,
        last_dm_date        DATE,
        last_dm_content     TEXT,
        next_action         TEXT,
        notes               TEXT,
        updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ---- candidate pool (路 1-6 汇合) ----
    """
    CREATE TABLE IF NOT EXISTS candidates (
        candidate_id                TEXT PRIMARY KEY,
        source_route                INTEGER CHECK(source_route BETWEEN 1 AND 6),
        title_hypothesis            TEXT,
        narrative_anchor_potential  TEXT,
        data_sources_hint           TEXT,
        why_relevant                TEXT,
        relevance_score_self        INTEGER,
        ranker_score                INTEGER,
        status                      TEXT CHECK(status IN ('raw','scored','picked','dropped')),
        created_at                  DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ---- indexes ----
    "CREATE INDEX IF NOT EXISTS idx_journey_user      ON user_journey(user_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_journey_piece     ON user_journey(piece_id)",
    "CREATE INDEX IF NOT EXISTS idx_pieces_state      ON pieces(state)",
    "CREATE INDEX IF NOT EXISTS idx_publishings_piece ON publishings(piece_id)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_pub       ON metrics_daily(publishing_id, snapshot_type)",
    "CREATE INDEX IF NOT EXISTS idx_landing_date      ON landing_metrics(snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_heartbeat_job     ON heartbeat(job_name, last_run_at)",
]


# --------------------------------------------------------------------------- #
# Dataclass row shapes (typed return values for the most-used queries)
# --------------------------------------------------------------------------- #


@dataclass
class Piece:
    id: str
    selection_card_yaml: str
    state: str
    created_at: str
    updated_at: str
    last_actor: str | None = None
    notes: str | None = None

    def card(self) -> dict[str, Any]:
        """Return parsed selection_card payload (YAML or JSON-encoded)."""
        try:
            import yaml

            return yaml.safe_load(self.selection_card_yaml) or {}
        except Exception:  # pragma: no cover - fall back to JSON
            try:
                return json.loads(self.selection_card_yaml)
            except Exception:
                return {}


@dataclass
class Publishing:
    id: int
    piece_id: str
    platform: str
    external_post_id: str | None
    postiz_post_id: str | None
    published_at: str | None
    utm_campaign: str | None
    utm_content: str | None
    utm_term: str | None


# --------------------------------------------------------------------------- #
# Domain adapters
# --------------------------------------------------------------------------- #


class _PiecesAdapter:
    def __init__(self, parent: "Database") -> None:
        self._db = parent

    def create(self, piece_id: str, selection_card: str | dict, actor: str = "system") -> None:
        """Insert a new piece in state='candidate'.

        ``selection_card`` may be a dict, a JSON string, or a YAML string. All
        three are normalized to a canonical JSON string for storage so that
        downstream consumers (notably ``json_extract`` in
        ``attribution_engine.compute_weekly_aggregates``) can pull fields by
        path without caring about the input format.
        """
        payload = self._normalize_card(selection_card)
        self._db.execute(
            "INSERT INTO pieces (id, selection_card_yaml, state, last_actor) VALUES (?, ?, 'candidate', ?)",
            (piece_id, payload, actor),
        )
        self._db.state_events.log(piece_id, None, "candidate", actor)

    @staticmethod
    def _normalize_card(card: str | dict) -> str:
        """Return a canonical JSON string regardless of YAML/JSON/dict input."""
        if isinstance(card, dict):
            return json.dumps(card, ensure_ascii=False, default=str)
        text = str(card).strip()
        # Try JSON first (cheap path); fall back to YAML.
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            try:
                import yaml

                obj = yaml.safe_load(text)
            except Exception:  # pragma: no cover - extremely malformed input
                obj = None
        if not isinstance(obj, dict):
            # Preserve the original text under a wrapper so json_extract calls
            # don't crash but the data is still inspectable.
            obj = {"_raw": text}
        # default=str makes date / datetime / Path / Decimal etc. serializable
        # — YAML parsing in particular returns ``datetime.date`` for unquoted
        # ISO dates in the selection card, and naive json.dumps() raises
        # TypeError. Stringifying is lossy but consistent (callers re-parse
        # the JSON; date strings remain valid ISO and are queryable via
        # SQLite's json_extract for attribution_engine).
        return json.dumps(obj, ensure_ascii=False, default=str)

    def update_state(self, piece_id: str, new_state: str, actor: str = "system", notes: str | None = None) -> None:
        old = self._db.fetchone("SELECT state FROM pieces WHERE id = ?", (piece_id,))
        from_state = old["state"] if old else None
        self._db.execute(
            "UPDATE pieces SET state = ?, updated_at = CURRENT_TIMESTAMP, last_actor = ? WHERE id = ?",
            (new_state, actor, piece_id),
        )
        self._db.state_events.log(piece_id, from_state, new_state, actor, notes)

    def get(self, piece_id: str) -> Piece | None:
        row = self._db.fetchone("SELECT * FROM pieces WHERE id = ?", (piece_id,))
        if not row:
            return None
        return Piece(
            id=row["id"],
            selection_card_yaml=row["selection_card_yaml"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_actor=row["last_actor"],
            notes=row["notes"],
        )

    def list_by_state(self, state: str) -> list[Piece]:
        rows = self._db.fetchall("SELECT * FROM pieces WHERE state = ? ORDER BY updated_at DESC", (state,))
        return [
            Piece(
                id=r["id"],
                selection_card_yaml=r["selection_card_yaml"],
                state=r["state"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                last_actor=r["last_actor"],
                notes=r["notes"],
            )
            for r in rows
        ]

    def published_after(self, iso_date: str) -> list[Piece]:
        rows = self._db.fetchall(
            "SELECT * FROM pieces WHERE state = 'published' AND updated_at >= ? ORDER BY updated_at",
            (iso_date,),
        )
        return [
            Piece(
                id=r["id"],
                selection_card_yaml=r["selection_card_yaml"],
                state=r["state"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                last_actor=r["last_actor"],
                notes=r["notes"],
            )
            for r in rows
        ]


class _StateEventsAdapter:
    def __init__(self, parent: "Database") -> None:
        self._db = parent

    def log(
        self,
        piece_id: str,
        from_state: str | None,
        to_state: str,
        actor: str,
        notes: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO state_events (piece_id, from_state, to_state, actor, notes) VALUES (?, ?, ?, ?, ?)",
            (piece_id, from_state, to_state, actor, notes),
        )


class _PublishingsAdapter:
    def __init__(self, parent: "Database") -> None:
        self._db = parent

    def upsert(
        self,
        *,
        piece_id: str,
        platform: str,
        external_post_id: str | None = None,
        postiz_post_id: str | None = None,
        published_at: str | None = None,
        utm_campaign: str | None = None,
        utm_content: str | None = None,
        utm_term: str | None = None,
        media_path: str | None = None,
    ) -> int:
        # ``media_path`` is added by migration 008; tolerate the column being
        # absent so older state.db files still upsert without crashing.
        try:
            cur = self._db.execute(
                """
                INSERT INTO publishings (piece_id, platform, external_post_id, postiz_post_id,
                                         published_at, utm_campaign, utm_content, utm_term,
                                         media_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (piece_id, platform, external_post_id, postiz_post_id, published_at,
                 utm_campaign, utm_content, utm_term, media_path),
            )
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc).lower():
                raise
            # Pre-migration-008 schema — fall back to the original 8-column insert.
            logger.warning(
                "publishings.media_path column missing (migration 008 not applied?); "
                "falling back to legacy insert"
            )
            cur = self._db.execute(
                """
                INSERT INTO publishings (piece_id, platform, external_post_id, postiz_post_id,
                                         published_at, utm_campaign, utm_content, utm_term)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (piece_id, platform, external_post_id, postiz_post_id, published_at,
                 utm_campaign, utm_content, utm_term),
            )
        return cur.lastrowid or 0

    def set_media_path(self, publishing_id: int, media_path: str) -> None:
        """Stamp ``media_path`` on an existing publishings row (post-rendering)."""
        try:
            self._db.execute(
                "UPDATE publishings SET media_path = ? WHERE id = ?",
                (media_path, publishing_id),
            )
        except sqlite3.OperationalError as exc:
            if "no such column" in str(exc).lower():
                logger.warning(
                    "publishings.media_path missing; skipping set_media_path (run migration 008)"
                )
                return
            raise

    def get_by_external(self, platform: str, external_post_id: str) -> Publishing | None:
        row = self._db.fetchone(
            "SELECT * FROM publishings WHERE platform = ? AND external_post_id = ?",
            (platform, external_post_id),
        )
        if not row:
            return None
        return Publishing(**{k: row[k] for k in Publishing.__dataclass_fields__})  # type: ignore[arg-type]


class _MetricsAdapter:
    def __init__(self, parent: "Database") -> None:
        self._db = parent

    def insert(self, **fields: Any) -> int:
        cols = ",".join(fields.keys())
        placeholders = ",".join(["?"] * len(fields))
        cur = self._db.execute(
            f"INSERT INTO metrics_daily ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cur.lastrowid or 0


class _HeartbeatAdapter:
    def __init__(self, parent: "Database") -> None:
        self._db = parent

    def record(
        self,
        job_name: str,
        status: str,
        duration_seconds: int,
        rows_written: int = 0,
        error_message: str | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO heartbeat (job_name, status, duration_seconds, rows_written, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_name, status, duration_seconds, rows_written, error_message),
        )

    def last(self, job_name: str) -> dict[str, Any] | None:
        row = self._db.fetchone(
            "SELECT * FROM heartbeat WHERE job_name = ? ORDER BY last_run_at DESC LIMIT 1",
            (job_name,),
        )
        return dict(row) if row else None


class _GenericTableAdapter:
    """Lightweight generic adapter for tables that only need ``insert`` /
    ``insert_many`` / ``fetchall``. Used for the long-tail tables that don't
    need a hand-written API surface."""

    def __init__(self, parent: "Database", table: str) -> None:
        self._db = parent
        self._table = table

    def insert(self, **fields: Any) -> int:
        cols = ",".join(fields.keys())
        placeholders = ",".join(["?"] * len(fields))
        cur = self._db.execute(
            f"INSERT INTO {self._table} ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cur.lastrowid or 0

    def insert_many(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        cols = list(rows[0].keys())
        placeholders = ",".join(["?"] * len(cols))
        sql = f"INSERT INTO {self._table} ({','.join(cols)}) VALUES ({placeholders})"
        values: list[tuple[Any, ...]] = [tuple(r[c] for c in cols) for r in rows]
        self._db.executemany(sql, values)
        return len(values)

    def all(self, where: str = "", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        sql = f"SELECT * FROM {self._table}"
        if where:
            sql += f" WHERE {where}"
        return self._db.fetchall(sql, tuple(params))


# --------------------------------------------------------------------------- #
# Main Database singleton
# --------------------------------------------------------------------------- #


class Database:
    """Thread-safe SQLite singleton with WAL + DDL bootstrap + adapter facade."""

    _instance: "Database | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "Database":  # noqa: D401
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._path = self._resolve_path()
        self._ensure_parent_dir()
        self._initialized = True

        # Lazy adapters; instantiated on first attribute access.
        self.pieces = _PiecesAdapter(self)
        self.state_events = _StateEventsAdapter(self)
        self.publishings = _PublishingsAdapter(self)
        self.metrics_daily = _MetricsAdapter(self)
        self.landing_metrics = _GenericTableAdapter(self, "landing_metrics")
        self.leads = _GenericTableAdapter(self, "leads")
        self.newsletter_campaigns = _GenericTableAdapter(self, "newsletter_campaigns")
        self.newsletter_link_clicks = _GenericTableAdapter(self, "newsletter_link_clicks")
        self.btouch_daily = _GenericTableAdapter(self, "btouch_daily")
        self.user_journey = _GenericTableAdapter(self, "user_journey")
        self.weekly_aggregates = _GenericTableAdapter(self, "weekly_aggregates")
        self.heartbeat = _HeartbeatAdapter(self)
        self.publish_failures = _GenericTableAdapter(self, "publish_failures")
        self.kol_watchlist = _GenericTableAdapter(self, "kol_watchlist")
        self.candidates = _GenericTableAdapter(self, "candidates")

        # DDL is bootstrapped on first connect; force it now so callers fail
        # fast if the DB is unreachable.
        self._ensure_schema()

    # -- path / connection -------------------------------------------------- #

    @staticmethod
    def _resolve_path() -> Path:
        from os import environ

        env_path = environ.get("SQLITE_PATH")
        if env_path:
            return Path(env_path).expanduser().resolve()
        # Default: engine/runtime/state.db relative to this file.
        return (Path(__file__).resolve().parent.parent / "runtime" / "state.db").resolve()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_parent_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        """Return a per-thread connection. Creates one if missing."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(
            str(self._path),
            timeout=30.0,
            isolation_level=None,        # explicit transaction control
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        # PRAGMAs: WAL + busy_timeout + FK
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        self._local.conn = conn
        return conn

    # -- DDL bootstrap + migrations ---------------------------------------- #

    def _ensure_schema(self) -> None:
        conn = self._conn()
        with self._write_lock:
            for stmt in DDL_STATEMENTS:
                conn.execute(stmt)
            conn.commit()
        self._run_migrations()

    def _run_migrations(self) -> None:
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if not migrations_dir.is_dir():
            return
        files = sorted(p for p in migrations_dir.glob("*.sql"))
        if not files:
            return
        conn = self._conn()
        applied = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
        for p in files:
            try:
                version = int(p.name.split("_", 1)[0])
            except ValueError:
                logger.warning("skip migration with non-numeric prefix: %s", p.name)
                continue
            if version in applied:
                continue
            sql = p.read_text(encoding="utf-8")
            with self._write_lock:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, filename) VALUES (?, ?)",
                    (version, p.name),
                )
                conn.commit()
            logger.info("migration applied: %s", p.name)

    # -- raw SQL surface ---------------------------------------------------- #

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = self._conn()
        with self._write_lock:
            return conn.execute(sql, params)

    def executemany(self, sql: str, params_seq: Iterable[tuple]) -> sqlite3.Cursor:
        conn = self._conn()
        with self._write_lock:
            return conn.executemany(sql, list(params_seq))

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, params).fetchall())

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction scope. Use for multi-statement atomicity."""
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- maintenance -------------------------------------------------------- #

    def vacuum(self) -> None:
        with self._write_lock:
            self._conn().execute("VACUUM")

    def backup_to(self, dest_path: str | os.PathLike[str]) -> None:
        """Online backup via SQLite Backup API. Safe while writers are active."""
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest_conn = sqlite3.connect(str(dest))
        try:
            with self._write_lock:
                self._conn().backup(dest_conn)
        finally:
            dest_conn.close()

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


# Module-level singleton -- the only blessed handle.
db: Database = Database()


__all__ = ["db", "Database", "Piece", "Publishing", "DDL_STATEMENTS"]
