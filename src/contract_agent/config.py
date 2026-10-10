"""Runtime configuration: environment variables + an optional ``.env`` file.

All provider/API configuration comes from environment variables — nothing
is hardcoded beyond the defaults documented in ``.env.example`` and the
README's configuration table.

At startup the entry points (API, CLI, demo) call :func:`load_dotenv`,
which reads a ``.env`` file when one exists — first in the current working
directory, then at the repository root. The parser is deliberately small
and built in (no third-party dependency): ``KEY=VALUE`` lines, ``#``
comments, optional surrounding quotes, optional ``export`` prefix.

Precedence rule: a variable already set in the real process environment
always wins. ``.env`` only fills in variables that are not set, so CI and
production environments override the file.
"""

from __future__ import annotations

import os
from pathlib import Path


def _candidate_paths() -> list[Path]:
    return [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",  # repository root
    ]


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not key:
        return None
    return key, value


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load ``.env`` values into ``os.environ`` without overriding it.

    Returns the path that was loaded, or ``None`` when no file exists.
    Safe to call more than once: keys already present in the environment
    (from the process or an earlier load) are never overwritten.
    """
    candidates = [path] if path is not None else _candidate_paths()
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def silence_langchain_deprecation_warnings() -> None:
    """Entry-point hygiene: hide langgraph's `allowed_objects` warning.

    LangGraph currently emits a LangChainPendingDeprecationWarning about
    a default that will change in a future version. A CLI/demo user can
    do nothing about it, so the entry points (CLI, demo, API) filter it
    by message. Two quirks make this less trivial than it looks: the
    warning fires while langgraph imports (so entry points call this
    before importing the graph), and langchain_core re-registers its own
    "default" filters at import time, jumping ahead of any filter set
    earlier — so langchain_core is imported first here and the ignore
    is registered after it. Library code never touches global warning
    state; this runs only where a human is looking at the output.
    """
    import warnings

    try:
        import langchain_core  # noqa: F401 — surfaces its filters first
    except ImportError:
        pass
    warnings.filterwarnings("ignore", message=r".*allowed_objects.*")


def env_str(name: str, default: str | None = None) -> str | None:
    """Read a string variable, treating an empty value as unset."""
    value = os.environ.get(name)
    return value if value else default


def env_float(name: str, default: float) -> float:
    """Read a float variable, falling back to ``default`` when unset/invalid."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    """Read an int variable, falling back to ``default`` when unset/invalid."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
