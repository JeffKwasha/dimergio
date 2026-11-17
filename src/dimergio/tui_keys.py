from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

_CSI_U_KEYS = {
    "1": "\x1b[H",   # Home
    "4": "\x1b[F",   # End
    "5": "\x1b[5~",  # PageUp
    "6": "\x1b[6~",  # PageDown
    "9": "\t",       # Tab
    "13": "\n",      # Enter
    "27": "\x1b",    # Esc
    "32": " ",       # Space
}


def _csiu_to_key(code: str) -> str:
    """Map a kitty CSI-u key code (e.g. ``27`` from ``\x1b[27;5u``) to the
    canonical readchar key. Unknown codes pass through as ``\x1b[<code>u``."""
    base = code.split(";", 1)[0]
    return _CSI_U_KEYS.get(base, "\x1b[" + base + "u")


def normalize_key(seq: str) -> str:
    """Normalize any terminal's escape-sequence dialect to the canonical
    readchar key constants the handlers compare against.

    Covers the common modes dimergio can be launched under: plain CSI arrows
    (``\x1b[A``), application-cursor mode (SS3 ``\x1bOA``), xterm alternate
    Home/End (``\x1b[1~``/``\x1b[4~``), modified arrows (``\x1b[1;5A``), and
    the kitty keyboard protocol (CSI-u, e.g. ``\x1b[1;1A`` / ``\x1b[27u``).
    Modifiers are stripped so Shift/Ctrl+arrow still navigates/sorts.
    """
    if len(seq) < 3 or not seq.startswith("\x1b"):
        return seq
    if seq.startswith("\x1bO"):
        # SS3/application-cursor mode: ESC O <letter> → ESC [ <letter>.
        return "\x1b[" + seq[2:]
    if not seq.startswith("\x1b["):
        return seq
    body = seq[2:]
    if body.endswith("u"):
        # Kitty CSI-u: ESC [ <code> [; <modifier>] u.
        return _csiu_to_key(body[:-1])
    final = body[-1]
    if final in "ABCDHF":
        # Arrows / Home / End, possibly with a parameter prefix (kitty
        # ``\x1b[1;1A``, xterm ``\x1b[1;5A``, DEC ``\x1b[1A``) — the plain
        # key is enough for navigation/sort.
        return "\x1b[" + final
    if final == "~":
        # ESC [ <num> [; <mod>] ~  (PageUp/Down, alternate Home/End).
        nums = [p for p in body[:-1].split(";") if p.isdigit()]
        num = nums[0] if nums else ""
        if num in ("1", "7"):
            return "\x1b[H"
        if num in ("4", "8"):
            return "\x1b[F"
        if num in ("5",):
            return "\x1b[5~"
        if num in ("6",):
            return "\x1b[6~"
    return seq


# terminfo capabilities whose sequences are mapped to the canonical keys.
# Capability → canonical readchar key (the values in Keys.NAV etc.).
_TERMINFO_KEY_CAPS = {
    "kcub1": "\x1b[D",   # cursor left
    "kcuf1": "\x1b[C",   # cursor right
    "kcuu1": "\x1b[A",   # cursor up
    "kcud1": "\x1b[B",   # cursor down
    "khome": "\x1b[H",   # home
    "kend":  "\x1b[F",   # end
    "kpp":   "\x1b[5~",  # page up
    "knp":   "\x1b[6~",  # page down
}


