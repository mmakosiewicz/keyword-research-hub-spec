# Workflow 5: Tier + Cluster Lookup

**Goal:** Enrich every keyword with semantic context: its topical-distance **tier**, nearest topic **cluster**, and optional 0–3 **business potential** score.

## Where the data comes from

The Hub reads stable, precomputed classifications. It does not recompute embeddings during normal page loads.

A user can launch the in-app generator described in [Workflow 7](./07-tier-generator.md). The generator defines a core set, embeds keywords, clusters the core, calculates distance tiers, optionally scores BP, and writes the enrichment data. Normal workflow views then perform fast lookups.

The app must still run before generation: unknown keywords return null tier/cluster/BP fields.

## Input/output shape

```json
{
  "results": [
    {
      "keyword": "seo",
      "list": "backlog",
      "volume": "415000",
      "kd": "89",
      "position": "23",
      "url": "https://example.com/blog/what-is-seo/",
      "traffic_potential": "288",
      "tabs": "Tracker, Gap, Discovery",
      "nearest_cluster": 6,
      "nearest_cluster_name": "SEO Strategy and Performance",
      "distance": 0.2012,
      "avg_distance": 0.5897,
      "is_in_core": true,
      "tier": 1,
      "bp": 1
    }
  ],
  "clusters": [
    {"id": 6, "name": "SEO Strategy and Performance", "size": 142}
  ],
  "tier_labels": {
    "1": "Core orbit",
    "2": "Adjacent",
    "3": "Far orbit",
    "4": "Outside"
  }
}
```

A synthetic schema sample is provided at [`sample-data/keyword_tiers.sample.json`](../sample-data/keyword_tiers.sample.json). It is enough to test parsing/UI, not to classify a real site.

## Lookup

Load into a dictionary keyed by normalized lowercase keyword:

```python
tier_cache = {}
for row in data["results"]:
    tier_cache[row["keyword"].strip().lower()] = {
        "tier": row.get("tier"),
        "tier_label": tier_labels.get(str(row.get("tier")), ""),
        "cluster_id": row.get("nearest_cluster"),
        "cluster": row.get("nearest_cluster_name", ""),
        "distance": row.get("distance"),
        "is_in_core": bool(row.get("is_in_core")),
        "bp": row.get("bp"),
        "list": row.get("list"),
    }
```

Hot-reload after a completed generation run (or when the file mtime changes in file-backed implementations). Do not require a process restart.

## Usage

Every workflow result may include `tier`, `tier_label`, `cluster`, `distance`, and `bp`. The Master List provides:

- tier/BP/cluster filters;
- a sortable table;
- an integrated **Table / Topic map** view toggle;
- a keyword details panel when a map point is selected.

The Topic map is part of the Hub, not a separate sibling application. See [Workflow 7](./07-tier-generator.md) for visualization behavior.

## Edge cases

- Keyword absent from generated data → return null enrichment fields; never error.
- No generation exists → the app runs and shows a clear setup monitor.
- Generated data is corrupt → log loudly, retain the previous valid cache, and do not crash.
- Latest research session is newer than the enrichment run → show a staleness notice and a regenerate action.
- Sample data → clearly label synthetic; never present it as a real classification.
