"""main() — subcommand dispatch, the unknown-command error, and the macOS
Keychain guard (ISSUE 6)."""

import pytest
from conftest import add_account

import agent_balance as ab


@pytest.fixture
def routed(cfg, monkeypatch):
    """Patch make_config -> the sandbox cfg and every command handler to a
    recorder, so dispatch is observed without running any real command.
    `calls` collects (name, cfg) tuples in the order main() routes."""
    calls = []
    monkeypatch.setattr(ab, "make_config", lambda: cfg)
    # cmd_status takes keyword-only as_json/refresh now — swallow them.
    monkeypatch.setattr(
        ab, "cmd_status", lambda c, **k: calls.append(("status", c)) or 0
    )
    monkeypatch.setattr(ab, "tick", lambda c: calls.append(("tick", c)) or 0)
    monkeypatch.setattr(ab, "cmd_watch", lambda c: calls.append(("watch", c)) or 0)
    monkeypatch.setattr(ab, "cmd_install", lambda c: calls.append(("install", c)) or 0)
    monkeypatch.setattr(
        ab, "cmd_uninstall", lambda c: calls.append(("uninstall", c)) or 0
    )
    monkeypatch.setattr(
        ab, "cmd_launch", lambda c, a, **k: calls.append(("launch", a)) or 0
    )
    # Default to Linux so the guard tests can opt into darwin explicitly.
    monkeypatch.setattr(ab.sys, "platform", "linux")
    return calls


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        ([], "status"),
        (["status"], "status"),
        (["tick"], "tick"),
        (["watch"], "watch"),
        (["install"], "install"),
        (["uninstall"], "uninstall"),
    ],
)
def test_each_subcommand_routes(routed, argv, name):
    assert ab.main(argv) == 0
    assert [c[0] for c in routed] == [name]


def test_launch_passes_remainder(routed):
    # The REMAINDER trailer reaches cmd_launch verbatim. A leading positional
    # (a claude subcommand / prompt) is the portable form: argparse's
    # nargs=REMAINDER refuses a *leading* optional-looking token (a `-p`
    # first) on Python 3.11..3.14 alike — `launch -- -p hi` is the escape
    # hatch for that case (asserted below).
    assert ab.main(["launch", "chat", "-p", "hi"]) == 0
    assert routed == [("launch", ["chat", "-p", "hi"])]


def test_launch_double_dash_escapes_leading_flag(routed):
    assert ab.main(["launch", "--", "-p", "hi"]) == 0
    assert routed == [("launch", ["--", "-p", "hi"])]


def test_claude_argv_strips_one_leading_dashdash():
    # The shell wrapper passes `-- "$@"`; launch drops exactly one leading `--`
    # so claude's own flags survive.
    assert ab.claude_argv(["--", "-p", "hi"]) == ["claude", "-p", "hi"]
    assert ab.claude_argv(["--"]) == ["claude"]
    assert ab.claude_argv([]) == ["claude"]
    assert ab.claude_argv(["chat"]) == ["claude", "chat"]
    assert ab.claude_argv(["--", "--"]) == ["claude", "--"]  # only one stripped


def test_unknown_command_errors(routed):
    # argparse rejects the unregistered subcommand before main's dispatch.
    with pytest.raises(SystemExit) as exc:
        ab.main(["bogus"])
    assert exc.value.code == 2
    assert routed == []


@pytest.mark.parametrize("argv", [["tick"], ["watch"], ["install"], ["launch"]])
def test_darwin_guard_blocks_mutating_commands(routed, monkeypatch, capsys, argv):
    monkeypatch.setattr(ab.sys, "platform", "darwin")
    assert ab.main(argv) == 1
    assert "Keychain" in capsys.readouterr().err
    assert routed == []


@pytest.mark.parametrize("argv", [[], ["status"], ["uninstall"]])
def test_darwin_allows_readonly_commands(routed, monkeypatch, argv):
    # status (even with --refresh) and uninstall are read-only / cleanup, so
    # they stay allowed on macOS; the guard only blocks credential mutators.
    monkeypatch.setattr(ab.sys, "platform", "darwin")
    assert ab.main(argv) == 0
    assert len(routed) == 1


def test_darwin_status_refresh_allowed(routed, monkeypatch):
    monkeypatch.setattr(ab.sys, "platform", "darwin")
    assert ab.main(["status", "--refresh"]) == 0
    assert [c[0] for c in routed] == ["status"]


def test_routed_fixture_does_not_touch_disk(routed, cfg):
    # A guard test must not have side effects: dispatch is fully stubbed, so
    # add_account here would be the only thing on disk if any handler ran.
    add_account(cfg, "alt1")
    assert ab.main(["status"]) == 0
    assert routed == [("status", cfg)]
