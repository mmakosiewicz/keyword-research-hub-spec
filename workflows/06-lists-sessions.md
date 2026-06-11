# Workflow 6: Lists & Sessions

**Goal:** Move discovered keywords from transient sessions into durable, user-managed buckets — "named lists" — and merge multiple session results into a single deduped view.

This is the "what do I actually care about?" layer above raw research output.

## Two concepts

### Sessions
Every workflow run creates a session row (see [data-model](../data-model.md#sessions)). Sessions are immutable history — they represent what the run found at that point in time, with that filter config.

### Lists
Persistent, user-defined buckets across sessions. Examples a user might create:
- `"backlog"` — keywords I'll write about eventually
- `"q3-priorities"` — this quarter's focus
- `"maybe"` — interesting but unconvinced
- `"rejected"` — explicitly dismissed (acts as a stop-list)

## Important behavior: one keyword, one list

A keyword can only be in **one** list at any time. Adding to a new list silently removes from any previous list. This is intentional — lists are a coarse classification, not tags.

Implementation:
```sql
-- on add:
DELETE FROM keyword_lists WHERE keyword = %s;
INSERT INTO keyword_lists (keyword, list_name) VALUES (%s, %s);
```

If you want multi-list / tags-style behavior, that's a different table design — but it changes the UI mental model significantly. Don't break this rule without a deliberate decision.

## API

(Already in [`api.md`](../api.md#saved-lists). Recapped here for convenience.)

- `GET /api/lists` — `[{ list_name, count }]`
- `GET /api/lists/<name>` — keywords in the list + their latest metrics (join against `results`)
- `POST /api/lists/add` — `{keywords: [...], list_name: "..."}`
- `POST /api/lists/remove` — `{keywords: [...]}`
- `POST /api/lists/lookup` — `{keywords: [...]}` → `{kw: list_name, ...}`

The lookup endpoint is what lets every results table show "currently in: backlog" badges next to keywords.

## Master List view

A merge of the **latest completed session from each tab** (discovery + content_gap + breakout + targets), deduped by keyword.

### Logic

For each tab:
1. Find the most recent `status = 'completed'` session.
2. Pull all its results.

Merge by keyword. When the same keyword appears in N tabs:
- Take the **highest** `volume`
- Take the **lowest** `position` (best rank)
- Take the **highest** `traffic_potential`
- **Concatenate** the source tabs into a `tabs` field: `"discovery, content_gap"`

Return as a sortable table. Same sort/dir API as `/api/sessions/<id>/results`.

### Why this matters

A keyword that surfaces in *both* Discovery and Content Gap is a strong signal — you're seeing it from multiple lenses. The Master List makes this explicit at a glance via the `tabs` field.

## CSV Export

Two endpoints:

- `GET /api/sessions/<id>/csv` — single session
- `GET /api/master/csv` — the Master List

Standard CSV: comma-separated, double-quote escaping, header row from the result columns.

## Future expansion (deliberately out of v1)

- **List sharing** — make lists exportable to teammates' workspaces.
- **List versioning** — track when keywords were added/removed.
- **Tag-style lists** — multi-list memberships per keyword. Big UX change.
- **Auto-suggest lists** — "you keep moving high-volume keywords to `backlog`; suggest `q3-priorities` based on…" — LLM-territory.

## Edge cases

- Add to a list that doesn't exist yet → just create it implicitly via the INSERT. No CREATE LIST endpoint needed.
- Empty list after a remove — keep the empty list around for a few days then sweep? In v1 just leave empty lists; they're cheap.
- A keyword in a list that isn't in any current session → still returned by `GET /api/lists/<name>`, just with null metrics. The list is the source of truth, not the session.
