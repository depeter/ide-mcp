"""files MCP server: read, edit, find and search files anywhere on disk, no IDE or open
project required."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import guard
from .common import (
    WINDOWS,
    Blocked,
    ToolError,
    active,
    allowed_roots,
    clip,
    glob_matcher,
    is_link,
    iter_files,
    real,
    rel,
    run_proc,
    within,
    worktree_root,
)
from .guard import EXECUTION, TARGET, ExpectedRoot, Readiness

mcp = FastMCP(
    "files",
    instructions=(
        "File tools that work on any path on this machine; nothing needs to be opened in an IDE "
        "first.\n"
        "Every tool requires `expected_root`: the absolute path of the Git worktree (or a directory "
        "inside it) or the standalone document folder the request is for. Each path a tool uses must "
        "resolve inside it and belong to the same worktree, or the call is rejected before anything "
        "is read, written or run; a folder that merely contains several checkouts does not select "
        "one. It restricts the request and grants nothing. Pass absolute paths (or ~/...); on "
        "Windows both C:\\dir\\file and C:/dir/file work. Relative paths and omitted directories "
        "resolve against expected_root, never the server's start directory. *.properties files are "
        "never read, searched or edited.\n"
        "Rejections and failures are tool errors that start with [outcome=... reason=... "
        "category=...]. `readiness` reports, without running anything, whether commands can be "
        "started for a scope.\n"
        "Search and listing tools take a `root` directory and honour .gitignore inside git "
        "repositories; elsewhere they skip build and cache directories. Pass include_ignored=true "
        "to look inside those too.\n"
        "To edit: read_file, then replace_text with enough surrounding text to match exactly once. "
        "Use create_file for new files or whole-file rewrites (overwrite=true)."
    ),
)
ops = guard.Operations(mcp)

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_LINE = 300

FilePath = Annotated[str, Field(description="Absolute path (or ~/...) of the file, inside expected_root.")]
Root = Annotated[
    str | None,
    Field(description="Directory to look in, absolute or ~/..., inside expected_root. Defaults to expected_root."),
]
IncludeIgnored = Annotated[
    bool,
    Field(description="Also look in git-ignored files and build/cache directories."),
]


def _existing_file(path: str) -> Path:
    return active().file(path)


def _decode(p: Path) -> str:
    data = active().read_bytes(p)
    if b"\0" in data[:8192]:
        raise ToolError(f"{p} looks like a binary file.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{p} is not valid UTF-8 text.") from None


def _write_atomic(p: Path, text: str) -> None:
    """Write via a temp file in the same directory so a crash can't leave a half-written file.
    The text is written as given: BOM and line endings are the caller's."""
    active().write_bytes(p, text.encode("utf-8"))


def _numbered(lines: list[str], first: int) -> str:
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{first + i:>{width}}\t{line}" for i, line in enumerate(lines))


def _short(line: str) -> str:
    line = line.rstrip("\r\n")
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + " [...]"


