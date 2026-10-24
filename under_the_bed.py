#!/usr/bin/env python3
"""under-the-bed: find what the AI swept under the bed in your diff.

Reads a git diff and lists the mess an assistant tends to hide while "cleaning
up": swallowed exceptions, silenced checks, TODOs, commented-out code, skipped
or weakened tests, flag parameters bolted on instead of a refactor, copies of
functions that already exist, files that ballooned. Each finding comes out as
a question to put back to the AI. No network, no LLM, standard library only.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field

__version__ = "0.1.0"

CODE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt", ".go",
    ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".rb", ".php", ".scala",
    ".dart", ".sh", ".vue", ".svelte",
}
HASH_COMMENT = {".py", ".pyi", ".rb", ".sh"}
TS_LIKE = {".ts", ".tsx", ".vue", ".svelte"}
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "dist", "build", "target",
             "__pycache__", ".tox", ".mypy_cache", "vendor", ".next", "coverage"}

QUESTIONS = {
    "swallowed-exception": "Why is this error swallowed? What should happen when it fires, and where will anyone see it?",
    "silenced-check": "Which warning does this silence, and why is hiding it better than fixing it?",
    "todo": "Who does this TODO, and when? If it is needed, why is it not done in this change?",
    "commented-out-code": "Why is dead code kept in a comment? Git remembers it. Can it go?",
    "skip": "Why is this test switched off? Is the code it guards broken now?",
}


@dataclass
class Finding:
    kind: str
    file: str
    line: int
    question: str
    snippet: str = ""


@dataclass
class FileDiff:
    path: str
    new: bool = False
    added: list = field(default_factory=list)    # (new line no, text)
    removed: list = field(default_factory=list)  # (old line no, text)

    @property
    def ext(self):
        return os.path.splitext(self.path)[1].lower()


# ---------- diff parsing ----------

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(text):
    files, cur, old_no, new_no, pending_new = [], None, 0, 0, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = None
            continue
        if line.startswith("--- "):
            pending_new = line[4:].strip() == "/dev/null"
            continue
        if line.startswith("+++ "):
            path = line[4:].strip().split("\t")[0]
            if path == "/dev/null":
                cur = None
                continue
            path = path[2:] if path.startswith("b/") else path
            cur = FileDiff(path, new=pending_new)
            files.append(cur)
            continue
        m = HUNK.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            continue
        if cur is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            cur.added.append((new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            cur.removed.append((old_no, line[1:]))
            old_no += 1
        else:
            old_no += 1
            new_no += 1
    return files


# ---------- line checks ----------

SILENCED = [
    re.compile(p) for p in (
        r"#\s*type:\s*ignore", r"#\s*noqa", r"#\s*pylint:\s*disable", r"#\s*pragma:\s*no cover",
        r"eslint-disable", r"@ts-ignore", r"@ts-expect-error", r"@ts-nocheck", r"//\s*nolint",
        r"@SuppressWarnings", r"#!?\[allow\(", r"#\s*pyright:\s*ignore", r"\bas\s+any\b",
    )
]
TS_ANY = re.compile(r"[:<,]\s*any\b(?!\w)")
TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
CODE_KW = re.compile(
    r"^(return|if|elif|else|for|while|def|class|import|from|const|let|var|function|try|"
    r"catch|except|print|await|async|raise|throw|switch|case)\b")
ASSIGN = re.compile(r"^[A-Za-z_][\w.\[\]'\"]*\s*(=|\+=|-=|:=)\s*[^=\s]")
CALL_END = re.compile(r"[\w\]\)]\s*\(.*\)\s*[;{}]?\s*$")


def _comment_body(text, ext):
    s = text.strip()
    if ext in HASH_COMMENT:
        if s.startswith("#") and not s.startswith("#!"):
            return s[1:].strip()
        return None
    if s.startswith("//"):
        return s[2:].strip()
    return None


def _looks_like_code(body):
    if not body or body.startswith(("-*-", "type:", "noqa", "eslint", "@ts-", "pylint")):
        return False
    if ASSIGN.match(body) or CALL_END.match(body) and " " not in body.split("(")[0].strip():
        return True
    m = CODE_KW.match(body)
    if m:
        kw = m.group(1)
        if kw == "return":
            return bool(re.match(r"^return(\s+[\w.\[\]()\"'+\-*/ ,]+)?;?$", body)) and len(body.split()) <= 4
        if kw in ("else", "try"):
            return body.rstrip(":{ ").strip() == kw
        return bool(re.search(r"[(=;{]|:\s*$", body))
    return False


def _next_added(added, i):
    """The added line right after added[i], if it directly follows it."""
    if i + 1 < len(added) and added[i + 1][0] == added[i][0] + 1:
        return added[i + 1][1]
    return None


PY_EXCEPT = re.compile(r"^\s*except\b([^:]*):\s*(.*)$")
PY_NOOP = re.compile(r"^\s*(pass|\.\.\.|continue)\s*(#.*)?$")
INLINE_CATCH = re.compile(r"\bcatch\s*(\([^)]*\))?\s*\{\s*\}")
PROMISE_CATCH = re.compile(
    r"\.catch\(\s*(?:\(\s*\w*\s*\)|\w+|function\s*\(\s*\w*\s*\))\s*(?:=>)?\s*(?:\{\s*\}|null|undefined|void 0)\s*\)")
OPEN_CATCH = re.compile(r"\bcatch\s*(\([^)]*\))?\s*\{\s*$")


def check_lines(fd):
    out = []
    if fd.ext not in CODE_EXT:
        return out
    add = fd.added
    for i, (no, text) in enumerate(add):
        s = text.strip()
        # swallowed exceptions
        if fd.ext in (".py", ".pyi"):
            m = PY_EXCEPT.match(text)
            if m:
                bare = m.group(1).strip() == ""
                inline = m.group(2).strip()
                body = inline if inline and not inline.startswith("#") else (_next_added(add, i) or "")
                if bare or PY_NOOP.match(body):
                    out.append(Finding("swallowed-exception", fd.path, no, QUESTIONS["swallowed-exception"]
                                       + (" A bare `except:` also catches Ctrl+C and typos." if bare else ""), s))
                    continue
        else:
            nxt = _next_added(add, i)
            if INLINE_CATCH.search(text) or PROMISE_CATCH.search(text) or (
                    OPEN_CATCH.search(text) and nxt is not None and nxt.strip() == "}"):
                out.append(Finding("swallowed-exception", fd.path, no, QUESTIONS["swallowed-exception"], s))
                continue
        # silenced checks
        if "@ts-expect-error" in text and TEST_PATH.search(fd.path):
            pass  # a type test: the error IS the assertion
        elif any(p.search(text) for p in SILENCED) or (fd.ext in TS_LIKE and TS_ANY.search(text)):
            out.append(Finding("silenced-check", fd.path, no, QUESTIONS["silenced-check"], s))
            continue
        body = _comment_body(text, fd.ext)
        if TODO.search(text) and (body is not None or "/*" in text or "#" in text):
            out.append(Finding("todo", fd.path, no, QUESTIONS["todo"], s))
            continue
        if body is not None and _looks_like_code(body):
            out.append(Finding("commented-out-code", fd.path, no, QUESTIONS["commented-out-code"], s))
    return out


# ---------- tests ----------

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|Tests?\.(java|kt|cs)$")
SKIP = re.compile(
    r"@pytest\.mark\.(skip|xfail)|@unittest\.skip|\bpytest\.skip\(|\b(it|test|describe|context)\.(skip|todo)\(|"
    r"\bx(it|test|describe)\(|@Disabled\b|@Ignore\b|\bt\.Skip(Now|f)?\(|\b(it|test|describe)\.only\(|#\[ignore\]")
ASSERTION = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bassert_\w+!|\bt\.(Error|Fatal)f?\(|\bverify\(")


def check_tests(fd):
    out = []
    if not TEST_PATH.search(fd.path):
        return out
    for no, text in fd.added:
        if SKIP.search(text):
            out.append(Finding("weakened-test", fd.path, no, QUESTIONS["skip"], text.strip()))
    gone = sum(1 for _, t in fd.removed if ASSERTION.search(t))
    came = sum(1 for _, t in fd.added if ASSERTION.search(t))
    if gone > came:
        first = next(no for no, t in fd.removed if ASSERTION.search(t))
        out.append(Finding(
            "weakened-test", fd.path, first,
            f"This change removes {gone} assertion(s) and adds {came}. What is no longer checked, and why is that safe?",
            next(t.strip() for _, t in fd.removed if ASSERTION.search(t))))
    return out


# ---------- flag parameters ----------

SIG = [
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\((.*)\)"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\((.*?)\)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\((.*?)\)\s*(?::[^=]*)?=>"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\((.*?)\)"),
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|async|override|internal)\s+)+[\w<>\[\], ]*?\b(\w+)\s*\((.*?)\)"),
]
FLAG_NAME = re.compile(
    r"^(is|has|use|skip|enable|disable|no|force|legacy|allow|should|with|without|dry|ignore|include|exclude|raw|quiet|strict|fast|old|new)([_A-Z]|$)|(_flag|_mode|Flag|Mode)$")
BOOL_DEFAULT = re.compile(r"=\s*(True|False|true|false)\b|:\s*bool(ean)?\b|\bbool\s+\w+$")


def _split_params(s):
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _param_name(p):
    p = p.lstrip("*&").strip()
    m = re.match(r"^(?:[\w<>\[\]]+\s+)?(\w+)\s*(?:[:=?]|$)", p)
    if m:
        return m.group(1)
    return re.split(r"[\s:=?]", p)[0]


def _sigs(lines):
    out = {}
    for no, text in lines:
        for rx in SIG:
            m = rx.match(text)
            if m and m.group(1) not in ("if", "for", "while", "switch", "catch", "return"):
                out.setdefault(m.group(1), (no, _split_params(m.group(2))))
                break
    return out


def check_flags(fd):
    out = []
    if fd.ext not in CODE_EXT:
        return out
    before, after = _sigs(fd.removed), _sigs(fd.added)
    for name, (no, params) in after.items():
        if name not in before:
            continue
        old = {_param_name(p) for p in before[name][1]}
        for p in params:
            pn = _param_name(p)
            if pn in old or pn in ("self", "cls"):
                continue
            if FLAG_NAME.search(pn) or BOOL_DEFAULT.search(p):
                out.append(Finding(
                    "flag-parameter", fd.path, no,
                    f"`{name}` grew a flag `{pn}`. Which callers pass it, and why is this not two functions, "
                    f"or a fix of the one?", p))
    return out


# ---------- growth ----------

def check_growth(fd, limit):
    grew = len(fd.added) - len(fd.removed)
    if limit and grew >= limit:
        line = fd.added[0][0] if fd.added else 1
        what = "This new file is" if fd.new else "This file grew by"
        return [Finding("file-grew", fd.path, line,
                        f"{what} {grew} lines in one change. What should have been split out, or reused from "
                        f"code that already exists?")]
    return []


# ---------- duplicates ----------

PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
BRACE_DEF = [
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]*)?=>\s*\{"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("),
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|async|override|internal)\s+)+[\w<>\[\], ]*?\b(\w+)\s*\("),
    re.compile(r"^\s*fn\s+(\w+)\s*[<(]"),
]
TOKEN = re.compile(r"[A-Za-z_]\w*|\d+|\S")
BIND = re.compile(r"\b([A-Za-z_]\w*)\s*(?:=(?!=)|\+=|-=|:=)|\bfor\s*\(?\s*(?:const|let|var)?\s*([A-Za-z_]\w*)|\bas\s+([A-Za-z_]\w*)|\b(?:const|let|var)\s+([A-Za-z_]\w*)")
MIN_TOKENS = 30


def _functions(lines, ext):
    """[(name, first line no, body lines)] from a list of (no, text)."""
    found = []
    if ext in (".py", ".pyi"):
        for i, (no, text) in enumerate(lines):
            m = PY_DEF.match(text)
            if not m:
                continue
            indent = len(m.group(1).expandtabs())
            body, prev = [], no
            for no2, t2 in lines[i + 1:]:
                if no2 != prev + 1:
                    break
                prev = no2
                if t2.strip() and len(t2) - len(t2.lstrip()) <= indent and not t2.lstrip().startswith(")"):
                    break
                body.append(t2)
            found.append((m.group(2), no, [text] + body))
        return found
    for i, (no, text) in enumerate(lines):
        for rx in BRACE_DEF:
            m = rx.match(text)
            if not m or m.group(1) in ("if", "for", "while", "switch", "catch"):
                continue
            depth, body, started, prev = 0, [], False, no - 1
            for no2, t2 in lines[i:]:
                if no2 != prev + 1:
                    break
                prev = no2
                body.append(t2)
                depth += t2.count("{") - t2.count("}")
                started = started or "{" in t2
                if started and depth <= 0:
                    break
            found.append((m.group(1), no, body))
            break
    return found


def _norm(body):
    """Tokens of a function, with its own parameter and local names renamed in order."""
    text = "\n".join(body)
    first = body[0]
    params = re.search(r"\((.*)\)", first)
    names = [_param_name(p) for p in _split_params(params.group(1))] if params else []
    for m in BIND.finditer(text):
        names.extend(g for g in m.groups() if g)
    rename = {}
    for n in names:
        if n not in rename and n not in ("self", "this", "cls"):
            rename[n] = f"v{len(rename)}"
    toks = TOKEN.findall("\n".join(body[1:]) if len(body) > 1 else text)
    return [rename.get(t, t) for t in toks]


def _walk(root, exts):
    count = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                p = os.path.join(d, f)
                try:
                    if os.path.getsize(p) > 300_000:
                        continue
                except OSError:
                    continue
                yield p
                count += 1
                if count > 5000:
                    return


def check_duplicates(files, root, threshold=0.9):
    out = []
    new_funcs = []
    for fd in files:
        if fd.ext in CODE_EXT:
            for name, no, body in _functions(fd.added, fd.ext):
                toks = _norm(body)
                if len(toks) >= MIN_TOKENS:
                    new_funcs.append((fd, name, no, toks))
    if not new_funcs:
        return out
    exts = {fd.ext for fd, *_ in new_funcs}
    corpus = []
    for p in _walk(root, exts):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = list(enumerate(f.read().splitlines(), 1))
        except OSError:
            continue
        ext = os.path.splitext(p)[1].lower()
        for name, no, body in _functions(lines, ext):
            corpus.append((rel, ext, name, no, _norm(body)))
    for fd, name, no, toks in new_funcs:
        best = None
        for rel, ext, oname, ono, otoks in corpus:
            if ext != fd.ext or (rel == fd.path and oname == name) or len(otoks) < MIN_TOKENS:
                continue
            if abs(len(otoks) - len(toks)) > 0.3 * max(len(toks), len(otoks)):
                continue
            sm = difflib.SequenceMatcher(None, toks, otoks, autojunk=False)
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            r = sm.ratio()
            if r >= threshold and (best is None or r > best[0]):
                best = (r, rel, oname, ono)
        if best:
            r, rel, oname, ono = best
            out.append(Finding(
                "duplicate", fd.path, no,
                f"`{name}` looks like `{oname}` in {rel}:{ono} ({round(100 * r)}% the same). "
                f"Why not call that one?", f"def {name}" if fd.ext == ".py" else name))
    return out


# ---------- driver ----------

def scan(diff_text, root=None, grow_limit=400):
    files = parse_diff(diff_text)
    found = []
    for fd in files:
        found += check_lines(fd)
        found += check_tests(fd)
        found += check_flags(fd)
        found += check_growth(fd, grow_limit)
    if root:
        found += check_duplicates(files, root)
    order = {fd.path: i for i, fd in enumerate(files)}
    found.sort(key=lambda f: (order.get(f.file, 0), f.line, f.kind))
    return found


def render_md(findings, source):
    if not findings:
        return f"# Under the bed: clean\n\nNothing swept under the bed in {source}.\n"
    out = [f"# Under the bed: {len(findings)} thing(s) to ask about",
           "", f"Source: {source}. Paste these back to the assistant, one by one."]
    cur = None
    for f in findings:
        if f.file != cur:
            cur = f.file
            out += ["", f"## {cur}", ""]
        out.append(f"- [ ] **L{f.line} {f.kind}**: {f.question}")
        if f.snippet:
            out.append(f"  `{f.snippet[:160]}`")
    return "\n".join(out) + "\n"


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def main(argv=None):
    ap = argparse.ArgumentParser(prog="under-the-bed", description=__doc__.splitlines()[0])
    ap.add_argument("-C", dest="cwd", default=".", help="repository to look at (default: here)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--staged", action="store_true", help="only what is staged")
    src.add_argument("--range", help="a commit range, e.g. main..HEAD or HEAD~3")
    src.add_argument("--diff", help="read a unified diff from a file ('-' = stdin)")
    ap.add_argument("--grow-limit", type=int, default=400, help="flag files that grew by this many lines (0 = off)")
    ap.add_argument("--no-dupes", action="store_true", help="skip the search for copies of existing functions")
    ap.add_argument("--json", action="store_true", help="JSON instead of a Markdown checklist")
    ap.add_argument("-o", "--output", help="write the report here instead of stdout")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    root = None
    try:
        if a.diff:
            text = sys.stdin.read() if a.diff == "-" else open(a.diff, encoding="utf-8", errors="replace").read()
            source = "stdin" if a.diff == "-" else a.diff
        else:
            root = _git(a.cwd, "rev-parse", "--show-toplevel").strip()
            if a.staged:
                text, source = _git(root, "diff", "--cached", "-U3"), "staged changes"
            elif a.range:
                rng = a.range if ".." in a.range else f"{a.range}..HEAD"
                text, source = _git(root, "diff", "-U3", rng), rng
            else:
                text, source = _git(root, "diff", "HEAD", "-U3"), "uncommitted changes (git diff HEAD)"
    except (OSError, RuntimeError) as e:
        print(f"under-the-bed: {e}", file=sys.stderr)
        return 2

    findings = scan(text, root=None if a.no_dupes else root, grow_limit=a.grow_limit)
    if a.json:
        report = json.dumps({"source": source, "count": len(findings),
                             "findings": [asdict(f) for f in findings]}, indent=2)
    else:
        report = render_md(findings, source)
    if a.output:
        with open(a.output, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")  # a cp1252 console must not crash on a snippet
        sys.stdout.write(report)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
