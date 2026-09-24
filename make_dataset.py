"""Build a labelled screen-content dataset without labelling anything by hand.

Idea
----
You supply two small folders of raw screenshots:

    base/      clean screenshots of the exam window, full screen, nothing else
               (10-30 is plenty: different questions, scroll positions, themes)

    overlay/   screenshots of things that must not be on screen during an exam

               FLAT (binary model):  drop 20-40 images straight in overlay/
               and you get two classes, clean vs flagged.

               SUBFOLDERS (multi-class): group them by CATEGORY and each
               subfolder becomes its own class, e.g.

                   overlay/ai_chat/     ChatGPT, Gemini, Claude, Copilot
                   overlay/search_web/  Google, Bing, Wikipedia, StackOverflow
                   overlay/document/    PDF, Word, Docs, OneNote
                   overlay/messaging/   WhatsApp Web, Telegram, Discord

               Category, not product: chat UIs all look alike so the model
               would confuse ChatGPT with Gemini, and product redesigns break
               a product-specific model while a category model survives them.

               Multi-class lets an open-book exam permit `document` while
               still flagging `ai_chat`, which binary cannot express.
               Aim for 15+ screenshots per category.

This script composites overlay windows onto the base screenshots at random
positions, sizes and occlusions. Because YOU decide whether an overlay was
pasted, the label is free. One afternoon of screenshotting turns into
thousands of labelled training images.

    clean/     base screenshot, lightly augmented        -> class 0
    flagged/   base screenshot + a visible other window  -> class 1

Realism matters more than volume here. The script therefore varies:
  - overlay size (a snapped half-screen window vs a small floating one)
  - position (including partially off-screen, like a dragged window)
  - a drop shadow and a title-bar strip, so the model learns "a window is
    on top of the exam", not "there is a rectangle of different pixels"
  - global brightness/contrast, JPEG quality, and slight blur, so it
    survives different monitors and capture settings

Usage
-----
    python make_dataset.py --base base --overlay overlay --out dataset \
                           --per-base 40

Then train with train_screen.py.
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# A flagged window must cover at least this fraction of the screen to count.
# Below it, the window is arguably not readable and the label would be noise.
MIN_COVER = 0.06
MAX_COVER = 0.55


def list_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.suffix.lower() in IMG_EXT and p.is_file())


def overlay_groups(folder: Path) -> dict[str, list[Path]]:
    """Return {class_name: [paths]}.

    Subfolders become classes. A flat folder collapses to one 'flagged' class,
    so the binary and multi-class workflows share this one code path.
    """
    subdirs = sorted(d for d in folder.iterdir() if d.is_dir())
    if subdirs:
        groups = {d.name: list_images(d) for d in subdirs}
        empty = [k for k, v in groups.items() if not v]
        if empty:
            raise SystemExit(f"empty overlay categories: {', '.join(empty)}")
        return groups
    return {"flagged": list_images(folder)}


def fit_screen(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Cover-crop to the target aspect, so every sample is one screen shape."""
    tw, th = size
    scale = max(tw / im.width, th / im.height)
    im = im.resize((max(1, round(im.width * scale)),
                    max(1, round(im.height * scale))), Image.LANCZOS)
    left = (im.width - tw) // 2
    top = (im.height - th) // 2
    return im.crop((left, top, left + tw, top + th))


def add_titlebar(win: Image.Image) -> Image.Image:
    """Give the overlay a title bar, so it reads as a window, not a patch."""
    bar_h = max(6, round(win.height * 0.045))
    bar = Image.new("RGB", (win.width, bar_h),
                    random.choice([(238, 238, 238), (60, 60, 62), (32, 33, 36)]))
    out = Image.new("RGB", (win.width, win.height + bar_h))
    out.paste(bar, (0, 0))
    out.paste(win, (0, bar_h))
    return out


