"""A small full-screen terminal UI built on the standard library only.

Windows reads keys with msvcrt, macOS/Linux with termios; drawing is plain ANSI
(Windows 10+ consoles get virtual-terminal mode switched on). Keys and the screen
can be injected, so every widget is testable without a terminal.

Widgets: `select` (checkbox or radio list with a details panel that follows the
cursor, filtering with `/`), `view` (scrollable text), `ask_text`, `message`, `form`.

Shared with skillpantry (https://github.com/Dakirchik/skillpantry), same author, MIT.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from agent_recall.i18n import T

APP_NAME = "agent-recall"

# ---- styles ------------------------------------------------------------------------

BOLD, DIM, REV = "1", "2", "7"
RED, GREEN, YELLOW, BLUE, CYAN = "31", "32", "33", "34", "36"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Quit(Exception):
    """The user asked to leave the application (q)."""


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s: str) -> int:
    return sum(char_width(c) for c in s)


def fit(s: str, w: int, pad: bool = True) -> str:
    """Cut to display width `w` (with an ellipsis) and optionally pad with spaces."""
    if w <= 0:
        return ""
    if width(s) <= w:
        return s + " " * (w - width(s)) if pad else s
    out, used = [], 0
    for ch in s:
        cw = char_width(ch)
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    res = "".join(out) + "…"
    return res + " " * (w - width(res)) if pad else res


def style(text: str, *codes: str) -> str:
    codes = tuple(c for c in codes if c)
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes and text else text


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


Line = tuple[str, str]  # (text, style codes separated by ';' or '')


def wrap(lines: Iterable[Line], w: int) -> list[Line]:
    """Wrap (text, style) lines to width `w`, keeping bullet indents."""
    out: list[Line] = []
    for text, st in lines:
        if not text:
            out.append(("", ""))
            continue
        m = re.match(r"^(\s*(?:[•·!✓✗↑-]\s+)?)", text)
        indent = " " * width(m.group(1)) if m else ""
        parts = textwrap.wrap(text, width=max(10, w), subsequent_indent=indent,
                              break_long_words=True, break_on_hyphens=False) or [""]
        out.extend((p, st) for p in parts)
    return out


# ---- keys ----------------------------------------------------------------------------

WIN_KEYS = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end",
            "I": "pgup", "Q": "pgdn", "S": "delete"}
ESC_KEYS = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down",
            "OC": "right", "OD": "left", "[H": "home", "[F": "end", "OH": "home", "OF": "end",
            "[1~": "home", "[4~": "end", "[7~": "home", "[8~": "end", "[5~": "pgup",
            "[6~": "pgdn", "[3~": "delete", "[Z": "shift-tab"}


def normalize(ch: str) -> str:
    return {"\r": "enter", "\n": "enter", " ": "space", "\t": "tab", "\x1b": "esc",
            "\x7f": "backspace", "\x08": "backspace"}.get(ch, ch)


class Term:
    """Terminal session. Pass `keys` (an iterable of key names) and `out` to test."""

    def __init__(self, keys: Iterable[str] | None = None, out=None, size: tuple[int, int] | None = None):
        self._keys: Iterator[str] | None = iter(keys) if keys is not None else None
        self.out = out or sys.stdout
        self._size = size
        self._old = None
        self.active = False
        self.frames = 0

    # -- lifecycle --
    def __enter__(self) -> "Term":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if self.active:
            return
        if self._keys is None:
            if os.name == "nt":
                _enable_vt()
            else:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(fd)
                tty.setcbreak(fd)
        self.out.write("\x1b[?1049h\x1b[?25l\x1b[H\x1b[2J")
        self.out.flush()
        self.active = True

    def stop(self) -> None:
        if not self.active:
            return
        self.out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        self.out.flush()
        if self._old is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            self._old = None
        self.active = False

    def suspend(self):
        """Context manager: back to the normal screen to print and ask, then return."""
        term = self

        class _S:
            def __enter__(self_inner):
                term.stop()

            def __exit__(self_inner, *exc):
                term.start()
        return _S()

    # -- io --
    def size(self) -> tuple[int, int]:
        if self._size:
            return self._size
        s = shutil.get_terminal_size((100, 30))
        return max(40, s.columns), max(12, s.lines)

    def draw(self, lines: list[str]) -> None:
        cols, rows = self.size()
        buf = ["\x1b[H"]
        for i in range(rows):
            text = lines[i] if i < len(lines) else ""
            buf.append(text + "\x1b[0m\x1b[K")
            if i < rows - 1:
                buf.append("\r\n")
        self.out.write("".join(buf))
        self.out.flush()
        self.frames += 1

    def key(self) -> str:
        if self._keys is not None:
            try:
                return next(self._keys)
            except StopIteration:
                raise Quit() from None
        return _read_key_windows() if os.name == "nt" else _read_key_unix()


def _enable_vt() -> bool:
    """Switch a Windows console to ANSI mode; False on consoles that cannot (before Windows 10)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def supported() -> bool:
    """Can this console show the full-screen UI?"""
    if os.name == "nt":
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            return False
        return _enable_vt()
    try:
        import termios  # noqa: F401
        return True
    except ImportError:
        return False


