"""Grab screenshots automatically while you browse, so building the dataset
is two minutes of clicking instead of an hour of pressing PrtScn.

Run it, then just use your computer normally. It saves a full-screen shot
every few seconds into the folder you name.

    pip install mss

    # 1) exam screens: open SnapNPlan's quiz page, then run
    python capture.py --out base --every 3 --count 30
    #    ...and for 90 seconds: scroll, change question, resize, switch
    #    light/dark, open a different subject. Variety is what matters.

    # 2) the things that must not be on screen
    python capture.py --out overlay --every 3 --count 40
    #    ...and cycle through ChatGPT, Gemini, Google results, a PDF, Word,
    #    WhatsApp Web, VS Code, YouTube. A few seconds each.

Then:
    python make_dataset.py --base base --overlay overlay --out dataset --per-base 40

A countdown gives you time to switch windows before the first shot, and each
capture prints a dot so you can see it working without alt-tabbing back.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True,
                    help="folder to write into (base/ or overlay/)")
    ap.add_argument("--every", type=float, default=3.0,
                    help="seconds between shots")
    ap.add_argument("--count", type=int, default=30,
                    help="how many shots to take")
    ap.add_argument("--delay", type=int, default=5,
                    help="countdown before starting, to switch windows")
    ap.add_argument("--monitor", type=int, default=1,
                    help="which monitor (1 = primary)")
    args = ap.parse_args()

    try:
        import mss
        import mss.tools
    except ImportError:
        sys.exit("pip install mss")

    args.out.mkdir(parents=True, exist_ok=True)
    existing = len(list(args.out.glob("shot_*.png")))

    print(f"Capturing {args.count} shots, one every {args.every}s, "
          f"into {args.out}/")
    for s in range(args.delay, 0, -1):
        print(f"  starting in {s}...", end="\r", flush=True)
        time.sleep(1)
    print(" " * 30, end="\r")

    with mss.mss() as sct:
        if args.monitor >= len(sct.monitors):
            sys.exit(f"no monitor {args.monitor}; "
                     f"found {len(sct.monitors) - 1}")
        mon = sct.monitors[args.monitor]
        for i in range(args.count):
            shot = sct.grab(mon)
            path = args.out / f"shot_{existing + i:03d}.png"
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
            print(f"  [{i + 1}/{args.count}] {path.name}", end="\r", flush=True)
            if i < args.count - 1:
                time.sleep(args.every)

    total = len(list(args.out.glob("shot_*.png")))
    print(f"\nDone. {args.out}/ now holds {total} screenshots.")
    print("\nDelete any that are wrong (a half-open menu, your desktop, "
          "this terminal) before generating the dataset - a mislabelled "
          "base image poisons every sample derived from it.")


if __name__ == "__main__":
    main()
