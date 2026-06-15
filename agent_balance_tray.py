#!/usr/bin/python3
# agent-balance-tray — the agent-pick --list table as a tray dropdown.
#
# An AppIndicator (GNOME: needs the ubuntu-appindicators/AppIndicator
# extension) whose label shows the installed account and its 5h usage, and
# whose dropdown renders every account's 5h/7d windows as colored bars.
# Probes share agent-balance's 45s cache, so the tray adds no API load on
# top of the balancer's tick.
#
# Uses the SYSTEM python (PyGObject + libayatana typelibs are distro
# packages): on Debian/Ubuntu, `apt install python3-gi gir1.2-ayatanaappindicator3-0.1`.
#
#   agent-balance-tray                      run in the foreground
#   agent-balance-tray --install-autostart  start with every login

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator
from gi.repository import GLib, Gtk

GREEN, YELLOW, RED, DIM = "#73d216", "#edd400", "#ef2929", "#888888"
BAR_ON, BAR_OFF = "▰", "▱"

# The tray owns its presentation: the `status --json` contract ships RAW
# numbers plus a single `now`, so countdown/staleness are recomputed here at
# GTK draw time (the JSON may be seconds old). These are verbatim copies of
# agent_balance's reset_in and stale_age_min — trivial, branch-free, and the
# ab-side logic stays covered by test_format.py. Keeping them local is what
# lets the tray drop its import of agent_balance entirely.
USAGE_STALE_AFTER = 90  # = 2 * agent-balance USAGE_TTL; the tray's own copy


def reset_in(epoch: int, now: float) -> str:
    """Compact reset countdown — verbatim copy of agent_balance.reset_in."""
    if epoch == 0:
        return "-"
    s = epoch - now
    if s <= 0:
        return "now"
    if s < 3600:
        return f"{int(s // 60) + 1}m"
    if s < 86400:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def stale_age_min(asof: float, now: float) -> float | None:
    """Age in minutes once it crosses the staleness threshold; None when
    fresh (or asof unknown) — the tray's copy of agent_balance.stale_age_min,
    reading the raw asof epoch out of the JSON usage block."""
    if asof and now - asof > USAGE_STALE_AFTER:
        return (now - asof) / 60
    return None


def esc(text: str) -> str:
    return GLib.markup_escape_text(text)


def bar_markup(pct: float) -> str:
    p = int(pct)
    filled = (p + 5) // 10
    if filled == 0 and p > 0:
        filled = 1
    filled = min(filled, 10)
    color = GREEN if p < 50 else YELLOW if p < 80 else RED
    return (
        f'<span foreground="{color}">{BAR_ON * filled}'
        f"{BAR_OFF * (10 - filled)} {p:3d}%</span>"
    )


class Row(NamedTuple):
    name: str
    email: str
    usage: dict | None  # {"five","seven","r5","r7","asof"} or None (word row)
    status: str | None  # status word, or None when usage is present
    installed: bool


@dataclass
class Snapshot:
    """One worker-thread collection pass, handed to the GTK thread."""

    now: float = 0.0
    installed_name: str = ""
    rows: list[Row] = field(default_factory=list)
    error: str | None = None  # collection blew up; rows are empty


def cli() -> str | None:
    return shutil.which("agent-balance")


