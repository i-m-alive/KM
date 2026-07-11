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
  decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  run_id UUID REFERENCES agent_runs(id),
  actor_id UUID REFERENCES users(id),
  action TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO roles (name) VALUES
  ('km_governance'), ('km_reviewer'), ('practice_lead'), ('delivery'), ('read_only')
ON CONFLICT (name) DO NOTHING;
