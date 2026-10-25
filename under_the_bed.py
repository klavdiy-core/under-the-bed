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
import hashlib
from dataclasses import asdict, dataclass, field

__version__ = "0.5.0"

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
    level: str = "fail"      # "fail": hides a failure, fails the run; "info": worth a look, does not fail
    idiom: str = ""          # a known harmless shape (listed as info, with why)


@dataclass
class FileDiff:
    path: str
    new: bool = False
    added: list = field(default_factory=list)    # (new line no, text)
    removed: list = field(default_factory=list)  # (old line no, text)
    headers: list = field(default_factory=list)  # text after "@@ ... @@": the enclosing function, per hunk
    context: list = field(default_factory=list)  # unchanged lines shown in the diff

    @property
    def ext(self):
        return os.path.splitext(self.path)[1].lower()


# ---------- diff parsing ----------

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


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
            if cur is not None and m.group(3).strip():
                cur.headers.append(m.group(3).strip())
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
            cur.context.append(line[1:])
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
        if kw in ("import", "from"):
            # only real import syntax: prose that starts with "from a file; ..." is a comment (found on herald)
            return bool(re.match(r"^(from\s+[\w.]+\s+import\s+[\w*, ()]+|import\s+[\w.]+(\s+as\s+\w+)?"
                                 r"(\s*,\s*[\w.]+(\s+as\s+\w+)?)*)\s*;?$", body))
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
# a swallow followed at once by a fallback is a choice, not a hidden failure (try-each-font loops)
# a plain `return x` is NOT a fallback (the README example returns with `tax` maybe unset): only a call that names one
FALLBACK = re.compile(r"\b\w*(default|fallback)\w*\s*\(", re.I)


PARSE_ERRORS = re.compile(r"^\s*\(?\s*((json\.)?JSONDecodeError|ValueError|UnicodeDecodeError|KeyError|TypeError|"
                          r"IndexError|AttributeError)(\s*,\s*((json\.)?JSONDecodeError|ValueError|UnicodeDecodeError|"
                          r"KeyError|TypeError|IndexError|AttributeError))*\s*\)?\s*(as\s+\w+)?\s*$")
PARSE_CALL = re.compile(r"\b(json\.loads?|int|float|fromisoformat|strptime|literal_eval|Decimal|UUID)\s*\(|\.decode\(")
LOG_WRITE = re.compile(r"(log|err)\w*\b.*\b(open|write)|\.write\(|print\(.*stderr", re.I)


def _swallow_idiom(caught, body, tried):
    """Two harmless shapes, seen in three real repos: a damaged record skipped while reading (a parse error, then
    continue), and a failed write of the error log itself (OSError while writing the log, then pass)."""
    if PY_NOOP.match(body) and body.strip().startswith("continue") and PARSE_ERRORS.match(caught) \
            and PARSE_CALL.search(tried):
        return "a damaged record is skipped while reading"
    if re.match(r"^\s*\(?\s*(OSError|IOError)\s*\)?\s*$", caught) and LOG_WRITE.search(tried) and "log" in tried.lower():
        return "writing the error log itself failed"
    return ""


def _in_string(text, pos):
    """True when position pos sits inside a '...' or "..." literal on this line (crude, single line)."""
    quote = None
    i = 0
    while i < pos:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
        elif c == quote:
            quote = None
        i += 1
    return quote is not None


REASON = re.compile(r"(?:noqa:?\s*[A-Z]+\d+(?:,\s*[A-Z]+\d+)*|type:\s*ignore(?:\[[\w-]+\])?|pyright:\s*ignore(?:\[[\w-]+\])?|"
                    r"pylint:\s*disable=[\w,-]+|eslint-disable[\w-]*(?:\s+[\w/@-]+(?:,\s*[\w/@-]+)*)?|@ts-ignore|"
                    r"@ts-expect-error)\s*(?:[-—–:]+|because|--)\s*(.+)$")


