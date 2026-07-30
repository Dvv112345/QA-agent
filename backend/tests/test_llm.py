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
from backend.services.llm_prompts import EXPLORATION_SYSTEM_PROMPT, TestCaseLike


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


# ── Exploratory testing ───────────────────────────────────────────────


class _ScriptedClient:
    """Returns a queued sequence of responses; records every request.

    The exploration loop is a multi-round conversation, so unlike
    ``_StubClient`` it must hand back a different message each round.
    """

    def __init__(self, responses: list, prompt_tokens=None):
        self.responses = list(responses)
        self.requests: list[dict] = []
        # None models a provider that reports no usage, which is what drives
        # the char-estimate fallback. A list is consumed per call with the
        # last value sticky, so a test can let context grow before the limit
        # is crossed — as it does in reality.
        self.prompt_tokens = prompt_tokens

        stub = self

        def next_usage():
            if stub.prompt_tokens is None:
                return None
            if isinstance(stub.prompt_tokens, int):
                return SimpleNamespace(prompt_tokens=stub.prompt_tokens)
            value = stub.prompt_tokens[0]
            if len(stub.prompt_tokens) > 1:
                stub.prompt_tokens = stub.prompt_tokens[1:]
            return SimpleNamespace(prompt_tokens=value)

        class _Completions:
            def create(self, **kwargs):
                stub.requests.append(kwargs)
                if not stub.responses:
                    raise AssertionError("scripted client ran out of responses")
                message = stub.responses.pop(0)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message)], usage=next_usage()
                )

        self.chat = SimpleNamespace(completions=_Completions())