# ----------------------------------------------------------------------------- reading


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def read_file(
    path: FilePath,
    start_line: Annotated[int, Field(description="1-based line to start from.", ge=1)] = 1,
    max_lines: Annotated[int, Field(description="Maximum number of lines to return.", ge=1, le=10_000)] = 2000,
    line_numbers: Annotated[bool, Field(description="Prefix each line with its line number.")] = True,
    expected_root: ExpectedRoot = None,
) -> str:
    """Read a text file. Returns the requested line range with line numbers and says how many
    lines the file has, so you can page through large files with start_line."""
    p = _existing_file(path)
    lines = (await asyncio.to_thread(_decode, p)).splitlines()
    total = len(lines)
    if total == 0:
        return f"{p} is empty."
    if start_line > total:
        raise ToolError(f"start_line {start_line} is past the end of {p} ({total} lines).")
    chunk = lines[start_line - 1 : start_line - 1 + max_lines]
    end = start_line + len(chunk) - 1
    body = _numbered(chunk, start_line) if line_numbers else "\n".join(chunk)
    header = f"{p} (lines {start_line}-{end} of {total})"
    if end < total:
        header += f"; continue with start_line={end + 1}"
    return header + "\n" + clip(body, 120_000, keep="head")


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def list_directory_tree(
    path: Root = None,
    max_depth: Annotated[int, Field(description="How many directory levels to expand.", ge=1, le=10)] = 3,
    include_ignored: IncludeIgnored = False,
    expected_root: ExpectedRoot = None,
) -> str:
    """Show a directory as an indented tree. Directories deeper than max_depth are collapsed to
    a file count. Git-ignored files and build output are left out unless include_ignored."""
    root = active().dir(path)

    def build() -> str:
        tree: dict = {}
        files = iter_files(root, include_ignored)
        for f in files:
            parts = Path(rel(f, root)).parts
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault(parts[-1], None)

        def count(node: dict) -> int:
            return sum(1 if v is None else count(v) for v in node.values())

        lines: list[str] = []
        limit = 500

        def walk(node: dict, depth: int) -> None:
            dirs = sorted(k for k, v in node.items() if v is not None)
            leaves = sorted(k for k, v in node.items() if v is None)
            for name in dirs:
                if len(lines) >= limit:
                    return
                child = node[name]
                if depth >= max_depth:
                    lines.append(f"{'  ' * depth}{name}/  ({count(child)} files)")
                else:
                    lines.append(f"{'  ' * depth}{name}/")
                    walk(child, depth + 1)
            for name in leaves:
                if len(lines) >= limit:
                    return
                lines.append(f"{'  ' * depth}{name}")

        walk(tree, 0)
        out = f"{root}/  ({len(files)} files)\n" + "\n".join(lines)
        if len(lines) >= limit:
            out += f"\n[... stopped at {limit} entries; lower max_depth or list a subdirectory ...]"
        return out

    return await asyncio.to_thread(build)


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def find_files(
    root: Root = None,
    glob: Annotated[
        str | None,
        Field(
            description="Glob over the path relative to root, e.g. '**/*Test.kt', 'app/src/**/*.xml', "
            "'*.{kt,java}'. A pattern without '/' matches file names at any depth."
        ),
    ] = None,
    name_contains: Annotated[
        str | None, Field(description="Case-insensitive substring the file name must contain.")
    ] = None,
    include_ignored: IncludeIgnored = False,
    limit: Annotated[int, Field(description="Maximum number of paths to return.", ge=1, le=5000)] = 200,
    expected_root: ExpectedRoot = None,
) -> str:
    """Find files by glob and/or by a substring of the file name. Returns paths relative to
    root."""
    base = active().dir(root)
    match = glob_matcher(glob)
    needle = name_contains.lower() if name_contains else None

    def scan() -> list[str]:
        hits = []
        for f in iter_files(base, include_ignored):
            r = rel(f, base)
            if needle and needle not in f.name.lower():
                continue
            if match(r):
                hits.append(r)
        return sorted(hits)

    hits = await asyncio.to_thread(scan)
    if not hits:
        return f"No files matched under {base}."
    out = f"{len(hits)} file(s) under {base}:\n" + "\n".join(hits[:limit])
    if len(hits) > limit:
        out += f"\n[... {len(hits) - limit} more; narrow the pattern or raise limit ...]"
    return out


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def search_in_files(
    query: Annotated[str, Field(description="Text to look for, or a Python regular expression when regex=true.")],
    root: Root = None,
    regex: Annotated[bool, Field(description="Treat query as a regular expression (matched per line).")] = False,
    case_sensitive: bool = True,
    file_glob: Annotated[
        str | None, Field(description="Only search files matching this glob, e.g. '*.kt' or 'app/**/*.xml'.")
    ] = None,
    context_lines: Annotated[int, Field(description="Lines of context around each match.", ge=0, le=10)] = 0,
    include_ignored: IncludeIgnored = False,
    limit: Annotated[int, Field(description="Maximum number of matching lines to return.", ge=1, le=2000)] = 100,
    expected_root: ExpectedRoot = None,
) -> str:
    """Search file contents under root for text or a regex. Results are grouped by file with
    line numbers. Binary files and files over 2 MB are skipped; protected files (*.properties)
    and files that resolve outside expected_root are never opened."""
    scope = active()
    base = scope.dir(root)
    try:
        rx = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}") from None
    match = glob_matcher(file_glob)

    def scan() -> tuple[list[str], int, int, int, dict[str, int]]:
        out: list[str] = []
        shown = total = searched = 0
        refused: dict[str, int] = {}
        for f in sorted(iter_files(base, include_ignored)):
            r = rel(f, base)
            if not match(r):
                continue
            try:
                data = scope.read_bytes(f, MAX_SEARCH_FILE_BYTES)
                if b"\0" in data[:8192]:
                    continue
                lines = data.decode("utf-8", "replace").splitlines()
            except Blocked as e:
                refused[e.reason or "blocked"] = refused.get(e.reason or "blocked", 0) + 1
                continue
            except (OSError, ToolError):
                continue
            searched += 1
            hit_idx = [i for i, line in enumerate(lines) if rx.search(line)]
            if not hit_idx:
                continue
            total += len(hit_idx)
            if shown >= limit:
                continue
            hit_idx = hit_idx[: limit - shown]
            shown += len(hit_idx)
            out.append(r)
            hits = set(hit_idx)
            wanted = sorted({j for i in hit_idx for j in range(i - context_lines, i + context_lines + 1) if 0 <= j < len(lines)})
            prev = None
            for j in wanted:
                if prev is not None and j > prev + 1:
                    out.append("  ...")
                out.append(f"  {j + 1}{':' if j in hits else '-'} {_short(lines[j])}")
                prev = j
        return out, shown, total, searched, refused

    out, shown, total, searched, refused = await asyncio.to_thread(scan)
    note = ""
    if n := refused.pop("protected_path", 0):
        note += f"\n[{n} protected file(s) were not opened]"
    if n := sum(refused.values()):
        note += f"\n[{n} file(s) resolve outside expected_root or changed while being opened, and were not read]"
    if not total:
        return f"No matches in {searched} file(s) under {base}." + note
    header = f"{total} matching line(s) in {searched} searched file(s) under {base}"
    if total > shown:
        header += f"; showing the first {shown} (narrow with file_glob or raise limit)"
    return clip(header + ":\n" + "\n".join(out), 60_000, keep="head") + note


