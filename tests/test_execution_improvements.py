"""Run ownership and validation-adapter acceptance with disposable inputs."""

import asyncio
import sys

import pytest

from ide_mcp import common, guard


def test_job_cursors_replay_and_final_result(tmp_path):
    from ide_mcp.execution import Jobs

    async def check():
        jobs = Jobs()
        with guard.activate(guard.Scope.of(str(tmp_path))):
            async def work():
                r = await common.run_proc([sys.executable, "-c", "print('password=synthetic')"], cwd=tmp_path)
                return guard.result(r.output)
            job = jobs.start(work)
            assert job.state == "pending"
            await job.task
            assert job.state == "finished" and job.final["outcome"] == "passed"
            first = jobs.output(job, 0, 100)
            assert first == jobs.output(job, 0, 100)
            assert "synthetic" not in first["text"]
            assert jobs.output(job, first["cursor"], 100)["text"] == ""
    asyncio.run(check())


def test_job_rejects_another_scope_and_unknown_restart(tmp_path):
    from ide_mcp.execution import Jobs

    async def check():
        jobs = Jobs()
        with guard.activate(guard.Scope.of(str(tmp_path))):
            async def work():
                return guard.result("done")
            job = jobs.start(work)
            await job.task
            with pytest.raises(common.ToolError, match="unknown"):
                Jobs().get(job.id)
        other = tmp_path / "other"
        other.mkdir()
        with guard.activate(guard.Scope.of(str(other))), pytest.raises(common.ToolError, match="scope"):
            jobs.get(job.id)
    asyncio.run(check())


def test_test_report_no_tests_and_all_skipped_cannot_pass():
    from ide_mcp.validation import parse_junit, parse_trx
    assert parse_junit(b'<testsuite tests="0"/>')["executed"] == 0
    assert parse_trx(
        b'<TestRun><Results><UnitTestResult outcome="NotExecuted"/></Results></TestRun>'
    )["executed"] == 0
    with pytest.raises(common.ToolError):
        parse_junit(b"<bad")


def test_lock_classifier_does_not_guess_from_access_denied():
    from ide_mcp.validation import classify_failure
    assert classify_failure("Access denied")[0] == "unknown"
    assert classify_failure("MSB3027: file is locked by a running process")[1] == "output_locked"
    assert classify_failure("NU1301: Unable to load the service index")[0] == "environment"


def test_sdk_has_shared_jobs_and_partial_read_is_a_tool_error(tmp_path):
    from ide_mcp import files_server as fs
    from ide_mcp import kotlin_server as kt
    expected = {"job_status", "job_output", "job_result", "cancel_job", "read_artifact", "start_validation"}
    for server in (fs, kt):
        tools = asyncio.run(server.mcp.list_tools())
        assert expected <= {t.name for t in tools}
        assert next(t for t in tools if t.name == "job_status").outputSchema["properties"]["outcome"]
    result = asyncio.run(fs.mcp.call_tool("read_files", {
        "items": [{"path": str(tmp_path / "missing.txt")}], "expected_root": str(tmp_path),
    }))
    assert result.isError is True
    assert result.structuredContent["data"]["items"][0]["outcome"] == "failed"


def test_gradle_run_captures_reports_before_releasing_workspace(tmp_path, monkeypatch):
    from conftest import make_gradle_project

    from ide_mcp import gradle

    root = make_gradle_project(tmp_path / "project")
    results = root / "app/build/test-results/testDebugUnitTest"
    results.mkdir(parents=True)
    report = results / "TEST-a.xml"
    report.write_text('<testsuite tests="1" skipped="0"><testcase name="a"/></testsuite>')

    async def fake_proc(*args, **kwargs):
        # Unchanged XML is stale even with a task marker.
        return common.ProcResult(0, "> Task :app:testDebugUnitTest\nBUILD SUCCESSFUL\n")

    monkeypatch.setattr(gradle, "run_proc", fake_proc)
    monkeypatch.setattr(gradle, "execution_state", lambda _p: ("available", None, None))
    with guard.activate(guard.Scope.of(str(root))):
        p = gradle.find_project(str(root))
        result = asyncio.run(gradle.run_gradle(p, [":app:testDebugUnitTest"],
                            report_dirs=[(":app:testDebugUnitTest", results)]))
    assert result.test_summary.executed == 0
    assert result.test_summary.complete is False
    assert result.artifacts[0]["verified"] is True


