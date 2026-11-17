"""Client for JetBrains' Kotlin language server (kotlin-lsp, the headless IntelliJ engine).

One server process runs per project root, started on first use and kept until it has been
idle for a while. It gives the kotlin MCP server what text matching cannot: references and
renames that resolve symbols, and IntelliJ inspections.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .common import WINDOWS, ToolError, _terminate, iter_files, path_key

DEFAULT_VERSION = "271.2085.0"
DOWNLOAD_BASE = "https://download.jetbrains.com/language-server/kotlin-server"
SOURCE_SUFFIXES = (".kt", ".kts", ".java")
IDLE_SECONDS = float(os.environ.get("KOTLIN_LSP_IDLE_MINUTES", "30")) * 60
MAX_OPEN_DOCS = 40


def data_dir() -> Path:
    if WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ide-mcp"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "ide-mcp"


def cache_dir() -> Path:
    if WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ide-mcp" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ide-mcp"


# ----------------------------------------------------------------------------- locating


def _launcher_in(home: Path) -> Path | None:
    for name in ("intellij-server.exe", "intellij-server.bat", "intellij-server") if WINDOWS else ("intellij-server",):
        p = home / "bin" / name
        if p.is_file():
            return p
    return None


def _version_key(p: Path) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in p.name.split("."))


def find_server() -> Path | None:
    """The language server launcher: KOTLIN_LSP_PATH (launcher or install directory), then the
    newest version installed by `kotlin-mcp --install-lsp`, then `kotlin-lsp` on PATH."""
    explicit = os.environ.get("KOTLIN_LSP_PATH")
    if explicit:
        p = Path(os.path.expanduser(explicit))
        if p.is_file():
            return p
        return _launcher_in(p) if p.is_dir() else None
    base = data_dir() / "kotlin-lsp"
    if base.is_dir():
        for version in sorted((d for d in base.iterdir() if d.is_dir()), key=_version_key, reverse=True):
            for home in (version, *(d for d in version.iterdir() if d.is_dir())):
                if launcher := _launcher_in(home):
                    return launcher
    for name in ("kotlin-lsp", "intellij-server"):
        if found := shutil.which(name):
            return Path(found)
    return None


NOT_INSTALLED = (
    "The Kotlin language server is not installed. Install it with `kotlin-mcp --install-lsp` "
    "(about 370 MB download, 1.2 GB on disk), or set KOTLIN_LSP_PATH to an existing kotlin-lsp."
)


def _archive_name(version: str) -> str:
    arm = platform.machine().lower() in ("arm64", "aarch64")
    suffix = "-aarch64" if arm else ""
    if WINDOWS:
        return f"kotlin-server-{version}{suffix}.win.zip"
    if sys.platform == "darwin":
        raise ToolError("On macOS install it with: brew install JetBrains/utils/kotlin-lsp")
    return f"kotlin-server-{version}{suffix}.tar.gz"


def install(version: str | None = None, log: Callable[[str], None] = print) -> Path:
    """Download JetBrains' standalone Kotlin language server, verify its SHA-256 against the
    published checksum, and unpack it under the data directory."""
    version = version or os.environ.get("KOTLIN_LSP_VERSION") or DEFAULT_VERSION
    target = data_dir() / "kotlin-lsp" / version
    if target.is_dir() and any(_launcher_in(h) for h in (target, *(d for d in target.iterdir() if d.is_dir()))):
        log(f"Kotlin language server {version} is already installed in {target}")
        return target
    name = _archive_name(version)
    url = f"{DOWNLOAD_BASE}/{version}/{name}"
    target.mkdir(parents=True, exist_ok=True)
    archive = target.parent / name
    log(f"Downloading {url}")
    log("This is JetBrains software (Alpha), used under JetBrains' terms: https://github.com/Kotlin/kotlin-lsp")
    try:
        with urllib.request.urlopen(url + ".sha256", timeout=60) as r:
            expected = r.read().decode().split()[0].lower()
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = last = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total and done - last >= 50 << 20:
                    last = done
                    log(f"  {done >> 20} / {total >> 20} MB")
        if digest.hexdigest() != expected:
            raise ToolError(f"Checksum mismatch for {name}: expected {expected}, got {digest.hexdigest()}. Nothing was installed.")
        log(f"Checksum verified. Unpacking to {target}")
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)
        else:
            with tarfile.open(archive) as t:
                _extract_tar(t, target)
    except ToolError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(target, ignore_errors=True)
        raise ToolError(f"Could not install the Kotlin language server: {e}") from None
    finally:
        archive.unlink(missing_ok=True)
    if not find_server():
        raise ToolError(f"Unpacked {name} but found no bin/intellij-server launcher in {target}.")
    log(f"Installed: {find_server()}")
    return target


def _extract_tar(t: tarfile.TarFile, target: Path) -> None:
    """Extract, refusing members that would land outside the target directory."""
    try:
        t.extractall(target, filter="tar")
        return
    except TypeError:  # Python without extraction filters
        pass
    base = os.path.realpath(target)
    for m in t.getmembers():
        dest = os.path.realpath(os.path.join(base, m.name))
        if dest != base and not dest.startswith(base + os.sep):
            raise ToolError(f"Archive member escapes the install directory: {m.name}")
    t.extractall(target)


# ----------------------------------------------------------------------------- positions


def uri_of(path: Path) -> str:
    return path.as_uri()


def path_of(uri: str) -> Path:
    p = unquote(urlparse(uri).path)
    if WINDOWS and len(p) > 2 and p[0] == "/" and p[2] == ":":
        p = p[1:]
    return Path(p)


def utf16_col(line: str, index: int) -> int:
    """LSP character offset (UTF-16 code units) of a Python string index within a line."""
    return len(line[:index].encode("utf-16-le")) // 2


def py_index(line: str, utf16: int) -> int:
    """Python string index of an LSP character offset within a line."""
    units = 0
    for i, ch in enumerate(line):
        if units >= utf16:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def offset_of(lines: list[str], pos: dict) -> int:
    """Absolute string offset of an LSP position; `lines` come from splitlines(keepends=True)."""
    ln = pos["line"]
    if ln >= len(lines):
        return sum(len(s) for s in lines)
    body = lines[ln].rstrip("\r\n")
    return sum(len(s) for s in lines[:ln]) + py_index(body, pos["character"])


def apply_text_edits(text: str, edits: list[dict]) -> str:
    """Apply LSP TextEdits to a document, last edit first so earlier offsets stay valid."""
    lines = text.splitlines(keepends=True)
    spans = sorted(
        ((offset_of(lines, e["range"]["start"]), offset_of(lines, e["range"]["end"]), e["newText"]) for e in edits),
        key=lambda s: (s[0], s[1]),
        reverse=True,
    )
    crlf = "\r\n" in text
    for start, end, new in spans:
        if crlf:
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        text = text[:start] + new + text[end:]
    return text


@dataclass
class FileChange:
    """One file's part of a WorkspaceEdit, already resolved against the text on disk."""

    path: Path
    old_text: str | None  # None when the file is created
    new_text: str | None  # None when the file is deleted
    new_path: Path | None = None  # set when the file is renamed
    edit_count: int = 0

    def changed_lines(self) -> list[tuple[int, str]]:
        if self.old_text is None or self.new_text is None:
            return []
        old, new = self.old_text.splitlines(), self.new_text.splitlines()
        return [(i + 1, n.strip()) for i, n in enumerate(new) if i >= len(old) or old[i] != n]


