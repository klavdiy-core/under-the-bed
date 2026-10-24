"""Render examples/demo.gif from the tool's REAL output on examples/ai_cleanup.diff.

Nothing in the picture is typed by hand: the diff stat is counted from the diff,
the report is what under_the_bed.py prints when this script runs it.

    pip install pillow
    python examples/make_demo_gif.py
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIFF = os.path.join(HERE, "ai_cleanup.diff")
OUT = os.path.join(HERE, "demo.gif")

W, H = 960, 450
PAD, LINE = 22, 22
BG, FG, DIM = (24, 26, 33), (220, 223, 228), (120, 126, 140)
GREEN, RED, YELLOW, CYAN = (120, 200, 120), (230, 110, 110), (230, 200, 110), (110, 190, 230)


def font(bold=False):
    for name in (("consolab.ttf", "DejaVuSansMono-Bold.ttf") if bold else ("consola.ttf", "DejaVuSansMono.ttf")):
        try:
            return ImageFont.truetype(name, 16)
        except OSError:
            pass
    return ImageFont.load_default()


REG, BOLD = font(), font(True)


def diff_stat():
    stat, current = {}, None
    with open(DIFF, encoding="utf-8") as f:
        for line in f:
            if line.startswith("+++ b/"):
                current = line[6:].strip()
                stat[current] = [0, 0]
            elif current and line.startswith("+") and not line.startswith("+++"):
                stat[current][0] += 1
            elif current and line.startswith("-") and not line.startswith("---"):
                stat[current][1] += 1
    return stat


def report():
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "under_the_bed.py"), "--diff", DIFF],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
    ).stdout
    lines, head = [], None
    for line in out.splitlines():
        if line.startswith("# "):
            lines.append((line[2:], BOLD, YELLOW))
        elif line.startswith("## Worth a look"):
            lines.append((line[3:], BOLD, DIM))
        elif line.startswith(("## ", "### ")):
            lines.append((line.lstrip("# "), BOLD, CYAN))
        elif line.startswith("- [ ] **"):
            head = line[8:].partition("**:")[0]
        elif head and line.startswith("  `"):
            lines.append((f"  {head:<26} {line.strip().strip('`')}", REG, FG))
            head = None
    return lines


def fit(text, f):
    if f.getlength(text) <= W - 2 * PAD:
        return text
    while f.getlength(text + "…") > W - 2 * PAD:
        text = text[:-1]
    return text + "…"


def frame(rows, caption):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    for text, f, color in rows:
        d.text((PAD, y), fit(text, f), font=f, fill=color)
        y += LINE
    d.rectangle((0, H - 40, W, H), fill=(40, 43, 54))
    d.text((PAD, H - 30), caption, font=BOLD, fill=FG)
    return img


def main():
    before = [("assistant: Fixed the EU tax bug. Tests pass. Done!", BOLD, GREEN), ("", REG, FG),
              ("$ git diff --stat", REG, DIM)]
    for path, (plus, minus) in diff_stat().items():
        before.append((f" {path:<24} | {plus + minus:>2} " + "+" * plus + "-" * minus, REG, FG))
    before += [("", REG, FG), ("Looks tidy. Commit?", REG, DIM)]

    cmd = "$ python under_the_bed.py --diff examples/ai_cleanup.diff"
    after_head = before[:0] + [(cmd, REG, DIM), ("", REG, FG)]
    rep = report()

    frames, durations = [], []
    for i in range(3, len(before) + 1):
        frames.append(frame(before[:i], "BEFORE: the assistant says it is done"))
        durations.append(500)
    durations[-1] = 2200
    for i in range(0, len(cmd) + 1, 4):
        frames.append(frame([(cmd[:i], REG, DIM)], "under-the-bed reads the diff"))
        durations.append(60)
    for i in range(1, len(rep) + 1):
        frames.append(frame(after_head + rep[:i], "AFTER: what was swept under the bed"))
        durations.append(350)
    tail = [("", REG, FG), ("Each line comes with a question to paste back to the assistant.", REG, DIM)]
    frames.append(frame(after_head + rep + tail, "AFTER: what was swept under the bed"))
    durations.append(350)
    durations[-1] = 5000

    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    print(f"{OUT}: {len(frames)} frames, {os.path.getsize(OUT) // 1024} KB, {len(rep)} report lines")


if __name__ == "__main__":
    main()
