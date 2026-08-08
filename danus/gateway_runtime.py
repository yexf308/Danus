"""Fail-closed runtime check for Codex sessions that require the Danus gateway.

The gateway is a child MCP process, so importing it successfully in the parent
does not prove that the exact interpreter configured for Codex can start it.
This module probes that interpreter in a separate process before Codex starts.
"""

from __future__ import annotations

import os
import subprocess
import sys


class GatewayRuntimeUnavailable(RuntimeError):
    """The configured Python cannot load the complete Danus MCP runtime."""


_IMPORT_CHECK = (
    "import danus.gateway.server; "
    "from danus._mcp import FastMCP; "
    "assert callable(FastMCP), 'danus._mcp.FastMCP is not callable'"
)


def _probe(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
    return detail[-2000:]


def _require_gateway_runtime(executable: str) -> None:
    if not os.path.isabs(executable):
        raise GatewayRuntimeUnavailable(
            f"gateway interpreter is not an absolute path: {executable!r}"
        )

    isolated = [executable, "-I", "-B", "-c", _IMPORT_CHECK]
    try:
        completed = _probe(isolated)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GatewayRuntimeUnavailable(
            f"Danus gateway runtime preflight could not run for {executable}: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise GatewayRuntimeUnavailable(
            "Danus gateway runtime preflight failed for "
            f"{executable} (exit {completed.returncode}): {_failure_detail(completed)}"
        )


def require_gateway_runtime() -> None:
    """Verify the exact current interpreter can import the full gateway runtime."""
    _require_gateway_runtime(sys.executable)


__all__ = [
    "GatewayRuntimeUnavailable",
    "require_gateway_runtime",
]
