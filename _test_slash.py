#!/usr/bin/env python3
"""Batch test all harness TUI slash commands, report results."""
import subprocess, sys, json, time

CMDS = [
    ("/help", "help"),
    ("/help all", "help"),
    ("/status", "status"),
    ("/home", "status"),
    ("/dashboard", "status"),
    ("/pwd", "pwd"),
    ("/init", "init"),
    ("/mode", "mode"),
    ("/mode codex-like", "mode"),
    ("/mode normal", "mode"),
    ("/tools", "tools"),
    ("/orchestrators", "orchestrators"),
    ("/agents", "agents"),
    ("/capabilities", "capabilities"),
    ("/adapters", "adapters"),
    ("/tasks", "tasks"),
    ("/runs", "runs"),
    ("/leases", "leases"),
    ("/memory", "memory"),
    ("/project .", "project"),
    ("/workspace .", "workspace"),
    ("/reset", "reset"),
    ("/stop", "stop"),
    ("/quit", "quit"),
]

def test_command(cmd, label):
    inp = cmd + "\n/quit\n"
    t0 = time.time()
    proc = subprocess.run(
        ["harness", "--project", ".", "--plain"],
        input=inp, capture_output=True, text=True, timeout=30,
        cwd="/Users/oscarxue/Documents/harness"
    )
    elapsed = time.time() - t0
    out = proc.stdout
    status = "PASS" if proc.returncode == 0 else "FAIL"
    crash = "Traceback" in out or "Error" in out
    lines = out.split("\n")
    response_lines = [l for l in lines if label in l.lower() or "error" in l.lower() or "traceback" in l.lower()]
    return status, elapsed, crash, len(out), "\n".join(response_lines[:5])

results = []
for cmd, label in CMDS:
    status, elapsed, crash, length, resp = test_command(cmd, label)
    results.append((cmd, status, elapsed, crash, length))
    print(f"{status}  {cmd:<30} {elapsed:4.1f}s  crash={crash}  out={length}b")
    if crash:
        print(f"  -> {resp[:200]}")

print("\n--- Summary ---")
fails = [r for r in results if r[1] != "PASS"]
crashed = [r for r in results if r[3]]
print(f"Tested: {len(results)}, Passed: {len(results)-len(fails)}, Crashed: {len(crashed)}")
if fails:
    for r in fails:
        print(f"  FAIL: {r[0]}")
if crashed:
    for r in crashed:
        print(f"  CRASH: {r[0]}")
