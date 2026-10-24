import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import under_the_bed as utb  # noqa: E402


def filediff(path, added=(), removed=(), start=10):
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
             f"@@ -{start},{len(removed) + 1} +{start},{len(added) + 1} @@"]
    lines += [" context line"] + [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


DIFF = (filediff("billing/invoice.py", ["    return amount + tax_for(region, amount) * rate"],
                 ["    return amount + tax_for(region, amount)"])
        + filediff("tests/test_invoice.py", ["    assert total(LINES, 'EU') == 121"])
        + filediff("billing/currency.py", ["ROUND = 3"], ["ROUND = 2"])
        + filediff("README.md", ["Now with EU tax."]))


def scope(task, diff=DIFF):
    return [f for f in utb.scan(diff, task=task) if f.kind.startswith("out-of-scope") or f.kind == "scope-unclear"]


class ScopeTest(unittest.TestCase):
    def test_files_the_task_never_mentions_are_questioned(self):
        got = scope("Fix the EU tax in `tax_for` (billing/invoice.py).")
        self.assertEqual(sorted(f.file for f in got), ["README.md", "billing/currency.py"])
        self.assertTrue(all(f.kind == "out-of-scope" for f in got))
        self.assertIn("did not ask", got[0].question)

    def test_an_identifier_named_in_the_task_puts_its_hunk_in_scope(self):
        got = scope("tax_for() returns the wrong value for EU")
        self.assertNotIn("billing/invoice.py", [f.file for f in got])

    def test_the_function_named_in_the_hunk_header_counts(self):
        d = ("diff --git a/b/inv.py b/b/inv.py\n--- a/b/inv.py\n+++ b/b/inv.py\n"
             "@@ -10,3 +10,3 @@ def total(lines, region):\n     amount = 1\n-    return round(a, 1)\n+    return round(a, 2)\n"
             + filediff("config/settings.py", ["TIMEOUT = 60"], ["TIMEOUT = 5"]))
        got = scope("Fix the rounding in total()", d)
        self.assertEqual([f.file for f in got], ["config/settings.py"])

    def test_an_identifier_matches_whole_words_only(self):
        d = filediff("a/cart.py", ["    return total(items)"]) + filediff("b/report.py", ["subtotal = 3"])
        got = scope("`total` is wrong for empty carts", d)
        self.assertEqual([f.file for f in got], ["b/report.py"])

    def test_tests_of_an_in_scope_file_are_in_scope(self):
        got = scope("fix billing/invoice.py")
        self.assertNotIn("tests/test_invoice.py", [f.file for f in got])

    def test_a_plain_word_matching_a_path_part_counts(self):
        got = scope("update the readme and fix tax_for")
        self.assertNotIn("README.md", [f.file for f in got])

    def test_a_task_that_names_nothing_in_the_diff_gives_one_honest_line(self):
        got = scope("make it faster please")
        self.assertEqual([f.kind for f in got], ["scope-unclear"])

    def test_no_task_no_scope_findings(self):
        self.assertEqual([f for f in utb.scan(DIFF) if "scope" in f.kind], [])

    def test_a_folder_named_in_the_task_covers_its_files(self):
        d = filediff("polyglot/cards.py", ["x = 1"]) + filediff("polyglot/README.md", ["hi"]) + filediff("other/a.py", ["y"])
        got = scope("polyglot: spaced review for homework words", d)
        self.assertEqual([f.file for f in got], ["other/a.py"])

    def test_test_helpers_follow_their_tests(self):
        d = (filediff("herald/telegram.py", ["x = 1"]) + filediff("tests/test_telegram.py", ["t"])
             + filediff("tests/fake_api.py", ["f"]) + filediff("herald/voice.py", ["v"]))
        got = scope("telegram: hear only owners", d)
        self.assertEqual([f.file for f in got], ["herald/voice.py"])

    def test_words_match_parts_of_file_names(self):
        d = (filediff("config.example.json", ["{}"]) + filediff("pixel_snap.py", ["x"])
             + filediff("tests/test_pixel_snap.py", ["t"]) + filediff("io/a.py", ["y"]))
        got = scope("pixel-snap: read the example config", d)
        self.assertEqual([f.file for f in got], ["io/a.py"])

    def test_a_weak_match_alone_does_not_accuse_the_rest(self):
        # a word matching part of a test file name is not enough to flag the real source as unasked
        d = (filediff("tests/test_baseline.py", ["t"]) + filediff("under_the_bed.py", ["x"]) + filediff("README.md", ["r"]))
        got = scope("--baseline: hide findings you already acknowledged", d)
        self.assertEqual([f.kind for f in got], ["scope-unclear"])

    def test_stopwords_do_not_match_paths(self):
        d = filediff("fix/the/thing.py", ["x = 1"]) + filediff("billing/invoice.py", ["y = 2"])
        got = scope("fix the bug in billing/invoice.py", d)
        self.assertEqual([f.file for f in got], ["fix/the/thing.py"])


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d.diff")
            with open(p, "w", encoding="utf-8") as f:
                f.write(DIFF)
            task = os.path.join(t, "TASK.md")
            with open(task, "w", encoding="utf-8") as f:
                f.write("Fix the EU tax in tax_for, billing/invoice.py\n")
            args = [a.replace("TASKFILE", task) for a in args]
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "under_the_bed.py"),
                                "--diff", p, "--json", *args], capture_output=True, text=True, encoding="utf-8")
            return json.loads(r.stdout)

    def test_task_and_task_file(self):
        for args in (["--task", "Fix the EU tax in tax_for, billing/invoice.py"], ["--task-file", "TASKFILE"]):
            with self.subTest(args=args[0]):
                out = self.run_cli(*args)
                files = sorted(f["file"] for f in out["findings"] if f["kind"] == "out-of-scope")
                self.assertEqual(files, ["README.md", "billing/currency.py"])


if __name__ == "__main__":
    unittest.main()
