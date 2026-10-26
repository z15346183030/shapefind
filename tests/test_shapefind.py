"""Tests for shapefind. Run with: python -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import shapefind  # noqa: E402

CLI = os.path.join(ROOT, "shapefind.py")


def run_cli(args, cwd=None):
    proc = subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True, text=True, cwd=cwd or ROOT,
    )
    return proc.returncode, proc.stdout, proc.stderr


class Workspace:
    """A throwaway directory holding source files for one test."""

    def __init__(self, files):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for name, text in files.items():
            path = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)

    def path(self, name):
        return os.path.join(self.root, name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8", newline="") as handle:
            return handle.read()

    def cleanup(self):
        self.tmp.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace({
            "demo.py": (
                "# print(x) lives in this comment\n"
                "import os\n"
                "\n"
                "def f(a, b=2):\n"
                "    print(a)\n"
                "    print ( b )\n"
                "    sprint(a)\n"
                "    return a + b\n"
                "\n"
                "class K:\n"
                "    class Inner:\n"
                "        def g(self):\n"
                "            print('nested')\n"
                "\n"
                "def h():\n"
                "    try:\n"
                "        pass\n"
                "    except ValueError:\n"
                "        pass\n"
            ),
        })

    def tearDown(self):
        self.ws.cleanup()

    def search(self, pattern, *args):
        code, out, err = run_cli([pattern] + list(args) + [self.ws.root])
        self.assertEqual(code, 0, err)
        return out

    def lines(self, pattern, *args):
        return [line for line in self.search(pattern, *args).splitlines() if line]


class TestMatchingIgnoresFormatting(Base):
    def test_comment_is_not_code(self):
        out = self.search("print($X)")
        self.assertNotIn("comment", out)
        self.assertEqual(len(self.lines("print($X)")), 3)

    def test_spacing_does_not_matter(self):
        out = self.search("print($X)")
        self.assertIn("print ( b )", out)

    def test_parentheses_do_not_matter(self):
        ws = Workspace({"p.py": "f((1))\nf(1)\n"})
        try:
            code, out, _ = run_cli(["f(1)", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 2)
        finally:
            ws.cleanup()

    def test_quote_style_does_not_matter(self):
        ws = Workspace({"q.py": "print('a')\nprint(\"a\")\nprint('b')\n"})
        try:
            code, out, _ = run_cli(["print('a')", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 2)
        finally:
            ws.cleanup()

    def test_similar_name_is_not_a_match(self):
        out = self.search("print($X)")
        self.assertNotIn("sprint", out)


class TestMetavariables(Base):
    def test_single_capture(self):
        out = self.search("print($X)", "--json")
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(records[0]["captures"]["X"], "a")

    def test_repeated_name_must_match_structurally(self):
        ws = Workspace({"r.py": "a == a\nb == c\nx == (x)\n"})
        try:
            code, out, _ = run_cli(["$L == $L", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 2)
        finally:
            ws.cleanup()

    def test_anonymous_wildcard(self):
        ws = Workspace({"a.py": "f(1)\nf(2)\n"})
        try:
            code, out, _ = run_cli(["f($_)", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 2)
        finally:
            ws.cleanup()

    def test_multi_capture_in_call(self):
        ws = Workspace({"m.py": "f()\nf(1)\nf(1, 2, 3)\n"})
        try:
            code, out, _ = run_cli(["f($$ARGS)", "--json", ws.root])
            self.assertEqual(code, 0)
            records = [json.loads(line) for line in out.splitlines()]
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["captures"]["ARGS"], "")
            self.assertEqual(records[2]["captures"]["ARGS"], "1, 2, 3")
        finally:
            ws.cleanup()

    def test_multi_capture_in_body(self):
        out = self.search("def $F($$ARGS): $$BODY", "--json")
        record = json.loads(out.splitlines()[0])
        self.assertEqual(record["captures"]["F"], "f")
        self.assertEqual(record["captures"]["ARGS"], "a, b")
        self.assertTrue(record["captures"]["BODY"].startswith("print(a)"))

    def test_identifier_position_function_name(self):
        out = self.search("def $F($$ARGS): $$BODY", "--json")
        names = [json.loads(l)["captures"]["F"] for l in out.splitlines()]
        self.assertEqual(names, ["f", "g", "h"])

    def test_identifier_position_class_name(self):
        ws = Workspace({"c.py": "class Alpha:\n    pass\nclass Beta:\n    pass\n"})
        try:
            code, out, _ = run_cli(["class $C: $$BODY", "--json", ws.root])
            self.assertEqual(code, 0)
            names = [json.loads(l)["captures"]["C"] for l in out.splitlines()]
            self.assertEqual(names, ["Alpha", "Beta"])
        finally:
            ws.cleanup()

    def test_attribute_name(self):
        ws = Workspace({"s.py": "self.value = 1\nself.other = 2\nx.value = 3\n"})
        try:
            code, out, _ = run_cli(["self.$ATTR = $V", "--json", ws.root])
            self.assertEqual(code, 0)
            attrs = [json.loads(l)["captures"]["ATTR"] for l in out.splitlines()]
            self.assertEqual(attrs, ["value", "other"])
        finally:
            ws.cleanup()

    def test_metavar_in_string_is_literal(self):
        ws = Workspace({"t.py": "re.match('$X', s)\nre.match('zzz', s)\n"})
        try:
            code, out, _ = run_cli(["re.match('$X', s)", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 1)
        finally:
            ws.cleanup()


class TestConstants(unittest.TestCase):
    def test_true_is_not_one(self):
        ws = Workspace({"v.py": "f(True)\nf(1)\n"})
        try:
            code, out, _ = run_cli(["f(True)", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 1)
        finally:
            ws.cleanup()


class TestPatternContexts(Base):
    def test_return_needs_no_context_declaration(self):
        out = self.search("return $A + $B", "--json")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(json.loads(out)["captures"]["A"], "a")

    def test_except_handler(self):
        out = self.search("except $E: $$BODY", "--json")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(json.loads(out)["captures"]["E"], "ValueError")

    def test_if_pattern(self):
        ws = Workspace({"i.py": "if x:\n    pass\nwhile x:\n    pass\n"})
        try:
            code, out, _ = run_cli(["if $C: pass", "--count", ws.root])
            self.assertEqual(code, 0)
            self.assertIn("i.py:1", out)
        finally:
            ws.cleanup()

    def test_sequence_pattern(self):
        ws = Workspace({
            "s.py": "def a():\n    x = 1\n    y = 2\n    return x\ndef b():\n    y = 2\n",
        })
        try:
            code, out, _ = run_cli(["$A = 1\n$B = 2", ws.root])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 2)
            self.assertIn("x = 1", out)
        finally:
            ws.cleanup()

    def test_bad_pattern_message(self):
        code, out, err = run_cli(["def (", self.ws.root])
        self.assertEqual(code, 2)
        self.assertIn("not valid Python", err)
        self.assertIn("$NAME", err)


class TestRewrite(unittest.TestCase):
    def test_simple_rewrite(self):
        ws = Workspace({"a.py": "print(x)\n"})
        try:
            code, out, err = run_cli(
                ["print($X)", "--rewrite", "log($X)", "--diff", ws.root])
            self.assertEqual(code, 0, err)
            self.assertIn("-print(x)", out)
            self.assertIn("+log(x)", out)
            self.assertEqual(ws.read("a.py"), "print(x)\n")
        finally:
            ws.cleanup()

    def test_write_in_place(self):
        ws = Workspace({"a.py": "print(x)\nprint(y)\n"})
        try:
            code, _, err = run_cli(
                ["print($X)", "--rewrite", "log($X)", "--write", ws.root])
            self.assertEqual(code, 0, err)
            self.assertEqual(ws.read("a.py"), "log(x)\nlog(y)\n")
            self.assertIn("rewrote 1 file", err)
        finally:
            ws.cleanup()

    def test_write_without_rewrite_is_rejected(self):
        code, _, err = run_cli(["--write", "x"])
        self.assertEqual(code, 2)
        self.assertIn("--write needs --rewrite", err)

    def test_nested_indentation_survives(self):
        """Regression: a rewrite inside a nested class must not jump left."""
        ws = Workspace({"n.py": (
            "if something:\n"
            "    class B(A):\n"
            "        def replace(self, *args, **kwargs):\n"
            "            return None\n"
        )})
        try:
            template = (
                'class $C($$BASES):\n'
                '    """generated"""\n'
                '    batch_mode = True\n'
                '    $$BODY'
            )
            code, out, err = run_cli(
                ["class $C($$BASES): $$BODY", "--rewrite", template,
                 "--diff", ws.root])
            self.assertEqual(code, 0, err)
            self.assertIn("+        \"\"\"generated\"\"\"", out)
            self.assertIn("+        batch_mode = True", out)
            self.assertIn("\n         def replace", out)
            self.assertIn("\n             return None", out)
        finally:
            ws.cleanup()

    def test_multiline_body_keeps_relative_depth(self):
        ws = Workspace({"d.py": (
            "def outer():\n"
            "    x = 1\n"
            "    def inner():\n"
            "        return 1\n"
        )})
        try:
            template = 'def outer():\n    """doc"""\n    $$BODY'
            code, out, err = run_cli(
                ["def outer(): $$BODY", "--rewrite", template, "--diff", ws.root])
            self.assertEqual(code, 0, err)
            self.assertIn('+    """doc"""', out)
            self.assertIn("\n     def inner():", out)
            self.assertIn("\n         return 1", out)
        finally:
            ws.cleanup()

    def test_rewrite_is_idempotent(self):
        ws = Workspace({"i.py": "f(1)\n"})
        try:
            run_cli(["f($X)", "--rewrite", "g($X)", "--write", ws.root])
            first = ws.read("i.py")
            code, out, _ = run_cli(["f($X)", "--rewrite", "g($X)", "--diff", ws.root])
            self.assertEqual(first, "g(1)\n")
            self.assertEqual(out, "")
        finally:
            ws.cleanup()

    def test_non_ascii_column_offsets(self):
        ws = Workspace({"u.py": 's = "中文"; print(1)\n'})
        try:
            code, _, err = run_cli(
                ["print($X)", "--rewrite", "log($X)", "--write", ws.root])
            self.assertEqual(code, 0, err)
            self.assertEqual(ws.read("u.py"), 's = "中文"; log(1)\n')
        finally:
            ws.cleanup()


class TestCli(Base):
    def test_count(self):
        out = self.search("print($X)", "--count")
        self.assertIn("demo.py:3", out)

    def test_check_exit_code(self):
        code, _, _ = run_cli(["print($X)", "--check", self.ws.root])
        self.assertEqual(code, 1)
        code, _, _ = run_cli(["nothing_like_this($X)", "--check", self.ws.root])
        self.assertEqual(code, 0)

    def test_limit(self):
        out = self.search("print($X)", "--limit", "2")
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_json_is_valid_and_ordered(self):
        out = self.search("print($X)", "--json")
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["line"] for r in records], [5, 6, 13])

    def test_syntax_error_file_is_skipped(self):
        ws = Workspace({"ok.py": "print(1)\n", "bad.py": "def (:\n"})
        try:
            code, out, err = run_cli(["print($X)", ws.root])
            self.assertEqual(code, 0)
            self.assertIn("print(1)", out)
            self.assertIn("skipped 1 file", err)
        finally:
            ws.cleanup()

    def test_glob_filter(self):
        ws = Workspace({"a.py": "print(1)\n", "b.txt": "print(1)\n"})
        try:
            code, out, _ = run_cli(["print($X)", "--glob", "*.txt", ws.root])
            self.assertEqual(code, 0)
            self.assertIn("b.txt", out)
            self.assertNotIn("a.py", out)
        finally:
            ws.cleanup()

    def test_hidden_dirs_skipped_by_default(self):
        ws = Workspace({"pkg/a.py": "print(1)\n", ".venv/b.py": "print(1)\n"})
        try:
            code, out, _ = run_cli(["print($X)", ws.root])
            self.assertEqual(code, 0)
            self.assertIn("a.py", out)
            self.assertNotIn(".venv", out)
        finally:
            ws.cleanup()

    def test_single_file_argument(self):
        code, out, _ = run_cli(["print($X)", self.ws.path("demo.py")])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 3)


class TestInternals(unittest.TestCase):
    def test_mask_leaves_strings_alone(self):
        self.assertEqual(shapefind.mask_metavars("f('$X')"), "f('$X')")

    def test_mask_handles_comment(self):
        self.assertEqual(shapefind.mask_metavars("x  # $Y"), "x  # $Y")

    def test_mask_replaces_metavars(self):
        masked = shapefind.mask_metavars("f($A, $$B)")
        self.assertIn("__sf_s_A", masked)
        self.assertIn("__sf_m_B", masked)

    def test_meta_of_name(self):
        self.assertEqual(shapefind.meta_of_name("__sf_s_X"), ("X", False))
        self.assertEqual(shapefind.meta_of_name("__sf_m_X"), ("X", True))
        self.assertIsNone(shapefind.meta_of_name("plain"))

    def test_source_offsets_round_trip(self):
        src = shapefind.SourceText('a = "中"\nb = 2\n')
        start, end = src.span(shapefind.ast.parse('a = "中"\nb = 2\n').body[1])
        self.assertEqual(src.text[start:end], "b = 2")

    def test_reindent_skips_blank_lines(self):
        self.assertEqual(shapefind.reindent("a\n\nb", "  "), "a\n\n  b")


if __name__ == "__main__":
    unittest.main()