def _tool_call(call_id: str, name: str, **arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _acting(*tool_calls):
    """An assistant message that calls tools."""
    return SimpleNamespace(content=None, tool_calls=list(tool_calls))


def _answering(content: str):
    """An assistant message with no tool calls."""
    return SimpleNamespace(content=content, tool_calls=None)


def _scripted(monkeypatch, responses, prompt_tokens=None):
    client = _ScriptedClient(responses, prompt_tokens=prompt_tokens)
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    return client


def _run_loop(
    tools,
    max_actions=25,
    snapshot_window=3,
    rounds=None,
    base_urls=("https://app.test",),
    secret_values=None,
    max_free_recordings=llm.EXPLORATORY_MAX_FINDINGS,
    context_token_limit=llm.EXPLORATORY_CONTEXT_TOKEN_LIMIT,
):
    """Invoke the loop with sensible defaults for the fields under test."""
    return llm.run_exploration_loop(
        name="Export reports",
        description="Users can export reports as CSV",
        charter="Explore the export flow with unusual data",
        sfdipot_areas=["Data"],
        base_urls=list(base_urls),
        env_var_names=["APP_URL", "ADMIN_PASSWORD"],
        readme=None,
        file_tree=None,
        tools=tools,
        max_actions=max_actions,
        snapshot_window=snapshot_window,
        on_round=(rounds.append) if rounds is not None else (lambda _actions: None),
        secret_values=secret_values,
        max_free_recordings=max_free_recordings,
        context_token_limit=context_token_limit,
    )


class TestGenerateCharters:
    def _payload(self, **overrides):
        payload = {
            "charters": [
                {"charter": "Explore export triggers", "sfdipot_areas": ["Function"]},
                {"charter": "Explore export with edge data", "sfdipot_areas": ["Data"]},
            ],
            "base_url_env_vars": ["APP_URL"],
        }
        payload.update(overrides)
        return json.dumps(payload)

    def test_parses_charters_and_url_vars(self, stub_client):
        stub_client.content = self._payload()

        result = llm.generate_charters(
            name="Export reports",
            description="Users can export reports as CSV",
            covered_cases=[],
            env_var_names=["APP_URL"],
            readme=None,
            file_tree=None,
        )

        assert [c.charter for c in result.charters] == [
            "Explore export triggers",
            "Explore export with edge data",
        ]
        assert result.charters[1].sfdipot_areas == ["Data"]
        assert result.base_url_env_vars == ["APP_URL"]

    def test_covered_cases_reach_the_prompt(self, stub_client):
        stub_client.content = self._payload()

        llm.generate_charters(
            name="Export reports",
            description="Users can export reports as CSV",
            covered_cases=[
                TestCaseLike(
                    title="Export with one row",
                    preconditions=None,
                    steps="Click export",
                    expected_result="A CSV downloads",
                    case_type="functional",
                    priority="high",
                )
            ],
            env_var_names=["APP_URL"],
            readme=None,
            file_tree=None,
        )

        prompt = _user_prompt(stub_client)
        assert "already covered" in prompt
        assert "Export with one row" in prompt
        assert "APP_URL" in prompt

    def test_rejects_empty_charter_list(self, stub_client):
        stub_client.content = self._payload(charters=[])
        with pytest.raises(LLMError, match="no charters"):
            llm.generate_charters("R", "D", [], ["APP_URL"], None, None)

    def test_rejects_over_cap(self, stub_client, monkeypatch):
        monkeypatch.setattr(llm, "EXPLORATORY_MAX_CHARTERS", 2)
        stub_client.content = self._payload(
            charters=[{"charter": f"Charter {i}", "sfdipot_areas": ["Function"]} for i in range(3)]
        )
        with pytest.raises(LLMError, match="above the cap"):
            llm.generate_charters("R", "D", [], ["APP_URL"], None, None)

    def test_rejects_blank_charter(self, stub_client):
        stub_client.content = self._payload(
            charters=[{"charter": "   ", "sfdipot_areas": ["Function"]}]
        )
        with pytest.raises(LLMError, match="blank charter"):
            llm.generate_charters("R", "D", [], ["APP_URL"], None, None)

    def test_rejects_unknown_sfdipot_area(self, stub_client):
        stub_client.content = self._payload(
            charters=[{"charter": "Explore", "sfdipot_areas": ["Usability"]}]
        )
        with pytest.raises(LLMError, match="unknown SFDIPOT area"):
            llm.generate_charters("R", "D", [], ["APP_URL"], None, None)

    def test_rejects_empty_url_var_list(self, stub_client):
        stub_client.content = self._payload(base_url_env_vars=[])
        with pytest.raises(LLMError, match="no environment variable"):
            llm.generate_charters("R", "D", [], ["APP_URL"], None, None)


class TestSummarizeExploration:
    def _session(self, **overrides):
        from backend.services.llm_prompts import ExploratorySessionLike, FindingLike

        defaults = {
            "charter": "Explore export with edge data",
            "sfdipot_areas": ["Data"],
            "status": "completed",
            "actions_used": 18,
            "stop_reason": "charter_complete",
            "session_notes": "Exported with zero rows; file had no header.",
            "findings": [
                FindingLike(
                    finding_type="bug",
                    severity="high",
                    title="Empty export omits header row",
                    expected="A header row is always present",
                    actual="Zero-byte file",
                )
            ],
        }
        defaults.update(overrides)
        return ExploratorySessionLike(**defaults)

    def test_parses_summary(self, stub_client):
        stub_client.content = json.dumps({"summary": "Export is broken for empty result sets."})
        result = llm.summarize_exploration("Export", "Users can export", [self._session()])
        assert result.summary == "Export is broken for empty result sets."

    def test_session_sheets_reach_the_prompt(self, stub_client):
        stub_client.content = json.dumps({"summary": "ok"})
        llm.summarize_exploration("Export", "Users can export", [self._session()])

        prompt = _user_prompt(stub_client)
        assert "Explore export with edge data" in prompt
        assert "Empty export omits header row" in prompt
        assert "no header" in prompt

    def test_blank_summary_raises(self, stub_client):
        stub_client.content = json.dumps({"summary": "   "})
        with pytest.raises(LLMError, match="blank exploration summary"):
            llm.summarize_exploration("Export", "Users can export", [self._session()])


class TestRunExplorationLoop:
    def test_dispatches_tool_and_feeds_result_back(self, monkeypatch):
        client = _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "snapshot")),
                _acting(_tool_call("c2", "finish_session", notes="Done exploring.")),
            ],
        )
        calls = []
        tools = {"snapshot": lambda **kw: calls.append(kw) or "- button 'Export' [ref=e3]"}

        result = _run_loop(tools)

        assert calls == [{}]
        assert result.notes == "Done exploring."
        assert result.stop_reason == llm.STOP_CHARTER_COMPLETE
        assert result.actions_used == 1
        # The snapshot's output was fed back as a tool message.
        tool_messages = [
            m
            for m in client.requests[-1]["messages"]
            if isinstance(m, dict) and m.get("role") == "tool"
        ]
        assert "ref=e3" in tool_messages[0]["content"]

    def test_finish_session_does_not_consume_an_action(self, monkeypatch):
        _scripted(monkeypatch, [_acting(_tool_call("c1", "finish_session", notes="Nothing here."))])
        result = _run_loop({})
        assert result.actions_used == 0
        assert result.stop_reason == llm.STOP_CHARTER_COMPLETE

    def test_base_urls_reach_the_prompt(self, monkeypatch):
        """Without this the model only sees variable names, never the app's URL."""
        client = _scripted(monkeypatch, [_acting(_tool_call("c1", "finish_session", notes="x"))])

        _run_loop({}, base_urls=["https://app.test", "https://api.test"])

        prompt = client.requests[0]["messages"][1]["content"]
        assert "https://app.test" in prompt
        assert "https://api.test" in prompt
        # The first one is where the browser already is — say so, since the
        # ordering is what BrowserSession.__enter__ acts on.
        assert "already open" in prompt

    def test_no_base_urls_omits_the_block(self, monkeypatch):
        client = _scripted(monkeypatch, [_acting(_tool_call("c1", "finish_session", notes="x"))])
        _run_loop({}, base_urls=[])
        assert "Application under test:" not in client.requests[0]["messages"][1]["content"]

    def test_action_cap_forces_wrap_up(self, monkeypatch):
        client = _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "snapshot")),
                _acting(_tool_call("c2", "snapshot")),
                _answering(json.dumps({"notes": "Ran out of budget.", "stop_reason": "cap"})),
            ],
        )
        tools = {"snapshot": lambda **kw: "page"}

        result = _run_loop(tools, max_actions=2)

        assert result.actions_used == 2
        assert result.stop_reason == llm.STOP_ACTION_CAP
        assert result.notes == "Ran out of budget."
        # The forced final call must disable tools.
        assert client.requests[-1]["tool_choice"] == "none"

    def test_acting_rounds_do_not_use_json_mode(self, monkeypatch):
        """JSON mode tells the model to emit content; acting rounds want a tool call.

        Regression test for sessions ending at zero actions because the model
        answered with {"tool": "snapshot", "params": {}} as message content
        instead of calling the tool.
        """
        client = _scripted(monkeypatch, [_acting(_tool_call("c1", "finish_session", notes="x"))])
        _run_loop({})
        assert "response_format" not in client.requests[0]

    def test_a_tool_free_response_is_nudged_not_fatal(self, monkeypatch):
        """One content-only reply must not cost the whole charter."""
        client = _scripted(
            monkeypatch,
            [
                # Exactly what DeepSeek returned in the wild.
                _answering('{"tool": "snapshot", "params": {}}'),
                _acting(_tool_call("c1", "snapshot")),
                _acting(_tool_call("c2", "finish_session", notes="Explored it.")),
            ],
        )

        result = _run_loop({"snapshot": lambda **kw: "page"})

        assert result.actions_used == 1  # recovered and actually explored
        assert result.notes == "Explored it."
        nudges = [
            m
            for m in client.requests[-1]["messages"]
            if isinstance(m, dict)
            and m.get("role") == "user"
            and "did not call a tool" in m["content"]
        ]
        assert len(nudges) == 1

    def test_two_tool_free_responses_end_the_session(self, monkeypatch):
        """Twice in a row, take the model at its word."""
        _scripted(
            monkeypatch,
            [_answering('{"notes": "I have finished exploring."}')] * 2,
        )
        result = _run_loop({})
        assert result.stop_reason == llm.STOP_MODEL_STOPPED
        # Parsed, not stored raw — otherwise the session sheet shows JSON.
        assert result.notes == "I have finished exploring."

    def test_answer_that_is_not_the_wrap_up_shape_falls_back_to_raw_text(self, monkeypatch):
        """Never lose what the model said, even off-contract."""
        _scripted(monkeypatch, [_answering("I have finished exploring.")] * 2)
        result = _run_loop({})
        assert result.notes == "I have finished exploring."

    def test_empty_answer_gets_a_placeholder(self, monkeypatch):
        _scripted(monkeypatch, [_answering("")] * 2)
        result = _run_loop({})
        assert result.notes == "(model ended the session without notes)"

    def test_record_finding_does_not_consume_an_action(self, monkeypatch):
        """Findings are the deliverable — they must not compete with exploring."""
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "snapshot")),
                _acting(_tool_call("c2", "record_finding", title="Export drops a row")),
                _acting(_tool_call("c3", "finish_session", notes="done")),
            ],
        )

        result = _run_loop(
            {"snapshot": lambda **kw: "page", "record_finding": lambda **kw: "Recorded."}
        )

        assert result.actions_used == 1  # the snapshot only
        assert any("record_finding" in line for line in result.action_log)

    def test_recordings_stop_being_free_past_the_cap(self, monkeypatch):
        """The termination guarantee: an always-free non-terminal tool never exits.

        ``actions_used < max_actions`` is the loop's only bound, and past the
        finding cap record_finding returns "limit reached" without changing
        anything — so if it stayed free a model could call it forever.
        """
        # 2 free rounds + 3 charged rounds exhausts max_actions=3, then the
        # forced wrap-up. Distinct titles, so the repeat-nudge never fires and
        # cannot be what saves us here.
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call(f"c{i}", "record_finding", title=f"Finding {i}"))
                for i in range(2 + 3)
            ]
            + [_answering('{"notes": "budget gone"}')],
        )

        result = _run_loop(
            {"record_finding": lambda **kw: "Recorded."},
            max_actions=3,
            max_free_recordings=2,
        )

        assert result.actions_used == 3
        assert result.stop_reason == llm.STOP_ACTION_CAP

    def test_total_rounds_are_bounded_by_actions_plus_free_recordings(self, monkeypatch):
        client = _scripted(
            monkeypatch,
            [_acting(_tool_call(f"c{i}", "record_finding", title=f"F{i}")) for i in range(4 + 3)]
            + [_answering('{"notes": "budget gone"}')],
        )

        _run_loop(
            {"record_finding": lambda **kw: "Recorded."},
            max_actions=4,
            max_free_recordings=3,
        )

        # 3 free + 4 charged acting rounds, plus the forced wrap-up call —
        # the ceiling a free non-terminal tool would otherwise remove.
        assert len(client.requests) == 4 + 3 + 1

    def test_low_budget_switches_to_record_now(self, monkeypatch):
        """Past the cap record_finding is unreachable, so warn while it isn't.

        The forced wrap-up runs with tool_choice="none", so a finding still
        unrecorded when the budget runs out can only land in the notes — where
        nothing reads it as a finding.
        """
        client = _scripted(
            monkeypatch,
            [_acting(_tool_call(f"c{i}", "click", ref=f"e{i}")) for i in range(6)]
            + [_acting(_tool_call("cf", "finish_session", notes="done"))],
        )

        _run_loop({"click": lambda **kw: "clicked"}, max_actions=8)

        budget_notes = [
            m["content"].rsplit("\n[", 1)[-1]
            for m in client.requests[-1]["messages"]
            if isinstance(m, dict)
            and m.get("role") == "tool"
            and "actions remaining" in m["content"]
        ]
        # 7 remaining down to 5: ordinary wrap-up advice.
        assert all("call finish_session" in note for note in budget_notes[:3])
        # 3 remaining and below: record-or-lose-it.
        assert all("not yet recorded" in note for note in budget_notes[4:])

    def test_wrap_up_call_still_uses_json_mode(self, monkeypatch):
        """The forced wrap-up genuinely wants a JSON object, and says so."""
        client = _scripted(
            monkeypatch,
            [_acting(_tool_call("c1", "snapshot"))] * 2 + [_answering('{"notes": "n"}')],
        )
        _run_loop({"snapshot": lambda **kw: "page"}, max_actions=2)
        assert client.requests[-1]["response_format"] == {"type": "json_object"}
        assert client.requests[-1]["tool_choice"] == "none"

    def test_unknown_tool_returns_error_string(self, monkeypatch):
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "teleport", to="mars")),
                _acting(_tool_call("c2", "finish_session", notes="done")),
            ],
        )
        result = _run_loop({})
        assert any("unknown tool" in entry for entry in result.action_log)

    def test_executor_receives_arguments(self, monkeypatch):
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "fill", ref="e7", value="hello")),
                _acting(_tool_call("c2", "finish_session", notes="done")),
            ],
        )
        seen = {}
        tools = {"fill": lambda **kw: seen.update(kw) or "filled"}

        _run_loop(tools)
        assert seen == {"ref": "e7", "value": "hello"}

    def test_fill_secret_logs_variable_name_not_value(self, monkeypatch):
        """The literal never reaches this module — the executor resolves it."""
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "fill_secret", ref="e8", env_var_name="ADMIN_PASSWORD")),
                _acting(_tool_call("c2", "finish_session", notes="done")),
            ],
        )
        tools = {"fill_secret": lambda **kw: "filled ADMIN_PASSWORD"}

        result = _run_loop(tools)
        log = "\n".join(result.action_log)
        assert "ADMIN_PASSWORD" in log
        assert "hunter2" not in log

    def test_fill_with_a_secret_literal_is_redacted(self, monkeypatch):
        """Backstop for a model that ignores fill_secret and types the value."""
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "fill", ref="e8", value="hunter2")),
                _acting(_tool_call("c2", "finish_session", notes="done")),
            ],
        )

        result = _run_loop({"fill": lambda **kw: "filled"}, secret_values={"hunter2"})

        log = "\n".join(result.action_log)
        assert "hunter2" not in log
        assert "***" in log

    def test_non_secret_fill_values_stay_readable(self, monkeypatch):
        """Exact-match redaction must not mangle ordinary log lines."""
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "fill", ref="e8", value="hunter2 is my dog")),
                _acting(_tool_call("c2", "finish_session", notes="done")),
            ],
        )

        result = _run_loop({"fill": lambda **kw: "filled"}, secret_values={"hunter2"})

        assert "hunter2 is my dog" in "\n".join(result.action_log)

    def test_heartbeats_every_round(self, monkeypatch):
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "snapshot")),
                _acting(_tool_call("c2", "finish_session", notes="done")),
            ],
        )
        rounds = []
        _run_loop({"snapshot": lambda **kw: "page"}, rounds=rounds)
        assert len(rounds) >= 2

    def test_on_round_reports_the_running_action_count(self, monkeypatch):
        """The caller persists this, so it must climb as actions are spent."""
        _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "snapshot")),
                _acting(_tool_call("c2", "snapshot")),
                _acting(_tool_call("c3", "finish_session", notes="done")),
            ],
        )
        rounds: list[int] = []

        result = _run_loop({"snapshot": lambda **kw: "page"}, rounds=rounds)

        # Once before the first round's actions, then after each one — the
        # count reaches the round's own actions without waiting for the next
        # LLM call.
        assert rounds == [0, 1, 1, 2, 2]
        assert result.actions_used == 2

    def test_on_round_reports_the_final_count_at_the_wrap_up(self, monkeypatch):
        _scripted(
            monkeypatch,
            [_acting(_tool_call("c1", "snapshot"))] + [_answering('{"notes": "budget gone"}')],
        )
        rounds: list[int] = []

        result = _run_loop({"snapshot": lambda **kw: "page"}, max_actions=1, rounds=rounds)

        assert rounds[-1] == result.actions_used == 1

    def test_repeated_identical_calls_get_a_nudge(self, monkeypatch):
        calls = []
        _scripted(
            monkeypatch,
            [_acting(_tool_call(f"c{i}", "click", ref="e5")) for i in range(5)]
            + [_acting(_tool_call("cf", "finish_session", notes="done"))],
        )
        tools = {"click": lambda **kw: calls.append(kw) or "clicked"}

        result = _run_loop(tools)

        # Executed the first three, then nudged instead of executing again.
        assert len(calls) == 3
        assert any("repeated this exact action" in entry for entry in result.action_log)
        # The nudged rounds still cost budget so a stuck model cannot loop forever.
        assert result.actions_used == 5

    def test_llm_error_propagates(self, monkeypatch):
        class _Boom:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                raise openai.APIError("boom", request=httpx.Request("POST", "http://x"), body=None)

        monkeypatch.setattr(llm, "_get_client", lambda: _Boom())
        with pytest.raises(LLMError, match="LLM request failed"):
            _run_loop({})


