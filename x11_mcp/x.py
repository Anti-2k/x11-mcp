"""X11 client for the agent's display: EWMH, XTEST input, keymap, damage, capture.

All window and workspace control goes through EWMH (Openbox implements all of
it), and all input through XTEST on the agent's own X server. Connections
are always made to an explicit display with the desktop's own cookie, never
via $DISPLAY, so they cannot fall back to the user's X server.
"""
import logging
import os
import select
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from PIL import Image
from Xlib import X, XK, display as xdisplay, error as xerror
from Xlib.ext import damage, res, xtest
from Xlib.protocol import event as xevent

log = logging.getLogger(__name__)

for _group in ("xkb", "xf86", "latin2", "latin3", "latin4", "greek", "cyrillic",
               "technical", "special", "publishing"):
    XK.load_keysym_group(_group)

_auth_lock = threading.Lock()


def open_display(name: str, xauthority) -> xdisplay.Display:
    """Connect to the local display `name` with the cookie in `xauthority`.

    python-xlib only reads $XAUTHORITY, so it is set for the duration of the
    handshake. Child processes never see this: they always get an explicit env.
    """
    if not name.startswith(":"):
        raise ValueError(f"not a local display: {name!r}")
    with _auth_lock:
        saved = os.environ.get("XAUTHORITY")
        os.environ["XAUTHORITY"] = str(xauthority)
        try:
            return xdisplay.Display(name)
        finally:
            if saved is None:
                del os.environ["XAUTHORITY"]
            else:
                os.environ["XAUTHORITY"] = saved


def _prop(d, win, name, type=X.AnyPropertyType):
    try:
        p = win.get_full_property(d.intern_atom(name), type)
    except xerror.XError:
        return None
    return p.value if p is not None else None


def wm_name(d) -> str | None:
    """Name of the running EWMH window manager, None if there is none (yet)."""
    root = d.screen().root
    check = _prop(d, root, "_NET_SUPPORTING_WM_CHECK")
    if not check:
        return None
    # The check window points at itself while the WM lives; a stale root
    # property left by a WM that died points at a window that is gone.
    win = d.create_resource_object("window", check[0])
    if (_prop(d, win, "_NET_SUPPORTING_WM_CHECK") or [None])[0] != check[0]:
        return None
    name = _prop(d, win, "_NET_WM_NAME")
    return _decode(name) if name else "unknown"


def workspace_count(d) -> int:
    value = _prop(d, d.screen().root, "_NET_NUMBER_OF_DESKTOPS")
    return int(value[0]) if value else 0


def _decode(value) -> str:
    if isinstance(value, str):
        return value
    return bytes(value).decode("utf-8", errors="replace")


# -- EWMH constants -------------------------------------------------------------

_SOURCE_PAGER = 2  # EWMH source indication: requests from a pager/taskbar are always honored
_STATE_REMOVE, _STATE_ADD, _STATE_TOGGLE = 0, 1, 2
_ALL_DESKTOPS = 0xFFFFFFFF
_STATE_NAMES = {
    "_NET_WM_STATE_FULLSCREEN": "fullscreen",
    "_NET_WM_STATE_MAXIMIZED_VERT": "maximized_vert",
    "_NET_WM_STATE_MAXIMIZED_HORZ": "maximized_horz",
    "_NET_WM_STATE_HIDDEN": "minimized",
    "_NET_WM_STATE_SHADED": "shaded",
    "_NET_WM_STATE_ABOVE": "above",
    "_NET_WM_STATE_BELOW": "below",
    "_NET_WM_STATE_STICKY": "sticky",
    "_NET_WM_STATE_MODAL": "modal",
    "_NET_WM_STATE_DEMANDS_ATTENTION": "urgent",
}