def _read_key_windows() -> str:
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return WIN_KEYS.get(msvcrt.getwch(), "")
    if ch == "\x03":
        raise KeyboardInterrupt
    return normalize(ch)


def _read_key_unix() -> str:
    import select
    fd = sys.stdin.fileno()

    def read1(timeout: float | None = None) -> bytes:
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return b""
        return os.read(fd, 1)

    b = read1()
    if b == b"\x1b":
        seq = b""
        while True:
            nb = read1(0.03)
            if not nb:
                break
            seq += nb
            if len(seq) >= 2 and (nb.isalpha() or nb == b"~"):
                break
        if not seq:
            return "esc"
        return ESC_KEYS.get(seq.decode("ascii", "replace"), "")
    if b and b[0] >= 0xC0:  # UTF-8 lead byte: read the rest of the character
        need = 1 if b[0] < 0xE0 else 2 if b[0] < 0xF0 else 3
        for _ in range(need):
            b += read1(0.05)
        return b.decode("utf-8", "replace")
    if b == b"\x03":
        raise KeyboardInterrupt
    return normalize(b.decode("utf-8", "replace"))


# ---- layout helpers ---------------------------------------------------------------------

def header(title: str, crumbs: str, cols: int) -> list[str]:
    left = f" {APP_NAME} "
    return [style(fit(f"{left}› {crumbs}", cols), REV, BOLD),
            style(fit(" " + title, cols), BOLD) if title else ""]


def footer(hint: str, cols: int) -> str:
    return style(fit(" " + hint, cols), REV)


def paint(line: Line, w: int) -> str:
    text, st = line
    return style(fit(text, w), *st.split(";")) if st else fit(text, w)


# ---- list widget ----------------------------------------------------------------------------

@dataclass
class Item:
    key: str
    label: str
    right: str = ""                 # short marker on the right of the row
    right_style: str = ""
    disabled: str | None = None     # reason the row cannot be chosen
    header: bool = False            # a group heading, not selectable
    search: str = ""                # extra text the filter looks at


@dataclass
class SelectResult:
    keys: list[str] = field(default_factory=list)
    back: bool = False


Details = Callable[[Item], list[Line]]


