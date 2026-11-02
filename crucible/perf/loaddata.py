"""Input data for templated requests: CSV or JSON files the load generator reads.

Uploaded by the operator from the wizard (control token) into
``<state>/load-data/``. A scenario's ``data`` maps each ``${var}`` in a request
template to a file here and a column in it; rows are cycled in order.

Only plain file names are accepted, and only this directory is read, so a
scenario can never point the load generator at an arbitrary file.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

LOAD_DATA_DIR = "load-data"
MAX_BYTES = 5 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.(csv|json)$")


class LoadDataError(ValueError):
    pass


def data_dir(state_dir: Path) -> Path:
    return Path(state_dir) / LOAD_DATA_DIR


def _rows(name: str, text: str) -> list[dict[str, Any]]:
    if name.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise LoadDataError(f"{name}: a CSV needs a header row naming its columns")
        return list(reader)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise LoadDataError(f"{name}: not valid JSON ({exc})") from exc
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        raise LoadDataError(f"{name}: a JSON data file must be a list of objects")
    return value


def describe(name: str, text: str) -> dict[str, Any]:
    if not _NAME.match(name):
        raise LoadDataError("data file name must be a plain name ending .csv or .json")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise LoadDataError(f"{name}: larger than {MAX_BYTES // (1024 * 1024)} MB")
    rows = _rows(name, text)
    if not rows:
        raise LoadDataError(f"{name}: no data rows")
    columns = sorted({key for row in rows for key in row})
    return {"name": name, "rows": len(rows), "columns": columns}


def save(state_dir: Path, name: str, text: str) -> dict[str, Any]:
    info = describe(name, text)
    folder = data_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f".{name}.partial"
    partial.write_text(text, encoding="utf-8", newline="")
    partial.replace(folder / name)
    return info


def listing(state_dir: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(data_dir(state_dir).glob("*")):
        if path.name.startswith(".") or not _NAME.match(path.name):
            continue
        try:
            out.append(describe(path.name, path.read_text(encoding="utf-8")))
        except (OSError, LoadDataError) as exc:
            out.append({"name": path.name, "error": str(exc)})
    return out


def resolve(state_dir: Path, data: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Check every mapped file exists and has its column; return absolute paths."""
    resolved = {}
    for var, spec in data.items():
        path = data_dir(state_dir) / spec["file"]
        try:
            info = describe(spec["file"], path.read_text(encoding="utf-8"))
        except OSError:
            raise LoadDataError(f"${{{var}}} maps to {spec['file']!r}, which has not been uploaded") from None
        if spec["column"] not in info["columns"]:
            raise LoadDataError(f"${{{var}}} maps to column {spec['column']!r}, which {spec['file']} does not "
                                f"have; it has {info['columns']}")
        resolved[var] = {"file": str(path.resolve()), "column": spec["column"]}
    return resolved
