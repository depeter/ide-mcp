"""kotlin MCP server: Gradle builds, tests, detekt, ktlint and Kotlin/Java symbol lookup for
any project on disk, without an IDE holding it open."""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import gradle, guard, lsp, symbols
from .common import (
    Blocked,
    OutcomeError,
    ToolError,
    active,
    android_sdk,
    clip,
    glob_matcher,
    identity,
    iter_files,
    path_key,
    protected_name,
    real,
    rel,
    run_proc,
    within,
)
from .gradle import GradleProject, GradleRun, task_path
from .guard import EXECUTION, PROJECT, SEMANTIC, TARGET, ExpectedRoot, Readiness

mcp = FastMCP(
    "kotlin",
    instructions=(
        "Gradle, detekt, ktlint and Kotlin/Java symbol tools that work on any project on disk; "
        "nothing has to be open in Android Studio or IntelliJ.\n"
        "Every tool requires `expected_root`: the absolute path of the Git worktree the request is "
        "for (or a directory inside it at or above the Gradle root), or, outside Git, the project "
        "root itself. The project a tool actually resolves, every file it is given and the language "
        "server session it uses must belong to that worktree, or the call is rejected before "
        "anything is read, written or run; a folder that merely contains several checkouts does not "
        "select one. It restricts the request and grants nothing. *.properties files are never "
        "read.\n"
        "Rejections and failures are tool errors that start with [outcome=... reason=... "
        "category=...]; builds, tests and lint runs state passed, failed, timed_out or not_run. "
        "`readiness` reports, without running Gradle or starting the language server, whether "
        "execution and code-aware analysis are available for a scope; each tool checks again when "
        "it runs.\n"
        "Gradle tools take `project_dir`: any path inside the project (a file works too), "
        "defaulting to expected_root. The Gradle root is found from there, including <repo>/android layouts. Modules are "
        "Gradle paths like ':app' or ':features:login'; passing `file` instead picks the module "
        "that owns that file.\n"
        "After editing code, call `compile` with the file you changed: it compiles just that module "
        "and lists errors as path:line:col. Use run_tests for unit tests, detekt and ktlint for "
        "static analysis, gradle_run for any other task. Builds can take minutes; results are "
        "summarised and the full log path is included.\n"
        "With the Kotlin language server installed, find_usages, rename_symbol, symbol_info and "
        "file_problems are code-aware like the IDE: they resolve the exact symbol, and file_problems "
        "reports IntelliJ inspections without a build. The first call per project imports and "
        "indexes it, which takes from seconds to minutes; later calls are fast. When a name is "
        "declared more than once, pass `file` and `line` of one place it appears. If the language "
        "server is missing, still indexing, or cannot import the project, the code-aware tools are "
        "blocked and say why. Only find_usages can answer with whole-word text matches instead "
        "(engine='auto', the default, or engine='text'), and it labels them as unresolved text "
        "matches. Every navigation and rename result starts with [engine=... requested=... "
        "project=... complete=...]: the engine actually used, the project actually used, and whether "
        "the listing is the whole answer.\n"
        "rename_symbol is always semantic and works in two calls: a preview that writes nothing and "
        "returns a preview_id, then dry_run=false with that preview_id, which is rejected as stale "
        "if anything changed in between. find_implementations, find_callers and find_callees are "
        "separate, direct, bounded queries; they describe static relationships, not a runtime "
        "order, and do not follow chains. find_symbol and file_outline read source text and resolve "
        "nothing. Run compile after a rename."
    ),
)
ops = guard.Operations(mcp)

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

ProjectDir = Annotated[
    str | None,
    Field(description="Absolute path anywhere inside the Gradle project (directory or file), inside expected_root. Defaults to expected_root."),
]
Module = Annotated[
    str | None,
    Field(description="Gradle project path such as ':app' or ':features:login'. Omit to use `file`'s module, or all modules."),
]
SourceFile = Annotated[
    str | None,
    Field(description="Absolute path of a source file; its module is used and results are narrowed to this file."),
]
Variant = Annotated[
    str,
    Field(description="Android build variant, e.g. 'debug' or 'gplayDebug'. Ignored for plain JVM modules."),
]
Timeout = Annotated[int, Field(description="Kill Gradle after this many seconds.", ge=10, le=7200)]
SearchRoot = Annotated[
    str | None,
    Field(description="Absolute directory to search (a project root or any subdirectory), inside expected_root. Defaults to expected_root."),
]


# ----------------------------------------------------------------------------- helpers


class Progress:
    """Forwards Gradle's current task to the client as progress notifications, which also keeps
    clients that time out idle tool calls from giving up on a long build."""

    def __init__(self, ctx: Context | None):
        self.ctx = ctx
        self.count = 0
        self.last = "starting Gradle"
        self._task: asyncio.Task | None = None

    def on_line(self, line: str) -> None:
        self.count += 1
        if line.startswith("> Task ") or line.startswith("> Configure project"):
            self.last = line[2:]

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.ctx.report_progress(self.count, None, self.last)  # type: ignore[union-attr]
            except Exception:
                return

    async def __aenter__(self) -> "Progress":
        if self.ctx is not None:
            self._task = asyncio.create_task(self._beat())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task:
            self._task.cancel()


async def _run(ctx: Context | None, project: GradleProject, args: list[str], timeout: float) -> GradleRun:
    async with Progress(ctx) as p:
        return await gradle.run_gradle(project, args, timeout=timeout, on_line=p.on_line)


async def _tasks(ctx: Context | None, project: GradleProject, refresh: bool = False) -> dict[str, dict[str, str]]:
    async with Progress(ctx) as p:
        return await gradle.project_tasks(project, refresh=refresh, on_line=p.on_line)


def _project(project_dir: str | None, file: str | None = None, *, search_below: bool = True) -> GradleProject:
    """The Gradle build a request targets, verified against its scope.

    The path the caller gave is checked first, so a target in another worktree is rejected
    before any directory is looked at. The build that discovery then returns is checked again,
    because that, not the path supplied, is what Gradle would run in."""
    scope = active()
    start = scope.check(project_dir or file or scope.root, protected=False)
    found = gradle.find_project(str(start), search_below=search_below)
    actual = real(found.root)
    if actual != scope.root and within(scope.root, actual):
        raise Blocked(
            "invalid_scope",
            f"expected_root {scope.root} is inside the Gradle project {actual}, so it cannot confine a build of that "
            f"project. Pass the worktree or {actual} as expected_root.",
            "input",
        )
    return GradleProject(scope.check_project(actual), found.launcher)


def _source(file: str | None) -> Path | None:
    if not file:
        return None
    p = active().check(file)
    if not p.is_file():
        raise ToolError(f"File does not exist: {p}")
    return p


def _finish(run: GradleRun, text: str, *, source: bool = False, not_run: str = "") -> str:
    """State the outcome of a Gradle run. `source` says the failure is located in project code
    (compiler errors, failing tests, lint findings); other build failures are not classified,
    since a broken build script and a broken environment look alike from here."""
    if run.timed_out:
        return guard.outcome("timed_out", text, category="unknown")
    if not run.ok or source:
        return guard.outcome("failed", text, category="source" if source else "unknown")
    if not_run:
        return guard.outcome("not_run", text, reason=not_run)
    return guard.outcome("passed", text)


def _modules(
    project: GradleProject, tasks: dict[str, dict[str, str]], module: str | None, files: list[Path]
) -> list[str] | None:
    """Modules a tool call targets: the explicit one, else the owners of `files`, else None
    (meaning every module)."""
    if module:
        m = gradle.normalize_module(module)
        assert m is not None
        if m not in tasks:
            close = difflib.get_close_matches(m, list(tasks), n=5, cutoff=0.4)
            raise ToolError(
                f"No Gradle project '{m}' in {project.root}."
                + (f" Did you mean: {', '.join(close)}?" if close else " See project_info for the module list.")
            )
        return [m]
    if files:
        mods = []
        for f in files:
            m = gradle.module_of(f, project.root)
            if m not in tasks:
                raise ToolError(f"{f} maps to Gradle project '{m}', which this build does not define. Pass `module` explicitly.")
            if m not in mods:
                mods.append(m)
        return mods
    return None


def _note_alternatives(task: str, others: list[str]) -> str:
    return f"Used {task}; other variants available: {', '.join(others[:8])}." if others else ""


# ----------------------------------------------------------------------------- project


@ops.tool(kind="file", needs=(TARGET, PROJECT), annotations=READ)
async def project_info(project_dir: ProjectDir = None, expected_root: ExpectedRoot = None) -> str:
    """Describe the Gradle build that owns a path: root directory, AGP/Kotlin versions,
    modules, and which static-analysis tools are configured. Reads build scripts only; does not
    run Gradle. The Gradle version is not reported, because it is recorded in
    gradle-wrapper.properties, a protected file."""
    project = _project(project_dir)
    root = project.root
    scope = active()

    def text_of(f: Path) -> str:
        return scope.read_bytes(f).decode("utf-8", "replace")

    def gather() -> str:
        out = [f"Gradle root: {root}", f"Launcher:    {' '.join(project.launcher)}"]
        out.append("Gradle:      not reported (gradle/wrapper/gradle-wrapper.properties is a protected file and is not read)")
        out.append(f"Java:        {os.environ.get('JAVA_HOME') or shutil.which('java') or 'not found (set JAVA_HOME for this server)'}")

        build_text = ""
        for name in (*gradle.BUILD_FILES, *gradle.SETTINGS, "gradle/libs.versions.toml"):
            f = root / name
            if f.is_file():
                build_text += text_of(f) + "\n"
        versions = []
        toml = root / "gradle" / "libs.versions.toml"
        if toml.is_file():
            for line in text_of(toml).splitlines():
                if m := re.match(r'^\s*(agp|android[_-]?gradle[_-]?plugin|kotlin|ksp|detekt|ktlint|compose[_-]?bom|java|jvm[_-]?target)\s*=\s*"([^"]+)"', line, re.I):
                    versions.append(f"{m.group(1)}={m.group(2)}")
        if versions:
            out.append("Versions:    " + ", ".join(versions))
        tools = [t for t in ("detekt", "ktlint", "spotless", "kover", "ksp") if re.search(rf"\b{t}\b", build_text, re.I)]
        out.append("Tooling:     " + (", ".join(tools) if tools else "none detected in root build files") + "  (gradle_tasks shows the actual tasks)")

        mods = gradle.module_dirs(root)
        names = sorted(m for m in mods if m != ":")
        out.append(f"\nModules ({len(names)}):")
        out.extend(f"  {m:<40} {rel(mods[m], root)}" for m in names[:200])
        if len(names) > 200:
            out.append(f"  [... {len(names) - 200} more ...]")
        if not names:
            out.append("  (single-project build; use module ':')")
        return "\n".join(out)

    return await asyncio.to_thread(gather)


