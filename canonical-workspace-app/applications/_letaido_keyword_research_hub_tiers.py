"""Tier/cluster generator for Keyword Research Hub — helper + detached worker.

Pipeline (the "keyword universe" method):
  1. core set    — keywords defining what the site is about
  2. embed       — all keywords -> vectors (text-embedding-3-small via local proxy)
  3. cluster     — k-means over CORE embeddings; LLM labels each cluster
  4. tier        — distance of every keyword to nearest core centroid;
                   fixed cuts (default) or percentile cuts
  5. bp (opt.)   — LLM judge scores business potential 0-3 per keyword

Runs as a detached subprocess (survives Flask reloads); state in Postgres
kr_tier_runs. Output: /home/console/http/default/data/keyword_tiers.json
(read by both the Hub master list and the Keyword Universe app).
"""

import hashlib
import json
import os
import pwd
import subprocess
import sys
import time
import uuid
from datetime import datetime

import psycopg2
import psycopg2.extras
import requests

LLM_BASE = "http://127.0.0.1:18080/api/v1"
API_PROXY = "http://127.0.0.1:18081/capabilities/invoke"
EMBED_MODEL = "text-embedding-3-small"
LABEL_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_BP_MODEL = "anthropic/claude-opus-5"
BP_MODELS = {
    "anthropic/claude-opus-5": "Claude Opus 5 (best judgment — default)",
    "anthropic/claude-sonnet-5": "Claude Sonnet 5 (strong, 2.5× cheaper)",
    "anthropic/claude-haiku-4.5": "Claude Haiku 4.5 (fastest & cheapest)",
    "openai/gpt-5.6-sol": "ChatGPT Sol (GPT-5.6, deep-reasoning)",
    "openai/gpt-5.6-luna": "ChatGPT Luna (GPT-5.6, fast & very cheap)",
}

TIERS_OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "keyword_tiers.json"))
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "kr_tier_runs"))

TIER_LABELS = {"1": "Core orbit", "2": "Adjacent", "3": "Far orbit", "4": "Outside"}
DEFAULT_THRESHOLDS = (0.45, 0.60, 0.70)

STEPS = [
    ("core", "Collect core keywords"),
    ("candidates", "Collect candidate keywords"),
    ("embed", "Embed keywords"),
    ("cluster", "Cluster core & label topics"),
    ("tier", "Compute distances & assign tiers"),
    ("bp", "Score business potential"),
    ("write", "Write keyword_tiers.json"),
]


def get_db():
    user = pwd.getpwuid(os.getuid()).pw_name
    return psycopg2.connect(host="/var/run/postgresql", user=user, database="console_db")


