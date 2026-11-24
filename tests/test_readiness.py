"""Operation prerequisites and readiness: code-aware sessions are bound to one project,
unavailable or unverifiable prerequisites block the operation with a reason, and the readiness
query observes without doing."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from conftest import launched, run
from test_scope import blocked, door

READY_CAPABILITIES = {
    "hoverProvider": True,
    "definitionProvider": True,
    "referencesProvider": True,
    "renameProvider": {"prepareProvider": True},
    "diagnosticProvider": {},
    "workspaceSymbolProvider": True,
    "implementationProvider": True,
    "callHierarchyProvider": True,
}


class FakeProc:
    """Stands in for the language server process; nothing is started."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = -1  # never a real process: the fixture replaces lsp._terminate as well
        self.stdin = self
        self.stdout = None

    def write(self, _data: bytes) -> None:
        pass

    async def wait(self) -> int:
        return 0


def fake_session(root: Path, *, responses: dict | None = None, status: str = "SUCCESS", done: bool = True, capabilities: dict | None = None):
    """A Session bound to `root` whose transport records what is sent and answers from `responses`."""
    from ide_mcp import lsp
    from ide_mcp.common import identity

    class Recording(lsp.Session):
        def _send(self, message: dict) -> None:
            self.sent.append(message)
            if "id" in message and "method" in message:
                answer = (responses or {}).get(message["method"])
                if callable(answer):
                    answer = answer(message.get("params"))
                asyncio.get_running_loop().call_soon(self._handle, {"id": message["id"], "result": answer})

    s = Recording(root=root, proc=FakeProc(), ident=identity(root))  # type: ignore[arg-type]
    s.sent = []
    s.import_status = status
    s.capabilities = READY_CAPABILITIES if capabilities is None else capabilities
    if done:
        s.import_done.set()
    s.quiet_since = 0.0
    return s


def queries(s) -> list[str]:
    """Source requests and notifications sent to a session (lifecycle messages excluded)."""
    return [m["method"] for m in s.sent if m.get("method") not in ("initialize", "initialized", "shutdown", "exit")]


@pytest.fixture
def lsp_env(monkeypatch, launches):
    """A language server that is 'installed' but can never actually be started."""
    from ide_mcp import lsp

    started: list[Path] = []

    async def no_start(root, launcher, system):
        started.append(root)
        raise lsp.LspError("test: the language server cannot be started here")

    monkeypatch.setattr(lsp, "find_server", lambda: Path("kotlin-lsp-launcher"))
    async def fake_terminate(proc) -> None:
        proc.returncode = -15

    monkeypatch.setattr(lsp, "_start", no_start)
    monkeypatch.setattr(lsp, "_terminate", fake_terminate)
    monkeypatch.setattr(lsp, "_sessions", {})
    monkeypatch.setattr(lsp, "_start_locks", {})
    monkeypatch.setattr(lsp, "cache_dir", lambda: launches.parent / "lsp-cache")
    return started


def semantic_calls(a: Path):
    from ide_mcp import kotlin_server as kt

    A, f = str(a), str(door(a))
    return {
        "find_usages": lambda: kt.find_usages(None, "Door", file=f, line=3, engine="semantic", expected_root=A),
        "rename_symbol": lambda: kt.rename_symbol(None, "Door", "Gate", file=f, line=3, dry_run=False, preview_id="0" * 16, expected_root=A),
        "rename_symbol_auto": lambda: kt.rename_symbol(None, "Door", "Gate", file=f, line=3, engine="auto", dry_run=False, preview_id="0" * 16, expected_root=A),
        "find_implementations": lambda: kt.find_implementations(None, "Door", file=f, line=3, expected_root=A),
        "find_callers": lambda: kt.find_callers(None, "open", file=f, line=5, expected_root=A),
        "find_callees": lambda: kt.find_callees(None, "open", file=f, line=5, expected_root=A),
        "symbol_info": lambda: kt.symbol_info(None, f, 3, name="Door", expected_root=A),
        "file_problems": lambda: kt.file_problems(None, f, expected_root=A),
    }


# ----------------------------------------------------------------------------- session binding


