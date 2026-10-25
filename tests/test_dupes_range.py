"""Copies: short ones were skipped in silence, and --range compared with today's files, not the commit's.
(An outside re-grade, 25.10: a 2-line make_slug copy of slugify was missed; --range HEAD~2..HEAD~1 matched a
function that only came in a later commit.) Red on 0.4.0."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import under_the_bed as utb  # noqa: E402

SLUGIFY = "def slugify(text):\n    out = text.strip().lower().replace(' ', '-')\n    return out\n"
MAKE_SLUG = "def make_slug(name):\n    res = name.strip().lower().replace(' ', '-')\n    return res\n"
OTHER_SHORT = "def area(w, h):\n    size = w * h\n    return size\n"
LATER = ("def handle_rows(rows):\n    total = 0\n    for row in rows:\n        if row.get('ok'):\n"
         "            total += row['amount'] * row.get('rate', 1)\n        else:\n            total -= 1\n"
         "    return round(total, 2)\n")


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = self.tmp.name
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "t")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", "-C", self.r, *a], capture_output=True, text=True, check=True).stdout

    def commit(self, name, text, msg):
        with open(os.path.join(self.r, name), "a", encoding="utf-8") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", msg)

    def run_range(self, rng):
        out = os.path.join(self.r, "..", f"report-{os.getpid()}.json")
        code = utb.main(["-C", self.r, "--range", rng, "--json", "-o", out])
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        os.remove(out)
        items = data["findings"] if isinstance(data, dict) else data
        return code, [(f["kind"], f.get("snippet", "")) for f in items]

    def test_a_short_renamed_copy_is_found(self):
        self.commit("utils.py", SLUGIFY, "utils")
        self.commit("views.py", MAKE_SLUG, "views")
        _, found = self.run_range("HEAD~1")
        self.assertIn(("duplicate", "def make_slug"), found)

    def test_different_short_functions_are_not_copies(self):
        self.commit("utils.py", SLUGIFY, "utils")
        self.commit("geo.py", OTHER_SHORT, "geo")
        _, found = self.run_range("HEAD~1")
        self.assertNotIn("duplicate", [k for k, _ in found])

    def test_similar_test_cases_are_not_copies_but_copied_helpers_are(self):
        case = ("def test_status_is_free(self):\n    self.h.handle(text('/status'))\n    self.assertEqual(self.calls, [])\n"
                "    self.assertIn('stopped', self.bot.texts[-1][1].lower())\n    self.assertTrue(self.bot.texts)\n")
        helper = "def tap(data, uid=50):\n    return Message(uid, OWNER, 'callback', text=data, callback_id=f'cb{uid}')\n"
        os.makedirs(os.path.join(self.r, "tests"))
        self.commit("tests/test_a.py", case + "\n" + helper, "a")
        self.commit("tests/test_b.py", case.replace("test_status_is_free", "test_retry_is_free").replace("status", "retry")
                    + "\n" + helper, "b")
        _, found = self.run_range("HEAD~1")
        self.assertIn(("duplicate", "def tap"), found)
        self.assertNotIn(("duplicate", "def test_retry_is_free"), found)

    def test_range_compares_with_the_code_before_the_change_not_today(self):
        self.commit("a.py", "def keep():\n    return 1\n", "start")
        self.commit("b.py", LATER.replace("handle_rows", "sum_rows"), "the change under review")
        self.commit("c.py", LATER, "a later commit")
        _, found = self.run_range("HEAD~2..HEAD~1")
        self.assertNotIn("duplicate", [k for k, _ in found])          # handle_rows did not exist yet
        _, found = self.run_range("HEAD~1..HEAD")
        self.assertIn(("duplicate", "def handle_rows"), found)         # now sum_rows did exist


if __name__ == "__main__":
    unittest.main()