def decode_terminfo_escapes(val: str) -> str:
    """Decode infocmp's escaped capability string (``\\E[D``, ``^X``, ``\\s``,
    octal ``\\023``, ``\\\\``) into the literal byte string."""
    out: list[str] = []
    i = 0
    while i < len(val):
        c = val[i]
        if c == "\\" and i + 1 < len(val):
            n = val[i + 1]
            if n in "01234567":
                j = i + 1
                while j < len(val) and j < i + 4 and val[j] in "01234567":
                    j += 1
                out.append(chr(int(val[i + 1 : j], 8)))
                i = j
                continue
            simple = {"E": "\x1b", "s": " ", "\\": "\\", "n": "\n",
                      "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\x00"}
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
        if c == "^" and i + 1 < len(val):
            out.append(chr(ord(val[i + 1]) & 0x1F))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def terminfo_keymap() -> dict[str, str] | None:
    """Resolve the current terminal's key sequences from its terminfo entry.

    Broad compatibility: instead of assuming one escape-sequence dialect, we
    ask ``infocmp`` what THIS terminal actually sends for each navigation key
    and map those bytes onto the canonical keys the handlers compare against.

    Returns ``{raw_sequence: canonical_key}``, or ``None`` when the lookup is
    impossible (no ``TERM``, no ``infocmp``, unknown terminal) — in which case
    the caller falls back to the built-in ``normalize_key`` dialect table,
    which is kitty-compatible.
    """
    term = os.environ.get("TERM", "")
    if not term or shutil.which("infocmp") is None:
        return None
    try:
        out = subprocess.run(
            ["infocmp", "-1", term],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    keymap: dict[str, str] = {}
    for line in out.stdout.splitlines():
        line = line.strip().rstrip(",")
        name, sep, val = line.partition("=")
        if not sep or name not in _TERMINFO_KEY_CAPS:
            continue
        seq = decode_terminfo_escapes(val)
        if seq:
            keymap[seq] = _TERMINFO_KEY_CAPS[name]
    return keymap or None


def read_key(
    read: Callable[[], str | None],
    wait: Callable[[], bool],
    keymap: dict[str, str] | None = None,
) -> str | None:
    """Assemble one keystroke from raw bytes without a blocking read after ESC.

    A terminal sends a bare ESC for the Esc key, so waiting for a follow-up
    byte forever swallows it and lets the next keypress complete a phantom
    escape sequence. ``wait`` reports whether another byte is available within
    the escape timeout; a lone ESC is returned as-is.
    """
    ch = read()
    if not ch:
        return None
    if ch != "\x1b":
        return ch
    if not wait():
        return ch

    ch2 = read()
    if ch2 is None:
        return ch
    if ch2 not in "\x4f\x5b":
        return ch + ch2

    seq = ch + ch2
    if ch2 == "\x4f":
        # SS3/application-cursor mode: ESC O <final>.
        if wait():
            nxt = read()
            if nxt:
                seq += nxt
    else:
        # CSI: consume parameters (digits/;) until the final byte.
        while wait():
            c = read()
            if not c:
                break
            seq += c
            if c not in "0123456789;":
                break

    if keymap and seq in keymap:
        return keymap[seq]
    return normalize_key(seq)


class Keys:
    """Single source of truth for every interactive keybinding.

    All key constants, lookup maps, and the on-screen help hints live here so
    the handlers and the rendered legend can never drift apart. Built once per
    session (needs ``readchar.key`` constants, imported lazily).
    """

    def __init__(self) -> None:
        from readchar import key as k

        self.QUIT = ("q", "Q")
        self.ENTER = k.ENTER
        self.ESC = k.ESC
        self.SPACE = k.SPACE
        self.SORT_LEFT = k.LEFT
        self.SORT_RIGHT = k.RIGHT
        self.CLEAR_MARK = "-"
        self.CLEAR_STATS = "c"
        self.SHOW_EXITED = "s"
        self.SAMPLE_DOWN = "["
        self.SAMPLE_UP = "]"
        self.NAND = "M"
        self.MMAP = "m"

        # Arrow keys navigate within a list, Left/Right re-sort whichever panel
        # is focused, and Tab/Home/End switch focus between the two lists.
        self.FOCUS_TOGGLE = k.TAB
        self.FOCUS_PROCS = k.HOME
        self.FOCUS_FILES = k.END

        # Arrow/paging keys → navigation verbs understood by apply_nav.
        self.NAV = {
            k.UP: "up",
            k.DOWN: "down",
            k.PAGE_UP: "page_up",
            k.PAGE_DOWN: "page_down",
        }

        # Sort columns cycled with Left/Right; order defines rotation.
        self.SORT = ("iowait_per_mb", "iowait", "reads")
        self.PROC_SORT = ("mmap", "reads", "iowait")

        # Help hints rendered as panel subtitles. Kept next to the bindings so
        # a change to a key forces a change to its documentation.
        self.BROWSE_HINT = (
            "↑↓:scroll  ←/→:sort  Tab:files/procs  Space:mark  "
            "Enter:review  -:clear  c:clear stats  s:show-exited  "
            "m:mmap []:sample  M:nand  q:quit"
        )
        self.PREVIEW_HINT = "Enter: execute  Esc: back  q: quit"


def apply_nav(scroll: int, selected: int, n: int, max_vis: int, key: str) -> tuple[int, int]:
    """Pure list navigation.

    Returns the new (scroll, selected) given the current values, the total
    number of rows ``n``, the visible window size ``max_vis`` and a navigation
    verb from ``Keys.NAV``.

    Unknown keys leave state unchanged. This is the single source of truth for
    scrolling in both panels, so navigation behaves identically everywhere and
    can be unit-tested without a terminal.
    """
    if n <= 0:
        return 0, 0
    if key == "up":
        selected = max(0, selected - 1)
        if selected < scroll:
            scroll = selected
    elif key == "down":
        selected = min(n - 1, selected + 1)
        if selected >= scroll + max_vis:
            scroll = selected - max_vis + 1
    elif key == "page_up":
        scroll = max(0, scroll - max_vis)
        selected = scroll
    elif key == "page_down":
        scroll = min(max(0, n - max_vis), scroll + max_vis)
        selected = min(n - 1, scroll + max_vis - 1)
    return scroll, selected
