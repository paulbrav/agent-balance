#!/usr/bin/env bash
# Installs agent-balance to ~/.local/bin (override with BINDIR=/some/path).
# Run from a clone:  ./install.sh
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/paulbrav/agent-balance/HEAD"
BINDIR="${BINDIR:-$HOME/.local/bin}"

command -v python3 >/dev/null 2>&1 || {
  echo "install.sh: python3 is required" >&2
  exit 1
}

mkdir -p "$BINDIR"

src_dir=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [[ -n "$src_dir" && -f "$src_dir/agent_balance.py" ]]; then
  install -m 0755 "$src_dir/agent_balance.py" "$BINDIR/agent-balance"
  install -m 0755 "$src_dir/agent_balance_tray.py" "$BINDIR/agent-balance-tray"
  echo "Installed agent-balance + agent-balance-tray -> $BINDIR/"
else
  # Piped install (curl ... | bash): fetch the scripts from GitHub.
  curl -fsSL "$REPO_RAW/agent_balance.py" -o "$BINDIR/agent-balance"
  curl -fsSL "$REPO_RAW/agent_balance_tray.py" -o "$BINDIR/agent-balance-tray"
  chmod 0755 "$BINDIR/agent-balance" "$BINDIR/agent-balance-tray"
  echo "Downloaded agent-balance + agent-balance-tray -> $BINDIR/"
fi

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *)
    echo ""
    echo "NOTE: $BINDIR is not on your PATH. Add this to your shell rc:"
    echo ""
    echo "  export PATH=\"$BINDIR:\$PATH\""
    ;;
esac

echo ""
echo "Done. Quick start:"
echo "  agent-balance status     # discovered accounts, installed creds, timer health"
echo "  agent-balance install    # enable the 60s systemd user timer"
echo "  agent-balance launch     # start claude through the balanced pool"
