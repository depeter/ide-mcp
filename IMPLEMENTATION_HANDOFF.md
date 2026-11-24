# MCP implementation handoff

## Scope and delivery

Implemented on 20 November 2026 in `C:\dev\mcp\ide-mcp`.

Base SHA: `9b5ddc004f1dc8303ae0195179d04f83967ec771`.
The implementation is uncommitted. There is no new implementing commit.
The pre-existing `uv.lock` changes were not edited.
No dependencies were installed. No client configuration, production worktree or running
application was changed. No server was deployed or restarted.

Request contract: **3.0**. Package version: **0.2.0**.
Readiness and operation output schemas: **1.0**.
Installed validation environment: Python **3.12.16**, MCP SDK **1.30.0**, pytest **9.2.0**.

## Implemented changes

| Area | Delivery |
| --- | --- |
| Shared contracts | Structured results alongside legacy text; SDK error flags; per-request scope; accurate incomplete and partial outcomes |
| Safe access | Fresh worktree-boundary checks; guarded ranged reads; staged writes; content revisions checked before replacement |
| Execution | One shared runner; sanitisation before clipping/progress; verified cancellation and timeout; bounded scoped jobs |
| Coordination | Native cross-process workspace lease held through report capture |
| Artifacts | Unique sanitised logs; final-byte hashes; guarded retrieval; explicit artifact-write failures |
| Files | Bounded batch reads and traversal; exact revision-aware edit preview/apply; byte representation preservation |
| Windows validation | Explicit Python/.NET adapters; current-run report parsing; executed/skipped/reused evidence kept separate |
| Kotlin | Mutation coverage gate; consistent provenance; stale-resistant query pagination; guarded complete rename previews |
| Gradle | Current-run report freshness checks; actual-case counts; known failures retained with incomplete evidence; sibling diagnostics |

New runtime modules are `src/ide_mcp/execution.py` and `src/ide_mcp/validation.py`.
Existing changes are in `common.py`, `guard.py`, `files_server.py`, `kotlin_server.py`,
`gradle.py` and `lsp.py`. `.gitignore` excludes generated `.ide-mcp` output.
Existing tests were extended. New tests are in `test_improvements.py`,
`test_execution_improvements.py` and `test_semantic_improvements.py`.
Final tool names, limits and request examples are documented in `README.md`.

## Future validation runs

Follow the temporary-directory rules in `C:\dev\scripts\ai\MCP_WORKFLOW.md`.
Use a fresh directory under `%TEMP%` for each fixture run:

```powershell
$ValidationTemp = Join-Path $env:TEMP ('ide-mcp-validation-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest -q -rs -p no:cacheprovider --basetemp "$ValidationTemp" -o faulthandler_timeout=30
```

## Validation evidence

Final complete fixture run on 20 November 2026 (historical command):

```powershell
.\.venv\Scripts\python.exe -m pytest -q -rs -p no:cacheprovider --basetemp 'C:\dev\mcp\validation-delivery-20261120-d' -o faulthandler_timeout=30
```

**181 passed, 3 skipped, 0 failed**, in 165.13 seconds.
This run used approved Windows process permissions for native cancellation checks.
The three skips are the real symbolic-link tests at `tests/test_scope.py:290,329,354`.
Symbolic links were unavailable in this environment. These skips are not passes.

The suite includes real local pytest subprocesses for successful and failing Python
validation. Synchronous and job execution both reported one executed test in the success
fixture. The .NET/TRX and semantic-engine fixtures are not real .NET or engine acceptance.

Red-before-green regressions reproduced and then fixed:

- Cancellation while the process handle is still being acquired.
- Oversized credential lines exposing an unlabelled tail.
- Native tree-stop failure incorrectly accepting parent exit as proof.
- Invalid-handle lease errors retrying indefinitely.
- Invalid validation inputs being queued.
- Truncated lint diagnostics hiding incompleteness and source failure.
- Incomplete JUnit/TRX reports losing known failing cases.
- Backend moves bypassing the declaration-file convention.
- Source metadata accepting another selected scope.
- Imported .NET framework selection being treated as verified report coverage.

