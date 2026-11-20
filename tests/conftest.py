"""Fixtures for the scope and readiness tests: two Git worktrees at one commit, a fake Gradle
wrapper that records where it was launched, and a spy over file and process side effects."""

from __future__ import annotations

import asyncio
import builtins
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"

TASKS_OUTPUT = """\

------------------------------------------------------------
Tasks runnable from root project 'demo'
------------------------------------------------------------

Build tasks
-----------
app:classes - Assembles main classes.
app:compileDebugSources

Verification tasks
------------------
app:testDebugUnitTest - Run unit tests for the debug build.
app:detekt

Help tasks
----------
app:dependencies - Displays all dependencies.
"""

GRADLEW_SH = """\
#!/bin/sh
echo "$(pwd -P)|$*" >> "$IDE_MCP_TEST_LAUNCHES"
case " $* " in *" tasks "*) cat "$(dirname "$0")/gradle-tasks.txt" ;; esac
echo "BUILD SUCCESSFUL in 1s"
"""

GRADLEW_BAT = """\
@echo off
echo %CD%^|%*>> "%IDE_MCP_TEST_LAUNCHES%"
if "%2"=="tasks" type "%~dp0gradle-tasks.txt"
echo BUILD SUCCESSFUL in 1s
"""


def run(coro):
    return asyncio.run(coro)


def make_gradle_project(root: Path) -> Path:
    """A minimal Gradle build with one module and a wrapper that only records its launch."""
    (root / "app" / "src" / "main" / "kotlin").mkdir(parents=True)
    (root / "app" / "src" / "test" / "kotlin").mkdir(parents=True)
    (root / "settings.gradle.kts").write_text('include(":app")\n')
    (root / "build.gradle.kts").write_text("// root build\n")
    (root / "app" / "build.gradle.kts").write_text("// app build\n")
    (root / "gradle-tasks.txt").write_text(TASKS_OUTPUT)
    (root / "gradlew").write_text(GRADLEW_SH, newline="\n")
    (root / "gradlew.bat").write_text(GRADLEW_BAT, newline="\r\n")
    if not WINDOWS:
        os.chmod(root / "gradlew", 0o755)
    (root / "app" / "src" / "main" / "kotlin" / "Door.kt").write_text("package demo\n\nclass Door\n\nfun open(d: Door) = d\n")
    (root / "app" / "src" / "test" / "kotlin" / "DoorTest.kt").write_text("package demo\n\nclass DoorTest\n")
    return root


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def launches(tmp_path: Path, monkeypatch) -> Path:
    """File the fake Gradle wrapper appends '<cwd>|<args>' to, one line per launch."""
    log = tmp_path / "launches.log"
    monkeypatch.setenv("IDE_MCP_TEST_LAUNCHES", str(log))
    monkeypatch.setenv("JAVA_HOME", str(tmp_path))
    monkeypatch.delenv("IDE_MCP_ALLOWED_ROOTS", raising=False)
    from ide_mcp import gradle

    monkeypatch.setattr(gradle, "LOG_DIR", tmp_path / "gradle-logs")
    gradle._tasks_cache.clear()
    return log


def launched(log: Path) -> list[tuple[str, str]]:
    if not log.exists():
        return []
    return [tuple(line.split("|", 1)) for line in log.read_text().splitlines() if line]  # type: ignore[misc]


@pytest.fixture
def worktrees(tmp_path: Path, launches: Path) -> tuple[Path, Path]:
    """Two distinct worktrees of one repository at the same commit, side by side under a shared
    parent, as `git worktree add` lays them out."""
    dev = tmp_path / "dev"
    a = make_gradle_project(dev / "app")
    git(a, "init", "-q", "-b", "main")
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "initial")
    git(a, "worktree", "add", "-q", "-b", "other", str(dev / "app-other"))
    b = dev / "app-other"
    if not WINDOWS:
        os.chmod(b / "gradlew", 0o755)
    return Path(os.path.realpath(a)), Path(os.path.realpath(b))


def _inside(candidate: object, root: str) -> bool:
    if isinstance(candidate, bytes):
        candidate = os.fsdecode(candidate)
    if isinstance(candidate, os.PathLike):
        candidate = os.fspath(candidate)
    if not isinstance(candidate, str) or not candidate:
        return False
    if root in candidate:  # also catches a path embedded in a shell command line
        return True
    try:
        real = os.path.realpath(candidate)
        return os.path.commonpath([root, real]) == root
    except (OSError, ValueError):
        return False


class Effects:
    """Records content opens, writes, directory walks and process launches made during a test."""

    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[object, ...]]] = []

    def record(self, kind: str, *paths: object) -> None:
        self.events.append((kind, paths))

    def touching(self, root: Path, *kinds: str) -> list[tuple[str, tuple[object, ...]]]:
        real = os.path.realpath(root)
        return [e for e in self.events if (not kinds or e[0] in kinds) and any(_inside(p, real) for p in e[1])]

    def of(self, *kinds: str) -> list[tuple[str, tuple[object, ...]]]:
        return [e for e in self.events if e[0] in kinds]


@pytest.fixture
def effects(monkeypatch) -> Effects:
    fx = Effects()

    def spy(module, name: str, kind: str, n_paths: int = 1):
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            fx.record(kind, *args[:n_paths])
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    spy(builtins, "open", "open")
    spy(io, "open", "open")
    spy(os, "open", "open")
    for name in ("unlink", "remove", "mkdir", "rmdir", "makedirs"):
        spy(os, name, "write")
    spy(os, "replace", "write", 2)
    spy(os, "rename", "write", 2)
    spy(shutil, "move", "write", 2)
    spy(shutil, "rmtree", "write")
    spy(os, "walk", "walk")

    def launch_spy(module, name: str):
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            argv = args[0] if args and isinstance(args[0], (list, tuple)) else args
            fx.record("launch", kwargs.get("cwd"), *argv)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    class SpyPopen(subprocess.Popen):  # a subclass, so `Popen[bytes]` and isinstance keep working
        def __init__(self, args, *rest, **kwargs):
            fx.record("launch", kwargs.get("cwd"), *(args if isinstance(args, (list, tuple)) else [args]))
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    launch_spy(asyncio, "create_subprocess_exec")
    launch_spy(asyncio, "create_subprocess_shell")
    return fx
