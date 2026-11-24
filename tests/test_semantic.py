"""Semantic navigation and rename: results say how they were obtained, rename is previewed and
then applied only as reviewed, and implementation and call queries are direct and bounded."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from conftest import run
from test_readiness import fake_session, lsp_env, queries  # noqa: F401  (lsp_env is a fixture)
from test_scope import door

USE = """\
package demo

// Door is mentioned in this comment
val label = "Door"
val pair = Door() to Door()

fun close(d: Door) = d
fun close(d: Door, hard: Boolean) = d
"""
OTHER = "package demo.other\n\nclass Door\n"


def rng(l1, c1, l2, c2):
    return {"start": {"line": l1, "character": c1}, "end": {"line": l2, "character": c2}}


def loc(path: Path, line: int, c1: int = 0, c2: int = 4):
    return {"uri": path.as_uri(), "range": rng(line, c1, line, c2)}


@pytest.fixture
def project(worktrees, lsp_env):  # noqa: F811
    """Worktree A with same-named declarations in two packages, overloads, two occurrences on
    one line, a comment, a string and a declaration file named after its class."""
    a, b = worktrees
    src = door(a).parent
    (src / "Use.kt").write_text(USE)
    (src / "other").mkdir()
    (src / "other" / "Door.kt").write_text(OTHER)
    return a, b


def edits_for(a: Path) -> dict:
    """What the backend returns for renaming demo.Door to Gate: the declaration, its use in the
    same file, and the three code uses in Use.kt. Not the comment, the string or demo.other.Door."""
    use = door(a).parent / "Use.kt"
    return {
        "documentChanges": [
            {"textDocument": {"uri": door(a).as_uri(), "version": 1}, "edits": [
                {"range": rng(2, 6, 2, 10), "newText": "Gate"}, {"range": rng(4, 12, 4, 16), "newText": "Gate"}]},
            {"textDocument": {"uri": use.as_uri(), "version": 1}, "edits": [
                {"range": rng(4, 11, 4, 15), "newText": "Gate"}, {"range": rng(4, 21, 4, 25), "newText": "Gate"},
                {"range": rng(6, 13, 6, 17), "newText": "Gate"}, {"range": rng(7, 13, 7, 17), "newText": "Gate"}]},
        ]
    }


def serve(a: Path, responses: dict | None = None, **kw):
    from ide_mcp import lsp

    base = {
        "textDocument/definition": [loc(door(a), 2, 6, 10)],
        "textDocument/prepareRename": {"range": rng(2, 6, 2, 10), "placeholder": "Door"},
        "textDocument/rename": edits_for(a),
    }
    s = fake_session(a, responses={**base, **(responses or {})}, **kw)
    lsp._sessions[lsp.session_key(a)] = s
    return s


def header(text: str) -> dict:
    m = re.search(r"^\[engine=(\S+) requested=(\S+) project=(.+?) complete=(true|false)(?: incomplete_reason=(\S+))?\]$", text, re.M)
    assert m, text
    return {"engine": m.group(1), "requested": m.group(2), "project": m.group(3), "complete": m.group(4) == "true", "reason": m.group(5)}


def error(coro):
    from ide_mcp.common import OutcomeError

    with pytest.raises(OutcomeError) as info:
        run(coro)
    return info.value


# ----------------------------------------------------------------------------- K02 provenance


def test_find_usages_states_engine_project_symbol_and_completeness(project):
    from ide_mcp import kotlin_server as kt

    a, _b = project
    use = door(a).parent / "Use.kt"
    refs = [loc(door(a), 2, 6, 10), loc(door(a), 4, 12, 16), loc(use, 4, 11, 15), loc(use, 4, 21, 25), loc(use, 6, 13, 17)]

    async def call(**kw):
        if not kw.get("query_id"):
            serve(a, {"textDocument/references": refs})
        return await kt.find_usages(None, "Door", file=str(door(a)), line=3, expected_root=str(a), **kw)

    out = run(call())
    h = header(out)
    assert h == {"engine": "semantic", "requested": "auto", "project": str(a), "complete": True, "reason": None}
    assert "symbol: Door, declared at app/src/main/kotlin/Door.kt:3" in out and "class Door" in out
    assert "demo/other" not in out and "label" not in out

    out = run(call(engine="semantic", limit=2))
    h = header(out)
    assert (h["engine"], h["complete"], h["reason"]) == ("semantic", False, "truncated")
    assert "offset=2" in out
    rest = run(call(engine="semantic", limit=2, offset=2, query_id=out.fields["data"]["query_id"]))
    assert header(rest)["complete"] is True and "Use.kt" in rest

    async def empty():
        serve(a, {"textDocument/references": []})
        return await kt.find_usages(None, "Door", file=str(door(a)), line=3, engine="semantic", expected_root=str(a))

    out = run(empty())
    assert header(out)["complete"] is True and "No references" in out


def test_empty_answer_from_a_partial_import_is_not_reported_as_complete(project):
    from ide_mcp import kotlin_server as kt

    a, _b = project
    (a / "app" / "src" / "androidTest" / "kotlin").mkdir(parents=True)
    (a / "app" / "src" / "androidTest" / "kotlin" / "T.kt").write_text("package demo\n")

    async def call():
        s = serve(a, {"textDocument/references": []})
        s.workaround = frozenset({"androidTest"})
        return await kt.find_usages(None, "Door", file=str(door(a)), line=3, engine="semantic", expected_root=str(a))

    out = run(call())
    h = header(out)
    assert (h["complete"], h["reason"]) == (False, "partial_import")
    assert "not proof" in out


def test_auto_falls_back_to_labelled_read_only_text_and_semantic_does_not(project, effects, monkeypatch):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, b = project
    monkeypatch.setattr(lsp, "find_server", lambda: None)
    effects.events.clear()
    out = run(kt.find_usages(None, "Door", expected_root=str(a)))
    h = header(out)
    assert (h["engine"], h["requested"], h["complete"], h["reason"]) == ("text", "auto", False, "text_match")
    assert "semantic_not_installed" in out and "not confirmed" in out
    assert "other/Door.kt" in out  # text cannot tell the two classes apart, and says so
    assert effects.of("write") == []
    assert error(kt.find_usages(None, "Door", engine="semantic", expected_root=str(a))).reason == "semantic_not_installed"
    # A scope denial is not something to fall back from.
    assert error(kt.find_usages(None, "Door", str(b), expected_root=str(a))).reason == "target_mismatch"
    assert error(kt.find_usages(None, "Door", file=str(door(b)), line=3, expected_root=str(a))).reason == "target_mismatch"


def test_ambiguous_selection_is_rejected_with_candidates_and_a_column_resolves_it(project):
    from ide_mcp import kotlin_server as kt

    a, _b = project
    use = door(a).parent / "Use.kt"
    other = door(a).parent / "other" / "Door.kt"
    seen = []

    def references(params):
        seen.append(params["position"])
        return [loc(use, 4, 21, 25)]

    async def call(**kw):
        serve(a, {
            "textDocument/references": references,
            "workspace/symbol": [
                {"name": "Door", "kind": 5, "containerName": "demo", "location": loc(door(a), 2, 6, 10)},
                {"name": "Door", "kind": 5, "containerName": "demo.other", "location": loc(other, 2, 6, 10)},
            ],
        })
        return await kt.find_usages(None, "Door", engine="semantic", expected_root=str(a), **kw)

    e = error(call())  # two declarations with this name
    assert e.reason == "ambiguous_symbol" and "demo.other" in str(e) and f"file={other}" in str(e)
    e = error(call(file=str(use), line=5))  # two occurrences on the line
    assert e.reason == "ambiguous_symbol" and "column=12" in str(e) and "column=22" in str(e)
    e = error(call(file=str(use)))  # several lines in the file
    assert e.reason == "ambiguous_symbol" and "line=5" in str(e) and "line=7" in str(e)
    assert seen == []
    run(call(file=str(use), line=5, column=22))
    assert seen == [{"line": 4, "character": 21}]
    e = error(call(file=str(use), line=5, column=3))
    assert "column 3" in str(e)


# ----------------------------------------------------------------------------- K03 preview


def preview_id(text: str) -> str:
    m = re.search(r"^preview_id: ([0-9a-f]{16})$", text, re.M)
    assert m, text
    return m.group(1)


def tree(a: Path) -> dict:
    return {p.relative_to(a).as_posix(): p.read_bytes() for p in sorted((a / "app").rglob("*")) if p.is_file()}


def test_rename_never_uses_text_replacement(project, effects, monkeypatch):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, _b = project
    before = tree(a)
    effects.events.clear()
    f = str(door(a))
    e = error(kt.rename_symbol(None, "Door", "Gate", engine="text", dry_run=False, expected_root=str(a)))
    assert e.reason == "text_rename_rejected" and "find_usages" in str(e)
    assert not hasattr(kt, "_text_rename")

    monkeypatch.setattr(lsp, "find_server", lambda: None)
    for engine in ("auto", "semantic"):
        for dry_run in (True, False):
            e = error(kt.rename_symbol(None, "Door", "Gate", file=f, line=3, engine=engine, dry_run=dry_run, expected_root=str(a)))
            assert (e.outcome, e.reason) == ("blocked", "semantic_not_installed")
    monkeypatch.setattr(lsp, "find_server", lambda: Path("kotlin-lsp-launcher"))

    async def failed_import():
        serve(a, status="FAILURE")
        return await kt.rename_symbol(None, "Door", "Gate", file=f, line=3, dry_run=False, expected_root=str(a))

    assert error(failed_import()).reason == "semantic_import_failed"

    async def ambiguous():
        serve(a)
        return await kt.rename_symbol(None, "Door", "Gate", file=str(door(a).parent / "Use.kt"), line=5, expected_root=str(a))

    assert error(ambiguous()).reason == "ambiguous_symbol"

    async def not_this_symbol():
        serve(a, {"textDocument/prepareRename": {"range": rng(2, 0, 2, 5), "placeholder": "class"}})
        return await kt.rename_symbol(None, "Door", "Gate", file=f, line=3, expected_root=str(a))

    assert error(not_this_symbol()).reason == "symbol_mismatch"

    async def not_renameable():
        serve(a, {"textDocument/prepareRename": None})
        return await kt.rename_symbol(None, "Door", "Gate", file=f, line=3, expected_root=str(a))

    assert "cannot rename" in str(error(not_renameable()))
    assert effects.of("write") == [] and tree(a) == before


def test_default_is_a_preview_that_changes_nothing_and_describes_the_edit(project, effects):
    from ide_mcp import kotlin_server as kt

    a, _b = project
    before = tree(a)
    effects.events.clear()

    async def call():
        serve(a)
        return await kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, expected_root=str(a))  # dry_run omitted

    out = run(call())
    assert out.startswith("[outcome=not_run reason=dry_run]")
    h = header(out)
    assert (h["engine"], h["project"], h["complete"]) == ("semantic", str(a), True)
    assert "symbol: Door, declared at app/src/main/kotlin/Door.kt:3" in out
    assert "app/src/main/kotlin/Door.kt (2)  -> will be renamed to Gate.kt" in out
    assert "app/src/main/kotlin/Use.kt (3)" in out and "val pair = Gate() to Gate()" in out
    assert len(re.findall(r"revision [0-9a-f]{12}", out)) == 2
    assert "other/Door.kt" not in out
    assert preview_id(out) != preview_id(run(call()))  # a backend reconnect requires a fresh preview
    assert effects.of("write") == [] and tree(a) == before


def test_declaration_file_move_collision_is_rejected_without_changes(project, effects):
    from ide_mcp import kotlin_server as kt

    a, _b = project
    (door(a).parent / "Gate.kt").write_text("package demo\n")
    before = tree(a)
    effects.events.clear()

    async def call(**kw):
        serve(a)
        return await kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, expected_root=str(a), **kw)

    e = error(call())
    assert e.reason == "destination_collision" and "Gate.kt" in str(e)
    assert error(call(rename_files=False)).reason == "declaration_file_required"
    assert effects.of("write") == [] and tree(a) == before


# ----------------------------------------------------------------------------- K04 application


def _apply(a: Path, pid: str | None, responses: dict | None = None, **kw):
    from ide_mcp import kotlin_server as kt

    async def call():
        from ide_mcp import lsp
        if responses is not None or lsp.session_key(a) not in lsp._sessions:
            serve(a, responses)
        return await kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, dry_run=False, preview_id=pid, expected_root=str(a), **kw)

    return call()


def _preview(a: Path) -> str:
    from ide_mcp import kotlin_server as kt

    async def call():
        serve(a)
        return await kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, expected_root=str(a))

    return preview_id(run(call()))


def test_reviewed_preview_is_applied_and_leaves_unrelated_bytes_alone(project):
    a, _b = project
    src = door(a).parent
    door(a).write_bytes(b"\xef\xbb\xbfpackage demo\r\n\r\nclass Door\r\n\r\nfun open(d: Door) = d\r\n")  # BOM + CRLF
    pid = _preview(a)
    out = run(_apply(a, pid))
    assert out.startswith("[outcome=passed]") and header(out)["engine"] == "semantic"
    assert (src / "Gate.kt").read_bytes() == b"\xef\xbb\xbfpackage demo\r\n\r\nclass Gate\r\n\r\nfun open(d: Gate) = d\r\n"
    assert not door(a).exists()
    assert (src / "Use.kt").read_text() == USE.replace("Door() to Door()", "Gate() to Gate()").replace("d: Door", "d: Gate")
    assert '// Door is mentioned in this comment\nval label = "Door"' in (src / "Use.kt").read_text()  # LF kept, comment and string kept
    assert (src / "other" / "Door.kt").read_text() == OTHER


def test_apply_requires_the_reviewed_preview(project, effects):
    a, _b = project
    before = tree(a)
    effects.events.clear()
    e = error(_apply(a, None))
    assert e.reason == "preview_required"
    e = error(_apply(a, "0" * 16))
    assert e.reason == "stale_preview"
    assert effects.of("write") == [] and tree(a) == before


def test_file_changed_after_preview_rejects_before_the_first_write(project, effects):
    a, _b = project
    pid = _preview(a)
    use = door(a).parent / "Use.kt"  # the later file of the two-file edit
    use.write_text(USE + "\n// edited after the preview\n")
    before = tree(a)
    effects.events.clear()
    e = error(_apply(a, pid))
    assert e.reason in ("stale_preview", "stale_document")
    assert effects.of("write") == [] and tree(a) == before and door(a).exists()


def test_file_changed_between_planning_and_writing_rejects_before_the_first_write(project, effects, monkeypatch):
    """The disk file changes behind the engine's back after the edit set is recomputed."""
    from ide_mcp import lsp

    a, _b = project
    pid = _preview(a)
    use = door(a).parent / "Use.kt"
    real_write = lsp.write_changes

    def edit_then_write(changes):
        use.write_text(USE + "// raced\n")
        effects.events.clear()
        return real_write(changes)

    monkeypatch.setattr(lsp, "write_changes", edit_then_write)
    e = error(_apply(a, pid))
    assert e.reason == "stale_preview"
    assert effects.of("write") == [] and door(a).read_text().count("Door") == 2


