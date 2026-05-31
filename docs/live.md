# Live

`from rich.live import Live`

Animated terminal display for real-time updates. Used as a context manager.

## Constructor

```python
Live(
    renderable=None,              # Initial renderable (NOT callable — use get_renderable for that)
    console=None,                 # Console instance (creates one if None)
    screen=False,                 # use alternate screen (full-screen mode)
    refresh_per_second=4,         # auto-refresh rate
    auto_refresh=True,            # disable if updates are manual
    transient=False,              # clear on exit (no effect when screen=True)
    redirect_stdout=True,         # capture print() to avoid messing display
    redirect_stderr=True,         # capture stderr
    vertical_overflow="ellipsis", # "crop", "ellipsis", "visible"
    get_renderable=None,          # Callable[[], RenderableType] — called on each refresh
)
```

## Auto-refresh with callable (recommended for dynamic content)

Pass a zero-argument callable as `get_renderable=` to have it called on every
auto-refresh tick. No manual `live.update()` needed.

```python
def make_table() -> Table:
    table = Table()
    table.add_column("Value")
    table.add_row(str(random.random()))
    return table

with Live(screen=True, refresh_per_second=4, get_renderable=make_table):
    time.sleep(10)
```

## Static renderable — update manually

Pass a renderable as the first positional arg, then mutate it or call
`live.update()`:

```python
table = Table()
table.add_column("Row")
with Live(table, refresh_per_second=4, screen=True) as live:
    for row in range(12):
        time.sleep(0.4)
        table.add_row(f"{row}")            # mutate in-place
        # or: live.update(Table(...))      # replace entirely
```

## Key methods

| Method | Purpose |
|---|---|
| `live.update(renderable, refresh=True)` | Replace displayed renderable |
| `live.refresh()` | Force re-render |
| `live.start(refresh=False)` | Start rendering (called by __enter__) |
| `live.stop()` | Stop rendering (called by __exit__) |
| `live.console` | Console object for logging above live area |

## Important notes

- `screen=True` enters/exits alternate screen automatically (`\033[?1049h`/`\033[?1049l`)
- **Do NOT** pass a callable/function as the first positional `renderable` argument.
  Use the separate `get_renderable=` parameter instead.
- If the initial renderable raises during `__enter__`, the alt screen is entered
  but nothing is rendered → black screen.
- `tty.setraw()` or raw termios changes affect output. Rich uses `\n` in its
  rendered output, which relies on `OPOST` being enabled for `\n` → `\r\n`
  conversion. Keep termios `oflag` untouched.
