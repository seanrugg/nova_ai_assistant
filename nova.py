#!/usr/bin/env python3
"""
Nova - AI companion for the Rugge family
Runs on Jetson Orin Nano with Ollama (gemma3:4b), Faster-Whisper STT, and Piper TTS.

Hardware:
  - Mic:    Microsoft LifeCam Cinema (PulseAudio via parec)
  - Audio:  DisplayPort monitor speakers (PulseAudio default sink)
  - Camera: /dev/video0 (Microsoft LifeCam Cinema)

Usage:
  python3 nova.py                # vision-based speaker ID
  python3 nova.py --user sean    # skip vision, identify as Sean

Press Ctrl+C to exit.
"""

import argparse
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
import base64
import signal

# ─── Configuration ────────────────────────────────────────────────────────────

# Mic — PulseAudio via parec (confirmed working on JetPack 6.2.2 / NVMe boot)
MIC_SOURCE        = "alsa_input.usb-Microsoft_Microsoft___LifeCam_Cinema_TM_-02.mono-fallback"
MIC_RATE          = 16000            # Hz — Whisper native rate
MIC_CHANNELS      = 1               # Mono

# Smart listening — silence detection
CHUNK_SECONDS      = 0.5            # Record in 0.5s chunks
MAX_LISTEN_SECONDS = 30             # Never listen longer than this
SILENCE_SECONDS    = 1.5            # Stop after this much silence post-speech
SPEECH_THRESHOLD   = 300            # RMS above this = speech detected
SILENCE_THRESHOLD  = 200            # RMS below this = silence

# Speaker — PulseAudio default sink (no explicit device — works with DP monitor)
PIPER_BIN        = os.path.expanduser("~/.local/bin/piper")
PIPER_VOICE      = os.path.expanduser("~/piper-voices/en_GB-alba-medium.onnx")
PIPER_RATE       = 22050             # Hz — Alba medium sample rate

# Ollama
OLLAMA_URL       = "http://localhost:11434/api/chat"
OLLAMA_MODEL     = "gemma3:4b"

# Whisper
WHISPER_MODEL    = "base"

Hardware:
  - Mic:    Microsoft LifeCam Cinema (PulseAudio via parec)
  - Audio:  DisplayPort monitor speakers (PulseAudio default sink)
  - Camera: /dev/video0 (Microsoft LifeCam Cinema)

Usage:
  python3 nova.py                # vision-based speaker ID
  python3 nova.py --user sean    # skip vision, identify as Sean

Press Ctrl+C to exit.
"""

import argparse
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
import base64
import signal

# ─── Configuration ────────────────────────────────────────────────────────────

# Mic — PulseAudio via parec (confirmed working on JetPack 6.2.2 / NVMe boot)
MIC_SOURCE       = "alsa_input.usb-Microsoft_Microsoft___LifeCam_Cinema_TM_-02.mono-fallback"
MIC_RATE         = 16000             # Hz — Whisper native rate
MIC_CHANNELS     = 1                 # Mono
RECORD_SECONDS   = 5                 # How long to listen each turn

# Speaker — PulseAudio default sink (no explicit device — works with DP monitor)
PIPER_BIN        = os.path.expanduser("~/.local/bin/piper")
PIPER_VOICE      = os.path.expanduser("~/piper-voices/en_GB-alba-medium.onnx")
PIPER_RATE       = 22050             # Hz — Alba medium sample rate

# Ollama
OLLAMA_URL       = "http://localhost:11434/api/chat"
OLLAMA_MODEL     = "gemma4:e2b"      # Vision-capable

# Whisper
WHISPER_MODEL    = "base"

# Audio thresholds
SILENCE_THRESHOLD = 300              # RMS below this = silence, skip turn

# Camera
CAMERA_DEVICE    = "/dev/video0"

# Family config location (private — never commit to GitHub)
FAMILY_CONFIG_PATH = os.path.expanduser("~/nova_config/family_config.py")

# ─── Load family config ───────────────────────────────────────────────────────

def load_family_config():
    """Load family config from private file."""
    if not os.path.exists(FAMILY_CONFIG_PATH):
        print(f"⚠️  Family config not found at {FAMILY_CONFIG_PATH}")
        print("   Running with default Nova identity only.")
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("family_config", FAMILY_CONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ─── Build system prompt ──────────────────────────────────────────────────────

def build_system_prompt(family_config, member_name=None):
    """Build Nova's system prompt for the given family member."""
    if family_config is None:
        return """You are Nova, a friendly AI companion. Be warm, helpful, and concise."""

    identity = family_config.NOVA_IDENTITY.strip()

    if member_name is None:
        profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
        style = family_config.PERSONAS["adult"]["style"].strip()
        return f"{identity}\n\n{style}\n\n{profile}"

    member = next(
        (m for m in family_config.FAMILY_MEMBERS
         if m["name"].lower() == member_name.lower()),
        None
    )

    if member is None:
        profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
        style = family_config.PERSONAS["adult"]["style"].strip()
        return f"{identity}\n\n{style}\n\n{profile}"

    persona = family_config.PERSONAS.get(member["persona"], family_config.PERSONAS["adult"])
    style = persona["style"].strip()
    profile = member["profile"].strip()

    return f"{identity}\n\n{style}\n\n{profile}"

