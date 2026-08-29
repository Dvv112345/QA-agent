"""Tests for backend/tasks/run_nonfunctional.py — the task as a plain function.

``BrowserSession``, ``load_runner`` and ``llm`` are all monkeypatched: no
browser, no network, no Redis. The conftest engine patch makes
``new_session()`` hit the same in-memory SQLite database as ``db_session``.
"""

import json
from types import SimpleNamespace

import pytest

import backend.tasks.run_nonfunctional as task_module
from backend.config import MAX_AUTO_RETRIES
from backend.models.database import (
    SUPERSEDED_ERROR,
    DomainOutcome,
    NonfunctionalChildStatus,
    NonfunctionalFinding,
    NonfunctionalLoadProfile,
    NonfunctionalRun,
    NonfunctionalRunStatus,
    NonfunctionalTarget,
    RequirementStatus,
    TargetKind,
    TestEnvironmentStatus,
    TestPlanStatus,
)
from backend.services import browser_session as browser_module
from backend.services.load_runner import LoadResult
from backend.tasks.run_nonfunctional import run_nonfunctional_task
from backend.tests.test_nonfunctional_models import (
    _seed_load_profile,
    _seed_nonfunctional_run,
    _seed_nonfunctional_target,
)
from backend.tests.test_requirement_routes import _seed_requirement, _seed_sprint
from backend.tests.test_sprints import _seed_test_case, _seed_test_env, _seed_test_plan

BASE_URL = "https://staging.example.com"
ENV_VARS_JSON = json.dumps({"BASE_URL": BASE_URL})

AXE_CLEAN = {"violations": [], "testEngine": {"version": "4.12.1"}}
AXE_DIRTY = {
    "violations": [
        {
            "id": "image-alt",
            "impact": "critical",
            "description": "Images must have alternative text",
            "help": "Add an alt attribute",
            "helpUrl": "https://example.com/image-alt",
            "tags": ["wcag2a", "wcag111"],
            "nodes": [{"target": ["img"], "html": "<img src='x'>"}],
        }
    ],
    "testEngine": {"version": "4.12.1"},
}
CLEAN_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@pytest.fixture(autouse=True)
def _isolate_readme_resolution(monkeypatch):
    """Keep README resolution deterministic: no disk reads, no GitHub calls."""
    import backend.utils.readme_utils as readme_utils

    async def _no_readme(*args, **kwargs):
        return None

    monkeypatch.setattr(readme_utils, "STORE_OFFLINE", False)
    monkeypatch.setattr(readme_utils, "download_readme", _no_readme)