def ensure_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kr_tier_runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            step TEXT,
            progress JSONB DEFAULT '{}'::jsonb,
            config JSONB DEFAULT '{}'::jsonb,
            summary JSONB,
            error TEXT,
            created_at TIMESTAMPTZ DEFAULT now(),
            finished_at TIMESTAMPTZ
        )
    """)
    conn.commit()
    conn.close()


def _update(run_id, **fields):
    conn = get_db()
    cur = conn.cursor()
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("progress", "summary", "config"):
            sets.append(f"{k} = %s::jsonb")
            vals.append(json.dumps(v))
        else:
            sets.append(f"{k} = %s")
            vals.append(v)
    vals.append(run_id)
    cur.execute(f"UPDATE kr_tier_runs SET {', '.join(sets)} WHERE id = %s", vals)
    conn.commit()
    conn.close()


def launch_run(config):
    """Insert run row + spawn detached worker. Returns run_id."""
    ensure_table()
    run_id = uuid.uuid4().hex[:12]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO kr_tier_runs (id, status, config) VALUES (%s, 'queued', %s::jsonb)",
        (run_id, json.dumps(config)),
    )
    conn.commit()
    conn.close()
    os.makedirs(LOG_DIR, exist_ok=True)
    log_fh = open(os.path.join(LOG_DIR, f"{run_id}.log"), "w")
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--run", run_id],
        start_new_session=True,
        stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    return run_id


def get_run(run_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM kr_tier_runs WHERE id = %s", (run_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
        row["finished_at"] = row["finished_at"].isoformat() if row["finished_at"] else None
    return row


def latest_run():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM kr_tier_runs ORDER BY created_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row:
        row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
        row["finished_at"] = row["finished_at"].isoformat() if row["finished_at"] else None
    return row


def tier_file_status():
    if not os.path.exists(TIERS_OUT):
        return {"exists": False}
    try:
        with open(TIERS_OUT) as f:
            d = json.load(f)
        mt = os.path.getmtime(TIERS_OUT)
        return {
            "exists": True,
            "keywords": len(d.get("results", [])),
            "clusters": len(d.get("clusters", [])),
            "mtime": datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"),
            "mtime_epoch": mt,
        }
    except Exception as e:
        return {"exists": True, "error": str(e)}


# ─── data collection ─────────────────────────────────────────────────

def _invoke(cap, args):
    r = requests.post(
        f"{API_PROXY}/{cap}",
        json={"caller": "app", "secret_name": "ahrefs_oauth", "args": args},
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "ok":
        raise RuntimeError(f"{cap}: {json.dumps(body)[:300]}")
    return body["result"]


def list_rank_tracker_projects():
    res = _invoke("ahrefs_rank_tracker.list_projects", {"limit": 200})
    return [
        {"id": p["id"], "name": p["name"], "target_url": p.get("target_url", ""),
         "keywords": p.get("number_of_keywords", 0)}
        for p in res.get("records", [])
    ]


def _core_from_rank_tracker(project_id):
    res = _invoke(
        "ahrefs_rank_tracker.overview_keywords_export",
        {"project_id": str(project_id), "limit": 10000},
    )
    kws = []
    for rec in res.get("records", []):
        kw = rec.get("keyword")
        if kw:
            kws.append(kw.strip())
    return kws


def _core_from_lists(list_names):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT keyword FROM kr_keyword_lists WHERE list_name = ANY(%s)",
        (list_names,),
    )
    kws = [r[0] for r in cur.fetchall()]
    conn.close()
    return kws


def _candidates_from_bank(include_nope=False):
    """Distinct keywords across the Hub's keyword bank, with best metrics."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT keyword,
               MAX(volume) AS volume,
               MAX(difficulty) AS kd,
               MIN(NULLIF(position, 0)) AS position,
               MAX(traffic_potential) AS traffic_potential,
               (array_agg(ranking_url ORDER BY volume DESC NULLS LAST))[1] AS url,
               string_agg(DISTINCT source, ', ') AS tabs
        FROM kr_results
        GROUP BY keyword
    """)
    rows = {r["keyword"].lower(): dict(r) for r in cur.fetchall()}
    cur.execute("SELECT keyword, list_name FROM kr_keyword_lists")
    lists = {r["keyword"].lower(): r["list_name"] for r in cur.fetchall()}
    conn.close()
    out = []
    for key, r in rows.items():
        lst = lists.get(key, "")
        if lst == "nope" and not include_nope:
            continue
        out.append({
            "keyword": r["keyword"],
            "list": lst,
            "volume": str(r["volume"] or ""),
            "kd": str(r["kd"] or ""),
            "position": str(r["position"] or ""),
            "url": r["url"] or "",
            "traffic_potential": str(r["traffic_potential"] or ""),
            "tabs": r["tabs"] or "",
        })
    return out


# ─── pipeline pieces ─────────────────────────────────────────────────

def _embed_batch(chunk):
    """One embeddings call with a hard wall-clock deadline (thread + join)."""
    import threading
    result, err = [], []

    def call():
        try:
            with requests.Session() as s:
                r = s.post(
                    f"{LLM_BASE}/embeddings",
                    json={"model": EMBED_MODEL, "input": chunk},
                    timeout=(10, 90),
                    headers={"Connection": "close"},
                )
                r.raise_for_status()
                result.extend(d["embedding"] for d in r.json()["data"])
        except Exception as e:
            err.append(e)

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(timeout=120)
    if t.is_alive():
        raise TimeoutError("embeddings call exceeded 120s wall clock")
    if err:
        raise err[0]
    return result


def _embed(texts, run_id):
    import numpy as np
    vecs = []
    for i in range(0, len(texts), 100):
        chunk = [t.replace("\n", " ")[:500] for t in texts[i:i + 100]]
        for attempt in range(4):
            try:
                print(f"[embed] batch {i}-{i+len(chunk)} attempt {attempt+1}", flush=True)
                vecs.extend(_embed_batch(chunk))
                break
            except Exception as e:
                print(f"[embed] batch {i} attempt {attempt+1} failed: {e}", flush=True)
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        _update(run_id, progress={"step": "embed", "done": min(i + 100, len(texts)), "total": len(texts)})
    a = np.asarray(vecs, dtype=np.float64)
    return a / np.linalg.norm(a, axis=1, keepdims=True)


def _chat(model, prompt, max_tokens=2000):
    r = requests.post(
        f"{LLM_BASE}/chat/completions",
        json={"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _cluster_and_label(core_vecs, core_kws, k):
    import numpy as np
    from sklearn.cluster import KMeans

    if not k:
        from sklearn.metrics import silhouette_score
        n = core_vecs.shape[0]
        kmax = min(30, max(4, n // 8))
        best_k, best_s = 4, -1
        if kmax > 4:
            idx = np.random.RandomState(42).choice(n, min(1500, n), replace=False)
            for kk in range(4, kmax + 1, 2):
                km = KMeans(n_clusters=kk, n_init=4, random_state=42).fit(core_vecs)
                s = silhouette_score(core_vecs[idx], km.labels_[idx])
                if s > best_s:
                    best_k, best_s = kk, s
        k = best_k
    k = min(k, core_vecs.shape[0])

    km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(core_vecs)
    centroids = km.cluster_centers_ / np.linalg.norm(km.cluster_centers_, axis=1, keepdims=True)
    reps = {}
    for c in range(k):
        members = np.where(km.labels_ == c)[0]
        d = 1 - core_vecs[members] @ centroids[c]
        order = members[np.argsort(d)][:8]
        reps[c] = [core_kws[i] for i in order]
    _cluster_and_label.last_reps = reps
    prompt = (
        "Name each keyword cluster with a short (2-5 word) topic label. "
        "Reply with ONLY a JSON object mapping cluster id to label.\n\n"
        + "\n".join(f'{c}: {", ".join(kws)}' for c, kws in sorted(reps.items()))
    )
    try:
        labels = {int(kk): str(v) for kk, v in _parse_json(_chat(LABEL_MODEL, prompt)).items()}
    except Exception:
        labels = {c: f"Cluster {c}" for c in reps}
    return centroids, labels, k


def _score_bp(keywords, product, run_id, model=None):
    """Score BP robustly with reasoning-model-safe output headroom.

    Returns a complete keyword->score mapping. Residual failures are explicit
    None values, logged by chunk, and surfaced in the run summary.
    """
    model = model or DEFAULT_BP_MODEL
    scores = {}
    system = (
        "You score keywords for business potential (BP) for this product:\n"
        f"{product}\n\n"
        "Scale:\n"
        "3 = the product is an irreplaceable solution for the search intent\n"
        "2 = the product helps a lot but isn't essential\n"
        "1 = the product is only tangentially relevant\n"
        "0 = no realistic way to pitch the product\n\n"
        "Reply with ONLY a valid JSON object mapping every exact keyword to its integer score. "
        "Do not omit keywords and do not add commentary."
    )
    chunk_size = 25
    for i in range(0, len(keywords), chunk_size):
        chunk = keywords[i:i + chunk_size]
        prompt = system + "\n\nKeywords:\n" + "\n".join(chunk)
        got = None
        for attempt in range(3):
            try:
                candidate = _parse_json(_chat(model, prompt, max_tokens=8000))
                if not isinstance(candidate, dict) or not candidate:
                    raise ValueError("model returned an empty/non-object JSON response")
                got = candidate
                break
            except Exception as exc:
                print(f"[bp] chunk {i}-{i+len(chunk)} attempt {attempt+1} failed: {exc}", flush=True)
        got = got or {}
        normalized = {str(key).strip().lower(): value for key, value in got.items()}
        missing = []
        for kw in chunk:
            value = got.get(kw)
            if value is None:
                value = normalized.get(kw.strip().lower())
            text = str(value).strip() if value is not None else ""
            score = int(text) if text in {"0", "1", "2", "3"} else None
            scores[kw] = score
            if score is None:
                missing.append(kw)
        if missing:
            print(f"[bp] chunk {i}-{i+len(chunk)} left {len(missing)} unscored: {missing[:5]}", flush=True)
        _update(run_id, progress={"step": "bp", "done": min(i + chunk_size, len(keywords)), "total": len(keywords)})
    return scores


# ─── worker main ─────────────────────────────────────────────────────

def run_pipeline(run_id):
    import numpy as np

    run = get_run(run_id)
    cfg = run["config"]
    mode = cfg.get("mode", "full")
    _update(run_id, status="running", step="core")

    # 1. Core set. Master List "update" uses the frozen model created by the
    # setup wizard; it never depends on the current Manage tiers controls.
    frozen_data = {}
    if mode == "update":
        if not os.path.exists(TIERS_OUT):
            raise RuntimeError("Complete the setup wizard before updating Master List classifications.")
        with open(TIERS_OUT) as f:
            frozen_data = json.load(f)
        frozen_meta = frozen_data.get("metadata") or {}
        if not frozen_meta.get("centroids"):
            raise RuntimeError("The saved model cannot be updated incrementally. Rerun the setup wizard once.")
        core_kws = [r.get("keyword", "") for r in frozen_data.get("results", []) if r.get("is_in_core")]
        core_hash = frozen_meta.get("core_hash")
    else:
        src = cfg.get("core_source", "lists")
        if src == "rank_tracker":
            core_kws = _core_from_rank_tracker(cfg["project_id"])
        elif src == "paste":
            core_kws = [k.strip() for k in cfg.get("core_keywords", []) if k.strip()]
        else:
            core_kws = _core_from_lists(cfg.get("core_lists", ["pitch", "backlog", "maybe"]))
        core_kws = sorted(set(core_kws), key=str.lower)
        if len(core_kws) < 4:
            raise RuntimeError(
                f"Core set too small ({len(core_kws)} keywords). Need at least 4 — "
                "add keywords to your lists, pick a Rank Tracker project, or paste more."
            )
        core_hash = hashlib.sha256("\n".join(k.lower() for k in core_kws).encode()).hexdigest()
    _update(run_id, step="candidates", progress={"core_count": len(core_kws)})

    # 2. Current keyword bank.
    cand_rows = _candidates_from_bank(include_nope=cfg.get("include_nope", False))
    universe = {r["keyword"].lower(): r for r in cand_rows}
    for kw in core_kws:
        universe.setdefault(kw.lower(), {"keyword": kw, "list": "", "volume": "", "kd": "",
                                         "position": "", "url": "", "traffic_potential": "", "tabs": ""})
    core_set = {k.lower() for k in core_kws}

    old_data, old_by_kw = {}, {}
    if mode in ("quick", "update"):
        if mode == "update":
            old_data = frozen_data
        else:
            if not os.path.exists(TIERS_OUT):
                raise RuntimeError("Run a Full rebuild before using Quick update.")
            with open(TIERS_OUT) as f:
                old_data = json.load(f)
        metadata = old_data.get("metadata") or {}
        if mode == "quick" and metadata.get("core_hash") != core_hash:
            raise RuntimeError("The core keyword set changed. Run a Full rebuild.")
        if not metadata.get("centroids"):
            raise RuntimeError("The saved model predates incremental updates. Run one Full rebuild.")
        old_by_kw = {r.get("keyword", "").strip().lower(): r for r in old_data.get("results", [])}
        keys = [key for key in universe if key not in old_by_kw or old_by_kw[key].get("tier") is None
                or (cfg.get("bp") and old_by_kw[key].get("bp") is None)]
        if not keys:
            _update(run_id, status="completed", step="done",
                    summary={"mode": mode, "keywords": 0, "message": "Everything is already enriched."},
                    finished_at=datetime.now())
            return
        centroids = np.asarray(metadata["centroids"], dtype=np.float64)
        k = len(centroids)
        clusters = old_data.get("clusters", {})
        labels = {int(cid): info.get("label", f"Cluster {cid}") for cid, info in clusters.items()}
        thresholds = old_data.get("thresholds") or {}
        t1, t2, t3 = float(thresholds.get("t1", .45)), float(thresholds.get("t2", .60)), float(thresholds.get("t3", .70))
        _update(run_id, step="embed", progress={"core_count": len(core_kws), "universe": len(keys)})
        vecs = _embed([universe[key]["keyword"] for key in keys], run_id)
    else:
        keys = list(universe.keys())
        _update(run_id, step="embed", progress={"core_count": len(core_kws), "universe": len(keys)})
        vecs = _embed([universe[key]["keyword"] for key in keys], run_id)
        idx = {key: i for i, key in enumerate(keys)}
        core_idx = [idx[key] for key in keys if key in core_set]
        core_vecs = vecs[core_idx]
        core_texts = [universe[keys[i]]["keyword"] for i in core_idx]
        _update(run_id, step="cluster")
        centroids, labels, k = _cluster_and_label(core_vecs, core_texts, int(cfg.get("k") or 0))

        # Full rebuild chooses fresh thresholds.
        all_sims = vecs @ centroids.T
        all_nearest = np.argmax(all_sims, axis=1)
        all_dist = 1 - all_sims[np.arange(len(keys)), all_nearest]
        if cfg.get("threshold_mode") == "percentile":
            t1, t2, t3 = np.percentile(all_dist, [75, 90, 95])
        else:
            t1, t2, t3 = cfg.get("thresholds") or DEFAULT_THRESHOLDS

    # 3. Apply saved/new semantic model to rows in scope.
    _update(run_id, step="tier")
    sims = vecs @ centroids.T
    nearest = np.argmax(sims, axis=1)
    dist = 1 - sims[np.arange(len(keys)), nearest]
    avg_dist = 1 - sims.mean(axis=1)
    tiers = np.select([dist < t1, dist < t2, dist < t3], [1, 2, 3], default=4)

    bp = {}
    if cfg.get("bp") and cfg.get("product"):
        _update(run_id, step="bp")
        bp_model = cfg.get("bp_model") or DEFAULT_BP_MODEL
        if bp_model not in BP_MODELS:
            bp_model = DEFAULT_BP_MODEL
        bp = _score_bp([universe[key]["keyword"] for key in keys], cfg["product"], run_id, bp_model)

    # 4. Merge quick rows into the existing enrichment; full replaces all rows.
    _update(run_id, step="write")
    updated = {}
    for i, key in enumerate(keys):
        row = universe[key]
        c = int(nearest[i])
        previous = old_by_kw.get(key, {})
        updated[key] = {
            "keyword": row["keyword"], "list": row.get("list", ""),
            "volume": row.get("volume", ""), "kd": row.get("kd", ""),
            "position": row.get("position", ""), "url": row.get("url", ""),
            "traffic_potential": row.get("traffic_potential", ""), "tabs": row.get("tabs", ""),
            "nearest_cluster": c, "nearest_cluster_name": labels.get(c, f"Cluster {c}"),
            "distance": round(float(dist[i]), 4), "avg_distance": round(float(avg_dist[i]), 4),
            "is_in_core": key in core_set, "tier": int(tiers[i]),
            "bp": bp.get(row["keyword"], previous.get("bp")),
        }
    if mode in ("quick", "update"):
        merged = {key: value for key, value in old_by_kw.items() if key in universe}
        merged.update(updated)
        results = list(merged.values())
    else:
        results = list(updated.values())

    cluster_sizes = {c: sum(1 for row in results if int(row.get("nearest_cluster", -1)) == c) for c in range(k)}
    previous_clusters = old_data.get("clusters", {}) if mode in ("quick", "update") else {}
    out = {
        "results": results,
        "clusters": {
            str(c): {"label": labels.get(c, f"Cluster {c}"), "size": cluster_sizes[c],
                     "representative": previous_clusters.get(str(c), {}).get("representative",
                        getattr(_cluster_and_label, "last_reps", {}).get(c, []))}
            for c in range(k)
        },
        "tier_labels": TIER_LABELS,
        "thresholds": {"mode": cfg.get("threshold_mode", "fixed"),
                       "t1": float(t1), "t2": float(t2), "t3": float(t3)},
        "metadata": {"core_hash": core_hash, "core_count": len(core_kws),
                     "centroids": np.asarray(centroids).tolist(),
                     "embedding_model": EMBED_MODEL,
                     "updated_at": datetime.now().isoformat()},
    }
    tmp = TIERS_OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, TIERS_OUT)
    try: os.chmod(TIERS_OUT, 0o664)
    except Exception: pass

    from collections import Counter
    tc = Counter(r["tier"] for r in results)
    summary = {"mode": mode, "keywords": len(keys), "total_keywords": len(results),
               "core": len(core_kws),
               "clusters": [{"id": c, "name": labels.get(c, ""), "size": cluster_sizes[c]} for c in range(k)],
               "tier_counts": {str(t): tc.get(t, 0) for t in (1, 2, 3, 4)},
               "thresholds": out["thresholds"],
               "bp_scored": sum(1 for v in bp.values() if v is not None),
               "bp_failed": sum(1 for v in bp.values() if v is None)}
    _update(run_id, status="completed", step="done", summary=summary,
            finished_at=datetime.now())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        rid = sys.argv[2]
        try:
            run_pipeline(rid)
        except Exception as e:
            import traceback
            traceback.print_exc()
            _update(rid, status="failed", error=str(e)[:500], finished_at=datetime.now())
            sys.exit(1)
