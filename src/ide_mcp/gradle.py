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
from typing import Callable

from .common import SKIP_DIRS, WINDOWS, ToolError, android_sdk, clip, path_key, rel, resolve, run_proc

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


def find_project(path: str | None) -> GradleProject:
    """Find the Gradle build that owns `path` (a file or directory anywhere inside it).

    Walks up to the nearest settings file that has a wrapper next to it, so a path inside an
    included build without its own wrapper lands on the outer build. If nothing is found above,
    looks a few levels below, which covers Flutter/React Native layouts where the Gradle build
    lives in <repo>/android or <repo>/app/android.
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
    for dirpath, dirnames, _files in os.walk(start):
        depth = len(Path(dirpath).relative_to(start).parts)
        dirnames[:] = [] if depth >= 3 else [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
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

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


_locks: dict[Path, asyncio.Lock] = {}


def _gradle_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("TERM", "dumb")
    if "ANDROID_HOME" not in env and "ANDROID_SDK_ROOT" not in env and (sdk := android_sdk()):
        env["ANDROID_HOME"] = sdk
    return env


def _save_log(project: GradleProject, args: list[str], output: str) -> Path | None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{project.root.name}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
        path = LOG_DIR / name
        path.write_text(f"$ gradle {' '.join(args)}   (in {project.root})\n\n{output}", encoding="utf-8")
        for old in sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)[:-KEEP_LOGS]:
            old.unlink(missing_ok=True)
        return path
    except OSError:
        return None


async def run_gradle(
    project: GradleProject,
    args: list[str],
    *,
    timeout: float = 900.0,
    on_line: Callable[[str], None] | None = None,
    save_log: bool = True,
) -> GradleRun:
    """Run Gradle in the project root. Invocations for the same build are serialised, since a
    second client would only sit waiting for the first one's lock anyway."""
    lock = _locks.setdefault(project.root, asyncio.Lock())
    async with lock:
        started = time.time()
        r = await run_proc(
            [*project.launcher, "--console=plain", *args],
            cwd=project.root,
            timeout=timeout,
            env=_gradle_env(),
            on_line=on_line,
        )
        seconds = time.time() - started
    log = _save_log(project, args, r.output) if save_log else None
    return GradleRun(project, args, r.code, r.output, seconds, log, r.timed_out, started)


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
        raise ToolError("Could not list Gradle tasks; the build does not configure.\n" + format_report(run, parse_output(run.output, project.root)))
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
        hidden = sum(1 for d in errors if d.key not in only_files)
        errors = [d for d in errors if d.key in only_files]
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

    def render(self, max_failures: int = 25) -> str:
        if not self.suites:
            return "No test results were written (no tests matched, or the tasks were up to date and skipped)."
        passed = self.total - self.failed - self.skipped
        out = [f"Tests: {self.total} run, {passed} passed, {self.failed} failed, {self.skipped} skipped ({self.suites} classes)"]
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
    summary = TestSummary()
    for results in dirs:
        if not results.is_dir():
            continue
        for xml in sorted(results.rglob("TEST-*.xml")):
            try:
                if xml.stat().st_mtime < since - 1:
                    continue
                suite = ET.parse(xml).getroot()
            except (OSError, ET.ParseError):
                continue
            summary.suites += 1
            summary.total += int(suite.get("tests", 0))
            summary.skipped += int(suite.get("skipped", 0))
            for case in suite.iter("testcase"):
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
    return summary