def test_session_bound_to_another_worktree_is_never_queried(worktrees, lsp_env, effects):
    """The stored session for A's key is really bound to B: no query or edit reaches it."""
    from ide_mcp import lsp

    a, b = worktrees

    async def scenario():
        wrong = fake_session(b)
        lsp._sessions[lsp.session_key(a)] = wrong
        results = []
        for call in semantic_calls(a).values():
            lsp._sessions[lsp.session_key(a)] = wrong
            wrong.proc.returncode = None
            results.append(await asyncio.gather(call(), return_exceptions=True))
        return wrong, results

    effects.events.clear()
    wrong, results = run(scenario())
    assert wrong.sent == []  # not even a shutdown request
    for (result,) in results:
        assert getattr(result, "outcome", None) == "blocked", result
    assert lsp_env  # a fresh session for A was attempted instead of reusing the wrong one
    assert all(root == a for root in lsp_env)
    assert effects.touching(b, "open", "write") == []
    assert door(a).read_text().count("Door") == 2 and "Gate" not in door(b).read_text()


def test_session_request_checks_its_binding_every_time(worktrees, lsp_env):
    """Even handed the wrong session directly, a source request is refused before it is sent."""
    from ide_mcp import guard, lsp
    from ide_mcp.common import Blocked

    a, b = worktrees

    async def scenario():
        s = fake_session(b)
        with guard.activate(guard.Scope.of(str(a))):
            with pytest.raises(lsp.LspError) as info:
                await s.request("textDocument/references", {})
            with pytest.raises(Blocked):
                s.open(door(b))
        return s, info.value

    s, error = run(scenario())
    assert error.reason == "semantic_session_mismatch" and s.sent == []


def test_two_concurrent_worktree_requests_use_their_own_sessions(worktrees, lsp_env):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, b = worktrees

    def hover(root: Path) -> dict:
        return {"textDocument/hover": {"contents": {"value": f"class Door in {root.name}"}}, "textDocument/definition": []}

    async def scenario():
        sa, sb = fake_session(a, responses=hover(a)), fake_session(b, responses=hover(b))
        lsp._sessions[lsp.session_key(a)] = sa
        lsp._sessions[lsp.session_key(b)] = sb
        out = await asyncio.gather(
            kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)),
            kt.symbol_info(None, str(door(b)), 3, name="Door", expected_root=str(b)),
            kt.symbol_info(None, str(door(b)), 3, name="Door", expected_root=str(a)),
            return_exceptions=True,
        )
        return sa, sb, out

    sa, sb, (in_a, in_b, crossed) = run(scenario())
    assert in_a.endswith("class Door in app") and in_b.endswith("class Door in app-other")
    assert getattr(crossed, "reason", None) == "target_mismatch"
    opened = lambda s: {m["params"]["textDocument"]["uri"] for m in s.sent if m.get("method") == "textDocument/didOpen"}  # noqa: E731
    assert opened(sa) == {door(a).as_uri()} and opened(sb) == {door(b).as_uri()}
    assert lsp_env == []


def test_reconnect_or_backend_replacement_does_not_reuse_old_state(worktrees, lsp_env):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, _b = worktrees
    key = lsp.session_key(a)

    async def scenario():
        old = fake_session(a)
        lsp._sessions[key] = old
        before = (await kt.readiness(expected_root=str(a))).semantic
        # The backend goes away; whatever it reported while alive no longer counts.
        old.proc.returncode = 1
        after_exit = (await kt.readiness(expected_root=str(a))).semantic
        call = await asyncio.gather(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)), return_exceptions=True)
        # The directory is replaced by another one at the same path while a session is alive.
        stale = fake_session(a)
        lsp._sessions[key] = stale
        shutil.move(str(a), str(a) + ".old")
        shutil.copytree(str(a) + ".old", str(a), symlinks=True)
        replaced = (await kt.readiness(expected_root=str(a))).semantic
        call2 = await asyncio.gather(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)), return_exceptions=True)
        return old, stale, before, after_exit, call[0], replaced, call2[0]

    old, stale, before, after_exit, call, replaced, call2 = run(scenario())
    assert before.state == "ready"
    assert (after_exit.state, after_exit.reason) == ("unknown", "semantic_not_started")
    assert getattr(call, "outcome", None) == "blocked" and queries(old) == []
    assert len(lsp_env) == 2  # each time a new session was started rather than the old one reused
    assert (replaced.state, replaced.reason) == ("unavailable", "semantic_session_mismatch")
    assert getattr(call2, "outcome", None) == "blocked" and queries(stale) == []


# ----------------------------------------------------------------------------- blocked, never text


