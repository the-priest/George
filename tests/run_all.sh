#!/usr/bin/env bash
# Everything George is checked against. Run from the repo root:  bash tests/run_all.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
step() { printf '\n== %s ==\n' "$1"; }

step "static"
python3 -m py_compile george*.py || fail=1
command -v pyflakes >/dev/null && { python3 -m pyflakes george*.py || fail=1; }
bash -n install.sh || fail=1
command -v shellcheck >/dev/null && { shellcheck -S warning install.sh || fail=1; }

step "logic + safety gate"
python3 tests/test_logic.py || fail=1

step "2.0 surface (theme, hud, config repair, new tools, watchdog)"
python3 tests/test_v2.py || fail=1

step "gate fuzz"
python3 tests/test_gate_fuzz.py || fail=1

step "agent loop (mock ollama)"
python3 tests/test_agent_loop.py || fail=1

step "ollama lifecycle"
python3 tests/test_lifecycle.py || fail=1

step "gtk ui (needs xvfb-run)"
if command -v xvfb-run >/dev/null; then
  xvfb-run -a python3 tests/test_ui.py || fail=1
else
  echo "skipped - install xvfb to run the UI test headless"
fi

printf '\n'
[ "$fail" = "0" ] && echo "ALL GREEN" || echo "FAILURES ABOVE"
exit "$fail"