def collect() -> Snapshot:
    """Worker-thread data gathering: shell out to `agent-balance status
    --json` and parse the DATA contract. Cache-only by design — the balancer
    tick is the machine's only steady poller, so the tray's passive redraws
    add zero network load. Any failure becomes a Snapshot with .error set so
    the tray shows an error row instead of crashing."""
    exe = cli()
    if not exe:
        return Snapshot(error="agent-balance not found on PATH")
    try:
        r = subprocess.run(
            [exe, "status", "--json"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Snapshot(error=f"agent-balance failed: {e}")
    if r.returncode != 0:
        return Snapshot(
            error=(r.stderr.strip() or f"agent-balance exited {r.returncode}")
        )
    try:
        doc = json.loads(r.stdout)
    except ValueError as e:
        return Snapshot(error=f"bad status JSON: {e}")
    rows = [
        Row(a["name"], a["email"], a.get("usage"), a.get("status"), a["installed"])
        for a in doc.get("accounts", [])
    ]
    return Snapshot(
        now=doc["now"],
        installed_name=(doc.get("installed") or {}).get("name", ""),
        rows=rows,
    )


class Tray:
    def __init__(self):
        self.indicator = AppIndicator.Indicator.new(
            "agent-balance",
            "utilities-system-monitor-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.build_menu(Snapshot()))
        self.refresh()
        # Honor a tuned cadence without importing agent_balance: read the same
        # env knob the CLI's make_config reads, default 60, floor 30.
        try:
            interval = max(int(os.environ.get("AGENT_BALANCE_INTERVAL") or 60), 30)
        except ValueError:
            interval = 60
        GLib.timeout_add_seconds(interval, self.refresh)

    def refresh(self):
        threading.Thread(target=self._work, daemon=True).start()
        return True  # keep the GLib timer alive

    def _work(self):
        try:
            snap = collect()
        except Exception as e:  # never let a probe hiccup kill the tray
            snap = Snapshot(error=str(e))
        GLib.idle_add(self._update, snap)

    def _update(self, snap: Snapshot):
        if snap.error is None:  # on a hiccup, keep the last good label
            installed = next((r for r in snap.rows if r.installed), None)
            if installed and installed.usage is not None:
                # Identity is the email, not the directory name; the local
                # part keeps the label short.
                who = (installed.email or installed.name).split("@")[0]
                self.indicator.set_label(f" {who} {installed.usage['five']:.0f}%", "")
            else:
                self.indicator.set_label(f" {snap.installed_name}", "")
        self.indicator.set_menu(self.build_menu(snap))
        return False

    def row_item(self, markup: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem()
        label = Gtk.Label(xalign=0)
        label.set_markup(f"<tt>{markup}</tt>")
        item.add(label)
        return item

    def build_menu(self, snap: Snapshot) -> Gtk.Menu:
        menu = Gtk.Menu()
        menu.append(self.row_item("<b>CLAUDE</b>"))
        for row in snap.rows:
            who = row.email or row.name  # emails are identity; dirs are plumbing
            if row.usage is not None:
                u = row.usage
                stale = stale_age_min(u["asof"], snap.now)
                age = (
                    f"<span foreground='{DIM}'> · {stale:.0f}m old</span>"
                    if stale is not None
                    else ""
                )
                r5 = reset_in(u["r5"], snap.now)
                r7 = reset_in(u["r7"], snap.now)
                cells = (
                    f"{esc(who):<32} "
                    f"{bar_markup(u['five'])}"
                    f"<span foreground='{DIM}'> {r5:<4}</span>"
                    f"{bar_markup(u['seven'])}"
                    f"<span foreground='{DIM}'> {r7:<3}</span>"
                    f"{age}"
                )
            else:
                cells = (
                    f"{esc(who):<32} "
                    f"<span foreground='{DIM}'>{esc(str(row.status))}</span>"
                )
            if row.installed:
                cells += f" <span foreground='{GREEN}'>◀ installed</span>"
            menu.append(self.row_item(cells))
        if snap.error is not None:
            menu.append(
                self.row_item(f"<span foreground='{DIM}'>{esc(snap.error)}</span>")
            )
        elif not snap.rows:
            menu.append(self.row_item("no accounts found"))

        # No manual Refresh / Rebalance items: the systemd tick rebalances and
        # refreshes (usage + token renewal) on its own cadence, and the tray
        # repaints from that cache below — so both were just "skip the wait"
        # shortcuts. The tray is a pure viewer; balancing lives in the tick.
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", Gtk.main_quit)
        menu.append(quit_item)
        menu.show_all()
        return menu


def install_autostart() -> int:
    autostart = Path(os.path.expanduser("~/.config/autostart"))
    autostart.mkdir(parents=True, exist_ok=True)
    exe = shutil.which("agent-balance-tray") or str(Path(__file__).resolve())
    (autostart / "agent-balance-tray.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=agent-balance tray\n"
        "Comment=Claude account usage in the tray\n"
        f"Exec={exe}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    print(
        f"agent-balance-tray: autostart installed — starts at next login; "
        f"start it now with: {exe} &"
    )
    return 0


def main() -> int:
    if "--install-autostart" in sys.argv:
        return install_autostart()
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
