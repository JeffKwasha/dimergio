"""Key-handling tests for the interactive TUI.

The historical bug: the TUI hand-rolled a byte-by-byte escape-sequence
parser with tight timeouts, so a down-arrow's ``\x1b[B`` could be misread
as a lone ``\x1b`` (Esc) and kick the user back to MONITOR from SELECT.

The fix delegates key parsing to ``readchar.readkey()``, which returns
complete, unambiguous keystroke strings, and routes all scrolling through
the pure ``_apply_nav`` helper. The MONITOR and SELECT screens were merged
into a single BROWSE screen with continuous sampling, and fatrace's process
and reader thread are now owned together by ``start_fatrace``/
``stop_fatrace`` (so a respawn can never leave a running fatrace with no
reader). These tests lock that behavior in without needing a real terminal.
"""

import shutil
import subprocess
import threading
from unittest import mock

from dimergio.collector import Collector, _apply_nav


def _make_collector() -> Collector:
    """Build a Collector with a minimal in-memory pool (no real I/O)."""
    from dimergio.model import Branch, Pool

    branches = [
        Branch(path=__import__("pathlib").Path("/hdd"), device="", rotational=True),
        Branch(path=__import__("pathlib").Path("/ssd"), device="", rotational=False),
    ]
    pool = Pool(mount=__import__("pathlib").Path("/pool"), name="POOL", branches=branches)
    return Collector(pool=pool, data_path=__import__("pathlib").Path("/pool"))


# ─── readchar disambiguates arrows from Esc ─────────────────────────
def test_readchar_distinguishes_arrow_from_esc():
    """The core invariant that prevents the MONITOR/SELECT bounce bug.

    ``readchar`` reports a down-arrow as the full multi-byte sequence
    ``"\\x1b[B"`` and a lone Escape as ``"\\x1b"``. Because the two are
    different strings, the parser can never misread an arrow as Esc — the
    old hand-rolled parser's failure mode. (readkey() itself needs a real
    TTY, so we assert on the canonical sequences readchar publishes.)
    """
    from readchar import key

    assert key.DOWN == "\x1b[B"
    assert key.UP == "\x1b[A"
    assert key.ESC == "\x1b"
    assert key.DOWN != key.ESC
    assert key.UP != key.ESC
    # A down-arrow string starts with ESC but is strictly longer, so a
    # prefix-only match (the old bug) would be wrong.
    assert not key.DOWN.startswith(key.ESC) or len(key.DOWN) > len(key.ESC)


def test_readchar_enter_is_lf():
    # The handlers compare against key.ENTER, which is "\n" (LF).
    from readchar import key

    assert key.ENTER == "\n"


# ─── escape-sequence dialect normalization ───────────────────────────
def test_normalize_key_canonical_forms_pass_through():
    """The canonical CSI arrow/home/end sequences are unchanged, so every
    dialect maps onto them and readchar's constants stay the source of truth."""
    from dimergio.collector import _normalize_key

    assert _normalize_key("\x1b[A") == "\x1b[A"
    assert _normalize_key("\x1b[B") == "\x1b[B"
    assert _normalize_key("\x1b[C") == "\x1b[C"
    assert _normalize_key("\x1b[D") == "\x1b[D"
    assert _normalize_key("\x1b[H") == "\x1b[H"
    assert _normalize_key("\x1b[F") == "\x1b[F"


def test_normalize_key_application_cursor_mode():
    """Terminals running with application-cursor (DECCKM) send SS3 ``\x1bO*``;
    these must decode to the same keys as plain CSI arrows."""
    from dimergio.collector import _normalize_key

    assert _normalize_key("\x1bOA") == "\x1b[A"
    assert _normalize_key("\x1bOB") == "\x1b[B"
    assert _normalize_key("\x1bOC") == "\x1b[C"
    assert _normalize_key("\x1bOD") == "\x1b[D"
    assert _normalize_key("\x1bOH") == "\x1b[H"
    assert _normalize_key("\x1bOF") == "\x1b[F"


def test_normalize_key_kitty_and_modified_arrows():
    """The kitty keyboard protocol (and xterm modifier encodings) prefix
    arrows with parameters; modifiers are stripped for navigation/sort."""
    from dimergio.collector import _normalize_key

    assert _normalize_key("\x1b[1;1A") == "\x1b[A"  # kitty plain up
    assert _normalize_key("\x1b[1;2A") == "\x1b[A"  # kitty shift-up
    assert _normalize_key("\x1b[1;5C") == "\x1b[C"  # kitty ctrl-right
    assert _normalize_key("\x1b[1A") == "\x1b[A"    # DEC prefix form
    assert _normalize_key("\x1b[1;5B") == "\x1b[B"  # xterm ctrl-down


