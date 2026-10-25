"""Edits from outside this session: another session in the same tree, or a subagent writing outside its worktree.
Complaints (25.10): x.com/amir3_9/status/2114027274190348748 (two sessions in one tree see each other's edits),
x.com/hodu382759/status/2114190002389409935 (a subagent resumed after its worktree was cleaned up and edited the
main project; the user did not know)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import under_the_bed as utb  # noqa: E402
from test_scope import filediff  # noqa: E402


def transcript(path, sid, cwd, edits):
    """edits: [(tool, file_path, iso time)] as Claude Code records them."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": "2026-10-25T09:00:00Z",
                            "message": {"role": "user", "content": "go"}}) + "\n")
        for tool, fp, ts in edits:
            key = "notebook_path" if tool == "NotebookEdit" else "file_path"
            f.write(json.dumps({"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": ts,
                                "message": {"role": "assistant", "content": [
                                    {"type": "tool_use", "name": tool, "input": {key: fp}}]}}) + "\n")


class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "shop")
        self.proj = os.path.join(self.tmp.name, "projects", "shop")
        os.makedirs(self.root)
        self.mine = os.path.join(self.proj, "aaaa1111.jsonl")
        transcript(self.mine, "aaaa1111", self.root, [("Edit", os.path.join(self.root, "billing", "invoice.py"),
                                                        "2026-10-25T09:05:00Z")])
        self.diff = (filediff("billing/invoice.py", ["    return 1"], ["    return 0"])
                     + filediff("config/settings.py", ["TIMEOUT = 60"], ["TIMEOUT = 5"]))

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self, found):
        return {(f.kind, f.file) for f in found}

    def test_a_file_another_session_edited_is_named_with_that_session(self):
        transcript(os.path.join(self.proj, "bbbb2222.jsonl"), "bbbb2222", self.root,
                   [("Write", os.path.join(self.root, "config", "settings.py"), "2026-10-25T09:07:00Z")])
        found = utb.check_sessions(utb.parse_diff(self.diff), self.root, self.mine)
        self.assertEqual(self.kinds(found), {("other-session", "config/settings.py")})
        self.assertIn("bbbb2222", found[0].question)
        self.assertIn("09:07", found[0].question)

    def test_a_file_no_session_edited_is_only_a_question(self):
        found = utb.check_sessions(utb.parse_diff(self.diff), self.root, self.mine)
        self.assertEqual(self.kinds(found), {("not-this-session", "config/settings.py")})

    def test_a_subagent_that_wrote_outside_its_worktree_is_loud(self):
        wt = os.path.join(self.root, ".claude", "worktrees", "agent-1")
        transcript(os.path.join(self.proj, "aaaa1111", "subagents", "agent-1.jsonl"), "aaaa1111", wt,
                   [("Edit", os.path.join(wt, "billing", "invoice.py"), "2026-10-25T09:10:00Z"),
                    ("Edit", os.path.join(self.root, "config", "settings.py"), "2026-10-25T09:40:00Z")])
        found = utb.check_sessions(utb.parse_diff(self.diff), self.root, self.mine)
        self.assertIn(("subagent-outside-worktree", "config/settings.py"), self.kinds(found))
        self.assertNotIn(("not-this-session", "config/settings.py"), self.kinds(found))   # its author is known
        f = [x for x in found if x.kind == "subagent-outside-worktree"][0]
        self.assertIn("agent-1", f.question)

    def test_a_subagent_in_a_plain_folder_writing_where_it_was_told_is_not_flagged(self):
        # measured live (25.10): subagents run in a repo folder wrote their results into the parent project, as
        # asked - three false alarms. Only a real worktree (.git is a file), a 'worktrees' path or a folder that is
        # gone (the complaint: the worktree was cleaned up) counts.
        plain = os.path.join(self.root, "tools")
        os.makedirs(os.path.join(plain, ".git"))
        transcript(os.path.join(self.proj, "aaaa1111", "subagents", "agent-2.jsonl"), "aaaa1111", plain,
                   [("Write", os.path.join(self.root, "config", "settings.py"), "2026-10-25T09:40:00Z")])
        found = utb.check_sessions(utb.parse_diff(self.diff), self.root, self.mine)
        self.assertEqual(found, [])
        wt = os.path.join(self.tmp.name, "wt")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write("gitdir: elsewhere\n")
        transcript(os.path.join(self.proj, "aaaa1111", "subagents", "agent-2.jsonl"), "aaaa1111", wt,
                   [("Write", os.path.join(self.root, "config", "settings.py"), "2026-10-25T09:40:00Z")])
        found = utb.check_sessions(utb.parse_diff(self.diff), self.root, self.mine)
        self.assertEqual({f.kind for f in found}, {"subagent-outside-worktree"})

    def test_the_session_flags_fail_the_run_through_the_cli(self):
        transcript(os.path.join(self.proj, "bbbb2222.jsonl"), "bbbb2222", self.root,
                   [("Write", os.path.join(self.root, "config", "settings.py"), "2026-10-25T09:07:00Z")])
        dpath = os.path.join(self.tmp.name, "change.diff")
        with open(dpath, "w", encoding="utf-8") as f:
            f.write(self.diff)
        out = io.StringIO()
        with redirect_stdout(out):
            code = utb.main(["--diff", dpath, "--session", self.mine, "--session-root", self.root])
        self.assertEqual(code, 1)
        self.assertIn("other-session", out.getvalue())


if __name__ == "__main__":
    unittest.main()
