from __future__ import annotations

import sys

from harness.process_supervisor import ProcessRecord, ProcessSupervisor, reset_process_supervisor


def test_process_supervisor_captures_output_and_unregisters_after_completion(tmp_path) -> None:
    supervisor = ProcessSupervisor()
    started: list[ProcessRecord] = []

    result = supervisor.run(
        [sys.executable, "-c", "print('hello')"],
        cwd=tmp_path,
        owner="test",
        session_id="sess",
        run_id="run",
        on_start=started.append,
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.session_id == "sess"
    assert result.run_id == "run"
    assert started[0].process_id == result.process_id
    assert supervisor.active_process_ids() == []
    assert supervisor.get(result.process_id) is None


def test_process_supervisor_times_out_and_kills_process(tmp_path) -> None:
    supervisor = ProcessSupervisor()

    result = supervisor.run(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.2,
        owner="test-timeout",
    )

    assert result.status == "timed_out"
    assert result.timed_out is True
    assert result.killed is True
    assert result.exit_code is None
    assert "started" in result.stdout
    assert supervisor.active_process_ids() == []


def test_process_supervisor_singleton_can_be_reset(tmp_path) -> None:
    from harness.process_supervisor import get_process_supervisor

    first = get_process_supervisor(tmp_path)
    second = get_process_supervisor(tmp_path)
    reset_process_supervisor(tmp_path)
    third = get_process_supervisor(tmp_path)

    assert first is second
    assert third is not first
