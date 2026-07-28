# Privacy notes

The app includes no analytics, advertising, or telemetry.

## Stored locally

PostgreSQL stores imported keywords, SEO metrics, URLs, core flags, tiers, clusters, BP scores, and generation-run metadata. Run logs are written under Flask's `instance/runs/` and excluded by `.gitignore`.

## Sent externally

When a tier-generation run starts:

- every keyword is sent to the configured embedding provider;
- representative core keywords are sent to the cluster-labeling model;
- when BP scoring is enabled, every keyword and the product description are sent to the selected BP model.

No data is sent until a generation run starts. This repository does not select or control provider retention/training policies.

## Operator responsibilities

- Obtain any consent required for customer or employee data.
- Avoid importing personal names, emails, customer domains, or confidential product terms unless the provider is approved for that data.
- Configure retention, deletion, backups, and access controls for PostgreSQL and run logs.
- Remove exports and backups when no longer needed.
- Never commit live CSVs, `.env`, database dumps, generated files, or logs.
