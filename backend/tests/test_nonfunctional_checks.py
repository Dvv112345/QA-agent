"""Tests for backend/services/nonfunctional_checks.py — the oracle.

Pure functions over captured output: no browser, no network. The axe
fixture is a **real** axe-core 4.12.1 payload, captured against a
deliberately broken page, so a package upgrade that changes the shape
fails here rather than in production.
"""

import json
from pathlib import Path

import pytest

from backend.models.database import FindingSeverity, FindingType, NonfunctionalDomain
from backend.services.nonfunctional_checks import (
    MAX_NODES_PER_VIOLATION,
    CheckError,
    RawViolation,
    RunViolations,
    axe_violations,
    capped_axe_payload,
    passive_security_violations,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "axe_result.json"
URL = "https://staging.example.com/login"


@pytest.fixture
def axe_payload() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class _AxeResults:
    """Stands in for ``axe_playwright_python.base.AxeResults``."""

    def __init__(self, response):
        self.response = response


def _violation(**kwargs) -> dict:
    base = {
        "id": "image-alt",
        "impact": "critical",
        "description": "Images must have alternative text",
        "help": "Images must have an alt attribute",
        "helpUrl": "https://dequeuniversity.com/rules/axe/4.12/image-alt",
        "tags": ["cat.text-alternatives", "wcag2a", "wcag111"],
        "nodes": [{"target": ["img"], "html": "<img src='x.png'>"}],
    }
    base.update(kwargs)
    return base


def _result(violations) -> dict:
    return {"violations": violations, "testEngine": {"name": "axe-core", "version": "4.12.1"}}


# ── Accessibility ─────────────────────────────────────────────────────


class TestAxeViolations:
    def test_the_real_0_1_8_payload_parses(self, axe_payload):
        """Captured from axe-core 4.12.1 through the installed wrapper."""
        scan = axe_violations(_AxeResults(axe_payload), URL)

        assert scan.engine_version == "4.12.1"
        rules = {violation.rule for violation in scan.violations}
        assert {"image-alt", "label", "link-name", "color-contrast"} <= rules
        # best-practice rules are data, not findings
        assert {"region", "landmark-one-main", "page-has-heading-one"} & rules == set()
        assert {item["rule"] for item in scan.advisory} >= {"region", "landmark-one-main"}

    def test_a_plain_dict_is_accepted_too(self, axe_payload):
        assert axe_violations(axe_payload, URL).violations

    @pytest.mark.parametrize(
        ("impact", "severity"),
        [
            ("critical", FindingSeverity.HIGH),
            ("serious", FindingSeverity.HIGH),
            ("moderate", FindingSeverity.MEDIUM),
            ("minor", FindingSeverity.LOW),
            ("unheard-of", FindingSeverity.MEDIUM),
            (None, FindingSeverity.MEDIUM),
        ],
    )
    def test_impact_maps_to_severity(self, impact, severity):
        scan = axe_violations(_result([_violation(impact=impact)]), URL)
        assert scan.violations[0].severity == severity

    def test_a_best_practice_rule_is_data_and_never_a_finding(self):
        scan = axe_violations(
            _result([_violation(id="region", tags=["cat.keyboard", "best-practice"])]), URL
        )

        assert scan.violations == []
        assert scan.advisory == [
            {
                "rule": "region",
                "impact": "critical",
                "node_count": 1,
                "tags": ["cat.keyboard", "best-practice"],
            }
        ]

    def test_one_finding_per_rule_with_the_nodes_inside_it(self):
        nodes = [{"target": [f"#b{n}"], "html": f"<button id='b{n}'></button>"} for n in range(3)]
        scan = axe_violations(_result([_violation(id="button-name", nodes=nodes)]), URL)

        assert len(scan.violations) == 1
        assert len(scan.violations[0].nodes) == 3
        assert "#b1" in scan.violations[0].nodes[1]

    def test_a_rule_broken_everywhere_stays_readable(self):
        nodes = [{"target": [f"#b{n}"], "html": "<button></button>"} for n in range(40)]
        scan = axe_violations(_result([_violation(id="button-name", nodes=nodes)]), URL)

        listed = scan.violations[0].nodes
        assert len(listed) == MAX_NODES_PER_VIOLATION + 1
        assert "30 more element(s)" in listed[-1]

    def test_every_violation_leaves_with_complete_finding_text(self, axe_payload):
        """The property ``iter_findings`` depends on — a title-less finding is dropped."""
        for violation in axe_violations(axe_payload, URL).violations:
            assert violation.title
            assert violation.steps_to_reproduce
            assert violation.expected
            assert violation.actual
            assert violation.finding_type == FindingType.BUG
            assert URL in violation.steps_to_reproduce

    @pytest.mark.parametrize(
        "payload",
        [
            {"testEngine": {}},  # no violations key at all
            {"violations": "nope"},  # not a list
            {"violations": [{"impact": "serious", "nodes": []}]},  # no rule id
            {"violations": [_violation(nodes=None)]},  # nodes absent
            {"violations": [_violation(nodes=["not-an-object"])]},
            {"violations": ["not-an-object"]},
        ],
        ids=["no-key", "not-a-list", "no-id", "no-nodes", "bad-node", "bad-violation"],
    )
    def test_a_malformed_payload_raises_rather_than_reading_clean(self, payload):
        """`failed_to_run`, never `clean` — the failure is otherwise permanent."""
        with pytest.raises(CheckError):
            axe_violations(payload, URL)

    def test_something_that_is_not_a_payload_raises(self):
        with pytest.raises(CheckError):
            axe_violations("<html>", URL)


class TestCappedAxePayload:
    def test_an_oversized_payload_is_capped(self, axe_payload):
        text = capped_axe_payload(axe_payload, max_chars=500)
        assert len(text) <= 500 + len("…[truncated]")
        assert text.endswith("…[truncated]")

    def test_a_small_payload_is_untouched(self):
        payload = _result([])
        assert capped_axe_payload(payload, max_chars=10_000) == json.dumps(payload)

    def test_it_never_raises_on_junk(self):
        assert capped_axe_payload(object()) == ""


# ── Passive security ──────────────────────────────────────────────────


CLEAN_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Server": "nginx",
}
CLEAN_COOKIE = {"name": "session", "secure": True, "httpOnly": True, "sameSite": "Lax"}


