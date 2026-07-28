# letaido-store-app: keyword-research-hub — do not remove this line; the App Store uses it to identify the installed app
"""Keyword Research Hub — automates 4-step keyword research workflow."""

import json
import os
import threading
import uuid
import traceback
from datetime import datetime, date

import requests as http_requests
from flask import Blueprint, render_template, request, jsonify, Response

import psycopg2
import psycopg2.extras

from applications._letaido_keyword_research_hub_setup import ensure_schema
ensure_schema()

NAME = "Keyword Research Hub"

blueprint = Blueprint(
    "letaido_keyword_research_hub",
    __name__,
    template_folder="../templates/keyword_research",
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "keyword_research")
TIERS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "keyword_tiers.json")

# Cached tier/cluster lookup (hot-reloads when the file's mtime changes)
_tier_cache = None
_tier_cache_mtime = None

def _load_tier_cache():
    global _tier_cache, _tier_cache_mtime
    import os
    if not os.path.exists(TIERS_FILE):
        _tier_cache = {}
        _tier_cache_mtime = None
        return _tier_cache
    mtime = os.path.getmtime(TIERS_FILE)
    if _tier_cache is not None and _tier_cache_mtime == mtime:
        return _tier_cache
    _tier_cache_mtime = mtime
    with open(TIERS_FILE) as f:
        data = json.load(f)
    tier_labels = data.get("tier_labels", {})
    _tier_cache = {}
    for r in data.get("results", []):
        kw = r.get("keyword", "").strip().lower()
        tier_num = r.get("tier")
        _tier_cache[kw] = {
            "tier": tier_num,
            "tier_label": tier_labels.get(str(tier_num), ""),
            "cluster": r.get("nearest_cluster_name", ""),
            "cluster_id": r.get("nearest_cluster"),
            "distance": r.get("distance"),
            "is_in_core": bool(r.get("is_in_core")),
            "bp": r.get("bp"),
        }
    return _tier_cache


def get_db():
    return psycopg2.connect(host="/var/run/postgresql", user="console", database="console_db")


# In-memory job tracker
_jobs = {}


# ─── Shared helpers ──────────────────────────────────────────────────

def _load_settings():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT key, value FROM kr_settings")
    rows = cur.fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def _apply_keyword_filters(kw, row_or_data, filters, own_brand):
    """Return True if keyword should be KEPT, False if filtered out.
    row_or_data: either a raw KE API row (camelCase) or a dict we built.
    For SE-sourced keywords, pass attrs/categories as a pre-built dict.
    """
    min_volume = filters.get("min_volume", 100)
    max_kd = filters.get("max_kd")
    exclude_branded = filters.get("exclude_branded", True)
    exclude_local = filters.get("exclude_local", True)
    exclude_terms = [t.lower() for t in filters.get("exclude_terms", [])]
    allowed_categories = filters.get("allowed_categories", [])
    category_filter_enabled = bool(allowed_categories)
    drop_uncategorized = filters.get("drop_uncategorized", False)

    vol = row_or_data.get("volume") or 0
    kd = row_or_data.get("difficulty")
    attrs = row_or_data.get("attrs") or {}
    cat_info = row_or_data.get("categories") or {}

    if vol < (min_volume or 0):
        return False
    if max_kd is not None and kd is not None and kd > max_kd:
        return False

    # Branded
    if exclude_branded:
        is_branded = attrs.get("branded", False)
        if is_branded and own_brand and own_brand not in kw.lower():
            return False

    # Local intent
    if exclude_local and attrs.get("local", False):
        return False

    # Exclude terms
    if exclude_terms:
        kw_lower = kw.lower()
        if any(t in kw_lower for t in exclude_terms):
            return False

    # NSFW filter — always drop Adult content
    nsfw = cat_info.get("nsfw", [])
    if "Adult" in nsfw or "Nsfw" in nsfw:
        return False
    kw_cats = cat_info.get("category", [])
    if "Adult" in kw_cats:
        return False

    # Category filter
    if category_filter_enabled:
        if not kw_cats:
            if drop_uncategorized:
                return False
        else:
            matched = any(
                kw_cat.startswith(allowed)
                for kw_cat in kw_cats
                for allowed in allowed_categories
            )
            if not matched:
                return False

    return True


def _check_rankings(client_se, kw_list, target_site, country, min_position, job_id):
    """Batch-check rankings on target_site. Returns {keyword: {position, url}} for ranked kws,
    and filters to keep only pos >= min_position or unranked."""
    from ahrefs_internal import se_filter

    rankings = {}
    batch_size = 50
    for i in range(0, len(kw_list), batch_size):
        batch = kw_list[i : i + batch_size]
        _jobs[job_id]["progress"] = f"Checking rankings: {i}/{len(kw_list)} keywords..."
        try:
            ranked = client_se.site_explorer_organic_keywords(
                target=target_site,
                country=country,
                mode="subdomains",
                limit=len(batch),
                filter=se_filter("keyword", "in", batch),
            )
            for r in ranked:
                rankings[r.keyword] = {"position": r.position, "url": r.url}
        except Exception as e:
            print(f"Error checking rankings batch {i}: {e}")

    return rankings