def _reason_written(text):
    """A silence that says why on the same line ('# noqa: BLE001 — a watcher never takes the turn down') already
    answers the question; three words or more after a dash or a colon count as a reason."""
    m = REASON.search(text)
    return bool(m and len(re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", m.group(1))) >= 3)


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
                after = " ".join(t for _, t in add[i + 2:i + 5])
                if (bare or PY_NOOP.match(body)) and not (not bare and FALLBACK.search(after)):
                    tried = " ".join(t for _, t in add[max(0, i - 3):i])
                    out.append(Finding("swallowed-exception", fd.path, no, QUESTIONS["swallowed-exception"]
                                       + (" A bare `except:` also catches Ctrl+C and typos." if bare else ""), s,
                                       idiom="" if bare else _swallow_idiom(m.group(1), body, tried)))
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
        elif re.search(r"noqa:?\s*E402", text) and any("sys.path" in t for t in fd.context + [t for _, t in add[:i]]):
            pass  # an import after a sys.path edit: the standard test idiom, not a silenced problem
        elif any(p.search(text) for p in SILENCED) or (fd.ext in TS_LIKE and TS_ANY.search(text)):
            if not _reason_written(text):
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
# a conditional skip on the environment: the test cannot run here, it is not switched off
ENV_SKIP = re.compile(r"(skipIf|skipUnless|skipif)\s*\(\s*(not\s+)?\(?\s*(sys\.platform|sys\.version_info|os\.name|"
                      r"platform\.|shutil\.which|importlib|find_spec|os\.environ|os\.getenv|sys\.maxsize|HAS_\w+|"
                      r"\w+\s+is\s+None)")


def check_tests(fd):
    out = []
    if not TEST_PATH.search(fd.path):
        return out
    for no, text in fd.added:
        m = SKIP.search(text)
        if m and ENV_SKIP.search(text):
            continue                                    # skipped where it cannot run (platform, version, a missing tool)
        if m and not _in_string(text, m.start()):      # a skip marker inside a string or regex is test data
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
            # a flag is a boolean: a flag-like name with a non-boolean default (should_abort=None, a callback) is not
            has_default = "=" in p
            if BOOL_DEFAULT.search(p) or (FLAG_NAME.search(pn) and not has_default):
                out.append(Finding(
                    "flag-parameter", fd.path, no,
                    f"`{name}` grew a flag `{pn}`. Which callers pass it, and why is this not two functions, "
                    f"or a fix of the one?", p))
    return out


# ---------- growth ----------

def check_growth(fd, limit):
    grew = len(fd.added) - len(fd.removed)
    if TEST_PATH.search(fd.path):
        return []                                       # tests grow with every fix; that is not code to split out
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
MIN_TOKENS = 30      # from here a near copy counts (90% the same)
MIN_SHORT = 8        # shorter functions count only as exact copies (names renamed): `make_slug` = `slugify`


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


def _sources(root, exts, rev=None):
    """(relative path, text) of the code to compare against: the working tree, or the tree of commit `rev` (for
    --range: what existed before the change, not what came later)."""
    if rev is None:
        for p in _walk(root, exts):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    yield os.path.relpath(p, root).replace(os.sep, "/"), f.read()
            except OSError:
                continue
        return
    names = _git(root, "ls-tree", "-r", "--name-only", rev).splitlines()
    for n, rel in enumerate(r for r in names if os.path.splitext(r)[1].lower() in exts):
        if n >= 5000:
            return
        try:
            yield rel, _git(root, "show", f"{rev}:{rel}")
        except (OSError, RuntimeError):
            continue


def check_duplicates(files, root, threshold=0.9, rev=None):
    out = []
    new_funcs = []
    for fd in files:
        if fd.ext in CODE_EXT:
            for name, no, body in _functions(fd.added, fd.ext):
                if TEST_PATH.search(fd.path) and re.match(r"(?i)^test", name):
                    continue                           # test cases look alike by nature; copied helpers still count
                toks = _norm(body)
                if len(toks) >= MIN_SHORT:
                    new_funcs.append((fd, name, no, toks))
    if not new_funcs:
        return out
    exts = {fd.ext for fd, *_ in new_funcs}
    corpus = []
    for rel, text in _sources(root, exts, rev):
        lines = list(enumerate(text.splitlines(), 1))
        ext = os.path.splitext(rel)[1].lower()
        for name, no, body in _functions(lines, ext):
            corpus.append((rel, ext, name, no, _norm(body)))
    for fd, name, no, toks in new_funcs:
        best = None
        short = len(toks) < MIN_TOKENS
        for rel, ext, oname, ono, otoks in corpus:
            if ext != fd.ext or (rel == fd.path and oname == name) or len(otoks) < MIN_SHORT:
                continue
            if short or len(otoks) < MIN_TOKENS:
                if toks == otoks:                      # a short function counts only as an exact copy
                    best = (1.0, rel, oname, ono)
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


# ---------- baseline: findings a person already acknowledged ----------

def _norm_snippet(snippet):
    return " ".join(snippet.split())


def fingerprints(findings):
    """One sha256 per finding: file + kind + normalized snippet + occurrence index (1-based, in scan order).

    The occurrence index is what keeps two identical copies apart: the second copy gets index 2 and stays visible.
    """
    seen, out = {}, []
    for f in findings:
        key = (f.file, f.kind, _norm_snippet(f.snippet))
        seen[key] = seen.get(key, 0) + 1
        raw = "|".join([f.file, f.kind, key[2], str(seen[key])])
        out.append(hashlib.sha256(raw.encode("utf-8")).hexdigest())
    return out


def write_baseline(path, findings):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "fingerprints": fingerprints(findings)}, fh, indent=2)


