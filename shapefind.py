#!/usr/bin/env python3
"""shapefind — find Python code by its shape, not by its text.

A pattern is written as ordinary Python, with metavariables standing in for
the parts you do not care about:

    $X      matches one node (an expression, a statement, a name...)
    $$X     matches a run of nodes (arguments, statements)
    $_      matches one node and does not remember it

Everything the parser throws away — comments, blank lines, parentheses,
indentation width, quote style — is invisible to matching, because it is
invisible to Python's own parser.

No configuration file. No rule language. One command.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import sys
import textwrap

__version__ = "1.0.0"

# Placeholders we substitute into the pattern before handing it to ast.parse.
# Python identifiers cannot contain '$', so these can never collide with code
# the user wrote in the pattern itself.
SINGLE_PREFIX = "__sf_s_"
MULTI_PREFIX = "__sf_m_"
ANON_NAME = "__sf_anon__"

# Fields that carry no meaning for a structural comparison. `ctx` is Load/Store
# bookkeeping; `type_comment` comes from comments.
SKIP_FIELDS = frozenset({"ctx", "type_comment"})

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "build", "dist", ".eggs", "site-packages", ".idea", ".vscode",
})


class PatternError(Exception):
    """The pattern could not be understood as Python code."""


# ---------------------------------------------------------------------------
# Metavariable handling
# ---------------------------------------------------------------------------

_META_RE = re.compile(r"\$\$([A-Za-z_]\w*)|\$([A-Za-z_]\w*)|(\$_)")


def mask_metavars(text):
    """Replace $X / $$X / $_ with valid Python identifiers.

    String literals and comments are left alone, so a pattern like
    ``re.match(r"$X", s)`` keeps its dollar sign where it is literal text.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        if ch == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(text[i:j])
            i = j
            continue

        if ch in "\"'":
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
            continue

        if ch == "$":
            m = _META_RE.match(text, i)
            if m:
                multi, single, anon = m.groups()
                if anon:
                    out.append(ANON_NAME)
                elif multi:
                    out.append(MULTI_PREFIX + multi)
                else:
                    out.append(SINGLE_PREFIX + single)
                i = m.end()
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def _skip_string(text, start):
    """Return the index just past the string literal starting at `start`."""
    quote = text[start]
    triple = text[start:start + 3] in ("'''", '"""')
    delim = text[start:start + 3] if triple else quote
    i = start + len(delim)
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if text.startswith(delim, i):
            return i + len(delim)
        if c == "\n" and not triple:
            return i  # unterminated single-line string; stop at the newline
        i += 1
    return n


def meta_of_name(ident):
    """Placeholder check for identifier-shaped fields (`ClassDef.name`, `arg.arg`)."""
    if not isinstance(ident, str):
        return None
    if ident == ANON_NAME:
        return (None, False)
    if ident.startswith(MULTI_PREFIX):
        return (ident[len(MULTI_PREFIX):], True)
    if ident.startswith(SINGLE_PREFIX):
        return (ident[len(SINGLE_PREFIX):], False)
    return None


def meta_of(node):
    """Return (name, is_multi) when `node` is a placeholder, else None."""
    if isinstance(node, ast.Expr):
        node = node.value
    if isinstance(node, ast.arg):
        # A parameter is a node, but its name lives in a string field.
        return meta_of_name(node.arg)
    if not isinstance(node, ast.Name):
        return None
    return meta_of_name(node.id)


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------

# A pattern like `return $X` is not a legal module, but it is a legal function
# body. Instead of making the user declare a context and pick a selector out of
# it, we retry the parse inside each of these wrappers, in order.
# (header, indent the pattern?, attribute of the wrapper node to keep)
_WRAPPERS = (
    ("def __sf_wrap__():\n", True, "body"),
    ("async def __sf_wrap__():\n", True, "body"),
    ("class __sf_wrap__:\n", True, "body"),
    ("while True:\n", True, "body"),
    ("try:\n    pass\n", False, "handlers"),
)


class Pattern:
    """A compiled pattern: either a single node or a run of statements."""

    __slots__ = ("kind", "node", "stmts", "source", "names")

    def __init__(self, kind, node=None, stmts=None, source=""):
        self.kind = kind          # "node" | "seq"
        self.node = node
        self.stmts = stmts
        self.source = source
        self.names = set()

    def describe(self):
        if self.kind == "node":
            return ast.dump(self.node, annotate_fields=False)[:70]
        return "%d statements" % len(self.stmts)


