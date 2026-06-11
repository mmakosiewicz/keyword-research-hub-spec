# Workflow 3: Breakout

**Goal:** Find your blog's "almost-there" keywords — ranking 31–100 — and decide for each whether the rest of your domain already covers it (cannibalization) or not (true breakout opportunity).

## Why this workflow exists

A blog post that ranks position 35 is one optimization sprint away from page 1. But before you optimize, check whether your `/product/` or `/learn/` page on the same domain is already ranking better — in which case the blog post is redundant and should probably be merged.

## Inputs

- Settings — `target_site`, `target_country`, `filters`
- Blog path is derived: `f"{target_site}/blog/"`

## Algorithm

### Phase 1 — Pull blog keywords ranking 31–100

Paginated call against Site Explorer with `mode="prefix"` (because we're targeting a path, not a domain):

```
client.site_explorer_organic_keywords(
  target=f"{target_site}/blog/",
  country=target_country,
  mode="prefix",
  limit=500, offset=offset,
  filter=se_and(
    se_filter("position", ">=", 31),
    se_filter("position", "<=", 100),
    se_filter("volume", ">=", filters.min_volume or 100),
  ),
)
```

Paginate by `offset += 500` until you get a partial batch back, OR you hit `offset >= 5000` (safety cap — Ahrefs gets noisy past that).

For each result:
```python
{
  "keyword": kw.keyword,
  "volume": kw.volume,
  "difficulty": kw.difficulty,
  "cpc_cents": kw.cpc_cents,
  "position": kw.position,       # the blog page's position
  "ranking_url": kw.url,         # the blog URL
}
```

### Phase 2 — Cross-check the full domain

For each blog keyword, find if **any non-blog URL on the same domain** ranks for it. Batched in groups of 50:

```
ranked = client.site_explorer_organic_keywords(
  target=target_site,
  country=target_country,
  mode="subdomains",
  limit=len(batch) * 2,
  filter=se_filter("keyword", "in", batch),
)
```

Walk results, keep only `r.url` where `"/blog/" not in r.url`. If multiple non-blog URLs match the same keyword, keep the one with the lowest (best) position:

```python
domain_rankings[r.keyword] = { "position": r.position, "url": r.url }
```

### Phase 3 — Enrich via Keywords Explorer

Same KE batch pattern as Content Gap (batches of 10, `MatchingTermsPhraseMatch`). Collect `growthRate`, `monthlySearchVolume`, `parentTopic`, `trafficPotential`, `attrs`, `categories`.

### Phase 4 — Filter + classify

Apply the [filter pipeline](../filter-pipeline.md): exclude_terms first (text-only), then branded/local/NSFW/category from the enrichment.

For survivors, classify each:

- **`status = "breakout"`** — no non-blog page ranks for this. Pure opportunity to optimize the blog post.
- **`status = "cannibalization"`** — a non-blog page also ranks. Decide: kill the blog post, redirect, or restructure.

Build the result row:

```python
{
  "keyword": ...,
  "volume": data.volume,
  "traffic_potential": ke_row.trafficPotential or 0,
  "difficulty": data.difficulty,
  "cpc_cents": ke_row.cpc or data.cpc_cents,
  "position": data.position,           # blog position
  "ranking_url": data.ranking_url,     # blog URL
  "parent_topic": ke_row.parentTopic,
  "trend_3m": ke_row.growthRate.months_3,
  "trend_6m": ke_row.growthRate.months_6,
  "volume_history": parse_msv(ke_row.monthlySearchVolume),
  "extra": {
    "status": "breakout" | "cannibalization",
    "other_url": domain_match.url if domain_match else None,
    "other_position": domain_match.position if domain_match else None,
  },
}
```

### Phase 5 — Save session

```
save_session("breakout", filters, survivors, excluded,
  summary={"breakout_count": N, "cannibalization_count": M})
```

## Progress messages

- `"Fetching blog keywords ranking 31-100..."`
- `"Fetching blog keywords: 1500 loaded..."`
- `"Found 1500 blog keywords in pos 31-100"`
- `"Cross-checking domain rankings for 1500 keywords..."`
- `"Cross-checking: 200/1500 keywords..."`
- `"<N> keywords have non-blog pages. Enriching..."`
- `"Enriching: 100/1500 keywords..."`
- `"Applying filters..."`
- `"Saving 1100 results..."`
- `"Done! 720 breakout + 380 cannibalization (23 filtered)"`

## Edge cases

- The site has no `/blog/` path → Phase 1 returns 0; that's a real signal, save an empty session.
- Cap at 5000 keywords pulled in Phase 1 — past that you're looking at long tail noise.
- The "best non-blog URL" rule can be tweaked per user preference (some want any match, some want a specific path prefix). Make it configurable later if needed.
