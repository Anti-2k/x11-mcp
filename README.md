# x11-mcp

An MCP server that gives an agent its own computer to use: a private, headless
X11 desktop (Xvfb + Openbox) running next to yours. The agent launches apps,
takes screenshots, and drives the mouse and keyboard there. None of it reaches
your session: your cursor stays put, your focus is never taken, nothing
appears on your screen, and you don't need to be on any particular workspace.

The agent's desktop is a separate X server with no physical input, so this
works from any desktop (GNOME, KDE, Sway, Hyprland, an X11 session, ...)
or none at all, e.g. over ssh or in CI.

Apps run on their X11 backend, so this tests an app's X11 path, not its Wayland one.

## Install

### Nix (any distro)

The flake bundles everything (Xvfb, Openbox, `dbus-daemon`, TigerVNC's `Xvnc`, `x11vnc`):

```sh
claude mcp add --scope user x11-desktop -- nix run github:Anti-2k/a11
```

Or build a local copy: `git clone https://github.com/Anti-2k/a11 && cd
a11 && nix build`, then point your client at `result/bin/x11-mcp`.

### Other distros

1. Install the X server, the window manager and D-Bus. `Xvnc` and `x11vnc`
   are optional (see [Watching the agent](#watching-the-agent)).

   | Distro | Required | Optional |
   |---|---|---|
   | Arch | `sudo pacman -S xorg-server-xvfb openbox dbus` | `tigervnc x11vnc` |
   | Debian, Ubuntu | `sudo apt install xvfb openbox dbus` | `tigervnc-standalone-server x11vnc` |
   | Fedora | `sudo dnf install xorg-x11-server-Xvfb openbox dbus-daemon` | `tigervnc-server x11vnc` |
   | openSUSE | `sudo zypper install xorg-x11-server-Xvfb openbox dbus-1` | `xorg-x11-Xvnc x11vnc` |
   | Gentoo | `sudo emerge x11-base/xorg-server x11-wm/openbox sys-apps/dbus` (`USE=xvfb`) | `net-misc/tigervnc` (`USE=server`) `x11-misc/x11vnc` |
   | Void | `sudo xbps-install xorg-server-xvfb openbox dbus` | `tigervnc x11vnc` |
   | Alpine | `doas apk add xvfb openbox dbus` | `tigervnc x11vnc` |
   | Guix | `guix install xorg-server openbox dbus` | `tigervnc-server x11vnc` |

   Package names drift; [repology](https://repology.org) has the current ones.

2. Install the server (Python 3.10+) with [uv](https://docs.astral.sh/uv/) or
   pipx. Its Python dependencies (`mcp`, `python-xlib`, `Pillow`) come as wheels.

   ```sh
   uv tool install git+https://github.com/Anti-2k/a11
   # or: pipx install git+https://github.com/Anti-2k/a11
   ```

3. Check that everything is found, then register it:

   ```sh
   x11-mcp check
   claude mcp add --scope user x11-desktop -- x11-mcp
   ```

Any other MCP client: run `x11-mcp` as a stdio server. The desktop starts on
the first tool call and is torn down, along with every app the agent started,
when the server exits.

### Options (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `X11_MCP_NAME` | `agent` | Desktop name. A second server with the same name gets `agent-2`, and so on, so two agents never share one desktop. |
| `X11_MCP_RESOLUTION` | `1920x1080` | Size of the headless screen. |
| `X11_MCP_SERVER` | `xvfb` | `xvfb`, or `xvnc` for TigerVNC's Xvnc, which serves a view-only VNC stream itself. |
| `X11_MCP_WORKSPACES` | `4` | Number of workspaces at start (1-32). The agent can change it with `set_workspace_count`. |
| `X11_MCP_SHARE_HOME` | unset | `1` runs agent apps with your real `$HOME`. See below before turning this on. |
| `X11_MCP_PASS_ENV` | unset | Comma-separated variables to pass through anyway, e.g. `SSH_AUTH_SOCK`. The session-pointing ones (`DISPLAY`, `XAUTHORITY`, `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `SESSION_MANAGER`, ...) are refused. |
| `X11_MCP_OPENBOX` | `openbox` on `PATH` | Use a different Openbox binary. |

## Tools

| Tool | |
|---|---|
| `desktop_info` | Screen size, workspaces (which is shown, window counts), focused window |
| `screenshot` | Whole screen or a region; `scale` < 1 to save tokens; `workspace` to capture a hidden one |
| `zoom` | A region magnified (up to 4x) to read small text |
| `launch` | Run a shell command on the desktop, optionally on a given workspace; focuses and reports the new window |
| `bash` | Run a short command in the desktop's environment and return its output (killed after `timeout`) |
| `list_windows` | Windows topmost first: id, title, class, pid, workspace, frame and content geometry, focused/maximized/fullscreen/minimized |
| `focus_window`, `close_window`, `window_action` | Window management (`toggle_fullscreen`, `toggle_maximize`, `kill`) |
| `move_resize_window`, `tile_windows` | Place a window; arrange a workspace as `columns`, `rows` or a `grid` |
| `view_workspace`, `move_window_to_workspace`, `set_workspace_count` | Workspaces, numbered from 1 |
| `mouse_move`, `click`, `drag`, `scroll` | Pointer, in screen pixels |
| `type_text`, `press_keys` | Keyboard: any Unicode text regardless of keymap; combos like `ctrl+shift+t` |
| `wait`, `wait_until_idle`, `restart_desktop` | `wait_until_idle` returns once the screen stops changing |

Windows float: they keep the size and position they open with until something
moves them, so coordinates from a screenshot stay valid when the agent clicks.
`tile_windows` gives side-by-side tiling on demand.

A hidden workspace has no pixels (Openbox unmaps its windows), so
`screenshot(workspace=N)` briefly switches the view there, waits for the apps to
repaint, captures, switches back and restores the focus.

`wait_until_idle` watches the whole screen with the X DAMAGE extension.
Redrawn rectangles smaller than `min_area` (default 400 px², about 20x20: blinking
text cursors, small spinners) don't count, so a blinking cursor doesn't keep it
waiting forever.

## Watching the agent

```sh
x11-mcp watch agent              # view-only VNC server on localhost; connect any viewer to the PORT it prints
eval "$(x11-mcp env agent)"      # point this shell at the agent desktop: exports DISPLAY and XAUTHORITY
```

`watch` runs `x11vnc -viewonly -localhost -nosel` against the agent's display.
With `X11_MCP_SERVER=xvnc` the X server is the VNC server: `watch` then prints
its port (5900 + display number). It runs with viewer keyboard, pointer,
resize and clipboard input all disabled. Either way, a viewer can look but
cannot type, click or move the agent's pointer (the live test checks this).

Both listen on `127.0.0.1` without a password, so on a machine shared with other
users, they can watch too while it runs.

## How the isolation works

The agent gets its own X server rather than a corner of yours. In one X
session there is one pointer and one keyboard focus, so any synthetic click or
keystroke would have to move your cursor and take your focus.

| Way the two sides could interfere | What prevents it |
|---|---|
| Your keyboard and mouse reaching the agent | The agent's Xvfb (or Xvnc) is headless with no input devices. Xvnc and `watch` are view-only. |
| Agent input reaching your windows | Input goes in through the XTEST extension on the agent's own X server. Nothing uses `/dev/uinput`, ydotool or portals. |
| Agent apps opening on your screen | Every process gets a freshly built environment whose only display is the agent's (`DISPLAY=:N`, its own `XAUTHORITY`), with the X11 backend forced (`GDK_BACKEND=x11`, `QT_QPA_PLATFORM=xcb`, ...). `WAYLAND_DISPLAY`, your toolkit and input-method settings (`GDK_BACKEND=wayland`, `NIXOS_OZONE_WL`, `GTK_IM_MODULE`, `XMODIFIERS`, ...), `SESSION_MANAGER`, `TMUX`, `KITTY_*`, `NVIM`, `SWAYSOCK`, `HYPRLAND_*`, `MANGO*`, ssh/gpg agent variables and similar are dropped. |
| Other programs connecting to the agent's display | The X server runs with `-nolisten tcp -auth <cookie file>`. The cookie is random per desktop and lives in the agent's runtime directory (mode 0600). Connections without it are refused. |
| The shared `/tmp/.X11-unix` | Both X servers have their sockets there (plus abstract ones, `@/tmp/.X11-unix/X0`). **The cookie guards the agent's display, not yours.** Some compositors start Xwayland without any access control (no `-auth`), and where your session does use a cookie, it is in a file any of your processes can read. Agent apps stay off your display only because their `DISPLAY` points at the agent's. |
| Single-instance apps handing off to your copy | A private D-Bus session bus, plus by default a separate `$HOME` (`~/.local/share/x11-mcp/<name>/home`), which holds the profile locks Chromium, Electron and Firefox use. |
| Notifications, portals and other D-Bus services showing up on your desktop | The private bus. Services activated on it inherit the agent's display. |
| Your session's environment being rewritten | Openbox is started directly (not `openbox-session`) with no D-Bus address and no autostart, so nothing runs `systemctl --user import-environment` or `dbus-update-activation-environment`. |
| Window manager bindings | The agent's Openbox config has no key bindings, no root-window or desktop mouse bindings and no autostart. The only mouse bindings are the ones a window frame needs (click to focus, drag the title bar or edges, the title bar buttons), none with a modifier, so no app shortcut is intercepted. VT-switch, server-kill and grab-breaking keysyms are refused. |
| Two agents fighting over one desktop | Each server takes an exclusive lock on its desktop name. |
| Leftover processes | Every `launch` and `bash` runs in its own session, and teardown kills everything still in those sessions, background jobs included. The X server, Openbox and the bus die with the MCP server even if it is SIGKILLed (`PR_SET_PDEATHSIG`), which also disconnects every agent app. |

`tests/test_live.py` checks this end to end:
- It starts a desktop and drives terminals on two workspaces.
- While they run, it asserts (from `ss -x` socket peers) that no agent process holds a connection to your X server or Wayland compositor.
- Afterwards, it asserts that your `systemctl --user show-environment` and the contents of your runtime directory are unchanged.

### Limits

This keeps the agent and you from getting in each other's way. **It is not a
security sandbox.** Agent processes run as your user, with your files and
network.
- **Your display:** a process that deliberately sets `DISPLAY=:0`, or
  `WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$UID`, can still reach
  your session. The abstract X socket can't even be hidden with a mount namespace;
  that takes a network namespace too.
- **gpg:** `gpg` finds its agent at a fixed path, so signing inside the agent
  desktop can pop a pinentry dialog on *your* screen.
- **Sound:** agent apps have none. The private runtime directory has no
  PipeWire or PulseAudio socket.
- **Graphics:** Xvfb renders OpenGL in software (Mesa llvmpipe).
  GPU-heavy apps work, but slowly.

Covering the first two needs namespaces (e.g. bubblewrap). That is not done here.

With `X11_MCP_SHARE_HOME=1`, an app that is already running on your desktop
(Chromium, VS Code, Discord, ...) may receive the agent's launch and open the
window on your screen. To use those apps with a shared home, give them a
separate profile (`--user-data-dir`, `firefox --profile ... --new-instance`).

## License

MIT, see [LICENSE](LICENSE).

## Development

```sh
nix develop
python -m pytest                                        # unit tests
X11_MCP_LIVE=1 python -m pytest                         # plus the live end-to-end test (needs xterm)
X11_MCP_LIVE=1 X11_MCP_SERVER=xvnc python -m pytest     # the same against Xvnc
```

Logs: `~/.local/state/x11-mcp/server.log`; the per-desktop `xserver.log`,
`openbox.log`, `dbus.log` and per-app logs live in the desktop's runtime
directory (`$XDG_RUNTIME_DIR/x11-mcp/<name>`) while it runs.
