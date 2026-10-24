"""Per-collection hooks: before_each / after_each / on_abort (§10).

Hooks solve database drift: experiment 1 inserts 100k rows; experiment 2 then
runs against a bigger table and is not comparable. ``before_each`` is where
reset-and-reseed, token refresh and log capture live.

**A failing hook blocks the experiment** (§10). A reset that silently did
nothing would corrupt every later measurement without leaving a visible trace.
Blocking is the only safe option; the campaign records the failure on the
manifest so an operator can distinguish "hook failed" from "measurement failed".

Hooks are defined per collection, not per campaign: the same investigation
against the same service always runs the same reset logic. Since 24 October 2026
(operator decision) a hook is a Python file, run with Crucible's own interpreter
in isolated mode -- no shell, so no question of which runtime or tools exist --
with an allowlisted environment that never carries Crucible's own secrets.
Running hooks as a separate low-privilege OS user, so a hook cannot read
Crucible's code, state or reports, is a deployment step (docs/HOOKS.md).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HookFailed(RuntimeError):
    """A hook returned a non-zero exit code or raised an exception.

    §10: a failing hook blocks the experiment. The error is recorded on the
    manifest; the campaign does not proceed past the hook.
    """


@dataclass
class HookResult:
    """What happened when a hook ran."""

    hook: str
    ok: bool
    exit_code: int | None
    output: str
    duration_s: float
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hook": self.hook,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "output": self.output,
            "duration_s": self.duration_s,
            "error": self.error,
        }


#: Environment a hook process starts with: only these from Crucible's own
#: environment (allowlist, never blocklist, DESIGN.md §12), plus the operator's
#: declared ``pass_env`` and three CRUCIBLE_HOOK_* context variables. Crucible's
#: own variables -- the control token, API keys -- never reach a hook.
BASE_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG",
            "LC_ALL", "LC_CTYPE", "TZ", "PYTHONIOENCODING")
HOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.py$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
STAGES = ("before_each", "after_each", "on_abort")


def check_hook_name(name: str) -> str:
    if not HOOK_NAME.match(name):
        raise ValueError(f"hook {name!r} must be the plain name of an uploaded .py file")
    return name


def check_pass_env(names: list[str]) -> list[str]:
    for name in names:
        if not ENV_NAME.match(name):
            raise ValueError(f"pass_env entry {name!r} is not an environment variable name")
        if name.upper().startswith("CRUCIBLE_"):
            raise ValueError(f"pass_env may not pass {name!r}: Crucible's own variables never reach a hook")
    return list(names)


@dataclass
class HookSet:
    """The three hooks that guard one experiment (§10).

    Each hook is either:
    - the plain name of a ``.py`` file in ``hooks_dir`` (operator-uploaded),
      run as ``python -I <file>``: Crucible's interpreter, isolated mode, no
      shell, in the workspace, with an allowlisted environment and a timeout;
    - a zero-arg (async) callable, for programmatic use in tests.

    ``None`` means "not configured"; the slot is skipped silently.
    """

    before_each: "str | Any | None" = None
    after_each: "str | Any | None" = None
    on_abort: "str | Any | None" = None

    #: Directory the hook runs in (the target's workspace). Defaults to cwd.
    workspace: "str | None" = None
    #: How long a hook may run before it is declared failed. 0 = no timeout.
    timeout_s: float = 60.0
    #: Where hook files live (``<state>/hooks``).
    hooks_dir: "str | None" = None
    #: Variables the operator explicitly passes through, e.g. a DB password.
    pass_env: tuple[str, ...] = ()
    run_id: str = ""

    def has_hooks(self) -> bool:
        return any(h is not None for h in (self.before_each, self.after_each, self.on_abort))

    async def run_before_each(self, experiment: int) -> HookResult:
        """Run before_each. Raises ``HookFailed`` if the hook fails."""
        return await self._run("before_each", self.before_each, experiment=experiment)

    async def run_after_each(self, experiment: int) -> HookResult:
        """Run after_each. Raises ``HookFailed`` if the hook fails."""
        return await self._run("after_each", self.after_each, experiment=experiment)

    async def run_on_abort(self) -> HookResult:
        """Run on_abort. §10: called when the campaign aborts.

        Does NOT raise HookFailed on failure -- the campaign is already aborting
        and there is nothing productive to block. The failure is still recorded.
        """
        return await self._run("on_abort", self.on_abort, experiment=None, raise_on_fail=False)

    # ------------------------------------------------------------------

    async def _run(
        self,
        name: str,
        hook: "str | Any | None",
        *,
        experiment: "int | None",
        raise_on_fail: bool = True,
    ) -> HookResult:
        if hook is None:
            return HookResult(hook=name, ok=True, exit_code=None, output="", duration_s=0.0)

        t0 = time.monotonic()
        if callable(hook):
            return await self._run_callable(name, hook, t0, raise_on_fail=raise_on_fail)
        return await self._run_python(name, str(hook), experiment, t0, raise_on_fail=raise_on_fail)

    async def _run_callable(
        self, name: str, fn: Any, t0: float, *, raise_on_fail: bool
    ) -> HookResult:
        try:
            if asyncio.iscoroutinefunction(fn):
                await asyncio.wait_for(fn(), timeout=self.timeout_s or None)
            else:
                fn()
        except asyncio.TimeoutError:
            err = f"hook {name!r} timed out after {self.timeout_s:.0f} s"
            result = HookResult(hook=name, ok=False, exit_code=None, output="", duration_s=time.monotonic() - t0, error=err)
            if raise_on_fail:
                raise HookFailed(err)
            return result
        except Exception as exc:  # noqa: BLE001
            err = f"hook {name!r} raised: {exc}"
            result = HookResult(hook=name, ok=False, exit_code=None, output="", duration_s=time.monotonic() - t0, error=err)
            if raise_on_fail:
                raise HookFailed(err) from exc
            return result
        return HookResult(hook=name, ok=True, exit_code=0, output="", duration_s=time.monotonic() - t0)

    def environment(self, stage: str, experiment: "int | None") -> dict[str, str]:
        env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
        env.update({k: os.environ[k] for k in self.pass_env if k in os.environ})
        env.update({"CRUCIBLE_HOOK_STAGE": stage, "CRUCIBLE_HOOK_RUN_ID": self.run_id,
                    "CRUCIBLE_HOOK_EXPERIMENT": "" if experiment is None else str(experiment)})
        return env

    async def _run_python(
        self, name: str, file_name: str, experiment: "int | None", t0: float, *, raise_on_fail: bool
    ) -> HookResult:
        def failed(err: str) -> HookResult:
            result = HookResult(hook=name, ok=False, exit_code=None, output="",
                                duration_s=time.monotonic() - t0, error=err)
            if raise_on_fail:
                raise HookFailed(err)
            return result

        try:
            check_hook_name(file_name)
        except ValueError as exc:
            return failed(str(exc))
        path = Path(self.hooks_dir or ".") / file_name
        if not path.is_file():
            return failed(f"hook {name!r}: {file_name} has not been uploaded to {self.hooks_dir}")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(path.resolve()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.workspace or None,
                env=self.environment(name, experiment),
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s or None)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return failed(f"hook {name!r} timed out after {self.timeout_s:.0f} s")
        except OSError as exc:
            return failed(f"hook {name!r} could not be launched: {exc}")

        output = (stdout or b"").decode("utf-8", errors="replace")
        code = proc.returncode or 0
        result = HookResult(hook=name, ok=code == 0, exit_code=code,
                            output=output[:2048],  # a hook that spews megabytes must not fill the manifest
                            duration_s=time.monotonic() - t0, error="" if code == 0 else f"exit {code}")
        if not result.ok and raise_on_fail:
            raise HookFailed(f"hook {name!r} ({file_name}) exited {code}: {output.strip()[-300:]}")
        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def hooks_from_mapping(mapping: "dict[str, Any] | None", workspace: "str | None" = None,
                       hooks_dir: "str | None" = None, run_id: str = "") -> HookSet:
    """Build a ``HookSet`` from the SLA's ``hooks:`` block.

    Expected shape::

        hooks:
          before_each: reset_db.py      # plain names of uploaded .py files
          after_each:  collect_logs.py
          on_abort:    dump_state.py
          timeout_s: 120
          pass_env: [PGPASSWORD]        # passed through explicitly; never CRUCIBLE_*
    """
    if not mapping:
        return HookSet(workspace=workspace, hooks_dir=hooks_dir, run_id=run_id)
    hooks = {stage: mapping.get(stage) for stage in STAGES}
    for value in hooks.values():
        if value is not None:
            check_hook_name(str(value))
    return HookSet(
        **hooks,
        workspace=workspace,
        timeout_s=float(mapping.get("timeout_s", 60)),
        hooks_dir=hooks_dir,
        pass_env=tuple(check_pass_env(list(mapping.get("pass_env") or []))),
        run_id=run_id,
    )
