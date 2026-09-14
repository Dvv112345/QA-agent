"""The one derivation of a load profile's stages, and the arithmetic over them.

Pure and **stdlib-only**, on purpose: the Locust child process imports this
module as a sibling file (``import load_shapes``), and the child must not
import ``backend`` — which would drag psycopg2, RQ and the rest of the worker
into a gevent-patched interpreter. A test pins the import allowlist.

Stages are *derived* from ``(shape, users, duration)``, never stored as
inputs, and written into the profile's result alongside the figures, so a
later change to these formulas cannot re-describe a run that already happened.

Timelines (``d`` = duration, ``u`` = users)::

    load / soak   u ┤████████████████████████████  one "steady" stage
                    0                            d

    stress        u ┤                     ┌──────  k = min(5, u) steps of d/k,
                    │               ┌─────┘        step i holds ceil(u·i/k);
                    │         ┌─────┘              arrivals spread over the
                    │   ┌─────┘                    first 10% of each step
                    │───┘                          (▒ = ramp, not measured)
                    0  ▒     ▒     ▒     ▒     ▒ d

    spike         u ┤          ┌────┐              baseline max(1, u // 10)
                    │          │    │              for 40%, u for 20%
                    │──────────┘    └──────────    (instant), baseline for 40%
                    0         .4d  .6d          d

Nothing here judges a figure. Performance is data only: a p95 that rises
across stress steps is a measurement, not a failure.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

STRESS_MAX_STEPS = 5
# Fraction of a stress step over which its new users arrive.
STRESS_RAMP_FRACTION = 0.1
SPIKE_BASELINE_DIVISOR = 10
# Where the burst starts and ends, as fractions of the duration.
SPIKE_START_FRACTION = 0.4
SPIKE_END_FRACTION = 0.6
# The soak compares its first and last slices of wall time.
SOAK_WINDOW_FRACTION = 0.1

# A response the child observed: (seconds since the test started, latency in
# ms, whether it counts as an error). Errors follow the load runner's rule:
# no response at all, or a 5xx.
Sample = tuple[float, float, bool]


@dataclass(frozen=True)
class Stage:
    """One span of a profile with a fixed target user count.

    ``measure_from_s`` is where the stage's figures start counting. It equals
    ``start_s`` everywhere except stress, where the arrival ramp is left out
    so the spawning itself does not skew the step's latency.
    """

    name: str
    start_s: float
    end_s: float
    users: int
    spawn_rate: float
    measure_from_s: float

    def to_dict(self) -> dict:
        return asdict(self)


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile — no interpolation, no numpy.

    ``ceil``, not ``round(x + 0.5)``: the latter meets Python's
    banker's rounding on an exact half and rounds *down* to even, so at
    twenty samples p95 answered the maximum where nearest rank wants the
    nineteenth. One sample out, always pessimistic, and only at some sizes.
    """
    if not sorted_values:
        return 0.0
    rank = math.ceil(fraction * len(sorted_values))
    return sorted_values[max(0, min(len(sorted_values) - 1, rank - 1))]


def min_duration(shape: str, min_step_seconds: int) -> int:
    """The shortest duration a shape accepts.

    Only stress has a floor: five steps each at least ``min_step_seconds``
    long. ``min_step_seconds`` is passed in rather than read from config,
    because this module may not import ``backend``.
    """
    return STRESS_MAX_STEPS * min_step_seconds if shape == "stress" else 1


def stages_for(shape: str, users: int, duration_seconds: float) -> list[Stage]:
    """The stages a profile runs, contiguous over ``[0, duration)``.

    An unknown shape runs as constant load — the routes refuse unknown shapes
    long before this, so the fallback only keeps this function total.
    """
    users = max(1, int(users))
    duration = max(1.0, float(duration_seconds))
    if shape == "stress":
        return _stress_stages(users, duration)
    if shape == "spike":
        return _spike_stages(users, duration)
    return [Stage("steady", 0.0, duration, users, float(users), 0.0)]


def _stress_stages(users: int, duration: float) -> list[Stage]:
    steps = min(STRESS_MAX_STEPS, users)
    step_s = duration / steps
    # Halved on very short steps so every step still measures something; at
    # any duration the route accepts (≥ 5 × the minimum step) this is inert.
    ramp_s = min(step_s / 2, max(1.0, STRESS_RAMP_FRACTION * step_s))
    stages: list[Stage] = []
    previous = 0
    for index in range(1, steps + 1):
        target = math.ceil(users * index / steps)
        start = (index - 1) * step_s
        end = duration if index == steps else index * step_s
        spawn_rate = float(max(1, math.ceil((target - previous) / ramp_s)))
        stages.append(Stage(f"step {index}", start, end, target, spawn_rate, start + ramp_s))
        previous = target
    return stages


