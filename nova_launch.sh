#!/bin/bash
# nova_launch.sh — Nova AI Companion launcher
# Usage:
#   ./nova_launch.sh                          # vision-based speaker ID
#   ./nova_launch.sh --user sam              # identify as Sam
#   ./nova_launch.sh --user jane --skills education
#   ./nova_launch.sh --user james --skills homework,fitness

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Starting Nova..."

# Save current clock state and pin to max for inference performance
sudo jetson_clocks --store /tmp/nova_clocks_restore
sudo jetson_clocks
echo "⚡ Clocks pinned to max"

# Restart PulseAudio and ensure correct audio sink
pulseaudio --kill 2>/dev/null
sleep 2
pulseaudio --daemonize=yes
sleep 3
echo "🔊 Audio ready"

# Launch Nova — all arguments passed through
python3 "$SCRIPT_DIR/nova.py" "$@"

# Restore clocks on exit
sudo jetson_clocks --restore /tmp/nova_clocks_restore
echo "🔋 Clocks restored"
