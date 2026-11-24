# ide-mcp

Two MCP servers that give coding agents (Codex, Claude Code, ...) what the JetBrains MCP server
offers, without an IDE: nothing has to be opened in Android Studio or IntelliJ first. Every
tool takes the directory the request is for (`expected_root`) and a path, and refuses to use
anything that resolves outside that directory or belongs to a different Git worktree.

| Server | Binary | What it does |
| --- | --- | --- |
| `files` | `files-mcp` | Read, create, edit, find and search files; directory trees; git status; shell commands |
| `kotlin` | `kotlin-mcp` | Gradle builds, compile checks, unit tests, dependencies, detekt, ktlint, and code-aware find usages, rename and inspections |

Both run locally as stdio processes; the agent starts them on demand. They are independent, so
register only one if that is all you need.

## Install

Requirements: [`uv`](https://docs.astral.sh/uv/) or Python 3.10+, `git`, and for the `kotlin`
server a JDK (projects bring their own Gradle through `gradlew`).

Linux and macOS:

```bash
scripts/install.sh            # build .venv and register both servers with Codex
scripts/install.sh --claude   # ...and also with Claude Code
```

Windows (PowerShell; do not use `install.sh` from Git Bash):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1           # Codex
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Claude   # ...and Claude Code
```

On Windows the JDK is taken from `JAVA_HOME`, then `java` on `PATH`, then the one bundled with
Android Studio. `run_command` runs PowerShell there instead of bash, and Gradle is started
through `gradlew.bat`.

The script pins `JAVA_HOME` and `ANDROID_HOME` for the `kotlin` server, because agents launch
MCP servers with a trimmed environment, and raises Codex's tool timeout for it to 30 minutes
(the default of 60 seconds is shorter than most builds). Re-running is safe.

| Script | Purpose |
| --- | --- |
| `scripts/check.sh`, `scripts\check.ps1` | Show what each server sees: tools, JDK, Android SDK, lint CLIs |
| `scripts/uninstall.sh [--purge]`, `scripts\uninstall.ps1 [-Purge]` | Unregister from Codex/Claude; also delete `.venv` |

### Manual registration

```toml
# ~/.codex/config.toml
[mcp_servers.files]
command = "/absolute/path/to/ide-mcp/.venv/bin/files-mcp"

[mcp_servers.kotlin]
command = "/absolute/path/to/ide-mcp/.venv/bin/kotlin-mcp"   # Windows: '...\.venv\Scripts\kotlin-mcp.exe'
tool_timeout_sec = 1800
env = { JAVA_HOME = "/usr/lib/jvm/java-17-openjdk-amd64", ANDROID_HOME = "/home/you/Android/Sdk" }
```

The package is installed in editable mode, so edits under `src/` take effect the next time the
agent starts a server.

## How paths work

- **Every tool** takes `expected_root` (see [Request scope](#request-scope-outcomes-and-readiness)).
  Relative paths and omitted directories resolve against it, never against the directory the
  server was started in.
- **File tools** take absolute paths (or `~/...`). Search and listing tools take a `root`
  directory. Inside a git repository they honour `.gitignore`; elsewhere they skip build and
  cache directories. `include_ignored=true` looks everywhere.
- **Gradle tools** take `project_dir`: any path inside the project, a file included. The server
  walks up to the Gradle root (the nearest `settings.gradle[.kts]` with a `gradlew`), and if
  there is none above, looks below, which covers `<repo>/android` and `<repo>/app/android`
  layouts.
- **Modules** are Gradle paths (`:app`, `:features:login`). Most tools also accept `file`
  instead and use the module that owns it.

## Request scope, outcomes and readiness

Request contract **3.0** (package 0.2.0). Every call needs `expected_root`; there is no
unscoped route. Existing-file content writes now require a SHA-256 `expected_revision`.
Semantic query continuations require the returned `query_id` as well as `offset`.

### `expected_root`

The absolute path of the directory the caller selected for the request:

| Request | What to pass |
| --- | --- |
| Anything in a Git checkout | The worktree root, or a directory inside it. For Gradle and code-aware tools it must be at or above the Gradle root (`<repo>` or `<repo>\android`). |
| A project that is not in Git | The project root itself. |
| Standalone files (notes, tickets, saved logs) | The folder that holds them. No repository is needed. |

What the servers then enforce, inside each operation and at the point of access:

- Every path is resolved (links, junctions, `..`, 8.3 names, case, `\\?\` prefixes) and compared
  with the scope component by component. `C:\dev\app-other` is not inside `C:\dev\app`.
- The target must belong to the same Git worktree as the scope. A worktree is identified by
  the directory that holds its `.git` entry, not by repository name, branch, commit or the
  shared `.git` directory, so two worktrees at one commit never stand in for each other, and a
  shared parent such as `C:\dev` selects neither.
- The Gradle project that discovery actually returns, the directory the language-server
  session was actually started in, and the working directory of every process are checked
  again immediately before use. Gradle options that redirect a build (`-p`, `--project-dir`,
  `-c`, `--settings-file`, `-b`, `--build-file`, `--include-build`, `-I`, `--init-script`) are
  accepted only when they point inside the scope. `run_command` rejects a command line that
  names a path in another worktree.
- A file is opened, and the open handle is compared with the validated path before a byte is
  read; a new file is written through a temporary file whose location is confirmed first.
- `*.properties` files are never read, searched, parsed, edited, moved or returned, under any
  spelling (`LOCAL.PROPERTIES`, `local.properties.`, `local.properties::$DATA`, a link, an 8.3
  name). `project_info` therefore no longer reports the Gradle version.

`expected_root` restricts a request. It grants nothing: `IDE_MCP_ALLOWED_ROOTS` and the
client's own approvals still apply. The server cannot know whether the scope a caller passes is
the one the user meant; binding it to the user's task is the client's job. Advertised MCP roots
and tool annotations are not used as authorisation.

Limits worth knowing: a hard link to a protected file under another name cannot be told apart
from an ordinary file; `run_command` runs a real shell, so beyond its working directory and the
paths spelled out in the command it is not confined; on platforms without handle-relative file
APIs a swapped directory is detected after the open or the temporary-file creation rather than
prevented, so an empty temporary file can exist for an instant in the wrong place before it is
removed, though the payload is never written there.

### Outcomes and errors

Non-readiness tools return legacy text and `structuredContent` with the same outcome.
The `OperationResult` output schema is version `1.0`. It includes `operation`, `result`,
`resolved_scope`, `worktree_root`, `outcome`, `reason`, `category`, `complete`,
`incomplete_reason`, `continuation` and operation-specific `data`. Missing provenance
remains unknown. Legacy handlers without verified completeness are labelled incomplete.

Failures use the SDK's MCP tool-error flag (`isError: true`). Guard failures retain a
parseable header:

```
[outcome=blocked reason=target_mismatch category=policy] C:\dev\app-other\README.md resolves to ...
```

| Outcome | Meaning |
| --- | --- |
| `passed` | First line of a successful build, test, lint, command or applied rename. |
| `failed` | It ran and failed, or the request was wrong. `category` says which: `source` (compiler errors, failing tests, lint findings), `input`, `environment`, `policy`, or `unknown` when a build failed without located errors. |
| `blocked` | A prerequisite prevented the requested access, mutation or launch. Safe preflight reads may already have occurred. |
| `timed_out` | Timeout elapsed and the owned process was confirmed stopped. |
| `not_run` | Nothing was executed: a rename preview (`reason=dry_run`, a normal result), or a test run in which no test executed (`reason=no_tests_executed`, an error). |
| `cancelled` | A job was cancelled before launch, or its owned execution was confirmed stopped. Unconfirmed termination has no final outcome. |

`read_files` preserves successful independent items when another item fails. The overall
result is `failed`, reason `partial_items`, with `isError: true` and per-item outcomes in
`data.items`. A bounded successful query can be incomplete and have a continuation.
Do not interpret `complete=false`, zero executed tests or a queued job as a pass.

Reason codes: `missing_scope`, `invalid_scope`, `scope_too_broad`, `permission_denied`,
`target_mismatch`, `target_changed`, `protected_path`, `unsupported_path`, `argument_redirect`,
`execution_unavailable`, `semantic_not_installed`, `semantic_unavailable`, `semantic_indexing`,
`semantic_readiness_unknown`, `semantic_import_failed`, `semantic_session_mismatch`,
`semantic_request_failed`, `unsupported_operation`, `ambiguous_symbol`, `symbol_mismatch`,
`text_rename_rejected`, `preview_required`, `stale_preview`, `destination_collision`,
`apply_failed`, `unguarded_access`, `undeclared_prerequisite`.

### `readiness`

Both servers have a `readiness(expected_root, ...)` tool with an output schema. It reports
what is known without doing anything: no Gradle run, no language-server start, no import, no
directory scan, no write.

```json
{
  "schema_version": "1.0",
  "contract_version": "3.0",
  "server": "kotlin",
  "server_version": "0.2.0",
  "resolved_scope": "C:\\dev\\app",
  "worktree_root": "C:\\dev\\app",
  "project_root": "C:\\dev\\app\\android",
  "execution": {"state": "unknown", "reason": "execution_not_observed", "detail": "A Gradle launcher and a JDK are present, but this server has not run Gradle for this project yet; ..."},
  "semantic": {"state": "unknown", "reason": "semantic_not_started", "detail": "No language server session is running for this project. ..."},
  "operations": [{"name": "compile", "kind": "execution", "requires": ["scope", "target_access", "verified_project", "execution"]}]
}
```

- `execution.state`: `available` (this server process has started Gradle, or the shell, here),
  `unavailable`, `unknown` (the launcher and JDK exist, which is not evidence that they work),
  `not_applicable`.
- `semantic.state`: `ready`, `indexing`, `unavailable`, `unknown` (no session yet, or one that
  never reported a finished import), `not_applicable` (the files server).
- There is no overall flag. A response shows that the query reached the server, nothing more:
  it is not an authorisation, each operation checks its prerequisites again when it runs, and
  it says nothing about whether a build or test passed.
- `operations` is generated from the tool registrations, which is also where each tool's
  checks come from. `serverInfo.version` in the MCP handshake carries the package version; the
  tool list is fixed for the life of a server process, so `listChanged` is not advertised and
  clients pick up a new contract when they reconnect and list tools.

### Examples

A valid read, and the same read with the wrong worktree selected:

```json
{"name": "read_file", "arguments": {"expected_root": "C:\\dev\\app", "path": "C:\\dev\\app\\README.md"}}
{"name": "read_file", "arguments": {"expected_root": "C:\\dev\\app", "path": "C:\\dev\\app-other\\README.md"}}
```

```
[outcome=blocked reason=target_mismatch category=policy] C:\dev\app-other\README.md resolves to
C:\dev\app-other\README.md, which is outside expected_root C:\dev\app. Nothing was read, written or run. ...
```

Files MCP reads, searches and edit previews preserve file content in both text and structured
results. Credential labels, payment-related identifiers and URLs do not trigger redaction.
Scope checks, output limits and the prohibition on accessing `*.properties` files still apply.
Command and validation output remains sanitised.

New logs are sanitised, run-owned artifacts under the operating system's temporary directory
in `ide-mcp/<scope-path-hash>/logs`. They are never written into the selected worktree.
Use `read_artifact` with the returned path, SHA-256 and `output_scope` as `expected_root`.
This route rejects changed artifacts and older raw logs. Artifact write failure is reported
separately; a missing log is never returned as a verified path.

Readiness, then a code-aware query:

```json
{"name": "readiness", "arguments": {"expected_root": "C:\\dev\\app"}}
{"name": "find_usages", "arguments": {"expected_root": "C:\\dev\\app", "name": "Door", "file": "C:\\dev\\app\\src\\main\\kotlin\\demo\\Door.kt", "line": 3, "engine": "semantic"}}
```

```
[engine=semantic requested=semantic project=C:\dev\app complete=true]
symbol: Door, declared at src/main/kotlin/demo/Door.kt:3
  class Door(val width: Int)
'Door': 4 usage(s), 1 declaration(s), 0 import(s) on 5 listed line(s).  [D = declaration]
src/main/kotlin/demo/Door.kt
      3 D class Door(val width: Int)
      5   fun open(d: Door): Door = d
...
```

An ambiguous selection is rejected with the candidates:

```
[outcome=failed reason=ambiguous_symbol category=input]
'Door' occurs 2 times on line 5 of C:\dev\app\src\main\kotlin\demo\Use.kt. Call again with one of: column=12, column=23
```

Labelled text search (`engine="text"`, or `engine="auto"` when the language server is not
available): the first line reads `[engine=text requested=auto project=... complete=false
incomplete_reason=text_match]`, followed by the reason semantic resolution was not used.

Rename is two calls. The preview writes nothing:

```json
{"name": "rename_symbol", "arguments": {"expected_root": "C:\\dev\\app", "name": "Door", "new_name": "Gate", "file": "C:\\dev\\app\\src\\main\\kotlin\\demo\\Door.kt", "line": 3}}
```

```
[outcome=not_run reason=dry_run]
[engine=semantic requested=semantic project=C:\dev\app complete=true]
symbol: Door, declared at src/main/kotlin/demo/Door.kt:3
  class Door(val width: Int)
Would rename 'Door' to 'Gate': 5 line(s) in 2 file(s).
src/main/kotlin/demo/Door.kt (2)  -> will be renamed to Gate.kt  [revision 540a40e158e9]
      3  class Gate(val width: Int)
      5  fun open(d: Gate): Gate = d
src/main/kotlin/demo/Use.kt (3)  [revision 1dff929d0937]
      5  val pair = Gate(1) to Gate(2)
...
preview_id: 1989c1bb2affc8b4
```

Applying repeats the arguments with `"dry_run": false, "preview_id": "1989c1bb2affc8b4"`. The
edit set is computed again and applied only if it is byte-for-byte the one previewed; after an
edit to `Use.kt` in between:

```
[outcome=blocked reason=stale_preview category=input] The edit set for renaming 'Door' to 'Gate' is no
longer the one previewed as 1989c1bb2affc8b4: ... Nothing was written. Preview again and review the new edit set.
```

Direct navigation queries, one level each:

```json
{"name": "find_implementations", "arguments": {"expected_root": "C:\\dev\\app", "name": "area", "file": "C:\\dev\\app\\src\\main\\kotlin\\demo\\Shapes.kt", "line": 4}}
{"name": "find_callers", "arguments": {"expected_root": "C:\\dev\\app", "name": "total", "file": "C:\\dev\\app\\src\\main\\kotlin\\demo\\Shapes.kt", "line": 16, "column": 5}}
{"name": "find_callees", "arguments": {"expected_root": "C:\\dev\\app", "name": "total", "file": "C:\\dev\\app\\src\\main\\kotlin\\demo\\Shapes.kt", "line": 16, "column": 5}}
```

```
2 direct callee(s) of 'total' found by the language server.
  src/main/kotlin/demo/Shapes.kt:16  fun total(s: Shape, depth: Int): Int = ...  [unverified exactness: declaration-text estimate]
      call at src/main/kotlin/demo/Shapes.kt:16:81
  src/main/kotlin/demo/Shapes.kt:4  fun area(): Int  [possible dispatch: declared in an interface]
      call at src/main/kotlin/demo/Shapes.kt:16:58
```

These are static relationships, not a runtime order. Dispatch labels are declaration-text
estimates, not proof from the backend. Chains are not followed; traversal belongs to the client.
Semantic pages bind `query_id` to the session, project, selected symbol, open documents and
result set. Repeat the query arguments with its `offset` and `query_id`. A changed binding
returns `stale_continuation`. The response limit does not bound backend collection work.

### What clients need to change

1. Pass `expected_root` on every call to both servers, and pass the worktree, not a parent.
2. Treat `isError` results as failures and read the `[outcome=... reason=...]` line. A failed
   build, failing tests, lint findings and a non-zero `run_command` exit are now tool errors.
3. `find_usages` and the other code-aware tools no longer answer silently from text. Read the
   `[engine=...]` line; `engine="auto"` on `find_usages` is the only labelled text fallback.
4. `rename_symbol`: `engine="text"` is rejected and `file_glob` is gone; `dry_run=false` needs
   the `preview_id` from a preview.
5. Reconnect and list tools after an upgrade. `readiness` is optional and changes from moment
   to moment; do not cache it as a permission.
6. Read with `include_revision=true` before an existing-file content write. Pass its complete
   SHA-256 as `expected_revision`. Review a complete `edit_files` preview before applying.
7. Retain semantic `query_id` and job IDs with their selected scope. Poll jobs with backoff;
   consume output using the returned character cursor, then retrieve the final result.
8. Do not apply a semantic rename on a partial import. Android rename uses the current imported
   variant. It does not guarantee coverage of inactive variants.

Binding the scope to the user's task, caching and any routing between tools remain client
work and are not part of these servers.

## Tools

### files

`read_file`, `read_files`, `list_directory_tree`, `find_files` (glob and/or name substring),
`search_in_files` (text or regex, with context lines), `create_file`, `replace_text` (exact or
regex; must match once unless `replace_all`), `move_path`, `delete_path`, `git_status`,
`edit_files`, `run_command`, `start_command`, `validate_python`, `validate_dotnet`,
`start_validation`, `job_status`, `job_output`, `cancel_job`, `job_result`,
`read_artifact`, `readiness`.

#### Bounded reads, traversal and exact edits

- `read_file` scans at most 2 MB for a range and returns at most 10,000 lines. Total lines
  remain unknown before EOF. `include_revision=true` explicitly hashes at most 8 MB.
- `read_files` accepts 1–32 ranges and shares a 120,000-content-character budget.
  UTF-8 is the default. Select `encoding="cp1252"` explicitly for Windows legacy text.
  Binary, UTF-16 and unsupported encodings are rejected.
- Listings expand only `max_depth` (1–10). Collapsed directories have no recursive counts.
  `limit` bounds returned entries; `work_limit` bounds enumerated metadata (default 10,000,
  maximum 100,000). Discovery and search stop at their work/result limits. They have no
  continuation; narrow the directory or glob after an incomplete scan.
- Search reads at most 2 MB per file and 20 MB in aggregate. Skipped/unreadable files and
  output clipping make completeness false. An empty incomplete search proves no absence.
- `replace_text` and `create_file(overwrite=true)` require the current content revision.
  `edit_files` accepts 1–32 unique files, up to 100 exact unique replacements per file,
  8 MB per file and 16 MB in aggregate. Overlapping edits are rejected. The full focused
  diff must fit 120,000 characters; it is not clipped into an applicable preview.
- Exact edits preserve BOM, encoding, line endings and unrelated EOF bytes. New non-empty
  files end with a blank line. Preflight failures write nothing. Writes are staged and
  revisions are checked immediately before replacement, but there is no filesystem
  compare-and-swap guarantee or atomic multi-file transaction. Partial failures list
  written and pending files. No unconditional rollback is attempted.

Read, preview and apply using the final tool names:

```json
{"name":"read_file","arguments":{"expected_root":"C:\\dev\\app","path":"notes.txt","include_revision":true}}
{"name":"edit_files","arguments":{"expected_root":"C:\\dev\\app","edits":[{"path":"notes.txt","expected_revision":"<returned SHA-256>","replacements":[{"old_text":"exact old snippet","new_text":"new snippet"}]}]}}
{"name":"edit_files","arguments":{"expected_root":"C:\\dev\\app","dry_run":false,"preview_id":"<reviewed preview_id>","edits":[{"path":"notes.txt","expected_revision":"<same SHA-256>","replacements":[{"old_text":"exact old snippet","new_text":"new snippet"}]}]}}
```

A changed file returns `stale_revision` or `stale_preview`. Generate and review a new
preview. Text editing is not a substitute for a blocked semantic rename.

#### Focused Windows validation

`validate_python` requires an explicit Python interpreter, project directory and 1–32
target files. It supports `pytest`, `ruff` and `basedpyright`. Pytest accepts node IDs and
an optional `filter` (`-k`); lint/type checks use file targets. No dependency is installed
and no interpreter is silently substituted.

`validate_dotnet` requires an explicit executable, project file and `check="build"` or
`"test"`. Tests accept `filter`. Static project references must resolve inside the selected
source scope. Tests require one explicit literal `TargetFramework`, pinned through
`--framework`. Imported/conditional framework selection, dynamic references and multi-target
test projects are blocked where report ownership cannot be verified.
`isolate_outputs=true` requires verified .NET SDK 8+ and uses
native `--artifacts-path`. `output_root` selects a separate explicit output scope and is
checked independently; it does not widen source access.

Run-owned JUnit/TRX provides actual executed, skipped and failed counts. Missing,
malformed, inconclusive, zero-test or all-skipped evidence cannot produce a pass.
Recognised dependency/tooling failures are environment failures. A specific MSBuild lock
message is `output_locked`; access denied alone does not prove an application-owned lock.

Native alternate outputs are not an immutable source snapshot. Git SHA and dirty state
are recorded at start/end; dirty-file byte stability remains unverified. No snapshot
fallback, app termination, restart or live UI check is performed.

```json
{"name":"validate_python","arguments":{"expected_root":"C:\\dev\\app","project":"C:\\dev\\app","executable_path":"C:\\dev\\app\\.venv\\Scripts\\python.exe","check":"pytest","files":["tests/test_example.py"],"filter":"focused"}}
{"name":"validate_dotnet","arguments":{"expected_root":"C:\\dev\\app","project":"tests\\App.Tests.csproj","executable_path":"C:\\Program Files\\dotnet\\dotnet.exe","check":"test","filter":"FullyQualifiedName~Focused","isolate_outputs":true}}
```

### kotlin

| Tool | Purpose |
| --- | --- |
| `project_info` | Gradle root, versions, modules, configured tooling. Reads files only |
| `gradle_tasks` | Tasks per module, filterable |
| `gradle_run` | Any tasks and arguments, with a summarised result |
| `compile` | Compile a file's module, a module, or everything; errors as `path:line:col` |
| `run_tests` | Unit tests by file, `--tests` filter or module; failures with trimmed stack traces |
| `detekt` | Findings as `path:line:col: message [Rule]`, optionally auto-corrected |
| `ktlint` | Style check or `format=true`, via ktlint, kotlinter or spotless tasks |
| `dependencies` | Resolved libraries and project dependencies of a module; filter or tree |
| `find_symbol` | Where a class, function or property is declared, with signature and KDoc (text-based) |
| `file_outline` | Declarations in a file with line numbers (text-based) |
| `find_usages` | References to one symbol, labelled declaration / usage / import, with the engine actually used and completeness. Code-aware; `engine="auto"` or `"text"` can return labelled text matches |
| `rename_symbol` | The IDE's rename refactoring, never a text replacement. Preview by default; applying needs the preview's `preview_id`; renames `Name.kt` too |
| `find_implementations` | Implementations of an interface or abstract class, or overrides of a member. Needs the language server |
| `find_callers` | Direct callers of one function, with call sites. Needs the language server |
| `find_callees` | Direct callees with conservative declaration-text dispatch estimates. Needs the language server |
| `symbol_info` | What the symbol at a position resolves to: signature, docs, where it is declared. Needs the language server |
| `readiness` | Validated scope, Gradle project, execution and semantic state, registered operations. Runs nothing |
| `file_problems` | IntelliJ inspections and compiler errors for one file, with available quick fixes, no build. Needs the language server |
| `start_validation` | Queue compile, tests, detekt, ktlint or explicit Gradle tasks through shared jobs |
| `job_status`, `job_output`, `cancel_job`, `job_result`, `read_artifact` | Shared scoped lifecycle and sanitised output retrieval |

Gradle output is reduced to what matters: outcome, failed tasks, located errors, test results
and Gradle's "What went wrong". Sanitised logs use unique names under the operating system's
temporary `ide-mcp/<scope-path-hash>/logs/` directory (last 30 per scope).
Final sanitised bytes are hashed. Recognised credential/private-key payloads are redacted
and recognised payment lines are withheld before clipping or progress.
This is not certification for arbitrary payment traces; do not supply unknown sensitive formats.

Gradle execution and report capture hold a native workspace file lease across server
processes. Its `validation.lock` stays in temporary `ide-mcp/<project-path-hash>/`, not the
worktree. The lock file remains after release so competing processes use the same file.
Reports are checked before/after the run while the lease is held. Unchanged
reports from executed tasks are unverified; cached/up-to-date evidence is labelled reused,
not fresh execution. Counts come from actual test cases, excluding skips. Known failures
remain failures when other reports are incomplete. Focused compilation retains sibling
module errors in bounded diagnostics instead of concealing them.

`compile`, `run_tests`, `detekt` and `ktlint` pick the task for you (`compileDebugSources` on
Android modules, `classes` on JVM ones, flavoured variants when present). To do that they read
the build's task list once with `gradle tasks --all` and cache it for the life of the server,
so the first call into a large project is slower. `gradle_tasks(refresh=true)` re-reads it
after you change build scripts.

`detekt` and `ktlint` use the project's own Gradle tasks, so its config, baselines and plugins
apply. If a build has no such task they fall back to a `detekt` / `ktlint` binary on `PATH`.

### Shared jobs and artifacts

Files exposes `start_command` and `start_validation(adapter="python"|"dotnet", ...)`.
Kotlin exposes `start_validation(check="compile"|"tests"|"detekt"|"ktlint"|"gradle", ...)`.
Both expose `job_status`, `job_output`, `cancel_job`, `job_result` and `read_artifact`.
Synchronous and asynchronous adapters use the same runner and result classification.

Jobs are `pending`, `running`, `cancelling`, `finished` or `unknown`. Starting a job does
not establish a final validation outcome. Cancellation targets only the owned process
tree, never an IDE or shared daemon by name. Unconfirmed termination stays `unknown`.
Store the ID with its scope; another worktree cannot poll or cancel it.

`job_output` uses monotonic character cursors over sanitised output (maximum 30,000
characters per page). Cursor replay returns the same retained slice. Discarded history
returns `cursor_expired` with the retained starting cursor. Each job retains at most
1,000,000 characters. At most eight jobs are active and 64 are retained. Finished jobs
expire 30 minutes after creation. Run-owned report directories are left for explicit
user cleanup; only `.safe.log` retention is automatic.

Logs and workspace locks require access to the operating system's temporary directory.
If `IDE_MCP_ALLOWED_ROOTS` is set, include that directory in the allowlist.
Denied temporary storage never falls back to the worktree. Validation reports and explicitly
selected build-output locations are unchanged.

Jobs are in memory. Client disconnect does not relaunch work. Server exit loses job
metadata. Cooperative task cancellation waits for owned termination, but server shutdown
or a hard process kill cannot guarantee it. After reconnect, an unknown ID reports
`job_unknown`; never infer completion or restart a job automatically.

```json
{"name":"start_command","arguments":{"expected_root":"C:\\dev\\app","cwd":"C:\\dev\\app","command":"git --no-pager status --short"}}
{"name":"job_status","arguments":{"expected_root":"C:\\dev\\app","job_id":"<returned ID>"}}
{"name":"job_output","arguments":{"expected_root":"C:\\dev\\app","job_id":"<returned ID>","cursor":0}}
{"name":"job_result","arguments":{"expected_root":"C:\\dev\\app","job_id":"<returned ID>"}}
{"name":"read_artifact","arguments":{"expected_root":"<artifact output_scope>","path":"<artifact path>","sha256":"<artifact sha256>"}}
```

## Code-aware tools (Kotlin language server)

`find_usages`, `rename_symbol`, `symbol_info` and `file_problems` can use JetBrains'
[Kotlin language server](https://github.com/Kotlin/kotlin-lsp), the IntelliJ engine running
headless. With it they resolve symbols the way the IDE does: renaming the `scope` property of
one class leaves every other `scope` alone, and `file_problems` reports inspections such as
unused code or deprecated API, not only compiler errors.

It is not bundled. The install script downloads it on first run and reuses it afterwards; pass
`--no-lsp` / `-NoLsp` to skip it. To install it on its own:

```bash
.venv/bin/kotlin-mcp --install-lsp          # Linux
.venv\Scripts\kotlin-mcp.exe --install-lsp  # Windows
brew install JetBrains/utils/kotlin-lsp     # macOS
```

The download is about 370 MB (1.2 GB
unpacked, under `~/.local/share/ide-mcp` or `%LOCALAPPDATA%\ide-mcp`) and is verified against
JetBrains' published SHA-256. It is JetBrains software under JetBrains' terms, currently Alpha.

How it behaves:

- One language server runs per project, started by the first code-aware call and stopped after
  30 idle minutes (`KOTLIN_LSP_IDLE_MINUTES`). Nothing has to be opened by hand.
- The first call imports and indexes the project: about 10 seconds for a small one, minutes for
  a large one. If that outlasts the tool call, the error says so and the import carries on in
  the background; call again.
- It uses up to 2 GB of heap per project by default. Set `IJ_JAVA_OPTIONS=-Xmx4g` for the
  `kotlin` server to change that.
- Files edited on disk between calls are picked up automatically.
- When a name is declared more than once, or occurs more than once on the given line, the tool
  rejects the call with the candidates and asks for the `file`, `line` and `column` of the one
  you mean. It never picks the first.
- Without the language server, `find_usages` with `engine="auto"` (the default) or
  `engine="text"` returns whole-word text matches, labelled as such on the first line.
  `rename_symbol`, `find_implementations`, `find_callers` and `find_callees` are blocked with
  the reason; none of them uses text.
- Android results describe the current imported variant. `rename_symbol(variant="paidDebug")`
  selects a variant for that import. Omitting it reuses the current model or the backend default.

### Import freshness

Before reusing a session, the server checks build inputs inside the selected project.
It monitors Gradle scripts, TOML catalogues, lockfiles, wrappers, Maven configuration,
local JAR/AAR metadata, `buildSrc` and files in nested Gradle builds with their own settings.
Ignored configuration is included. Generated output and cache directories are excluded.
Text inputs are checked by content hash. Protected `*.properties` files and local JAR/AAR
dependencies are checked by file identity, size and timestamps only. Their contents are never read.
Changes that preserve all metadata of these inputs cannot be detected.

Changed inputs invalidate the model. An idle session is stopped and replaced.
The replacement keeps the selected variant and uses an input-bound backend cache directory.
Old cache directories are retained. A successful import of the selected project is still
required before semantic answers or rename writes are permitted.
Changes during a query or rename preflight reject that operation. An active operation or
import is not terminated for a refresh; retry after it finishes.
Reviewed rename previews and semantic continuations do not survive a replacement.

An incomplete, unreadable or out-of-scope input scan blocks semantic use.
The `readiness` tool remains observational; it does not scan for new changes.
Inputs outside the selected project, including user-level Gradle/Maven configuration,
remote dependency changes and arbitrary files loaded by build scripts, are not monitored.

### Android projects

Android import in the language server is experimental, and one defect matters in practice:
modules that enable `buildConfig`, `viewBinding` or `dataBinding` fail to import
([Kotlin/kotlin-lsp#253](https://github.com/Kotlin/kotlin-lsp/issues/253), seen with AGP 8.13).
In a single-module app that takes the whole project with it.

The `kotlin` server works around it by itself. When an import fails that way, it imports again
with the Android test components that caused it switched off, and remembers that for the
project, so only the first call pays for two imports. No project settings are changed.
The switch is a generated Gradle init script handed to the language server's import alone.
Ordinary builds never see it.

The sources of omitted components, normally `src/androidTest`, are invisible to the engine.
Read-only results state partial coverage, and `file_problems` and `symbol_info` refuse files
in omitted components. A partial import blocks rename before an applicable preview or write.
A later compile is not a substitute for complete relevant semantic coverage. Set
`KOTLIN_LSP_ANDROID_WORKAROUND=0` to disable the workaround.

Android rename uses one imported model, like IDE refactoring. It does not require proof of
coverage across every variant. Preview and apply report `variant_coverage="imported_model"`.
An omitted variant name is reported as `null`, not guessed from source folders or task names.

An explicit `variant` is passed through the import process's `LSP_ANDROID_VARIANT`.
An import-only init script checks that the requested variant is enabled in each Android
module and that no `lsp.android.variant` property overrides it. Missing variants and conflicting
overrides block the import. Rename also requires confirmation from that import.
No properties files or machine-wide environment settings are changed.
Changing the variant starts a fresh session and invalidates earlier previews.
The current semantic operation or import must finish before its variant can change.
Gradle validation's variant input remains separate from semantic import selection.

Rename validates UTF-16 positions, engine document versions/bytes, session identity,
declaration resolution, all file revisions and destinations. Unsupported resource creation
or deletion, malformed/overlapping edits and filename collisions are blocked.
`rename_files=false` cannot bypass a required class/file-name move. Preview includes every
move, file revision, document version and the full focused diff. Review bounds are 150
changed lines, 500,000 proposed source characters, 35,000 diff characters and 50,000 preview
characters. Oversized previews are blocked rather than clipped into applicable plans.

If an import still fails, the tools never answer from an incomplete model: `find_usages` with
`engine="auto"` returns labelled text matches with the reason, and `rename_symbol`, the
navigation queries, `symbol_info` and `file_problems` are blocked with the reason. `compile`, `detekt` and `ktlint` are unaffected either way.

## Compared with the JetBrains MCP server

| JetBrains tool | Here |
| --- | --- |
| `get_file_text_by_path`, `create_new_file`, `replace_text_in_file` | `read_file`, `create_file`, `replace_text` |
| `list_directory_tree`, `find_files_by_glob`, `find_files_by_name_keyword` | `list_directory_tree`, `find_files` |
| `search_in_files_by_text`, `search_in_files_by_regex` | `search_in_files` |
| `execute_terminal_command`, `get_repositories` | `run_command`, `git_status` |
| `get_project_modules`, `get_project_dependencies` | `project_info`, `dependencies` |
| `get_run_configurations`, `execute_run_configuration` | `gradle_tasks`, `gradle_run`, `run_tests` |
| `get_file_problems` | `file_problems` (inspections), `compile` with `file`, `detekt` / `ktlint` with `files` |
| `reformat_file` | `ktlint` with `format=true` |
| `get_symbol_info`, `rename_refactoring` | `symbol_info`, `find_symbol`, `find_usages`, `rename_symbol` |
| `open_file_in_editor`, `get_all_open_file_paths` | not applicable without an editor |

Where this differs from the IDE:

- **Inspections and refactoring need the language server.** With it, `file_problems`,
  `find_usages` and `rename_symbol` use the same engine as the IDE. Without it, problems come
  from the compiler and detekt only, usages are labelled whole-word text matches that cannot
  tell apart two symbols with the same name, and renaming is not available.
- **Android instrumented tests may be left out.** Where the language server's Android import
  needs the workaround above, `src/androidTest` is not covered by the code-aware tools.
- **The language server covers Kotlin best.** Java files are analysed too, but its documented
  diagnostics and quick fixes are for Kotlin.
- **Quick fixes are listed, not applied.** `file_problems` names the fixes the IDE would offer;
  apply them with an edit.
- **Formatting is ktlint's**, not the IDE code style.

## Configuration

| Env var | Server | Effect |
| --- | --- | --- |
| `IDE_MCP_ALLOWED_ROOTS` | files | Directories the file tools may touch, separated by `:` (`;` on Windows). Unset means anywhere |
| `JAVA_HOME`, `ANDROID_HOME` | kotlin | Passed to Gradle. Pinned by `install.sh` |
| `DETEKT_CLI`, `KTLINT_CLI` | kotlin | Explicit path to the fallback CLI binaries |
| `KOTLIN_LSP_PATH` | kotlin | Use an existing Kotlin language server (its `intellij-server` launcher or install directory) |
| `KOTLIN_LSP_IDLE_MINUTES` | kotlin | Stop a project's language server after this long unused. Default 30 |
| `KOTLIN_LSP_ANDROID_WORKAROUND` | kotlin | `0` disables the automatic workaround for the Android import defect |
| `IJ_JAVA_OPTIONS` | kotlin | JVM options for the language server, e.g. `-Xmx4g` |

`run_command`, `gradle_run`, `delete_path` and `rename_symbol` can change or remove things and
are annotated as destructive, so rely on your agent's approval settings accordingly.

## Development

See `IMPLEMENTATION_HANDOFF.md` for the verified fixture results, compatibility changes
and remaining live acceptance checks.

```bash
uv run pytest                                              # unit tests, no Gradle needed
npx @modelcontextprotocol/inspector .venv/bin/kotlin-mcp   # interactive tool browser
```
