"""Additional mutation prerequisites, using the existing semantic fixture."""

import pytest
from conftest import run
from test_readiness import lsp_env as lsp_env  # noqa: PLC0414 - expose the pytest fixture
from test_scope import door
from test_semantic import edits_for, serve, tree
from test_semantic import project as project  # noqa: PLC0414 - expose the pytest fixture

from ide_mcp import kotlin_server as kt
from ide_mcp.common import ToolError


@pytest.mark.parametrize("tool", ["symbol_info", "find_usages_auto"])
def test_another_worktrees_import_cannot_enable_semantic_tools(project, tool):
    from test_readiness import queries

    a, b = project
    s = serve(a)
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [{"folderUri": b.as_uri(), "status": "SUCCESS"}],
    }})
    with pytest.raises(ToolError) as error:
        if tool == "symbol_info":
            run(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)))
        else:
            run(kt.find_usages(None, "Door", file=str(door(a)), line=3,
                               engine="auto", expected_root=str(a)))
    assert error.value.reason == "semantic_import_scope_unverified"
    assert queries(s) == []


@pytest.mark.parametrize("suffix", ["?selected=other", "#source"])
def test_import_scope_requires_an_unambiguous_folder_uri(project, suffix):
    a, _b = project
    s = serve(a)
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [{"folderUri": a.as_uri() + suffix, "status": "SUCCESS"}],
    }})
    assert run(kt.readiness(expected_root=str(a))).semantic.reason == "semantic_import_scope_unverified"


def test_import_scope_denial_is_not_hidden_by_background_indexing(project):
    a, b = project
    s = serve(a)
    s._begun["indexing"] = 0
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [{"folderUri": b.as_uri(), "status": "SUCCESS"}],
    }})
    assert run(kt.readiness(expected_root=str(a))).semantic.reason == "semantic_import_scope_unverified"


@pytest.mark.parametrize("folders", [None, [], [{}], [None], {}, [{"folderUri": "relative"}],
                                   [{"folderUri": "https://example.invalid/source", "status": "SUCCESS"}]])
def test_missing_or_malformed_import_scope_cannot_enable_semantics(project, folders):
    from test_readiness import queries

    a, _b = project
    s = serve(a)
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": folders,
    }})
    with pytest.raises(ToolError) as error:
        run(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)))
    assert error.value.reason == "semantic_import_scope_unverified"
    assert queries(s) == []


def test_only_the_selected_folders_successful_reimport_restores_readiness(project):
    a, b = project
    s = serve(a)
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [
            {"folderUri": a.as_uri(), "status": "SUCCESS"},
            {"folderUri": b.as_uri(), "status": "SUCCESS"},
        ],
    }})
    assert run(kt.readiness(expected_root=str(a))).semantic.state != "ready"
    s._handle({"method": "intellij/workspaceImportState", "params": {"phase": "IN_PROGRESS"}})
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [{
            "folderUri": a.as_uri().replace("file:///", "file://localhost/"), "status": "SUCCESS",
        }],
    }})
    s._imported_at = 0
    assert run(kt.readiness(expected_root=str(a))).semantic.state == "ready"
    answer = run(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)))
    assert answer.fields["data"]["project_root"] == str(a)


@pytest.mark.parametrize("phase", ["IN_PROGRESS", "CANCELLED", "FAILED"])
def test_a_new_import_invalidates_previous_readiness(project, phase):
    a, _b = project
    s = serve(a)
    s._handle({"method": "intellij/workspaceImportState", "params": {"phase": phase}})
    assert run(kt.readiness(expected_root=str(a))).semantic.state != "ready"


def test_a_successful_reimport_invalidates_the_reviewed_rename(project):
    a, _b = project
    s = serve(a)
    before = tree(a)
    args = {"file": str(door(a)), "line": 3, "expected_root": str(a)}
    preview = run(kt.rename_symbol(None, "Door", "Gate", **args))
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [{"folderUri": a.as_uri(), "status": "SUCCESS"}],
    }})
    s._imported_at = 0  # The fixture has no indexing to settle.
    with pytest.raises(ToolError) as error:
        run(kt.rename_symbol(None, "Door", "Gate", dry_run=False,
                             preview_id=preview.fields["data"]["preview_id"], **args))
    assert error.value.reason == "stale_preview"
    assert tree(a) == before


def test_a_successful_reimport_invalidates_query_continuation(project):
    from test_semantic import loc

    a, _b = project
    s = serve(a, {"textDocument/references": [loc(door(a), 2), loc(door(a), 4)]})
    args = {"file": str(door(a)), "line": 3, "expected_root": str(a), "engine": "semantic"}
    first = run(kt.find_usages(None, "Door", limit=1, **args))
    s._handle({"method": "intellij/workspaceImportState", "params": {
        "phase": "FINISHED", "folders": [{"folderUri": a.as_uri(), "status": "SUCCESS"}],
    }})
    s._imported_at = 0
    with pytest.raises(ToolError) as error:
        run(kt.find_usages(None, "Door", offset=1,
                           query_id=first.fields["data"]["query_id"], **args))
    assert error.value.reason == "stale_continuation"


