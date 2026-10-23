"""Lifecycle and isolation of the agent's headless X11 desktop.

Everything the agent touches lives behind these walls:

* its own X server (Xvfb, or Xvnc with viewer input disabled) with no input
  devices, so the user's keyboard and mouse never reach it, guarded by a
  per-desktop MIT-MAGIC-COOKIE so no other local client can connect to it;
* its own XDG_RUNTIME_DIR, so the user's Wayland socket, PipeWire, systemd and
  D-Bus sockets are not where agent programs look for them;
* its own D-Bus session bus, so single-instance apps cannot hand off to the
  user's copy and notifications do not land on the user's screen;
* (by default) its own $HOME, so per-profile singletons such as Chromium's and
  Electron's cannot forward a launch into the user's running instance.

Every child process gets an environment built here, never the server's own.
What keeps agent apps off the user's display is that environment: the
user's own X server may well accept any local client.
"""
import json
import logging
import os
import secrets
import select
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from x11_mcp import isolation
from x11_mcp.isolation import NEVER_PASS, NameLock, Sessions

log = logging.getLogger(__name__)

SERVERS = ("xvfb", "xvnc")
_MAX_WORKSPACES = 32


def runtime_base() -> Path:
    return isolation.user_runtime_dir() / "x11-mcp"


def server_binary(kind: str) -> str:
    """Xvfb, or TigerVNC's Xvnc (Debian installs it as Xtigervnc)."""
    names = {"xvfb": ("Xvfb",), "xvnc": ("Xvnc", "Xtigervnc")}[kind]
    for name in names:
        if shutil.which(name):
            return shutil.which(name)
    return names[0]


def wm_binary() -> str:
    return os.environ.get("X11_MCP_OPENBOX") or shutil.which("openbox") or "openbox"


@dataclass
class Config:
    name: str = "agent"
    width: int = 1920
    height: int = 1080
    server: str = "xvfb"
    workspaces: int = 4
    share_home: bool = False
    pass_env: tuple = ()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        cfg = cls()
        cfg.name = env.get("X11_MCP_NAME", cfg.name)
        if not cfg.name or not cfg.name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("X11_MCP_NAME may contain only letters, digits, - and _")
        if env.get("X11_MCP_RESOLUTION"):
            try:
                w, h = env["X11_MCP_RESOLUTION"].lower().split("x")
                cfg.width, cfg.height = int(w), int(h)
            except ValueError:
                raise ValueError("X11_MCP_RESOLUTION must look like 1920x1080") from None
            if not (320 <= cfg.width <= 8192 and 200 <= cfg.height <= 8192):
                raise ValueError("X11_MCP_RESOLUTION must be between 320x200 and 8192x8192")
        cfg.server = env.get("X11_MCP_SERVER", cfg.server).lower() or cfg.server
        if cfg.server not in SERVERS:
            raise ValueError(f"X11_MCP_SERVER must be one of {', '.join(SERVERS)}")
        if env.get("X11_MCP_WORKSPACES"):
            cfg.workspaces = parse_workspace_count(env["X11_MCP_WORKSPACES"])
        cfg.share_home = env.get("X11_MCP_SHARE_HOME") == "1"
        cfg.pass_env = tuple(v.strip() for v in env.get("X11_MCP_PASS_ENV", "").split(",") if v.strip())
        refused = sorted(set(cfg.pass_env) & NEVER_PASS)
        if refused:
            raise ValueError(f"X11_MCP_PASS_ENV cannot pass {', '.join(refused)}: they point at your session")
        return cfg


def parse_workspace_count(value) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"workspace count must be a number, got {value!r}") from None
    if not 1 <= count <= _MAX_WORKSPACES:
        raise ValueError(f"workspace count must be between 1 and {_MAX_WORKSPACES}, got {count}")
    return count


def write_xauthority(path: Path, cookie: bytes):
    """An Xauthority file whose cookie matches any display number.

    Two entries: FamilyLocal with this host's name (what python-xlib looks
    for) and FamilyWild (which libXau, i.e. every app, also accepts). The
    display number is left empty because -displayfd picks it after the file
    must exist; the X server itself loads every cookie in the file.
    """
    def field(data: bytes) -> bytes:
        return struct.pack(">H", len(data)) + data

    entries = b""
    for family, address in ((256, socket.gethostname().encode()), (0xFFFF, b"")):
        entries += (struct.pack(">H", family) + field(address) + field(b"")
                    + field(b"MIT-MAGIC-COOKIE-1") + field(cookie))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(entries)