@ops.tool(kind="execution", needs=(PROJECT, EXECUTION), annotations=READ)
async def gradle_tasks(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Annotated[str | None, Field(description="Gradle project path, e.g. ':app'. Omit to search every module.")] = None,
    filter: Annotated[str | None, Field(description="Case-insensitive substring of the task name, e.g. 'test' or 'detekt'.")] = None,
    refresh: Annotated[bool, Field(description="Re-query Gradle instead of using the cached task list (after editing build scripts).")] = False,
    expected_root: ExpectedRoot = None,
) -> str:
    """List Gradle tasks. The first call per project runs `gradle tasks --all` (slow on large
    builds) and caches the result. Give `module` or `filter` to keep the list short."""
    project = _project(project_dir)
    tasks = await _tasks(ctx, project, refresh)
    mods = _modules(project, tasks, module, []) or sorted(tasks)
    needle = filter.lower() if filter else None
    if not module and not needle:
        lines = [f"{len(tasks)} Gradle project(s) in {project.root}. Pass module or filter to list tasks."]
        lines += [f"  {m:<40} {len(tasks[m])} tasks" for m in mods[:200]]
        return "\n".join(lines)
    lines = []
    for m in mods:
        for name, desc in sorted(tasks[m].items()):
            if needle and needle not in name.lower():
                continue
            lines.append(f"{task_path(m, name)}" + (f" - {desc}" if desc else ""))
    if not lines:
        return "No tasks matched."
    return clip(f"{len(lines)} task(s):\n" + "\n".join(lines), 40_000, keep="head")


@ops.tool(kind="execution", needs=(PROJECT, EXECUTION), annotations=DESTRUCTIVE)
async def gradle_run(
    ctx: Context,
    tasks: Annotated[list[str], Field(description="Tasks to run, e.g. [':app:assembleDebug'] or ['clean', 'build'].")],
    args: Annotated[list[str] | None, Field(description="Extra Gradle arguments, e.g. ['--offline', '-Pfoo=bar', '--stacktrace'].")] = None,
    project_dir: ProjectDir = None,
    show_warnings: Annotated[bool, Field(description="List compiler warnings as well as errors.")] = False,
    timeout_sec: Timeout = 1800,
    expected_root: ExpectedRoot = None,
) -> str:
    """Run any Gradle tasks and summarise the result: outcome, failed tasks, compiler and lint
    errors as path:line:col, test failures, and Gradle's 'What went wrong'. The full log is
    saved to a file whose path is returned. Prefer compile / run_tests / detekt / ktlint for
    those jobs; they pick the right task for you. Arguments that point Gradle at another
    project (-p, --project-dir, -c, --settings-file, -b, --build-file, --include-build, -I,
    --init-script) are accepted only when their path is inside expected_root."""
    project = _project(project_dir)
    if not tasks and not args:
        raise ToolError("Give at least one task.")
    gradle.check_arguments(project, [*tasks, *(args or [])])
    run = await _run(ctx, project, [*tasks, *(args or [])], timeout_sec)
    rep = gradle.parse_output(run.output, project.root)
    extra = ""
    if any("test" in t.lower() for t in tasks):
        dirs = [d / "build" / "test-results" for d in gradle.module_dirs(project.root).values()]
        summary = await asyncio.to_thread(gradle.collect_tests, dirs, run.started)
        if summary.suites:
            extra = summary.render()
            return _finish(run, format_run(run, rep, show_warnings=show_warnings, extra=extra), source=bool(rep.errors or summary.failed))
    return _finish(run, format_run(run, rep, show_warnings=show_warnings, extra=extra), source=bool(rep.errors))


def format_run(run: GradleRun, rep: gradle.Report, **kw) -> str:
    return clip(gradle.format_report(run, rep, **kw), 40_000, keep="head")


@ops.tool(name="compile", kind="execution", needs=(TARGET, PROJECT, EXECUTION), annotations=ACT)
async def compile_sources(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Module = None,
    file: SourceFile = None,
    variant: Variant = "debug",
    tests: Annotated[bool, Field(description="Compile unit-test sources too (which also compiles main).")] = False,
    show_warnings: Annotated[bool, Field(description="List compiler warnings as well as errors.")] = False,
    timeout_sec: Timeout = 1200,
    expected_root: ExpectedRoot = None,
) -> str:
    """Compile Kotlin and Java without packaging, and report errors as path:line:col. This is
    the way to check whether an edit is correct. With `file`, only that file's module is
    compiled and only that file's problems are listed; with `module`, that module; with neither,
    every module. A test file passed as `file` compiles test sources automatically."""
    src = _source(file)
    project = _project(project_dir, file)
    tasks = await _tasks(ctx, project)
    mods = _modules(project, tasks, module, [src] if src else [])
    if src and re.search(r"/src/test\w*/", src.as_posix()):
        tests = True

    targets, notes = [], []
    for m in mods if mods is not None else sorted(tasks):
        name, others = gradle.compile_task(tasks[m], variant, tests)
        if name:
            targets.append(task_path(m, name))
            if mods is not None and (note := _note_alternatives(name, others)):
                notes.append(note)
        elif mods is not None:
            raise ToolError(
                f"Project '{m}' has no {'test ' if tests else ''}compile task for variant '{variant}'. "
                f"Check gradle_tasks(module='{m}', filter='compile') and pass the right variant."
            )
    if not targets:
        raise ToolError(f"No compile tasks found in {project.root}.")
    args = targets + (["--continue"] if len(targets) > 1 else [])
    run = await _run(ctx, project, args, timeout_sec)
    rep = gradle.parse_output(run.output, project.root)
    only = {path_key(src)} if src and not module else None
    if only and run.ok and not [d for d in rep.diagnostics if d.key in only]:
        return _finish(run, f"No problems in {src} ({rep.outcome or 'BUILD SUCCESSFUL'}; ran {' '.join(targets)})   [{project.root}]")
    return _finish(run, format_run(run, rep, show_warnings=show_warnings, only_files=only, extra="\n".join(notes)), source=bool(rep.errors))


def _guess_test_module(project: GradleProject, patterns: list[str]) -> str | None:
    """Find the module of a test class named in a --tests filter such as
    'com.example.FooTest.method' or 'FooTest'."""
    names = {seg for p in patterns for seg in re.split(r"[.#$*]", p) if seg[:1].isupper()}
    if not names:
        return None
    wanted = {f"{n}{ext}" for n in names for ext in (".kt", ".java")}
    for f in iter_files(project.root):
        if f.name in wanted:
            return gradle.module_of(f, project.root)
    return None


@ops.tool(kind="execution", needs=(TARGET, PROJECT, EXECUTION), annotations=ACT)
async def run_tests(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Module = None,
    file: Annotated[str | None, Field(description="Absolute path of a test file; runs the tests in that file.")] = None,
    tests: Annotated[
        list[str] | None,
        Field(description="Gradle --tests filters, e.g. ['com.example.FooTest'], ['FooTest.returns empty list'], ['*.login.*']."),
    ] = None,
    variant: Variant = "debug",
    timeout_sec: Timeout = 1800,
    expected_root: ExpectedRoot = None,
) -> str:
    """Run JVM unit tests and report counts plus each failure with its message and trimmed
    stack trace. Scope with `file` (one test file), `tests` filters, and/or `module`; with none
    of them every module's unit tests run. When only `tests` is given, the module is located
    from the test class name. A run in which no test executed is reported as not_run, never as
    passed."""
    src = _source(file)
    project = _project(project_dir, file)
    filters = list(tests or [])
    if src and not filters:
        text = active().read_bytes(src).decode("utf-8", "replace")
        pkg = re.search(r"^\s*package\s+([\w.]+)", text, re.M)
        filters = [f"{pkg.group(1)}.{src.stem}" if pkg else src.stem]
    all_tasks = await _tasks(ctx, project)
    mods = _modules(project, all_tasks, module, [src] if src else [])
    if mods is None and filters:
        guessed = await asyncio.to_thread(_guess_test_module, project, filters)
        if guessed is None or guessed not in all_tasks:
            raise ToolError("Could not tell which module these tests live in. Pass `module` or `file`.")
        mods = [guessed]

    dirs = gradle.module_dirs(project.root)
    args: list[str] = []
    ran: list[tuple[str, Path]] = []
    notes = []
    for m in mods if mods is not None else sorted(all_tasks):
        name, others = gradle.test_task(all_tasks[m], variant)
        if not name:
            if mods is not None:
                raise ToolError(f"Project '{m}' has no unit-test task for variant '{variant}'. Check gradle_tasks(module='{m}', filter='test').")
            continue
        tp = task_path(m, name)
        args.append(tp)
        for f in filters:
            args += ["--tests", f]
        mdir = dirs.get(m) or project.root.joinpath(*m.strip(":").split(":"))
        ran.append((tp, mdir / "build" / "test-results" / name))
        if mods is not None and (note := _note_alternatives(name, others)):
            notes.append(note)
    if not args:
        raise ToolError(f"No unit-test tasks found in {project.root}.")
    if len(ran) > 1:
        args.append("--continue")

    run = await _run(ctx, project, args, timeout_sec)
    rep = gradle.parse_output(run.output, project.root)
    # Only trust result files of tasks that actually ran; otherwise a compile failure would
    # report the previous run's results.
    executed = [d for tp, d in ran if re.search(rf"^> Task {re.escape(tp)}(?: (?!NO-SOURCE|SKIPPED)\S+)?$", run.output, re.M)]
    summary = await asyncio.to_thread(gradle.collect_tests, executed, 0.0)
    if summary.failed:
        # Gradle's own message only says "There were failing tests"; the failures say more.
        rep.what_went_wrong = ""
    text = format_run(run, rep, extra="\n".join([summary.render(), *notes]), tail=not summary.failed)
    return _finish(run, text, source=bool(rep.errors or summary.failed), not_run="" if summary.total else "no_tests_executed")