class _FakeBrowser:
    """Stands in for a live BrowserSession.

    ``visit`` is what a navigation tool would do: it fires the arrival
    callback exactly as the real executors do.
    """

    def __init__(self, **kwargs):
        self.on_navigated = kwargs.get("on_navigated")
        self.catalogue_timeout = kwargs.get("catalogue_timeout", 30)
        self.axe_result = AXE_CLEAN
        self.axe_error = None
        self.headers = dict(CLEAN_HEADERS)
        self.headers_error = None
        self.performance = {"load_ms": 340}
        self.discovered_endpoints: list[str] = []
        self.cookies = {"session": "abc"}
        self.screenshot_bytes = b"PNG"
        self.visits: list[str] = ["https://staging.example.com/reports"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    # ── what the task calls ──
    def scan_accessibility(self):
        if self.axe_error:
            return browser_module.CheckOutcome(error=self.axe_error)
        return browser_module.CheckOutcome(data=self.axe_result)

    def check_headers(self):
        if self.headers_error:
            return browser_module.CheckOutcome(error=self.headers_error)
        return browser_module.CheckOutcome(
            data={
                "url": "u",
                "status": 200,
                "headers": self.headers,
                "cookies": [],
                "body_sample": "",
            }
        )

    def measure_performance(self):
        return browser_module.CheckOutcome(data=self.performance)

    def cookies_for_load(self):
        return dict(self.cookies)

    def screenshot(self):
        return self.screenshot_bytes

    def nonfunctional_tool_registry(self):
        return {"snapshot": lambda: "page"}


@pytest.fixture
def patched(monkeypatch):
    """Every outside edge stubbed; the returned dict steers each one."""
    state = {
        "browser": None,
        "browser_factory_error": None,
        "loop_error": None,
        "triaged": {},
        "triage_error": None,
        "summary": "A summary.",
        "summary_error": None,
        "load_result": LoadResult(requests_sent=5, p50_ms=10.0),
        "load_calls": [],
        "grouped": [],
        "exported": [],
    }

    def _factory(**kwargs):
        if state["browser_factory_error"]:
            raise state["browser_factory_error"]
        browser = _FakeBrowser(**kwargs)
        state["browser"] = browser
        return browser

    def _loop(**kwargs):
        if state["loop_error"]:
            raise state["loop_error"]
        browser = state["browser"]
        # The walk: every URL the model reached fires the arrival hook.
        for url in browser.visits:
            browser.on_navigated(url, task_module.time.monotonic() + 30)
        return SimpleNamespace(
            notes="Walked it.", stop_reason="charter_complete", actions_used=3, action_log=[]
        )

    def _triage(violations, **kwargs):
        if state["triage_error"]:
            raise state["triage_error"]
        return state["triaged"]

    def _summarize(**kwargs):
        if state["summary_error"]:
            raise state["summary_error"]
        on_attempt = kwargs.get("on_attempt")
        if on_attempt is not None:
            on_attempt()
        return SimpleNamespace(summary=state["summary"])

    def _run_profile(**kwargs):
        state["load_calls"].append(kwargs)
        return state["load_result"]

    monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
    monkeypatch.setattr(task_module.llm, "run_nonfunctional_loop", _loop)
    monkeypatch.setattr(task_module.llm, "triage_nonfunctional_findings", _triage)
    monkeypatch.setattr(task_module.llm, "summarize_nonfunctional", _summarize)
    monkeypatch.setattr(task_module.load_runner, "run_profile", _run_profile)
    monkeypatch.setattr(
        task_module.finding_grouping,
        "assign_defect_groups",
        lambda session, run: state["grouped"].append(run.id),
    )
    monkeypatch.setattr(
        task_module.finding_export,
        "export_findings",
        lambda session, run, **kwargs: state["exported"].append(run.id),
    )
    monkeypatch.setattr(task_module, "_store_screenshot", lambda *a, **k: "/tmp/shot.png")
    return state


def _seed_run(db_session, **run_kwargs):
    sprint = _seed_sprint(db_session)
    requirement = _seed_requirement(db_session, sprint, status=RequirementStatus.CONFIRMED)
    plan = _seed_test_plan(db_session, requirement, status=TestPlanStatus.APPROVED)
    _seed_test_case(db_session, plan)
    _seed_test_env(
        db_session, sprint, status=TestEnvironmentStatus.CONFIRMED, env_vars_json=ENV_VARS_JSON
    )
    db_session.refresh(sprint)
    run = _seed_nonfunctional_run(db_session, sprint, requirement, **run_kwargs)
    return sprint, requirement, run


def _reload(db_session, run_id) -> NonfunctionalRun:
    db_session.expire_all()
    return db_session.get(NonfunctionalRun, run_id)


def _targets(db_session, run_id) -> list[NonfunctionalTarget]:
    db_session.expire_all()
    return sorted(
        db_session.exec(
            task_module.select(NonfunctionalTarget).where(
                NonfunctionalTarget.nonfunctional_run_id == run_id
            )
        ).all(),
        key=lambda t: t.position,
    )


# ── the happy path ────────────────────────────────────────────────────


class TestCleanRun:
    def test_completes_and_records_every_domain_at_every_target(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session)

        run_nonfunctional_task(run.id)

        stored = _reload(db_session, run.id)
        assert stored.status == NonfunctionalRunStatus.COMPLETED
        assert stored.summary == "A summary."
        assert stored.last_heartbeat is None

        targets = _targets(db_session, run.id)
        # The coverage floor is the confirmed base URL, plus whatever the
        # walk reached.
        assert [t.url for t in targets] == [BASE_URL, "https://staging.example.com/reports"]
        for target in targets:
            assert target.status == NonfunctionalChildStatus.COMPLETED
            assert target.a11y_outcome == DomainOutcome.CLEAN
            assert target.security_outcome == DomainOutcome.CLEAN
            assert target.performance_outcome == DomainOutcome.CLEAN
            assert json.loads(target.metrics_json)["load_ms"] == 340
            assert target.kind == TargetKind.PAGE

    def test_a_url_reached_twice_is_one_target(self, db_session, patched, monkeypatch):
        _sprint, _requirement, run = _seed_run(db_session)
        patched["browser"] = None

        def _loop(**kwargs):
            browser = patched["browser"]
            for url in (BASE_URL, BASE_URL + "#anchor", BASE_URL):
                browser.on_navigated(url, task_module.time.monotonic() + 30)
            return SimpleNamespace(
                notes="", stop_reason="charter_complete", actions_used=1, action_log=[]
            )

        monkeypatch.setattr(task_module.llm, "run_nonfunctional_loop", _loop)
        run_nonfunctional_task(run.id)

        assert len(_targets(db_session, run.id)) == 1

    def test_only_selected_domains_are_examined(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session, domains_csv="performance")

        run_nonfunctional_task(run.id)

        target = _targets(db_session, run.id)[0]
        assert target.performance_outcome == DomainOutcome.CLEAN
        # None, not "clean": the run never looked, and saying it did would
        # be the one reading that is false.
        assert target.a11y_outcome is None
        assert target.security_outcome is None

    def test_the_target_cap_stops_new_targets_without_ending_the_walk(
        self, db_session, patched, monkeypatch
    ):
        monkeypatch.setattr(task_module, "NONFUNCTIONAL_MAX_TARGETS", 2)
        _sprint, _requirement, run = _seed_run(db_session)
        patched_visits = [f"{BASE_URL}/p{n}" for n in range(6)]

        def _loop(**kwargs):
            browser = patched["browser"]
            notes = [
                browser.on_navigated(url, task_module.time.monotonic() + 30)
                for url in patched_visits
            ]
            assert any("limit for this run" in note for note in notes)
            return SimpleNamespace(
                notes="", stop_reason="charter_complete", actions_used=1, action_log=[]
            )

        monkeypatch.setattr(task_module.llm, "run_nonfunctional_loop", _loop)
        run_nonfunctional_task(run.id)

        assert len(_targets(db_session, run.id)) == 2
        assert _reload(db_session, run.id).status == NonfunctionalRunStatus.COMPLETED


# ── outcomes that are not "clean" ─────────────────────────────────────


class TestOutcomes:
    def test_violations_become_findings_with_the_tools_severity(
        self, db_session, patched, monkeypatch
    ):
        _sprint, _requirement, run = _seed_run(db_session)
        patched["triaged"] = {}

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.axe_result = AXE_DIRTY
            browser.visits = []
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        db_session.expire_all()
        findings = db_session.exec(task_module.select(NonfunctionalFinding)).all()
        assert len(findings) == 1
        assert findings[0].rule == "image-alt"
        assert findings[0].severity == "high"  # axe `critical`, not a model's opinion
        assert findings[0].finding_type == "bug"
        assert findings[0].title  # deterministic fallback text
        assert findings[0].screenshot_path == "/tmp/shot.png"
        assert _targets(db_session, run.id)[0].a11y_outcome == DomainOutcome.VIOLATIONS

    def test_the_same_rule_at_the_same_url_is_one_finding(self, db_session, patched, monkeypatch):
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.axe_result = AXE_DIRTY
            browser.visits = [BASE_URL]  # revisit the seeded target
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        db_session.expire_all()
        assert len(db_session.exec(task_module.select(NonfunctionalFinding)).all()) == 1

    def test_a_refused_axe_run_records_failed_to_run_not_clean(
        self, db_session, patched, monkeypatch
    ):
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.axe_error = "Execution context was destroyed"
            browser.visits = []
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        target = _targets(db_session, run.id)[0]
        assert target.a11y_outcome == DomainOutcome.FAILED_TO_RUN
        assert target.security_outcome == DomainOutcome.CLEAN
        assert "Execution context" in target.error

    def test_a_malformed_axe_payload_records_failed_to_run(self, db_session, patched, monkeypatch):
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.axe_result = {"testEngine": {}}  # no violations key
            browser.visits = []
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        assert _targets(db_session, run.id)[0].a11y_outcome == DomainOutcome.FAILED_TO_RUN

    def test_an_exhausted_budget_records_failed_to_run_never_silence(
        self, db_session, patched, monkeypatch
    ):
        _sprint, _requirement, run = _seed_run(db_session)
        monkeypatch.setattr(task_module._Catalogue, "_expired", staticmethod(lambda deadline: True))

        run_nonfunctional_task(run.id)

        target = _targets(db_session, run.id)[0]
        assert target.a11y_outcome == DomainOutcome.FAILED_TO_RUN
        assert target.security_outcome == DomainOutcome.FAILED_TO_RUN
        assert target.performance_outcome == DomainOutcome.FAILED_TO_RUN
        assert "time budget" in target.error

    def test_a_check_that_raises_costs_the_target_not_the_run(
        self, db_session, patched, monkeypatch
    ):
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.visits = []

            def _boom():
                raise RuntimeError("browser exploded")

            browser.scan_accessibility = _boom
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        assert _reload(db_session, run.id).status == NonfunctionalRunStatus.COMPLETED
        target = _targets(db_session, run.id)[0]
        assert target.status == NonfunctionalChildStatus.ERROR
        assert "browser exploded" in target.error


# ── endpoints ─────────────────────────────────────────────────────────


class TestEndpoints:
    def _stub_httpx(self, monkeypatch, status=200, headers=None, text="{}"):
        class _Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def get(self, url):
                return SimpleNamespace(
                    status_code=status, headers=headers or CLEAN_HEADERS, text=text
                )

        monkeypatch.setattr(task_module.httpx, "Client", _Client)

    def test_a_discovered_endpoint_is_examined_and_a11y_is_not_applicable(
        self, db_session, patched, monkeypatch
    ):
        self._stub_httpx(monkeypatch)
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.visits = []
            browser.discovered_endpoints = [f"{BASE_URL}/api/items"]
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        endpoint = next(t for t in _targets(db_session, run.id) if t.kind == TargetKind.ENDPOINT)
        assert endpoint.a11y_outcome == DomainOutcome.NOT_APPLICABLE
        assert endpoint.security_outcome == DomainOutcome.CLEAN
        assert json.loads(endpoint.metrics_json)["status"] == 200

    def test_an_unreachable_endpoint_records_the_failure(self, db_session, patched, monkeypatch):
        class _Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def get(self, url):
                raise OSError("connection refused")

        monkeypatch.setattr(task_module.httpx, "Client", _Client)
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.visits = []
            browser.discovered_endpoints = [f"{BASE_URL}/api/items"]
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        run_nonfunctional_task(run.id)

        endpoint = next(t for t in _targets(db_session, run.id) if t.kind == TargetKind.ENDPOINT)
        assert endpoint.status == NonfunctionalChildStatus.ERROR
        assert endpoint.security_outcome == DomainOutcome.FAILED_TO_RUN
        assert _reload(db_session, run.id).status == NonfunctionalRunStatus.COMPLETED


# ── load profiles ─────────────────────────────────────────────────────


class TestLoadProfiles:
    def test_profiles_run_last_safe_first_and_record_what_was_sent(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session, environment_disposable=True)
        _seed_load_profile(db_session, run, position=0, method="POST", url=f"{BASE_URL}/w")
        _seed_load_profile(db_session, run, position=1, method="GET", url=f"{BASE_URL}/r")

        run_nonfunctional_task(run.id)

        assert [call["method"] for call in patched["load_calls"]] == ["GET", "POST"]
        db_session.expire_all()
        for profile in db_session.exec(task_module.select(NonfunctionalLoadProfile)).all():
            assert profile.status == NonfunctionalChildStatus.COMPLETED
            assert profile.requests_sent == 5
            assert json.loads(profile.results_json)["p50_ms"] == 10.0

    def test_the_browsers_cookies_are_carried(self, db_session, patched, monkeypatch):
        _sprint, _requirement, run = _seed_run(db_session)
        _seed_load_profile(db_session, run, url=f"{BASE_URL}/r")

        run_nonfunctional_task(run.id)

        assert patched["load_calls"][0]["cookies"] == {"session": "abc"}

    def test_a_profile_that_already_sent_traffic_is_never_re_sent(
        self, db_session, patched, monkeypatch
    ):
        """The invariant reads `requests_sent`, not `status`."""
        _sprint, _requirement, run = _seed_run(db_session, status=NonfunctionalRunStatus.PENDING)
        _seed_load_profile(
            db_session,
            run,
            requests_sent=20,
            # Deliberately NOT terminal: a status reset must not be enough
            # to make this eligible again.
            status=NonfunctionalChildStatus.PENDING,
        )

        run_nonfunctional_task(run.id)

        assert patched["load_calls"] == []

    def test_a_refused_profile_is_recorded_as_an_error_not_a_run_failure(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session)
        profile = _seed_load_profile(db_session, run, url=f"{BASE_URL}/r")
        patched["load_result"] = LoadResult(refused="private address space")

        run_nonfunctional_task(run.id)

        db_session.expire_all()
        stored = db_session.get(NonfunctionalLoadProfile, profile.id)
        assert stored.status == NonfunctionalChildStatus.ERROR
        assert stored.error == "private address space"
        assert _reload(db_session, run.id).status == NonfunctionalRunStatus.COMPLETED


# ── failure paths ─────────────────────────────────────────────────────


class TestFailures:
    def test_an_upstream_edit_fails_the_run_and_settles_both_child_types(
        self, db_session, patched, monkeypatch
    ):
        _sprint, requirement, run = _seed_run(db_session)
        target = _seed_nonfunctional_target(db_session, run, position=9)
        profile = _seed_load_profile(db_session, run)

        def _loop(**kwargs):
            # An edit lands while the browser is open.
            requirement.content_revision += 1
            db_session.add(requirement)
            db_session.commit()
            return SimpleNamespace(
                notes="", stop_reason="charter_complete", actions_used=1, action_log=[]
            )

        monkeypatch.setattr(task_module.llm, "run_nonfunctional_loop", _loop)
        run_nonfunctional_task(run.id)

        stored = _reload(db_session, run.id)
        assert stored.status == NonfunctionalRunStatus.FAILED
        assert stored.error == SUPERSEDED_ERROR
        assert db_session.get(NonfunctionalTarget, target.id).status == (
            NonfunctionalChildStatus.SKIPPED
        )
        assert db_session.get(NonfunctionalLoadProfile, profile.id).status == (
            NonfunctionalChildStatus.SKIPPED
        )
        assert patched["load_calls"] == []

    def test_a_crash_spends_a_retry_and_re_pends(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session)
        patched["loop_error"] = RuntimeError("worker exploded")

        run_nonfunctional_task(run.id)

        stored = _reload(db_session, run.id)
        assert stored.status == NonfunctionalRunStatus.PENDING
        assert stored.retry_count == 1

    def test_a_crash_at_the_cap_fails_and_settles_children(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session, retry_count=MAX_AUTO_RETRIES - 1)
        profile = _seed_load_profile(db_session, run)
        patched["loop_error"] = RuntimeError("worker exploded")

        run_nonfunctional_task(run.id)

        stored = _reload(db_session, run.id)
        assert stored.status == NonfunctionalRunStatus.FAILED
        assert db_session.get(NonfunctionalLoadProfile, profile.id).status == (
            NonfunctionalChildStatus.SKIPPED
        )

    def test_a_finished_sprint_fails_the_run(self, db_session, patched):
        sprint, _requirement, run = _seed_run(db_session)
        sprint.active = False
        db_session.add(sprint)
        db_session.commit()

        run_nonfunctional_task(run.id)

        assert _reload(db_session, run.id).status == NonfunctionalRunStatus.FAILED

    def test_a_deleted_requirement_fails_the_run_by_its_own_name(self, db_session, patched):
        _sprint, requirement, run = _seed_run(db_session)
        requirement.archived = True
        db_session.add(requirement)
        db_session.commit()

        run_nonfunctional_task(run.id)

        stored = _reload(db_session, run.id)
        assert stored.status == NonfunctionalRunStatus.FAILED
        assert "deleted" in stored.error

    def test_a_stale_job_is_skipped(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session, status=NonfunctionalRunStatus.COMPLETED)

        run_nonfunctional_task(run.id)

        assert _targets(db_session, run.id) == []

    def test_a_missing_run_is_a_no_op(self, db_session, patched):
        run_nonfunctional_task(9999)

    def test_a_triage_failure_costs_the_prose_not_the_run(self, db_session, patched, monkeypatch):
        _sprint, _requirement, run = _seed_run(db_session)

        def _factory(**kwargs):
            browser = _FakeBrowser(**kwargs)
            browser.axe_result = AXE_DIRTY
            browser.visits = []
            patched["browser"] = browser
            return browser

        monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
        # `triage_nonfunctional_findings` never raises, so the realistic
        # failure is an empty mapping — every finding on its fallback text.
        patched["triaged"] = {}

        run_nonfunctional_task(run.id)

        db_session.expire_all()
        finding = db_session.exec(task_module.select(NonfunctionalFinding)).all()[0]
        assert _reload(db_session, run.id).status == NonfunctionalRunStatus.COMPLETED
        assert "image-alt" in finding.title
        assert finding.steps_to_reproduce

    def test_a_summary_failure_leaves_the_run_completed(self, db_session, patched):
        from backend.services.llm import LLMError

        _sprint, _requirement, run = _seed_run(db_session)
        patched["summary_error"] = LLMError("down")

        run_nonfunctional_task(run.id)

        stored = _reload(db_session, run.id)
        assert stored.status == NonfunctionalRunStatus.COMPLETED
        assert stored.summary is None


# ── grouping and export ───────────────────────────────────────────────


class TestGroupingAndExport:
    def test_both_run_after_the_completed_commit(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session)

        run_nonfunctional_task(run.id)

        assert patched["grouped"] == [run.id]
        assert patched["exported"] == [run.id]

    def test_neither_runs_on_a_failure_path(self, db_session, patched):
        _sprint, _requirement, run = _seed_run(db_session)
        patched["loop_error"] = RuntimeError("worker exploded")

        run_nonfunctional_task(run.id)

        assert patched["grouped"] == []
        assert patched["exported"] == []

    def test_grouping_runs_before_the_export(self, db_session, patched, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(
            task_module.finding_grouping,
            "assign_defect_groups",
            lambda s, r: order.append("group"),
        )
        monkeypatch.setattr(
            task_module.finding_export,
            "export_findings",
            lambda s, r, **k: order.append("export"),
        )
        _sprint, _requirement, run = _seed_run(db_session)

        run_nonfunctional_task(run.id)

        assert order == ["group", "export"]


# ── restart ───────────────────────────────────────────────────────────


class TestRestart:
    def test_completed_targets_are_re_examined_but_sent_profiles_are_not(self, db_session, patched):
        """A page re-read costs a page load; a profile re-sent costs traffic."""
        _sprint, _requirement, run = _seed_run(db_session)
        _seed_nonfunctional_target(
            db_session, run, position=0, url=BASE_URL, status=NonfunctionalChildStatus.COMPLETED
        )
        _seed_load_profile(
            db_session,
            run,
            requests_sent=7,
            status=NonfunctionalChildStatus.COMPLETED,
            url=f"{BASE_URL}/r",
        )
        _seed_load_profile(db_session, run, position=1, requests_sent=0, url=f"{BASE_URL}/r2")

        run_nonfunctional_task(run.id)

        assert [call["url"] for call in patched["load_calls"]] == [f"{BASE_URL}/r2"]
        # The seeded target's URL is walked again: a second row for it is
        # expected, and its findings de-duplicate by (rule, url) anyway.
        assert any(t.url == BASE_URL for t in _targets(db_session, run.id))
