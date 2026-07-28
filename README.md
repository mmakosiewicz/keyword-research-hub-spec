# Keyword Research Hub — Spec

A logic and data-shape spec for the **Keyword Research Hub**, a single workspace for SEO keyword research. Any agentic AI (Cursor, Claude Code, Replit, etc.) can use this to recreate the app.

**This spec is intentionally not prescriptive about stack.** Pick whatever web framework, database, and frontend you want — as long as the workflows, filter logic, and data shapes match what's described here, the recreation is faithful.

## What the app does

Six interlocking workflows that share one filter pipeline and one keyword bank:

1. **[Discovery](./workflows/01-discovery.md)** — seed → expand via Ahrefs Keywords Explorer → filter → save
2. **[Content Gap](./workflows/02-content-gap.md)** — competitor domains → pull their organic keywords → filter where you don't already rank
3. **[Breakout](./workflows/03-breakout.md)** — your domain's blog keywords ranking 31–100, cross-checked for cannibalization
4. **[Ranking Targets](./workflows/04-ranking-targets.md)** — tagged keyword watchlist with live rank tracking
5. **[Tier + Cluster Lookup](./workflows/05-tier-cluster.md)** — semantic enrichment from a precomputed JSON
6. **[Lists & Sessions](./workflows/06-lists-sessions.md)** — every run is a named session; keywords can be tagged to lists; CSV export
7. **[Tier + Cluster Generator](./workflows/07-tier-generator.md)** — define a core, embed, cluster, tier, score BP, and explore the integrated Topic map

## What ties them together

- **One shared [filter pipeline](./filter-pipeline.md)** — every workflow applies the same volume / KD / branded / local / NSFW / category / exclude-terms checks.
- **One shared keyword bank** — accumulated across sessions per workflow tab. Re-running a workflow doesn't wipe prior results; it merges.
- **One shared settings object** — target domain, country, brand terms, competitor list, filter defaults.
- **One Master List view** — merge of the latest session from each workflow, deduped by keyword.

## Required reading order for a recreating agent

1. This README (you're here)
2. [`data-model.md`](./data-model.md) — the 7 tables you need
3. [`filter-pipeline.md`](./filter-pipeline.md) — the keep/drop rules every workflow shares
4. [`api.md`](./api.md) — the HTTP surface
5. [`workflows/*.md`](./workflows/) — read in order; each builds on the previous
6. [`reference-app/`](./reference-app/) — sanitized runnable implementation with PostgreSQL, generator, Table/Topic map, and public-host security controls

## External dependencies

- **Ahrefs API** — Keywords Explorer (`ke_ideas`) and Site Explorer (`organic_keywords`). The app cannot function without it. User provides credentials.
- **An OpenAI-compatible model endpoint** — used by the in-app tier generator for embeddings, cluster labels, and optional business-potential scoring. Configure endpoint/key through environment variables only.

`keyword_tiers.json` is optional at first launch: the Hub runs with empty tier/cluster fields. Generate a real file from your own core + keyword bank with [Workflow 7](./workflows/07-tier-generator.md). Do not reuse another site's tier file. A schema sample remains at [`sample-data/keyword_tiers.sample.json`](./sample-data/keyword_tiers.sample.json).

## Secrets, IDs, and credentials

**Anything that identifies a specific Ahrefs project, customer, team member, or environment is a secret.** Do not hardcode it in source. Sources of truth, in order of preference:

1. Environment variables (`AHREFS_API_KEY`, `RANK_TRACKER_PROJECT_ID`, etc.)
2. The `settings` table for user-configurable values (target domain, country, default tags, competitor list)
3. A `.env.example` checked into the repo with placeholder values

If you see a project ID, API key, real domain, or person's name in the spec or sample data, treat it as a placeholder — substitute your own.

## Definition of done

A user can:
1. Configure settings (target domain, country, competitors, brand terms, filter defaults).
2. Seed a Discovery run, watch progress, save results to a named session.
3. Run a Content Gap pull from competitor domains; results filtered to only what they don't already rank for.
4. Run a Breakout pull to find their own blog's almost-ranking keywords, flagged for cannibalization vs. true opportunities.
5. Pull rank data from a Ranking Targets list (Ahrefs Rank Tracker).
6. Move keywords into named saved lists; later look up which list any keyword belongs to.
7. CSV-export any session or list.
8. View a "Master List" merging the latest completed session from each workflow.
9. Generate tiers/clusters from a user-defined core and explore the same filtered Master List as a table or Topic map.

No part of this requires leaving the app.

## Runnable reference implementation

A sanitized standalone application is available at [`reference-app/`](./reference-app/). It contains no private keyword bank, credentials, domains, project IDs, member data, or generated tier file. Read its [`SECURITY.md`](./reference-app/SECURITY.md) and [`PRIVACY.md`](./reference-app/PRIVACY.md) before exposing it outside localhost.
