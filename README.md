# shapefind

**Search Python code by its shape, not by its text.**

`grep` finds the letters `print(`. `shapefind` finds calls to `print` — not the ones in comments, not `pprint`, not the ones inside a string. You write the pattern as ordinary Python, with `$NAME` standing in for the part you do not care about.

```bash
python shapefind.py 'print($X)' .
```

```
src/app.py:12:5: print(user)
src/app.py:40:9: print ( "retry" )
```

No config file. No rule language. No `strictness` knob. One file, standard library only.

---

## Install

Nothing to install. Copy `shapefind.py` anywhere, or clone the repo.

```bash
git clone https://github.com/z15346183030/shapefind
cd shapefind
python shapefind.py --version
```

Python 3.9+. Zero dependencies.

---

## Metavariables

| Write | Matches | Captured as |
|---|---|---|
| `$X` | one node — an expression, a statement, a name | `X` |
| `$$X` | a run of nodes — arguments, statements | `X` |
| `$_` | one node, forgotten | — |

A name used twice must match the same shape twice:

```bash
python shapefind.py '$X == $X' .
```

finds `a == a` and `a == (a)`, but not `a == b`.

---

## Examples

### 1. Find every bare `except: pass`

```bash
python shapefind.py 'except: pass' src/
```

`except` is not a legal module on its own, so the pattern is retried inside a `try` block automatically. You do not declare a context, and you do not pick a selector out of it.

### 2. Find functions that swallow their arguments

```bash
python shapefind.py 'def $F($$ARGS): $$BODY' --json .
```

```json
{"path": "demo.py", "line": 4, "column": 1,
 "text": "def f(a, b=2):\n    print(a)\n    return a + b",
 "captures": {"ARGS": "a, b", "BODY": "print(a)\nreturn a + b", "F": "f"}}
```

### 3. Rename a call everywhere, indentation intact

```bash
python shapefind.py 'print($$ARGS)' --rewrite 'debug($$ARGS)' --diff .
```

```diff
--- app.py
+++ app.py
@@ -1,3 +1,3 @@
 def main():
-    print("start", 1)
+    debug("start", 1)
```

Add `--write` to apply it, drop `--diff` to stay quiet. Files are rewritten in place and the diff is shown first if you ask for both.

### 4. Fail the build when a pattern appears

```bash
python shapefind.py 'eval($X)' --check .
```

Exit code 1 when anything matches, 0 when nothing does. That is the whole linter.

### 5. Rewrite a nested class without breaking its indentation

```bash
python shapefind.py 'class $C($$BASES): $$BODY' \
  --rewrite 'class $C($$BASES):
    """Generated wrapper."""
    batch_mode = True
    $$BODY' --diff .
```

```diff
 if something:
     class B(A):
+        """Generated wrapper."""
+        batch_mode = True
         def replace(self, *args, **kwargs):
             return None
```

The `return None` keeps its depth. Every continuation line of a capture is re-indented to where it landed, so a three-level nest stays a three-level nest.

---

## Options

| Flag | Meaning |
|---|---|
| `--rewrite TEXT` | replacement text, may use `$X` / `$$X` |
| `--write` | apply the rewrite in place |
| `--diff` | show the rewrite as a unified diff |
| `--count` | one line per file with a match, plus a total |
| `--json` | one JSON object per match, captures included |
| `--check` | exit 1 if anything matches |
| `--glob PATTERN` | file name filter, default `*.py` |
| `--hidden` | descend into dot-directories |
| `--limit N` | stop after N matches |

`.git`, `__pycache__`, `.venv`, `node_modules`, `site-packages`, `build`, `dist` and friends are skipped by default. Files that do not parse are skipped with a count on stderr — one broken file never aborts a scan.

---

## What a match ignores

Anything the Python parser throws away:

- comments and blank lines
- spacing, line breaks inside brackets
- parentheses around an expression
- quote style: `'a'` and `"a"` are the same node

And anything that is not structure:

- `True` is not `1` (they compare equal in Python, but they are different constants)
- `print(a)` is not `print(a, b)` — `$X` is one node, `$$X` is a run
- `print(a)` is not `print(a, sep="")` — keywords are compared, not ignored

The last two are the price of having no knobs. If you want both, write the pattern that describes both: `print($$ARGS)`.

---

## Limitations

- **Python only.** The parser is `ast`; there is no tree-sitter behind it.
- **`else:` and `finally:` blocks** cannot be a pattern on their own. `except:` can.
- **A `$$` in a signature** stands for the whole parameter area, so defaults and `*args` / `**kwargs` are not compared separately.
- **Nested matches are all reported.** `def $F(): $$BODY` matches the outer function and every function inside it.

---

## Tests

```bash
python -m unittest discover -s tests
```

41 cases, no third-party test runner.

---

## Measured

Against the Python 3.13 standard library — 920 files, 386,762 lines, `site-packages` excluded:

| Query | Result |
|---|---|
| `grep -rn 'print('` | 970 lines |
| `shapefind 'print($X)'` | 246 matches (one argument, no keywords) |
| `shapefind 'print($$ARGS)'` | 368 matches (any argument count, no keywords) |
| scan time | 3.7 s for 920 files |

The gap between 970 and 368 is the point: comments, `pprint`, docstrings and `print` inside string literals are not calls.

## License

MIT. See [LICENSE](LICENSE). Background and design notes in [ANALYSIS.md](ANALYSIS.md).
