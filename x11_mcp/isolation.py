"""Backend-independent isolation: environment, private HOME, name locks, processes.

Everything the agent runs gets an environment built here from scratch, never
the server's own, so nothing it starts can find the user's display, compositor,
D-Bus session, terminal multiplexer, input method or session manager through an
inherited variable.
"""
import ctypes
import fcntl
import logging
import os
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

# Variables that point a process at the user's session, or at a tool running in
# it (terminal multiplexers, editors, keyring and ssh agents, input methods,
# the X session manager). Never inherited.
_STRIP_EXACT = {
    "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "XAUTHORITY", "XAUTHLOCALHOSTNAME", "WINDOWPATH",
    "DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID",
    "XDG_RUNTIME_DIR", "XDG_SEAT", "XDG_VTNR", "XDG_ACTIVATION_TOKEN", "XDG_CURRENT_DESKTOP",
    "DESKTOP_STARTUP_ID", "DESKTOP_SESSION", "GDMSESSION",
    "SESSION_MANAGER", "ICEAUTHORITY",
    "SWAYSOCK", "I3SOCK", "NIRI_SOCKET", "HYPRLAND_INSTANCE_SIGNATURE", "HYPRLAND_CMD",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO", "GNOME_KEYRING_CONTROL",
    "GTK_IM_MODULE", "QT_IM_MODULE", "SDL_IM_MODULE", "GLFW_IM_MODULE", "XMODIFIERS",
    "PULSE_SERVER", "PULSE_COOKIE", "PIPEWIRE_REMOTE",
    # Toolkit backend choices made for the user's (Wayland) session; app_env sets X11 ones.
    "GDK_BACKEND", "QT_QPA_PLATFORM", "SDL_VIDEODRIVER", "CLUTTER_BACKEND", "MOZ_ENABLE_WAYLAND",
    "NIXOS_OZONE_WL", "ELECTRON_OZONE_PLATFORM_HINT",
    "TMUX", "TMUX_PANE", "STY", "NVIM", "NVIM_LISTEN_ADDRESS",
    "TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERMINFO",
    "ALACRITTY_SOCKET", "ALACRITTY_WINDOW_ID", "WINDOWID",
    "PWD", "OLDPWD", "SHLVL", "_", "PS1", "PS2", "PROMPT_COMMAND",
}
_STRIP_PREFIXES = (
    "XDG_SESSION_", "WAYLAND_", "KITTY_", "WEZTERM_", "ZELLIJ", "VSCODE_", "GIO_LAUNCHED_",
    "GNOME_", "KDE_", "IBUS_", "QT_WAYLAND_", "MANGO", "WLR_", "X11_MCP_", "CLAUDE", "MCP_",
)
# Can never be passed through with X11_MCP_PASS_ENV: they are the isolation.
NEVER_PASS = frozenset({
    "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "SESSION_MANAGER",
})
# Re-derived from HOME when HOME is isolated.
_HOME_DERIVED = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def scrub_env(environ, pass_env=(), share_home: bool = False) -> dict:
    """The parts of `environ` safe to hand to agent processes."""
    env = {}
    for key, value in environ.items():
        if key in pass_env and key not in NEVER_PASS:
            env[key] = value
        elif key in _STRIP_EXACT or key.startswith(_STRIP_PREFIXES):
            continue
        elif key in _HOME_DERIVED and not share_home:
            continue
        else:
            env[key] = value
    return env


# -- directories -------------------------------------------------------------


