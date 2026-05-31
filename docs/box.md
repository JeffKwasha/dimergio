# Box

`from rich import box`

Constants for border drawing characters used by Table and Panel.

## Common styles

| Constant | Look |
|---|---|
| `box.ROUNDED` | `╭──╮` rounded corners |
| `box.SQUARE` | `┌──┐` square corners |
| `box.HEAVY` | `┏━━┓` thick lines |
| `box.HEAVY_EDGE` | `┏━━┓` thick outer, thin inner |
| `box.DOUBLE` | `╔══╗` double lines |
| `box.DOUBLE_EDGE` | `╔══╗` double outer, single inner |
| `box.MINIMAL` | `──` no vertical edges |
| `box.MINIMAL_HEAVY_HEAD` | minimal with heavy header separator |
| `box.MINIMAL_DOUBLE_HEAD` | minimal with double header separator |
| `box.SIMPLE` | `──` simple lines, no corners |
| `box.SIMPLE_HEAVY` | simple with heavy header separator |
| `box.ASCII` | `+--+` pure ASCII |
| `box.ASCII2` | `+-+` compact ASCII |
| `box.HORIZONTALS` | horizontal lines only |

## Usage

```python
Table(box=box.SIMPLE)
Panel(table, box=box.ROUNDED)
```

## Notes

- `box=None` removes all borders from Tables
- Default for Tables is `box.ROUNDED`
- Default for Panels is `box.ROUNDED`
- On Windows legacy terminals with raster fonts, Rich uses safe ASCII fallback
- Use `safe_box=True` on Table to force ASCII on any platform