def test_backend_replacement_or_a_different_edit_set_invalidates_the_preview(project, effects):
    from ide_mcp import lsp

    a, _b = project
    pid = _preview(a)
    before = tree(a)
    different = edits_for(a)
    different["documentChanges"][1]["edits"].pop()  # the new backend resolves one usage fewer
    effects.events.clear()
    assert error(_apply(a, pid, {"textDocument/rename": different})).reason == "stale_preview"
    # A different declaration at the same position (the symbol moved).
    assert error(_apply(a, pid, {"textDocument/definition": [loc(door(a), 4, 4, 8)]})).reason == "stale_preview"
    # The project behind the path was replaced: the session is not reused, and nothing is applied.
    from ide_mcp import kotlin_server as kt

    async def replaced():
        s = serve(a)
        s.ident = lsp.identity(a.parent)  # the stored session is bound to some other directory
        return s, await asyncio.gather(
            kt.rename_symbol(None, "Door", "Gate", file=str(door(a)), line=3, dry_run=False, preview_id=pid, expected_root=str(a)),
            return_exceptions=True,
        )

    stale, (result,) = run(replaced())
    assert getattr(result, "outcome", None) == "blocked" and stale.sent == []
    assert effects.of("write") == [] and tree(a) == before


def test_denied_and_protected_targets_reject_the_application(project, effects, monkeypatch):
    a, b = project
    pid = _preview(a)
    before = tree(a)
    for extra, reason in (
        ({"textDocument": {"uri": (a / "gradle.properties").as_uri(), "version": 0}, "edits": []}, "protected_path"),
        ({"textDocument": {"uri": door(b).as_uri(), "version": 0}, "edits": [{"range": rng(2, 6, 2, 10), "newText": "Gate"}]}, "target_mismatch"),
    ):
        edit = edits_for(a)
        edit["documentChanges"].append(extra)
        effects.events.clear()
        assert error(_apply(a, pid, {"textDocument/rename": edit})).reason == reason
        assert effects.of("write") == [] and effects.touching(b, "open") == []
    monkeypatch.setenv("IDE_MCP_ALLOWED_ROOTS", str(b))
    assert error(_apply(a, pid)).reason == "permission_denied"
    assert tree(a) == before


