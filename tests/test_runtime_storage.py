"""Execution bookkeeping stays outside the selected workspace."""

import asyncio
import hashlib
import tempfile
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from ide_mcp import common, execution, guard


@pytest.fixture
def runtime_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    temporary = tmp_path / "temporary"
    workspace.mkdir()
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.delenv("IDE_MCP_ALLOWED_ROOTS", raising=False)
    return workspace, temporary


def test_logs_are_temporary_sanitised_and_retrievable(runtime_workspace):
    from ide_mcp import files_server as fs

    workspace, temporary = runtime_workspace
    with guard.activate(guard.Scope.of(str(workspace))):
        artifact = execution.save_artifact("safe output\npassword=synthetic-secret")
    path = Path(artifact["path"])
    assert path.is_relative_to(temporary / "ide-mcp")
    assert not list(workspace.iterdir())
    assert artifact["verified"] and artifact["sanitised"]
    assert b"synthetic-secret" not in path.read_bytes()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    arguments = {"path": str(path), "sha256": artifact["sha256"],
                 "expected_root": artifact["output_scope"]}
    result = asyncio.run(fs.mcp.call_tool("read_artifact", arguments))
    assert isinstance(result, CallToolResult) and result.structuredContent is not None
    assert not result.isError
    assert "safe output" in result.structuredContent["result"]
    assert "synthetic-secret" not in str(result.structuredContent)
    path.write_text("changed", encoding="utf-8")
    result = asyncio.run(fs.mcp.call_tool("read_artifact", arguments))
    assert isinstance(result, CallToolResult) and result.structuredContent is not None
    assert result.isError and result.structuredContent["reason"] == "artifact_changed"


def test_log_retention_is_separate_for_each_workspace(runtime_workspace):
    workspace, _temporary = runtime_workspace
    other = workspace.parent / "other"
    other.mkdir()
    with guard.activate(guard.Scope.of(str(other))):
        other_artifact = execution.save_artifact("other")
    with guard.activate(guard.Scope.of(str(workspace))):
        artifact = [execution.save_artifact(str(number)) for number in range(32)][-1]
    logs = Path(artifact["path"]).parent
    assert len(list(logs.glob("*.safe.log"))) == 30
    assert Path(other_artifact["path"]).exists()
    assert other_artifact["output_scope"] != artifact["output_scope"]
    assert not list(workspace.iterdir()) and not list(other.iterdir())


def test_explicit_output_scope_does_not_put_logs_in_it(runtime_workspace):
    workspace, temporary = runtime_workspace
    output = workspace.parent / "output"
    output.mkdir()
    with guard.activate(guard.Scope.of(str(workspace))):
        artifact = execution.save_artifact("safe", output_scope=guard.Scope.of(str(output)))
    assert Path(artifact["path"]).is_relative_to(temporary / "ide-mcp")
    assert not list(workspace.iterdir()) and not list(output.iterdir())


def test_workspace_lock_is_stable_and_temporary(runtime_workspace):
    workspace, temporary = runtime_workspace

    async def check():
        with guard.activate(guard.Scope.of(str(workspace))):
            async with execution.WorkspaceLease(workspace):
                locks = list((temporary / "ide-mcp").rglob("validation.lock"))
                assert len(locks) == 1
                assert not list(workspace.iterdir())
            async with execution.WorkspaceLease(workspace):
                assert list((temporary / "ide-mcp").rglob("validation.lock")) == locks
            other = workspace / "other"
            other.mkdir()
            async with execution.WorkspaceLease(other):
                assert len(list((temporary / "ide-mcp").rglob("validation.lock"))) == 2
    asyncio.run(check())


def test_denied_temporary_storage_never_falls_back_to_workspace(runtime_workspace, monkeypatch):
    workspace, temporary = runtime_workspace
    monkeypatch.setenv("IDE_MCP_ALLOWED_ROOTS", str(workspace))
    with guard.activate(guard.Scope.of(str(workspace))):
        artifact = execution.save_artifact("safe")
        assert artifact["path"] is None and not artifact["verified"]
        with pytest.raises(common.Blocked, match="permission_denied"):
            asyncio.run(execution.WorkspaceLease(workspace).__aenter__())
    assert not list(workspace.iterdir()) and not list(temporary.iterdir())


def test_workspace_path_is_checked_before_creating_temporary_files(runtime_workspace):
    workspace, temporary = runtime_workspace
    other = workspace.parent / "other"
    other.mkdir()
    with guard.activate(guard.Scope.of(str(workspace))), pytest.raises(common.Blocked, match="target_mismatch"):
        asyncio.run(execution.WorkspaceLease(other).__aenter__())
    assert not list(temporary.iterdir()) and not list(other.iterdir())


def test_temporary_directory_inside_workspace_is_rejected(runtime_workspace, monkeypatch):
    workspace, _temporary = runtime_workspace
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(workspace))
    with guard.activate(guard.Scope.of(str(workspace))):
        artifact = execution.save_artifact("safe")
        assert artifact["path"] is None and not artifact["verified"]
        with pytest.raises(common.Blocked, match="temporary_directory_in_workspace"):
            asyncio.run(execution.WorkspaceLease(workspace).__aenter__())
    assert not list(workspace.iterdir())
