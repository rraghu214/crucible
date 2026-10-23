"""Which Locust tasks drive which endpoint, read from the locustfile's source.

The SLA names an endpoint; the load generator must drive that endpoint and only
that one. Until 23 October 2026 the tag was chosen by
`"db" if endpoint.endswith("/db") else ()`, so an SLA on any other endpoint ran
every task at once and judged the blend against the SLA. On Box A such a blend's
p99 belonged to /api/downstream whatever the SLA was about.

Now the tag is the one on the task that requests the SLA's endpoint, and an
endpoint no task drives is refused: measuring something else is worse than not
measuring.

Parsed with `ast`, never imported: importing a locustfile starts Locust's event
machinery, and the load profile is a protected path that is only ever read.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

DEFAULT_LOCUSTFILE = "locust/locustfile.py"


class LoadProfileError(ValueError):
    pass


def locust_tasks(path: str | Path) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    tasks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        tag, weight, is_task = "", 1, False
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "tag" and deco.args:
                tag = str(getattr(deco.args[0], "value", ""))
            if getattr(deco, "id", "") == "task":
                is_task = True
            if isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "task":
                is_task = True
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    weight = int(deco.args[0].value)
        if not is_task:
            continue
        endpoint = ""
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "_get":
                named = next((k.value for k in call.keywords if k.arg == "name"), None)
                if isinstance(named, ast.Constant):
                    endpoint = str(named.value)
                elif call.args and isinstance(call.args[0], ast.Constant):
                    endpoint = str(call.args[0].value)
                break
        family = (ast.get_docstring(node) or "").split(".")[0].strip()
        tasks.append({"tag": tag, "endpoint": endpoint, "weight": weight, "family": family})
    return tasks


def _same_endpoint(a: str, b: str) -> bool:
    # A trailing slash is ignored (operator decision, 24 October 2026); nothing else is.
    return bool(a) and bool(b) and (a.rstrip("/") or "/") == (b.rstrip("/") or "/")


def tag_for_endpoint(endpoint: str, path: str | Path = DEFAULT_LOCUSTFILE) -> str:
    matches = sorted({t["tag"] for t in locust_tasks(path) if _same_endpoint(t["endpoint"], endpoint)})
    if not matches:
        driven = ", ".join(sorted({t["endpoint"] for t in locust_tasks(path) if t["endpoint"]})) or "none"
        raise LoadProfileError(f"no Locust task in {path} drives {endpoint!r}; it drives: {driven}")
    if len(matches) > 1 or not matches[0]:
        raise LoadProfileError(f"{endpoint!r} is driven by tasks tagged {matches}; exactly one tag is required")
    return matches[0]