def select(term: Term, items: list[Item], *, title: str = "", crumbs: str = "", multi: bool = False,
           details: Details | None = None, selected: Iterable[str] = (), hint: str = "",
           empty_means_cursor: bool = True, start: str | None = None,
           allow_empty: bool = False, right_enters: bool = False) -> SelectResult:
    """Checkbox (multi) or radio list. Enter confirms, Esc goes back, q quits.

    With `multi` and nothing ticked, Enter takes the row under the cursor
    (`empty_means_cursor`) so a quick single choice needs no Space.
    → opens the details (or works as Enter with `right_enters`); ←, Backspace and Esc go back.
    """
    chosen = [i.key for i in items if i.key in set(selected) and not i.header and not i.disabled]
    query = ""
    filtering = False
    cursor = 0
    offset = 0

    def visible() -> list[Item]:
        if not query:
            return items
        q = query.lower()
        keep: list[Item] = []
        pending_header = None
        for it in items:
            if it.header:
                pending_header = it
                continue
            if q in (it.label + " " + it.search + " " + it.key).lower():
                if pending_header is not None:
                    keep.append(pending_header)
                    pending_header = None
                keep.append(it)
        return keep

    def selectable(vis: list[Item], i: int) -> bool:
        return 0 <= i < len(vis) and not vis[i].header

    def first_selectable(vis: list[Item]) -> int:
        for i, it in enumerate(vis):
            if not it.header:
                return i
        return 0

    vis = visible()
    cursor = first_selectable(vis)
    if start:
        for i, it in enumerate(vis):
            if it.key == start and not it.header:
                cursor = i

    while True:
        vis = visible()
        if not selectable(vis, cursor):
            cursor = first_selectable(vis)
        cols, rows = term.size()
        top = header(title, crumbs, cols)
        if filtering or query:
            top.append(style(fit(T(f" Search: {query}", f" Поиск: {query}") + ("▏" if filtering else "") +
                                 (T("   (Esc — clear)", "   (Esc — сбросить)") if query else ""), cols), YELLOW))
        else:
            top.append("")
        body_h = rows - len(top) - 1
        two_col = details is not None and cols >= 96
        if details is None:
            list_w, list_h = cols, body_h
        elif two_col:
            list_w, list_h = max(34, int(cols * 0.42)), body_h
        else:
            list_w, list_h = cols, min(len(vis), max(5, body_h * 5 // 10))
        # keep the cursor visible
        if cursor < offset:
            offset = cursor
        if cursor >= offset + list_h:
            offset = cursor - list_h + 1
        offset = max(0, min(offset, max(0, len(vis) - list_h)))

        rows_out: list[str] = []
        for i in range(offset, min(len(vis), offset + list_h)):
            it = vis[i]
            if it.header:
                rows_out.append(style(fit(" " + it.label, list_w), BOLD, CYAN))
                continue
            mark = ""
            if multi:
                mark = "[-] " if it.disabled else ("[x] " if it.key in chosen else "[ ] ")
            pointer = "›" if i == cursor else " "
            right = f" {it.right}" if it.right else ""
            label_w = list_w - 2 - width(mark) - width(right)
            text = f"{pointer} {mark}{fit(it.label, label_w)}"
            if i == cursor:
                rows_out.append(style(text + right, REV))
            elif it.disabled:
                rows_out.append(style(text + right, DIM))
            else:
                rows_out.append(text + (style(right, *it.right_style.split(";")) if it.right_style else right))
        if not vis:
            rows_out.append(style(fit(T("  nothing found", "  ничего не найдено"), list_w), DIM))
        rows_out += [" " * list_w] * (list_h - len(rows_out))
        # scroll position
        if len(vis) > list_h:
            rows_out[-1] = style(fit(T(f"  {offset + 1}–{min(len(vis), offset + list_h)} of {len(vis)} ↓", f"  {offset + 1}–{min(len(vis), offset + list_h)} из {len(vis)} ↓"),
                                     list_w), DIM)

        cur = vis[cursor] if selectable(vis, cursor) else None
        info: list[Line] = details(cur) if (details and cur) else []
        if cur and cur.disabled:
            info = [(T(f"Unavailable: {cur.disabled}", f"Недоступно: {cur.disabled}"), YELLOW), ("", "")] + info
        lines = list(top)
        if two_col:
            dw = cols - list_w - 3
            panel = wrap(info, dw)[:list_h]
            panel += [("", "")] * (list_h - len(panel))
            for l, r in zip(rows_out, panel):
                lines.append(l + style(" │ ", DIM) + paint(r, dw))
        else:
            lines += rows_out
            if details is not None:
                lines.append(style("─" * cols, DIM))
                panel = wrap(info, cols - 2)[: body_h - list_h - 1]
                lines += [" " + paint(p, cols - 2) for p in panel]
        lines = lines[: rows - 1]
        lines += [""] * (rows - 1 - len(lines))
        count = T(f"Selected: {len(chosen)}   ", f"Отмечено: {len(chosen)}   ") if multi and chosen else ""
        if filtering:
            base = T("Type to search  Backspace erase  Enter/↑↓ back to the list  Esc clear", "Печатайте для поиска  Backspace стереть  Enter/↑↓ к списку  Esc сбросить")
        else:
            base = hint or keys_hint(multi=multi, details=details is not None)
        lines.append(footer(compact(count + base, cols), cols))
        term.draw(lines)

        k = term.key()
        if filtering:
            if k == "enter":
                filtering = False
            elif k == "esc":
                filtering, query = False, ""
            elif k == "backspace":
                query = query[:-1]
            elif k in ("up", "down"):
                filtering = False
                cursor = _move(vis, cursor, -1 if k == "up" else 1)
            elif k == "space":
                query += " "
            elif len(k) == 1 and k.isprintable():
                query += k
            continue
        if k in ("up", "k"):
            cursor = _move(vis, cursor, -1)
        elif k in ("down", "j"):
            cursor = _move(vis, cursor, 1)
        elif k == "pgup":
            cursor = _move(vis, cursor, -max(1, list_h - 1))
        elif k == "pgdn":
            cursor = _move(vis, cursor, max(1, list_h - 1))
        elif k == "home":
            cursor = first_selectable(vis)
        elif k == "end":
            cursor = _move(vis, len(vis) - 1, 0)
        elif k == "space" and multi and cur and not cur.disabled:
            if cur.key in chosen:
                chosen.remove(cur.key)
            else:
                chosen.append(cur.key)
            cursor = _move(vis, cursor, 1) if cursor < len(vis) - 1 else cursor
        elif k in ("a", "A", "ф", "Ф") and multi:
            pool = [it.key for it in vis if not it.header and not it.disabled]
            if all(p in chosen for p in pool):
                chosen = [c for c in chosen if c not in pool]
            else:
                chosen += [p for p in pool if p not in chosen]
        elif k == "/":
            filtering = True
        elif k == "right" and right_enters and cur and not cur.disabled and not multi:
            return SelectResult([cur.key])
        elif k in ("?", "right", "tab") and details and cur:
            view(term, details(cur), title=cur.label, crumbs=crumbs + T(" › details", " › подробно"))
        elif k == "enter":
            if multi:
                keys = [i.key for i in items if i.key in chosen]
                if not keys and empty_means_cursor and cur and not cur.disabled:
                    keys = [cur.key]
                if keys or allow_empty:
                    return SelectResult(keys)
            elif cur and not cur.disabled:
                return SelectResult([cur.key])
        elif k in ("esc", "left", "backspace"):
            if query:
                query = ""
            else:
                return SelectResult(back=True)
        elif k in ("q", "Q", "й", "Й"):
            raise Quit()


def back_hint() -> str:
    return T("←/Backspace/Esc back", "←/Backspace/Esc назад")


def keys_hint(*, multi: bool = False, details: bool = True, enter: str | None = None,
              back: str | None = None, search: bool = True) -> str:
    """Footer line for a list: the most used keys first, so a narrow terminal cuts the rare ones."""
    parts = [T("↑↓ move", "↑↓ выбор")]
    if multi:
        parts += [T("Space tick", "Пробел отметить"), T(f"Enter {enter or 'next'}", f"Enter {enter or 'далее'}")]
    else:
        parts.append(T(f"Enter {enter or 'choose'}", f"Enter {enter or 'выбрать'}"))
    if details:
        parts.append(T("→ details", "→ подробно"))
    parts.append(back or back_hint())
    if multi:
        parts.append(T("A all", "A все"))
    if search:
        parts.append(T("/ search", "/ поиск"))
    parts.append(T("Q quit", "Q выход"))
    return "  ".join(parts)


def compact(hint: str, cols: int) -> str:
    """Shorten a footer step by step until it fits, so no key drops off the end."""
    shorter = ((back_hint(), T("←/Bksp/Esc back", "←/Bksp/Esc назад")),
               (T("Space tick", "Пробел отметить"), T("Space ✓", "Пробел ✓")),
               (T("↑↓ move", "↑↓ выбор"), "↑↓"),
               (T("←/Bksp/Esc back", "←/Bksp/Esc назад"), T("← back", "← назад")),
               (T("  Q quit", "  Q выход"), "  Q"))
    for long, short in shorter:
        if width(hint) + 1 <= cols:
            break
        hint = hint.replace(long, short)
    return hint


def _move(vis: list[Item], cursor: int, delta: int) -> int:
    if not vis:
        return 0
    i = max(0, min(len(vis) - 1, cursor + delta))
    step = 1 if delta >= 0 else -1
    while 0 <= i < len(vis) and vis[i].header:
        i += step
    if not (0 <= i < len(vis)) or vis[i].header:
        i = cursor
        while 0 <= i < len(vis) and vis[i].header:
            i -= step
    return max(0, min(len(vis) - 1, i))


# ---- other widgets ------------------------------------------------------------------------

def view(term: Term, lines: list[Line], *, title: str = "", crumbs: str = "") -> None:
    """Scrollable text; any of Esc/Enter/q closes it."""
    offset = 0
    while True:
        cols, rows = term.size()
        body = wrap(lines, cols - 4)
        h = rows - 4
        offset = max(0, min(offset, max(0, len(body) - h)))
        out = header(title, crumbs, cols) + [""]
        out += ["  " + paint(b, cols - 4) for b in body[offset: offset + h]]
        out += [""] * (rows - 1 - len(out))
        more = T(f"  {offset + 1}–{min(len(body), offset + h)} of {len(body)}", f"  {offset + 1}–{min(len(body), offset + h)} из {len(body)}") if len(body) > h else ""
        out.append(footer(T(f"↑↓ PgUp PgDn scroll  {back_hint()}{more}", f"↑↓ PgUp PgDn прокрутка  {back_hint()}{more}"), cols))
        term.draw(out)
        k = term.key()
        if k in ("up", "k"):
            offset -= 1
        elif k in ("down", "j", "space"):
            offset += 1
        elif k == "pgup":
            offset -= h - 1
        elif k == "pgdn":
            offset += h - 1
        elif k == "home":
            offset = 0
        elif k == "end":
            offset = len(body)
        elif k in ("esc", "enter", "left", "backspace", "q", "Q", "й", "?"):
            return


def ask_text(term: Term, prompt: str, *, title: str = "", crumbs: str = "", default: str = "",
             note: list[Line] | None = None) -> str | None:
    """One-line input. Enter returns the text, Esc returns None."""
    text = default
    while True:
        cols, rows = term.size()
        out = header(title, crumbs, cols) + [""]
        out.append(" " + prompt)
        out.append(style(fit(" › " + text + "▏", cols), BOLD))
        out.append("")
        out += ["  " + paint(n, cols - 4) for n in wrap(note or [], cols - 4)]
        out += [""] * (rows - 1 - len(out))
        out.append(footer(T("Enter done  Del clear  Esc cancel  Backspace erase", "Enter готово  Del очистить  Esc отмена  Backspace стереть"), cols))
        term.draw(out)
        k = term.key()
        if k == "enter":
            return text.strip()
        if k == "esc":
            return None
        if k == "backspace":
            text = text[:-1]
        elif k in ("delete", "\x15"):
            text = ""
        elif k == "space":
            text += " "
        elif len(k) == 1 and k.isprintable():
            text += k


def message(term: Term, lines: list[Line], *, title: str = "", crumbs: str = "") -> None:
    view(term, lines, title=title, crumbs=crumbs)


# ---- form ---------------------------------------------------------------------------------------

@dataclass
class Field:
    """One row of a form.

    kind: text (type right away), choice (Enter opens a picker), toggle (Space flips),
    button (Enter submits; key "cancel" leaves without saving).
    """
    key: str
    label: str
    value: object = ""
    kind: str = "text"
    help: list[Line] = field(default_factory=list)
    show: Callable[[object], str] | None = None   # how a choice value reads in the row
    placeholder: str = ""
    hidden: bool = False


def form(term: Term, fields: list[Field], *, title: str = "", crumbs: str = "",
         pick: Callable[[Field], object] | None = None,
         changed: Callable[[Field], None] | None = None,
         check: Callable[[], dict[str, str]] | None = None,
         extra: Callable[[Field], list[Line]] | None = None,
         start: str | None = None) -> str | None:
    """Edit `fields` in place. Returns the pressed button key, or None when cancelled.

    Text fields take typing directly (Backspace erases, Del clears). `pick` returns a new value
    for a choice field or None to keep it; `changed` runs after any edit; `check` returns
    problems by field key and blocks the buttons (except "cancel") while there are any.
    """
    cursor = next((i for i, f in enumerate(fields) if f.key == start and not f.hidden), 0)
    errors: dict[str, str] = {}
    dirty = False
    leaving = False
    offset = 0

    def shown() -> list[int]:
        return [i for i, f in enumerate(fields) if not f.hidden]

    def move(delta: int) -> None:
        nonlocal cursor
        idx = shown()
        pos = idx.index(cursor) if cursor in idx else 0
        cursor = idx[max(0, min(len(idx) - 1, pos + delta))]

    while True:
        idx = shown()
        if cursor not in idx:
            cursor = idx[0]
        cols, rows = term.size()
        top = header(title, crumbs, cols) + [""]
        body_h = rows - len(top) - 1
        two_col = cols >= 96
        list_w = max(40, int(cols * 0.55)) if two_col else cols
        label_w = min(26, max(width(fields[i].label) for i in idx) + 1)
        rows_out: list[str] = []
        pos = {}
        for i in idx:
            f = fields[i]
            pos[i] = len(rows_out)
            on = i == cursor
            if f.kind == "button":
                if not rows_out or fields[idx[idx.index(i) - 1]].kind != "button":
                    rows_out.append("")
                text = f"{'›' if on else ' '} [ {f.label} ]"
                rows_out.append(style(fit(text, list_w), REV, BOLD) if on else fit(text, list_w))
                continue
            if f.kind == "toggle":
                val = T("[x] yes", "[x] да") if f.value else T("[ ] no", "[ ] нет")
            elif f.kind == "choice":
                val = (f.show(f.value) if f.show else str(f.value)) or f.placeholder
            else:
                val = " ".join(str(f.value).split())
                if on:
                    room = list_w - label_w - 5
                    val = ("…" + val[-(room - 2):] if width(val) > room - 1 else val) + "▏"
                elif not val:
                    val = f.placeholder
            mark = "!" if f.key in errors else ("›" if on else " ")
            row = f"{mark} {fit(f.label, label_w)} {fit(val, list_w - label_w - 3)}"
            if on:
                rows_out.append(style(fit(row, list_w), REV))
            elif f.key in errors:
                rows_out.append(style(fit(row, list_w), YELLOW))
            elif not str(f.value) and f.kind == "text":
                rows_out.append(style(fit(row, list_w), DIM))
            else:
                rows_out.append(fit(row, list_w))
        cur = fields[cursor]
        info: list[Line] = []
        if cur.key in errors:
            info += [(errors[cur.key], YELLOW), ("", "")]
        info += cur.help
        if cur.kind == "text" and width(str(cur.value)) > list_w - label_w - 6:
            info += [("", ""), (T("Now:", "Сейчас:"), DIM), (str(cur.value), "")]
        if extra:
            info += extra(cur)
        if errors and cur.kind == "button":
            info = [(T("Fix the fields marked «!»:", "Исправьте поля с «!»:"), YELLOW)] + [
                (f"• {next((f.label for f in fields if f.key == k), k)}: {v}", YELLOW)
                for k, v in errors.items()] + [("", "")] + info
        list_h = body_h if two_col else max(6, min(len(rows_out), body_h * 6 // 10))
        cur_row = pos.get(cursor, 0)
        if cur_row < offset:
            offset = cur_row
        if cur_row >= offset + list_h:
            offset = cur_row - list_h + 1
        offset = max(0, min(offset, max(0, len(rows_out) - list_h)))
        visible_rows = rows_out[offset: offset + list_h]
        visible_rows += [" " * list_w] * (list_h - len(visible_rows))
        lines = list(top)
        if two_col:
            dw = cols - list_w - 3
            panel = wrap(info, dw)[:list_h]
            panel += [("", "")] * (list_h - len(panel))
            for l, r in zip(visible_rows, panel):
                lines.append(fit(l, list_w) if not l.startswith("\x1b") else l)
                lines[-1] += style(" │ ", DIM) + paint(r, dw)
        else:
            lines += visible_rows
            lines.append(style("─" * cols, DIM))
            lines += [" " + paint(p, cols - 2) for p in wrap(info, cols - 2)[: body_h - list_h - 1]]
        lines = lines[: rows - 1]
        lines += [""] * (rows - 1 - len(lines))
        if leaving:
            hint = T("Unsaved changes: Esc again — leave without saving, any other key — stay", "Есть несохранённые изменения: Esc ещё раз — выйти без сохранения, любая клавиша — остаться")
        elif cur.kind == "text":
            hint = T("Type  Backspace erase  Del clear  ↑↓ field  Enter next  Esc cancel", "Печатайте  Backspace стереть  Del очистить  ↑↓ поле  Enter дальше  Esc отмена")
        elif cur.kind == "choice":
            hint = T("Enter/→ choose  ↑↓ field  Esc cancel", "Enter/→ выбрать  ↑↓ поле  Esc отмена")
        elif cur.kind == "toggle":
            hint = T("Space/←→ yes/no  ↑↓ field  Esc cancel", "Пробел/←→ да/нет  ↑↓ поле  Esc отмена")
        else:
            hint = T("Enter press  ↑↓ field  Esc cancel", "Enter нажать  ↑↓ поле  Esc отмена")
        lines.append(footer(hint, cols))
        term.draw(lines)

        k = term.key()
        if leaving:
            if k == "esc":
                return None
            leaving = False
            continue
        if k in ("up", "shift-tab"):
            move(-1)
        elif k in ("down", "tab"):
            move(1)
        elif k == "pgup":
            move(-5)
        elif k == "pgdn":
            move(5)
        elif k == "esc":
            if dirty:
                leaving = True
            else:
                return None
        elif cur.kind == "text":
            before = cur.value
            if k == "enter":
                move(1)
            elif k == "backspace":
                cur.value = str(cur.value)[:-1]
            elif k in ("delete", "\x15"):
                cur.value = ""
            elif k == "space":
                cur.value = str(cur.value) + " "
            elif len(k) == 1 and k.isprintable():
                cur.value = str(cur.value) + k
            if cur.value != before:
                dirty = True
                if changed:
                    changed(cur)
                if errors and check:
                    errors = check()
        elif cur.kind == "toggle" and k in ("space", "enter", "left", "right"):
            cur.value = not cur.value
            dirty = True
            if changed:
                changed(cur)
        elif cur.kind == "choice" and k in ("enter", "space", "right") and pick:
            new = pick(cur)
            if new is not None and new != cur.value:
                cur.value = new
                dirty = True
                if changed:
                    changed(cur)
                if errors and check:
                    errors = check()
        elif cur.kind == "button" and k == "enter":
            if cur.key == "cancel":
                if dirty:
                    leaving = True
                    continue
                return None
            errors = check() if check else {}
            if not errors:
                return cur.key
            bad = next((i for i in idx if fields[i].key in errors), None)
            if bad is not None:
                cursor = bad