def test_normalize_key_alternate_home_end_page():
    """xterm alternate (application-keypad) Home/End and the page keys decode
    to the canonical sequences."""
    from dimergio.collector import _normalize_key

    assert _normalize_key("\x1b[1~") == "\x1b[H"
    assert _normalize_key("\x1b[7~") == "\x1b[H"
    assert _normalize_key("\x1b[4~") == "\x1b[F"
    assert _normalize_key("\x1b[8~") == "\x1b[F"
    assert _normalize_key("\x1b[5~") == "\x1b[5~"
    assert _normalize_key("\x1b[6~") == "\x1b[6~"
    assert _normalize_key("\x1b[5;2~") == "\x1b[5~"


def test_normalize_key_csi_u_codes():
    """The kitty CSI-u protocol encodes plain keys as ``\x1b[<code>u``."""
    from dimergio.collector import _normalize_key

    assert _normalize_key("\x1b[27u") == "\x1b"
    assert _normalize_key("\x1b[13u") == "\n"
    assert _normalize_key("\x1b[9u") == "\t"
    assert _normalize_key("\x1b[32u") == " "
    assert _normalize_key("\x1b[1u") == "\x1b[H"
    assert _normalize_key("\x1b[4u") == "\x1b[F"
    assert _normalize_key("\x1b[27;5u") == "\x1b"


def test_normalize_key_ordinary_keys_unchanged():
    """Letters, Tab, Enter and lone Esc must pass through untouched."""
    from dimergio.collector import _normalize_key

    assert _normalize_key("q") == "q"
    assert _normalize_key("\t") == "\t"
    assert _normalize_key("\n") == "\n"
    assert _normalize_key("\x1b") == "\x1b"
    assert _normalize_key(" ") == " "


# ─── terminfo resolution (broad compatibility) ───────────────────────
_FAKE_INFOCMP = "\n".join([
    "\tkcub1=\\EOD,",
    "\tkcud1=\\EOB,",
    "\tkcuf1=\\EOC,",
    "\tkcuu1=\\EOA,",
    "\tkend=\\EOF,",
    "\tkhome=\\EOH,",
    "\tknp=\\E[6~,",
    "\tkpp=\\E[5~,",
    "\tka1=\\EOP,",   # F1-style app-keypad: ignored (not a navigation cap)
])


def _fake_run(args, **_kwargs):
    return subprocess.CompletedProcess(args, 0, stdout=_FAKE_INFOCMP, stderr="")


def test_decode_terminfo_escapes():
    from dimergio.collector import _decode_terminfo_escapes

    assert _decode_terminfo_escapes(r"\E[D") == "\x1b[D"
    assert _decode_terminfo_escapes(r"\E[1~") == "\x1b[1~"
    assert _decode_terminfo_escapes(r"^I") == "\t"
    assert _decode_terminfo_escapes(r"\s") == " "
    assert _decode_terminfo_escapes(r"\E\023") == "\x1b\x13"


def test_terminfo_keymap_uses_infocmp_for_current_term():
    """Broad compatibility: key sequences come from the *current* $TERM's
    terminfo entry, not from a fixed table."""
    import os

    from dimergio.collector import _terminfo_keymap

    with mock.patch.object(os.environ, "get", return_value="xterm-256color"), \
            mock.patch.object(shutil, "which", return_value="/usr/bin/infocmp"), \
            mock.patch.object(subprocess, "run", side_effect=_fake_run):
        keymap = _terminfo_keymap()
    assert keymap == {
        "\x1bOD": "\x1b[D", "\x1bOB": "\x1b[B", "\x1bOC": "\x1b[C",
        "\x1bOA": "\x1b[A", "\x1bOF": "\x1b[F", "\x1bOH": "\x1b[H",
        "\x1b[6~": "\x1b[6~", "\x1b[5~": "\x1b[5~",
    }


def test_terminfo_keymap_falls_back_to_none():
    """No TERM, missing infocmp, or an unknown terminal must yield None so the
    kitty-compatible dialect normalizer is used instead."""
    import os

    from dimergio.collector import _terminfo_keymap

    with mock.patch.object(os.environ, "get", return_value=""):
        assert _terminfo_keymap() is None
    with mock.patch.object(os.environ, "get", return_value="xterm-256color"), \
            mock.patch.object(shutil, "which", return_value=None):
        assert _terminfo_keymap() is None
    with mock.patch.object(os.environ, "get", return_value="weirdterm"), \
            mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")), \
            mock.patch.object(shutil, "which", return_value="/usr/bin/infocmp"):
        assert _terminfo_keymap() is None