@pytest.mark.parametrize("stage", ["prepare", "plan"])
def test_a_reimport_during_rename_cannot_produce_a_preview(project, monkeypatch, stage):
    from test_semantic import rng

    from ide_mcp import lsp

    a, _b = project

    def reimport():
        s._handle({"method": "intellij/workspaceImportState", "params": {
            "phase": "FINISHED", "folders": [{"folderUri": a.as_uri(), "status": "SUCCESS"}],
        }})
        s._imported_at = 0

    def prepare(_params):
        reimport()
        return {"range": rng(2, 6, 2, 10), "placeholder": "Door"}

    s = serve(a, {"textDocument/prepareRename": prepare} if stage == "prepare" else None)
    if stage == "plan":
        original = lsp.plan_workspace_edit

        def plan(*args):
            changes = original(*args)
            reimport()
            return changes

        monkeypatch.setattr(lsp, "plan_workspace_edit", plan)
    before = tree(a)
    with pytest.raises(ToolError) as error:
        run(kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3,
                             expected_root=str(a)))
    assert error.value.reason == "semantic_import_changed"
    assert tree(a) == before


def test_a_reimport_during_apply_preflight_blocks_before_writing(project, monkeypatch):
    from ide_mcp import guard, lsp

    a, _b = project
    s = serve(a)
    args = {"file": str(door(a)), "line": 3, "expected_root": str(a)}
    preview = run(kt.rename_symbol(None, "Door", "Gate", **args))
    before = tree(a)
    reading = guard.Scope.read_bytes
    writing = lsp.write_changes
    preflight = False

    def read(scope, path, *args, **kwargs):
        data = reading(scope, path, *args, **kwargs)
        if preflight:
            s._handle({"method": "intellij/workspaceImportState", "params": {
                "phase": "FINISHED", "folders": [{"folderUri": a.as_uri(), "status": "SUCCESS"}],
            }})
            s._imported_at = 0
        return data

    def write(*args, **kwargs):
        nonlocal preflight
        preflight = True
        return writing(*args, **kwargs)

    monkeypatch.setattr(guard.Scope, "read_bytes", read)
    monkeypatch.setattr(lsp, "write_changes", write)
    with pytest.raises(ToolError) as error:
        run(kt.rename_symbol(None, "Door", "Gate", dry_run=False,
                             preview_id=preview.fields["data"]["preview_id"], **args))
    assert error.value.reason == "semantic_import_changed"
    assert tree(a) == before


def test_a_reimport_during_navigation_cannot_return_semantic_results(project):
    from test_semantic import loc

    a, _b = project

    def definition(_params):
        s._handle({"method": "intellij/workspaceImportState", "params": {
            "phase": "FINISHED", "folders": [{"folderUri": a.as_uri(), "status": "SUCCESS"}],
        }})
        s._imported_at = 0
        return [loc(door(a), 2, 6, 10)]

    s = serve(a, {"textDocument/definition": definition})
    with pytest.raises(ToolError) as error:
        run(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)))
    assert error.value.reason == "semantic_import_changed"


def test_partial_import_blocks_before_rename_preview(project):
    a, _b = project
    s = serve(a)
    s.workaround = frozenset({"androidTest", "unitTest"})
    before = tree(a)
    with pytest.raises(ToolError, match="coverage|partial"):
        run(kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3,
                             expected_root=str(a)))
    assert tree(a) == before


def test_unresolved_declaration_cannot_bind_a_preview(project):
    a, _b = project
    serve(a, responses={"textDocument/definition": []})
    with pytest.raises(ToolError, match="resolved|declaration"):
        run(kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3,
                             expected_root=str(a)))


def test_empty_symbol_info_has_partial_provenance(project):
    a, _b = project
    s = serve(a, responses={"textDocument/definition": [], "textDocument/hover": None})
    s.workaround = frozenset({"unitTest"})
    text = run(kt.symbol_info(None, str(door(a)), 3, name="Door", expected_root=str(a)))
    assert "engine=semantic" in text and "complete=false" in text


def test_rename_preview_includes_full_focused_diff_and_byte_revisions(project):
    a, _b = project
    serve(a)
    text = run(kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, expected_root=str(a)))
    assert "-class Door" in text and "+class Gate" in text
    assert text.fields["data"]["edits"][0]["expected_revision"]


def test_query_continuation_rejects_changed_results(project):
    from test_semantic import loc
    a, _b = project
    serve(a, {"textDocument/references": [loc(door(a), 2), loc(door(a), 4)]})
    first = run(kt.find_usages(None, "Door", file=str(door(a)), line=3, limit=1, expected_root=str(a)))
    query_id = first.fields["data"]["query_id"]
    serve(a, {"textDocument/references": []})
    with pytest.raises(ToolError, match="stale|continuation"):
        run(kt.find_usages(None, "Door", file=str(door(a)), line=3, offset=1,
                           query_id=query_id, expected_root=str(a)))


@pytest.mark.parametrize("source,destination", [("Use.kt", "Other.kt"), ("Door.kt", "Wrong.kt")])
def test_backend_moves_must_match_the_declaration_filename_convention(project, source, destination):
    from ide_mcp import lsp

    a, _b = project
    edit = edits_for(a)
    edit["documentChanges"].append({"kind": "rename",
                                   "oldUri": lsp.uri_of(door(a).with_name(source)),
                                   "newUri": lsp.uri_of(door(a).with_name(destination))})
    serve(a, {"textDocument/rename": edit})
    before = tree(a)
    with pytest.raises(ToolError, match="move|filename"):
        run(kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, expected_root=str(a)))
    assert tree(a) == before

