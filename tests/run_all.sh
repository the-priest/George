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

step "auto-run gate (what may run without asking)"
python3 tests/test_autorun.py || fail=1

step "windows behaviour (runs on linux)"
python3 tests/test_windows.py || fail=1

step "gate fuzz"
python3 tests/test_gate_fuzz.py || fail=1

step "agent loop (mock ollama)"
python3 tests/test_agent_loop.py || fail=1

echo "== greetings and canned final answers =="
python3 tests/test_greeting_reply.py || fail=1

echo "== sweep findings (retention, growth, atomic writes) =="
python3 tests/test_scan_2_5_2.py || fail=1

echo "== inline markdown -> pango =="
python3 tests/test_markup.py || fail=1

echo "== no unverified claims about the screen =="
python3 tests/test_no_false_claims.py || fail=1

echo "== prompt stability / speed =="
python3 tests/test_speed.py || fail=1

echo "== intent router =="
python3 tests/test_router.py || fail=1

echo "== system prompt =="
python3 tests/test_prompt.py || fail=1

echo "== reply firewall (no scratchpad on screen) =="
python3 tests/test_firewall.py || fail=1

echo "== constrained decoding =="
python3 tests/test_structured.py || fail=1

echo "== write-and-run code =="
python3 tests/test_code_tool.py || fail=1

echo "== every tool, every arg shape =="
python3 tests/test_tools_surface.py || fail=1

echo "== verification pass =="
python3 tests/test_verify.py || fail=1

echo "== structured tool results =="
python3 tests/test_structured_results.py || fail=1

echo "== whole sessions, replayed =="
python3 tests/test_sessions.py || fail=1

echo "== tool argument repair =="
python3 tests/test_arg_repair.py || fail=1

echo "== results cache =="
python3 tests/test_cache.py || fail=1

echo "== the stop button =="
python3 tests/test_stop.py || fail=1

echo "== the trace =="
python3 tests/test_trace.py || fail=1

echo "== reference lookup (wikipedia, then web) =="
python3 tests/test_lookup.py || fail=1

echo "== design lint (contrast, type scale, sheet) =="
python3 tests/test_design.py || fail=1

step "ollama lifecycle"
python3 tests/test_lifecycle.py || fail=1

step "audit (cross-module names, swallowed exceptions)"
xvfb-run -a python3 tests/test_audit.py || fail=1

step "gtk ui (needs xvfb-run)"
if command -v xvfb-run >/dev/null; then
  xvfb-run -a python3 tests/test_ui.py || fail=1
else
  echo "skipped - install xvfb to run the UI test headless"
fi

printf '\n'
[ "$fail" = "0" ] && echo "ALL GREEN" || echo "FAILURES ABOVE"
exit "$fail"