def test_cancellation_before_launch_and_after_owned_start(tmp_path):
    from ide_mcp.execution import Jobs

    async def check():
        jobs = Jobs()
        with guard.activate(guard.Scope.of(str(tmp_path))):
            async def work():
                result = await common.run_proc(
                    [sys.executable, "-u", "-c", "import time;print('started');time.sleep(60)"], cwd=tmp_path)
                return guard.result(result.output)
            pending = jobs.start(work)
            jobs.cancel(pending)
            assert pending.state == "finished" and pending.final["outcome"] == "cancelled"
            running = jobs.start(work)
            for _ in range(100):
                if "started" in running.log:
                    break
                await asyncio.sleep(0.02)
            assert "started" in running.log
            jobs.cancel(running)
            assert running.state == "cancelling"
            await asyncio.wait_for(running.task, 15)
            assert running.state == "finished" and running.final["outcome"] == "cancelled"
            jobs.cancel(running)
            assert running.state == "finished"
    asyncio.run(check())


def test_python_adapter_does_not_retain_raw_sensitive_reports(tmp_path):
    from ide_mcp import validation

    test = tmp_path / "test_sensitive.py"
    test.write_text("def test_sensitive():\n    assert False, 'password=synthetic-secret'\n", encoding="utf-8")
    with guard.activate(guard.Scope.of(str(tmp_path))):
        result = asyncio.run(validation.validate_python(str(tmp_path), sys.executable, "pytest", [str(test)]))
    assert result.fields["outcome"] == "failed"
    assert result.fields["data"]["counts"]["executed"] == 1
    assert "synthetic-secret" not in str(result.fields)
    for report in (tmp_path / ".ide-mcp/reports").glob("*.xml"):
        assert b"synthetic-secret" not in report.read_bytes()


def test_structured_sanitiser_handles_key_values_and_argument_pairs():
    value = common.sanitise_value({"password": "synthetic-one", "args": ["--password", "synthetic-two"]})
    assert "synthetic" not in str(value)


def test_split_credentials_are_withheld_before_progress(tmp_path):
    lines = []
    with guard.activate(guard.Scope.of(str(tmp_path))):
        result = asyncio.run(common.run_proc(
            [sys.executable, "-c", "print('password=');print('synthetic-next-line')"],
            cwd=tmp_path, on_line=lines.append,
        ))
    assert "synthetic-next-line" not in result.output
    assert "synthetic-next-line" not in str(lines)


def test_trx_declared_counts_must_match_actual_results():
    from ide_mcp.validation import parse_trx
    with pytest.raises(common.ToolError, match="count|incomplete"):
        parse_trx(b'<TestRun><ResultSummary><Counters total="2" executed="2"/></ResultSummary>'
                  b'<Results><UnitTestResult outcome="Passed"/></Results></TestRun>')


def test_gradle_incomplete_report_keeps_known_failure_without_inventing_execution(tmp_path):
    from ide_mcp import gradle
    report = tmp_path / "TEST-incomplete.xml"
    data = (b'<testsuite tests="10" skipped="0"><testcase name="bad"><failure message="broken"/>'
            b'</testcase></testsuite>')
    summary = gradle._parse_test_files({report: data})
    assert summary.failed == 1
    assert summary.executed == 1
    assert summary.complete is False


def test_workspace_lease_is_released_after_failure(tmp_path):
    from ide_mcp.execution import WorkspaceLease

    async def check():
        with guard.activate(guard.Scope.of(str(tmp_path))):
            with pytest.raises(RuntimeError):
                async with WorkspaceLease(tmp_path):
                    raise RuntimeError("synthetic")
            async def acquire():
                async with WorkspaceLease(tmp_path):
                    pass
            await asyncio.wait_for(acquire(), 2)
    asyncio.run(check())


