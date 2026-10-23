"""MCP tools for driving the agent's private X11 desktop."""
import atexit
import io
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from typing import Literal

try:  # mcp >= 2 renamed FastMCP to MCPServer; the parts used here are unchanged
    from mcp.server.mcpserver import Image, MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer, Image

from PIL import Image as PILImage

from x11_mcp import isolation
from x11_mcp.desktop import AgentDesktop, Config, parse_workspace_count, runtime_base
from x11_mcp.layout import tile
from x11_mcp.x import MODIFIER_KEYSYMS, Window, XClient, forbidden_keysym, keysym_from_name

log = logging.getLogger("x11_mcp")

mcp = MCPServer(
    "x11-desktop",
    instructions=(
        "Controls a private, headless X11 desktop (Xvfb + Openbox) that belongs to you alone. "
        "The user's own session is separate: nothing you do here moves their cursor, takes "
        "their focus or shows up on their screen, and they cannot type into yours. Start apps "
        "with `launch`, look with `screenshot`, act with the mouse and keyboard tools. Windows "
        "float: they keep their size and position until something moves them, and "
        "`tile_windows` arranges them side by side. Workspaces are numbered from 1; a "
        "screenshot shows the current one unless you pass `workspace`. Coordinates are screen "
        "pixels of the screenshot at scale 1. After an action that redraws, `wait_until_idle` "
        "waits for the screen to settle."
    ),
)

_MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "altgr": "altgr",
    "super": "super", "logo": "super", "win": "super", "meta": "super", "cmd": "super",
}
_KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "backspace": "BackSpace", "del": "Delete", "delete": "Delete", "tab": "Tab",
    "space": "space", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "pgup": "Page_Up", "pgdn": "Page_Down", "insert": "Insert", "plus": "plus", "minus": "minus",
    "print": "Print", "menu": "Menu",
}
# Keysym names an X server may act on itself (VT switch, server kill, grab breaking).
_FORBIDDEN_KEYS = ("xf86switch_vt", "terminate_server", "xf86ungrab", "xf86cleargrab",
                   "xf86logwindowtree", "xf86loggrabinfo", "xf86next_vmode", "xf86prev_vmode")
_BUTTONS = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}


class _State:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = Config.from_env()
        self.desktop: AgentDesktop | None = None
        self.x: XClient | None = None

    def ensure(self) -> tuple[AgentDesktop, XClient]:
        with self.lock:
            if self.desktop is not None and not self.desktop.alive():
                log.warning("agent X server died; restarting the desktop")
                self.shutdown()
            if self.desktop is not None and not self.desktop.wm_alive():
                self.desktop.restart_wm(self.x.workspace_count())
            if self.desktop is None:
                desktop = AgentDesktop(self.config)
                try:
                    desktop.start()
                    x = XClient(desktop.display, desktop.xauthority)
                except Exception:
                    desktop.stop()
                    raise
                self.desktop, self.x = desktop, x
            return self.desktop, self.x

    def shutdown(self):
        with self.lock:
            if self.x is not None:
                self.x.close()
            if self.desktop is not None:
                self.desktop.stop()
            self.desktop = self.x = None


_state = _State()


# -- helpers ---------------------------------------------------------------

# Longest image side a vision model takes without downscaling it again.
_ZOOM_TARGET = 1568
_MAX_OUTPUT = 30_000