# ----------------------------------------------------------------------------- writing


@ops.tool(kind="file", needs=(TARGET,), annotations=DESTRUCTIVE)
async def create_file(
    path: FilePath,
    text: Annotated[str, Field(description="Full contents of the file.")],
    overwrite: Annotated[bool, Field(description="Replace the file if it already exists.")] = False,
    expected_root: ExpectedRoot = None,
) -> str:
    """Create a file with the given contents, creating parent directories as needed. Fails if
    the file exists unless overwrite=true."""
    p = active().check(path)
    if p.is_dir():
        raise ToolError(f"{p} is a directory.")
    existed = p.exists()
    if existed and not overwrite:
        raise ToolError(f"{p} already exists. Pass overwrite=true to replace it, or use replace_text to edit it.")
    await asyncio.to_thread(_write_atomic, p, text)
    return f"{'Overwrote' if existed else 'Created'} {p} ({len(text.splitlines())} lines)."


@ops.tool(kind="file", needs=(TARGET,), annotations=DESTRUCTIVE)
async def replace_text(
    path: FilePath,
    old_text: Annotated[str, Field(description="Exact text to replace (or a regex when regex=true). Must not be empty.")],
    new_text: Annotated[str, Field(description="Replacement. With regex=true, \\1 and \\g<name> refer to groups.")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring exactly one.")] = False,
    regex: Annotated[bool, Field(description="Treat old_text as a Python regular expression (multi-line, ^/$ per line).")] = False,
    case_sensitive: bool = True,
    expected_root: ExpectedRoot = None,
) -> str:
    """Replace text in a file. By default old_text must occur exactly once, so include enough
    surrounding lines to make it unique; the error lists the lines of every match otherwise.
    Whitespace and indentation must match the file exactly."""
    p = _existing_file(path)
    if not old_text:
        raise ToolError("old_text must not be empty. Use create_file to write a whole file.")
    content = await asyncio.to_thread(_decode, p)
    # Callers almost always send \n; keep a CRLF file CRLF.
    if "\r\n" in content and "\r\n" not in old_text:
        old_text = old_text.replace("\n", "\r\n") if not regex else old_text
        new_text = new_text.replace("\n", "\r\n")

    flags = (0 if case_sensitive else re.IGNORECASE) | re.MULTILINE
    try:
        rx = re.compile(old_text if regex else re.escape(old_text), flags)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}") from None
    matches = list(rx.finditer(content))
    if not matches:
        hint = ""
        if not regex and re.sub(r"\s+", "", old_text) in re.sub(r"\s+", "", content):
            hint = " The text is present with different whitespace or indentation; copy it exactly from read_file."
        raise ToolError(f"old_text was not found in {p}.{hint}")
    line_of = lambda pos: content.count("\n", 0, pos) + 1  # noqa: E731
    if len(matches) > 1 and not replace_all:
        where = ", ".join(str(line_of(m.start())) for m in matches[:20])
        raise ToolError(
            f"old_text matches {len(matches)} places in {p} (lines {where}). Add surrounding "
            "context to make it unique, or pass replace_all=true."
        )
    try:
        updated = rx.sub(new_text if regex else (lambda _m: new_text), content)
    except (re.error, IndexError) as e:
        raise ToolError(f"Invalid replacement: {e}") from None
    if updated == content:
        return f"No change: the replacement is identical to the existing text in {p}."
    await asyncio.to_thread(_write_atomic, p, updated)

    first = line_of(matches[0].start())
    new_lines = updated.splitlines()
    span = len((matches[0].expand(new_text) if regex else new_text).splitlines()) or 1
    lo = max(1, first - 2)
    hi = min(len(new_lines), first + span + 1)
    snippet = _numbered([_short(line) for line in new_lines[lo - 1 : hi]], lo) if new_lines else "(file is now empty)"
    return f"Replaced {len(matches)} occurrence(s) in {p}. Around line {first} it now reads:\n{clip(snippet, 4000, keep='head')}"


