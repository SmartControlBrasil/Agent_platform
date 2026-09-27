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
- optional website enrichment with strict SSRF controls, exact seed URL first, per-page failure warnings (root 404 does not abort the whole run), and conservative text phone extraction (tel/JSON-LD preferred; CEP/dates/IDs rejected);
- manual prospect qualification in Hando (`qualification_status`, manual `priority`, `qualification_note`) — qualification is not a CRM pipeline and priority is not an automated score;
- `ProspectContact` for people/channels linked to a prospect company (`name`, `role_title`, email, phone, note, provenance).

Conceptual split:

- **Prospect** — company/account discovered in prospection;
- **ProspectContact** — person or identifiable channel (e.g. “Recepção”, “Compras”) within that company;
- **ProspectEnrichment** — observed data point (email/phone from website, manual note on a field) without implying a contact record.

Enrichment does **not** auto-create contacts in the current phase.

- **ProspectActivity** — manual commercial history (notes, calls, meetings, etc.) optionally linked to a `ProspectContact`; separate from `AuditEvent` governance trail.

Activity does not change qualification, send messages, or sync calendars.

- **ProspectOutreachDraft** — commercial outreach **preparation** (contact, channel, subject/body, draft/ready/archived status) for qualified prospects only; stores destination snapshots from `ProspectContact`; saving a draft does **not** create `ProspectActivity`.
- **ProspectOutreachSend** — **actual transmission attempt** for EMAIL drafts (snapshots frozen at send time, idempotent single successful send per draft, manual retry on technical failure); on **SENT**, creates `ProspectActivity` with type **EMAIL_SENT**. Distinct from draft content and from leads/Lívia notification outbox.
- **ProspectContactOutcome** — structured **manual** result after contact (interest, callback requested, no response, etc.); may link to a send; creates past-tense `ProspectActivity`; does not auto-change qualification.
- **ProspectFollowUp** — **planned next action** (call, email intent, meeting, …) with optional `due_at`; overdue is derived in UI; completing a follow-up does not fake an activity.
- **Hando · Acompanhamentos** (`/painel/prospeccao/acompanhamentos/`) — tenant-scoped **work queue** for pending follow-ups (filters, counters, quick complete/cancel). Overdue/today/upcoming buckets are **not** stored on the model. No scheduler or automatic reminders in this phase.
- **Hando · Minha Fila** (`/painel/prospeccao/minha-fila/`) — **operational projection** (not a CRM pipeline, not persisted queue state) answering *who to approach next*. Built in `prospecting/application/commercial_queue.py` from existing models only.

#### Minha Fila — categories and precedence

Six derived categories (highest precedence first):

1. **Follow-up overdue** — `ProspectFollowUp` `PENDING`, `due_at` in the past (local timezone).
2. **Follow-up today** — `PENDING`, `due_at` on the current local day.
3. **Outreach pending** — prospect `QUALIFIED` with draft `DRAFT`/`READY` (READY counts only while no `SENT` send exists for that draft), or send `PENDING`/`SENDING`/`FAILED`.
4. **Waiting outcome** — `QUALIFIED`, `ProspectOutreachSend` `SENT` without a `ProspectContactOutcome` linked to that send (email structured path only; no `ProspectActivity` heuristics; PHONE/WHATSAPP manual outreach is not classified here).
5. **Missing contact** — `QUALIFIED`, no **usable** `ProspectContact` (`normalized_email` or `normalized_phone` non-empty). `ProspectEnrichment` does not count; name-only contacts do not count in v1.
6. **Ready for outreach** — `QUALIFIED`, usable contact, none of the above.

**Follow-ups** appear regardless of prospect `qualification_status` (human tasks already created stay visible). Categories 3–6 apply only to `QUALIFIED` prospects.

**Compact mode** (`compact=True`, default in Hando): if a prospect has overdue/today follow-ups, prospect-level rows (3–6) for that prospect are suppressed; multiple follow-ups remain separate rows. Between 3–6 each prospect appears in at most one category.

Tenant isolation: all queries are scoped to the active tenant or explicit `tenant_ids` (global read-only). Dashboard card **Minha Fila** is shown only with a **specific tenant** selected (same pattern as the Acompanhamentos card); global dashboard does not aggregate this card.

### Contact enrichment flow (Maps → website → operator)

1. **Google Maps** (`prospecting.search_google_maps`) fills **SearchResult** with raw fields (name, address, phone, website, maps URL). Maps is **not** a reliable email source; phone/address may be missing when not exposed in the collected DOM (feed cards vs detail panel).
2. **Promotion** copies observations onto **Prospect** (`phone`, `address`, `website`, …) and persists **ProspectEnrichment** rows with `SourceType.SEARCH_RESULT` (Maps provenance) — still **no** automatic **ProspectContact**.
3. **Website enrichment** (`enrich_prospect_from_website`) fetches the official site (homepage + a few contact-like internal pages, same host), extracts published emails/phones/addresses (including JSON-LD when present), and stores **ProspectEnrichment** with `source_url` per page. SSRF protections remain mandatory.
4. Operators convert enrichments to **ProspectContact** explicitly in Hando (**Criar contato**); the platform does not infer roles or merge channels automatically.

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