def test_workspace_lease_coordinates_separate_processes(tmp_path):
    from ide_mcp.execution import WorkspaceLease

    async def check():
        lines = []
        child = (
            "import asyncio,sys;from ide_mcp import guard;from ide_mcp.execution import WorkspaceLease;"
            "from pathlib import Path\n"
            "async def check():\n"
            " with guard.activate(guard.Scope.of(sys.argv[1])):\n"
            "  print('ready',flush=True)\n"
            "  async with WorkspaceLease(Path(sys.argv[1])):\n"
            "   print('acquired',flush=True)\n"
            "asyncio.run(check())"
        )
        with guard.activate(guard.Scope.of(str(tmp_path))):
            async with WorkspaceLease(tmp_path):
                task = asyncio.create_task(common.run_proc(
                    [sys.executable, "-c", child, str(tmp_path)], cwd=tmp_path,
                    timeout=10, on_line=lines.append))
                for _ in range(150):
                    if "ready" in lines:
                        break
                    await asyncio.sleep(0.02)
                assert "ready" in lines
                await asyncio.sleep(0.15)
                assert "acquired" not in lines
            result = await task
        assert result.code == 0 and "acquired" in result.output
    asyncio.run(check())


def test_expired_and_invalid_job_cursors_are_explicit(tmp_path, monkeypatch):
    from ide_mcp import execution
    job = execution.Job("synthetic", guard.Scope.of(str(tmp_path)))
    monkeypatch.setattr(execution, "MAX_LOG", 5)
    job.append("abcdef")
    jobs = execution.Jobs()
    with pytest.raises(common.ToolError, match="cursor_expired"):
        jobs.output(job, 0, 10)
    page = jobs.output(job, job.base, 5)
    assert page["history_complete"] is False
    with pytest.raises(common.ToolError, match="invalid_cursor"):
        jobs.output(job, 100, 5)


def test_missing_artifact_write_is_not_a_verified_path(tmp_path, monkeypatch):
    from ide_mcp.execution import save_artifact
    monkeypatch.setattr(guard.Scope, "write_bytes", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("synthetic")))
    with guard.activate(guard.Scope.of(str(tmp_path))):
        artifact = save_artifact("safe")
    assert artifact["path"] is None and artifact["verified"] is False


def test_cancellation_during_launch_keeps_ownership_until_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    async def check():
        launched, release = asyncio.Event(), asyncio.Event()
        owned = SimpleNamespace(returncode=None)

        async def launch(*_args, **_kwargs):
            launched.set()
            await release.wait()
            return owned

        async def terminate(proc):
            assert proc is owned
            proc.returncode = -9

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        monkeypatch.setattr(common, "_terminate", terminate)
        with guard.activate(guard.Scope.of(str(tmp_path))):
            task = asyncio.create_task(common.run_proc(["synthetic.exe"], cwd=tmp_path))
            await launched.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert owned.returncode == -9

    asyncio.run(check())


def test_oversized_sensitive_line_cannot_expose_its_unlabelled_tail(tmp_path):
    lines = []
    with guard.activate(guard.Scope.of(str(tmp_path))):
        result = asyncio.run(common.run_proc(
            [sys.executable, "-c", "print('password=' + 'x' * 5_000_000 + 'synthetic-tail')"],
            cwd=tmp_path, on_line=lines.append,
        ))
    assert result.output_complete is False
    assert "synthetic-tail" not in result.output
    assert "synthetic-tail" not in str(lines)


def test_workspace_lease_does_not_retry_invalid_handle_errors(tmp_path, monkeypatch):
    import errno

    from ide_mcp.execution import WorkspaceLease

    def invalid(*_args):
        raise OSError(errno.EBADF, "synthetic invalid handle")

    if common.WINDOWS:
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", invalid)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", invalid)

    async def check():
        with guard.activate(guard.Scope.of(str(tmp_path))), pytest.raises(common.Blocked, match="workspace_lease_unavailable"):
            await asyncio.wait_for(WorkspaceLease(tmp_path).__aenter__(), 0.3)

    asyncio.run(check())