@pytest.mark.parametrize(
    "setup,reason",
    [
        ("not_installed", "semantic_not_installed"),
        ("cannot_start", "semantic_unavailable"),
        ("unknown", "semantic_readiness_unknown"),
        ("indexing", "semantic_indexing"),
        ("import_failed", "semantic_import_failed"),
        ("unsupported", "unsupported_operation"),
    ],
)
def test_semantic_operations_are_blocked_without_text_fallback(worktrees, lsp_env, monkeypatch, effects, setup, reason):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, _b = worktrees
    text_routes: list[str] = []
    real_usages = kt._text_find_usages

    async def spy_usages(*args, **kwargs):
        text_routes.append("find_usages")
        return await real_usages(*args, **kwargs)

    monkeypatch.setattr(kt, "_text_find_usages", spy_usages)
    original = door(a).read_bytes()

    async def scenario():
        s = None
        if setup == "not_installed":
            monkeypatch.setattr(lsp, "find_server", lambda: None)
        elif setup == "unknown":
            s = fake_session(a, status="")  # alive and quiet, but never reported an import
        elif setup == "indexing":
            s = fake_session(a, done=False)
            s.started = 1e18  # still importing however long we wait
            real_wait = s.wait_ready
            s.wait_ready = lambda timeout: real_wait(0.05)
        elif setup == "import_failed":
            s = fake_session(a, status="FAILURE")
        elif setup == "unsupported":
            s = fake_session(a, capabilities={})
        results = {}
        for name, call in semantic_calls(a).items():
            if s is not None:
                lsp._sessions[lsp.session_key(a)] = s
            results[name] = (await asyncio.gather(call(), return_exceptions=True))[0]
        state = (await kt.readiness(expected_root=str(a))).semantic
        return s, results, state

    s, results, state = run(scenario())
    for name, result in results.items():
        assert getattr(result, "outcome", None) == "blocked", (name, result)
        assert result.reason == reason, (name, str(result))
    assert text_routes == []
    assert door(a).read_bytes() == original
    if s is not None:
        assert [m for m in queries(s) if m.startswith(("textDocument/re", "textDocument/hover", "textDocument/diag", "textDocument/impl", "textDocument/prepare", "callHierarchy", "workspace/symbol"))] == []
    expected_state = {
        "not_installed": "unavailable", "cannot_start": "unknown", "unknown": "unknown",
        "indexing": "indexing", "import_failed": "unavailable", "unsupported": "ready",
    }[setup]
    assert state.state == expected_state, state
    if expected_state != "ready":
        assert state.reason and state.detail


def test_indexing_means_sustained_work_not_momentary_progress_pulses(worktrees, lsp_env):
    """Observed with kotlin-lsp 271.2085.0: 'Indexing' tokens that begin and end at once, about
    one a second, for as long as the server runs. They must not read as indexing."""
    import time

    from ide_mcp.common import identity

    a, _b = worktrees

    async def scenario():
        s = fake_session(a)
        ident = identity(a)
        for i in range(5):
            s._handle({"method": "$/progress", "params": {"token": f"t{i}", "value": {"kind": "begin", "title": "Indexing"}}})
            pulse = s.state(ident)[0]
            s._handle({"method": "$/progress", "params": {"token": f"t{i}", "value": {"kind": "end"}}})
            assert pulse == "ready" and s.state(ident)[0] == "ready"
        started = time.time()
        await s.wait_ready(30)  # does not sit out a quiet period that never comes
        waited = time.time() - started
        s._handle({"method": "$/progress", "params": {"token": "long", "value": {"kind": "begin", "title": "Indexing"}}})
        s._begun["long"] -= 5  # still open five seconds later
        return waited, s.state(ident)

    waited, state = run(scenario())
    assert waited < 1.0
    assert state[:2] == ("indexing", "semantic_indexing")


def test_text_matching_stays_read_only_and_labelled(worktrees, lsp_env, monkeypatch, effects):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, _b = worktrees
    monkeypatch.setattr(lsp, "find_server", lambda: None)
    effects.events.clear()
    out = run(kt.find_usages(None, "Door", engine="text", expected_root=str(a)))
    assert out.startswith("[engine=text requested=text ") and "1 usage(s), 1 declaration(s)" in out
    assert effects.of("write") == []
    # Independent file reads stay available whatever the semantic state is.
    assert "class demo.Door" in run(kt.find_symbol("Door", expected_root=str(a)))


# ----------------------------------------------------------------------------- rename


def _rng(l1, c1, l2, c2):
    return {"start": {"line": l1, "character": c1}, "end": {"line": l2, "character": c2}}


