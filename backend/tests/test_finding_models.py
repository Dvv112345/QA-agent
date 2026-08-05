"""Tests for the derived finding fields on ``TestCaseExecution``.

These two properties are the contract the nested API response is built on,
so they are worth pinning independently of any route or task.
"""

from backend.models.database import (
    FindingSeverity,
    FindingType,
    TestCaseExecution,
    TestCaseExecutionStatus,
)


def _finished(status: str, **kwargs) -> TestCaseExecution:
    return TestCaseExecution(test_execution_id=1, test_case_id=1, status=status, **kwargs)


def _with_finding(status: str) -> TestCaseExecution:
    return _finished(
        status,
        finding_severity=FindingSeverity.HIGH,
        finding_title="Checkout total omits tax",
        finding_steps_to_reproduce="Open /cart\nAdd an item\nProceed to checkout",
        finding_expected="Total includes tax",
        finding_actual="Total excludes tax",
        environment="Windows-10 · Python 3.12.4",
    )


class TestFindingType:
    def test_failed_is_a_bug(self):
        assert _finished(TestCaseExecutionStatus.FAILED).finding_type == FindingType.BUG

    def test_error_is_an_issue(self):
        assert _finished(TestCaseExecutionStatus.ERROR).finding_type == FindingType.ISSUE

    def test_non_terminal_and_passing_have_none(self):
        for status in (
            TestCaseExecutionStatus.PENDING,
            TestCaseExecutionStatus.RUNNING,
            TestCaseExecutionStatus.PASSED,
        ):
            assert _finished(status).finding_type is None


class TestFindingProperty:
    def test_maps_columns_onto_the_shared_shape(self):
        finding = _with_finding(TestCaseExecutionStatus.FAILED).finding
        assert finding == {
            "finding_type": FindingType.BUG,
            "severity": FindingSeverity.HIGH,
            "title": "Checkout total omits tax",
            "steps_to_reproduce": "Open /cart\nAdd an item\nProceed to checkout",
            "expected": "Total includes tax",
            "actual": "Total excludes tax",
            "environment": "Windows-10 · Python 3.12.4",
            "tracker_issue_key": None,
            "tracker_issue_url": None,
            "tracker_error": None,
            "tracker_is_duplicate": False,
        }

    def test_error_case_reports_an_issue(self):
        assert _with_finding(TestCaseExecutionStatus.ERROR).finding["finding_type"] == (
            FindingType.ISSUE
        )

    def test_none_without_a_finding(self):
        for status in (
            TestCaseExecutionStatus.PENDING,
            TestCaseExecutionStatus.RUNNING,
            TestCaseExecutionStatus.PASSED,
        ):
            assert _finished(status).finding is None

    def test_missing_fields_become_empty_strings(self):
        """FindingBase declares these non-optional over nullable columns, so
        a single None would 500 the whole run-detail response rather than
        just this card. Unreachable today — the task writes them as a group."""
        row = _finished(TestCaseExecutionStatus.FAILED, finding_title="Only a title")

        finding = row.finding

        assert finding["severity"] == ""
        assert finding["steps_to_reproduce"] == ""
        assert finding["expected"] == ""
        assert finding["actual"] == ""
        assert finding["title"] == "Only a title"
        # environment stays nullable — the response model allows it.
        assert finding["environment"] is None

    def test_none_for_a_legacy_failed_row(self):
        """Rows written before findings were structured have no title.

        Gating on the title rather than the status is what keeps these out
        of the API instead of surfacing an all-null card.
        """
        assert _finished(TestCaseExecutionStatus.FAILED, error="something broke").finding is None

    def test_tracker_fields_pass_through_uncoalesced(self):
        """A filed finding carries its receipt; an unfiled one carries nulls.

        Deliberately not coalesced like the five required fields above —
        the response model allows null here, and an empty string would
        render as a link to nowhere.
        """
        row = _with_finding(TestCaseExecutionStatus.FAILED)
        row.tracker_issue_key = "QA-142"
        row.tracker_issue_url = "https://acme.atlassian.net/browse/QA-142"
        row.tracker_is_duplicate = True

        finding = row.finding

        assert finding["tracker_issue_key"] == "QA-142"
        assert finding["tracker_issue_url"] == "https://acme.atlassian.net/browse/QA-142"
        assert finding["tracker_is_duplicate"] is True
        assert finding["tracker_error"] is None

    def test_tracker_error_surfaces_without_a_key(self):
        """A failed filing leaves the error and no key — both are reported."""
        row = _with_finding(TestCaseExecutionStatus.FAILED)
        row.tracker_error = "Jira rejected the request (401)"

        finding = row.finding

        assert finding["tracker_error"] == "Jira rejected the request (401)"
        assert finding["tracker_issue_key"] is None


class TestSeverityNormalization:
    """One definition, shared by both finding sources.

    Two definitions would make ``high_severity_count`` mean different
    things depending on which testing mode found the bug.
    """

    def test_valid_values_pass_through(self):
        for value in ("high", "medium", "low"):
            assert FindingSeverity.normalize(value) == value

    def test_unknown_value_becomes_medium(self):
        assert FindingSeverity.normalize("critical") == FindingSeverity.MEDIUM

    def test_missing_value_becomes_medium(self):
        assert FindingSeverity.normalize(None) == FindingSeverity.MEDIUM
        assert FindingSeverity.normalize("") == FindingSeverity.MEDIUM

    def test_returns_a_plain_string_never_an_enum_member(self):
        """An enum member f-strings as 'FindingSeverity.MEDIUM' on 3.12, and
        these values reach both prompt text and stored columns."""
        assert f"{FindingSeverity.normalize('nonsense')}" == "medium"
        assert f"{FindingSeverity.normalize('high')}" == "high"


class TestFindingTypeNormalization:
    """Same treatment as severity, for the same reason.

    An unrecognised type counts toward neither ``bug_count`` nor
    ``issue_count`` while still counting toward ``finding_count``, so the
    run page would show numbers that do not add up.
    """

    def test_valid_values_pass_through(self):
        for value in ("bug", "issue"):
            assert FindingType.normalize(value) == value

    def test_unknown_value_becomes_bug(self):
        """Matches what record_finding already defaults to when the model
        omits the field entirely."""
        assert FindingType.normalize("defect") == FindingType.BUG

    def test_missing_value_becomes_bug(self):
        assert FindingType.normalize(None) == FindingType.BUG
        assert FindingType.normalize("") == FindingType.BUG

    def test_returns_a_plain_string_never_an_enum_member(self):
        assert f"{FindingType.normalize('nonsense')}" == "bug"
        assert f"{FindingType.normalize('issue')}" == "issue"


class TestDerivedFindingTypeIsAPlainString:
    def test_property_returns_plain_strings(self):
        assert f"{_finished(TestCaseExecutionStatus.FAILED).finding_type}" == "bug"
        assert f"{_finished(TestCaseExecutionStatus.ERROR).finding_type}" == "issue"
