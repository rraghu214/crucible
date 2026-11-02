"""Which step of the experiment loop a run is on, read from its log. Group 55.

The Active page draws the loop each experiment follows. It used to be a static
diagram with one live dot. The campaign now prints one ``[campaign] <event>: ...``
line per stage (Group 53), so the current step can be read from the log, by the
server, as a pure function that is tested against real log lines.

The step ids match the nodes on the page. Two steps are not observed directly:
"recall" (reading earlier manifests) takes no measurable time before the model call,
so it is shown done as soon as the experiment starts and the wait is attributed to
"diagnose"; "record" (writing the manifest) is shown done when the run reports done.
"""

from __future__ import annotations

import re

NODES: tuple[str, ...] = (
    "baseline", "recall", "diagnose", "guard", "approve", "apply",
    "deploy", "verify", "remeasure", "verdict", "record",
)
#: Steps that repeat for every experiment; the baseline and the record do not.
_PER_EXPERIMENT = frozenset(NODES[1:-1])
_EVENT = re.compile(r"^\[campaign\] ([a-z_]+)(?:: (.*))?$")


def loop_stage(log: str) -> dict[str, object]:
    """``{"done": [...], "current": id or None}`` for the run the log describes.

    Reads only the ``[campaign]`` event lines, so what else the log prints cannot
    move it. A new ``started`` event (the next scenario of a series) starts again.
    """
    done: set[str] = set()
    current: str | None = None
    for line in log.splitlines():
        match = _EVENT.match(line.strip())
        if match is None:
            continue
        kind, rest = match.group(1), match.group(2) or ""
        if kind == "started":
            done, current = set(), None
        elif kind == "measuring" and "window=baseline" in rest:
            current = "baseline"
        elif kind == "baseline_done":
            done.add("baseline")
            current = None
        elif kind == "experiment_started":
            done = (done - _PER_EXPERIMENT) | {"baseline", "recall"}
            current = "diagnose"
        elif kind == "guard_refusal":
            done |= {"recall", "diagnose"}
            current = "guard"
        elif kind == "advisory":
            done |= {"recall", "diagnose", "guard"}
            current = None
        elif kind == "proposed":
            done |= {"recall", "diagnose", "guard"}
            current = "approve"
        elif kind == "approval":
            done.add("approve")
            current = "apply" if "approved=True" in rest else None
        elif kind == "applying":
            done.add("apply")
            current = "deploy"
        elif kind == "deployed":
            done.add("deploy")
            current = "verify"
        elif kind == "in_force":
            done.add("verify")
            current = "remeasure"
        elif kind == "measuring":
            # Without a deployer there is no deployed or in_force line; measuring
            # means everything before it has happened.
            done |= {"recall", "diagnose", "guard", "approve", "apply", "deploy", "verify"}
            current = "remeasure"
        elif kind == "verdict":
            done |= {"remeasure", "verdict"}
            current = None
        elif kind == "done":
            done, current = set(NODES), None
        elif kind in ("paused", "aborted"):
            current = None
    return {"done": [n for n in NODES if n in done], "current": current}
