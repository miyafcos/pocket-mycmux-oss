from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_mirror():
    spec = importlib.util.spec_from_file_location("public_relay_mirror", ROOT / "relay_mirror.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unconfigured_mirror_does_not_start_or_fetch(tmp_path: Path) -> None:
    module = load_mirror()
    opener = Mock(side_effect=AssertionError("unexpected network"))
    watcher = SimpleNamespace(status=lambda: {"winApiOk": False})
    mirror = module.Mirror(watcher, root=tmp_path / "unused", base_url="", opener=opener)
    mirror.start()
    mirror.sync_once()
    assert mirror._thread is None
    assert mirror._fetch("/overview") == ("notConfigured", None)
    assert not mirror.root.exists()
    opener.assert_not_called()


@pytest.mark.parametrize("url", ["http://example.test", "file:///tmp/test", "https://user:password@example.test", "https://example.test/?key=value", "https://example.test/#fragment", "https://example.test:bad", "https://exa mple.test"])
def test_relay_rejects_unsafe_configured_urls(url: str) -> None:
    module = load_mirror()
    with pytest.raises(ValueError):
        module.configured_https_url(url)


def test_relay_origin_requires_no_path() -> None:
    module = load_mirror()
    assert module.configured_https_url("") == ""
    assert module.configured_https_url("https://example.test/", origin_only=True) == "https://example.test"
    assert module.configured_https_url("https://example.test/pocket/api/health") == "https://example.test/pocket/api/health"
    with pytest.raises(ValueError):
        module.configured_https_url("https://example.test/pocket", origin_only=True)


@pytest.mark.parametrize("value,expected", [
    ("https://RELAY.EXAMPLE.TEST:443/", "https://relay.example.test"),
    ("https://relay.example.test:8443/", "https://relay.example.test:8443"),
    ("https://[::1]:443/", "https://[::1]"),
    ("https://[0:0:0:0:0:0:0:1]:443/", "https://[::1]"),
    ("https://[2001:0DB8:0000:0000:0000:0000:0000:0001]:8443/", "https://[2001:db8::1]:8443"),
])
def test_relay_origin_matches_browser_serialization(value: str, expected: str) -> None:
    assert load_mirror().configured_https_url(value, origin_only=True) == expected


def extract_function(filename: str, name: str, namespace: dict):
    parsed = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    selected = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), selected], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "relay-peer-test", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("host", ["pc", "PC.EXAMPLE.TEST", "pc.example.test."])
def test_relay_peer_accepts_short_or_full_dns(host: str) -> None:
    status = {"Peer": {"test": {"HostName": "pc", "DNSName": "pc.example.test.", "Online": True}}}
    namespace = {"WIN_PEER_HOST": "", "TAILSCALE_CLI": "synthetic", "PROBE_TIMEOUT_SECONDS": 1,
                 "subprocess": subprocess, "json": json, "os": os, "_note_peer_reason": lambda _: None}
    check = extract_function("relay_server.py", "tailscale_peer_online", namespace)
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(status), ""))
    assert check(host=host, runner=runner) is True


@pytest.mark.parametrize("host", ["mac", "MAC.EXAMPLE.TEST", "mac.example.test."])
def test_mac_peer_accepts_short_or_full_dns(host: str, monkeypatch) -> None:
    monkeypatch.setenv("POCKET_MAC_PEER_HOST", host)
    status = {"Peer": {"test": {"DNSName": "mac.example.test.", "Online": True}}}
    check = extract_function("server.py", "get_mac_status", {"os": os, "_health_timestamp": lambda: "synthetic"})
    server = SimpleNamespace(tailscale_status=SimpleNamespace(get=lambda: status))
    assert check(server)["macPeerOnline"] is True
