# Filter Pipeline

The single keep/drop rule that every workflow applies to every candidate keyword. **Same logic in all six workflows** — if you only get this right, half the app works.

## Inputs

Filter config (stored in `settings.filters`):

```json
{
  "min_volume": 100,
  "max_kd": null,
  "min_position": 41,
  "exclude_branded": true,
  "exclude_local": true,
  "exclude_terms": ["cheap", "free", "near me"],
  "allowed_categories": ["Business & Industrial > SEO"],
  "drop_uncategorized": false
}
```

Per-candidate input (what the Ahrefs APIs return; structure depends on source):

- `keyword` (string)
- `volume` (int, monthly searches)
- `difficulty` (int 0–100, nullable)
- `attrs` (object) — `{branded: bool, local: bool}` — only present on Keywords Explorer results
- `categories` (object) — `{category: string[], nsfw: string[]}` — only present on Keywords Explorer results

Plus context:
- `own_brand` (string) — derived from `target_site` (e.g. `"ahrefs"` from `"ahrefs.com"`)

## Algorithm

Return `True` to keep, `False` to drop.

```
if volume < min_volume:                         drop
if max_kd is not None and difficulty > max_kd:  drop

if exclude_branded and attrs.branded:
    if own_brand and own_brand not in keyword.lower():
        drop                # keep own-brand keywords even when filtering branded

if exclude_local and attrs.local:               drop

if any term in exclude_terms is a substring of keyword.lower():   drop

# NSFW is always dropped — not optional
if "Adult" in categories.nsfw:                  drop
if "Nsfw"  in categories.nsfw:                  drop
if "Adult" in categories.category:              drop

if allowed_categories is non-empty:
    if categories.category is empty:
        if drop_uncategorized: drop
        else: keep            # uncategorized is allowed by default
    else:
        # keep iff at least one of the kw's categories starts with one of allowed_categories
        if not any(kw_cat.startswith(allowed) for kw_cat in categories.category
                                              for allowed in allowed_categories):
            drop

keep
```

## Important behaviors

1. **Own-brand exception** — when `exclude_branded` is on, we still keep keywords that mention our own brand. The check is `own_brand not in keyword.lower()` → drop. The intuition: "remove competitor brand searches, but don't accidentally hide our own brand queries."
2. **`exclude_terms` is substring, not whole-word.** `"near me"` excludes `"plumber near mexico"` — that's intentional, but document it for users.
3. **NSFW is always-on.** It's not exposed as a filter toggle.
4. **Category filter is prefix match, not exact.** `"Business & Industrial"` in `allowed_categories` matches `"Business & Industrial > SEO"`.
5. **`drop_uncategorized` defaults to `false`.** If a keyword has no category info, we keep it unless the user opts in to dropping it.

## Two-pass filtering for Content Gap and Breakout

These workflows pull from Site Explorer first (which doesn't return `attrs` or `categories`), so the filter runs in two passes:

**Pass 1 — text-only filters** (after Site Explorer):
- `min_volume`, `max_kd`, `exclude_terms`

**Pass 2 — enrichment filters** (after Keywords Explorer enrichment):
- `exclude_branded`, `exclude_local`, NSFW, category

This avoids paying for KE enrichment on keywords that text filters will already drop.

## Position filter (separate from the keyword filter)

`min_position` is applied separately, after rank-checking against `target_site`:

- If the keyword has **no** current ranking on target_site → **keep** (it's a true opportunity)
- If the keyword ranks at position `< min_position` (better than threshold) → **drop** (we already rank well enough)
- If the keyword ranks at position `>= min_position` (worse than threshold) → **keep** (room to improve)

Default `min_position = 41` (anything ranking 41+ is fair game).
