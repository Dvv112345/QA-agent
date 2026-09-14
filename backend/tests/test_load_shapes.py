"""Tests for backend/services/load_shapes.py — pure stage math, no I/O."""

import ast
import math
from pathlib import Path

import pytest

import backend.services.load_shapes as load_shapes
from backend.services.load_shapes import (
    Stage,
    derived_figures,
    min_duration,
    percentile,
    stage_figures,
    stages_for,
)

SHAPES = ("load", "stress", "spike", "soak")


# ── Stage coverage ────────────────────────────────────────────────────


class TestStagesCoverTheDuration:
    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize(("users", "duration"), [(1, 1), (3, 50), (50, 100), (1000, 120)])
    def test_contiguous_over_the_whole_duration_with_no_empty_stage(self, shape, users, duration):
        stages = stages_for(shape, users, duration)

        assert stages[0].start_s == 0
        assert stages[-1].end_s == pytest.approx(duration)
        for previous, current in zip(stages, stages[1:], strict=False):
            assert current.start_s == pytest.approx(previous.end_s)
        assert all(stage.end_s > stage.start_s for stage in stages)
        assert all(stage.start_s <= stage.measure_from_s < stage.end_s for stage in stages)

    @pytest.mark.parametrize("shape", ("load", "soak", "no-such-shape"))
    def test_constant_shapes_are_one_steady_stage(self, shape):
        assert stages_for(shape, 7, 30) == [Stage("steady", 0.0, 30.0, 7, 7.0, 0.0)]


class TestStress:
    def test_monotonic_and_the_final_step_is_the_ceiling(self):
        stages = stages_for("stress", 50, 100)

        users = [stage.users for stage in stages]
        assert users == sorted(users)
        assert users == [10, 20, 30, 40, 50]

    def test_few_users_mean_fewer_steps(self):
        assert [stage.users for stage in stages_for("stress", 3, 60)] == [1, 2, 3]

    def test_duration_is_independent_of_the_ceiling(self):
        """The D11 property: a high ceiling never adds steps or time."""
        low = stages_for("stress", 50, 100)
        high = stages_for("stress", 1000, 100)

        assert len(high) == len(low) == 5
        assert [(s.start_s, s.end_s) for s in high] == [(s.start_s, s.end_s) for s in low]
        assert [s.end_s - s.start_s for s in high] == pytest.approx([20.0] * 5)

    def test_arrivals_spread_over_a_tenth_of_each_step(self):
        stages = stages_for("stress", 1000, 100)  # 20 s steps → 2 s ramp

        for previous_users, stage in zip([0] + [s.users for s in stages], stages, strict=False):
            ramp = max(1.0, 0.1 * (stage.end_s - stage.start_s))
            assert stage.measure_from_s - stage.start_s == pytest.approx(ramp)
            assert stage.spawn_rate == math.ceil((stage.users - previous_users) / ramp)

    def test_ramp_samples_are_left_out_of_the_step_figures(self):
        stages = stages_for("stress", 50, 100)
        # Step 1 spans [0, 20) with a 2 s ramp: a slow sample inside the ramp,
        # a fast one after it.
        samples = [(1.0, 9000.0, False), (5.0, 10.0, False)]

        figures = derived_figures("stress", stages, samples)

        assert figures["p95_by_users"][0] == [10, 10.0]
        assert stage_figures(stages, samples)[0]["responses"] == 2

    def test_the_minimum_duration_is_five_minimum_steps(self):
        assert min_duration("stress", 10) == 50
        assert min_duration("load", 10) == 1
        assert min_duration("spike", 10) == 1


class TestStoppedBetweenUsers:
    def test_an_error_stop_in_the_first_step_reads_from_zero(self):
        stages = stages_for("stress", 50, 100)

        figures = derived_figures("stress", stages, [], error_stop_at_s=3.0)

        assert figures["stopped_between_users"] == [0, 10]

    def test_an_error_stop_in_the_fourth_step_names_the_step_before(self):
        stages = stages_for("stress", 50, 100)

        figures = derived_figures("stress", stages, [], error_stop_at_s=65.0)

        assert figures["stopped_between_users"] == [30, 40]

    def test_no_stop_is_no_range(self):
        stages = stages_for("stress", 50, 100)

        assert derived_figures("stress", stages, [])["stopped_between_users"] is None


