#!/usr/bin/env python3
"""
perception.py — Nova's visual awareness engine
Runs on the Coral TPU via coral_env venv.

This script runs as a background process, capturing frames from the LifeCam,
running person and object detection on the Coral TPU, and writing structured
environment observations to a shared JSON file that nova.py reads periodically.

Usage:
    ~/coral_env/bin/python3 perception.py

Output:
    ~/.nova_perception.json  — updated every CAPTURE_INTERVAL seconds

Dependencies (coral_env):
    tflite-runtime==2.17.0
    numpy<2
    opencv-python

Models required (~/coral_models/):
    ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite
    coco_labels.txt
"""

import json
import os
import sys
import time
import subprocess
import threading
import signal
from datetime import datetime

import numpy as np
import tflite_runtime.interpreter as tflite
import cv2

# ── Configuration ─────────────────────────────────────────────────────────────

CAMERA_DEVICE       = 0                  # /dev/video0 — LifeCam
CAPTURE_INTERVAL    = 5.0               # seconds between perception updates
CONFIDENCE_THRESHOLD = 0.45             # minimum detection confidence
MAX_DETECTIONS      = 5                 # max objects to report per frame
INPUT_SIZE          = 300               # SSD MobileNet input size

MODEL_DIR           = os.path.expanduser("~/coral_models")
MODEL_PATH          = os.path.join(MODEL_DIR, "ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite")
LABELS_PATH         = os.path.join(MODEL_DIR, "coco_labels.txt")
OUTPUT_PATH         = os.path.expanduser("~/.nova_perception.json")
EDGETPU_LIB         = "libedgetpu.so.1"

# Objects that are meaningful to report in a home environment
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
        if len(parts) == 2:
            labels[int(parts[0])] = parts[1]
        elif len(parts) == 1:
            labels[len(labels)] = parts[0]
    return labels

# ── Load model ────────────────────────────────────────────────────────────────

def load_model():
    try:
        delegate = tflite.load_delegate(EDGETPU_LIB)
        interpreter = tflite.Interpreter(
            model_path=MODEL_PATH,
            experimental_delegates=[delegate]
        )
        interpreter.allocate_tensors()
        print("Coral TPU loaded successfully")
        return interpreter, True
    except Exception as e:
        print(f"Coral TPU unavailable ({e}) — falling back to CPU")
        interpreter = tflite.Interpreter(model_path=MODEL_PATH.replace(
            "_edgetpu.tflite", ".tflite"
        ))
        interpreter.allocate_tensors()
        return interpreter, False

# ── Run inference ─────────────────────────────────────────────────────────────

def run_inference(interpreter, frame):
    """Run object detection on a frame. Returns list of detections."""
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    # Resize and preprocess
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (INPUT_SIZE, INPUT_SIZE))
    input_data = np.expand_dims(resized, axis=0).astype(np.uint8)

    interpreter.set_tensor(input_details[0]['index'], input_data)
    interpreter.invoke()

    # SSD MobileNet outputs: boxes, classes, scores, count
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
            unique_objects = list(dict.fromkeys(objects))  # deduplicate, preserve order
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
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(observation, f, indent=2)
    os.replace(tmp, OUTPUT_PATH)  # atomic write

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    # Verify model files exist
    if not os.path.exists(MODEL_PATH):
        print(f"Model not found: {MODEL_PATH}")
        print("Run setup_coral_models.sh to download required models")
        sys.exit(1)

    if not os.path.exists(LABELS_PATH):
        print(f"Labels not found: {LABELS_PATH}")
        sys.exit(1)

    labels = load_labels(LABELS_PATH)
    interpreter, using_tpu = load_model()

    # Open camera
    cap = cv2.VideoCapture(CAMERA_DEVICE)
    if not cap.isOpened():
        print(f"Could not open camera at /dev/video{CAMERA_DEVICE}")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    print(f"Camera opened — perception running every {CAPTURE_INTERVAL}s")
    print(f"Output: {OUTPUT_PATH}")

    # Handle Ctrl+C gracefully
    running = True
    def shutdown(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        loop_start = time.time()

        ret, frame = cap.read()
        if not ret:
            print("Camera read failed — retrying...")
            time.sleep(1)
            continue

        try:
            boxes, classes, scores = run_inference(interpreter, frame)
            observation = build_observation(boxes, classes, scores, labels, using_tpu)
            write_observation(observation)
            print(f"[{observation['timestamp'][:19]}] {observation['summary']}")
        except Exception as e:
            print(f"Inference error: {e}")

        # Sleep for remainder of interval
        elapsed = time.time() - loop_start
        sleep_time = max(0, CAPTURE_INTERVAL - elapsed)
        time.sleep(sleep_time)

    cap.release()
    print("Perception stopped.")

if __name__ == "__main__":
    main()