@ops.tool(kind="file", needs=(TARGET,), annotations=DESTRUCTIVE)
async def move_path(
    source: Annotated[str, Field(description="Absolute path of the file or directory to move or rename.")],
    destination: Annotated[str, Field(description="Absolute path it should have afterwards.")],
    overwrite: Annotated[bool, Field(description="Replace an existing file at the destination.")] = False,
    expected_root: ExpectedRoot = None,
) -> str:
    """Move or rename a file or directory, creating the destination's parent directories. Both
    ends must be inside expected_root."""
    scope = active()
    src, dst = scope.entry(source), scope.entry(destination)
    if not os.path.lexists(src):
        raise ToolError(f"Source does not exist: {src}")
    if dst.is_dir() and not src.is_dir():
        dst = scope.entry(dst / src.name)
    if dst.exists():
        if not overwrite or dst.is_dir():
            raise ToolError(f"Destination already exists: {dst}" + ("" if dst.is_dir() else " (pass overwrite=true to replace it)"))
    scope.make_dirs(dst.parent)
    if scope.entry(src) != src or scope.entry(dst) != dst:  # both ends again, immediately before the move
        raise Blocked("target_changed", f"{src} or {dst} changed between validation and the move. Nothing was moved.")
    await asyncio.to_thread(shutil.move, str(src), str(dst))
    return f"Moved {src} -> {dst}"


