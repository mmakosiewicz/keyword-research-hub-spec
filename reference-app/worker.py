"""Detached tier-generation worker. Run only as: python worker.py <run_id>."""
from __future__ import annotations
import json,os,sys,time
from datetime import datetime,timezone
import numpy as np,requests
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from app import ALLOWED_MODELS,Keyword,TierRun,app,db
BASE_URL=os.getenv("OPENAI_BASE_URL","").rstrip("/"); API_KEY=os.getenv("OPENAI_API_KEY",""); EMBED_MODEL=os.getenv("EMBED_MODEL",""); LABEL_MODEL=os.getenv("LABEL_MODEL","anthropic/claude-haiku-4.5")
def require_model_env():
    missing=[k for k,v in {"OPENAI_BASE_URL":BASE_URL,"OPENAI_API_KEY":API_KEY,"EMBED_MODEL":EMBED_MODEL}.items() if not v]
    if missing: raise RuntimeError("Missing model configuration: "+", ".join(missing))
    if not BASE_URL.startswith("https://") and not BASE_URL.startswith("http://127.0.0.1"): raise RuntimeError("OPENAI_BASE_URL must use HTTPS (or loopback for local development)")
def update(run_id,**fields):
    run=db.session.get(TierRun,run_id)
    if not run: raise RuntimeError("run not found")
    for key,value in fields.items(): setattr(run,key,value)
    db.session.commit()
def post(path,payload,timeout=(10,120)):
    r=requests.post(f"{BASE_URL}{path}",headers={"Authorization":f"Bearer {API_KEY}","Connection":"close"},json=payload,timeout=timeout,allow_redirects=False); r.raise_for_status(); return r.json()
def embeddings(texts,run_id):
    vectors=[]
    for i in range(0,len(texts),100):
        chunk=[t.replace("\n"," ")[:500] for t in texts[i:i+100]]; last=None
        for attempt in range(4):
            try: vectors.extend(item["embedding"] for item in post("/embeddings",{"model":EMBED_MODEL,"input":chunk})["data"]); last=None; break
            except Exception as exc: last=exc; time.sleep(2*(attempt+1))
        if last: raise last
        update(run_id,progress={"done":min(i+100,len(texts)),"total":len(texts),"step":"embed"})
    arr=np.asarray(vectors,dtype=np.float64); return arr/np.linalg.norm(arr,axis=1,keepdims=True)
def chat(model,prompt,max_tokens=2000):
    payload=post("/chat/completions",{"model":model,"max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]}); text=payload["choices"][0]["message"]["content"].strip()
    if text.startswith("```"): text=text.split("```",2)[1].removeprefix("json").strip()
    return json.loads(text)
def choose_k(core,requested):
    if requested: return min(requested,len(core))
    upper=min(30,max(4,len(core)//8))
    if upper<=4: return min(4,len(core))
    sample_idx=np.random.RandomState(42).choice(len(core),min(1500,len(core)),replace=False); best=(4,-1.0)
    for k in range(4,upper+1,2):
        km=KMeans(n_clusters=k,n_init=4,random_state=42).fit(core); score=silhouette_score(core[sample_idx],km.labels_[sample_idx])
        if score>best[1]: best=(k,score)
    return best[0]
def bp_scores(keywords,product,model,run_id):
    scores={}; instruction=f"Score each keyword for business potential for this product:\n{product}\n\n3 = product is an irreplaceable solution; 2 = strongly helpful; 1 = tangentially relevant; 0 = no realistic pitch.\nReturn ONLY a JSON object mapping every exact keyword to integer 0-3."
    for i in range(0,len(keywords),50):
        chunk=keywords[i:i+50]
        try: result=chat(model,instruction+"\n\n"+"\n".join(chunk))
        except Exception: result={}
        for kw in chunk:
            value=result.get(kw); scores[kw]=int(value) if str(value) in {"0","1","2","3"} else None
        update(run_id,progress={"done":min(i+50,len(keywords)),"total":len(keywords),"step":"bp"})
    return scores
def run(run_id):
    require_model_env(); run_row=db.session.get(TierRun,run_id)
    if not run_row: raise RuntimeError("run not found")
    cfg=run_row.config
    if cfg.get("bp_model") not in ALLOWED_MODELS: raise RuntimeError("invalid model")
    rows=Keyword.query.order_by(Keyword.id).all(); core_rows=[r for r in rows if r.is_core]
    if len(core_rows)<4: raise RuntimeError("core set too small")
    update(run_id,status="running",step="embed"); vecs=embeddings([r.keyword for r in rows],run_id); row_index={r.id:i for i,r in enumerate(rows)}; core_idx=[row_index[r.id] for r in core_rows]; core_vecs=vecs[core_idx]
    update(run_id,step="cluster",progress={}); k=choose_k(core_vecs,int(cfg.get("cluster_count") or 0)); km=KMeans(n_clusters=k,n_init=10,random_state=42).fit(core_vecs); centroids=km.cluster_centers_/np.linalg.norm(km.cluster_centers_,axis=1,keepdims=True)
    reps={}
    for cid in range(k):
        members=np.where(km.labels_==cid)[0]; distance=1-core_vecs[members]@centroids[cid]; reps[cid]=[core_rows[members[i]].keyword for i in np.argsort(distance)[:8]]
    label_prompt="Name each cluster with a concise 2-5 word topic. Return ONLY JSON id->label.\n"+"\n".join(f"{cid}: {', '.join(items)}" for cid,items in reps.items())
    try: labels={int(key):str(value)[:200] for key,value in chat(LABEL_MODEL,label_prompt).items()}
    except Exception: labels={cid:f"Cluster {cid+1}" for cid in range(k)}
    update(run_id,step="tier"); sims=vecs@centroids.T; nearest=np.argmax(sims,axis=1); dist=1-sims[np.arange(len(rows)),nearest]; thresholds=np.percentile(dist,[75,90,95]) if cfg.get("threshold_mode")=="percentile" else np.array([.45,.60,.70]); tiers=np.select([dist<thresholds[0],dist<thresholds[1],dist<thresholds[2]],[1,2,3],default=4)
    bp={}
    if cfg.get("score_bp"): update(run_id,step="bp"); bp=bp_scores([r.keyword for r in rows],cfg.get("product",""),cfg["bp_model"],run_id)
    update(run_id,step="write")
    for i,row in enumerate(rows):
        cid=int(nearest[i]); row.cluster_id=cid; row.cluster=labels.get(cid,f"Cluster {cid+1}"); row.distance=round(float(dist[i]),4); row.tier=int(tiers[i]); row.bp=bp.get(row.keyword); row.updated_at=datetime.now(timezone.utc)
    db.session.commit(); counts={str(t):int(np.sum(tiers==t)) for t in (1,2,3,4)}
    update(run_id,status="completed",step="done",summary={"keywords":len(rows),"core":len(core_rows),"clusters":k,"tier_counts":counts,"thresholds":{"mode":cfg.get("threshold_mode"),"t1":float(thresholds[0]),"t2":float(thresholds[1]),"t3":float(thresholds[2])}},finished_at=datetime.now(timezone.utc))
if __name__=="__main__":
    rid=sys.argv[1] if len(sys.argv)==2 else ""
    if not rid.isalnum() or len(rid)>32: raise SystemExit("invalid run id")
    with app.app_context():
        try: run(rid)
        except Exception as exc: update(rid,status="failed",error=str(exc)[:1000],finished_at=datetime.now(timezone.utc)); raise