class TestSpike:
    def test_three_phases_with_a_baseline_of_at_least_one(self):
        stages = stages_for("spike", 5, 100)

        assert [s.name for s in stages] == ["baseline", "spike", "recovery"]
        assert [s.users for s in stages] == [1, 5, 1]
        assert [(s.start_s, s.end_s) for s in stages] == [(0, 40), (40, 60), (60, 100)]
        assert stages[1].spawn_rate == 5  # the whole burst in one second

    def test_a_single_user_cannot_spike_and_says_so(self):
        stages = stages_for("spike", 1, 30)

        assert len(stages) == 1
        assert "ran as constant load" in derived_figures("spike", stages, [])["note"]

    def test_phase_p95s_and_the_recovery_ratio(self):
        stages = stages_for("spike", 50, 100)
        samples = [(10.0, 100.0, False), (50.0, 900.0, False), (80.0, 150.0, False)]

        figures = derived_figures("spike", stages, samples)

        assert figures == {
            "baseline_p95_ms": 100.0,
            "spike_p95_ms": 900.0,
            "recovery_p95_ms": 150.0,
            "recovery_p95_ratio": 1.5,
        }

    def test_an_empty_baseline_has_no_ratio(self):
        stages = stages_for("spike", 50, 100)

        figures = derived_figures("spike", stages, [(80.0, 150.0, False)])

        assert figures["baseline_p95_ms"] is None
        assert figures["recovery_p95_ratio"] is None


class TestSoak:
    def test_first_and_last_tenth_and_their_drift(self):
        stages = stages_for("soak", 5, 300)
        samples = [(5.0, 100.0, False), (150.0, 999.0, False), (295.0, 250.0, False)]

        figures = derived_figures("soak", stages, samples)

        assert figures == {
            "first_window_p95_ms": 100.0,
            "last_window_p95_ms": 250.0,
            "p95_drift_ratio": 2.5,
        }

    def test_a_soak_that_stopped_early_measures_back_from_when_it_stopped(self):
        stages = stages_for("soak", 5, 300)
        samples = [(5.0, 100.0, False), (95.0, 400.0, False)]

        figures = derived_figures("soak", stages, samples, elapsed_s=100.0)

        assert figures["last_window_p95_ms"] == 400.0


class TestStageFigures:
    def test_rows_carry_the_definition_and_the_measurements(self):
        stages = stages_for("load", 2, 10)
        samples = [(1.0, 10.0, False), (2.0, 30.0, True), (10.5, 20.0, False)]

        (row,) = stage_figures(stages, samples)

        assert row["name"] == "steady"
        assert row["responses"] == 3  # the straggler past the end lands in the last stage
        assert row["p50_ms"] == 20.0
        assert row["error_rate"] == pytest.approx(0.3333)

    def test_an_empty_stage_reports_none_rather_than_zero(self):
        (row,) = stage_figures(stages_for("load", 1, 10), [])

        assert row["responses"] == 0
        assert row["p95_ms"] is None
        assert row["error_rate"] is None

    def test_a_constant_load_has_no_derived_figures(self):
        assert derived_figures("load", stages_for("load", 1, 10), []) == {}


# ── Percentiles (moved verbatim from test_load_runner) ────────────────


class TestPercentileRank:
    """Nearest rank, which is `ceil` — `round(x + 0.5)` is not.

    On an exact half Python rounds to even and goes *down*, so twenty
    samples put p95 on the maximum instead of the nineteenth value.
    """

    def test_p95_of_twenty_samples_is_the_nineteenth_not_the_maximum(self):
        values = [float(n) for n in range(1, 21)]

        assert percentile(values, 0.95) == 19.0

    def test_p50_of_twenty_samples_is_the_tenth(self):
        values = [float(n) for n in range(1, 21)]

        assert percentile(values, 0.50) == 10.0

    def test_an_empty_sample_is_zero(self):
        assert percentile([], 0.95) == 0.0


# ── The import rule ───────────────────────────────────────────────────


def test_only_the_standard_library_is_imported():
    """The Locust child imports this file directly and must not reach `backend`."""
    tree = ast.parse(Path(load_shapes.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert imported <= {"__future__", "math", "dataclasses"}