def plan_workspace_edit(edit: dict) -> list[FileChange]:
    """Turn a WorkspaceEdit into per-file changes without touching the disk."""
    changes: dict[str, FileChange] = {}

    def entry(path: Path) -> FileChange:
        key = path_key(path)
        if key not in changes:
            text = path.read_bytes().decode("utf-8") if path.is_file() else None
            changes[key] = FileChange(path, text, text)
        return changes[key]

    def edit_text(path: Path, edits: list[dict]) -> None:
        c = entry(path)
        if c.new_text is None:
            raise ToolError(f"The rename wants to edit {path}, which does not exist.")
        # The server emits several micro-edits per occurrence ("Event" -> "Sig" + "al"), so count
        # edited lines, which is what a reader thinks of as the size of the change.
        c.edit_count += len({e["range"]["start"]["line"] for e in edits})
        c.new_text = apply_text_edits(c.new_text, edits)

    for change in edit.get("documentChanges") or []:
        kind = change.get("kind")
        if kind == "rename":
            c = entry(path_of(change["oldUri"]))
            c.new_path = path_of(change["newUri"])
        elif kind == "create":
            c = entry(path_of(change["uri"]))
            c.new_text = c.new_text or ""
        elif kind == "delete":
            entry(path_of(change["uri"])).new_text = None
        else:
            edit_text(path_of(change["textDocument"]["uri"]), change.get("edits") or [])
    for uri, edits in (edit.get("changes") or {}).items():
        edit_text(path_of(uri), edits)
    return [c for c in changes.values() if c.old_text != c.new_text or c.new_path]


