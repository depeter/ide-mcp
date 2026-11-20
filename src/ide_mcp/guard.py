"""Request scope, target enforcement, handler registration and readiness for both servers.

Every tool takes `expected_root`: the directory the caller selected for the request. It
restricts the request; it grants nothing (IDE_MCP_ALLOWED_ROOTS and the client's own approvals
still apply), and the server cannot know whether it matches the task the user had in mind.
What the server does guarantee is that the file, project, language-server session or working
directory a handler actually uses lies inside that directory and belongs to the same Git
worktree, and that this is checked where the access happens rather than once up front.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import inspect
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Iterator, Literal

from pydantic import BaseModel, Field

from . import __version__
from .common import (
    _ACTIVE,
    WINDOWS,
    Blocked,
    Identity,
    OutcomeError,
    ToolError,
    identity,
    is_link,
    protected_name,
    real,
    resolve,
    within,
    worktree_root,
)

# Version of the request contract (tool arguments). 2.0 made `expected_root` mandatory.
CONTRACT_VERSION = "2.0"
# Version of the readiness response, independent of the package version.
READINESS_SCHEMA_VERSION = "1.0"

ExpectedRoot = Annotated[
    str | None,
    Field(
        description="Required. Absolute path of the directory this request is confined to: the selected Git "
        "worktree (or a directory inside it), the project root of a non-Git project, or a standalone document "
        "folder. Every path the tool uses must resolve inside it and belong to the same worktree; a shared parent "
        "of several checkouts is rejected. Relative paths and omitted directories resolve against it."
    ),
]

MISSING_SCOPE = (
    "expected_root is required (request contract 2.0). Pass the absolute path of the Git worktree, project root or "
    "document folder this request is meant for. The server does not fall back to its start directory."
)


# ----------------------------------------------------------------------------- scope


class FileTooLarge(ToolError):
    pass


@dataclass(frozen=True)
class Scope:
    """The validated expected scope of one request."""

    root: Path  # resolved
    ident: Identity
    worktree: Path | None  # the Git checkout `root` belongs to, if any
    _worktrees: dict[str, Path | None] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def of(cls, expected_root: str | None) -> "Scope":
        if expected_root is None or not str(expected_root).strip():
            raise Blocked("missing_scope", MISSING_SCOPE, "input")
        raw = Path(os.path.expanduser(str(expected_root).strip()))
        if not raw.is_absolute():
            raise Blocked("invalid_scope", f"expected_root must be an absolute path, got '{expected_root}'.", "input")
        lexical = resolve(raw)  # applies the IDE_MCP_ALLOWED_ROOTS permission
        if not lexical.is_dir():
            raise Blocked("invalid_scope", f"expected_root is not an existing directory: {lexical}", "input")
        root = real(lexical)
        home = real(Path.home())
        if len(root.parts) < 2 or within(home, root):
            raise Blocked(
                "scope_too_broad",
                f"expected_root {root} is a filesystem root or contains your home directory. Select the worktree, "
                "project root or document folder the request is for.",
            )
        return cls(root, identity(root), worktree_root(root))

    # ---- validation

    def lexical(self, target: str | os.PathLike[str]) -> Path:
        """Absolute form of a caller-supplied path; relative ones are taken from the scope."""
        p = Path(os.path.expanduser(os.fspath(target)))
        if not p.is_absolute():
            p = self.root / p
        return resolve(p)

    def _worktree_of(self, resolved: Path) -> Path | None:
        start = resolved if resolved.is_dir() else resolved.parent
        key = str(start)
        if key not in self._worktrees:
            self._worktrees[key] = worktree_root(start)
        return self._worktrees[key]

    def check(self, target: str | os.PathLike[str], *, protected: bool = True) -> Path:
        """Validate one target and return its resolved path, which is what must then be used.

        Rejects a target that resolves outside the scope (by traversal, another drive, a link or
        a junction), one that belongs to a different Git worktree than the scope, and, unless
        `protected=False` for directories and launch locations, a protected file under any of
        its spellings. Only names and directory metadata are consulted; no content is opened."""
        lexical = self.lexical(target)
        if WINDOWS and any(":" in part for part in lexical.parts[1:]):
            if protected_name(lexical.name):
                raise self._protected(lexical)
            raise Blocked("unsupported_path", f"{lexical} names an alternate data stream, which these tools do not open.")
        resolved = real(lexical)
        if not within(resolved, self.root):
            via = " through a link or junction" if within(lexical, self.root) else ""
            raise Blocked(
                "target_mismatch",
                f"{lexical} resolves to {resolved}{via}, which is outside expected_root {self.root}. Nothing was read, "
                "written or run. If that location is the intended target, pass its worktree as expected_root.",
            )
        owner = self._worktree_of(resolved)
        if owner != self.worktree:
            raise Blocked(
                "target_mismatch",
                f"{resolved} belongs to the Git worktree {owner or '(none)'}, but expected_root {self.root} identifies "
                f"{self.worktree or 'no worktree'}. Nothing was read, written or run. A directory that merely contains "
                "a checkout does not select it; pass that worktree as expected_root.",
            )
        if protected and (protected_name(lexical.name) or protected_name(resolved.name)):
            raise self._protected(lexical)
        return resolved

    @staticmethod
    def _protected(path: Path) -> Blocked:
        return Blocked(
            "protected_path",
            f"{path} is a protected file (*.properties, under any spelling or link): these tools do not read, search, "
            "edit, move or return it.",
        )

    def dir(self, target: str | os.PathLike[str] | None, what: str = "Directory") -> Path:
        """A directory inside the scope; the scope itself when none is given."""
        resolved = self.check(self.root if target is None or not str(target).strip() else target, protected=False)
        if not resolved.exists():
            raise ToolError(f"{what} does not exist: {resolved}")
        if not resolved.is_dir():
            raise ToolError(f"Not a directory: {resolved}")
        return resolved

    def launch_dir(self, cwd: str | os.PathLike[str]) -> Path:
        """Working directory for a process, verified immediately before the launch."""
        return self.dir(cwd, "Working directory")

    def file(self, target: str | os.PathLike[str]) -> Path:
        """An existing regular file inside the scope."""
        resolved = self.check(target)
        if not resolved.exists():
            raise ToolError(f"File does not exist: {resolved}")
        if resolved.is_dir():
            raise ToolError(f"{resolved} is a directory. Use list_directory_tree to see its contents.")
        return resolved

    def entry(self, target: str | os.PathLike[str]) -> Path:
        """The directory entry a move or delete acts on: validated like any target, but a final
        link is kept as the link itself, so removing it never reaches what it points at."""
        lexical = self.lexical(target)
        if real(lexical) == self.root:
            return self.root
        parent = self.check(lexical.parent, protected=False)
        if is_link(lexical):
            if protected_name(lexical.name):
                raise self._protected(lexical)
            return parent / lexical.name
        return self.check(lexical)

    def check_project(self, root: str | os.PathLike[str]) -> Path:
        """Validate the project a backend actually resolved (a Gradle root, a language-server
        workspace). Inside a Git worktree it must be that worktree, at or below the scope.
        Without Git there is no checkout identity to compare, so the scope must be the project
        root itself: a parent folder would match any sibling project equally well."""
        resolved = self.check(root, protected=False)
        if self.worktree is None and resolved != self.root:
            raise Blocked(
                "target_mismatch",
                f"The project resolved to {resolved}, but expected_root {self.root} is not inside a Git worktree and so "
                "must be the project root itself. Nothing was read, written or run.",
            )
        return resolved

    # ---- access

    def read_bytes(self, target: str | os.PathLike[str], limit: int | None = None) -> bytes:
        """Read a file. The path is validated, opened, and the open handle is then compared with
        the validated path before any byte is read, so a link or directory swapped in between
        the check and the open is caught rather than followed. With `limit`, a larger file is
        refused (FileTooLarge) without being read."""
        resolved = self.check(target)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
        try:
            fd = os.open(resolved, flags)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise self._changed(resolved) from None
            raise
        try:
            self._verify_open(fd, resolved, expect_regular=True)
            if limit is not None and os.fstat(fd).st_size > limit:
                raise FileTooLarge(f"{resolved} is larger than {limit} bytes.")
            with os.fdopen(fd, "rb") as f:
                fd = -1
                return f.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def _changed(self, resolved: Path) -> Blocked:
        return Blocked(
            "target_changed",
            f"{resolved} changed between validation and access (a link, junction or directory was swapped in). "
            "Nothing was read or written.",
        )

    def _verify_open(self, fd: int, resolved: Path, *, expect_regular: bool) -> None:
        opened = os.fstat(fd)
        again = real(resolved)
        if again != resolved or not within(again, self.root):
            raise self._changed(resolved)
        try:
            current = os.stat(again, follow_symlinks=False)
        except OSError:
            raise self._changed(resolved) from None
        if not os.path.samestat(opened, current):
            raise self._changed(resolved)
        if expect_regular and not stat.S_ISREG(opened.st_mode):
            raise ToolError(f"{resolved} is not a regular file.")

    def make_dirs(self, directory: Path) -> Path:
        """Create a directory (and missing parents) inside the scope and confirm where it landed."""
        resolved = self.check(directory, protected=False)
        if not resolved.is_dir():
            os.makedirs(resolved, exist_ok=True)
            if real(resolved) != resolved:
                raise self._changed(resolved)
        return resolved

    def write_bytes(self, target: str | os.PathLike[str], data: bytes) -> Path:
        """Replace a file's contents atomically. The destination is resolved through its existing
        parent and validated before anything is created; the temporary file is confirmed to sit
        in the validated directory before the payload is written to it."""
        resolved = self.check(target)
        parent = self.make_dirs(resolved.parent)
        mode = resolved.stat().st_mode & 0o7777 if resolved.exists() else None
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{resolved.name}.", suffix=".tmp")
        try:
            try:
                self._verify_open(fd, Path(tmp), expect_regular=True)
                if self.check(resolved) != resolved:
                    raise self._changed(resolved)
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if mode is not None:
                os.chmod(tmp, mode)
            else:
                umask = os.umask(0)
                os.umask(umask)
                os.chmod(tmp, 0o666 & ~umask)
            os.replace(tmp, resolved)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return resolved


# ----------------------------------------------------------------------------- registration

# Operation kinds.
FILE, SEMANTIC_OP, EXECUTION_OP, READINESS = "file", "semantic", "execution", "readiness"
# Prerequisites. Every operation needs SCOPE; the rest are declared per handler.
SCOPE = "scope"  # a valid expected_root
TARGET = "target_access"  # permitted access to each resolved target
PROJECT = "verified_project"  # the project the backend resolved is the one in scope
EXECUTION = "execution"  # an available execution mechanism; required to start any process
SEMANTIC = "semantic_session"  # a verified, ready language-server session and a supported operation

_REQUIRED = {FILE: (TARGET,), SEMANTIC_OP: (SEMANTIC,), EXECUTION_OP: (EXECUTION,), READINESS: ()}
_PREREQUISITES = (TARGET, PROJECT, EXECUTION, SEMANTIC)


@dataclass(frozen=True)
class Operation:
    name: str
    kind: str
    needs: tuple[str, ...]


@dataclass(frozen=True)
class _Active:
    name: str
    needs: tuple[str, ...]
    scope: Scope


@contextlib.contextmanager
def activate(scope: Scope, needs: tuple[str, ...] = _PREREQUISITES, name: str = "direct") -> Iterator[Scope]:
    """Run a block as an operation in `scope`. Handlers get this from ops.tool; it is public for
    code that drives the shared helpers directly, such as tests."""
    token = _ACTIVE.set(_Active(name, (SCOPE, *needs), scope))
    try:
        yield scope
    finally:
        _ACTIVE.reset(token)


class Operations:
    """The only way to register a tool. Each registration states the operation's kind and its
    prerequisites; the same record wraps the handler in the scope check, gates the shared
    helpers (common.active) and is what readiness reports, so the three cannot drift apart."""

    def __init__(self, mcp: Any):
        self.mcp = mcp
        self.registered: dict[str, Operation] = {}
        self._add = mcp.add_tool
        mcp.tool = self._refuse
        mcp.add_tool = self._refuse
        mcp._mcp_server.version = __version__  # serverInfo.version in standard discovery

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Register tools with ops.tool(kind=..., needs=...), which applies the request-scope checks.")

    def tool(self, *, kind: str, needs: tuple[str, ...], name: str | None = None, **options: Any) -> Callable:
        if kind not in _REQUIRED:
            raise ValueError(f"Unknown operation kind '{kind}'.")
        unknown = [n for n in needs if n not in _PREREQUISITES]
        missing = [n for n in _REQUIRED[kind] if n not in needs]
        if unknown or missing:
            raise ValueError(f"A '{kind}' operation must declare the prerequisite(s) {missing or unknown} it uses.")

        def decorator(fn: Callable) -> Callable:
            signature = inspect.signature(fn)
            if "expected_root" not in signature.parameters:
                raise TypeError(f"{fn.__name__} must take `expected_root` to be registered as a tool.")
            op = Operation(name or fn.__name__, kind, (SCOPE, *needs))

            @functools.wraps(fn)
            async def guarded(*args: Any, **kwargs: Any) -> Any:
                try:
                    scope = Scope.of(signature.bind_partial(*args, **kwargs).arguments.get("expected_root"))
                    token = _ACTIVE.set(_Active(op.name, op.needs, scope))
                    try:
                        return await fn(*args, **kwargs)
                    finally:
                        _ACTIVE.reset(token)
                except OutcomeError:
                    raise
                except ToolError as e:
                    raise OutcomeError("failed", str(e), category=e.category) from None

            self._add(guarded, name=name, **options)
            self.registered[op.name] = op
            return guarded

        return decorator

    def verify(self) -> None:
        """Fail if the server exposes a tool that did not come through tool()."""
        listed = {t.name for t in self.mcp._tool_manager.list_tools()}
        if listed != set(self.registered):
            raise RuntimeError(f"Tools registered without scope checks: {sorted(listed ^ set(self.registered))}")

    def report(
        self,
        scope: Scope,
        project_root: Path | None,
        execution: tuple[str, str | None, str | None],
        semantic: tuple[str, str | None, str | None],
    ) -> "Readiness":
        return Readiness(
            schema_version=READINESS_SCHEMA_VERSION,
            contract_version=CONTRACT_VERSION,
            server=self.mcp.name,
            server_version=__version__,
            resolved_scope=str(scope.root),
            worktree_root=str(scope.worktree) if scope.worktree else None,
            project_root=str(project_root) if project_root else None,
            execution=ExecutionState(state=execution[0], reason=execution[1], detail=execution[2]),  # type: ignore[arg-type]
            semantic=SemanticState(state=semantic[0], reason=semantic[1], detail=semantic[2]),  # type: ignore[arg-type]
            operations=[
                OperationInfo(name=op.name, kind=op.kind, requires=list(op.needs))  # type: ignore[arg-type]
                for op in sorted(self.registered.values(), key=lambda o: o.name)
            ],
        )


# ----------------------------------------------------------------------------- results


def outcome(result: str, text: str, *, category: str | None = None, reason: str | None = None) -> str:
    """Finish a build, test, command or mutation with an explicit outcome line. Only `passed`
    and a requested preview (`not_run` with reason `dry_run`) are returned as results; `failed`,
    `timed_out` and any other `not_run` are raised, which the SDK reports as a tool error."""
    if result == "passed" or (result == "not_run" and reason == "dry_run"):
        return f"[outcome={result}" + (f" reason={reason}" if reason else "") + "]\n" + text
    raise OutcomeError(result, "\n" + text.lstrip("\n"), category=category, reason=reason)


# ----------------------------------------------------------------------------- readiness


class ExecutionState(BaseModel):
    """Whether builds, tests or commands can be started for the resolved scope right now."""

    state: Literal["available", "unavailable", "unknown", "not_applicable"]
    reason: str | None = Field(None, description="Stable reason code when the state is unavailable or unknown.")
    detail: str | None = Field(None, description="Short explanation of the reason.")


class SemanticState(BaseModel):
    """Whether code-aware (language server) operations can be answered for the resolved scope."""

    state: Literal["ready", "indexing", "unavailable", "unknown", "not_applicable"]
    reason: str | None = Field(None, description="Stable reason code when the state is not ready.")
    detail: str | None = Field(None, description="Short explanation of the reason.")


class OperationInfo(BaseModel):
    name: str
    kind: Literal["file", "semantic", "execution", "readiness"]
    requires: list[str] = Field(description="Prerequisites checked inside the operation on every call.")


class Readiness(BaseModel):
    """A current observation, taken without building, importing or indexing anything. It is not
    an authorisation and not a promise: each operation checks its prerequisites again when it
    runs. Receiving it shows only that the query reached the server. There is deliberately no
    overall flag; read `execution` and `semantic` separately."""

    schema_version: str = Field(description="Version of this response contract.")
    contract_version: str = Field(description="Version of the request contract (tool arguments).")
    server: str
    server_version: str
    resolved_scope: str = Field(description="The validated expected_root actually used for this query, links resolved.")
    worktree_root: str | None = Field(description="Git worktree the scope belongs to, or null outside Git.")
    project_root: str | None = Field(description="Build project resolved inside the scope, or null if none was found.")
    execution: ExecutionState
    semantic: SemanticState
    operations: list[OperationInfo] = Field(description="Registered operations and the prerequisites each one enforces.")
