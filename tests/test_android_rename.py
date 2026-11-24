"""Android rename uses one imported variant, not an all-variant coverage gate."""

# Pytest loads the shared fixtures as top-level modules.
# pyright: reportImplicitRelativeImport=false

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import run
from test_readiness import FakeProc
from test_readiness import lsp_env as lsp_env  # noqa: PLC0414 - expose the pytest fixture
from test_scope import door
from test_semantic import project as project  # noqa: PLC0414 - expose the pytest fixture
from test_semantic import serve, tree

from ide_mcp import guard, lsp
from ide_mcp import kotlin_server as kt
from ide_mcp.common import OutcomeError
from ide_mcp.lsp import _start


async def rename(root: Path, **options: Any) -> guard.TextResult:
    # The guarded tool returns structured facts alongside its legacy str result.
    tool = cast(Callable[..., Awaitable[guard.TextResult]], kt.rename_symbol)
    return await tool(None, "Door", "Gate", file=str(door(root)), line=3,
                      expected_root=str(root), **options)


@pytest.fixture
def android_project(project):
    a, b = project
    (a / "app" / "src" / "main" / "AndroidManifest.xml").write_text("<manifest />\n")
    return a, b


@pytest.fixture
def variant_import(android_project, monkeypatch):
    """Replace the backend transport, retaining the real session-selection logic."""
    a, _b = android_project
    started = []

    async def start(root, launcher, system, variant=None):
        s = serve(root)
        s.variant = variant
        s._handle({"method": "intellij/importLog", "params": {
            "message": f"[ide-mcp:variant] :app={variant}",
        }})
        started.append((s, system))
        return s

    async def initialise(self):
        pass

    monkeypatch.setattr(lsp, "_start", start)
    monkeypatch.setattr(lsp.Session, "initialize", initialise)
    return a, started


def test_android_default_model_can_preview_and_apply_rename(android_project):
    a, _b = android_project
    serve(a)
    before = tree(a)
    preview = run(rename(a))
    assert tree(a) == before
    assert preview.fields["data"]["variant_coverage"] == "imported_model"
    assert "Inactive Android variants are not guaranteed" in preview

    applied = run(rename(a, dry_run=False, preview_id=preview.fields["data"]["preview_id"]))
    assert applied.fields["outcome"] == "passed"
    assert applied.startswith("[outcome=passed]")
    assert applied.fields["data"]["variant_coverage"] == "imported_model"
    assert not door(a).exists()
    assert "class Gate" in door(a).with_name("Gate.kt").read_text()
    assert (door(a).parent / "other" / "Door.kt").read_text() == "package demo.other\n\nclass Door\n"
    assert '// Door is mentioned' in (door(a).parent / "Use.kt").read_text()
    assert 'val label = "Door"' in (door(a).parent / "Use.kt").read_text()


