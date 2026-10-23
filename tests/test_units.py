import os
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict

import pytest

from x11_mcp import isolation
from x11_mcp.desktop import AgentDesktop, Config, openbox_config, write_xauthority
from x11_mcp.isolation import NameLock, Sessions, name_in_use, parse_stat, scrub_env
from x11_mcp.layout import tile
from x11_mcp.server import _parse_combo, _truncate, _zoom_factor
from x11_mcp.x import _Keymap, char_keysym, decode_states, keysym_from_name, moveresize_data, plan_typing


# -- config ------------------------------------------------------------------


def test_config_defaults_and_parsing():
    cfg = Config.from_env({})
    assert (cfg.name, cfg.width, cfg.height, cfg.server, cfg.workspaces) == ("agent", 1920, 1080, "xvfb", 4)
    cfg = Config.from_env({"X11_MCP_RESOLUTION": "1280X720", "X11_MCP_SERVER": "Xvnc",
                           "X11_MCP_WORKSPACES": "9", "X11_MCP_PASS_ENV": "SSH_AUTH_SOCK, LANG",
                           "X11_MCP_SHARE_HOME": "1"})
    assert (cfg.width, cfg.height, cfg.server, cfg.workspaces) == (1280, 720, "xvnc", 9)
    assert cfg.pass_env == ("SSH_AUTH_SOCK", "LANG") and cfg.share_home


@pytest.mark.parametrize("env", [
    {"X11_MCP_NAME": "../x"},
    {"X11_MCP_RESOLUTION": "big"},
    {"X11_MCP_RESOLUTION": "100x100"},
    {"X11_MCP_SERVER": "xorg"},
    {"X11_MCP_WORKSPACES": "0"},
    {"X11_MCP_WORKSPACES": "33"},
    {"X11_MCP_PASS_ENV": "DISPLAY"},
    {"X11_MCP_PASS_ENV": "SSH_AUTH_SOCK,WAYLAND_DISPLAY"},
])
def test_config_rejects(env):
    with pytest.raises(ValueError):
        Config.from_env(env)


# -- environment -------------------------------------------------------------

_USER_SESSION = {
    "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "/run/user/1000/xauth_x",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "XDG_RUNTIME_DIR": "/run/user/1000",
    "SESSION_MANAGER": "local/host:@/tmp/.ICE-unix/42", "TMUX": "/tmp/tmux-1000/default,1,0",
    "KITTY_LISTEN_ON": "unix:@kitty", "SSH_AUTH_SOCK": "/run/user/1000/ssh",
    "MANGO_INSTANCE_SIGNATURE": "x", "SWAYSOCK": "/run/user/1000/sway.sock", "HYPRLAND_INSTANCE_SIGNATURE": "y",
    "GTK_IM_MODULE": "fcitx", "XMODIFIERS": "@im=fcitx", "GDK_BACKEND": "wayland", "QT_QPA_PLATFORM": "wayland",
    "NIXOS_OZONE_WL": "1", "XDG_CONFIG_HOME": "/home/u/.config", "XDG_CURRENT_DESKTOP": "mango",
    "X11_MCP_NAME": "agent", "PATH": "/usr/bin", "LANG": "en_US.UTF-8", "EDITOR": "vim",
}


def test_scrub_env():
    env = scrub_env(_USER_SESSION, pass_env=("SSH_AUTH_SOCK", "DISPLAY"))
    assert env == {"PATH": "/usr/bin", "LANG": "en_US.UTF-8", "EDITOR": "vim",
                   "SSH_AUTH_SOCK": "/run/user/1000/ssh"}  # DISPLAY can never be passed
    assert "XDG_CONFIG_HOME" in scrub_env(_USER_SESSION, share_home=True)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    for key, value in _USER_SESSION.items():
        if key != "PATH":  # keep a PATH that has sleep and friends
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "user-run"))
    (tmp_path / "user-run").mkdir()
    d = AgentDesktop(Config(pass_env=("SSH_AUTH_SOCK",)))
    d.runtime = tmp_path / "agent-run"
    d.home = tmp_path / "agent-home"
    d.display = ":7"
    d.xauthority = d.runtime / "Xauthority"
    return d