class TestHistoryCompaction:
    """Threshold-triggered backstop — pressure-driven, never on a schedule."""

    # Usage stays under the limit while history accumulates, crosses on round
    # 8 — by which point there are enough complete groups to compact — then
    # falls back under, as it does once the span has actually been replaced.
    GROWING_USAGE = [50] * 7 + [9999] + [50] * 30

    @staticmethod
    def _acting_script(n):
        return [_acting(_tool_call(f"c{i}", "snapshot")) for i in range(n)]

    @staticmethod
    def _patch_compaction(monkeypatch, summary="EARLIER: created record #4471."):
        """Stub the compaction call, which goes through _complete, not the loop client."""
        calls = []

        def fake_complete(system_prompt, user_prompt, model_cls):
            calls.append({"system": system_prompt, "user": user_prompt})
            return llm.ExplorationSummaryResult(summary=summary)

        monkeypatch.setattr(llm, "_complete", fake_complete)
        return calls

    def test_does_not_fire_under_the_limit(self, monkeypatch):
        _scripted(
            monkeypatch,
            self._acting_script(3) + [_acting(_tool_call("cf", "finish_session", notes="x"))],
            prompt_tokens=100,
        )
        calls = self._patch_compaction(monkeypatch)

        _run_loop({"snapshot": lambda **kw: "page"}, context_token_limit=5000)

        assert calls == []

    def test_fires_over_the_limit_and_preserves_the_oracle(self, monkeypatch):
        client = _scripted(
            monkeypatch,
            self._acting_script(9) + [_acting(_tool_call("cf", "finish_session", notes="x"))],
            prompt_tokens=self.GROWING_USAGE,
        )
        self._patch_compaction(monkeypatch)

        _run_loop({"snapshot": lambda **kw: "page"}, context_token_limit=100)

        messages = client.requests[-1]["messages"]
        # The system prompt and the charter/requirement message are the
        # session's oracle and must survive every compaction.
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == EXPLORATION_SYSTEM_PROMPT
        assert messages[1]["role"] == "user"
        assert "Your charter for this session" in messages[1]["content"]
        assert any(
            isinstance(m, dict) and "compacted to save context" in str(m.get("content", ""))
            for m in messages
        )

    def test_leaves_no_orphan_tool_results(self, monkeypatch):
        """A tool result without its assistant parent makes the provider 400."""
        client = _scripted(
            monkeypatch,
            self._acting_script(9) + [_acting(_tool_call("cf", "finish_session", notes="x"))],
            prompt_tokens=self.GROWING_USAGE,
        )
        self._patch_compaction(monkeypatch)

        _run_loop({"snapshot": lambda **kw: "page"}, context_token_limit=100)

        messages = client.requests[-1]["messages"]
        live_ids: set[str] = set()
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "tool":
                assert message["tool_call_id"] in live_ids, "orphaned tool result"
            elif not isinstance(message, dict):
                live_ids.update(c.id for c in getattr(message, "tool_calls", None) or [])

    def test_snapshot_indices_survive_compaction(self, monkeypatch):
        """Indices are absolute; compaction shifts them and pruning would corrupt.

        After compacting, a later prune must still hit snapshot results and
        never an unrelated message.
        """
        script = []
        for i in range(9):
            script.append(_acting(_tool_call(f"s{i}", "snapshot")))
            script.append(_acting(_tool_call(f"k{i}", "click", ref=f"e{i}")))
        script.append(_acting(_tool_call("cf", "finish_session", notes="x")))
        client = _scripted(monkeypatch, script, prompt_tokens=self.GROWING_USAGE)
        self._patch_compaction(monkeypatch)

        _run_loop(
            {"snapshot": lambda **kw: "SNAP-BODY", "click": lambda **kw: "CLICK-RESULT"},
            context_token_limit=100,
            snapshot_window=1,
        )

        messages = client.requests[-1]["messages"]
        # Whatever got replaced by the pruner must have been a snapshot; a
        # click result turning into the placeholder is the corruption.
        for message in messages:
            if isinstance(message, dict) and llm._PRUNED_SNAPSHOT in str(message.get("content")):
                assert message.get("role") == "tool"
        assert not any(
            isinstance(m, dict)
            and m.get("role") == "tool"
            and m.get("content") == llm._PRUNED_SNAPSHOT
            and m.get("tool_call_id", "").startswith("k")
            for m in messages
        )

    def test_estimate_counts_every_message(self):
        assert llm._estimate_tokens([{"role": "user", "content": "x" * 400}]) == 100

    def test_uses_char_estimate_when_provider_reports_no_usage(self, monkeypatch):
        """No usage field must not mean "no limit" — nor a TypeError on None."""
        _scripted(
            monkeypatch,
            [_acting(_tool_call("c0", "snapshot")), _answering('{"notes": "no room"}')],
            prompt_tokens=None,
        )
        self._patch_compaction(monkeypatch)

        result = _run_loop({"snapshot": lambda **kw: "page"}, context_token_limit=1)

        assert result.stop_reason == llm.STOP_CONTEXT_LIMIT

    def test_compaction_failure_ends_the_session_cleanly(self, monkeypatch):
        _scripted(
            monkeypatch,
            self._acting_script(8) + [_answering('{"notes": "ran out of room"}')],
            prompt_tokens=self.GROWING_USAGE,
        )

        def boom(*args, **kwargs):
            raise LLMError("provider exploded")

        monkeypatch.setattr(llm, "_complete", boom)

        result = _run_loop({"snapshot": lambda **kw: "page"}, context_token_limit=100)

        assert result.stop_reason == llm.STOP_CONTEXT_LIMIT
        assert result.notes == "ran out of room"

    def test_nothing_to_compact_ends_rather_than_thrashing(self, monkeypatch):
        """When the floor itself exceeds the limit, retrying every round is futile."""
        calls = self._patch_compaction(monkeypatch)
        _scripted(
            monkeypatch,
            [_acting(_tool_call("c0", "snapshot")), _answering('{"notes": "no room"}')],
            prompt_tokens=9999,
        )

        result = _run_loop({"snapshot": lambda **kw: "page"}, context_token_limit=100)

        assert result.stop_reason == llm.STOP_CONTEXT_LIMIT
        assert calls == []  # never even attempted — nothing worth compacting

    def test_compaction_does_not_consume_an_action(self, monkeypatch):
        rounds = []
        _scripted(
            monkeypatch,
            self._acting_script(9) + [_acting(_tool_call("cf", "finish_session", notes="x"))],
            prompt_tokens=self.GROWING_USAGE,
        )
        self._patch_compaction(monkeypatch)

        result = _run_loop(
            {"snapshot": lambda **kw: "page"}, context_token_limit=100, rounds=rounds
        )

        assert result.actions_used == 9  # the snapshots only
        # Heartbeats cover the acting rounds plus every compaction, so a slow
        # compaction cannot get the run swept as a dead worker.
        assert len(rounds) > 9


