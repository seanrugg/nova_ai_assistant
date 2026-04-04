#!/usr/bin/env python3
"""
Nova - AI companion for the Rugge family
Runs on Jetson Orin Nano with Ollama (gemma3:4b), Faster-Whisper STT, and Piper TTS.

Hardware:
  - Mic:    Microsoft LifeCam Cinema (PulseAudio via parec)
  - Audio:  DisplayPort monitor speakers (PulseAudio default sink)
  - Camera: /dev/video0 (Microsoft LifeCam Cinema)

Usage:
  python3 nova.py                              # vision-based speaker ID
  python3 nova.py --user sean                  # skip vision, identify as Sean
  python3 nova.py --user devyn --skills education
  python3 nova.py --user jihan --skills homework,fitness

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
import importlib.util

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

# Family config — lives at family_config/family_config.py relative to this script
_SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
FAMILY_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "family_config", "family_config.py")

# Skills — lives at skills/ relative to this script
SKILLS_DIR         = os.path.join(_SCRIPT_DIR, "skills")

# ── Load family config ────────────────────────────────────────────────────────

def load_family_config():
    if not os.path.exists(FAMILY_CONFIG_PATH):
        print(f"Warning: Family config not found at {FAMILY_CONFIG_PATH}")
        return None
    spec = importlib.util.spec_from_file_location("family_config", FAMILY_CONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Load skills ───────────────────────────────────────────────────────────────

def load_skills(skill_names, user_persona=None):
    """
    Load skill modules by name from ~/nova_config/skills/.
    If user_persona is provided, filters to skills that list that persona
    (or skills with no persona filter at all).
    Returns list of loaded skill modules.
    """
    if not skill_names:
        return []

    loaded = []
    for name in skill_names:
        path = os.path.join(SKILLS_DIR, f"skill_{name}.py")
        if not os.path.exists(path):
            print(f"Warning: Skill '{name}' not found at {path}")
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"skill_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Persona filter: if skill declares SKILL_PERSONAS, check compatibility
            skill_personas = getattr(mod, "SKILL_PERSONAS", None)
            if skill_personas and user_persona and user_persona not in skill_personas:
                print(f"Skill '{name}' skipped (persona '{user_persona}' not in {skill_personas})")
                continue

            loaded.append(mod)
            print(f"Skill loaded: {getattr(mod, 'SKILL_NAME', name)}")
        except Exception as e:
            print(f"Warning: Could not load skill '{name}': {e}")

    return loaded

# ── Build system prompt ───────────────────────────────────────────────────────

def build_system_prompt(family_config, member_name=None, skills=None):
    if family_config is None:
        return "You are Nova, a friendly AI companion. Be warm, helpful, and concise."

    identity = family_config.NOVA_IDENTITY.strip()

    if member_name is None:
        profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
        style = family_config.PERSONAS["adult"]["style"].strip()
        base = f"{identity}\n\n{style}\n\n{profile}"
    else:
        member = next(
            (m for m in family_config.FAMILY_MEMBERS if m["name"].lower() == member_name.lower()),
            None
        )
        if member is None:
            profile = family_config.UNKNOWN_VISITOR_PROFILE.strip()
            style = family_config.PERSONAS["adult"]["style"].strip()
            base = f"{identity}\n\n{style}\n\n{profile}"
        else:
            persona = family_config.PERSONAS.get(member["persona"], family_config.PERSONAS["adult"])
            base = f"{identity}\n\n{persona['style'].strip()}\n\n{member['profile'].strip()}"

    # Append skill prompts
    if skills:
        skill_blocks = []
        for skill in skills:
            prompt = getattr(skill, "SKILL_PROMPT", "").strip()
            if prompt:
                skill_blocks.append(prompt)
        if skill_blocks:
            base += "\n\n" + "\n\n".join(skill_blocks)

    return base

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

def speak(text):
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

# Sentence-ending punctuation used to split the stream into speakable chunks
SENTENCE_ENDINGS = {'.', '!', '?'}


def ask_ollama_streaming(messages):
    """
    Stream tokens from Ollama. Speak each sentence as it completes.
    Returns the full response text for conversation history.
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
    buffer = ""

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
                if not token:
                    continue

                buffer += token
                full_reply += token

                # Speak when we hit a sentence boundary followed by a space
                # (avoids splitting on e.g. "Mr." or "3.5")
                if any(buffer.rstrip().endswith(p) for p in SENTENCE_ENDINGS):
                    sentence = buffer.strip()
                    if sentence:
                        speak(sentence)
                    buffer = ""

                if chunk.get("done", False):
                    break

        # Speak any remaining buffer (response didn't end with punctuation)
        if buffer.strip():
            speak(buffer.strip())

    except Exception as e:
        print(f"   Ollama error: {e}")
        fallback = "I had a little hiccup. Can you say that again?"
        speak(fallback)
        return fallback

    return full_reply.strip()


def check_ollama():
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=3)
        return True
    except Exception:
        return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova AI Companion")
    parser.add_argument("--user", type=str, default=None)
    parser.add_argument("--skills", type=str, default=None,
                        help="Comma-separated skill names to load, e.g. education,homework")
    args = parser.parse_args()

    print("=" * 55)
    print("  Nova - Rugge Family AI Companion")
    print("  Press Ctrl+C to exit")
    print("=" * 55)

    if not check_ollama():
        print("Ollama is not running. Start it with: ollama serve")
        sys.exit(1)
    print("✅ Ollama connected")

    if not os.path.exists(PIPER_BIN):
        print(f"Piper not found at {PIPER_BIN}")
        sys.exit(1)
    print("✅ Piper ready")

    if not os.path.exists(PIPER_VOICE):
        print(f"Voice model not found at {PIPER_VOICE}")
        sys.exit(1)
    print("✅ Voice model ready (Alba)")

    family_config = load_family_config()
    if family_config:
        names = [m["name"] for m in family_config.FAMILY_MEMBERS]
        print(f"✅ Family config loaded ({', '.join(names)})")

    if args.user:
        current_user = args.user.capitalize()
        print(f"✅ User: {current_user}")
    else:
        print("Identifying speaker via vision...")
        current_user = identify_person(family_config)
        if current_user is None:
            print("Could not identify — using unknown visitor profile")

    # Determine persona for skill filtering
    user_persona = None
    if current_user and family_config:
        member = next(
            (m for m in family_config.FAMILY_MEMBERS if m["name"].lower() == current_user.lower()),
            None
        )
        if member:
            user_persona = member.get("persona")

    # Load skills
    skill_names = [s.strip() for s in args.skills.split(",")] if args.skills else []
    skills = load_skills(skill_names, user_persona)
    if not skills and skill_names:
        print("No skills loaded.")
    elif skills:
        print(f"✅ Skills active: {', '.join(getattr(s, 'SKILL_NAME', '?') for s in skills)}")

    system_prompt = build_system_prompt(family_config, current_user, skills)

    print("Loading Whisper...")
    try:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("✅ Whisper ready\n")
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
            messages.append({"role": "user", "content": user_text})

            print("Nova: ", end="", flush=True)
            reply = ask_ollama_streaming(messages)
            print()  # newline after streamed output
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
