"""files MCP server: read, edit, find and search files anywhere on disk, no IDE or open
project required."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .common import (
    ToolError,
    allowed_roots,
    clip,
    existing_dir,
    glob_matcher,
    is_binary,
    iter_files,
    rel,
    resolve,
    run_proc,
)

mcp = FastMCP(
    "files",
    instructions=(
        "File tools that work on any path on this machine; nothing needs to be opened in an IDE "
        "first. Pass absolute paths (or ~/...). Relative paths resolve against the directory the "
        "server was started in, which may not be your project, so prefer absolute ones.\n"
        "Search and listing tools take a `root` directory and honour .gitignore inside git "
        "repositories; elsewhere they skip build and cache directories. Pass include_ignored=true "
        "to look inside those too.\n"
        "To edit: read_file, then replace_text with enough surrounding text to match exactly once. "
        "Use create_file for new files or whole-file rewrites (overwrite=true)."
    ),
)

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_LINE = 300

FilePath = Annotated[str, Field(description="Absolute path (or ~/...) of the file.")]
Root = Annotated[
    str | None,
    Field(description="Directory to look in, absolute or ~/... Defaults to the server's start directory."),
]
IncludeIgnored = Annotated[
    bool,
    Field(description="Also look in git-ignored files and build/cache directories."),
]


def _existing_file(path: str) -> Path:
    p = resolve(path)
    if not p.exists():
        raise ToolError(f"File does not exist: {p}")
    if p.is_dir():
        raise ToolError(f"{p} is a directory. Use list_directory_tree to see its contents.")
    return p


def _decode(p: Path) -> str:
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{p} looks like a binary file.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{p} is not valid UTF-8 text.") from None


def _write_atomic(p: Path, text: str) -> None:
    """Write via a temp file in the same directory so a crash can't leave a half-written file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = p.stat().st_mode & 0o7777 if p.exists() else None
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _numbered(lines: list[str], first: int) -> str:
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{first + i:>{width}}\t{line}" for i, line in enumerate(lines))


def _short(line: str) -> str:
    line = line.rstrip("\r\n")
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + " [...]"


# ----------------------------------------------------------------------------- reading


@mcp.tool(annotations=READ)
async def read_file(
    path: FilePath,
    start_line: Annotated[int, Field(description="1-based line to start from.", ge=1)] = 1,
    max_lines: Annotated[int, Field(description="Maximum number of lines to return.", ge=1, le=10_000)] = 2000,
    line_numbers: Annotated[bool, Field(description="Prefix each line with its line number.")] = True,
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


@mcp.tool(annotations=READ)
async def list_directory_tree(
    path: Root = None,
    max_depth: Annotated[int, Field(description="How many directory levels to expand.", ge=1, le=10)] = 3,
    include_ignored: IncludeIgnored = False,
) -> str:
    """Show a directory as an indented tree. Directories deeper than max_depth are collapsed to
    a file count. Git-ignored files and build output are left out unless include_ignored."""
    root = existing_dir(path)

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


@mcp.tool(annotations=READ)
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
) -> str:
    """Find files by glob and/or by a substring of the file name. Returns paths relative to
    root."""
    base = existing_dir(root)
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


@mcp.tool(annotations=READ)
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
) -> str:
    """Search file contents under root for text or a regex. Results are grouped by file with
    line numbers. Binary files and files over 2 MB are skipped."""
    base = existing_dir(root)
    try:
        rx = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}") from None
    match = glob_matcher(file_glob)

    def scan() -> tuple[list[str], int, int, int]:
        out: list[str] = []
        shown = total = searched = 0
        for f in sorted(iter_files(base, include_ignored)):
            r = rel(f, base)
            if not match(r):
                continue
            try:
                if f.stat().st_size > MAX_SEARCH_FILE_BYTES or is_binary(f):
                    continue
                lines = f.read_bytes().decode("utf-8", "replace").splitlines()
            except OSError:
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
        return out, shown, total, searched

    out, shown, total, searched = await asyncio.to_thread(scan)
    if not total:
        return f"No matches in {searched} file(s) under {base}."
    header = f"{total} matching line(s) in {searched} searched file(s) under {base}"
    if total > shown:
        header += f"; showing the first {shown} (narrow with file_glob or raise limit)"
    return clip(header + ":\n" + "\n".join(out), 60_000, keep="head")


# ----------------------------------------------------------------------------- writing


@mcp.tool(annotations=DESTRUCTIVE)
async def create_file(
    path: FilePath,
    text: Annotated[str, Field(description="Full contents of the file.")],
    overwrite: Annotated[bool, Field(description="Replace the file if it already exists.")] = False,
) -> str:
    """Create a file with the given contents, creating parent directories as needed. Fails if
    the file exists unless overwrite=true."""
    p = resolve(path)
    if p.is_dir():
        raise ToolError(f"{p} is a directory.")
    existed = p.exists()
    if existed and not overwrite:
        raise ToolError(f"{p} already exists. Pass overwrite=true to replace it, or use replace_text to edit it.")
    await asyncio.to_thread(_write_atomic, p, text)
    return f"{'Overwrote' if existed else 'Created'} {p} ({len(text.splitlines())} lines)."


