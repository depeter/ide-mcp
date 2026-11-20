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

from .common import WINDOWS, Blocked, Identity, OutcomeError, ToolError, _terminate, active, identity, iter_files, path_key
from .guard import SEMANTIC

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

    scope = active()

    def entry(path: Path) -> FileChange:
        path = scope.check(path)  # every file the edit names is validated before any is read
        key = path_key(path)
        if key not in changes:
            try:
                text = scope.read_bytes(path).decode("utf-8") if path.is_file() else None
            except UnicodeDecodeError:
                raise ToolError(f"{path} is not UTF-8 text, so it cannot be edited without changing its encoding. Nothing was changed.") from None
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
            c.new_path = scope.check(path_of(change["newUri"]))
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


def _sha(text: str | None) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else "-"


def revision(change: FileChange) -> str:
    """Short identifier of the on-disk content an edit was computed against."""
    return _sha(change.old_text)[:12]


def plan_digest(changes: list[FileChange], *binding: str) -> str:
    """Identifier of one exact edit set: what it is bound to (project, symbol, names), every
    affected file's content before and after, and every file move. Two plans share it only if
    they would write the same bytes over the same bytes, so it changes when a file is edited,
    the backend resolves the symbol differently, or the project behind the path is another one."""
    h = hashlib.sha256()
    for part in binding:
        h.update(part.encode("utf-8") + b"\0")
    for c in sorted(changes, key=lambda c: path_key(c.path)):
        for part in (path_key(c.path), _sha(c.old_text), _sha(c.new_text), path_key(c.new_path) if c.new_path else "-"):
            h.update(part.encode("utf-8") + b"\0")
    return h.hexdigest()[:16]


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def write_changes(changes: list[FileChange]) -> None:
    """Apply planned changes.

    Before the first write, every source and destination is validated against the request
    scope, every file is re-read from disk and compared with the text its edits were computed
    against, and every move destination is checked for a collision. Any failure there means
    nothing at all is written.

    The writes themselves are one file at a time. If one fails, the earlier ones stay: the
    error lists what was and was not written. There is no rollback."""
    scope = active()
    for c in changes:
        path = scope.check(c.path)
        if c.old_text is None:
            current: bytes | None = None if not os.path.lexists(path) else b""
        else:
            try:
                current = scope.read_bytes(path)
            except OSError:
                current = None
        if (c.old_text is None) != (current is None) or (c.old_text is not None and current != c.old_text.encode("utf-8")):
            raise Blocked("stale_preview", f"{path} changed on disk after the edit set was computed. Nothing was written.", "input")
        if c.new_path and c.new_path != c.path:
            target = scope.check(c.new_path)
            if os.path.lexists(target) and not _same_file(path, target):
                raise Blocked("destination_collision", f"{target} already exists, so {path.name} cannot be moved there. Nothing was written.", "input")
    done: list[str] = []
    try:
        for c in changes:
            if c.new_text is None:
                scope.entry(c.path).unlink(missing_ok=True)
                done.append(f"deleted {c.path}")
                continue
            if c.old_text != c.new_text:
                scope.write_bytes(c.path, c.new_text.encode("utf-8"))
                done.append(f"edited {c.path}")
            if c.new_path and c.new_path != c.path:
                scope.make_dirs(c.new_path.parent)
                scope.entry(c.path).rename(scope.entry(c.new_path))
                done.append(f"moved {c.path} -> {c.new_path}")
    except Exception as e:
        pending = [str(c.path) for c in changes if not any(str(c.path) in d for d in done)]
        partly = [str(c.path) for c in changes if any(str(c.path) in d for d in done) and c.new_path and c.new_path != c.path and not any(d.startswith(f"moved {c.path} ") for d in done)]
        raise OutcomeError(
            "failed",
            f"Applying the edit set failed part-way: {e}\n"
            "Written: " + ("; ".join(done) if done else "nothing") + "\n"
            "Not written: " + ("; ".join(pending + [f"move of {q}" for q in partly]) if pending or partly else "nothing") + "\n"
            "The files already written were not rolled back; the project is in a partly renamed state.",
            category="policy" if isinstance(e, Blocked) else "environment",
            reason="apply_failed",
        ) from None