def _save_session(tab, filters, all_keywords, excluded_count, extra_summary=None):
    """Save a session + results to DB with accumulation.

    Keywords from the previous session are carried forward into the new session.
    New-run keywords update existing ones (fresher metrics); keywords not in
    this run are preserved from the previous session with is_new=False.
    Returns (session_id, total_count, new_count).
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "INSERT INTO kr_sessions (tab, filters, status, started_at) VALUES (%s, %s, 'running', NOW()) RETURNING id",
        (tab, json.dumps(filters)),
    )
    session_id = cur.fetchone()["id"]

    # Load previous session's keywords (the accumulated bank)
    cur.execute(
        "SELECT id FROM kr_sessions WHERE tab = %s AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
        (tab,),
    )
    prev = cur.fetchone()
    prev_rows = {}  # keyword -> full row dict
    if prev:
        cur.execute("SELECT * FROM kr_results WHERE session_id = %s", (prev["id"],))
        for r in cur.fetchall():
            prev_rows[r["keyword"]] = dict(r)

    prev_keywords = set(prev_rows.keys())
    new_run_keywords = set(all_keywords.keys())

    def _insert_result(kw_data, is_new):
        sources = kw_data.get("sources", set())
        source_str = ", ".join(sorted(sources)) if isinstance(sources, set) else str(sources) if sources else ""
        cur.execute(
            """INSERT INTO kr_results (session_id, keyword, volume, traffic_potential, difficulty,
                cpc_cents, parent_topic, parent_topic_kd, position, ranking_url, source,
                volume_history, trend_3m, trend_6m, competitors, is_new, extra)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (session_id, keyword) DO NOTHING""",
            (
                session_id,
                kw_data["keyword"],
                kw_data.get("volume"),
                kw_data.get("traffic_potential"),
                kw_data.get("difficulty"),
                kw_data.get("cpc_cents"),
                kw_data.get("parent_topic"),
                kw_data.get("parent_topic_kd"),
                kw_data.get("position"),
                kw_data.get("ranking_url"),
                source_str,
                json.dumps(kw_data["volume_history"]) if kw_data.get("volume_history") else None,
                kw_data.get("trend_3m"),
                kw_data.get("trend_6m"),
                json.dumps(kw_data.get("competitors")) if kw_data.get("competitors") else None,
                is_new,
                json.dumps(kw_data.get("extra")) if kw_data.get("extra") else None,
            ),
        )

    # 1. Insert all new-run keywords (fresh metrics)
    for kw_data in all_keywords.values():
        is_new = kw_data["keyword"] not in prev_keywords
        _insert_result(kw_data, is_new)

    # 2. Carry forward keywords from previous session that weren't in this run
    carried = 0
    for kw, prev_row in prev_rows.items():
        if kw not in new_run_keywords:
            # Carry forward with is_new=False and preserved data
            carried_data = {
                "keyword": kw,
                "volume": prev_row.get("volume"),
                "traffic_potential": prev_row.get("traffic_potential"),
                "difficulty": prev_row.get("difficulty"),
                "cpc_cents": prev_row.get("cpc_cents"),
                "parent_topic": prev_row.get("parent_topic"),
                "parent_topic_kd": prev_row.get("parent_topic_kd"),
                "position": prev_row.get("position"),
                "ranking_url": prev_row.get("ranking_url"),
                "sources": prev_row.get("source") or "",
                "volume_history": prev_row.get("volume_history"),
                "trend_3m": prev_row.get("trend_3m"),
                "trend_6m": prev_row.get("trend_6m"),
                "competitors": prev_row.get("competitors"),
                "extra": prev_row.get("extra"),
            }
            # volume_history and extra are already JSON from DB
            sources = carried_data.pop("sources", "")
            cur.execute(
                """INSERT INTO kr_results (session_id, keyword, volume, traffic_potential, difficulty,
                    cpc_cents, parent_topic, parent_topic_kd, position, ranking_url, source,
                    volume_history, trend_3m, trend_6m, competitors, is_new, extra)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (session_id, keyword) DO NOTHING""",
                (
                    session_id, kw,
                    carried_data["volume"], carried_data["traffic_potential"],
                    carried_data["difficulty"], carried_data["cpc_cents"],
                    carried_data["parent_topic"], carried_data["parent_topic_kd"],
                    carried_data["position"], carried_data["ranking_url"],
                    sources if isinstance(sources, str) else "",
                    json.dumps(carried_data["volume_history"]) if carried_data.get("volume_history") and not isinstance(carried_data["volume_history"], str) else carried_data.get("volume_history"),
                    carried_data["trend_3m"], carried_data["trend_6m"],
                    json.dumps(carried_data["competitors"]) if carried_data.get("competitors") and not isinstance(carried_data["competitors"], str) else carried_data.get("competitors"),
                    False,
                    json.dumps(carried_data["extra"]) if carried_data.get("extra") and not isinstance(carried_data["extra"], str) else carried_data.get("extra"),
                ),
            )
            carried += 1

    total = len(all_keywords) + carried
    new_count = sum(1 for d in all_keywords.values() if d["keyword"] not in prev_keywords)
    summary = {
        "total_keywords": total,
        "new_keywords": new_count,
        "excluded_count": excluded_count,
        "carried_forward": carried,
        "refreshed": len(new_run_keywords & prev_keywords),
    }
    if extra_summary:
        summary.update(extra_summary)

    cur.execute(
        "UPDATE kr_sessions SET status='completed', completed_at=NOW(), summary=%s WHERE id=%s",
        (json.dumps(summary), session_id),
    )
    conn.commit()
    conn.close()
    return session_id, total, new_count


# ─── Pages ───────────────────────────────────────────────────────────

@blueprint.route("/")
def index():
    return render_template("keyword_research/index.html")


# ─── Settings API ────────────────────────────────────────────────────

@blueprint.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(_load_settings())


@blueprint.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    for key, value in data.items():
        cur.execute(
            "INSERT INTO kr_settings (key, value, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (key, json.dumps(value)),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ─── Seeds API ───────────────────────────────────────────────────────

@blueprint.route("/api/seeds", methods=["GET"])
def get_seeds():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, keyword FROM kr_seeds ORDER BY keyword")
    seeds = cur.fetchall()
    conn.close()
    return jsonify(seeds)


@blueprint.route("/api/seeds", methods=["POST"])
def add_seeds():
    keywords = request.json.get("keywords", [])
    conn = get_db()
    cur = conn.cursor()
    added = 0
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        cur.execute("INSERT INTO kr_seeds (keyword) VALUES (%s) ON CONFLICT DO NOTHING", (kw,))
        if cur.rowcount > 0:
            added += 1
    conn.commit()
    conn.close()
    return jsonify({"added": added})


@blueprint.route("/api/seeds/<int:seed_id>", methods=["DELETE"])
def delete_seed(seed_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM kr_seeds WHERE id = %s", (seed_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ─── Sessions API ────────────────────────────────────────────────────

@blueprint.route("/api/sessions", methods=["GET"])
def get_sessions():
    tab = request.args.get("tab", "discovery")
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, tab, filters, status, started_at, completed_at, summary FROM kr_sessions WHERE tab = %s ORDER BY started_at DESC LIMIT 20",
        (tab,),
    )
    sessions = cur.fetchall()
    conn.close()
    for s in sessions:
        s["started_at"] = s["started_at"].isoformat() if s["started_at"] else None
        s["completed_at"] = s["completed_at"].isoformat() if s["completed_at"] else None
    return jsonify(sessions)


@blueprint.route("/api/sessions/<int:session_id>/results", methods=["GET"])
def get_session_results(session_id):
    sort = request.args.get("sort", "volume")
    direction = request.args.get("dir", "desc")
    allowed_sorts = {
        "keyword": "keyword", "volume": "volume", "traffic_potential": "traffic_potential",
        "difficulty": "difficulty", "cpc_cents": "cpc_cents", "trend_3m": "trend_3m",
        "trend_6m": "trend_6m", "position": "position",
    }
    sort_col = allowed_sorts.get(sort, "volume")
    sort_dir = "ASC" if direction == "asc" else "DESC"
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"SELECT * FROM kr_results WHERE session_id = %s ORDER BY {sort_col} {sort_dir} NULLS LAST",
        (session_id,),
    )
    results = cur.fetchall()
    conn.close()
    return jsonify(results)


@blueprint.route("/api/sessions/<int:session_id>/csv", methods=["GET"])
def export_session_csv(session_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT tab FROM kr_sessions WHERE id = %s", (session_id,))
    session = cur.fetchone()
    cur.execute("SELECT * FROM kr_results WHERE session_id = %s ORDER BY volume DESC NULLS LAST", (session_id,))
    results = cur.fetchall()
    conn.close()
    if not results:
        return "No results", 404

    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)

    tab = session["tab"] if session else "unknown"
    if tab == "targets":
        headers = ["Keyword", "Category", "Volume", "KD", "Position", "30d Delta",
                   "Prev Position", "URL", "Traffic",
                   "Tags",
                   "Comp 1 Pos", "Comp 1 URL", "Comp 2 Pos", "Comp 2 URL",
                   "Comp 3 Pos", "Comp 3 URL", "Is New"]
        writer.writerow(headers)
        for r in results:
            extra = r.get("extra") or {}
            if isinstance(extra, str):
                extra = json.loads(extra)
            comps = extra.get("competitors") or []
            tags = ", ".join(extra.get("tags", []))
            delta = extra.get("pos_delta_30d")
            delta_str = ""
            if delta == 101: delta_str = "NEW"
            elif delta == -101: delta_str = "LOST"
            elif delta is not None: delta_str = f"+{delta}" if delta > 0 else str(delta)
            row = [
                r["keyword"], extra.get("category", ""),
                r["volume"], r["difficulty"],
                r.get("position") or "", delta_str,
                extra.get("prev_position") or "",
                r.get("ranking_url") or "",
                r.get("traffic_potential") or "",
                tags,
            ]
            for i in range(3):
                if i < len(comps) and comps[i]:
                    row.extend([comps[i].get("position", ""), comps[i].get("url", "")])
                else:
                    row.extend(["", ""])
            row.append("Yes" if r["is_new"] else "No")
            writer.writerow(row)
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=kr_{tab}_{session_id}.csv"},
        )
    elif tab == "breakout":
        headers = ["Keyword", "Volume", "KD", "Type", "Blog Position", "Blog URL",
                   "Other Page Position", "Other Page URL", "Traffic Potential",
                   "3mo Trend %", "6mo Trend %", "Is New"]
        writer.writerow(headers)
        for r in results:
            extra = r.get("extra") or {}
            if isinstance(extra, str):
                extra = json.loads(extra)
            writer.writerow([
                r["keyword"], r["volume"], r["difficulty"],
                extra.get("status", ""),
                r.get("position", ""), r.get("ranking_url", ""),
                extra.get("other_position", ""), extra.get("other_url", ""),
                r.get("traffic_potential", ""),
                f"{r['trend_3m']:.1f}" if r.get("trend_3m") is not None else "",
                f"{r['trend_6m']:.1f}" if r.get("trend_6m") is not None else "",
                "Yes" if r["is_new"] else "No",
            ])
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=kr_{tab}_{session_id}.csv"},
        )
    elif tab == "content_gap":
        headers = ["Keyword", "Volume", "Traffic Potential", "KD", "CPC ($)",
                    "3mo Trend %", "6mo Trend %", "Our Position", "Our URL",
                    "# Competitors", "Competitors", "Is New"]
    else:
        headers = ["Keyword", "Volume", "Traffic Potential", "KD", "CPC ($)",
                    "3mo Trend %", "6mo Trend %", "Parent Topic", "Position", "URL", "Source", "Is New"]
    writer.writerow(headers)

    for r in results:
        if tab == "content_gap":
            comps = r.get("competitors") or []
            if isinstance(comps, str):
                comps = json.loads(comps)
            comp_str = "; ".join(f"{c['domain']} #{c['position']}" for c in comps)
            writer.writerow([
                r["keyword"], r["volume"], r["traffic_potential"], r["difficulty"],
                f"{(r['cpc_cents'] or 0)/100:.2f}",
                f"{r['trend_3m']:.1f}" if r["trend_3m"] is not None else "",
                f"{r['trend_6m']:.1f}" if r["trend_6m"] is not None else "",
                r.get("position") or "", r.get("ranking_url") or "",
                len(comps), comp_str,
                "Yes" if r["is_new"] else "No",
            ])
        else:
            writer.writerow([
                r["keyword"], r["volume"], r["traffic_potential"], r["difficulty"],
                f"{(r['cpc_cents'] or 0)/100:.2f}",
                f"{r['trend_3m']:.1f}" if r["trend_3m"] is not None else "",
                f"{r['trend_6m']:.1f}" if r["trend_6m"] is not None else "",
                r.get("parent_topic", ""), r.get("position") or "", r.get("ranking_url") or "",
                r["source"], "Yes" if r["is_new"] else "No",
            ])

    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=kr_{tab}_{session_id}.csv"},
    )


# ─── Job polling ─────────────────────────────────────────────────────

@blueprint.route("/api/job/<job_id>", methods=["GET"])
def check_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ─── Tab 1: Discovery ───────────────────────────────────────────────

@blueprint.route("/api/discovery/run", methods=["POST"])
def run_discovery():
    data = request.json or {}
    adhoc_seeds = [s.strip().lower() for s in data.get("adhoc_seeds", []) if s.strip()]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT keyword FROM kr_seeds")
    stored_seeds = [r[0] for r in cur.fetchall()]
    conn.close()

    settings = _load_settings()
    all_seeds = list(set(stored_seeds + adhoc_seeds))
    if not all_seeds:
        return jsonify({"error": "No seed keywords provided"}), 400

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running", "progress": "", "session_id": None}

    t = threading.Thread(
        target=_run_discovery_job,
        args=(job_id, all_seeds, settings),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


def _run_discovery_job(job_id, seeds, settings):
    try:
        from ahrefs_internal import AhrefsClient, AhrefsInternalClient

        filters = settings.get("filters", {})
        country = settings.get("target_country", "us")
        target_site = settings.get("target_site", "")
        own_brand = target_site.split(".")[0].lower() if target_site else ""
        min_position = filters.get("min_position", 41)

        all_keywords = {}
        ideas_types = [
            ("MatchingTermsTermsMatch", "matching"),
            ("RelatedTerms", "related"),
            ("SearchSuggestions", "suggestions"),
        ]

        # Phase 1: Fetch keyword ideas from KE
        with AhrefsInternalClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as client:
            for i, seed in enumerate(seeds):
                for ideas_type_val, ideas_label in ideas_types:
                    _jobs[job_id]["progress"] = f"Seed {i+1}/{len(seeds)}: '{seed}' ({ideas_label})"
                    try:
                        result = client.ke_ideas(
                            seed=["Keywords", [seed]],
                            country=country,
                            search_engine="Google",
                            ideas_type=[ideas_type_val],
                            offset=0, limit=100,
                            with_position=False, filters=[],
                            sort={"by": ["Volume"], "order": ["Desc"]},
                        )
                        for row in (result.get("results", []) if isinstance(result, dict) else []):
                            kw = row.get("keyword", "")
                            if not kw:
                                continue
                            if kw in all_keywords:
                                all_keywords[kw]["sources"].add(seed)
                                continue
                            if not _apply_keyword_filters(kw, row, filters, own_brand):
                                continue

                            msv = row.get("monthlySearchVolume") or {}
                            volume_history = _parse_volume_history(msv)
                            growth = row.get("growthRate") or {}

                            all_keywords[kw] = {
                                "keyword": kw,
                                "volume": row.get("volume") or 0,
                                "traffic_potential": row.get("trafficPotential") or 0,
                                "difficulty": row.get("difficulty"),
                                "cpc_cents": row.get("cpc") or 0,
                                "parent_topic": row.get("parentTopic"),
                                "volume_history": volume_history,
                                "trend_3m": growth.get("months_3"),
                                "trend_6m": growth.get("months_6"),
                                "sources": {seed},
                            }
                    except Exception as e:
                        print(f"Error fetching {ideas_label} for '{seed}': {e}")

        # Phase 2: Check rankings
        _jobs[job_id]["progress"] = f"Checking rankings for {len(all_keywords)} keywords..."
        with AhrefsClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as se_client:
            rankings = _check_rankings(se_client, list(all_keywords.keys()), target_site, country, min_position, job_id)

        filtered, excluded = {}, 0
        for kw, data in all_keywords.items():
            rank = rankings.get(kw)
            if rank:
                data["position"] = rank["position"]
                data["ranking_url"] = rank["url"]
                if rank["position"] < min_position:
                    excluded += 1
                    continue
            else:
                data["position"] = None
                data["ranking_url"] = None
            filtered[kw] = data

        _jobs[job_id]["progress"] = f"Saving {len(filtered)} keywords..."
        sid, total, new_count = _save_session("discovery", filters, filtered, excluded, {"seeds_used": len(seeds)})

        _jobs[job_id].update(status="completed", session_id=sid,
            progress=f"Done! {total} keywords ({new_count} new, {excluded} excluded)")
    except Exception as e:
        _jobs[job_id].update(status="failed", progress=f"Error: {e}", error=traceback.format_exc())
        print(f"Discovery job failed: {traceback.format_exc()}")


def _parse_volume_history(msv):
    if not msv or not msv.get("volume"):
        return None
    try:
        start = msv.get("startDate", "")
        parts = start.split("-")
        y, m = int(parts[0]), int(parts[1])
        history = []
        for v in msv["volume"]:
            history.append({"date": f"{y}-{m:02d}", "volume": v})
            m += 1
            if m > 12:
                m = 1
                y += 1
        return history[-12:]
    except Exception:
        return None


# ─── Tab 2: Content Gap ─────────────────────────────────────────────

@blueprint.route("/api/content_gap/run", methods=["POST"])
def run_content_gap():
    data = request.json or {}
    settings = _load_settings()
    competitors = data.get("competitors") or settings.get("competitors", [])
    kw_per_competitor = data.get("kw_per_competitor", 500)

    if not competitors:
        return jsonify({"error": "No competitors configured"}), 400

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running", "progress": "", "session_id": None}

    t = threading.Thread(
        target=_run_content_gap_job,
        args=(job_id, competitors, kw_per_competitor, settings),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


def _run_content_gap_job(job_id, competitors, kw_per_competitor, settings):
    try:
        from ahrefs_internal import AhrefsClient, AhrefsInternalClient, se_filter

        filters = settings.get("filters", {})
        country = settings.get("target_country", "us")
        target_site = settings.get("target_site", "")
        own_brand = target_site.split(".")[0].lower() if target_site else ""
        min_position = filters.get("min_position", 41)
        min_volume = filters.get("min_volume", 100)

        # Phase 1: Pull top keywords from each competitor
        all_gap_kws = {}  # keyword -> {volume, difficulty, competitors: [...]}

        with AhrefsClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as client:
            for i, comp in enumerate(competitors):
                _jobs[job_id]["progress"] = f"Fetching keywords from {comp} ({i+1}/{len(competitors)})..."
                try:
                    kws = client.site_explorer_organic_keywords(
                        target=comp, country=country, mode="subdomains",
                        limit=kw_per_competitor,
                        filter=se_filter("volume", ">=", min_volume or 100),
                    )
                    for kw in kws:
                        keyword = kw.keyword
                        if keyword not in all_gap_kws:
                            all_gap_kws[keyword] = {
                                "keyword": keyword,
                                "volume": kw.volume,
                                "difficulty": kw.difficulty,
                                "cpc_cents": kw.cpc_cents,
                                "competitors": [],
                            }
                        all_gap_kws[keyword]["competitors"].append({
                            "domain": comp,
                            "position": kw.position,
                            "url": kw.url,
                            "traffic": kw.traffic,
                        })
                        # Keep highest volume across sources
                        if kw.volume and kw.volume > (all_gap_kws[keyword]["volume"] or 0):
                            all_gap_kws[keyword]["volume"] = kw.volume
                except Exception as e:
                    print(f"Error fetching keywords for {comp}: {e}")

            _jobs[job_id]["progress"] = f"Collected {len(all_gap_kws)} unique keywords from {len(competitors)} competitors"

            # Phase 2: Check our rankings and filter out where we already rank well
            kw_list = list(all_gap_kws.keys())
            rankings = _check_rankings(client, kw_list, target_site, country, min_position, job_id)

        # Phase 3: Apply text-based filters, then enrich survivors with trends
        _jobs[job_id]["progress"] = "Applying filters..."
        pre_enrich = {}
        excluded = 0

        for kw, data in all_gap_kws.items():
            # Position filter
            rank = rankings.get(kw)
            if rank:
                if rank["position"] < min_position:
                    excluded += 1
                    continue
                data["position"] = rank["position"]
                data["ranking_url"] = rank["url"]
            else:
                data["position"] = None
                data["ranking_url"] = None

            # Apply text-based filters (exclude terms, volume, KD)
            # Pass empty attrs/categories — we don't have KE data yet
            text_filter_row = {
                "volume": data["volume"],
                "difficulty": data["difficulty"],
                "attrs": {},
                "categories": {},
            }
            # Manually check text filters (skip category/branded/local since we lack attrs)
            exclude_terms = [t.lower() for t in filters.get("exclude_terms", [])]
            if data["volume"] and data["volume"] < (min_volume or 0):
                excluded += 1
                continue
            max_kd = filters.get("max_kd")
            if max_kd is not None and data.get("difficulty") is not None and data["difficulty"] > max_kd:
                excluded += 1
                continue
            if exclude_terms and any(t in kw.lower() for t in exclude_terms):
                excluded += 1
                continue

            pre_enrich[kw] = data

        # Phase 4: Enrich survivors with KE data (trends, categories, attrs)
        # Use small batches of 10 with PhraseMatch for best exact-match rate
        _jobs[job_id]["progress"] = f"Enriching {len(pre_enrich)} keywords with trends & categories..."
        ke_data = {}
        kw_survivors = list(pre_enrich.keys())
        ke_batch_size = 10

        with AhrefsInternalClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as ke_client:
            kw_batches = [kw_survivors[i:i+ke_batch_size] for i in range(0, len(kw_survivors), ke_batch_size)]
            for bi, batch in enumerate(kw_batches):
                if bi % 10 == 0:
                    _jobs[job_id]["progress"] = f"Enriching: {bi*ke_batch_size}/{len(kw_survivors)} keywords..."
                try:
                    batch_set = set(batch)
                    result = ke_client.ke_ideas(
                        seed=["Keywords", batch],
                        country=country,
                        search_engine="Google",
                        ideas_type=["MatchingTermsPhraseMatch"],
                        offset=0, limit=ke_batch_size * 2,
                        with_position=False, filters=[],
                        sort={"by": ["Volume"], "order": ["Desc"]},
                    )
                    for row in (result.get("results", []) if isinstance(result, dict) else []):
                        rk = row.get("keyword", "")
                        if rk in batch_set:
                            ke_data[rk] = row
                except Exception as e:
                    print(f"Error enriching batch {bi}: {e}")

        _jobs[job_id]["progress"] = f"Enriched {len(ke_data)}/{len(kw_survivors)} keywords. Final filtering..."

        # Phase 5: Apply category/branded/local filters using enrichment, build final
        final_keywords = {}
        for kw, data in pre_enrich.items():
            ke_row = ke_data.get(kw, {})
            attrs = ke_row.get("attrs") or {}
            cats = ke_row.get("categories") or {}

            # Branded filter (needs attrs)
            if filters.get("exclude_branded", True):
                if attrs.get("branded", False) and own_brand and own_brand not in kw.lower():
                    excluded += 1
                    continue

            # Local filter
            if filters.get("exclude_local", True) and attrs.get("local", False):
                excluded += 1
                continue

            # NSFW
            nsfw = cats.get("nsfw", [])
            kw_cats = cats.get("category", [])
            if "Adult" in nsfw or "Nsfw" in nsfw or "Adult" in kw_cats:
                excluded += 1
                continue

            # Category filter
            allowed_categories = filters.get("allowed_categories", [])
            if allowed_categories:
                if not kw_cats:
                    # No category data — either KE didn't have it or it's uncategorized
                    # For content gap: unenriched keywords are often junk, drop them
                    if not ke_row or filters.get("drop_uncategorized", False):
                        excluded += 1
                        continue
                else:
                    matched = any(
                        c.startswith(a) for c in kw_cats for a in allowed_categories
                    )
                    if not matched:
                        excluded += 1
                        continue

            # Merge enrichment
            growth = ke_row.get("growthRate") or {}
            msv = ke_row.get("monthlySearchVolume") or {}
            data["traffic_potential"] = ke_row.get("trafficPotential") or 0
            data["parent_topic"] = ke_row.get("parentTopic")
            data["trend_3m"] = growth.get("months_3")
            data["trend_6m"] = growth.get("months_6")
            data["volume_history"] = _parse_volume_history(msv)
            if ke_row.get("cpc") is not None:
                data["cpc_cents"] = ke_row.get("cpc")

            final_keywords[kw] = data

        # Save
        _jobs[job_id]["progress"] = f"Saving {len(final_keywords)} gap keywords..."
        sid, total, new_count = _save_session(
            "content_gap", filters, final_keywords, excluded,
            {"competitors_analyzed": len(competitors), "competitors": competitors},
        )

        _jobs[job_id].update(status="completed", session_id=sid,
            progress=f"Done! {total} gap keywords ({new_count} new, {excluded} filtered out)")
    except Exception as e:
        _jobs[job_id].update(status="failed", progress=f"Error: {e}", error=traceback.format_exc())
        print(f"Content gap job failed: {traceback.format_exc()}")


# ─── Tab 3: Breakout Opportunities ──────────────────────────────────

@blueprint.route("/api/breakout/run", methods=["POST"])
def run_breakout():
    settings = _load_settings()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running", "progress": "", "session_id": None}

    t = threading.Thread(
        target=_run_breakout_job,
        args=(job_id, settings),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


def _run_breakout_job(job_id, settings):
    """Find blog keywords ranking 31-100, cross-check if the rest of the domain
    already ranks for them.  Two signals:
    - non-blog page exists & ranks better → cannibalization / blog is redundant
    - no non-blog page → opportunity to create a dedicated page
    """
    try:
        from ahrefs_internal import AhrefsClient, AhrefsInternalClient, se_filter, se_and

        filters = settings.get("filters", {})
        country = settings.get("target_country", "us")
        target_site = settings.get("target_site", "")
        own_brand = target_site.split(".")[0].lower() if target_site else ""
        min_volume = filters.get("min_volume", 100)
        blog_path = f"{target_site}/blog/"
        exclude_terms = [t.lower() for t in filters.get("exclude_terms", [])]

        # Phase 1: Pull blog keywords ranking 31-100
        _jobs[job_id]["progress"] = f"Fetching blog keywords ranking 31-100..."

        blog_keywords = {}  # keyword -> {data}
        with AhrefsClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as client:
            offset = 0
            batch_limit = 500
            while True:
                _jobs[job_id]["progress"] = f"Fetching blog keywords: {len(blog_keywords)} loaded..."
                kws = client.site_explorer_organic_keywords(
                    target=blog_path, country=country, mode="prefix",
                    limit=batch_limit, offset=offset,
                    filter=se_and(
                        se_filter("position", ">=", 31),
                        se_filter("position", "<=", 100),
                        se_filter("volume", ">=", min_volume or 100),
                    ),
                )
                for kw in kws:
                    blog_keywords[kw.keyword] = {
                        "keyword": kw.keyword,
                        "volume": kw.volume,
                        "difficulty": kw.difficulty,
                        "cpc_cents": kw.cpc_cents,
                        "position": kw.position,
                        "ranking_url": kw.url,
                    }
                if len(kws) < batch_limit:
                    break
                offset += batch_limit
                if offset >= 5000:
                    break

        _jobs[job_id]["progress"] = f"Found {len(blog_keywords)} blog keywords in pos 31-100"

        # Phase 2: Cross-check full domain rankings for these keywords
        _jobs[job_id]["progress"] = f"Cross-checking domain rankings for {len(blog_keywords)} keywords..."
        domain_rankings = {}  # keyword -> {position, url} for best non-blog URL

        kw_list = list(blog_keywords.keys())
        batch_size = 50
        with AhrefsClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as client:
            for i in range(0, len(kw_list), batch_size):
                batch = kw_list[i:i + batch_size]
                if i % 200 == 0:
                    _jobs[job_id]["progress"] = f"Cross-checking: {i}/{len(kw_list)} keywords..."
                try:
                    ranked = client.site_explorer_organic_keywords(
                        target=target_site, country=country, mode="subdomains",
                        limit=len(batch) * 2,
                        filter=se_filter("keyword", "in", batch),
                    )
                    for r in ranked:
                        is_blog = "/blog/" in r.url
                        if not is_blog:
                            # Non-blog page ranks for this keyword
                            existing = domain_rankings.get(r.keyword)
                            if not existing or r.position < existing["position"]:
                                domain_rankings[r.keyword] = {
                                    "position": r.position,
                                    "url": r.url,
                                }
                except Exception as e:
                    print(f"Error cross-checking batch {i}: {e}")

        _jobs[job_id]["progress"] = f"{len(domain_rankings)} keywords have non-blog pages. Enriching..."

        # Phase 3: Enrich with KE data (trends)
        ke_data = {}
        ke_batch_size = 10
        with AhrefsInternalClient(base_url="http://127.0.0.1:18081/ahrefs-internal") as ke_client:
            batches = [kw_list[i:i+ke_batch_size] for i in range(0, len(kw_list), ke_batch_size)]
            for bi, batch in enumerate(batches):
                if bi % 10 == 0:
                    _jobs[job_id]["progress"] = f"Enriching: {bi*ke_batch_size}/{len(kw_list)} keywords..."
                try:
                    batch_set = set(batch)
                    result = ke_client.ke_ideas(
                        seed=["Keywords", batch],
                        country=country,
                        search_engine="Google",
                        ideas_type=["MatchingTermsPhraseMatch"],
                        offset=0, limit=ke_batch_size * 2,
                        with_position=False, filters=[],
                        sort={"by": ["Volume"], "order": ["Desc"]},
                    )
                    for row in (result.get("results", []) if isinstance(result, dict) else []):
                        rk = row.get("keyword", "")
                        if rk in batch_set:
                            ke_data[rk] = row
                except Exception as e:
                    print(f"Error enriching batch {bi}: {e}")

        # Phase 4: Apply filters and build results
        _jobs[job_id]["progress"] = "Applying filters..."
        final_keywords = {}
        excluded = 0

        for kw, data in blog_keywords.items():
            # Text filters
            if exclude_terms and any(t in kw.lower() for t in exclude_terms):
                excluded += 1
                continue

            ke_row = ke_data.get(kw, {})
            attrs = ke_row.get("attrs") or {}
            cats = ke_row.get("categories") or {}

            if filters.get("exclude_branded", True) and attrs.get("branded", False):
                if own_brand and own_brand not in kw.lower():
                    excluded += 1
                    continue
            if filters.get("exclude_local", True) and attrs.get("local", False):
                excluded += 1
                continue
            nsfw = cats.get("nsfw", [])
            if "Adult" in nsfw or "Nsfw" in nsfw or "Adult" in cats.get("category", []):
                excluded += 1
                continue

            # Build result
            domain_match = domain_rankings.get(kw)
            growth = ke_row.get("growthRate") or {}
            msv = ke_row.get("monthlySearchVolume") or {}

            status = "cannibalization" if domain_match else "breakout"

            final_keywords[kw] = {
                "keyword": kw,
                "volume": data["volume"],
                "traffic_potential": ke_row.get("trafficPotential") or 0,
                "difficulty": data["difficulty"],
                "cpc_cents": ke_row.get("cpc") or data.get("cpc_cents") or 0,
                "position": data["position"],  # blog position
                "ranking_url": data["ranking_url"],  # blog URL
                "parent_topic": ke_row.get("parentTopic"),
                "trend_3m": growth.get("months_3"),
                "trend_6m": growth.get("months_6"),
                "volume_history": _parse_volume_history(msv),
                "extra": {
                    "status": status,
                    "other_url": domain_match["url"] if domain_match else None,
                    "other_position": domain_match["position"] if domain_match else None,
                },
                "sources": set(),
            }

        # Save
        breakout_count = sum(1 for d in final_keywords.values()
                            if (d.get("extra") or {}).get("status") == "breakout")
        cannibal_count = len(final_keywords) - breakout_count
        _jobs[job_id]["progress"] = f"Saving {len(final_keywords)} results..."
        sid, total, new_count = _save_session(
            "breakout", filters, final_keywords, excluded,
            {"breakout_count": breakout_count, "cannibalization_count": cannibal_count},
        )

        _jobs[job_id].update(status="completed", session_id=sid,
            progress=f"Done! {breakout_count} breakout + {cannibal_count} cannibalization ({excluded} filtered)")
    except Exception as e:
        _jobs[job_id].update(status="failed", progress=f"Error: {e}", error=traceback.format_exc())
        print(f"Breakout job failed: {traceback.format_exc()}")


# ─── Master List (cross-tab merge) ───────────────────────────

@blueprint.route("/api/master/results")
def master_results():
    """Merge latest completed session from each tab, dedup by keyword."""
    sort = request.args.get("sort", "volume")
    direction = request.args.get("dir", "desc")
    allowed_sorts = {
        "keyword": "keyword", "volume": "volume", "difficulty": "difficulty",
        "position": "position", "traffic_potential": "traffic_potential",
    }
    sort_col = allowed_sorts.get(sort, "volume")
    sort_dir = "ASC" if direction == "asc" else "DESC"

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get latest completed session per tab
    tabs = ["discovery", "content_gap", "breakout", "targets"]
    session_ids = []
    session_map = {}  # session_id -> tab
    for tab in tabs:
        cur.execute(
            "SELECT id FROM kr_sessions WHERE tab = %s AND status = 'completed' ORDER BY started_at DESC LIMIT 1",
            (tab,),
        )
        row = cur.fetchone()
        if row:
            session_ids.append(row["id"])
            session_map[row["id"]] = tab

    if not session_ids:
        conn.close()
        return jsonify([])

    # Pull all results from those sessions
    cur.execute(
        f"SELECT * FROM kr_results WHERE session_id = ANY(%s) ORDER BY {sort_col} {sort_dir} NULLS LAST",
        (session_ids,),
    )
    all_rows = cur.fetchall()
    conn.close()

    # Merge: dedup by keyword, collect sources
    TAB_LABELS = {
        "discovery": "Discovery",
        "content_gap": "Gap",
        "breakout": "Breakout",
        "targets": "Tracker",
    }
    merged = {}  # keyword -> row dict
    for row in all_rows:
        kw = row["keyword"]
        tab = session_map.get(row["session_id"], "")
        label = TAB_LABELS.get(tab, tab)

        if kw not in merged:
            # Use first occurrence (sorted by the requested column)
            merged[kw] = dict(row)
            merged[kw]["tabs"] = [label]
            merged[kw]["tab_count"] = 1
        else:
            # Merge: add tab source, prefer data with position
            if label not in merged[kw]["tabs"]:
                merged[kw]["tabs"].append(label)
                merged[kw]["tab_count"] = len(merged[kw]["tabs"])
            # If existing has no position but this one does, update
            if merged[kw]["position"] is None and row["position"] is not None:
                merged[kw]["position"] = row["position"]
                merged[kw]["ranking_url"] = row["ranking_url"]
            # Keep higher volume if different
            if (row["volume"] or 0) > (merged[kw]["volume"] or 0):
                merged[kw]["volume"] = row["volume"]

    # Filter out "nope" blacklisted keywords
    show_nope = request.args.get("show_nope", "false") == "true"
    nope_kws = set()
    if not show_nope:
        cur2 = conn2 = None
        try:
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute("SELECT keyword FROM kr_keyword_lists WHERE list_name = 'nope'")
            nope_kws = {r[0] for r in cur2.fetchall()}
        finally:
            if conn2:
                conn2.close()

    # Enrich with tier/cluster data
    tc = _load_tier_cache()

    result_list = []
    for kw, data in merged.items():
        if kw in nope_kws:
            continue
        info = tc.get(kw, {})
        data["tier"] = info.get("tier")
        data["tier_label"] = info.get("tier_label", "")
        data["cluster"] = info.get("cluster", "")
        data["cluster_id"] = info.get("cluster_id")
        data["distance"] = info.get("distance")
        data["is_in_core"] = info.get("is_in_core", False)
        data["bp"] = info.get("bp")
        result_list.append(data)

    # Re-sort after merge
    reverse = sort_dir == "DESC"
    def sort_key(r):
        v = r.get(sort_col)
        if v is None:
            return (1, 0) if reverse else (1, 0)  # nulls last
        return (0, v)
    result_list.sort(key=sort_key, reverse=reverse)

    return jsonify(result_list)


@blueprint.route("/api/master/csv")
def master_csv():
    """Export master list as CSV, respecting list assignments."""
    import csv, io

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get latest session per tab
    tabs_list = ["discovery", "content_gap", "breakout", "targets"]
    session_ids = []
    session_map = {}
    for tab in tabs_list:
        cur.execute(
            "SELECT id FROM kr_sessions WHERE tab = %s AND status = 'completed' ORDER BY started_at DESC LIMIT 1",
            (tab,),
        )
        row = cur.fetchone()
        if row:
            session_ids.append(row["id"])
            session_map[row["id"]] = tab

    if not session_ids:
        conn.close()
        return Response("No data", mimetype="text/plain")

    cur.execute(
        "SELECT * FROM kr_results WHERE session_id = ANY(%s) ORDER BY volume DESC NULLS LAST",
        (session_ids,),
    )
    all_rows = cur.fetchall()

    # Get nope keywords + all list assignments
    cur.execute("SELECT keyword, list_name FROM kr_keyword_lists")
    kw_list_map = {r["keyword"]: r["list_name"] for r in cur.fetchall()}
    nope_kws = {kw for kw, ln in kw_list_map.items() if ln == "nope"}
    conn.close()

    # Merge & dedup
    TAB_LABELS = {"discovery": "Discovery", "content_gap": "Gap", "breakout": "Breakout", "targets": "Tracker"}
    merged = {}
    for row in all_rows:
        kw = row["keyword"]
        if kw in nope_kws:
            continue
        tab = session_map.get(row["session_id"], "")
        label = TAB_LABELS.get(tab, tab)
        if kw not in merged:
            merged[kw] = dict(row)
            merged[kw]["tabs"] = [label]
        else:
            if label not in merged[kw]["tabs"]:
                merged[kw]["tabs"].append(label)
            if merged[kw]["position"] is None and row["position"] is not None:
                merged[kw]["position"] = row["position"]
                merged[kw]["ranking_url"] = row["ranking_url"]
            if (row["volume"] or 0) > (merged[kw]["volume"] or 0):
                merged[kw]["volume"] = row["volume"]

    tc = _load_tier_cache()
    result_list = sorted(merged.values(), key=lambda r: r.get("volume") or 0, reverse=True)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Keyword", "List", "Tier", "Cluster", "BP", "Volume", "KD", "Position", "URL",
                     "Traffic Potential", "# Tabs", "Tabs"])
    for r in result_list:
        kw = r["keyword"]
        info = tc.get(kw, {})
        writer.writerow([
            kw, kw_list_map.get(kw, ""),
            info.get("tier_label", ""), info.get("cluster", ""), info.get("bp", ""),
            r["volume"], r["difficulty"],
            r.get("position") or "", r.get("ranking_url") or "",
            r.get("traffic_potential") or "",
            len(r["tabs"]), ", ".join(r["tabs"]),
        ])
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=kr_master_list.csv"},
    )


# ─── Keyword Lists (Pitch / Maybe / Backlog / Nope) ─────────

KW_LISTS = ["pitch", "maybe", "backlog", "nope"]


@blueprint.route("/api/lists")
def get_all_lists():
    """Return all keywords grouped by list, plus counts."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT list_name, COUNT(*) as count FROM kr_keyword_lists GROUP BY list_name")
    counts = {r["list_name"]: r["count"] for r in cur.fetchall()}
    conn.close()
    return jsonify({"counts": counts, "lists": KW_LISTS})


