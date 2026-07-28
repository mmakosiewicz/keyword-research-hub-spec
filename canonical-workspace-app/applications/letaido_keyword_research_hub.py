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


CONNECTOR_PROXY = "http://127.0.0.1:18081/capabilities/invoke"

def _invoke_connector(connector_id, args, timeout=180):
    response = http_requests.post(f"{CONNECTOR_PROXY}/{connector_id}", json={"caller":"app","secret_name":"ahrefs_oauth","args":args}, timeout=timeout)
    response.raise_for_status(); payload=response.json()
    if payload.get("status") != "ok": raise RuntimeError(payload.get("error") or f"{connector_id} failed")
    return payload.get("result") or {}

def _rankings_for_keywords(keywords, target_site, country, job_id=None):
    rankings,warnings,successes={},[],0
    if not target_site or not keywords: return rankings,warnings,successes
    for i in range(0,len(keywords),100):
        batch=keywords[i:i+100]
        if job_id: _jobs[job_id]["progress"]=f"Checking target rankings: {i}/{len(keywords)}…"
        try:
            result=_invoke_connector("ahrefs_keywords_explorer.keywords_overview_by_page_or_domain", {"target":target_site,"mode":"domain","country":country,"keywords":batch,"include_related_keywords":False,"limit":max(1,len(batch))})
            successes+=1
            for row in result.get("records",[]):
                keyword=(row.get("keyword") or "").strip()
                if keyword: rankings[keyword]={"position":row.get("top_position"),"url":row.get("top_url") or "","urls":row.get("urls") or []}
        except Exception as exc: warnings.append(f"Target ranking batch {i//100+1} failed: {exc}")
    return rankings,warnings,successes

def _history_from_connector(row):
    values=row.get("sv_trend") or []
    return [{"date":str(i),"volume":value} for i,value in enumerate(values[-12:])] or None

def _overview_terms(keywords, country, job_id=None):
    data,warnings,successes={},[],0
    for i in range(0,len(keywords),100):
        batch=keywords[i:i+100]
        if job_id: _jobs[job_id]["progress"]=f"Enriching keyword metrics: {i}/{len(keywords)}…"
        try:
            result=_invoke_connector("ahrefs_keywords_explorer.keywords_overview_by_terms_export", {"keywords":batch,"country":country,"with_position":False,"limit":max(1,len(batch)),"order_by":"volume","direction":"desc","filters":{"terms":batch}})
            successes+=1
            for row in result.get("records",[]):
                keyword=(row.get("keyword") or "").strip()
                if keyword: data[keyword]=row
        except Exception as exc: warnings.append(f"Keyword metrics batch {i//100+1} failed: {exc}")
    return data,warnings,successes

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


