"""Per-collection hooks: before_each / after_each / on_abort (§10).

Hooks solve database drift: experiment 1 inserts 100k rows; experiment 2 then
runs against a bigger table and is not comparable. ``before_each`` is where
reset-and-reseed, token refresh and log capture live.

**A failing hook blocks the experiment** (§10). A reset that silently did
nothing would corrupt every later measurement without leaving a visible trace.
Blocking is the only safe option; the campaign records the failure on the
manifest so an operator can distinguish "hook failed" from "measurement failed".

Hooks are defined per collection, not per campaign: the same investigation
against the same service always runs the same reset logic. Each hook is a shell
command or an async callable; shell commands are executed in the workspace
directory so relative paths work.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from dataclasses import dataclass
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


@dataclass
class HookSet:
    """The three hooks that guard one experiment (§10).

    Each hook is either:
    - a string (shell command, run with ``sh -c`` in the workspace directory);
    - a zero-arg async callable returning nothing (for programmatic use in tests).

    ``None`` means "not configured"; the slot is skipped silently.

    All three are optional. A collection with no hooks defined runs as today:
    there is no overhead and no change in behaviour.
    """

    before_each: "str | Any | None" = None
    after_each: "str | Any | None" = None
    on_abort: "str | Any | None" = None

    #: Directory to run shell commands in. Defaults to the current directory.
    workspace: "str | None" = None
    #: How long a hook may run before it is declared failed. 0 = no timeout.
    timeout_s: float = 60.0

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

        Does NOT raise HookFailed on failure — the campaign is already aborting
        and there is nothing productive to block. The failure is still recorded.
        """
        result = await self._run("on_abort", self.on_abort, experiment=None, raise_on_fail=False)
        return result

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
        return await self._run_shell(name, str(hook), t0, raise_on_fail=raise_on_fail)

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

    async def _run_shell(
        self, name: str, command: str, t0: float, *, raise_on_fail: bool
    ) -> HookResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.workspace or None,
            )
            timeout = self.timeout_s or None
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                err = f"hook {name!r} timed out after {self.timeout_s:.0f} s"
                result = HookResult(hook=name, ok=False, exit_code=None, output="", duration_s=time.monotonic() - t0, error=err)
                if raise_on_fail:
                    raise HookFailed(err)
                return result

            output = (stdout or b"").decode("utf-8", errors="replace")
            code = proc.returncode or 0
            ok = (code == 0)
            result = HookResult(
                hook=name,
                ok=ok,
                exit_code=code,
                output=output[:2048],  # truncate; a hook that spews megabytes fills the manifest
                duration_s=time.monotonic() - t0,
                error="" if ok else f"exit {code}",
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, HookFailed):
                raise
            err = f"hook {name!r} could not be launched: {exc}"
            result = HookResult(hook=name, ok=False, exit_code=None, output="", duration_s=time.monotonic() - t0, error=err)

        if not result.ok and raise_on_fail:
            raise HookFailed(result.error or f"hook {name!r} failed")
        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def hooks_from_mapping(mapping: "dict[str, Any] | None", workspace: "str | None" = None) -> HookSet:
    """Build a ``HookSet`` from a collection-YAML ``hooks:`` block.

    Expected shape::

        hooks:
          before_each: "psql -c 'truncate orders'"
          after_each:  "curl -s http://target/flush-cache"
          on_abort:    "cp /var/log/app.log /tmp/abort-dump.log"
          timeout_s: 120
    """
    if not mapping:
        return HookSet(workspace=workspace)
    return HookSet(
        before_each=mapping.get("before_each"),
        after_each=mapping.get("after_each"),
        on_abort=mapping.get("on_abort"),
        workspace=workspace,
        timeout_s=float(mapping.get("timeout_s", 60)),
    )
