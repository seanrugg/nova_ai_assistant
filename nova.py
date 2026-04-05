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
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import wave

# ── Configuration ─────────────────────────────────────────────────────────────

# Mic — PulseAudio via parec (confirmed working on JetPack 6.2.2 / NVMe boot)
MIC_SOURCE         = "alsa_input.usb-Microsoft_Microsoft___LifeCam_Cinema_TM_-02.mono-fallback"
MIC_RATE           = 16000
MIC_CHANNELS       = 1

# Smart listening
CHUNK_SECONDS      = 0.5
MAX_LISTEN_SECONDS = 30
SILENCE_SECONDS    = 1.5
SPEECH_THRESHOLD   = 300
SILENCE_THRESHOLD  = 200

# Speaker
PIPER_BIN          = os.path.expanduser("~/.local/bin/piper")
PIPER_VOICE        = os.path.expanduser("~/piper-voices/en_GB-alba-medium.onnx")
PIPER_RATE         = 22050

# Ollama
OLLAMA_URL         = "http://localhost:11434/api/chat"
OLLAMA_MODEL       = "gemma3:4b"

# Whisper
WHISPER_MODEL      = "base"

# Camera
CAMERA_DEVICE      = "/dev/video0"

# Family config
FAMILY_CONFIG_PATH = os.path.expanduser("~/nova_config/family_config.py")

# ── Load family config ────────────────────────────────────────────────────────

