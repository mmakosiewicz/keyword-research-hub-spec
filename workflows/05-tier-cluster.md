# Workflow 5: Tier + Cluster Lookup

**Goal:** Enrich every keyword in every workflow with two pieces of semantic context — its **tier** (priority bucket) and its **nearest cluster** (topic group) — looked up from a precomputed JSON file.

## Why this exists

Tiers and clusters are produced offline by a separate clustering process (embedding the keyword universe, k-means or HDBSCAN clusters, tier-assignment from a heuristic or LLM judge). The Hub doesn't recompute these on the fly — it just reads the result and joins it onto live keyword data.

This makes the app:
- **Fast** — pure dict lookup, in-memory.
- **Decoupled** — clustering can be improved without touching the app.
- **Honest** — tiers are static labels; they don't drift as you re-rank candidates.

## Input file: `keyword_tiers.json`

A single JSON file with this shape:

```json
{
  "results": [
    {
      "keyword": "seo",
      "list": "backlog",
      "volume": "415000",
      "kd": "89",
      "position": "23",
      "url": "https://ahrefs.com/blog/what-is-seo/",
      "traffic_potential": "288",
      "tabs": "Tracker, Gap, Discovery",
      "nearest_cluster": 6,
      "nearest_cluster_name": "SEO Strategy and Performance",
      "distance": 0.2012,
      "avg_distance": 0.5897,
      "is_in_core": true,
      "tier": 1,
      "bp": 1
    },
    ...
  ],
  "clusters": [
    { "id": 6, "name": "SEO Strategy and Performance", "size": 142 },
    ...
  ],
  "tier_labels": {
    "1": "Core priority",
    "2": "High value",
    "3": "Long tail",
    "4": "Speculative"
  }
}
```

A real sample is provided at [`sample-data/keyword_tiers.sample.json`](../sample-data/keyword_tiers.sample.json).

## Loader

On first use, load the file into an in-memory dict keyed by lowercased keyword:

```python
tier_cache = {}
for row in data["results"]:
    tier_cache[row["keyword"].lower()] = {
        "tier": row["tier"],
        "tier_label": tier_labels.get(str(row["tier"]), ""),
        "cluster": row.get("nearest_cluster_name", ""),
        "bp": row.get("bp"),
        "list": row.get("list"),
    }
```

Hot-reload when the file's mtime changes. Don't ship a process restart for a tier rebuild.

## Usage in other workflows

Every workflow's result row should optionally include `tier`, `tier_label`, `cluster` fields populated from this lookup. UI can color-code by tier, group by cluster, etc.

## Future: separate "Universe" view

A standalone visualization (a "galaxy" / 2-D scatter / cluster browser) of the tier/cluster file. Not strictly part of the Hub — but a natural sibling app reading the same file. In the reference implementation, this is split into a separate app (`keyword_universe`).

## Edge cases

- Keyword not in tier file → return null for all four fields. Don't error.
- File missing → app must still run; tier columns are just empty.
- File corrupt → log loudly, keep the previous cache, don't crash.