def apply_baseline(findings, path):
    """Drop findings whose fingerprint is in the baseline file. Returns (kept findings, hidden count)."""
    with open(path, encoding="utf-8") as fh:
        known = set(json.load(fh)["fingerprints"])
    prints = fingerprints(findings)
    kept = [f for f, p in zip(findings, prints) if p not in known]
    return kept, len(findings) - len(kept)


# ---------- driver ----------

# ---------- scope: what was changed that nobody asked for ----------

FILE_MENTION = re.compile(r"[\w./\\-]*\w\.[A-Za-z]\w{0,5}\b")
CODE_IDENT = re.compile(r"`([^`]+)`|\b([A-Za-z_]\w*_\w*|[a-z]+[A-Z]\w*|[A-Za-z_]\w*(?=\s*\())")
WORD = re.compile(r"[A-Za-z][\w-]{2,}")
STOP = {"the", "fix", "fixes", "fixed", "and", "for", "with", "this", "that", "make", "please", "add", "bug",
        "change", "update", "file", "code", "test", "tests", "into", "from", "when", "should", "not", "but"}
TEST_STEM = re.compile(r"^(test_|tests?_)|(_test|_spec|\.test|\.spec)$")


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0].lower()


def check_scope(files, task):
    """Files the task text never points at: by path, by file name, or by an identifier in the changed lines."""
    text = task.replace("\\", "/")
    mentions = {m.lower().strip("./") for m in FILE_MENTION.findall(text)}
    idents = {g for m in CODE_IDENT.finditer(text) for g in m.groups() if g}
    idents = {i.strip().rstrip("()") for i in idents if len(i.strip()) >= 3}
    words = {w.lower().replace("-", "_") for w in WORD.findall(FILE_MENTION.sub(" ", text))} - STOP

    def named(fd):
        """'strong' = the task clearly points here; 'weak' = a word matches part of the name (exempt, not proof)."""
        p = fd.path.lower()
        stem = _stem(fd.path).replace("-", "_")
        dirs = {d.replace("-", "_") for d in p.split("/")[:-1]}
        changed = "\n".join([t for _, t in fd.added + fd.removed] + fd.headers)
        if (any(p == m or p.endswith("/" + m) or os.path.basename(p) == m for m in mentions)
                or stem in words or dirs & words
                or any(re.search(r"(?<!\w)" + re.escape(i) + r"(?!\w)", changed) for i in idents)):
            return "strong"
        if {x for x in re.split(r"[._]", stem) if len(x) >= 3} & words:
            return "weak"
        return None

    strength = {fd.path: named(fd) for fd in files}
    inside = {p for p, s in strength.items() if s}
    # flags only when the task gives a strong anchor; a weak match alone would turn guesses into accusations
    if not any(s == "strong" for s in strength.values()):
        return [Finding("scope-unclear", "(task)", 0,
                        "The task names no file, file name or identifier found in this diff, so its scope "
                        "cannot be checked. Which files did you mean?")]
    stems = {_stem(p) for p in inside}
    # a test of an in-scope file is in scope; so are the helpers next to such a test (fakes, fixtures)
    inside |= {fd.path for fd in files if TEST_STEM.sub("", _stem(fd.path)) in stems}
    test_dirs = {os.path.dirname(p) for p in inside if TEST_STEM.search(_stem(p))}
    out = []
    for fd in files:
        if fd.path in inside or os.path.dirname(fd.path) in test_dirs:
            continue
        line = (fd.added or fd.removed or [(0, "")])[0][0]
        out.append(Finding("out-of-scope", fd.path, line,
                           "You did not ask for this file to change. Why was it changed, and should it be undone?"))
    return out


