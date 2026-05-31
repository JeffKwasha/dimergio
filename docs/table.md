# Table

`from rich.table import Table`

Tabular data rendering with automatic column sizing.

## Constructor

```python
Table(
    *columns,                    # column headers as strings or Column objects
    title=None,                  # title above table
    caption=None,                # caption below table
    box=ROUNDED,                 # Box style; None for no borders
    show_header=True,
    show_footer=False,
    show_edge=True,
    show_lines=False,            # lines between ALL rows
    expand=False,                # stretch to full terminal width
    min_width=None,
    width=None,                  # exact width
    padding=(0, 1),
    collapse_padding=False,
    pad_edge=True,
    highlight=False,
    row_styles=None,             # alternating row styles e.g. ["dim", ""]
    header_style="",
    footer_style="",
    border_style="",
    title_style="",
    caption_style="",
    safe_box=False,              # ASCII-only box chars
)
```

## Columns

```python
table.add_column(
    header,
    header_style="",
    footer_style="",
    style="",                    # default cell style for this column
    justify="left",              # "left", "center", "right"
    vertical="top",              # "top", "middle", "bottom"
    width=None,                  # fixed width (disables auto-size)
    min_width=None,
    max_width=None,
    ratio=None,                  # flex ratio for distributing extra space
    no_wrap=False,
    highlight=False,
)
```

## Rows

```python
table.add_row(*cells, style="", end_section=False)
table.add_section()              # line between current/next rows
```

- Cells are strings (markup is parsed) or any Rich renderable
- `style=` on `add_row()` applies to the **entire row**
- Styles: row > column > table (row styles override)

## Grid mode

```python
Table.grid(expand=True)          # borderless layout grid
```

## Known pitfalls

- `width=N` on columns disables auto-calculation
- `no_wrap=True` prevents cells from spanning lines
- With `expand=True`, extra width is distributed among columns with `ratio=`
  or proportionally; columns with explicit `width=` stay fixed
