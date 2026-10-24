import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import under_the_bed as utb  # noqa: E402


def diff(path, added, removed="", start=1, new=False):
    """Build a tiny unified diff for one file."""
    a = [l for l in textwrap.dedent(added).strip("\n").split("\n")] if added else []
    r = [l for l in textwrap.dedent(removed).strip("\n").split("\n")] if removed else []
    head = f"diff --git a/{path} b/{path}\n"
    head += "new file mode 100644\n--- /dev/null\n" if new else f"--- a/{path}\n"
    head += f"+++ b/{path}\n@@ -{start},{len(r)} +{start},{len(a)} @@\n"
    return head + "".join(f"-{l}\n" for l in r) + "".join(f"+{l}\n" for l in a)


def kinds(findings):
    return sorted(f.kind for f in findings)


class Planted(unittest.TestCase):
    def test_swallowed_exceptions(self):
        d = diff("app.py", """
            try:
                save(x)
            except Exception:
                pass
            try:
                load()
            except:
                return None
        """)
        d += diff("web.ts", """
            try { send(x) } catch (e) {}
            fetch(u).catch(() => {});
            try {
              go();
            } catch (err) {
            }
        """)
        got = kinds(utb.scan(d))
        self.assertEqual(got.count("swallowed-exception"), 5, got)

    def test_suppressions_and_todos(self):
        d = diff("a.py", """
            x = f()  # type: ignore
            y = g()  # noqa: E501
            # TODO: handle errors later
        """)
        d += diff("b.ts", """
            // eslint-disable-next-line
            const v = data as any;
            // @ts-ignore
            let w: any = 1;
            // FIXME this is wrong
        """)
        got = kinds(utb.scan(d))
        self.assertEqual(got.count("silenced-check"), 6, got)
        self.assertEqual(got.count("todo"), 2, got)

    def test_commented_out_code(self):
        d = diff("c.py", """
            # result = compute(a, b)
            # return result
            # This explains why we round down.
        """)
        d += diff("d.js", """
            // if (user.isAdmin) { grant(); }
            // We keep the old order for clients.
        """)
        got = kinds(utb.scan(d))
        self.assertEqual(got.count("commented-out-code"), 3, got)

    def test_weakened_tests(self):
        d = diff("tests/test_pay.py", """
            @pytest.mark.skip(reason="flaky")
            def test_refund():
                assert refund(10)
        """, removed="""
            def test_refund():
                assert refund(10) == 10
                assert balance() == 0
        """)
        d += diff("src/pay.spec.ts", """
            it.skip('charges once', () => {
        """, removed="""
            it('charges once', () => {
        """)
        got = kinds(utb.scan(d))
        self.assertEqual(got.count("weakened-test"), 3, got)  # 2 skips + assertions dropped

    def test_flag_instead_of_refactor(self):
        d = diff("svc.py", """
            def export(rows, path, legacy_mode=False):
        """, removed="""
            def export(rows, path):
        """)
        d += diff("mail.py", """
            def send(msg, to, retry=True):
        """, removed="""
            def send(msg, to):
        """)
        d += diff("svc.ts", """
            function render(items, skipHeader = false) {
        """, removed="""
            function render(items) {
        """)
        got = kinds(utb.scan(d))
        self.assertEqual(got.count("flag-parameter"), 3, got)

    def test_file_grew(self):
        body = "\n".join(f"x{i} = {i}" for i in range(320))
        got = kinds(utb.scan(diff("big.py", body, new=True), grow_limit=300))
        self.assertEqual(got.count("file-grew"), 1, got)

    def test_duplicate_of_existing_function(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "pkg"))
            with open(os.path.join(root, "pkg", "utils.py"), "w") as f:
                f.write(textwrap.dedent("""
                    def slugify(text):
                        text = text.strip().lower()
                        out = []
                        for ch in text:
                            if ch.isalnum():
                                out.append(ch)
                            elif out and out[-1] != "-":
                                out.append("-")
                        return "".join(out).strip("-")
                """))
            d = diff("pkg/views.py", """
                def make_slug(title):
                    title = title.strip().lower()
                    res = []
                    for c in title:
                        if c.isalnum():
                            res.append(c)
                        elif c == "_":
                            continue
                        elif res and res[-1] != "-":
                            res.append("-")
                    return "".join(res).strip("-")

                def count_words(title):
                    title = title.strip().lower()
                    total = 0
                    for c in title.split():
                        if c.isalpha():
                            total += len(c)
                        elif total and c != "-":
                            total -= 1
                    return str(total).strip("-")
            """, new=True)
            with open(os.path.join(root, "pkg", "views.py"), "w") as f:
                f.write("\n".join(l[1:] for l in d.splitlines() if l.startswith("+") and not l.startswith("+++")))
            found = utb.scan(d, root=root)
            dup = [f for f in found if f.kind == "duplicate"]
            self.assertEqual(len(dup), 1, kinds(found))
            self.assertIn("slugify", dup[0].question)
            self.assertIn("utils.py", dup[0].question)


class Clean(unittest.TestCase):
    def test_clean_diff_has_no_findings(self):
        d = diff("app.py", """
            def parse(s, base=10):
                try:
                    return int(s, base)
                except ValueError:
                    log.warning("bad number %r", s)
                    return None

            # Round down: the bank does the same.
            total = sum(rows)
        """)
        d += diff("src/types.test.ts", """
            // @ts-expect-error a number is not a string
            const s: string = 5;
        """)
        d += diff("tests/test_app.py", """
            def test_parse():
                assert parse("12") == 12
                assert parse("x") is None
        """)
        d += diff("ui.ts", """
            try {
              await save();
            } catch (err) {
              toast(err.message);
            }
            // Keep the list sorted for screen readers.
            const items: Item[] = sorted(list);
        """)
        d += diff("README.md", """
            TODO lists are a feature of this app.
        """)
        self.assertEqual(utb.scan(d), [])


class Cli(unittest.TestCase):
    def test_reads_real_git_diff(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*a):
                subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "t@t")
            git("config", "user.name", "t")
            p = os.path.join(root, "m.py")
            with open(p, "w") as f:
                f.write("def f():\n    return 1\n")
            git("add", ".")
            git("commit", "-qm", "init")
            with open(p, "w") as f:
                f.write("def f():\n    try:\n        return 1\n    except Exception:\n        pass\n")
            out = os.path.join(root, "report.json")
            code = utb.main(["-C", root, "--json", "-o", out])
            self.assertEqual(code, 1)
            with open(out) as f:
                rep = json.load(f)
            self.assertEqual([x["kind"] for x in rep["findings"]], ["swallowed-exception"])
            self.assertEqual(rep["findings"][0]["line"], 4)

    def test_clean_exit_zero(self):
        with tempfile.TemporaryDirectory() as root:
            dp = os.path.join(root, "x.diff")
            with open(dp, "w") as f:
                f.write(diff("a.py", "x = 1"))
            self.assertEqual(utb.main(["--diff", dp, "-o", os.path.join(root, "r.md")]), 0)


if __name__ == "__main__":
    unittest.main()
