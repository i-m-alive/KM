# NaviKnow — Step 0 Walking Skeleton + Step 0.5 RBAC

A thin end-to-end slice through the NaviKnow stack: auth, Postgres, a Bedrock
(Claude) call, local file storage, and a generic frontend demo view — proven
via one trivial "Demo Agent" (`dummy-echo`). No real KM agent logic yet.

On top of that, **Step 0.5** adds the RBAC foundation the real agents will
need: a 6-role capability matrix, an Admin Panel for user/role management,
and a KM-governance view for assigning practice-leads to client accounts.

## Stack

- Frontend: React + Vite
- Backend: FastAPI (Python 3.11+)
- Database: PostgreSQL
- LLM: Claude via Amazon Bedrock (Converse API, `boto3`)
- File storage: local filesystem (`backend/outputs/`)
- Auth: JWT access token (15 min) + refresh token (7 days, httpOnly cookie)

## Prerequisites

- Python 3.11+
- Node.js 18+
- Docker (for Postgres)
- AWS credentials with `bedrock:InvokeModel`/Converse access to a Claude model,
  available via the standard AWS credential chain (env vars, `~/.aws/credentials`,
  SSO, or an instance role) — this app does not manage credentials itself.

## 1. Start Postgres

```bash
docker compose up -d postgres
```

This creates the database and applies `backend/init.sql` (tables + seed roles)
automatically the first time the volume is created. Data persists across
restarts via the `naviknow_pgdata` docker volume.

Postgres is exposed on **host port 5433** (not the default 5432) to avoid
clashing with any Postgres you may already have running natively on your
machine — the container still uses 5432 internally. `DATABASE_URL` in
`.env.example` already points at 5433.

