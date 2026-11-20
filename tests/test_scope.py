"""Target enforcement: every handler stays inside the scope the caller selected, and a wrong
target is rejected before anything is read, written or launched."""

from __future__ import annotations

import asyncio
import ntpath
import os
import posixpath
from pathlib import Path

import pytest

from conftest import launched, make_gradle_project, run


def blocked(reason: str, coro) -> str:
    from ide_mcp.common import Blocked

    with pytest.raises(Blocked) as info:
        run(coro)
    assert info.value.reason == reason, str(info.value)
    assert f"[outcome=blocked reason={reason} " in str(info.value)
    return str(info.value)


def door(root: Path) -> Path:
    return root / "app" / "src" / "main" / "kotlin" / "Door.kt"


# ----------------------------------------------------------------------------- two worktrees


def _wrong_target_calls(a: Path, b: Path):
    """One call per handler, with worktree A as the expected scope and a target in worktree B."""
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    A, B, target = str(a), str(b), str(door(b))
    return {
        "read_file": lambda: fs.read_file(target, expected_root=A),
        "read_file#ranged": lambda: fs.read_file(target, start_line=2, max_lines=1, expected_root=A),
        "list_directory_tree": lambda: fs.list_directory_tree(B, expected_root=A),
        "find_files": lambda: fs.find_files(B, glob="*.kt", expected_root=A),
        "search_in_files": lambda: fs.search_in_files("Door", B, expected_root=A),
        "create_file": lambda: fs.create_file(str(b / "new.txt"), "x", expected_root=A),
        "replace_text": lambda: fs.replace_text(target, "Door", "Gate", replace_all=True, expected_root=A),
        "move_path#source": lambda: fs.move_path(target, str(a / "Moved.kt"), expected_root=A),
        "move_path#destination": lambda: fs.move_path(str(door(a)), str(b / "Moved.kt"), expected_root=A),
        "delete_path": lambda: fs.delete_path(target, expected_root=A),
        "git_status": lambda: fs.git_status(B, expected_root=A),
        "run_command": lambda: fs.run_command("echo hi", cwd=B, expected_root=A),
        "readiness#files": lambda: fs.readiness(expected_root=A, path=B),
        "project_info": lambda: kt.project_info(B, expected_root=A),
        "gradle_tasks": lambda: kt.gradle_tasks(None, B, expected_root=A),
        "gradle_run": lambda: kt.gradle_run(None, [":app:classes"], project_dir=B, expected_root=A),
        "compile": lambda: kt.compile_sources(None, project_dir=B, expected_root=A),
        "compile#file": lambda: kt.compile_sources(None, project_dir=A, file=target, expected_root=A),
        "run_tests": lambda: kt.run_tests(None, project_dir=B, module=":app", expected_root=A),
        "detekt": lambda: kt.detekt(None, project_dir=A, files=[target], expected_root=A),
        "ktlint": lambda: kt.ktlint(None, project_dir=B, expected_root=A),
        "dependencies": lambda: kt.dependencies(None, B, expected_root=A),
        "find_symbol": lambda: kt.find_symbol("Door", B, expected_root=A),
        "file_outline": lambda: kt.file_outline(target, expected_root=A),
        "find_usages#text": lambda: kt.find_usages(None, "Door", B, engine="text", expected_root=A),
        "find_usages#auto": lambda: kt.find_usages(None, "Door", B, expected_root=A),
        "find_usages#semantic": lambda: kt.find_usages(None, "Door", A, file=target, line=3, engine="semantic", expected_root=A),
        "rename_symbol#root": lambda: kt.rename_symbol(None, "Door", "Gate", B, dry_run=False, preview_id="0" * 16, expected_root=A),
        "rename_symbol#file": lambda: kt.rename_symbol(None, "Door", "Gate", A, file=target, dry_run=False, preview_id="0" * 16, expected_root=A),
        "find_implementations": lambda: kt.find_implementations(None, "Door", A, file=target, line=3, expected_root=A),
        "find_callers": lambda: kt.find_callers(None, "open", B, expected_root=A),
        "find_callees": lambda: kt.find_callees(None, "open", A, file=target, line=5, expected_root=A),
        "symbol_info": lambda: kt.symbol_info(None, target, 3, name="Door", expected_root=A),
        "file_problems": lambda: kt.file_problems(None, target, expected_root=A),
        "readiness#kotlin": lambda: kt.readiness(expected_root=A, project_dir=B),
    }


