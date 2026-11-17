# ide-mcp

Two MCP servers that give coding agents (Codex, Claude Code, ...) what the JetBrains MCP server
offers, without an IDE: nothing has to be opened in Android Studio or IntelliJ first, and no
tool is scoped to a workspace. Every tool takes a path and works out the rest.

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
| `find_symbol` | Where a class, function or property is declared, with signature and KDoc (text-based) |
| `file_outline` | Declarations in a file with line numbers (text-based) |
| `find_usages` | References to one symbol, labelled declaration / usage / import. Code-aware with the language server |
| `rename_symbol` | The IDE's rename refactoring; dry run by default; renames `Name.kt` too. Code-aware with the language server |
| `symbol_info` | What the symbol at a position resolves to: signature, docs, where it is declared. Needs the language server |
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

It is optional and not bundled. Install it once:

```bash
.venv/bin/kotlin-mcp --install-lsp          # Linux
.venv\Scripts\kotlin-mcp.exe --install-lsp  # Windows
brew install JetBrains/utils/kotlin-lsp     # macOS
```

or pass `--with-lsp` / `-WithLsp` to the install script. The download is about 370 MB (1.2 GB
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
- When a name is declared more than once, the tool lists the candidates and asks for the `file`
  and `line` of the one you mean.
- Without the language server, or with `engine="text"`, `find_usages` and `rename_symbol` use
  whole-word text matching and say so in their output.

### Android projects

Android import in the language server is experimental, and one defect matters in practice:
modules that enable `buildConfig`, `viewBinding` or `dataBinding` fail to import with AGP 8.13
([Kotlin/kotlin-lsp#253](https://github.com/Kotlin/kotlin-lsp/issues/253)). In a single-module
app that takes the whole project with it.

The tools detect a failed or partial import and never answer from an incomplete model:
`find_usages` falls back to text matching with a note, `rename_symbol` shows the text-based
preview and writes nothing unless you ask for `engine="text"`, and `symbol_info` and
`file_problems` return the reason. `compile`, `detekt` and `ktlint` are unaffected. Once a
language server release fixes the import, install it with
`kotlin-mcp --install-lsp <version>` and the code-aware path takes over.

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
  from the compiler and detekt only, and usages and renames are whole-word text matches that
  cannot tell apart two symbols with the same name.
- **Android projects may not import** in the language server yet (see above), in which case
  the text-based behaviour applies there.
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
| `IJ_JAVA_OPTIONS` | kotlin | JVM options for the language server, e.g. `-Xmx4g` |

`run_command`, `gradle_run`, `delete_path` and `rename_symbol` can change or remove things and
are annotated as destructive, so rely on your agent's approval settings accordingly.

## Development

```bash
uv run pytest                                              # unit tests, no Gradle needed
npx @modelcontextprotocol/inspector .venv/bin/kotlin-mcp   # interactive tool browser
```
