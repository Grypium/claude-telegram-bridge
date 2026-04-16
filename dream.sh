#!/bin/bash
# Run dreaming for one or all agents
# Usage: ./dream.sh <agent_name|all>

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$BRIDGE_DIR/venv/bin/python3"

get_agents() {
    ls -d "$BRIDGE_DIR/agents"/*/config.env 2>/dev/null | while read f; do
        basename "$(dirname "$f")"
    done
}

dream_agent() {
    local agent="$1"
    echo "🌙 Dreaming for $agent..."
    cd "$BRIDGE_DIR" && PYTHONUNBUFFERED=1 "$PYTHON" -u -m bridge.dreaming "agents/$agent" 2>&1
    echo ""
}

AGENT="${1:-all}"

if [ "$AGENT" = "all" ]; then
    for agent in $(get_agents); do
        dream_agent "$agent"
    done
else
    dream_agent "$AGENT"
fi
