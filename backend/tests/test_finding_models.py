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

    def test_none_for_a_legacy_failed_row(self):
        """Rows written before findings were structured have no title.

        Gating on the title rather than the status is what keeps these out
        of the API instead of surfacing an all-null card.
        """
        assert _finished(TestCaseExecutionStatus.FAILED, error="something broke").finding is None
