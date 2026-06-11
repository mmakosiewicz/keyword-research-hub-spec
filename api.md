# HTTP API

The minimum surface the recreation needs. Method + path + JSON shape.

## Settings

### `GET /api/settings`
Returns the full settings object:
```json
{
  "target_site": "ahrefs.com",
  "target_country": "us",
  "competitors": ["semrush.com", "moz.com"],
  "filters": { "min_volume": 100, "max_kd": null, ... }
}
```

### `POST /api/settings`
Body: a partial settings object. Each key is upserted into the `settings` table.

## Seeds (Discovery seed list)

- `GET /api/seeds` → `[{id, keyword}]`
- `POST /api/seeds` → `{keywords: ["seed1", "seed2"]}` → `{added: int}`
- `DELETE /api/seeds/<id>` → `{ok: true}`

## Sessions

- `GET /api/sessions?tab=discovery` → last 20 sessions for that tab, newest first
  ```json
  [{ "id": 1, "tab": "discovery", "filters": {...}, "status": "completed",
     "started_at": "...", "completed_at": "...", "summary": {...} }]
  ```
- `GET /api/sessions/<id>/results?sort=volume&dir=desc` → all `kr_results` rows for the session.
  - Allowed sort columns: `keyword, volume, traffic_potential, difficulty, cpc_cents, trend_3m, trend_6m, position`
  - `dir`: `asc | desc` (default `desc`). `NULLS LAST` ordering.
- `GET /api/sessions/<id>/csv` → CSV download

## Jobs

- `GET /api/job/<job_id>` →
  ```json
  { "status": "running | completed | failed",
    "progress": "Seed 3/12: 'link building' (matching)",
    "session_id": 42,
    "error": null }
  ```

Polled by the frontend every ~2s while a workflow runs.

## Workflow runs (kick-offs)

All four return `{ "job_id": "<uuid>" }` immediately and run in a background thread.

- `POST /api/discovery/run` → `{ "adhoc_seeds": ["optional", "extra", "seeds"] }`
  Merges with the persistent `seeds` table.
- `POST /api/content_gap/run` → `{ "competitors": [...], "kw_per_competitor": 500 }`
  Either field optional; falls back to settings.
- `POST /api/breakout/run` → `{}` (no body needed; uses settings)
- `POST /api/targets/run` → `{ "tags": ["high-priority", "writer-a"] }`

## Master List

The merge view across tabs.

- `GET /api/master/results?sort=volume&dir=desc` → deduped keyword rows from the latest completed session of each tab.
  - When the same keyword appears in two tabs, take the better data (higher volume, lower position, etc.).
  - Returned rows include a `tabs` field: `"discovery, content_gap"`.
- `GET /api/master/csv` → CSV

## Saved Lists

- `GET /api/lists` → `[{ list_name, count }]`
- `GET /api/lists/<list_name>` → all keyword rows in that list, joined with their latest `results` row for metrics
- `POST /api/lists/add` → `{ keywords: [...], list_name: "..." }` → `{ added: int }`
  - **Side effect**: the keyword is first removed from any other list it's in. One keyword, one list.
- `POST /api/lists/remove` → `{ keywords: [...] }` → `{ removed: int }`
- `POST /api/lists/lookup` → `{ keywords: [...] }` → `{ "<keyword>": "<list_name>", ... }`
  - For UI: show which list a keyword is already in.

## Ranking Targets (Ahrefs Rank Tracker)

- `GET /api/targets/config` → `{ project_id, default_tags: [...] }`
- `POST /api/targets/run` → kicks off rank tracker pull for selected tags (see Sessions / Jobs)

## Response conventions

- All routes return JSON unless explicitly CSV.
- Errors: `{ "error": "<message>" }` with appropriate HTTP status.
- Timestamps: ISO-8601 strings.
- All keywords are stored and matched **lowercased**.
