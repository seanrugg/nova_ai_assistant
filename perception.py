#!/usr/bin/env python3
"""
perception.py — Nova's visual awareness engine
Runs via coral_env venv (for tflite compatibility).

Captures frames from the LifeCam every CAPTURE_INTERVAL seconds,
sends them to Ollama's vision model for rich scene description,
and writes the result to ~/.nova_perception.json for nova.py to read.

Usage:
    ~/coral_env/bin/python3 perception.py

Output:
    ~/.nova_perception.json  — updated every CAPTURE_INTERVAL seconds

Dependencies (coral_env):
    Pillow

System dependencies (already installed):
    ffmpeg
    Ollama (gemma3:4b with vision)
"""

import json
import os
import sys
import time
import signal
import subprocess
import tempfile
import base64
import urllib.request
from datetime import datetime

from PIL import Image

# ── Configuration ─────────────────────────────────────────────────────────────

CAMERA_DEVICE    = "/dev/video0"
CAPTURE_INTERVAL = 30.0              # seconds between vision updates
OUTPUT_PATH      = os.path.expanduser("~/.nova_perception.json")
OLLAMA_URL       = "http://localhost:11434/api/generate"
OLLAMA_MODEL     = "gemma3:4b"

VISION_PROMPT = (
    "You are Nova's eyes. Describe what you see in this image in 2-3 sentences. "
    "Be specific and factual — mention people, objects, lighting, and activity. "
    "Do not speculate about things you cannot see. "
    "Write as if giving a quiet, observant report to Nova so she can be aware of her surroundings."
)

# ── Capture frame ─────────────────────────────────────────────────────────────

def capture_frame():
    """Capture a single JPEG frame using ffmpeg. Returns path to temp file or None."""
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-i", CAMERA_DEVICE,
            "-frames:v", "1",
            "-q:v", "3",
            tmp.name
        ], capture_output=True, timeout=10)

        if result.returncode == 0 and os.path.exists(tmp.name):
            return tmp.name
        return None
    except Exception as e:
        print(f"Capture error: {e}")
        return None

# ── Describe frame ────────────────────────────────────────────────────────────

def describe_frame(image_path):
    """Send image to Ollama vision model. Returns description string or None."""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": VISION_PROMPT,
            "images": [img_b64],
            "stream": False
        }).encode("utf-8")

        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()

    except Exception as e:
        print(f"Vision error: {e}")
        return None

# ── Write output ──────────────────────────────────────────────────────────────

def write_observation(summary):
    """Atomic write to output JSON file."""
    observation = {
        "timestamp": datetime.now().isoformat(),
        "summary": summary,
    }
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(observation, f, indent=2)
    os.replace(tmp, OUTPUT_PATH)

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print(f"👁️  Perception running — interval: {CAPTURE_INTERVAL}s — output: {OUTPUT_PATH}")

    running = True
    def shutdown(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        loop_start = time.time()

        image_path = capture_frame()
        if image_path is None:
            print("Camera capture failed — retrying...")
            time.sleep(5)
            continue

        try:
            description = describe_frame(image_path)
            if description:
                write_observation(description)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {description[:80]}...")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] No description returned")
        except Exception as e:
            print(f"Error: {e}")
        finally:
            try:
                os.unlink(image_path)
            except Exception:
                pass

        elapsed = time.time() - loop_start
        sleep_time = max(0, CAPTURE_INTERVAL - elapsed)
        time.sleep(sleep_time)

    print("Perception stopped.")

if __name__ == "__main__":
    main()
