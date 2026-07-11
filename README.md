# NaviKnow — Step 0 Walking Skeleton

A thin end-to-end slice through the NaviKnow stack: auth, Postgres, a Bedrock
(Claude) call, local file storage, and a generic frontend demo view — proven
via one trivial "Demo Agent" (`dummy-echo`). No real KM agent logic yet.

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

The backend also re-seeds the five roles on startup as a safety net, so it
works even against a Postgres instance that didn't run `init.sql`.

## 3. Frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

## 4. Open the app

Visit **http://localhost:5173**, sign up, log in, open **Agents**, pick
**Demo Agent**, submit some text, and watch the result (output, confidence,
flags, steps, token/cost) render on screen.

- The run + its steps/flags are stored in Postgres (`agent_runs`, `run_steps`,
  `run_flags`).
- The full result JSON is also written to `backend/outputs/dummy-echo/<run_id>.json`.
- **Run history** lists all past runs and links back into the result view.

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
- **New users default to the `read_only` role**; promote via direct DB update
  for now (no admin UI yet, also per spec).
- Postgres runs in Docker; backend and frontend run locally via `uvicorn`
  and `vite dev` for faster reload cycles during this early stage.

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
once. Background/async execution, an admin UI for roles, and the
`review_items`/`audit_log` tables (created but unused) are also deferred.
