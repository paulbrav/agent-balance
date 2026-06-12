"""Live end-to-end rehearsal: a running claude session follows a swap.

Opt-in (burns two haiku turns against real accounts):

    AGENT_BALANCE_LIVE=1 pytest tests/test_hotswap_live.py -v

Copies of two real accounts' credentials are staged in a scratch root, so
nothing touches the real account dirs or the real pool.
"""

import json
import os
import shutil
import subprocess
import time

import pytest

import agent_balance as ab

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_BALANCE_LIVE") != "1",
    reason="live test: set AGENT_BALANCE_LIVE=1 to run (burns 2 haiku turns)",
)


def real_logged_in_accounts():
    cfg = ab.make_config()
    now = time.time()
    fresh = []
    for a in ab.discover_accounts(cfg):
        oauth = ab.read_json(a.creds).get("claudeAiOauth") or {}
        if (oauth.get("expiresAt") or 0) / 1000 > now + 600:
            fresh.append(a)
    return fresh


def send(proc, text):
    msg = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def read_result(proc, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return ev
    return None


def test_running_session_follows_swap(tmp_path):
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH")
    sources = real_logged_in_accounts()
    if len(sources) < 2:
        pytest.skip("need two logged-in accounts with fresh tokens")
    a, b = sources[:2]

    root = tmp_path / "accounts"
    for src in (a, b):
        home = root / src.name
        home.mkdir(parents=True)
        shutil.copy(src.creds, home / ".credentials.json")
        shutil.copy(ab.meta_path(src.home), home / ".claude.json")
    # draw=0: feasibility must not block the forced swap when the second
    # account happens to be high in its 5h window.
    cfg = ab.Config(
        root=root,
        cache=tmp_path / "cache",
        threshold=0,
        min_gap=0,
        interval=60,
        draw=0,
        pull_margin=0,
    )

    out = []
    assert ab.tick(cfg, out=out.append) == 0
    first = ab.read_state(cfg, time.time())["installed"]
    assert first in (a.name, b.name)

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDECODE",
        )
    }
    env["CLAUDE_CONFIG_DIR"] = str(cfg.pool)
    proc = subprocess.Popen(
        [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "claude-haiku-4-5-20251001",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        bufsize=1,
        cwd=tmp_path,
    )
    try:
        send(proc, "Reply with exactly: ALPHA")
        r1 = read_result(proc)
        assert r1 and not r1.get("is_error"), f"turn 1 failed: {r1}"

        # threshold=0 makes every tick a soft swap; exclusion guarantees
        # the OTHER account gets installed beneath the running session.
        out2 = []
        assert ab.tick(cfg, out=out2.append) == 0
        second = ab.read_state(cfg, time.time())["installed"]
        assert second != first, f"no swap happened: {out2}"

        send(proc, "Reply with exactly: BRAVO")
        r2 = read_result(proc)
        assert r2 and not r2.get("is_error"), (
            f"turn 2 failed after swap to {second}: {r2}"
        )
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
