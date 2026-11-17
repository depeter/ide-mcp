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

from . import gradle, lsp, symbols
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
        "With the Kotlin language server installed, find_usages, rename_symbol, symbol_info and "
        "file_problems are code-aware like the IDE: they resolve the exact symbol, and file_problems "
        "reports IntelliJ inspections without a build. The first call per project imports and "
        "indexes it, which takes from seconds to minutes; later calls are fast. When a name is "
        "declared more than once, pass `file` and `line` of one place it appears. If the language "
        "server is missing or cannot import the project, find_usages and rename_symbol say so and "
        "fall back to whole-word text matching. find_symbol and file_outline always read source "
        "text. Run compile after a rename."
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


async def _text_find_usages(name: str, root: str | None, file_glob: str | None, include_imports: bool, limit: int) -> str:
    """Whole-word text match over Kotlin, Java and XML sources."""
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


async def _text_rename(name: str, new_name: str, root: str | None, file_glob: str | None, dry_run: bool, rename_files: bool) -> str:
    """Whole-word replacement in Kotlin, Java and XML files; does not resolve symbols."""
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


# ----------------------------------------------------------------------------- code-aware tools

Engine = Annotated[
    Literal["auto", "semantic", "text"],
    Field(
        description="'semantic' resolves the symbol with the Kotlin language server; 'text' matches the "
        "identifier as a whole word. 'auto' uses semantic when the language server is installed."
    ),
]
AtFile = Annotated[
    str | None,
    Field(description="Absolute path of a file where the symbol appears (declaration or any usage). Needed when the name is not unique."),
]
AtLine = Annotated[int | None, Field(description="1-based line in `file` where the symbol appears.", ge=1)]

_SYMBOL_KINDS = {
    2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property", 8: "field", 9: "constructor",
    10: "enum", 11: "interface", 12: "function", 13: "variable", 14: "constant", 22: "enum member", 23: "struct", 26: "type parameter",
}
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _workspace_root(root: str | None, file: str | None) -> Path:
    """Project the language server should import: the Gradle root when there is one."""
    try:
        return gradle.find_project(root or file).root
    except ToolError:
        if root:
            return existing_dir(root)
        if file:
            return resolve(file).parent
        return existing_dir(None)


def _use_semantic(engine: str) -> bool:
    if engine == "text":
        return False
    if lsp.find_server() is None:
        if engine == "semantic":
            raise ToolError(lsp.NOT_INSTALLED)
        return False
    return True


async def _session(ctx: Context | None, root: Path, timeout: float = 600.0) -> lsp.Session:
    """The language server session for a project. Raises when its import failed or was partial,
    because answers from an incomplete model look complete and are not."""
    async with Progress(ctx) as p:
        s = await lsp.session_for(root, timeout, lambda message: setattr(p, "last", message))
    if why := s.unreliable():
        raise lsp.LspError("Code-aware analysis is unavailable for this project: " + why)
    return s


def _require_imported(s: lsp.Session, path: Path) -> None:
    if s.left_out(path):
        raise lsp.LspError(
            f"{path} was left out of the language server's import (workaround for Kotlin/kotlin-lsp#253 "
            "on Android test sources), so it cannot be analysed. Use compile or detekt for it."
        )


async def _session_or_text(ctx: Context | None, root: Path, engine: str) -> tuple[lsp.Session | None, str]:
    """For tools with a text fallback: the session, or None plus a note saying why text matching
    is used instead. engine='semantic' never falls back."""
    try:
        return await _session(ctx, root), ""
    except lsp.LspError as e:
        if engine == "semantic":
            raise
        return None, f"[text match, same-named symbols are not told apart. {e}]\n"