## 2. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: set JWT_SECRET, AWS_REGION, BEDROCK_MODEL_ID, and AWS credentials if not already in your environment
uvicorn app.main:app --reload --port 8000
```

The backend also re-seeds the six roles (`admin`, `km_governance`,
`km_reviewer`, `practice_lead`, `delivery`, `read_only`) on startup as a
safety net, and runs `Base.metadata.create_all()` so new tables (like
`client_accounts`/`account_ownership`) appear without wiping an existing dev
database.

**To unlock the Admin Panel:** every new signup defaults to `read_only`. Set
`ADMIN_BOOTSTRAP_EMAIL` in `backend/.env` to the email you signed up with,
then restart the backend — that account is promoted to `admin` on every
startup. From there, use the Admin Panel (nav bar, visible to `admin` and
`km_governance`) to promote other users.

### 2b. Background worker (required for the Sanitization & Tagging agents)

The real agents run asynchronously. In a **separate terminal**, start the
DB-polling worker:

```bash
cd backend && source .venv/bin/activate
python -m app.worker      # logs "worker started; polling every 2.0s"
```

The worker spawns the `naviknow-mcp` stdio server itself (as a subprocess) when
an agent needs it — you do not start the MCP server manually.

### 2c. Optional: Presidio NER model (better Sanitization recall)

The Sanitization pre-pass uses Microsoft Presidio for name/org/location
candidates. It's optional — without it the agent falls back to regex + the LLM —
but recommended. After `pip install`, download the spaCy model once:

```bash
python -m spacy download en_core_web_lg
# (or en_core_web_sm for a lighter, lower-accuracy model)
```

### 2d. Optional: LibreOffice (for in-browser document preview)

The "Compare original vs. sanitized" and document-preview pages convert
DOCX/PPTX to PDF via headless LibreOffice so the browser can render them
(PDFs need no conversion). Without it, previews return a 503 with an
install hint, but everything else — masking, download of the sanitized
file in its original format, image redaction — still works.

```bash
brew install --cask libreoffice   # Mac
```

If `soffice` isn't automatically found on your `PATH` after installing,
set `SOFFICE_PATH` in `backend/.env` to its full path (commonly
`/Applications/LibreOffice.app/Contents/MacOS/soffice` on Mac).

## 3. Frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

## 4. Open the app

Visit **http://localhost:5173**, sign up, and log in.

**Demo Agent (Step 0):** open **Agents → Demo Agent**, submit text, watch the
structured result render.

**Sanitization → Tagging (the real pipeline):**

1. Promote your user to a role that can submit + review. For a solo test the
   simplest path is: make yourself `admin` via `ADMIN_BOOTSTRAP_EMAIL`, then in
   the **Admin** panel create/promote a second account (or set your own role to
   `km_governance` in the DB). **Note:** submitter ≠ approver is enforced — the
   account that submits a run cannot approve it, so you need two accounts to
   exercise review end-to-end (e.g. a `delivery` submitter and a `km_governance`
   reviewer).
2. **Documents → Upload** a PDF or DOCX, then **Run Sanitization**. You land on
   the run page; it polls while the worker detects client identifiers, then
   parks at **awaiting_review**.
3. As a reviewer (different account with `km_reviewer`/`km_governance`), open
   **Review**, inspect the proposed masks, untick any false positives, and
   **Approve**. The masks are applied, the global masking dictionary grows, and
   a sanitized summary + metadata are produced.
4. Back on the completed Sanitization run, click **Run Tagging**. High-confidence
   in-vocabulary tags auto-apply; low-confidence tags or new-term proposals go to
   **Review**. Manage the vocabulary under **Tags**.

Everything is persisted in Postgres (`agent_runs`, `run_steps`, `run_flags`,
`masking_entities`/`masking_aliases`/`masking_occurrences`, `source_registry`,
`document_metadata`, `tag_vocabulary`, `run_tags`); run outputs are also written
under `backend/outputs/<agent_id>/<run_id>.json`.

## Design decisions made for Step 0

- **Migrations: raw SQL, not Alembic.** `backend/init.sql` is mounted into the
  Postgres container's init directory and applied once, on first volume
  creation. This keeps Step 0 minimal; introduce Alembic when the schema
  starts changing across environments that already have data.
- **Refresh token: httpOnly cookie.** `/auth/login`, `/auth/signup`, and
  `/auth/refresh` set it via `Set-Cookie` (path-scoped to `/auth`, `SameSite=Lax`).
  The access token lives only in memory in the frontend (`AuthContext` +
  `api/client.js`'s in-memory token store) — never `localStorage`. A 401 from
  any API call triggers a silent `/auth/refresh` and retries once.
  A `POST /auth/logout` endpoint (not explicitly requested, but a two-line
  necessity of the cookie approach) clears the cookie.
- **Synchronous execution.** `POST /runs` runs the agent inline and returns
  the completed result — no task queue yet, per the "don't over-engineer
  Step 0" instruction.
- **New users default to the `read_only` role**; promote via the Admin Panel
  (see above) once you have an admin account.
- Postgres runs in Docker; backend and frontend run locally via `uvicorn`
  and `vite dev` for faster reload cycles during this early stage.
- **Capability matrix, not scattered role lists** (`app/auth/permissions.py`):
  routes gate on named capabilities (`manage_users`, `assign_account_ownership`,
  etc.), each mapped to roles in one dict — a direct transcription of the
  RBAC matrix design doc, so it's the single place the permission model
  changes as new agents land.
- **`client_accounts` is a placeholder registry.** Real client identity only
  exists once Sanitization (A-01) is built; until then, KM-governance can
  create accounts and assign practice-leads to them, but there's no real
  document linked to an account yet.
- **Agent-level role gating**: `Agent.allowed_roles` (empty = all roles) is
  checked in `POST /runs` before a run is created, so future agents can
  restrict who's allowed to trigger them without touching the runs service.

## Repository layout

```
naviknow/
  backend/app/         FastAPI app (auth, agents, llm, storage, runs)
  backend/init.sql      schema + seed roles, applied by docker-compose
  backend/outputs/       local run outputs (gitignored, .gitkeep tracked)
  frontend/src/          React app (auth, agents, generic run-result components)
  docker-compose.yml     Postgres only
```

## What's NOT built yet (by design)

No real agents (Sanitization, Tagging, Cleanup, Coordinator, Search, Deck) —
only `dummy-echo`, whose only job is to exercise every layer of the stack
once. Background/async execution and the `review_items` table (created but
unused — mandatory human review lands with Sanitization) are also deferred.
