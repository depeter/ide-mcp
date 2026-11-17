"""Lightweight Kotlin/Java symbol index built from source text.

There is no compiler behind this: declarations are recognised by syntax, and usages and renames
are whole-word text matches. That is fast and needs no project import, at the cost of not
telling apart two symbols that share a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SOURCE_SUFFIXES = (".kt", ".kts", ".java")

_KT_MODIFIERS = (
    "public|private|internal|protected|open|abstract|sealed|data|inline|value|annotation|enum|inner|"
    "override|suspend|const|lateinit|actual|expect|operator|infix|tailrec|external|final|companion|fun"
)
_KT_DECL = re.compile(
    rf"^\s*(?:@[\w.:]+(?:\([^)]*\))?\s+)*(?:(?:{_KT_MODIFIERS})\s+)*"
    r"(?P<kind>class|interface|object|fun|val|var|typealias)\b\s*(?P<rest>.*)$"
)
_TYPE_PARAMS = re.compile(r"^<[^<>]*(?:<[^<>]*(?:<[^<>]*>[^<>]*)*>[^<>]*)*>\s*")
_IDENT = r"(?:`[^`]+`|[A-Za-z_]\w*)"

_JAVA_MODS = "public|private|protected|static|final|abstract|sealed|non-sealed|strictfp|synchronized|native|default|transient|volatile"
_JAVA_TYPE = re.compile(rf"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:{_JAVA_MODS})\s+)*(?P<kind>class|interface|enum|record|@interface)\s+(?P<name>\w+)")
_JAVA_METHOD = re.compile(
    rf"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:{_JAVA_MODS})\s+)+(?:<[^>]+>\s+)?(?:[\w<>\[\],.?]+\s+)?(?P<name>\w+)\s*\("
)
_JAVA_FIELD = re.compile(rf"^\s*(?:(?:{_JAVA_MODS})\s+)+[\w<>\[\],.?]+\s+(?P<name>\w+)\s*(?:=|;)")

_STRING = re.compile(r'"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'')
_TYPE_KINDS = {"class", "interface", "object", "enum", "record", "@interface"}


@dataclass(frozen=True)
class Symbol:
    kind: str
    name: str
    line: int  # 1-based
    container: str  # "Outer.Inner" or ""

    @property
    def qualified(self) -> str:
        return f"{self.container}.{self.name}" if self.container else self.name


def _strip_code(line: str, in_raw: bool) -> tuple[str, bool]:
    """Blank out string literals and // comments so braces inside them aren't counted."""
    out = []
    i = 0
    while i < len(line):
        if in_raw:
            j = line.find('"""', i)
            if j == -1:
                return "".join(out), True
            i, in_raw = j + 3, False
            continue
        m = _STRING.search(line, i)
        cut = line.find("//", i)
        if cut != -1 and (m is None or cut < m.start()):
            out.append(line[i:cut])
            break
        if m is None:
            out.append(line[i:])
            break
        out.append(line[i : m.start()])
        if m.group() == '"""':
            in_raw = True
        else:
            out.append('""')
        i = m.end()
    return "".join(out), in_raw


def _kotlin_name(kind: str, rest: str) -> str | None:
    if kind in ("class", "interface", "typealias"):
        m = re.match(_IDENT, rest)
        return m.group() if m else None
    if kind == "object":
        m = re.match(_IDENT, rest)
        return m.group() if m else "Companion"
    rest = _TYPE_PARAMS.sub("", rest, count=1)
    if kind == "fun":
        head = rest.split("(", 1)[0] if "(" in rest else ""
    else:
        if rest.startswith("("):  # destructuring declaration
            return None
        head = re.split(r"[:=\n]|\bby\b|\bget\b", rest, maxsplit=1)[0]
    m = re.search(rf"({_IDENT})\s*$", head.strip())
    return m.group(1) if m else None


