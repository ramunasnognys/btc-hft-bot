#!/usr/bin/env bash
# Build a clean Python virtual environment for the BTC bot. Safe to re-run.
# Run this ON YOUR MAC, from the project folder:
#     cd ~/claude_HFT && bash setup_env.sh
#
# Safe order: it builds and VERIFIES the new .venv first, and only removes the
# old path/to/venv afterwards — so a failure never leaves you without an env.

set -uo pipefail
cd "$(dirname "$0")"

echo "── BTC bot environment setup ─────────────────────────────────"

# 0. Drop any active conda/venv so detection sees the real interpreters
deactivate         2>/dev/null || true
conda deactivate   2>/dev/null || true

# 1. Find a Python >= 3.11 (the Polymarket SDK requires it)
PYBIN=""
for cand in /opt/homebrew/opt/python@3.14/bin/python3.14 \
            /opt/homebrew/opt/python@3.13/bin/python3.13 \
            /opt/homebrew/opt/python@3.12/bin/python3.12 \
            /opt/homebrew/opt/python@3.11/bin/python3.11 \
            python3.14 python3.13 python3.12 python3.11 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  ver=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)
  if [ "${ver%%.*}" -eq 3 ] && [ "${ver##*.}" -ge 11 ]; then
    PYBIN=$("$cand" -c 'import sys;print(sys.executable)')
    echo "✓ Using $PYBIN (Python $ver)"; break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "✗ No Python 3.11+ found. Install one:  brew install python@3.12"
  echo "  then re-run:  bash setup_env.sh"
  exit 1
fi

# 2. Build the new venv FIRST (into a temp name so we never destroy a working one)
echo "Creating virtual environment ..."
rm -rf .venv.new
if ! "$PYBIN" -m venv .venv.new; then
  echo "✗ 'python -m venv' failed. If you see an ensurepip error, run:"
  echo "    $PYBIN -m ensurepip --upgrade   (then re-run this script)"
  rm -rf .venv.new; exit 1
fi

echo "Installing dependencies (can take a minute) ..."
./.venv.new/bin/python -m pip install --upgrade pip -q
if ! ./.venv.new/bin/python -m pip install -r requirements.txt -q; then
  echo "✗ pip install failed — see output above. Old env left untouched."
  rm -rf .venv.new; exit 1
fi

# 3. Verify the SDK import BEFORE touching anything else
echo "Verifying Polymarket SDK import ..."
if ! ./.venv.new/bin/python -c "from py_clob_client.client import ClobClient"; then
  echo "✗ SDK import failed. Old env left untouched. .venv.new kept for inspection."
  exit 1
fi
echo "  ✓ py_clob_client SDK OK"

# 4. Success — swap the verified env into place, then clean up the old one
rm -rf .venv && mv .venv.new .venv
rm -rf path/to/venv 2>/dev/null || true
[ -d path ] && rmdir path 2>/dev/null || true

cat <<'EOF'

✅ Done. Always run the bot with the venv's Python:
     ./.venv/bin/python btc_live.py           # dry-run (no money)
     ./.venv/bin/python btc_live.py --check    # connection / balance
     ./.venv/bin/python btc_live.py --live     # real orders (asks to confirm)

   Or:  source .venv/bin/activate   (then: python btc_live.py ...)
EOF
