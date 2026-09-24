"""Page classifier: the MobileNetV2 trained in Colab.

Preprocessing matches training exactly: fix phone rotation (EXIF),
shrink to 512px, centre-square crop, resize to the model's size,
then the backbone's own preprocess_input.
"""
import json
import threading
import time
from functools import lru_cache

import numpy as np
from django.conf import settings
from PIL import Image, ImageOps

_lock = threading.Lock()


@lru_cache(maxsize=1)
def _load_once():
    t0 = time.time()
    import tensorflow as tf
    cfg = json.loads((settings.ML_DIR / "config.json").read_text())
    model = tf.keras.models.load_model(
        settings.ML_DIR / "model.keras", compile=False)
    apps = tf.keras.applications
    preprocess = {
        "mobilenetv2": apps.mobilenet_v2.preprocess_input,
        "resnet50": apps.resnet50.preprocess_input,
        "vgg16": apps.vgg16.preprocess_input,
    }[cfg["model"]]
    print(f"[model] loaded in {time.time() - t0:.1f}s")
    return model, preprocess, cfg


def _load():
    with _lock:          # never load twice at the same time
        return _load_once()


# Start loading as soon as the server starts, so the first
# student doesn't wait for TensorFlow.
threading.Thread(target=_load, daemon=True).start()


def classify(path):
    """Return {class_name: probability}, highest first."""
    model, preprocess, cfg = _load()
    t0 = time.time()
    size = cfg["img_size"]
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((512, 512), Image.LANCZOS)   # as in training
        w, h = im.size
        s = min(w, h)
        left, top = (w - s) // 2, (h - s) // 2
        im = im.crop((left, top, left + s, top + s))
        im = im.resize((size, size), Image.BILINEAR)
        x = np.asarray(im, dtype=np.float32)[None]
    probs = model(preprocess(x), training=False).numpy()[0]
    print(f"[classify] {time.time() - t0:.2f}s")
    ranked = sorted(zip(cfg["classes"], probs.tolist()),
                    key=lambda t: -t[1])
    return dict(ranked)