"""End-to-end run against a real Xvfb + Openbox. Opt in with X11_MCP_LIVE=1.

Starts an agent desktop, drives terminals in it through the MCP tool
functions, and checks that the user's session saw none of it. Set
X11_MCP_SERVER=xvnc to run the same test against Xvnc.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("X11_MCP_LIVE") != "1", reason="live test")

TERMINAL = os.environ.get("X11_MCP_TEST_TERMINAL", "xterm -u8 -fa Monospace -fs 11")


def _systemd_env():
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True)
    except OSError:
        return ""
    return out.stdout


def _user_runtime_entries():
    from x11_mcp.isolation import user_runtime_dir

    return set(os.listdir(user_runtime_dir()))


def _user_display_sockets() -> set:
    """Listening socket addresses of the user's own X server and compositor."""
    paths = set()
    display = os.environ.get("DISPLAY", "")
    if display.startswith(":"):
        number = display[1:].split(".")[0]
        paths |= {f"/tmp/.X11-unix/X{number}", f"@/tmp/.X11-unix/X{number}"}
    wayland = os.environ.get("WAYLAND_DISPLAY")
    if wayland:
        paths.add(wayland if wayland.startswith("/") else str(Path(os.environ["XDG_RUNTIME_DIR"]) / wayland))
    return paths


def _client_inodes_of(paths: set) -> set:
    """Socket inodes of every client connected to one of `paths` (from `ss -x`)."""
    out = subprocess.run(["ss", "-xH"], capture_output=True, text=True, check=True).stdout
    peers = set()
    for line in out.splitlines():
        fields = line.split()
        # u_str ESTAB 0 0 /tmp/.X11-unix/X0 <server inode> * <client inode>
        if len(fields) >= 8 and fields[4] in paths:
            peers.add(fields[7])
    return peers


def _socket_inodes(pid: int) -> set:
    inodes = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        pass
    return inodes


def assert_no_agent_process_on_user_display(server):
    """No process the agent desktop runs holds a connection to the user's X server or compositor."""
    desktop = server._state.desktop
    pids = desktop.sessions.members()
    assert pids, "found no agent processes to check"
    agent_sockets = set().union(*(_socket_inodes(pid) for pid in pids))
    # Positive control: the same method must see agent apps on the agent's own display.
    number = desktop.display[1:]
    agent_clients = _client_inodes_of({f"/tmp/.X11-unix/X{number}", f"@/tmp/.X11-unix/X{number}"})
    assert agent_sockets & agent_clients, "could not see agent apps on the agent display; the check is broken"
    paths = _user_display_sockets()
    if paths:
        leaked = agent_sockets & _client_inodes_of(paths)
        assert not leaked, f"an agent process is connected to the user's display ({paths})"


@pytest.fixture(scope="module")
def server():
    os.environ["X11_MCP_NAME"] = "livetest"
    os.environ.setdefault("X11_MCP_WORKSPACES", "3")
    from x11_mcp import server

    server._state = server._State()
    before = (_systemd_env(), _user_runtime_entries())
    yield server
    server._state.shutdown()
    time.sleep(0.5)
    after = (_systemd_env(), _user_runtime_entries())
    assert after[0] == before[0], "user's systemd environment changed"
    assert after[1] - before[1] <= {"x11-mcp"}, f"new entries in user runtime dir: {after[1] - before[1]}"