def test_app_env_points_only_at_agent(desktop):
    env = desktop.app_env()
    assert env["DISPLAY"] == ":7"
    assert env["XAUTHORITY"] == str(desktop.runtime / "Xauthority")
    assert env["XDG_RUNTIME_DIR"] == str(desktop.runtime)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={desktop.runtime / 'bus'}"
    assert env["HOME"] == str(desktop.home)
    assert (env["GDK_BACKEND"], env["QT_QPA_PLATFORM"], env["XDG_SESSION_TYPE"]) == ("x11", "xcb", "x11")
    for leaked in ("WAYLAND_DISPLAY", "SESSION_MANAGER", "TMUX", "KITTY_LISTEN_ON", "SWAYSOCK",
                   "MANGO_INSTANCE_SIGNATURE", "GTK_IM_MODULE", "XMODIFIERS", "NIXOS_OZONE_WL",
                   "XDG_CONFIG_HOME", "X11_MCP_NAME"):
        assert leaked not in env, leaked
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/ssh"  # explicitly passed through


def test_shell_uses_agent_env_and_kills_on_timeout(desktop, monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "alive", lambda: True)
    desktop.home.mkdir()
    status, out = desktop.shell('echo "$DISPLAY $TMUX"; pwd; exit 3')
    assert status == 3
    assert out.decode().split("\n")[:2] == [":7 ", str(desktop.home)]

    marker = tmp_path / "child-survived"
    start = time.monotonic()
    status, _ = desktop.shell(f"(sleep 1; touch {marker}) & sleep 5", timeout=0.3)
    assert status is None and time.monotonic() - start < 2
    time.sleep(1.2)
    assert not marker.exists()


# -- X server setup ----------------------------------------------------------


def test_xauthority_matches_any_display(tmp_path):
    from Xlib import xauth

    path = tmp_path / "Xauthority"
    write_xauthority(path, b"\x01" * 16)
    assert path.stat().st_mode & 0o777 == 0o600
    auth = xauth.Xauthority(str(path))
    # python-xlib looks up FamilyLocal + hostname; the display number is left open.
    for display_number in (1, 7, 42):
        assert auth.get_best_auth(xauth.FamilyLocal, socket.gethostname().encode(), display_number) == (
            b"MIT-MAGIC-COOKIE-1", b"\x01" * 16)
    assert any(entry[0] == 0xFFFF for entry in auth.entries)  # FamilyWild, for libXau


def test_openbox_config(tmp_path):
    root = ET.fromstring(openbox_config(6, tmp_path / "menu.xml"))
    ns = {"ob": "http://openbox.org/3.4/rc"}
    assert root.find("ob:desktops/ob:number", ns).text == "6"
    assert root.find("ob:desktops/ob:popupTime", ns).text == "0"
    assert root.find("ob:focus/ob:followMouse", ns).text == "no"
    assert root.find("ob:placement/ob:policy", ns).text == "Smart"
    assert root.find("ob:keyboard", ns) is not None and not list(root.find("ob:keyboard", ns))
    contexts = {c.get("name") for c in root.iterfind("ob:mouse/ob:context", ns)}
    assert "Root" not in contexts and "Desktop" not in contexts
    for bind in root.iterfind(".//ob:mousebind", ns):
        assert "-" not in bind.get("button"), "no modifier+button bindings (they would eat app input)"
    assert not root.findall(".//ob:action[@name='Execute']", ns)


# -- layout and EWMH ---------------------------------------------------------


def test_tile_columns_rows_grid():
    area = (0, 0, 1920, 1080)
    assert tile(2, area, "columns") == [(0, 0, 960, 1080), (960, 0, 960, 1080)]
    assert tile(3, (10, 20, 100, 50), "columns") == [(10, 20, 34, 50), (44, 20, 33, 50), (77, 20, 33, 50)]
    assert tile(2, area, "rows") == [(0, 0, 1920, 540), (0, 540, 1920, 540)]
    assert tile(1, area, "grid") == [area]
    assert tile(4, area, "grid") == [(0, 0, 960, 540), (960, 0, 960, 540), (0, 540, 960, 540), (960, 540, 960, 540)]
    # 3 in a grid: two on top, the last one takes the whole bottom row
    assert tile(3, area, "grid") == [(0, 0, 960, 540), (960, 0, 960, 540), (0, 540, 1920, 540)]
    assert tile(0, area, "grid") == []
    with pytest.raises(ValueError):
        tile(2, area, "spiral")