# ─── _apply_nav: the shared navigation source of truth ──────────────
def test_nav_up_down_clamped():
    assert _apply_nav(0, 0, 10, 5, "down") == (0, 1)
    assert _apply_nav(0, 0, 10, 5, "up") == (0, 0)  # clamp at top
    assert _apply_nav(0, 9, 10, 5, "down") == (5, 9)  # clamp at bottom + scroll


def test_nav_scroll_follows_selection():
    # Selected moves past the visible window -> scroll follows.
    assert _apply_nav(0, 4, 10, 5, "down") == (1, 5)
    assert _apply_nav(0, 5, 10, 5, "down") == (2, 6)
    # Moving up above the scroll origin pulls scroll back.
    assert _apply_nav(1, 1, 10, 5, "up") == (0, 0)


def test_nav_page_home_end():
    assert _apply_nav(0, 0, 10, 5, "page_down") == (5, 9)
    assert _apply_nav(5, 9, 10, 5, "page_up") == (0, 0)
    # Home/End no longer scroll lists — they switch focus between the file
    # and process lists, so _apply_nav ignores them.
    assert _apply_nav(0, 0, 10, 5, "end") == (0, 0)
    assert _apply_nav(5, 9, 10, 5, "home") == (5, 9)


def test_nav_unknown_key_leaves_state_unchanged():
    assert _apply_nav(3, 3, 10, 5, "not-a-key") == (3, 3)


def test_nav_empty_list_is_safe():
    assert _apply_nav(0, 0, 0, 5, "down") == (0, 0)
    assert _apply_nav(0, 0, 0, 5, "page_down") == (0, 0)


# ─── Navigation keys are identical in monitor and select modes ──────
def test_nav_kind_mapping_is_complete():
    """Every navigation verb _apply_nav knows is bound in the _Keys map."""
    from dimergio.collector import _Keys

    nav_kind = _Keys().NAV
    for kind in ("up", "down", "page_up", "page_down"):
        assert any(v == kind for v in nav_kind.values())


def test_keys_is_single_source_of_truth():
    """Bindings live only on _Keys; handlers/legend must not hardcode keys.

    Guards the DRY refactor: the interactive handlers reference ``KEYS.*`` and
    the on-screen hints come from ``_Keys`` rather than duplicated literals.
    """
    import inspect

    from dimergio.collector import Collector, _Keys

    keys = _Keys()
    assert keys.SORT[0] == "iowait_per_mb"
    assert keys.ENTER and keys.ESC and keys.SPACE
    assert keys.TAB == "\t"
    assert keys.PROC_SORT == ("mmap", "reads", "iowait")

    src = inspect.getsource(Collector._run_interactive)
    # The old scattered lookup tables must be gone.
    assert "_SHIFT_MAP" not in src
    assert "_SORT_KEYS" not in src
    assert "_NAV_KIND" not in src
    # Handlers dispatch through the shared bindings object.
    assert "KEYS.ENTER" in src
    assert "KEYS.NAV" in src
    assert "KEYS.FOCUS_TOGGLE" in src
    assert "KEYS.PROC_SORT" in src
    # Legend hints are sourced from _Keys, not inline strings.
    assert "KEYS.BROWSE_HINT" in src
    assert "KEYS.PREVIEW_HINT" in src


def test_no_digit_mark_keys():
    """0-9 / Shift+0-9 mark shortcuts are removed — SPACE on the selected row
    is the only way to cycle a file's target branch."""
    import inspect

    from dimergio.collector import Collector, _Keys

    assert not hasattr(_Keys(), "SHIFT_DIGIT")
    src = inspect.getsource(Collector._run_interactive)
    assert "SHIFT_DIGIT" not in src
    assert "key.isdigit()" not in src
    # SPACE (mark the selected file) remains the marking gesture.
    assert "KEYS.SPACE" in src


def test_tab_toggles_focus_and_left_right_sort_focused_panel():
    """TAB switches files↔procs; Left/Right re-sort the *focused* panel, so
    the procs panel gets its own sort cycle while files keep theirs."""
    import inspect

    from dimergio.collector import Collector

    src = inspect.getsource(Collector._run_interactive)
    assert "KEYS.FOCUS_TOGGLE" in src
    # Sort handlers act on the focused panel (proc_sort vs sort_key).
    assert "proc_sort = _cycle_sort_key" in src
    assert "KEYS.PROC_SORT" in src


def test_empty_procs_list_falls_through_to_files():
    """Focus on an empty process list must not swallow arrows — it falls back
    to the file list so navigation/sort never feel dead."""
    import inspect

    from dimergio.collector import Collector

    src = inspect.getsource(Collector._run_interactive)
    assert "if not _proc_visible_rows():" in src
    assert 'focus = "files"' in src
    assert 'focus == "procs"' in src