def _rename_edit(a: Path, extra: list[dict]) -> dict:
    return {
        "documentChanges": [
            {"textDocument": {"uri": door(a).as_uri(), "version": 1}, "edits": [{"range": _rng(2, 6, 2, 10), "newText": "Gate"}]},
            *extra,
        ]
    }


@pytest.mark.parametrize("kind", ["edit_other_worktree", "rename_into_other_worktree", "create_outside", "edit_protected", "edit_outside_project"])
def test_rename_with_an_out_of_scope_destination_applies_nothing(worktrees, lsp_env, effects, kind):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, b = worktrees
    (a / "notes.txt").write_text("Door notes\n")
    extra = {
        "edit_other_worktree": [{"textDocument": {"uri": door(b).as_uri(), "version": 0}, "edits": [{"range": _rng(2, 6, 2, 10), "newText": "Gate"}]}],
        "rename_into_other_worktree": [{"kind": "rename", "oldUri": door(a).as_uri(), "newUri": (b / "Gate.kt").as_uri()}],
        "create_outside": [{"kind": "create", "uri": (a.parent / "Gate.kt").as_uri()}],
        "edit_protected": [{"textDocument": {"uri": (a / "gradle.properties").as_uri(), "version": 0}, "edits": []}],
        "edit_outside_project": [{"textDocument": {"uri": (a / "notes.txt").as_uri(), "version": 0}, "edits": [{"range": _rng(0, 0, 0, 4), "newText": "Gate"}]}],
    }[kind]
    scope_root = a
    if kind == "edit_outside_project":  # inside the scope, but not in the project the session serves
        nested = a / "android"
        nested.mkdir()
        for name in ("settings.gradle.kts", "gradlew", "gradlew.bat", "app"):
            shutil.move(str(a / name), str(nested / name))
        extra = [{"textDocument": {"uri": (a / "notes.txt").as_uri(), "version": 0}, "edits": [{"range": _rng(0, 0, 0, 4), "newText": "Gate"}]}]
        project_root = nested
    else:
        project_root = a
    target = project_root / "app" / "src" / "main" / "kotlin" / "Door.kt"
    edit = _rename_edit(a, extra)
    edit["documentChanges"][0]["textDocument"]["uri"] = target.as_uri()
    before = {p: p.read_bytes() for p in (target, door(b), a / "notes.txt")}

    async def scenario():
        s = fake_session(project_root, responses={
            "textDocument/prepareRename": {"range": _rng(2, 6, 2, 10), "placeholder": "Door"},
            "textDocument/definition": [{"uri": target.as_uri(), "range": _rng(2, 6, 2, 10)}], "textDocument/rename": edit,
        })
        lsp._sessions[lsp.session_key(project_root)] = s
        return await asyncio.gather(
            kt.rename_symbol(None, "Door", "Gate", file=str(target), line=3, dry_run=False, preview_id="0" * 16, expected_root=str(scope_root)),
            return_exceptions=True,
        )

    effects.events.clear()
    (result,) = run(scenario())
    assert getattr(result, "outcome", None) == "blocked", result
    assert result.reason == ("protected_path" if kind == "edit_protected" else
                             "unsupported_resource_operation" if kind == "create_outside" else
                             "stale_document" if kind == "edit_outside_project" else "target_mismatch")
    assert effects.of("write") == []
    assert effects.touching(b, "open") == []
    assert {p: p.read_bytes() for p in before} == before
    assert not (b / "Gate.kt").exists() and not (a.parent / "Gate.kt").exists()


# ----------------------------------------------------------------------------- execution


def test_unavailable_execution_blocks_the_launch(worktrees, launches, monkeypatch):
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    monkeypatch.delenv("JAVA_HOME")
    monkeypatch.setenv("PATH", str(a / "no-such-bin"))
    state = run(kt.readiness(expected_root=str(a))).execution
    assert (state.state, state.reason) == ("unavailable", "execution_unavailable") and "JDK" in state.detail
    for call in (
        lambda: kt.compile_sources(None, module=":app", expected_root=str(a)),
        lambda: kt.run_tests(None, module=":app", expected_root=str(a)),
        lambda: kt.gradle_run(None, [":app:classes"], expected_root=str(a)),
        lambda: kt.gradle_tasks(None, expected_root=str(a)),
    ):
        message = blocked("execution_unavailable", call())
        assert "category=environment" in message
    assert launched(launches) == []


