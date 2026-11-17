"""Unit tests for the parsing and matching logic (no Gradle needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ide_mcp import files_server as fs
from ide_mcp import gradle, symbols
from ide_mcp.common import ToolError, glob_matcher, iter_files


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- common


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("*.kt", "a/b/Foo.kt", True),
        ("*.kt", "a/b/Foo.kts", False),
        ("**/*Test.kt", "FooTest.kt", True),
        ("**/*Test.kt", "a/b/FooTest.kt", True),
        ("app/**/*.xml", "app/src/main/res/layout/a.xml", True),
        ("app/**/*.xml", "lib/src/a.xml", False),
        ("*.{kt,java}", "x/A.java", True),
        ("*.{kt,java}", "x/A.xml", False),
        ("src/*/A.kt", "src/main/A.kt", True),
        ("src/*/A.kt", "src/main/kotlin/A.kt", False),
    ],
)
def test_glob(pattern, path, expected):
    assert glob_matcher(pattern)(path) is expected


def test_iter_files_skips_build_dirs(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.kt").write_text("class A")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "junk.kt").write_text("x")
    names = {f.name for f in iter_files(tmp_path)}
    assert names == {"A.kt"}
    assert {f.name for f in iter_files(tmp_path, include_ignored=True)} == {"A.kt", "junk.kt"}


# ----------------------------------------------------------------------------- files server


def test_replace_text_unique_and_ambiguous(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\none\n")
    with pytest.raises(ToolError, match="matches 2 places"):
        run(fs.replace_text(str(f), "one", "1"))
    run(fs.replace_text(str(f), "two", "2"))
    assert f.read_text() == "one\n2\none\n"
    run(fs.replace_text(str(f), "one", "1", replace_all=True))
    assert f.read_text() == "1\n2\n1\n"
    with pytest.raises(ToolError, match="not found"):
        run(fs.replace_text(str(f), "missing", "x"))


def test_replace_text_keeps_crlf_and_backslashes(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    run(fs.replace_text(str(f), "a\nb", r"x\1\ny"))
    assert f.read_bytes() == b"x\\1\\ny\r\nc\r\n"  # literal mode: backslashes are not escapes
    f.write_text("val x = 1\n")
    run(fs.replace_text(str(f), r"val (\w+)", r"var \1", regex=True))
    assert f.read_text() == "var x = 1\n"


def test_create_read_move_delete(tmp_path: Path):
    f = tmp_path / "deep" / "dir" / "a.txt"
    run(fs.create_file(str(f), "l1\nl2\nl3\n"))
    with pytest.raises(ToolError, match="already exists"):
        run(fs.create_file(str(f), "x"))
    out = run(fs.read_file(str(f), start_line=2, max_lines=1))
    assert "lines 2-2 of 3" in out and "2\tl2" in out and "start_line=3" in out
    run(fs.move_path(str(f), str(tmp_path / "b.txt")))
    assert (tmp_path / "b.txt").exists() and not f.exists()
    with pytest.raises(ToolError, match="non-empty"):
        run(fs.delete_path(str(tmp_path)))
    run(fs.delete_path(str(tmp_path / "b.txt")))
    assert not (tmp_path / "b.txt").exists()


def test_search_and_find(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "Foo.kt").write_text("class Foo\nfun bar() = Foo()\n")
    (tmp_path / "a" / "note.md").write_text("Foo is great\n")
    out = run(fs.search_in_files("Foo", str(tmp_path), file_glob="*.kt"))
    assert "2 matching line(s)" in out and "a/Foo.kt" in out and "note.md" not in out
    out = run(fs.search_in_files(r"fun \w+\(", str(tmp_path), regex=True))
    assert "2: fun bar() = Foo()" in out
    out = run(fs.find_files(str(tmp_path), name_contains="foo"))
    assert "a/Foo.kt" in out
    out = run(fs.list_directory_tree(str(tmp_path)))
    assert "a/" in out and "  Foo.kt" in out


# ----------------------------------------------------------------------------- gradle parsing

OUTPUT = """\
> Task :app:compileDebugKotlin FAILED
e: file:///p/app/src/main/java/A.kt:12:5 Unresolved reference 'foo'.
w: file:///p/app/src/main/java/A.kt:3:1 Parameter 'x' is never used
e: /p/app/src/main/java/B.kt: (7, 9): Type mismatch
/p/lib/src/main/java/C.java:40: error: cannot find symbol
/p/app/src/main/java/D.kt:8:1: Line is too long [MaxLineLength]
/p/app/src/main/java/E.kt:3:1: Unused import (standard:no-unused-imports)
/p/app/src/main/java/F.kt:6:19 Whitespace after ',' is missing

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':app:compileDebugKotlin'.
> Compilation error. See log for more details