# ----------------------------------------------------------------------------- client


class LspError(Blocked):
    """A code-aware operation that could not be carried out. It is never answered by text
    matching instead; the reason code says what is missing."""

    def __init__(self, message: str, reason: str = "semantic_unavailable", category: str = "environment"):
        super().__init__(reason, message, category)


# Requests that manage the server process rather than ask about source code.
_LIFECYCLE = ("initialize", "shutdown")
# Which advertised capability each source request depends on.
_PROVIDERS = {
    "textDocument/hover": "hoverProvider",
    "textDocument/definition": "definitionProvider",
    "textDocument/references": "referencesProvider",
    "textDocument/rename": "renameProvider",
    "textDocument/diagnostic": "diagnosticProvider",
    "workspace/symbol": "workspaceSymbolProvider",
    "textDocument/implementation": "implementationProvider",
    "textDocument/prepareCallHierarchy": "callHierarchyProvider",
    "callHierarchy/incomingCalls": "callHierarchyProvider",
    "callHierarchy/outgoingCalls": "callHierarchyProvider",
}


_TOO_EARLY = re.compile(r"before task '(:[^']+)' has completed is not supported")

# ---- workaround for Kotlin/kotlin-lsp#253
#
# The importer reads the generated-source directories of Android test components before the
# tasks producing them have run, which fails the whole import. Turning those components off for
# the import avoids it, at the price of their sources being invisible to the language server.
# The script reaches Gradle only through the server's own init-script property, so ordinary
# builds never see it.

WORKAROUND_ENV = "KOTLIN_LSP_ANDROID_WORKAROUND"  # "0" disables it
INIT_SCRIPTS_PROPERTY = "com.jetbrains.ls.imports.gradle.init.scripts"
_COMPONENT_SOURCES = {"androidTest": "src/androidTest", "unitTest": "src/test"}
_DISABLE = {
    "androidTest": (
        "try { variant.enableAndroidTest = false } catch (Throwable e1) {\n"
        "                        try { variant.androidTest.enable = false } catch (Throwable e2) {}\n"
        "                    }"
    ),
    "unitTest": (
        "try { variant.enableUnitTest = false } catch (Throwable e1) {\n"
        "                        try { variant.hostTests.values().each { it.enable = false } } catch (Throwable e2) {}\n"
        "                    }"
    ),
}


