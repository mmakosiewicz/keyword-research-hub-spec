"""Fresh-workspace schema bootstrap for Keyword Research Hub."""
import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS kr_settings (
  key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kr_seeds (
  id SERIAL PRIMARY KEY, keyword TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kr_targets (
  id SERIAL PRIMARY KEY, keyword TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kr_sessions (
  id SERIAL PRIMARY KEY, tab TEXT NOT NULL, filters JSONB DEFAULT '{}'::jsonb,
  status TEXT DEFAULT 'running', started_at TIMESTAMPTZ DEFAULT now(),
  completed_at TIMESTAMPTZ, summary JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_kr_sessions_tab ON kr_sessions(tab);
CREATE TABLE IF NOT EXISTS kr_results (
  id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES kr_sessions(id) ON DELETE CASCADE,
  keyword TEXT NOT NULL, volume INTEGER, traffic_potential INTEGER, difficulty INTEGER,
  cpc_cents INTEGER, position INTEGER, ranking_url TEXT, parent_topic TEXT,
  parent_topic_kd INTEGER, source TEXT, competitors JSONB DEFAULT '[]'::jsonb,
  volume_history JSONB, trend_3m REAL, trend_6m REAL, extra JSONB DEFAULT '{}'::jsonb,
  is_new BOOLEAN DEFAULT false, UNIQUE(session_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_kr_results_keyword ON kr_results(keyword);
CREATE INDEX IF NOT EXISTS idx_kr_results_session ON kr_results(session_id);
CREATE TABLE IF NOT EXISTS kr_keyword_lists (
  id SERIAL PRIMARY KEY, keyword TEXT NOT NULL, list_name TEXT NOT NULL,
  added_at TIMESTAMP DEFAULT now(), UNIQUE(keyword, list_name)
);
CREATE INDEX IF NOT EXISTS idx_kr_keyword_lists_keyword ON kr_keyword_lists(keyword);
CREATE INDEX IF NOT EXISTS idx_kr_keyword_lists_list ON kr_keyword_lists(list_name);
CREATE TABLE IF NOT EXISTS kr_tier_runs (
  id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'queued', step TEXT,
  progress JSONB DEFAULT '{}'::jsonb, config JSONB DEFAULT '{}'::jsonb,
  summary JSONB, error TEXT, created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ
);
"""

def ensure_schema():
    conn = psycopg2.connect(host="/var/run/postgresql", user="console", database="console_db")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.kr_sessions')")
            if cur.fetchone()[0] is not None:
                # Existing installation: do not attempt DDL that requires table ownership.
                return
            cur.execute(DDL)
        conn.commit()
    finally:
        conn.close()
