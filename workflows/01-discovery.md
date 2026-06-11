# Workflow 1: Discovery

**Goal:** Take a list of seed keywords, expand them into a much larger candidate pool via Ahrefs Keywords Explorer, filter the pool, and save the survivors as a session.

## Inputs

- **Seeds** — combined from:
  - `seeds` table (user's persistent list)
  - `adhoc_seeds` posted in the API call (one-shot additions, not saved to the seeds table)
- **Settings** — `target_site`, `target_country`, `filters`

## Algorithm

### Phase 1 — Expand each seed via Keywords Explorer

For each seed, call **three** different idea types and accumulate keywords:
- `MatchingTermsTermsMatch` — keywords containing all the seed's terms
- `RelatedTerms` — semantically related keywords
- `SearchSuggestions` — autosuggest-derived keywords

For each call:
- `country` = settings.target_country
- `search_engine` = `"Google"`
- `offset = 0, limit = 100`
- Sort by Volume DESC
- No filters passed to Ahrefs (we filter ourselves)

For every result row:
1. Skip if `keyword` is empty.
2. If we've seen this keyword before, just add the current seed to `sources` and continue.
3. Apply the [filter pipeline](../filter-pipeline.md) (full version — we have `attrs` + `categories` from KE).
4. Store:
   ```python
   {
     "keyword": kw,
     "volume": row.volume,
     "traffic_potential": row.trafficPotential,
     "difficulty": row.difficulty,
     "cpc_cents": row.cpc,
     "parent_topic": row.parentTopic,
     "volume_history": parse_msv(row.monthlySearchVolume),
     "trend_3m": row.growthRate.months_3,
     "trend_6m": row.growthRate.months_6,
     "sources": {seed},   # set of seeds that produced this kw
   }
   ```

### Phase 2 — Rank-check survivors

Call Site Explorer's `organic_keywords` against `target_site`, batched in groups of 50 keywords, filtered by `keyword IN (batch)`. Collect `{keyword: {position, url}}`.

### Phase 3 — Apply position filter

For each candidate:
- If currently ranks at `position < min_position` → drop (we already rank well enough)
- If currently ranks at `position >= min_position` → keep, attach position + url
- If not ranking at all → keep, position/url = null

### Phase 4 — Save session

Call `save_session("discovery", filters, survivors, excluded_count, summary={"seeds_used": N})`. See [Session Accumulation](#session-accumulation) below.

## Progress messages (for UX)

The background job should update its `progress` string at each phase boundary:

- `"Seed 3/12: 'link building' (matching)"`
- `"Checking rankings for 487 keywords..."`
- `"Checking rankings: 100/487 keywords..."`
- `"Saving 312 keywords..."`
- `"Done! 312 keywords (45 new, 23 excluded)"`

## Session Accumulation

**Critical behavior.** Discovery doesn't replace prior sessions — it accumulates.

When saving a new session:

1. Find the previous **completed** session for this tab (`'discovery'`).
2. Load all its `results` rows into a `prev_rows` dict keyed by keyword.
3. For each new-run keyword:
   - If it was in `prev_rows` too → mark `is_new = false`, merge metric updates from new run.
   - If it's net-new → mark `is_new = true`.
4. For each keyword in `prev_rows` but NOT in the new run → carry it forward unchanged with `is_new = false`.

Summary written back: `{ "seeds_used": N, "new": new_count, "carried": prev_count, "excluded": excluded_count }`.

## Volume history parsing

Ahrefs returns `monthlySearchVolume` as:
```json
{ "startDate": "2024-06-01", "volume": [110, 120, 90, 105, ...] }
```

Convert to:
```json
[ { "date": "2024-06", "volume": 110 },
  { "date": "2024-07", "volume": 120 }, ... ]
```

Keep only the last 12 entries.

## Edge cases

- Seeds with no expansion results — log and continue, don't fail the whole job.
- Ahrefs rate-limits / 5xx — catch per-batch, log, continue. Partial results are fine.
- Empty seed list (no persistent seeds AND no adhoc_seeds) — return 400.
