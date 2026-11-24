"""Gradle support: locate the build from any path, run it, and turn its output into a short
report of what failed and where."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .common import SKIP_DIRS, WINDOWS, Blocked, ToolError, active, android_sdk, clip, is_link, path_key, rel, resolve, run_proc
from .execution import WorkspaceLease, save_artifact, source_identity

SETTINGS = ("settings.gradle.kts", "settings.gradle")
BUILD_FILES = ("build.gradle.kts", "build.gradle")
LOG_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ide-mcp" / "logs"
KEEP_LOGS = 30


def _has(d: Path, names: tuple[str, ...]) -> bool:
    return any((d / n).is_file() for n in names)


# ----------------------------------------------------------------------------- locating


@dataclass(frozen=True)
class GradleProject:
    root: Path
    launcher: tuple[str, ...]


def _has_wrapper(root: Path) -> bool:
    return (root / ("gradlew.bat" if WINDOWS else "gradlew")).is_file()


def _launcher(root: Path) -> tuple[str, ...] | None:
    if WINDOWS:
        wrapper = root / "gradlew.bat"
        if wrapper.is_file():
            return (str(wrapper),)
    else:
        wrapper = root / "gradlew"
        if wrapper.is_file():
            # Run through sh so a wrapper checked in without the executable bit still works.
            return (str(wrapper),) if os.access(wrapper, os.X_OK) else ("sh", str(wrapper))
    system = shutil.which("gradle")
    return (system,) if system else None


def find_project(path: str | None, *, search_below: bool = True) -> GradleProject:
    """Find the Gradle build that owns `path` (a file or directory anywhere inside it).

    Walks up to the nearest settings file that has a wrapper next to it, so a path inside an
    included build without its own wrapper lands on the outer build. If nothing is found above,
    looks a few levels below, which covers Flutter/React Native layouts where the Gradle build
    lives in <repo>/android or <repo>/app/android. `search_below=False` skips that walk, for
    callers that must not scan directories.

    This only locates a build from file names. Callers verify the result against the request
    scope (kotlin_server._project) before using it.
    """
    start = resolve(path)
    if not start.exists():
        raise ToolError(f"Path does not exist: {start}")
    if start.is_file():
        start = start.parent

    with_settings = [d for d in (start, *start.parents) if _has(d, SETTINGS)]
    for d in with_settings:
        if _has_wrapper(d):
            return GradleProject(d, _launcher(d))  # type: ignore[arg-type]
    for d in with_settings or [d for d in (start, *start.parents) if _has(d, BUILD_FILES)]:
        launcher = _launcher(d)
        if launcher:
            return GradleProject(d, launcher)
        raise ToolError(f"Found a Gradle build at {d} but it has no gradlew wrapper and `gradle` is not on PATH.")

    below: list[Path] = []
    for dirpath, dirnames, _files in os.walk(start) if search_below else ():
        depth = len(Path(dirpath).relative_to(start).parts)
        dirnames[:] = [] if depth >= 3 else [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and not is_link(Path(dirpath) / d)
        ]
        if depth and _has(Path(dirpath), SETTINGS):
            below.append(Path(dirpath))
            dirnames[:] = []
    if len(below) == 1:
        launcher = _launcher(below[0])
        if launcher:
            return GradleProject(below[0], launcher)
    if below:
        raise ToolError(
            f"{start} is not inside a Gradle build, and several were found below it. Pass one as project_dir:\n"
            + "\n".join(f"  {d}" for d in sorted(below))
        )
    raise ToolError(
        f"No Gradle build found at or above {start} (looked for settings.gradle[.kts]). "
        "Pass project_dir pointing into the project."
    )


# ----------------------------------------------------------------------------- modules


def module_dirs(root: Path) -> dict[str, Path]:
    """Gradle project path -> directory, derived from where build files sit. This matches Gradle
    unless a build remaps projectDir; nested builds (their own settings file) are left out."""
    out: dict[str, Path] = {":": root}
    for dirpath, dirnames, _files in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = sorted(
            n for n in dirnames
            if n not in SKIP_DIRS and not n.startswith(".") and n != "buildSrc" and not _has(d / n, SETTINGS)
            and not is_link(d / n)
        )
        if d != root and _has(d, BUILD_FILES):
            out[":" + ":".join(d.relative_to(root).parts)] = d
    return out


def normalize_module(module: str | None) -> str | None:
    """Accept 'app', ':app', 'features/login' or ':features:login'."""
    if module is None:
        return None
    m = module.strip().replace("/", ":").strip(":")
    return ":" + m if m else ":"


def module_of(path: Path, root: Path) -> str:
    """Gradle project path of the module that contains `path`."""
    d = path if path.is_dir() else path.parent
    for cur in (d, *d.parents):
        if cur == root:
            return ":"
        if root not in cur.parents:
            break
        if _has(cur, BUILD_FILES):
            return ":" + ":".join(cur.relative_to(root).parts)
    raise ToolError(f"{path} is not inside the Gradle build at {root}.")


def task_path(module: str, task: str) -> str:
    return f":{task}" if module == ":" else f"{module}:{task}"


# ----------------------------------------------------------------------------- running


@dataclass
class GradleRun:
    project: GradleProject
    args: list[str]
    code: int
    output: str
    seconds: float
    log: Path | None
    timed_out: bool = False
    started: float = 0.0
    test_summary: TestSummary | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    source_start: dict[str, Any] = field(default_factory=dict)
    source_end: dict[str, Any] = field(default_factory=dict)
    output_complete: bool = True

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


_locks: dict[Path, asyncio.Lock] = {}
_launched: set[str] = set()  # path_key of every project root this server process has started Gradle in

# Options whose value points Gradle at a different project, settings or build script.
_REDIRECT_LONG = ("--project-dir", "--settings-file", "--build-file", "--include-build", "--init-script")
_REDIRECT_SHORT = ("-p", "-c", "-b", "-I")


def redirect_values(args: list[str]) -> list[tuple[str, str | None]]:
    """(option, path) for every argument that selects another project directory, settings
    file, build file, included build or init script, in all the forms Gradle accepts. The path
    is None when the option is the last argument and its value is missing."""
    found: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        a = args[i]
        following = args[i + 1] if i + 1 < len(args) else None
        if a in _REDIRECT_LONG or a in _REDIRECT_SHORT:
            found.append((a, following))
            i += 1
        elif a.startswith("--"):
            name, eq, value = a.partition("=")
            if eq and name in _REDIRECT_LONG:
                found.append((name, value))
        elif a[:2] in _REDIRECT_SHORT and len(a) > 2:
            found.append((a[:2], a[2:].lstrip("=")))
        i += 1
    return found


def check_arguments(project: GradleProject, args: list[str]) -> None:
    """Reject arguments that would make Gradle build something other than the verified project.
    A redirect to a location inside the request scope, such as `-p app`, is allowed."""
    scope = active()
    for option, value in redirect_values(args):
        if not value:
            raise Blocked("argument_redirect", f"Gradle option {option} is missing its path. Nothing was run.", "input")
        target = Path(os.path.expanduser(value))
        try:
            scope.check(target if target.is_absolute() else project.root / target,
                        protected=option in ("--settings-file", "--build-file", "--init-script", "-c", "-b", "-I"))
        except Blocked as e:
            if e.reason != "target_mismatch":
                raise
            raise Blocked(
                "argument_redirect",
                f"Gradle option {option} {value} would redirect the build away from {project.root}: {e.detail}",
            ) from None


def java() -> str | None:
    home = os.environ.get("JAVA_HOME")
    if home:
        return home if Path(home).is_dir() else None
    return shutil.which("java")


def execution_state(project: GradleProject) -> tuple[str, str | None, str | None]:
    """Whether Gradle can be started for this project, from what is already known: no process
    is run. `available` needs evidence, which an installed wrapper and JDK are not, so it is
    only reported once this server has actually started Gradle here."""
    launcher = Path(project.launcher[-1] if project.launcher[0] == "sh" else project.launcher[0])
    if not launcher.is_file():
        return "unavailable", "execution_unavailable", f"The Gradle launcher {launcher} is missing."
    if java() is None:
        return "unavailable", "execution_unavailable", "No JDK found: set JAVA_HOME for this server or put java on PATH."
    if path_key(project.root) in _launched:
        return "available", None, None
    return (
        "unknown",
        "execution_not_observed",
        "A Gradle launcher and a JDK are present, but this server has not run Gradle for this project yet; "
        "only running it shows whether it works.",
    )


def _gradle_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("TERM", "dumb")
    if "ANDROID_HOME" not in env and "ANDROID_SDK_ROOT" not in env and (sdk := android_sdk()):
        env["ANDROID_HOME"] = sdk
    return env


def _save_log(project: GradleProject, args: list[str], output: str) -> Path | None:
    artifact = save_artifact(f"$ gradle {' '.join(args)}   (in {project.root})\n\n{output}")
    return Path(artifact["path"]) if artifact["path"] else None


async def run_gradle(
    project: GradleProject,
    args: list[str],
    *,
    timeout: float = 900.0,
    on_line: Callable[[str], None] | None = None,
    save_log: bool = True,
    report_dirs: list[tuple[str, Path]] | None = None,
) -> GradleRun:
    """Run Gradle in the project root. Invocations for the same build are serialised, since a
    second client would only sit waiting for the first one's lock anyway.

    The project, its launcher and the arguments are verified here, immediately before the
    launch, whatever an earlier readiness query or project lookup found."""
    scope = active("execution")
    project = replace(project, root=scope.check_project(project.root))
    if within_project := [a for a in project.launcher if Path(a).name.startswith("gradlew")]:
        scope.check(within_project[0], protected=False)
    check_arguments(project, args)
    state, reason, detail = execution_state(project)
    if state == "unavailable":
        raise Blocked(reason or "execution_unavailable", f"Gradle cannot be started for {project.root}: {detail}", "environment")
    lock = _locks.setdefault(project.root, asyncio.Lock())
    async with lock, WorkspaceLease(project.root):
        started = time.time()
        source_start = source_identity(project.root)
        before = _test_files([p for _task, p in report_dirs or []]) if report_dirs else {}
        r = await run_proc(
            [*project.launcher, "--console=plain", *args],
            cwd=project.root,
            timeout=timeout,
            env=_gradle_env(),
            on_line=on_line,
        )
        _launched.add(path_key(project.root))  # it started, whatever the build's result
        seconds = time.time() - started
        summary = None
        if report_dirs is not None:
            summary = TestSummary()
            for task, directory in report_dirs:
                marker = re.search(rf"^> Task {re.escape(task)}(?: ([A-Z-]+))?$", r.output, re.MULTILINE)
                if marker is None or marker.group(1) in ("SKIPPED", "NO-SOURCE"):
                    summary.complete = False
                    summary.problems.append(f"{task}: no verified current test execution")
                    continue
                after = _test_files([directory])
                fresh = {path: data for path, (stamp, data) in after.items()
                         if path not in before or before[path] != (stamp, data)}
                reused = marker.group(1) in ("UP-TO-DATE", "FROM-CACHE")
                parsed = _parse_test_files({p: data for p, (_stamp, data) in after.items()} if reused else fresh)
                if reused:
                    summary.reused += parsed.executed
                    if parsed.failed:
                        summary.problems.append(f"{task}: reused evidence includes failures")
                    summary.complete = False
                else:
                    summary.total += parsed.total
                    summary.skipped += parsed.skipped
                    summary.failed += parsed.failed
                    summary.failures += parsed.failures
                    summary.suites += parsed.suites
                    summary.complete = summary.complete and parsed.complete
                    summary.problems += parsed.problems
        artifact = save_artifact(f"$ gradle {' '.join(args)}\n\n{r.output}") if save_log else None
        source_end = source_identity(project.root)
        log = Path(artifact["path"]) if artifact and artifact["path"] else None
    return GradleRun(project, args, r.code, r.output, seconds, log, r.timed_out, started,
                     summary, [artifact] if artifact else [], source_start, source_end, r.output_complete)


# ----------------------------------------------------------------------------- tasks

_TASK_LINE = re.compile(r"^([A-Za-z0-9_.\-]+(?::[A-Za-z0-9_.\-]+)*)(?: - (.*))?$")
_tasks_cache: dict[Path, dict[str, dict[str, str]]] = {}


def parse_tasks(output: str) -> dict[str, dict[str, str]]:
    """Parse `gradle tasks --all` into {project path: {task name: description}}."""
    result: dict[str, dict[str, str]] = {}
    lines = output.splitlines()
    in_section = False
    for i, line in enumerate(lines):
        if set(line) == {"-"} and len(line) > 2:
            in_section = i > 0 and lines[i - 1].endswith("tasks")
            continue
        if not line.strip():
            in_section = False
            continue
        if not in_section:
            continue
        m = _TASK_LINE.match(line)
        if not m:
            continue
        *proj, name = m.group(1).split(":")
        result.setdefault(":" + ":".join(proj), {})[name] = m.group(2) or ""
    return result


async def project_tasks(
    project: GradleProject, *, refresh: bool = False, on_line: Callable[[str], None] | None = None
) -> dict[str, dict[str, str]]:
    """All tasks of all projects in the build, from one `tasks --all` run, cached for the life
    of the server (pass refresh after changing build scripts)."""
    if not refresh and project.root in _tasks_cache:
        return _tasks_cache[project.root]
    run = await run_gradle(project, ["tasks", "--all", "-q"], timeout=600, on_line=on_line, save_log=False)
    if not run.ok:
        e = ToolError("Could not list Gradle tasks; the build does not configure.\n" + format_report(run, parse_output(run.output, project.root)))
        e.category = "unknown"  # a broken build script or a broken environment; the report says which
        raise e
    tasks = parse_tasks(run.output)
    if not tasks:
        raise ToolError("Gradle reported no tasks.\n" + clip(run.output, 4000, keep="tail"))
    _tasks_cache[project.root] = tasks
    return tasks


def forget_tasks(project: GradleProject) -> None:
    _tasks_cache.pop(project.root, None)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def pick_task(names: dict[str, str] | set[str], exact: list[str], patterns: list[str]) -> tuple[str | None, list[str]]:
    """First task present from `exact`, else the alphabetically first match of each regex in
    `patterns` in turn. Returns (task, other candidates of the same pattern) so the caller can
    mention product-flavour alternatives."""
    for name in exact:
        if name in names:
            return name, []
    for pat in patterns:
        rx = re.compile(pat)
        found = sorted(n for n in names if rx.fullmatch(n))
        if found:
            return found[0], found[1:]
    return None, []


def compile_task(names: dict[str, str] | set[str], variant: str, tests: bool) -> tuple[str | None, list[str]]:
    """Task that compiles a module's Kotlin and Java without packaging anything."""
    v = _cap(variant)
    if tests:
        return pick_task(
            names,
            [f"compile{v}UnitTestSources", "testClasses", f"compile{v}UnitTestKotlin", "compileTestKotlin", "jvmTestClasses"],
            [rf"compile\w+{v}UnitTestSources", r"compileTestKotlin\w*"],
        )
    return pick_task(
        names,
        [f"compile{v}Sources", "classes", f"compile{v}Kotlin", "compileKotlin", "jvmMainClasses", "compileJava"],
        [rf"compile(?!.*Test)\w+{v}Sources", r"compileKotlin(?!.*Test)\w*"],
    )


