"""Layout math for tile_windows: split an area into frame rectangles."""
import math

LAYOUTS = ("columns", "rows", "grid")


def _split(start: int, length: int, n: int) -> list[tuple[int, int]]:
    """n consecutive (position, size) spans covering exactly `length` pixels."""
    base, extra = divmod(length, n)
    spans, pos = [], start
    for i in range(n):
        size = base + (1 if i < extra else 0)
        spans.append((pos, size))
        pos += size
    return spans


def tile(n: int, area: tuple, layout: str) -> list[tuple[int, int, int, int]]:
    """Frame rectangles (x, y, w, h) for n windows filling `area`, in reading order.

    columns: side by side; rows: stacked; grid: ceil(sqrt(n)) columns, with
    the last row's windows sharing its full width.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}")
    if n <= 0:
        return []
    ax, ay, aw, ah = area
    if layout == "columns":
        return [(x, ay, w, ah) for x, w in _split(ax, aw, n)]
    if layout == "rows":
        return [(ax, y, aw, h) for y, h in _split(ay, ah, n)]
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    rects = []
    for r, (y, h) in enumerate(_split(ay, ah, rows)):
        for x, w in _split(ax, aw, min(cols, n - r * cols)):
            rects.append((x, y, w, h))
    return rects
