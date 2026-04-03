#!/usr/bin/env python3
"""
Nova - AI Companion for the Rugge Family
Runs on Jetson Orin Nano with Ollama (gemma4:e2b), Faster-Whisper STT, and Piper TTS.

Hardware:
  - Mic:    Microsoft LifeCam Cinema  (ALSA hw:2,0)
  - Camera: /dev/video0 (Microsoft LifeCam Cinema)
  - Audio:  DisplayPort monitor speakers (PulseAudio default sink)

Usage:
  python3 nova.py                  # Vision-based speaker identification (default)
  python3 nova.py --user devyn     # Override: skip vision, load Devyn's profile
  python3 nova.py --user sean
  python3 nova.py --user jihan
  python3 nova.py --user dahna
  python3 nova.py --user yumi
  python3 nova.py --user unknown   # Generic visitor mode

Family config: ~/nova_config/family_config.py  (private — not in GitHub repo)
Press Ctrl+C to exit.
"""

import subprocess
import tempfile
import os
import sys
import wave
import struct
import time
import urllib.request
import urllib.error
import json
import argparse
import base64
import importlib.util
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────

MIC_DEVICE        = "hw:2,0"
MIC_RATE          = 16000
MIC_CHANNELS      = 1
MIC_FORMAT        = "S16_LE"
RECORD_SECONDS    = 5

PIPER_BIN         = os.path.expanduser("~/.local/bin/piper")
PIPER_VOICE       = os.path.expanduser("~/piper-voices/en_GB-alba-medium.onnx")
PIPER_RATE        = 22050

OLLAMA_URL        = "http://localhost:11434/api/chat"
OLLAMA_MODEL      = "gemma4:e2b"

WHISPER_MODEL     = "base"
SILENCE_THRESHOLD = 500

CAMERA_DEVICE     = "/dev/video0"
VISION_SNAPSHOT   = "/tmp/nova_vision.jpg"

# ─── Load family config ───────────────────────────────────────────────────────

def load_family_config():
    """Load private family config if present, otherwise return None."""
    config_path = Path.home() / "nova_config" / "family_config.py"
    try:
        spec = importlib.util.spec_from_file_location("family_config", config_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print(f"✅ Family config loaded from {config_path}")
        return mod
    except FileNotFoundError:
        print(f"ℹ️  No family config found at {config_path} — running in generic mode")
        return None
    except Exception as e:
        print(f"⚠️  Family config error: {e} — running in generic mode")
        return None


def get_member_by_name(family_config, name):
    """Look up a family member by name or alias (case-insensitive)."""
    if not family_config:
        return None
    name_lower = name.lower().strip()
    for member in family_config.FAMILY_MEMBERS:
        if member["name"].lower() == name_lower:
            return member
        if any(alias.lower() == name_lower for alias in member.get("aliases", [])):
            return member
    return None


def build_system_prompt(family_config, member):
    """Build a complete system prompt for the identified family member."""
    if not family_config:
        return GENERIC_SYSTEM_PROMPT

    identity = family_config.NOVA_IDENTITY.strip()

    if member is None:
        profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
        style = family_config.PERSONAS["adult"]["style"].strip()
        return f"{identity}\n\n---\n\n{style}\n\n---\n\n{profile}"

    persona_key = member.get("persona", "adult")
    persona = family_config.PERSONAS.get(persona_key, family_config.PERSONAS["adult"])
    style = persona["style"].strip()
    profile = member["profile"].strip()
    name = member["name"]

    return (
        f"{identity}\n\n"
        f"---\n\n"
        f"You are currently speaking with {name}. Here is what you know about them:\n\n"
        f"{profile}\n\n"
        f"---\n\n"
        f"Interaction style for this conversation:\n{style}"
    )


# ─── Generic fallback prompt (no family config) ───────────────────────────────

GENERIC_SYSTEM_PROMPT = """You are Nova, a warm and friendly AI companion.
You are helpful, curious, and genuinely care about the people you talk with.
Adapt your tone to the person — playful with children, natural and direct with adults.
Keep responses appropriately concise. Ask follow-up questions to stay engaged.
"""

# ─── Vision identification ────────────────────────────────────────────────────

def capture_frame(output_path=VISION_SNAPSHOT):
    """Capture a single frame from the webcam using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-i", CAMERA_DEVICE,
        "-frames:v", "1",
        "-q:v", "2",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and os.path.exists(output_path)


def image_to_base64(path):
    """Read an image file and return base64-encoded string."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def identify_person_via_vision(family_config):
    """
    Capture a frame and ask gemma4:e2b to identify who is in the image.
    Returns a family member dict, or None for unknown.
    """
    if not family_config:
        return None

    print("📷 Capturing image for identification...")
    if not capture_frame():
        print("   ⚠️  Camera capture failed — skipping vision ID")
        return None

    try:
        img_b64 = image_to_base64(VISION_SNAPSHOT)
    except Exception as e:
        print(f"   ⚠️  Could not read image: {e}")
        return None

    # Build name list for the prompt
    names = [m["name"] for m in family_config.FAMILY_MEMBERS]
    names_str = ", ".join(names)

    prompt = (
        f"Look at this image carefully. The person in the image is a member of the Rugge family. "
        f"Their names are: {names_str}. "
        f"Reply with ONLY the single first name of the person you see, exactly as listed. "
        f"If you cannot identify them or no person is clearly visible, reply with exactly: Unknown"
    )

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [img_b64]
            }
        ],
        "stream": False
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data["message"]["content"].strip()
            print(f"   Vision result: '{raw}'")

            # Clean up the response — model sometimes adds punctuation
            identified_name = raw.strip(".,!? ").split()[0] if raw else "Unknown"

            if identified_name.lower() == "unknown":
                return None

            member = get_member_by_name(family_config, identified_name)
            if member:
                print(f"   ✅ Identified: {member['name']}")
                return member
            else:
                print(f"   ⚠️  '{identified_name}' not matched in family config")
                return None

    except Exception as e:
        print(f"   ⚠️  Vision identification error: {e}")
        return None


