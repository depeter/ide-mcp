"""files MCP server: read, edit, find and search files anywhere on disk, no IDE or open
project required."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import execution, guard, validation
from .common import (
    WINDOWS,
    Blocked,
    ScanState,
    ToolError,
    active,
    allowed_roots,
    bounded_entries,
    clip,
    glob_matcher,
    is_link,
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
        "Request contract 3.0 returns legacy text and structured OperationResult schema 1.0. "
        "Batch item failures retain successful items but set isError=true. Read with "
        "include_revision=true, then pass expected_revision to replace_text or an existing-file "
        "create_file(overwrite=true). edit_files previews exact unique snippets and requires the "
        "complete reviewed preview_id to apply. Reads and traversal have explicit work bounds; "
        "incomplete output is not proof of absence. Shell execution is not a filesystem sandbox. "
        "start_command and start_validation use shared scoped jobs; poll job_status/job_output "
        "and retrieve job_result. Only confirmed owned termination is final."
    ),
)
ops = guard.Operations(mcp)
jobs = execution.register_jobs(ops)

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_LINE = 300
MAX_READ_WORK = 2_000_000
MAX_EDIT_BYTES = 8_000_000
MAX_BATCH_OUTPUT = 120_000

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


def _decode_bytes(data: bytes, encoding: str | None = None) -> tuple[str, str, bytes]:
    if b"\0" in data:
        raise ToolError("Binary and UTF-16 files are not supported.")
    if encoding not in (None, "utf-8", "cp1252"):
        raise ToolError("Supported encodings are utf-8 and explicitly selected cp1252.")
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    chosen = encoding or "utf-8"
    if bom and chosen != "utf-8":
        raise ToolError("The UTF-8 BOM conflicts with the requested encoding.")
    try:
        return data[len(bom):].decode(chosen), chosen, bom
    except UnicodeError:
        raise ToolError("Unsupported text encoding. Select cp1252 explicitly for Windows legacy text.") from None


def _revision_required(value: str | None) -> str:
    if value is None or not re.fullmatch(r"[0-9a-f]{64}|-", value):
        raise Blocked("revision_required", "expected_revision is required. Obtain it with read_file(include_revision=true).", "input")
    return value


def _range(path: str, start_line: int, max_lines: int, line_numbers: bool,
           include_revision: bool = False, encoding: str | None = None, output_limit: int = MAX_BATCH_OUTPUT) -> dict[str, Any]:
    scope = active()
    p = scope.file(path)
    if start_line < 1 or max_lines < 1 or max_lines > 10_000:
        raise ToolError("Invalid line range.")
    revision_bytes = scope.read_bytes(p, MAX_EDIT_BYTES) if include_revision else None
    lines, consumed, eof, returned = [], 0, False, 0
    with (io.BytesIO(revision_bytes) if revision_bytes is not None else scope.open_read(p)) as f:
        for index in range(1, start_line + max_lines):
            raw = f.readline(MAX_READ_WORK - consumed + 1)
            consumed += len(raw)
            if consumed > MAX_READ_WORK:
                raise guard.FileTooLarge("The requested range exceeds the 2 MB scan budget. Narrow the range.")
            if not raw:
                eof = True
                break
            text, _encoding, _bom = _decode_bytes(raw, encoding)
            if index >= start_line:
                if returned + len(text) > output_limit:
                    break
                lines.append(text.rstrip("\r\n"))
                returned += len(text)
        else:
            eof = not f.read(1)
    if not lines and not eof:
        raise ToolError("The first requested line exceeds the output bound.")
    if not lines and start_line > 1:
        raise ToolError(f"start_line {start_line} is past the end of {p}.")
    end = start_line + len(lines) - 1
    content = "\n".join(lines)
    numbered = _numbered(lines, start_line) if line_numbers and lines else content
    item = {"path": str(p), "outcome": "passed", "start_line": start_line, "end_line": end,
            "total_lines": end if eof else None, "content": content, "result": numbered,
            "complete": eof, "continuation": None if eof else {"start_line": end + 1},
            "scanned_bytes": consumed, "revision": None, "encoding": encoding or "utf-8"}
    if include_revision:
        assert revision_bytes is not None
        data = revision_bytes
        _text, chosen, bom = _decode_bytes(data, encoding)
        item.update(revision=hashlib.sha256(data).hexdigest(), encoding=chosen,
                    bom=bool(bom), revision_hashed_bytes=len(data))
    return item


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
    include_revision: bool = False,
    encoding: str | None = None,
) -> str:
    """Read a bounded range. Total lines stay unknown until EOF. Opt-in revision hashes at most 8 MB."""
    item = await asyncio.to_thread(_range, path, start_line, max_lines, line_numbers, include_revision, encoding)
    header = f"{item['path']} (lines {start_line}-{item['end_line']} of {item['total_lines'] if item['total_lines'] is not None else 'unknown'})"
    if item["continuation"]:
        header += f"; continue with start_line={item['continuation']['start_line']}"
    if item["revision"]:
        header += f" [revision {item['revision']}]"
    return guard.result(header + "\n" + item["result"], complete=item["complete"],
                        incomplete_reason=None if item["complete"] else "range_limited",
                        continuation=item["continuation"], data=item)


class ReadRange(BaseModel):
    path: str
    start_line: int = Field(1, ge=1)
    max_lines: int = Field(200, ge=1, le=10_000)
    include_revision: bool = False
    encoding: str | None = None


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def read_files(items: Annotated[list[ReadRange], Field(min_length=1, max_length=32)],
                     expected_root: ExpectedRoot = None) -> str:
    """Read up to 32 explicit ranges. Return independent item errors and at most 120,000 content characters."""
    if not 1 <= len(items) <= 32:
        raise ToolError("A batch read requires 1–32 items.")
    results, remaining, seen = [], MAX_BATCH_OUTPUT, set()
    for raw in items:
        item = ReadRange.model_validate(raw)
        try:
            key = (str(active().check(item.path)), item.start_line, item.max_lines)
            if key in seen:
                raise ToolError("Duplicate range item.")
            seen.add(key)
            if remaining <= 0:
                raise ToolError("Aggregate output limit reached; request the remaining items separately.")
            value = await asyncio.to_thread(_range, item.path, item.start_line, item.max_lines, True,
                                            item.include_revision, item.encoding, remaining)
            remaining -= len(value["content"])
            results.append(value)
        except (ToolError, OSError) as e:
            results.append({"path": item.path, "outcome": getattr(e, "outcome", "failed"),
                            "reason": getattr(e, "reason", "read_failed"), "error": str(e)})
    rejected = any(r["outcome"] != "passed" for r in results)
    complete = not rejected and all(r["complete"] for r in results)
    text = "\n\n".join(f"{r['path']}\n{r.get('result', r.get('error', ''))}" for r in results)
    return guard.result(text, outcome="failed" if rejected else "passed", complete=complete,
                        reason="partial_items" if rejected else None,
                        incomplete_reason=None if complete else "partial_items" if rejected else "range_limited",
                        data={"items": results, "content_limit": MAX_BATCH_OUTPUT})


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def list_directory_tree(
    path: Root = None,
    max_depth: Annotated[int, Field(description="How many directory levels to expand.", ge=1, le=10)] = 3,
    include_ignored: IncludeIgnored = False,
    expected_root: ExpectedRoot = None,
    limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    work_limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
) -> str:
    """List visible entries only. Do not descend beyond max_depth or invent recursive totals."""
    root = active().dir(path)

    def build() -> str:
        state, entries, lines = ScanState(), [], []
        for p, depth, directory in bounded_entries(root, include_ignored, max_depth, work_limit, state):
            if len(entries) >= limit:
                state.complete, state.reason = False, "entry_limit"
                break
            collapsed = directory and depth == max_depth
            entries.append({"path": rel(p, root), "directory": directory, "expanded": directory and not collapsed})
            lines.append("  " * (depth - 1) + p.name + ("/ [unexpanded]" if collapsed else "/" if directory else ""))
        return guard.result(f"{root}/\n" + "\n".join(lines), complete=state.complete,
                            incomplete_reason=state.reason, data={
                                "entries": entries, "visited_entries": state.visited,
                                "skipped_entries": state.skipped, "max_depth": max_depth,
                                "continuation_supported": False,
                            })

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
    work_limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
) -> str:
    """Find files by glob and/or by a substring of the file name. Returns paths relative to
    root."""
    base = active().dir(root)
    match = glob_matcher(glob)
    needle = name_contains.lower() if name_contains else None

    def scan() -> str:
        hits, state = [], ScanState()
        for f, _depth, directory in bounded_entries(base, include_ignored, 100, work_limit, state):
            if directory:
                continue
            r = rel(f, base)
            if needle and needle not in f.name.lower():
                continue
            if match(r):
                hits.append(r)
                if len(hits) >= limit:
                    state.complete, state.reason = False, "result_limit"
                    break
        return guard.result(f"{len(hits)} file(s) found under {base}:\n" + "\n".join(hits),
                            complete=state.complete, incomplete_reason=state.reason,
                            data={"paths": hits, "visited_entries": state.visited, "continuation_supported": False})
    return await asyncio.to_thread(scan)


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
    work_limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
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

    def scan() -> tuple[list[str], int, int, int, dict[str, int], ScanState]:
        out: list[str] = []
        shown = total = searched = 0
        refused: dict[str, int] = {}
        state = ScanState()
        scanned_bytes = 0
        for f, _depth, directory in bounded_entries(base, include_ignored, 100, work_limit, state):
            if directory:
                continue
            if shown >= limit or scanned_bytes >= 20_000_000:
                state.complete, state.reason = False, "search_limit"
                break
            r = rel(f, base)
            if not match(r):
                continue
            try:
                data = scope.read_bytes(f, min(MAX_SEARCH_FILE_BYTES, 20_000_000 - scanned_bytes))
                scanned_bytes += len(data)
                if b"\0" in data[:8192]:
                    refused["binary"] = refused.get("binary", 0) + 1
                    continue
                lines = data.decode("utf-8").splitlines()
            except Blocked as e:
                refused[e.reason or "blocked"] = refused.get(e.reason or "blocked", 0) + 1
                continue
            except (OSError, ToolError, UnicodeError):
                refused["unreadable_or_unsupported"] = refused.get("unreadable_or_unsupported", 0) + 1
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
        return out, shown, total, searched, refused, state

    out, shown, total, searched, refused, state = await asyncio.to_thread(scan)
    complete = state.complete and not refused and shown < limit
    data = {"matching_lines": shown, "searched_files": searched, "skipped": dict(refused),
            "visited_entries": state.visited, "continuation_supported": False}
    note = ""
    if n := refused.pop("protected_path", 0):
        note += f"\n[{n} protected file(s) were not opened]"
    if n := sum(refused.values()):
        note += f"\n[{n} file(s) resolve outside expected_root or changed while being opened, and were not read]"
    if not total:
        return guard.result(f"No matches in {searched} searched file(s) under {base}." + note,
                            complete=complete, incomplete_reason=None if complete else state.reason or "skipped_files",
                            data=data)
    header = f"{total} matching line(s) in {searched} searched file(s) under {base}"
    if total > shown:
        header += f"; showing the first {shown} (narrow with file_glob or raise limit)"
    text = header + ":\n" + "\n".join(out)
    if len(text) > 60_000:
        complete = False
    return guard.result(clip(text, 60_000, keep="head") + note, complete=complete,
                        incomplete_reason=None if complete else state.reason or "bounded_or_skipped",
                        data=data)


# ----------------------------------------------------------------------------- writing


@ops.tool(kind="file", needs=(TARGET,), annotations=DESTRUCTIVE)
async def create_file(
    path: FilePath,
    text: Annotated[str, Field(description="Full contents of the file.")],
    overwrite: Annotated[bool, Field(description="Replace the file if it already exists.")] = False,
    expected_root: ExpectedRoot = None,
    expected_revision: str | None = None,
) -> str:
    """Create a file with the given contents, creating parent directories as needed. Fails if
    the file exists unless overwrite=true."""
    p = active().check(path)
    if p.is_dir():
        raise ToolError(f"{p} is a directory.")
    existed = p.exists()
    if existed and not overwrite:
        raise ToolError(f"{p} already exists. Pass overwrite=true to replace it, or use replace_text to edit it.")
    revision = _revision_required(expected_revision) if existed else "-"
    if not existed and expected_revision not in (None, "-"):
        raise ToolError("A new file requires expected_revision='-'.")
    if not existed and text and not text.endswith("\n\n"):
        text = text.rstrip("\r\n") + ("\r\n\r\n" if "\r\n" in text else "\n\n")
    await asyncio.to_thread(active().write_bytes, p, text.encode("utf-8"), expected_revision=revision)
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
    expected_revision: str | None = None,
    encoding: str | None = None,
) -> str:
    """Replace text in a file. By default old_text must occur exactly once, so include enough
    surrounding lines to make it unique; the error lists the lines of every match otherwise.
    Whitespace and indentation must match the file exactly."""
    p = _existing_file(path)
    if not old_text:
        raise ToolError("old_text must not be empty. Use create_file to write a whole file.")
    data = await asyncio.to_thread(active().read_bytes, p, MAX_EDIT_BYTES)
    content, chosen, bom = _decode_bytes(data, encoding)
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
        active().require_revision(p, _revision_required(expected_revision))
        return f"No change: the replacement is identical to the existing text in {p}."
    await asyncio.to_thread(active().write_bytes, p, bom + updated.encode(chosen),
                            expected_revision=_revision_required(expected_revision))

    first = line_of(matches[0].start())
    new_lines = updated.splitlines()
    span = len((matches[0].expand(new_text) if regex else new_text).splitlines()) or 1
    lo = max(1, first - 2)
    hi = min(len(new_lines), first + span + 1)
    snippet = _numbered([_short(line) for line in new_lines[lo - 1 : hi]], lo) if new_lines else "(file is now empty)"
    return f"Replaced {len(matches)} occurrence(s) in {p}. Around line {first} it now reads:\n{clip(snippet, 4000, keep='head')}"


class Replacement(BaseModel):
    old_text: str = Field(min_length=1, max_length=120_000)
    new_text: str = Field(max_length=120_000)


class ExactEdit(BaseModel):
    path: str
    expected_revision: str
    replacements: list[Replacement] = Field(min_length=1, max_length=100)
    encoding: str | None = None


@ops.tool(kind="file", needs=(TARGET,), annotations=DESTRUCTIVE)
async def edit_files(edits: Annotated[list[ExactEdit], Field(min_length=1, max_length=32)],
                     dry_run: bool = True, preview_id: str | None = None,
                     expected_root: ExpectedRoot = None) -> str:
    """Preview or apply unique exact snippets with content revisions. Preflight all files.

    Apply requires the complete preview's ID. A partial failure retains and lists completed
    writes. There is no multi-file atomicity or unconditional rollback.
    """
    if not 1 <= len(edits) <= 32:
        raise ToolError("An exact-edit batch requires 1–32 items.")
    scope, plans, seen, budget = active(), [], set(), 0
    for raw in edits:
        item = ExactEdit.model_validate(raw)
        p = scope.file(item.path)
        key = str(p).casefold() if WINDOWS else str(p)
        if key in seen or any(os.path.samefile(p, q["path"]) for q in plans):
            raise ToolError("Duplicate resolved edit path.")
        seen.add(key)
        scope.require_revision(p, _revision_required(item.expected_revision))
        data = scope.read_bytes(p, MAX_EDIT_BYTES)
        if hashlib.sha256(data).hexdigest() != item.expected_revision:
            raise Blocked("stale_revision", f"{p} changed during edit preflight.", "input")
        budget += len(data)
        if budget > 16_000_000:
            raise ToolError("The exact-edit batch exceeds its 16 MB byte budget.")
        text, encoding, bom = _decode_bytes(data, item.encoding)
        spans = []
        for r in item.replacements:
            count = text.count(r.old_text)
            if count != 1:
                raise ToolError(f"{p}: exact snippet matches {count} places; exactly one is required.")
            start = text.index(r.old_text)
            spans.append((start, start + len(r.old_text), r.new_text))
        spans.sort()
        if any(b[0] < a[1] for a, b in pairwise(spans)):
            raise ToolError(f"{p}: overlapping replacements.")
        updated = text
        for start, end, replacement in reversed(spans):
            updated = updated[:start] + replacement + updated[end:]
        try:
            payload = bom + updated.encode(encoding)
        except UnicodeError:
            raise ToolError(f"{p}: replacement cannot be encoded as {encoding}.") from None
        diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), updated.splitlines(keepends=True),
                                          fromfile=str(p), tofile=str(p), n=2))
        if sum(len(q["diff"]) for q in plans) + len(diff) > MAX_BATCH_OUTPUT:
            raise Blocked("preview_too_large", "The full preview exceeds 120,000 characters. Split the edit batch.", "input")
        plans.append({"path": p, "revision": item.expected_revision, "payload": payload, "diff": diff,
                      "ranges": [{"start_line": text.count("\n", 0, s) + 1,
                                  "end_line": text.count("\n", 0, e) + 1} for s, e, _r in spans]})
    binding = [str(scope.ident), [(str(q["path"]), q["revision"], q["ranges"],
                                 hashlib.sha256(q["payload"]).hexdigest()) for q in plans]]
    digest = hashlib.sha256(json.dumps(binding).encode()).hexdigest()
    public = [{"path": str(q["path"]), "expected_revision": q["revision"], "changed_ranges": q["ranges"],
               "new_revision": hashlib.sha256(q["payload"]).hexdigest()} for q in plans]
    if dry_run:
        return guard.result("\n".join(q["diff"] for q in plans) + f"\npreview_id: {digest}",
                            outcome="not_run", reason="dry_run", data={"preview_id": digest, "edits": public})
    if preview_id != digest:
        raise Blocked("stale_preview", "Apply requires the ID of this exact complete preview. Preview again.", "input")
    for q in plans:
        scope.require_revision(q["path"], q["revision"])
    done = []
    try:
        with contextlib.ExitStack() as stack:
            commits = [stack.enter_context(scope.stage_write(q["path"], q["payload"],
                                                              expected_revision=q["revision"])) for q in plans]
            for q, commit in zip(plans, commits):
                commit()
                done.append(str(q["path"]))
    except (OSError, ToolError) as e:
        return guard.result(f"Application failed: {e}. Written: {done}. No rollback was attempted.",
                            outcome="failed", reason="apply_failed", category="environment", complete=False,
                            incomplete_reason="partial_application",
                            data={"written": done, "pending": [str(q["path"]) for q in plans if str(q["path"]) not in done],
                                  "edits": public})
    return guard.result("Applied the reviewed exact edits.", data={"written": done, "edits": public})


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
    if re.search(r"""(?i)[^\s"';&|]+\.properties(?:[. :][^\s"';&|]*)?(?=$|[\s"';&|])""", command):
        raise Blocked("protected_path", "The shell command names a protected file. Raw execution cannot bypass a denied file operation.")
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
    artifact = execution.save_artifact(r.output)
    return guard.finish(guard.result(text, outcome="timed_out" if r.timed_out else "passed" if r.code == 0 else "failed",
                        category=None if r.code == 0 else "unknown", complete=r.output_complete,
                        incomplete_reason=None if r.output_complete else "output_limit",
                        data={"executable": SHELL_ARGV[0], "exit_code": r.code, "artifacts": [artifact],
                              "shell_confinement": "working_directory_only"}))


@ops.tool(kind="execution", needs=(EXECUTION,), annotations=DESTRUCTIVE)
async def start_command(command: str, cwd: str | None = None, timeout_sec: int = 120,
                        expected_root: ExpectedRoot = None) -> str:
    """Start one approved shell command. Shell execution is not a filesystem sandbox."""
    root = active().launch_dir(cwd)
    _check_command_targets(command, root)
    if not 1 <= timeout_sec <= 3600:
        raise ToolError("timeout_sec must be between 1 and 3600.")
    job = jobs.start(lambda: run_command(command, cwd=str(root), timeout_sec=timeout_sec,
                                         expected_root=expected_root))
    return guard.result("Command queued.", complete=False, incomplete_reason="job_not_final",
                        data={"job_id": job.id, "state": job.state})


@ops.tool(kind="execution", needs=(TARGET, EXECUTION), annotations=ACT)
async def validate_python(project: str, executable_path: str, check: str, files: list[str],
                          filter: str | None = None, timeout_sec: int = 600,
                          expected_root: ExpectedRoot = None) -> str:
    """Run focused pytest, ruff or basedpyright using an explicitly selected Python interpreter."""
    if not 1 <= timeout_sec <= 3600:
        raise ToolError("timeout_sec must be between 1 and 3600.")
    return guard.finish(await validation.validate_python(project, executable_path, check, files, filter, timeout_sec))


@ops.tool(kind="execution", needs=(TARGET, EXECUTION), annotations=ACT)
async def validate_dotnet(project: str, executable_path: str, check: str,
                          filter: str | None = None, timeout_sec: int = 600,
                          isolate_outputs: bool = False, output_root: str | None = None,
                          expected_root: ExpectedRoot = None) -> str:
    """Run an explicit .NET project build/test. Native .NET 8+ output isolation leaves the app alone."""
    if not 1 <= timeout_sec <= 3600:
        raise ToolError("timeout_sec must be between 1 and 3600.")
    return guard.finish(await validation.validate_dotnet(project, executable_path, check, filter,
                                                         timeout_sec, isolate_outputs, output_root))


@ops.tool(kind="execution", needs=(TARGET, EXECUTION), annotations=ACT)
async def start_validation(adapter: str, project: str, executable_path: str, check: str,
                           files: list[str] | None = None, filter: str | None = None,
                           timeout_sec: int = 600, isolate_outputs: bool = False,
                           output_root: str | None = None, expected_root: ExpectedRoot = None) -> str:
    """Start the same focused Python/.NET check as the synchronous adapter."""
    active().check(project, protected=False)
    if adapter not in ("python", "dotnet") or not 1 <= timeout_sec <= 3600:
        raise ToolError("Select python or dotnet and a timeout from 1 to 3600 seconds.")
    if adapter == "python":
        if isolate_outputs or output_root:
            raise ToolError("Output isolation parameters belong to the .NET adapter.")
        validation.python_inputs(project, executable_path, check, files or [], filter)
    else:
        if files:
            raise ToolError(".NET validation uses an explicit project, not Python file targets.")
        validation.dotnet_inputs(project, executable_path, check, filter, output_root)
    async def work():
        if adapter == "python":
            return await validate_python(project, executable_path, check, files or [], filter,
                                         timeout_sec, expected_root=expected_root)
        return await validate_dotnet(project, executable_path, check, filter, timeout_sec,
                                     isolate_outputs, output_root, expected_root=expected_root)
    job = jobs.start(work)
    return guard.result("Validation queued.", complete=False, incomplete_reason="job_not_final",
                        data={"job_id": job.id, "state": job.state})


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