# ----------------------------------------------------------------------------- static analysis


def _find_cli(env_var: str, *names: str) -> str | None:
    explicit = os.environ.get(env_var)
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    for n in names:
        if found := shutil.which(n):
            return found
    return None


def _findings(title: str, run: GradleRun | None, diags: list[gradle.Diagnostic], root: Path, only: set[str] | None, limit: int = 150) -> str:
    if only is not None:
        diags = [d for d in diags if d.key in only]
    by_rule: dict[str, int] = {}
    for d in diags:
        by_rule[d.rule or "?"] = by_rule.get(d.rule or "?", 0) + 1
    out = [f"{title}: {len(diags)} finding(s)" + (f" in the {len(only)} requested file(s)" if only is not None else "")]
    if len(by_rule) > 1:
        out.append("By rule: " + ", ".join(f"{r} x{n}" for r, n in sorted(by_rule.items(), key=lambda kv: -kv[1])[:25]))
    out.extend("  " + d.render(root) for d in diags[:limit])
    if len(diags) > limit:
        out.append(f"  [... {len(diags) - limit} more ...]")
    if run and run.log:
        out.append(f"Full log: {run.log}")
    return "\n".join(out)


async def _lint_via_gradle(
    ctx: Context,
    project: GradleProject,
    tasks: dict[str, dict[str, str]],
    mods: list[str] | None,
    task: str,
    task_args: list[str],
    timeout: int,
) -> GradleRun | None:
    """Run `task` in the target modules (or everywhere it exists). None if no module has it."""
    if mods is None:
        if not any(task in names for names in tasks.values()):
            return None
        args = [task, *task_args, "--continue"]
    else:
        have = [m for m in mods if task in tasks[m]]
        if not have:
            return None
        args = []
        for m in have:
            args += [task_path(m, task), *task_args]
        args.append("--continue")
    return await _run(ctx, project, args, timeout)


def _lint_result(title: str, run: GradleRun, project: GradleProject, only: set[str] | None) -> str:
    rep = gradle.parse_output(run.output, project.root)
    findings = [d for d in rep.diagnostics if d.rule]
    if run.timed_out or (not run.ok and not findings):
        return _finish(run, format_run(run, rep))  # the build itself broke; show why
    shown = [d for d in findings if only is None or d.key in only]
    return _lint_outcome(_findings(title, run, findings, project.root, only), bool(shown))


def _lint_outcome(text: str, findings: bool) -> str:
    return guard.outcome("failed", text, category="source") if findings else guard.outcome("passed", text)


_TASK_NAME = re.compile(r"[A-Za-z][\w.\-]*")


@ops.tool(kind="execution", needs=(TARGET, PROJECT, EXECUTION), annotations=ACT)
async def detekt(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Module = None,
    files: Annotated[
        list[str] | None,
        Field(description="Absolute source file paths. Their modules are analysed and findings narrowed to these files."),
    ] = None,
    auto_correct: Annotated[bool, Field(description="Let detekt fix what it can (needs the formatting ruleset). Modifies files.")] = False,
    task: Annotated[str, Field(description="Detekt task name; e.g. 'detektMain' or 'detektDebug' for type-resolution rules.")] = "detekt",
    timeout_sec: Timeout = 1200,
    expected_root: ExpectedRoot = None,
) -> str:
    """Run detekt and list findings as path:line:col: message [Rule]. Uses the project's Gradle
    detekt task (so its config, baseline and plugins apply); if the build has no such task,
    falls back to a `detekt` CLI on PATH with the project's detekt.yml."""
    if not _TASK_NAME.fullmatch(task):
        raise ToolError(f"'{task}' is not a task name. Options and paths are not accepted here.")
    paths = [p for p in (_source(f) for f in files or []) if p]
    project = _project(project_dir, files[0] if files else None)
    tasks = await _tasks(ctx, project)
    mods = _modules(project, tasks, module, paths)
    only = {path_key(p) for p in paths} or None

    run = await _lint_via_gradle(ctx, project, tasks, mods, task, ["--auto-correct"] if auto_correct else [], timeout_sec)
    if run is not None:
        return _lint_result("detekt", run, project, only)

    cli = _find_cli("DETEKT_CLI", "detekt", "detekt-cli")
    if not cli:
        raise ToolError(
            f"No '{task}' Gradle task in " + (", ".join(mods) if mods else "any module") + " and no detekt CLI found. "
            "Apply the detekt Gradle plugin, or install detekt-cli and put `detekt` on PATH (or set DETEKT_CLI)."
        )
    inputs = paths or [gradle.module_dirs(project.root).get(m, project.root) for m in (mods or [":"])]
    argv = [cli, "--input", ",".join(str(p) for p in inputs)]
    for candidate in ("config/detekt/detekt.yml", "detekt.yml", "tools/detekt/detekt.yml", "config/detekt.yml"):
        if (project.root / candidate).is_file():
            argv += ["--config", str(project.root / candidate), "--build-upon-default-config"]
            break
    if auto_correct:
        argv.append("--auto-correct")
    r = await run_proc(argv, cwd=project.root, timeout=timeout_sec)
    diags = gradle.parse_output(r.output, project.root).diagnostics
    if r.timed_out:
        return guard.outcome("timed_out", f"detekt CLI was killed after {timeout_sec}s:\n" + clip(r.output, 6000, keep="tail"), category="unknown")
    if r.code != 0 and not diags:
        return guard.outcome("failed", f"detekt CLI failed (exit {r.code}):\n" + clip(r.output, 6000, keep="tail"), category="unknown")
    return _lint_outcome(_findings("detekt (CLI)", None, diags, project.root, only), any(only is None or d.key in only for d in diags))


_KTLINT_TASKS = (("ktlintCheck", "ktlintFormat"), ("lintKotlin", "formatKotlin"), ("spotlessCheck", "spotlessApply"))


@ops.tool(kind="execution", needs=(TARGET, PROJECT, EXECUTION), annotations=ACT)
async def ktlint(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Module = None,
    files: Annotated[
        list[str] | None,
        Field(description="Absolute source file paths. Their modules are checked and findings narrowed to these files."),
    ] = None,
    format: Annotated[bool, Field(description="Fix style violations in place instead of only reporting them. Modifies files.")] = False,
    timeout_sec: Timeout = 1200,
    expected_root: ExpectedRoot = None,
) -> str:
    """Check Kotlin code style, or reformat with format=true. Uses the project's Gradle tasks
    (ktlintCheck/ktlintFormat, lintKotlin/formatKotlin, or spotlessCheck/spotlessApply); if the
    build has none, falls back to a `ktlint` CLI on PATH, which honours .editorconfig."""
    paths = [p for p in (_source(f) for f in files or []) if p]
    project = _project(project_dir, files[0] if files else None)
    tasks = await _tasks(ctx, project)
    mods = _modules(project, tasks, module, paths)
    only = {path_key(p) for p in paths} or None

    for check_task, format_task in _KTLINT_TASKS:
        name = format_task if format else check_task
        run = await _lint_via_gradle(ctx, project, tasks, mods, name, [], timeout_sec)
        if run is None:
            continue
        if format and run.ok:
            return _finish(run, f"Formatted with {name} ({gradle.parse_output(run.output, project.root).outcome or 'done'}).\nFull log: {run.log}")
        return _lint_result(f"ktlint ({name})", run, project, only)

    cli = _find_cli("KTLINT_CLI", "ktlint")
    if not cli:
        raise ToolError(
            "No ktlint/kotlinter/spotless Gradle task in " + (", ".join(mods) if mods else "any module") + " and no ktlint CLI found. "
            "Apply one of those plugins, or put the `ktlint` binary on PATH (or set KTLINT_CLI)."
        )
    if paths:
        targets = [str(p) for p in paths]
    else:
        dirs = gradle.module_dirs(project.root)
        targets = []
        for m in mods or [":"]:
            prefix = "" if m == ":" else rel(dirs.get(m, project.root), project.root) + "/"
            targets += [f"{prefix}**/*.kt", f"{prefix}**/*.kts"]
    r = await run_proc([cli, *(["-F"] if format else []), *targets], cwd=project.root, timeout=timeout_sec)
    diags = gradle.parse_output(r.output, project.root).diagnostics
    if r.timed_out:
        return guard.outcome("timed_out", f"ktlint CLI was killed after {timeout_sec}s:\n" + clip(r.output, 6000, keep="tail"), category="unknown")
    if r.code != 0 and not diags:
        return guard.outcome("failed", f"ktlint CLI failed (exit {r.code}):\n" + clip(r.output, 6000, keep="tail"), category="unknown")
    title = "ktlint (CLI, formatted; remaining" if format else "ktlint (CLI"
    return _lint_outcome(_findings(title + ")", None, diags, project.root, only), any(only is None or d.key in only for d in diags))


_DEP_LINE = re.compile(r"^[| ]*[+\\]--- (?P<coord>[^\s:]+:[^\s:]+)(?::(?P<ver>[^\s:]+))?(?: -> (?P<res>\S+))?")
_PROJECT_DEP = re.compile(r"^[| ]*[+\\]--- project (?P<path>:\S*)")