class TestSnapshotPruning:
    def _messages_of_last_request(self, client):
        return client.requests[-1]["messages"]

    def test_older_snapshots_replaced_newest_kept(self, monkeypatch):
        # Interleave clicks so consecutive snapshots aren't identical actions
        # (four bare snapshots in a row would trip the repeat nudge, which is
        # itself correct behaviour — nothing changed the page between them).
        script = []
        for i in range(4):
            script.append(_acting(_tool_call(f"s{i}", "snapshot")))
            script.append(_acting(_tool_call(f"k{i}", "click", ref=f"e{i}")))
        script.append(_acting(_tool_call("cf", "finish_session", notes="done")))
        client = _scripted(monkeypatch, script)

        counter = {"n": 0}

        def snapshot(**kw):
            counter["n"] += 1
            return f"SNAPSHOT-BODY-{counter['n']}"

        _run_loop(
            {"snapshot": snapshot, "click": lambda **kw: "clicked"},
            snapshot_window=2,
        )

        messages = self._messages_of_last_request(client)
        snapshot_contents = [
            m["content"]
            for m in messages
            if isinstance(m, dict)
            and m.get("role") == "tool"
            and ("SNAPSHOT-BODY" in m["content"] or llm._PRUNED_SNAPSHOT in m["content"])
        ]
        # Four snapshots taken, window of 2: the first two are placeholders.
        assert len(snapshot_contents) == 4
        assert llm._PRUNED_SNAPSHOT in snapshot_contents[0]
        assert llm._PRUNED_SNAPSHOT in snapshot_contents[1]
        assert "SNAPSHOT-BODY-3" in snapshot_contents[2]
        assert "SNAPSHOT-BODY-4" in snapshot_contents[3]

    def test_assistant_messages_are_never_pruned(self, monkeypatch):
        """Rule 2 — the model's own commentary is the narrative thread."""
        client = _scripted(
            monkeypatch,
            [_acting(_tool_call(f"c{i}", "snapshot")) for i in range(4)]
            + [_acting(_tool_call("cf", "finish_session", notes="done"))],
        )
        _run_loop({"snapshot": lambda **kw: "BODY"}, snapshot_window=1)

        messages = self._messages_of_last_request(client)
        assistant_messages = [
            m for m in messages if not isinstance(m, dict) and getattr(m, "tool_calls", None)
        ]
        # Four snapshot rounds plus the finish_session round — every one still
        # present and untouched, even though snapshots were pruned beneath them.
        assert len(assistant_messages) == 5
        for message in assistant_messages:
            assert message.tool_calls  # untouched objects, not placeholders

    def test_non_snapshot_results_are_never_pruned(self, monkeypatch):
        client = _scripted(
            monkeypatch,
            [
                _acting(_tool_call("c1", "click", ref="e1")),
                _acting(_tool_call("c2", "snapshot")),
                _acting(_tool_call("c3", "snapshot")),
                _acting(_tool_call("cf", "finish_session", notes="done")),
            ],
        )
        tools = {"click": lambda **kw: "CLICK-RESULT", "snapshot": lambda **kw: "SNAP"}

        _run_loop(tools, snapshot_window=1)

        messages = self._messages_of_last_request(client)
        tool_contents = [
            m["content"] for m in messages if isinstance(m, dict) and m.get("role") == "tool"
        ]
        assert "CLICK-RESULT" in tool_contents[0]