@pytest.mark.parametrize("n", range(1, 12))
@pytest.mark.parametrize("layout", ["columns", "rows", "grid"])
def test_tile_covers_area_exactly(n, layout):
    rects = tile(n, (5, 7, 1001, 777), layout)
    assert len(rects) == n
    assert sum(w * h for _, _, w, h in rects) == 1001 * 777
    assert all(5 <= x and x + w <= 1006 and 7 <= y and y + h <= 784 for x, y, w, h in rects)


def test_moveresize_data():
    flags, x, y, w, h = moveresize_data((100, 50, 800, 600), (1, 1, 22, 1))
    assert flags & 0xF == 1  # NorthWest gravity: x/y are the frame's corner
    assert flags >> 8 & 0xF == 0xF and flags >> 12 & 0x3 == 2  # x/y/w/h set, from a pager
    assert (x, y, w, h) == (100, 50, 798, 577)  # client size = frame minus extents
    assert moveresize_data((-10, 0, 10, 10), (0, 0, 0, 0))[1] == 2**32 - 10


def test_decode_states():
    assert decode_states(["_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"]) == ["maximized"]
    assert decode_states(["_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_FOO"]) == [
        "maximized_vert", "fullscreen"]


# -- keyboard ----------------------------------------------------------------


def test_parse_combo():
    assert _parse_combo("ctrl+shift+t") == (["ctrl", "shift"], ord("t"))
    assert _parse_combo("super+Return") == (["super"], 0xFF0D)
    assert _parse_combo("enter") == ([], 0xFF0D)
    assert _parse_combo("f5") == ([], 0xFFC2)
    assert _parse_combo("ctrl+alt+Delete") == (["ctrl", "alt"], 0xFFFF)
    assert _parse_combo("XF86AudioMute") == ([], 0x1008FF12)
    assert _parse_combo("ctrl+é") == (["ctrl"], 0xE9)
    assert _parse_combo("super") == ([], 0xFFEB)  # a modifier on its own presses Super_L
    for bad in ("ctrl+alt+XF86Switch_VT_1", "Terminate_Server", "XF86Ungrab", "hyper+a", "ctrl+nosuchkey", ""):
        with pytest.raises(ValueError):
            _parse_combo(bad)


def test_char_keysym():
    assert char_keysym("a") == ord("a")
    assert char_keysym("é") == 0xE9  # Latin-1 keysyms are the code point
    assert char_keysym("✓") == 0x1002713
    assert char_keysym("😀") == 0x101F600
    assert char_keysym("\n") == 0xFF0D and char_keysym("\t") == 0xFF09
    with pytest.raises(ValueError):
        char_keysym("\x07")
    assert keysym_from_name("nosuchkey") == 0


def _us_keymap():
    """A slice of a US core keymap: a/A on 38, 1/! on 10, Shift_L on 50, spares 8 and 97."""
    mapping = [(0, 0)] * 100
    mapping[38 - 8] = (ord("a"), ord("A"))
    mapping[10 - 8] = (ord("1"), ord("!"))
    mapping[50 - 8] = (0xFFE1, 0)
    mapping[36 - 8] = (0xFF0D, 0)
    for kc in range(8, 108):
        if kc not in (8, 97) and not any(mapping[kc - 8]):
            mapping[kc - 8] = (0xFFFFFF, 0)  # anything but NoSymbol
    return mapping


def test_keymap_finds_keys_and_spares():
    assigned = OrderedDict()
    km = _Keymap(_us_keymap(), 8, assigned)
    assert km.lookup[ord("a")] == (38, False) and km.lookup[ord("A")] == (38, True)
    assert km.lookup[ord("!")] == (10, True)
    assert km.spare == [8, 97]
    # A spare we mapped earlier stays ours; one someone else remapped is dropped.
    mapping = _us_keymap()
    mapping[8 - 8] = (0xE9, 0xE9)
    mapping[97 - 8] = (0x41, 0x41)
    assigned = OrderedDict([(8, 0xE9), (97, 0xF1)])
    km = _Keymap(mapping, 8, assigned)
    assert km.spare == [8] and assigned == OrderedDict([(8, 0xE9)])


