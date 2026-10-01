"""Agent service: one codebase, two runtime instances (A and B).

Each instance knows only its own run-scoped mandate, its own signing key and its own model
credentials. It has no database connection and no RPC connection (docs/architecture.md section
3.3). Run one with `python -m agent`; the modules are listed in that section.
"""
