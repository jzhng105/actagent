from __future__ import annotations

import json

import pytest

from trace_recorder.adapters.mcp_proxy import ProxyConfig


def _write(tmp_path, data):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return path


def test_load_minimal_json(tmp_path):
    cfg = ProxyConfig.load(
        _write(
            tmp_path,
            {"downstream": [{"name": "db", "command": ["python", "-m", "db_mcp"]}]},
        )
    )
    assert cfg.downstream[0].name == "db"
    assert cfg.downstream[0].transport == "stdio"
    assert cfg.core_endpoint is None


def test_http_downstream_rejected_with_actionable_message(tmp_path):
    path = _write(
        tmp_path,
        {"downstream": [{"name": "gw", "transport": "http", "url": "https://x/mcp"}]},
    )
    with pytest.raises(ValueError, match="Milestone 2"):
        ProxyConfig.load(path)


def test_missing_downstream_rejected(tmp_path):
    with pytest.raises(ValueError, match="at least one downstream"):
        ProxyConfig.load(_write(tmp_path, {"downstream": []}))


def test_yaml_config_when_available(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "listen: stdio\n"
        "downstream:\n"
        "  - name: legacydb\n"
        "    transport: stdio\n"
        "    command: [python, -m, legacydb_mcp]\n"
        'core: { endpoint: "http://127.0.0.1:7717" }\n'
    )
    cfg = ProxyConfig.load(path)
    assert cfg.downstream[0].command == ["python", "-m", "legacydb_mcp"]
    assert cfg.core_endpoint == "http://127.0.0.1:7717"
