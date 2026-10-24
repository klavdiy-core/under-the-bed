"""--baseline / --write-baseline: findings a person already acknowledged stay hidden; copies still show up."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import under_the_bed as utb  # noqa: E402


def added_file(path, added_lines):
    """A tiny new-file diff whose added lines are exactly added_lines (line numbers start at 1)."""
    body = "".join(f"+{ln}\n" for ln in added_lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added_lines)} @@\n") + body


# two identical copies of the same swallowed exception, in different places of one file
TWO_COPIES = ["try:", "    a()", "except Exception:", "    pass", "x = 1",
              "try:", "    b()", "except Exception:", "    pass"]


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _path(self, name):
        return os.path.join(self.dir, name)

    def _run(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return utb.main(argv)

    def test_without_baseline_both_copies_show(self):
        diff_path = self._path("two.diff")
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(added_file("app.py", TWO_COPIES))
        out = self._path("all.json")
        self._run(["--diff", diff_path, "--json", "-o", out])
        with open(out, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual([x["kind"] for x in rep["findings"]], ["swallowed-exception"] * 2)

    def test_baseline_with_first_copy_keeps_second_copy_visible(self):
        # acknowledge only the first copy
        first_only = self._path("first.diff")
        with open(first_only, "w", encoding="utf-8") as f:
            f.write(added_file("app.py", TWO_COPIES[:4]))
        baseline = self._path("baseline.json")
        self.assertEqual(self._run(["--diff", first_only, "--write-baseline", baseline]), 0)

        full = self._path("two.diff")
        with open(full, "w", encoding="utf-8") as f:
            f.write(added_file("app.py", TWO_COPIES))
        out = self._path("after.json")
        code = self._run(["--diff", full, "--baseline", baseline, "--json", "-o", out])
        with open(out, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual(code, 1)
        self.assertEqual(len(rep["findings"]), 1, rep)
        self.assertEqual(rep["findings"][0]["line"], 8)  # the second copy, not the acknowledged first

    def test_same_snippet_in_another_file_is_a_new_finding(self):
        # reviewer's case: acknowledging `except: pass` in a.py must not hide the same lines in b.py
        two = self._path("two_files.diff")
        with open(two, "w", encoding="utf-8") as f:
            f.write(added_file("b.py", TWO_COPIES[:4]) + added_file("a.py", TWO_COPIES[:4]))
        only_a = self._path("a.diff")
        with open(only_a, "w", encoding="utf-8") as f:
            f.write(added_file("a.py", TWO_COPIES[:4]))
        baseline = self._path("baseline.json")
        self._run(["--diff", only_a, "--write-baseline", baseline])
        out = self._path("files.json")
        self._run(["--diff", two, "--baseline", baseline, "--json", "-o", out])
        with open(out, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual([x["file"] for x in rep["findings"]], ["b.py"])

    def test_baseline_hides_everything_it_recorded(self):
        diff_path = self._path("two.diff")
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(added_file("app.py", TWO_COPIES))
        baseline = self._path("baseline.json")
        self._run(["--diff", diff_path, "--write-baseline", baseline])
        out = self._path("clean.json")
        code = self._run(["--diff", diff_path, "--baseline", baseline, "--json", "-o", out])
        with open(out, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual(rep["findings"], [])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
