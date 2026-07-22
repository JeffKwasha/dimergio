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
    assert _apply_nav(0, 0, 10, 5, "end") == (5, 9)
    assert _apply_nav(5, 9, 10, 5, "home") == (0, 0)


def test_nav_unknown_key_leaves_state_unchanged():
    assert _apply_nav(3, 3, 10, 5, "not-a-key") == (3, 3)


def test_nav_empty_list_is_safe():
    assert _apply_nav(0, 0, 0, 5, "down") == (0, 0)
    assert _apply_nav(0, 0, 0, 5, "end") == (0, 0)


# ─── Navigation keys are identical in monitor and select modes ──────
def test_nav_kind_mapping_is_complete():
    """Every navigation verb _apply_nav knows is bound in the _Keys map."""
    from dimergio.collector import _Keys

    nav_kind = _Keys().NAV
    for kind in ("up", "down", "page_up", "page_down", "home", "end"):
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
    assert len(keys.SHIFT_DIGIT) == 10

    src = inspect.getsource(Collector._run_interactive)
    # The old scattered lookup tables must be gone.
    assert "_SHIFT_MAP" not in src
    assert "_SORT_KEYS" not in src
    assert "_NAV_KIND" not in src
    # Handlers dispatch through the shared bindings object.
    assert "KEYS.ENTER" in src
    assert "KEYS.SHIFT_DIGIT" in src
    assert "KEYS.NAV" in src
    # Legend hints are sourced from _Keys, not inline strings.
    assert "KEYS.BROWSE_HINT" in src
    assert "KEYS.PREVIEW_HINT" in src


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
