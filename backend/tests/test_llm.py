"""Tests for backend/services/llm.py — mocked OpenAI client, no network."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from backend.services import llm
from backend.services.llm import (
    ClarityResult,
    LLMError,
    PrdRequirementItem,
    TestEnvironmentResult,
    check_clarity,
    check_test_environment,
    diagnose_and_fix_script,
    generate_env_vars,
    generate_test_plan,
    generate_test_script,
    revise_requirement,
    revise_test_environment,
    revise_test_plan,
    split_prd,
)
from backend.services.llm_prompts import TestCaseLike


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

    def test_survives_broken_ssl_cert_file(self, monkeypatch, tmp_path):
        # conda on Windows can leave SSL_CERT_FILE pointing at a missing file
        # (conftest normally deletes it — re-break it here on purpose).
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-cacert.pem"))
        monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(llm, "_client", None)
        assert llm._get_client() is not None


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


class TestSplitPrd:
    def test_parses_requirements(self, stub_client):
        stub_client.content = json.dumps(
            {
                "requirements": [
                    {"name": "Login", "description": "Users can log in."},
                    {"name": "Upload", "description": "Users can upload files."},
                ]
            }
        )
        result = split_prd("PRD text", None, None)
        assert result.requirements == [
            PrdRequirementItem(name="Login", description="Users can log in."),
            PrdRequirementItem(name="Upload", description="Users can upload files."),
        ]

    def test_strips_whitespace_and_drops_blank_items(self, stub_client):
        stub_client.content = json.dumps(
            {
                "requirements": [
                    {"name": "  Login  ", "description": "  Users can log in.  "},
                    {"name": "   ", "description": ""},
                ]
            }
        )
        result = split_prd("PRD text", None, None)
        assert result.requirements == [
            PrdRequirementItem(name="Login", description="Users can log in.")
        ]

    def test_empty_list_returned_without_raising(self, stub_client):
        stub_client.content = json.dumps({"requirements": []})
        assert split_prd("Not a PRD", None, None).requirements == []

    def test_partially_empty_item_raises(self, stub_client):
        stub_client.content = json.dumps({"requirements": [{"name": "Login", "description": " "}]})
        with pytest.raises(LLMError, match="missing name or description"):
            split_prd("PRD text", None, None)

    def test_malformed_json_raises(self, stub_client):
        stub_client.content = "not json"
        with pytest.raises(LLMError, match="malformed"):
            split_prd("PRD text", None, None)

    def test_missing_requirements_key_raises(self, stub_client):
        stub_client.content = json.dumps({"items": []})
        with pytest.raises(LLMError, match="malformed"):
            split_prd("PRD text", None, None)

    def test_prompt_includes_prd_and_context(self, stub_client):
        stub_client.content = json.dumps({"requirements": []})
        split_prd("The PRD body", "# My README", "src/app.py")
        prompt = _user_prompt(stub_client)
        assert "The PRD body" in prompt
        assert "# My README" in prompt
        assert "src/app.py" in prompt


_REQS = [("Login", "Users can log in."), ("Search", "Users can search products.")]


class TestCheckTestEnvironment:
    def test_parses_sufficient_result(self, stub_client):
        stub_client.content = json.dumps({"sufficient": True, "clarifying_question": None})
        result = check_test_environment("SSH to staging as qa.", _REQS, None, None)
        assert result == TestEnvironmentResult(sufficient=True, clarifying_question=None)

    def test_parses_insufficient_result(self, stub_client):
        stub_client.content = json.dumps(
            {"sufficient": False, "clarifying_question": "What are the credentials?"}
        )
        result = check_test_environment("SSH to staging.", _REQS, None, None)
        assert result.sufficient is False
        assert result.clarifying_question == "What are the credentials?"

    def test_insufficient_without_question_raises(self, stub_client):
        stub_client.content = json.dumps({"sufficient": False, "clarifying_question": None})
        with pytest.raises(LLMError, match="no clarifying question"):
            check_test_environment("SSH to staging.", _REQS, None, None)

    def test_malformed_json_raises(self, stub_client):
        stub_client.content = "not json at all"
        with pytest.raises(LLMError, match="malformed"):
            check_test_environment("SSH to staging.", _REQS, None, None)

    def test_requirements_and_contexts_in_prompt(self, stub_client):
        stub_client.content = json.dumps({"sufficient": True, "clarifying_question": None})
        check_test_environment("SSH to staging.", _REQS, "# My README", "src/app.py\nsrc/db.py")
        prompt = _user_prompt(stub_client)
        assert "Login: Users can log in." in prompt
        assert "Search: Users can search products." in prompt
        assert "# My README" in prompt
        assert "src/app.py" in prompt
        assert "SSH to staging." in prompt

    def test_long_readme_truncated(self, stub_client):
        stub_client.content = json.dumps({"sufficient": True, "clarifying_question": None})
        check_test_environment("SSH to staging.", _REQS, "x" * 20000, None)
        assert len(_user_prompt(stub_client)) < 20000


class TestReviseTestEnvironment:
    def test_parses_rewritten_result(self, stub_client):
        stub_client.content = json.dumps(
            {
                "sufficient": True,
                "clarifying_question": None,
                "rewritten_content": "SSH to staging.example.com as qa with key ~/.ssh/qa.",
            }
        )
        result = revise_test_environment(
            "SSH to staging.", "Which host?", "staging.example.com", _REQS, None, None
        )
        assert result.sufficient is True
        assert result.rewritten_content == "SSH to staging.example.com as qa with key ~/.ssh/qa."

    def test_question_answer_and_requirements_in_prompt(self, stub_client):
        stub_client.content = json.dumps(
            {"sufficient": True, "clarifying_question": None, "rewritten_content": "New."}
        )
        revise_test_environment(
            "SSH to staging.", "Which host?", "staging.example.com", _REQS, None, None
        )
        prompt = _user_prompt(stub_client)
        assert "Which host?" in prompt
        assert "staging.example.com" in prompt
        assert "Login: Users can log in." in prompt

    def test_missing_rewrite_raises(self, stub_client):
        stub_client.content = json.dumps({"sufficient": True, "clarifying_question": None})
        with pytest.raises(LLMError, match="rewritten content"):
            revise_test_environment("SSH.", "Q?", "A.", _REQS, None, None)

    def test_insufficient_without_question_raises(self, stub_client):
        stub_client.content = json.dumps(
            {"sufficient": False, "clarifying_question": None, "rewritten_content": "New."}
        )
        with pytest.raises(LLMError, match="no clarifying question"):
            revise_test_environment("SSH.", "Q?", "A.", _REQS, None, None)


# ── Test plan generation (bounded read_file tool loop) ────────────────


_PLAN_PAYLOAD = {
    "complexity": "medium",
    "summary": "Covers the login flows.",
    "cases": [
        {
            "title": "Valid login",
            "preconditions": "A registered user exists.",
            "steps": ["Open the login page", "Enter valid credentials", "Submit"],
            "expected_result": "User lands on the dashboard.",
            "case_type": "functional",
            "priority": "high",
        }
    ],
}


def _plan_payload(**overrides) -> dict:
    """A valid plan payload with top-level and first-case overrides applied."""
    payload = json.loads(json.dumps(_PLAN_PAYLOAD))
    case_overrides = overrides.pop("case", {})
    payload.update(overrides)
    if payload["cases"]:
        payload["cases"][0].update(case_overrides)
    return payload


def _final_response(payload) -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call_response(*paths: str) -> SimpleNamespace:
    tool_calls = [
        SimpleNamespace(
            id=f"call_{i}",
            type="function",
            function=SimpleNamespace(name="read_file", arguments=json.dumps({"path": path})),
        )
        for i, path in enumerate(paths)
    ]
    message = SimpleNamespace(content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _SequenceClient:
    """Returns queued responses (or raises queued exceptions) per create() call."""

    def __init__(self, *items):
        self.items = list(items)
        self.requests: list[dict] = []

        stub = self

        class _Completions:
            def create(self, **kwargs):
                stub.requests.append(kwargs)
                item = stub.items.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        self.chat = SimpleNamespace(completions=_Completions())


def _sequence_client(monkeypatch, *items) -> _SequenceClient:
    client = _SequenceClient(*items)
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    return client


def _bad_request_error(message: str = "tools are not supported") -> openai.BadRequestError:
    request = httpx.Request("POST", "https://api.test/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError(message, response=response, body=None)


def _generate(**overrides):
    kwargs = {
        "name": "Login",
        "description": "Users can log in.",
        "sibling_names": ["Search", "Checkout"],
        "test_env_content": "SSH to staging as qa.",
        "readme": None,
        "file_tree": None,
        "read_file": lambda path: "FILE CONTENT",
        "on_round": lambda: None,
    }
    kwargs.update(overrides)
    return generate_test_plan(**kwargs)


class TestGenerateTestPlan:
    def test_happy_path_without_tool_calls(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response(_plan_payload()))

        result = _generate()

        assert result.complexity == "medium"
        assert result.summary == "Covers the login flows."
        assert len(result.cases) == 1
        assert result.cases[0].title == "Valid login"
        assert result.cases[0].steps == ["Open the login page", "Enter valid credentials", "Submit"]
        assert len(client.requests) == 1
        # Every round sends tools together with strict JSON mode — the
        # DeepSeek spike (2026-07-16) confirmed the combination works and
        # that omitting response_format yields unparseable final answers.
        tools = client.requests[0]["tools"]
        assert [t["function"]["name"] for t in tools] == ["read_file"]
        assert client.requests[0]["response_format"] == {"type": "json_object"}

    def test_tool_round_trip(self, monkeypatch):
        client = _sequence_client(
            monkeypatch,
            _tool_call_response("src/app.py"),
            _final_response(_plan_payload()),
        )
        read_paths: list[str] = []

        def read_file(path: str) -> str:
            read_paths.append(path)
            return "FILE CONTENT"

        result = _generate(read_file=read_file)

        assert result.complexity == "medium"
        assert read_paths == ["src/app.py"]
        assert len(client.requests) == 2
        tool_messages = [
            m
            for m in client.requests[1]["messages"]
            if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "tool"
        ]
        assert len(tool_messages) == 1
        assert "FILE CONTENT" in tool_messages[0]["content"]

    def test_on_round_called_after_every_api_round(self, monkeypatch):
        _sequence_client(
            monkeypatch,
            _tool_call_response("src/app.py"),
            _final_response(_plan_payload()),
        )
        rounds = []

        _generate(on_round=lambda: rounds.append(1))

        assert len(rounds) == 2

    def test_total_budget_stated_in_initial_prompt(self, monkeypatch):
        monkeypatch.setattr(llm, "TEST_PLAN_TOOL_ROUNDS", 3)
        client = _sequence_client(monkeypatch, _final_response(_plan_payload()))

        _generate()

        prompt = client.requests[0]["messages"][1]["content"]
        assert "up to 3" in prompt
        assert "read_file" in prompt

    def test_remaining_budget_appended_to_tool_result(self, monkeypatch):
        monkeypatch.setattr(llm, "TEST_PLAN_TOOL_ROUNDS", 3)
        client = _sequence_client(
            monkeypatch,
            _tool_call_response("src/app.py"),
            _final_response(_plan_payload()),
        )

        _generate()

        tool_messages = [
            m
            for m in client.requests[1]["messages"]
            if isinstance(m, dict) and m.get("role") == "tool"
        ]
        assert "2 of 3 rounds remaining" in tool_messages[-1]["content"]
        assert "FILE CONTENT" in tool_messages[-1]["content"]

    def test_round_cap_forces_final_json_answer(self, monkeypatch):
        monkeypatch.setattr(llm, "TEST_PLAN_TOOL_ROUNDS", 2)
        client = _sequence_client(
            monkeypatch,
            _tool_call_response("a.py"),
            _tool_call_response("b.py"),
            _final_response(_plan_payload()),
        )

        result = _generate()

        assert result.complexity == "medium"
        assert len(client.requests) == 3
        final = client.requests[2]
        assert final["tool_choice"] == "none"
        assert final["response_format"] == {"type": "json_object"}
        last_message = final["messages"][-1]
        assert last_message["role"] == "user"
        assert "exhaust" in last_message["content"].lower()

    def test_read_file_none_skips_tools(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response(_plan_payload()))

        result = _generate(read_file=None)

        assert result.complexity == "medium"
        assert len(client.requests) == 1
        assert "tools" not in client.requests[0]
        assert client.requests[0]["response_format"] == {"type": "json_object"}

    def test_bad_request_on_first_round_falls_back_to_no_tools(self, monkeypatch):
        client = _sequence_client(
            monkeypatch,
            _bad_request_error(),
            _final_response(_plan_payload()),
        )

        result = _generate()

        assert result.complexity == "medium"
        assert len(client.requests) == 2
        assert "tools" not in client.requests[1]
        assert client.requests[1]["response_format"] == {"type": "json_object"}

    def test_bad_request_after_first_round_raises(self, monkeypatch):
        _sequence_client(
            monkeypatch,
            _tool_call_response("src/app.py"),
            _bad_request_error(),
        )

        with pytest.raises(LLMError):
            _generate()

    def test_other_openai_error_raises_llm_error(self, monkeypatch):
        error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.test"))
        _sequence_client(monkeypatch, error)

        with pytest.raises(LLMError, match="LLM request failed"):
            _generate()

    def test_malformed_json_raises(self, monkeypatch):
        _sequence_client(monkeypatch, _final_response("not json at all"))

        with pytest.raises(LLMError, match="malformed"):
            _generate()

    @pytest.mark.parametrize(
        "payload",
        [
            _plan_payload(cases=[]),
            _plan_payload(case={"title": "   "}),
            _plan_payload(case={"steps": []}),
            _plan_payload(case={"steps": ["   ", ""]}),
            _plan_payload(case={"expected_result": "  "}),
            _plan_payload(case={"case_type": ""}),
            _plan_payload(case={"priority": "urgent"}),
            _plan_payload(complexity="extreme"),
        ],
        ids=[
            "empty-cases",
            "blank-title",
            "no-steps",
            "blank-steps",
            "blank-expected-result",
            "blank-case-type",
            "bad-priority",
            "bad-complexity",
        ],
    )
    def test_invalid_plan_raises(self, monkeypatch, payload):
        _sequence_client(monkeypatch, _final_response(payload))

        with pytest.raises(LLMError):
            _generate()

    def test_prompt_contains_all_context(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response(_plan_payload()))

        _generate(readme="# My README", file_tree="src/app.py\nsrc/db.py")

        prompt = client.requests[0]["messages"][1]["content"]
        assert "Login" in prompt
        assert "Users can log in." in prompt
        assert "Search" in prompt
        assert "Checkout" in prompt
        assert "SSH to staging as qa." in prompt
        assert "# My README" in prompt
        assert "src/app.py" in prompt

    def test_long_readme_truncated(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response(_plan_payload()))

        _generate(readme="x" * 20000)

        assert len(client.requests[0]["messages"][1]["content"]) < 20000


class TestReviseTestPlan:
    def _revise(self, **overrides):
        kwargs = {
            "name": "Login",
            "description": "Users can log in.",
            "sibling_names": ["Search"],
            "test_env_content": "SSH to staging as qa.",
            "readme": None,
            "file_tree": None,
            "current_plan_json": json.dumps(_PLAN_PAYLOAD),
            "feedback": "Add negative test cases for lockout.",
            "read_file": lambda path: "FILE CONTENT",
            "on_round": lambda: None,
        }
        kwargs.update(overrides)
        return revise_test_plan(**kwargs)

    def test_returns_validated_plan(self, monkeypatch):
        _sequence_client(monkeypatch, _final_response(_plan_payload()))

        result = self._revise()

        assert result.complexity == "medium"
        assert result.cases[0].title == "Valid login"

    def test_prompt_includes_current_plan_and_feedback(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response(_plan_payload()))

        self._revise()

        prompt = client.requests[0]["messages"][1]["content"]
        assert "Valid login" in prompt  # from the current plan JSON
        assert "Add negative test cases for lockout." in prompt

    def test_invalid_revision_raises(self, monkeypatch):
        _sequence_client(monkeypatch, _final_response(_plan_payload(cases=[])))

        with pytest.raises(LLMError):
            self._revise()


# ── Test execution (env-var extraction, script gen, self-heal diagnosis) ──


class TestGenerateEnvVars:
    def test_happy_path(self, stub_client):
        stub_client.content = json.dumps(
            {"variables": {"BASE_URL": "https://staging.example.com", "PASSWORD": "hunter2"}}
        )
        result = generate_env_vars("SSH to staging as qa.", None, None)
        assert result.variables == {
            "BASE_URL": "https://staging.example.com",
            "PASSWORD": "hunter2",
        }

    def test_empty_variables_raises(self, stub_client):
        stub_client.content = json.dumps({"variables": {}})
        with pytest.raises(LLMError):
            generate_env_vars("SSH to staging.", None, None)

    def test_blank_key_raises(self, stub_client):
        stub_client.content = json.dumps({"variables": {"  ": "value"}})
        with pytest.raises(LLMError):
            generate_env_vars("SSH to staging.", None, None)

    def test_blank_value_raises(self, stub_client):
        stub_client.content = json.dumps({"variables": {"BASE_URL": "  "}})
        with pytest.raises(LLMError):
            generate_env_vars("SSH to staging.", None, None)

    def test_malformed_json_raises(self, stub_client):
        stub_client.content = "not json at all"
        with pytest.raises(LLMError, match="malformed"):
            generate_env_vars("SSH to staging.", None, None)

    def test_prompt_contains_raw_content_verbatim(self, stub_client):
        stub_client.content = json.dumps({"variables": {"BASE_URL": "x"}})
        generate_env_vars(
            "SSH to staging.example.com as qa with key ~/.ssh/qa.",
            "# My README",
            "src/app.py",
        )
        prompt = _user_prompt(stub_client)
        assert "SSH to staging.example.com as qa with key ~/.ssh/qa." in prompt
        assert "# My README" in prompt
        assert "src/app.py" in prompt

    def test_no_tools_sent(self, stub_client):
        stub_client.content = json.dumps({"variables": {"BASE_URL": "x"}})
        generate_env_vars("SSH to staging.", None, None)
        assert "tools" not in stub_client.requests[-1]


_TEST_CASE = TestCaseLike(
    title="Valid login",
    preconditions="A registered user exists.",
    steps="Open the login page\nEnter valid credentials\nSubmit",
    expected_result="User lands on the dashboard.",
    case_type="functional",
    priority="high",
)


def _generate_script(**overrides):
    kwargs = {
        "name": "Login",
        "description": "Users can log in.",
        "test_case": _TEST_CASE,
        "env_var_names": ["BASE_URL", "PASSWORD"],
        "readme": None,
        "file_tree": None,
        "read_file": lambda path: "FILE CONTENT",
        "on_round": lambda: None,
    }
    kwargs.update(overrides)
    return generate_test_script(**kwargs)


class TestGenerateTestScript:
    def test_happy_path_without_tool_calls(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response({"script": "print('hello')"}))

        result = _generate_script()

        assert result.script == "print('hello')"
        assert len(client.requests) == 1
        tools = client.requests[0]["tools"]
        assert [t["function"]["name"] for t in tools] == ["read_file"]

    def test_tool_round_trip(self, monkeypatch):
        client = _sequence_client(
            monkeypatch,
            _tool_call_response("src/app.py"),
            _final_response({"script": "print('hello')"}),
        )
        read_paths: list[str] = []

        def read_file(path: str) -> str:
            read_paths.append(path)
            return "FILE CONTENT"

        result = _generate_script(read_file=read_file)

        assert result.script == "print('hello')"
        assert read_paths == ["src/app.py"]
        assert len(client.requests) == 2

    def test_round_cap_forces_final_answer(self, monkeypatch):
        monkeypatch.setattr(llm, "TEST_EXECUTION_TOOL_ROUNDS", 2)
        client = _sequence_client(
            monkeypatch,
            _tool_call_response("a.py"),
            _tool_call_response("b.py"),
            _final_response({"script": "print('hello')"}),
        )

        result = _generate_script()

        assert result.script == "print('hello')"
        assert len(client.requests) == 3
        assert client.requests[2]["tool_choice"] == "none"

    def test_read_file_none_skips_tools(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response({"script": "print('hello')"}))

        result = _generate_script(read_file=None)

        assert result.script == "print('hello')"
        assert "tools" not in client.requests[0]

    def test_blank_script_raises(self, monkeypatch):
        _sequence_client(monkeypatch, _final_response({"script": "   "}))

        with pytest.raises(LLMError):
            _generate_script()

    def test_prompt_contains_env_var_names_and_case_fields_but_not_secrets(self, monkeypatch):
        client = _sequence_client(monkeypatch, _final_response({"script": "print('hello')"}))

        _generate_script(readme="# My README", file_tree="src/app.py")

        prompt = client.requests[0]["messages"][1]["content"]
        assert "BASE_URL" in prompt
        assert "PASSWORD" in prompt
        assert "os.environ" in prompt or "environ" in prompt.lower()
        assert "Valid login" in prompt
        assert "User lands on the dashboard." in prompt
        assert "# My README" in prompt
        assert "src/app.py" in prompt


def _diagnose(**overrides):
    kwargs = {
        "name": "Login",
        "description": "Users can log in.",
        "test_case": _TEST_CASE,
        "env_var_names": ["BASE_URL", "PASSWORD"],
        "readme": None,
        "file_tree": None,
        "script": "print('hello')",
        "stdout": "",
        "stderr": "AssertionError: expected dashboard",
        "exit_code": 1,
        "read_file": lambda path: "FILE CONTENT",
        "on_round": lambda: None,
    }
    kwargs.update(overrides)
    return diagnose_and_fix_script(**kwargs)


class TestDiagnoseAndFixScript:
    def test_script_bug_returns_fix(self, monkeypatch):
        _sequence_client(
            monkeypatch,
            _final_response(
                {
                    "classification": "script_bug",
                    "fixed_script": "print('fixed')",
                    "explanation": "Wrong selector used.",
                }
            ),
        )

        result = _diagnose()

        assert result.classification == "script_bug"
        assert result.fixed_script == "print('fixed')"

    def test_app_bug_has_no_fix(self, monkeypatch):
        _sequence_client(
            monkeypatch,
            _final_response(
                {
                    "classification": "app_bug",
                    "fixed_script": None,
                    "explanation": "Login genuinely fails for valid credentials.",
                }
            ),
        )

        result = _diagnose()

        assert result.classification == "app_bug"
        assert result.fixed_script is None

    def test_script_bug_without_fix_raises(self, monkeypatch):
        _sequence_client(
            monkeypatch,
            _final_response(
                {"classification": "script_bug", "fixed_script": None, "explanation": "Broken."}
            ),
        )

        with pytest.raises(LLMError):
            _diagnose()

    def test_blank_explanation_raises(self, monkeypatch):
        _sequence_client(
            monkeypatch,
            _final_response(
                {"classification": "app_bug", "fixed_script": None, "explanation": "  "}
            ),
        )

        with pytest.raises(LLMError):
            _diagnose()

    def test_tool_round_trip(self, monkeypatch):
        client = _sequence_client(
            monkeypatch,
            _tool_call_response("src/app.py"),
            _final_response(
                {
                    "classification": "script_bug",
                    "fixed_script": "print('fixed')",
                    "explanation": "Wrong endpoint.",
                }
            ),
        )

        result = _diagnose()

        assert result.classification == "script_bug"
        assert len(client.requests) == 2

    def test_prompt_includes_script_and_output(self, monkeypatch):
        client = _sequence_client(
            monkeypatch,
            _final_response(
                {"classification": "app_bug", "fixed_script": None, "explanation": "Real bug."}
            ),
        )

        _diagnose()

        prompt = client.requests[0]["messages"][1]["content"]
        assert "print('hello')" in prompt
        assert "AssertionError: expected dashboard" in prompt
        assert "Exit code: 1" in prompt


class TestScriptPromptsContainSafetyInstructions:
    """Static assertions — Decision 16's precondition/cleanup contract."""

    def test_generation_prompt_mentions_preconditions_and_cleanup(self):
        from backend.services.llm_prompts import TEST_SCRIPT_SYSTEM_PROMPT

        assert "precondition" in TEST_SCRIPT_SYSTEM_PROMPT.lower()
        assert "try/finally" in TEST_SCRIPT_SYSTEM_PROMPT
        assert "os.environ" in TEST_SCRIPT_SYSTEM_PROMPT

    def test_diagnosis_prompt_mentions_preconditions_and_cleanup(self):
        from backend.services.llm_prompts import TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT

        assert "precondition" in TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT.lower()
        assert "try/finally" in TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT
        assert "os.environ" in TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT


class TestScriptPromptsAdvertiseAvailableLibraries:
    """Static assertions — the curated library set must reach both prompts,
    identically, so generation and diagnosis never drift apart on what's
    actually importable in the worker's venv."""

    @pytest.mark.parametrize("library", ["requests", "faker", "psycopg2", "sqlite3"])
    def test_generation_prompt_lists_libraries(self, library):
        from backend.services.llm_prompts import TEST_SCRIPT_SYSTEM_PROMPT

        assert library in TEST_SCRIPT_SYSTEM_PROMPT

    @pytest.mark.parametrize("library", ["requests", "faker", "psycopg2", "sqlite3"])
    def test_diagnosis_prompt_lists_libraries(self, library):
        from backend.services.llm_prompts import TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT

        assert library in TEST_SCRIPT_DIAGNOSIS_SYSTEM_PROMPT
