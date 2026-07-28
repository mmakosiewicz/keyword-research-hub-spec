# Instructions for AI coding agents

## Default action: install, do not recreate

If a user gives you this repository and asks to use, install, build, or recreate the Keyword Research Hub in a Letaido workspace:

1. Treat [`canonical-workspace-app/`](./canonical-workspace-app/) as the source of truth.
2. Copy its files **verbatim** into the matching Console app/template locations.
3. Follow [`canonical-workspace-app/install_prompt.txt`](./canonical-workspace-app/install_prompt.txt) and [`canonical-workspace-app/manifest.json`](./canonical-workspace-app/manifest.json).
4. Do **not** synthesize a replacement UI from the Markdown workflow documents.
5. Do **not** invent extra tabs, layouts, schemas, routes, sample data, domains, project IDs, tags, or users.
6. Start with an empty database and first-run configuration.
7. Confirm the installed page shows the professional light product header, five workflow tabs, Manage tiers, onboarding, and the Master List Table / Topic map toggle.

The `workflows/`, `api.md`, `data-model.md`, and `filter-pipeline.md` documents explain behavior and extension points. They are not permission to redesign the canonical app during installation.

## Privacy

Never copy data from another workspace. Do not commit or seed live keyword banks, domains, Rank Tracker project IDs, tags, customer names, member names, generated tier files, secrets, logs, or database exports.

## Non-Letaido environments

The canonical package targets Letaido Console. If the user explicitly asks for a standalone deployment, use [`reference-app/`](./reference-app/) as a security-oriented starter, but explain that it is not the complete multi-workflow product.
