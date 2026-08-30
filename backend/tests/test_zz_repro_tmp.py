"""TEMPORARY reproduction — delete after review.

Makes _FakeBrowser.__enter__ do what the real BrowserSession.__enter__ does:
navigate to base_urls[0] and fire the arrival hook with the settled URL.
"""

import pytest

from backend.models.database import NonfunctionalChildStatus
from backend.tasks.run_nonfunctional import run_nonfunctional_task

from .test_run_nonfunctional import (  # noqa: F401
    BASE_URL,
    _FakeBrowser,
    _seed_run,
    _targets,
    patched,
    task_module,
)


class _RealisticBrowser(_FakeBrowser):
    """__enter__ opens on base_urls[0] and fires the hook, as production does."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._settled = kwargs.pop("settled_url", None) or self.current_url

    def __enter__(self):
        self.current_url = self._settled
        if self.on_navigated is not None:
            self.on_navigated(self._settled, task_module.time.monotonic() + 30)
        return self


@pytest.mark.parametrize(
    "settled",
    [
        BASE_URL,  # no redirect, identical spelling
        BASE_URL.rstrip("/") + "/login",  # a redirect to a login page
    ],
)
def test_enter_hook_effect(db_session, patched, monkeypatch, settled):  # noqa: F811
    _sprint, _requirement, run = _seed_run(db_session)

    def _factory(**kwargs):
        browser = _RealisticBrowser(settled_url=settled, **kwargs)
        browser.visits = []
        patched["browser"] = browser
        return browser

    monkeypatch.setattr(task_module.browser_session, "BrowserSession", _factory)
    run_nonfunctional_task(run.id)

    targets = _targets(db_session, run.id)
    print(f"\n--- settled={settled!r} BASE_URL={BASE_URL!r}")
    for t in targets:
        print(
            f"    target pos={t.position} url={t.url!r} status={t.status!r} "
            f"a11y={t.a11y_outcome!r} error={t.error!r}"
        )
    errored = [t for t in targets if t.status == NonfunctionalChildStatus.ERROR]
    print(f"    -> {len(targets)} target(s), {len(errored)} errored")