def workaround_enabled() -> bool:
    return os.environ.get(WORKAROUND_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def components_to_disable(broken_tasks: set[str]) -> frozenset[str]:
    """Which Android test components the tasks named in a failed import belong to."""
    out = set()
    for task in broken_tasks:
        name = task.rsplit(":", 1)[-1]
        if "AndroidTest" in name:
            out.add("androidTest")
        elif "UnitTest" in name:
            out.add("unitTest")
    return frozenset(out)


def init_script_text(components: frozenset[str]) -> str:
    body = "\n                    ".join(_DISABLE[c] for c in sorted(components))
    return f"""// Written by ide-mcp for the Kotlin language server's project import only.
// Works around https://github.com/Kotlin/kotlin-lsp/issues/253 by leaving out: {", ".join(sorted(components))}
gradle.allprojects {{ project ->
    ['com.android.application', 'com.android.library', 'com.android.dynamic-feature'].each {{ id ->
        project.pluginManager.withPlugin(id) {{
            def components = project.extensions.findByName('androidComponents')
            if (components != null) {{
                components.beforeVariants(components.selector().all()) {{ variant ->
                    {body}
                }}
            }}
        }}
    }}
}}
"""


def _read_marker(system: Path) -> frozenset[str]:
    try:
        names = (system / "ide-mcp-import-workaround").read_text(encoding="utf-8").split(",")
    except OSError:
        return frozenset()
    return frozenset(n.strip() for n in names if n.strip() in _DISABLE)


def _write_marker(system: Path, components: frozenset[str]) -> None:
    (system / "ide-mcp-import-workaround").write_text(",".join(sorted(components)), encoding="utf-8")


@dataclass
class Session:
    """A running language server for one project root."""

    root: Path
    proc: asyncio.subprocess.Process
    ident: Identity | None = None  # the directory the process was actually started in
    dynamic: set[str] = field(default_factory=set)  # methods the server registered after initialize
    started: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    import_done: asyncio.Event = field(default_factory=asyncio.Event)
    import_status: str = ""
    import_log: list[str] = field(default_factory=list)
    import_error: str = ""  # Gradle's "What went wrong" for a failed import
    broken_tasks: set[str] = field(default_factory=set)  # tasks whose module imported without sources
    _after_went_wrong: bool = False
    workaround: frozenset[str] = frozenset()  # Android test components left out of the import
    init_script: Path | None = None
    progress: dict[str, str] = field(default_factory=dict)
    _begun: dict[str, float] = field(default_factory=dict)  # when each open progress token started
    _imported_at: float = 0.0
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

    def supports(self, method: str) -> bool:
        provider = _PROVIDERS.get(method)
        return provider is None or bool(self.capabilities.get(provider)) or method in self.dynamic

    def verify(self, method: str) -> None:
        """Checked before every source request: the request's scope covers this session's
        project, the directory is still the one the process was started in, and the server
        offers the operation. A session bound elsewhere is never sent the query."""
        scope = active(SEMANTIC)
        try:
            scope.check_project(self.root)
            current = identity(self.root)
        except OSError:
            current = None
        except Blocked as e:
            raise LspError(f"This language server session is bound to {self.root}, not to the request's scope: {e.detail}", "semantic_session_mismatch", "policy") from None
        if self.ident is None or current != self.ident:
            raise LspError(
                f"The language server session for {self.root} was started for a different directory than the one at that path now. "
                "It was not queried; the next call starts a new session.",
                "semantic_session_mismatch", "policy",
            )
        if not self.supports(method):
            raise LspError(
                f"The Kotlin language server does not offer {method} (no {_PROVIDERS[method]} capability), so this operation is unsupported. "
                "Nothing was changed, and no text match was substituted.",
                "unsupported_operation",
            )

    def state(self, ident: Identity | None) -> tuple[str, str | None, str | None]:
        """Readiness of this session for the project `ident`, from what it has already reported."""
        if not self.alive:
            return "unavailable", "semantic_backend_exited", "The Kotlin language server process has exited."
        if self.ident is None or ident is None or self.ident != ident:
            return "unavailable", "semantic_session_mismatch", f"The running session is bound to {self.root}, not to this project."
        if not self.import_done.is_set() or self.busy():
            return "indexing", "semantic_indexing", f"The Kotlin language server is still importing or indexing ({self.last_activity})."
        if why := self.unreliable():
            return "unavailable", "semantic_import_failed", why
        if self.import_status == "SUCCESS":
            return "ready", None, None
        return (
            "unknown",
            "semantic_readiness_unknown",
            "The Kotlin language server did not report a finished project import, so its answers cannot be trusted to be complete.",
        )

    async def request(self, method: str, params: Any, timeout: float = 120.0) -> Any:
        if method not in _LIFECYCLE:
            self.verify(method)
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
                    error = msg["error"] if isinstance(msg["error"], dict) else {"message": msg["error"]}
                    unsupported = error.get("code") == -32601  # JSON-RPC MethodNotFound
                    fut.set_exception(LspError(
                        f"Kotlin language server: {error.get('message', error)}",
                        "unsupported_operation" if unsupported else "semantic_request_failed",
                    ))
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
            elif method == "client/registerCapability":
                self.dynamic.update(str(r.get("method")) for r in params.get("registrations") or [] if isinstance(r, dict))
            elif method == "client/unregisterCapability":
                self.dynamic.difference_update(str(r.get("method")) for r in params.get("unregisterations") or [] if isinstance(r, dict))
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LspError:
                pass
            return
        if method == "$/progress":
            value, token = params.get("value") or {}, str(params.get("token"))
            if value.get("kind") == "begin":
                self.progress[token] = value.get("title") or "working"
                self._begun[token] = time.time()
                self.last_activity = self.progress[token]
            elif value.get("kind") == "end":
                self.progress.pop(token, None)
                self._begun.pop(token, None)
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
            self._imported_at = time.time()
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
                    "It keeps going in the background; call again in a minute.",
                    "semantic_indexing",
                )
            await asyncio.sleep(0.25)
        if not self.alive:
            raise LspError("The Kotlin language server exited while importing the project." + self._log_hint(), "semantic_backend_exited")
        settle = time.time() + 60
        while (self.busy() or time.time() - self._imported_at < 2.0) and time.time() < min(settle, deadline):
            await asyncio.sleep(0.2)

    def busy(self) -> bool:
        """Whether the server is in the middle of sustained background work. kotlin-lsp also
        sends a steady stream of progress tokens that begin and end in the same instant, long
        after indexing has finished; those are not work in progress, so only a token that has
        stayed open counts."""
        now = time.time()
        return any(now - begun > 1.0 for begun in self._begun.values())

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
        if self.workaround:
            known += f" Leaving out {' and '.join(sorted(self.workaround))} components did not get around it."
        elif self.broken_tasks and not workaround_enabled():
            known += f" The workaround for it is switched off ({WORKAROUND_ENV})."
        if self.import_status and self.import_status != "SUCCESS":
            return (
                f"the Kotlin language server could not import {self.root} (status {self.import_status})."
                + (known if self.broken_tasks else self._log_hint())
            )
        if modules:
            return f"the Kotlin language server imported {self.root} without the sources of {', '.join(modules)}." + known
        return ""

    def caveat(self) -> str:
        """A note for results when part of the project was deliberately left out of the import."""
        hidden = set(self.workaround)
        # Say nothing about instrumented tests in a project that has none.
        if not any(self.left_out(Path(p)) for p in self._mtimes or {}):
            hidden.discard("androidTest")
        if not hidden:
            return ""
        dirs = " and ".join(_COMPONENT_SOURCES[c] for c in sorted(hidden))
        return (
            f"[Android modules were imported without {dirs} (workaround for Kotlin/kotlin-lsp#253): "
            "code there is not analysed, searched or renamed]\n"
        )

    def left_out(self, path: Path) -> bool:
        """Whether `path` is an Android instrumented-test source the import workaround dropped."""
        if "androidTest" not in self.workaround:
            return False
        parts = path.parts
        return any(a == "src" and b.startswith("androidTest") for a, b in zip(parts, parts[1:]))

    def write_init_script(self) -> None:
        """(Re)create the import init script. The server deletes it after every import, and a
        later re-import (a build file changed) needs it again."""
        if self.init_script is None or self.init_script.exists():
            return
        try:
            self.init_script.parent.mkdir(parents=True, exist_ok=True)
            self.init_script.write_text(init_script_text(self.workaround), encoding="utf-8")
        except OSError:
            pass

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
        self.write_init_script()
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
        scope = active(SEMANTIC)
        for uri, (version, text) in list(self._open.items()):
            path = path_of(uri)
            try:
                disk = scope.read_bytes(path).decode("utf-8", "replace")
            except (OSError, ToolError):
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
        path = active(SEMANTIC).check(path)
        uri = uri_of(path)
        if uri in self._open:
            return self._open[uri][1]
        text = active().read_bytes(path).decode("utf-8", "replace")
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