def openbox_config(workspaces: int, menu_file: Path) -> str:
    """A private rc.xml: no key bindings, no autostart, no root menu.

    Mouse bindings are limited to what a window's own frame does on any
    desktop (click to focus and raise, drag the title bar to move, drag an
    edge to resize, the title bar buttons), so the agent can work with windows
    the way it has seen in screenshots. Nothing uses a modifier, so no app
    shortcut is intercepted, and the root window does nothing.
    """
    focus = '<action name="Focus"/><action name="Raise"/>'
    edges = "".join(
        f'<context name="{edge}"><mousebind button="Left" action="Press">{focus}</mousebind>'
        f'<mousebind button="Left" action="Drag"><action name="Resize"/></mousebind></context>'
        for edge in ("Top", "Bottom", "Left", "Right", "TLCorner", "TRCorner", "BLCorner", "BRCorner")
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by x11-mcp for a private agent desktop. -->
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <resistance><strength>0</strength><screen_edge_strength>0</screen_edge_strength></resistance>
  <focus>
    <focusNew>yes</focusNew>
    <followMouse>no</followMouse>
    <focusLast>yes</focusLast>
    <underMouse>no</underMouse>
    <raiseOnFocus>no</raiseOnFocus>
  </focus>
  <placement><policy>Smart</policy><center>yes</center><monitor>Primary</monitor></placement>
  <theme>
    <name>Clearlooks</name>
    <titleLayout>NLIMC</titleLayout>
    <keepBorder>yes</keepBorder>
    <animateIconify>no</animateIconify>
  </theme>
  <desktops><number>{workspaces}</number><firstdesk>1</firstdesk><names/><popupTime>0</popupTime></desktops>
  <resize><drawContents>yes</drawContents><popupShow>Never</popupShow></resize>
  <margins><top>0</top><bottom>0</bottom><left>0</left><right>0</right></margins>
  <menu><file>{escape(str(menu_file))}</file></menu>
  <applications/>
  <keyboard/>
  <mouse>
    <dragThreshold>1</dragThreshold>
    <doubleClickTime>500</doubleClickTime>
    <screenEdgeWarpTime>0</screenEdgeWarpTime>
    <context name="Client">
      <mousebind button="Left" action="Press">{focus}</mousebind>
      <mousebind button="Middle" action="Press">{focus}</mousebind>
      <mousebind button="Right" action="Press">{focus}</mousebind>
    </context>
    <context name="Titlebar">
      <mousebind button="Left" action="Press">{focus}</mousebind>
      <mousebind button="Left" action="Drag"><action name="Move"/></mousebind>
      <mousebind button="Left" action="DoubleClick"><action name="ToggleMaximize"/></mousebind>
    </context>
    <context name="Icon"><mousebind button="Left" action="Press">{focus}</mousebind></context>
    {edges}
    <context name="Close">
      <mousebind button="Left" action="Press">{focus}</mousebind>
      <mousebind button="Left" action="Click"><action name="Close"/></mousebind>
    </context>
    <context name="Maximize">
      <mousebind button="Left" action="Press">{focus}</mousebind>
      <mousebind button="Left" action="Click"><action name="ToggleMaximize"/></mousebind>
    </context>
    <context name="Iconify">
      <mousebind button="Left" action="Press">{focus}</mousebind>
      <mousebind button="Left" action="Click"><action name="Iconify"/></mousebind>
    </context>
  </mouse>
</openbox_config>
"""


class AgentDesktop:
    def __init__(self, config: Config):
        self.config = config
        self.base = runtime_base()
        self.data_dir = isolation.data_home() / "x11-mcp"
        self.name = None
        self.runtime: Path | None = None
        self.home: Path | None = None
        self.display = ""
        self.xauthority: Path | None = None
        self.vnc_port: int | None = None
        self._lock = NameLock(self.base)
        self.sessions = Sessions()
        self._xserver = None
        self._dbus = None
        self._wm = None
        self._apps: list[subprocess.Popen] = []

    # -- environment ------------------------------------------------------

    def _base_env(self) -> dict:
        env = isolation.scrub_env(os.environ, self.config.pass_env, self.config.share_home)
        env["HOME"] = str(self.home)
        env["XDG_RUNTIME_DIR"] = str(self.runtime)
        env.setdefault("LANG", "C.UTF-8")
        return env

    def _x_env(self) -> dict:
        env = self._base_env()
        env.update(DISPLAY=self.display, XAUTHORITY=str(self.xauthority))
        return env

    def app_env(self) -> dict:
        """Environment for anything the agent runs: points only at the agent desktop."""
        env = self._x_env()
        env.update(
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={self.runtime / 'bus'}",
            XDG_SESSION_TYPE="x11",
            XDG_CURRENT_DESKTOP="Openbox",
            GDK_BACKEND="x11",
            QT_QPA_PLATFORM="xcb",
            SDL_VIDEODRIVER="x11",
            CLUTTER_BACKEND="x11",
            MOZ_ENABLE_WAYLAND="0",
            ELECTRON_OZONE_PLATFORM_HINT="x11",
        )
        return env

    # -- lifecycle --------------------------------------------------------

    def start(self):
        cfg = self.config
        self.name = self._lock.claim(cfg.name)
        self.runtime = self.base / self.name
        if self.runtime.exists():
            shutil.rmtree(self.runtime)  # stale: we hold the lock, so nobody uses it
        self.runtime.mkdir(mode=0o700)
        if self.runtime.resolve() == isolation.user_runtime_dir().resolve():
            raise RuntimeError("refusing to share the user's runtime directory")
        self.home = Path.home() if cfg.share_home else self.data_dir / self.name / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        if not cfg.share_home:
            self._seed_home()

        self.xauthority = self.runtime / "Xauthority"
        write_xauthority(self.xauthority, secrets.token_bytes(16))
        self._start_xserver()
        self._start_dbus()
        self._start_wm()
        (self.runtime / "desktop.json").write_text(json.dumps({
            "display": self.display, "xauthority": str(self.xauthority), "server": cfg.server,
            "vnc_port": self.vnc_port, "pid": os.getpid(),
        }))
        log.info("agent desktop %s up on %s (%s)", self.name, self.display, cfg.server)

    def _seed_home(self):
        """Keep first-run wizards (zsh-newuser-install) from eating the agent's input."""
        zshrc = self.home / ".zshrc"
        if not zshrc.exists():
            zshrc.write_text("# x11-mcp agent home\nPROMPT='%~ %# '\n")

    def _spawn_tied(self, argv, **kwargs) -> subprocess.Popen:
        since = isolation.boot_ticks()
        proc = isolation.popen_tied_to_server(argv, **kwargs)
        self.sessions.add(proc.pid, since)
        return proc

    def _start_xserver(self):
        cfg = self.config
        binary = server_binary(cfg.server)
        if cfg.server == "xvfb":
            argv = [binary, "-screen", "0", f"{cfg.width}x{cfg.height}x24"]
        else:
            # A view-only VNC server on localhost: viewers cannot type, click,
            # resize the desktop or exchange clipboards with the agent.
            argv = [binary, "-geometry", f"{cfg.width}x{cfg.height}", "-depth", "24",
                    "-SecurityTypes", "None", "-localhost",
                    "-AcceptKeyEvents=0", "-AcceptPointerEvents=0", "-AcceptSetDesktopSize=0",
                    "-AcceptCutText=0", "-SendCutText=0", "-SendPrimary=0", "-SetPrimary=0"]
        read_fd, write_fd = os.pipe()
        argv += ["-displayfd", str(write_fd), "-nolisten", "tcp", "-auth", str(self.xauthority),
                 "-noreset", "-dpi", "96"]
        try:
            with open(self.runtime / "xserver.log", "wb") as log_file:
                self._xserver = self._spawn_tied(
                    argv, env=self._base_env(), pass_fds=(write_fd,),
                    stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                )
            os.close(write_fd)
            write_fd = None
            number = self._read_display_number(read_fd)
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        self.display = f":{number}"
        if cfg.server == "xvnc":
            self.vnc_port = 5900 + number

    def _read_display_number(self, fd, timeout=15.0) -> int:
        data = b""
        deadline = time.monotonic() + timeout
        while not data.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"timed out waiting for the X server; see {self.runtime / 'xserver.log'}")
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.2))
            if ready:
                chunk = os.read(fd, 64)
                if not chunk:
                    break
                data += chunk
            elif self._xserver.poll() is not None:
                break
        if not data.strip().isdigit():
            raise RuntimeError(
                f"X server failed to start (status {self._xserver.poll()}); see {self.runtime / 'xserver.log'}"
            )
        return int(data.strip())

    def _start_dbus(self):
        bus = self.runtime / "bus"
        env = self.app_env()  # activated services inherit it, so they land on the agent display too
        with open(self.runtime / "dbus.log", "wb") as log_file:
            self._dbus = self._spawn_tied(
                ["dbus-daemon", "--session", "--nofork", "--nopidfile", f"--address=unix:path={bus}"],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_file,
            )
        isolation.wait_for(bus.exists, 10, "agent D-Bus", self._dbus)

    def _start_wm(self, workspaces: int | None = None):
        from x11_mcp.x import open_display, wm_name, workspace_count

        workspaces = workspaces or self.config.workspaces
        rc = self.runtime / "openbox-rc.xml"
        menu = self.runtime / "openbox-menu.xml"
        menu.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<openbox_menu xmlns="http://openbox.org/3.4/menu"/>\n')
        rc.write_text(openbox_config(workspaces, menu))
        binary = wm_binary()
        env = self._x_env()  # no D-Bus: Openbox has no use for it
        # Openbox finds its themes under XDG_DATA_DIRS; add its own share/ in
        # case it lives outside the standard ones (Nix, /opt, a local build).
        resolved = shutil.which(binary)
        if resolved:
            share = Path(resolved).resolve().parent.parent / "share"
            dirs = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
            env["XDG_DATA_DIRS"] = f"{dirs}:{share}"
        with open(self.runtime / "openbox.log", "wb") as log_file:
            self._wm = self._spawn_tied(
                [binary, "--config-file", str(rc), "--sm-disable"],
                env=env, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            )
        conn = open_display(self.display, self.xauthority)
        try:
            # Openbox announces itself before it has set up the desktops.
            isolation.wait_for(lambda: wm_name(conn) is not None and workspace_count(conn) == workspaces,
                               15, "Openbox", self._wm)
        finally:
            conn.close()

    def alive(self) -> bool:
        return self._xserver is not None and self._xserver.poll() is None

    def wm_alive(self) -> bool:
        return self._wm is not None and self._wm.poll() is None

    def restart_wm(self, workspaces: int | None = None):
        """Start Openbox again after it died, keeping the workspace count the agent last set."""
        log.warning("Openbox exited with status %s; restarting it", self._wm and self._wm.poll())
        self._start_wm(workspaces)

    # -- agent processes --------------------------------------------------

    def launch(self, command: str, cwd: str | None = None) -> subprocess.Popen:
        if not self.alive():
            raise RuntimeError("agent X server is not running")
        self._apps = [p for p in self._apps if p.poll() is None]
        proc = isolation.spawn(command, self.app_env(), cwd or str(self.home), self.runtime / "logs", self.sessions)
        self._apps.append(proc)
        return proc

    def shell(self, command: str, cwd: str | None = None, timeout: float = 30) -> tuple[int | None, bytes]:
        if not self.alive():
            raise RuntimeError("agent X server is not running")
        return isolation.run_shell(command, self.app_env(), cwd or str(self.home), timeout, self.sessions)

    def stop(self):
        """Kill every process the agent started, then the WM, the bus and the X server."""
        servers = [p for p in (self._wm, self._dbus, self._xserver) if p is not None]
        self.sessions.kill_all(children=self._apps + servers)
        self._apps.clear()
        self._xserver = self._dbus = self._wm = None
        if self.runtime is not None and self.runtime.exists():
            shutil.rmtree(self.runtime, ignore_errors=True)
        self._lock.release()