def test_task(names: dict[str, str] | set[str], variant: str) -> tuple[str | None, list[str]]:
    v = _cap(variant)
    return pick_task(names, [f"test{v}UnitTest", "test", "jvmTest", "allTests"], [rf"test\w+{v}UnitTest"])


# ----------------------------------------------------------------------------- parsing output


@dataclass(frozen=True)
class Diagnostic:
    severity: str  # "error" | "warning"
    path: str
    line: int
    col: int
    message: str
    rule: str = ""

    @property
    def key(self) -> str:
        return path_key(self.path)

    def render(self, root: Path) -> str:
        loc = rel(Path(self.path), root) + (f":{self.line}" if self.line else "") + (f":{self.col}" if self.col else "")
        return f"{loc}: {self.message}" + (f" [{self.rule}]" if self.rule else "")


@dataclass
class Report:
    outcome: str = ""  # the "BUILD SUCCESSFUL in 3s" line
    failed_tasks: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    what_went_wrong: str = ""
    up_to_date: bool = False

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]


# An absolute path as tools print it: /abs/File.kt, C:\abs\File.kt or C:/abs/File.kt.
_P = r"(?:[A-Za-z]:[\\/]|/)[^:\n]+"
# kotlinc (K1 and K2): "e: file:///abs/File.kt:12:5 Unresolved reference 'x'." On Windows the
# URL form is file:///C:/abs/File.kt, so the slash before the drive letter is not part of the path.
_KOTLINC = re.compile(rf"^(e|w): (?:\[ksp\] )?(?:file:///(?=[A-Za-z]:)|file://)?({_P}?):(\d+):(?:(\d+):?)? ?(.*)$")
# javac: "/abs/File.java:12: error: cannot find symbol"
# kotlinc before 1.8: "e: /abs/File.kt: (7, 9): Type mismatch"
_KOTLINC_OLD = re.compile(rf"^(e|w): ({_P}): \((\d+), (\d+)\): (.*)$")
_JAVAC = re.compile(rf"^({_P}\.java):(\d+): (error|warning): (.*)$")
# detekt: "/abs/File.kt:12:5: Message text [RuleName]"
_DETEKT = re.compile(rf"^({_P}):(\d+):(\d+): (.+) \[(\w+)\]$")
# ktlint CLI and Gradle plugins: "/abs/File.kt:3:1: Unused import (standard:no-unused-imports)"
_KTLINT = re.compile(rf"^({_P}):(\d+):(\d+):? (?:Lint error > )?(.+?) \(([\w:.\-]+)\)$")
_KTLINT_BRACKET = re.compile(rf"^({_P}):(\d+):(\d+):? Lint error > \[([\w:.\-]+)\] (.+)$")
# ktlint Gradle plugin console output carries no rule id: "/abs/File.kt:6:19 Whitespace after ',' is missing"
_KTLINT_PLAIN = re.compile(rf"^({_P}\.kts?):(\d+):(\d+):? (.+)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FAILED_TASK = re.compile(r"^> Task (:\S+) FAILED")
_OUTCOME = re.compile(r"^BUILD (SUCCESSFUL|FAILED) in .*$")


def parse_output(output: str, root: Path) -> Report:
    rep = Report()
    seen: set[Diagnostic] = set()
    lines = _ANSI.sub("", output).splitlines()

    def add(d: Diagnostic) -> None:
        d = replace(d, path=os.path.normpath(d.path))
        if d not in seen:
            seen.add(d)
            rep.diagnostics.append(d)

    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if m := _FAILED_TASK.match(line):
            rep.failed_tasks.append(m.group(1))
        elif m := _OUTCOME.match(line):
            rep.outcome = line
        elif (m := _KOTLINC.match(line)) or (m := _KOTLINC_OLD.match(line)):
            add(Diagnostic("error" if m.group(1) == "e" else "warning", m.group(2), int(m.group(3)), int(m.group(4) or 0), m.group(5).strip()))
        elif m := _JAVAC.match(line):
            add(Diagnostic(m.group(3), m.group(1), int(m.group(2)), 0, m.group(4).strip()))
        elif m := _DETEKT.match(line):
            add(Diagnostic("error", m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).strip(), m.group(5)))
        elif m := _KTLINT_BRACKET.match(line):
            add(Diagnostic("error", m.group(1), int(m.group(2)), int(m.group(3)), m.group(5).strip(), m.group(4)))
        elif m := _KTLINT.match(line):
            add(Diagnostic("error", m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).strip(), m.group(5)))
        elif m := _KTLINT_PLAIN.match(line):
            add(Diagnostic("error", m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).strip(), "ktlint"))
        elif line.startswith("* What went wrong:"):
            block = []
            for nxt in lines[i + 1 :]:
                if nxt.startswith("* Try:") or nxt.startswith("* Exception is:") or nxt.startswith("BUILD "):
                    break
                block.append(nxt.rstrip())
            text = "\n".join(block).strip()
            if text and text not in rep.what_went_wrong:
                rep.what_went_wrong = (rep.what_went_wrong + "\n\n" + text).strip()
    return rep