def outline(text: str, java: bool = False) -> list[Symbol]:
    """Declarations at file and class level (locals inside function bodies are skipped)."""
    symbols: list[Symbol] = []
    # Each open brace pushes (kind, name): a type kind, "fun", or "block" for anything else.
    stack: list[tuple[str, str]] = []
    # A type or function whose body brace has not been seen yet.
    pending: tuple[str, str] | None = None
    parens = 0
    in_block_comment = in_raw = False

    for idx, raw in enumerate(text.splitlines()):
        line = raw
        if in_block_comment:
            end = line.find("*/")
            if end == -1:
                continue
            line, in_block_comment = line[end + 2 :], False
        line = re.sub(r"/\*.*?\*/", "", line)
        code, in_raw = _strip_code(line, in_raw)
        start = code.find("/*")
        if start != -1:
            code, in_block_comment = code[:start], True
        stripped = code.strip()
        if not stripped:
            continue

        in_body = any(k in ("fun", "block") for k, _ in stack)
        container = ".".join(n for k, n in stack if k in _TYPE_KINDS)
        decl: tuple[str, str] | None = None
        if parens > 0:
            # Inside a parameter list: only primary-constructor properties count.
            if not java and not in_body and pending and pending[0] in _TYPE_KINDS and (m := _KT_DECL.match(code)):
                if m.group("kind") in ("val", "var") and (name := _kotlin_name(m.group("kind"), m.group("rest"))):
                    owner = f"{container}.{pending[1]}" if container else pending[1]
                    symbols.append(Symbol(m.group("kind"), name.strip("`"), idx + 1, owner))
        elif not in_body and not stripped.startswith(("import ", "package ")):
            if java:
                if m := _JAVA_TYPE.match(code):
                    decl = (m.group("kind"), m.group("name"))
                elif m := _JAVA_METHOD.match(code):
                    decl = ("fun", m.group("name"))
                elif m := _JAVA_FIELD.match(code):
                    decl = ("val", m.group("name"))
            elif m := _KT_DECL.match(code):
                kind = m.group("kind")
                name = _kotlin_name(kind, m.group("rest"))
                if name:
                    if kind == "class" and re.search(r"\benum\s+class\b", code):
                        kind = "enum"
                    decl = (kind, name.strip("`"))
        if decl:
            symbols.append(Symbol(decl[0], decl[1], idx + 1, container))
            pending = decl if decl[0] in _TYPE_KINDS or decl[0] == "fun" else None

        saw_brace = False
        for ch in code:
            if ch == "(":
                parens += 1
            elif ch == ")":
                parens = max(0, parens - 1)
            elif ch == "{":
                saw_brace = True
                if pending and parens == 0:
                    stack.append(pending)
                    pending = None
                else:
                    stack.append(("block", ""))
            elif ch == "}":
                saw_brace = True
                if stack:
                    stack.pop()
                if parens == 0:
                    pending = None
        # A body-less declaration (abstract fun, `class Marker`) must not claim a later brace.
        if pending and not decl and not saw_brace and parens == 0 and not _continues(stripped):
            pending = None
    return symbols


def _continues(stripped: str) -> bool:
    """Whether a line looks like part of a multi-line signature rather than a new statement."""
    return stripped.startswith((")", ":", "where ", "->", ",", "=", "@")) or stripped.endswith((",", "(", "=", ":"))


def signature(lines: list[str], line: int, max_lines: int = 8) -> str:
    """The declaration starting at 1-based `line`, up to its body or initializer."""
    out: list[str] = []
    depth = 0
    for raw in lines[line - 1 : line - 1 + max_lines]:
        text = raw.rstrip()
        out.append(text)
        depth += text.count("(") - text.count(")")
        if depth <= 0:
            break
    return re.sub(r"\s*\{\s*$", "", "\n".join(out)).strip("\n")


def doc_comment(lines: list[str], line: int, max_lines: int = 15) -> str:
    """KDoc/Javadoc immediately above 1-based `line` (annotations in between are skipped)."""
    i = line - 2
    while i >= 0 and lines[i].strip().startswith("@"):
        i -= 1
    if i < 0 or not lines[i].strip().endswith("*/"):
        return ""
    end = i
    while i >= 0 and "/**" not in lines[i]:
        if "/*" in lines[i]:
            return ""  # a plain block comment, not documentation
        i -= 1
    if i < 0:
        return ""
    body = []
    for raw in lines[i : end + 1]:
        s = raw.strip()
        s = re.sub(r"^/\*\*\s?|\s*\*/$", "", s)
        s = re.sub(r"^\*\s?", "", s)
        body.append(s)
    text = [s for s in body]
    while text and not text[0]:
        text.pop(0)
    while text and not text[-1]:
        text.pop()
    if len(text) > max_lines:
        text = text[:max_lines] + ["..."]
    return "\n".join(text)


def word_regex(name: str) -> re.Pattern[str]:
    """Match `name` as a whole identifier."""
    return re.compile(rf"(?<![\w$`]){re.escape(name)}(?![\w$`])")


def is_identifier(name: str) -> bool:
    return re.fullmatch(r"[A-Za-z_]\w*", name) is not None


def classify_line(line: str, name: str, declared_here: bool) -> str:
    s = line.strip()
    if s.startswith("import "):
        return "import"
    if declared_here:
        return "declaration"
    if s.startswith(("//", "*", "/*")):
        return "comment"
    return "usage"


def is_source(path: Path) -> bool:
    return path.suffix in SOURCE_SUFFIXES
