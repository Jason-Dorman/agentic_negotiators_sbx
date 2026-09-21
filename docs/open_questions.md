# Open Questions

Decisions the governance documents could not settle from the spec. Each has a provisional answer that the documents currently assume, so work can proceed. Answer here or in conversation; the matching decision-log entry is updated when you do.

| # | Question | Provisional answer in the docs | Where it matters |
|---|---|---|---|
| Q1 | **Model provider and default model.** The spec names no provider. Is Anthropic Claude the intended provider, and is `claude-opus-5` the right default for both sides? Would you like a cheaper model (`claude-sonnet-5`) as the default for batch evaluation to keep the USD 2.00 per-run ceiling comfortable across 60 scenarios × 4 pairings × 3 repetitions? | Anthropic, `claude-opus-5` for both sides and all modes; effort `high`. Estimated batch cost at roughly 10 calls per run and ~2k input / ~100 output tokens per call is on the order of USD 0.02 to 0.05 per run, so the default is affordable, but confirm. | ADR-014, architecture 7, PRD 5 |
| Q2 | **Repository and version control.** The project directory is not yet a git repository. Should I initialize it as a single monorepo per spec 10.2 with the layout in the architecture doc? Any organization or license to record? | Single monorepo, layout per spec 10.2, no license file until you say. | build_plan stage 0 |
| Q3 | **Sepolia key custody.** For the public demo, are encrypted web3 keystore files with an env-provided password acceptable for participant, operator, and relay keys? Or do you prefer plain env variables for simplicity given these are throwaway test wallets? | Keystore files for Sepolia, env for local. | ADR-023, security 7 |
| Q4 | **Sepolia RPC provider.** Do you already have an RPC endpoint (Alchemy, Infura, QuickNode, own node)? Rate limits affect the indexer poll interval. | Operator supplies `SEPOLIA_RPC_URL`; poll interval 4 s. | infra, runbook |
| Q5 | **Operator authentication for remote demos.** Is a single static bearer token plus TLS at a reverse proxy sufficient for v0.1, or do you expect to expose the UI to an audience on a network you do not control? | Static token, localhost by default. | api_contract 1, security 8 |
| Q6 | **Mandate storage at rest.** Mandates are stored in plaintext in PostgreSQL with access limited by repository code. Do you want column-level encryption in v0.1? | Plaintext; classification and access rules only. | data_model 3.4 |
| Q7 | **Explanation field.** The docs resolve the tension between "exactly three shapes, reject extra fields" and "optional short explanation" by wrapping the decision in an envelope with a separate `explanation`. Agreed? | Yes, envelope. | ADR-013, protocol 11 |
| Q8 | **Scenario file format.** JSON with schema validation, or YAML for hand-editing comfort? | JSON. | ADR-022 |
| Q9 | **Batch concurrency.** The spec allows one active run at a time. For the 720-run initial research batch (60 × 4 × 3) at roughly 60 to 90 s per local run this is 12 to 18 hours sequential. Is that acceptable for v0.1, or should we plan per-run relay keys to allow parallel local runs? | Sequential in v0.1; parallelism noted as a follow-on. | ADR-019, build_plan stage 6 |
| Q10 | **Explorer.** Etherscan Sepolia for explorer links? | Yes. | api_contract deployments |
| Q11 | **Prompt content ownership.** Should the agent system prompt be a versioned file you review, and do you want the mandate `instructions` free text to be able to override protocol rules in the prompt? | Versioned file under `services/agent/prompts/`; mandate instructions are appended as "your private guidance" and cannot override protocol rules or the output schema. | architecture 7 |
| Q12 | **Runbook location and format.** `docs/runbook.md` produced in stage 5 alongside the Sepolia deployment, or earlier as a living document from stage 2? | Living document from stage 2, completed in stage 5. | build_plan |
| Q13 | **Coverage thresholds.** The test strategy proposes 100 percent on the contract and the agent validator and signer, 85 percent on the backend. Too strict, too loose? | As proposed. | test_strategy 10 |
| Q14 | **Confirmation threshold on Sepolia.** Spec says 2. With ~12 s blocks that adds ~24 s per turn. Keep 2 for the demo? | Keep 2; configurable per run. | PRD 5 |

## Resolved

| # | Resolution | Date |
|---|---|---|
| — | All diagrams are Mermaid. | 19 September 2026 |