def confirm_identification(member, speak_fn):
    """
    Ask Nova to confirm identification aloud. Returns True if confirmed,
    False if the person says no (triggering a fallback ask).
    """
    name = member["name"]
    speak_fn(f"Oh! Is that you, {name}?")

    # Listen for a yes/no
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name

    if not record_audio(tmp, seconds=4):
        os.unlink(tmp)
        return True  # assume yes on capture failure

    try:
        from faster_whisper import WhisperModel
        wm = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _ = wm.transcribe(tmp, language="en")
        response = " ".join(s.text.strip() for s in segments).strip().lower()
        os.unlink(tmp)
        print(f"   Confirmation response: '{response}'")
        # Simple yes/no detection
        if any(word in response for word in ["no", "nope", "not", "wrong", "nah"]):
            return False
        return True  # default to confirmed
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return True


# ─── Audio helpers ────────────────────────────────────────────────────────────

def record_audio(filename, seconds=RECORD_SECONDS):
    """Record audio from the mic using arecord."""
    print(f"🎤 Listening for {seconds} seconds...")
    cmd = [
        "arecord",
        "-D", MIC_DEVICE,
        "-f", MIC_FORMAT,
        "-r", str(MIC_RATE),
        "-c", str(MIC_CHANNELS),
        "-d", str(seconds),
        filename
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def is_silent(filename, threshold=SILENCE_THRESHOLD):
    """Return True if the recording is basically silence."""
    try:
        with wave.open(filename, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            samples = struct.unpack(f"{len(frames)//2}h", frames)
            rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
            print(f"   Audio RMS: {rms:.0f} (threshold: {threshold})")
            return rms < threshold
    except Exception as e:
        print(f"   Warning: could not check silence: {e}")
        return False


def speak(text):
    """Convert text to speech using Piper and play via aplay."""
    print(f"🔊 Nova: {text}")
    try:
        piper_cmd = [
            PIPER_BIN,
            "--model", PIPER_VOICE,
            "--output_raw"
        ]
        aplay_cmd = [
            "aplay",
            "-r", str(PIPER_RATE),
            "-f", "S16_LE",
            "-c", "1"
        ]
        piper_proc = subprocess.Popen(
            piper_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        aplay_proc = subprocess.Popen(
            aplay_cmd,
            stdin=piper_proc.stdout,
            stderr=subprocess.DEVNULL
        )
        piper_proc.stdin.write(text.encode("utf-8"))
        piper_proc.stdin.close()
        piper_proc.stdout.close()
        aplay_proc.wait()
        piper_proc.wait()
    except Exception as e:
        print(f"   TTS error: {e}")


# ─── Ollama ───────────────────────────────────────────────────────────────────

def ask_ollama(messages):
    """Send conversation to Ollama and return Nova's reply."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["message"]["content"].strip()
    except Exception as e:
        print(f"   Ollama error: {e}")
        return "I had a little hiccup there. Could you say that again?"


def check_ollama():
    """Check if Ollama is running."""
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=3)
        return True
    except Exception:
        return False


# ─── Greeting ─────────────────────────────────────────────────────────────────

def build_greeting(member):
    """Return an appropriate greeting for the identified person."""
    if member is None:
        return "Hello! I'm Nova. I don't think we've met — what's your name?"

    name = member["name"]
    persona = member.get("persona", "adult")
    nickname = member.get("aliases", [name])[0] if member.get("aliases") else name

    if persona == "child":
        return f"Hi {nickname}! It's me, Nova! Beep boop! What do you want to talk about today?"
    elif persona == "teen":
        return f"Hey {name}! Good to see you. What's going on?"
    else:
        return f"Hello {name}. Nova here — what's on your mind?"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova AI Companion")
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="Override vision ID — specify family member name (devyn, jihan, dahna, yumi, sean, unknown)"
    )
    args = parser.parse_args()

    print("=" * 55)
    print("  Nova — AI Companion")
    print("  Press Ctrl+C to exit")
    print("=" * 55)

    # Startup checks
    if not check_ollama():
        print("❌ Ollama is not running. Start it with: ollama serve")
        sys.exit(1)
    print("✅ Ollama connected")

    if not os.path.exists(PIPER_BIN):
        print(f"❌ Piper not found at {PIPER_BIN}")
        sys.exit(1)
    print("✅ Piper ready")

    if not os.path.exists(PIPER_VOICE):
        print(f"❌ Voice model not found at {PIPER_VOICE}")
        sys.exit(1)
    print("✅ Voice model ready")

    # Load family config
    family_config = load_family_config()

    # Load Whisper once
    print("⏳ Loading speech recognition model...")
    try:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("✅ Whisper ready")
    except Exception as e:
        print(f"❌ Could not load Whisper: {e}")
        sys.exit(1)

    print()

    # ── Identify speaker ──
    member = None

    if args.user:
        # Manual override
        if args.user.lower() == "unknown":
            member = None
            print("ℹ️  Running in unknown visitor mode (manual override)")
        else:
            member = get_member_by_name(family_config, args.user)
            if member:
                print(f"ℹ️  User override: {member['name']}")
            else:
                print(f"⚠️  '{args.user}' not found in family config — running as unknown visitor")
    else:
        # Vision identification
        if family_config and os.path.exists(CAMERA_DEVICE):
            member = identify_person_via_vision(family_config)
            if member:
                # Confirm identification aloud
                confirmed = confirm_identification(member, speak)
                if not confirmed:
                    # Ask directly
                    speak("Sorry about that! Who am I talking to?")
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        tmp = f.name
                    if record_audio(tmp, seconds=4):
                        segments, _ = whisper_model.transcribe(tmp, language="en")
                        name_said = " ".join(s.text.strip() for s in segments).strip()
                        os.unlink(tmp)
                        member = get_member_by_name(family_config, name_said.split()[0] if name_said else "")
                    else:
                        os.unlink(tmp)
                        member = None
        else:
            print("ℹ️  No camera or family config — running in generic mode")

    # ── Build system prompt for this person ──
    system_prompt = build_system_prompt(family_config, member)

    # ── Greet ──
    greeting = build_greeting(member)
    messages = [{"role": "system", "content": system_prompt}]
    speak(greeting)
    messages.append({"role": "assistant", "content": greeting})

    print()

    # ── Conversation loop ──
    while True:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_audio = f.name

            if not record_audio(tmp_audio):
                print("   Recording failed, retrying...")
                time.sleep(1)
                continue

            if is_silent(tmp_audio):
                print("   (silence — listening again...)\n")
                os.unlink(tmp_audio)
                continue

            print("💭 Thinking...")
            segments, _ = whisper_model.transcribe(tmp_audio, language="en")
            user_text = " ".join(seg.text.strip() for seg in segments).strip()
            os.unlink(tmp_audio)

            if not user_text:
                print("   (nothing heard — listening again...)\n")
                continue

            # Label the speaker in the log
            speaker_label = member["name"] if member else "Visitor"
            print(f"👤 {speaker_label}: {user_text}")

            messages.append({"role": "user", "content": user_text})
            reply = ask_ollama(messages)
            messages.append({"role": "assistant", "content": reply})

            speak(reply)
            print()

            # Trim history: keep system prompt + last 20 turns
            if len(messages) > 21:
                messages = [messages[0]] + messages[-20:]

        except KeyboardInterrupt:
            farewell = "Goodbye! Talk to you soon."
            if member and member.get("persona") == "child":
                farewell = "Bye bye! See you soon! Beep boop!"
            elif member and member.get("persona") == "teen":
                farewell = f"Later, {member['name']}. Take care."
            print(f"\n\n👋 {farewell}")
            speak(farewell)
            break
        except Exception as e:
            print(f"   Unexpected error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
