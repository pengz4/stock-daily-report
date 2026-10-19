import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise

import pytest


def _request_start(directory):
    from stock_daily_report.providers.requests import ProviderRequestGate

    gate = ProviderRequestGate(
        directory, interval_seconds=0.04, cooldown_seconds=0
    )
    gate.acquire("akshare")
    return time.monotonic()


def test_request_spacing_is_shared_across_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    with context.Pool(3) as pool:
        starts = sorted(pool.map(_request_start, [tmp_path] * 3))

    assert all(b - a >= 0.03 for a, b in pairwise(starts))


def test_concurrent_threads_share_spacing(tmp_path):
    with ThreadPoolExecutor(max_workers=3) as executor:
        starts = sorted(executor.map(_request_start, [tmp_path] * 3))

    assert all(b - a >= 0.03 for a, b in pairwise(starts))


def test_provider_cooldown_is_shared_and_recovers(tmp_path):
    from stock_daily_report.providers.base import ProviderAvailabilityError
    from stock_daily_report.providers.requests import ProviderRequestGate

    first = ProviderRequestGate(
        tmp_path, interval_seconds=0, cooldown_seconds=0.05
    )
    second = ProviderRequestGate(
        tmp_path, interval_seconds=0, cooldown_seconds=0.05
    )
    first.record_failure("akshare")
    with pytest.raises(ProviderAvailabilityError, match="provider_circuit_open"):
        second.acquire("akshare")
    second.acquire("sina")
    time.sleep(0.06)
    second.acquire("akshare")


def test_invalid_gate_state_is_not_silently_ignored(tmp_path):
    from stock_daily_report.providers.requests import ProviderRequestGate

    gate = ProviderRequestGate(tmp_path, interval_seconds=0.01, cooldown_seconds=0)
    gate.acquire("akshare")
    path = next((tmp_path / ".request-limits").glob("*.json"))
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="request gate"):
        gate.acquire("akshare")


def test_interrupted_state_update_preserves_previous_gate_state(tmp_path, monkeypatch):
    from stock_daily_report.providers import requests as gate_module

    gate = gate_module.ProviderRequestGate(
        tmp_path, interval_seconds=0, cooldown_seconds=30
    )
    gate.acquire("akshare")
    path = next((tmp_path / ".request-limits").glob("*.json"))
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("interrupted update")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted update"):
        gate.record_failure("akshare")
    assert path.read_bytes() == before