class TestPassiveSecurity:
    def test_a_clean_response_produces_nothing(self):
        assert (
            passive_security_violations(
                URL, status=200, headers=CLEAN_HEADERS, cookies=[CLEAN_COOKIE]
            )
            == []
        )

    @pytest.mark.parametrize(
        ("drop", "rule", "severity"),
        [
            ("Strict-Transport-Security", "missing-hsts", FindingSeverity.MEDIUM),
            ("Content-Security-Policy", "missing-csp", FindingSeverity.MEDIUM),
            ("X-Content-Type-Options", "missing-x-content-type-options", FindingSeverity.LOW),
            ("Referrer-Policy", "missing-referrer-policy", FindingSeverity.LOW),
        ],
    )
    def test_each_missing_header_fires_its_own_rule(self, drop, rule, severity):
        headers = {k: v for k, v in CLEAN_HEADERS.items() if k != drop}

        found = passive_security_violations(URL, status=200, headers=headers)

        match = next(v for v in found if v.rule == rule)
        assert match.severity == severity
        assert match.domain == NonfunctionalDomain.SECURITY
        assert match.title and match.expected and match.actual

    def test_a_short_hsts_max_age_is_weak_rather_than_missing(self):
        headers = {**CLEAN_HEADERS, "Strict-Transport-Security": "max-age=600"}

        found = passive_security_violations(URL, status=200, headers=headers)

        assert [v.rule for v in found] == ["weak-hsts"]

    def test_hsts_is_not_asked_of_a_plain_http_url(self):
        headers = {k: v for k, v in CLEAN_HEADERS.items() if k != "Strict-Transport-Security"}

        found = passive_security_violations("http://localhost:3000/", status=200, headers=headers)

        assert not [v for v in found if "hsts" in v.rule]

    def test_a_versioned_server_header_discloses_and_a_bare_one_does_not(self):
        versioned = passive_security_violations(
            URL, headers={**CLEAN_HEADERS, "Server": "nginx/1.25.3"}
        )
        bare = passive_security_violations(URL, headers=CLEAN_HEADERS)

        assert "server-header-disclosure" in {v.rule for v in versioned}
        assert "server-header-disclosure" not in {v.rule for v in bare}

    def test_x_powered_by_discloses(self):
        found = passive_security_violations(
            URL, headers={**CLEAN_HEADERS, "X-Powered-By": "Express"}
        )
        assert "x-powered-by-disclosure" in {v.rule for v in found}

    @pytest.mark.parametrize(
        ("cookie", "rule", "severity"),
        [
            (
                {"name": "s", "secure": False, "httpOnly": True, "sameSite": "Lax"},
                "cookie-missing-secure",
                FindingSeverity.HIGH,
            ),
            (
                {"name": "s", "secure": True, "httpOnly": False, "sameSite": "Lax"},
                "cookie-missing-httponly",
                FindingSeverity.MEDIUM,
            ),
            (
                {"name": "s", "secure": True, "httpOnly": True, "sameSite": None},
                "cookie-missing-samesite",
                FindingSeverity.LOW,
            ),
        ],
    )
    def test_each_cookie_attribute_rule_fires(self, cookie, rule, severity):
        found = passive_security_violations(URL, headers=CLEAN_HEADERS, cookies=[cookie])

        match = next(v for v in found if v.rule == rule)
        assert match.severity == severity

    def test_a_cookie_value_never_reaches_a_violation(self):
        cookie = {"name": "session", "value": "s3cr3t-token", "secure": False}

        found = passive_security_violations(URL, headers=CLEAN_HEADERS, cookies=[cookie])

        rendered = json.dumps([v.__dict__ for v in found], default=str)
        assert "s3cr3t-token" not in rendered

    def test_a_stack_trace_in_an_error_body_is_high(self):
        found = passive_security_violations(
            URL,
            status=500,
            headers=CLEAN_HEADERS,
            cookies=[CLEAN_COOKIE],
            body_sample='Traceback (most recent call last):\n  File "app.py"',
        )

        match = next(v for v in found if v.rule == "error-response-leaks-a-stack-trace")
        assert match.severity == FindingSeverity.HIGH

    def test_the_same_text_in_a_200_body_is_not_a_finding(self):
        """A page *about* tracebacks is not a leaking error response."""
        found = passive_security_violations(
            URL,
            status=200,
            headers=CLEAN_HEADERS,
            cookies=[CLEAN_COOKIE],
            body_sample="Traceback (most recent call last):",
        )

        assert found == []

    def test_non_mapping_headers_raise(self):
        with pytest.raises(CheckError):
            passive_security_violations(URL, headers=["Server: nginx"])