def drop_shadow(canvas: Image.Image, box: tuple[int, int, int, int],
                blur: int = 12, alpha: int = 90) -> Image.Image:
    """Darken behind the window edges, as a real compositor would."""
    shadow = Image.new("L", canvas.size, 0)
    x0, y0, x1, y1 = box
    pad = blur // 2
    Image.Image.paste(
        shadow, Image.new("L", (x1 - x0 + pad * 2, y1 - y0 + pad * 2), alpha),
        (x0 - pad, y0 - pad))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    dark = Image.new("RGB", canvas.size, (0, 0, 0))
    return Image.composite(dark, canvas, shadow.point(lambda v: v // 2))


def paste_window(base: Image.Image, overlay: Image.Image) -> Image.Image:
    """Composite one overlay window onto the base screenshot."""
    sw, sh = base.size
    target = random.uniform(MIN_COVER, MAX_COVER)

    # Window aspect: snapped-half, floating, or wide-short (a chat panel).
    aspect = random.choice([0.5 * sw / sh, random.uniform(0.6, 1.9),
                            random.uniform(1.4, 2.4)])
    area = target * sw * sh
    wh = max(40, round((area / aspect) ** 0.5))
    ww = max(40, round(wh * aspect))
    ww, wh = min(ww, round(sw * 0.98)), min(wh, round(sh * 0.98))

    win = fit_screen(overlay, (ww, wh))
    if random.random() < 0.85:
        win = add_titlebar(win)
    ww, wh = win.size

    # Position, sometimes hanging off an edge like a dragged window.
    if random.random() < 0.25:
        x = random.choice([random.randint(-ww // 3, 0),
                           random.randint(sw - ww, sw - ww + ww // 3)])
        y = random.randint(-wh // 6, sh - wh + wh // 6)
    else:
        x = random.randint(0, max(0, sw - ww))
        y = random.randint(0, max(0, sh - wh))

    out = base.copy()
    box = (max(0, x), max(0, y), min(sw, x + ww), min(sh, y + wh))
    if box[2] - box[0] < 20 or box[3] - box[1] < 20:
        return out                      # degenerate placement, skip overlay
    out = drop_shadow(out, box)
    out.paste(win, (x, y))
    return out


def augment(im: Image.Image) -> Image.Image:
    """Capture-condition noise, applied to BOTH classes so it can't leak."""
    if random.random() < 0.7:
        im = ImageEnhance.Brightness(im).enhance(random.uniform(0.75, 1.25))
    if random.random() < 0.7:
        im = ImageEnhance.Contrast(im).enhance(random.uniform(0.8, 1.2))
    if random.random() < 0.3:
        im = im.filter(ImageFilter.GaussianBlur(random.uniform(0.3, 1.1)))
    return im


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, required=True)
    ap.add_argument("--overlay", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("dataset"))
    ap.add_argument("--per-base", type=int, default=40,
                    help="images generated per base screenshot, per class")
    ap.add_argument("--size", type=int, nargs=2, default=(1280, 720),
                    help="output screen size W H")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    random.seed(args.seed)
    bases = list_images(args.base)
    if not bases:
        raise SystemExit(f"no images in {args.base}")
    groups = overlay_groups(args.overlay)

    size = tuple(args.size)
    classes = ["clean"] + list(groups)
    dirs = {c: args.out / c for c in classes}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    counts = {c: 0 for c in classes}
    names = list(groups)

    for bi, bp in enumerate(bases):
        with Image.open(bp) as raw:
            base = fit_screen(raw.convert("RGB"), size)

        for k in range(args.per_base):
            augment(base).save(dirs["clean"] / f"{bi:03d}_{k:03d}.jpg",
                               quality=random.randint(72, 95))
            counts["clean"] += 1

            # Round-robin the categories, so each gets equal representation
            # however many screenshots you happened to collect for it.
            cls = names[(bi * args.per_base + k) % len(names)]
            with Image.open(random.choice(groups[cls])) as ov:
                comp = paste_window(base, ov.convert("RGB"))
            # Sometimes a second window of the SAME category - mixing
            # categories would make the label ambiguous.
            if random.random() < 0.18:
                with Image.open(random.choice(groups[cls])) as ov2:
                    comp = paste_window(comp, ov2.convert("RGB"))
            augment(comp).save(dirs[cls] / f"{bi:03d}_{k:03d}.jpg",
                               quality=random.randint(72, 95))
            counts[cls] += 1

    total_ov = sum(len(v) for v in groups.values())
    print(f"[done] -> {args.out}")
    for c in classes:
        print(f"       {c:<14} {counts[c]}")
    print(f"       from {len(bases)} base and {total_ov} overlay images "
          f"in {len(groups)} categor{'y' if len(groups) == 1 else 'ies'}")
    print("\nSanity check before training: open a few flagged images and")
    print("confirm the overlay is genuinely readable. If it is not, the")
    print("label is wrong and the model will learn noise.")


if __name__ == "__main__":
    main()
