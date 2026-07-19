# trace-recorder

Agent-agnostic tool-call trace recorder — the infrastructure layer of the
workflow-compilation framework. It is the passive, always-on witness of
everything an agentic process does with its tools, regardless of which agent
framework, orchestrator, or model provider is driving. When a session ends, a
complete, wire-level, tamper-evident record of the solution path sits on disk
in `./.traces/<session-id>.jsonl`, ready for the **workflow-compiler** skill
(see the `actskills` repo) to crystallize into a deterministic workflow that
replays with zero tokens.

Design document: `development_guidebooks/recorder-development-guidebook.md`
in the `actskills` repo. The trace contract there is normative; this package
implements it at schema version 1.

```
FRONTENDS (adapters)              IR                    BACKENDS
─────────────────────      ─────────────────      ─────────────────────
LLM gateway tap        ─┐                      ┌─▶ workflow-compiler
MCP recording proxy    ─┤                      │   (skill / service)
OTel GenAI ingestion   ─┼──▶  Trace Contract ──┤
Framework callbacks    ─┤     (.traces JSONL)  └─▶ deterministic runtime
Host hooks (CC, etc.)  ─┘                          (zero-token replay)
```

## Principles

- **The recorder is infrastructure, not a participant.** No agent's
  cooperation, memory, or discretion is involved; adapters live at transport
  layers no model mediates.
- **The trace is wire-level truth.** Raw arguments, raw results, timestamps,
  errors — never an agent's description of what it did.
- **Recording must never break the session.** Fail open everywhere: on
  internal error, log to stderr, drop the event, let the traffic proceed.
- **Agent-agnosticism lives in the contract.** Everything downstream of the
  IR runs identically on a trace produced by any host.

## Install

```bash
pipx install ./recorder          # or: pip install ./recorder
# optional: YAML config support for the proxy
pipx install './recorder[yaml]'
```

The core write path is stdlib-plus-nothing by design and must stay that way.

## CLI

```bash
trace-recorder daemon                 # start the event sink (single writer)
trace-recorder status                 # sessions + uncompiled traces (the nag)
trace-recorder verify <trace.jsonl>   # recompute the hash chain + Phase 1 checks
trace-recorder mcp-proxy --config proxy.yaml   # Tier 2 recording proxy
```

`status` exits non-zero when any trace is newer than the newest
`*.workflow.yaml`, so a pre-commit hook, CI job, or session-end hook can all
enforce "no session ends without compilation being at least offered."

`verify` recomputes the per-session hash chain (tamper evidence) and runs the
structural checks the compiler's Phase 1 performs: seq gaps, call/result
pairing, definitive end record.

## The MCP recording proxy (first adapter)

An MCP server that wraps downstream servers, forwards JSON-RPC verbatim, and
emits contract events as traffic passes. Any MCP client — Claude Code, Cowork,
LangGraph's MCP integration, a custom loop — is covered without knowing the
client, with wire-execution fidelity: exact timing, exact payloads.

```yaml
# proxy.yaml (JSON also accepted; YAML needs the optional pyyaml extra)
listen: stdio
downstream:
  - name: legacydb
    transport: stdio
    command: ["python", "-m", "legacydb_mcp"]
  - name: gw
    transport: stdio
    command: ["python", "-m", "gw_mcp"]
core: { endpoint: "http://127.0.0.1:7717" }   # omit to embed the core in-process
```

Point your MCP client at `trace-recorder mcp-proxy --config proxy.yaml`
instead of the underlying servers. The proxy aggregates downstream tool
inventories at `tools/list` and namespaces them per the canonical scheme
(`mcp__<server>__<tool>`), so compiled workflows are portable across hosts.
With exactly one downstream, non-tool traffic passes through untouched and
unrecorded; with several, aggregation covers tool traffic.

Session identity: adapters propagate the host's session id where one exists;
set `TRACE_SESSION_ID` in the launching environment when multiple adapters
observe the same logical session. Otherwise the core mints `{date}-{hash8}`.

## Trace contract (schema v1)

One JSON object per line, common envelope:

```json
{"v": 1, "seq": 42, "ts": "2026-07-18T15:04:22.117-04:00",
 "session_id": "8f3a9c2e", "type": "tool_call",
 "prev_hash": "sha256:...", "hash": "sha256:...", "body": {}}
```

Event types: `session_meta` (start/end), `tool_call`, `tool_result`,
`reasoning_note`. `seq` is strictly increasing (gaps signal dropped events);
`prev_hash`/`hash` chain from a genesis marker derived from the session id.
Results over 32 KB spill to `.traces/spillover/<session>/<call_id>.json` with
a head sample, byte count, and full-payload SHA-256 kept inline. Deliberately
absent: conversation transcripts, user messages, model outputs — the trace
records *actions*, not dialogue.

The byte-exact contract is pinned by `tests/test_contract_golden.py`; any
change to those bytes is a versioned contract change to be coordinated with
the workflow-compiler skill.

## Redaction

Runs in the core, identically for every adapter, before anything reaches
disk: key-name detectors (`api_key`, `token`, `secret`, `password`, …),
credential-shaped values (JWTs, `sk-`/`ghp_` prefixes, cloud keys), and
connection-string passwords. Replaced values become `"__REDACTED__:<name>"`
with the JSON path logged in `args_redactions`. Results get key-name
detectors only. This is best-effort pattern matching — the trace directory
remains sensitive: it is gitignored, and should be encrypted at rest where
policy requires.

## Testing

```bash
cd recorder && python -m pytest
```

- **Contract/golden** — synthetic adapter input → byte-exact golden trace.
- **Fail-open** — unreachable core, unwritable trace dir, malformed events,
  downstream crash mid-session: traffic proceeds, stderr explains.
- **Chaos durability** — `kill -9` the writer repeatedly; every surviving
  trace verifies to its last complete line and gets an `interrupted` end
  record on restart.
- **End-to-end** — the renewal-tracking join (fixture SQLite DBs behind two
  proxied MCP servers: loops, spillover, redaction, a tool failure) produces
  a trace that passes the compiler's Phase 1 validation with zero gaps.

## Milestone status

- **Milestone 1 (this)** — recorder core (envelope, chain, redaction,
  spillover, lifecycle, CLI, `verify`), MCP recording proxy over stdio,
  contract + fail-open + chaos + e2e tests.
- **Milestone 2** — LiteLLM gateway tap with cross-turn `tool_use`/`tool_result`
  correlation and reasoning-note capture; dual-posture dedup by `call_id`
  affinity; `http` downstream transport for the proxy; the cross-adapter
  equivalence test.
- **Milestone 3** — OTLP GenAI receiver; Tier 4 convenience adapters (Claude
  Code hooks, LangChain callbacks); multi-agent session linking
  (`parent_session_id`); schema version negotiation.
- **Deferred by design** — trace visualization, conversation recording, the
  deterministic replay runtime (its own component and guidebook).
