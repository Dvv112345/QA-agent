"""Tests for backend/services/llm.py — mocked OpenAI client, no network."""

import json
from types import SimpleNamespace

import pytest

from backend.services import llm
from backend.services.llm import ClarityResult, LLMError, check_clarity, revise_requirement


class _StubClient:
    """Records completion requests and returns a canned message content."""

    def __init__(self, content: str):
        self.content = content
        self.requests: list[dict] = []

        stub = self

        class _Completions:
            def create(self, **kwargs):
                stub.requests.append(kwargs)
                message = SimpleNamespace(content=stub.content)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture
def stub_client(monkeypatch):
    """Patch ``_get_client`` with a recording stub; tests set ``.content``."""
    client = _StubClient(json.dumps({"clear": True, "clarifying_question": None}))
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    return client


def _user_prompt(client: _StubClient) -> str:
    return client.requests[-1]["messages"][1]["content"]


class TestGetClient:
    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.setattr(llm, "OPENAI_API_KEY", "")
        monkeypatch.setattr(llm, "_client", None)
        with pytest.raises(LLMError, match="OPENAI_API_KEY"):
            llm._get_client()

    def test_builds_client_once(self, monkeypatch):
        monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(llm, "_client", None)
        first = llm._get_client()
        second = llm._get_client()
        assert first is second


class TestCheckClarity:
    def test_parses_clear_result(self, stub_client):
        result = check_clarity("Login", "Users can log in.", None, None)
        assert result == ClarityResult(clear=True, clarifying_question=None)

    def test_parses_unclear_result(self, stub_client):
        stub_client.content = json.dumps({"clear": False, "clarifying_question": "Which users?"})
        result = check_clarity("Login", "Users can log in.", None, None)
        assert result.clear is False
        assert result.clarifying_question == "Which users?"

    def test_malformed_json_raises(self, stub_client):
        stub_client.content = "not json at all"
        with pytest.raises(LLMError, match="malformed"):
            check_clarity("Login", "desc", None, None)

    def test_missing_fields_raise(self, stub_client):
        stub_client.content = json.dumps({"clarifying_question": None})
        with pytest.raises(LLMError, match="malformed"):
            check_clarity("Login", "desc", None, None)

    def test_unclear_without_question_raises(self, stub_client):
        stub_client.content = json.dumps({"clear": False, "clarifying_question": None})
        with pytest.raises(LLMError, match="no clarifying question"):
            check_clarity("Login", "desc", None, None)

    def test_readme_included_when_present(self, stub_client):
        check_clarity("Login", "desc", "# My README", None)
        prompt = _user_prompt(stub_client)
        assert "# My README" in prompt
        assert "file tree" not in prompt.lower()

    def test_file_tree_included_when_present(self, stub_client):
        check_clarity("Login", "desc", None, "src/app.py\nsrc/db.py")
        prompt = _user_prompt(stub_client)
        assert "src/app.py" in prompt
        assert "README" not in prompt

    def test_both_contexts_omitted_when_absent(self, stub_client):
        check_clarity("Login", "desc", None, None)
        prompt = _user_prompt(stub_client)
        assert "README" not in prompt
        assert "file tree" not in prompt.lower()

    def test_long_readme_truncated(self, stub_client):
        check_clarity("Login", "desc", "x" * 20000, None)
        assert len(_user_prompt(stub_client)) < 20000

    def test_uses_json_object_response_format(self, stub_client):
        check_clarity("Login", "desc", None, None)
        assert stub_client.requests[-1]["response_format"] == {"type": "json_object"}


class TestReviseRequirement:
    def test_parses_rewritten_result(self, stub_client):
        stub_client.content = json.dumps(
            {
                "clear": True,
                "clarifying_question": None,
                "rewritten_description": "All registered users can log in via SSO.",
            }
        )
        result = revise_requirement(
            "Login", "Users can log in.", "Which users?", "Registered ones", None, None
        )
        assert result.clear is True
        assert result.rewritten_description == "All registered users can log in via SSO."

    def test_question_and_answer_in_prompt(self, stub_client):
        stub_client.content = json.dumps(
            {"clear": True, "clarifying_question": None, "rewritten_description": "New."}
        )
        revise_requirement("Login", "desc", "Which users?", "Registered ones", None, None)
        prompt = _user_prompt(stub_client)
        assert "Which users?" in prompt
        assert "Registered ones" in prompt

    def test_missing_rewrite_raises(self, stub_client):
        stub_client.content = json.dumps({"clear": True, "clarifying_question": None})
        with pytest.raises(LLMError, match="rewritten description"):
            revise_requirement("Login", "desc", "Q?", "A.", None, None)
