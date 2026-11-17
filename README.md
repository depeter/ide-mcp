# ide-mcp

Two MCP servers that give coding agents (Codex, Claude Code, ...) what the JetBrains MCP server
offers, without an IDE: nothing has to be opened in Android Studio or IntelliJ first, and no
tool is scoped to a workspace. Every tool takes a path and works out the rest.

| Server | Binary | What it does |
| --- | --- | --- |
| `files` | `files-mcp` | Read, create, edit, find and search files; directory trees; git status; shell commands |
| `kotlin` | `kotlin-mcp` | Gradle builds, compile checks, unit tests, dependencies, detekt, ktlint, Kotlin/Java symbols |

Both run locally as stdio processes; the agent starts them on demand. They are independent, so
register only one if that is all you need.

## Install

Requirements: [`uv`](https://docs.astral.sh/uv/) or Python 3.10+, `git`, and for the `kotlin`
server a JDK (projects bring their own Gradle through `gradlew`).

```bash
scripts/install.sh            # build .venv and register both servers with Codex
scripts/install.sh --claude   # ...and also with Claude Code
```

The script pins `JAVA_HOME` and `ANDROID_HOME` for the `kotlin` server, because agents launch
MCP servers with a trimmed environment, and raises Codex's tool timeout for it to 30 minutes
(the default of 60 seconds is shorter than most builds). Re-running is safe.

| Script | Purpose |
| --- | --- |
| `scripts/check.sh` | Show what each server sees: tools, JDK, Android SDK, lint CLIs |
| `scripts/uninstall.sh [--purge]` | Unregister from Codex/Claude; `--purge` also deletes `.venv` |

### Manual registration

```toml
# ~/.codex/config.toml
[mcp_servers.files]
command = "/absolute/path/to/ide-mcp/.venv/bin/files-mcp"

[mcp_servers.kotlin]
command = "/absolute/path/to/ide-mcp/.venv/bin/kotlin-mcp"
tool_timeout_sec = 1800
env = { JAVA_HOME = "/usr/lib/jvm/java-17-openjdk-amd64", ANDROID_HOME = "/home/you/Android/Sdk" }
```

The package is installed in editable mode, so edits under `src/` take effect the next time the
agent starts a server.

## How paths work

- **File tools** take absolute paths (or `~/...`). Search and listing tools take a `root`
  directory. Inside a git repository they honour `.gitignore`; elsewhere they skip build and
  cache directories. `include_ignored=true` looks everywhere.
- **Gradle tools** take `project_dir`: any path inside the project, a file included. The server
  walks up to the Gradle root (the nearest `settings.gradle[.kts]` with a `gradlew`), and if
  there is none above, looks below, which covers `<repo>/android` and `<repo>/app/android`
  layouts.
- **Modules** are Gradle paths (`:app`, `:features:login`). Most tools also accept `file`
  instead and use the module that owns it.

## Tools

### files

`read_file`, `list_directory_tree`, `find_files` (glob and/or name substring),
`search_in_files` (text or regex, with context lines), `create_file`, `replace_text` (exact or
regex; must match once unless `replace_all`), `move_path`, `delete_path`, `git_status`,
`run_command`.

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
| `find_symbol` | Where a class, function or property is declared, with signature and KDoc |
| `file_outline` | Declarations in a file with line numbers |
| `find_usages` | Whole-word occurrences, labelled declaration / usage / comment |
| `rename_symbol` | Whole-word rename across sources; dry run by default; renames `Name.kt` too |

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

## Compared with the JetBrains MCP server

| JetBrains tool | Here |
| --- | --- |
| `get_file_text_by_path`, `create_new_file`, `replace_text_in_file` | `read_file`, `create_file`, `replace_text` |
| `list_directory_tree`, `find_files_by_glob`, `find_files_by_name_keyword` | `list_directory_tree`, `find_files` |
| `search_in_files_by_text`, `search_in_files_by_regex` | `search_in_files` |
| `execute_terminal_command`, `get_repositories` | `run_command`, `git_status` |
| `get_project_modules`, `get_project_dependencies` | `project_info`, `dependencies` |
| `get_run_configurations`, `execute_run_configuration` | `gradle_tasks`, `gradle_run`, `run_tests` |
| `get_file_problems` | `compile` with `file`, plus `detekt` / `ktlint` with `files` |
| `reformat_file` | `ktlint` with `format=true` |
| `get_symbol_info`, `rename_refactoring` | `find_symbol`, `find_usages`, `rename_symbol` |
| `open_file_in_editor`, `get_all_open_file_paths` | not applicable without an editor |

Where this differs from the IDE:

- **Problems come from the compiler, not from inspections.** `compile` reports what `kotlinc`
  and `javac` report. IDE-only inspections (unused symbols, simplifiable expressions) are not
  included; detekt covers much of that ground.
- **Symbol tools read source text and do not resolve types.** `find_usages` and
  `rename_symbol` match whole identifiers, so two unrelated symbols with the same name are
  treated as one. For a distinctive name that is equivalent to the IDE refactoring; for a
  common one (`id`, `State`), check the dry run and narrow `root` or `file_glob`. Always
  `compile` after a rename.
- **Formatting is ktlint's**, not the IDE code style.

## Configuration

| Env var | Server | Effect |
| --- | --- | --- |
| `IDE_MCP_ALLOWED_ROOTS` | files | `:`-separated directories the file tools may touch. Unset means anywhere |
| `JAVA_HOME`, `ANDROID_HOME` | kotlin | Passed to Gradle. Pinned by `install.sh` |
| `DETEKT_CLI`, `KTLINT_CLI` | kotlin | Explicit path to the fallback CLI binaries |

`run_command`, `gradle_run`, `delete_path` and `rename_symbol` can change or remove things and
are annotated as destructive, so rely on your agent's approval settings accordingly.

## Development

```bash
uv run pytest                                              # unit tests, no Gradle needed
npx @modelcontextprotocol/inspector .venv/bin/kotlin-mcp   # interactive tool browser
```