# ---------- sessions: edits this session did not make ----------

EDIT_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def _key(p):
    return os.path.normcase(os.path.abspath(p))


def _edits(transcript, want=None):
    """(path key, iso time) for every Write/Edit/MultiEdit/NotebookEdit a Claude Code transcript records, and the
    cwd it ran in. Bash edits (sed, a script) are not recorded as file paths, so they cannot be attributed.
    want: file names; a transcript whose text holds none of them is skipped without parsing (a project folder can
    hold gigabytes of transcripts: 31 s on a real one before this)."""
    out, cwd = [], None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out, cwd
    if want is not None and not any(w in text for w in want):
        return out, cwd
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        cwd = cwd or rec.get("cwd")
        content = (rec.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                p = (b.get("input") or {}).get(EDIT_TOOLS[b["name"]])
                if isinstance(p, str) and p:
                    out.append((_key(p), str(rec.get("timestamp", ""))[:16].replace("T", " ")))
    return out, cwd


def _is_worktree(cwd):
    """A git worktree (.git is a file pointing elsewhere), a path under a 'worktrees' folder, or a folder that is gone
    - the cleaned-up worktree of the complaint. A plain folder is not: a subagent may be told to write outside it."""
    if not os.path.isdir(cwd):
        return True
    parts = {x.lower() for x in re.split(r"[\\/]", cwd)}
    return os.path.isfile(os.path.join(cwd, ".git")) or "worktrees" in parts


def _under(path_key, dir_path):
    d = _key(dir_path)
    return path_key == d or path_key.startswith(d.rstrip("\\/") + os.sep)


def find_session(spec, root):
    """A transcript path as given, or ID / latest under <config>/projects/<the folder name Claude Code makes>."""
    if spec.endswith(".jsonl") and os.path.isfile(spec):
        return spec
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    proj = os.path.join(base, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(root)))
    if spec == "latest":
        js = [os.path.join(proj, n) for n in os.listdir(proj) if n.endswith(".jsonl")] if os.path.isdir(proj) else []
        return max(js, key=os.path.getmtime) if js else None
    p = os.path.join(proj, spec + ".jsonl")
    return p if os.path.isfile(p) else None


