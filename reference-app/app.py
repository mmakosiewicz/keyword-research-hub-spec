"""Standalone Keyword Research Hub reference app.

Security defaults:
- binds to 127.0.0.1
- refuses to start without SECRET_KEY and authentication configuration
- 10 MB upload cap
- CSRF on state-changing requests
- PostgreSQL only
- no user-supplied URL fetching
- model endpoint/key only from environment variables
"""
from __future__ import annotations
import csv, io, os, secrets, subprocess, sys, uuid
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, Response, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.dialects.postgresql import JSONB
from werkzeug.security import check_password_hash

def required_env(name: str) -> str:
    value=os.getenv(name,"").strip()
    if not value: raise RuntimeError(f"{name} is required; see .env.example")
    return value

app=Flask(__name__)
app.config.update(SECRET_KEY=required_env("SECRET_KEY"),SQLALCHEMY_DATABASE_URI=required_env("DATABASE_URL"),SQLALCHEMY_TRACK_MODIFICATIONS=False,MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB","10"))*1024*1024,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE","true").lower()=="true")
csrf=CSRFProtect(app); db=SQLAlchemy(app)
ALLOW_PROXY_AUTH=os.getenv("ALLOW_PROXY_AUTH","0")=="1"; APP_USERNAME=os.getenv("APP_USERNAME","").strip(); APP_PASSWORD_HASH=os.getenv("APP_PASSWORD_HASH","").strip()
if not ALLOW_PROXY_AUTH and (not APP_USERNAME or not APP_PASSWORD_HASH): raise RuntimeError("Set APP_USERNAME + APP_PASSWORD_HASH, or explicitly set ALLOW_PROXY_AUTH=1 behind trusted auth")

class Keyword(db.Model):
    __tablename__="krh_keywords"
    id=db.Column(db.Integer,primary_key=True); keyword=db.Column(db.String(500),nullable=False,unique=True,index=True)
    volume=db.Column(db.Integer); difficulty=db.Column(db.Integer); position=db.Column(db.Integer); traffic_potential=db.Column(db.Integer)
    ranking_url=db.Column(db.Text); source=db.Column(db.String(100)); list_name=db.Column(db.String(40)); is_core=db.Column(db.Boolean,nullable=False,default=False)
    tier=db.Column(db.Integer); bp=db.Column(db.Integer); cluster_id=db.Column(db.Integer); cluster=db.Column(db.String(200)); distance=db.Column(db.Float)
    updated_at=db.Column(db.DateTime(timezone=True),nullable=False,default=lambda:datetime.now(timezone.utc))

class TierRun(db.Model):
    __tablename__="krh_tier_runs"
    id=db.Column(db.String(32),primary_key=True); status=db.Column(db.String(20),nullable=False,default="queued"); step=db.Column(db.String(40))
    progress=db.Column(JSONB,nullable=False,default=dict); config=db.Column(JSONB,nullable=False,default=dict); summary=db.Column(JSONB); error=db.Column(db.Text)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=lambda:datetime.now(timezone.utc)); finished_at=db.Column(db.DateTime(timezone=True))

class RunRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    cluster_count:int=Field(default=0,ge=0,le=50); threshold_mode:str=Field(default="fixed",pattern="^(fixed|percentile)$")
    score_bp:bool=True; product:str=Field(default="",max_length=2000); bp_model:str=Field(default="anthropic/claude-opus-5",max_length=100)