def _indent_block(text):
    return "\n".join("    " + line if line.strip() else line
                     for line in text.split("\n"))


def compile_pattern(text):
    """Turn a pattern string into a Pattern, trying several parse contexts."""
    if not text or not text.strip():
        raise PatternError("the pattern is empty — pass something like 'print($X)'")

    masked = mask_metavars(text)
    errors = []

    try:
        module = ast.parse(masked)
    except SyntaxError as exc:
        errors.append(exc)
    else:
        return _from_statements(module.body, text)

    for header, indent, attr in _WRAPPERS:
        body = _indent_block(masked) if indent else masked
        try:
            module = ast.parse(header + body)
        except SyntaxError as exc:
            errors.append(exc)
            continue
        holder = getattr(module.body[0], attr, None)
        if holder:
            return _from_statements(holder, text)

    last = errors[0] if errors else None
    where = ""
    if last is not None:
        where = " (%s at line %s of the pattern)" % (last.msg, last.lineno)
    raise PatternError(
        "the pattern is not valid Python%s.\n"
        "A pattern must be one complete statement or expression, e.g.\n"
        "  print($X)\n"
        "  if $COND:\n"
        "      $BODY\n"
        "Metavariables are $NAME (one node) and $$NAME (a run of nodes)."
        % where
    )


def _from_statements(body, text):
    if not body:
        raise PatternError("the pattern contains no code")
    if len(body) == 1:
        stmt = body[0]
        node = stmt.value if isinstance(stmt, ast.Expr) else stmt
        pat = Pattern("node", node=node, source=text)
    else:
        pat = Pattern("seq", stmts=body, source=text)
    _collect_names(pat)
    return pat


def _collect_names(pat):
    roots = [pat.node] if pat.kind == "node" else pat.stmts
    for root in roots:
        for node in ast.walk(root):
            for field, value in ast.iter_fields(node):
                meta = None
                if isinstance(value, str):
                    meta = meta_of_name(value)
                elif isinstance(value, ast.AST):
                    meta = meta_of(value)
                if meta and meta[0]:
                    pat.names.add(meta[0])


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

def _norm(node):
    """A hashable, ctx-free structural fingerprint used to compare captures."""
    if isinstance(node, ast.AST):
        parts = [type(node).__name__]
        for field in node._fields:
            if field in SKIP_FIELDS:
                continue
            parts.append((field, _norm(getattr(node, field, None))))
        return tuple(parts)
    if isinstance(node, list):
        return tuple(_norm(item) for item in node)
    if node is None:
        return None
    # Guard against True == 1: keep the type in the fingerprint.
    return (type(node).__name__, node)


def _same(a, b):
    return _norm(a) == _norm(b)


def match_node(pat, tgt, caps):
    """Try to match `pat` against `tgt`, filling `caps`. Rolls back on failure."""
    snapshot = dict(caps)
    if _match_node(pat, tgt, caps):
        return True
    caps.clear()
    caps.update(snapshot)
    return False


def _match_node(pat, tgt, caps):
    meta = meta_of(pat)
    if meta is not None:
        name, multi = meta
        if multi:
            # A run-of-nodes placeholder only means something inside a list.
            return False
        if name is None:
            return True
        if name in caps:
            return _same(caps[name][0], tgt)
        caps[name] = [tgt]
        return True

    if type(pat) is not type(tgt):
        return False

    if isinstance(pat, ast.Constant):
        # `True` and `1` are equal in Python; structurally they are not.
        return type(pat.value) is type(tgt.value) and pat.value == tgt.value

    if isinstance(pat, ast.arguments) and _signature_has_multi(pat):
        # A `$$ARGS` in a signature stands for the whole parameter area, so the
        # parallel defaults/vararg/kwarg lists are not compared separately.
        for field in ("posonlyargs", "args", "kwonlyargs"):
            constrained = getattr(pat, field)
            if constrained and _match_seq(constrained, getattr(tgt, field), caps) < 0:
                return False
        return True

    for field in pat._fields:
        if field in SKIP_FIELDS:
            continue
        pv = getattr(pat, field, None)
        tv = getattr(tgt, field, None)
        if isinstance(pv, list):
            if not isinstance(tv, list):
                return False
            if _match_seq(pv, tv, caps) < 0:
                return False
        elif isinstance(pv, ast.AST):
            if not isinstance(tv, ast.AST) or not _match_node(pv, tv, caps):
                return False
        else:
            meta = meta_of_name(pv)
            if meta is not None:
                name, multi = meta
                if multi:
                    return False
                if name is None:
                    continue
                if name in caps:
                    if not _same(caps[name][0], tv):
                        return False
                else:
                    # Identifier fields hold plain strings, not AST nodes.
                    caps[name] = [tv]
                continue
            if pv != tv:
                return False
    return True