async def _locate(
    s: lsp.Session, name: str | None, file: str | None, line: int | None, column: int | None = None
) -> tuple[Path, dict]:
    """Resolve what the caller pointed at into a document position the server understands."""
    if file:
        path = _source(file)
        assert path is not None
        lines = s.open(path).splitlines()
        rx = symbols.word_regex(name) if name else None
        if line is not None:
            if line > len(lines):
                raise ToolError(f"{path} has only {len(lines)} lines.")
            text = lines[line - 1]
            if column is not None:
                idx = column - 1
            elif rx and (m := rx.search(text)):
                idx = m.start()
            elif rx:
                raise ToolError(f"'{name}' does not appear on line {line} of {path}: {text.strip()[:120]}")
            else:
                raise ToolError("Give `name` (the identifier on that line) or `column`.")
            return path, {"line": line - 1, "character": lsp.utf16_col(text, idx)}
        if not rx:
            raise ToolError("Give `line`, or `name` to use its first occurrence in the file.")
        for i, text in enumerate(lines):
            if not text.lstrip().startswith(("import ", "package ", "//", "*", "/*")) and (m := rx.search(text)):
                return path, {"line": i, "character": lsp.utf16_col(text, m.start())}
        raise ToolError(f"'{name}' does not appear in {path}.")

    if not name:
        raise ToolError("Give `name`, or `file` and `line`.")
    found = await s.request("workspace/symbol", {"query": name}) or []
    root_key = path_key(s.root)
    exact = []
    for sym in found:
        loc = sym.get("location") or {}
        if sym.get("name") != name or "range" not in loc:
            continue
        p = lsp.path_of(loc["uri"])
        if path_key(p).startswith(root_key):
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
        raise ToolError(f"'{name}' is declared in {len(exact)} places. Call again with `file` and `line` of the one you mean:\n{listing}")
    return exact[0][0], exact[0][1]


def _line_text(cache: dict[str, list[str]], path: Path, line: int) -> str:
    key = str(path)
    if key not in cache:
        try:
            cache[key] = path.read_bytes().decode("utf-8", "replace").splitlines()
        except OSError:
            cache[key] = []
    return cache[key][line].strip()[:240] if line < len(cache[key]) else ""


def _where(path: Path, root: Path) -> str:
    """Project-relative path, or a readable form for library and JDK sources."""
    text = rel(path, root)
    return text


