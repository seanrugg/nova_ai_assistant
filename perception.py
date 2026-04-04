#!/usr/bin/env python3
"""
perception.py — Nova's visual awareness engine
Runs via coral_env venv (for tflite compatibility).

Captures frames from the LifeCam at startup and every CAPTURE_INTERVAL seconds,
sends them to Ollama's vision model for rich scene description, and writes the
result to ~/.nova_perception.json for nova.py to read.

Usage:
    ~/coral_env/bin/python3 perception.py

Optional flags:
    --once              Capture a single frame, print result, and exit (for testing)
    --interval SECS     Override CAPTURE_INTERVAL at runtime

Output:
    ~/.nova_perception.json  — updated at startup, then every CAPTURE_INTERVAL seconds

Dependencies (coral_env):
    Pillow

System dependencies (already installed):
    ffmpeg
    Ollama (gemma3:4b with vision)
"""

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

from PIL import Image

# ── Configuration ─────────────────────────────────────────────────────────────

CAMERA_DEVICE    = "/dev/video0"
CAPTURE_INTERVAL = 300.0             # seconds between vision updates (5 minutes)
OUTPUT_PATH      = os.path.expanduser("~/.nova_perception.json")
OLLAMA_URL       = "http://localhost:11434/api/generate"
OLLAMA_MODEL     = "gemma3:4b"
OLLAMA_TIMEOUT   = 30                # seconds to wait for vision response
FRAME_QUALITY    = 2                 # ffmpeg -q:v (1=best, 31=worst; 2 is excellent)

# Vision prompt — direct, factual, example-anchored to suppress preamble
VISION_PROMPT = (
    "Describe this image in 2-3 short sentences. "
    "State only what is clearly visible: people, objects, lighting, activity. "
    "Be direct and factual. Do not greet, introduce yourself, or add any preamble. "
    "Example output: A man sits at a desk with a laptop. "
    "The room is warmly lit. A dog is visible in the background."
)

# ── Preamble stripping ────────────────────────────────────────────────────────

def strip_preamble(text):
    """
    Remove conversational preamble that vision models sometimes add despite the prompt.
    Handles patterns like:
      "Here's what I see: A man sits..."
      "Sure! Here is a description: The room..."
      "Okay, looking at the image: ..."
    Strategy: if the text opens with a known filler phrase followed by a colon,
    strip everything up to and including that colon.
    """
    text = text.strip()
    cleaned = re.sub(
        r'^(?:(?:here(?:\'s| is)|okay|sure|alright|certainly|of course)[^.!?\n]*?[:\-]\s*'
        r'|[^.!?\n]{0,60}?:\s*)',
        '',
        text,
        count=1,
        flags=re.IGNORECASE
    ).strip()
    # Only accept the stripped version if it left a meaningful sentence
    return cleaned if len(cleaned) > 20 else text

# ── Capture frame ─────────────────────────────────────────────────────────────

def capture_frame():
    """Capture a single JPEG frame using ffmpeg. Returns path to temp file or None."""
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, prefix="nova_frame_")
    tmp.close()
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-i", CAMERA_DEVICE,
            "-frames:v", "1",
            "-q:v", str(FRAME_QUALITY),
            tmp.name
        ], capture_output=True, timeout=10)

        if result.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
            return tmp.name
        return None
    except subprocess.TimeoutExpired:
        print("[perception] ffmpeg timed out", flush=True)
        return None
    except Exception as e:
        print(f"[perception] Capture error: {e}", flush=True)
        return None

# ── Describe frame ────────────────────────────────────────────────────────────

def describe_frame(image_path):
    """Send image to Ollama vision model. Returns cleaned description string or None."""
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

        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("response", "").strip()
            return strip_preamble(raw) if raw else None

    except urllib.error.URLError as e:
        print(f"[perception] Ollama unreachable: {e}", flush=True)
        return None
    except Exception as e:
        print(f"[perception] Vision error: {e}", flush=True)
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

# ── Single pass ───────────────────────────────────────────────────────────────

def run_once():
    """Capture one frame, describe it, write output. Returns True on success."""
    image_path = capture_frame()
    if image_path is None:
        print("[perception] Camera capture failed", flush=True)
        return False
    try:
        description = describe_frame(image_path)
        if description:
            write_observation(description)
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {description}", flush=True)
            return True
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No description returned", flush=True)
            return False
    finally:
        try:
            os.unlink(image_path)
        except Exception:
            pass

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova Visual Awareness Engine")
    parser.add_argument(
        "--once", action="store_true",
        help="Capture one frame, print result, and exit (for testing)"
    )
    parser.add_argument(
        "--interval", type=float, default=CAPTURE_INTERVAL,
        help=f"Override capture interval in seconds (default: {CAPTURE_INTERVAL})"
    )
    args = parser.parse_args()

    if args.once:
        print("[perception] Running single capture...", flush=True)
        success = run_once()
        sys.exit(0 if success else 1)

    interval = args.interval
    print(f"👁️  Perception running — interval: {interval}s — output: {OUTPUT_PATH}", flush=True)

    running = True
    def shutdown(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Capture immediately at startup so nova.py has context before first conversation
    print("[perception] Initial capture...", flush=True)
    run_once()

    # Then loop on the interval
    while running:
        loop_start = time.time()

        if not run_once():
            # On failure, wait a short retry interval rather than the full cycle
            time.sleep(min(10, interval))
            continue

        elapsed = time.time() - loop_start
        sleep_time = max(0, interval - elapsed)
        time.sleep(sleep_time)

    print("Perception stopped.", flush=True)

if __name__ == "__main__":
    main()