@ops.tool(kind="execution", needs=(PROJECT, EXECUTION), annotations=READ)
async def dependencies(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Annotated[str | None, Field(description="Gradle project path. Defaults to ':app' if it exists, else the root project.")] = None,
    configuration: Annotated[
        str | None,
        Field(description="Gradle configuration, e.g. 'debugRuntimeClasspath', 'releaseCompileClasspath', 'testRuntimeClasspath'. Default: the debug/main runtime classpath."),
    ] = None,
    filter: Annotated[str | None, Field(description="Case-insensitive substring to keep, e.g. 'okhttp' or 'androidx.compose'.")] = None,
    tree: Annotated[bool, Field(description="Return Gradle's dependency tree instead of the flat resolved list.")] = False,
    timeout_sec: Timeout = 600,
    expected_root: ExpectedRoot = None,
) -> str:
    """List a module's resolved dependencies (group:name:version, after conflict resolution)
    plus the project modules it depends on. Use filter to find a library, tree=true to see
    what pulls it in."""
    project = _project(project_dir)
    tasks = await _tasks(ctx, project)
    m = gradle.normalize_module(module) or (":app" if ":app" in tasks else ":")
    _modules(project, tasks, m, [])
    if not configuration:
        android = any(n.startswith("assembleDebug") or n == "assembleRelease" for n in tasks[m])
        configuration = "debugRuntimeClasspath" if android else "runtimeClasspath"
    run = await _run(ctx, project, [task_path(m, "dependencies"), "--configuration", configuration], timeout_sec)
    if not run.ok:
        return _finish(run, format_run(run, gradle.parse_output(run.output, project.root)))
    needle = filter.lower() if filter else None
    lines = [ln for ln in run.output.splitlines() if _DEP_LINE.match(ln) or _PROJECT_DEP.match(ln)]
    if not lines:
        return f"Configuration '{configuration}' of {m} has no dependencies (or does not exist; try gradle_run [{task_path(m, 'dependencies')}])."
    header = f"{m} / {configuration}"
    if tree:
        kept = [ln for ln in lines if not needle or needle in ln.lower()]
        return clip(f"{header} ({len(kept)} tree lines):\n" + "\n".join(kept), 40_000, keep="head")
    libs: set[str] = set()
    projects: set[str] = set()
    for ln in lines:
        if pm := _PROJECT_DEP.match(ln):
            projects.add(pm.group("path"))
        elif dm := _DEP_LINE.match(ln):
            ver = dm.group("res") or dm.group("ver") or "?"
            libs.add(f"{dm.group('coord')}:{ver}")
    keep = lambda s: not needle or needle in s.lower()  # noqa: E731
    lib_list, proj_list = sorted(x for x in libs if keep(x)), sorted(x for x in projects if keep(x))
    out = [f"{header}: {len(lib_list)} libraries, {len(proj_list)} project modules" + (f" matching '{filter}'" if filter else "")]
    if proj_list:
        out.append("Projects: " + ", ".join(proj_list))
    out.extend(lib_list)
    return clip("\n".join(out), 40_000, keep="head")


# ----------------------------------------------------------------------------- symbols

Kind = Literal["class", "interface", "object", "enum", "fun", "val", "var", "typealias"]


def _package(text: str) -> str:
    m = re.search(r"^\s*package\s+([\w.]+)", text, re.M)
    return m.group(1) if m else ""


def _sources(root: Path, file_glob: str | None, extra_suffixes: tuple[str, ...] = ()) -> list[Path]:
    match = glob_matcher(file_glob)
    suffixes = symbols.SOURCE_SUFFIXES + extra_suffixes
    return sorted(
        f for f in iter_files(root)
        if (file_glob or f.suffix in suffixes) and not protected_name(f.name) and match(rel(f, root))
    )


def _read(f: Path) -> str | None:
    """Text of a source file inside the request scope; None for anything that cannot or must
    not be read (too large, not UTF-8, protected, or resolving outside the scope)."""
    try:
        return active().read_bytes(f, 2_000_000).decode("utf-8")
    except (OSError, UnicodeDecodeError, ToolError):
        return None


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def find_symbol(
    name: Annotated[str, Field(description="Simple name of a class, function, property, object or typealias.")],
    root: SearchRoot = None,
    kind: Annotated[Kind | None, Field(description="Only declarations of this kind.")] = None,
    exact: Annotated[bool, Field(description="Match the whole name. False finds names containing it, case-insensitively.")] = True,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
    expected_root: ExpectedRoot = None,
) -> str:
    """Find where a Kotlin or Java symbol is declared. Returns each declaration's location,
    package, signature and doc comment. Matching is by name from source text, so overloads and
    same-named symbols in different packages are all listed."""
    base = active().dir(root)
    needle = name.lower()

    def scan() -> list[str]:
        out = []
        for f in _sources(base, None):
            text = _read(f)
            if text is None or (name not in text if exact else needle not in text.lower()):
                continue
            lines = text.splitlines()
            for s in symbols.outline(text, java=f.suffix == ".java"):
                if (s.name != name) if exact else (needle not in s.name.lower()):
                    continue
                if kind and s.kind != kind and not (kind == "class" and s.kind in ("record", "@interface")):
                    continue
                pkg = _package(text)
                entry = [f"{rel(f, base)}:{s.line}  {s.kind} {(pkg + '.') if pkg else ''}{s.qualified}"]
                entry += ["    " + ln for ln in symbols.signature(lines, s.line).splitlines()]
                if doc := symbols.doc_comment(lines, s.line):
                    entry += ["    /// " + ln for ln in doc.splitlines()]
                out.append("\n".join(entry))
        return out

    found = await asyncio.to_thread(scan)
    if not found:
        return f"No declaration named '{name}' under {base}." + ("" if not exact else " Try exact=false, or search_in_files on the files server.")
    out = f"{len(found)} declaration(s) under {base}:\n\n" + "\n\n".join(found[:limit])
    if len(found) > limit:
        out += f"\n\n[... {len(found) - limit} more; narrow with kind or a deeper root ...]"
    return clip(out, 40_000, keep="head")


@ops.tool(kind="file", needs=(TARGET,), annotations=READ)
async def file_outline(
    path: Annotated[str, Field(description="Absolute path of a .kt, .kts or .java file.")],
    expected_root: ExpectedRoot = None,
) -> str:
    """List the declarations in a Kotlin or Java file (classes, functions, properties) with
    line numbers, nested by containing class. A quick way to see a large file's structure."""
    p = _source(path)
    assert p is not None
    text = (await asyncio.to_thread(active().read_bytes, p)).decode("utf-8", "replace")
    lines = text.splitlines()
    syms = symbols.outline(text, java=p.suffix == ".java")
    if not syms:
        return f"No declarations recognised in {p} ({len(lines)} lines)."
    out = [f"{p} ({len(lines)} lines, package {_package(text) or '(default)'})"]
    for s in syms:
        depth = s.container.count(".") + 1 if s.container else 0
        src = re.sub(r"\s*\{\s*$", "", lines[s.line - 1].strip())
        out.append(f"{s.line:>5}  {'  ' * depth}{src[:200]}")
    return clip("\n".join(out), 40_000, keep="head")


def _usage_scan(base: Path, name: str, file_glob: str | None) -> list[tuple[Path, list[tuple[int, str, str]]]]:
    rx = symbols.word_regex(name)
    results = []
    for f in _sources(base, file_glob, (".xml",)):
        text = _read(f)
        if text is None or name not in text:
            continue
        declared = {s.line for s in symbols.outline(text, java=f.suffix == ".java") if s.name == name} if symbols.is_source(f) else set()
        hits = [
            (i + 1, symbols.classify_line(line, name, (i + 1) in declared), line.strip())
            for i, line in enumerate(text.splitlines())
            if rx.search(line)
        ]
        if hits:
            results.append((f, hits))
    return results


def _provenance(engine: str, requested: str, root: Path, complete: bool, reason: str = "") -> str:
    """First line of every navigation and rename result: the engine that actually produced it
    (not merely the one asked for), the project root actually used, and whether the listing is
    the whole answer within the query's scope."""
    return (
        f"[engine={engine} requested={requested} project={root} complete={'true' if complete else 'false'}"
        + (f" incomplete_reason={reason}" if reason and not complete else "")
        + "]"
    )


def _page(rows: list, offset: int, limit: int) -> tuple[list, bool, str]:
    """One bounded page of a result: (rows shown, whether the end was reached, continuation note)."""
    shown = rows[offset : offset + limit]
    end = offset + len(shown)
    if end >= len(rows):
        return shown, True, ""
    return shown, False, f"[showing {offset + 1}-{end} of {len(rows)}; continue with offset={end}, or narrow the query]"


async def _text_find_usages(
    name: str, root: str | None, file_glob: str | None, include_imports: bool, limit: int, offset: int = 0,
    requested: str = "text", why: str = "",
) -> str:
    """Whole-word text match over Kotlin, Java and XML sources. Read-only, and never presented
    as symbol resolution."""
    base = active().dir(root)
    results = await asyncio.to_thread(_usage_scan, base, name, file_glob)
    counts = {"usage": 0, "declaration": 0, "import": 0, "comment": 0}
    rows: list[tuple[str, str]] = []
    for f, hits in results:
        for line_no, cls, text in hits:
            counts[cls] += 1
            if cls == "import" and not include_imports:
                continue
            tag = {"usage": " ", "declaration": "D", "comment": "#", "import": "I"}[cls]
            rows.append((rel(f, base), f"  {line_no:>5} {tag} {text[:240]}"))
    out = [
        _provenance("text", requested, base, False, "text_match"),
        "Text matches of the identifier, not confirmed references to one symbol: same-named symbols, comments and "
        "strings are included, and nothing here was resolved.",
    ]
    if why:
        out.append(f"Semantic resolution was requested through engine='auto' but not used: {why}")
    if not rows and not sum(counts.values()):
        return "\n".join(out + [f"'{name}' does not appear under {base}."])
    shown, _end, more = _page(rows, offset, limit)
    out.append(
        f"'{name}' under {base}: {counts['usage']} usage(s), {counts['declaration']} declaration(s), "
        f"{counts['import']} import(s), {counts['comment']} in comments, across {len(results)} file(s)."
        "  [D = declaration, # = comment" + (", I = import" if include_imports else "") + "]"
    )
    last = None
    for shown_file, line in shown:
        if shown_file != last:
            out.append(shown_file)
            last = shown_file
        out.append(line)
    if more:
        out.append(more)
    return clip("\n".join(out), 50_000, keep="head")


