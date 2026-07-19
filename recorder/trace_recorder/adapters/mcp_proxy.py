"""Tier 2 adapter: the MCP recording proxy.

An MCP server that wraps downstream servers, forwards JSON-RPC verbatim,
and emits contract-shaped events as traffic passes. Any MCP client —
Claude Code, Cowork, LangGraph's MCP integration, a custom loop — is
covered without knowing the client. This is wire-execution truth: exact
timing, exact payloads, errors as they happened.

Behavior:

- Speaks MCP over stdio to the upstream client (newline-delimited
  JSON-RPC). Protocol traffic is the ONLY thing written to stdout; all
  logging goes to stderr.
- At startup, initializes each configured downstream server (as an MCP
  client), aggregates their tool inventories, and namespaces tool names
  per the canonical scheme: ``mcp__<server-name>__<tool>``.
- Answers ``initialize``/``tools/list`` itself; routes ``tools/call`` by
  namespaced name, recording ``tool_call``/``tool_result`` events
  correlated by JSON-RPC id.
- Passes non-tool traffic through untouched and unrecorded when exactly
  one downstream is configured; with multiple downstreams, non-tool
  requests receive a method-not-found error (aggregation covers tools).
- Fails open everywhere: recorder errors are logged to stderr and the
  event dropped; agent traffic always proceeds.

Milestone 1 supports ``stdio`` downstream transports. ``http`` downstreams
arrive with Milestone 2 and are rejected at config load with a clear
message.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .. import __version__, contract
from ..core import CoreConfig, mint_session_id
from .core_client import CoreClient

PROTOCOL_VERSION = "2025-06-18"
STARTUP_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 600.0


def _log(message: str) -> None:
    print(f"trace-recorder mcp-proxy: {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------- config


@dataclass
class DownstreamConfig:
    name: str
    transport: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass
class ProxyConfig:
    downstream: list[DownstreamConfig]
    core_endpoint: str | None = None
    trace_dir: Path = Path(".traces")
    session_id: str | None = None

    @staticmethod
    def load(path: Path) -> "ProxyConfig":
        text = path.read_text(encoding="utf-8")
        data: dict[str, Any]
        try:
            data = json.loads(text)
        except ValueError:
            try:
                import yaml  # optional dependency; JSON config needs nothing
            except ImportError as exc:
                raise ValueError(
                    f"{path} is not JSON and PyYAML is not installed; "
                    f"install pyyaml or provide the config as JSON"
                ) from exc
            data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: config must be a mapping")
        listen = data.get("listen", "stdio")
        if listen != "stdio":
            raise ValueError(f"unsupported listen transport {listen!r}; only 'stdio' is supported")
        downstreams: list[DownstreamConfig] = []
        for i, entry in enumerate(data.get("downstream") or []):
            name = entry.get("name")
            transport = entry.get("transport", "stdio")
            if not name:
                raise ValueError(f"downstream[{i}]: 'name' is required")
            if transport == "http":
                raise ValueError(
                    f"downstream {name!r}: http transport is not yet supported "
                    f"(Milestone 2); wrap the server with an stdio bridge or use stdio"
                )
            if transport != "stdio":
                raise ValueError(f"downstream {name!r}: unknown transport {transport!r}")
            command = entry.get("command")
            if not command or not isinstance(command, list):
                raise ValueError(f"downstream {name!r}: 'command' (argv list) is required")
            downstreams.append(
                DownstreamConfig(
                    name=str(name),
                    transport=transport,
                    command=[str(c) for c in command],
                    env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    cwd=entry.get("cwd"),
                )
            )
        if not downstreams:
            raise ValueError("at least one downstream server is required")
        core = data.get("core") or {}
        return ProxyConfig(
            downstream=downstreams,
            core_endpoint=core.get("endpoint"),
            trace_dir=Path(data.get("trace_dir", ".traces")),
            session_id=data.get("session_id"),
        )


# --------------------------------------------------------------- downstream


class DownstreamStdio:
    """MCP client connection to one downstream server over stdio."""

    def __init__(self, cfg: DownstreamConfig, on_async_message: Any = None) -> None:
        self.name = cfg.name
        self._on_async_message = on_async_message
        env = dict(os.environ)
        env.update(cfg.env)
        self._proc = subprocess.Popen(
            cfg.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            cwd=cfg.cwd,
            env=env,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._stdin: BinaryIO = self._proc.stdin
        self._write_lock = threading.Lock()
        self._pending: dict[Any, "threading.Event"] = {}
        self._responses: dict[Any, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._own_id = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"ds-{cfg.name}")
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                _log(f"downstream {self.name}: dropped non-JSON line")
                continue
            msg_id = message.get("id")
            is_response = "result" in message or "error" in message
            if is_response and msg_id is not None:
                with self._pending_lock:
                    waiter = self._pending.pop(msg_id, None)
                    if waiter is not None:
                        self._responses[msg_id] = message
                        waiter.set()
                        continue
            if self._on_async_message is not None:
                self._on_async_message(self.name, message)
        # EOF: release every waiter with a synthetic error.
        with self._pending_lock:
            for msg_id, waiter in self._pending.items():
                self._responses[msg_id] = _rpc_error_response(
                    msg_id, -32603, f"downstream server '{self.name}' exited"
                )
                waiter.set()
            self._pending.clear()

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self._stdin.write(data)
            self._stdin.flush()

    def request_raw(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Forward a fully-formed JSON-RPC request verbatim; await response."""
        msg_id = message["id"]
        waiter = threading.Event()
        with self._pending_lock:
            self._pending[msg_id] = waiter
        try:
            self.send(message)
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            return _rpc_error_response(msg_id, -32603, f"downstream '{self.name}' unreachable: {exc}")
        if not waiter.wait(timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            return _rpc_error_response(msg_id, -32603, f"downstream '{self.name}' timed out after {timeout}s")
        with self._pending_lock:
            return self._responses.pop(msg_id)

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self._own_id += 1
        msg_id = f"proxy-{self._own_id}"
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.request_raw(message, timeout)

    def initialize(self) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "trace-recorder-mcp-proxy", "version": __version__},
            },
            STARTUP_TIMEOUT_S,
        )
        if "error" in response:
            raise RuntimeError(f"downstream '{self.name}' failed initialize: {response['error']}")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response.get("result") or {}

    def list_tools(self) -> list[dict[str, Any]]:
        response = self.request("tools/list", {}, STARTUP_TIMEOUT_S)
        if "error" in response:
            raise RuntimeError(f"downstream '{self.name}' failed tools/list: {response['error']}")
        return (response.get("result") or {}).get("tools") or []

    def close(self) -> None:
        try:
            self._stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()


