from __future__ import annotations

import pytest

import rlm_ig as rlm
from rlm_ig.models.scripted import Scripted


@pytest.fixture
def cfg(tmp_path):
    """A quiet config that keeps traces inside the test's temp directory."""
    return rlm.Config(verbosity=0, trace_dir=str(tmp_path / "traces"), max_parallel=8)


@pytest.fixture
def solve(cfg):
    """Run a task against a scripted model and return the Result."""

    def _solve(script, task="Do the task.", context=None, *, asks=None, **kwargs):
        config = kwargs.pop("config", cfg)
        model = script if isinstance(script, rlm.Model) else Scripted(script, asks=asks)
        return rlm.run(task, context, model=model, config=config, **kwargs)

    return _solve
