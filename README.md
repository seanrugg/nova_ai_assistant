# Nova — Family AI Companion

Nova is a local, voice-based AI companion designed to live in your home and genuinely know your family. She runs entirely on your own hardware — no cloud required, no subscriptions, no data leaving your network.

Nova listens, speaks, and responds differently to each family member — playful with young children, peer-like with teenagers, direct and honest with adults. She is built to help families grow, learn, and connect.

---

## What Nova Is

- A **local voice assistant** — speech-in, speech-out, running on your hardware
- **Family-aware** — knows each person by name and adapts her personality accordingly
- **Skill-extensible** — load optional skill modules to add educational activities, homework help, fitness coaching, and more
- **Private** — everything runs on your device; nothing is sent to the cloud

## What Nova Is Not

- A replacement for human connection
- A cloud service or subscription product
- A general-purpose smart speaker

---

## Hardware Requirements

Nova is designed for edge AI hardware but runs on any Linux system capable of running Ollama.

**Recommended:**
- NVIDIA Jetson Orin Nano 8GB (primary development platform)
- 8GB+ RAM
- Microphone (USB recommended)
- Speakers

**Also works on:**
- Any Ubuntu/Debian Linux machine with sufficient RAM for local LLM inference
- Raspberry Pi 5 (with reduced model performance)

---

## Software Requirements

| Component | Purpose | Install |
|---|---|---|
| [Ollama](https://ollama.com) | Local LLM inference | `curl -fsSL https://ollama.com/install.sh \| sh` |
| [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) | Speech-to-text | `pip install faster-whisper` |
| [Piper TTS](https://github.com/rhasspy/piper) | Text-to-speech | See Piper docs |
| Python 3.10+ | Runtime | System package |
| PulseAudio or PipeWire | Audio | System package |

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/nova_ai_assistant.git
cd nova_ai_assistant
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Pull the language model

```bash
ollama pull gemma3:4b
```

### 4. Install a Piper voice

Download a voice model from the [Piper voices repository](https://github.com/rhasspy/piper/releases).
Place the `.onnx` and `.onnx.json` files in `~/piper-voices/`.

The default voice is `en_GB-alba-medium`. To use a different voice, update `PIPER_VOICE` in `nova.py`.

### 5. Configure your family

```bash
cp family_config/family_config.py family_config/family_config.py.bak
nano family_config/family_config.py
```

Edit the `FAMILY_MEMBERS` list to reflect your household. Each member needs a name, age, persona (`child`, `teen`, or `adult`), and a profile describing who they are and how Nova should speak with them.

> **Coming soon:** First-run onboarding — Nova will interview your family and build this file automatically through conversation.

### 6. Run Nova

```bash
chmod +x nova_launch.sh
./nova_launch.sh --user yourname
```

Or with a skill loaded:

```bash
./nova_launch.sh --user yourchild --skills education
```

---

## Skills

Skills are optional modules that extend Nova's capabilities for specific contexts.

| Skill | Description | Best for |
|---|---|---|
| `education` | Storytelling, letter games, animal facts, phonics | Young children |
| `homework` | Study help, quiz games, subject tutoring | Teens and kids |
| `fitness` | Workout coaching, habit encouragement | Anyone |
| `cookbook` | Recipe suggestions, meal planning | Adults |
| `mindfulness` | Breathing exercises, reflection prompts | Anyone |

Load one or more skills at launch:

```bash
./nova_launch.sh --user sam --skills education,mindfulness
```

Skills live in the `skills/` directory. See `skills/README.md` for how to write your own.

---

## Project Structure

```
nova_ai_assistant/
  nova.py                   # Main script
  nova_launch.sh            # Hardware-aware launcher
  requirements.txt          # Python dependencies
  family_config/
    family_config.py        # Your family configuration (edit this)
  skills/
    skill_education.py
    skill_homework.py
    ...
    README.md               # How to write a skill
```

---

## Configuration

Key settings in `nova.py`:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_MODEL` | `gemma3:4b` | Language model to use |
| `WHISPER_MODEL` | `base` | Whisper model size (`tiny`, `base`, `small`) |
| `PIPER_VOICE` | `en_GB-alba-medium` | TTS voice model |
| `MIC_SOURCE` | *(PulseAudio source name)* | Your microphone's PulseAudio source |
| `SILENCE_SECONDS` | `1.5` | Seconds of silence before Nova stops listening |

To find your microphone's PulseAudio source name:
```bash
pactl list sources short
```

---

## Jetson-Specific Notes

Nova was developed on an NVIDIA Jetson Orin Nano 8GB running JetPack 6.2.2.

- `nova_launch.sh` automatically detects Jetson hardware and pins clocks for optimal inference performance
- Clocks are restored to their original state when Nova exits
- `gemma3:4b` runs comfortably within 8GB unified memory
- Larger models (7B+) may cause throttling on the Orin Nano 8GB

---

## Roadmap

- [ ] First-run family onboarding ("family imprint") — Nova builds `family_config.py` through conversation
- [ ] Vision-based speaker identification (camera required)
- [ ] Coral TPU acceleration for faster inference
- [ ] Wake word detection ("Hey Nova")
- [ ] More skills: news, language learning, bedtime stories
- [ ] Web UI for family config management

---

## License

MIT License. See `LICENSE` for details.

---

## Contributing

Pull requests welcome — especially new skills. See `skills/README.md` for the skill format spec.
