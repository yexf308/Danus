from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def test_reasoning_first_worker_contract_prioritizes_deep_work_over_churn():
    worker = _text("agents/contracts/worker.md")
    assert "reasoning_first_v1" in worker
    assert "at most one consolidated shareable proof/candidate checkpoint" in worker
    assert "Global memory is a coordination checkpoint, not a transcript" in worker
    assert "repeated broad polling is not reasoning" in worker
    assert "Do not perform a perfunctory literature search every phase" in worker
    assert "Always call `search_arxiv_theorems`" not in worker
    assert "just retry" not in worker


def test_root_critic_confirmation_cannot_auto_authorize_pro():
    worker = _text("agents/contracts/worker.md")
    main = _text("agents/contracts/main_agent.md")
    direct = _text("agents/skills/worker/direct-proving/SKILL.md")
    failures = _text("agents/skills/worker/identify-key-failures/SKILL.md")
    assert "pins the two `max` workers as deep root and independent critic" in main
    assert "`critic_obstacle_review`" in main
    assert "fixed critic receives a fresh review-phase thread" in main
    assert (
        "An unconfirmed terminal review advances to fresh root/critic reasoning" in main
    )
    assert "confirmation citing the same entry id and generation" in main
    assert (
        "The coordinator recommendation itself does not create this checkpoint" in main
    )
    assert "Root opinion alone" in main
    assert "confirms_entry_id" in worker
    assert "grants no browser or advisor authority" in worker
    assert "Neither record calls, prepares, or authorizes an advisor" in failures
    for skill in (direct, failures):
        assert 'links={"confirms_entry_id":"<exact returned root gm id>"}' in skill
        assert "inside the evidence JSON" in skill


def test_consult_is_event_driven_and_not_a_project_start_prerequisite():
    main = _text("agents/contracts/main_agent.md")
    consult = _text(".claude/skills/consult/SKILL.md")
    assert "A configured API/CLI consult is optional" in main
    assert "browser Pro is not a project-start prerequisite" in main
    assert "grants capability, not a schedule" in main
    assert "A clock neither authorizes nor forbids a consult" in consult
    assert "Never invoke browser Pro merely because the project is new" in consult
    assert "discuss the problem with both the model AND the human" not in consult


def test_candidate_is_consolidated_once_and_bound_to_fact_submit():
    direct = _text("agents/skills/worker/direct-proving/SKILL.md")
    verify = _text("agents/skills/worker/verify-proof/SKILL.md")
    assert "one consolidated record for the whole" in direct
    assert "not one record per subgoal" in direct
    assert "Use the returned global-memory `id` as the `source_id`" in direct
    assert 'source_id="<consolidated_entry_id>"' in verify
    assert 'verification_reuse: "active_exact_fact"' in verify
    assert "scheduler fields are performance telemetry" in verify


def test_browser_prepare_contract_pins_checkpoint_identity_everywhere():
    consult = _text(".claude/skills/consult/SKILL.md")
    browser = _text("docs/browser-advisor.md")
    tools = _text("docs/cli-and-tools.md")
    strategy = _text("danus/strategy/README.md")
    main = _text("agents/contracts/main_agent.md")

    for text in (consult, browser, tools):
        assert "--checkpoint-id" in text
        assert "--checkpoint-sha256" in text
        assert "--checkpoint-bytes" in text
    for text in (consult, browser, tools, strategy):
        assert "--browser-checkpoint-id" in text
        assert "--browser-checkpoint-sha256" in text
        assert "--browser-checkpoint-bytes" in text
    assert "prompt bytes must equal" in browser.lower()
    assert "prompt bytes must equal" in consult.lower()
    assert "canonical SHA-256" in main
    assert "checkpoint-bound v5 receipt" in browser
    assert "cannot authorize a new Send" in browser
