# Security policy and deployment checklist

This repository is a reference implementation. Run it on localhost or behind an authenticated HTTPS reverse proxy. Do not expose Flask's development server directly.

## Threat model

The application stores potentially confidential SEO strategy: keyword candidates, rankings, URLs, core topics, clusters, BP judgments, and product descriptions. Database, app, backup, log, or model-provider access may reveal editorial plans, competitive targets, unreleased products, or customer relationships.

## Required controls

- Keep `SECRET_KEY`, `DATABASE_URL`, `APP_PASSWORD_HASH`, and model credentials out of Git.
- Use HTTPS outside localhost.
- Prefer an identity-aware proxy for multi-user deployments.
- If `ALLOW_PROXY_AUTH=1`, the proxy must strip spoofed inbound `X-Authenticated-User` headers before injecting its trusted value.
- Restrict PostgreSQL network access and use a least-privilege role.
- Encrypt backups and define retention/deletion procedures.
- Never commit `.env`, CSV imports, generated outputs, SQL dumps, or `instance/` run logs.
- Review model-provider retention and training policies: keywords and product descriptions are transmitted during generation.

## Public-host preflight

- [ ] Auth enforced on every route
- [ ] CSRF on state changes
- [ ] App refuses startup without credentials
- [ ] Bind host remains loopback unless deliberately overridden
- [ ] `debug=False`
- [ ] HTTPS reverse proxy configured
- [ ] `SESSION_COOKIE_SECURE=true`
- [ ] Upload limit matches reverse-proxy limits
- [ ] Database is private and encrypted backups are access-controlled
- [ ] Model endpoint uses HTTPS and a scoped key
- [ ] Repository/history contain no personal/customer keyword data
- [ ] Auth proxy strips spoofed auth headers

## Implemented defenses

Fail-closed startup; route-wide auth; CSRF; upload byte/type/encoding/length/row limits; PostgreSQL; no user-controlled URL fetching; redirect rejection; validated run IDs; loopback/debug-safe defaults; secure-cookie flags; CSP, frame denial, no-sniff, referrer, permissions, cache, and HSTS headers; DOM escaping for imported text.

Report vulnerabilities privately to the repository owner rather than posting exploit details or private data publicly.