Earlier work also reproduced stale edit revisions, partial imports, malformed edit ranges,
raw sensitive report retention, split credentials and stale Gradle report counts.

SDK-level discovery verified **22 Files tools** and **24 Kotlin tools**, all with output
schemas. Registration coverage was checked against the prerequisite registry. Context
parameters remain absent from public input schemas. A read-only SDK probe confirmed the
selected server worktree. Cross-worktree rejection is covered by disposable Git fixtures.

Critical Ruff checks (`ruff check src tests --select E9,F --no-cache`) pass.
Full inherited Ruff and Basedpyright checks still fail on baseline debt:

| Check | Base SHA | Implementation |
| --- | ---: | ---: |
| Ruff findings | 40 | 36 |
| Basedpyright errors | 143 | 66 |

No new Ruff finding or Basedpyright error remains after the focused test-style cleanup.
Comparison used an isolated baseline copy, the same installed tools and interpreter, and
matched diagnostic messages by file and rule. It is not a claim that warnings are clean.
`git diff --check` passes.

## Limits and outstanding acceptance

- Readiness observes prerequisites. It does not prove execution or indexing success.
- Jobs are in memory. A restart loses their identity. Unknown jobs are never relaunched.
  Cooperative cancellation waits for owned cleanup. A hard server exit cannot guarantee it.
- Windows tree termination must be confirmed by the native tree-stop operation. Parent exit
  alone cannot establish that descendants stopped.
- Sanitisation covers the tested labelled credential/private-key formats and recognised
  payment lines. It is not certification for arbitrary payment traces.
- Native alternate .NET outputs are not a coherent immutable source snapshot. SHA and dirty
  state are recorded. Dirty-file byte stability remains explicitly unverified.
- .NET tests require one explicit literal target framework, pinned with `--framework`.
  Imported/conditional selection and unsupported multi-target coverage stay blocked.
  There is no snapshot fallback or application restart.
- Partial semantic imports block rename before an applicable preview or write. Android rename
  uses the current imported variant, not every variant. Explicit selection uses the LSP's native
  selector and an import-only enabled-variant check. Each import must confirm that selection.
- Query response bounds do not bound backend collection work. Dispatch labels are
  conservative declaration-text estimates, not runtime or exact-dispatch proof.
- Multi-file edits are not atomic. Partial failures report completed and pending work.
  There is no unconditional rollback or filesystem compare-and-swap guarantee.
- Exact previews are bounded. Oversized rename/edit plans stay blocked rather than exposing
  clipped applicable previews.

Still required before claiming live acceptance:

1. Compare navigation with the real Kotlin engine in an explicitly selected disposable
   project. Compile a successful fixture rename and its consumers.
2. Run a real focused Android test task with confirmed variant and filters.
3. Verify .NET native output isolation while the intended WPF application remains running.
   Keep validated source identity separate from the running application's identity.
4. Exercise client reconnect, schema consumption and job polling after approved deployment.
5. Run the skipped real-link checks in an environment that supports them.
6. Collect repeated bounded-traversal measurements before making performance-gain claims.

## User-performed deployment

1. Review the uncommitted diff. Approve deployment separately.
2. End or inspect active jobs before an approved server restart.
3. Reconnect both integrations and list their tools. The currently connected tools may still
   have the earlier schemas.
4. Migrate existing-file writes to `expected_revision`. Retain `preview_id` and semantic
   `query_id`. Keep job IDs and artifact output scopes bound to the selected worktree.
5. Treat partial item errors, incomplete evidence and non-final jobs as such.
6. Perform the live checks above in explicitly authorised projects.

`C:\dev\scripts\test.ps1` was not run. It remains user-run where applicable to the wider
application or payment validation workflow. No commit or push was performed.