def _check_rankings(_unused, kw_list, target_site, country, min_position, job_id):
    rankings, _warnings, _successes = _rankings_for_keywords(kw_list, target_site, country, job_id)
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
                    json.dumps(carried_data["volume_history"]) if carried_data.get("volume_history") is not None and not isinstance(carried_data["volume_history"], str) else carried_data.get("volume_history"),
                    carried_data["trend_3m"], carried_data["trend_6m"],
                    json.dumps(carried_data["competitors"]) if carried_data.get("competitors") is not None and not isinstance(carried_data["competitors"], str) else carried_data.get("competitors"),
                    False,
                    json.dumps(carried_data["extra"]) if carried_data.get("extra") is not None and not isinstance(carried_data["extra"], str) else carried_data.get("extra"),
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
        filters = settings.get("filters", {})
        country = settings.get("target_country", "us")
        target_site = settings.get("target_site", "")
        own_brand = target_site.split(".")[0].lower() if target_site else ""
        min_position = filters.get("min_position", 41)
        all_keywords, warnings, successful_calls = {}, [], 0
        modes = [("matching_terms", "matching"), ("related_all", "related"), ("search_suggestions", "suggestions")]

        for i, seed in enumerate(seeds):
            for mode, label in modes:
                _jobs[job_id]["progress"] = f"Seed {i+1}/{len(seeds)}: '{seed}' ({label})"
                try:
                    result = _invoke_connector("ahrefs_keywords_explorer.ideas_by_terms_export", {
                        "seed_keywords": [seed], "country": country, "mode": mode,
                        "related_top_n": 100, "limit": 100, "order_by": "volume",
                        "direction": "desc", "with_position": bool(target_site),
                        "filters": ({"target_url": target_site, "target_url_mode": "subdomains", "target_rank": "all"}
                                    if target_site else {}),
                    })
                    successful_calls += 1
                    for row in result.get("records", []):
                        kw = (row.get("keyword") or "").strip()
                        if not kw:
                            continue
                        intents = {str(v).lower() for v in (row.get("intents") or [])}
                        filter_row = {"volume": row.get("volume") or 0, "difficulty": row.get("difficulty"),
                                      "attrs": {"branded": "branded" in intents, "local": "local" in intents},
                                      "categories": {"category": [row["category"]] if row.get("category") else []}}
                        if not _apply_keyword_filters(kw, filter_row, filters, own_brand):
                            continue
                        if kw in all_keywords:
                            all_keywords[kw]["sources"].add(seed)
                            continue
                        pos = row.get("position")
                        if pos is not None and pos < min_position:
                            continue
                        all_keywords[kw] = {
                            "keyword": kw, "volume": row.get("volume") or 0,
                            "traffic_potential": row.get("traffic_potential") or 0,
                            "difficulty": row.get("difficulty"),
                            "cpc_cents": int(float(row.get("cpc") or 0) * 100),
                            "parent_topic": row.get("parent_keyword"),
                            "volume_history": _history_from_connector(row),
                            "trend_3m": row.get("growth_3mo"), "trend_6m": row.get("growth_6mo"),
                            "position": pos, "ranking_url": None, "sources": {seed},
                        }
                except Exception as exc:
                    warning = f"{label} ideas failed for '{seed}': {exc}"
                    warnings.append(warning); print(warning)

        if successful_calls == 0:
            raise RuntimeError("All Ahrefs Discovery calls failed. " + (warnings[0] if warnings else "Check connector approval."))

        # Position can be present on ideas exports. Exact ranking lookup adds URLs and
        # fills positions where the ideas endpoint did not return one.
        rankings, rank_warnings, _ = _rankings_for_keywords(list(all_keywords), target_site, country, job_id)
        warnings.extend(rank_warnings)
        excluded, filtered = 0, {}
        for kw, data in all_keywords.items():
            rank = rankings.get(kw)
            if rank:
                data["position"] = rank.get("position")
                data["ranking_url"] = rank.get("url")
            if data.get("position") is not None and data["position"] < min_position:
                excluded += 1
                continue
            filtered[kw] = data

        summary = {"seeds_used": len(seeds), "warnings": warnings, "warning_count": len(warnings)}
        sid, total, new_count = _save_session("discovery", filters, filtered, excluded, summary)
        message = f"Done! {total} keywords ({new_count} new, {excluded} excluded)"
        if warnings: message += f" · {len(warnings)} warning(s)"
        _jobs[job_id].update(status="completed", session_id=sid, progress=message, warnings=warnings)
    except Exception as exc:
        _jobs[job_id].update(status="failed", progress=f"Ahrefs Discovery failed: {exc}", error=traceback.format_exc())
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
        filters=settings.get("filters",{}); country=settings.get("target_country","us"); target_site=settings.get("target_site","")
        own_brand=target_site.split(".")[0].lower() if target_site else ""; min_position=filters.get("min_position",41); min_volume=filters.get("min_volume",100)
        all_gap_kws,warnings,successful_calls={},[],0
        for i,comp in enumerate(competitors):
            _jobs[job_id]["progress"]=f"Fetching keywords from {comp} ({i+1}/{len(competitors)})…"
            try:
                result=_invoke_connector("ahrefs_site_explorer.organic_keywords", {"target":comp,"country":country,"mode":"subdomains","limit":min(1000,int(kw_per_competitor)),"order_by":"traffic","direction":"desc","filters":{"min_volume":min_volume or 0}})
                successful_calls+=1
                for row in result.get("records",[]):
                    keyword=(row.get("keyword") or "").strip()
                    if not keyword: continue
                    item=all_gap_kws.setdefault(keyword,{"keyword":keyword,"volume":row.get("volume") or 0,"difficulty":row.get("difficulty"),"cpc_cents":row.get("cpc") or 0,"competitors":[]})
                    item["competitors"].append({"domain":comp,"position":row.get("position"),"url":row.get("url") or "","traffic":row.get("traffic") or 0})
                    item["volume"]=max(item.get("volume") or 0,row.get("volume") or 0)
            except Exception as exc:
                warning=f"Competitor {comp} failed: {exc}";warnings.append(warning);print(warning)
        if successful_calls==0: raise RuntimeError("All Ahrefs Content Gap calls failed. "+(warnings[0] if warnings else "Check connector approval."))

        rankings,rank_warnings,_=_rankings_for_keywords(list(all_gap_kws),target_site,country,job_id);warnings.extend(rank_warnings)
        metrics,metric_warnings,_=_overview_terms(list(all_gap_kws),country,job_id);warnings.extend(metric_warnings)
        final_keywords={};excluded=0;exclude_terms=[t.lower() for t in filters.get("exclude_terms",[])]
        for kw,data in all_gap_kws.items():
            rank=rankings.get(kw)
            if rank and rank.get("position") is not None and rank["position"]<min_position: excluded+=1;continue
            data["position"]=rank.get("position") if rank else None;data["ranking_url"]=rank.get("url") if rank else None
            metric=metrics.get(kw,{})
            if metric:
                data["volume"]=metric.get("volume") or data.get("volume") or 0;data["difficulty"]=metric.get("difficulty") if metric.get("difficulty") is not None else data.get("difficulty")
            intents={str(v).lower() for v in (metric.get("intents") or [])};category=metric.get("category")
            filter_row={"volume":data.get("volume") or 0,"difficulty":data.get("difficulty"),"attrs":{"branded":"branded" in intents,"local":"local" in intents},"categories":{"category":[category] if category else []}}
            if exclude_terms and any(t in kw.lower() for t in exclude_terms): excluded+=1;continue
            if not _apply_keyword_filters(kw,filter_row,filters,own_brand): excluded+=1;continue
            data.update({"traffic_potential":metric.get("traffic_potential") or 0,"parent_topic":metric.get("parent_keyword"),"trend_3m":metric.get("growth_3mo"),"trend_6m":metric.get("growth_6mo"),"volume_history":_history_from_connector(metric)})
            if metric.get("cpc") is not None:data["cpc_cents"]=int(float(metric["cpc"])*100)
            final_keywords[kw]=data
        summary={"competitors_analyzed":len(competitors),"competitors":competitors,"warnings":warnings,"warning_count":len(warnings)}
        sid,total,new_count=_save_session("content_gap",filters,final_keywords,excluded,summary)
        msg=f"Done! {total} gap keywords ({new_count} new, {excluded} filtered out)"+(f" · {len(warnings)} warning(s)" if warnings else "")
        _jobs[job_id].update(status="completed",session_id=sid,progress=msg,warnings=warnings)
    except Exception as exc:
        _jobs[job_id].update(status="failed",progress=f"Ahrefs Content Gap failed: {exc}",error=traceback.format_exc());print(f"Content gap job failed: {traceback.format_exc()}")


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
    try:
        from urllib.parse import urlparse
        filters = settings.get("filters", {})
        country = settings.get("target_country", "us")
        target_site = settings.get("target_site", "")
        if not target_site:
            raise RuntimeError("Configure a target site first.")
        own_brand = target_site.split(".")[0].lower()
        min_volume = filters.get("min_volume", 100)
        exclude_terms = [t.lower() for t in filters.get("exclude_terms", [])]
        scope_mode = settings.get("breakout_scope_mode") or "whole_site"
        raw_paths = settings.get("breakout_paths") or []
        content_paths = []
        for value in raw_paths:
            path = "/" + str(value).strip().strip("/") + "/"
            if path != "//" and path not in content_paths:
                content_paths.append(path)
        if scope_mode == "paths" and not content_paths:
            raise RuntimeError("Add at least one content path in Settings, or switch Breakout scope to Whole site.")

        def path_of(url):
            try: return urlparse(url).path or "/"
            except Exception: return "/"
        def in_content_scope(url):
            path = path_of(url)
            return any(path.startswith(prefix) for prefix in content_paths)

        warnings, candidates, successful_calls = [], {}, 0
        scopes = content_paths if scope_mode == "paths" else [None]
        for idx, scope in enumerate(scopes):
            label = scope or "whole site"
            try:
                target = (f"https://{target_site.strip('/')}{scope}" if scope else target_site)
                mode = "prefix" if scope else "subdomains"
                offset = 0
                while offset < 5000:
                    _jobs[job_id]["progress"] = (
                        f"Fetching {label} keywords ranking 31–100: "
                        f"{len(candidates):,} loaded…"
                    )
                    result = _invoke_connector("ahrefs_site_explorer.organic_keywords", {
                        "target": target, "mode": mode, "country": country,
                        "limit": 1000, "offset": offset,
                        "order_by": "position", "direction": "asc",
                        "filters": {"min_position": 31, "max_position": 100,
                                    "min_volume": min_volume or 0},
                    })
                    successful_calls += 1
                    batch = result.get("records", [])
                    for row in batch:
                        kw = (row.get("keyword") or "").strip()
                        if not kw:
                            continue
                        candidate = {
                            "keyword": kw, "volume": row.get("volume") or 0,
                            "difficulty": row.get("difficulty"),
                            # organic_keywords reports CPC in cents already.
                            "cpc_cents": int(row.get("cpc") or 0),
                            "position": row.get("position"),
                            "ranking_url": row.get("url") or "",
                            "content_scope": scope or "whole_site",
                        }
                        existing = candidates.get(kw)
                        if not existing or (candidate.get("position") or 999) < (existing.get("position") or 999):
                            candidates[kw] = candidate
                    if len(batch) < 1000:
                        break
                    offset += 1000
            except Exception as exc:
                warning = f"Breakout scope {label} failed: {exc}"
                warnings.append(warning)
                print(warning)
        if successful_calls == 0:
            raise RuntimeError("All Ahrefs Breakout calls failed. " + (warnings[0] if warnings else "Check connector approval."))
        if not candidates:
            summary = {"breakout_count": 0, "cannibalization_count": 0, "scope_mode": scope_mode,
                       "content_paths": content_paths, "warnings": warnings, "warning_count": len(warnings)}
            sid, total, new_count = _save_session("breakout", filters, {}, 0, summary)
            _jobs[job_id].update(status="completed", session_id=sid,
                progress="Done! No keywords matched positions 31–100.", warnings=warnings)
            return

        _jobs[job_id]["progress"] = f"Cross-checking {len(candidates)} keywords against the domain…"
        domain_rankings, rank_warnings, rank_successes = _rankings_for_keywords(list(candidates), target_site, country, job_id)
        warnings.extend(rank_warnings)
        if rank_successes == 0 and rank_warnings:
            raise RuntimeError("All domain cross-check calls failed. " + rank_warnings[0])
        metrics, metric_warnings, _ = _overview_terms(list(candidates), country, job_id)
        warnings.extend(metric_warnings)

        final_keywords, excluded = {}, 0
        for kw, data in candidates.items():
            if exclude_terms and any(t in kw.lower() for t in exclude_terms): excluded += 1; continue
            metric = metrics.get(kw, {})
            intents = {str(v).lower() for v in (metric.get("intents") or [])}
            category = metric.get("category")
            filter_row = {"volume": data.get("volume") or 0, "difficulty": data.get("difficulty"),
                "attrs": {"branded": "branded" in intents, "local": "local" in intents},
                "categories": {"category": [category] if category else []}}
            if not _apply_keyword_filters(kw, filter_row, filters, own_brand): excluded += 1; continue

            other = None
            for pos in (domain_rankings.get(kw, {}).get("urls") or []):
                url, position = pos.get("url") or "", pos.get("position")
                if not url or url == data["ranking_url"] or position is None: continue
                outside_scope = not in_content_scope(url) if scope_mode == "paths" else True
                better_page = position < (data.get("position") or 999)
                if outside_scope and better_page and (not other or position < other["position"]):
                    other = {"url": url, "position": position}
            status = "cannibalization" if other else ("breakout" if scope_mode == "paths" else "opportunity")
            final_keywords[kw] = {"keyword": kw, "volume": data["volume"],
                "traffic_potential": metric.get("traffic_potential") or 0, "difficulty": data["difficulty"],
                "cpc_cents": int(float(metric.get("cpc") or 0)*100) if metric.get("cpc") is not None else data.get("cpc_cents") or 0,
                "position": data["position"], "ranking_url": data["ranking_url"],
                "parent_topic": metric.get("parent_keyword"), "trend_3m": metric.get("growth_3mo"),
                "trend_6m": metric.get("growth_6mo"), "volume_history": _history_from_connector(metric),
                "extra": {"status": status, "scope_mode": scope_mode, "content_scope": data.get("content_scope"),
                          "other_url": other["url"] if other else None, "other_position": other["position"] if other else None},
                "sources": set()}
        primary_count = sum(1 for d in final_keywords.values() if d["extra"]["status"] in ("breakout", "opportunity"))
        cannibal_count = len(final_keywords) - primary_count
        summary = {"breakout_count": primary_count, "cannibalization_count": cannibal_count,
                   "scope_mode": scope_mode, "content_paths": content_paths,
                   "warnings": warnings, "warning_count": len(warnings)}
        sid, total, new_count = _save_session("breakout", filters, final_keywords, excluded, summary)
        primary_label = "breakout" if scope_mode == "paths" else "opportunity"
        msg = f"Done! {primary_count} {primary_label} + {cannibal_count} cannibalization ({excluded} filtered)"
        if warnings: msg += f" · {len(warnings)} warning(s)"
        _jobs[job_id].update(status="completed", session_id=sid, progress=msg, warnings=warnings)
    except Exception as exc:
        _jobs[job_id].update(status="failed", progress=f"Ahrefs Breakout failed: {exc}", error=traceback.format_exc())
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
            "SELECT id, COALESCE(completed_at, started_at) AS observed_at FROM kr_sessions WHERE tab = %s AND status = 'completed' ORDER BY started_at DESC LIMIT 1",
            (tab,),
        )
        row = cur.fetchone()
        if row:
            session_ids.append(row["id"])
            session_map[row["id"]] = {"tab": tab, "observed_at": row.get("observed_at")}

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
    merged = {}  # keyword -> lossless merged row
    for row in all_rows:
        kw = row["keyword"]
        session_info = session_map.get(row["session_id"], {})
        tab = session_info.get("tab", "")
        label = TAB_LABELS.get(tab, tab)
        observed_at = session_info.get("observed_at")
        extra = row.get("extra") or {}
        if isinstance(extra, str):
            try: extra = json.loads(extra)
            except Exception: extra = {}
        competitors = row.get("competitors") or []
        if isinstance(competitors, str):
            try: competitors = json.loads(competitors)
            except Exception: competitors = []

        evidence = {
            "source": label, "observed_at": observed_at.isoformat() if observed_at else None,
            "volume": row.get("volume"), "traffic_potential": row.get("traffic_potential"),
            "difficulty": row.get("difficulty"), "position": row.get("position"),
            "ranking_url": row.get("ranking_url"), "parent_topic": row.get("parent_topic"),
            "trend_3m": row.get("trend_3m"), "trend_6m": row.get("trend_6m"),
            "volume_history": row.get("volume_history"), "competitors": competitors,
            "extra": extra,
        }
        if kw not in merged:
            merged[kw] = dict(row)
            merged[kw].update({
                "tabs": [label], "tab_count": 1, "source_evidence": {label: evidence},
                "action_signals": [], "_freshest_at": observed_at,
                "trend_source": label if row.get("trend_3m") is not None or row.get("trend_6m") is not None else None,
            })
        else:
            item = merged[kw]
            if label not in item["tabs"]:
                item["tabs"].append(label)
                item["tab_count"] = len(item["tabs"])
            item["source_evidence"][label] = evidence
            # Best value per metric.
            if (row.get("volume") or 0) > (item.get("volume") or 0):
                item["volume"] = row.get("volume")
            if (row.get("traffic_potential") or 0) > (item.get("traffic_potential") or 0):
                item["traffic_potential"] = row.get("traffic_potential")
            if row.get("position") is not None and (item.get("position") is None or row["position"] < item["position"]):
                item["position"] = row["position"]
                item["ranking_url"] = row.get("ranking_url")
            # Freshest non-null contextual metrics.
            freshest = item.get("_freshest_at")
            is_fresher = observed_at is not None and (freshest is None or observed_at >= freshest)
            if is_fresher:
                item["_freshest_at"] = observed_at
                for field in ("difficulty", "cpc_cents", "parent_topic", "parent_topic_kd"):
                    if row.get(field) is not None: item[field] = row.get(field)
            if (row.get("trend_3m") is not None or row.get("trend_6m") is not None) and is_fresher:
                item["trend_3m"] = row.get("trend_3m")
                item["trend_6m"] = row.get("trend_6m")
                item["volume_history"] = row.get("volume_history")
                item["trend_source"] = label

        # Preserve every source-specific decision signal separately.
        item = merged[kw]
        if tab == "targets":
            pos = row.get("position")
            category = "update" if pos is not None and 3 <= pos <= 10 else "rewrite" if pos is None or pos >= 11 else None
            if category:
                item["action_signals"].append({"type": category, "source": label,
                    "label": "Update" if category == "update" else "Rewrite"})
            item["position_change_30d"] = extra.get("pos_delta_30d")
            item["previous_position"] = extra.get("prev_position")
            item["tracker_tags"] = extra.get("tags") or []
        elif tab == "breakout":
            status = extra.get("status")
            if status in ("breakout", "opportunity", "cannibalization"):
                item["action_signals"].append({"type": status, "source": label,
                    "label": "Breakout" if status == "breakout" else "Opportunity" if status == "opportunity" else "Cannibalization"})
            item["other_url"] = extra.get("other_url")
            item["other_position"] = extra.get("other_position")
        elif tab == "content_gap":
            item["gap_competitors"] = competitors

    # Internal merge bookkeeping must not leak through the API.
    for item in merged.values():
        item.pop("_freshest_at", None)

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
        "project_name": settings.get("rank_tracker_project_name") or "",
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
            category = (
                "update" if cur_pos is not None and 3 <= cur_pos <= 10
                else "rewrite" if cur_pos is None or cur_pos >= 11
                else None
            )
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
        rewrite_count = sum(1 for d in final_keywords.values() if d["extra"]["category"] == "rewrite")
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
    update_model = settings.get("enrichment_bp_model") or _kr_tiers.DEFAULT_BP_MODEL
    update_count = max(missing_tier, missing_bp)
    per_6500 = {
        "anthropic/claude-opus-5": 1.25,
        "anthropic/claude-sonnet-5": 0.50,
        "anthropic/claude-haiku-4.5": 0.25,
        "openai/gpt-5.6-sol": 1.50,
        "openai/gpt-5.6-luna": 0.15,
    }.get(update_model, 1.25)
    estimated_cost = round(max(0.01, (missing_bp / 6500) * per_6500 + (missing_tier / 6500) * 0.01), 2) if update_count else 0
    model_label = _kr_tiers.BP_MODELS.get(update_model, update_model)
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
        "update_count": update_count,
        "update_model": update_model,
        "update_model_label": model_label,
        "update_estimated_cost": estimated_cost,
        "bp_models": [{"id": k, "label": v} for k, v in _kr_tiers.BP_MODELS.items()],
        "default_bp_model": _kr_tiers.DEFAULT_BP_MODEL,
        "enrichment_product": settings.get("enrichment_product") or "",
        "enrichment_bp_model": settings.get("enrichment_bp_model") or _kr_tiers.DEFAULT_BP_MODEL,
        "enrichment_core_config": settings.get("enrichment_core_config") or {},
    })


