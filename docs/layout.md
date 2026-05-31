# Layout

`from rich.layout import Layout`

Splits the terminal screen into named regions for full-screen apps.

## Basic usage

```python
layout = Layout()
layout.split_column(
    Layout(name="header", size=3),
    Layout(name="body"),
    Layout(name="footer", size=3),
)
layout["body"].split_row(
    Layout(name="left"),
    Layout(name="right"),
)
```

## Setting content

```python
layout["header"].update(Panel("Title", style="bold"))
layout["body"].update(table)
```

## Sizing

- `size=N` — fixed number of rows (vertical) or columns (horizontal)
- `ratio=N` — flex ratio relative to siblings (default 1)
- `minimum_size=N` — won't shrink below this

## With Live

```python
with Live(layout, screen=True, refresh_per_second=4) as live:
    while running:
        layout["body"].update(build_table(...))
        # layout is mutated in-place, no need to call live.update()
```

## Visibility

```python
layout["sidebar"].visible = False   # hide a section
```

## Debug

```python
print(layout.tree)    # visualize layout structure
```
