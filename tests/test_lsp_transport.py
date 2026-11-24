"""Transport failures retain bounded diagnostics without claiming an unverified exit."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ide_mcp import guard, lsp


def test_startup_exit_reports_code_and_sanitised_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spawn = asyncio.create_subprocess_exec

    async def launch(*_argv, **kwargs):
        return await spawn(
            sys.executable, "-c",
            "import sys; sys.stderr.write('Error loading java.security\\npassword=hidden\\n'); sys.exit(7)",
            **kwargs,
        )

    monkeypatch.setattr(lsp.asyncio, "create_subprocess_exec", launch)

    async def scenario():
        with guard.activate(guard.Scope.of(str(tmp_path))):
            session = await lsp._start(tmp_path, tmp_path / "launcher", tmp_path / "system")
            try:
                with pytest.raises(lsp.LspError) as error:
                    await session.initialize()
                assert error.value.reason == "semantic_backend_exited"
                assert "exit code 7" in error.value.detail
                assert "Error loading java.security" in error.value.detail
                assert "hidden" not in error.value.detail
                assert "[redacted]" in error.value.detail
                assert session._pending == {}
            finally:
                await session.stop()

    asyncio.run(scenario())


def test_transport_eof_does_not_claim_a_running_process_exited(tmp_path: Path):
    async def scenario():
        stdout = asyncio.StreamReader()
        stdout.feed_eof()

        async def wait():
            await asyncio.Event().wait()

        proc = SimpleNamespace(returncode=None, stdout=stdout, stdin=SimpleNamespace(write=lambda _: None), wait=wait)
        session = lsp.Session(tmp_path, cast(asyncio.subprocess.Process, cast(object, proc)))
        with pytest.raises(lsp.LspError) as error:
            await session.initialize()
        assert error.value.reason == "semantic_transport_closed"
        assert "exit" not in error.value.detail.lower()
        assert session._pending == {}
        assert session.state(None)[0] == "unavailable"

    asyncio.run(scenario())


def test_missing_input_stream_does_not_claim_process_exit_or_leak_request(tmp_path: Path):
    async def scenario():
        session = lsp.Session(tmp_path, cast(asyncio.subprocess.Process, cast(object, SimpleNamespace(returncode=None, stdin=None))))
        with pytest.raises(lsp.LspError) as error:
            await session.request("initialize", {})
        assert error.value.reason == "semantic_transport_closed"
        assert "exit" not in error.value.detail.lower()
        assert session._pending == {}

    asyncio.run(scenario())


def test_stderr_read_failure_does_not_mask_transport_failure(tmp_path: Path):
    async def scenario():
        async def read(_size: int) -> bytes:
            raise ConnectionResetError("synthetic stream failure")

        proc = SimpleNamespace(returncode=1, stderr=SimpleNamespace(read=read))
        session = lsp.Session(tmp_path, cast(asyncio.subprocess.Process, cast(object, proc)))
        session._stderr_reader = asyncio.create_task(session._read_stderr())
        error = await session._transport_error()
        assert error.reason == "semantic_backend_exited"
        assert "[stderr unavailable:" in error.detail

    asyncio.run(scenario())


@pytest.mark.parametrize("payload, expected", [
    (b"-----BEGIN PRIVATE KEY-----\nprivate-material\n", "[private key withheld]"),
    (b"password=\nsecret-continuation\n", "[redacted]"),
    (b"password=" + b"x" * 100_000, "[stderr withheld: capture limit exceeded]"),
])
def test_stderr_is_bounded_and_sanitised_before_reporting(tmp_path: Path, payload: bytes, expected: str):
    async def scenario():
        stderr = asyncio.StreamReader()
        stderr.feed_data(payload)
        stderr.feed_eof()
        session = lsp.Session(tmp_path, cast(asyncio.subprocess.Process, cast(object, SimpleNamespace(stderr=stderr))))
        await session._read_stderr()
        assert expected in session.stderr_log
        assert "private-material" not in session.stderr_log
        assert "secret-continuation" not in session.stderr_log
        assert "xxxx" not in session.stderr_log
        assert len(session.stderr_log) <= 65_536

    asyncio.run(scenario())
