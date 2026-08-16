"""Batch concurrency (plan §10): the DUB_LOCK serializes dub/mux to 1 across the whole batch,
while non-dub work runs up to the pool width. Uses fake sleepy jobs — no ffmpeg, no engines.

We don't run the real ``ytpipe`` here (it would call framework leaves); we exercise the exact
concurrency contract the batch runner relies on: a single ``threading.Semaphore(1)`` held around
the dub/mux critical section, and a ``ThreadPoolExecutor`` of width N for everything else.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from lib import batch


class _Peak:
    """Tracks the peak number of threads simultaneously inside a region."""

    def __init__(self):
        self._cur = 0
        self._peak = 0
        self._lock = threading.Lock()

    def enter(self):
        with self._lock:
            self._cur += 1
            self._peak = max(self._peak, self._cur)

    def leave(self):
        with self._lock:
            self._cur -= 1

    @property
    def peak(self):
        return self._peak


def test_dub_lock_serializes_to_one():
    """No matter the pool width, at most ONE thread is inside the dub-lock region at a time."""
    dub_peak = _Peak()
    n_jobs = 6

    def job(_i):
        with batch.DUB_LOCK:  # the exact lock the pipeline holds around dub/mux
            dub_peak.enter()
            time.sleep(0.02)
            dub_peak.leave()

    with ThreadPoolExecutor(max_workers=n_jobs) as pool:
        list(pool.map(job, range(n_jobs)))

    assert dub_peak.peak == 1


def test_non_dub_work_runs_up_to_pool_width():
    """Work OUTSIDE the dub lock parallelizes up to the pool width."""
    non_dub_peak = _Peak()
    width = 3
    n_jobs = 6

    def job(_i):
        # non-dub region (e.g. transcribe/translate) — no lock.
        non_dub_peak.enter()
        time.sleep(0.03)
        non_dub_peak.leave()

    with ThreadPoolExecutor(max_workers=width) as pool:
        list(pool.map(job, range(n_jobs)))

    # peak concurrency should reach the pool width (>1, up to width).
    assert non_dub_peak.peak == width


def test_dub_serialized_while_non_dub_parallel_in_one_run():
    """Mixed: each 'video' does parallel non-dub work then a serialized dub. Dub peak stays 1,
    non-dub peak exceeds 1."""
    dub_peak = _Peak()
    non_dub_peak = _Peak()
    width = 4
    n_jobs = 8

    def pipeline(_i):
        non_dub_peak.enter(); time.sleep(0.01); non_dub_peak.leave()  # transcribe/translate
        with batch.DUB_LOCK:                                          # dub + mux
            dub_peak.enter(); time.sleep(0.01); dub_peak.leave()

    with ThreadPoolExecutor(max_workers=width) as pool:
        list(pool.map(pipeline, range(n_jobs)))

    assert dub_peak.peak == 1
    assert non_dub_peak.peak > 1


def test_build_entries_single_and_list(tmp_path):
    import json

    # single ref
    entries = batch.build_entries(ref="abc123")
    assert entries == [{"id": "abc123", "url": "abc123"}]

    # list file
    lst = tmp_path / "list.json"
    lst.write_text(json.dumps({"videos": [
        {"id": "a", "url": "ua"},
        {"id": "b", "url": "ub", "overrides": {"dubLangs": ["en"]}},
    ]}), encoding="utf-8")
    entries = batch.build_entries(list_path=str(lst))
    assert [e["id"] for e in entries] == ["a", "b"]
    assert entries[1]["overrides"]["dubLangs"] == ["en"]


def test_build_entries_rejects_both_or_neither():
    import pytest

    with pytest.raises(ValueError):
        batch.build_entries()
    with pytest.raises(ValueError):
        batch.build_entries(ref="x", list_path="y")