def test_apply_failure_reports_what_was_and_was_not_written(project, monkeypatch):
    a, _b = project
    pid = _preview(a)
    import os
    real_write = os.replace
    calls = []

    def failing(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_write(source, target)

    monkeypatch.setattr(os, "replace", failing)
    e = error(_apply(a, pid))
    assert (e.outcome, e.reason) == ("failed", "apply_failed")
    text = str(e)
    assert "disk full" in text and "Written:" in text and "Not written:" in text and "not rolled back" in text
    moved = door(a).parent / "Gate.kt"  # the first file really was changed and moved, and the report says so
    assert "class Gate" in moved.read_text() and not door(a).exists() and "-> " in text.split("Not written:")[0]
    assert "Door()" in (door(a).parent / "Use.kt").read_text()
    assert "Door.kt" in text.split("Not written:")[0] and "Use.kt" in text.split("Not written:")[1]


# ----------------------------------------------------------------------------- K05 navigation

SHAPES = """\
package demo

interface Shape {
    fun area(): Int
}

class Square : Shape {
    override fun area(): Int = side() * side()
    private fun side(): Int = 2
}

open class Circle : Shape {
    override fun area(): Int = 3
}

fun total(s: Shape): Int = s.area() + total(s)

fun describe(s: Shape, verbose: Boolean): String = "a"
fun describe(s: Shape): String = describe(s, false)
"""


def item(path: Path, name: str, line: int, c1: int, kind: int = 12, detail: str = "demo"):
    return {"name": name, "kind": kind, "detail": detail, "uri": path.as_uri(),
            "range": rng(line, 0, line, 40), "selectionRange": rng(line, c1, line, c1 + len(name))}


@pytest.fixture
def shapes(project):
    a, b = project
    f = door(a).parent / "Shapes.kt"
    f.write_text(SHAPES)
    return a, b, f


def test_find_implementations(shapes):
    from ide_mcp import kotlin_server as kt

    a, _b, f = shapes

    async def call(**kw):
        if not kw.get("query_id"):
            serve(a, {"textDocument/definition": [loc(f, 3, 8, 12)],
                      "textDocument/implementation": [loc(f, 7, 17, 21), loc(f, 12, 17, 21)]})
        return await kt.find_implementations(None, "area", file=str(f), line=4, expected_root=str(a), **kw)

    out = run(call())
    h = header(out)
    assert (h["engine"], h["requested"], h["complete"]) == ("semantic", "semantic", True)
    assert "symbol: area, declared at app/src/main/kotlin/Shapes.kt:4" in out
    assert "Shapes.kt:8" in out and "override fun area(): Int = side() * side()" in out and "Shapes.kt:13" in out
    out = run(call(limit=1))
    assert (header(out)["complete"], header(out)["reason"]) == (False, "truncated") and "offset=1" in out and "Shapes.kt:13" not in out
    assert "Shapes.kt:13" in run(call(limit=1, offset=1, query_id=out.fields["data"]["query_id"]))


def test_find_callers_keeps_overloads_apart_and_shows_a_cycle_once(shapes):
    from ide_mcp import kotlin_server as kt

    a, _b, f = shapes
    total = item(f, "total", 15, 4)
    prepared = []

    def prepare(params):
        prepared.append(params["position"])
        return [total]

    async def call(**kw):
        s = serve(a, {
            "textDocument/definition": [loc(f, 15, 4, 9)],
            "textDocument/prepareCallHierarchy": prepare,
            "callHierarchy/incomingCalls": [{"from": total, "fromRanges": [rng(15, 38, 15, 43)]}],  # total calls itself
        })
        return s, await kt.find_callers(None, "total", file=str(f), line=16, column=5, expected_root=str(a), **kw)

    s, out = run(call())
    assert prepared == [{"line": 15, "character": 4}]
    assert header(out)["complete"] is True
    assert "1 direct caller" in out and out.count("fun total(s: Shape): Int") == 2  # the symbol and its one caller
    assert "Shapes.kt:16:39" in out
    assert "not a runtime order" in out
    assert queries(s).count("callHierarchy/incomingCalls") == 1  # one direct query, no traversal

    # The two-argument overload is selected by position, and only its caller is listed.
    two = item(f, "describe", 17, 4, detail="demo (Shape, Boolean)")
    one = item(f, "describe", 18, 4, detail="demo (Shape)")

    async def overload():
        serve(a, {
            "textDocument/definition": [loc(f, 17, 4, 12)],
            "textDocument/prepareCallHierarchy": [two],
            "callHierarchy/incomingCalls": [{"from": one, "fromRanges": [rng(18, 33, 18, 41)]}],
        })
        return await kt.find_callers(None, "describe", file=str(f), line=18, expected_root=str(a))

    out = run(overload())
    assert "symbol: describe, declared at app/src/main/kotlin/Shapes.kt:18" in out
    assert "Shapes.kt:19  fun describe(s: Shape): String" in out and "Shapes.kt:19:34" in out


def test_find_callees_marks_dispatch_candidates_and_unresolved_targets(shapes):
    from ide_mcp import kotlin_server as kt

    a, _b, f = shapes
    outside = Path("/library/kotlin/Int.kt") if not str(a).startswith("C:") else Path("C:/library/kotlin/Int.kt")

    async def call(**kw):
        serve(a, {
            "textDocument/definition": [loc(f, 15, 4, 9)],
            "textDocument/prepareCallHierarchy": [item(f, "total", 15, 4)],
            "callHierarchy/outgoingCalls": [
                {"to": item(f, "area", 3, 8, 6), "fromRanges": [rng(15, 29, 15, 33)]},
                {"to": item(f, "total", 15, 4), "fromRanges": [rng(15, 38, 15, 43)]},
                {"to": item(outside, "plus", 10, 4, 6, "kotlin.Int"), "fromRanges": [rng(15, 36, 15, 37)]},
            ],
        })
        return await kt.find_callees(None, "total", file=str(f), line=16, column=5, expected_root=str(a), **kw)

    out = run(call())
    assert "3 direct callee" in out and header(out)["complete"] is True
    area, total, plus = (next(ln for ln in out.splitlines() if marker in ln) for marker in ("Shapes.kt:4 ", "Shapes.kt:16 ", "Int.kt"))
    assert "possible dispatch" in area and "find_implementations" in out
    assert "exact" in total
    assert "unresolved" in plus and "outside" in plus
    assert "not a runtime order" in out
    out = run(call(limit=2))
    assert (header(out)["complete"], header(out)["reason"]) == (False, "truncated") and "offset=2" in out


@pytest.mark.parametrize("tool,method", [
    ("find_implementations", "textDocument/implementation"),
    ("find_callers", "textDocument/prepareCallHierarchy"),
    ("find_callees", "textDocument/prepareCallHierarchy"),
])
def test_unsupported_or_unready_navigation_is_explicit_and_never_text(shapes, monkeypatch, tool, method):
    from ide_mcp import kotlin_server as kt
    from ide_mcp import lsp

    a, b, f = shapes
    fn = getattr(kt, tool)

    async def without_capability():
        s = serve(a, capabilities={"definitionProvider": True, "referencesProvider": True, "workspaceSymbolProvider": True})
        r = await asyncio.gather(fn(None, "area", file=str(f), line=4, expected_root=str(a)), return_exceptions=True)
        return s, r[0]

    s, e = run(without_capability())
    assert (e.outcome, e.reason) == ("blocked", "unsupported_operation") and method not in queries(s)

    async def indexing():
        s = serve(a, done=False)
        s.started = 1e18
        real_wait = s.wait_ready
        s.wait_ready = lambda timeout: real_wait(0.05)
        return await fn(None, "area", file=str(f), line=4, expected_root=str(a))

    assert error(indexing()).reason == "semantic_indexing"
    monkeypatch.setattr(lsp, "find_server", lambda: None)
    assert error(fn(None, "area", file=str(f), line=4, expected_root=str(a))).reason == "semantic_not_installed"
    assert error(fn(None, "area", file=str(door(b)), line=3, expected_root=str(a))).reason == "target_mismatch"
    assert kt.ops.registered[tool].kind == "semantic"
