"""Run ``crucible`` commands for the UI, as processes that outlive the UI.

A campaign can run for an hour or more. If it ran inside the UI server, closing
or restarting the UI would kill a campaign mid-experiment: the case DESIGN.md §7
says leaves the target in a state no manifest describes. So each action is its
own process, in its own session, writing to a log file under the state
directory. The UI only starts it and reads its log.

Each job directory holds three files:

- ``job.json``: what was run, by which label, when, and the process id;
- ``output.log``: everything the command printed;
- ``exit_code``: written by the runner when the command returns.

A job with no ``exit_code`` whose process the UI no longer holds is reported as
``unknown``, never as running or finished. That covers a job started by an
earlier UI session on Windows, where checking a process id safely needs more
than the standard library. Same rule as an unsampled gauge (§4.2): say what was
not observed.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JOBS_SUBDIR = "ui-jobs"
RUNNING, FINISHED, UNKNOWN = "running", "finished", "unknown"

#: Processes started by this UI process, so their state can be read directly.
_LIVE: dict[str, subprocess.Popen[bytes]] = {}


@dataclass
class Job:
    id: str
    label: str
    args: list[str]
    started_at_epoch_s: float
    pid: int | None
    directory: Path
    status: str = UNKNOWN
    exit_code: int | None = None

    @property
    def command(self) -> str:
        return self.args[0] if self.args else ""

    @property
    def command_line(self) -> str:
        return "crucible " + " ".join(self.args)

    @property
    def log_path(self) -> Path:
        return self.directory / "output.log"


def jobs_root(state_dir: Path) -> Path:
    return Path(state_dir) / JOBS_SUBDIR


def start_job(state_dir: Path, cwd: Path, args: list[str], label: str = "") -> Job:
    """Start ``crucible <args>`` detached, logging to the job directory."""
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    directory = jobs_root(state_dir) / job_id
    directory.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    detach: dict[str, Any] = (
        {"start_new_session": True}
        if os.name == "posix"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    )
    with open(directory / "output.log", "wb") as log:
        process = subprocess.Popen(  # noqa: S603 -- argv list, no shell
            [sys.executable, "-m", "crucible.ui.jobs", str(directory), "--", *args],
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **detach,
        )
    _LIVE[job_id] = process
    started = time.time()
    (directory / "job.json").write_text(
        json.dumps(
            {"id": job_id, "label": label or args[0], "args": args,
             "started_at_epoch_s": started, "pid": process.pid, "cwd": str(cwd)},
            indent=2,
        ),
        encoding="utf-8",
    )
    return Job(job_id, label or args[0], list(args), started, process.pid, directory, RUNNING)


def _pid_alive(pid: int | None) -> bool | None:
    """``None`` where it cannot be told safely (os.kill(pid, 0) terminates on Windows)."""
    if not pid or os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_job(directory: Path) -> Job | None:
    try:
        meta = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job = Job(
        id=str(meta.get("id", directory.name)),
        label=str(meta.get("label", "")),
        args=[str(a) for a in meta.get("args") or []],
        started_at_epoch_s=float(meta.get("started_at_epoch_s") or 0.0),
        pid=meta.get("pid"),
        directory=directory,
    )
    exit_file = directory / "exit_code"
    if exit_file.exists():
        try:
            job.exit_code = int(exit_file.read_text(encoding="utf-8").strip())
        except ValueError:
            job.exit_code = None
        job.status = FINISHED
        return job
    process = _LIVE.get(job.id)
    if process is not None:
        code = process.poll()
        if code is None:
            job.status = RUNNING
        else:
            job.status, job.exit_code = FINISHED, code
        return job
    alive = _pid_alive(job.pid)
    job.status = RUNNING if alive else UNKNOWN
    return job


def get_job(state_dir: Path, job_id: str) -> Job | None:
    if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
        return None
    directory = jobs_root(state_dir) / job_id
    return load_job(directory) if directory.is_dir() else None


def list_jobs(state_dir: Path, limit: int = 50) -> list[Job]:
    """Newest first."""
    root = jobs_root(state_dir)
    if not root.exists():
        return []
    jobs = [job for d in sorted(root.iterdir(), reverse=True) if d.is_dir() and (job := load_job(d))]
    return jobs[:limit]


def read_output(job: Job, max_bytes: int = 200_000) -> str:
    """The tail of what the command printed."""
    try:
        with open(job.log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return ("[...earlier output truncated...]\n" + text) if size > max_bytes else text


# ---------------------------------------------------------------------------
# The runner: python -m crucible.ui.jobs <job_dir> -- <crucible args>
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> int:
    directory = Path(argv[0])
    args = argv[2:] if len(argv) > 1 and argv[1] == "--" else argv[1:]
    print("$ crucible " + " ".join(args), flush=True)
    code = 1
    try:
        from crucible.cli import main

        sys.argv = ["crucible", *args]
        result = main()
        code = int(result or 0)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:  # noqa: BLE001 -- the log must say why, whatever it was
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        (directory / "exit_code").write_text(str(code), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