def test_plan_typing_uses_keymap_then_spares():
    km = _Keymap(_us_keymap(), 8, OrderedDict())
    assigned = OrderedDict()
    steps = plan_typing([ord("a"), ord("A"), 0xE9, ord("!"), 0xE9], km.lookup, km.spare, assigned)
    assert steps == [("key", 38, False), ("key", 38, True), ("map", 8, 0xE9), ("key", 8, False),
                     ("key", 10, True), ("key", 8, False)]
    assert assigned == OrderedDict([(8, 0xE9)])
    # Next call: é is still on 8, so only the new character takes a spare.
    steps = plan_typing([0xE9, 0xF1], km.lookup, km.spare, assigned)
    assert steps == [("key", 8, False), ("map", 97, 0xF1), ("key", 97, False)]


def test_plan_typing_flushes_before_reusing_a_spare():
    km = _Keymap(_us_keymap(), 8, OrderedDict())
    assigned = OrderedDict()
    steps = plan_typing([0x1000001, 0x1000002, 0x1000003, 0x1000001], km.lookup, km.spare, assigned)
    assert steps == [
        ("map", 8, 0x1000001), ("key", 8, False),
        ("map", 97, 0x1000002), ("key", 97, False),
        ("flush",),  # both spares used in this batch: let apps read them before remapping
        ("map", 8, 0x1000003), ("key", 8, False),  # least recently used
        ("map", 97, 0x1000001), ("key", 97, False),
    ]


def test_plan_typing_without_spares():
    with pytest.raises(RuntimeError):
        plan_typing([0xE9], {}, [], OrderedDict())
    assert plan_typing([ord("a")], {ord("a"): (38, False)}, [], OrderedDict()) == [("key", 38, False)]


# -- processes and names -----------------------------------------------------


def test_parse_stat_with_odd_command_names():
    text = "1234 (my (weird) app) S 10 1234 1234 0 -1 4194560 " + " ".join(["0"] * 12) + " 98765 0 0"
    assert parse_stat(text) == (10, 1234, 98765, "S")


def test_sessions_track_and_kill_everything(tmp_path):
    sessions = Sessions()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    proc = isolation.spawn("sleep 30 & sleep 30 & wait", env, str(tmp_path), tmp_path / "logs", sessions)
    time.sleep(0.3)
    members = sessions.members()
    assert proc.pid in members and len(members) == 3
    assert sessions.owner(members[-1]) == proc.pid
    assert sessions.owner(os.getpid()) is None
    sessions.kill_all(children=[proc], grace=2)
    assert proc.poll() is not None
    assert not any(os.path.exists(f"/proc/{pid}") and isolation.proc_stat(pid)[3] != "Z" for pid in members)


def test_sessions_ignore_processes_older_than_the_session():
    sessions = Sessions()
    proc = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        sessions.add(proc.pid, isolation.boot_ticks() + 10_000)  # as if the session was created later
        assert sessions.members() == [] and sessions.owner(proc.pid) is None
    finally:
        proc.kill()
        proc.wait()


def test_name_lock(tmp_path):
    base = tmp_path / "x11-mcp"
    first, second = NameLock(base), NameLock(base)
    assert first.claim("agent") == "agent"
    assert second.claim("agent") == "agent-2"
    assert name_in_use(base, "agent") and not name_in_use(base, "agent-3")
    (base / "agent-2").mkdir()
    second.release()
    (base / "stale").mkdir()
    third = NameLock(base)
    assert third.claim("agent") == "agent-2"
    assert not (base / "stale").exists()  # swept: nobody holds its lock
    first.release()
    third.release()
    assert not name_in_use(base, "agent")


def test_user_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert isolation.user_runtime_dir() == tmp_path


# -- output ------------------------------------------------------------------


def test_zoom_factor():
    assert _zoom_factor(392, 200) == 4.0
    assert _zoom_factor(784, 100) == 2.0
    assert _zoom_factor(1920, 1080) == 1.0


def test_truncate_keeps_head_and_tail():
    text = "a" * 100 + "b" * 100
    out = _truncate(text, limit=50)
    assert out.startswith("a" * 25) and out.endswith("b" * 25)
    assert "150 characters cut" in out
    assert _truncate("short") == "short"
