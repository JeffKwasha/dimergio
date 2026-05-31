# Panel

`from rich.panel import Panel`

Draws a border around any renderable.

## Constructor

```python
Panel(
    renderable,                   # content inside the border (any renderable)
    title=None,                   # text drawn on top border line
    subtitle=None,                # text drawn on bottom border line
    box=ROUNDED,                  # box style from rich.box
    style="",                     # style for panel border
    title_align="center",         # "left", "center", "right"
    subtitle_align="center",      # "left", "center", "right"
    border_style="",              # style for border only (not content)
    padding=(1, 1),              # (top, right, bottom, left) or int
    expand=True,                  # True = full terminal width, False = fit content
    width=None,                   # exact width in chars
    highlight=False,              # auto-highlight content
)
```

## Usage

```python
Panel(table, title="dimergio", subtitle="q:quit", border_style="dim")
Panel.fit("hello")                # fit content width (expand=False)
Panel("content", box=box.SIMPLE)  # change border style
```

## Nesting

Use `Group` to put multiple renderables inside one Panel:

```python
Panel(Group(header, table, status), title="...")
```

## Notes

- `expand=True` (default) stretches to terminal width
- `expand=False` or `Panel.fit()` fits to content width
- `title` and `subtitle` use string markup
- `border_style="dim"` only affects the border, not the content