def test_validation_start_rejects_invalid_inputs_before_queuing(tmp_path):
    from ide_mcp import files_server as fs

    async def check():
        before = set(fs.jobs.jobs)
        with pytest.raises(common.ToolError):
            await fs.start_validation("python", str(tmp_path), sys.executable, "unknown",
                                      files=[], expected_root=str(tmp_path))
        assert set(fs.jobs.jobs) == before
        assert not (tmp_path / ".ide-mcp").exists()

    asyncio.run(check())


def test_lint_diagnostic_cap_is_incomplete_and_retains_source_failure(tmp_path, monkeypatch):
    import json

    from ide_mcp import validation

    target = tmp_path / "example.py"
    target.write_text("pass\n", encoding="utf-8")

    async def findings(*_args, **_kwargs):
        return common.ProcResult(1, json.dumps([{"code": "F401", "message": "unused import"}] * 201))

    monkeypatch.setattr(validation, "run_proc", findings)
    with guard.activate(guard.Scope.of(str(tmp_path))):
        result = asyncio.run(validation.validate_python(str(tmp_path), sys.executable, "ruff", [str(target)]))
    assert result.fields["outcome"] == "failed"
    assert result.fields["category"] == "source"
    assert result.fields["complete"] is False
    assert len(result.fields["data"]["diagnostics"]) == 200


@pytest.mark.parametrize("kind,report", [
    ("junit", b'<testsuite tests="10"><testcase name="bad"><failure>broken</failure></testcase></testsuite>'),
    ("trx", (b'<TestRun><Counters total="10" executed="10" failed="1"/>'
             b'<UnitTestResult testName="bad" outcome="Failed"/></TestRun>')),
])
def test_incomplete_adapter_report_keeps_known_failure(kind, report):
    from ide_mcp import validation

    counts = (validation.parse_junit if kind == "junit" else validation.parse_trx)(report)
    assert counts["failed"] == 1 and counts["executed"] == 1
    assert counts["complete"] is False


def test_real_python_validation_has_equivalent_sync_and_job_counts(tmp_path):
    from ide_mcp import files_server as fs

    target = tmp_path / "test_example.py"
    target.write_text("def test_example():\n    assert 2 + 2 == 4\n", encoding="utf-8")

    async def check():
        arguments = {"project": str(tmp_path), "executable_path": sys.executable, "check": "pytest",
                     "files": [str(target)], "expected_root": str(tmp_path)}
        direct = await fs.validate_python(**arguments)
        queued = await fs.start_validation("python", **arguments)
        with guard.activate(guard.Scope.of(str(tmp_path))):
            job = fs.jobs.get(queued.fields["data"]["job_id"])
        await job.task
        assert direct.fields["outcome"] == job.final["outcome"] == "passed"
        assert direct.fields["data"]["counts"] == job.final["data"]["counts"]
        assert job.final["data"]["counts"]["executed"] == 1
        assert job.final["complete"] is True

    asyncio.run(check())


@pytest.mark.skipif(not common.WINDOWS, reason="Windows native tree-termination contract")
def test_failed_tree_termination_cannot_use_parent_exit_as_proof(monkeypatch):
    from types import SimpleNamespace

    async def parent_wait():
        return 0

    async def killer_wait():
        return 1

    async def launch(*_args, **_kwargs):
        return SimpleNamespace(wait=killer_wait)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    owned = SimpleNamespace(pid=-1, returncode=0, wait=parent_wait)
    with pytest.raises(common.Blocked, match="termination_unconfirmed"):
        asyncio.run(common._terminate(owned))


def test_source_identity_rejects_another_selected_scope(tmp_path):
    from ide_mcp.execution import source_identity

    selected, other = tmp_path / "selected", tmp_path / "other"
    selected.mkdir()
    other.mkdir()
    with guard.activate(guard.Scope.of(str(selected))), pytest.raises(common.Blocked, match="target_mismatch"):
        source_identity(other)


def test_dotnet_test_cannot_trust_an_imported_framework_model(tmp_path):
    from ide_mcp.validation import dotnet_inputs

    project = tmp_path / "Tests.csproj"
    project.write_text('<Project><Import Project="shared.props"/></Project>', encoding="utf-8")
    with guard.activate(guard.Scope.of(str(tmp_path))), pytest.raises(common.Blocked, match="target_framework_unverified"):
        dotnet_inputs(str(project), sys.executable, "test")



