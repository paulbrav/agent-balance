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

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

GREEN, YELLOW, RED, DIM = "#73d216", "#edd400", "#ef2929", "#888888"
BAR_ON, BAR_OFF = "▰", "▱"


def load_agent_balance():
    """Import the agent_balance module — from the same directory when run
    out of the repo, else from the installed `agent-balance` script."""
    try:
        import agent_balance
        return agent_balance
    except ImportError:
        pass
    candidates = [Path(__file__).resolve().with_name("agent_balance.py")]
    installed = shutil.which("agent-balance")
    if installed:
        candidates.append(Path(installed))
    for cand in candidates:
        if cand.is_file():
            loader = importlib.machinery.SourceFileLoader(
                "agent_balance", str(cand))
            spec = importlib.util.spec_from_loader("agent_balance", loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            sys.modules["agent_balance"] = mod
            return mod
    sys.exit("agent-balance-tray: agent_balance.py not found "
             "(install agent-balance or run from the repo)")


ab = load_agent_balance()


def esc(text: str) -> str:
    return GLib.markup_escape_text(text)


def bar_markup(pct: float) -> str:
    p = int(pct)
    filled = (p + 5) // 10
    if filled == 0 and p > 0:
        filled = 1
    filled = min(filled, 10)
    color = GREEN if p < 50 else YELLOW if p < 80 else RED
    return (f'<span foreground="{color}">{BAR_ON * filled}'
            f'{BAR_OFF * (10 - filled)} {p:3d}%</span>')


def reset_in(epoch: int, now: float) -> str:
    s = epoch - now
    if epoch == 0:
        return "-"
    if s <= 0:
        return "now"
    if s < 3600:
        return f"{int(s // 60) + 1}m"
    if s < 86400:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def collect():
    """Worker-thread data gathering: discovery + cache-first probes."""
    cfg = ab.make_config()
    now = time.time()
    state = ab.read_state(cfg, now)
    rows = []
    for a in ab.discover_accounts(cfg):
        rows.append((a.name, a.email, ab.probe(a, cfg, now),
                     a.name == state["installed"]))
    return cfg, now, state, rows


class Tray:
    def __init__(self):
        self.indicator = AppIndicator.Indicator.new(
            "agent-balance", "utilities-system-monitor-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.build_menu(None, 0, None, []))
        self.refresh()
        interval = max(int(ab.make_config().interval), 30)
        GLib.timeout_add_seconds(interval, self.refresh)

    def refresh(self):
        threading.Thread(target=self._work, daemon=True).start()
        return True  # keep the GLib timer alive

    def _work(self):
        try:
            data = collect()
        except Exception as e:  # never let a probe hiccup kill the tray
            data = None, 0, None, [("error", str(e), "error", False)]
        GLib.idle_add(self._update, data)

    def _update(self, data):
        cfg, now, state, rows = data
        installed = next((r for r in rows if r[3]), None)
        if installed and isinstance(installed[2], ab.Usage):
            self.indicator.set_label(
                f" {installed[0]} {installed[2].five:.0f}%", "")
        elif state is not None:
            self.indicator.set_label(f" {state['installed']}", "")
        self.indicator.set_menu(self.build_menu(cfg, now, state, rows))
        return False

    def row_item(self, markup: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem()
        label = Gtk.Label(xalign=0)
        label.set_markup(f"<tt>{markup}</tt>")
        item.add(label)
        return item

    def build_menu(self, cfg, now, state, rows) -> Gtk.Menu:
        menu = Gtk.Menu()
        menu.append(self.row_item("<b>CLAUDE</b>"))
        for name, email, st, is_installed in rows:
            if isinstance(st, ab.Usage):
                cells = (f"{esc(name):<9} {esc(email):<30} "
                         f"{bar_markup(st.five)}"
                         f"<span foreground='{DIM}'> {reset_in(st.r5, now):<4}</span>"
                         f"{bar_markup(st.seven)}"
                         f"<span foreground='{DIM}'> {reset_in(st.r7, now):<3}</span>")
            else:
                cells = (f"{esc(name):<9} {esc(email):<30} "
                         f"<span foreground='{DIM}'>{esc(str(st))}</span>")
            if is_installed:
                cells += f" <span foreground='{GREEN}'>◀ installed</span>"
            menu.append(self.row_item(cells))
        if not rows:
            menu.append(self.row_item("no accounts found"))

        menu.append(Gtk.SeparatorMenuItem())
        tick = Gtk.MenuItem(label="Tick now")
        tick.connect("activate", self.on_tick)
        menu.append(tick)
        refresh = Gtk.MenuItem(label="Refresh")
        refresh.connect("activate", lambda *_: self.refresh())
        menu.append(refresh)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", Gtk.main_quit)
        menu.append(quit_item)
        menu.show_all()
        return menu

    def on_tick(self, *_):
        def run():
            exe = shutil.which("agent-balance")
            if exe:
                subprocess.run([exe, "tick"], capture_output=True)
            else:
                ab.tick(ab.make_config(), out=lambda *_: None)
            self.refresh()
        threading.Thread(target=run, daemon=True).start()


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
        "X-GNOME-Autostart-enabled=true\n")
    print(f"agent-balance-tray: autostart installed — starts at next login; "
          f"start it now with: {exe} &")
    return 0


def main() -> int:
    if "--install-autostart" in sys.argv:
        return install_autostart()
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