* Try:
> Run with --stacktrace option to get the stack trace.

BUILD FAILED in 4s
"""


def test_parse_output():
    rep = gradle.parse_output(OUTPUT, Path("/p"))
    assert rep.failed_tasks == [":app:compileDebugKotlin"]
    assert rep.outcome == "BUILD FAILED in 4s"
    assert "Compilation error" in rep.what_went_wrong and "Try" not in rep.what_went_wrong
    by_path = {Path(d.path).name: d for d in rep.errors}
    assert by_path["A.kt"].line == 12 and by_path["A.kt"].col == 5
    assert by_path["B.kt"].line == 7 and by_path["B.kt"].message == "Type mismatch"
    assert by_path["C.java"].line == 40 and by_path["C.java"].severity == "error"
    assert by_path["D.kt"].rule == "MaxLineLength"
    assert by_path["E.kt"].rule == "standard:no-unused-imports"
    assert by_path["F.kt"].rule == "ktlint" and by_path["F.kt"].col == 19
    assert len(rep.warnings) == 1


TASKS = """\

------------------------------------------------------------
Tasks runnable from root project 'demo'
------------------------------------------------------------

Build tasks
-----------
assemble - Assembles the outputs of this project.
app:assembleDebug - Assembles main outputs for variant debug
app:compileDebugSources
app:compileGplayDebugUnitTestSources
lib:core:classes - Assembles main classes.

Verification tasks
------------------
app:testDebugUnitTest - Run unit tests for the debug build.
lib:core:test - Runs the test suite.
detekt

