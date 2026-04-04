#!/bin/bash
# nova_launch.sh — Nova AI Companion launcher
#
# Hardware-aware: Jetson optimizations applied automatically when available.
# Works on any Linux system with Ollama, Piper, and Faster-Whisper installed.
#
# Usage:
#   ./nova_launch.sh                            # vision-based speaker ID
#   ./nova_launch.sh --user sean                # identify as Sean
#   ./nova_launch.sh --user devyn --skills education
#   ./nova_launch.sh --user jihan --skills homework,fitness

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JETSON_CLOCKS_BASELINE="$SCRIPT_DIR/jetson_clocks_baseline"
PERCEPTION_PID=""

echo "🚀 Starting Nova..."

# ── Jetson: pin clocks for inference performance ───────────────────────────────
JETSON_CLOCKS_ACTIVE=false
if command -v jetson_clocks &>/dev/null; then
    if [ ! -f "$JETSON_CLOCKS_BASELINE" ]; then
        echo "   Capturing clock baseline (first run)..."
        sudo jetson_clocks --store "$JETSON_CLOCKS_BASELINE"
    fi
    sudo jetson_clocks
    JETSON_CLOCKS_ACTIVE=true
    echo "⚡ Clocks pinned (Jetson)"
    sleep 3
fi

# ── PulseAudio: restart and ensure correct sink ───────────────────────────────
if command -v pulseaudio &>/dev/null; then
    pulseaudio --kill 2>/dev/null
    sleep 2
    pulseaudio --daemonize=yes
    sleep 3
    echo "🔊 Audio ready (PulseAudio)"
fi

# ── Perception: start Coral TPU vision engine in background ───────────────────
CORAL_PYTHON="$HOME/coral_env/bin/python3"
PERCEPTION_SCRIPT="$SCRIPT_DIR/perception.py"

if [ -f "$CORAL_PYTHON" ] && [ -f "$PERCEPTION_SCRIPT" ]; then
    "$CORAL_PYTHON" "$PERCEPTION_SCRIPT" &
    PERCEPTION_PID=$!
    echo "👁️  Perception started (PID $PERCEPTION_PID)"
else
    echo "👁️  Perception unavailable (coral_env or perception.py not found)"
fi

# ── Launch Nova — all arguments passed through ────────────────────────────────
python3 "$SCRIPT_DIR/nova.py" "$@"

# ── Cleanup on exit ───────────────────────────────────────────────────────────
if [ -n "$PERCEPTION_PID" ]; then
    kill "$PERCEPTION_PID" 2>/dev/null
    wait "$PERCEPTION_PID" 2>/dev/null
    echo "👁️  Perception stopped"
fi

if [ "$JETSON_CLOCKS_ACTIVE" = true ] && [ -f "$JETSON_CLOCKS_BASELINE" ]; then
    sudo jetson_clocks --restore "$JETSON_CLOCKS_BASELINE"
    sudo bash -c 'echo 0 > /sys/kernel/debug/bpmp/debug/clk/emc/mrq_rate_locked' 2>/dev/null
    echo "🔋 Clocks restored"
fi
