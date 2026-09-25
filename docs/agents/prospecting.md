# Prospecting Agent

## Purpose

Prospecting Agent is the second concrete runtime of the Agent Platform.

Its initial role is architectural: prove that a non-Lívia agent can be registered, installed, configured, and executed through the generic runtime registry and ports without changing the generic execution endpoint.

## Configuration

Current supported fields:

- `target_market`
- `target_region`
- `target_profile`
- `objective`
- `max_results`

Validation rules:

- strings are stripped and length-limited;
- `max_results` must be an integer between `1` and `500`;
- plaintext secrets are not allowed in `AgentInstallation.configuration`.

## Current Version

- AgentDefinition slug: `prospecting`
- AgentVersion: `1.0.0`
- Runtime handler: `prospecting`

## Operational Context

The operational prospecting workflow lives in the same repository under the `prospecting` app and is exposed in the Hando portal (`/painel/prospeccao/...`), not in a parallel UI. This workflow includes:

- `SearchRun` and `SearchResult` for discovery records;
- `SearchRunExecutionAttempt` for explicit execution history (`retry` creates a new attempt; `redispatch` reuses a non-terminal `ToolExecution`);
- explicit promotion `SearchResult -> Prospect`;
- `ProspectSource` provenance history;
- `ProspectEnrichment` manual observations;
- optional website enrichment with strict SSRF controls;
- manual prospect qualification in Hando (`qualification_status`, manual `priority`, `qualification_note`) — qualification is not a CRM pipeline and priority is not an automated score;
- `ProspectContact` for people/channels linked to a prospect company (`name`, `role_title`, email, phone, note, provenance).

Conceptual split:

- **Prospect** — company/account discovered in prospection;
- **ProspectContact** — person or identifiable channel (e.g. “Recepção”, “Compras”) within that company;
- **ProspectEnrichment** — observed data point (email/phone from website, manual note on a field) without implying a contact record.

Enrichment does **not** auto-create contacts in the current phase.

- **ProspectActivity** — manual commercial history (notes, calls, meetings, etc.) optionally linked to a `ProspectContact`; separate from `AuditEvent` governance trail.

Activity does not change qualification, send messages, or sync calendars.

- **ProspectOutreachDraft** — commercial outreach **preparation** (contact, channel, subject/body, draft/ready/archived status) for qualified prospects only; stores destination snapshots from `ProspectContact` but does **not** send email, WhatsApp, or any external message, and does **not** create `ProspectActivity` when saved.

Qualification statuses:

- `UNQUALIFIED` — not yet reviewed commercially;
- `QUALIFIED` — worth commercial follow-up;
- `NOT_A_FIT` — not a fit right now (does not delete prospect, sources, or enrichments);
- `ON_HOLD` — potential interest, not prioritized now (no automatic reminders in this phase).

Manual priority values: `UNSET`, `LOW`, `MEDIUM`, `HIGH`.

## Current Limitations

Still out of scope in the current platform phase:

- Google Maps scraping or browser automation from this app;
- smart_sales runtime coupling;
- AI/LLM scoring or autonomous qualification;
- CRM pipeline management and outreach orchestration.

## Future Planned Tools

Possible future capabilities include:

- prospect source connectors,
- enrichment tools,
- qualification pipelines,
- async execution orchestration,
- customer-product integrations such as `smart_sales`.
