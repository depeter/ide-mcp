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

Request contract **2.0** (package 0.2.0). Earlier callers must add `expected_root` to every
call; there is no unscoped route.

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

Failures are MCP tool errors (`isError: true`) whose text starts with one parseable line:

```
[outcome=blocked reason=target_mismatch category=policy] C:\dev\app-other\README.md resolves to ...
```

| Outcome | Meaning |
| --- | --- |
| `passed` | First line of a successful build, test, lint, command or applied rename. |
| `failed` | It ran and failed, or the request was wrong. `category` says which: `source` (compiler errors, failing tests, lint findings), `input`, `environment`, `policy`, or `unknown` when a build failed without located errors. |
| `blocked` | Stopped by a check before anything was read, written or run. |
| `timed_out` | Killed after `timeout_sec`. |
| `not_run` | Nothing was executed: a rename preview (`reason=dry_run`, a normal result), or a test run in which no test executed (`reason=no_tests_executed`, an error). |
| `cancelled` | Not a result: a cancelled request is ended by the protocol and its process tree is killed. |

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
  "contract_version": "2.0",
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

A saved Gradle log is read by naming its folder as the scope:
`{"expected_root": "C:\\Users\\you\\.cache\\ide-mcp\\logs", "path": "app-20261120-101500-4242.log"}`.

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
  src/main/kotlin/demo/Shapes.kt:16  fun total(s: Shape, depth: Int): Int = ...  [exact]
      call at src/main/kotlin/demo/Shapes.kt:16:81
  src/main/kotlin/demo/Shapes.kt:4  fun area(): Int  [possible dispatch: declared in an interface]
      call at src/main/kotlin/demo/Shapes.kt:16:58
```

These are static relationships, not a runtime order, and chains are not followed: walking a
call chain means calling the query again on a result, which is left to the client.

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

Binding the scope to the user's task, caching and any routing between tools remain client
work and are not part of these servers.

## Tools

### files

`read_file`, `list_directory_tree`, `find_files` (glob and/or name substring),
`search_in_files` (text or regex, with context lines), `create_file`, `replace_text` (exact or
regex; must match once unless `replace_all`), `move_path`, `delete_path`, `git_status`,
`run_command`, `readiness`.

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
| `find_callees` | Direct callees of one function, each marked exact, possible dispatch or unresolved. Needs the language server |
| `symbol_info` | What the symbol at a position resolves to: signature, docs, where it is declared. Needs the language server |
| `readiness` | Validated scope, Gradle project, execution and semantic state, registered operations. Runs nothing |
| `file_problems` | IntelliJ inspections and compiler errors for one file, with available quick fixes, no build. Needs the language server |

Gradle output is reduced to what matters: outcome, failed tasks, located errors, test results
and Gradle's "What went wrong". The full log of every run is kept under
`~/.cache/ide-mcp/logs/` (last 30) and its path is part of each result.

`compile`, `run_tests`, `detekt` and `ktlint` pick the task for you (`compileDebugSources` on
Android modules, `classes` on JVM ones, flavoured variants when present). To do that they read
the build's task list once with `gradle tasks --all` and cache it for the life of the server,
so the first call into a large project is slower. `gradle_tasks(refresh=true)` re-reads it
after you change build scripts.

`detekt` and `ktlint` use the project's own Gradle tasks, so its config, baselines and plugins
apply. If a build has no such task they fall back to a `detekt` / `ktlint` binary on `PATH`.

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
- The language server offers no way to choose an Android build variant, so results describe
  whatever it imported.

### Android projects

Android import in the language server is experimental, and one defect matters in practice:
modules that enable `buildConfig`, `viewBinding` or `dataBinding` fail to import
([Kotlin/kotlin-lsp#253](https://github.com/Kotlin/kotlin-lsp/issues/253), seen with AGP 8.13).
In a single-module app that takes the whole project with it.

The `kotlin` server works around it by itself. When an import fails that way, it imports again
with the Android test components that caused it switched off, and remembers that for the
project, so only the first call pays for two imports. Nothing in the project or in
`~/.gradle` is changed: the switch is a Gradle init script handed to the language server's
import alone, and ordinary builds never see it.

The price is that the sources of those components, normally `src/androidTest`, are invisible
to the code-aware tools: `find_usages` and `rename_symbol` do not look there and say so in
their output when the project has such sources, and `file_problems` and `symbol_info` refuse
files in them. Unit tests under `src/test` stay visible unless they too break the import. Run
`compile` after a rename to catch anything missed in instrumented tests. Set
`KOTLIN_LSP_ANDROID_WORKAROUND=0` to switch the workaround off.

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

```bash
uv run pytest                                              # unit tests, no Gradle needed
npx @modelcontextprotocol/inspector .venv/bin/kotlin-mcp   # interactive tool browser
```