# ─── Audio recording via parec ────────────────────────────────────────────────

def chunk_rms(data):
    """Calculate RMS of raw s16le bytes."""
    if len(data) < 2:
        return 0
    samples = struct.unpack(f"{len(data)//2}h", data[:len(data)//2*2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def record_audio(raw_file):
    """
    Record audio from LifeCam via PulseAudio parec using smart silence detection.
    Listens until speech is detected, then stops after SILENCE_SECONDS of silence.
    """
    print("🎤 Listening...")
    bytes_per_chunk = int(MIC_RATE * MIC_CHANNELS * 2 * CHUNK_SECONDS)
    silence_chunks_needed = int(SILENCE_SECONDS / CHUNK_SECONDS)
    max_chunks = int(MAX_LISTEN_SECONDS / CHUNK_SECONDS)

    cmd = [
        "parec",
        "-d", MIC_SOURCE,
        f"--rate={MIC_RATE}",
        f"--channels={MIC_CHANNELS}",
        "--format=s16le",
    ]

    all_audio = bytearray()
    speech_detected = False
    silence_count = 0

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    try:
        for _ in range(max_chunks):
            chunk = proc.stdout.read(bytes_per_chunk)
            if not chunk:
                break
            rms = chunk_rms(chunk)
            all_audio.extend(chunk)

            if rms >= SPEECH_THRESHOLD:
                if not speech_detected:
                    print("   💬 Speech detected...")
                speech_detected = True
                silence_count = 0
            elif speech_detected:
                silence_count += 1
                if silence_count >= silence_chunks_needed:
                    print("   ✋ End of speech detected")
                    break
    finally:
        proc.terminate()
        proc.wait()

    if not speech_detected:
        return False

    with open(raw_file, "wb") as f:
        f.write(all_audio)

    rms_total = chunk_rms(bytes(all_audio))
    print(f"   Audio RMS: {rms_total:.0f}")
    return os.path.getsize(raw_file) > 0


def raw_to_wav(raw_file, wav_file):
    """Convert raw s16le file to wav for Whisper."""
    with open(raw_file, "rb") as f:
        raw_data = f.read()
    with wave.open(wav_file, "wb") as wf:
        wf.setnchannels(MIC_CHANNELS)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(MIC_RATE)
        wf.writeframes(raw_data)


def is_silent(raw_file, threshold=SILENCE_THRESHOLD):
    """Return True if the recording is basically silence."""
    try:
        with open(raw_file, "rb") as f:
            data = f.read()
        if len(data) < 2:
            return True
        samples = struct.unpack(f"{len(data)//2}h", data[:len(data)//2*2])
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        print(f"   Audio RMS: {rms:.0f} (threshold: {threshold})")
        return rms < threshold
    except Exception as e:
        print(f"   Warning: could not check silence: {e}")
        return True

# ─── Vision identification ────────────────────────────────────────────────────

def capture_image(path):
    """Capture a single frame from the LifeCam."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "v4l2", "-i", CAMERA_DEVICE,
         "-frames:v", "1", "-q:v", "2", path],
        capture_output=True
    )
    return result.returncode == 0 and os.path.exists(path)


def identify_person(family_config):
    """Use gemma4:e2b vision to identify who is speaking."""
    if family_config is None:
        return None

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        img_path = f.name

    try:
        if not capture_image(img_path):
            print("   ⚠️  Camera capture failed")
            return None

        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        names = [m["name"] for m in family_config.FAMILY_MEMBERS]
        name_list = ", ".join(names)
        prompt_text = (
            f"Look at the person in this image. The possible people are: {name_list}. "
            f"Reply with ONLY the person's first name from that list, or 'unknown' if you cannot tell. "
            f"Do not explain. Just one word."
        )

        # Ollama native vision format — content as plain string, images as parallel key
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt_text,
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

        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            name = data["message"]["content"].strip().lower()
            # Match against known names
            for member in family_config.FAMILY_MEMBERS:
                if member["name"].lower() in name:
                    print(f"👤 Identified: {member['name']}")
                    return member["name"]
            print(f"   Vision returned: {name} — treating as unknown")
            return None

    except Exception as e:
        print(f"   Vision ID error: {e}")
        return None
    finally:
        try:
            os.unlink(img_path)
        except Exception:
            pass

# ─── TTS via Piper ────────────────────────────────────────────────────────────

def speak(text):
    """Convert text to speech using Piper and play via aplay (PulseAudio default)."""
    print(f"🔊 Nova: {text}")
    try:
        piper_cmd = [
            PIPER_BIN,
            "--model", PIPER_VOICE,
            "--output_raw"
        ]
        # No -D flag — let PulseAudio route to default sink automatically
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

# ─── Ollama chat ──────────────────────────────────────────────────────────────

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
        return "I had a little hiccup. Can you say that again?"


def check_ollama():
    """Check if Ollama is running."""
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=3)
        return True
    except Exception:
        return False

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova AI Companion")
    parser.add_argument("--user", type=str, default=None,
                        help="Skip vision ID and use this family member name directly")
    args = parser.parse_args()

    print("=" * 55)
    print("  Nova — Rugge Family AI Companion")
    print("  Press Ctrl+C to exit")
    print("=" * 55)

    # Check Ollama
    if not check_ollama():
        print("❌ Ollama is not running. Start it with: ollama serve")
        sys.exit(1)
    print("✅ Ollama connected")

    # Check Piper
    if not os.path.exists(PIPER_BIN):
        print(f"❌ Piper not found at {PIPER_BIN}")
        sys.exit(1)
    print("✅ Piper ready")

    # Check voice model
    if not os.path.exists(PIPER_VOICE):
        print(f"❌ Voice model not found at {PIPER_VOICE}")
        sys.exit(1)
    print("✅ Voice model ready (Alba)")

    # Load family config
    family_config = load_family_config()
    if family_config:
        names = [m["name"] for m in family_config.FAMILY_MEMBERS]
        print(f"✅ Family config loaded ({', '.join(names)})")
    else:
        print("⚠️  Running without family config")

    # Identify user
    if args.user:
        current_user = args.user.capitalize()
        print(f"✅ User set via --user: {current_user}")
    else:
        print("📷 Identifying speaker via vision...")
        current_user = identify_person(family_config)
        if current_user is None:
            print("   Could not identify — using unknown visitor profile")

    # Build system prompt
    system_prompt = build_system_prompt(family_config, current_user)

    # Load Whisper
    print("⏳ Loading speech recognition model...")
    try:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("✅ Whisper ready\n")
    except Exception as e:
        print(f"❌ Could not load Whisper: {e}")
        sys.exit(1)

    # Greeting
    if current_user:
        greeting = f"Hello {current_user}! Nova here — what's on your mind?"
    else:
        greeting = "Hello! I'm Nova. I don't think we've met — what's your name?"
    speak(greeting)

    # Conversation history
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "assistant", "content": greeting}
    ]

    # Main loop
    while True:
        try:
            with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
                tmp_raw = f.name
            tmp_wav = tmp_raw.replace(".raw", ".wav")

            # Record — returns False if no speech detected
            if not record_audio(tmp_raw):
                print("   (no speech detected — listening again...)\n")
                try:
                    os.unlink(tmp_raw)
                except Exception:
                    pass
                continue

            # Convert to wav for Whisper
            raw_to_wav(tmp_raw, tmp_wav)
            os.unlink(tmp_raw)

            # Transcribe
            print("💭 Thinking...")
            segments, _ = whisper_model.transcribe(tmp_wav, language="en")
            user_text = " ".join(seg.text.strip() for seg in segments).strip()
            os.unlink(tmp_wav)

            if not user_text:
                print("   (nothing heard — listening again...)\n")
                continue

            print(f"👤 {current_user or 'Visitor'}: {user_text}")

            # Add to conversation
            messages.append({"role": "user", "content": user_text})

            # Get reply
            reply = ask_ollama(messages)
            messages.append({"role": "assistant", "content": reply})

            # Speak
            speak(reply)
            print()

            # Trim history (system + last 20 turns)
            if len(messages) > 22:
                messages = [messages[0]] + messages[-20:]

        except KeyboardInterrupt:
            print("\n\n👋 Nova is going to sleep. Goodbye!")
            if current_user:
                speak(f"Goodbye {current_user}! Talk soon!")
            else:
                speak("Goodbye! Come talk to me again soon!")
            break
        except Exception as e:
            print(f"   Unexpected error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