# ----------------------------------------------------------------------------- code-aware tools

Engine = Annotated[
    Literal["auto", "semantic", "text"],
    Field(
        description="'semantic' resolves the symbol with the Kotlin language server and is an error when that is "
        "not possible. 'text' is a read-only whole-word search that does not tell same-named symbols apart. 'auto' "
        "uses semantic resolution and, only when the language server is missing, still indexing or could not import "
        "the project, returns text matches instead, labelled as such with the reason. The first line of the result "
        "always states the engine actually used."
    ),
]
RenameEngine = Annotated[
    Literal["semantic", "auto", "text"],
    Field(
        description="Renaming is always semantic. 'auto' means 'semantic' here and never selects text replacement. "
        "'text' is rejected: this tool does not perform whole-word replacement."
    ),
]
AtFile = Annotated[
    str | None,
    Field(description="Absolute path of a file where the symbol appears (declaration or any usage). Needed when the name is not unique."),
]
AtLine = Annotated[int | None, Field(description="1-based line in `file` where the symbol appears.", ge=1)]
AtColumn = Annotated[
    int | None,
    Field(description="1-based column of the identifier on that line. Needed when the name occurs more than once on the line.", ge=1),
]
Limit = Annotated[int, Field(description="Maximum entries to list.", ge=1, le=2000)]
Offset = Annotated[int, Field(description="Entries to skip, to continue a truncated listing.", ge=0)]

_SYMBOL_KINDS = {
    2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property", 8: "field", 9: "constructor",
    10: "enum", 11: "interface", 12: "function", 13: "variable", 14: "constant", 22: "enum member", 23: "struct", 26: "type parameter",
}
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_STATIC_NOTE = (
    "These are static, direct relationships resolved by the language server. This is not a runtime order: coroutine "
    "scheduling, event dispatch and dynamic dispatch are not modelled, and chains are not followed. Query a result "
    "in turn to go one step further."
)


def _workspace_root(root: str | None, file: str | None) -> Path:
    """Verified project the language server should import: the Gradle root when there is one,
    otherwise the request scope itself."""
    try:
        return _project(root or file).root
    except Blocked:
        raise
    except ToolError:
        scope = active()
        scope.check(root or file or scope.root, protected=False)
        return scope.check_project(scope.root)


def _require_server(alternative: str) -> None:
    if lsp.find_server() is None:
        raise lsp.LspError(lsp.NOT_INSTALLED + " " + alternative, "semantic_not_installed")


async def _session(ctx: Context | None, root: Path, timeout: float = 600.0) -> lsp.Session:
    """The language server session for a verified project. Raises unless the session is bound
    to that project and has reported a complete import: answers from a session for another
    worktree would be about other code, and answers from an incomplete model look complete and
    are not. The state is established here on every call, never taken from an earlier one."""
    async with Progress(ctx) as p:
        s = await lsp.session_for(root, timeout, lambda message: setattr(p, "last", message))
    state, reason, detail = s.state(identity(root))
    if state != "ready":
        raise lsp.LspError(
            f"Code-aware analysis is blocked for {root}: {detail} No text match was substituted.",
            reason or "semantic_unavailable",
        )
    return s


async def _semantic(ctx: Context | None, root: str | None, file: str | None, alternative: str, *methods: str) -> lsp.Session:
    """Everything a code-aware operation checks before its first query: the targets, the
    project, an installed server, a ready session bound to that project, and backend support
    for each request the operation is going to make."""
    _source(file)  # a file to resolve the symbol in is a target too
    workspace = _workspace_root(root, file)
    _require_server(alternative)
    s = await _session(ctx, workspace)
    for method in methods:
        s.verify(method)
    return s


def _require_imported(s: lsp.Session, path: Path) -> None:
    if s.left_out(path):
        raise lsp.LspError(
            f"{path} was left out of the language server's import (workaround for Kotlin/kotlin-lsp#253 "
            "on Android test sources), so it cannot be analysed. Use compile or detekt for it.",
            "semantic_import_failed",
        )


def _ambiguous(message: str) -> OutcomeError:
    return OutcomeError("failed", message, category="input", reason="ambiguous_symbol")


async def _locate(
    s: lsp.Session, name: str | None, file: str | None, line: int | None, column: int | None = None
) -> tuple[Path, dict]:
    """Resolve what the caller pointed at into exactly one document position, in the UTF-16
    columns the server uses. Anything that could mean more than one occurrence is rejected with
    the candidates, never settled by taking the first."""
    if file:
        path = _source(file)
        assert path is not None
        lines = s.open(path).splitlines()
        rx = symbols.word_regex(name) if name else None
        if line is not None:
            if line > len(lines):
                raise ToolError(f"{path} has only {len(lines)} lines.")
            text = lines[line - 1]
            found = list(rx.finditer(text)) if rx else []
            if column is not None:
                idx = column - 1
                if idx > len(text) or (rx and not any(m.start() <= idx < m.end() for m in found)):
                    at = ", ".join(f"column={m.start() + 1}" for m in found) or "nowhere on this line"
                    raise ToolError(f"'{name}' is not at column {column} of line {line} in {path} (it is at {at}): {text.strip()[:120]}")
            elif len(found) == 1:
                idx = found[0].start()
            elif found:
                raise _ambiguous(
                    f"'{name}' occurs {len(found)} times on line {line} of {path}. Call again with one of: "
                    + ", ".join(f"column={m.start() + 1}" for m in found[:30])
                    + f"\n  {text.strip()[:200]}"
                )
            elif rx:
                raise ToolError(f"'{name}' does not appear on line {line} of {path}: {text.strip()[:120]}")
            else:
                raise ToolError("Give `name` (the identifier on that line) or `column`.")
            return path, {"line": line - 1, "character": lsp.utf16_col(text, idx)}
        if not rx:
            raise ToolError("Give `line`, or `name` when it occurs once in the file.")
        hits = [
            (i, m) for i, text in enumerate(lines)
            if not text.lstrip().startswith(("import ", "package ", "//", "*", "/*"))
            for m in rx.finditer(text)
        ]
        if not hits:
            raise ToolError(f"'{name}' does not appear in {path}.")
        if len(hits) > 1:
            raise _ambiguous(
                f"'{name}' occurs {len(hits)} times in {path}. Call again with `line` (and `column`) of the one you mean:\n"
                + "\n".join(f"  line={i + 1} column={m.start() + 1}: {lines[i].strip()[:160]}" for i, m in hits[:30])
                + (f"\n  [... {len(hits) - 30} more ...]" if len(hits) > 30 else "")
            )
        i, m = hits[0]
        return path, {"line": i, "character": lsp.utf16_col(lines[i], m.start())}

    if not name:
        raise ToolError("Give `name`, or `file` and `line`.")
    found = await s.request("workspace/symbol", {"query": name}) or []
    exact = []
    for sym in found:
        loc = sym.get("location") or {}
        if sym.get("name") != name or "range" not in loc:
            continue
        p = lsp.path_of(loc["uri"])
        if within(p, s.root):
            exact.append((p, loc["range"]["start"], sym))
    if not exact:
        raise ToolError(
            f"The language server knows no declaration named '{name}' in {s.root}. For a local variable, parameter "
            "or member with a common name, pass `file` and `line` of a place where it appears."
        )
    if len(exact) > 1:
        listing = "\n".join(
            f"  {_SYMBOL_KINDS.get(sym.get('kind'), 'symbol')} {(sym.get('containerName') + '.') if sym.get('containerName') else ''}{name}"
            f"  -> file={p} line={pos['line'] + 1}"
            for p, pos, sym in exact[:30]
        )
        raise _ambiguous(f"'{name}' is declared in {len(exact)} places. Call again with `file` and `line` of the one you mean:\n{listing}")
    return exact[0][0], exact[0][1]


def _file_lines(cache: dict[str, list[str] | None], path: Path) -> list[str] | None:
    """Lines of a source file inside the request scope, or None when it is outside it (library
    and JDK sources) or cannot be read."""
    key = str(path)
    if key not in cache:
        try:
            cache[key] = active().read_bytes(path).decode("utf-8", "replace").splitlines()
        except (OSError, ToolError):
            cache[key] = None
    return cache[key]


def _line_text(cache: dict[str, list[str] | None], path: Path, line: int) -> str:
    lines = _file_lines(cache, path) or []
    return lines[line].strip()[:240] if line < len(lines) else ""


def _as_list(result: object) -> list[dict]:
    if isinstance(result, dict):
        return [result]
    return [r for r in result or [] if isinstance(r, dict)]  # type: ignore[union-attr]


def _target(location: dict) -> tuple[str, dict]:
    """(uri, start position) of an LSP Location or LocationLink."""
    return location.get("uri") or location.get("targetUri") or "", (location.get("range") or location.get("targetSelectionRange") or {}).get("start") or {"line": 0, "character": 0}


