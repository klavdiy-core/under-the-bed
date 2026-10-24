"""Render examples/scope.png from the tool's REAL output on examples/scope.diff with --task.

    pip install pillow ; python examples/make_scope_png.py
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TASK = "Fix the rounding in total(): it should round to cents."
W, PAD, LINE = 1000, 26, 24
BG, FG, DIM, GREEN, YELLOW, CYAN, RED = (24, 26, 33), (220, 223, 228), (120, 126, 140), (120, 200, 120), (230, 200, 110), (110, 190, 230), (235, 120, 110)


def font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    return ImageFont.load_default()


MONO, BOLD = font(["consola.ttf", "DejaVuSansMono.ttf"], 17), font(["consolab.ttf", "DejaVuSansMono-Bold.ttf"], 17)


def main():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "under_the_bed.py"), "--diff",
                          os.path.join(HERE, "scope.diff"), "--task", TASK],
                         capture_output=True, text=True, encoding="utf-8").stdout
    files = [l[3:] for l in out.splitlines() if l.startswith("## ")]
    diff = open(os.path.join(HERE, "scope.diff"), encoding="utf-8").read().splitlines()
    changed = [l[6:] for l in diff if l.startswith("+++ b/")]
    rows = [("you asked:  " + TASK, BOLD, CYAN), ("assistant:  Done! Totals round to cents now.", BOLD, GREEN), ("", MONO, FG),
            ("$ git diff --name-only", MONO, DIM)]
    rows += [("  " + c, MONO, FG) for c in changed]
    rows += [("", MONO, FG), (f'$ python under_the_bed.py --task "{TASK}"', MONO, DIM), ("", MONO, FG)]
    rows += [(out.splitlines()[0].lstrip("# "), BOLD, YELLOW)]
    for f in files:
        snip = next((l[1:].strip() for l in diff[diff.index(f"+++ b/{f}"):] if l.startswith("+") and not l.startswith("+++")), "")
        rows.append((f"  {f:<28} {snip}", MONO, RED))
    rows += [("", MONO, FG), ("  -> \"You did not ask for this file to change. Why was it changed?\"", MONO, DIM)]
    img = Image.new("RGB", (W, PAD * 2 + LINE * len(rows)), BG)
    d = ImageDraw.Draw(img)
    for i, (t, f, c) in enumerate(rows):
        d.text((PAD, PAD + i * LINE), t, font=f, fill=c)
    p = os.path.join(HERE, "scope.png")
    img.save(p)
    print(p, img.size, files)


if __name__ == "__main__":
    main()