@blueprint.route("/api/lists/<list_name>")
def get_list(list_name):
    """Return all keywords in a specific list."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT keyword, added_at FROM kr_keyword_lists WHERE list_name = %s ORDER BY added_at DESC", (list_name,))
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        if r.get("added_at"):
            r["added_at"] = r["added_at"].isoformat()
    return jsonify(rows)


@blueprint.route("/api/lists/add", methods=["POST"])
def add_to_list():
    """Add one or more keywords to a list. Moves if already in another list."""
    data = request.get_json(force=True)
    keywords = data.get("keywords", [])
    list_name = data.get("list", "")
    if not keywords or list_name not in KW_LISTS:
        return jsonify({"error": "Invalid list or keywords"}), 400

    conn = get_db()
    cur = conn.cursor()
    added = 0
    for kw in keywords:
        kw = kw.strip().lower() if isinstance(kw, str) else str(kw).strip().lower()
        if not kw:
            continue
        # Remove from any existing list first (keyword can only be in one list)
        cur.execute("DELETE FROM kr_keyword_lists WHERE keyword = %s", (kw,))
        cur.execute(
            "INSERT INTO kr_keyword_lists (keyword, list_name) VALUES (%s, %s) ON CONFLICT (keyword, list_name) DO NOTHING",
            (kw, list_name),
        )
        added += 1
    conn.commit()
    conn.close()
    return jsonify({"added": added})


@blueprint.route("/api/lists/remove", methods=["POST"])
def remove_from_list():
    """Remove keywords from their list (back to unsorted)."""
    data = request.get_json(force=True)
    keywords = data.get("keywords", [])
    conn = get_db()
    cur = conn.cursor()
    for kw in keywords:
        cur.execute("DELETE FROM kr_keyword_lists WHERE keyword = %s", (kw.strip().lower(),))
    conn.commit()
    conn.close()
    return jsonify({"removed": len(keywords)})


@blueprint.route("/api/lists/lookup", methods=["POST"])
def lookup_lists():
    """Bulk lookup: which list is each keyword in? Returns {keyword: list_name}."""
    data = request.get_json(force=True)
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({})
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT keyword, list_name FROM kr_keyword_lists WHERE keyword = ANY(%s)", (keywords,))
    result = {r["keyword"]: r["list_name"] for r in cur.fetchall()}
    conn.close()
    return jsonify(result)


# ─── Tab 4: Target Keywords Tracker ────────────────────────────────

# Rank Tracker data is fetched through the approved Console connector.


@blueprint.route("/api/targets/config")
def targets_config():
    settings = _load_settings()
    return jsonify({
        "project_id": str(settings.get("rank_tracker_project_id") or ""),
        "default_tags": settings.get("rank_tracker_tags") or [],
    })


@blueprint.route("/api/targets/run", methods=["POST"])
def run_targets():
    settings = _load_settings()
    body = request.get_json(force=True) or {}
    selected_tags = body.get("tags") or settings.get("rank_tracker_tags") or []
    project_id = str(settings.get("rank_tracker_project_id") or "")
    if not project_id:
        return jsonify({"error": "Configure a Rank Tracker project in Settings first."}), 422
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running", "progress": "", "session_id": None}
    t = threading.Thread(target=_run_targets_job, args=(job_id, selected_tags, project_id), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _run_targets_job(job_id, selected_tags, project_id):
    try:
        from applications import _letaido_keyword_research_hub_tiers as tier_helper
        _jobs[job_id]["progress"] = "Fetching Rank Tracker keywords…"
        args = {"project_id": project_id, "limit": 10000, "compare_to": "1m"}
        if selected_tags:
            args["filters"] = {"tags_in": selected_tags}
        result = tier_helper._invoke("ahrefs_rank_tracker.overview_keywords_export", args)
        rows = result.get("records", [])
        final_keywords = {}
        for kw in rows:
            keyword = (kw.get("keyword") or "").strip()
            if not keyword or keyword in final_keywords:
                continue
            cur_pos = kw.get("current_position") if result.get("is_compared") else kw.get("position")
            prev_pos = kw.get("previous_position")
            pos_delta = None
            if cur_pos is not None and prev_pos is not None:
                pos_delta = prev_pos - cur_pos
            elif cur_pos is not None:
                pos_delta = 101
            elif prev_pos is not None:
                pos_delta = -101
            category = "update" if cur_pos is not None and 3 <= cur_pos <= 10 else "rewrite"
            final_keywords[keyword] = {
                "keyword": keyword, "volume": kw.get("volume") or 0,
                "difficulty": kw.get("difficulty"),
                "cpc_cents": int(float(kw.get("cpc") or 0) * 100),
                "position": cur_pos,
                "ranking_url": kw.get("current_url") or kw.get("url") or "",
                "parent_topic": kw.get("parent_topic"),
                "traffic_potential": kw.get("current_traffic") or kw.get("traffic") or 0,
                "extra": {"tags": kw.get("tags") or [], "competitors": [],
                          "category": category, "prev_position": prev_pos,
                          "pos_delta_30d": pos_delta},
                "sources": set(),
            }
        filters = {"tags": selected_tags}
        update_count = sum(1 for d in final_keywords.values() if d["extra"]["category"] == "update")
        rewrite_count = len(final_keywords) - update_count
        ranking = sum(1 for d in final_keywords.values() if d.get("position") is not None)
        sid, total, new_count = _save_session(
            "targets", filters, final_keywords, 0,
            {"ranking": ranking, "not_ranking": len(final_keywords)-ranking,
             "tags": selected_tags, "update_count": update_count,
             "rewrite_count": rewrite_count},
        )
        _jobs[job_id].update(status="completed", session_id=sid,
            progress=f"Done! {update_count} to update, {rewrite_count} to rewrite")
    except Exception as e:
        _jobs[job_id].update(status="failed", progress=f"Error: {e}", error=traceback.format_exc())
        print(f"Targets job failed: {traceback.format_exc()}")


# ─── Tier & Cluster Generator ────────────────────────────────────────

from applications import _letaido_keyword_research_hub_tiers as _kr_tiers


@blueprint.route("/api/tiers/status")
def tiers_status():
    """Current tier file + latest generation run."""
    import importlib
    importlib.reload(_kr_tiers)
    _kr_tiers.ensure_table()
    counts = {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT list_name, COUNT(*) FROM kr_keyword_lists WHERE list_name != 'nope' GROUP BY list_name")
    for name, n in cur.fetchall():
        counts[name] = n
    cur.execute("SELECT COUNT(DISTINCT keyword) FROM kr_results")
    bank_count = cur.fetchone()[0]
    cur.execute("SELECT EXTRACT(EPOCH FROM MAX(completed_at)) FROM kr_sessions WHERE status = 'completed'")
    last_session_epoch = cur.fetchone()[0]
    conn.close()
    file_status = _kr_tiers.tier_file_status()
    classified = {}
    tier_model = {}
    if file_status.get("exists"):
        try:
            with open(_kr_tiers.TIERS_OUT) as f:
                tier_model = json.load(f)
            classified = {r.get("keyword", "").strip().lower(): r for r in tier_model.get("results", [])}
        except Exception:
            classified = {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        WITH latest AS (
          SELECT DISTINCT ON (tab) id
          FROM kr_sessions
          WHERE status='completed'
          ORDER BY tab, started_at DESC
        )
        SELECT DISTINCT lower(r.keyword)
        FROM kr_results r
        JOIN latest l ON l.id=r.session_id
        LEFT JOIN kr_keyword_lists kl ON lower(kl.keyword)=lower(r.keyword) AND kl.list_name='nope'
        WHERE kl.id IS NULL
    """)
    bank_keywords = {r[0] for r in cur.fetchall()}
    conn.close()
    missing_tier = sum(1 for kw in bank_keywords if not classified.get(kw) or classified[kw].get("tier") is None)
    missing_bp = sum(1 for kw in bank_keywords if not classified.get(kw) or classified[kw].get("bp") is None)
    metadata = tier_model.get("metadata") or {}
    quick_available = bool(metadata.get("core_hash") and metadata.get("centroids"))
    stale = bool(
        file_status.get("exists")
        and last_session_epoch
        and file_status.get("mtime_epoch")
        and float(last_session_epoch) > file_status["mtime_epoch"]
    )
    settings = _load_settings()
    return jsonify({
        "file": file_status,
        "latest_run": _kr_tiers.latest_run(),
        "list_counts": counts,
        "bank_count": bank_count,
        "core_available": sum(counts.values()),
        "stale": stale,
        "missing_tier": missing_tier,
        "missing_bp": missing_bp,
        "needs_enrichment": bool(missing_tier or missing_bp),
        "quick_available": quick_available,
        "bp_models": [{"id": k, "label": v} for k, v in _kr_tiers.BP_MODELS.items()],
        "default_bp_model": _kr_tiers.DEFAULT_BP_MODEL,
        "enrichment_product": settings.get("enrichment_product") or "",
        "enrichment_bp_model": settings.get("enrichment_bp_model") or _kr_tiers.DEFAULT_BP_MODEL,
    })