def format_report(
    run: GradleRun,
    rep: Report,
    *,
    show_warnings: bool = False,
    only_files: set[str] | None = None,
    max_items: int = 60,
    extra: str = "",
    tail: bool = True,
) -> str:
    """Human/model-readable summary of a Gradle run: outcome first, then what to fix."""
    root = run.project.root
    errors, warnings = rep.errors, rep.warnings
    hidden = 0
    if only_files is not None:  # a set of path_key() values
        # Keep sibling errors: the selected file is a focus, not a build boundary.
        errors = sorted(errors, key=lambda d: d.key not in only_files)
        warnings = [d for d in warnings if d.key in only_files]

    if run.timed_out:
        head = f"TIMED OUT after {run.seconds:.0f}s (Gradle was killed)"
    else:
        head = rep.outcome or ("BUILD SUCCESSFUL" if run.ok else f"BUILD FAILED (exit code {run.code})")
    out = [f"{head}   $ gradle {' '.join(run.args)}   [{root}]"]
    if rep.failed_tasks:
        out.append("Failed tasks: " + ", ".join(rep.failed_tasks))

    def section(title: str, items: list[Diagnostic]) -> None:
        out.append(f"\n{title} ({len(items)}):")
        out.extend("  " + d.render(root) for d in items[:max_items])
        if len(items) > max_items:
            out.append(f"  [... {len(items) - max_items} more ...]")

    if errors:
        section("Errors", errors)
    if hidden:
        out.append(f"\n{hidden} error(s) in other files are not listed; they may be what blocks the build.")
    if warnings:
        if show_warnings:
            section("Warnings", warnings)
        else:
            out.append(f"\n{len(warnings)} warning(s) not shown (pass show_warnings=true).")
    if extra:
        out.append("\n" + extra.strip())
    # With located errors in hand, Gradle's wrapper message ("Compilation error. See log") adds nothing.
    if rep.what_went_wrong and not run.ok and not errors:
        out.append("\nWhat went wrong:\n" + clip("\n".join("  " + s for s in rep.what_went_wrong.splitlines()), 6000, keep="head"))
    if tail and not run.ok and not rep.diagnostics and not rep.what_went_wrong:
        out.append("\nLast output:\n" + clip(run.output.strip(), 6000, keep="tail"))
    if run.log:
        out.append(f"\nFull log: {run.log}")
    return "\n".join(out)