def test_readiness_changing_after_a_successful_query_is_rechecked(worktrees, launches):
    """A readiness answer is not a token: the operation looks again when it runs."""
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    A = str(a)
    first = run(kt.readiness(expected_root=A))
    assert (first.execution.state, first.execution.reason) == ("unknown", "execution_not_observed")
    assert first.project_root == A and first.resolved_scope == A and first.worktree_root == A
    assert launched(launches) == []  # asking did not run Gradle

    assert "[outcome=passed]" in run(kt.compile_sources(None, module=":app", expected_root=A))
    assert run(kt.readiness(expected_root=A)).execution.state == "available"

    # The wrapper disappears between the query and the next operation.
    os.remove(a / "gradlew")
    os.remove(a / "gradlew.bat")
    before = len(launched(launches))
    with pytest.raises(Exception) as info:
        run(kt.compile_sources(None, module=":app", expected_root=A))
    assert getattr(info.value, "outcome", None) in ("blocked", "failed"), info.value
    assert len(launched(launches)) == before

    # The same for the scope itself: valid when queried, gone when used.
    doc = a.parent.parent / "tickets"
    doc.mkdir()
    assert run(fs.readiness(expected_root=str(doc))).resolved_scope == str(doc)
    doc.rmdir()
    blocked("invalid_scope", fs.find_files(expected_root=str(doc)))


def test_wrapper_removed_after_project_lookup_blocks_the_launch(worktrees, launches, monkeypatch):
    from ide_mcp import gradle
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    A = str(a)
    run(kt.gradle_tasks(None, expected_root=A))  # fills the task cache, one launch
    real_state = gradle.execution_state

    def vanish(project):
        for name in ("gradlew", "gradlew.bat"):
            (project.root / name).unlink(missing_ok=True)
        return real_state(project)

    monkeypatch.setattr(gradle, "execution_state", vanish)
    before = len(launched(launches))
    blocked("execution_unavailable", kt.compile_sources(None, module=":app", expected_root=A))
    assert len(launched(launches)) == before


def test_execution_outcomes_are_explicit(worktrees, launches, monkeypatch):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt
    from ide_mcp.common import OutcomeError

    a, _b = worktrees
    A = str(a)
    assert run(kt.compile_sources(None, module=":app", expected_root=A)).startswith("[outcome=passed]")
    assert run(kt.gradle_tasks(None, module=":app", expected_root=A)).startswith("7 task(s)") is False

    # The build "succeeds" but no test ran: that is not a pass.
    with pytest.raises(OutcomeError) as info:
        run(kt.run_tests(None, module=":app", expected_root=A))
    assert (info.value.outcome, info.value.reason) == ("not_run", "no_tests_executed")

    results = a / "app" / "build" / "test-results" / "testDebugUnitTest"
    results.mkdir(parents=True)
    (results / "TEST-demo.DoorTest.xml").write_text(
        '<testsuite name="demo.DoorTest" tests="2" skipped="0" failures="1" errors="0">'
        '<testcase name="ok" classname="demo.DoorTest"/>'
        '<testcase name="bad" classname="demo.DoorTest"><failure message="expected 1 but was 2">x</failure></testcase></testsuite>'
    )
    wrapper = a / ("gradlew.bat" if os.name == "nt" else "gradlew")
    wrapper.write_text(wrapper.read_text().replace("BUILD SUCCESSFUL in 1s", "^> Task :app:testDebugUnitTest" if os.name == "nt" else "> Task :app:testDebugUnitTest"))
    from ide_mcp import gradle
    real_proc = gradle.run_proc

    async def fresh_reports(*args, **kwargs):
        result = await real_proc(*args, **kwargs)
        for report in results.glob("TEST-*.xml"):
            os.utime(report, None)  # fixture evidence generated during the owned run
        return result
    monkeypatch.setattr(gradle, "run_proc", fresh_reports)
    with pytest.raises(OutcomeError) as info:
        run(kt.run_tests(None, module=":app", expected_root=A))
    assert (info.value.outcome, info.value.category) == ("failed", "source")
    assert "Tests: 2 executed, 1 passed, 1 failed" in str(info.value)

    (results / "TEST-demo.DoorTest.xml").write_text(
        '<testsuite name="demo.DoorTest" tests="1" skipped="0" failures="0" errors="0"><testcase name="ok" classname="demo.DoorTest"/></testsuite>'
    )
    out = run(kt.run_tests(None, module=":app", expected_root=A))
    assert out.startswith("[outcome=passed]") and "Tests: 1 executed, 1 passed" in out

    with pytest.raises(OutcomeError) as info:
        run(fs.run_command("exit 3", expected_root=A))
    assert info.value.outcome == "failed" and "exit code 3" in str(info.value)
    with pytest.raises(OutcomeError) as info:
        run(fs.run_command("sleep 5" if os.name != "nt" else "Start-Sleep 5", timeout_sec=1, expected_root=A))
    assert info.value.outcome == "timed_out"
    with pytest.raises(OutcomeError) as info:
        run(fs.read_file(str(a / "missing.txt"), expected_root=A))
    assert (info.value.outcome, info.value.category) == ("failed", "input")