@ops.tool(kind="file", needs=(TARGET,), annotations=DESTRUCTIVE)
async def delete_path(
    path: Annotated[str, Field(description="Absolute path of the file or directory to delete.")],
    recursive: Annotated[bool, Field(description="Required to delete a directory that is not empty.")] = False,
    expected_root: ExpectedRoot = None,
) -> str:
    """Delete a file or directory. This is permanent; nothing goes to a trash folder. A link or
    junction is removed as a link; what it points at is left alone."""
    scope = active()
    p = scope.entry(path)
    if not os.path.lexists(p):
        raise ToolError(f"Does not exist: {p}")
    if p == scope.root:
        raise ToolError(f"Refusing to delete {p}: it is the expected_root of this request.")
    if len(p.parts) < 3 or within(Path.home(), real(p)):
        raise ToolError(f"Refusing to delete {p}: it is too close to the filesystem root or is your home directory.")
    if is_link(p):
        try:
            p.unlink()
        except OSError:
            p.rmdir()  # a Windows junction
        return f"Deleted link {p}"
    if p.is_dir():
        if any(p.iterdir()):
            if not recursive:
                raise ToolError(f"{p} is a non-empty directory. Pass recursive=true to delete it and everything in it.")
            await asyncio.to_thread(shutil.rmtree, p)
        else:
            p.rmdir()
        return f"Deleted directory {p}"
    p.unlink()
    return f"Deleted {p}"


# ----------------------------------------------------------------------------- git and shell


async def _git(repo: Path, *args: str) -> str:
    r = await run_proc(["git", *args], cwd=repo, timeout=30)
    return r.output.strip() if r.code == 0 else ""


@ops.tool(kind="file", needs=(TARGET, EXECUTION), annotations=READ)
async def git_status(
    path: Annotated[
        str | None,
        Field(description="A directory inside a repository, or a directory containing repositories. Defaults to expected_root."),
    ] = None,
    expected_root: ExpectedRoot = None,
) -> str:
    """Show the git repositories at or below a path: root, branch, ahead/behind, last commit,
    and changed files. With a worktree as expected_root this is that worktree. With a plain
    folder of checkouts it lists each one's status, which is how to find the worktree to pass
    as expected_root next; it reads no file contents."""
    scope = active()
    base = scope.dir(path)
    if scope.worktree is not None:
        repos = [scope.worktree]
    else:
        def scan() -> list[Path]:
            found = []
            for dirpath, dirnames, filenames in os.walk(base):
                here = Path(dirpath)
                depth = len(here.relative_to(base).parts)
                if ".git" in dirnames or ".git" in filenames:
                    found.append(here)
                    dirnames[:] = []
                    continue
                dirnames[:] = [] if depth >= 3 else [d for d in dirnames if d not in ("node_modules", "build", ".venv") and not is_link(here / d)]
            return sorted(found)

        repos = await asyncio.to_thread(scan)
    if not repos:
        return f"No git repository at or (up to 3 levels) below {base}."
    blocks = []
    for repo in repos[:30]:
        # Each checkout is its own target: run git there under a scope that names exactly it.
        with guard.activate(guard.Scope.of(str(repo)), needs=(TARGET, EXECUTION), name="git_status"):
            status = await _git(repo, "status", "-sb")
            last = await _git(repo, "log", "-1", "--format=%h %s (%cr)")
        blocks.append(f"{repo}\n  last commit: {last or '(no commits yet)'}\n" + clip("\n".join("  " + s for s in status.splitlines()), 4000, keep="head"))
    return "\n\n".join(blocks)


def _shell() -> tuple[str, list[str]]:
    """Shell used by run_command: bash on POSIX, PowerShell on Windows."""
    if not WINDOWS:
        return "bash", ["bash", "-c"]
    exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    return "PowerShell", [exe, "-NoProfile", "-NonInteractive", "-Command"]


SHELL_NAME, SHELL_ARGV = _shell()
_shell_started = False  # whether this server process has started the shell at least once

# Absolute paths (drive, UNC, POSIX or ~) and parent-relative paths as they appear in a command.
_COMMAND_PATH = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]|\.\.(?:[\\/]|$))[^\s"'|&;<>()`]*""")
_QUOTED = re.compile(r""""([^"]+)"|'([^']+)'""")