# ----------------------------------------------------------------------------- test results


@dataclass
class TestSummary:
    total: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    suites: int = 0
    complete: bool = True
    problems: list[str] = field(default_factory=list)
    reused: int = 0

    @property
    def executed(self) -> int:
        return self.total - self.skipped

    def render(self, max_failures: int = 25) -> str:
        if not self.suites:
            return f"No current-run test results: 0 executed; {self.reused} reused. " + "; ".join(self.problems)
        passed = self.total - self.failed - self.skipped
        out = [f"Tests: {self.executed} executed, {passed} passed, {self.failed} failed, {self.skipped} skipped ({self.suites} classes); {self.reused} reused"]
        if not self.complete:
            out.append("Incomplete reports: " + "; ".join(self.problems))
        for f in self.failures[:max_failures]:
            out.append("\n" + f)
        if len(self.failures) > max_failures:
            out.append(f"\n[... {len(self.failures) - max_failures} more failures ...]")
        return "\n".join(out)


_NOISE_FRAMES = (
    "at org.junit.", "at junit.", "at org.gradle.", "at worker.org.gradle.", "at java.", "at jdk.", "at sun.",
    "at kotlin.", "at kotlinx.coroutines.", "at org.robolectric.internal.", "at org.opentest4j.", "at io.mockk.impl.",
)


