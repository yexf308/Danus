"""Offline tests for the gateway-runtime subprocess preflight."""

from __future__ import annotations

import subprocess

import pytest

from danus import gateway_runtime


def _completed(command: list[str], returncode: int, stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, "", stderr)


def test_isolated_probe_uses_exact_interpreter_and_imports_complete_runtime(monkeypatch):
    calls = []

    def fake_probe(command):
        calls.append(command)
        return _completed(command, 0)

    monkeypatch.setattr(gateway_runtime, "_probe", fake_probe)
    gateway_runtime.require_gateway_runtime()

    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == [gateway_runtime.sys.executable, "-I", "-B", "-c"]
    assert "danus.gateway.server" in command[4]
    assert "danus._mcp import FastMCP" in command[4]


def test_broken_import_fails_closed(monkeypatch):
    def fake_probe(command):
        return _completed(command, 1, "ModuleNotFoundError: No module named 'mcp'")

    monkeypatch.setattr(gateway_runtime, "_probe", fake_probe)
    with pytest.raises(gateway_runtime.GatewayRuntimeUnavailable, match="No module named 'mcp'"):
        gateway_runtime.require_gateway_runtime()


def test_every_launch_boundary_runs_a_fresh_probe(monkeypatch):
    calls = []

    def fake_probe(command):
        calls.append(command)
        return _completed(command, 0)

    monkeypatch.setattr(gateway_runtime, "_probe", fake_probe)
    gateway_runtime.require_gateway_runtime()
    gateway_runtime.require_gateway_runtime()
    assert len(calls) == 2


def test_relative_interpreter_is_rejected_without_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway_runtime.sys, "executable", "python")
    monkeypatch.setattr(gateway_runtime, "_probe", lambda *a, **k: calls.append(a))
    with pytest.raises(gateway_runtime.GatewayRuntimeUnavailable, match="not an absolute"):
        gateway_runtime.require_gateway_runtime()
    assert calls == []