def check_sessions(files, root, transcript):
    """Files in the diff this session never edited, named with the session that did (from the other transcripts of
    the same project folder), and edits a subagent of this session made outside its own worktree."""
    proj = os.path.dirname(os.path.abspath(transcript))
    sid = os.path.splitext(os.path.basename(transcript))[0]
    mine = {p for p, _ in _edits(transcript)[0]}
    out, sub_author = [], {}
    sub_dir = os.path.join(proj, sid, "subagents")
    for name in sorted(os.listdir(sub_dir)) if os.path.isdir(sub_dir) else []:
        if not name.endswith(".jsonl"):
            continue
        edits, cwd = _edits(os.path.join(sub_dir, name))
        for p, ts in edits:
            mine.add(p)
            sub_author[p] = name[:-6]
            if cwd and _key(cwd) != _key(root) and _is_worktree(cwd) and not _under(p, cwd):
                rel = os.path.relpath(p, _key(root)).replace("\\", "/") if _under(p, root) else p
                out.append(Finding("subagent-outside-worktree", rel, 0,
                                   f"Subagent {name[:-6]} ran in its worktree {cwd} but edited this file outside it at "
                                   f"{ts} (a resumed agent whose worktree was cleaned up writes into the main "
                                   f"project). Did you mean that change to land here?"))
    want = {os.path.basename(fd.path) for fd in files if _key(os.path.join(root, fd.path)) not in mine}
    others = {}
    if not want:
        return out
    for dirpath, _, names in os.walk(proj):
        for name in names:
            path = os.path.join(dirpath, name)
            if not name.endswith(".jsonl") or os.path.abspath(path) == os.path.abspath(transcript) \
                    or _under(_key(path), sub_dir):
                continue
            who = os.path.splitext(name)[0] if dirpath == proj else os.path.relpath(path, proj).replace("\\", "/")
            for p, ts in _edits(path, want)[0]:
                if p not in others or ts > others[p][1]:
                    others[p] = (who, ts)
    flagged = {f.file for f in out}
    for fd in files:
        p = _key(os.path.join(root, fd.path))
        if p in mine or fd.path in flagged:
            continue
        line = (fd.added or fd.removed or [(0, "")])[0][0]
        if p in others:
            who, ts = others[p]
            out.append(Finding("other-session", fd.path, line,
                               f"This session never edited this file; session {who} did, at {ts}. Two sessions in "
                               f"one working tree see each other's edits: is this change yours to commit?"))
        else:
            out.append(Finding("not-this-session", fd.path, line,
                               "This session never edited this file with Write or Edit, and no other session of this "
                               "project did. A hand edit, a Bash command (sed, a script), or a session in another "
                               "folder?"))
    return out


# ---------- levels and repo idiom ----------

# only these hide a failure and fail the run; the rest is worth a look but does not (measured on real commits:
# with every kind failing, 46 of 75 findings on the author's own repos failed the run, 0 of them real defects)
FAIL_KINDS = {"swallowed-exception", "weakened-test", "out-of-scope", "other-session", "subagent-outside-worktree"}
MARKER = re.compile(r"(noqa:?\s*[A-Z]+\d+|type:\s*ignore|@ts-ignore|eslint-disable[\w-]*|pylint:\s*disable|"
                    r"pyright:\s*ignore|nolint)")