def moveresize_data(frame, extents) -> list[int]:
    """_NET_MOVERESIZE_WINDOW data placing a window's *frame* at `frame` (x, y, w, h).

    With NorthWest gravity x/y are the frame's top-left corner, but width and
    height are always the client's, so the frame extents are taken off.
    """
    x, y, w, h = frame
    left, right, top, bottom = extents
    flags = 1 | (0b1111 << 8) | (_SOURCE_PAGER << 12)  # NorthWestGravity, x/y/w/h present
    width = max(1, w - left - right)
    height = max(1, h - top - bottom)
    return [flags, x & 0xFFFFFFFF, y & 0xFFFFFFFF, width, height]


def decode_states(names) -> list[str]:
    states = [_STATE_NAMES[n] for n in names if n in _STATE_NAMES]
    if "maximized_vert" in states and "maximized_horz" in states:
        states = [s for s in states if not s.startswith("maximized_")] + ["maximized"]
    return states


@dataclass
class Window:
    id: int
    title: str
    wm_class: str
    pid: int | None
    workspace: int | None  # 0-based; None = on every workspace
    frame: tuple  # outer rectangle, title bar and borders included: x, y, w, h
    content: tuple  # the app's own area
    extents: tuple  # left, right, top, bottom
    states: list = field(default_factory=list)
    type: str = "normal"
    transient_for: int | None = None
    mapped: bool = True

    @property
    def focused(self) -> bool:
        return "focused" in self.states


# -- keyboard -------------------------------------------------------------------

_FORBIDDEN_KEYSYMS = (
    {0xFED5}  # Terminate_Server
    | set(range(0x1008FE01, 0x1008FE0D))  # XF86Switch_VT_1..12
    | set(range(0x1008FE20, 0x1008FE26))  # XF86Ungrab, ClearGrab, Next/Prev_VMode, LogWindowTree, LogGrabInfo
)
MODIFIER_KEYSYMS = {
    "ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "altgr": "ISO_Level3_Shift", "super": "Super_L",
}
_CHAR_KEYSYMS = {"\n": XK.XK_Return, "\t": XK.XK_Tab, "\b": XK.XK_BackSpace}


def forbidden_keysym(keysym: int) -> bool:
    return keysym in _FORBIDDEN_KEYSYMS


def char_keysym(ch: str) -> int:
    """The keysym that types `ch`: Latin-1 keysyms are the code point, the rest 0x1000000 + it."""
    if ch in _CHAR_KEYSYMS:
        return _CHAR_KEYSYMS[ch]
    cp = ord(ch)
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        raise ValueError(f"cannot type control character {cp:#x}")
    if cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def keysym_from_name(name: str) -> int:
    """An X keysym from its name (Return, F5, a, XF86AudioMute) or a single character; 0 if unknown."""
    keysym = XK.string_to_keysym(name)
    if keysym:
        return keysym
    if name[:4].lower() == "xf86" and name[4:5] != "_":  # X's XF86AudioMute is python-xlib's XF86_AudioMute
        return XK.string_to_keysym("XF86_" + name[4:])
    if len(name) == 1:
        return char_keysym(name)
    if name[0] in "fF" and name[1:].isdigit():
        return XK.string_to_keysym("F" + name[1:])
    return 0


def plan_typing(keysyms, lookup: dict, spare: list, assigned: OrderedDict) -> list:
    """Turn keysyms into steps: ("key", keycode, shift), ("map", keycode, keysym) or ("flush",).

    Keysyms on the keyboard map are typed with their own key. The rest are put
    on spare (unmapped) keycodes. A spare keycode is never remapped while a key
    press that used its previous keysym may still be unread by an app: an app
    that reloads the keymap after the change would decode the old press with
    the new symbol. So once every spare keycode is in use, the plan flushes
    (send, then give apps time to catch up) before reusing any. `assigned`
    (keycode -> keysym, least recently used first) carries over between calls.
    """
    if not spare and any(k not in lookup for k in keysyms):
        raise RuntimeError("no free keycode to type characters missing from the keymap")
    steps = []
    in_batch = set()
    by_keysym = {ks: kc for kc, ks in assigned.items()}
    for ks in keysyms:
        if ks in lookup:
            kc, shift = lookup[ks]
            steps.append(("key", kc, shift))
            continue
        kc = by_keysym.get(ks)
        if kc is None:
            free = [k for k in spare if k not in in_batch]
            if not free:
                steps.append(("flush",))
                in_batch.clear()
                free = list(spare)
            unused = [k for k in free if k not in assigned]
            kc = unused[0] if unused else next(k for k in assigned if k in free)
            old = assigned.pop(kc, None)
            by_keysym.pop(old, None)
            assigned[kc] = ks
            by_keysym[ks] = kc
            steps.append(("map", kc, ks))
        else:
            assigned.move_to_end(kc)
        in_batch.add(kc)
        steps.append(("key", kc, False))
    return steps


