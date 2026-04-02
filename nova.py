#!/usr/bin/env python3
"""
Nova - A friendly AI companion for kids
Runs on Jetson Orin Nano with Ollama (gemma3:4b), Faster-Whisper STT, and Piper TTS.

Hardware:
  - Mic:    Microsoft LifeCam Cinema  (ALSA hw:2,0)
  - Audio:  DisplayPort monitor speakers (PulseAudio default sink)
  - Camera: /dev/video0 (Microsoft LifeCam Cinema)

Usage:
  python3 nova.py

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

# ─── Configuration ────────────────────────────────────────────────────────────

MIC_DEVICE       = "hw:2,0"          # ALSA device for LifeCam Cinema mic
MIC_RATE         = 16000             # Hz — Whisper wants 16kHz
MIC_CHANNELS     = 1                 # Mono
MIC_FORMAT       = "S16_LE"          # 16-bit little endian
RECORD_SECONDS   = 5                 # How long to listen each turn

PIPER_BIN        = os.path.expanduser("~/.local/bin/piper")
PIPER_VOICE      = "/mnt/piper-voices/en_US-amy-medium.onnx"
PIPER_RATE       = 22050             # Hz — Amy medium sample rate

OLLAMA_URL       = "http://localhost:11434/api/chat"
OLLAMA_MODEL     = "gemma3:4b"

WHISPER_MODEL    = "base"            # tiny / base / small — base is good for kids' voices

SILENCE_THRESHOLD = 500              # RMS below this = silence, skip turn

# ─── Nova's personality ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Nova, a friendly and gentle robot companion for a 4-year-old child.

Your personality:
- Warm, patient, and enthusiastic — like a kind older friend
- Use very simple words and short sentences (2-3 sentences max per response)
- You love animals, colours, dinosaurs, and silly jokes
- Ask one simple question at the end of each response to keep conversation going
- If you don't understand, say "Can you say that again?" in a friendly way
- Never say anything scary or confusing
- Use sounds like "Ooooh!" and "Wow!" to show excitement
- You are a robot so you can make fun robot sounds like "beep boop" occasionally

Important: Keep ALL responses very short — 2 to 3 short sentences maximum.
The child's name is unknown — use "friend" or "little one" as terms of endearment.
"""

# ─── Helpers ──────────────────────────────────────────────────────────────────

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
            rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
            print(f"   Audio RMS: {rms:.0f} (threshold: {threshold})")
            return rms < threshold
    except Exception as e:
        print(f"   Warning: could not check silence: {e}")
        return False


def transcribe(filename):
    """Transcribe audio using faster-whisper via Python API."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, info = model.transcribe(filename, language="en")
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
    except Exception as e:
        print(f"   Whisper error: {e}")
        return ""


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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["message"]["content"].strip()
    except Exception as e:
        print(f"   Ollama error: {e}")
        return "Beep boop! I had a little hiccup. Can you ask me again?"


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


def check_ollama():
    """Check if Ollama is running."""
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=3)
        return True
    except Exception:
        return False

# ─── Main loop ────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  Nova - Friendly AI Companion")
    print("  Press Ctrl+C to exit")
    print("=" * 50)

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
    print("✅ Voice model ready")

    print("\n🤖 Nova is starting up...\n")

    # Conversation history — persists across turns
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Greeting
    greeting = "Hello! I am Nova, your robot friend! Beep boop! What would you like to talk about today?"
    speak(greeting)
    messages.append({"role": "assistant", "content": greeting})

    # Preload Whisper model once
    print("⏳ Loading speech recognition model...")
    try:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("✅ Whisper ready\n")
    except Exception as e:
        print(f"❌ Could not load Whisper: {e}")
        sys.exit(1)

    # Main conversation loop
    while True:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_audio = f.name

            # Record
            if not record_audio(tmp_audio):
                print("   Recording failed, retrying...")
                time.sleep(1)
                continue

            # Check for silence
            if is_silent(tmp_audio):
                print("   (silence detected, listening again...)\n")
                os.unlink(tmp_audio)
                continue

            # Transcribe
            print("💭 Thinking...")
            segments, _ = whisper_model.transcribe(tmp_audio, language="en")
            user_text = " ".join(seg.text.strip() for seg in segments).strip()
            os.unlink(tmp_audio)

            if not user_text:
                print("   (nothing heard, listening again...)\n")
                continue

            print(f"👧 Child: {user_text}")

            # Add to conversation
            messages.append({"role": "user", "content": user_text})

            # Get Nova's response
            reply = ask_ollama(messages)
            messages.append({"role": "assistant", "content": reply})

            # Speak reply
            speak(reply)
            print()

            # Keep conversation history manageable (system + last 10 turns)
            if len(messages) > 21:
                messages = [messages[0]] + messages[-20:]

        except KeyboardInterrupt:
            print("\n\n👋 Nova is going to sleep. Goodbye!")
            speak("Goodbye, friend! See you next time! Beep boop!")
            break
        except Exception as e:
            print(f"   Unexpected error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