def session_key(root: Path) -> str:
    """Sessions are kept per resolved project directory, so two worktrees never share one and
    two spellings of the same directory do."""
    return identity(root).path


async def _discard(key: str, s: Session, *, quietly: bool) -> None:
    """Drop a session. `quietly` stops the process without sending it anything, for a session
    that turned out to belong to a different directory."""
    if _sessions.get(key) is s:
        _sessions.pop(key, None)
    if quietly:
        if s.alive:
            await _terminate(s.proc)
        if s._reader:
            s._reader.cancel()
    else:
        await s.stop()


async def session_for(root: Path, timeout: float = 600.0, on_status: Callable[[str], None] | None = None) -> Session:
    """The language server for the verified project `root`, started and imported if necessary.

    A stored session is reused only if it is alive and still bound to this very directory;
    after a restart, a replaced backend or a directory swapped under the same path, a new one
    is started and its readiness established afresh."""
    global _reaper
    launcher = find_server()
    if launcher is None:
        raise LspError(NOT_INSTALLED, "semantic_not_installed")
    root = active(SEMANTIC).check_project(root)
    bound = identity(root)
    key = bound.path
    system = cache_dir() / "lsp" / hashlib.sha256(key.encode()).hexdigest()[:16]
    deadline = time.time() + timeout
    while True:
        async with _start_locks.setdefault(key, asyncio.Lock()):
            s = _sessions.get(key)
            if s is not None and s.alive and s.ident != bound:
                await _discard(key, s, quietly=True)
                s = None
            if s is None or not s.alive:
                s = await _start(root, launcher, system)
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
        waiter = asyncio.create_task(s.wait_ready(max(deadline - time.time(), 1.0)))
        while not waiter.done():
            if on_status:
                on_status(f"Kotlin language server: {s.last_activity}")
            await asyncio.wait([waiter], timeout=2)
        await waiter
        # Kotlin/kotlin-lsp#253: import again without the test components that broke it. The
        # choice is remembered per project, so this double import happens once.
        extra = components_to_disable(s.broken_tasks) - s.workaround
        if not extra or not workaround_enabled():
            return s
        _write_marker(system, s.workaround | extra)
        if on_status:
            on_status(f"Kotlin language server: importing again without {', '.join(sorted(extra))} components")
        await _discard(key, s, quietly=False)