def test_wrong_worktree_is_rejected_before_any_access(worktrees, launches, effects, monkeypatch):
    """Two worktrees at the same commit: A is the expected scope, the request targets B."""
    from ide_mcp import lsp

    a, b = worktrees
    monkeypatch.setattr(lsp, "find_server", lambda: Path("kotlin-lsp-launcher"))
    before = sorted(p.relative_to(b).as_posix() for p in b.rglob("*"))
    text_before = door(b).read_bytes()
    effects.events.clear()

    calls = _wrong_target_calls(a, b)
    for name, call in calls.items():
        message = blocked("target_mismatch", call())
        assert str(b) in message, name
        touched = effects.touching(b, "open", "write", "launch", "walk")
        assert touched == [], f"{name} touched the wrong worktree: {touched}"

    assert launched(launches) == []  # Gradle was never started, in either worktree
    assert sorted(p.relative_to(b).as_posix() for p in b.rglob("*")) == before
    assert door(b).read_bytes() == text_before
    assert door(a).exists()


def test_every_registered_tool_is_covered_by_the_wrong_worktree_test(worktrees):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    covered = {name.split("#")[0] for name in _wrong_target_calls(*worktrees)}
    registered = set(fs.ops.registered) | set(kt.ops.registered)
    assert registered <= covered, registered - covered


def test_backend_that_resolves_the_other_worktree_is_rejected(worktrees, launches, effects, monkeypatch):
    """The request names A, but project discovery hands back B: the resolved project is what
    gets checked, not the path the caller supplied."""
    from ide_mcp import gradle
    from ide_mcp import kotlin_server as kt

    a, b = worktrees
    real_find = gradle.find_project
    monkeypatch.setattr(gradle, "find_project", lambda path, **kw: real_find(str(b)))
    effects.events.clear()
    for call in (
        lambda: kt.compile_sources(None, project_dir=str(a), expected_root=str(a)),
        lambda: kt.gradle_run(None, [":app:classes"], project_dir=str(a), expected_root=str(a)),
        lambda: kt.project_info(str(a), expected_root=str(a)),
    ):
        blocked("target_mismatch", call())
    assert launched(launches) == []
    assert effects.touching(b, "open", "write", "launch") == []


def test_shared_parent_is_not_a_project_scope(worktrees, launches):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    dev = a.parent
    blocked("target_mismatch", kt.compile_sources(None, project_dir=str(a), expected_root=str(dev)))
    blocked("target_mismatch", kt.gradle_tasks(None, str(a), expected_root=str(dev)))
    # The same holds for file access: the parent does not identify either worktree.
    blocked("target_mismatch", fs.read_file(str(door(a)), expected_root=str(dev)))
    assert launched(launches) == []


def test_two_concurrent_worktree_requests_keep_their_own_targets(worktrees, launches):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    a, b = worktrees
    door(b).write_text("package demo\n\nclass OtherDoor\n")

    async def both():
        return await asyncio.gather(
            kt.compile_sources(None, project_dir=str(a), module=":app", expected_root=str(a)),
            kt.compile_sources(None, project_dir=str(b), module=":app", expected_root=str(b)),
            fs.read_file(str(door(a)), expected_root=str(a)),
            fs.read_file(str(door(b)), expected_root=str(b)),
            fs.read_file(str(door(b)), expected_root=str(a)),
            return_exceptions=True,
        )

    compiled_a, compiled_b, read_a, read_b, crossed = run(both())
    assert "[outcome=passed]" in compiled_a and f"[{a}]" in compiled_a
    assert "[outcome=passed]" in compiled_b and f"[{b}]" in compiled_b
    assert "class Door" in read_a and "OtherDoor" not in read_a
    assert "OtherDoor" in read_b
    assert getattr(crossed, "reason", None) == "target_mismatch"
    cwds = [cwd for cwd, args in launched(launches) if "compileDebugSources" in args]
    assert sorted(cwds) == sorted([str(a), str(b)])


# ----------------------------------------------------------------------------- valid scopes


