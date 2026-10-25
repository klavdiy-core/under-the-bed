"""Noise measured on a real diff (10 commits of herald, 25.10): 5 of 7 findings were noise. Each case is the real
line, red on 0.3.0; each 'still found' case keeps the rule honest."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import under_the_bed as utb  # noqa: E402
from test_context import diff  # noqa: E402


def found(d, **kw):
    return [f.kind for f in utb.scan(d, **kw)]


def changed(path, old, new):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{old}\n+{new}\n")


class Noise(unittest.TestCase):
    def test_a_callback_parameter_is_not_a_flag(self):
        d = changed("engine.py", "def run_turn(prompt, workspace, cfg, session_id=None):",
                    "def run_turn(prompt, workspace, cfg, session_id=None, should_abort=None, check_s=1.0):")
        self.assertEqual(found(d), [])

    def test_a_real_flag_is_still_found(self):
        d = changed("export.py", "def export(rows, path):", "def export(rows, path, legacy_mode=False):")
        self.assertEqual(found(d), ["flag-parameter"])
        d = changed("export.py", "def export(rows, path):", "def export(rows, path, skip_checks):")
        self.assertEqual(found(d), ["flag-parameter"])

    def test_prose_starting_with_from_is_not_code(self):
        d = diff("learning.py", ["# from a file; another tool that succeeded on the same file. Every candidate is "
                                 "then judged: a cause, a"])
        self.assertEqual(found(d), [])

    def test_a_commented_out_import_is_still_code(self):
        self.assertEqual(found(diff("app.py", ["# from os import path"])), ["commented-out-code"])
        self.assertEqual(found(diff("app.py", ["# import requests"])), ["commented-out-code"])

    def test_a_skip_on_a_platform_condition_with_a_reason_is_not_weakening(self):
        d = diff("tests/test_reliability.py",
                 ['@unittest.skipUnless(sys.platform == "win32", "cmd.exe quoting is a Windows matter")',
                  "class X(unittest.TestCase):", "    def test_a(self):", "        self.assertTrue(1)"])
        self.assertNotIn("weakened-test", found(d))
        d = diff("tests/test_b.py", ['@pytest.mark.skipif(sys.version_info < (3, 9), reason="needs 3.9")',
                                     "def test_b():", "    assert f()"])
        self.assertNotIn("weakened-test", found(d))

    def test_an_unconditional_skip_is_still_weakening(self):
        d = diff("tests/test_c.py", ['@unittest.skip("flaky")', "def test_c(self):", "    self.assertTrue(f())"])
        self.assertIn("weakened-test", found(d))
        d = diff("tests/test_d.py", ['@unittest.skipIf(True, "later")', "def test_d(self):", "    assert f()"])
        self.assertIn("weakened-test", found(d))

    def test_a_silenced_check_with_its_reason_written_is_not_asked_again(self):
        d = diff("run.py", ["try:", "    x()", "except Exception as e:                     "
                            "# noqa: BLE001 — a watcher never takes the turn down", "    log(e)"])
        self.assertNotIn("silenced-check", found(d))

    def test_a_bare_silence_is_still_asked(self):
        self.assertIn("silenced-check", found(diff("run.py", ["x = f()  # type: ignore"])))
        self.assertIn("silenced-check", found(diff("run.py", ["x = f()  # noqa: E501 ok"])))   # two words: no reason

    def test_a_growing_test_file_is_not_flagged(self):
        d = diff("tests/test_reliability.py", [f"    x{i} = {i}" for i in range(400)])
        self.assertNotIn("file-grew", found(d, grow_limit=150))
        d = diff("app/core.py", [f"x{i} = {i}" for i in range(400)])
        self.assertIn("file-grew", found(d, grow_limit=150))


def levels(d):
    return [(f.kind, f.level) for f in utb.scan(d)]


class Idioms(unittest.TestCase):
    """Two idioms seen in three real repos: a damaged record skipped while reading, and a failed write of the error
    log itself. Listed for a look, they do not fail the run."""

    def test_skipping_a_damaged_json_line_is_info(self):
        d = diff("reader.py", ["for line in f:", "    try:", "        e = json.loads(line)",
                               "    except ValueError:", "        continue"])
        self.assertEqual(levels(d), [("swallowed-exception", "info")])

    def test_a_failed_write_of_the_error_log_is_info(self):
        d = diff("hook.py", ["    try:", "        with open(os.path.join(d, 'errors.log'), 'a') as f:",
                             "            f.write(line)", "    except OSError:", "        pass"])
        self.assertEqual(levels(d), [("swallowed-exception", "info")])

    def test_a_real_swallow_still_fails(self):
        d = diff("pay.py", ["try:", "    charge(card)", "except Exception:", "    pass"])
        self.assertEqual(levels(d), [("swallowed-exception", "fail")])
        d = diff("pay.py", ["try:", "    total = compute(order)", "except ValueError:", "    continue"])
        self.assertEqual(levels(d), [("swallowed-exception", "fail")])           # no parse call: not the idiom


if __name__ == "__main__":
    unittest.main()
