"""Shared helpers: path resolution, file walking, output clipping, subprocesses."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

WINDOWS = sys.platform == "win32"
MAX_OUTPUT = 30_000
MAX_FILES = 200_000

# Directories never worth descending into when git can't tell us what is ignored.
SKIP_DIRS = frozenset(
    {
        ".git", ".gradle", ".idea", ".kotlin", ".cxx", ".venv", "__pycache__", "node_modules",
        "build", "out", "target", ".dart_tool", "Pods", ".pytest_cache",
    }
)


class ToolError(Exception):
    """A failure whose message is meant to be read by the calling model."""


def clip(text: str, limit: int = MAX_OUTPUT, keep: Literal["both", "tail", "head"] = "both") -> str:
    """Bound output size so one noisy command can't flood the model's context."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    if keep == "tail":
        return f"[... {omitted} earlier characters omitted ...]\n" + text[-limit:]
    if keep == "head":
        return text[:limit] + f"\n[... {omitted} more characters omitted ...]"
    half = limit // 2
    return text[:half] + f"\n[... {omitted} characters omitted ...]\n" + text[-half:]


# ----------------------------------------------------------------------------- paths


def allowed_roots() -> list[Path]:
    """Optional allowlist from IDE_MCP_ALLOWED_ROOTS (os.pathsep-separated). Empty = anywhere."""
    raw = os.environ.get("IDE_MCP_ALLOWED_ROOTS", "")
    return [Path(os.path.realpath(os.path.expanduser(p))) for p in raw.split(os.pathsep) if p.strip()]


def resolve(path: str | os.PathLike[str] | None, base: Path | None = None) -> Path:
    """Turn user input into an absolute path. Relative paths resolve against `base`, or the
    directory the server was started in."""
    if path is None or str(path).strip() == "":
        p = base or Path.cwd()
    else:
        p = Path(os.path.expanduser(str(path)))
        if not p.is_absolute():
            p = (base or Path.cwd()) / p
    p = Path(os.path.normpath(p))
    roots = allowed_roots()
    if roots:
        real = Path(os.path.realpath(p))
        if not any(real == r or r in real.parents for r in roots):
            raise ToolError(
                f"{p} is outside the allowed roots ({', '.join(map(str, roots))}). "
                "Adjust IDE_MCP_ALLOWED_ROOTS for this MCP server to permit it."
            )
    return p


def existing_dir(path: str | None, what: str = "directory") -> Path:
    p = resolve(path)
    if not p.exists():
        raise ToolError(f"{what} does not exist: {p}")
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    return p


def rel(path: Path, root: Path) -> str:
    """Path relative to root when inside it, else absolute. Always forward slashes."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def path_key(path: str | os.PathLike[str]) -> str:
    """Form of a path for equality checks: separators and (on Windows) case normalised, so a
    path the compiler printed compares equal to the one the caller passed."""
    return os.path.normcase(os.path.normpath(str(path)))


def android_sdk() -> str | None:
    """Android SDK location from the environment or the usual install directories."""
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            return os.environ[var]
    home = Path.home()
    candidates = [home / "Android" / "Sdk", home / "android-sdk", home / "Library" / "Android" / "sdk"]
    if os.environ.get("LOCALAPPDATA"):
        candidates.insert(0, Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    return next((str(c) for c in candidates if c.is_dir()), None)


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


# ----------------------------------------------------------------------------- walking


def _git_files(root: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-ignored files under root, or None if root isn't in a repo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    seen: set[str] = set()
    out: list[Path] = []
    for name in r.stdout.decode("utf-8", "replace").split("\0"):
        # Entries ending in "/" are nested repositories; tracked-but-deleted files are filtered
        # by callers when they fail to open.
        if not name or name.endswith("/") or name in seen:
            continue
        seen.add(name)
        out.append(root / name)
    return out


def _walk_files(root: Path, skip: frozenset[str] = SKIP_DIRS) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            out.append(Path(dirpath) / name)
            if len(out) >= MAX_FILES:
                return out
    return out


def iter_files(root: Path, include_ignored: bool = False) -> list[Path]:
    """Files under root. Honours .gitignore inside a git repository; elsewhere (or with
    include_ignored) walks the tree, skipping build/cache directories unless include_ignored."""
    if include_ignored:
        return _walk_files(root, frozenset({".git"}))
    files = _git_files(root)
    if files is None:
        return _walk_files(root)
    return files[:MAX_FILES]


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob with **, *, ?, [..] and {a,b} into a regex over forward-slash paths."""
    i, n, out, depth = 0, len(pattern), [], 0
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 2)
            if j == -1:
                out.append(r"\[")
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "{":
            out.append("(?:")
            depth += 1
        elif c == "}" and depth:
            out.append(")")
            depth -= 1
        elif c == "," and depth:
            out.append("|")
        else:
            out.append(re.escape(c))
        i += 1
    out.append(")" * depth)
    return re.compile("".join(out) + r"\Z")