def test_nested_build_project_and_standalone_document_folder(tmp_path, launches):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt
    from conftest import git

    repo = tmp_path / "dev" / "mobile"
    nested = make_gradle_project(repo / "android")
    sibling = make_gradle_project(tmp_path / "dev" / "mobile-copy" / "android")
    git(repo, "init", "-q", "-b", "main")
    repo, nested = Path(os.path.realpath(repo)), Path(os.path.realpath(nested))

    # Worktree root as scope, build root nested inside it; found from the repo root downwards.
    out = run(kt.compile_sources(None, project_dir=str(repo), module=":app", expected_root=str(repo)))
    assert "[outcome=passed]" in out and f"[{nested}]" in out
    # The nested build root itself is an equally valid scope.
    out = run(kt.gradle_tasks(None, str(nested), filter="compile", expected_root=str(nested)))
    assert ":app:compileDebugSources" in out
    assert {cwd for cwd, _ in launched(launches)} == {str(nested)}
    info = run(kt.project_info(expected_root=str(repo)))
    assert f"Gradle root: {nested}" in info and str(sibling) not in info

    # A ticket folder with no repository anywhere above it.
    ticket = tmp_path / "tickets" / "T-1"
    ticket.mkdir(parents=True)
    run(fs.create_file(str(ticket / "notes.md"), "line one\nline two\n", expected_root=str(ticket)))
    assert "line two" in run(fs.read_file("notes.md", expected_root=str(ticket)))  # relative to the scope
    assert "notes.md" in run(fs.find_files(expected_root=str(ticket)))
    assert "1 matching line" in run(fs.search_in_files("two", expected_root=str(ticket)))
    blocked("target_mismatch", fs.read_file(str(door(nested)), expected_root=str(ticket)))

    # A non-Git project is its own scope; its parent is not.
    plain = make_gradle_project(tmp_path / "plain" / "proj")
    assert "[outcome=passed]" in run(kt.compile_sources(None, module=":app", expected_root=str(plain)))
    blocked("target_mismatch", kt.compile_sources(None, project_dir=str(plain), expected_root=str(plain.parent)))
    blocked("invalid_scope", kt.compile_sources(None, expected_root=str(plain / "app")))


# ----------------------------------------------------------------------------- path checks


@pytest.mark.parametrize(
    "path,root,expected",
    [
        (r"C:\dev\app\src\A.kt", r"C:\dev\app", True),
        (r"c:\DEV\App\src\A.kt", r"C:\dev\app", True),  # case-insensitive volume
        (r"C:/dev/app/src/A.kt", r"C:\dev\app", True),
        (r"\\?\C:\dev\app\src\A.kt", r"C:\dev\app", True),  # extended-length alias
        (r"C:\dev\app", r"C:\dev\app", True),
        (r"C:\dev\app-other\src\A.kt", r"C:\dev\app", False),  # prefix collision
        (r"C:\dev\app2", r"C:\dev\app", False),
        (r"C:\dev\app\..\app-other\A.kt", r"C:\dev\app", False),  # traversal
        (r"D:\dev\app\src\A.kt", r"C:\dev\app", False),  # another drive
        (r"C:\dev", r"C:\dev\app", False),  # the shared parent
        (r"\\server\share\app\A.kt", r"\\server\share\app", True),
        (r"\\?\UNC\server\share\app\A.kt", r"\\server\share\app", True),
        (r"\\server\other\app\A.kt", r"\\server\share\app", False),
    ],
)
def test_windows_paths_compare_by_component(path, root, expected):
    from ide_mcp.common import within

    assert within(path, root, ntpath) is expected


def test_posix_paths_compare_by_component():
    from ide_mcp.common import within

    assert within("/dev/app/src/A.kt", "/dev/app", posixpath)
    assert not within("/dev/app-other/A.kt", "/dev/app", posixpath)
    assert not within("/dev/App/A.kt", "/dev/app", posixpath)
    assert not within("/dev/app/../app-other/A.kt", "/dev/app", posixpath)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("gradle.properties", True),
        ("LOCAL.PROPERTIES", True),
        ("local.properties.", True),  # Win32 drops trailing dots and spaces
        ("local.properties ", True),
        ("local.properties::$DATA", True),  # the default data stream of the same file
        ("local.properties:hidden", True),
        (".properties", True),
        ("properties.kt", False),
        ("local.properties.md", False),
    ],
)
def test_protected_names(name, expected):
    from ide_mcp.common import protected_name

    assert protected_name(name) is expected


