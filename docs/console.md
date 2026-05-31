# Console

`from rich.console import Console`

Central output handler. Auto-detects terminal size, color system, and encoding.

## Constructor

```python
Console(
    color_system="auto",     # "auto", "standard", "256", "truecolor", None
    force_terminal=None,     # True/False to override TTY detection
    force_interactive=None,  # True/False to override interactive mode
    soft_wrap=False,         # default soft-wrap for print()
    stderr=False,            # write to stderr instead of stdout
    file=None,               # file-like object to write to
    width=None,              # override terminal width
    height=None,             # override terminal height
    style=None,              # default style for all output
    no_color=None,           # disable color
    markup=True,             # enable console markup by default
    emoji=True,              # enable emoji codes by default
    highlight=True,          # auto-highlight content
    record=False,            # enable SVG/HTML export
    tab_size=8,
    log_time=True,
    log_path=True,
    safe_box=True,           # ASCII fallback for legacy Windows
)
```

## Key methods

| Method | Purpose |
|---|---|
| `console.print(*args, style=, justify=, overflow=)` | Print renderables with markup parsing |
| `console.log(*args, style=)` | Print with timestamp + source location |
| `console.input(prompt)` | Read input with Rich markup in prompt |
| `console.out(*args, style=)` | Low-level, no markup/wrapping |
| `console.rule(title, style=, align=)` | Horizontal divider line |
| `console.status(msg, spinner=)` | Context manager for spinner |
| `console.screen(hide_cursor=, style=)` | Context manager for alternate screen |
| `console.capture()` | Context manager to capture output as string |
| `console.clear()` | Clear screen |
| `console.size` | Terminal `(width, height)` tuple |
| `console.is_terminal` | Bool — True if writing to TTY |

## Group

`from rich.console import Group`

Combines multiple renderables vertically into one renderable:

```python
Group(header, table, status)
```

## @group() decorator

`from rich.console import group`

For dynamic content — wraps a generator that yields renderables.

```python
@group()
def get_content():
    yield Text("header")
    yield table
    yield Text("footer")
```

## Console Markup

`[bold red]text[/bold red]` — style tags. Close with `[/]` for last style.
Emoji via `:emoji_name:`. Escape literal `[` with backslash.
