"""Shared helpers: path resolution, file walking, output clipping, subprocesses."""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

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

    category = "input"  # environment | input | policy | source | unknown


class OutcomeError(ToolError):
    """A tool failure that states its outcome. The message starts with a stable, parseable
    line: `[outcome=<outcome> reason=<code> category=<category>]`."""

    def __init__(self, outcome: str, message: str, *, category: str | None = None, reason: str | None = None):
        self.outcome, self.reason, self.detail = outcome, reason, message
        if category:
            self.category = category
        head = f"[outcome={outcome}" + (f" reason={reason}" if reason else "") + (f" category={category}" if category else "") + "]"
        super().__init__(head + (" " if "\n" not in message else "\n") + message)


class Blocked(OutcomeError):
    """The request was stopped by a scope, policy or prerequisite check before its target was
    read, written or run."""

    def __init__(self, reason: str, message: str, category: str = "policy"):
        super().__init__("blocked", message, category=category, reason=reason)


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
        if not any(within(real, r) for r in roots):
            raise Blocked(
                "permission_denied",
                f"{p} is outside the allowed roots ({', '.join(map(str, roots))}). "
                "Adjust IDE_MCP_ALLOWED_ROOTS for this MCP server to permit it.",
            )
    return p


def path_parts(path: str | os.PathLike[str], flavour: Any = os.path) -> tuple[str, ...]:
    """A path as comparable components: drive, root marker, then each name, with separators,
    `..`, case (on Windows) and the `\\\\?\\` extended-length prefix normalised away. `flavour` is
    os.path, or ntpath/posixpath to apply one platform's rules on another."""
    s = os.fspath(path)
    if flavour.sep == "\\":
        s = s.replace("/", "\\")
        if s.upper().startswith("\\\\?\\UNC\\"):
            s = "\\\\" + s[8:]
        elif s.startswith("\\\\?\\"):
            s = s[4:]
    s = flavour.normcase(flavour.normpath(s))
    drive, tail = flavour.splitdrive(s)
    return (drive, flavour.sep if tail.startswith(flavour.sep) else "", *(part for part in tail.split(flavour.sep) if part))


def within(path: str | os.PathLike[str], root: str | os.PathLike[str], flavour: Any = os.path) -> bool:
    """Whether `path` is `root` or lies below it, compared component by component. Never a
    string-prefix test: `C:\\dev\\app-other` is not inside `C:\\dev\\app`. Callers pass resolved paths
    when links matter; see real()."""
    parts, base = path_parts(path, flavour), path_parts(root, flavour)
    return parts[: len(base)] == base


def real(path: str | os.PathLike[str]) -> Path:
    """The path with symbolic links, junctions, 8.3 names and case aliases resolved. A name that
    does not exist yet is resolved through its nearest existing parent."""
    return Path(os.path.realpath(path))


# IO_REPARSE_TAG_SYMLINK and IO_REPARSE_TAG_MOUNT_POINT (a junction). The stat module only names
# them on Windows. Other reparse points, such as cloud-storage placeholders, are ordinary entries.
_LINK_REPARSE_TAGS = (0xA000000C, 0xA0000003)


def is_link(path: str | os.PathLike[str]) -> bool:
    """A symbolic link or, on Windows, a directory junction or other name-surrogate mount."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


@dataclass(frozen=True)
class Identity:
    """Which directory a path really is: its resolved location plus the volume and file ids, so
    a directory swapped for another one at the same path is noticed."""

    path: str
    dev: int
    ino: int


def identity(path: str | os.PathLike[str]) -> Identity:
    resolved = os.path.realpath(path)
    st = os.stat(resolved)
    return Identity(path_key(resolved), st.st_dev, st.st_ino)


def worktree_root(path: Path) -> Path | None:
    """The Git checkout a resolved path belongs to: the nearest directory at or above it that
    holds a `.git` entry (a directory in the main checkout, a file in a linked worktree or a
    submodule). Only the entry's existence is used; nothing in it is read. Two worktrees of one
    repository share a branch history and a metadata directory but never this root."""
    for d in (path, *path.parents):
        if os.path.lexists(d / ".git"):
            return d
    return None


# Files whose contents these servers never read, search, parse or return.
PROTECTED_SUFFIXES = (".properties",)


def protected_name(name: str) -> bool:
    """Whether a file name denotes a protected file, including the spellings Windows treats as
    the same file: other case, trailing dots or spaces, and an alternate-data-stream suffix."""
    base = name.split(":", 1)[0].rstrip(" .").casefold()
    return base.endswith(PROTECTED_SUFFIXES)


# The operation a request is running, set by guard.Operations around each handler. A
# ContextVar, so concurrent requests each see their own and worker threads inherit it.
_ACTIVE: contextvars.ContextVar[Any] = contextvars.ContextVar("ide_mcp_operation", default=None)


def active(need: str | None = None) -> Any:
    """The guard.Scope of the running request. Shared helpers call this before touching a file
    or starting a process, so code that is not inside a registered operation cannot use them,
    and an operation cannot use a mechanism whose prerequisite it did not declare."""
    op = _ACTIVE.get()
    if op is None:
        raise Blocked(
            "unguarded_access",
            "This helper was called outside a registered operation, so there is no request scope to check against. "
            "Register the handler with ops.tool(kind=..., needs=...).",
        )
    if need and need not in op.needs:
        raise Blocked(
            "undeclared_prerequisite",
            f"Operation '{op.name}' did not declare the '{need}' prerequisite it is trying to use.",
        )
    return op.scope


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


# ----------------------------------------------------------------------------- walking


def _git_files(root: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-ignored files under root, or None if root isn't in a repo."""
    root = active().launch_dir(root)
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
    """Files below root. Does not descend through links or junctions, nor into another
    checkout nested below root: both lead to a different target than the one selected."""
    root = active().dir(root)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in skip and not is_link(here / d) and not os.path.lexists(here / d / ".git")
        )
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

    The working directory is required and is verified against the request scope here, at the
    point of launch, whatever the caller checked earlier.
    """
    if cwd is None:
        raise Blocked("unguarded_access", f"{argv[0]} was about to be started without a verified working directory.")
    cwd = active("execution").launch_dir(cwd)
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
        raise Blocked("execution_unavailable", f"Executable not found: {argv[0]}", "environment") from None
    except OSError as e:
        raise Blocked("execution_unavailable", f"Could not start {argv[0]}: {e}", "environment") from None

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
