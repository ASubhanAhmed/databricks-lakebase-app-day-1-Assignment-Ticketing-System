"""
Lakebase (Databricks-managed Postgres) helper.

Reads a single connection string from the LAKEBASE_URL environment variable.
In Databricks Apps that value comes from a secret wired up in
App -> Resources (see app.yaml). Locally it comes from .env.

Responsibilities:
  * build one shared SQLAlchemy engine
  * create the tickets / ticket_messages tables if they don't exist
  * insert sample data on an empty database
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_engine: Engine | None = None


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    """Make a Lakebase connection string safe for SQLAlchemy + psycopg2."""
    url = url.strip()

    # SQLAlchemy wants an explicit driver; Lakebase hands out postgresql://
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]

    # Lakebase requires TLS. Add it if the pasted URL left it off.
    host_is_local = "localhost" in url or "127.0.0.1" in url
    if "sslmode=" not in url and not host_is_local:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"

    return url


def get_engine() -> Engine:
    """Return the shared engine, creating it on first use."""
    global _engine
    if _engine is not None:
        return _engine

    raw_url = os.environ.get("LAKEBASE_URL") #lakebase_url
    if not raw_url:
        raise RuntimeError(
            "LAKEBASE_URL is not set. In Databricks, add the connection string as a "
            "secret under App -> Resources and map it in app.yaml. Locally, put it in .env."
        )

    _engine = create_engine(
        _normalize_url(raw_url),
        pool_pre_ping=True,   # drop connections Lakebase closed while idle
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        future=True,
    )
    return _engine


@contextmanager
def get_connection() -> Iterator[Connection]:
    """Transactional connection. Commits on success, rolls back on error."""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

TICKET_STATUSES = ("open", "in_progress", "resolved")

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS tickets (
        ticket_id   BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        title       TEXT        NOT NULL,
        status      TEXT        NOT NULL DEFAULT 'open',
        created_by  TEXT        NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT tickets_status_check
            CHECK (status IN ('open', 'in_progress', 'resolved'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_messages (
        message_id   BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ticket_id    BIGINT      NOT NULL
            REFERENCES tickets (ticket_id) ON DELETE CASCADE,
        message_text TEXT        NOT NULL,
        author       TEXT        NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket_id ON ticket_messages (ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status)",
]


SAMPLE_TICKETS = [
    {
        "title": "VPN drops every few minutes on the 5th floor",
        "status": "open",
        "created_by": "priya.n@example.com",
        "messages": [
            ("priya.n@example.com", "Connection drops roughly every 4 minutes. Started this morning."),
            ("support.desk@example.com", "Thanks Priya. Which VPN client version are you on?"),
        ],
    },
    {
        "title": "Cannot access the finance dashboard",
        "status": "in_progress",
        "created_by": "marcus.l@example.com",
        "messages": [
            ("marcus.l@example.com", "I get a permission error opening the Q3 finance dashboard."),
            ("support.desk@example.com", "Your group membership was missing. Requesting access now."),
            ("marcus.l@example.com", "Appreciate it, no rush before Thursday."),
        ],
    },
    {
        "title": "New laptop request for incoming analyst",
        "status": "resolved",
        "created_by": "dana.k@example.com",
        "messages": [
            ("dana.k@example.com", "New analyst starts on the 14th and needs a laptop."),
            ("it.procurement@example.com", "Shipped, arriving the 12th. Closing this out."),
        ],
    },
]


def _seed_sample_data(conn: Connection) -> int:
    """Insert sample tickets. Only called when the tickets table is empty."""
    created = 0
    for ticket in SAMPLE_TICKETS:
        ticket_id = conn.execute(
            text(
                """
                INSERT INTO tickets (title, status, created_by)
                VALUES (:title, :status, :created_by)
                RETURNING ticket_id
                """
            ),
            {
                "title": ticket["title"],
                "status": ticket["status"],
                "created_by": ticket["created_by"],
            },
        ).scalar_one()

        for author, message_text in ticket["messages"]:
            conn.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id, message_text, author)
                    VALUES (:ticket_id, :message_text, :author)
                    """
                ),
                {"ticket_id": ticket_id, "message_text": message_text, "author": author},
            )
        created += 1
    return created


def init_db(seed: bool = True) -> None:
    """Create tables if needed, then seed sample data on a fresh database."""
    with get_connection() as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(text(statement))
        logger.info("Lakebase schema ready (tickets, ticket_messages).")

        if not seed:
            return

        existing = conn.execute(text("SELECT count(*) FROM tickets")).scalar_one()
        if existing:
            logger.info("Found %s existing tickets, skipping sample data.", existing)
            return

        created = _seed_sample_data(conn)
        logger.info("Inserted %s sample tickets with messages.", created)
