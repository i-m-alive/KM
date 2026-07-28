-- NaviKnow schema, applied once when the Postgres container's data volume is first created.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS roles (
  id SERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL   -- 'km_governance', 'km_reviewer', 'practice_lead', 'delivery', 'read_only'
);

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL,
  hashed_password TEXT NOT NULL,
  role_id INTEGER REFERENCES roles(id) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id TEXT NOT NULL,               -- matches Agent.agent_id
  status TEXT NOT NULL DEFAULT 'pending', -- pending | running | completed | failed
  input_json JSONB NOT NULL,
  output_json JSONB,
  confidence REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  estimated_cost_usd NUMERIC(10,4),
  output_file_path TEXT,
  created_by UUID REFERENCES users(id) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),  -- refreshed on every ORM update; the stale-run reaper's clock
  completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS run_steps (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE,
  step_order INTEGER NOT NULL,
  name TEXT NOT NULL,
  detail TEXT,
  tool TEXT,
  duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS run_flags (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE,
  message TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning'
);

-- Not used by Step 0, but created now so the schema doesn't need to change later:
CREATE TABLE IF NOT EXISTS review_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID REFERENCES agent_runs(id),
  reviewer_id UUID REFERENCES users(id),
  decision TEXT,             -- approved | edited | rejected
  notes TEXT,
  edits_json JSONB,
  decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id),
  actor_id UUID REFERENCES users(id),
  action TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Placeholder client-account registry: Sanitization (A-01) will later link
-- masked documents to these rows via its source registry. Created now so
-- account_ownership (practice-lead scoping) has something to reference.
CREATE TABLE IF NOT EXISTS client_accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account_ownership (
  id SERIAL PRIMARY KEY,
  user_id UUID REFERENCES users(id) ON DELETE CASCADE NOT NULL,
  client_account_id UUID REFERENCES client_accounts(id) ON DELETE CASCADE NOT NULL,
  assigned_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, client_account_id)
);

-- A-01 Sanitization + A-02 Tagging (built together).
-- NOTE: the backend also runs Base.metadata.create_all() at startup, so these
-- tables are created against an existing volume too; this block keeps fresh
-- installs in parity.
CREATE TABLE IF NOT EXISTS uploaded_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  filename TEXT NOT NULL,
  content_type TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  uploaded_by UUID REFERENCES users(id) NOT NULL,
  uploaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS masking_entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type TEXT NOT NULL,
  mask_token TEXT UNIQUE NOT NULL,
  client_account_id UUID REFERENCES client_accounts(id),
  status TEXT NOT NULL DEFAULT 'pending_approval',
  created_by_run_id UUID REFERENCES agent_runs(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  -- Reviewer-chosen replacement string (e.g. "Acme Pharma") used instead of
  -- mask_token when set. Global scope, same as mask_token - reused across
  -- every future document for this entity. See app.models.MaskingEntity.
  custom_replacement TEXT
);

-- CREATE TABLE IF NOT EXISTS is a no-op against an already-existing table,
-- so on a database created before custom_replacement existed, the column
-- above never actually gets added - this is the ALTER equivalent of that,
-- safe to re-run.
ALTER TABLE masking_entities ADD COLUMN IF NOT EXISTS custom_replacement TEXT;

CREATE TABLE IF NOT EXISTS masking_aliases (
  id SERIAL PRIMARY KEY,
  entity_id UUID REFERENCES masking_entities(id) ON DELETE CASCADE NOT NULL,
  raw_value TEXT NOT NULL,
  normalized_key TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS logo_references (
  id SERIAL PRIMARY KEY,
  mask_entity_id UUID REFERENCES masking_entities(id) ON DELETE CASCADE NOT NULL,
  phash TEXT NOT NULL,
  source_run_id UUID REFERENCES agent_runs(id),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Memoizes the raw vision-model verdict for one image's content, scoped to a
-- single run - detect() (worker process) and apply()'s verify (API process)
-- are genuinely different processes, so this is what lets the SAME run ask
-- the SAME question about the SAME image bytes once, not twice with a
-- possibly-different answer. Only the model's own judgment is cached; the
-- deterministic OCR/own-firm/logo-hash overrides in image_scan.py always
-- re-run fresh. See app.models.VisionVerdictCache.
CREATE TABLE IF NOT EXISTS vision_verdict_cache (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE NOT NULL,
  content_key TEXT NOT NULL,
  contains_client_identity BOOLEAN NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL,
  ocr_text JSONB NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (run_id, content_key)
);

CREATE TABLE IF NOT EXISTS masking_occurrences (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE NOT NULL,
  entity_id UUID REFERENCES masking_entities(id),
  chunk_id INTEGER NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset INTEGER NOT NULL,
  surface_text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_registry (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_account_id UUID REFERENCES client_accounts(id),
  run_id UUID REFERENCES agent_runs(id) NOT NULL,
  raw_identity_json JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_metadata (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE UNIQUE NOT NULL,
  sanitized_summary TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tag_vocabulary (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  category TEXT NOT NULL,
  value TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'approved',
  proposed_by UUID REFERENCES users(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (category, value)
);

CREATE TABLE IF NOT EXISTS run_tags (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE NOT NULL,
  tag_id UUID REFERENCES tag_vocabulary(id) NOT NULL,
  confidence REAL,
  status TEXT NOT NULL DEFAULT 'applied'
);

INSERT INTO roles (name) VALUES
  ('admin'), ('km_governance'), ('km_reviewer'), ('practice_lead'), ('delivery'), ('read_only')
ON CONFLICT (name) DO NOTHING;
