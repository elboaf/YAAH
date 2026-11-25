"""#844 Stage B: the completion verifier engine (prompt, parser, call)."""
import pytest

from backend.agent import verify
from backend.agent import model_client


# ------------------------------------------------------------- prompt build

def test_prompt_is_two_messages_with_untrusted_objective():
    msgs = verify.format_verification_prompt("Fix bug A", "did stuff")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    # objective framed as untrusted data, delimited
    assert "<untrusted_objective>" in msgs[1]["content"]
    assert "<objective>\nFix bug A\n</objective>" in msgs[1]["content"]
    assert "<transcript>" in msgs[1]["content"]
    # untrusted-data wording is carried by the system prompt
    assert "user-provided data" in msgs[0]["content"]


def test_prompt_encodes_the_adversarial_audit_rules():
    system = verify.format_verification_prompt("o", "t")[0]["content"]
    # restate as deliverables + prompt-to-artifact checklist
    assert "deliverables" in system.lower() or "success criteria" in system.lower()
    assert "prompt-to-artifact" in system
    # reject proxy signals explicitly
    assert "plan" in system and "todo" in system
    assert "do not treat" in system.lower()
    # fail closed on uncertainty
    assert "in doubt" in system.lower()
    # strict output contract
    assert '"passed"' in system and '"nextAction"' in system


# ----------------------------------------------------------------- parsing

def test_parse_bare_json_verdict():
    r = verify.parse_verification_result('{"passed": true, "reason": "ok", "nextAction": ""}')
    assert r == {"passed": True, "reason": "ok", "nextAction": ""}


def test_parse_json_in_code_fence_with_prose():
    text = (
        "Here is my verdict:\n```json\n"
        '{"passed": false, "reason": "file missing", "nextAction": "create config.py"}\n'
        "```\nHope that helps."
    )
    r = verify.parse_verification_result(text)
    assert r["passed"] is False
    assert r["reason"] == "file missing"
    assert r["nextAction"] == "create config.py"


def test_parse_defaults_missing_next_action():
    r = verify.parse_verification_result('{"passed": false, "reason": "x"}')
    assert r["passed"] is False
    assert r["nextAction"] == verify._DEFAULT_NEXT_ACTION


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "no json here at all",
        '{"reason": "no passed field"}',   # passed missing -> infra error
        "```",                              # only a fence, no object
    ],
)
def test_parse_infra_failures_fail_open(raw):
    r = verify.parse_verification_result(raw)
    assert r.get("error") is True


def test_parse_coerces_non_bool_passed():
    assert verify.parse_verification_result('{"passed": "true"}')["passed"] is True
    assert verify.parse_verification_result('{"passed": 0}')["passed"] is False
    assert verify.parse_verification_result('{"passed": 1}')["passed"] is True


def test_parse_loose_keys_when_json_not_clean():
    text = 'passed: true, reason: "looks done", nextAction: "nothing"'
    r = verify.parse_verification_result(text)
    assert r["passed"] is True
    assert r["reason"] == "looks done"


# -------------------------------------------------------------- the call

def _fake_chat_factory(verdict_text):
    async def _chat(messages, tools=None, stream=False, model="", effort=None, max_tokens=None):
        # the verifier must be tool-less
        assert tools is None
        assert stream is False
        return {"choices": [{"message": {"content": verdict_text}}]}
    return _chat


@pytest.mark.asyncio
async def test_run_verifier_success(monkeypatch):
    monkeypatch.setattr(
        model_client, "chat", _fake_chat_factory('{"passed": true, "reason": "done", "nextAction": ""}')
    )
    r = await verify.run_completion_verifier("do X", "did X")
    assert r == {"passed": True, "reason": "done", "nextAction": ""}


@pytest.mark.asyncio
async def test_run_verifier_provider_error_fails_open(monkeypatch):
    async def _chat(messages, tools=None, stream=False, model="", effort=None, max_tokens=None):
        raise model_client.ModelError("500 server error")
    monkeypatch.setattr(model_client, "chat", _chat)
    r = await verify.run_completion_verifier("do X", "did X")
    # fail open: an infra error is NOT a failed verdict
    assert r.get("error") is True
    assert "passed" not in r


@pytest.mark.asyncio
async def test_run_verifier_bad_output_fails_open(monkeypatch):
    monkeypatch.setattr(
        model_client, "chat", _fake_chat_factory("garbage, no verdict")
    )
    r = await verify.run_completion_verifier("do X", "did X")
    assert r.get("error") is True


@pytest.mark.asyncio
async def test_run_verifier_returns_a_real_failed_verdict(monkeypatch):
    monkeypatch.setattr(
        model_client,
        "chat",
        _fake_chat_factory('{"passed": false, "reason": "no evidence", "nextAction": "write the file"}'),
    )
    r = await verify.run_completion_verifier("do X", "claimed done")
    assert r["passed"] is False
    assert r["nextAction"] == "write the file"