async def _describe(s: lsp.Session, name: str, doc: dict, cache: dict[str, list[str] | None]) -> tuple[str, str, list[dict]]:
    """The selected symbol as the language server resolved it: where it is declared and its
    signature. Returns (text for the result, a key identifying the declaration, declarations).
    The key is the backend's own declaration location, valid only until that file changes."""
    decls = _as_list(await s.request("textDocument/definition", doc))
    if not decls:
        return f"symbol: {name}, declaration not resolved by the language server", "unresolved", decls
    uri, start = _target(decls[0])
    key = f"{uri}#{start['line']}:{start['character']}"
    if not uri.startswith("file:"):
        return f"symbol: {name}, declared in {uri}", key, decls
    path = lsp.path_of(uri)
    text = f"symbol: {name}, declared at {rel(path, s.root)}:{start['line'] + 1}"
    lines = _file_lines(cache, path)
    if lines and start["line"] < len(lines):
        text += "\n" + "\n".join("  " + ln.strip() for ln in symbols.signature(lines, start["line"] + 1).splitlines()[:4])
    if len(decls) > 1:
        text += f"\n  (and {len(decls) - 1} more declaration(s) of the same symbol)"
    return text, key, decls


def _completeness(s: lsp.Session, reached_end: bool) -> tuple[bool, str, str]:
    """(complete, reason, note) for a semantic listing from a ready session."""
    if caveat := s.caveat():
        return False, "partial_import", caveat.strip() + "\nAn empty or short answer is not proof that nothing else exists in the sources left out."
    if not reached_end:
        return False, "truncated", ""
    return True, "", ""


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=READ)
async def find_usages(
    ctx: Context,
    name: Annotated[str, Field(description="Identifier to find usages of.")],
    root: SearchRoot = None,
    file: AtFile = None,
    line: AtLine = None,
    engine: Engine = "auto",
    file_glob: Annotated[str | None, Field(description="Only list results in files matching this glob.")] = None,
    include_imports: Annotated[bool, Field(description="Also list import lines.")] = False,
    limit: Annotated[int, Field(description="Maximum lines to list.", ge=1, le=2000)] = 150,
    column: AtColumn = None,
    offset: Offset = 0,
    expected_root: ExpectedRoot = None,
) -> str:
    """Find usages of a symbol across the project. With the Kotlin language server this is
    code-aware, like Find Usages in the IDE: references to that one symbol, ignoring unrelated
    symbols with the same name. A name declared more than once, or occurring more than once on
    the given line, is rejected with the candidates; call again with `file`, `line` and, if
    needed, `column`.

    The first line states how the result was obtained: `[engine=... requested=... project=...
    complete=...]`, followed by the declaration the symbol resolved to. `complete=false` comes
    with a reason: `truncated` (continue with `offset`), `partial_import` (part of the project
    is not in the language server's model, so an empty answer proves nothing) or `text_match`.
    engine='semantic' is an error when the language server is missing, still indexing or could
    not import the project. engine='auto' then returns whole-word text matches instead, labelled
    as unresolved text matches with the reason; a scope or permission rejection is never
    answered that way. engine='text' asks for the text search directly."""
    if engine == "text":
        return await _text_find_usages(name, root, file_glob, include_imports, limit, offset)
    try:
        s = await _semantic(ctx, root, file, "For a whole-word text search instead, call again with engine='text'.", "textDocument/references")
    except lsp.LspError as e:
        if engine != "auto" or e.category != "environment":
            raise
        return await _text_find_usages(name, root, file_glob, include_imports, limit, offset, "auto", f"{e.reason}: {e.detail}")
    cache: dict[str, list[str] | None] = {}
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line, column)
        s.open(path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        symbol, _key, decls = await _describe(s, name, doc, cache)
        refs = await s.request("textDocument/references", {**doc, "context": {"includeDeclaration": True}}) or []
    decl_keys = {(path_key(lsp.path_of(uri)), start["line"]) for uri, start in map(_target, decls)}
    match = glob_matcher(file_glob)
    counts = {"usage": 0, "declaration": 0, "import": 0}
    rows: set[tuple[str, int, str, str]] = set()
    for r in refs:
        p = lsp.path_of(r["uri"])
        ln = r["range"]["start"]["line"]
        shown = rel(p, s.root)
        if not match(shown):
            continue
        text = _line_text(cache, p, ln)
        kind = "declaration" if (path_key(p), ln) in decl_keys else "import" if text.startswith("import ") else "usage"
        if (shown, ln + 1, {"usage": " ", "declaration": "D", "import": "I"}[kind], text) in rows:
            continue
        counts[kind] += 1
        if kind == "import" and not include_imports:
            continue
        rows.add((shown, ln + 1, {"usage": " ", "declaration": "D", "import": "I"}[kind], text))
    listed, reached_end, more = _page(sorted(rows), offset, limit)
    complete, reason, note = _completeness(s, reached_end)
    out = [_provenance("semantic", engine, s.root, complete, reason), symbol]
    if note:
        out.append(note)
    if not refs:
        out.append(f"No references to '{name}' found by the language server in {s.root}.")
        return "\n".join(out)
    out.append(
        f"'{name}': {counts['usage']} usage(s), {counts['declaration']} declaration(s), {counts['import']} import(s) "
        f"on {len(rows)} listed line(s)" + (f" matching {file_glob}" if file_glob else "")
        + ".  [D = declaration" + (", I = import" if include_imports else "") + "]"
    )
    last = None
    for shown, ln, tag, text in listed:
        if shown != last:
            out.append(shown)
            last = shown
        out.append(f"  {ln:>5} {tag} {text}")
    if more:
        out.append(more)
    return clip("\n".join(out), 50_000, keep="head")


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=DESTRUCTIVE)
async def rename_symbol(
    ctx: Context,
    name: Annotated[str, Field(description="Current identifier.")],
    new_name: Annotated[str, Field(description="New identifier.")],
    root: SearchRoot = None,
    file: AtFile = None,
    line: AtLine = None,
    engine: RenameEngine = "semantic",
    dry_run: Annotated[bool, Field(description="True (the default) previews and writes nothing. False applies the previewed edit set and requires preview_id.")] = True,
    rename_files: Annotated[bool, Field(description="Also rename Name.kt / Name.java when the file declares the renamed top-level type.")] = True,
    column: AtColumn = None,
    preview_id: Annotated[
        str | None,
        Field(description="The preview_id returned by the dry run, required with dry_run=false. The edit set is recomputed and applied only if it is identical to the one previewed."),
    ] = None,
    expected_root: ExpectedRoot = None,
) -> str:
    """Rename a symbol everywhere it is used, with the language server's rename refactoring:
    only references to that one symbol change; same-named symbols, comments and strings are
    left alone. It is never a text replacement, whatever `engine` says, and it is blocked with
    the reason when the language server is missing, still indexing, could not import the
    project or does not offer rename.

    It works in two calls. The first (dry_run omitted or true) writes nothing and returns the
    declaration the symbol resolved to, every affected line, any declaration-file move, the
    revision of each file the edits were computed against, and a `preview_id`. The second
    repeats the same arguments with dry_run=false and that preview_id. Before the first write
    the project, the session, the symbol and the edit set are established again and every file
    is re-read: if anything differs from what was previewed (a file was edited, the server
    resolves the symbol differently, the project or session changed), the call is rejected with
    `stale_preview` and nothing is written; it does not apply a regenerated edit set. A name
    that is ambiguous, an edit outside expected_root, a protected file or an existing file at a
    move destination also rejects the whole rename.

    Files are written one at a time and are not rolled back: if a write fails part-way, the
    error lists what was and was not written. Run compile afterwards."""
    if not symbols.is_identifier(name) or not symbols.is_identifier(new_name):
        raise ToolError("name and new_name must be plain identifiers (letters, digits, underscore).")
    if name == new_name:
        raise ToolError("new_name is the same as name.")
    if engine == "text":
        raise Blocked(
            "text_rename_rejected",
            "rename_symbol does not perform whole-word text replacement: it would also change unrelated symbols with "
            "the same name, comments and strings. Use engine='semantic'. To see where the identifier occurs as text, "
            "use find_usages(engine='text'), which is read-only; a deliberate textual edit is a job for the files "
            "server's replace_text, one reviewed file at a time.",
        )
    s = await _semantic(ctx, root, file, "Renaming needs it; no text replacement is made in its place.", "textDocument/rename")
    cache: dict[str, list[str] | None] = {}
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line, column)
        s.open(path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        if isinstance(s.capabilities.get("renameProvider"), dict) and s.capabilities["renameProvider"].get("prepareProvider"):
            prepared = await s.request("textDocument/prepareRename", doc)
            if not prepared:
                raise ToolError(f"The language server cannot rename what is at {path}:{pos['line'] + 1} (not a renameable symbol, or it is declared in a library).")
            span = prepared.get("range") or (prepared if "start" in prepared else None)
            current = prepared.get("placeholder")
            if current is None and span and span["start"]["line"] == span["end"]["line"]:
                text = (s.open(path).splitlines() or [""])[span["start"]["line"]] if span["start"]["line"] < len(s.open(path).splitlines()) else ""
                current = text[lsp.py_index(text, span["start"]["character"]) : lsp.py_index(text, span["end"]["character"])]
            if current is not None and current != name:
                raise OutcomeError(
                    "failed",
                    f"The symbol the language server would rename at {path}:{pos['line'] + 1} is '{current}', not '{name}'. Nothing was changed.",
                    category="input", reason="symbol_mismatch",
                )
        symbol, key, decls = await _describe(s, name, doc, cache)
        edit = await s.request("textDocument/rename", {**doc, "newName": new_name}, timeout=300)
        if not edit:
            raise ToolError(f"The language server cannot rename '{name}' at {path}:{pos['line'] + 1} (not a renameable symbol, or it is declared in a library).")
        changes = await asyncio.to_thread(lsp.plan_workspace_edit, edit)
        if not changes:
            return guard.outcome("not_run", f"Renaming '{name}' to '{new_name}' changes nothing.", reason="no_occurrences", category="input")

        # Keep the file name in step with a top-level type it is named after, as the class/file
        # convention requires. Nothing else is ever moved.
        if rename_files:
            for uri, _start in map(_target, decls):
                dpath = lsp.path_of(uri)
                change = next((c for c in changes if path_key(c.path) == path_key(dpath)), None)
                if not uri.startswith("file:") or dpath.stem != name or dpath.suffix not in symbols.SOURCE_SUFFIXES or change is None or change.new_path:
                    continue
                declares_type = any(
                    sym.name == name and not sym.container and sym.kind not in ("fun", "val", "var")
                    for sym in symbols.outline(change.old_text or "", java=dpath.suffix == ".java")
                )
                if declares_type:
                    change.new_path = active().check(dpath.with_name(new_name + dpath.suffix))
        for c in changes:
            if c.new_path and c.new_path != c.path and os.path.lexists(c.new_path) and not lsp._same_file(c.path, c.new_path):
                raise Blocked(
                    "destination_collision",
                    f"Renaming '{name}' to '{new_name}' moves {rel(c.path, s.root)} to {c.new_path.name}, which already exists. "
                    "Nothing was changed. Resolve the collision, or pass rename_files=false to leave the file name alone.",
                    "input",
                )
        outside = [q for c in changes for q in (c.path, c.new_path) if q is not None and not within(q, s.root)]
        if outside:
            raise Blocked("target_mismatch", "The rename would edit files outside the project, so nothing was changed:\n" + "\n".join(f"  {q}" for q in outside[:10]))

        digest = lsp.plan_digest(changes, identity(s.root).path, key, name, new_name)
        if not dry_run:
            if not preview_id:
                raise OutcomeError(
                    "failed",
                    "dry_run=false needs the preview_id of the edit set you reviewed. Call with dry_run=true first; nothing was written.",
                    category="input", reason="preview_required",
                )
            if preview_id.strip() != digest:
                raise Blocked(
                    "stale_preview",
                    f"The edit set for renaming '{name}' to '{new_name}' is no longer the one previewed as {preview_id.strip()}: a file "
                    "changed, the language server resolves the symbol differently, or the project or session is another one. "
                    "Nothing was written. Preview again and review the new edit set.",
                    "input",
                )
            await asyncio.to_thread(lsp.write_changes, changes)
            s.forget_open()

    total = sum(len(c.changed_lines()) for c in changes)
    complete, reason, note = _completeness(s, True)
    verb = "Would rename" if dry_run else "Renamed"
    out = [_provenance("semantic", engine, s.root, complete, reason), symbol]
    if note:
        out.append(note)
    out.append(f"{verb} '{name}' to '{new_name}': {total} line(s) in {len(changes)} file(s).")
    budget = 150
    for c in sorted(changes, key=lambda c: str(c.path)):
        lines = c.changed_lines()
        label = rel(c.path, s.root) + f" ({len(lines)})"
        if c.new_path:
            label += f"  -> {'will be ' if dry_run else ''}renamed to {c.new_path.name}"
        out.append(label + f"  [revision {lsp.revision(c)}]")
        for ln, text in lines[: max(budget, 0)]:
            out.append(f"  {ln:>5}  {text[:200]}")
        budget -= len(lines)
    if dry_run:
        out.append(f"preview_id: {digest}")
        out.append(
            "Nothing was written. To apply exactly this edit set, call again with the same arguments, dry_run=false and "
            "this preview_id. It is rejected as stale if any of these files, the symbol or the project has changed by then."
        )
        return guard.outcome("not_run", clip("\n".join(out), 50_000, keep="head"), reason="dry_run")
    out.append(f"Applied the edit set previewed as {digest}. Run compile to verify.")
    return guard.outcome("passed", clip("\n".join(out), 50_000, keep="head"))


# ----------------------------------------------------------------------------- flow navigation


def _dispatch(cache: dict[str, list[str] | None], path: Path | None, line: int) -> str:
    """How firmly a call target is known, read off its declaration: `exact` for a final
    declaration, `possible dispatch` when an override may run instead, `unresolved` when the
    declaration cannot be inspected."""
    lines = _file_lines(cache, path) if path is not None else None
    if lines is None or line >= len(lines):
        return "unresolved: declared outside expected_root, so overriding cannot be checked"
    declared = symbols.outline("\n".join(lines), java=path.suffix == ".java")  # type: ignore[union-attr]
    sym = next((d for d in declared if d.line == line + 1), None)
    if sym is None:
        return "unresolved: the declaration on that line was not recognised"
    text = lines[line]
    owner = next((d for d in declared if d.qualified == sym.container), None) if sym.container else None
    if owner is not None and owner.kind == "interface":
        return "possible dispatch: declared in an interface"
    if path.suffix == ".java":  # type: ignore[union-attr]
        virtual = sym.container and not re.search(r"\b(final|static|private)\b", text)
        return "possible dispatch: an overridable Java method" if virtual else "exact"
    if re.search(r"\b(abstract|open|override)\b", text) and not re.search(r"\bfinal\b", text):
        return "possible dispatch: declared abstract, open or as an override"
    return "exact"


async def _call_item(s: lsp.Session, name: str, doc: dict) -> dict:
    items = _as_list(await s.request("textDocument/prepareCallHierarchy", doc))
    if not items:
        raise ToolError(f"'{name}' at that position is not something the language server has a call hierarchy for (a function, constructor or property accessor).")
    if len(items) > 1:
        raise _ambiguous(
            f"'{name}' at that position resolves to {len(items)} callables:\n"
            + "\n".join(f"  {i.get('name')} {i.get('detail') or ''}  -> file={lsp.path_of(i['uri'])} line={i['selectionRange']['start']['line'] + 1}" for i in items[:30])
        )
    return items[0]


def _item_row(s: lsp.Session, cache: dict[str, list[str] | None], item: dict) -> tuple[Path | None, int, str]:
    uri, start = item.get("uri") or "", item["selectionRange"]["start"]
    if not uri.startswith("file:"):
        return None, start["line"], f"{uri}  {item.get('name')} {item.get('detail') or ''}".rstrip()
    path = lsp.path_of(uri)
    text = _line_text(cache, path, start["line"]) or f"{_SYMBOL_KINDS.get(item.get('kind'), 'symbol')} {item.get('name')} {item.get('detail') or ''}".rstrip()
    return path, start["line"], f"{rel(path, s.root)}:{start['line'] + 1}  {text}"


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=READ)
async def find_implementations(
    ctx: Context,
    name: Annotated[str, Field(description="Identifier of the interface, class or member to find implementations of.")],
    root: SearchRoot = None,
    file: AtFile = None,
    line: AtLine = None,
    column: AtColumn = None,
    limit: Limit = 100,
    offset: Offset = 0,
    expected_root: ExpectedRoot = None,
) -> str:
    """List the implementations of an interface or abstract class, or the overrides of a member,
    as the language server resolves them (Go to Implementation in the IDE). Semantic only: when
    the language server is missing, not ready or does not offer the request, this is an error
    and nothing is inferred from text. The first line states engine, project and completeness;
    a truncated listing continues with `offset`."""
    s = await _semantic(ctx, root, file, "Implementations are not inferred from text.", "textDocument/implementation")
    cache: dict[str, list[str] | None] = {}
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line, column)
        s.open(path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        symbol, _key, _decls = await _describe(s, name, doc, cache)
        found = _as_list(await s.request("textDocument/implementation", doc))
    rows = []
    for uri, start in sorted({(u, (st["line"], st["character"])) for u, st in map(_target, found)}):
        if uri.startswith("file:"):
            p = lsp.path_of(uri)
            rows.append(f"{rel(p, s.root)}:{start[0] + 1}  {_line_text(cache, p, start[0])}".rstrip())
        else:
            rows.append(uri)
    listed, reached_end, more = _page(rows, offset, limit)
    complete, reason, note = _completeness(s, reached_end)
    out = [_provenance("semantic", "semantic", s.root, complete, reason), symbol]
    if note:
        out.append(note)
    out.append(f"{len(rows)} implementation(s) of '{name}' found by the language server." if rows else f"No implementations of '{name}' found by the language server.")
    out.extend("  " + r for r in listed)
    if more:
        out.append(more)
    return clip("\n".join(out), 50_000, keep="head")


async def _calls(ctx: Context, direction: str, name: str, root: str | None, file: str | None, line: int | None, column: int | None, limit: int, offset: int) -> str:
    method = "callHierarchy/incomingCalls" if direction == "caller" else "callHierarchy/outgoingCalls"
    s = await _semantic(ctx, root, file, "Calls are not inferred from text.", "textDocument/prepareCallHierarchy", method)
    cache: dict[str, list[str] | None] = {}
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line, column)
        s.open(path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        symbol, _key, _decls = await _describe(s, name, doc, cache)
        item = await _call_item(s, name, doc)
        calls = [c for c in await s.request(method, {"item": item}) or [] if isinstance(c, dict)]
    site_path = None if direction == "caller" else lsp.path_of(item["uri"])  # callee call sites are in the selected symbol's file
    entries = []
    for call in calls:
        other = call.get("from" if direction == "caller" else "to") or {}
        target_path, target_line, row = _item_row(s, cache, other)
        where = site_path or target_path
        sites = [
            f"      call at {rel(where, s.root) if where else other.get('uri')}:{r['start']['line'] + 1}:{r['start']['character'] + 1}"
            for r in call.get("fromRanges") or []
        ]
        if direction == "callee":
            row += f"  [{_dispatch(cache, target_path, target_line)}]"
        entries.append((row, sites))
    entries.sort(key=lambda e: e[0])
    listed, reached_end, more = _page(entries, offset, limit)
    complete, reason, note = _completeness(s, reached_end)
    out = [_provenance("semantic", "semantic", s.root, complete, reason), symbol]
    if note:
        out.append(note)
    out.append(f"{len(entries)} direct {direction}(s) of '{name}' found by the language server." if entries else f"No direct {direction}s of '{name}' found by the language server.")
    for row, sites in listed:
        out.append("  " + row)
        out.extend(sites[:20])
        if len(sites) > 20:
            out.append(f"      [... {len(sites) - 20} more call sites ...]")
    if more:
        out.append(more)
    if direction == "callee" and any("possible dispatch" in row for row, _ in listed):
        out.append("A 'possible dispatch' target is the declaration the call resolves to statically; an override may run instead. find_implementations on that declaration lists the candidates.")
    out.append(_STATIC_NOTE)
    return clip("\n".join(out), 50_000, keep="head")


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=READ)
async def find_callers(
    ctx: Context,
    name: Annotated[str, Field(description="Identifier of the function, constructor or property whose callers to list.")],
    root: SearchRoot = None,
    file: AtFile = None,
    line: AtLine = None,
    column: AtColumn = None,
    limit: Limit = 100,
    offset: Offset = 0,
    expected_root: ExpectedRoot = None,
) -> str:
    """List the direct callers of one function: each calling declaration with its call sites, as
    the language server's call hierarchy resolves them. One level only; a function that calls
    itself is listed as its own caller, and chains are not followed. Overloads are separate
    symbols, selected by `file`, `line` and `column`. Semantic only: an error, never a text
    search, when the language server is missing, not ready or has no call hierarchy. Static
    relationships, not a runtime order."""
    return await _calls(ctx, "caller", name, root, file, line, column, limit, offset)


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=READ)
async def find_callees(
    ctx: Context,
    name: Annotated[str, Field(description="Identifier of the function, constructor or property whose outgoing calls to list.")],
    root: SearchRoot = None,
    file: AtFile = None,
    line: AtLine = None,
    column: AtColumn = None,
    limit: Limit = 100,
    offset: Offset = 0,
    expected_root: ExpectedRoot = None,
) -> str:
    """List what one function calls directly: each target declaration with the call sites inside
    the function, as the language server's call hierarchy resolves them. Each target is marked
    `exact` (a final declaration), `possible dispatch` (an interface, abstract, open or
    overriding member, so an override may run instead; see find_implementations) or
    `unresolved` (declared outside expected_root, such as a library). One level only; chains are
    not followed. Semantic only: an error, never a text search, when the language server is
    missing, not ready or has no call hierarchy. Static relationships, not a runtime order."""
    return await _calls(ctx, "callee", name, root, file, line, column, limit, offset)