def _wait_for_file(path: Path, timeout=5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().endswith("\n"):
            break
        time.sleep(0.1)
    return path.read_text() if path.exists() else ""


def _png_size(data: bytes):
    import struct

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def _launch_terminal(server, workspace=None) -> int:
    out = server.launch(TERMINAL, workspace=workspace, wait_for_window=15)
    assert "new window" in out, out
    window_id = int(out.split("new window ")[1].split(":")[0])
    server.wait_until_idle(quiet_ms=300, timeout=5)
    time.sleep(0.5)  # let the shell print its prompt and read the terminal
    return window_id


def test_desktop_info(server):
    info = server.desktop_info()
    assert "screen: 1920x1080" in info
    assert "Openbox" in info
    assert "workspaces: 1*(0) 2(0) 3(0)" in info


def test_typing_and_screenshots(server, tmp_path):
    _launch_terminal(server, workspace=1)
    marker = tmp_path / "ascii"
    server.type_text(f"echo agent-ok > {marker}\n")
    assert _wait_for_file(marker).strip() == "agent-ok"

    # Characters that are not on the US keymap go through spare keycodes.
    text = "héllo ✓ 日本 ñ€ Ωmega «»"
    unicode_marker = tmp_path / "unicode"
    server.type_text(f"printf '%s\\n' '{text}' > {unicode_marker}")
    server.press_keys("Return")
    assert _wait_for_file(unicode_marker).strip() == text

    # A combo: ctrl+u clears the line, so the half-typed command never runs.
    never = tmp_path / "never"
    server.type_text(f"touch {never}")
    server.press_keys("ctrl+u Return")
    time.sleep(0.5)
    assert not never.exists()

    shot = server.screenshot(scale=0.5)
    assert _png_size(shot[0].data) == (960, 540)
    assert "960x540" in shot[1]
    region = server.screenshot(x=10, y=20, width=300, height=200, format="jpeg")
    assert region[0].data[:2] == b"\xff\xd8"
    zoomed = server.zoom(0, 0, 392, 200)
    assert _png_size(zoomed[0].data) == (1568, 800)
    with pytest.raises(ValueError):
        server.zoom(0, 0, 100, 100, factor=5)
    with pytest.raises(ValueError):
        server.screenshot(x=1900, y=0, width=100, height=100)

    server.mouse_move(960, 540)
    assert "(960, 540)" in server.desktop_info()
    server.click(960, 540)
    server.click(button="right")
    server.scroll(960, 540, down=3)
    server.drag(100, 100, 300, 300)
    with pytest.raises(ValueError):
        server.press_keys("ctrl+alt+Terminate_Server")
    with pytest.raises(ValueError):
        server.press_keys("XF86Switch_VT_1")


def _frame(server, window_id):
    return server._state.x.window(window_id).frame


def _close_enough(actual, wanted, slack=40):
    """Terminals snap to character cells, so they may come out a little smaller."""
    ax, ay, aw, ah = actual
    wx, wy, ww, wh = wanted
    return (ax, ay) == (wx, wy) and wanted[2] - slack <= aw <= ww and wh - slack <= ah <= wh


def test_windows_and_workspaces(server):
    from PIL import Image
    import io

    a = _launch_terminal(server, workspace=2)
    b = _launch_terminal(server, workspace=2)
    info = server.desktop_info()
    assert "2*(2)" in info, info
    assert f"focused: {b}" in info

    listing = server.list_windows(workspace=2).splitlines()
    assert listing[1].startswith(f"{b}:") and "[focused]" in listing[1]  # topmost first
    assert listing[2].startswith(f"{a}:")

    out = server.tile_windows(layout="columns")
    assert "tiled 2 windows on workspace 2" in out
    assert _close_enough(_frame(server, a), (0, 0, 960, 1080)), _frame(server, a)
    assert _close_enough(_frame(server, b), (960, 0, 960, 1080)), _frame(server, b)
    server.tile_windows(workspace=2, layout="rows")
    assert _close_enough(_frame(server, a), (0, 0, 1920, 540)), _frame(server, a)
    assert _close_enough(_frame(server, b), (0, 540, 1920, 540)), _frame(server, b)

    server.move_window_to_workspace(3, b)
    info = server.desktop_info()
    assert "2*(1)" in info and "3(1)" in info, info

    # Workspace 3 is hidden: the view switches for the shot and comes back.
    server.move_resize_window(b, 200, 150, 700, 500)
    shot = Image.open(io.BytesIO(server.screenshot(workspace=3)[0].data))
    assert "2*(1)" in server.desktop_info()
    content = server._state.x.window(b).content
    assert shot.getpixel((content[0] + 50, content[1] + content[3] - 30)) == (255, 255, 255)  # xterm's white
    here = Image.open(io.BytesIO(server.screenshot()[0].data))
    assert here.getpixel((content[0] + 50, content[1] + content[3] - 30)) != (255, 255, 255)

    server.focus_window(b)
    assert "3*(1)" in server.desktop_info()
    assert "maximized" in server.window_action("toggle_maximize", b)
    assert "maximized" not in server.window_action("toggle_maximize")  # the focused one
    assert "fullscreen" in server.window_action("toggle_fullscreen", b)
    assert _frame(server, b) == (0, 0, 1920, 1080)
    out = server.move_resize_window(b, 100, 120, 800, 600)  # leaves fullscreen first
    assert "fullscreen" not in out
    assert _close_enough(_frame(server, b), (100, 120, 800, 600)), _frame(server, b)

    server.set_workspace_count(4)
    assert "4(0)" in server.desktop_info()
    server.set_workspace_count(2)  # b moves off the removed workspace 3
    info = server.desktop_info()
    assert "workspaces: 1(" in info and "3(" not in info, info
    with pytest.raises(ValueError):
        server.view_workspace(3)
    server.set_workspace_count(3)

    server.close_window(a)
    assert str(a) not in server.list_windows()
    assert "killed" in server.window_action("kill", b)
    assert str(b) not in server.list_windows()


def test_launch_reports(server, tmp_path):
    out = server.launch("exit 3", wait_for_window=5)
    assert "exited with status 3" in out and "new window" not in out
    out = server.launch("true", wait_for_window=0.5)
    assert "no window yet" in out

    # A window opened by a process outside this launch's session tree, like a
    # single-instance app handing the launch to its running copy.
    out = server.launch(f"setsid -f {TERMINAL}", wait_for_window=10)
    assert "new window" in out and "not opened by this command's own processes" in out, out
    window_id = int(out.split("new window ")[1].split(":")[0])
    server.close_window(window_id)

    # A launched app's environment points only at the agent desktop.
    env_file = tmp_path / "env"
    server.launch(f"env > {env_file}", wait_for_window=0)
    _wait_for_file(env_file)
    env = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    desktop = server._state.desktop
    assert env["DISPLAY"] == desktop.display != os.environ.get("DISPLAY")
    assert env["XAUTHORITY"] == str(desktop.xauthority)
    assert env["XDG_RUNTIME_DIR"] == str(desktop.runtime)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={desktop.runtime / 'bus'}"
    assert env["GDK_BACKEND"] == "x11" and env["QT_QPA_PLATFORM"] == "xcb"
    for leaked in ("WAYLAND_DISPLAY", "SWAYSOCK", "TMUX", "SESSION_MANAGER", "SSH_AUTH_SOCK"):
        assert leaked not in env, leaked

    out = server.bash('echo "$DISPLAY"; exit 2')
    assert out.splitlines() == ["exit status 2", desktop.display]
    assert server.bash("sleep 10", timeout=1).startswith("timed out")


def test_wait_until_idle(server):
    out = server.launch(f"{TERMINAL} -e sh -c 'for i in $(seq 15); do echo output line $i; sleep 0.1; done; sleep 30'",
                        wait_for_window=10)
    window_id = int(out.split("new window ")[1].split(":")[0])
    start = time.monotonic()
    assert server.wait_until_idle(quiet_ms=400, timeout=8).startswith("idle")
    assert time.monotonic() - start >= 1.0  # it saw the output scrolling
    server.close_window(window_id)

    # Never idle: a terminal printing continuously.
    out = server.launch(f"{TERMINAL} -e sh -c 'while :; do date +%N; sleep 0.05; done'", wait_for_window=10)
    window_id = int(out.split("new window ")[1].split(":")[0])
    out = server.wait_until_idle(quiet_ms=300, timeout=1.5)
    assert out.startswith("not idle after 1.5s; still changing around ("), out
    server.close_window(window_id)

    # A blinking text cursor is ignored by default, but not with min_area=0.
    out = server.launch(f"{TERMINAL} -bc -bcn 100 -bcf 100", wait_for_window=10)
    window_id = int(out.split("new window ")[1].split(":")[0])
    server.wait_until_idle(quiet_ms=300, timeout=5)
    assert server.wait_until_idle(quiet_ms=500, timeout=3).startswith("idle")
    assert server.wait_until_idle(quiet_ms=500, timeout=1.5, min_area=0).startswith("not idle")
    server.close_window(window_id)


def _vnc_viewer_tries_input(port: int):
    """Connect as a VNC viewer (RFB 3.8, no auth) and send a click at (5, 5) and a keypress."""
    import socket
    import struct

    def recv(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            assert chunk, "VNC server closed the connection"
            data += chunk
        return data

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        assert recv(sock, 12).startswith(b"RFB 003.")
        sock.sendall(b"RFB 003.008\n")
        count = recv(sock, 1)[0]
        assert 1 in recv(sock, count), "no 'None' security type offered"
        sock.sendall(b"\x01")
        assert struct.unpack(">I", recv(sock, 4))[0] == 0
        sock.sendall(b"\x01")  # ClientInit, shared
        recv(sock, 20)
        recv(sock, struct.unpack(">I", recv(sock, 4))[0])  # desktop name
        for mask in (1, 0):
            sock.sendall(struct.pack(">BBHH", 5, mask, 5, 5))  # PointerEvent
        for down in (1, 0):
            sock.sendall(struct.pack(">BBHI", 4, down, 0, ord("x")))  # KeyEvent
        time.sleep(0.5)


def test_cli_env_and_watch(server):
    import re
    import sys

    from x11_mcp.x import open_display

    server.desktop_info()  # make sure the desktop is up
    desktop = server._state.desktop
    out = subprocess.run([sys.executable, "-m", "x11_mcp", "env", desktop.name],
                         capture_output=True, text=True, check=True).stdout
    exports = dict(re.findall(r"export (\w+)='([^']*)'", out))
    assert exports == {"DISPLAY": desktop.display, "XAUTHORITY": str(desktop.xauthority)}
    open_display(exports["DISPLAY"], exports["XAUTHORITY"]).close()
    with pytest.raises(Exception):  # the cookie is what lets a client in
        open_display(exports["DISPLAY"], "/dev/null").close()
    missing = subprocess.run([sys.executable, "-m", "x11_mcp", "env", "no-such-desktop"],
                             capture_output=True, text=True).stdout
    assert missing.startswith("# no running agent desktop")

    if desktop.config.server == "xvnc":
        out = subprocess.run([sys.executable, "-m", "x11_mcp", "watch", desktop.name],
                             capture_output=True, text=True, check=True).stdout
        assert f"localhost:{desktop.vnc_port}" in out
        server.mouse_move(700, 400)
        _vnc_viewer_tries_input(desktop.vnc_port)
        assert server._state.x.pointer() == (700, 400), "a VNC viewer moved the agent's pointer"
        return
    import shutil

    if not shutil.which("x11vnc"):
        pytest.skip("x11vnc not installed")
    watch = subprocess.Popen([sys.executable, "-m", "x11_mcp", "watch", desktop.name],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        port = None
        deadline = time.monotonic() + 15
        while port is None and time.monotonic() < deadline:
            line = watch.stdout.readline()
            match = re.match(r"PORT=(\d+)", line)
            if match:
                port = int(match.group(1))
        assert port, "x11vnc did not report a port"
        server.mouse_move(700, 400)
        _vnc_viewer_tries_input(port)
        assert server._state.x.pointer() == (700, 400), "a VNC viewer moved the agent's pointer"
    finally:
        watch.terminate()
        watch.wait(5)


def test_nothing_reaches_user_display(server, tmp_path):
    """Run while agent apps (a terminal, a D-Bus client, a background job) are alive."""
    _launch_terminal(server)
    server.bash("dbus-send --session --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames")
    server.launch(f"{TERMINAL} -e sh -c 'sleep 30'", wait_for_window=10)
    assert_no_agent_process_on_user_display(server)


def _alive(pid: int) -> bool:
    from x11_mcp.isolation import proc_stat

    stat = proc_stat(pid)
    return stat is not None and stat[3] not in ("Z", "X")


def _pids_with_display(display: str) -> list:
    """Every process whose environment points it at `display`."""
    pids = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                environ = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"DISPLAY={display}".encode() in environ:
                pids.append(int(entry))
    return pids


def _wait_gone(pids, timeout=5.0) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.1)
    return [p for p in pids if _alive(p)]


@pytest.mark.parametrize("ending", ["stdin closed", "SIGKILL"])
def test_stdio_server_cleans_up(ending):
    """The real server over MCP stdio: tools work, and nothing outlives it."""
    import asyncio
    import json
    import signal
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from x11_mcp.desktop import runtime_base

    name = f"stdio-{ending.split()[0].lower()}"
    env = dict(os.environ, X11_MCP_NAME=name)
    params = StdioServerParameters(command=sys.executable, args=["-m", "x11_mcp"], env=env)
    state = {}

    async def session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                assert {"screenshot", "launch", "tile_windows", "wait_until_idle", "type_text"} <= tools
                info = (await s.call_tool("desktop_info", {})).content[0].text
                assert f"desktop: {name} " in info, info
                shot = await s.call_tool("screenshot", {"scale": 0.25})
                assert shot.content[0].type == "image" and "480x270" in shot.content[1].text
                out = (await s.call_tool("launch", {"command": TERMINAL})).content[0].text
                assert "new window" in out, out
                bad = await s.call_tool("view_workspace", {"workspace": 99})
                assert bad.isError
                record = json.loads((runtime_base() / name / "desktop.json").read_text())
                state["server_pid"] = record["pid"]
                state["apps"] = _pids_with_display(record["display"])
                assert len(state["apps"]) >= 3, state["apps"]  # Openbox, D-Bus, the terminal, its shell
                if ending == "SIGKILL":
                    os.kill(state["server_pid"], signal.SIGKILL)

    try:
        asyncio.run(session())
    except Exception:
        if ending != "SIGKILL":
            raise  # the client notices the dead server on the way out
    assert _wait_gone([state["server_pid"]] + state["apps"]) == [], "something outlived the server"
    if ending == "stdin closed":
        assert not (runtime_base() / name).exists()
