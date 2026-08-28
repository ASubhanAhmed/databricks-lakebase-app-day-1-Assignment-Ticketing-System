"""
Internal support desk - Databricks App (FastAPI + Lakebase).

All ticket and message data lives in Lakebase. Nothing is held in memory.

Endpoints
    GET    /                              web UI
    GET    /healthz                       health check (also pings Lakebase)
    GET    /api/tickets                   list tickets, optional ?status=
    POST   /api/tickets                   create a ticket (+ optional first message)
    GET    /api/tickets/{id}              one ticket
    GET    /api/tickets/{id}/messages     messages on a ticket
    POST   /api/tickets/{id}/messages     add a message
    PATCH  /api/tickets/{id}/status       update the status
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import text

from lakebase import TICKET_STATUSES, get_connection, get_engine, init_db

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("support-app")

Status = Literal["open", "in_progress", "resolved"]


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    seed = os.environ.get("SEED_SAMPLE_DATA", "true").lower() in ("1", "true", "yes")
    init_db(seed=seed)
    yield
    get_engine().dispose()


app = FastAPI(title="Internal Support Desk", version="1.0.0", lifespan=lifespan)

# absolute path so the app works no matter what directory it is launched from
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


# --------------------------------------------------------------------------
# Who is calling
# --------------------------------------------------------------------------

def current_user(request: Request) -> str:
    """
    Databricks Apps forwards the signed-in user on these headers.
    Falls back to a local placeholder when running outside Databricks.
    """
    for header in ("X-Forwarded-Email", "X-Forwarded-Preferred-Username", "X-Forwarded-User"):
        value = request.headers.get(header)
        if value:
            return value
    return os.environ.get("LOCAL_USER", "local.dev@example.com")


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class TicketCreate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    status: Status = "open"
    created_by: str | None = None
    first_message: str | None = Field(default=None, max_length=5000)


class MessageCreate(BaseModel):
    message_text: str = Field(min_length=1, max_length=5000)
    author: str | None = None


class StatusUpdate(BaseModel):
    status: Status


class Ticket(BaseModel):
    ticket_id: int
    title: str
    status: Status
    created_by: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class Message(BaseModel):
    message_id: int
    ticket_id: int
    message_text: str
    author: str
    created_at: datetime


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _ticket_or_404(conn, ticket_id: int) -> dict:
    row = conn.execute(
        text("SELECT ticket_id FROM tickets WHERE ticket_id = :id"),
        {"id": ticket_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return dict(row._mapping)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"user": current_user(request), "statuses": list(TICKET_STATUSES)},
    )


@app.get("/healthz")
def healthz():
    try:
        with get_connection() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "lakebase": "connected"}
    except Exception as exc:  # surfaced in the Databricks app logs
        logger.exception("Health check failed")
        raise HTTPException(status_code=503, detail=f"Lakebase unreachable: {exc}") from exc


@app.get("/api/tickets", response_model=list[Ticket])
def list_tickets(status: Status | None = Query(default=None)):
    sql = """
        SELECT t.ticket_id,
               t.title,
               t.status,
               t.created_by,
               t.created_at,
               t.updated_at,
               count(m.message_id) AS message_count
          FROM tickets t
          LEFT JOIN ticket_messages m ON m.ticket_id = t.ticket_id
         WHERE (:status IS NULL OR t.status = :status)
      GROUP BY t.ticket_id
      ORDER BY t.updated_at DESC
    """
    with get_connection() as conn:
        rows = conn.execute(text(sql), {"status": status}).mappings().all()
    return [Ticket(**row) for row in rows]


@app.post("/api/tickets", response_model=Ticket, status_code=201)
def create_ticket(payload: TicketCreate, request: Request):
    author = payload.created_by or current_user(request)
    with get_connection() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO tickets (title, status, created_by)
                VALUES (:title, :status, :created_by)
                RETURNING ticket_id, title, status, created_by, created_at, updated_at
                """
            ),
            {"title": payload.title.strip(), "status": payload.status, "created_by": author},
        ).mappings().one()

        count = 0
        if payload.first_message and payload.first_message.strip():
            conn.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id, message_text, author)
                    VALUES (:ticket_id, :message_text, :author)
                    """
                ),
                {
                    "ticket_id": row["ticket_id"],
                    "message_text": payload.first_message.strip(),
                    "author": author,
                },
            )
            count = 1

    return Ticket(**row, message_count=count)


@app.get("/api/tickets/{ticket_id}", response_model=Ticket)
def get_ticket(ticket_id: int):
    sql = """
        SELECT t.ticket_id, t.title, t.status, t.created_by, t.created_at, t.updated_at,
               count(m.message_id) AS message_count
          FROM tickets t
          LEFT JOIN ticket_messages m ON m.ticket_id = t.ticket_id
         WHERE t.ticket_id = :id
      GROUP BY t.ticket_id
    """
    with get_connection() as conn:
        row = conn.execute(text(sql), {"id": ticket_id}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return Ticket(**row)


@app.get("/api/tickets/{ticket_id}/messages", response_model=list[Message])
def list_messages(ticket_id: int):
    with get_connection() as conn:
        _ticket_or_404(conn, ticket_id)
        rows = conn.execute(
            text(
                """
                SELECT message_id, ticket_id, message_text, author, created_at
                  FROM ticket_messages
                 WHERE ticket_id = :id
              ORDER BY created_at, message_id
                """
            ),
            {"id": ticket_id},
        ).mappings().all()
    return [Message(**row) for row in rows]


@app.post("/api/tickets/{ticket_id}/messages", response_model=Message, status_code=201)
def add_message(ticket_id: int, payload: MessageCreate, request: Request):
    author = payload.author or current_user(request)
    with get_connection() as conn:
        _ticket_or_404(conn, ticket_id)
        row = conn.execute(
            text(
                """
                INSERT INTO ticket_messages (ticket_id, message_text, author)
                VALUES (:ticket_id, :message_text, :author)
                RETURNING message_id, ticket_id, message_text, author, created_at
                """
            ),
            {"ticket_id": ticket_id, "message_text": payload.message_text.strip(), "author": author},
        ).mappings().one()

        # a new message counts as activity, so the ticket floats to the top
        conn.execute(
            text("UPDATE tickets SET updated_at = now() WHERE ticket_id = :id"),
            {"id": ticket_id},
        )
    return Message(**row)


@app.patch("/api/tickets/{ticket_id}/status", response_model=Ticket)
def update_status(ticket_id: int, payload: StatusUpdate):
    with get_connection() as conn:
        row = conn.execute(
            text(
                """
                UPDATE tickets
                   SET status = :status, updated_at = now()
                 WHERE ticket_id = :id
             RETURNING ticket_id, title, status, created_by, created_at, updated_at
                """
            ),
            {"status": payload.status, "id": ticket_id},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
        count = conn.execute(
            text("SELECT count(*) FROM ticket_messages WHERE ticket_id = :id"),
            {"id": ticket_id},
        ).scalar_one()
    return Ticket(**row, message_count=count)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("DATABRICKS_APP_PORT", os.environ.get("PORT", 8000)))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