@blueprint.route("/api/master/update-enrichment", methods=["POST"])
def master_update_enrichment():
    """Update only missing Tier/BP rows using the frozen wizard model/config."""
    settings = _load_settings()
    product = (settings.get("enrichment_product") or "").strip()
    model = settings.get("enrichment_bp_model") or _kr_tiers.DEFAULT_BP_MODEL
    if not settings.get("setup_completed") or not product:
        return jsonify({"error": "Complete the setup wizard before updating Master List classifications."}), 422
    status = _kr_tiers.tier_file_status()
    if not status.get("exists"):
        return jsonify({"error": "The saved tier model is missing. Rerun the setup wizard."}), 422
    config = {
        "mode": "update", "bp": True, "bp_model": model, "product": product,
        "include_nope": False, "threshold_mode": "fixed",
        # The update worker intentionally ignores mutable Manage tiers controls
        # and reads the frozen core + centroids from keyword_tiers.json.
        "core_source": "frozen", "core_lists": [], "project_id": "", "core_keywords": [], "k": 0,
    }
    run_id = _kr_tiers.launch_run(config)
    return jsonify({"run_id": run_id})


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
        for key, value in (
            ("enrichment_product", config["product"]),
            ("enrichment_bp_model", config["bp_model"]),
            ("enrichment_core_config", {
                "core_source": config["core_source"], "core_lists": config["core_lists"],
                "project_id": config["project_id"], "core_keywords": config["core_keywords"],
            }),
        ):
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