def _hover_text(hover: dict | None) -> str:
    if not hover:
        return ""
    contents = hover.get("contents")
    if isinstance(contents, dict):
        text = contents.get("value", "")
    elif isinstance(contents, list):
        text = "\n".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in contents)
    else:
        text = str(contents or "")
    text = re.sub(r"`{3,}\w*\n?", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=READ)
async def symbol_info(
    ctx: Context,
    file: Annotated[str, Field(description="Absolute path of the file where the symbol appears.")],
    line: Annotated[int, Field(description="1-based line where it appears.", ge=1)],
    name: Annotated[str | None, Field(description="The identifier on that line. Give this or `column`.")] = None,
    column: Annotated[int | None, Field(description="1-based column, when the identifier occurs more than once on the line.", ge=1)] = None,
    expected_root: ExpectedRoot = None,
) -> str:
    """Resolve the symbol at a position, like hovering it in the IDE: its full signature with
    inferred types, its documentation, and where it is declared (project file and line, or the
    library it comes from). Works for library and JDK symbols as well as project code. Requires
    the Kotlin language server."""
    s = await _semantic(ctx, None, file, "Without it, use find_symbol for a text-based lookup by name.", "textDocument/hover")
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line, column)
        _require_imported(s, path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        hover = await s.request("textDocument/hover", doc)
        decls = await s.request("textDocument/definition", doc) or []
    if isinstance(decls, dict):
        decls = [decls]
    text = _hover_text(hover)
    if not text and not decls:
        return f"Nothing resolvable at {path}:{line}" + (f" ('{name}')" if name else "") + "."
    out = [text] if text else []
    for d in decls[:5]:
        uri = d.get("uri") or d.get("targetUri")
        rng = d.get("range") or d.get("targetSelectionRange")
        if uri.startswith("file:"):
            out.append(f"Declared at: {rel(lsp.path_of(uri), s.root)}:{rng['start']['line'] + 1}")
        else:
            out.append(f"Declared in: {uri}")
    return clip("\n".join(out), 20_000, keep="head")


@ops.tool(kind="semantic", needs=(TARGET, PROJECT, SEMANTIC), annotations=READ)
async def file_problems(
    ctx: Context,
    file: Annotated[str, Field(description="Absolute path of a .kt, .kts or .java file.")],
    min_severity: Annotated[
        Literal["error", "warning", "info", "hint"], Field(description="Least severe level to report.")
    ] = "warning",
    expected_root: ExpectedRoot = None,
) -> str:
    """Analyse one file with IntelliJ's inspections, like the problems the IDE highlights:
    compiler errors plus inspection warnings (unused code, simplifiable expressions, deprecated
    API, ...), each with the quick fixes the IDE would offer. It needs no build and reflects the
    file as it is on disk. Requires the Kotlin language server; `compile` and `detekt` are the
    alternatives without it."""
    path = _source(file)
    assert path is not None
    s = await _semantic(ctx, None, file, "Without it, use compile(file=...) for compiler errors and detekt for static analysis.", "textDocument/diagnostic")
    _require_imported(s, path)
    async with s.lock:
        await s.sync()
        s.open(path)
        report = await s.request("textDocument/diagnostic", {"textDocument": {"uri": lsp.uri_of(path)}}, timeout=300) or {}
    worst = {"error": 1, "warning": 2, "info": 3, "hint": 4}[min_severity]
    items = sorted(
        (d for d in report.get("items") or [] if d.get("severity", 1) <= worst),
        key=lambda d: (d.get("severity", 1), d["range"]["start"]["line"], d["range"]["start"]["character"]),
    )
    header = f"{rel(path, s.root)}: "
    if not items:
        return header + f"no problems at {min_severity} level or above."
    counts: dict[str, int] = {}
    out = []
    for d in items[:200]:
        sev = _SEVERITY.get(d.get("severity", 1), "error")
        counts[sev] = counts.get(sev, 0) + 1
        start = d["range"]["start"]
        code = d.get("code")
        if isinstance(code, dict):
            code = code.get("value")
        message = " ".join(str(d.get("message", "")).split())
        entry = f"  {start['line'] + 1}:{start['character'] + 1}: {sev}: {message}" + (f" [{code}]" if code else "")
        fixes = [f.get("name") for f in ((d.get("data") or {}).get("fixes") or []) if isinstance(f, dict) and f.get("name")]
        if fixes:
            entry += "\n      fixes: " + "; ".join(fixes[:6])
        out.append(entry)
    summary = ", ".join(f"{n} {k}(s)" for k, n in counts.items())
    result = [header + summary + ":"] + out
    if len(items) > 200:
        result.append(f"  [... {len(items) - 200} more ...]")
    return clip("\n".join(result), 40_000, keep="head")


# ----------------------------------------------------------------------------- readiness


@ops.tool(kind="readiness", needs=(), annotations=READ, structured_output=True)
async def readiness(expected_root: ExpectedRoot = None, project_dir: ProjectDir = None) -> Readiness:
    """Report what this server could do for a scope right now, without doing it: the validated
    scope, the Gradle project found in it, whether Gradle can be started (`execution`) and
    whether code-aware analysis can be answered (`semantic`), each with a reason code when it
    is not, plus the registered operations and the checks each enforces.

    Nothing is run, imported, indexed, scanned or written to find this out, so a state that
    could only be established by doing the work is `unknown`: an installed wrapper, JDK or
    language server is not evidence that it works. This is a current observation, not an
    authorisation: every operation checks its own prerequisites again when it runs, and a
    readiness response says nothing about whether a build or test passed. There is no overall
    flag; read `execution` and `semantic` separately."""
    scope = active()
    try:
        project: GradleProject | None = _project(project_dir, search_below=False)
    except Blocked:
        raise
    except ToolError as e:
        project, why = None, str(e).splitlines()[0]
    if project is None:
        execution: tuple[str, str | None, str | None] = ("unavailable", "project_not_found", why)
    else:
        execution = gradle.execution_state(project)
    return ops.report(scope, project.root if project else None, execution, lsp.semantic_state(project.root if project else scope.root))


# ----------------------------------------------------------------------------- entry point


def check() -> int:
    """Self-test for install scripts: show what the server sees."""
    ops.verify()
    tools = asyncio.run(mcp.list_tools())
    print(f"server:  kotlin ({len(tools)} tools)")
    print(f"cwd:     {Path.cwd()}")
    java = os.environ.get("JAVA_HOME") or shutil.which("java")
    print(f"java:    {java or 'NOT FOUND - Gradle needs JAVA_HOME or java on PATH'}")
    sdk = android_sdk()
    print(f"android: {sdk or 'no SDK found (fine for JVM projects, or when local.properties sets sdk.dir)'}")
    print(f"detekt:  {_find_cli('DETEKT_CLI', 'detekt', 'detekt-cli') or 'no CLI (Gradle detekt tasks are used when present)'}")
    print(f"ktlint:  {_find_cli('KTLINT_CLI', 'ktlint') or 'no CLI (Gradle ktlint/spotless tasks are used when present)'}")
    print(f"lsp:     {lsp.find_server() or 'not installed (run: kotlin-mcp --install-lsp) - code-aware tools are blocked; engine=\'text\' is the explicit alternative'}")
    try:
        project = gradle.find_project(None)
        print(f"gradle:  build at {project.root} via {' '.join(project.launcher)}")
    except ToolError:
        print("gradle:  cwd is not a Gradle project (expected; tools take project_dir)")
    return 0 if java else 1


def main() -> None:
    args = sys.argv[1:]
    if "--check" in args:
        sys.exit(check())
    if "--install-lsp" in args:
        rest = [a for a in args if not a.startswith("--")]
        try:
            lsp.install(rest[0] if rest else None)
        except ToolError as e:
            sys.exit(f"FAILED: {e}")
        return
    ops.verify()
    mcp.run()


if __name__ == "__main__":
    main()