def _signature_has_multi(args):
    for field in ("posonlyargs", "args", "kwonlyargs"):
        for item in getattr(args, field, []):
            meta = meta_of(item)
            if meta is not None and meta[1]:
                return True
    return False


def _match_seq(pats, tgts, caps, exact=True):
    """Match a list of pattern nodes against a list of target nodes.

    Returns how many target nodes were consumed, or -1 when nothing matched.
    `exact=False` allows the target list to have leftover nodes at the end,
    which is what a multi-statement pattern needs.
    """
    if not pats:
        if exact and tgts:
            return -1
        return 0

    head = pats[0]
    meta = meta_of(head)
    if meta is not None and meta[1]:
        name = meta[0]
        for take in range(len(tgts) + 1):
            snapshot = dict(caps)
            chunk = tgts[:take]
            ok = True
            if name is not None:
                if name in caps:
                    prev = caps[name]
                    ok = (len(prev) == take
                          and all(_same(a, b) for a, b in zip(prev, chunk)))
                else:
                    caps[name] = list(chunk)
            if ok:
                rest = _match_seq(pats[1:], tgts[take:], caps, exact)
                if rest >= 0:
                    return take + rest
            caps.clear()
            caps.update(snapshot)
        return -1

    if not tgts:
        return -1
    if _match_node(head, tgts[0], caps):
        rest = _match_seq(pats[1:], tgts[1:], caps, exact)
        if rest >= 0:
            return 1 + rest
    return -1


class Match:
    __slots__ = ("node", "caps", "start_node", "end_node", "kind")

    def __init__(self, node, caps, start_node=None, end_node=None, kind="node"):
        self.node = node
        self.caps = caps
        self.start_node = start_node or node
        self.end_node = end_node or node
        self.kind = kind


def find_matches(tree, pattern):
    """Every place in `tree` where `pattern` matches, in source order."""
    found = []
    if pattern.kind == "node":
        for node in ast.walk(tree):
            caps = {}
            if match_node(pattern.node, node, caps):
                found.append(Match(node, caps))
    else:
        for node in ast.walk(tree):
            for field in node._fields:
                value = getattr(node, field, None)
                if not isinstance(value, list) or not value:
                    continue
                if not isinstance(value[0], ast.stmt):
                    continue
                for start in range(len(value)):
                    caps = {}
                    consumed = _match_seq(pattern.stmts, value[start:], caps,
                                          exact=False)
                    if consumed > 0:
                        chunk = value[start:start + consumed]
                        found.append(Match(chunk[0], caps, chunk[0], chunk[-1],
                                           kind="seq"))

    seen = set()
    unique = []
    for item in found:
        key = (_start_pos(item.start_node), _end_pos(item.end_node))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda m: (_start_pos(m.start_node), _end_pos(m.end_node)))
    return unique


# ---------------------------------------------------------------------------
# Source positions and rewriting
# ---------------------------------------------------------------------------

class SourceText:
    """Line table for one file, with byte-column to character-column fixes."""

    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.starts = []
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line) + 1

    def offset(self, lineno, col):
        """Absolute character offset for an ast (line, byte-column) pair."""
        index = max(0, min(lineno - 1, len(self.lines) - 1))
        line = self.lines[index]
        if col and not line.isascii():
            raw = line.encode("utf-8")[:col]
            col = len(raw.decode("utf-8", "ignore"))
        return self.starts[index] + col

    def span(self, node):
        start = self.offset(node.lineno, node.col_offset)
        end = self.offset(node.end_lineno, node.end_col_offset)
        return start, end

    def segment(self, node):
        start, end = self.span(node)
        return self.text[start:end]

    def line_indent(self, node):
        """The whitespace the matched code sits under, for re-indenting."""
        line = self.lines[node.lineno - 1]
        stripped = line.lstrip(" \t")
        if len(stripped) == len(line):
            return ""
        leading = len(line) - len(stripped)
        # A match that starts mid-line keeps its own column instead.
        col = node.col_offset
        return line[:col] if col > leading else line[:leading]