@pytest.mark.parametrize("component", ["unitTest", "androidTest"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_android_partial_import_still_blocks_rename(android_project, component, dry_run):
    a, _b = android_project
    s = serve(a)
    s.workaround = frozenset({component})
    hidden = a / "app" / "src" / "androidTest" / "Hidden.kt"
    hidden.parent.mkdir()
    hidden.write_text("class Hidden\n")
    s._mtimes = {str(hidden): 0}
    before = tree(a)
    with pytest.raises(OutcomeError) as error:
        run(rename(a, dry_run=dry_run, preview_id="old-preview"))
    assert error.value.reason == "incomplete_semantic_coverage"
    assert tree(a) == before


def test_explicit_variant_is_imported_and_reported(variant_import):
    a, started = variant_import
    preview = run(rename(a, variant="paidDebug"))
    assert len(started) == 1
    assert started[0][0].variant == "paidDebug"
    assert preview.fields["data"]["variant"] == "paidDebug"
    assert preview.fields["data"]["variant_modules"] == {":app": "paidDebug"}


def test_switching_variant_invalidates_preview_and_default_reuses_current_model(variant_import):
    a, started = variant_import
    async def scenario():
        preview = await rename(a, variant="paidDebug")
        before = tree(a)
        with pytest.raises(OutcomeError) as error:
            await rename(a, variant="paidRelease", dry_run=False,
                         preview_id=preview.fields["data"]["preview_id"])
        assert error.value.reason == "stale_preview"
        assert tree(a) == before
        current = await rename(a)
        assert current.fields["data"]["variant"] == "paidRelease"
        assert len(started) == 2
        assert not started[0][0].alive
        assert started[0][1] != started[1][1]

    run(scenario())


def test_variant_switch_cannot_interrupt_an_active_semantic_operation(variant_import):
    a, started = variant_import
    async def scenario():
        await rename(a, variant="paidDebug")
        s = started[0][0]
        async with s.lock:
            with pytest.raises(OutcomeError) as error:
                await rename(a, variant="paidRelease")
        assert error.value.reason == "semantic_variant_busy"
        assert s.alive
        assert len(started) == 1

    run(scenario())


@pytest.mark.parametrize("confirmation", [None, "paidRelease"])
def test_explicit_variant_without_matching_import_evidence_remains_blocked(android_project, confirmation):
    a, _b = android_project
    s = serve(a)
    s.variant = "paidDebug"
    if confirmation:
        s._handle({"method": "intellij/importLog", "params": {
            "message": f"[ide-mcp:variant] :app={confirmation}",
        }})
    before = tree(a)
    with pytest.raises(OutcomeError) as error:
        run(rename(a, variant="paidDebug"))
    assert error.value.reason == "semantic_variant_unverified"
    assert tree(a) == before


def test_reimport_does_not_reuse_previous_variant_confirmation(variant_import):
    a, started = variant_import
    async def scenario():
        await rename(a, variant="paidDebug")
        s = started[0][0]
        s._handle({"method": "intellij/workspaceImportState", "params": {"phase": "IN_PROGRESS"}})
        s._handle({"method": "intellij/workspaceImportState", "params": {
            "phase": "FINISHED", "folders": [{"folderUri": a.as_uri(), "status": "SUCCESS"}],
        }})
        s._imported_at = 0
        with pytest.raises(OutcomeError) as error:
            await rename(a, variant="paidDebug")
        assert error.value.reason == "semantic_variant_unverified"

    run(scenario())


@pytest.mark.parametrize("variant", ["", "paid debug", "debug'; exit", "../debug"])
def test_invalid_variant_is_rejected_before_backend_start(variant_import, variant):
    a, started = variant_import
    with pytest.raises(OutcomeError):
        run(rename(a, variant=variant))
    assert started == []


def test_variant_selection_is_scoped_to_the_import_process(android_project, monkeypatch, tmp_path):
    a, _b = android_project
    monkeypatch.setenv("LSP_ANDROID_VARIANT", "ambientDebug")
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(lsp.asyncio, "create_subprocess_exec", spawn)

    async def scenario():
        with guard.activate(guard.Scope.of(str(a))):
            return await _start(a, a / "launcher.exe", tmp_path / "lsp-system", variant="paidDebug")

    s = run(scenario())
    assert calls[0][1]["env"]["LSP_ANDROID_VARIANT"] == "paidDebug"
    assert os.environ["LSP_ANDROID_VARIANT"] == "ambientDebug"
    assert s.init_script and s.init_script.exists()
    script = s.init_script.read_text()
    assert 'gradleProperty("lsp.android.variant")' in script
    assert 'systemProperty("lsp.android.variant")' in script
    assert 'environmentVariable("LSP_ANDROID_VARIANT")' in script
    assert "onVariants" in script and 'contains("paidDebug")' in script
    assert "[ide-mcp:variant]" in script
    assert "GradleException" in script
    assert "-Dcom.jetbrains.ls.imports.gradle.init.scripts=" in calls[0][1]["env"]["IJ_JAVA_OPTIONS"]
    assert not any(a.glob("*.properties"))