def test_traversal_prefix_collision_and_link_escape(worktrees, effects):
    from ide_mcp import files_server as fs

    a, b = worktrees
    A = str(a)
    blocked("target_mismatch", fs.read_file(str(a / ".." / b.name / "settings.gradle.kts"), expected_root=A))
    blocked("target_mismatch", fs.read_file(f"../{b.name}/settings.gradle.kts", expected_root=A))
    blocked("target_mismatch", fs.create_file(str(a) + "-other/new.txt", "x", expected_root=A))  # 'app' vs 'app-other'
    assert not (b / "new.txt").exists()

    link = a / "linked"
    try:
        os.symlink(b, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    effects.events.clear()
    escaped = link / "app" / "src" / "main" / "kotlin" / "Door.kt"
    message = blocked("target_mismatch", fs.read_file(str(escaped), expected_root=A))
    assert "link or junction" in message
    blocked("target_mismatch", fs.create_file(str(link / "new.txt"), "x", expected_root=A))
    blocked("target_mismatch", fs.replace_text(str(escaped), "Door", "Gate", expected_root=A))
    blocked("target_mismatch", fs.delete_path(str(escaped), expected_root=A))
    blocked("target_mismatch", fs.run_command("echo hi", cwd=str(link), expected_root=A))
    assert effects.touching(b, "open", "write", "launch") == []
    assert not (b / "new.txt").exists()
    # Listing and searching do not walk through the link either.
    found = run(fs.find_files(expected_root=A, glob="*.kt", include_ignored=True))
    assert "linked/" not in found
    assert "linked" not in run(fs.search_in_files("class Door", expected_root=A, include_ignored=True))
    # Removing the link itself is an operation on A and leaves B alone.
    run(fs.delete_path(str(link), expected_root=A))
    assert not link.exists() and door(b).exists()


def test_target_swapped_between_check_and_open_is_rejected(worktrees, monkeypatch):
    """The directory is replaced by a link to the other worktree after validation and before
    the open: the opened handle no longer matches the validated path, so nothing is returned."""
    from ide_mcp import files_server as fs

    a, b = worktrees
    victim = a / "app" / "src" / "main" / "kotlin"
    parked = a / "parked"
    real_open = os.open
    swapped = []

    def swapping_open(path, flags, *args, **kwargs):
        if not swapped and os.fspath(path) == str(victim / "Door.kt"):
            swapped.append(path)
            os.rename(victim, parked)
            try:
                os.symlink(b / "app" / "src" / "main" / "kotlin", victim, target_is_directory=True)
            except (OSError, NotImplementedError):
                os.rename(parked, victim)
                pytest.skip("symbolic links are not available here")
        return real_open(path, flags, *args, **kwargs)

    (b / "app" / "src" / "main" / "kotlin" / "Door.kt").write_text("SECRET OF THE OTHER WORKTREE\n")
    monkeypatch.setattr(os, "open", swapping_open)
    message = blocked("target_changed", fs.read_file(str(victim / "Door.kt"), expected_root=str(a)))
    assert swapped and "SECRET" not in message


def test_write_target_swapped_after_validation_is_rejected(worktrees, monkeypatch):
    import tempfile

    from ide_mcp import files_server as fs

    a, b = worktrees
    out_dir = a / "out"
    out_dir.mkdir()
    real_mkstemp = tempfile.mkstemp

    def swapping_mkstemp(*args, **kwargs):
        os.rmdir(out_dir)
        try:
            os.symlink(b, out_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            os.mkdir(out_dir)
            pytest.skip("symbolic links are not available here")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", swapping_mkstemp)
    blocked("target_changed", fs.create_file(str(out_dir / "new.txt"), "payload", expected_root=str(a)))
    assert not (b / "new.txt").exists()
    assert [p.name for p in b.iterdir() if p.name.endswith(".tmp")] == []


# ----------------------------------------------------------------------------- protected files


def test_protected_files_and_aliases_are_never_opened(worktrees, effects, monkeypatch):
    """No property file exists on disk here: the entries are mocked, and the assertion is that
    no content-opening function is ever called for them."""
    from ide_mcp import common
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    A = str(a)
    secret = a / "local.properties"
    alias = a / "alias.txt"  # stands for a link whose target is the property file
    short = a / "LOCAL~1.PRO"  # 8.3 short name of the same file

    real_realpath = os.path.realpath

    def mocked_realpath(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        return str(secret) if resolved in (str(alias), str(short)) else resolved

    monkeypatch.setattr(os.path, "realpath", mocked_realpath)
    real_iter = common.iter_files
    listing = lambda root, include_ignored=False: [*real_iter(root, include_ignored), secret, alias]  # noqa: E731
    monkeypatch.setattr(fs, "iter_files", listing)
    monkeypatch.setattr(kt, "iter_files", listing)
    effects.events.clear()

    for spelling in (secret, a / "LOCAL.PROPERTIES", a / "local.properties.", a / "local.properties::$DATA", alias, short):
        blocked("protected_path", fs.read_file(str(spelling), expected_root=A))
        blocked("protected_path", fs.read_file(str(spelling), start_line=5, max_lines=1, expected_root=A))
        blocked("protected_path", fs.replace_text(str(spelling), "a", "b", expected_root=A))
        blocked("protected_path", fs.create_file(str(spelling), "x=1\n", overwrite=True, expected_root=A))
        blocked("protected_path", fs.move_path(str(spelling), str(a / "copy.txt"), expected_root=A))
        blocked("protected_path", fs.delete_path(str(spelling), expected_root=A))
        blocked("protected_path", kt.file_outline(str(spelling), expected_root=A))
        blocked("protected_path", kt.compile_sources(None, file=str(spelling), expected_root=A))
    blocked("protected_path", fs.move_path(str(door(a)), str(secret), expected_root=A))

    out = run(fs.search_in_files("sdk", expected_root=A, file_glob="*"))
    assert "2 protected file(s) were not opened" in out
    out = run(kt.find_usages(None, "sdk", engine="text", file_glob="*", expected_root=A))
    assert "does not appear" in out
    assert "local.properties" in run(fs.find_files(expected_root=A, name_contains="local"))  # names are not contents

    opened = [e for e in effects.of("open") if any(str(p).lower().rstrip(" .").split("::")[0].endswith((".properties", "alias.txt", "~1.pro")) for p in e[1])]
    assert opened == []
    assert not secret.exists() and not alias.exists()


def test_project_info_does_not_read_the_wrapper_properties(worktrees, effects):
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    (a / "gradle" / "wrapper").mkdir(parents=True)
    effects.events.clear()
    out = run(kt.project_info(expected_root=str(a)))
    assert f"Gradle root: {a}" in out and ":app" in out
    assert "protected" in out
    assert [e for e in effects.of("open") if any(str(p).endswith(".properties") for p in e[1])] == []


# ----------------------------------------------------------------------------- command execution


@pytest.mark.parametrize(
    "extra",
    [
        ["-p", "{b}"],
        ["-p{b}"],
        ["--project-dir", "{b}"],
        ["--project-dir={b}"],
        ["-c", "{b}/settings.gradle.kts"],
        ["--settings-file={b}/settings.gradle.kts"],
        ["-b", "{b}/build.gradle.kts"],
        ["--build-file", "{b}/build.gradle.kts"],
        ["--include-build", "{b}"],
        ["--include-build=../app-other"],
        ["-I", "{b}/init.gradle"],
        ["--init-script={b}/init.gradle"],
        ["-p"],
    ],
)
def test_gradle_arguments_cannot_redirect_the_project(worktrees, launches, extra):
    from ide_mcp import kotlin_server as kt

    a, b = worktrees
    args = [x.format(b=b) for x in extra]
    blocked("argument_redirect", kt.gradle_run(None, [":app:classes"], args, expected_root=str(a)))
    blocked("argument_redirect", kt.gradle_run(None, [":app:classes", *args], expected_root=str(a)))
    assert launched(launches) == []


def test_gradle_run_keeps_in_scope_arguments(worktrees, launches):
    from ide_mcp import kotlin_server as kt
    from ide_mcp.common import OutcomeError

    a, _b = worktrees
    out = run(kt.gradle_run(None, [":app:classes"], ["--offline", "-Pflavour=x", "-p", "app"], expected_root=str(a)))
    assert "[outcome=passed]" in out
    assert launched(launches)[-1] == (str(a), "--console=plain :app:classes --offline -Pflavour=x -p app")
    with pytest.raises(OutcomeError, match="not a task name"):
        run(kt.detekt(None, task="--project-dir=/tmp", expected_root=str(a)))


def test_shell_command_naming_another_worktree_is_rejected(worktrees, effects):
    from ide_mcp import files_server as fs

    a, b = worktrees
    effects.events.clear()
    for command in (f"git -C {b} status", f'cd "{b}" && ls', "ls ../app-other", f"cat '{door(b)}'"):
        blocked("argument_redirect", fs.run_command(command, expected_root=str(a)))
    assert effects.of("launch") == []
    out = run(fs.run_command("echo scoped", expected_root=str(a)))
    assert "[outcome=passed]" in out and "scoped" in out and f"(cwd: {a})" in out


# ----------------------------------------------------------------------------- registration


def test_handlers_cannot_be_registered_or_reach_helpers_without_checks(worktrees):
    from ide_mcp import files_server as fs
    from ide_mcp import gradle, guard, lsp
    from ide_mcp import kotlin_server as kt
    from ide_mcp.common import Blocked, iter_files, run_proc

    a, _b = worktrees
    for server in (fs, kt):
        listed = {t.name for t in run(server.mcp.list_tools())}
        assert listed == set(server.ops.registered)
        server.ops.verify()
        with pytest.raises(RuntimeError, match="ops.tool"):
            server.mcp.tool()(lambda: None)
        with pytest.raises(RuntimeError, match="ops.tool"):
            server.mcp.add_tool(lambda: None, name="legacy_alias")
        with pytest.raises(TypeError, match="expected_root"):
            server.ops.tool(kind="file", needs=(guard.TARGET,))(lambda path: None)
        with pytest.raises(ValueError, match="prerequisite"):
            server.ops.tool(kind="execution", needs=())(lambda expected_root=None: None)
        assert {t.name for t in run(server.mcp.list_tools())} == listed
        for tool in run(server.mcp.list_tools()):
            assert "expected_root" in tool.inputSchema["properties"], tool.name

    # Outside a registered operation the shared helpers refuse to touch anything.
    for call in (
        lambda: iter_files(a),
        lambda: run(run_proc(["git", "status"], cwd=a)),
        lambda: run(gradle.run_gradle(gradle.find_project(str(a)), ["tasks"])),
        lambda: lsp.plan_workspace_edit({"changes": {door(a).as_uri(): []}}),
    ):
        with pytest.raises(Blocked) as info:
            call()
        assert info.value.reason == "unguarded_access"

    # A file operation that did not declare the execution prerequisite cannot launch a process.
    with guard.activate(guard.Scope.of(str(a)), needs=(guard.TARGET,)):
        with pytest.raises(Blocked) as info:
            run(run_proc(["git", "status"], cwd=a))
        assert info.value.reason == "undeclared_prerequisite"


def test_tools_reject_through_the_sdk_error_mechanism(worktrees):
    """Called the way a client calls them: the SDK marks the result as an error."""
    from mcp.server.fastmcp.exceptions import ToolError as SdkToolError

    from ide_mcp import files_server as fs

    a, b = worktrees
    with pytest.raises(SdkToolError, match=r"\[outcome=blocked reason=target_mismatch "):
        run(fs.mcp.call_tool("read_file", {"path": str(door(b)), "expected_root": str(a)}))
    with pytest.raises(SdkToolError, match=r"\[outcome=blocked reason=missing_scope "):
        run(fs.mcp.call_tool("read_file", {"path": str(door(a))}))
    content, _structured = run(fs.mcp.call_tool("read_file", {"path": str(door(a)), "expected_root": str(a)}))
    assert "class Door" in content[0].text


# ----------------------------------------------------------------------------- scope and permission


def test_missing_scope_and_permission_denial_are_explicit(worktrees, launches, effects, monkeypatch, tmp_path):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    a, b = worktrees
    monkeypatch.chdir(a)  # the start directory must never stand in for a missing scope
    effects.events.clear()
    for call in (
        lambda: fs.read_file(str(door(a))),
        lambda: fs.read_file(str(door(a)), expected_root="  "),
        lambda: fs.find_files(),
        lambda: fs.run_command("echo hi"),
        lambda: kt.compile_sources(None, project_dir=str(a)),
        lambda: kt.find_symbol("Door"),
        lambda: kt.readiness(),
    ):
        message = blocked("missing_scope", call())
        assert "expected_root" in message
    blocked("invalid_scope", fs.read_file("Door.kt", expected_root="app"))  # relative scope
    blocked("invalid_scope", fs.read_file("x", expected_root=str(a / "missing")))
    blocked("scope_too_broad", fs.find_files(expected_root=str(Path(a.anchor))))
    blocked("scope_too_broad", fs.find_files(expected_root=str(Path.home())))
    assert effects.of("open", "launch", "write") == []

    # The existing allowlist stays a permission: a scope cannot grant what it denies.
    monkeypatch.setenv("IDE_MCP_ALLOWED_ROOTS", str(b))
    blocked("permission_denied", fs.read_file(str(door(a)), expected_root=str(a)))
    blocked("permission_denied", kt.compile_sources(None, expected_root=str(a)))
    assert "class Door" in run(fs.read_file(str(door(b)), expected_root=str(b)))
    blocked("permission_denied", fs.read_file(str(door(a)), expected_root=str(b)))  # no fallback, no widening
    assert launched(launches) == []