def glob_matcher(pattern: str | None) -> Callable[[str], bool]:
    """Matcher over root-relative posix paths. Patterns without '/' match the file name alone,
    so '*.kt' finds Kotlin files at any depth."""
    if not pattern:
        return lambda _p: True
    rx = glob_to_regex(pattern.lstrip("/"))
    if "/" not in pattern:
        return lambda p: rx.match(p.rsplit("/", 1)[-1]) is not None
    return lambda p: rx.match(p) is not None


# ----------------------------------------------------------------------------- processes


@dataclass
class ProcResult:
    code: int
    output: str
    timed_out: bool = False


async def run_proc(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    max_bytes: int = 20_000_000,
) -> ProcResult:
    """Run a process with stderr merged into stdout, streaming lines to on_line.

    stdin is /dev/null (the server's own stdin is the MCP transport). The child gets its own
    process group so a timeout or a cancelled tool call takes its whole tree down with it.
    """
    common = dict(
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=4 * 1024 * 1024,
    )
    try:
        if not WINDOWS:
            proc = await asyncio.create_subprocess_exec(*argv, start_new_session=True, **common)
        elif argv[0].lower().endswith((".bat", ".cmd")):
            # Batch files (gradlew.bat) only run through cmd.exe. The shell variant wraps the whole
            # line in one pair of quotes, which is the form cmd parses reliably.
            proc = await asyncio.create_subprocess_shell(
                _cmd_line(argv), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **common
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **common
            )
    except FileNotFoundError:
        raise ToolError(f"Executable not found: {argv[0]}") from None
    except OSError as e:
        raise ToolError(f"Could not start {argv[0]}: {e}") from None

    chunks: list[str] = []
    size = 0

    async def pump() -> None:
        nonlocal size
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:  # a single line longer than the stream limit
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").replace("\r\n", "\n")
            if size < max_bytes:
                chunks.append(line)
                size += len(line)
            if on_line:
                on_line(line.rstrip("\n"))
        await proc.wait()

    timed_out = False
    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _terminate(proc)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate(proc))
        raise
    code = proc.returncode if proc.returncode is not None else -1
    return ProcResult(code=code, output="".join(chunks), timed_out=timed_out)


def _cmd_line(argv: list[str]) -> str:
    """Join arguments for cmd.exe, quoting those with spaces or shell metacharacters."""
    parts = []
    for a in argv:
        if a and not re.search(r'[\s"&|<>^()%!]', a):
            parts.append(a)
        else:
            parts.append('"' + a.replace('"', '\\"') + '"')
    return " ".join(parts)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Stop the process and everything it started. POSIX: SIGTERM the process group, then
    SIGKILL if it hasn't gone within a few seconds. Windows: taskkill the process tree."""
    if WINDOWS:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        await proc.wait()