def _trim_stack(text: str, keep: int = 10) -> str:
    # Gradle prefixes frames with the class loader name ("at app//pkg.Class"); drop it.
    lines = [re.sub(r"^(\s*at )[\w.]*//", r"\1", ln.rstrip()) for ln in text.strip().splitlines()]
    # Drop framework frames; they are never where the fix is.
    kept = [ln for ln in lines if not re.sub(r"^at [\w.]+/", "at ", ln.strip()).startswith(_NOISE_FRAMES)]
    if len(kept) > keep:
        kept = kept[:keep] + [f"    ... {len(kept) - keep} more lines"]
    return "\n".join("    " + ln for ln in kept)


def collect_tests(dirs: list[Path], since: float) -> TestSummary:
    """Summarise the JUnit XML reports under the given result directories that were written
    since `since` (0 for all)."""
    files = _test_files(dirs)
    return _parse_test_files({p: data for p, (stamp, data) in files.items() if stamp / 1_000_000_000 >= since})


def _test_files(dirs: list[Path]) -> dict[Path, tuple[int, bytes]]:
    scope, files, size = active(), {}, 0
    for results in dirs:
        scope.check(results, protected=False)
        if not results.is_dir():
            continue
        for xml in results.rglob("TEST-*.xml"):
            if len(files) >= 1000 or size >= 20_000_000:
                raise Blocked("report_limit", "Test report capture exceeded its bounded file or byte limit.", "environment")
            try:
                data = scope.read_bytes(xml, 8_000_000)
                files[xml] = (xml.stat().st_mtime_ns, data)
                size += len(data)
            except OSError:
                files[xml] = (0, b"")
    return files