def write_changes(changes: list[FileChange]) -> None:
    for c in changes:
        if c.new_text is None:
            c.path.unlink(missing_ok=True)
            continue
        if c.old_text != c.new_text:
            c.path.parent.mkdir(parents=True, exist_ok=True)
            c.path.write_bytes(c.new_text.encode("utf-8"))
        if c.new_path and c.new_path != c.path:
            c.new_path.parent.mkdir(parents=True, exist_ok=True)
            c.path.rename(c.new_path)


# ----------------------------------------------------------------------------- client


class LspError(ToolError):
    pass


_TOO_EARLY = re.compile(r"before task '(:[^']+)' has completed is not supported")


@dataclass
class Session:
    """A running language server for one project root."""

    root: Path
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    import_done: asyncio.Event = field(default_factory=asyncio.Event)
    import_status: str = ""
    import_log: list[str] = field(default_factory=list)
    import_error: str = ""  # Gradle's "What went wrong" for a failed import
    broken_tasks: set[str] = field(default_factory=set)  # tasks whose module imported without sources
    _after_went_wrong: bool = False
    progress: dict[str, str] = field(default_factory=dict)
    last_activity: str = "starting"
    quiet_since: float = field(default_factory=time.time)
    capabilities: dict = field(default_factory=dict)
    _next_id: int = 0
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _open: dict[str, tuple[int, str]] = field(default_factory=dict)  # uri -> (version, text)
    _mtimes: dict[str, float] | None = None
    _reader: asyncio.Task | None = None

    # ---- transport

    def _send(self, message: dict) -> None:
        if self.proc.returncode is not None or self.proc.stdin is None:
            raise LspError("The Kotlin language server has exited; the next call starts a new one.")
        body = json.dumps(message).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: Any, timeout: float = 120.0) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            try:
                self.notify("$/cancelRequest", {"id": rid})
            except LspError:
                pass
            raise LspError(f"The Kotlin language server did not answer {method} within {timeout:.0f}s.") from None

    async def _read_loop(self) -> None:
        out = self.proc.stdout
        assert out is not None
        try:
            while True:
                length = 0
                while True:
                    line = await out.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if not length:
                    continue
                self._handle(json.loads(await out.readexactly(length)))
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError):
            return
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LspError("The Kotlin language server exited unexpectedly."))
            self._pending.clear()
            self.import_done.set()

    def _handle(self, msg: dict) -> None:
        if "method" not in msg:  # response to one of our requests
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(LspError(f"Kotlin language server: {msg['error'].get('message', msg['error'])}"))
                else:
                    fut.set_result(msg.get("result"))
            return
        method, params = msg["method"], msg.get("params") or {}
        if "id" in msg:  # request from the server; answer so it does not wait on us
            result: Any = None
            if method == "workspace/configuration":
                result = [None] * len(params.get("items") or [])
            elif method == "workspace/applyEdit":
                result = {"applied": False, "failureReason": "edits are applied by the client on request"}
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LspError:
                pass
            return
        if method == "$/progress":
            value, token = params.get("value") or {}, str(params.get("token"))
            if value.get("kind") == "begin":
                self.progress[token] = value.get("title") or "working"
                self.last_activity = self.progress[token]
            elif value.get("kind") == "end":
                self.progress.pop(token, None)
            self.quiet_since = time.time()
        elif method == "intellij/importLog":
            for line in str(params.get("message") or "").splitlines():
                line = line.strip()
                if not line or line.startswith("at "):
                    continue
                # Known importer defect (Kotlin/kotlin-lsp#253): a module whose generated-source
                # provider is read too early is imported with no sources at all.
                if m := _TOO_EARLY.search(line):
                    self.broken_tasks.add(m.group(1))
                if self._after_went_wrong and not self.import_error:
                    self.import_error = line[:400]
                self._after_went_wrong = line.startswith("* What went wrong")
                self.import_log.append(line[:300])
                del self.import_log[:-40]
                self.last_activity = line[:120]
        elif method == "intellij/workspaceImportState" and params.get("phase") == "FINISHED":
            statuses = {f.get("status") for f in params.get("folders") or []}
            self.import_status = "SUCCESS" if statuses <= {"SUCCESS"} else ", ".join(sorted(str(s) for s in statuses))
            self.import_done.set()

    # ---- lifecycle

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def initialize(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())
        uri = uri_of(self.root)
        result = await self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": uri,
                "workspaceFolders": [{"uri": uri, "name": self.root.name}],
                "clientInfo": {"name": "ide-mcp"},
                "capabilities": {
                    "workspace": {
                        "workspaceFolders": True,
                        "configuration": True,
                        "symbol": {},
                        "workspaceEdit": {"documentChanges": True, "resourceOperations": ["create", "rename", "delete"]},
                        "didChangeWatchedFiles": {"dynamicRegistration": True},
                    },
                    "textDocument": {
                        "synchronization": {},
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {},
                        "references": {},
                        "rename": {"prepareSupport": True},
                        "diagnostic": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    },
                    "window": {"workDoneProgress": True},
                },
            },
            timeout=120,
        )
        self.capabilities = (result or {}).get("capabilities") or {}
        self.notify("initialized", {})

    async def wait_ready(self, timeout: float) -> None:
        """Wait until the project is imported and indexing has gone quiet."""
        deadline = time.time() + timeout
        while not self.import_done.is_set():
            # A folder that is not a Gradle/Maven project never reports an import; treat a server
            # that has been idle for a while as ready.
            if time.time() - self.started > 45 and not self.progress and time.time() - self.quiet_since > 10:
                break
            if time.time() > deadline:
                raise LspError(
                    f"The Kotlin language server is still importing {self.root} ({self.last_activity}). "
                    "It keeps going in the background; call again in a minute."
                )
            await asyncio.sleep(0.25)
        if not self.alive:
            raise LspError("The Kotlin language server exited while importing the project." + self._log_hint())
        settle = time.time() + 60
        while (self.progress or time.time() - self.quiet_since < 1.5) and time.time() < min(settle, deadline):
            await asyncio.sleep(0.2)

    def _log_hint(self) -> str:
        if self.import_error:
            return f"\nGradle said: {self.import_error}"
        tail = "\n".join(self.import_log[-6:])
        return f"\nImport log:\n{tail}" if tail else ""

    def unreliable(self) -> str:
        """Why code-aware results for this project cannot be trusted, or '' when they can.

        A failed or partial import leaves the server answering from an incomplete model: it
        finds only some references and reports no problems, without saying so."""
        modules = sorted({t.rsplit(":", 1)[0] or ":" for t in self.broken_tasks})
        known = (
            " This is a known defect of the language server's Android import for modules that enable "
            "buildConfig, viewBinding or dataBinding (https://github.com/Kotlin/kotlin-lsp/issues/253)."
        )
        if self.import_status and self.import_status != "SUCCESS":
            return (
                f"the Kotlin language server could not import {self.root} (status {self.import_status})."
                + (known if self.broken_tasks else self._log_hint())
            )
        if modules:
            return f"the Kotlin language server imported {self.root} without the sources of {', '.join(modules)}." + known
        return ""

    async def stop(self) -> None:
        if self.alive:
            try:
                await self.request("shutdown", None, timeout=5)
                self.notify("exit", None)
                await asyncio.wait_for(self.proc.wait(), 5)
            except (LspError, asyncio.TimeoutError, ConnectionError):
                pass
        if self.alive:
            await _terminate(self.proc)
        if self._reader:
            self._reader.cancel()

    # ---- keeping the server's view of files current

    def _scan(self) -> dict[str, float]:
        out = {}
        for f in iter_files(self.root):
            if f.suffix in SOURCE_SUFFIXES:
                try:
                    out[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        return out

    async def sync(self) -> None:
        """Tell the server about files that changed on disk since the last call. Agents edit
        files behind its back, so nothing else would."""
        current = await asyncio.to_thread(self._scan)
        if self._mtimes is not None:
            events = []
            for p, m in current.items():
                if p not in self._mtimes:
                    events.append({"uri": uri_of(Path(p)), "type": 1})
                elif self._mtimes[p] != m:
                    events.append({"uri": uri_of(Path(p)), "type": 2})
            events += [{"uri": uri_of(Path(p)), "type": 3} for p in self._mtimes if p not in current]
            if events:
                self.notify("workspace/didChangeWatchedFiles", {"changes": events})
        self._mtimes = current
        for uri, (version, text) in list(self._open.items()):
            path = path_of(uri)
            try:
                disk = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                del self._open[uri]
                continue
            if disk != text:
                self._open[uri] = (version + 1, disk)
                self.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": uri, "version": version + 1}, "contentChanges": [{"text": disk}]},
                )

    def open(self, path: Path) -> str:
        """Make sure the server has `path` open with its current text; returns the text."""
        uri = uri_of(path)
        if uri in self._open:
            return self._open[uri][1]
        text = path.read_bytes().decode("utf-8", "replace")
        language = "java" if path.suffix == ".java" else "kotlin"
        self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language, "version": 1, "text": text}})
        self._open[uri] = (1, text)
        for old in list(self._open)[:-MAX_OPEN_DOCS]:
            self.notify("textDocument/didClose", {"textDocument": {"uri": old}})
            del self._open[old]
        return text

    def forget_open(self) -> None:
        """Close every document, e.g. after files were rewritten or renamed on disk."""
        for uri in list(self._open):
            try:
                self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            except LspError:
                break
        self._open.clear()