@mcp.tool(annotations=READ)
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
) -> str:
    """Find usages of a symbol across the project. With the Kotlin language server installed this
    is code-aware, like Find Usages in the IDE: it lists references to that one symbol and
    ignores unrelated symbols with the same name. If the name is declared more than once, the
    error lists the candidates; call again with `file` and `line`. Without the language server
    (or with engine='text') it is a whole-word text search, labelled as such."""
    if not _use_semantic(engine):
        note = "" if engine == "text" else "[text match; the Kotlin language server is not installed, so same-named symbols are not told apart]\n"
        return note + await _text_find_usages(name, root, file_glob, include_imports, limit)

    s, note = await _session_or_text(ctx, _workspace_root(root, file), engine)
    if s is None:
        return note + await _text_find_usages(name, root, file_glob, include_imports, limit)
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line)
        s.open(path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        refs = await s.request("textDocument/references", {**doc, "context": {"includeDeclaration": True}}) or []
        decls = await s.request("textDocument/definition", doc) or []
    if isinstance(decls, dict):
        decls = [decls]
    decl_keys = {(path_key(lsp.path_of(d.get("uri") or d.get("targetUri"))), (d.get("range") or d.get("targetSelectionRange"))["start"]["line"]) for d in decls}
    match = glob_matcher(file_glob)
    cache: dict[str, list[str]] = {}
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    counts = {"usage": 0, "declaration": 0, "import": 0}
    for r in refs:
        p = lsp.path_of(r["uri"])
        ln = r["range"]["start"]["line"]
        shown = rel(p, s.root)
        if not match(shown):
            continue
        text = _line_text(cache, p, ln)
        kind = "declaration" if (path_key(p), ln) in decl_keys else "import" if text.startswith("import ") else "usage"
        counts[kind] += 1
        if kind == "import" and not include_imports:
            continue
        by_file.setdefault(shown, []).append((ln + 1, {"usage": " ", "declaration": "D", "import": "I"}[kind], text))
    if not refs:
        return s.caveat() + f"No references to '{name}' found by the language server in {s.root}."
    out = [
        s.caveat() + f"'{name}' in {s.root}: {counts['usage']} usage(s), {counts['declaration']} declaration(s), {counts['import']} import(s) "
        f"across {len(by_file)} file(s).  [code-aware; D = declaration" + (", I = import" if include_imports else "") + "]"
    ]
    shown_lines = 0
    for f in sorted(by_file):
        if shown_lines >= limit:
            break
        out.append(f)
        for ln, tag, text in sorted(set(by_file[f]))[: limit - shown_lines]:
            out.append(f"  {ln:>5} {tag} {text}")
            shown_lines += 1
    total = sum(len(set(v)) for v in by_file.values())
    if total > shown_lines:
        out.append(f"[... {total - shown_lines} more lines; narrow with file_glob or raise limit ...]")
    return clip("\n".join(out), 50_000, keep="head")


@mcp.tool(annotations=DESTRUCTIVE)
async def rename_symbol(
    ctx: Context,
    name: Annotated[str, Field(description="Current identifier.")],
    new_name: Annotated[str, Field(description="New identifier.")],
    root: SearchRoot = None,
    file: AtFile = None,
    line: AtLine = None,
    engine: Engine = "auto",
    dry_run: Annotated[bool, Field(description="Only report what would change. Set false to write the changes.")] = True,
    rename_files: Annotated[bool, Field(description="Also rename Name.kt / Name.java when the file declares the renamed type.")] = True,
    file_glob: Annotated[str | None, Field(description="Text engine only: limit to files matching this glob.")] = None,
) -> str:
    """Rename a symbol everywhere it is used. With the Kotlin language server installed this is
    the IDE's rename refactoring: only references to that one symbol change, and unrelated
    symbols with the same name are left alone. If the name is declared more than once, the error
    lists the candidates; call again with `file` and `line`. It defaults to a dry run that shows
    every affected line; pass dry_run=false to apply, then compile. Without the language server
    (or with engine='text') it replaces the identifier as a whole word in all sources under root,
    which also hits same-named symbols, comments and strings."""
    if not symbols.is_identifier(name) or not symbols.is_identifier(new_name):
        raise ToolError("name and new_name must be plain identifiers (letters, digits, underscore).")
    if name == new_name:
        raise ToolError("new_name is the same as name.")
    if not _use_semantic(engine):
        note = "" if engine == "text" else "[text replacement; the Kotlin language server is not installed, so same-named symbols are not told apart]\n"
        return note + await _text_rename(name, new_name, root, file_glob, dry_run, rename_files)

    s, note = await _session_or_text(ctx, _workspace_root(root, file), engine)
    if s is None:
        # Never write a text-based rename the caller did not ask for: show what it would do.
        preview = await _text_rename(name, new_name, root, file_glob, True, rename_files)
        if dry_run:
            return note + preview
        return (
            note + "Nothing was written. A text replacement would affect every symbol with this name:\n"
            + preview.replace("\nNothing was written. Call again with dry_run=false to apply.", "")
            + "\nTo apply it anyway, call again with engine='text' and dry_run=false."
        )
    async with s.lock:
        await s.sync()
        path, pos = await _locate(s, name, file, line)
        s.open(path)
        doc = {"textDocument": {"uri": lsp.uri_of(path)}, "position": pos}
        decls = await s.request("textDocument/definition", doc) or []
        edit = await s.request("textDocument/rename", {**doc, "newName": new_name}, timeout=300)
        if not edit:
            raise ToolError(f"The language server cannot rename '{name}' at {path}:{pos['line'] + 1} (not a renameable symbol, or it is declared in a library).")
        changes = await asyncio.to_thread(lsp.plan_workspace_edit, edit)
        if not changes:
            return f"Renaming '{name}' to '{new_name}' changes nothing."

        # The refactoring renames the symbol; keep the file name in step with a top-level type.
        if isinstance(decls, dict):
            decls = [decls]
        if rename_files:
            for d in decls:
                dpath = lsp.path_of(d.get("uri") or d.get("targetUri"))
                if dpath.stem != name or dpath.suffix not in symbols.SOURCE_SUFFIXES:
                    continue
                target = dpath.with_name(new_name + dpath.suffix)
                change = next((c for c in changes if path_key(c.path) == path_key(dpath)), None)
                if change and not change.new_path and not target.exists():
                    change.new_path = target

        root_key = path_key(s.root)
        outside = [c.path for c in changes if not path_key(c.path).startswith(root_key)]
        if outside:
            raise ToolError("The rename would edit files outside the project, so nothing was changed:\n" + "\n".join(f"  {p}" for p in outside[:10]))
        if not dry_run:
            await asyncio.to_thread(lsp.write_changes, changes)
            s.forget_open()

    total = sum(len(c.changed_lines()) for c in changes)
    verb = "Would rename" if dry_run else "Renamed"
    out = [s.caveat() + f"{verb} '{name}' to '{new_name}': {total} line(s) in {len(changes)} file(s) under {s.root}.  [code-aware]"]
    budget = 150
    for c in sorted(changes, key=lambda c: str(c.path)):
        lines = c.changed_lines()
        label = rel(c.path, s.root) + f" ({len(lines)})"
        if c.new_path:
            label += f"  -> {'will be ' if dry_run else ''}renamed to {c.new_path.name}"
        out.append(label)
        for ln, text in lines[: max(budget, 0)]:
            out.append(f"  {ln:>5}  {text[:200]}")
        budget -= len(lines)
    out.append("\nNothing was written. Call again with dry_run=false to apply." if dry_run else "\nRun compile to verify.")
    return clip("\n".join(out), 50_000, keep="head")


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


@mcp.tool(annotations=READ)
async def symbol_info(
    ctx: Context,
    file: Annotated[str, Field(description="Absolute path of the file where the symbol appears.")],
    line: Annotated[int, Field(description="1-based line where it appears.", ge=1)],
    name: Annotated[str | None, Field(description="The identifier on that line. Give this or `column`.")] = None,
    column: Annotated[int | None, Field(description="1-based column, when the identifier occurs more than once on the line.", ge=1)] = None,
) -> str:
    """Resolve the symbol at a position, like hovering it in the IDE: its full signature with
    inferred types, its documentation, and where it is declared (project file and line, or the
    library it comes from). Works for library and JDK symbols as well as project code. Requires
    the Kotlin language server."""
    if lsp.find_server() is None:
        raise ToolError(lsp.NOT_INSTALLED + " Without it, use find_symbol for a text-based lookup by name.")
    s = await _session(ctx, _workspace_root(None, file))
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


@mcp.tool(annotations=READ)
async def file_problems(
    ctx: Context,
    file: Annotated[str, Field(description="Absolute path of a .kt, .kts or .java file.")],
    min_severity: Annotated[
        Literal["error", "warning", "info", "hint"], Field(description="Least severe level to report.")
    ] = "warning",
) -> str:
    """Analyse one file with IntelliJ's inspections, like the problems the IDE highlights:
    compiler errors plus inspection warnings (unused code, simplifiable expressions, deprecated
    API, ...), each with the quick fixes the IDE would offer. It needs no build and reflects the
    file as it is on disk. Requires the Kotlin language server; `compile` and `detekt` are the
    alternatives without it."""
    if lsp.find_server() is None:
        raise ToolError(lsp.NOT_INSTALLED + " Without it, use compile(file=...) for compiler errors and detekt for static analysis.")
    path = _source(file)
    assert path is not None
    s = await _session(ctx, _workspace_root(None, file))
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
    print(f"lsp:     {lsp.find_server() or 'not installed (run: kotlin-mcp --install-lsp) - find_usages/rename_symbol fall back to text matching'}")
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
    mcp.run()


if __name__ == "__main__":
    main()