async def _start(root: Path, launcher: Path, system: Path) -> Session:
    root = active(SEMANTIC).launch_dir(root)  # verified again at the point of launch
    system.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    workaround = _read_marker(system) if workaround_enabled() else frozenset()
    script: Path | None = None
    if workaround:
        script = system / "ide-mcp-import.gradle"
        option = f"-D{INIT_SCRIPTS_PROPERTY}={script}"
        if any(ch.isspace() for ch in option):
            # The launcher splits IJ_JAVA_OPTIONS on whitespace. The server resolves a relative
            # path against its working directory, the project root, whose .gradle is scratch space.
            script = root / ".gradle" / "ide-mcp-import.gradle"
            option = f"-D{INIT_SCRIPTS_PROPERTY}=.gradle/ide-mcp-import.gradle"
        env["IJ_JAVA_OPTIONS"] = " ".join(filter(None, [env.get("IJ_JAVA_OPTIONS", "").strip(), option]))
    argv = [str(launcher), "--stdio", f"--system-path={system}"]
    if WINDOWS and launcher.suffix.lower() in (".bat", ".cmd"):
        argv = ["cmd.exe", "/c", *argv]
    kwargs: dict[str, Any] = {"start_new_session": True} if not WINDOWS else {}
    s = Session(root=root, proc=None, ident=identity(root), workaround=workaround, init_script=script)  # type: ignore[arg-type]
    s.write_init_script()
    try:
        s.proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
            **kwargs,
        )
    except OSError as e:
        raise LspError(f"Could not start the Kotlin language server at {launcher}: {e}") from None
    return s


def semantic_state(root: Path) -> tuple[str, str | None, str | None]:
    """What is known right now about code-aware analysis for `root`, without starting, importing
    or querying anything. An installed language server is not evidence of readiness: until a
    session for this project has reported its import, the state is `unknown`."""
    if find_server() is None:
        return "unavailable", "semantic_not_installed", NOT_INSTALLED
    try:
        bound = identity(root)
    except OSError:
        return "unavailable", "semantic_unavailable", f"{root} does not exist."
    s = _sessions.get(bound.path)
    if s is None or not s.alive:
        return (
            "unknown",
            "semantic_not_started",
            "No language server session is running for this project. The first code-aware call starts one and "
            "imports the project; only then is its state known.",
        )
    return s.state(bound)


def status() -> list[str]:
    return [
        f"{s.root}: {'running' if s.alive else 'exited'}, import {s.import_status or 'in progress'}, idle {time.time() - s.last_used:.0f}s"
        for s in _sessions.values()
    ]