@blueprint.route("/api/tiers/projects")
def tiers_projects():
    try:
        return jsonify({"projects": _kr_tiers.list_rank_tracker_projects()})
    except Exception as e:
        return jsonify({"projects": [], "error": str(e)[:200]}), 502


@blueprint.route("/api/tiers/run", methods=["POST"])
def tiers_run():
    cfg = request.get_json(force=True) or {}
    core_source = cfg.get("core_source", "lists")
    config = {
        "core_source": core_source,
        "core_lists": cfg.get("core_lists") or ["pitch", "backlog", "maybe"],
        "project_id": str(cfg.get("project_id") or ""),
        "core_keywords": [
            k.strip() for k in (cfg.get("core_keywords") or "").replace(",", "\n").split("\n") if k.strip()
        ],
        "k": int(cfg.get("k") or 0),
        "threshold_mode": cfg.get("threshold_mode", "fixed"),
        "bp": bool(cfg.get("bp")),
        "bp_model": cfg.get("bp_model") or _kr_tiers.DEFAULT_BP_MODEL,
        "product": (cfg.get("product") or "").strip(),
        "include_nope": bool(cfg.get("include_nope")),
        "mode": cfg.get("mode") if cfg.get("mode") in {"quick", "full"} else "full",
    }
    if core_source == "rank_tracker" and not config["project_id"]:
        return jsonify({"error": "Pick a Rank Tracker project first."}), 422
    if core_source == "paste" and len(config["core_keywords"]) < 4:
        return jsonify({"error": "Paste at least 4 core keywords."}), 422
    if config["bp"] and not config["product"]:
        return jsonify({"error": "BP scoring needs a one-sentence product description."}), 422
    if config["product"]:
        conn = get_db()
        cur = conn.cursor()
        for key, value in (("enrichment_product", config["product"]), ("enrichment_bp_model", config["bp_model"])):
            cur.execute("INSERT INTO kr_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                        (key, json.dumps(value)))
        conn.commit(); conn.close()
    run_id = _kr_tiers.launch_run(config)
    return jsonify({"run_id": run_id})


@blueprint.route("/api/tiers/run/<run_id>")
def tiers_run_status(run_id):
    row = _kr_tiers.get_run(run_id)
    if not row:
        return jsonify({"error": "run not found"}), 404
    return jsonify(row)
