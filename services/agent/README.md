# `services/agent`

One codebase, two runtime instances configured with a role, a mandate, a signing key and model
credentials. Module responsibilities are in [architecture.md](../../docs/architecture.md)
section 3.3; the internal API it serves is [api_contract.md](../../docs/api_contract.md)
section 6.

The two rules that shape everything here:

- **The signer builds the message.** A decision from the model is validated against the mandate
  and public state, and the typed message is then constructed from that validated state — never
  from model-supplied bytes ([protocol.md](../../docs/protocol.md) section 11).
- **The mandate never leaves the instance.** It reaches this service once, at provisioning, and
  is discarded on release. It does not reach the other instance, the backend's logs, SSE or the
  default export ([protocol.md](../../docs/protocol.md) section 12).

`prompts/` holds versioned prompt templates, changed through review like any other source, with
the version hash recorded per decision ([ADR-029](../../docs/decision_log.md)). Code lands in
stage 2, the model policy in stage 3.