Rules
-----
Pattern: clean<TaskName>: Cleans the output files of a task.
"""


def test_parse_tasks_and_pick():
    tasks = gradle.parse_tasks(TASKS)
    assert set(tasks) == {":", ":app", ":lib:core"}
    assert tasks[":"] == {"assemble": "Assembles the outputs of this project.", "detekt": ""}
    assert gradle.compile_task(tasks[":app"], "debug", False)[0] == "compileDebugSources"
    assert gradle.compile_task(tasks[":app"], "debug", True)[0] == "compileGplayDebugUnitTestSources"
    assert gradle.compile_task(tasks[":lib:core"], "debug", False)[0] == "classes"
    assert gradle.test_task(tasks[":app"], "debug")[0] == "testDebugUnitTest"
    assert gradle.test_task(tasks[":lib:core"], "debug")[0] == "test"
    assert gradle.task_path(":", "detekt") == ":detekt"
    assert gradle.normalize_module("features/login") == ":features:login"


def test_find_project_and_modules(tmp_path: Path):
    root = tmp_path / "repo" / "android"
    (root / "app" / "src" / "main").mkdir(parents=True)
    (root / "settings.gradle.kts").write_text("")
    (root / "gradlew").write_text("#!/bin/sh\n")
    (root / "app" / "build.gradle.kts").write_text("")
    (root / "features" / "login").mkdir(parents=True)
    (root / "features" / "login" / "build.gradle").write_text("")
    src = root / "app" / "src" / "main" / "A.kt"
    src.write_text("class A")
    assert gradle.find_project(str(src)).root == root
    assert gradle.find_project(str(tmp_path / "repo")).root == root  # found below
    assert set(gradle.module_dirs(root)) == {":", ":app", ":features:login"}
    assert gradle.module_of(src, root) == ":app"
    with pytest.raises(ToolError, match="No Gradle build"):
        gradle.find_project("/usr/share")


def test_collect_tests(tmp_path: Path):
    d = tmp_path / "testDebugUnitTest"
    d.mkdir()
    (d / "TEST-com.x.FooTest.xml").write_text(
        '<testsuite name="com.x.FooTest" tests="3" skipped="1" failures="1" errors="0">'
        '<testcase name="ok" classname="com.x.FooTest"/>'
        '<testcase name="skip" classname="com.x.FooTest"><skipped/></testcase>'
        '<testcase name="bad" classname="com.x.FooTest"><failure message="expected 1 but was 2">'
        "java.lang.AssertionError: expected 1 but was 2\n\tat org.junit.Assert.fail(Assert.java:89)\n"
        "\tat com.x.FooTest.bad(FooTest.kt:12)\n</failure></testcase></testsuite>"
    )
    s = gradle.collect_tests([d], 0.0)
    assert (s.total, s.failed, s.skipped, s.suites) == (3, 1, 1, 1)
    text = s.render()
    assert "FAILED com.x.FooTest.bad: expected 1 but was 2" in text
    assert "FooTest.kt:12" in text and "org.junit.Assert" not in text


# ----------------------------------------------------------------------------- symbols

KOTLIN = '''\
package com.example

import com.example.other.Thing

/**
 * A repository.
 */
@Singleton
class UserRepository(
    private val api: Api,
    val cache: Cache = Cache { it },
) : Repository<User>() {

    val count: Int get() = 1

    companion object {
        const val TAG = "repo {"
        fun create(): UserRepository = UserRepository(Api(), Cache())
    }

    override suspend fun load(id: String): User {
        val local = cache[id]
        fun helper() = 1
        return local ?: api.fetch(id).also { cache[id] = it }
    }

    abstract fun bare(): Int

    inner class Editor {
        fun <T : Any> List<T>.firstOrThrow(): T = first()
    }
}

fun String.shout() = uppercase()

sealed interface State {
    data object Loading : State
    data class Done(val user: User) : State
}

enum class Mode { A, B }

typealias Handler = (User) -> Unit
'''


def test_kotlin_outline():
    syms = {(s.kind, s.qualified) for s in symbols.outline(KOTLIN)}
    for expected in [
        ("class", "UserRepository"),
        ("val", "UserRepository.api"),
        ("val", "UserRepository.cache"),
        ("val", "UserRepository.count"),
        ("object", "UserRepository.Companion"),
        ("val", "UserRepository.Companion.TAG"),
        ("fun", "UserRepository.Companion.create"),
        ("fun", "UserRepository.load"),
        ("fun", "UserRepository.bare"),
        ("class", "UserRepository.Editor"),
        ("fun", "UserRepository.Editor.firstOrThrow"),
        ("fun", "shout"),
        ("interface", "State"),
        ("object", "State.Loading"),
        ("class", "State.Done"),
        ("enum", "Mode"),
        ("typealias", "Handler"),
    ]:
        assert expected in syms, expected
    names = {s.name for s in symbols.outline(KOTLIN)}
    assert "local" not in names and "helper" not in names  # locals are skipped


def test_signature_and_doc():
    lines = KOTLIN.splitlines()
    sym = next(s for s in symbols.outline(KOTLIN) if s.name == "UserRepository")
    sig = symbols.signature(lines, sym.line)
    assert sig.startswith("class UserRepository(") and sig.endswith(") : Repository<User>()")
    assert symbols.doc_comment(lines, sym.line) == "A repository."


JAVA = '''\
package com.example;

public class Util {
    private static final String NAME = "x";

    public Util() {}

    public static <T> List<T> wrap(T item) {
        String local = "a";
        return List.of(item);
    }

    interface Callback {
        void done(int code);
    }
}
'''


def test_java_outline():
    syms = {(s.kind, s.qualified) for s in symbols.outline(JAVA, java=True)}
    assert {("class", "Util"), ("val", "Util.NAME"), ("fun", "Util.Util"), ("fun", "Util.wrap"), ("interface", "Util.Callback")} <= syms
    assert "local" not in {s.name for s in symbols.outline(JAVA, java=True)}


def test_word_regex():
    rx = symbols.word_regex("User")
    assert rx.search("val u: User?") and rx.search("List<User>")
    assert not rx.search("UserRepository") and not rx.search("myUser")
