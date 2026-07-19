"""trace-recorder CLI.

    trace-recorder daemon              # start the sink
    trace-recorder status              # sessions, uncompiled traces (the nag)
    trace-recorder verify <trace>      # recompute the hash chain
    trace-recorder mcp-proxy --config  # Tier 2 MCP recording proxy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trace-recorder",
        description="Agent-agnostic tool-call trace recorder (workflow-compilation IR producer).",
    )
    parser.add_argument("--version", action="version", version=f"trace-recorder {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_daemon = sub.add_parser("daemon", help="start the event sink (single writer)")
    p_daemon.add_argument("--port", type=int, default=7717)
    p_daemon.add_argument("--trace-dir", type=Path, default=Path(".traces"))

    p_status = sub.add_parser(
        "status", help="report sessions and uncompiled traces; exits 1 if any are uncompiled"
    )
    p_status.add_argument("--trace-dir", type=Path, default=Path(".traces"))
    p_status.add_argument(
        "--workflow-root",
        type=Path,
        default=Path.cwd(),
        help="directory searched (recursively) for *.workflow.yaml artifacts",
    )

    p_verify = sub.add_parser("verify", help="recompute a trace's hash chain and structure")
    p_verify.add_argument("trace", type=Path)

    p_proxy = sub.add_parser("mcp-proxy", help="run the MCP recording proxy over stdio")
    p_proxy.add_argument("--config", type=Path, required=True, help="proxy config (JSON or YAML)")

    args = parser.parse_args(argv)

    if args.command == "daemon":
        from .core import CoreConfig
        from .daemon import serve

        serve(port=args.port, config=CoreConfig(trace_dir=args.trace_dir))
        return 0

    if args.command == "status":
        from .status import scan_status

        report = scan_status(args.trace_dir, args.workflow_root)
        print("\n".join(report.summary_lines()))
        return 1 if report.uncompiled else 0

    if args.command == "verify":
        from .verify import verify_trace

        report = verify_trace(args.trace)
        print("\n".join(report.summary_lines()))
        return 0 if report.ok else 1

    if args.command == "mcp-proxy":
        from .adapters.mcp_proxy import main as proxy_main

        return proxy_main(args.config)

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
