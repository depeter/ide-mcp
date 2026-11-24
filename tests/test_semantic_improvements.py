"""Additional mutation prerequisites, using the existing semantic fixture."""

import pytest
from conftest import run
from test_readiness import lsp_env as lsp_env  # noqa: PLC0414 - expose the pytest fixture
from test_scope import door
from test_semantic import edits_for, serve, tree
from test_semantic import project as project  # noqa: PLC0414 - expose the pytest fixture

from ide_mcp import kotlin_server as kt
from ide_mcp.common import ToolError


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

