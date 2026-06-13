"""Pipeline package: orchestrator + harness for the long-to-short clip engine.

Layers:
    types        — Pydantic models for stage I/O contracts
    config       — env-backed runtime config + budgets
    state/       — durable run state (Upstash Redis)
    observability/ — OTel spans + cost ledger + JSON trace dump
    guardrails/  — input validation, output QC gate, hard limits
    stages/      — concrete stage implementations (ingest, transcribe, finalize)
    tools/       — adapter registry + dispatch choke point for tools 1/2/3
    llm          — Claude wrapper with cost capture
    orchestrator — state-machine runner
"""