class _Keymap:
    """The core keyboard map, split into keysym -> (keycode, needs shift) and spare keycodes."""

    def __init__(self, mapping, first_keycode: int, assigned: OrderedDict):
        self.lookup = {}
        self.spare = []
        for i, syms in enumerate(mapping):
            kc = first_keycode + i
            if kc in assigned and tuple(syms[:2]) == (assigned[kc], assigned[kc]):
                self.spare.append(kc)  # one of ours from an earlier call
                continue
            assigned.pop(kc, None)  # remapped by someone else (setxkbmap) since
            if not any(syms):
                self.spare.append(kc)
                continue
            for level in (0, 1):  # group 1, without and with shift
                ks = syms[level] if level < len(syms) else 0
                if ks and (ks not in self.lookup or (self.lookup[ks][1] and level == 0)):
                    self.lookup[ks] = (kc, level == 1)


# -- the client -----------------------------------------------------------------


class XClient:
    def __init__(self, display_name: str, xauthority):
        self.display_name = display_name
        self.xauthority = xauthority
        self.lock = threading.RLock()
        self.d = open_display(display_name, xauthority)
        self.d.set_error_handler(self._on_error)
        screen = self.d.screen()
        self.root = screen.root
        self.width, self.height = screen.width_in_pixels, screen.height_in_pixels
        for ext in ("XTEST", "DAMAGE", "XFIXES", "X-Resource"):
            if not self.d.has_extension(ext):
                raise RuntimeError(f"the agent X server lacks the {ext} extension")
        self.d.xfixes_query_version()
        self._assigned: OrderedDict[int, int] = OrderedDict()
        info = self.d.display.info
        self._first_keycode, self._last_keycode = info.min_keycode, info.max_keycode
        self._bgrx = info.image_byte_order == X.LSBFirst

    def _on_error(self, err, request):
        log.debug("X error (ignored): %s", err)

    def close(self):
        with self.lock:
            try:
                self.d.close()
            except Exception:
                pass

    def atom(self, name: str) -> int:
        return self.d.intern_atom(name)

    def _window(self, wid: int):
        return self.d.create_resource_object("window", wid)

    def _prop(self, win, name, type=X.AnyPropertyType):
        return _prop(self.d, win, name, type)

    def sync(self):
        with self.lock:
            self.d.sync()

    # -- EWMH reads -------------------------------------------------------

    def wm_name(self) -> str | None:
        with self.lock:
            return wm_name(self.d)

    def current_workspace(self) -> int:
        with self.lock:
            value = self._prop(self.root, "_NET_CURRENT_DESKTOP")
            return int(value[0]) if value else 0

    def workspace_count(self) -> int:
        with self.lock:
            return workspace_count(self.d) or 1

    def workarea(self, workspace: int) -> tuple:
        with self.lock:
            value = self._prop(self.root, "_NET_WORKAREA")
        if value and len(value) >= 4 * (workspace + 1):
            return tuple(int(v) for v in value[4 * workspace:4 * workspace + 4])
        return (0, 0, self.width, self.height)

    def active_window(self) -> int:
        with self.lock:
            value = self._prop(self.root, "_NET_ACTIVE_WINDOW")
            return int(value[0]) if value else 0

    def client_ids(self) -> list[int]:
        """Managed windows, oldest first."""
        with self.lock:
            return [int(w) for w in self._prop(self.root, "_NET_CLIENT_LIST") or []]

    def windows(self) -> list[Window]:
        """Managed windows, topmost first."""
        with self.lock:
            stack = self._prop(self.root, "_NET_CLIENT_LIST_STACKING") or []
            active = self.active_window()
            out = []
            for wid in reversed(stack):
                info = self.window(int(wid), active)
                if info is not None:
                    out.append(info)
            return out

    def window(self, wid: int, active: int | None = None) -> Window | None:
        with self.lock:
            win = self._window(wid)
            try:
                attrs = win.get_attributes()
                geom = win.get_geometry()
                origin = self.root.translate_coords(win, 0, 0)
            except xerror.XError:
                return None
            title = self._prop(win, "_NET_WM_NAME", self.atom("UTF8_STRING")) or self._prop(win, "WM_NAME")
            wm_class = self._prop(win, "WM_CLASS")
            class_name = ""
            if wm_class:
                parts = _decode(wm_class).split("\0")
                class_name = parts[1] if len(parts) > 1 and parts[1] else parts[0]
            desktop = self._prop(win, "_NET_WM_DESKTOP")
            workspace = None if not desktop or desktop[0] == _ALL_DESKTOPS else int(desktop[0])
            extents = tuple(int(v) for v in (self._prop(win, "_NET_FRAME_EXTENTS") or (0, 0, 0, 0)))
            state_atoms = self._prop(win, "_NET_WM_STATE") or []
            states = decode_states([self.d.get_atom_name(a) for a in state_atoms])
            if active is None:
                active = self.active_window()
            if wid == active:
                states.insert(0, "focused")
            transient = self._prop(win, "WM_TRANSIENT_FOR")
            types = self._prop(win, "_NET_WM_WINDOW_TYPE") or []
            wtype = "dialog" if transient else "normal"
            if types:
                wtype = self.d.get_atom_name(types[0]).removeprefix("_NET_WM_WINDOW_TYPE_").lower()
            left, right, top, bottom = extents
            x, y = origin.x, origin.y
            return Window(
                id=wid,
                title=_decode(title) if title else "",
                wm_class=class_name,
                pid=self.window_pid(wid),
                workspace=workspace,
                frame=(x - left, y - top, geom.width + left + right, geom.height + top + bottom),
                content=(x, y, geom.width, geom.height),
                extents=extents,
                states=states,
                type=wtype,
                transient_for=int(transient[0]) if transient else None,
                mapped=attrs.map_state == X.IsViewable,
            )

    def window_pid(self, wid: int) -> int | None:
        """The pid owning a window, as the X server sees it (XRes), else _NET_WM_PID."""
        with self.lock:
            try:
                reply = self.d.res_query_client_ids([{"client": wid, "mask": res.LocalClientPIDMask}])
                for cid in reply.ids:
                    if cid.spec.mask & res.LocalClientPIDMask and cid.value:
                        return int(cid.value[0])
            except xerror.XError:
                pass
            value = self._prop(self._window(wid), "_NET_WM_PID")
            return int(value[0]) if value else None

    # -- EWMH requests ----------------------------------------------------

    def _message(self, wid: int, type_name: str, data):
        data = (list(data) + [0] * 5)[:5]
        ev = xevent.ClientMessage(window=self._window(wid), client_type=self.atom(type_name), data=(32, data))
        with self.lock:
            self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            self.d.sync()

    def activate(self, wid: int):
        """Focus and raise a window, switching to its workspace and unminimizing it.

        The view is switched first: asked to activate a window on another
        desktop, Openbox (as EWMH allows) only marks it urgent.
        """
        w = self.window(wid)
        if w is not None and w.workspace is not None and w.workspace != self.current_workspace():
            self.view_workspace(w.workspace)
            self.wait_until(lambda: self.current_workspace() == w.workspace, 1.0)
        self._message(wid, "_NET_ACTIVE_WINDOW", [_SOURCE_PAGER, X.CurrentTime, self.active_window()])

    def close_window(self, wid: int):
        self._message(wid, "_NET_CLOSE_WINDOW", [X.CurrentTime, _SOURCE_PAGER])

    def set_state(self, wid: int, action: str, *names: str):
        code = {"remove": _STATE_REMOVE, "add": _STATE_ADD, "toggle": _STATE_TOGGLE}[action]
        atoms = [self.atom(n) for n in names] + [0, 0]
        self._message(wid, "_NET_WM_STATE", [code, atoms[0], atoms[1], _SOURCE_PAGER])

    def move_resize(self, wid: int, frame, extents):
        self._message(wid, "_NET_MOVERESIZE_WINDOW", moveresize_data(frame, extents))

    def set_window_workspace(self, wid: int, workspace: int):
        self._message(wid, "_NET_WM_DESKTOP", [workspace, _SOURCE_PAGER])

    def view_workspace(self, workspace: int):
        self._message(self.root.id, "_NET_CURRENT_DESKTOP", [workspace, X.CurrentTime])

    def set_workspace_count(self, count: int):
        self._message(self.root.id, "_NET_NUMBER_OF_DESKTOPS", [count])

    def kill_client(self, wid: int):
        with self.lock:
            self._window(wid).kill_client()
            self.d.sync()

    def wait_until(self, predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
        """Poll until the WM has done what was asked; False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    # -- pointer ----------------------------------------------------------

    def pointer(self) -> tuple[int, int]:
        with self.lock:
            p = self.root.query_pointer()
            return p.root_x, p.root_y

    def move_pointer(self, x: int, y: int):
        x = max(0, min(int(round(x)), self.width - 1))
        y = max(0, min(int(round(y)), self.height - 1))
        with self.lock:
            xtest.fake_input(self.d, X.MotionNotify, x=x, y=y)
            self.d.sync()

    def button(self, number: int, press: bool):
        with self.lock:
            xtest.fake_input(self.d, X.ButtonPress if press else X.ButtonRelease, number)
            self.d.sync()

    # -- keyboard ---------------------------------------------------------

    def _keymap(self) -> _Keymap:
        first, count = self._first_keycode, self._last_keycode - self._first_keycode + 1
        return _Keymap(self.d.get_keyboard_mapping(first, count), first, self._assigned)

    def _key(self, keycode: int, press: bool):
        xtest.fake_input(self.d, X.KeyPress if press else X.KeyRelease, keycode)

    def _run_steps(self, steps, shift_code: int, delay: float):
        for step in steps:
            if step[0] == "map":
                _, kc, ks = step
                self.d.change_keyboard_mapping(kc, [(ks, ks)])
                self.d.sync()
                time.sleep(0.01)
            elif step[0] == "flush":
                self.d.sync()
                time.sleep(0.1)
            else:
                _, kc, shift = step
                if shift:
                    self._key(shift_code, True)
                self._key(kc, True)
                self._key(kc, False)
                if shift:
                    self._key(shift_code, False)
                if delay:
                    self.d.sync()
                    time.sleep(delay)
        self.d.sync()

    def type_text(self, text: str, delay: float = 0.0):
        keysyms = [char_keysym(ch) for ch in text.replace("\r\n", "\n").replace("\r", "\n")]
        with self.lock:
            keymap = self._keymap()
            shift = keymap.lookup.get(XK.XK_Shift_L, (None,))[0]
            steps = plan_typing(keysyms, keymap.lookup, keymap.spare, self._assigned)
            if shift is None and any(s[0] == "key" and s[2] for s in steps):
                raise RuntimeError("the keymap has no Shift key")
            self._run_steps(steps, shift, delay)

    def press_combo(self, modifiers: list[str], keysym: int):
        """Hold the modifiers (ctrl, shift, alt, altgr, super), tap the key, release."""
        with self.lock:
            keymap = self._keymap()
            mod_codes = []
            for mod in modifiers:
                entry = keymap.lookup.get(XK.string_to_keysym(MODIFIER_KEYSYMS[mod]))
                if entry is None:
                    raise RuntimeError(f"the keymap has no {MODIFIER_KEYSYMS[mod]} key")
                mod_codes.append(entry[0])
            steps = plan_typing([keysym], keymap.lookup, keymap.spare, self._assigned)
            *maps, (_, kc, shift) = steps
            self._run_steps([s for s in maps if s[0] == "map"], 0, 0)
            if shift and "shift" not in modifiers:
                mod_codes.append(keymap.lookup[XK.XK_Shift_L][0])
            try:
                for code in mod_codes:
                    self._key(code, True)
                self._key(kc, True)
                self._key(kc, False)
            finally:
                for code in reversed(mod_codes):
                    self._key(code, False)
                self.d.sync()

    # -- capture ----------------------------------------------------------

    def capture(self, x: int, y: int, width: int, height: int, show_cursor: bool = False) -> Image.Image:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + width), min(self.height, y + height)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"region ({x}, {y}) {width}x{height} is outside the {self.width}x{self.height} screen")
        with self.lock:
            raw = self.root.get_image(x0, y0, x1 - x0, y1 - y0, X.ZPixmap, 0xFFFFFFFF)
            cursor = self.d.xfixes_get_cursor_image(self.root) if show_cursor else None
        img = Image.frombuffer("RGB", (x1 - x0, y1 - y0), bytes(raw.data), "raw",
                               "BGRX" if self._bgrx else "XRGB", 0, 1)
        if cursor is not None and cursor.width and cursor.height:
            argb = b"".join(int(p).to_bytes(4, "little") for p in cursor.cursor_image)
            sprite = Image.frombuffer("RGBA", (cursor.width, cursor.height), argb, "raw", "BGRA", 0, 1)
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            layer.paste(sprite, (cursor.x - cursor.xhot - x0, cursor.y - cursor.yhot - y0))  # clips at edges
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        return img

    def damage_watch(self) -> "DamageWatch":
        return DamageWatch(self.display_name, self.xauthority)


@dataclass
class IdleResult:
    idle: bool
    waited: float
    region: tuple | None = None  # bounding box of what was still changing


class DamageWatch:
    """Watches the whole screen for drawing (XDamage on the root window).

    Opens its own connection so its events never interleave with requests on
    the main one. Create it *before* the action whose effect you wait for.
    """

    def __init__(self, display_name: str, xauthority):
        self.d = open_display(display_name, xauthority)
        self.d.damage_query_version()
        # python-xlib registers a per-display copy of the event class, so
        # isinstance(ev, damage.DamageNotify) is never true: match the code.
        self._event = self.d.query_extension("DAMAGE").first_event + damage.DamageNotifyCode
        self.d.screen().root.damage_create(damage.DamageReportRawRectangles)
        self.d.sync()
        self._recent = deque(maxlen=2000)  # (time, x, y, w, h) of significant damage
        self.changes = 0

    def close(self):
        try:
            self.d.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _drain(self, min_area: int) -> bool:
        changed = False
        while self.d.pending_events():
            ev = self.d.next_event()
            if ev.type & 0x7F == self._event:
                a = ev.area
                if a.width * a.height >= min_area:
                    changed = True
                    self.changes += 1
                    self._recent.append((time.monotonic(), a.x, a.y, a.width, a.height))
        return changed

    def wait(self, quiet: float, timeout: float, min_area: int = 0, need_change: bool = False) -> IdleResult:
        """Return once nothing of at least `min_area` px² was drawn for `quiet` seconds.

        need_change: first wait for something to be drawn at all (up to timeout).
        """
        start = time.monotonic()
        last = None if need_change else start
        while True:
            if self._drain(min_area):
                last = time.monotonic()
            now = time.monotonic()
            if last is not None and now - last >= quiet:
                return IdleResult(True, now - start)
            if now - start >= timeout:
                return IdleResult(False, now - start, self._region(now - max(quiet, 0.5)))
            until = timeout - (now - start)
            if last is not None:
                until = min(until, quiet - (now - last))
            select.select([self.d], [], [], max(0.0, until))

    def _region(self, since: float):
        rects = [r[1:] for r in self._recent if r[0] >= since] or [r[1:] for r in list(self._recent)[-20:]]
        if not rects:
            return None
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        return (x0, y0, x1 - x0, y1 - y0)
