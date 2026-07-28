# Keyword Research Hub — standalone reference app

A sanitized, runnable reference implementation of the [Keyword Research Hub spec](../README.md).

It includes PostgreSQL persistence, CSV import, core-keyword monitoring, embeddings + k-means clustering, fixed or percentile tiers, optional LLM BP scoring, and an integrated Master List **Table / Topic map**.

No private workspace data, credentials, domains, project IDs, member identities, or generated keyword files are included.

## Security model

This app is deliberately fail-closed:

- refuses to start without `SECRET_KEY`, `DATABASE_URL`, and authentication;
- binds to `127.0.0.1` by default;
- authenticates every route (or uses an explicitly configured trusted auth proxy);
- protects state-changing routes with CSRF;
- caps uploads at 10 MB, UTF-8 CSV only, maximum 100,000 rows;
- uses PostgreSQL only;
- reads model endpoint/key only from environment variables;
- rejects model redirects and requires HTTPS, except loopback development;
- never fetches user-supplied ranking URLs;
- validates run identifiers;
- disables debug mode and sets security headers.

Read [`SECURITY.md`](./SECURITY.md) and [`PRIVACY.md`](./PRIVACY.md) before exposing it outside localhost.

## Quick start

Requirements: Python 3.12+, PostgreSQL 15+.

```bash
cd reference-app
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Create a PostgreSQL database/user, fill in `.env`, then:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('replace-me'))"
set -a; . ./.env; set +a
python app.py
```

Open `http://127.0.0.1:5000`, authenticate, and import [`sample-keywords.csv`](./sample-keywords.csv).

For a production-like local process:

```bash
gunicorn --bind 127.0.0.1:5000 --workers 1 --threads 8 --worker-class gthread app:app
```

Put Gunicorn behind an authenticated HTTPS reverse proxy. Do not expose Flask's development server.

## CSV input

Required: `keyword`.

Optional: `volume`, `kd` or `difficulty`, `position`, `traffic_potential`, `url`, `source`, and `core` (`true`, `yes`, `y`, or `1`).

At least four core keywords are required; 30+ representative terms is safer.

## Pipeline

1. Embed every keyword.
2. Cluster core keywords and label topics.
3. Calculate distance to the nearest core centroid.
4. Assign fixed tiers (default) or relative percentile tiers.
5. Optionally score business potential with the chosen model.
6. Explore the same filtered bank as a table or Topic map.

Fixed thresholds are `0.45`, `0.60`, `0.70` for the documented embedding baseline. Recalibrate for other embedding models. Percentile mode is relative and can make weak outliers look better when the bank is narrow.

## Model configuration

All provider access is environment-only. Set `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `EMBED_MODEL`, and `LABEL_MODEL`. The UI permits Opus 5 (default), Sonnet 5, Haiku 4.5, ChatGPT Sol, and ChatGPT Luna for BP scoring.

Check current provider prices before large runs; cost scales roughly with keyword count.

## Privacy

Keyword banks may reveal strategy, unreleased products, customer names, rankings, URLs, and editorial priorities. Embeddings send every keyword to the configured provider; BP scoring also sends the product description. Review provider retention/training policies and never commit imports, generated output, `.env`, database dumps, or run logs.

## Limitations

- CSV input is implemented. Provider-specific Rank Tracker imports belong behind a separate, scoped server-side adapter.
- Cluster labels are model output and need review.
- The polar Topic map is exploratory UI, not a scientific dimensionality-reduction plot.