_sessions: dict[str, Session] = {}
_start_locks: dict[str, asyncio.Lock] = {}
_reaper: asyncio.Task | None = None


async def _reap_idle() -> None:
    while True:
        await asyncio.sleep(60)
        for key, s in list(_sessions.items()):
            if not s.alive or (time.time() - s.last_used > IDLE_SECONDS and not s.lock.locked()):
                _sessions.pop(key, None)
                await s.stop()


async def session_for(root: Path, timeout: float = 600.0, on_status: Callable[[str], None] | None = None) -> Session:
    """The language server for `root`, started and imported if necessary."""
    global _reaper
    launcher = find_server()
    if launcher is None:
        raise LspError(NOT_INSTALLED)
    key = path_key(root)
    async with _start_locks.setdefault(key, asyncio.Lock()):
        s = _sessions.get(key)
        if s is None or not s.alive:
            system = cache_dir() / "lsp" / hashlib.sha256(key.encode()).hexdigest()[:16]
            system.mkdir(parents=True, exist_ok=True)
            argv = [str(launcher), "--stdio", f"--system-path={system}"]
            if WINDOWS and launcher.suffix.lower() in (".bat", ".cmd"):
                argv = ["cmd.exe", "/c", *argv]
            kwargs: dict[str, Any] = {"start_new_session": True} if not WINDOWS else {}
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(root),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=16 * 1024 * 1024,
                    **kwargs,
                )
            except OSError as e:
                raise LspError(f"Could not start the Kotlin language server at {launcher}: {e}") from None
            s = Session(root=root, proc=proc)
            _sessions[key] = s
            try:
                await s.initialize()
            except LspError:
                _sessions.pop(key, None)
                await s.stop()
                raise
            if _reaper is None or _reaper.done():
                _reaper = asyncio.create_task(_reap_idle())
    s.last_used = time.time()
    waiter = asyncio.create_task(s.wait_ready(timeout))
    while not waiter.done():
        if on_status:
            on_status(f"Kotlin language server: {s.last_activity}")
        await asyncio.wait([waiter], timeout=2)
    await waiter
    return s


def status() -> list[str]:
    return [
        f"{s.root}: {'running' if s.alive else 'exited'}, import {s.import_status or 'in progress'}, idle {time.time() - s.last_used:.0f}s"
        for s in _sessions.values()
    ]