def _parse_test_files(files: dict[Path, bytes]) -> TestSummary:
    summary = TestSummary()
    if not files:
        summary.complete = False
        summary.problems.append("required reports missing or stale")
    for xml, data in files.items():
        try:
            suite = ET.fromstring(data)
            if suite.tag not in ("testsuite", "testsuites"):
                raise ValueError("Invalid report root.")
            cases = list(suite.iter("testcase"))
            skipped = sum(c.find("skipped") is not None for c in cases)
            summary.suites += len(list(suite.iter("testsuite")))
            summary.total += len(cases)
            summary.skipped += skipped
            suites = [s for s in suite.iter("testsuite") if not s.findall("testsuite")]
            if (sum(int(s.get("tests", len(s.findall("testcase")))) for s in suites) != len(cases) or
                    sum(int(s.get("skipped", sum(c.find("skipped") is not None for c in s.findall("testcase"))))
                        for s in suites) != skipped):
                summary.complete = False
                summary.problems.append(f"incomplete report counts: {xml.name}")
            for case in cases:
                bad = case.find("failure")
                if bad is None:
                    bad = case.find("error")
                if bad is None:
                    continue
                summary.failed += 1
                message = (bad.get("message") or "").strip().splitlines()
                title = f"FAILED {case.get('classname')}.{case.get('name')}"
                if message:
                    title += f": {clip(message[0], 400, keep='head')}"
                summary.failures.append((title + "\n" + _trim_stack(bad.text or "")).rstrip())
        except (ET.ParseError, ValueError, TypeError):
            summary.complete = False
            summary.problems.append(f"malformed report: {xml.name}")
    return summary
