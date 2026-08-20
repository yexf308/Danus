"""Static regression tests for the repo-root Codex main-agent adapter."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_codex_entry_contract_replaces_claude_only_entrypoints():
    contract = _text("AGENTS.md")
    initialize = _text(".claude/skills/initialize/SKILL.md")
    consult = _text(".claude/skills/consult/SKILL.md")

    assert not (ROOT / "CLAUDE.md").exists()
    assert not (ROOT / ".mcp.json").exists()
    assert "Before reading or trusting any identity in `OPERATOR.md`" in contract
    assert contract.index("runtime/.danus-initialized") < contract.index(
        "read `OPERATOR.md` as the durable operator profile"
    )
    assert "reasoning_first_v1" in contract
    assert "`off` is the default" in contract
    for obsolete in ("@OPERATOR.md", "AskUserQuestion", "/loop"):
        assert obsolete not in contract
        assert obsolete not in initialize
        assert obsolete not in consult


def test_public_operator_profile_is_blank_and_uninitialized_cli_fails_closed(
    tmp_path: Path,
):
    operator = _text("OPERATOR.md")
    initialize = _text(".claude/skills/initialize/SKILL.md")
    assert "Name / how to address:** _(ask once; fill in)_" in operator
    assert "Felix" not in operator
    assert "filled `OPERATOR.md` belongs only to this deployment branch" in initialize

    env = os.environ.copy()
    env["DANUS_RUNTIME"] = str(tmp_path / "fresh-runtime")
    result = subprocess.run(
        [str(ROOT / "bin" / "danus"), "start", "copied-project"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 78
    assert "deployment identity is not initialized" in result.stderr
    assert "Do not trust any name already present in OPERATOR.md" in result.stderr

    runtime = tmp_path / "fresh-runtime"
    runtime.mkdir()
    (runtime / ".danus-initialized").write_text("legacy timestamp\n", encoding="utf-8")
    blank_profile = subprocess.run(
        [str(ROOT / "bin" / "danus"), "start", "copied-project"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert blank_profile.returncode == 78
    assert "deployment identity is not initialized" in blank_profile.stderr


def test_codex_main_agent_wires_expected_skills_and_mcp_servers():
    skills = ROOT / ".agents" / "skills"
    assert skills.is_symlink()
    assert skills.readlink() == Path("../.claude/skills")
    assert (skills / "initialize" / "SKILL.md").is_file()

    config = _text(".codex/config.toml")
    for server, command in (
        ("danus", "bin/danus-mcp"),
        ("write-paper", "bin/write-paper-mcp"),
        ("human-summary", "bin/human-summary-mcp"),
    ):
        assert f"[mcp_servers.{server}]" in config
        assert f'command = "{command}"' in config
        assert f"[mcp_servers.{server}.env]" in config
    assert config.count('DANUS_ROLE = "main"') == 3
    assert config.count('DANUS_AUTHOR = "main_agent"') == 3
    assert config.count('default_tools_approval_mode = "approve"') == 3
    assert config.count("required = true") == 3


def test_unattended_codex_launcher_and_legacy_consult_default_are_safe():
    launcher = _text("examples/ops/main-agent-tmux.sh")
    strategy_loop = _text("examples/ops/strategy-loop.sh")

    assert 'command -v codex' in launcher
    assert 'codex --dangerously-bypass-approvals-and-sandbox' in launcher
    assert 'command -v claude' not in launcher
    assert 'TRANSPORT="${DANUS_CONSULT_TRANSPORT:-off}"' in strategy_loop