def _start_pos(node):
    return (node.lineno, node.col_offset)


def _end_pos(node):
    return (node.end_lineno, node.end_col_offset)


_TEMPLATE_RE = re.compile(r"\$\$([A-Za-z_]\w*)|\$([A-Za-z_]\w*)")


def _text_of(item, source):
    """Source text for a capture, which is an AST node or a bare identifier."""
    if isinstance(item, str):
        return item
    text = source.segment(item)
    if isinstance(item, ast.stmt) and "\n" in text:
        # A multi-line statement carries the indentation of its own lines.
        # Strip it, so the caller can re-indent from a known baseline.
        text = _strip_indent(text, source.line_indent(item))
    return text


def _strip_indent(text, indent):
    if not indent:
        return text
    lines = text.split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        out.append(line[len(indent):] if line.startswith(indent) else line)
    return "\n".join(out)


def _join_capture(nodes, source):
    """Render a multi-node capture the way it would be written in code."""
    if not nodes:
        return ""
    separator = "\n" if isinstance(nodes[0], ast.stmt) else ", "
    return separator.join(_text_of(n, source) for n in nodes)


def render_template(template, caps, source):
    """Substitute captured source text into a replacement template."""
    def replace(m):
        multi, single = m.groups()
        name = multi or single
        nodes = caps.get(name)
        if not nodes:
            return m.group(0) if not multi else ""
        if multi:
            text = _join_capture(nodes, source)
        else:
            text = _text_of(nodes[0], source)
        if "\n" in text:
            # Keep a multi-line capture aligned under the column it lands in,
            # otherwise the second line of a nested block jumps to the left.
            column = m.start() - (template.rfind("\n", 0, m.start()) + 1)
            if column:
                pad = " " * column
                text = "\n".join(
                    [text.split("\n")[0]] +
                    [pad + line if line.strip() else line
                     for line in text.split("\n")[1:]]
                )
        return text

    return _TEMPLATE_RE.sub(replace, template)


def reindent(text, indent):
    """Push every continuation line of `text` to the match's indentation."""
    if "\n" not in text or not indent:
        return text
    lines = text.split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        out.append(indent + line if line.strip() else line)
    return "\n".join(out)


def build_edits(matches, template, source):
    """Turn matches into (start, end, new_text) edits, innermost first."""
    edits = []
    for item in matches:
        start, end = source.span(item.start_node)
        _, end = start, source.span(item.end_node)[1]
        body = render_template(template, item.caps, source)
        body = reindent(textwrap.dedent(body), source.line_indent(item.start_node))
        edits.append((start, end, body))
    # Apply from the end so earlier offsets stay valid.
    edits.sort(key=lambda e: (e[0], e[1]), reverse=True)
    kept = []
    last_start = None
    for edit in edits:
        if last_start is not None and edit[1] > last_start:
            continue  # overlaps a later edit that already covers it
        kept.append(edit)
        last_start = edit[0]
    return kept


def apply_edits(text, edits):
    out = text
    for start, end, body in edits:
        out = out[:start] + body + out[end:]
    return out


# ---------------------------------------------------------------------------
# File discovery and output
# ---------------------------------------------------------------------------

def iter_files(paths, glob_pattern="*.py", hidden=False):
    for path in paths:
        if os.path.isfile(path):
            yield path
            continue
        if not os.path.isdir(path):
            sys.stderr.write("shapefind: no such file or directory: %s\n" % path)
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(
                d for d in dirs
                if d not in SKIP_DIRS and (hidden or not d.startswith("."))
            )
            for name in sorted(files):
                if _match_glob(name, glob_pattern):
                    yield os.path.join(root, name)


def _match_glob(name, pattern):
    import fnmatch
    if pattern in ("*", "*.*"):
        return True
    return fnmatch.fnmatch(name, pattern)


