# Documentation Index

Governance documents for the Two-Agent Negotiation and Settlement Sandbox. Read in the order listed for a first pass.

| Document | Purpose | Read when |
|---|---|---|
| [Agent-Negotiation-Sandbox-System-Spec-v0.1.md](Agent-Negotiation-Sandbox-System-Spec-v0.1.md) | The originating system specification. Wins on conflict until a decision-log entry says otherwise. | First |
| [prd.md](prd.md) | Product requirements with stable IDs (`FR-…`, `NFR-…`), scope, release criteria, glossary | Planning any feature; checking scope |
| [protocol.md](protocol.md) | Normative on-chain protocol: typed messages, hashing, sequence and time rules, contract interface, events, reason codes, decision and observation schemas | Touching contracts, signer, indexer, or any schema |
| [architecture.md](architecture.md) | Components, deployment, key flows, state machines, model integration, cross-cutting concerns | Starting any implementation work |
| [api_contract.md](api_contract.md) | Operator REST and SSE API, export document, agent internal API, error codes | Touching routes, the web client, or the agent service interface |
| [data_model.md](data_model.md) | PostgreSQL schema, enums, invariants, data classification, migration policy | Touching persistence or export |
| [security_and_trust_boundaries.md](security_and_trust_boundaries.md) | What is protected, boundaries, authority model, threat table, secrets, disclosures | Touching anything in the privacy-sensitive module list |
| [test_strategy.md](test_strategy.md) | Test layers, acceptance mapping A01 to A17, CI gates, coverage thresholds | Writing or reviewing tests |
| [build_plan.md](build_plan.md) | Stages 0 to 6 with exit conditions and working agreements | Planning the next piece of work |
| [contributing.md](contributing.md) | Repository layout, import boundaries, code standards, workflow, review checklist | Before every PR |
| [decision_log.md](decision_log.md) | ADRs with rationale; includes the assumptions made where the spec was silent | Before changing a decision; when adding one |
| [open_questions.md](open_questions.md) | Questions for the product owner with provisional answers currently assumed | Now, and whenever an assumption needs confirming |
| [engineering-principles.md](engineering-principles.md) | General engineering principles (SOLID, coupling, cohesion, testing mindset) | Background reading |

Documents to be added during the build:

| Document | Stage |
|---|---|
| `runbook.md` | Started in stage 2, completed in stage 5 |
| `deployments/<chain>-<date>.json` | Stages 1 and 5 |
| `evidence/` | Stages 3, 5, 6 |
| `results-v0.1.md` | Stage 6 |

Conventions: all diagrams are Mermaid code blocks. Amounts in documents are written in whole units for readability and in minor units where the wire format matters. Anything marked **[assumption]** has a matching row in the open questions.