def _zoom_factor(width: int, height: int) -> float:
    return max(1.0, min(4.0, _ZOOM_TARGET / max(width, height)))


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    """Keep the head and the tail: errors and summaries tend to be at the end."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters cut] ...\n{text[-half:]}"


def _rect(r) -> str:
    return f"({r[0]},{r[1]} {r[2]}x{r[3]})"


def _describe(w: Window) -> str:
    where = "all workspaces" if w.workspace is None else f"workspace {w.workspace + 1}"
    parts = [f"{w.id}: {w.title!r}", f"class={w.wm_class or '?'}"]
    if w.pid:
        parts.append(f"pid={w.pid}")
    if w.type != "normal":
        parts.append(f"type={w.type}")
    if w.transient_for:
        parts.append(f"for={w.transient_for}")
    parts += [where, f"frame={_rect(w.frame)}", f"content={_rect(w.content)}"]
    line = " ".join(parts)
    return line + (f" [{', '.join(w.states)}]" if w.states else "")


def _workspace_summary(x: XClient) -> str:
    current = x.current_workspace()
    windows = x.windows()
    parts = []
    for i in range(x.workspace_count()):
        count = sum(1 for w in windows if w.workspace in (i, None))
        parts.append(f"{i + 1}{'*' if i == current else ''}({count})")
    return " ".join(parts)


def _workspace(x: XClient, number: int) -> int:
    """1-based workspace number from the agent -> 0-based EWMH desktop."""
    count = x.workspace_count()
    if not 1 <= number <= count:
        raise ValueError(f"workspace {number} does not exist; there are {count} (1-{count}), see set_workspace_count")
    return number - 1


def _target(x: XClient, window_id: int | None) -> int:
    wid = window_id or x.active_window()
    if not wid:
        raise ValueError("no window is focused; pass window_id (see list_windows)")
    if wid not in x.client_ids():
        raise ValueError(f"no window {wid}; see list_windows")
    return wid


def _view(x: XClient, workspace: int):
    if x.current_workspace() != workspace:
        x.view_workspace(workspace)
        x.wait_until(lambda: x.current_workspace() == workspace)


def _check_region(x: XClient, rx: int, ry: int, width: int, height: int):
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if rx < 0 or ry < 0 or rx + width > x.width or ry + height > x.height:
        raise ValueError(f"region ({rx}, {ry}) {width}x{height} is not inside the {x.width}x{x.height} screen")


def _encode(img: PILImage.Image, format: str) -> bytes:
    buf = io.BytesIO()
    if format == "jpeg":
        img.save(buf, "JPEG", quality=85)
    else:
        img.save(buf, "PNG", compress_level=3)
    return buf.getvalue()


def _states(x: XClient, wid: int) -> set:
    w = x.window(wid)
    return set(w.states) if w else set()


_SIZED_STATES = {"fullscreen", "maximized", "maximized_vert", "maximized_horz"}


def _unmaximize(x: XClient, wid: int) -> Window | None:
    """Drop fullscreen/maximized so the WM accepts an explicit geometry."""
    w = x.window(wid)
    if w is None or not _SIZED_STATES & set(w.states):
        return w
    if "fullscreen" in w.states:
        x.set_state(wid, "remove", "_NET_WM_STATE_FULLSCREEN")
    if (_SIZED_STATES - {"fullscreen"}) & set(w.states):
        x.set_state(wid, "remove", "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
    x.wait_until(lambda: not _SIZED_STATES & _states(x, wid), 1.0)
    return x.window(wid)


def _await_repaint(watch, quiet: float):
    """After switching the view: let the newly shown windows repaint. Nothing redrawn
    within half a second means there was nothing to repaint (two empty workspaces)."""
    if not watch.wait(quiet, 0.5, need_change=True).idle and watch.changes:
        watch.wait(quiet, 2.5)


# -- desktop ---------------------------------------------------------------


@mcp.tool()
def desktop_info() -> str:
    """Screen size, workspaces (* = shown, (n) = window count), focused window and how the user can watch."""
    desktop, x = _state.ensure()
    active = x.active_window()
    focused = x.window(active) if active else None
    server = "Xvnc" if desktop.config.server == "xvnc" else "Xvfb"
    lines = [
        f"desktop: {desktop.name} (private headless X11 display {desktop.display}, {server} + "
        f"{x.wm_name() or 'no window manager'}; isolated from the user's session)",
        f"screen: {x.width}x{x.height}",
        f"workspaces: {_workspace_summary(x)}",
        f"focused: {focused.id} {focused.title!r} ({focused.wm_class})" if focused else "focused: none",
        f"cursor: {x.pointer()}",
        f"home: {desktop.home}",
        f"the user can watch with: x11-mcp watch {desktop.name}",
    ]
    return "\n".join(lines)


@mcp.tool()
def screenshot(
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    scale: float = 1.0,
    show_cursor: bool = False,
    format: Literal["png", "jpeg"] = "png",
    workspace: int | None = None,
) -> list:
    """Capture the agent desktop, or a region of it (x, y, width, height).

    Use scale < 1 to save tokens; click coordinates are always full-scale screen pixels.
    `workspace` captures another workspace: the view switches there for the capture and
    then back (hidden workspaces have no pixels until shown).
    """
    _, xc = _state.ensure()
    if not 0.1 <= scale <= 2.0:
        raise ValueError("scale must be between 0.1 and 2")
    region = (x, y, width, height)
    if any(v is not None for v in region):
        if any(v is None for v in region):
            raise ValueError("a region needs all of x, y, width and height")
        _check_region(xc, x, y, width, height)
    else:
        x, y, width, height = 0, 0, xc.width, xc.height

    note = ""
    current = xc.current_workspace()
    target = current if workspace is None else _workspace(xc, workspace)
    if target != current:
        active = xc.active_window()
        with xc.damage_watch() as watch:
            _view(xc, target)
            _await_repaint(watch, 0.25)
        img = xc.capture(x, y, width, height, show_cursor)
        with xc.damage_watch() as watch:
            _view(xc, current)
            if active and active in xc.client_ids():
                xc.activate(active)
            _await_repaint(watch, 0.15)
        note = f", workspace {workspace} (view switched back to {current + 1})"
    else:
        img = xc.capture(x, y, width, height, show_cursor)
    if scale != 1.0:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         PILImage.Resampling.LANCZOS)
    data = _encode(img, format)
    return [Image(data=data, format=format),
            f"{img.width}x{img.height} image, origin ({x}, {y}), scale {scale}{note}"]


@mcp.tool()
def zoom(x: int, y: int, width: int, height: int, factor: float | None = None) -> list:
    """Magnify a region (x, y, width, height) of the screen to read small text or icons.

    The image is upscaled by `factor` (default: to about 1568px on the long side, at most 4x).
    Image pixel (px, py) is screen pixel (x + px / factor, y + py / factor).
    """
    _, xc = _state.ensure()
    _check_region(xc, x, y, width, height)
    if factor is None:
        factor = _zoom_factor(width, height)
    elif not 1.0 <= factor <= 4.0:
        raise ValueError("factor must be between 1 and 4")
    factor = round(factor, 3)
    img = xc.capture(x, y, width, height)
    img = img.resize((round(width * factor), round(height * factor)), PILImage.Resampling.LANCZOS)
    return [Image(data=_encode(img, "png"), format="png"),
            f"{img.width}x{img.height} image of ({x}, {y}) {width}x{height}, factor {factor}"]


@mcp.tool()
def bash(command: str, timeout: float = 30, cwd: str | None = None) -> str:
    """Run a shell command inside the agent desktop's environment and return its output.

    For short commands: xclip/xdotool, reading app logs, files under the agent's $HOME.
    It sees the agent desktop's display and D-Bus, not the user's. Use `launch` for
    GUI apps and anything long-running: this waits for the command, and kills it and
    everything it started after `timeout` seconds (max 300). stdin is empty.
    """
    desktop, _ = _state.ensure()
    timeout = max(1.0, min(timeout, 300.0))
    status, out = desktop.shell(command, cwd, timeout)
    text = _truncate(out.decode(errors="replace"))
    head = f"timed out after {timeout:g}s, killed" if status is None else f"exit status {status}"
    return f"{head}\n{text}" if text else head


@mcp.tool()
def launch(command: str, workspace: int | None = None, cwd: str | None = None,
           wait_for_window: float = 10.0) -> str:
    """Run a shell command on the agent desktop (e.g. 'xterm', 'firefox --new-instance').

    The window opens on `workspace` (the view switches there first) or the current one.
    Waits up to `wait_for_window` seconds (max 60) for the new window, focuses it and
    reports it.
    """
    desktop, x = _state.ensure()
    target = None
    if workspace is not None:
        target = _workspace(x, workspace)
        _view(x, target)
    before = set(x.client_ids())
    proc = desktop.launch(command, cwd)
    deadline = time.monotonic() + max(0.0, min(wait_for_window, 60.0))
    found, matched, foreign_since = None, False, None
    while time.monotonic() < deadline:
        new = [w for w in x.client_ids() if w not in before]
        ours = [w for w in new if desktop.sessions.owner(x.window_pid(w) or 0) == proc.pid]
        if ours:
            found, matched = ours[0], True
            break
        # A window from another process: likely a single-instance app handing
        # the launch to a copy that is already running. Give ours a moment.
        if new and foreign_since is None:
            foreign_since = time.monotonic()
        if new and time.monotonic() - foreign_since >= 1.0:
            found = new[0]
            break
        if not new and proc.poll() not in (None, 0):
            break
        time.sleep(0.05)

    lines = [f"started pid {proc.pid}, output logged to {proc.log_path}"]
    if proc.poll() not in (None, 0):
        lines.append(f"exited with status {proc.returncode}")
    if found is None:
        if proc.poll() in (None, 0):
            lines.append("no window yet; check list_windows later")
        return "\n".join(lines)

    info = x.window(found)
    if target is not None and info is not None and info.workspace not in (target, None):
        x.set_window_workspace(found, target)
    x.activate(found)

    def settled():
        w = x.window(found)
        return w is None or (x.active_window() == found and bool(w.title))

    x.wait_until(settled, 1.0)
    info = x.window(found)
    if info is None:
        lines.append(f"window {found} appeared and closed again")
        return "\n".join(lines)
    lines.append("new window " + _describe(info))
    if not matched:
        lines.append("  (not opened by this command's own processes: probably handed to an already running instance)")
    others = [w for w in x.client_ids() if w not in before and w != found]
    lines += ["also new: " + _describe(w) for w in map(x.window, others) if w is not None]
    return "\n".join(lines)


@mcp.tool()
def wait(seconds: float) -> str:
    """Sleep, e.g. to let an app finish loading before a screenshot. Max 30s."""
    time.sleep(max(0.0, min(seconds, 30.0)))
    return "ok"


@mcp.tool()
def wait_until_idle(quiet_ms: int = 500, timeout: float = 10.0, min_area: int = 400) -> str:
    """Wait until nothing on screen has been redrawn for `quiet_ms` (or `timeout` seconds pass, max 60).

    Each redrawn rectangle smaller than `min_area` square pixels (default about 20x20:
    blinking text cursors, small spinners) is ignored; min_area=0 counts every pixel.
    """
    _, x = _state.ensure()
    timeout = max(0.1, min(timeout, 60.0))
    quiet = max(0.05, min(quiet_ms / 1000, timeout))
    with x.damage_watch() as watch:
        result = watch.wait(quiet, timeout, max(0, min_area))
    if result.idle:
        return f"idle: nothing redrawn for {quiet * 1000:.0f} ms (waited {result.waited:.1f}s)"
    where = f"; still changing around {_rect(result.region)}" if result.region else ""
    return f"not idle after {timeout:g}s{where}"


@mcp.tool()
def restart_desktop() -> str:
    """Kill the agent desktop and every app on it, then start a fresh one."""
    _state.shutdown()
    desktop, _ = _state.ensure()
    return f"restarted {desktop.name} on {desktop.display}"


# -- windows ---------------------------------------------------------------


@mcp.tool()
def list_windows(workspace: int | None = None) -> str:
    """All windows (or those on one workspace), topmost first, with ids for the window tools.

    frame = the whole window on screen (title bar and borders included), content = the
    app's own area; both are (x,y widthxheight) in screen pixels.
    """
    _, x = _state.ensure()
    windows = x.windows()
    if workspace is not None:
        ws = _workspace(x, workspace)
        windows = [w for w in windows if w.workspace in (ws, None)]
    if not windows:
        return "no windows"
    head = f"topmost first; workspace {x.current_workspace() + 1} is shown"
    return "\n".join([head] + [_describe(w) for w in windows])


@mcp.tool()
def focus_window(window_id: int) -> str:
    """Focus and raise a window (switching to its workspace and unminimizing it if needed)."""
    _, x = _state.ensure()
    wid = _target(x, window_id)
    x.activate(wid)
    ok = x.wait_until(lambda: x.active_window() == wid)
    head = f"focused {wid}" if ok else f"asked to focus {wid}, but the focus is on {x.active_window() or 'nothing'}"
    return f"{head}; workspaces: {_workspace_summary(x)}"


@mcp.tool()
def close_window(window_id: int) -> str:
    """Ask a window to close, as its close button would. The app may ask to confirm."""
    _, x = _state.ensure()
    wid = _target(x, window_id)
    x.close_window(wid)
    if x.wait_until(lambda: wid not in x.client_ids(), 1.0):
        return f"closed {wid}"
    return f"asked {wid} to close; it is still open (maybe a confirmation dialog, see screenshot)"


def _frames(x: XClient, wids) -> list:
    return [getattr(x.window(w), "frame", None) for w in wids]


def _settle(x: XClient, wids, wanted, before, timeout: float = 1.0):
    """Wait until the WM has applied new geometry: the frames match `wanted`, or they
    changed from `before` and then held still (apps with size steps never match)."""
    deadline = time.monotonic() + timeout
    last, since = None, time.monotonic()
    while time.monotonic() < deadline:
        frames = _frames(x, wids)
        if frames == wanted:
            return
        if frames != last:
            last, since = frames, time.monotonic()
        elif time.monotonic() - since >= (0.15 if frames != before else 0.4):
            return
        time.sleep(0.02)


def _exited(pid: int) -> bool:
    stat = isolation.proc_stat(pid)
    return stat is None or stat[3] in ("Z", "X")


@mcp.tool()
def window_action(action: Literal["toggle_fullscreen", "toggle_maximize", "kill"],
                  window_id: int | None = None) -> str:
    """Apply an action to a window (default: the focused one).

    kill disconnects the window's app from the display (XKillClient), which ends most
    apps immediately, and kills its process if that survives; use close_window first.
    """
    desktop, x = _state.ensure()
    wid = _target(x, window_id)
    if action == "kill":
        pid = x.window_pid(wid)
        ours = bool(pid) and desktop.sessions.owner(pid) is not None  # before it exits
        x.kill_client(wid)
        gone = x.wait_until(lambda: wid not in x.client_ids(), 2.0)
        if ours and not x.wait_until(lambda: _exited(pid), 1.0):
            os.kill(pid, signal.SIGKILL)
            return f"killed {wid}; its process {pid} survived the disconnect and was SIGKILLed"
        return f"killed {wid}" if gone else f"killed the client of {wid}, but the window is still listed"
    flag = "fullscreen" if action == "toggle_fullscreen" else "maximized"
    was = flag in _states(x, wid)
    if flag == "fullscreen":
        x.set_state(wid, "toggle", "_NET_WM_STATE_FULLSCREEN")
    else:
        x.set_state(wid, "remove" if was else "add", "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
    x.wait_until(lambda: (flag in _states(x, wid)) != was, 1.0)
    after = x.window(wid)
    return f"{action} done: " + _describe(after) if after else f"{action} done"


@mcp.tool()
def move_resize_window(window_id: int, x: int, y: int, width: int, height: int) -> str:
    """Place a window's frame (title bar and borders included) at (x, y) with this outer size.

    Apps with size steps (terminals snap to character cells) or limits may end up a
    little smaller; the result reports the actual geometry.
    """
    _, xc = _state.ensure()
    wid = _target(xc, window_id)
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    w = _unmaximize(xc, wid)
    if w is None:
        raise ValueError(f"window {wid} is gone")
    xc.move_resize(wid, (x, y, width, height), w.extents)
    _settle(xc, [wid], [(x, y, width, height)], [w.frame])
    after = xc.window(wid)
    return "now " + _describe(after) if after else f"window {wid} is gone"


@mcp.tool()
def tile_windows(workspace: int | None = None, layout: Literal["columns", "rows", "grid"] = "columns") -> str:
    """Arrange the normal windows of a workspace (default: the shown one) side by side.

    columns: next to each other; rows: stacked; grid: rows of up to ceil(sqrt(n)).
    Windows go in the order they were opened. Dialogs and minimized windows are left alone.
    """
    _, x = _state.ensure()
    ws = x.current_workspace() if workspace is None else _workspace(x, workspace)
    order = {wid: i for i, wid in enumerate(x.client_ids())}
    windows = [w for w in x.windows()
               if w.workspace == ws and w.type == "normal" and not w.transient_for and "minimized" not in w.states]
    windows.sort(key=lambda w: order.get(w.id, 0))
    if not windows:
        return f"no windows to tile on workspace {ws + 1}"
    rects = tile(len(windows), x.workarea(ws), layout)
    wids = [w.id for w in windows]
    before = []
    for w, rect in zip(windows, rects):
        w = _unmaximize(x, w.id) or w
        before.append(w.frame)
        x.move_resize(w.id, rect, w.extents)
    _settle(x, wids, rects, before)
    lines = [f"tiled {len(windows)} windows on workspace {ws + 1} as {layout}"]
    lines += [_describe(w) for w in (x.window(w.id) for w in windows) if w is not None]
    return "\n".join(lines)


# -- workspaces ------------------------------------------------------------


@mcp.tool()
def view_workspace(workspace: int) -> str:
    """Show a workspace (numbered from 1). Windows on other workspaces are hidden, not closed."""
    _, x = _state.ensure()
    _view(x, _workspace(x, workspace))
    return f"workspaces: {_workspace_summary(x)}"


@mcp.tool()
def move_window_to_workspace(workspace: int, window_id: int | None = None) -> str:
    """Move a window (default: the focused one) to another workspace. The view stays where it is."""
    _, x = _state.ensure()
    ws = _workspace(x, workspace)
    wid = _target(x, window_id)
    x.set_window_workspace(wid, ws)
    x.wait_until(lambda: getattr(x.window(wid), "workspace", ws) == ws, 1.0)
    return f"moved {wid} to workspace {workspace}; workspaces: {_workspace_summary(x)}"


@mcp.tool()
def set_workspace_count(count: int) -> str:
    """Change how many workspaces there are (1-32). Windows on removed ones move to the last remaining."""
    _, x = _state.ensure()
    count = parse_workspace_count(count)
    x.set_workspace_count(count)
    x.wait_until(lambda: x.workspace_count() == count)
    return f"workspaces: {_workspace_summary(x)}"


# -- pointer ---------------------------------------------------------------


@mcp.tool()
def mouse_move(x: int, y: int) -> str:
    """Move the agent's pointer to screen pixel (x, y)."""
    _, xc = _state.ensure()
    xc.move_pointer(x, y)
    return f"pointer at {xc.pointer()}"