def read_source(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def snippet(source, item):
    start, end = source.span(item.start_node)
    _, end = start, source.span(item.end_node)[1]
    return source.text[start:end]


def format_text(path, source, item):
    start_line = item.start_node.lineno
    col = item.start_node.col_offset + 1
    body = snippet(source, item)
    lines = body.split("\n")
    head = "%s:%d:%d: %s" % (path, start_line, col, lines[0])
    if len(lines) == 1:
        return head
    tail = ["        " + line for line in lines[1:]]
    return "\n".join([head] + tail)


def format_json(path, source, item):
    return {
        "path": path,
        "line": item.start_node.lineno,
        "column": item.start_node.col_offset + 1,
        "text": snippet(source, item),
        "captures": {name: _join_capture(nodes, source)
                     for name, nodes in sorted(item.caps.items())},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="shapefind",
        description="Find Python code by shape. Comments, spacing and "
                    "parentheses never affect a match.",
        epilog="examples:\n"
               "  shapefind 'print($X)' .\n"
               "  shapefind 'except: pass' src/\n"
               "  shapefind 'open($F)' --rewrite 'Path($F).open()' --diff\n"
               "  shapefind 'def $F($$ARGS): $$BODY' --count\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pattern", help="Python code with $NAME / $$NAME wildcards")
    parser.add_argument("paths", nargs="*", default=["."],
                        help="files or directories (default: current directory)")
    parser.add_argument("--rewrite", metavar="TEMPLATE",
                        help="replacement text, may reference captured names")
    parser.add_argument("--write", action="store_true",
                        help="apply the rewrite in place")
    parser.add_argument("--diff", action="store_true",
                        help="show the rewrite as a unified diff, change nothing")
    parser.add_argument("--count", action="store_true", help="print match counts")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when anything matches (for CI)")
    parser.add_argument("--glob", default="*.py", metavar="PATTERN",
                        help="file name pattern (default: *.py)")
    parser.add_argument("--hidden", action="store_true", help="descend into dot-dirs")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="stop after N matches")
    parser.add_argument("--version", action="version",
                        version="shapefind " + __version__)
    return parser


def run(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        pattern = compile_pattern(args.pattern)
    except PatternError as exc:
        sys.stderr.write("shapefind: %s\n" % exc)
        return 2

    if args.write and not args.rewrite:
        parser.error("--write needs --rewrite")

    total = 0
    changed_files = 0
    skipped = 0
    per_file = []

    for path in iter_files(args.paths or ["."], args.glob, args.hidden):
        try:
            text = read_source(path)
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            skipped += 1
            continue
        except OSError as exc:
            sys.stderr.write("shapefind: cannot read %s: %s\n" % (path, exc))
            continue

        source = SourceText(text)
        matches = find_matches(tree, pattern)
        if not matches:
            continue
        if args.limit and total + len(matches) > args.limit:
            matches = matches[:max(0, args.limit - total)]

        per_file.append((path, len(matches)))
        total += len(matches)

        if args.rewrite and (args.write or args.diff):
            edits = build_edits(matches, args.rewrite, source)
            new_text = apply_edits(text, edits)
            if new_text == text:
                continue
            if args.diff:
                diff = difflib.unified_diff(
                    text.splitlines(True), new_text.splitlines(True),
                    fromfile=path, tofile=path,
                )
                sys.stdout.write("".join(diff))
            if args.write:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(new_text)
                changed_files += 1
            continue

        if args.json:
            for item in matches:
                print(json.dumps(format_json(path, source, item), ensure_ascii=False))
        elif not args.count:
            for item in matches:
                print(format_text(path, source, item))

        if args.limit and total >= args.limit:
            break

    if args.count:
        for path, count in per_file:
            print("%s:%d" % (path, count))
        if len(per_file) > 1:
            print("total:%d" % total)
        elif not per_file:
            print("total:0")

    if args.write and changed_files:
        sys.stderr.write("shapefind: rewrote %d file(s)\n" % changed_files)
    if skipped:
        sys.stderr.write("shapefind: skipped %d file(s) that do not parse\n" % skipped)

    if args.check:
        return 1 if total else 0
    return 0


def main():
    try:
        sys.exit(run())
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
