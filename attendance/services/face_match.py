"""
services/face_match.py

1:1 face verification for check-in selfies, CPU-only (no Coral / no GPU).

Design notes (why it's built this way):
  * Verification is ONE-TO-ONE: we already know whose check-in this is (from the
    session or the foreman's selection), so we only compare the selfie against
    that ONE enrolled face. Database size is irrelevant — 100 or 10,000 enrolled
    faces cost the same, because we never search the gallery.
  * The expensive step is the embedding (one neural-net inference, a few hundred
    ms on a Pi 5 CPU). It runs ONCE per check-in and does not grow with the
    number of employees. The comparison itself is a cosine similarity between
    two vectors — microseconds.
  * EVERYTHING here is best-effort and OPTIONAL. If numpy / tflite / the model
    file / OpenCV aren't present, every function returns None and the caller
    simply stores no score. The app never hard-depends on face matching, exactly
    like the existing _detect_face cascade.
  * The result is a similarity in [0,1]. We DO NOT gate attendance on it — it's
    a flag for HR review (green/amber/red), never an automatic rejection. A
    threshold on a CPU model produces occasional false rejects (same person, bad
    light) and the odd false accept, so a human still decides.

Model file (optional): a MobileFaceNet / FaceNet-style TFLite embedding model at
$FACE_MATCH_MODEL (default /var/lib/attendance/models/face_embedding.tflite),
input NxN RGB, output a 1-D embedding vector. Install it on the Pi to enable
scoring; leave it absent to disable the feature with zero code changes.
"""
import os
import functools

MODEL_PATH = os.getenv("FACE_MATCH_MODEL",
                       "/var/lib/attendance/models/face_embedding.tflite")

# Similarity thresholds for the HR badge (cosine similarity, 0..1).
# Deliberately conservative: below AMBER is "probably not the same person",
# between AMBER and GREEN is "review", at/above GREEN is "likely match".
THRESHOLD_GREEN = 0.65
THRESHOLD_AMBER = 0.45


def is_available() -> bool:
    """True only if every optional piece needed to score is present."""
    if not os.path.exists(MODEL_PATH):
        return False
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    if _load_interpreter() is None:
        return False
    return True


@functools.lru_cache(maxsize=1)
def _load_interpreter():
    """Load the TFLite interpreter once. Prefers the lightweight tflite_runtime
    (what you'd install on a Pi); falls back to full TensorFlow if present.
    Returns None if neither is installed or the model can't be loaded."""
    if not os.path.exists(MODEL_PATH):
        return None
    Interpreter = None
    # Preferred on current Raspberry Pi OS (Python 3.11/3.12): Google LiteRT,
    # the maintained successor to tflite-runtime with modern ARM64 wheels.
    try:
        from ai_edge_litert.interpreter import Interpreter  # type: ignore
    except ImportError:
        # Legacy tflite_runtime (Python <=3.9), then full TensorFlow.
        try:
            from tflite_runtime.interpreter import Interpreter  # type: ignore
        except ImportError:
            try:
                from tensorflow.lite import Interpreter  # type: ignore
            except ImportError:
                return None
    try:
        interp = Interpreter(model_path=MODEL_PATH)
        interp.allocate_tensors()
        return interp
    except Exception:
        return None


def _detect_and_crop(bgr, np):
    """Find the largest face and return it cropped, or the whole frame if no
    detector / no face (the embedding model is robust to a centered portrait)."""
    try:
        import cv2
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, 1.1, 4)
        if len(faces):
            x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
            # pad 20% around the box, clamped to the image
            pad = int(0.2 * max(w, h))
            y0, y1 = max(0, y - pad), min(bgr.shape[0], y + h + pad)
            x0, x1 = max(0, x - pad), min(bgr.shape[1], x + w + pad)
            return bgr[y0:y1, x0:x1]
    except Exception:
        pass
    return bgr


def embed(jpeg_bytes):
    """Return an L2-normalized embedding (numpy 1-D array) for a JPEG, or None
    if scoring isn't available or the image can't be processed."""
    interp = _load_interpreter()
    if interp is None:
        return None
    try:
        import numpy as np
        import cv2
    except ImportError:
        return None
    try:
        bgr = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8),
                           cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        crop = _detect_and_crop(bgr, np)

        inp = interp.get_input_details()[0]
        _, ih, iw, _ = inp["shape"]
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (int(iw), int(ih)))

        if inp["dtype"] == np.uint8:
            tensor = np.expand_dims(rgb.astype(np.uint8), 0)
        else:
            # float model: normalize to [-1, 1]
            tensor = np.expand_dims((rgb.astype(np.float32) - 127.5) / 128.0, 0)

        interp.set_tensor(inp["index"], tensor)
        interp.invoke()
        out = interp.get_tensor(interp.get_output_details()[0]["index"])
        vec = np.asarray(out, dtype=np.float32).flatten()
        norm = np.linalg.norm(vec)
        if norm == 0:
            return None
        return vec / norm
    except Exception:
        return None


def similarity(vec_a, vec_b):
    """Cosine similarity mapped to [0,1] for two L2-normalized vectors, or None."""
    if vec_a is None or vec_b is None:
        return None
    try:
        import numpy as np
        cos = float(np.dot(vec_a, vec_b))          # already normalized → [-1,1]
        return max(0.0, min(1.0, (cos + 1.0) / 2.0))
    except Exception:
        return None


def compare_jpegs(selfie_bytes, enrolled_bytes):
    """Convenience: embed both images and return their similarity, or None.
    This is the 1:1 check — one selfie against one enrolled face."""
    a = embed(selfie_bytes)
    if a is None:
        return None
    b = embed(enrolled_bytes)
    if b is None:
        return None
    return similarity(a, b)


def badge_for(score):
    """Map a score to a semantic label for the HR UI. Unknown → 'none'."""
    if score is None:
        return "none"
    if score >= THRESHOLD_GREEN:
        return "match"
    if score >= THRESHOLD_AMBER:
        return "review"
    return "mismatch"
