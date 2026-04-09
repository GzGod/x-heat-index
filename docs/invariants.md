# myproject — Functional Invariants

> **What this document is.** The authoritative list of "things that must be true about myproject, regardless of what the code currently does". Each entry is an invariant with a stable ID, a documented authority source, a classification, and a mapping to its current enforcement mechanism (live claim / deferred DSL / runtime test / unwritten).
>
> This document is the **Layer 1** artifact in claim-runtime's four-layer pipeline: natural-language product decisions (Layer 0) get transcribed here as structured invariants, which drive formal state-machine code (Layer 2), which gets protected by claim-runtime claims (Layer 3), which run through L1/L2/L3 gates (Layer 4). See `claim-runtime/docs/design-philosophy.md` §7 for the full model.
>
> **What this document is not.** A spec of what the system does (that's `architecture.md`). A list of bugs to fix (that's the audit log). A changelog. A roadmap.

## How to use this document

1. **Reading new code**: before touching a module, search this doc for `INV-<MODULE>-*` — those are the rules your change must not break. If a rule has a live claim, L2/L3 will catch you. If it's "deferred" or "unwritten", the discipline is on you.

2. **Writing new code**: when you introduce a new invariant (a rule you intend to preserve forever), add an entry here **before** implementing it. The discipline is that the invariant exists in this doc; the code making it true is the artifact.

3. **Writing new claims**: pick an entry whose enforcement is 🟡 or ❌, verify the current DSL can express it, write the claim, then update the "Enforcement" cell here to reference the live claim ID.

4. **Deprecating an invariant**: do not delete entries. Mark them `DEPRECATED` with a date and a one-line reason. The ID stays reserved forever — nobody gets INV-DOMAIN-03 twice.

## Rules for adding entries

- Every invariant MUST have a documented authority citation (CLAUDE.md, architecture doc, a design doc, an audit memory, industry consensus with link, product decision from a dated session). "The code currently doesn't do X" is **not** authority. Grep returning zero is **not** authority. See `claim-runtime/docs/design-philosophy.md` §5 for the T1-T6 authority hierarchy.

- Classify honestly. Don't mark behavioral invariants as "structural" just to get a claim written for them. Behavioral invariants belong in tests or monitoring, not in claim-runtime.

- Keep the ID stable. IDs are permanent handles — code, commit messages, and claims will reference them. Once assigned, an ID is never reused.

- **Every new claim must have a matching INV-ID.** A claim without a corresponding invariant is T6 preservation (grep-driven, no documented authority) — it's forbidden. Write the INV entry first, with authority citation, then write the claim.

## Status legend

- ✅ **Live** — enforced by an active claim at L1/L2/L3
- ⚠️ **Partial** — partially enforced (e.g., covers one file but the rule is broader)
- 🟡 **Deferred** — documented, claim blocked on a specific DSL feature (linked to `claim-runtime/docs/deferred-dsl-requirements.md`)
- ❌ **Unwritten** — authority documented, DSL could express it, no one has written the claim yet
- 📊 **Behavioral** — invariant is about runtime behavior (latency, coverage, ratios), not claim-shaped; enforced by monitoring / integration test
- 🔒 **Policy** — human-process rule, not code-checkable (e.g., "no direct server deployment")

## Entry template

When adding a new invariant, copy this block and fill it in:

```markdown
### INV-<DOMAIN>-<NN>: <one-line title>
**Statement.** <what must be true, in precise language. Name the exact attribute, function, or pattern.>

**Authority.** <cite the source — CLAUDE.md:<line>, docs/architecture.md §<n>, memory/<file>.md, OWASP <id>, product session <date>, etc. Must be a document that exists regardless of code state.>

**Type.** <Structural | Behavioral | Configuration | Policy>

**Enforcement.** <status emoji> <claim ID if live, blocking DSL feature if deferred, action plan if unwritten>

**Notes.** <optional: edge cases, related invariants, historical context, mutation test recipe>
```

---

## 1. State Machines

> Document every formal state machine in the system here. Each state machine should have: a transition table, a single entry point for writes, an audit log, and at least one claim enforcing "writes go through the single entry point".

<!-- Example entry — delete when real entries are added:

### INV-DOMAIN-01: <Entity> state transitions follow the TRANSITIONS table
**Statement.** An entity's `status` field may only be mutated via the `transition()` helper in `src/<domain>/lifecycle.py`, which validates the target state against a `TRANSITIONS` dict. Direct assignment to `entity.status` anywhere else in the source tree is forbidden.

**Authority.** `src/<domain>/lifecycle.py:<line>` + CLAUDE.md §"Constraints" on state machine discipline.

**Type.** Structural.

**Enforcement.** 🟡 Deferred — needs statement-level pattern support in claim-runtime DSL. See `claim-runtime/docs/deferred-dsl-requirements.md`.

-->

## 2. Module Dependency Graph

> If your project has an import-direction DAG (via a custom linter or a convention), document the DAG rules here. Each allowed dependency edge and each forbidden edge is potentially an invariant.

## 3. Data Contracts

> Rules about how data must be represented and accessed. Examples: "money uses Decimal not float", "API responses must parse through a dataclass", "timestamps must be timezone-aware", "enum values come from a single source".

## 4. Authentication & Authorization

> Rules about who can do what. Examples: "all API endpoints validate JWT", "admin endpoints require role check", "webhook signatures verified", "user input URLs validated before fetch".

## 5. Database Discipline

> Rules about how the database is accessed. Examples: "single connection pool through one module", "row locks on state transitions", "no raw SQL outside the ORM layer", "migrations are append-only".

## 6. External API Boundaries

> Rules about interactions with third-party services. Examples: "rate limits on outbound calls", "retry policies", "dry-run modes", "secret rotation intervals". Many of these will be 📊 Behavioral, not claim-shaped — that's fine, document them anyway.

## 7. Scheduler / Background Jobs

> If your project has background jobs, document the discipline: concurrency locking, failure alerting, retry semantics, clock-skew handling.

## 8. Code Quality

> Absorbed lint tools, style linters, and taste rules live here. Each absorbed tool is typically one invariant + one claim wrapping it.

## 9. Operational Policy

> Human-process rules that can't be mechanized. Examples: "no direct production deployment", "commits signed", "PR review required". These stay 🔒 Policy — document them so contributors know they exist, but don't try to claim them.

---

## Index — Invariants by enforcement status

### ✅ Live (0)
_none yet_

### ⚠️ Partial (0)
_none yet_

### 🟡 Deferred (0)
_none yet_

### ❌ Unwritten (0)
_none yet_

### 📊 Behavioral (0)
_none yet_

### 🔒 Policy (0)
_none yet_

**Total: 0 invariants.**

---

## Index — Claims by invariant reference

Reverse index: find the invariant for a known claim ID. Populate as claims ship.

| Claim ID | Invariant |
|---|---|
| _none yet_ | — |

---

## Changelog

- **<YYYY-MM-DD>** — Document created from the `claim-runtime` template. Initial enumeration: 0 invariants. Next step: run an outside-in pass over existing architecture docs (CLAUDE.md, `docs/architecture.md`, any constraint sections) and transcribe every MUST / must not / 必须 / 禁止 statement as a candidate INV entry.
