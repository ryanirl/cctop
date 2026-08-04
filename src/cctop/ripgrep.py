"""Locate and run a ripgrep-compatible line scanner for transcript search.

Search speed rests on ripgrep: a full scan of hundreds of megabytes of
transcripts finishes in tens of milliseconds, which is what makes live
search-as-you-type possible. Three backends, best first:

  1. a real `rg` on PATH;
  2. the Claude Code binary itself, which embeds ripgrep and behaves as it when
     invoked with argv[0] == "rg" (the same trick Claude Code's own `rg` shell
     function uses); it is validated with a --version probe before being
     trusted, since the behavior is undocumented;
  3. a pure-Python line scan, so search always works even with neither binary
     (slower: seconds rather than milliseconds on large histories).

Every backend yields the same RawMatch tuples, so callers never need to know
which one ran. Read-only throughout.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from shutil import which

_PROBE_TIMEOUT = 5
_SEARCH_TIMEOUT = 30

# Subagent transcripts live under <session-id>/subagents/agent-*.jsonl; they are
# internal tool traffic, not conversations, so they are excluded from search.
_EXCLUDE_GLOB = "!**/subagents/**"


@dataclass(frozen=True)
class Backend:
    """One way to run a ripgrep search: an argv prefix plus how to exec it.

    `executable` overrides the binary actually executed while argv[0] stays
    "rg", which is how the embedded ripgrep inside the Claude Code binary is
    activated.
    """

    name: str  # "rg", "claude-rg", or "python"
    argv: tuple[str, ...] = ()
    executable: str | None = None


@dataclass(frozen=True)
class RawMatch:
    """One matching line as reported by the scanner, before any parsing."""

    path: Path
    line_number: int
    line_text: str


def _runs_as_ripgrep(argv: list[str], executable: str | None) -> bool:
    """Whether this invocation really behaves as ripgrep (--version probe)."""
    try:
        result = subprocess.run(
            [*argv, "--version"],
            executable=executable,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.lstrip().startswith("ripgrep")


@lru_cache(maxsize=1)
def find_backend() -> Backend:
    """The best available search backend, probed once per process."""
    real = which("rg")
    if real is not None and _runs_as_ripgrep(["rg"], real):
        return Backend(name="rg", argv=("rg",), executable=real)

    from .authctl import find_claude_binary

    claude = find_claude_binary()
    if claude is not None and _runs_as_ripgrep(["rg"], claude):
        return Backend(name="claude-rg", argv=("rg",), executable=claude)

    return Backend(name="python")


def _json_escape_query(query: str) -> str:
    """Escape a literal query the way JSON escapes it inside a transcript line.

    Transcript lines are raw JSON, so a query containing `"` or `\\` appears
    escaped on disk; prefiltering with the escaped form keeps ripgrep from
    missing those lines. The decoded-text post-filter in the caller remains the
    source of truth.
    """
    return query.replace("\\", "\\\\").replace('"', '\\"')


def _run_ripgrep(
    backend: Backend,
    pattern: str,
    roots: list[Path],
    fixed_strings: bool,
    per_file_cap: int,
    max_total: int,
) -> tuple[list[RawMatch], bool]:
    argv = [
        *backend.argv,
        "--json",
        "--no-config",
        "--no-messages",
        "--ignore-case",
        "--glob",
        "*.jsonl",
        "--glob",
        _EXCLUDE_GLOB,
        "--max-count",
        str(per_file_cap),
    ]
    if fixed_strings:
        argv.append("--fixed-strings")
    argv += ["-e", pattern, *[str(root) for root in roots]]

    matches: list[RawMatch] = []
    truncated = False
    try:
        process = subprocess.Popen(
            argv,
            executable=backend.executable,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return [], False

    try:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_text = ((data.get("path") or {}).get("text")) or ""
            line_number = data.get("line_number")
            matched = ((data.get("lines") or {}).get("text")) or ""
            if not path_text or not isinstance(line_number, int):
                continue
            matches.append(RawMatch(Path(path_text), line_number, matched))
            if len(matches) >= max_total:
                truncated = True
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_SEARCH_TIMEOUT)

    return matches, truncated


def _iter_jsonl_files(roots: list[Path]) -> list[Path]:
    files = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if "subagents" in path.parts:
                continue
            files.append(path)
    return files


def _run_python_scan(
    pattern: str,
    roots: list[Path],
    fixed_strings: bool,
    per_file_cap: int,
    max_total: int,
) -> tuple[list[RawMatch], bool]:
    """Dependency-free fallback scan; same contract as the ripgrep run."""
    needle = pattern.casefold() if fixed_strings else None
    compiled = None if fixed_strings else re.compile(pattern, re.IGNORECASE)

    matches: list[RawMatch] = []
    for path in _iter_jsonl_files(roots):
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            in_file = 0
            for line_number, line in enumerate(handle, start=1):
                if needle is not None:
                    hit = needle in line.casefold()
                else:
                    assert compiled is not None
                    hit = compiled.search(line) is not None
                if not hit:
                    continue
                matches.append(RawMatch(path, line_number, line))
                in_file += 1
                if len(matches) >= max_total:
                    return matches, True
                if in_file >= per_file_cap:
                    break
    return matches, False


def search_lines(
    query: str,
    roots: list[Path],
    regex: bool = False,
    per_file_cap: int = 20,
    max_total: int = 500,
    backend: Backend | None = None,
) -> tuple[list[RawMatch], bool, str]:
    """Every transcript line matching the query, via the best backend.

    Returns (matches, truncated, backend_name). Literal queries are also
    JSON-escaped for the on-disk prefilter so quotes and backslashes still
    match. A regex query is passed through as-is (ripgrep and Python regex
    syntax agree on the common forms used interactively).
    """
    backend = backend or find_backend()
    pattern = query if regex else _json_escape_query(query)

    if backend.name == "python":
        matches, truncated = _run_python_scan(pattern, roots, not regex, per_file_cap, max_total)
    else:
        matches, truncated = _run_ripgrep(
            backend,
            pattern,
            roots,
            not regex,
            per_file_cap,
            max_total,
        )
    return matches, truncated, backend.name
