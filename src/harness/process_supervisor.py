from __future__ import annotations

import os
import signal
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.security import sanitize_for_logging


PROCESS_RECORD_SCHEMA_VERSION = "harness.process_record/v1"
PROCESS_RESULT_SCHEMA_VERSION = "harness.process_result/v1"


Command = str | Sequence[str]


class ProcessRecord(BaseModel):
    schema_version: str = PROCESS_RECORD_SCHEMA_VERSION
    process_id: str
    pid: int
    owner: str
    command: list[str] | str
    display_command: str
    cwd: str
    session_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    timeout_seconds: float | None = None
    started_at: datetime
    status: str = "running"


class SupervisedProcessResult(BaseModel):
    schema_version: str = PROCESS_RESULT_SCHEMA_VERSION
    process_id: str
    pid: int
    owner: str
    command: list[str] | str
    display_command: str
    cwd: str
    session_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    timeout_seconds: float | None = None
    started_at: datetime
    finished_at: datetime
    status: str
    exit_code: int | None = None
    timed_out: bool = False
    killed: bool = False
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    errors: list[str] = Field(default_factory=list)


class ProcessSupervisor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._records: dict[str, ProcessRecord] = {}

    def active_process_ids(self) -> list[str]:
        with self._lock:
            return list(self._processes)

    def active_records(self) -> list[ProcessRecord]:
        with self._lock:
            return list(self._records.values())

    def get(self, process_id: str) -> ProcessRecord | None:
        with self._lock:
            return self._records.get(process_id)

    def run(
        self,
        command: Command,
        *,
        cwd: Path | str,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
        executable: str | None = None,
        timeout_seconds: float | None = None,
        owner: str = "process_supervisor",
        session_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        on_start: Callable[[ProcessRecord], None] | None = None,
    ) -> SupervisedProcessResult:
        process_id = f"proc_{uuid.uuid4().hex[:12]}"
        started_at = _utc_now()
        display_command = _display_command(command)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": dict(env) if env is not None else None,
            "shell": shell,
            "executable": executable,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "start_new_session": os.name == "posix",
        }
        process = subprocess.Popen(command, **popen_kwargs)
        record = ProcessRecord(
            process_id=process_id,
            pid=process.pid,
            owner=owner,
            command=_command_payload(command),
            display_command=display_command,
            cwd=str(cwd),
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            timeout_seconds=timeout_seconds,
            started_at=started_at,
        )
        with self._lock:
            self._processes[process_id] = process
            self._records[process_id] = record
        if on_start is not None:
            on_start(record)
        stdout = ""
        stderr = ""
        timed_out = False
        killed = False
        errors: list[str] = []
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            killed = True
            stdout = _decode_output(exc.stdout)
            stderr = _decode_output(exc.stderr)
            self.kill(process_id)
            try:
                more_stdout, more_stderr = process.communicate(timeout=2)
                stdout += more_stdout or ""
                stderr += more_stderr or ""
            except subprocess.TimeoutExpired:
                errors.append("Process did not exit after timeout kill.")
        finally:
            with self._lock:
                self._processes.pop(process_id, None)
                self._records.pop(process_id, None)
        exit_code = None if timed_out else process.returncode
        status = "timed_out" if timed_out else "completed" if exit_code == 0 else "failed"
        stdout = str(sanitize_for_logging(stdout or ""))
        stderr = str(sanitize_for_logging(stderr or ""))
        return SupervisedProcessResult(
            process_id=process_id,
            pid=process.pid,
            owner=owner,
            command=_command_payload(command),
            display_command=display_command,
            cwd=str(cwd),
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            timeout_seconds=timeout_seconds,
            started_at=started_at,
            finished_at=_utc_now(),
            status=status,
            exit_code=exit_code,
            timed_out=timed_out,
            killed=killed,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
            errors=errors,
        )

    def kill(self, process_id: str) -> bool:
        with self._lock:
            process = self._processes.get(process_id)
        if process is None or process.poll() is not None:
            return False
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return False
        except Exception:
            try:
                process.kill()
            except Exception:
                return False
        return True

    def kill_all(self) -> list[str]:
        with self._lock:
            process_ids = list(self._processes)
        killed: list[str] = []
        for process_id in process_ids:
            if self.kill(process_id):
                killed.append(process_id)
        return killed


_SUPERVISORS: dict[Path, ProcessSupervisor] = {}
_SUPERVISORS_LOCK = threading.RLock()


def get_process_supervisor(project_root: Path | str) -> ProcessSupervisor:
    root = Path(project_root).resolve()
    with _SUPERVISORS_LOCK:
        supervisor = _SUPERVISORS.get(root)
        if supervisor is None:
            supervisor = ProcessSupervisor()
            _SUPERVISORS[root] = supervisor
        return supervisor


def reset_process_supervisor(project_root: Path | str | None = None) -> None:
    with _SUPERVISORS_LOCK:
        if project_root is None:
            supervisors = list(_SUPERVISORS.values())
            _SUPERVISORS.clear()
        else:
            supervisor = _SUPERVISORS.pop(Path(project_root).resolve(), None)
            supervisors = [supervisor] if supervisor is not None else []
    for supervisor in supervisors:
        supervisor.kill_all()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _command_payload(command: Command) -> list[str] | str:
    if isinstance(command, str):
        return str(sanitize_for_logging(command))
    return [str(sanitize_for_logging(part)) for part in command]


def _display_command(command: Command) -> str:
    if isinstance(command, str):
        return str(sanitize_for_logging(command))
    return " ".join(str(sanitize_for_logging(part)) for part in command)


def _decode_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