def _rpc_error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# -------------------------------------------------------------------- proxy


class RecordingProxy:
    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self.session_id = (
            config.session_id or os.environ.get("TRACE_SESSION_ID") or mint_session_id()
        )
        self.client = CoreClient(
            self.session_id,
            endpoint=config.core_endpoint,
            core_config=CoreConfig(trace_dir=config.trace_dir),
        )
        self._stdout_lock = threading.Lock()
        self._downstreams: dict[str, DownstreamStdio] = {}
        # canonical name -> (downstream name, original tool name)
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._tools_payload: list[dict[str, Any]] = []
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tools-call")
        self._ended = False
        self._end_lock = threading.Lock()
        self._call_ids_lock = threading.Lock()
        self._used_call_ids: set[str] = set()

    def _mint_call_id(self, msg_id: Any) -> str:
        """call_id from the JSON-RPC id, kept unique within the session even
        if an upstream client reuses ids."""
        base = f"c-{msg_id}"
        with self._call_ids_lock:
            call_id = base
            n = 1
            while call_id in self._used_call_ids:
                n += 1
                call_id = f"{base}-r{n}"
            self._used_call_ids.add(call_id)
        return call_id

    # -- startup

    def start_downstreams(self) -> None:
        single = len(self.config.downstream) == 1
        for cfg in self.config.downstream:
            on_async = self._forward_async_upstream if single else self._drop_async_message
            ds = DownstreamStdio(cfg, on_async_message=on_async)
            ds.initialize()
            self._downstreams[cfg.name] = ds
        for name, ds in self._downstreams.items():
            for tool in ds.list_tools():
                canonical = f"mcp__{name}__{tool.get('name')}"
                self._tool_map[canonical] = (name, str(tool.get("name")))
                exposed = dict(tool)
                exposed["name"] = canonical
                self._tools_payload.append(exposed)
        self._emit_session_start()

    def _emit_session_start(self) -> None:
        self.client.emit(
            contract.SESSION_META,
            {
                "phase": "start",
                "producer": {
                    "adapter": "mcp-recording-proxy",
                    "adapter_version": __version__,
                    "host": os.environ.get("TRACE_HOST", "unknown"),
                    "host_version": os.environ.get("TRACE_HOST_VERSION", "unknown"),
                    "model": os.environ.get("TRACE_MODEL", "unknown"),
                },
                "cwd": os.getcwd(),
                "downstream_servers": sorted(self._downstreams),
                "tool_inventory": sorted(self._tool_map),
            },
        )

    # -- upstream I/O

    def _write_upstream(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._stdout_lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    def _forward_async_upstream(self, downstream_name: str, message: dict[str, Any]) -> None:
        # Single-downstream posture: pass server-initiated traffic through.
        self._write_upstream(message)

    def _drop_async_message(self, downstream_name: str, message: dict[str, Any]) -> None:
        _log(
            f"dropped async message from downstream '{downstream_name}' "
            f"({message.get('method', 'response')}): multi-downstream aggregation "
            f"forwards tool traffic only"
        )

    # -- request handling

    def handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        is_request = method is not None and msg_id is not None
        if method == "initialize":
            self._write_upstream(self._initialize_response(message))
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list" and is_request:
            self._write_upstream(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": self._tools_payload}}
            )
        elif method == "tools/call" and is_request:
            self._executor.submit(self._handle_tools_call, message)
        elif method == "ping" and is_request:
            self._write_upstream({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif is_request:
            self._passthrough_request(message)
        elif method is not None:
            self._passthrough_notification(message)
        else:
            # A response from the upstream client (to a server-initiated
            # request passed through in single-downstream mode).
            if len(self._downstreams) == 1:
                next(iter(self._downstreams.values())).send(message)

    def _initialize_response(self, message: dict[str, Any]) -> dict[str, Any]:
        requested = ((message.get("params") or {}).get("protocolVersion")) or PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "trace-recorder-mcp-proxy", "version": __version__},
            },
        }

    def _handle_tools_call(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        params = message.get("params") or {}
        canonical = str(params.get("name"))
        arguments = params.get("arguments")
        mapping = self._tool_map.get(canonical)
        if mapping is None:
            known = ", ".join(sorted(self._tool_map)) or "none"
            self._write_upstream(
                _rpc_error_response(
                    msg_id, -32602, f"unknown tool {canonical!r}; available tools: {known}"
                )
            )
            return
        downstream_name, original_tool = mapping
        call_id = self._mint_call_id(msg_id)
        self.client.emit(
            contract.TOOL_CALL,
            {"call_id": call_id, "tool": canonical, "args": arguments, "args_redactions": []},
        )
        forwarded = dict(message)
        forwarded["params"] = {**params, "name": original_tool}
        started = time.monotonic()
        response = self._downstreams[downstream_name].request_raw(forwarded, CALL_TIMEOUT_S)
        duration_ms = int((time.monotonic() - started) * 1000)
        if "error" in response:
            status, result, error = "error", None, response["error"]
        else:
            result = response.get("result")
            is_error = bool(isinstance(result, dict) and result.get("isError"))
            status = "error" if is_error else "ok"
            error = result if is_error else None
        self.client.emit(
            contract.TOOL_RESULT,
            {
                "call_id": call_id,
                "status": status,
                "duration_ms": duration_ms,
                "result": result,
                "error": error,
            },
        )
        # Forward the downstream's response verbatim, restoring the upstream id.
        out = dict(response)
        out["id"] = msg_id
        self._write_upstream(out)

    def _passthrough_request(self, message: dict[str, Any]) -> None:
        if len(self._downstreams) == 1:
            ds = next(iter(self._downstreams.values()))
            self._executor.submit(
                lambda: self._write_upstream(ds.request_raw(dict(message), CALL_TIMEOUT_S))
            )
        else:
            self._write_upstream(
                _rpc_error_response(
                    message.get("id"),
                    -32601,
                    f"method {message.get('method')!r} is not routable through the "
                    f"multi-downstream recording proxy (tool traffic only)",
                )
            )

    def _passthrough_notification(self, message: dict[str, Any]) -> None:
        if len(self._downstreams) == 1:
            try:
                next(iter(self._downstreams.values())).send(message)
            except Exception as exc:
                _log(f"dropped notification: {exc}")
        else:
            _log(f"dropped notification {message.get('method')!r} (multi-downstream posture)")

    # -- lifecycle

    def end(self, end_reason: str) -> None:
        with self._end_lock:
            if self._ended:
                return
            self._ended = True
        self.client.end(end_reason)
        self._executor.shutdown(wait=False)
        for ds in self._downstreams.values():
            ds.close()

    def serve_stdio(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: (self.end("interrupted"), sys.exit(0)))
        try:
            for raw in sys.stdin.buffer:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    _log("dropped non-JSON line from upstream")
                    continue
                try:
                    self.handle_message(message)
                except Exception as exc:
                    # Fail open: a proxy bug must not kill the session.
                    _log(f"internal error handling message: {exc}")
                    if message.get("id") is not None and message.get("method") is not None:
                        self._write_upstream(
                            _rpc_error_response(message["id"], -32603, f"proxy internal error: {exc}")
                        )
        except KeyboardInterrupt:
            self.end("interrupted")
            return
        self.end("normal")


def main(config_path: Path) -> int:
    try:
        config = ProxyConfig.load(config_path)
    except (OSError, ValueError) as exc:
        _log(f"config error: {exc}")
        return 2
    proxy = RecordingProxy(config)
    try:
        proxy.start_downstreams()
    except Exception as exc:
        _log(f"startup failed: {exc}")
        proxy.end("error")
        return 1
    _log(
        f"session {proxy.session_id}: recording {len(proxy._tool_map)} tools "
        f"from {len(config.downstream)} downstream server(s)"
    )
    proxy.serve_stdio()
    return 0
