"""Import-input changes must not reuse a previously imported project model."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from conftest import run
from test_readiness import lsp_env as lsp_env  # noqa: PLC0414 - expose the pytest fixture
from test_readiness import queries
from test_scope import door
from test_semantic import loc, serve, tree
from test_semantic import project as project  # noqa: PLC0414 - expose the pytest fixture

from ide_mcp import guard, lsp
from ide_mcp import kotlin_server as kt
from ide_mcp.common import Blocked, ToolError, identity, protected_name


async def session(root: Path, **kwargs):
    with guard.activate(guard.Scope.of(str(root))):
        return await lsp.session_for(root, **kwargs)


@pytest.fixture
def backend(lsp_env, monkeypatch):
    """Keep real session selection and readiness, replacing only the backend transport."""
    started = []

    async def start(root, launcher, system, variant=None):
        s = serve(root, {"textDocument/references": [loc(door(root), 2), loc(door(root), 4)]},
                  status="", done=False)
        s.started = float(len(started) + 1)
        s.variant = variant
        started.append((s, system))
        return s

    async def initialise(s):
        s._handle({"method": "intellij/workspaceImportState", "params": {
            "phase": "FINISHED", "folders": [{"folderUri": s.root.as_uri(), "status": "SUCCESS"}],
        }})
        s._imported_at = 0

    monkeypatch.setattr(lsp, "_start", start)
    monkeypatch.setattr(lsp.Session, "initialize", initialise)
    return started


@pytest.mark.parametrize("name", [
    "build.gradle", "app/build.gradle.kts", "settings.gradle.kts",
    "gradle/libs.versions.toml", "gradle.lockfile", "gradle/verification-metadata.xml",
    "gradlew", "gradlew.bat", "gradle/wrapper/gradle-wrapper.jar", "libs/local.aar",
    "pom.xml", ".mvn/maven.config", "buildSrc/src/main/kotlin/Convention.kt",
])
def test_changed_import_input_restarts_only_its_worktree(worktrees, backend, name):
    a, b = worktrees
    path = a / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("before", encoding="utf-8")

    async def scenario():
        sa, sb = await session(a), await session(b)
        path.write_text("after", encoding="utf-8")
        fresh = await session(a)
        assert fresh is not sa and not sa.alive
        assert await session(b) is sb and sb.alive
        assert fresh.state(identity(a))[0] == "ready"
        assert backend[0][1] != backend[2][1]
        assert queries(sa) == []
        assert len(backend) == 3

    run(scenario())


def test_content_change_with_preserved_timestamp_is_detected(worktrees, backend):
    a, _b = worktrees
    build = a / "build.gradle.kts"
    build.write_text("// old\n", encoding="utf-8")

    async def scenario():
        old = await session(a)
        stamp = build.stat()
        build.write_text("// new\n", encoding="utf-8")
        os.utime(build, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert await session(a) is not old

    run(scenario())


@pytest.mark.parametrize("change", ["add", "delete", "rename"])
def test_import_input_set_changes_restart_the_session(worktrees, backend, change):
    a, _b = worktrees
    build = a / "custom.gradle"
    if change != "add":
        build.write_text("// before\n", encoding="utf-8")

    async def scenario():
        old = await session(a)
        if change == "add":
            build.write_text("// added\n", encoding="utf-8")
        elif change == "delete":
            build.unlink()
        else:
            build.rename(a / "renamed.gradle")
        assert await session(a) is not old

    run(scenario())


def test_nested_build_sources_are_import_inputs(worktrees, backend):
    a, _b = worktrees
    nested = a / "BuildPlugins"
    nested.mkdir()
    (nested / "settings.gradle.kts").write_text('rootProject.name = "plugins"\n', encoding="utf-8")
    plugin = nested / "src/main/kotlin/Plugin.kt"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("class Before\n", encoding="utf-8")

    async def scenario():
        old = await session(a)
        plugin.write_text("class After\n", encoding="utf-8")
        assert await session(a) is not old

    run(scenario())


@pytest.mark.parametrize("name", ["local.properties", "gradle.properties", "gradle/wrapper/gradle-wrapper.properties"])
def test_ignored_protected_inputs_use_metadata_without_reading_contents(worktrees, backend, monkeypatch, name):
    a, _b = worktrees
    (a / ".gitignore").write_text("*.properties\n", encoding="utf-8")
    path = a / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret=before\n", encoding="utf-8")
    reading = guard.Scope.read_bytes

    def read(scope, target, *args, **kwargs):
        assert not protected_name(Path(target).name), "Protected contents must not be read."
        return reading(scope, target, *args, **kwargs)

    monkeypatch.setattr(guard.Scope, "read_bytes", read)

    async def scenario():
        old = await session(a)
        path.write_text("secret=after-and-longer\n", encoding="utf-8")
        assert await session(a) is not old

    run(scenario())


def test_ordinary_sources_and_generated_outputs_do_not_restart_import(worktrees, backend):
    a, _b = worktrees

    async def scenario():
        old = await session(a)
        with guard.activate(guard.Scope.of(str(a))):
            await old.sync()
            old.open(door(a))
        door(a).write_text("package demo\n\nclass Changed\n", encoding="utf-8")
        for name in ["build/generated/build.gradle.kts", ".gradle/custom.gradle", ".ide-mcp/log.toml"]:
            path = a / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated\n", encoding="utf-8")
        assert await session(a) is old
        with guard.activate(guard.Scope.of(str(a))):
            await old.sync()
        assert "textDocument/didChange" in queries(old)
        assert len(backend) == 1

    run(scenario())


def test_active_operation_is_invalidated_but_not_terminated(worktrees, backend):
    a, _b = worktrees

    async def scenario():
        old = await session(a, variant="paidDebug")
        async with old.lock:
            (a / "build.gradle.kts").write_text("// changed\n", encoding="utf-8")
            with pytest.raises(lsp.LspError) as error:
                await session(a)
            assert error.value.reason == "semantic_import_inputs_changed"
            assert old.alive and len(backend) == 1
            assert old.state(identity(a))[0] != "ready"
        fresh = await session(a)
        assert fresh is not old and fresh.variant == "paidDebug"

    run(scenario())


def test_change_during_import_blocks_until_a_fresh_import(worktrees, backend, monkeypatch):
    a, _b = worktrees
    initialise = lsp.Session.initialize

    async def changed(s):
        await initialise(s)
        (a / "build.gradle.kts").write_text("// changed during import\n", encoding="utf-8")

    monkeypatch.setattr(lsp.Session, "initialize", changed)

    async def scenario():
        with pytest.raises(lsp.LspError) as error:
            await session(a)
        assert error.value.reason == "semantic_import_inputs_changed"
        old = backend[0][0]
        assert old.state(identity(a))[0] != "ready"
        monkeypatch.setattr(lsp.Session, "initialize", initialise)
        assert await session(a) is not old

    run(scenario())


def test_restart_without_successful_import_cannot_restore_readiness(worktrees, backend, monkeypatch):
    a, _b = worktrees

    async def unknown(s):
        s.import_done.set()

    async def scenario():
        old = await session(a)
        (a / "build.gradle.kts").write_text("// changed\n", encoding="utf-8")
        monkeypatch.setattr(lsp.Session, "initialize", unknown)
        fresh = await session(a)
        assert not old.alive
        assert fresh.state(identity(a))[0] != "ready"
        with guard.activate(guard.Scope.of(str(a))), pytest.raises(lsp.LspError):
            await fresh.request("textDocument/hover", {})
        assert queries(fresh) == []

    run(scenario())


def test_change_during_semantic_request_rejects_the_answer(worktrees, backend):
    a, _b = worktrees

    async def scenario():
        s = await session(a)
        sending = s._send

        def send(message):
            if message.get("method") == "textDocument/hover":
                (a / "build.gradle.kts").write_text("// changed during query\n", encoding="utf-8")
            sending(message)

        s._send = send
        with guard.activate(guard.Scope.of(str(a))), pytest.raises(lsp.LspError) as error:
            await s.request("textDocument/hover", {})
        assert error.value.reason == "semantic_import_inputs_changed"
        assert s.state(identity(a))[0] != "ready"

    run(scenario())


@pytest.mark.parametrize("operation", ["preview", "continuation"])
def test_import_input_change_invalidates_reviewed_results(project, backend, operation):
    a, _b = project
    old = serve(a, {"textDocument/references": [loc(door(a), 2), loc(door(a), 4)]})
    args = {"file": str(door(a)), "line": 3, "expected_root": str(a)}

    async def scenario():
        if operation == "preview":
            first = await kt.rename_symbol(None, "Door", "Gate", **args)
        else:
            first = await kt.find_usages(None, "Door", engine="semantic", limit=1, **args)
        (a / "build.gradle.kts").write_text("// changed\n", encoding="utf-8")
        before = tree(a)
        with pytest.raises(ToolError) as error:
            if operation == "preview":
                await kt.rename_symbol(None, "Door", "Gate", dry_run=False,
                                       preview_id=first.fields["data"]["preview_id"], **args)
            else:
                await kt.find_usages(None, "Door", engine="semantic", offset=1,
                                     query_id=first.fields["data"]["query_id"], **args)
        assert error.value.reason == ("stale_preview" if operation == "preview" else "stale_continuation")
        assert not old.alive and tree(a) == before

    run(scenario())


def test_unreadable_import_input_invalidates_existing_readiness(worktrees, backend, monkeypatch):
    a, _b = worktrees
    reading = guard.Scope.read_bytes

    async def scenario():
        old = await session(a)

        def denied(scope, target, *args, **kwargs):
            if Path(target).name == "build.gradle.kts":
                raise PermissionError("test: unreadable build input")
            return reading(scope, target, *args, **kwargs)

        monkeypatch.setattr(guard.Scope, "read_bytes", denied)
        with pytest.raises(lsp.LspError) as error:
            await session(a)
        assert error.value.reason == "semantic_import_inputs_unverified"
        assert old.state(identity(a))[0] != "ready"

    run(scenario())


def test_import_input_scan_preserves_scope_denials(worktrees, backend, monkeypatch):
    a, _b = worktrees

    async def scenario():
        old = await session(a)
        reading = guard.Scope.read_bytes

        def denied(scope, target, *args, **kwargs):
            if Path(target).name == "build.gradle.kts":
                raise Blocked("target_mismatch", "test: input moved to another worktree")
            return reading(scope, target, *args, **kwargs)

        monkeypatch.setattr(guard.Scope, "read_bytes", denied)
        with pytest.raises(Blocked) as error:
            await session(a)
        assert error.value.reason == "target_mismatch"
        assert old.state(identity(a))[0] != "ready"
        assert len(backend) == 1

    run(scenario())


def test_concurrent_refresh_requests_share_one_replacement(worktrees, backend):
    a, _b = worktrees

    async def scenario():
        old = await session(a)
        (a / "build.gradle.kts").write_text("// changed\n", encoding="utf-8")
        first, second = await asyncio.gather(session(a), session(a))
        assert first is second and first is not old
        assert len(backend) == 2

    run(scenario())


def test_changed_inputs_during_rename_preflight_block_before_writing(project, backend, monkeypatch):
    a, _b = project
    serve(a)
    args = {"file": str(door(a)), "line": 3, "expected_root": str(a)}
    writing = lsp.write_changes

    async def scenario():
        preview = await kt.rename_symbol(None, "Door", "Gate", **args)
        before = tree(a)

        def write(*args, **kwargs):
            (a / "build.gradle.kts").write_text("// changed during apply preflight\n", encoding="utf-8")
            return writing(*args, **kwargs)

        monkeypatch.setattr(lsp, "write_changes", write)
        with pytest.raises(ToolError) as error:
            await kt.rename_symbol(None, "Door", "Gate", dry_run=False,
                                   preview_id=preview.fields["data"]["preview_id"], **args)
        assert error.value.reason == "semantic_import_inputs_changed"
        assert tree(a) == before

    run(scenario())
