from __future__ import annotations

import os

import pytest

from tools.idle_benchmark import (
    PRIVATE_WORKING_SET_LIMIT_BYTES,
    WindowsProcessResources,
    cpu_percent_one_core,
    evaluate_budget,
    run_benchmark,
)


def test_cpu_percent_is_relative_to_one_core():
    assert cpu_percent_one_core(10.0, 10.25, 5.0) == pytest.approx(5.0)
    assert cpu_percent_one_core(2.0, 1.0, 5.0) == 0.0
    with pytest.raises(ValueError):
        cpu_percent_one_core(0.0, 1.0, 0.0)


def test_budget_operators_are_strict_and_serialisable():
    passing = evaluate_budget(0.999, PRIVATE_WORKING_SET_LIMIT_BYTES)
    assert passing["passed"] is True
    assert passing["cpu_one_core"]["operator"] == "<"
    assert passing["private_working_set"]["operator"] == "<="
    assert passing["measurement_duration"]["operator"] == ">="

    cpu_boundary = evaluate_budget(1.0, 1)
    assert cpu_boundary["passed"] is False
    assert cpu_boundary["cpu_one_core"]["passed"] is False

    memory_over = evaluate_budget(
        0.0, PRIVATE_WORKING_SET_LIMIT_BYTES + 1)
    assert memory_over["passed"] is False
    assert memory_over["private_working_set"]["passed"] is False

    too_short = evaluate_budget(0.0, 1, duration_seconds=59.999)
    assert too_short["passed"] is False
    assert too_short["measurement_duration"]["passed"] is False


def test_non_resource_checks_participate_in_result():
    webview = evaluate_budget(0.0, 1, no_webview_imported=False)
    shutdown = evaluate_budget(0.0, 1, shutdown_confirmed=False)
    assert webview["passed"] is False
    assert shutdown["passed"] is False


@pytest.mark.skipif(os.name != "nt", reason="Win32 resource API")
def test_windows_resource_reader_reports_coherent_current_process_values():
    reader = WindowsProcessResources()
    sample = reader.sample_basic()
    private_working_set = reader.private_working_set_bytes(
        sample.working_set_bytes)

    assert sample.cpu_seconds >= 0
    assert sample.working_set_bytes > 0
    assert sample.private_commit_bytes > 0
    assert 0 < private_working_set <= sample.working_set_bytes
    shape = reader.process_shape()
    assert shape.handle_count > 0
    assert shape.thread_count > 0


@pytest.mark.skipif(os.name != "nt", reason="Win32 resource API")
def test_short_benchmark_runs_full_mock_service_and_stops_cleanly():
    report = run_benchmark(
        duration_seconds=0.05,
        warmup_seconds=0,
        sample_interval_seconds=0.01,
    )
    assert report["service_exit_status"] == 0
    assert report["checks"]["safe_shutdown_confirmed"]["passed"] is True
    assert report["checks"]["webview_not_imported"]["passed"] is True
    assert report["measurement"]["handles_cold"] > 0
    assert report["measurement"]["threads_cold"] > 0
