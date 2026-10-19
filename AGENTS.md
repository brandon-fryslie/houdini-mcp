# Agent Instructions

## Architecture Decisions (ADRs)

Architecture Decision Records live in [`docs/adr/`](docs/adr/). Read them before
proposing structural or language changes:

- [ADR 0001: Language and Process Boundaries](docs/adr/0001-language-and-process-boundaries.md)
  — the server stays Python for `hou` semantics and the FastMCP gateway. A
  non-Python sidecar is authorized **only** when a measured threshold in that ADR
  is crossed and only behind a versioned, language-neutral protocol; a sidecar
  must never duplicate `hou`/node/geometry semantics. No rewrite is authorized
  without a follow-up ticket backed by profiling.

<!-- BEGIN LIT INTEGRATION -->
## lit Agent-Native Workflow

This repository uses `lit` for agent-native issue tracking.

Start by running `lit quickstart` to load the workflow instructions. It prints how tickets are found, created, updated, and closed here, so running it first means the rest of your work follows the conventions this repo expects. It's a quick, read-only command — no need to check in before running it.

<!-- END LIT INTEGRATION -->
