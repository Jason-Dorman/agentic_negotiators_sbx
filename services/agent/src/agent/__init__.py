"""Agent service: one codebase, two runtime instances (A and B).

Each instance knows only its own run-scoped mandate, its own signing key and its own
model credentials. It has no database connection and no RPC connection
(docs/architecture.md section 3.3). Modules arrive in stage 2 of docs/build_plan.md.
"""
