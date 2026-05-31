# Progress

`from rich.progress import Progress, track`

## track() — simple use

```python
for i in track(range(n), description="Processing..."):
    do_work(i)
```

## Progress — advanced use

```python
with Progress(
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TaskProgressColumn(),
    TimeRemainingColumn(),
    console=console,
    transient=False,         # remove on completion if True
    expand=False,            # stretch to terminal width
    refresh_per_second=10,   # auto-refresh rate
    auto_refresh=True,
) as progress:
    task = progress.add_task("[red]Downloading...", total=1000)
    progress.update(task, advance=1)     # or completed=500
    progress.advance(task)               # increment by 1
```

## Column types

| Column | Shows |
|---|---|
| `TextColumn(fmt)` | Formatted text with `{task.description}`, `{task.completed}`, `{task.fields[key]}` |
| `BarColumn()` | Progress bar |
| `TaskProgressColumn()` | Percentage complete |
| `TimeElapsedColumn()` | Elapsed time |
| `TimeRemainingColumn()` | ETA |
| `MofNCompleteColumn()` | `3/10` format |
| `FileSizeColumn()` | Bytes completed |
| `TotalFileSizeColumn()` | Total bytes |
| `DownloadColumn()` | Download-style display |
| `TransferSpeedColumn()` | Speed display |
| `SpinnerColumn()` | Animated spinner |
| `RenderableColumn()` | Any Rich renderable |

## Indeterminate progress

```python
progress.add_task("Waiting...", total=None)       # pulsing animation
progress.add_task("Loading...", total=100, start=False)  # will start later
```

## Multiple Progress instances in Live

```python
with Progress(...) as p1, Progress(...) as p2:
    # only one Progress context at a time
    ...

# For multiple simultaneous: put both in a Live display
with Live(Group(progress1, progress2)) as live:
    ...
```
