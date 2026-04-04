#!/usr/bin/env python3
"""
perception.py — Nova's visual awareness engine
Runs on the Coral TPU via coral_env venv.

Captures frames from the LifeCam using ffmpeg, runs person and object
detection on the Coral TPU, and writes structured environment observations
to a shared JSON file that nova.py reads periodically.

Usage:
    ~/coral_env/bin/python3 perception.py

Output:
    ~/.nova_perception.json  — updated every CAPTURE_INTERVAL seconds

Dependencies (coral_env):
    tflite-runtime==2.17.0
    numpy==1.26.4
    Pillow

System dependencies (already installed):
    ffmpeg
    libedgetpu.so.1
"""

import json
import os
import sys
import time
import signal
import subprocess
import tempfile
from datetime import datetime

import numpy as np
import tflite_runtime.interpreter as tflite
from PIL import Image

# ── Configuration ─────────────────────────────────────────────────────────────

CAMERA_DEVICE        = "/dev/video0"
CAPTURE_INTERVAL     = 5.0               # seconds between perception updates
CONFIDENCE_THRESHOLD = 0.45             # minimum detection confidence
MAX_DETECTIONS       = 5                # max objects to report per frame
INPUT_SIZE           = 300              # SSD MobileNet input size

MODEL_DIR    = os.path.expanduser("~/coral_models")
MODEL_PATH   = os.path.join(MODEL_DIR, "ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite")
CPU_MODEL    = os.path.join(MODEL_DIR, "ssd_mobilenet_v2_coco_quant_postprocess.tflite")
LABELS_PATH  = os.path.join(MODEL_DIR, "coco_labels.txt")
OUTPUT_PATH  = os.path.expanduser("~/.nova_perception.json")
EDGETPU_LIB  = "libedgetpu.so.1"

# Objects meaningful to report in a home/barn environment
RELEVANT_LABELS = {
    "person", "cat", "dog", "laptop", "cell phone", "book",
    "cup", "bottle", "chair", "couch", "tv", "keyboard",
    "mouse", "remote", "backpack", "sports ball", "bicycle"
}

# ── Load labels ───────────────────────────────────────────────────────────────

def load_labels(path):
    with open(path, "r") as f:
        lines = f.read().splitlines()
    labels = {}
    for line in lines:
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        # Format: "0 person"
        if len(parts) == 2 and parts[0].isdigit():
            labels[int(parts[0])] = parts[1]
        # Format: "person" — label only
        else:
            labels[len(labels)] = parts[0]
    return labels

# ── Load model ────────────────────────────────────────────────────────────────

def load_model():
    """Try Coral TPU first, fall back to CPU."""
    try:
        delegate = tflite.load_delegate(EDGETPU_LIB)
        interpreter = tflite.Interpreter(
            model_path=MODEL_PATH,
            experimental_delegates=[delegate]
        )
        interpreter.allocate_tensors()
        print("✅ Coral TPU loaded")
        return interpreter, True
    except Exception as e:
        print(f"⚠️  Coral TPU unavailable ({e}) — falling back to CPU")
        try:
            interpreter = tflite.Interpreter(model_path=CPU_MODEL)
            interpreter.allocate_tensors()
            print("✅ CPU model loaded")
            return interpreter, False
        except Exception as e2:
            print(f"❌ CPU model also failed: {e2}")
            sys.exit(1)

# ── Capture frame ─────────────────────────────────────────────────────────────

def capture_frame():
    """Capture a single frame from the camera using ffmpeg. Returns PIL Image or None."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp_path = f.name
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-i", CAMERA_DEVICE,
            "-frames:v", "1",
            "-q:v", "2",
            tmp_path
        ], capture_output=True, timeout=10)

        if result.returncode != 0 or not os.path.exists(tmp_path):
            return None

        img = Image.open(tmp_path).convert("RGB")
        return img
    except Exception as e:
        print(f"Frame capture error: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

# ── Run inference ─────────────────────────────────────────────────────────────

def run_inference(interpreter, image):
    """Run object detection on a PIL image. Returns boxes, classes, scores."""
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    # Resize to model input size
    resized = image.resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
    input_data = np.expand_dims(np.array(resized, dtype=np.uint8), axis=0)

    interpreter.set_tensor(input_details[0]['index'], input_data)
    interpreter.invoke()

    boxes   = interpreter.get_tensor(output_details[0]['index'])[0]
    classes = interpreter.get_tensor(output_details[1]['index'])[0]
    scores  = interpreter.get_tensor(output_details[2]['index'])[0]

    return boxes, classes, scores

# ── Build observation ─────────────────────────────────────────────────────────

def build_observation(boxes, classes, scores, labels, using_tpu):
    """Convert raw detections into a structured observation dict."""
    detections = []
    person_count = 0

    for i in range(min(len(scores), MAX_DETECTIONS)):
        if scores[i] < CONFIDENCE_THRESHOLD:
            continue
        label_id = int(classes[i])
        label = labels.get(label_id, f"object_{label_id}")

        if label not in RELEVANT_LABELS:
            continue

        if label == "person":
            person_count += 1

        detections.append({
            "label": label,
            "confidence": float(round(scores[i], 2)),
        })

    # Build natural language summary
    if not detections:
        summary = "The room appears empty — no people or notable objects detected."
    else:
        parts = []
        if person_count == 1:
            parts.append("1 person is present")
        elif person_count > 1:
            parts.append(f"{person_count} people are present")

        objects = [d["label"] for d in detections if d["label"] != "person"]
        if objects:
            unique_objects = list(dict.fromkeys(objects))
            parts.append(f"visible objects include: {', '.join(unique_objects)}")

        summary = ". ".join(parts).capitalize() + "."

    return {
        "timestamp": datetime.now().isoformat(),
        "using_tpu": using_tpu,
        "person_count": person_count,
        "detections": detections,
        "summary": summary,
    }

# ── Write output ──────────────────────────────────────────────────────────────

def write_observation(observation):
    """Atomic write to output file."""
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(observation, f, indent=2)
    os.replace(tmp, OUTPUT_PATH)

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(MODEL_PATH):
        print(f"Model not found: {MODEL_PATH}")
        print("Run setup_coral_models.sh to download required models")
        sys.exit(1)

    if not os.path.exists(LABELS_PATH):
        print(f"Labels not found: {LABELS_PATH}")
        sys.exit(1)

    labels = load_labels(LABELS_PATH)
    interpreter, using_tpu = load_model()

    print(f"👁️  Perception running — interval: {CAPTURE_INTERVAL}s — output: {OUTPUT_PATH}")

    running = True
    def shutdown(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        loop_start = time.time()

        image = capture_frame()
        if image is None:
            print("Camera capture failed — retrying...")
            time.sleep(2)
            continue

        try:
            boxes, classes, scores = run_inference(interpreter, image)
            observation = build_observation(boxes, classes, scores, labels, using_tpu)
            write_observation(observation)
            print(f"[{observation['timestamp'][:19]}] {observation['summary']}")
        except Exception as e:
            print(f"Inference error: {e}")

        elapsed = time.time() - loop_start
        sleep_time = max(0, CAPTURE_INTERVAL - elapsed)
        time.sleep(sleep_time)

    print("Perception stopped.")

if __name__ == "__main__":
    main()