@mcp.tool()
def click(
    x: int | None = None,
    y: int | None = None,
    button: Literal["left", "right", "middle", "back", "forward"] = "left",
    count: int = 1,
) -> str:
    """Click at (x, y), or where the pointer is. count=2 double-clicks, 3 triple-clicks."""
    _, xc = _state.ensure()
    if (x is None) != (y is None):
        raise ValueError("give both x and y, or neither")
    if x is not None:
        xc.move_pointer(x, y)
        time.sleep(0.02)
    count = max(1, min(count, 3))
    for i in range(count):
        xc.button(_BUTTONS[button], True)
        time.sleep(0.02)
        xc.button(_BUTTONS[button], False)
        if i + 1 < count:
            time.sleep(0.06)
    return f"{button} x{count} at {xc.pointer()}"


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, button: Literal["left", "right", "middle"] = "left") -> str:
    """Press at (x1, y1), move to (x2, y2) in steps, release."""
    _, xc = _state.ensure()
    xc.move_pointer(x1, y1)
    time.sleep(0.02)
    xc.button(_BUTTONS[button], True)
    try:
        steps = 12
        for i in range(1, steps + 1):
            xc.move_pointer(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
            time.sleep(0.015)
    finally:
        xc.button(_BUTTONS[button], False)
    return f"dragged ({x1}, {y1}) -> ({x2}, {y2})"


@mcp.tool()
def scroll(x: int | None = None, y: int | None = None, down: int = 0, right: int = 0) -> str:
    """Scroll by wheel notches at (x, y) or the pointer. Negative down = up, negative right = left."""
    _, xc = _state.ensure()
    if x is not None and y is not None:
        xc.move_pointer(x, y)
    down, right = max(-50, min(down, 50)), max(-50, min(right, 50))
    for amount, positive, negative in ((down, 5, 4), (right, 7, 6)):
        for _ in range(abs(amount)):
            button = positive if amount > 0 else negative
            xc.button(button, True)
            xc.button(button, False)
            time.sleep(0.01)
    return f"scrolled down={down} right={right} at {xc.pointer()}"


# -- keyboard --------------------------------------------------------------


def _parse_combo(combo: str) -> tuple[list[str], int]:
    """'ctrl+shift+t' -> (['ctrl', 'shift'], keysym of t)."""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    *mods, key = parts
    names = []
    for mod in mods:
        if mod.lower() not in _MODIFIERS:
            raise ValueError(f"unknown modifier {mod!r}; use ctrl, shift, alt, altgr, super")
        if _MODIFIERS[mod.lower()] not in names:
            names.append(_MODIFIERS[mod.lower()])
    key = _KEY_ALIASES.get(key.lower(), key)
    if key.lower() in _MODIFIERS:  # a modifier pressed on its own, e.g. 'super' or 'ctrl+shift'
        key = MODIFIER_KEYSYMS[_MODIFIERS[key.lower()]]
    if key.lower().startswith(_FORBIDDEN_KEYS):
        raise ValueError(f"{key} is reserved for the X server")
    keysym = keysym_from_name(key)
    if not keysym:
        raise ValueError(f"unknown key {key!r}; use X keysym names such as Return, Tab, F5, a, XF86AudioMute")
    if forbidden_keysym(keysym):
        raise ValueError(f"{key} is reserved for the X server")
    return names, keysym


@mcp.tool()
def type_text(text: str, delay_ms: int = 0) -> str:
    """Type text into the focused window. Any Unicode works, independent of keyboard layout.

    Newlines press Return and tabs press Tab.
    """
    _, x = _state.ensure()
    if len(text) > 20_000:
        raise ValueError("at most 20000 characters per call")
    x.type_text(text, max(0, min(delay_ms, 500)) / 1000)
    return f"typed {len(text)} characters"


@mcp.tool()
def press_keys(keys: str, repeat: int = 1) -> str:
    """Press key combos, space-separated: 'ctrl+l', 'Return', 'ctrl+shift+t alt+F4'.

    Key names are X keysyms (Return, Tab, Escape, BackSpace, Up, F5, a, 1, ...).
    """
    _, x = _state.ensure()
    combos = [_parse_combo(c) for c in keys.split()]
    if not combos:
        raise ValueError("no keys given")
    for _ in range(max(1, min(repeat, 50))):
        for mods, keysym in combos:
            x.press_combo(mods, keysym)
            time.sleep(0.01)
    return f"pressed {keys}" + (f" x{repeat}" if repeat > 1 else "")


# -- entry point -----------------------------------------------------------


def _desktop_record(name: str) -> dict | None:
    """What a running server wrote about its desktop `name`, if it is running."""
    base = runtime_base()
    record = base / name / "desktop.json"
    if not isolation.name_in_use(base, name) or not record.exists():
        return None
    return json.loads(record.read_text())


def _env_hint(name: str) -> str:
    """Exports that point a shell at a running agent desktop."""
    record = _desktop_record(name)
    if record is None:
        return f"# no running agent desktop named {name!r}"
    return f"export DISPLAY={record['display']!r}\nexport XAUTHORITY={record['xauthority']!r}"


def _watch(name: str) -> int:
    record = _desktop_record(name)
    if record is None:
        print(f"no running agent desktop named {name!r}", file=sys.stderr)
        return 1
    if record["server"] == "xvnc":
        print(f"{name} runs on Xvnc, which serves VNC itself: point a viewer at localhost:{record['vnc_port']}.\n"
              "It is view-only: viewers cannot type, click, resize it or exchange the clipboard.")
        return 0
    x11vnc = shutil.which("x11vnc")
    if not x11vnc:
        print("x11vnc is not installed (see `x11-mcp check`); or run the server with X11_MCP_SERVER=xvnc",
              file=sys.stderr)
        return 1
    print(f"Watching {name} ({record['display']}) view-only on localhost; connect a VNC viewer to the "
          "PORT x11vnc prints below. Ctrl-C stops it.", flush=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/"),
           "DISPLAY": record["display"], "XAUTHORITY": record["xauthority"]}
    argv = [x11vnc, "-display", record["display"], "-auth", record["xauthority"], "-viewonly", "-localhost",
            "-shared", "-forever", "-nopw", "-norc", "-nosel", "-quiet"]
    os.execve(x11vnc, argv, env)
    return 1  # not reached


_USAGE = """usage: x11-mcp              run the MCP server on stdio
       x11-mcp check        report missing dependencies
       x11-mcp env [NAME]   print exports pointing a shell at a running agent desktop
       x11-mcp watch [NAME] view a running agent desktop live over VNC (view-only)"""


def main():
    args = sys.argv[1:]
    command = args[0] if args else None
    if command == "check":
        from x11_mcp.check import run

        sys.exit(run())
    if command in ("-h", "--help", "help"):
        print(_USAGE)
        return
    if command in ("env", "watch"):
        name = args[1] if len(args) > 1 else os.environ.get("X11_MCP_NAME", "agent")
        if command == "env":
            print(_env_hint(name))
            return
        sys.exit(_watch(name))
    if command is not None:
        print(_USAGE, file=sys.stderr)
        sys.exit(2)

    log_dir = isolation.state_home() / "x11-mcp"
    log_dir.mkdir(parents=True, exist_ok=True)
    # force: constructing the MCP server already pointed the root logger at stderr.
    logging.basicConfig(
        filename=log_dir / "server.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
    )
    atexit.register(_state.shutdown)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    try:
        mcp.run()
    finally:
        _state.shutdown()