def _spike_stages(users: int, duration: float) -> list[Stage]:
    base = max(1, users // SPIKE_BASELINE_DIVISOR)
    if users <= base:
        # Nothing to spike to. `derived_figures` says so rather than
        # reporting three phases that are the same phase.
        return [Stage("steady", 0.0, duration, users, float(users), 0.0)]
    spike_start = SPIKE_START_FRACTION * duration
    spike_end = SPIKE_END_FRACTION * duration
    return [
        Stage("baseline", 0.0, spike_start, base, float(base), 0.0),
        # The whole burst arrives in one second: that is what makes it a spike.
        Stage("spike", spike_start, spike_end, users, float(users), spike_start),
        Stage("recovery", spike_end, duration, base, float(users), spike_end),
    ]


# ── Figures ───────────────────────────────────────────────────────────


def _p95(samples: list[Sample]) -> float | None:
    if not samples:
        return None
    return round(percentile(sorted(latency for _t, latency, _e in samples), 0.95), 2)


def _in_stage(stage: Stage, samples: list[Sample], *, last: bool, measured: bool) -> list[Sample]:
    """Samples inside a stage — the last stage also takes stragglers past its end."""
    lower = stage.measure_from_s if measured else stage.start_s
    return [s for s in samples if s[0] >= lower and (last or s[0] < stage.end_s)]


def stage_figures(stages: list[Stage], samples: list[Sample]) -> list[dict]:
    """One row per stage: its definition plus what it measured."""
    rows: list[dict] = []
    for index, stage in enumerate(stages):
        last = index == len(stages) - 1
        everything = _in_stage(stage, samples, last=last, measured=False)
        measured = _in_stage(stage, samples, last=last, measured=True)
        errors = sum(1 for _t, _latency, is_error in measured if is_error)
        latencies = sorted(latency for _t, latency, _e in measured)
        rows.append(
            {
                **stage.to_dict(),
                "responses": len(everything),
                "p50_ms": round(percentile(latencies, 0.50), 2) if latencies else None,
                "p95_ms": round(percentile(latencies, 0.95), 2) if latencies else None,
                "error_rate": round(errors / len(measured), 4) if measured else None,
            }
        )
    return rows


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 3)


def derived_figures(
    shape: str,
    stages: list[Stage],
    samples: list[Sample],
    *,
    error_stop_at_s: float | None = None,
    elapsed_s: float | None = None,
) -> dict:
    """The shape-specific arithmetic, or ``{}`` for constant load.

    ``error_stop_at_s`` is when the error-rate stop fired, if it did.
    ``elapsed_s`` is how long the profile actually ran, which the soak's
    last window is measured back from — a soak that stopped early compares
    the slices it really had.
    """
    if shape == "stress":
        return _stress_figures(stages, samples, error_stop_at_s)
    if shape == "spike":
        return _spike_figures(stages, samples)
    if shape == "soak":
        end = elapsed_s if elapsed_s is not None else (stages[-1].end_s if stages else 0.0)
        return _soak_figures(samples, end)
    return {}


def _stress_figures(
    stages: list[Stage], samples: list[Sample], error_stop_at_s: float | None
) -> dict:
    figures: dict = {
        "p95_by_users": [
            [stage.users, _p95(_in_stage(stage, samples, last=i == len(stages) - 1, measured=True))]
            for i, stage in enumerate(stages)
        ],
        "stopped_between_users": None,
    }
    if error_stop_at_s is not None and stages:
        # A range, not a number: with 20% steps, "failed at 40 users" claims a
        # precision the steps never had.
        index = next(
            (i for i, stage in enumerate(stages) if error_stop_at_s < stage.end_s),
            len(stages) - 1,
        )
        previous = stages[index - 1].users if index > 0 else 0
        figures["stopped_between_users"] = [previous, stages[index].users]
    return figures


def _spike_figures(stages: list[Stage], samples: list[Sample]) -> dict:
    if len(stages) != 3:
        users = stages[0].users if stages else 0
        return {
            "note": (
                f"A spike needs more than {max(1, users // SPIKE_BASELINE_DIVISOR)} users to "
                "differ from its baseline, so this profile ran as constant load."
            )
        }
    baseline, spike, recovery = (
        _p95(_in_stage(stage, samples, last=i == 2, measured=True))
        for i, stage in enumerate(stages)
    )
    return {
        "baseline_p95_ms": baseline,
        "spike_p95_ms": spike,
        "recovery_p95_ms": recovery,
        "recovery_p95_ratio": _ratio(recovery, baseline),
    }


def _soak_figures(samples: list[Sample], elapsed_s: float) -> dict:
    window = SOAK_WINDOW_FRACTION * elapsed_s
    first = _p95([s for s in samples if s[0] < window])
    last = _p95([s for s in samples if s[0] >= elapsed_s - window])
    return {
        "first_window_p95_ms": first,
        "last_window_p95_ms": last,
        "p95_drift_ratio": _ratio(last, first),
    }
