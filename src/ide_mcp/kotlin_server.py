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

from . import gradle, symbols
from .common import ToolError, android_sdk, clip, existing_dir, glob_matcher, iter_files, path_key, rel, resolve, run_proc
from .gradle import GradleProject, GradleRun, task_path

mcp = FastMCP(
    "kotlin",
    instructions=(
        "Gradle, detekt, ktlint and Kotlin/Java symbol tools that work on any project on disk; "
        "nothing has to be open in Android Studio or IntelliJ.\n"
        "Every Gradle tool takes `project_dir`: any absolute path inside the project (a file works "
        "too). The Gradle root is found from there, including <repo>/android layouts. Modules are "
        "Gradle paths like ':app' or ':features:login'; passing `file` instead picks the module "
        "that owns that file.\n"
        "After editing code, call `compile` with the file you changed: it compiles just that module "
        "and lists errors as path:line:col. Use run_tests for unit tests, detekt and ktlint for "
        "static analysis, gradle_run for any other task. Builds can take minutes; results are "
        "summarised and the full log path is included.\n"
        "find_symbol, find_usages and rename_symbol work on source text (whole-word matches), not "
        "on resolved types: check find_usages before renaming a name that is not unique, and run "
        "compile afterwards."
    ),
)

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

ProjectDir = Annotated[
    str | None,
    Field(description="Absolute path anywhere inside the Gradle project (directory or file). Defaults to the server's start directory."),
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
    Field(description="Absolute directory to search (a project root or any subdirectory). Defaults to the server's start directory."),
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


def _project(project_dir: str | None, file: str | None = None) -> GradleProject:
    return gradle.find_project(project_dir if project_dir else file)


def _source(file: str | None) -> Path | None:
    if not file:
        return None
    p = resolve(file)
    if not p.is_file():
        raise ToolError(f"File does not exist: {p}")
    return p


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


@mcp.tool(annotations=READ)
async def project_info(project_dir: ProjectDir = None) -> str:
    """Describe the Gradle build that owns a path: root directory, Gradle/AGP/Kotlin versions,
    modules, and which static-analysis tools are configured. Reads files only; does not run
    Gradle."""
    project = _project(project_dir)
    root = project.root

    def gather() -> str:
        out = [f"Gradle root: {root}", f"Launcher:    {' '.join(project.launcher)}"]
        props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
        if props.is_file() and (m := re.search(r"gradle-([\d.]+(?:-[\w.]+)?)-(?:bin|all)\.zip", props.read_text(errors="replace"))):
            out.append(f"Gradle:      {m.group(1)}")
        out.append(f"Java:        {os.environ.get('JAVA_HOME') or shutil.which('java') or 'not found (set JAVA_HOME for this server)'}")

        build_text = ""
        for name in (*gradle.BUILD_FILES, *gradle.SETTINGS, "gradle/libs.versions.toml"):
            f = root / name
            if f.is_file():
                build_text += f.read_text(errors="replace") + "\n"
        versions = []
        toml = root / "gradle" / "libs.versions.toml"
        if toml.is_file():
            for line in toml.read_text(errors="replace").splitlines():
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


@mcp.tool(annotations=READ)
async def gradle_tasks(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Annotated[str | None, Field(description="Gradle project path, e.g. ':app'. Omit to search every module.")] = None,
    filter: Annotated[str | None, Field(description="Case-insensitive substring of the task name, e.g. 'test' or 'detekt'.")] = None,
    refresh: Annotated[bool, Field(description="Re-query Gradle instead of using the cached task list (after editing build scripts).")] = False,
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


@mcp.tool(annotations=DESTRUCTIVE)
async def gradle_run(
    ctx: Context,
    tasks: Annotated[list[str], Field(description="Tasks to run, e.g. [':app:assembleDebug'] or ['clean', 'build'].")],
    args: Annotated[list[str] | None, Field(description="Extra Gradle arguments, e.g. ['--offline', '-Pfoo=bar', '--stacktrace'].")] = None,
    project_dir: ProjectDir = None,
    show_warnings: Annotated[bool, Field(description="List compiler warnings as well as errors.")] = False,
    timeout_sec: Timeout = 1800,
) -> str:
    """Run any Gradle tasks and summarise the result: outcome, failed tasks, compiler and lint
    errors as path:line:col, test failures, and Gradle's 'What went wrong'. The full log is
    saved to a file whose path is returned. Prefer compile / run_tests / detekt / ktlint for
    those jobs; they pick the right task for you."""
    project = _project(project_dir)
    if not tasks and not args:
        raise ToolError("Give at least one task.")
    run = await _run(ctx, project, [*tasks, *(args or [])], timeout_sec)
    rep = gradle.parse_output(run.output, project.root)
    extra = ""
    if any("test" in t.lower() for t in tasks):
        dirs = [d / "build" / "test-results" for d in gradle.module_dirs(project.root).values()]
        summary = await asyncio.to_thread(gradle.collect_tests, dirs, run.started)
        if summary.suites:
            extra = summary.render()
    return format_run(run, rep, show_warnings=show_warnings, extra=extra)


def format_run(run: GradleRun, rep: gradle.Report, **kw) -> str:
    return clip(gradle.format_report(run, rep, **kw), 40_000, keep="head")


@mcp.tool(name="compile", annotations=ACT)
async def compile_sources(
    ctx: Context,
    project_dir: ProjectDir = None,
    module: Module = None,
    file: SourceFile = None,
    variant: Variant = "debug",
    tests: Annotated[bool, Field(description="Compile unit-test sources too (which also compiles main).")] = False,
    show_warnings: Annotated[bool, Field(description="List compiler warnings as well as errors.")] = False,
    timeout_sec: Timeout = 1200,
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
        return f"No problems in {src} ({rep.outcome or 'BUILD SUCCESSFUL'}; ran {' '.join(targets)})."
    return format_run(run, rep, show_warnings=show_warnings, only_files=only, extra="\n".join(notes))


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


@mcp.tool(annotations=ACT)
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
) -> str:
    """Run JVM unit tests and report counts plus each failure with its message and trimmed
    stack trace. Scope with `file` (one test file), `tests` filters, and/or `module`; with none
    of them every module's unit tests run. When only `tests` is given, the module is located
    from the test class name."""
    src = _source(file)
    project = _project(project_dir, file)
    filters = list(tests or [])
    if src and not filters:
        text = src.read_text(errors="replace")
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
    return format_run(run, rep, extra="\n".join([summary.render(), *notes]), tail=not summary.failed)


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
        return format_run(run, rep)  # the build itself broke; show why
    return _findings(title, run, findings, project.root, only)


@mcp.tool(annotations=ACT)
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
) -> str:
    """Run detekt and list findings as path:line:col: message [Rule]. Uses the project's Gradle
    detekt task (so its config, baseline and plugins apply); if the build has no such task,
    falls back to a `detekt` CLI on PATH with the project's detekt.yml."""
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
    if r.timed_out or (r.code != 0 and not diags):
        raise ToolError(f"detekt CLI failed (exit {r.code}):\n" + clip(r.output, 6000, keep="tail"))
    return _findings("detekt (CLI)", None, diags, project.root, only)


_KTLINT_TASKS = (("ktlintCheck", "ktlintFormat"), ("lintKotlin", "formatKotlin"), ("spotlessCheck", "spotlessApply"))


@mcp.tool(annotations=ACT)
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
            return f"Formatted with {name} ({gradle.parse_output(run.output, project.root).outcome or 'done'}).\nFull log: {run.log}"
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
    if r.timed_out or (r.code != 0 and not diags):
        raise ToolError(f"ktlint CLI failed (exit {r.code}):\n" + clip(r.output, 6000, keep="tail"))
    title = "ktlint (CLI, formatted; remaining" if format else "ktlint (CLI"
    return _findings(title + ")", None, diags, project.root, only)


_DEP_LINE = re.compile(r"^[| ]*[+\\]--- (?P<coord>[^\s:]+:[^\s:]+)(?::(?P<ver>[^\s:]+))?(?: -> (?P<res>\S+))?")
_PROJECT_DEP = re.compile(r"^[| ]*[+\\]--- project (?P<path>:\S*)")


@mcp.tool(annotations=READ)
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
        return format_run(run, gradle.parse_output(run.output, project.root))
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
    return sorted(f for f in iter_files(root) if (file_glob or f.suffix in suffixes) and match(rel(f, root)))


def _read(f: Path) -> str | None:
    try:
        if f.stat().st_size > 2_000_000:
            return None
        return f.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


@mcp.tool(annotations=READ)
async def find_symbol(
    name: Annotated[str, Field(description="Simple name of a class, function, property, object or typealias.")],
    root: SearchRoot = None,
    kind: Annotated[Kind | None, Field(description="Only declarations of this kind.")] = None,
    exact: Annotated[bool, Field(description="Match the whole name. False finds names containing it, case-insensitively.")] = True,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
) -> str:
    """Find where a Kotlin or Java symbol is declared. Returns each declaration's location,
    package, signature and doc comment. Matching is by name from source text, so overloads and
    same-named symbols in different packages are all listed."""
    base = existing_dir(root)
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


@mcp.tool(annotations=READ)
async def file_outline(
    path: Annotated[str, Field(description="Absolute path of a .kt, .kts or .java file.")],
) -> str:
    """List the declarations in a Kotlin or Java file (classes, functions, properties) with
    line numbers, nested by containing class. A quick way to see a large file's structure."""
    p = _source(path)
    assert p is not None
    text = await asyncio.to_thread(p.read_text, "utf-8", "replace")
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


@mcp.tool(annotations=READ)
async def find_usages(
    name: Annotated[str, Field(description="Identifier to look for, matched as a whole word.")],
    root: SearchRoot = None,
    file_glob: Annotated[str | None, Field(description="Limit to files matching this glob. Default: *.kt, *.kts, *.java and *.xml.")] = None,
    include_imports: Annotated[bool, Field(description="Also list import lines.")] = False,
    limit: Annotated[int, Field(description="Maximum lines to list.", ge=1, le=2000)] = 150,
) -> str:
    """Find every place an identifier appears in Kotlin, Java and XML sources, grouped by file
    and labelled as declaration, usage or comment. This is a whole-word text match: unrelated
    symbols with the same name are included."""
    base = existing_dir(root)
    results = await asyncio.to_thread(_usage_scan, base, name, file_glob)
    counts = {"usage": 0, "declaration": 0, "import": 0, "comment": 0}
    out: list[str] = []
    shown = 0
    for f, hits in results:
        lines = []
        for line_no, cls, text in hits:
            counts[cls] += 1
            if cls == "import" and not include_imports:
                continue
            if shown < limit:
                tag = {"usage": " ", "declaration": "D", "comment": "#", "import": "I"}[cls]
                lines.append(f"  {line_no:>5} {tag} {text[:240]}")
                shown += 1
        if lines:
            out.append(rel(f, base))
            out.extend(lines)
    total = sum(counts.values())
    if not total:
        return f"'{name}' does not appear under {base}."
    listed = total - (0 if include_imports else counts["import"])
    header = (
        f"'{name}' under {base}: {counts['usage']} usage(s), {counts['declaration']} declaration(s), "
        f"{counts['import']} import(s), {counts['comment']} in comments, across {len(results)} file(s)."
        "  [D = declaration, # = comment" + (", I = import" if include_imports else "") + "]"
    )
    if listed > shown:
        header += f"\nShowing {shown} of {listed} lines; narrow with root or file_glob."
    return clip(header + "\n" + "\n".join(out), 50_000, keep="head")


@mcp.tool(annotations=DESTRUCTIVE)
async def rename_symbol(
    name: Annotated[str, Field(description="Current identifier.")],
    new_name: Annotated[str, Field(description="New identifier.")],
    root: SearchRoot = None,
    file_glob: Annotated[str | None, Field(description="Limit to files matching this glob. Default: *.kt, *.kts, *.java and *.xml.")] = None,
    dry_run: Annotated[bool, Field(description="Only report what would change. Set false to write the changes.")] = True,
    rename_files: Annotated[bool, Field(description="Also rename Name.kt / Name.java when the file declares the renamed type.")] = True,
) -> str:
    """Rename an identifier everywhere under root by whole-word replacement in Kotlin, Java and
    XML files, including imports, comments and strings. It does not resolve types, so every
    symbol with this name under root is renamed: run with dry_run=true first (the default),
    narrow root or file_glob if unrelated matches show up, and compile afterwards."""
    if not symbols.is_identifier(name) or not symbols.is_identifier(new_name):
        raise ToolError("name and new_name must be plain identifiers (letters, digits, underscore).")
    if name == new_name:
        raise ToolError("new_name is the same as name.")
    base = existing_dir(root)
    rx, new_rx = symbols.word_regex(name), symbols.word_regex(new_name)

    def work() -> str:
        changes: list[tuple[Path, int, list[str]]] = []
        clashes: list[str] = []
        moves: list[tuple[Path, Path]] = []
        for f in _sources(base, file_glob, (".xml",)):
            text = _read(f)
            if text is None or name not in text:
                continue
            updated, n = rx.subn(new_name, text)
            if not n:
                continue
            if new_rx.search(text):
                clashes.append(rel(f, base))
            preview = [f"  {i + 1:>5}  {ln.strip()[:200]}" for i, ln in enumerate(text.splitlines()) if rx.search(ln)]
            changes.append((f, n, preview))
            target = f.with_name(new_name + f.suffix)
            declares_type = f.stem == name and symbols.is_source(f) and any(
                s.name == name and not s.container and s.kind not in ("fun", "val", "var")
                for s in symbols.outline(text, java=f.suffix == ".java")
            )
            if not dry_run:
                f.write_bytes(updated.encode("utf-8"))
            if rename_files and declares_type:
                if target.exists():
                    clashes.append(f"{rel(target, base)} already exists; file not renamed")
                else:
                    moves.append((f, target))
                    if not dry_run:
                        f.rename(target)
        if not changes:
            return f"'{name}' does not appear under {base}; nothing to rename."
        total = sum(n for _, n, _ in changes)
        verb = "Would rename" if dry_run else "Renamed"
        out = [f"{verb} {total} occurrence(s) of '{name}' to '{new_name}' in {len(changes)} file(s) under {base}."]
        if clashes:
            out.append(f"WARNING: '{new_name}' already occurs in: " + ", ".join(clashes[:15]) + ". Check for name clashes.")
        for src, dst in moves:
            out.append(f"{'Would move' if dry_run else 'Moved'} {rel(src, base)} -> {dst.name}")
        budget = 120
        for f, n, preview in changes:
            out.append(f"{rel(f, base)} ({n})")
            if dry_run and budget > 0:
                out.extend(preview[:budget])
                budget -= len(preview)
        if dry_run:
            out.append("\nNothing was written. Call again with dry_run=false to apply.")
        else:
            out.append("\nRun compile to verify.")
        return "\n".join(out)

    return clip(await asyncio.to_thread(work), 50_000, keep="head")


# ----------------------------------------------------------------------------- entry point


def check() -> int:
    """Self-test for install scripts: show what the server sees."""
    tools = asyncio.run(mcp.list_tools())
    print(f"server:  kotlin ({len(tools)} tools)")
    print(f"cwd:     {Path.cwd()}")
    java = os.environ.get("JAVA_HOME") or shutil.which("java")
    print(f"java:    {java or 'NOT FOUND - Gradle needs JAVA_HOME or java on PATH'}")
    sdk = android_sdk()
    print(f"android: {sdk or 'no SDK found (fine for JVM projects, or when local.properties sets sdk.dir)'}")
    print(f"detekt:  {_find_cli('DETEKT_CLI', 'detekt', 'detekt-cli') or 'no CLI (Gradle detekt tasks are used when present)'}")
    print(f"ktlint:  {_find_cli('KTLINT_CLI', 'ktlint') or 'no CLI (Gradle ktlint/spotless tasks are used when present)'}")
    try:
        project = gradle.find_project(None)
        print(f"gradle:  build at {project.root} via {' '.join(project.launcher)}")
    except ToolError:
        print("gradle:  cwd is not a Gradle project (expected; tools take project_dir)")
    return 0 if java else 1


def main() -> None:
    if "--check" in sys.argv[1:]:
        sys.exit(check())
    mcp.run()


if __name__ == "__main__":
    main()