def _check_command_targets(command: str, workdir: Path) -> None:
    """Reject a command line that names a location in another Git worktree.

    This is a guard against a mistaken target, not a sandbox: a shell can reach other
    directories in ways no inspection of its command line will see. The working directory is
    enforced; beyond that, only paths spelled out in the command are checked."""
    scope = active()
    candidates = {m.group(0) for m in _COMMAND_PATH.finditer(command)}
    candidates |= {q for m in _QUOTED.finditer(command) for q in m.groups() if q and _COMMAND_PATH.match(q)}
    for raw in candidates:
        p = Path(os.path.expanduser(raw))
        try:
            resolved = real(p if p.is_absolute() else workdir / p)
            if not os.path.lexists(resolved) or within(resolved, scope.root):
                continue
        except (OSError, ValueError):
            continue
        owner = worktree_root(resolved)
        if owner is not None and owner != scope.worktree:
            raise Blocked(
                "argument_redirect",
                f"The command names {raw}, which is in the Git worktree {owner}, not the one expected_root {scope.root} "
                "identifies. Nothing was run. Pass that worktree as expected_root to work there.",
            )


def _shell_state() -> tuple[str, str | None, str | None]:
    if shutil.which(SHELL_ARGV[0]) is None:
        return "unavailable", "execution_unavailable", f"{SHELL_NAME} ({SHELL_ARGV[0]}) was not found."
    if _shell_started:
        return "available", None, None
    return "unknown", "execution_not_observed", f"{SHELL_NAME} is installed, but this server has not started a command yet."


@ops.tool(
    kind="execution",
    needs=(EXECUTION,),
    annotations=DESTRUCTIVE,
    description=(
        f"Run a {SHELL_NAME} command in a directory inside expected_root and return its outcome, exit code and "
        "combined output. A non-zero exit or a timeout is reported as a tool error. A command that names a path "
        "in another Git worktree is rejected. For Gradle, detekt and ktlint prefer the kotlin server's tools, "
        "which summarise the results."
    ),
)
async def run_command(
    command: Annotated[str, Field(description=f"Command line, run with {SHELL_NAME}.")],
    cwd: Annotated[str | None, Field(description="Absolute working directory inside expected_root. Defaults to expected_root.")] = None,
    timeout_sec: Annotated[int, Field(description="Kill the command after this many seconds.", ge=1, le=3600)] = 120,
    expected_root: ExpectedRoot = None,
) -> str:
    global _shell_started
    workdir = active().launch_dir(cwd)
    _check_command_targets(command, workdir)
    if _shell_state()[0] == "unavailable":
        raise Blocked("execution_unavailable", _shell_state()[2] or "", "environment")
    r = await run_proc([*SHELL_ARGV, command], cwd=workdir, timeout=timeout_sec)
    _shell_started = True
    status = f"timed out after {timeout_sec}s and was killed" if r.timed_out else f"exit code {r.code}"
    text = f"[{status}] (cwd: {workdir})\n" + clip(r.output.rstrip("\n") or "(no output)")
    if r.timed_out:
        return guard.outcome("timed_out", text, category="unknown")
    return guard.outcome("passed" if r.code == 0 else "failed", text, category=None if r.code == 0 else "unknown")


@ops.tool(kind="readiness", needs=(), annotations=READ, structured_output=True)
async def readiness(
    expected_root: ExpectedRoot = None,
    path: Annotated[str | None, Field(description="Directory inside expected_root to report on. Defaults to expected_root.")] = None,
) -> Readiness:
    """Report what this server could do for a scope right now, without running anything: the
    validated scope, whether a shell is available for run_command, and the registered
    operations with the checks each enforces. A current observation, not an authorisation;
    every operation checks again when it runs. This server has no code-aware operations."""
    scope = active()
    scope.dir(path)
    return ops.report(scope, None, _shell_state(), ("not_applicable", None, None))


# ----------------------------------------------------------------------------- entry point


def check() -> int:
    """Self-test for install scripts: show what the server sees."""
    ops.verify()
    tools = asyncio.run(mcp.list_tools())
    roots = allowed_roots()
    print(f"server:  files ({len(tools)} tools)")
    print(f"cwd:     {Path.cwd()}")
    print("roots:   " + (", ".join(map(str, roots)) if roots else "unrestricted (set IDE_MCP_ALLOWED_ROOTS to limit)"))
    print("git:     " + (shutil.which("git") or "NOT FOUND (search falls back to walking the tree)"))
    return 0


def main() -> None:
    if "--check" in sys.argv[1:]:
        sys.exit(check())
    ops.verify()
    mcp.run()


if __name__ == "__main__":
    main()
