# Workflow 2: Content Gap

**Goal:** Pull the top organic keywords each competitor ranks for, surface the ones you *don't* already rank well for.

## Inputs

- `competitors` — list of competitor domains. From the API body or from settings.
- `kw_per_competitor` — top-N per competitor (default 500).
- Settings — `target_site`, `target_country`, `filters`.

## Algorithm

### Phase 1 — Pull each competitor's top keywords

For each competitor domain, call Site Explorer:

```
client.site_explorer_organic_keywords(
  target=competitor,
  country=settings.target_country,
  mode="subdomains",
  limit=kw_per_competitor,
  filter=se_filter("volume", ">=", filters.min_volume or 100),
)
```

For each returned keyword, accumulate into a dict keyed by keyword:

```python
all_gap_kws[keyword] = {
  "keyword": ...,
  "volume": ...,
  "difficulty": ...,
  "cpc_cents": ...,
  "competitors": [
    { "domain": "semrush.com", "position": 4, "url": "...", "traffic": 1200 },
    ...
  ],
}
```

When the same keyword appears for multiple competitors, append to the `competitors` list and **keep the highest `volume`** across sources (different competitors may have slightly different volume estimates).

### Phase 2 — Rank-check against your domain

Batch-query Site Explorer with `filter=se_filter("keyword", "in", batch)` against `target_site` (`mode="subdomains"`). Collect `{keyword: {position, url}}` for each match.

### Phase 3 — Text-only filter pass

Apply (no enrichment yet — we don't have `attrs` or `categories` from Site Explorer):
- Position filter (drop if rank < min_position; keep if rank >= or unranked)
- `min_volume`, `max_kd`
- `exclude_terms`

### Phase 4 — Enrich survivors with Keywords Explorer

Batches of **10 keywords** per call (KE matches better with small batches):

```
ke_ideas(
  seed=["Keywords", batch],
  country=target_country,
  search_engine="Google",
  ideas_type=["MatchingTermsPhraseMatch"],
  offset=0, limit=batch_size * 2,
  sort={"by": ["Volume"], "order": ["Desc"]},
)
```

Match returned rows back to the batch by exact keyword string. Collect `attrs`, `categories`, `growthRate`, `monthlySearchVolume`, `trafficPotential`.

### Phase 5 — Enrichment filter pass + final build

Now apply the rest of the [filter pipeline](../filter-pipeline.md): branded, local, NSFW, category.

Build the final result rows. Each row has the standard fields + the `competitors` jsonb array intact.

### Phase 6 — Save session

`save_session("content_gap", filters, survivors, excluded_count, summary={"competitors": [...]})`.

Carries over from previous session same way as Discovery.

## Progress messages

- `"Fetching keywords from semrush.com (1/4)..."`
- `"Collected 1840 unique keywords from 4 competitors"`
- `"Cross-checking: 200/1840 keywords..."`
- `"Applying filters..."`
- `"Enriching 980 keywords with trends & categories..."`
- `"Enriching: 100/980 keywords..."`
- `"Enriched 873/980 keywords. Final filtering..."`
- `"Saving 612 keywords..."`
- `"Done! 612 keywords (147 new, 89 excluded)"`

## Why two-pass filtering matters

Site Explorer returns ~1500 keywords/competitor. Keywords Explorer is much slower and rate-limited. Filtering on the cheap stuff (volume, KD, exclude_terms) first cuts the KE call volume by 30–60% on typical runs.

## Edge cases

- A competitor returns 0 results — log, continue.
- Volume mismatch between competitors for the same keyword — take the max (most-favorable-to-us is fine here; the user only cares whether the kw is worth chasing).
- KE enrichment fails for a batch — drop those keywords through Pass 5's category/branded filters? **No.** If `ke_data[kw]` is missing, treat enrichment as empty `attrs={}, categories={}` and let it pass enrichment filters. The text-pass already screened them.