# ----------------------------------------------------------------------------- readiness query


def test_readiness_query_is_cheap_and_changes_nothing(worktrees, lsp_env, launches, effects):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees

    def snapshot() -> dict:
        return {p.relative_to(a).as_posix(): p.stat().st_mtime_ns for p in sorted(a.rglob("*")) if ".git/" not in p.as_posix() + "/"}

    before = snapshot()
    effects.events.clear()
    report = run(kt.readiness(expected_root=str(a)))
    files_report = run(fs.readiness(expected_root=str(a)))
    assert effects.of("launch", "walk", "write", "open") == []  # no build, no scan, no content read
    assert launched(launches) == [] and lsp_env == []  # no Gradle, no language server
    assert snapshot() == before
    assert not (launches.parent / "lsp-cache").exists() and not (launches.parent / "gradle-logs").exists()

    assert report.schema_version == "1.0" and report.contract_version == "3.0"
    assert report.server == "kotlin" and report.server_version == "0.2.0"
    assert (report.execution.state, report.semantic.state) == ("unknown", "unknown")
    assert report.semantic.reason == "semantic_not_started"
    assert not hasattr(report, "ready") and not hasattr(report, "ok")
    assert files_report.semantic.state == "not_applicable" and files_report.project_root is None
    assert files_report.execution.state in ("unknown", "available")

    # A scope with no project in it: stated, not guessed, and not searched for below.
    docs = a / "docs"
    docs.mkdir()
    (docs / ".keep").write_text("")
    outside = a.parent.parent / "notes"
    outside.mkdir()
    lone = run(kt.readiness(expected_root=str(outside)))
    assert lone.project_root is None and (lone.execution.state, lone.execution.reason) == ("unavailable", "project_not_found")


def test_readiness_operations_come_from_the_registrations(worktrees):
    from ide_mcp import files_server as fs
    from ide_mcp import guard
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    for server in (fs, kt):
        report = run(server.readiness(expected_root=str(a)))
        listed = {t.name for t in run(server.mcp.list_tools())}
        assert {o.name for o in report.operations} == listed == set(server.ops.registered)
        for o in report.operations:
            assert o.requires == list(server.ops.registered[o.name].needs) and o.requires[0] == guard.SCOPE
    by_name = {o.name: o for o in run(kt.readiness(expected_root=str(a))).operations}
    assert by_name["compile"].kind == "execution" and guard.EXECUTION in by_name["compile"].requires
    assert by_name["rename_symbol"].kind == "semantic" and guard.SEMANTIC in by_name["rename_symbol"].requires
    assert by_name["find_symbol"].kind == "file" and by_name["find_symbol"].requires == [guard.SCOPE, guard.TARGET]


def test_readiness_through_the_sdk_has_a_schema_and_structured_content(worktrees):
    from ide_mcp import kotlin_server as kt

    a, _b = worktrees
    tool = next(t for t in run(kt.mcp.list_tools()) if t.name == "readiness")
    schema = tool.outputSchema
    assert set(schema["required"]) >= {"schema_version", "resolved_scope", "execution", "semantic", "operations"}
    states = schema["$defs"]["ExecutionState"]["properties"]["state"]["enum"]
    assert states == ["available", "unavailable", "unknown", "not_applicable"]
    assert schema["$defs"]["SemanticState"]["properties"]["state"]["enum"] == ["ready", "indexing", "unavailable", "unknown", "not_applicable"]
    assert not {"ready", "ok", "healthy", "status"} & set(schema["properties"])
    _content, structured = run(kt.mcp.call_tool("readiness", {"expected_root": str(a)}))
    assert structured["resolved_scope"] == str(a) and structured["execution"]["state"] == "unknown"
    assert kt.mcp._mcp_server.version == "0.2.0"