@mcp.tool(annotations=DESTRUCTIVE)
async def replace_text(
    path: FilePath,
    old_text: Annotated[str, Field(description="Exact text to replace (or a regex when regex=true). Must not be empty.")],
    new_text: Annotated[str, Field(description="Replacement. With regex=true, \\1 and \\g<name> refer to groups.")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring exactly one.")] = False,
    regex: Annotated[bool, Field(description="Treat old_text as a Python regular expression (multi-line, ^/$ per line).")] = False,
    case_sensitive: bool = True,
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


@mcp.tool(annotations=DESTRUCTIVE)
async def move_path(
    source: Annotated[str, Field(description="Absolute path of the file or directory to move or rename.")],
    destination: Annotated[str, Field(description="Absolute path it should have afterwards.")],
    overwrite: Annotated[bool, Field(description="Replace an existing file at the destination.")] = False,
) -> str:
    """Move or rename a file or directory, creating the destination's parent directories."""
    src, dst = resolve(source), resolve(destination)
    if not src.exists():
        raise ToolError(f"Source does not exist: {src}")
    if dst.is_dir() and not src.is_dir():
        dst = dst / src.name
    if dst.exists():
        if not overwrite or dst.is_dir():
            raise ToolError(f"Destination already exists: {dst}" + ("" if dst.is_dir() else " (pass overwrite=true to replace it)"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src), str(dst))
    return f"Moved {src} -> {dst}"


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_path(
    path: Annotated[str, Field(description="Absolute path of the file or directory to delete.")],
    recursive: Annotated[bool, Field(description="Required to delete a directory that is not empty.")] = False,
) -> str:
    """Delete a file or directory. This is permanent; nothing goes to a trash folder."""
    p = resolve(path)
    if not p.exists() and not p.is_symlink():
        raise ToolError(f"Does not exist: {p}")
    real = Path(os.path.realpath(p))
    if len(real.parts) < 3 or real == Path.home() or real in Path.home().parents:
        raise ToolError(f"Refusing to delete {p}: it is too close to the filesystem root or is your home directory.")
    if p.is_dir() and not p.is_symlink():
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
    r = await run_proc(["git", "-C", str(repo), *args], timeout=30)
    return r.output.strip() if r.code == 0 else ""


@mcp.tool(annotations=READ)
async def git_status(
    path: Annotated[
        str | None,
        Field(description="A directory inside a repository, or a directory containing repositories. Defaults to the server's start directory."),
    ] = None,
) -> str:
    """Show the git repositories at or below a path: root, branch, ahead/behind, last commit,
    and changed files."""
    base = existing_dir(path)
    top = await _git(base, "rev-parse", "--show-toplevel")
    if top:
        repos = [Path(top)]
    else:
        def scan() -> list[Path]:
            found = []
            for dirpath, dirnames, filenames in os.walk(base):
                depth = len(Path(dirpath).relative_to(base).parts)
                if ".git" in dirnames or ".git" in filenames:
                    found.append(Path(dirpath))
                    dirnames[:] = []
                    continue
                dirnames[:] = [] if depth >= 3 else [d for d in dirnames if d not in ("node_modules", "build", ".venv")]
            return sorted(found)

        repos = await asyncio.to_thread(scan)
    if not repos:
        return f"No git repository at or (up to 3 levels) below {base}."
    blocks = []
    for repo in repos[:30]:
        status = await _git(repo, "status", "-sb")
        last = await _git(repo, "log", "-1", "--format=%h %s (%cr)")
        blocks.append(f"{repo}\n  last commit: {last or '(no commits yet)'}\n" + clip("\n".join("  " + s for s in status.splitlines()), 4000, keep="head"))
    return "\n\n".join(blocks)


@mcp.tool(annotations=DESTRUCTIVE)
async def run_command(
    command: Annotated[str, Field(description="Command line, run with bash -c.")],
    cwd: Annotated[str | None, Field(description="Absolute working directory. Defaults to the server's start directory.")] = None,
    timeout_sec: Annotated[int, Field(description="Kill the command after this many seconds.", ge=1, le=3600)] = 120,
) -> str:
    """Run a shell command and return its exit code and combined output. For Gradle, detekt and
    ktlint prefer the kotlin server's tools, which summarise the results."""
    workdir = existing_dir(cwd, "Working directory")
    r = await run_proc(["bash", "-c", command], cwd=workdir, timeout=timeout_sec)
    status = f"timed out after {timeout_sec}s and was killed" if r.timed_out else f"exit code {r.code}"
    return f"[{status}] (cwd: {workdir})\n" + clip(r.output.rstrip("\n") or "(no output)")


# ----------------------------------------------------------------------------- entry point


def check() -> int:
    """Self-test for install scripts: show what the server sees."""
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
    mcp.run()


if __name__ == "__main__":
    main()
