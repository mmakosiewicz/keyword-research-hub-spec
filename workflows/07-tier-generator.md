# Workflow 7: Tier + Cluster Generator

**Goal:** Make the Tier + Cluster enrichment reproducible inside the Hub instead of requiring an unexplained precomputed file.

## Product placement

Tier and BP generation is part of completing the **Master List**, not a separate destination. **Manage tiers** remains an advanced configuration surface for choosing the core, model, and thresholds.

Whenever Master List contains keywords with no Tier or BP, show an enrichment prompt with exact missing counts and two actions:

- **Quick update** — classify only missing rows against the saved centroids/labels from the last full rebuild. Preserve all existing classifications.
- **Full rebuild** — re-embed the complete bank, refit clusters, regenerate tiers, and rescore BP.

Quick update is unavailable until one full rebuild has saved a reusable semantic model. If the normalized core set changes, Quick update must be blocked and a Full rebuild required. Never silently classify against stale core centroids.

After enrichment, Master List shows a compact “fully enriched” state with an optional Rebuild all action.

The result is consumed in the Master List, which has two views:

- **Table** — metrics, classifications, filters, and triage.
- **Topic map** — integrated visualization of the exact same filtered keyword rows.

## Why a core is required

The core set defines the site's topical center. The generator clusters core keywords, then measures every candidate's semantic distance to the nearest core cluster. With no core there is nothing to measure from.

UI requirements:

- Show a blocking monitor when fewer than four core keywords exist.
- Explain that 30+ representative terms is preferable.
- Do not silently treat every candidate as core.
- Permit core keywords from a CSV `core` column. Provider-specific adapters (e.g. Rank Tracker) may be added separately.

## Pipeline

1. Validate and deduplicate candidate/core keywords.
2. Embed all keywords with the configured model endpoint.
3. Cluster core embeddings using k-means (auto-select `k` or use an explicit value).
4. Name each cluster from its representative core keywords.
5. Calculate cosine distance from every keyword to its nearest core centroid.
6. Assign fixed tiers by default; allow percentile mode with a warning that it is relative.
7. Optionally score business potential 0–3 against a product description.
8. Persist classifications to PostgreSQL and hot-refresh Table/Topic map views.

## Tier modes

### Fixed (default)

Text-embedding-3-small reference cuts:

- Tier 1: distance < 0.45
- Tier 2: 0.45–0.60
- Tier 3: 0.60–0.70
- Tier 4: ≥ 0.70

Calibrate if using another embedding model.

### Percentile

- Tier 1: below p75
- Tier 2: p75–p90
- Tier 3: p90–p95
- Tier 4: p95+

This guarantees a distribution even when the bank is clean or narrow, so it can assign flattering tiers to unrelated outliers. Label it clearly as relative.

## Business-potential judge

Supported reference choices:

- Claude Opus 5 — default
- Claude Sonnet 5
- Claude Haiku 4.5
- ChatGPT Sol
- ChatGPT Luna

The application must read provider endpoint/key only from environment variables. Record the selected model in run metadata. Model prices change; show no hardcoded price guarantee.

## Topic map

The Topic map is part of Master List, not a separate application.

- Same filtered rows as Table view.
- Color = tier.
- Radius = semantic distance.
- Angle = cluster.
- Point size = search volume.
- Hover = keyword, tier, volume, KD.
- Click = keyword details panel with metrics, tier, BP, cluster, distance, source, list, and URL.
- Deterministic placement across reloads.

The reference polar projection is exploratory UI, not a scientific dimensionality-reduction chart.

## Background execution

Embedding/scoring may exceed HTTP timeouts. Run in a detached process or proper job runner and persist `queued/running/completed/failed`, step, progress, errors, and summary. The UI resumes polling after reload.

## Privacy and security

Keyword banks may contain personal/customer names, unreleased products, strategic targets, and private URLs. The public reference must never include a live bank or generated output.

- Require auth and CSRF.
- Enforce upload caps, UTF-8 CSV, length/row limits.
- Keep PostgreSQL private.
- Do not fetch ranking URLs from the server.
- Reject model redirects and use HTTPS.
- Explain that keywords/product descriptions are sent to model providers.
- Never commit credentials, imports, outputs, DB dumps, or logs.