# ── Run-scoped de-duplication ─────────────────────────────────────────


def _raw(rule="image-alt", url=URL) -> RawViolation:
    return RawViolation(
        domain=NonfunctionalDomain.ACCESSIBILITY,
        rule=rule,
        url=url,
        severity=FindingSeverity.HIGH,
        summary="s",
        title="t",
        steps_to_reproduce="s",
        expected="e",
        actual="a",
    )


class TestRunViolations:
    def test_a_re_visit_collapses_and_keeps_both_states(self):
        accumulator = RunViolations()

        assert accumulator.add(_raw(), state="logged out") is True
        assert accumulator.add(_raw(), state="logged in") is False

        assert len(accumulator) == 1
        assert accumulator.states_for(_raw()) == ["logged out", "logged in"]

    def test_the_same_rule_at_another_url_is_a_separate_violation(self):
        accumulator = RunViolations()

        accumulator.add(_raw(url=URL))
        accumulator.add(_raw(url=URL + "/settings"))

        assert len(accumulator) == 2

    def test_the_cap_drops_rather_than_raising(self):
        accumulator = RunViolations(max_violations=1)

        accumulator.extend([_raw(rule="a"), _raw(rule="b"), _raw(rule="c")])

        assert len(accumulator) == 1
        assert accumulator.dropped == 2

    def test_order_is_first_seen(self):
        accumulator = RunViolations()
        accumulator.extend([_raw(rule="b"), _raw(rule="a"), _raw(rule="b")])

        assert [v.rule for v in accumulator.all()] == ["b", "a"]
