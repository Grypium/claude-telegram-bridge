#!/usr/bin/env bash
# Install the commitment-check Stop hook into Claude Code.
#
#   ./hooks/install.sh              install (idempotent)
#   ./hooks/install.sh --uninstall  remove
#   ./hooks/install.sh --dry-run    show what would change
#
# Merges into ~/.claude/settings.json without disturbing existing hooks, and backs the file up
# first. Safe to re-run.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
HOOK_DIR="$CLAUDE_DIR/hooks"
HOOK_PATH="$HOOK_DIR/commitment_check.py"
SETTINGS="$CLAUDE_DIR/settings.json"

MODE="install"
for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE="uninstall" ;;
    --dry-run)   MODE="dryrun" ;;
    -h|--help)   sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

echo "Claude config dir : $CLAUDE_DIR"
echo "Python            : $PY"
echo

if [ "$MODE" != "uninstall" ]; then
  # Verify the hook is sane BEFORE wiring it into settings.
  echo "Verifying hook..."
  "$PY" "$SRC_DIR/commitment_check.py" --selftest || {
    echo "error: hook self-test failed; refusing to install" >&2
    exit 1
  }
  echo
fi

if [ "$MODE" = "dryrun" ]; then
  echo "[dry-run] would copy $SRC_DIR/commitment_check.py -> $HOOK_PATH"
  echo "[dry-run] would register Stop hook in $SETTINGS"
  exit 0
fi

mkdir -p "$HOOK_DIR"

if [ "$MODE" = "install" ]; then
  cp "$SRC_DIR/commitment_check.py" "$HOOK_PATH"
  chmod +x "$HOOK_PATH"
  echo "Installed hook -> $HOOK_PATH"
fi

CMD="$PY $HOOK_PATH"

MODE="$MODE" SETTINGS="$SETTINGS" CMD="$CMD" "$PY" <<'PYEOF'
import json, os, shutil, sys

mode     = os.environ["MODE"]
settings = os.environ["SETTINGS"]
cmd      = os.environ["CMD"]

data = {}
if os.path.exists(settings):
    shutil.copyfile(settings, settings + ".bak")
    try:
        with open(settings) as f:
            data = json.load(f)
    except Exception as e:
        print(f"error: {settings} is not valid JSON ({e}); left untouched", file=sys.stderr)
        sys.exit(1)

hooks = data.setdefault("hooks", {})
stop  = hooks.setdefault("Stop", [])

def is_ours(entry):
    for h in entry.get("hooks", []) or []:
        if "commitment_check.py" in str(h.get("command", "")):
            return True
    return False

stop[:] = [e for e in stop if not is_ours(e)]      # drop any prior install

if mode == "install":
    stop.append({"hooks": [{"type": "command", "command": cmd}]})

if not stop:
    hooks.pop("Stop", None)
if not hooks:
    data.pop("hooks", None)

with open(settings, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

print(("Registered" if mode == "install" else "Unregistered") + f" Stop hook in {settings}")
if os.path.exists(settings + ".bak"):
    print(f"Previous settings backed up to {settings}.bak")
PYEOF

if [ "$MODE" = "uninstall" ]; then
  rm -f "$HOOK_PATH"
  echo "Removed $HOOK_PATH"
  echo
  echo "Done. Restart Claude Code for the change to take effect."
  exit 0
fi

echo
echo "Done. Restart Claude Code for the hook to take effect."
echo
echo "Test it by asking your agent to do something and watching for a message that"
echo "promises future work — the turn should be blocked until it acts or says NOT DONE."
echo
echo "Tune or disable without reinstalling:"
echo "  $HOOK_DIR/commitment_check.config.json"
echo '  {"extra_patterns": [], "extra_absolve": [], "disabled": false}'