# ─── fatrace lifecycle owns proc + reader thread together ───────────
def test_start_stop_fatrace_manages_proc_and_thread():
    """Regression test for the old MONITOR↔SELECT desync bug.

    Previously ESC-in-select respawned fatrace but spun up no reader thread,
    so accumulation silently froze. Now ``start_fatrace`` always creates both
    proc and thread together, and ``stop_fatrace`` tears both down.
    """
    collector = _make_collector()

    # Fake fatrace process whose stdout blocks until the proc is stopped,
    # so the reader thread stays alive for the duration of the test.
    stop = threading.Event()
    fake_stdout = mock.MagicMock()

    def _blocking_lines():
        while not stop.is_set():
            yield b"fake fatrace line\n"

    fake_stdout.__iter__.return_value = _blocking_lines()
    fake_proc = mock.MagicMock()
    fake_proc.stdout = fake_stdout
    # Mimic a real fatrace: terminating the process ends the stdout stream,
    # which unwinds the reader's `for raw in proc.stdout` loop.
    fake_proc.terminate.side_effect = stop.set

    sampler = mock.MagicMock()

    with mock.patch.object(subprocess, "Popen", return_value=fake_proc):
        # Starting twice is a no-op (idempotent).
        collector.start_fatrace(sampler)
        first_proc = collector._fatrace_proc
        first_thread = collector._fatrace_thread
        assert first_proc is not None
        assert first_thread is not None
        assert first_thread.is_alive()
        collector.start_fatrace(sampler)
        assert collector._fatrace_proc is first_proc

        # Stopping clears both, and joins the reader thread.
        collector.stop_fatrace()
        assert collector._fatrace_proc is None
        assert collector._fatrace_thread is None
        assert not first_thread.is_alive()


def test_stop_fatrace_is_safe_when_not_running():
    collector = _make_collector()
    # Should not raise when nothing is running.
    collector.stop_fatrace()
    assert collector._fatrace_proc is None


# ─── Merged BROWSE screen: single dispatch, no mode split ──────────
def test_run_interactive_no_separate_monitor_select_dispatch():
    """The merged screen must not retain the old monitor/select split.

    Confirm the implementation no longer references a ``mode``/``monitoring``
    split and routes everything through one browse handler plus a preview
    handler. We check the source text so a regression (re-adding the split)
    is caught even without a TTY.
    """
    import inspect

    from dimergio.collector import Collector

    src = inspect.getsource(Collector._run_interactive)
    # The old per-mode handlers must be gone.
    assert "_handle_monitor_key" not in src
    assert "_handle_select_key" not in src
    # The merged handlers must be present.
    assert "_handle_browse_key" in src
    assert "_handle_preview_key" in src
    # No leftover 'mode = "monitor"' / 'mode == "select"' style dispatch.
    assert 'mode == "monitor"' not in src
    assert 'mode == "select"' not in src


# ─── terminal is left clean on exit ─────────────────────────────────


def test_run_interactive_refuses_pipe():
    """Without a TTY on stdin, refuse and never build a Live display."""
    c = _make_collector()
    sampler = mock.MagicMock()

    with mock.patch("sys.stdin") as stdin, mock.patch("rich.live.Live") as live:
        stdin.isatty.return_value = False
        c._run_interactive(sampler=sampler)

    live.assert_not_called()


def test_run_interactive_restores_termios_on_exit():
    """The reader thread toggles raw/no-echo; exit must restore termios.

    Source-level guard: saved attributes are captured before the Live loop
    and handed back to ``termios.tcsetattr`` in the ``finally`` so the
    terminal is never left with ECHO off (invisible cursor/text) after quit.
    """
    import inspect

    from dimergio.collector import Collector

    src = inspect.getsource(Collector._run_interactive)
    assert "termios.tcgetattr" in src
    assert "termios.tcsetattr" in src
    assert 'sys.stdin.isatty' in src


def test_browse_enter_can_flip_in_preview():
    """ENTER must actually switch to preview/review mode.

    Regression: ``_handle_browse_key`` assigned ``in_preview = True`` without
    a ``nonlocal in_preview`` declaration, so the assignment created a local
    and the outer flag never flipped — ENTER appeared to do nothing.
    """
    import ast
    import inspect
    import textwrap

    from dimergio.collector import Collector

    src = textwrap.dedent(inspect.getsource(Collector._run_interactive))
    tree = ast.parse(src)
    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_browse_key"
    )
    nonlocals = {name for n in handler.body if isinstance(n, ast.Nonlocal) for name in n.names}
    assigns_in_preview = any(
        isinstance(n, ast.Name) and n.id == "in_preview" and isinstance(n.ctx, ast.Store)
        for n in ast.walk(handler)
    )
    assert assigns_in_preview, "browse handler should be able to enter preview"
    assert "in_preview" in nonlocals, "in_preview must be nonlocal or the flag never flips"