def _idiom_uses(repo, marker, exclude, cache):
    """How many times the marker appears at HEAD in files this diff does NOT touch (so the change never counts
    itself, whether it is committed in a --range or still uncommitted)."""
    if marker not in cache:
        spec = ["--", "."] + [f":(exclude){p}" for p in exclude]
        r = subprocess.run(["git", "-C", repo, "grep", "-c", "-F", marker, "HEAD", *spec], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        cache[marker] = sum(int(l.rsplit(":", 1)[1]) for l in r.stdout.splitlines() if l.rsplit(":", 1)[-1].isdigit())
    return cache[marker]


def mark_idioms(found, files, repo, threshold=3):
    """A silencing marker the repo already used >= threshold times elsewhere is its idiom: info + a note."""
    cache, touched = {}, [fd.path for fd in files]
    for f in found:
        m = MARKER.search(f.snippet) if f.kind == "silenced-check" else None
        if not m:
            continue
        before = _idiom_uses(repo, m.group(1), touched, cache)
        if before >= threshold:
            f.level = "info"
            f.question += f" (repo idiom: `{m.group(1)}` is used {before} times in files this change does not touch.)"


def scan(diff_text, root=None, grow_limit=400, task=None, repo=None, rev=None, session=None, session_root=None):
    files = parse_diff(diff_text)
    found = check_scope(files, task) if task else []
    if session:
        found += check_sessions(files, session_root or repo or root or ".", session)
    for fd in files:
        found += check_lines(fd)
        found += check_tests(fd)
        found += check_flags(fd)
        found += check_growth(fd, grow_limit)
    if root:
        found += check_duplicates(files, root, rev=rev)
    for f in found:
        f.level = "fail" if f.kind in FAIL_KINDS and not f.idiom else "info"
        if f.idiom:
            f.question += f" (Known shape: {f.idiom}; listed, not failed.)"
    if repo or root:
        mark_idioms(found, files, repo or root)
    order = {fd.path: i for i, fd in enumerate(files)}
    found.sort(key=lambda f: (f.level != "fail", order.get(f.file, -1), f.line, f.kind))
    return found


def render_md(findings, source):
    if not findings:
        return f"# Under the bed: clean\n\nNothing swept under the bed in {source}.\n"
    fails = [f for f in findings if f.level == "fail"]
    infos = [f for f in findings if f.level != "fail"]
    out = [f"# Under the bed: {len(fails)} thing(s) that hide a failure, {len(infos)} worth a look",
           "", f"Source: {source}. Paste these back to the assistant, one by one."]
    for title, group in (("", fails), ("Worth a look (does not fail the run)", infos)):
        if not group:
            continue
        if title:
            out += ["", f"## {title}"]
        cur = None
        for f in group:
            if f.file != cur:
                cur = f.file
                out += ["", f"{'###' if title else '##'} {cur}", ""]
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
    ap.add_argument("--baseline", metavar="FILE", help="hide the findings recorded in this baseline file")
    ap.add_argument("--write-baseline", metavar="FILE", help="record the current findings in FILE and exit 0")
    ap.add_argument("--strict", action="store_true", help="every finding fails the run (exit 1), not only the ones that hide a failure")
    ap.add_argument("--task", help="what you asked the assistant to do: files it never points at are questioned")
    ap.add_argument("--task-file", metavar="FILE", help="the same, read from a file (e.g. TASK.md)")
    ap.add_argument("--session", metavar="ID|FILE|latest", help="this Claude Code session (id, transcript path, or "
                    "latest): files it never edited are named with the session or subagent that did")
    ap.add_argument("--session-root", metavar="DIR", help="the folder the session ran in (default: the repo)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    task = a.task
    if a.task_file:
        try:
            with open(a.task_file, encoding="utf-8", errors="replace") as f:
                task = f.read()
        except OSError as e:
            print(f"under-the-bed: task file unreadable: {e}", file=sys.stderr)
            return 2

    root, rev = None, None
    try:
        if a.diff:
            if a.diff == "-":
                text = sys.stdin.read()
            else:
                with open(a.diff, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            source = "stdin" if a.diff == "-" else a.diff
        else:
            root = _git(a.cwd, "rev-parse", "--show-toplevel").strip()
            if a.staged:
                text, source = _git(root, "diff", "--cached", "-U3"), "staged changes"
            elif a.range:
                rng = a.range if ".." in a.range else f"{a.range}..HEAD"
                text, source = _git(root, "diff", "-U3", rng), rng
                # copies are looked for in the code as it was before the change, not in today's files
                start = rng.split("..")[0].strip(".") or "HEAD"
                rev = _git(root, "merge-base", start, rng.split("..")[-1].strip(".") or "HEAD").strip() \
                    if "..." in rng else start
            else:
                text, source = _git(root, "diff", "HEAD", "-U3"), "uncommitted changes (git diff HEAD)"
    except (OSError, RuntimeError) as e:
        print(f"under-the-bed: {e}", file=sys.stderr)
        return 2

    session = None
    if a.session:
        session = find_session(a.session, a.session_root or root or a.cwd)
        if not session:
            print(f"under-the-bed: no transcript for session {a.session!r} of {a.session_root or root or a.cwd}",
                  file=sys.stderr)
            return 2
    findings = scan(text, root=None if a.no_dupes else root, grow_limit=a.grow_limit, task=task, repo=root, rev=rev,
                    session=session, session_root=a.session_root or root)
    if a.write_baseline:
        write_baseline(a.write_baseline, findings)
        print(f"baseline: {len(findings)} finding(s) recorded in {a.write_baseline}", file=sys.stderr)
        return 0
    if a.baseline:
        try:
            findings, hidden = apply_baseline(findings, a.baseline)
        except (OSError, ValueError, KeyError) as e:
            print(f"under-the-bed: baseline unreadable: {e}", file=sys.stderr)
            return 2
        if hidden:
            print(f"baseline: {hidden} acknowledged finding(s) hidden", file=sys.stderr)
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
    return 1 if (findings if a.strict else [f for f in findings if f.level == "fail"]) else 0


if __name__ == "__main__":
    sys.exit(main())
