"""trace-recorder: agent-agnostic tool-call trace recorder.

The recorder is the passive, always-on witness of everything an agentic
process does with its tools. Adapters (MCP proxy, gateway tap, hooks)
translate host-native traffic into contract-shaped events; the core is
the single writer that owns sequencing, hash chaining, redaction,
spillover, durability, and session lifecycle.

See development_guidebooks/recorder-development-guidebook.md (actskills)
for the governing design document, and the workflow-compiler skill for
the consumer of the traces this package produces.
"""

__version__ = "0.2.0"