def user_runtime_dir() -> Path:
    """The user's XDG_RUNTIME_DIR, or /run/user/UID, or a private /tmp dir (CI, containers)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime)
    default = Path(f"/run/user/{os.getuid()}")
    if default.is_dir() and os.access(default, os.W_OK):
        return default
    fallback = Path(os.environ.get("TMPDIR") or "/tmp") / f"x11-mcp-runtime-{os.getuid()}"
    try:
        fallback.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = fallback.lstat()
    if not fallback.is_dir() or fallback.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(f"{fallback} is not a private directory owned by you")
    return fallback


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


# -- desktop names -----------------------------------------------------------


class NameLock:
    """An exclusive lock on a desktop name, so two servers never share one desktop."""

    def __init__(self, base: Path):
        self.base = base
        self.name = None
        self._fd = None

    def claim(self, wanted: str) -> str:
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.base, 0o700)
        for n in range(1, 100):
            name = wanted if n == 1 else f"{wanted}-{n}"
            fd = os.open(self.base / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            self._fd, self.name = fd, name
            self._sweep_stale()
            return name
        raise RuntimeError("could not claim an agent desktop name")

    def _sweep_stale(self):
        """Remove runtime dirs left by servers that were killed outright."""
        for entry in self.base.iterdir():
            if not entry.is_dir() or entry.name == self.name:
                continue
            fd = os.open(self.base / f"{entry.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # in use
            else:
                shutil.rmtree(entry, ignore_errors=True)
            finally:
                os.close(fd)

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def name_in_use(base: Path, name: str) -> bool:
    """Whether a server currently holds the lock on `name`."""
    try:
        fd = os.open(base / f"{name}.lock", os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        return False
    finally:
        os.close(fd)


# -- processes ---------------------------------------------------------------

_PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """preexec_fn: take the X server, WM and bus down if the MCP server is killed."""
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


# PR_SET_PDEATHSIG fires when the spawning *thread* exits, not the process. Long-
# lived children are therefore always started from this one worker, which lives
# until interpreter shutdown, whatever thread the MCP framework calls tools on.
_spawner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x11-mcp-spawn")


def popen_tied_to_server(argv, **kwargs) -> subprocess.Popen:
    return _spawner.submit(
        subprocess.Popen, argv, start_new_session=True, preexec_fn=_die_with_parent, **kwargs
    ).result()


def wait_for(predicate, timeout, what, proc=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"{what}: process exited with status {proc.returncode}")
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {what}")


_CLK_TCK = os.sysconf("SC_CLK_TCK")


def boot_ticks() -> int:
    """Now, in the unit and clock of /proc/PID/stat's starttime."""
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * _CLK_TCK)


def parse_stat(text: str) -> tuple[int, int, int, str]:
    """(ppid, session, starttime, state) from the contents of /proc/PID/stat."""
    # comm may contain spaces and parentheses; it ends at the last ')'.
    fields = text[text.rindex(")") + 2:].split()
    return int(fields[1]), int(fields[3]), int(fields[19]), fields[0]


def proc_stat(pid: int):
    try:
        return parse_stat(Path(f"/proc/{pid}/stat").read_text())
    except (OSError, ValueError, IndexError):
        return None


class Sessions:
    """Process sessions this desktop started, so everything in them can be found and killed.

    Each entry is (session id, boot ticks before it was created): a process
    counts as ours only if it is in the session *and* started after it was
    created, which rules out unrelated processes that reuse a recycled pid.
    """

    def __init__(self):
        self._sessions: dict[int, int] = {}

    def add(self, sid: int, since: int):
        if sid > 1 and sid != os.getsid(0):
            self._sessions[sid] = since

    def owner(self, pid: int) -> int | None:
        """Which of our sessions pid (or its nearest ancestor in one) belongs to, if any."""
        for _ in range(64):
            stat = proc_stat(pid)
            if stat is None:
                return None
            ppid, sid, start, _ = stat
            if sid in self._sessions and start >= self._sessions[sid]:
                return sid
            if ppid <= 1:
                return None
            pid = ppid
        return None

    def members(self) -> list[int]:
        me = os.getpid()
        pids = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == me:
                continue
            stat = proc_stat(int(entry))
            if stat is None:
                continue
            _, sid, start, state = stat
            if sid in self._sessions and start >= self._sessions[sid] and state not in ("Z", "X"):
                pids.append(int(entry))
        return pids

    def kill_all(self, children=(), grace: float = 3.0):
        """SIGTERM everything in our sessions, SIGKILL what is left after `grace` seconds."""
        for pid in self.members():
            _signal(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            for child in children:
                child.poll()  # reap, or our own children linger as zombies
            if not self.members():
                break
            time.sleep(0.05)
        for pid in self.members():
            _signal(pid, signal.SIGKILL)
        for child in children:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self._sessions.clear()


def _signal(pid: int, sig):
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def spawn(command: str, env: dict, cwd: str, log_dir: Path, sessions: Sessions) -> subprocess.Popen:
    """Start a long-running command in its own session, output to a log file."""
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"app-{time.time_ns()}.log"
    since = boot_ticks()
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    sessions.add(proc.pid, since)
    proc.log_path = str(log_path)
    return proc


def run_shell(command: str, env: dict, cwd: str, timeout: float, sessions: Sessions) -> tuple[int | None, bytes]:
    """Run a shell command to completion; returns (exit status or None on timeout, output).

    Own session so a timeout can kill everything the command started, not just
    the shell. Anything it leaves running in the background is killed when the
    desktop stops.
    """
    sh = shutil.which("bash") or "/bin/sh"
    since = boot_ticks()
    proc = subprocess.Popen(
        [sh, "-c", command],
        env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    sessions.add(proc.pid, since)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        return None, out