ALLOWED_MODELS={"anthropic/claude-opus-5","anthropic/claude-sonnet-5","anthropic/claude-haiku-4.5","openai/gpt-5.6-sol","openai/gpt-5.6-luna"}
def unauthorized(): return Response("Authentication required",401,{"WWW-Authenticate":'Basic realm="Keyword Research Hub"'})
def auth_required(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        if ALLOW_PROXY_AUTH:
            if request.headers.get("X-Authenticated-User"): return fn(*args,**kwargs)
            return unauthorized()
        auth=request.authorization
        if not auth or not secrets.compare_digest(auth.username or "",APP_USERNAME): return unauthorized()
        if not check_password_hash(APP_PASSWORD_HASH,auth.password or ""): return unauthorized()
        return fn(*args,**kwargs)
    return wrapped
@app.before_request
def enforce_auth():
    if request.endpoint=="static": return None
    return auth_required(lambda:None)()
@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"]="nosniff"; response.headers["X-Frame-Options"]="DENY"; response.headers["Referrer-Policy"]="same-origin"; response.headers["Permissions-Policy"]="camera=(), microphone=(), geolocation=()"; response.headers["Cache-Control"]="no-store"
    response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.is_secure: response.headers["Strict-Transport-Security"]="max-age=31536000; includeSubDomains"
    return response
@app.errorhandler(413)
def too_large(_): return jsonify(error="upload_too_large",message="Upload exceeds the configured limit."),413
@app.route("/")
def index(): return render_template("index.html")
@app.get("/api/keywords")
def keywords():
    rows=Keyword.query.order_by(Keyword.volume.desc().nullslast(),Keyword.keyword).limit(25000).all()
    return jsonify([{"id":r.id,"keyword":r.keyword,"volume":r.volume,"difficulty":r.difficulty,"position":r.position,"traffic_potential":r.traffic_potential,"ranking_url":r.ranking_url,"source":r.source,"list":r.list_name,"is_core":r.is_core,"tier":r.tier,"bp":r.bp,"cluster_id":r.cluster_id,"cluster":r.cluster,"distance":r.distance} for r in rows])
@app.post("/api/import")
def import_csv():
    file=request.files.get("file")
    if not file or not file.filename.lower().endswith(".csv"): return jsonify(error="invalid_file",message="Upload a .csv file."),422
    raw=file.read()
    if b"\x00" in raw: return jsonify(error="invalid_file",message="Binary content is not allowed."),422
    try: reader=csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    except UnicodeDecodeError: return jsonify(error="invalid_encoding",message="CSV must be UTF-8."),422
    columns={str(c).strip().lower():c for c in (reader.fieldnames or [])}
    if "keyword" not in columns: return jsonify(error="missing_column",message="CSV needs a keyword column."),422
    count=0
    for source_row in reader:
        if count>=100000: return jsonify(error="row_limit",message="Maximum 100,000 rows per import."),422
        kw=(source_row.get(columns["keyword"]) or "").strip()
        if not kw or len(kw)>500: continue
        row=Keyword.query.filter_by(keyword=kw.lower()).first() or Keyword(keyword=kw.lower())
        for src,attr in (("volume","volume"),("kd","difficulty"),("difficulty","difficulty"),("position","position"),("traffic_potential","traffic_potential")):
            if src in columns:
                value=(source_row.get(columns[src]) or "").replace(",","").strip()
                if value.isdigit(): setattr(row,attr,int(value))
        if "url" in columns: row.ranking_url=(source_row.get(columns["url"]) or "")[:4000]
        if "source" in columns: row.source=(source_row.get(columns["source"]) or "")[:100]
        if "core" in columns: row.is_core=(source_row.get(columns["core"]) or "").lower() in {"1","true","yes","y"}
        db.session.add(row); count+=1
    db.session.commit(); return jsonify(ok=True,imported=count)
@app.post("/api/tiers/run")
def start_run():
    try: body=RunRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc: return jsonify(error="validation_error",details=exc.errors(include_url=False)),422
    if body.score_bp and not body.product.strip(): return jsonify(error="product_required",message="Describe the product before BP scoring."),422
    if body.bp_model not in ALLOWED_MODELS: return jsonify(error="invalid_model"),422
    if Keyword.query.filter_by(is_core=True).count()<4: return jsonify(error="core_too_small",message="Mark at least four keywords as core first."),422
    if Keyword.query.count()<4: return jsonify(error="bank_too_small"),422
    run_id=uuid.uuid4().hex[:16]; db.session.add(TierRun(id=run_id,config=body.model_dump())); db.session.commit()
    log_dir=os.path.join(app.instance_path,"runs"); os.makedirs(log_dir,mode=0o700,exist_ok=True); log=open(os.path.join(log_dir,f"{run_id}.log"),"ab",buffering=0)
    subprocess.Popen([sys.executable,os.path.join(app.root_path,"worker.py"),run_id],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
    return jsonify(run_id=run_id),202
@app.get("/api/tiers/run/<run_id>")
def run_status(run_id):
    if not run_id.isalnum() or len(run_id)>32: return jsonify(error="not_found"),404
    run=db.session.get(TierRun,run_id)
    if not run: return jsonify(error="not_found"),404
    return jsonify(id=run.id,status=run.status,step=run.step,progress=run.progress,summary=run.summary,error=run.error)
@app.post("/api/keywords/<int:keyword_id>/core")
def set_core(keyword_id):
    row=db.session.get(Keyword,keyword_id)
    if not row: return jsonify(error="not_found"),404
    payload=request.get_json(silent=True) or {}
    if set(payload)!={"core"} or not isinstance(payload["core"],bool): return jsonify(error="validation_error"),422
    row.is_core=payload["core"]; db.session.commit(); return jsonify(ok=True)
with app.app_context(): db.create_all()
if __name__=="__main__": app.run(host=os.getenv("HOST","127.0.0.1"),port=int(os.getenv("PORT","5000")),debug=False,threaded=True)
