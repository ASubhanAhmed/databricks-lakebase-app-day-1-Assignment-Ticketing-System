# Internal Support Desk — Databricks App on Lakebase

A FastAPI app that runs as a Databricks App and keeps all of its data in Lakebase
(Databricks-managed Postgres). Users can browse a ticket queue, read a ticket's
thread, open new tickets, reply, and move a ticket between statuses.

Nothing is hard-coded in the app — every read and write goes to Lakebase.

## Files

| File | What it does |
| --- | --- |
| `app.py` | FastAPI routes + the web UI route |
| `lakebase.py` | Connection helper, table creation, sample data |
| `setup_secrets.py` | Run once from a notebook: saves the connection string to a secret scope |
| `templates/index.html` | Single-page UI (vanilla JS, no build step) |
| `app.yaml` | Databricks App config: start command + env from the secret resource |
| `requirements.txt` | Python dependencies |
| `.env.example` | Local dev template — copy to `.env`, never commit `.env` |

## Data model

Created automatically on startup with `CREATE TABLE IF NOT EXISTS`, so you never
run DDL by hand.

```sql
tickets
  ticket_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
  title       TEXT NOT NULL
  status      TEXT NOT NULL DEFAULT 'open'   -- CHECK: open | in_progress | resolved
  created_by  TEXT NOT NULL
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()

ticket_messages
  message_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
  ticket_id    BIGINT NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE
  message_text TEXT NOT NULL
  author       TEXT NOT NULL
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
```

`updated_at` is bumped on every status change and every new message, which is what
orders the queue.

### Sample data

On a **fresh** database (zero rows in `tickets`) the app inserts 3 tickets — one
`open`, one `in_progress`, one `resolved` — each with 2 or 3 messages. If the table
already has rows, seeding is skipped, so redeploys never duplicate anything. Set
`SEED_SAMPLE_DATA=false` in `app.yaml` to turn it off entirely.

## Setup

### 1. Create the Lakebase instance and a password role

1. In your workspace, open **Compute → Database instances** (also reachable from
   the Lakebase section of the catalog) and create an instance.
2. Wait for it to reach **Available**.
3. Open the instance → **Roles** and create a role using **password**
   authentication (not OAuth), so you get a static, non-expiring password.
4. Copy the connection string. It looks like:

```
postgresql://<role>:<password>@<host>.database.cloud.databricks.com:5432/databricks_postgres?sslmode=require
```

### 2. Store the connection string in a secret scope

Run `setup_secrets.py` once from a Databricks notebook:

```python
%pip install databricks-sdk --quiet
%run ./setup_secrets
```

It prompts for the scope, the key, and the connection string. The string is read
with `getpass`, so it never appears in cell output or shell history. Defaults are
scope `support-app`, key `lakebase-url`. The script creates the scope if it does
not exist, writes the secret, reads back the key list to confirm, and prints the
exact values for the next step.

### 3. Add the secret as an app resource

In your Databricks App → **Resources** → **Add resource** → **Secret**:

- **Scope** and **Key** = whatever `setup_secrets.py` printed
- **Resource key** = `lakebase-url`

`app.yaml` reads it with `valueFrom: lakebase-url` and exposes it to the app as
`LAKEBASE_URL`. The value never appears in the repo.

If you name the resource something else, change `valueFrom` to match.

### 4. Deploy

1. **Workspace → Create → Git folder**, pointed at this repo.
2. **Compute → Apps → Create app** (custom), source = that Git folder.
3. Deploy. Databricks reads `app.yaml` for the start command and env.
4. Open the app URL. `GET /healthz` should return
   `{"status":"ok","lakebase":"connected"}`.

After pushing changes: pull in the Git folder, then redeploy from the Apps UI.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web UI |
| GET | `/healthz` | Health check, pings Lakebase |
| GET | `/api/tickets` | All tickets, newest activity first. Optional `?status=open` |
| POST | `/api/tickets` | Create a ticket. Body: `title`, optional `status`, `first_message` |
| GET | `/api/tickets/{id}` | One ticket with its message count |
| GET | `/api/tickets/{id}/messages` | Messages on a ticket, oldest first |
| POST | `/api/tickets/{id}/messages` | Add a message. Body: `message_text` |
| PATCH | `/api/tickets/{id}/status` | Change status. Body: `status` |

Interactive docs are at `/docs`.

`created_by` and `author` default to the signed-in Databricks user, read from the
`X-Forwarded-Email` header that Databricks Apps adds to every request. Locally it
falls back to `LOCAL_USER` from `.env`.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # paste your LAKEBASE_URL
python app.py               # http://localhost:8000
```

Your laptop needs network access to the Lakebase instance. If that's blocked,
point `LAKEBASE_URL` at a local Postgres — the schema is plain Postgres and works
either way.

## Notes

- Invalid statuses are rejected twice: by the Pydantic `Literal` type (HTTP 422)
  and by the `CHECK` constraint in Postgres.
- Deleting a ticket cascades to its messages via the foreign key.
- `pool_pre_ping=True` handles connections Lakebase drops while the app is idle,
  which is the usual cause of a first-request failure after a quiet period.
- To stream these tables into Unity Catalog later, run
  `ALTER TABLE tickets REPLICA IDENTITY FULL;` (and the same for
  `ticket_messages`), then enable Lakebase CDF on the `public` schema.