def load_family_config():
    if not os.path.exists(FAMILY_CONFIG_PATH):
        print(f"Warning: Family config not found at {FAMILY_CONFIG_PATH}")
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("family_config", FAMILY_CONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Build system prompt ───────────────────────────────────────────────────────

def build_system_prompt(family_config, member_name=None):
    if family_config is None:
        return "You are Nova, a friendly AI companion. Be warm, helpful, and concise."

    identity = family_config.NOVA_IDENTITY.strip()

    if member_name is None:
        profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
        style = family_config.PERSONAS["adult"]["style"].strip()
        return f"{identity}\n\n{style}\n\n{profile}"

    member = next(
        (m for m in family_config.FAMILY_MEMBERS if m["name"].lower() == member_name.lower()),
        None
    )
    if member is None:
        profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
        style = family_config.PERSONAS["adult"]["style"].strip()
        return f"{identity}\n\n{style}\n\n{profile}"

    persona = family_config.PERSONAS.get(member["persona"], family_config.PERSONAS["adult"])
    return f"{identity}\n\n{persona['style'].strip()}\n\n{member['profile'].strip()}"

# ── Audio recording ───────────────────────────────────────────────────────────

def chunk_rms(data):
    if len(data) < 2:
        return 0
    samples = struct.unpack(f"{len(data)//2}h", data[:len(data)//2*2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def record_audio(raw_file):
    """Listen until speech detected, then stop after silence. Returns False if no speech."""
    print("Listening...")
    bytes_per_chunk = int(MIC_RATE * MIC_CHANNELS * 2 * CHUNK_SECONDS)
    silence_chunks_needed = int(SILENCE_SECONDS / CHUNK_SECONDS)
    max_chunks = int(MAX_LISTEN_SECONDS / CHUNK_SECONDS)

    cmd = [
        "parec", "-d", MIC_SOURCE,
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
                    print("   Speech detected...")
                speech_detected = True
                silence_count = 0
            elif speech_detected:
                silence_count += 1
                if silence_count >= silence_chunks_needed:
                    print("   End of speech.")
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
    with open(raw_file, "rb") as f:
        raw_data = f.read()
    with wave.open(wav_file, "wb") as wf:
        wf.setnchannels(MIC_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(MIC_RATE)
        wf.writeframes(raw_data)

# ── Vision identification ─────────────────────────────────────────────────────

def capture_image(path):
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "v4l2", "-i", CAMERA_DEVICE,
         "-frames:v", "1", "-q:v", "2", path],
        capture_output=True
    )
    return result.returncode == 0 and os.path.exists(path)


def identify_person(family_config):
    if family_config is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        img_path = f.name
    try:
        if not capture_image(img_path):
            print("   Camera capture failed")
            return None
        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        names = [m["name"] for m in family_config.FAMILY_MEMBERS]
        prompt_text = (
            f"Look at the person in this image. The possible people are: {', '.join(names)}. "
            f"Reply with ONLY the person's first name from that list, or 'unknown'. One word only."
        )
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt_text, "images": [img_b64]}],
            "stream": False
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            name = data["message"]["content"].strip().lower()
            for member in family_config.FAMILY_MEMBERS:
                if member["name"].lower() in name:
                    print(f"   Identified: {member['name']}")
                    return member["name"]
            return None
    except Exception as e:
        print(f"   Vision ID error: {e}")
        return None
    finally:
        try:
            os.unlink(img_path)
        except Exception:
            pass

# ── TTS ───────────────────────────────────────────────────────────────────────

def clean_for_speech(text):
    """Strip markdown and symbols that TTS would read aloud awkwardly."""
    # Strip leading "Nova:" prefix the model sometimes adds
    text = re.sub(r'^Nova:\s*', '', text, flags=re.IGNORECASE)
    # Remove parenthetical stage directions e.g. (A quiet whirring sound)
    text = re.sub(r'\([^)]*\)', '', text)
    # Remove bold and italic markers (**text**, *text*, __text__, _text_)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    # Remove standalone asterisks, hashes, backticks
    text = re.sub(r'[*#`]', '', text)
    # Remove markdown links [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Collapse multiple spaces
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def speak(text):
    """Speak text through a single piper+aplay pipeline. No per-sentence gaps."""
    text = clean_for_speech(text)
    if not text:
        return
    print(f"Nova: {text}")
    try:
        piper_proc = subprocess.Popen(
            [PIPER_BIN, "--model", PIPER_VOICE, "--output_raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        aplay_proc = subprocess.Popen(
            ["aplay", "-r", str(PIPER_RATE), "-f", "S16_LE", "-c", "1"],
            stdin=piper_proc.stdout, stderr=subprocess.DEVNULL
        )
        piper_proc.stdin.write(text.encode("utf-8"))
        piper_proc.stdin.close()
        piper_proc.stdout.close()
        aplay_proc.wait()
        piper_proc.wait()
    except Exception as e:
        print(f"   TTS error: {e}")

# ── Ollama ────────────────────────────────────────────────────────────────────

def ask_ollama(messages):
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["message"]["content"].strip()
    except Exception as e:
        print(f"   Ollama error: {e}")
        return "I had a little hiccup. Can you say that again?"


def ask_ollama_streaming(messages):
    """
    Stream tokens from Ollama for low-latency token display, then speak
    the full response through ONE persistent piper+aplay pipeline.

    Launching a new aplay process per sentence creates ~150ms gaps (audible chop
    for a child). A single pipeline eliminates this entirely — speech is seamless.
    """
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    full_reply = ""

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    full_reply += token
                    print(token, end="", flush=True)
                if chunk.get("done", False):
                    break
    except Exception as e:
        print(f"\n   Ollama error: {e}")
        fallback = "I had a little hiccup. Can you say that again?"
        speak(fallback)
        return fallback

    print()  # newline after streamed tokens

    # Speak full response through one pipeline — no gaps between sentences
    speech_text = clean_for_speech(full_reply.strip())
    if speech_text:
        try:
            piper_proc = subprocess.Popen(
                [PIPER_BIN, "--model", PIPER_VOICE, "--output_raw"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            aplay_proc = subprocess.Popen(
                ["aplay", "-r", str(PIPER_RATE), "-f", "S16_LE", "-c", "1"],
                stdin=piper_proc.stdout, stderr=subprocess.DEVNULL
            )
            piper_proc.stdin.write(speech_text.encode("utf-8"))
            piper_proc.stdin.close()
            piper_proc.stdout.close()
            aplay_proc.wait()
            piper_proc.wait()
        except Exception as e:
            print(f"   TTS error: {e}")

    return full_reply.strip()


def check_ollama():
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=3)
        return True
    except Exception:
        return False

# ── Perception ────────────────────────────────────────────────────────────────

PERCEPTION_OUTPUT  = os.path.expanduser("~/.nova_perception.json")
PERCEPTION_MAX_AGE = 360  # 6 minutes — matches 5-min capture interval with margin

# Mutable container so the main loop can update it without global keyword
_last_perception = [None]


def read_perception():
    """Read the latest perception observation. Returns string or None if stale/missing."""
    try:
        if not os.path.exists(PERCEPTION_OUTPUT):
            return None
        age = time.time() - os.path.getmtime(PERCEPTION_OUTPUT)
        if age > PERCEPTION_MAX_AGE:
            return None
        with open(PERCEPTION_OUTPUT, "r") as f:
            data = json.load(f)
        return data.get("summary")
    except Exception:
        return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova AI Companion")
    parser.add_argument("--user", type=str, default=None)
    args = parser.parse_args()

    print("=" * 55)
    print("  Nova - Rugge Family AI Companion")
    print("  Press Ctrl+C to exit")
    print("=" * 55)

    if not check_ollama():
        print("Ollama is not running. Start it with: ollama serve")
        sys.exit(1)
    print("Ollama connected")

    if not os.path.exists(PIPER_BIN):
        print(f"Piper not found at {PIPER_BIN}")
        sys.exit(1)
    print("Piper ready")

    if not os.path.exists(PIPER_VOICE):
        print(f"Voice model not found at {PIPER_VOICE}")
        sys.exit(1)
    print("Voice model ready (Alba)")

    family_config = load_family_config()
    if family_config:
        names = [m["name"] for m in family_config.FAMILY_MEMBERS]
        print(f"Family config loaded ({', '.join(names)})")

    if args.user:
        current_user = args.user.capitalize()
        print(f"User: {current_user}")
    else:
        print("Identifying speaker via vision...")
        current_user = identify_person(family_config)
        if current_user is None:
            print("Could not identify — using unknown visitor profile")

    system_prompt = build_system_prompt(family_config, current_user)

    print("Loading Whisper...")
    try:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("Whisper ready\n")
    except Exception as e:
        print(f"Could not load Whisper: {e}")
        sys.exit(1)

    greeting = f"Hello {current_user}! Nova here, what's on your mind?" if current_user else \
               "Hello! I'm Nova. I don't think we've met — what's your name?"
    speak(greeting)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "assistant", "content": greeting}
    ]

    while True:
        try:
            with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
                tmp_raw = f.name
            tmp_wav = tmp_raw.replace(".raw", ".wav")

            if not record_audio(tmp_raw):
                print("   (no speech — waiting...)\n")
                try:
                    os.unlink(tmp_raw)
                except Exception:
                    pass
                continue

            raw_to_wav(tmp_raw, tmp_wav)
            os.unlink(tmp_raw)

            print("Thinking...")
            segments, _ = whisper_model.transcribe(tmp_wav, language="en")
            user_text = " ".join(seg.text.strip() for seg in segments).strip()
            os.unlink(tmp_wav)

            if not user_text:
                print("   (nothing transcribed — waiting...)\n")
                continue

            print(f"{current_user or 'Visitor'}: {user_text}")

            # Inject perception context — only when scene has changed, or
            # when the user asks something vision-related
            perception = read_perception()
            if perception and perception != _last_perception[0]:
                user_content = f"[Nova's current visual awareness: {perception}]\n\n{user_text}"
                _last_perception[0] = perception
            elif perception:
                vision_words = {"see", "look", "room", "there", "what's", "spy",
                                "notice", "around", "eye", "watch", "show"}
                if any(w in user_text.lower() for w in vision_words):
                    user_content = f"[Nova's current visual awareness: {perception}]\n\n{user_text}"
                else:
                    user_content = user_text
            else:
                user_content = user_text

            messages.append({"role": "user", "content": user_content})

            reply = ask_ollama_streaming(messages)
            messages.append({"role": "assistant", "content": reply})
            print()

            if len(messages) > 22:
                messages = [messages[0]] + messages[-20:]

        except KeyboardInterrupt:
            print("\nNova is going to sleep. Goodbye!")
            speak(f"Goodbye {current_user}! Talk soon!" if current_user else "Goodbye!")
            break
        except Exception as e:
            print(f"   Error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
