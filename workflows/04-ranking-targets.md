# Workflow 4: Ranking Targets

**Goal:** A tagged watchlist of keywords that you want to track live rank for, scoped to specific tags (priorities, themes, projects, people). This workflow integrates with **Ahrefs Rank Tracker** rather than Site Explorer / Keywords Explorer.

## Inputs

- A configured Rank Tracker `project_id` in Ahrefs.
- A set of `tags` defined inside the Rank Tracker project (e.g. "High priority", "Project X", "Joshua Hardwick", "Q3 launch").
- User-selected subset of tags for this run.

## Configuration endpoint

`GET /api/targets/config` returns:
```json
{
  "project_id": "2286963",
  "default_tags": ["Mateusz", "Joshua Hardwick", "Chris Haines", "Tim Soulo", "Freelancers"]
}
```

The default tags are stored as a constant in the app (in the reference implementation, in code). Could be promoted to settings later.

## Algorithm

### Phase 1 — Pull rank data per tag

For each selected tag, call the Ahrefs Rank Tracker API at:

```
GET https://api.ahrefs.com/v3/rank-tracker/keyword-rankings
  ?project_id=<project_id>
  &tag=<tag>
  &country=<country>
  &select=keyword,position,best_position,url,volume,traffic,difficulty,...
```

(See Ahrefs Rank Tracker v3 docs for exact field selection; this varies by plan.)

Collect rank data per keyword. Same keyword across multiple tags → keep the rank from the first tag pull, but tag-merge the `tags` field: `tags = ["Mateusz", "High priority"]`.

### Phase 2 — Build results

```python
{
  "keyword": ...,
  "volume": ...,
  "difficulty": ...,
  "position": ...,           # current rank
  "ranking_url": ...,
  "extra": {
    "tags": ["Mateusz", "Joshua Hardwick"],
    "best_position": ...,
    "traffic": ...,
  }
}
```

### Phase 3 — Save session

```
save_session("targets", filters={}, results, excluded=0,
  summary={"tags_used": selected_tags})
```

The standard filter pipeline does **not** apply here — these are explicit user picks, not algorithmic candidates.

## Why this is different from the other three workflows

- It reads from Rank Tracker (an Ahrefs product the user has manually populated), not from Site Explorer / Keywords Explorer.
- Filters don't apply — every keyword in the watchlist is intentional.
- The interesting time-axis is **rank movement**, not new discoveries.

## Future expansion

Two natural follow-ups left out of v1:

1. **Rank history per keyword** — pull a date-series, store as `extra.rank_history`, render a sparkline.
2. **Movement alerts** — when a tracked keyword drops > N positions week-over-week, surface it in a dashboard widget.

## Edge cases

- Tag exists in Ahrefs but has 0 keywords assigned — skip silently.
- Rank Tracker API returns 401 — surface to UI as a credentials problem.
- Selected tags is empty — return 400 (don't pull the whole project).
