"""`x11-mcp check`: report missing runtime pieces, with install hints per distro."""
import os
import platform
import shutil
import subprocess
from pathlib import Path

# (os-release ID, command installing what is required, command for the optional extras)
_DISTROS = [
    ("arch", "sudo pacman -S xorg-server-xvfb openbox dbus", "sudo pacman -S tigervnc x11vnc"),
    ("fedora", "sudo dnf install xorg-x11-server-Xvfb openbox dbus-daemon",
     "sudo dnf install tigervnc-server x11vnc"),
    ("debian", "sudo apt install xvfb openbox dbus", "sudo apt install tigervnc-standalone-server x11vnc"),
    ("opensuse", "sudo zypper install xorg-x11-server-Xvfb openbox dbus-1",
     "sudo zypper install xorg-x11-Xvnc x11vnc"),
    ("gentoo", "sudo emerge x11-base/xorg-server x11-wm/openbox sys-apps/dbus  (xorg-server with USE=xvfb)",
     "sudo emerge net-misc/tigervnc x11-misc/x11vnc  (tigervnc with USE=server)"),
    ("void", "sudo xbps-install xorg-server-xvfb openbox dbus", "sudo xbps-install tigervnc x11vnc"),
    ("alpine", "doas apk add xvfb openbox dbus", "doas apk add tigervnc x11vnc"),
    ("guix", "guix install xorg-server openbox dbus", "guix install tigervnc-server x11vnc"),
    ("nixos", "nix run github:Anti-2k/x11-mcp  (bundles everything)", ""),
]


def _os_ids() -> list[str]:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return []
    fields = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
    )
    ids = [fields.get("ID", "").strip('"')]
    ids += fields.get("ID_LIKE", "").strip('"').split()
    return [i for i in ids if i]


def _hint():
    ids = _os_ids()
    for distro_id, required, optional in _DISTROS:
        if any(i == distro_id or i.startswith(distro_id) for i in ids):
            return required, optional
    return None, None


def _version(argv) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = [line.strip() for line in (out.stdout + out.stderr).splitlines() if line.strip()]
    return lines[0] if lines else ""


def run() -> int:
    from x11_mcp.desktop import server_binary, wm_binary
    from x11_mcp.isolation import user_runtime_dir

    ok = True
    optional_missing = False
    server = os.environ.get("X11_MCP_SERVER", "xvfb").lower()

    def report(good: bool, what: str, detail: str = "", optional: bool = False):
        nonlocal ok, optional_missing
        if optional and not good:
            optional_missing = True
            print(f"--      {what}  ({detail})")
            return
        ok &= good
        print(f"{'ok     ' if good else 'MISSING'} {what}" + (f"  ({detail})" if detail else ""))

    report(platform.system() == "Linux", "Linux", platform.system())
    try:
        report(True, "runtime directory", str(user_runtime_dir()))
    except RuntimeError as exc:
        report(False, "runtime directory", str(exc))

    for kind, flag, purpose in (("xvfb", None, "the headless X server"),
                                ("xvnc", "-version", "X11_MCP_SERVER=xvnc")):
        binary = server_binary(kind)
        found = shutil.which(binary)
        detail = (_version([found, flag]) if flag else found) if found else f"needed for {purpose}"
        report(bool(found), Path(binary).name, detail,
               optional=kind != server)
    wm = wm_binary()
    found = shutil.which(wm) or (wm if os.access(wm, os.X_OK) else None)
    report(bool(found), "openbox", _version([found, "--version"]) if found else "the window manager")
    path = shutil.which("dbus-daemon")
    report(bool(path), "dbus-daemon", path or "the private session bus")
    path = shutil.which("x11vnc")
    report(bool(path), "x11vnc", path or "optional, for `x11-mcp watch` on Xvfb", optional=True)

    from importlib.metadata import PackageNotFoundError, version

    for module, dist in (("Xlib", "python-xlib"), ("PIL", "Pillow"), ("mcp", "mcp")):
        try:
            __import__(module)
            report(True, dist, version(dist))
        except (ImportError, PackageNotFoundError) as exc:
            report(False, dist, str(exc))

    required, optional = _hint()
    if not ok:
        print()
        print(f"Install:  {required}" if required else
              "Install Xvfb, Openbox and dbus-daemon with your package manager.")
        print("Package names can change; https://repology.org lists them per distro.")
    if optional_missing and optional:
        print(f"Optional: {optional}")
    if ok:
        print("\nAll set. Add it to your MCP client, e.g.:")
        print("  claude mcp add --scope user x11-desktop -- x11-mcp")
    return 0 if ok else 1
