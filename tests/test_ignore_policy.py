"""Ignore-check failures retain their original diagnostics at the MCP boundary."""

import asyncio
import subprocess

import pytest

from ide_mcp import common
from ide_mcp import files_server as fs


@pytest.mark.parametrize("operation,arguments", [
    ("search_in_files", {"query": "match", "file_glob": "*.md"}),
    ("find_files", {"glob": "*.md"}),
    ("list_directory_tree", {}),
])
@pytest.mark.parametrize("stderr", [
    "fatal: empty string is not a valid pathspec.\nAdditional détail.\n".encode("utf-8"),
    b"",
], ids=["stderr", "no_stderr"])
def test_ignore_failure_relays_exact_git_stderr(tmp_path, monkeypatch, operation, arguments, stderr):
    (tmp_path / ".git").mkdir()

    def failed_git(argv, **kwargs):
        assert argv == ["git", "-C", str(tmp_path), "check-ignore", "--stdin", "-z"]
        return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=stderr)

    monkeypatch.setattr(common.subprocess, "run", failed_git)
    result = asyncio.run(fs.mcp.call_tool(operation, {**arguments, "expected_root": str(tmp_path)}))
    text = result.content[0].text
    assert result.isError is True
    assert text.endswith(stderr.decode("utf-8"))
    assert "status 128" in text
    assert "Git ignore policy could not be checked." not in text
    assert result.structuredContent["result"] == text
    assert result.structuredContent["reason"] == "ignore_policy_unavailable"
    assert result.structuredContent["category"] == "environment"


def test_ignore_failure_relays_real_git_error(tmp_path):
    (tmp_path / ".git").mkdir()
    argv = ["git", "-C", str(tmp_path), "check-ignore", "--stdin", "-z"]
    failed = subprocess.run(argv, input=b"note.md\0", capture_output=True, timeout=10, check=False)
    assert failed.returncode == 128
    assert failed.stderr

    result = asyncio.run(fs.mcp.call_tool("search_in_files", {
        "query": "match", "file_glob": "*.md", "expected_root": str(tmp_path),
    }))
    assert result.isError is True
    assert result.content[0].text.endswith(failed.stderr.decode("utf-8"))
    assert result.structuredContent["result"] == result.content[0].text


def test_ignore_failure_keeps_diagnostic_but_redacts_credentials(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def failed_git(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 128, stdout=b"", stderr=b"fatal: cannot read ignore rules\npassword=synthetic-ignore-secret\n",
        )

    monkeypatch.setattr(common.subprocess, "run", failed_git)
    result = asyncio.run(fs.mcp.call_tool("search_in_files", {
        "query": "match", "file_glob": "*.md", "expected_root": str(tmp_path),
    }))
    text = result.content[0].text
    assert result.isError is True
    assert "fatal: cannot read ignore rules\npassword=[redacted]\n" in text
    assert "synthetic-ignore-secret" not in str(result)
    assert result.structuredContent["result"] == text


@pytest.mark.parametrize("returncode,matching_lines", [(0, 0), (1, 1)])
def test_successful_ignore_checks_keep_search_behaviour(tmp_path, monkeypatch, returncode, matching_lines):
    (tmp_path / ".git").mkdir()
    note = tmp_path / "note.md"
    note.write_text("match\n", encoding="utf-8")

    def git_result(argv, **kwargs):
        stdout = str(note).encode("utf-8") + b"\0" if returncode == 0 else b""
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(common.subprocess, "run", git_result)
    result = asyncio.run(fs.mcp.call_tool("search_in_files", {
        "query": "match", "file_glob": "*.md", "expected_root": str(tmp_path),
    }))
    assert result.isError is False
    assert result.structuredContent["data"]["matching_lines"] == matching_lines


@pytest.mark.parametrize("failure", [
    FileNotFoundError(2, "The system cannot find the file specified", "git"),
    PermissionError(13, "Permission denied", "git"),
    subprocess.TimeoutExpired(["git", "check-ignore"], 10),
    subprocess.TimeoutExpired(["git", "check-ignore"], 10, stderr=b"fatal: cannot read ignore rules\n"),
], ids=["missing_git", "launch_denied", "timeout", "timeout_with_stderr"])
def test_ignore_failure_relays_launch_and_timeout_errors(tmp_path, monkeypatch, failure):
    (tmp_path / ".git").mkdir()

    def failed_git(argv, **kwargs):
        raise failure

    monkeypatch.setattr(common.subprocess, "run", failed_git)
    result = asyncio.run(fs.mcp.call_tool("search_in_files", {
        "query": "match", "file_glob": "*.md", "expected_root": str(tmp_path),
    }))
    text = result.content[0].text
    assert result.isError is True
    assert str(failure) in text
    if isinstance(failure, subprocess.TimeoutExpired) and failure.stderr:
        assert text.endswith(failure.stderr.decode("utf-8"))
    assert "Git ignore policy could not be checked." not in text
    assert result.structuredContent["result"] == text
    assert result.structuredContent["reason"] == "ignore_policy_unavailable"
