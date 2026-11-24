"""Thin, explicit .NET and Python validation adapters. No environment installation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import guard
from .common import (
    Blocked,
    ProcResult,
    ToolError,
    active,
    run_proc,
    sanitise,
    sanitise_value,
)
from .execution import WorkspaceLease, save_artifact, source_identity


def parse_junit(data: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(data)
        if root.tag not in ("testsuite", "testsuites"):
            raise ValueError("Unexpected JUnit root.")
        cases = list(root.iter("testcase"))
        skipped = sum(c.find("skipped") is not None for c in cases)
        failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
        suites = list(root.iter("testsuite"))
        declared = sum(int(s.get("tests", len(s.findall("testcase")))) for s in suites if not s.findall("testsuite"))
        complete = declared == len(cases)
        if not complete and not failed:
            raise ValueError("The declared test count does not match the report's test cases.")
        failures = []
        for case in cases:
            bad = case.find("failure")
            if bad is None:
                bad = case.find("error")
            if bad is not None:
                failures.append(sanitise(f"{case.get('classname', '')}.{case.get('name', '')}: " +
                                         " ".join(bad.itertext())))
        return {"total": len(cases), "executed": len(cases) - skipped,
                "skipped": skipped, "failed": failed, "failures": failures, "complete": complete}
    except (ET.ParseError, ValueError, TypeError) as e:
        raise ToolError(f"Malformed or incomplete JUnit report: {e}") from None


def parse_trx(data: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(data)
        if root.tag.rsplit("}", 1)[-1] != "TestRun":
            raise ValueError("Unexpected TRX root.")
        results = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "UnitTestResult"]
        outcomes = [e.get("outcome") for e in results]
        if any(o not in ("Passed", "Failed", "NotExecuted", "Inconclusive", "Timeout", "Aborted") for o in outcomes):
            raise ValueError("Unknown test outcome.")
        skipped = outcomes.count("NotExecuted")
        failed = sum(o in ("Failed", "Timeout", "Aborted") for o in outcomes)
        complete = "Inconclusive" not in outcomes
        counters = next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "Counters"), None)
        if counters is not None:
            for name, actual in (("total", len(results)), ("executed", len(results) - skipped), ("failed", failed)):
                declared = counters.get(name)
                if declared is not None and int(declared) != actual:
                    if not failed:
                        raise ValueError(f"The declared {name} count does not match actual test results.")
                    complete = False
        return {"total": len(results), "executed": len(results) - skipped, "skipped": skipped,
                "failed": failed, "complete": complete,
                "failures": [sanitise(e.get("testName", "") + ": " + " ".join(e.itertext()))
                             for e in results if e.get("outcome") in ("Failed", "Timeout", "Aborted")]}
    except (ET.ParseError, ValueError, TypeError) as e:
        raise ToolError(f"Malformed or incomplete TRX report: {e}") from None


def classify_failure(text: str) -> tuple[str, str]:
    lower = text.lower()
    if re.search(r"msb302[17]", lower) and ("locked by" in lower or "used by another process" in lower):
        return "environment", "output_locked"
    if any(s in lower for s in ("nu1301", "unable to load the service index", "could not resolve",
                                "connection timed out", "no module named", "unable to find package")):
        return "environment", "dependency_or_environment_failure"
    if re.search(r"(?:error (?:cs|bc|fs)\d+|\b[ew]\d{3}\b|failed\s+\S+|assertionerror)", lower):
        return "source", "source_failure"
    return "unknown", "execution_failed"


def executable(value: str) -> str:
    if not value or not (p := shutil.which(value)):
        raise Blocked("execution_unavailable", f"Explicit executable not found: {value}", "environment")
    return p


def sanitise_report(scope: guard.Scope, path: Path) -> bytes:
    """Parse run-owned XML before sanitising every value. Never retain raw failure payloads."""
    data = scope.read_bytes(path, 8_000_000)
    revision = hashlib.sha256(data).hexdigest()
    try:
        root = ET.fromstring(data)
        for node in root.iter():
            node.attrib.update(sanitise_value(node.attrib))
            if node.text:
                node.text = sanitise(node.text)
            if node.tail:
                node.tail = sanitise(node.tail)
        safe = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    except ET.ParseError:
        safe = b"<withheld>Malformed report content withheld.</withheld>"
    scope.write_bytes(path, safe, expected_revision=revision)
    return data


def _finish(r: ProcResult, started: float, argv: list[str], root: Path, before: dict[str, Any],
            counts: dict[str, Any] | None, required_report: bool, artifact: dict[str, Any],
            diagnostics: list[dict[str, Any]] | None = None) -> guard.TextResult:
    complete = r.output_complete and (not required_report or counts is not None and counts.get("complete", False))
    category, reason = classify_failure(r.output)
    if r.timed_out:
        outcome, category, reason = "timed_out", "unknown", "execution_timeout"
    elif r.code != 0 or counts and counts.get("failed"):
        outcome = "failed"
        if counts and counts.get("failed"):
            category, reason = "source", "tests_failed"
        elif diagnostics:
            category, reason = "source", "diagnostic_findings"
    elif required_report and counts is None:
        outcome, category, reason = "not_run", "unknown", "validation_unverified"
    elif required_report and counts is not None and counts["executed"] == 0:
        outcome, category, reason = "not_run", "input", "no_tests_executed"
    elif not complete:
        outcome, category, reason = "not_run", "unknown", "validation_unverified"
    else:
        outcome, category, reason = "passed", None, None
    data = {"executable": argv[0], "arguments": sanitise(" ".join(argv[1:])),
            "exit_code": r.code, "duration_seconds": time.monotonic() - started,
            "source_start": before, "source_end": source_identity(root), "counts": counts,
            "artifacts": [artifact], "diagnostics": diagnostics or []}
    return guard.result(f"[outcome={outcome}]\n{sanitise(r.output)[:30_000]}\nCounts: {counts}",
                        outcome=outcome, reason=reason, category=category, complete=complete,
                        incomplete_reason=None if complete else "incomplete_evidence", data=data)


def python_inputs(project: str, executable_path: str, check: str, files: list[str],
                  filter: str | None = None) -> tuple[Path, str, list[str]]:
    """Validate the same inputs before queuing and again before execution."""
    scope = active()
    root = scope.dir(project)
    if check not in ("pytest", "ruff", "basedpyright") or not files or len(files) > 32:
        raise ToolError("Select pytest, ruff or basedpyright and 1–32 explicit target files.")
    targets = []
    for target in files:
        path, separator, test = target.partition("::")
        if separator and check != "pytest":
            raise ToolError("Only pytest supports test node IDs.")
        p = scope.file(root / path)
        targets.append(str(p) + (separator + test if separator else ""))
    if filter and check != "pytest":
        raise ToolError("Lint/type checks use explicit file targets, not a pytest expression.")
    return root, executable(executable_path), targets


async def validate_python(project: str, executable_path: str, check: str, files: list[str],
                          filter: str | None = None, timeout_sec: int = 600):
    root, exe, targets = python_inputs(project, executable_path, check, files, filter)
    scope = active()
    report = scope.check(root / ".ide-mcp" / "reports" / f"{uuid.uuid4().hex}.xml")
    argv = [exe, "-m", check]
    if check == "pytest":
        scope.make_dirs(report.parent)
        argv += [*targets, f"--junitxml={report}"]
        if filter:
            argv += ["-k", filter]
    elif check == "ruff":
        argv += ["check", "--output-format=json", *targets]
    else:
        argv += ["--outputjson", *targets]
    async with WorkspaceLease(root):
        before, started = source_identity(root), time.monotonic()
        try:
            r = await run_proc(argv, cwd=root, timeout=timeout_sec)
        finally:
            report_data = sanitise_report(scope, report) if check == "pytest" and report.exists() else None
        counts, diagnostics = None, []
        if check == "pytest":
            try:
                counts = parse_junit(report_data) if report_data is not None else None
            except (OSError, ToolError):
                pass
        elif r.code in (0, 1):
            try:
                output = json.loads(r.output)
                diagnostics = output if check == "ruff" else output["generalDiagnostics"]
                if not isinstance(diagnostics, list) or any(not isinstance(d, dict) for d in diagnostics):
                    diagnostics = []
                    raise ValueError("Unsupported diagnostic collection.")
                if len(diagnostics) > 200:
                    r.output_complete = False
            except (ValueError, KeyError, TypeError):
                r.output_complete = False
        artifact = save_artifact(r.output)
        return _finish(r, started, argv, root, before, counts, check == "pytest", artifact, diagnostics[:200])


def dotnet_inputs(project: str, executable_path: str, check: str, filter: str | None = None,
                  output_root: str | None = None) -> tuple[Path, str, guard.Scope, str | None]:
    """Preflight static project references without creating outputs or launching work."""
    scope = active()
    p = scope.file(project)
    if p.suffix.lower() not in (".csproj", ".fsproj", ".vbproj") or check not in ("build", "test"):
        raise ToolError("Select an explicit project file and build or test.")
    if filter and check != "test":
        raise ToolError("A test filter cannot be used for a build.")
    # Verify direct project references rather than widening to their common parent.
    pending, visited = [p], set()
    framework = None
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        if len(visited) >= 100:
            raise Blocked("project_reference_limit", "Project reference validation exceeded 100 projects.")
        visited.add(current)
        try:
            xml = ET.fromstring(scope.read_bytes(current, 2_000_000))
        except ET.ParseError:
            raise ToolError(f"Malformed project: {current}") from None
        if current == p and check == "test":
            candidates = [n for n in xml.iter() if n.tag.rsplit("}", 1)[-1] == "TargetFramework"]
            conditional = any(n.get("Condition") and any(
                c.tag.rsplit("}", 1)[-1] in ("TargetFramework", "TargetFrameworks") for c in n.iter()
            ) for n in xml.iter())
            if (len(candidates) != 1 or conditional
                    or "targetframework" in xml.get("TreatAsLocalProperty", "").lower()
                    or not re.fullmatch(r"net[a-zA-Z0-9_.-]+", (candidates[0].text or "").strip())):
                raise Blocked("target_framework_unverified",
                              "Tests require one explicit literal TargetFramework. Imported or conditional framework selection cannot establish complete TRX ownership.", "input")
            framework = (candidates[0].text or "").strip()
        for node in xml.iter():
            if (current == p and check == "test" and node.tag.rsplit("}", 1)[-1] == "TargetFrameworks"
                    and node.text and (";" in node.text or "$(" in node.text)):
                raise Blocked("multi_target_reports_unverified",
                              "Select a single-target test project. This adapter cannot verify combined multi-target TRX ownership.", "input")
            if node.tag.rsplit("}", 1)[-1] == "ProjectReference":
                target = node.get("Include", "")
                if not target or "$(" in target or "*" in target:
                    raise Blocked("project_reference_unverified", "A dynamic project reference cannot be safely resolved.")
                pending.append(scope.file(current.parent / target.replace("\\", "/")))
    return p, executable(executable_path), guard.Scope.of(output_root) if output_root else scope, framework


async def validate_dotnet(project: str, executable_path: str, check: str, filter: str | None = None,
                          timeout_sec: int = 600, isolate_outputs: bool = False, output_root: str | None = None):
    p, exe, output_scope, framework = dotnet_inputs(project, executable_path, check, filter, output_root)
    root = p.parent
    output = output_scope.check(output_scope.root / ".ide-mcp" / "validation" / uuid.uuid4().hex, protected=False)
    output_scope.make_dirs(output)
    argv = [exe, check, str(p)]
    report = output / "tests.trx"
    if check == "test":
        assert framework is not None
        argv += ["--framework", framework, "--logger", "trx;LogFileName=tests.trx", "--results-directory", str(output)]
        if filter:
            argv += ["--filter", filter]
    async with WorkspaceLease(root):
        if isolate_outputs:
            version = await run_proc([exe, "--version"], cwd=root, timeout=30)
            match = re.match(r"(\d+)\.", version.output.strip())
            if not match or int(match.group(1)) < 8:
                raise Blocked("output_isolation_unsupported", "Native --artifacts-path isolation requires verified .NET SDK 8 or later.", "environment")
            argv += ["--artifacts-path", str(output / "artifacts")]
        before, started = source_identity(root), time.monotonic()
        try:
            r = await run_proc(argv, cwd=root, timeout=timeout_sec)
        finally:
            report_data = sanitise_report(output_scope, report) if check == "test" and report.exists() else None
        counts = None
        if check == "test":
            try:
                counts = parse_trx(report_data) if report_data is not None else None
            except (OSError, ToolError):
                pass
        artifact = save_artifact(r.output, output_scope=output_scope)
        result = _finish(r, started, argv, root, before, counts, check == "test", artifact)
        result.fields["data"].update(output_isolation="native_artifacts_path" if isolate_outputs else "none",
                                     running_application="not_inspected_or_changed")
        return result

