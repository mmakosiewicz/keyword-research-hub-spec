# Data Model

Seven tables. Names are suggested; you can rename freely as long as relationships hold.

## `settings`

A flat key/value bag for app config. Single-tenant; no user_id column.

| column | type | notes |
|---|---|---|
| key | text PK | e.g. `target_site`, `target_country`, `competitors`, `filters` |
| value | jsonb | arbitrary JSON |
| updated_at | timestamptz | |

Common keys:
- `target_site` (string, e.g. `"ahrefs.com"`)
- `target_country` (string, ISO-2, e.g. `"us"`)
- `competitors` (string[], e.g. `["semrush.com", "moz.com"]`)
- `filters` (object — see [`filter-pipeline.md`](./filter-pipeline.md))

## `seeds`

User-managed list of seed keywords for Discovery.

| column | type |
|---|---|
| id | serial PK |
| keyword | text UNIQUE |
| created_at | timestamptz |

## `sessions`

A workflow run. Every Discovery / Content Gap / Breakout / Targets run creates a row.

| column | type | notes |
|---|---|---|
| id | serial PK | |
| tab | text | `'discovery' \| 'content_gap' \| 'breakout' \| 'targets'` |
| filters | jsonb | the filter config used for this run |
| status | text | `'running' \| 'completed' \| 'failed'`, default `'running'` |
| started_at | timestamptz | |
| completed_at | timestamptz | nullable |
| summary | jsonb | e.g. `{"seeds_used": 12, "breakout_count": 8, "cannibalization_count": 3}` |

Indexes: `(tab)`.

## `results`

The keyword rows for a session.

| column | type | notes |
|---|---|---|
| id | serial PK | |
| session_id | int FK → sessions.id ON DELETE CASCADE | |
| keyword | text NOT NULL | |
| volume | int | monthly search volume |
| traffic_potential | int | |
| difficulty | int | Ahrefs KD 0–100 |
| cpc_cents | int | |
| position | int | current ranking on target_site, nullable |
| ranking_url | text | nullable |
| parent_topic | text | nullable |
| parent_topic_kd | int | nullable |
| source | text | comma-separated seeds/competitors that surfaced this keyword |
| competitors | jsonb | `[{domain, position, url, traffic}, ...]` — Content Gap only |
| volume_history | jsonb | `[{date: "YYYY-MM", volume: int}, ...]` — last 12 months |
| trend_3m | real | growth rate, 3-month |
| trend_6m | real | growth rate, 6-month |
| extra | jsonb | workflow-specific extras (e.g. Breakout's `{status, other_url, other_position}`) |
| is_new | bool | true if first seen in this session (vs. carried over from previous) |

Unique: `(session_id, keyword)`. Indexes: `(keyword)`, `(session_id)`.

## `keyword_lists`

Saved lists where users park interesting keywords across sessions.

| column | type | notes |
|---|---|---|
| id | serial PK | |
| keyword | text | |
| list_name | text | |
| added_at | timestamp | |

Unique: `(keyword, list_name)`. Indexes: `(keyword)`, `(list_name)`.

**Important rule**: a keyword can only be in **one** list at a time. Adding to a new list removes it from any previous list (delete-before-insert).

## `targets`

User's permanent ranking-targets watchlist (separate from the Discovery seed list).

| column | type |
|---|---|
| id | serial PK |
| keyword | text UNIQUE |
| created_at | timestamptz |

## `jobs` (in-memory or DB-backed)

Background job tracking. In-memory dict is fine for single-process; switch to Postgres when you scale.

Shape:
```json
{
  "<job_id>": {
    "status": "running | completed | failed",
    "progress": "Human-readable status string",
    "session_id": 42,
    "error": "stacktrace if failed"
  }
}
```

Polled by clients via `GET /api/job/<job_id>`.

## Sample data

A real-world `keyword_tiers.json` ([sample-data/keyword_tiers.sample.json](./sample-data/keyword_tiers.sample.json)) is included so the recreating agent has concrete shape to code against.
