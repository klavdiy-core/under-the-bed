"""Context, levels and repo idiom: found by using the tool on the author's own commits (79% noise, 24.10)."""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import under_the_bed as utb  # noqa: E402


def diff(path, added, context=()):
    body = [f" {c}" for c in context] + [f"+{a}" for a in added]
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(context)} +1,{len(context) + len(added)} @@\n" + "\n".join(body) + "\n")


def kinds(d, **kw):
    return [(f.kind, f.level) for f in utb.scan(d, **kw)]


class Context(unittest.TestCase):
    def test_e402_after_sys_path_insert_is_the_test_idiom(self):
        d = diff("tests/test_x.py", ["import sys", "sys.path.insert(0, '..')", "import x  # noqa: E402"])
        self.assertEqual(kinds(d), [])

    def test_e402_without_a_sys_path_edit_is_still_asked(self):
        d = diff("app.py", ["import os", "x = 1", "import y  # noqa: E402"])
        self.assertEqual([k for k, _ in kinds(d)], ["silenced-check"])

    def test_skip_words_inside_strings_and_regexes_are_not_skipped_tests(self):
        d = diff("tests/test_guard.py", ["SKIP = re.compile(r'@pytest.mark.skip|xfail')",
                                         "    src = \"it.skip('charges once', () => {})\""])
        self.assertEqual(kinds(d), [])

    def test_a_real_skip_is_still_found(self):
        d = diff("tests/test_a.py", ["@pytest.mark.skip(reason='flaky')", "def test_a():", "    assert f()"])
        self.assertIn("weakened-test", [k for k, _ in kinds(d)])

    def test_except_with_a_fallback_right_after_is_not_swallowed(self):
        d = diff("fonts.py", ["    for name in names:", "        try:", "            return truetype(name)",
                              "        except OSError:", "            pass", "    return load_default()"])
        self.assertEqual(kinds(d), [])

    def test_a_plain_return_after_the_swallow_is_not_a_fallback(self):
        # the README example: `except Exception: pass` then `return amount + tax` (tax may be unset) must stay found
        ex = os.path.join(os.path.dirname(HERE), "examples", "ai_cleanup.diff")
        with open(ex, encoding="utf-8") as f:
            self.assertIn("swallowed-exception", [k for k, _ in kinds(f.read())])

    def test_a_bare_swallow_is_still_found(self):
        d = diff("app.py", ["try:", "    charge(card)", "except Exception:", "    pass", "log('done')"])
        self.assertEqual([k for k, _ in kinds(d)], ["swallowed-exception"])


class Levels(unittest.TestCase):
    def test_only_failure_hiding_findings_fail_the_run(self):
        d = (diff("app.py", ["try:", "    charge(card)", "except Exception:", "    pass", "log('done')"])
             + diff("util.py", ["x = f()  # type: ignore", "# TODO: later"]))
        levels = dict(kinds(d))
        self.assertEqual(levels["swallowed-exception"], "fail")
        self.assertEqual(levels["silenced-check"], "info")
        self.assertEqual(levels["todo"], "info")

    def test_exit_code_follows_the_level_and_strict_restores_the_old_rule(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d.diff")
            with open(p, "w") as f:
                f.write(diff("util.py", ["x = f()  # type: ignore"]))
            run = lambda *a: subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "under_the_bed.py"),
                                             "--diff", p, *a], capture_output=True, text=True, encoding="utf-8")
            r = run()
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("type: ignore", r.stdout)               # still shown, just not failing
            self.assertEqual(run("--strict").returncode, 1)


class Idiom(unittest.TestCase):
    def test_a_marker_already_common_in_the_repo_is_info_with_a_note(self):
        with tempfile.TemporaryDirectory() as repo:
            g = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True)
            g("init", "-q")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            for i in range(3):
                with open(os.path.join(repo, f"m{i}.py"), "w") as f:
                    f.write("try:\n    x()\nexcept Exception:  # noqa: BLE001\n    raise\n")
            g("add", ".")
            g("commit", "-qm", "base")
            d = diff("m9.py", ["except Exception:  # noqa: BLE001"])
            got = [f for f in utb.scan(d, root=repo) if f.kind == "silenced-check"]
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].level, "info")
            self.assertIn("repo idiom", got[0].question)


if __name__ == "__main__":
    unittest.main()
