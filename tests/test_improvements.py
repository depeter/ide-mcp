"""Regression checks for bounded access, reviewed writes and honest execution."""

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

from ide_mcp import common, gradle, guard, lsp
from ide_mcp import files_server as fs


def run(coro):
    return asyncio.run(coro)


def revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_batch_read_has_partial_errors_and_bounded_ranges(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = run(fs.read_files(
        [{"path": str(f), "start_line": 2, "max_lines": 1},
         {"path": str(tmp_path / "missing.txt")}],
        expected_root=str(tmp_path),
    ))
    assert result.fields["complete"] is False
    assert result.fields["data"]["items"][0]["content"] == "two"
    assert result.fields["data"]["items"][0]["continuation"]["start_line"] == 3
    assert result.fields["data"]["items"][1]["outcome"] == "failed"


def test_shallow_tree_does_not_enumerate_collapsed_children(tmp_path, monkeypatch):
    child = tmp_path / "child"
    (child / "deep").mkdir(parents=True)
    (child / "deep" / "hidden.txt").write_text("x")
    (tmp_path / "empty").mkdir()
    visited = []
    original = os.scandir

    def scan(path):
        visited.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    text = run(fs.list_directory_tree(str(tmp_path), max_depth=1, expected_root=str(tmp_path)))
    assert "child/" in text and "empty/" in text
    assert "hidden.txt" not in text
    assert child not in visited
    assert "files)" not in text


def test_existing_mutation_cannot_bypass_revision(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\n")
    with pytest.raises(common.ToolError, match="revision"):
        run(fs.replace_text(str(f), "one", "two", expected_root=str(tmp_path)))
    assert f.read_text() == "one\n"


def test_exact_batch_preview_and_stale_apply_preserve_bytes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    edits = [{"path": str(f), "expected_revision": revision(f),
              "replacements": [{"old_text": "one", "new_text": "three"}]}]
    preview = run(fs.edit_files(edits, expected_root=str(tmp_path)))
    assert preview.fields["outcome"] == "not_run"
    assert f.read_bytes() == b"\xef\xbb\xbfone\r\ntwo\r\n"
    f.write_bytes(b"\xef\xbb\xbfuser\r\ntwo\r\n")
    with pytest.raises(common.ToolError, match="revision|stale"):
        run(fs.edit_files(edits, dry_run=False,
                          preview_id=preview.fields["data"]["preview_id"],
                          expected_root=str(tmp_path)))
    assert f.read_bytes() == b"\xef\xbb\xbfuser\r\ntwo\r\n"


def test_exact_batch_preflights_every_file(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    edits = [{"path": str(f), "expected_revision": revision(f),
              "replacements": [{"old_text": old, "new_text": "new"}]}
             for f, old in ((a, "a"), (b, "missing"))]
    with pytest.raises(common.ToolError):
        run(fs.edit_files(edits, dry_run=False, preview_id="bad", expected_root=str(tmp_path)))
    assert a.read_text() == "a" and b.read_text() == "b"


def test_atomic_writer_checks_revision_at_replacement(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_bytes(b"old")
    expected = revision(f)
    original = os.chmod

    def changed(path, mode):
        original(path, mode)
        f.write_bytes(b"user")

    monkeypatch.setattr(os, "chmod", changed)
    with guard.activate(guard.Scope.of(str(tmp_path))), pytest.raises(common.ToolError, match="revision|stale"):
        common.active().write_bytes(f, b"new", expected_revision=expected)
    assert f.read_bytes() == b"user"


@pytest.mark.parametrize("position", [
    {"line": -1, "character": 0}, {"line": 0, "character": 99},
    {"line": 3, "character": 0}, {"line": 0, "character": 1},
])
def test_invalid_lsp_ranges_are_rejected(position):
    with pytest.raises(common.ToolError):
        lsp.apply_text_edits("😀a\n", [{
            "range": {"start": position, "end": position}, "newText": "x",
        }])


def test_overlapping_lsp_ranges_are_rejected():
    with pytest.raises(common.ToolError):
        lsp.apply_text_edits("abcd", [
            {"range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 0, "character": 3}}, "newText": "x"},
            {"range": {"start": {"line": 0, "character": 2},
                       "end": {"line": 0, "character": 4}}, "newText": "y"},
        ])


def test_skipped_tests_are_not_executed_and_bad_report_is_incomplete(tmp_path):
    (tmp_path / "TEST-a.xml").write_text(
        '<testsuite tests="1" skipped="1"><testcase name="a"><skipped/></testcase></testsuite>')
    (tmp_path / "TEST-b.xml").write_text("<invalid")
    with guard.activate(guard.Scope.of(str(tmp_path))):
        result = gradle.collect_tests([tmp_path], 0)
    assert result.executed == 0
    assert result.complete is False
    assert "0 executed" in result.render()


def test_runner_sanitises_before_streaming_and_clipping(tmp_path):
    lines = []
    with guard.activate(guard.Scope.of(str(tmp_path))):
        result = run(common.run_proc(
            [sys.executable, "-c",
             "import sys;sys.stdout.write('password=synthetic-secret\\nPAN=4111111111111111\\n')"],
            cwd=tmp_path, on_line=lines.append, max_bytes=20,
        ))
    assert "synthetic-secret" not in result.output
    assert "4111111111111111" not in "\n".join(lines)
    assert result.output_complete is False


def test_structured_read_does_not_leak_redacted_credentials(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("password=synthetic-secret\n", encoding="utf-8")
    result = run(fs.mcp.call_tool("read_file", {"path": str(f), "expected_root": str(tmp_path)}))
    assert "synthetic-secret" not in str(result.structuredContent)


def test_explicit_protected_shell_target_is_blocked_before_launch(tmp_path):
    with pytest.raises(common.ToolError, match="protected_path"):
        run(fs.run_command("Get-Content local.properties", expected_root=str(tmp_path)))


def test_all_sources_support_declared_python_310_syntax():
    import ast
    for path in (Path(__file__).parents[1] / "src/ide_mcp").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


def test_batch_staging_failure_changes_no_source(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    edits = [{"path": str(f), "expected_revision": revision(f),
              "replacements": [{"old_text": old, "new_text": "new"}]}
             for f, old in ((a, "a"), (b, "b"))]
    preview = run(fs.edit_files(edits, expected_root=str(tmp_path)))
    original, calls = guard.tempfile.mkstemp, []

    def denied(*args, **kwargs):
        calls.append(kwargs.get("dir"))
        if len(calls) == 2:
            raise PermissionError("synthetic staging denial")
        return original(*args, **kwargs)
    monkeypatch.setattr(guard.tempfile, "mkstemp", denied)
    try:
        run(fs.edit_files(edits, dry_run=False, preview_id=preview.fields["data"]["preview_id"],
                          expected_root=str(tmp_path)))
    except common.ToolError:
        pass
    assert a.read_bytes() == b"a" and b.read_bytes() == b"b"


def test_scope_does_not_cache_a_changed_worktree_boundary(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    scope = guard.Scope.of(str(tmp_path))
    scope.check(nested, protected=False)
    (nested / ".git").mkdir()
    with pytest.raises(common.ToolError, match="worktree"):
        scope.check(nested, protected=False)


def test_exact_edit_keeps_explicit_legacy_encoding_and_unrelated_eof(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes(b"caf\xe9\r\nold")
    edits = [{"path": str(path), "encoding": "cp1252", "expected_revision": revision(path),
              "replacements": [{"old_text": "old", "new_text": "new"}]}]
    preview = run(fs.edit_files(edits, expected_root=str(tmp_path)))
    run(fs.edit_files(edits, dry_run=False, preview_id=preview.fields["data"]["preview_id"],
                      expected_root=str(tmp_path)))
    assert path.read_bytes() == b"caf\xe9\r\nnew"


def test_batch_requests_are_bounded_in_direct_and_sdk_routes(tmp_path):
    with pytest.raises(common.ToolError, match="32|bound"):
        run(fs.read_files([{"path": str(tmp_path / "missing")}] * 33, expected_root=str(tmp_path)))


def test_later_application_failure_reports_the_real_changed_set(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    edits = [{"path": str(f), "expected_revision": revision(f),
              "replacements": [{"old_text": old, "new_text": "new"}]}
             for f, old in ((a, "a"), (b, "b"))]
    preview = run(fs.edit_files(edits, expected_root=str(tmp_path)))
    original = os.replace

    def replace(source, destination):
        if Path(destination) == b:
            raise PermissionError("synthetic second write failure")
        return original(source, destination)
    monkeypatch.setattr(os, "replace", replace)
    result = run(fs.edit_files(edits, dry_run=False, preview_id=preview.fields["data"]["preview_id"],
                               expected_root=str(tmp_path)))
    assert result.fields["outcome"] == "failed"
    assert result.fields["data"]["written"] == [str(a)]
    assert result.fields["data"]["pending"] == [str(b)]
    assert a.read_bytes() == b"new" and b.read_bytes() == b"b"


@pytest.mark.parametrize("payload,encoding", [(b"a\0b", None), (b"\xff", None), (b"text", "utf-16")])
def test_unsupported_ranges_do_not_claim_text_success(tmp_path, payload, encoding):
    path = tmp_path / "unsupported.txt"
    path.write_bytes(payload)
    with pytest.raises(common.ToolError):
        run(fs.read_file(str(path), encoding=encoding, expected_root=str(tmp_path)))



