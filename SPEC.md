# dimergio — Specification

## 1. Overview

dimergio watches a program's file reads through a mergerfs pool,
identifies files that contribute most to I/O wait, and migrates the ones you
mark to the branch you pick. Reads are observed via fatrace; I/O device
contention is correlated via kernel block-device stats. No write-based
benchmarking is performed. No assumptions are made about hardware latency.

## 2. Terminology

| Term | Definition |
|---|---|
| **pool** | A mergerfs mount (e.g. `/mnt/games`), composed of multiple **branches** |
| **branch** | A filesystem path that is a member of a mergerfs pool. Each branch has a **speed class** and a **speed weight**. |
| **speed class** | Auto-detected tier from `rotational` flag + device name prefix. One of: `nvme`, `ssd`, `hdd`. |
| **speed weight** | Relative throughput estimate used to project savings and move time. Defaults: hdd=1, ssd=4, nvme=10. |
| **branch stats** | Per-branch cumulative totals: reads, writes, iowait_sec. Computed on-demand from per-file data, not maintained separately. |
| **candidate** | A tracked file with observed reads and no writes, so it can be selected for a move. |
| **effective size** | File size rounded up to the 128 KiB SSD block floor, minimum one block. Denominates the iowait-per-MB ranking. |
| **iowait debt** | Per-file score: sum of the I/O wait billed to this file's reads. Higher = more I/O impact. Primary ranking metric. |
| **observation session** | The period during which fatrace is running and the TUI is active. Runs in interactive mode (Rich TUI) or passive mode (headless). |

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       dimergio CLI                           │
│  dimergio watch --pool /mnt/games --pid 12345 --sudo        │
└─────────────────────────────────────────────────────────────┘
         │
         ├── __init__.py      Package version (0.1.1)
         ├── __main__.py      Entry point (python -m dimergio), logging config
         ├── pool.py          Mergerfs discovery, branch speed class, IO domains, and the
         │                    single pool-space path resolver used by every command
         ├── collector.py     fatrace subprocess, IOWaitSampler, Rich TUI (BROWSE + PREVIEW),
         │                    process tracking, branch marking, file selection
         ├── tui_keys.py      Key bindings, terminfo lookup, escape-sequence normalization
         ├── mmap.py          MmapWatcher: eBPF tracer plumbing for page-fault reads
         ├── model.py         Data classes (Pool, Branch, ReadEvent, FileAccumulator,
         │                    PidStat, Candidate, MovePlan, MoveEntry) and byte formatting
         ├── mover.py         Copy, verify, rename, execute_move_plan()
         ├── state.py         StateManager (MoveEntry tracking)
         ├── stats.py         YAML persistence of per-file stats under state_dir
         └── config.py        User configuration
```

### 3.1 Data Flow

```
fatrace -f RW -u -t -p <pid>
    │
    ▼
collector._parse_line()          ─── ReadEvent(timestamp, proc, pid, uid, event_type, file_path)
    │                                Also: PidStat.process_name (resolved from /proc/<pid>/comm)
    │
    ├── IOWaitSampler              ─── bills each window's domain busy time to
    │                                  the (file, pid) reads seen in that window
    │
    ├── collector._accumulate()    ─── FileAccumulator.total_reads++  (R events)
    │                                  FileAccumulator.write_count++   (W events)
    │
    ├── collector._charge()        ─── FileAccumulator.iowait_debt += billed seconds
    │                                  PidStat.total_iowait_sec += billed seconds
    │
    ├── collector._update_pid_stats() ── PidStat.read_count++
    │                                    PidStat.write_count++ on W events
    │
    └── TUI (BROWSE mode) ────── Live observation + selection on one screen
                                     │
                                     ├── Enter ──── Open PREVIEW panel
                                     └── Esc/q ──── Quit

BROWSE mode ────────────────────── Live observation + branch targeting
    │
    ├── ↑/↓ ────── Highlight file (cursor)
    ├── Space ──── Rotate highlighted file's target branch forward
    ├── - ──────── Clear mark
    ├── m ──────── Scan mmap candidates, focus the process list
    ├── Tab ────── Switch focus between file list and process list
    ├── Enter ──── Preview moves (PREVIEW panel) → confirm → execute_move_plan()
    ├── q ──────── Quit (press twice within 4s)
    ├── PageUp/Down ── Scroll focused list
    └── Home/End ──── Switch focus (Home: process list, End: file list)
```

## 4. Pool Discovery (pool.py)

### 4.1 Find mergerfs pools

Parse `/proc/mounts` for filesystems of type `fuse.mergerfs`. Extract:

- Mount point (e.g. `/mnt/games`)
- `fsname=` from super options (pool alias)
- Branch paths from `device` field (colon-separated)

### 4.2 Map branches to block devices

For each branch path (e.g. `/mnt/@/nvme_games`):

1. Find the mount whose mountpoint resolves to the branch path, read once from
   `/proc/mounts` per session.
2. Take the mount source and resolve it. The kernel device name is its basename,
   so `/dev/dm-11` gives `dm-11`, `/dev/nvme0n1p1` gives `nvme0n1p1`, and a
   `/dev/mapper/...` or `/dev/disk/by-uuid/...` symlink gives whatever it points
   at.
3. Read `rotational` from `/sys/block/<device>/queue/rotational`. A partition
   has no queue of its own, so its parent disk supplies the flag.
4. Store `branch → {device, rotational, speed_class, speed_weight}`.

A source that is not a `/dev` path (NFS, a loopback string) yields no device,
and the branch keeps the conservative hdd tier.

### 4.3 Speed class auto-detection

The rotational flag decides hdd versus flash. The device name only separates
NVMe from other flash, because both report rotational=0. An md array stays
conservative: the flag describes the array, not the disks under it.

| rotational | device prefix | speed class | weight |
|---|---|---|---|
| 1 | any | `hdd` | 1 |
| 0 | `nvme` | `nvme` | 10 |
| 0 | anything else | `ssd` | 4 |
| any | `md` | `hdd` | 1 |

Weights are fixed defaults (`SPEED_WEIGHTS` in `model.py`). A branch whose
device cannot be resolved stays `hdd`, which under-claims rather than
promising speed it cannot prove.

### 4.4 Branch short labels

Short labels for display are derived from the last path component of each branch,
with common suffixes stripped (`_games`, `_nvme`, `_ssd`, `_r1`, etc.):

| Branch path | Short label |
|---|---|
| `/mnt/@/ssd_games` | `ssd` |
| `/mnt/@/r1_games` | `r1` |
| `/mnt/@/nvme0` | `nvme` |

Duplicate labels are disambiguated with an index suffix (e.g. `r1`, `r2`).

### 4.5 Volume path remapping

fatrace reports paths through the btrfs volume mount (e.g. `/mnt/@/ssd_games/Data/...`),
not the user-facing pool-relative path (`Data/...`). The collector builds a volume map
from `/proc/mounts` to remap each fatrace path to a pool-relative path. This enables
consistent file tracking across branches that use different mount layouts.

## 5. Data Collection (collector.py)

### 5.1 Subprocess: fatrace

```
/usr/sbin/fatrace -f RW -u -t -t
```

| Flag | Purpose |
|---|---|
| `-f RW` | Read and write events (write tracking for move eligibility) |
| `-u` | Include `[uid:gid]` for user filtering |
| `-t -t` | Epoch-seconds timestamp prefix. One `-t` prints `HH:MM:SS.micro`, two print seconds since the epoch, which is what the parser reads. |

There is no per-PID filter on the fatrace command. Scope comes from the UID
filter plus the tracked-path rule, and `--pid` / `--process` only decide when to
stop observing.

Note: `-c` (only current mount) is NOT used — fanotify cannot watch FUSE/mergerfs
mounts. Instead, fatrace runs against the raw btrfs volume mounts, and paths are
remapped to pool-relative via the volume map (Section 4.5).

Output format (per line):

```
TIMESTAMP PROCESS(PID)[UID:GID]: EVENT /path

Examples:
  1748573021.456789 myapp(12345)[1000:1000]: R /mnt/@/ssd_games/Data/file.ba2
  1748573021.456789 myapp(12345)[1000:1000]: W /mnt/@/r1_games/Data/save.dat
```

Note the space between `)` and `[` in the fatrace output: `cmd(pid) [uid:gid]`.

### 5.2 Regex parsing

A compiled regex with named groups parses each fatrace line:

```python
TS = r"(?P<ts>\d+\.\d+)"
PROC = r"(?P<comm>[^(]+)"
PID = r"(?P<pid>\d+)"
UID = r"(?P<uid>\d+)"
GID = r"(?P<gid>\d+)"
EVENT = r"(?P<event>\w)"
PATH = r"(?P<path>.*)"
S = r"\s+"

FATRACE_RE = re.compile(
    rf"^{TS}\s+{PROC}\({PID}\)\s*\[{UID}:{GID}\]:\s*{EVENT}\s+{PATH}$"
)
```

Key details:
- `\s+` between event code and path (variable padding in fatrace output)
- `.*` for PATH — paths can contain spaces (e.g. `EVERSPACE™ 2/Data/...`)
- Event `R` = read, `W` = write, `O` = open, `C` = close, `D` = delete, `+` = create
- Write events (`W`) mark files in `_written_paths` and increment `write_count`

### 5.3 UID filter

When `--sudo` is NOT used: skip lines where uid ≠ current user's uid.
When `--sudo` IS used: skip UID check (fatrace runs as root).

### 5.4 I/O Wait Sampler (background thread)

Every **10ms** (100Hz default), read `io_ticks` for every device that backs a
branch from `/proc/diskstats`. The interval is adjustable at runtime with `[`
and `]` (5ms steps, minimum 5ms = 200Hz) and starts from the configured
`iowait_interval_ms`.

**IO domains.** Branches backed by the same set of block devices form one
domain. Two branches on the same multi-device btrfs pool contend for the same
device queue, so their busy time must not be attributed separately. A branch's
device set comes from its mount source plus the btrfs device list in
`/sys/fs/btrfs`.

The sampler bills at the tick that closes each window:

```
busy_ms   = sum(io_ticks[t] - io_ticks[t-1] for each device in the domain)
counts    = {(path, pid): reads recorded since the last tick}
total     = sum(counts.values())
per_read  = busy_ms / total if total else 0
charge[(path, pid)] += reads * per_read        # seconds reach the accumulator
counts    = {}                                 # nothing carries over
```

`io_ticks` is wall-clock time the device spent doing I/O, so summing it across
a domain's devices is the queue's busy time, not a percentage. Counters that go
backwards (device replaced, hotplug) are ignored rather than charged negative.

**Attribution.** A read is billed the busy time of its own window, in
proportion to its share of the reads that arrived in that window. A window with
one read bills that read the whole window. A window with no reads bills nobody,
because no observed read explains that busy time. Because the bill arrives at
the tick, the read itself is counted immediately and its `iowait_debt` grows a
moment later. The running total (`total_iowait_sec`) counts all device busy
time regardless of billing and feeds the `ΔIO` display.

### 5.5 Event Capture

On each fatrace line (from the main thread):

1. Parse: timestamp, proc, pid, uid, event_type, path
2. **UID filter**: skip if uid ≠ current user (unless `--sudo`)
3. **Tracked-path resolution** (`pool.resolve_tracked`): a path under
   `data_path` is canonicalized as-is; otherwise a raw btrfs volume path is
   remapped to pool space and accepted only when the real file lives under
   `data_path` or is the target of a symlink indexed there. Paths outside that
   scope are dropped.
4. **Write detection**: if event is `W`, add path to `_written_paths` and increment `write_count`
5. **Branch resolution** (`pool.branch_holding`): the branch that actually holds
   the file, or a drop when no branch does. A read is never charged to a
   guessed branch.
6. **Accumulate**: count the read on the FileAccumulator for this path
7. **Record**: `sampler.record_event(branch_idx, (path, pid))` for billing at the next tick
8. **PID tracking**: update PidStat (read_count, write_count, process_name from `/proc/<pid>/comm`)
9. **Return** ReadEvent for TUI consumption

### 5.6 FileAccumulator

```python
@dataclass
class FileAccumulator:
    path: Path               # always the real file, in pool space
    branch_idx: int          # which mergerfs branch (identifies the device)
    total_reads: int = 0
    write_count: int = 0     # W events on this file
    first_seen: float = 0.0   # epoch seconds
    last_seen: float = 0.0
    iowait_debt: float = 0.0  # sum of iowait_sec at each read event
    target_branch_idx: int | None = None   # SELECT-mode intent
    display_name: str = ""   # symlink path for display only

    def observe(self, ts: float) -> None:
        self.observe_n(ts, 1)

    def observe_n(self, ts: float, n: int) -> None:
        self.total_reads += n
        if not self.first_seen:
            self.first_seen = ts
        self.last_seen = ts

    def charge(self, seconds: float) -> None:
        self.iowait_debt += seconds
```

### 5.7 PidStat

```python
@dataclass
class PidStat:
    pid: int
    process_name: str          # resolved from /proc/<pid>/comm (cached)
    read_count: int = 0
    write_count: int = 0
    iowait_sec: float = 0.0    # sum of iowait for reads by this PID
```

Process name resolution: read `/proc/<pid>/comm`, cache in PidStat.
Fallback: use fatrace `comm` field (may be abbreviated for long names).

### 5.8 Auto-quit (passive mode only)

When `--pid PID` or `--process NAME` is given, the collector runs in passive
mode (`_run_passive`) and auto-quits when the known target process exits.
Interactive mode has no auto-quit; the user quits manually (press `q` twice
within 4s). Stats are persisted on quit regardless.

On stop:
1. TERM the fatrace subprocess
2. Read any remaining buffered output
3. Proceed to analysis/selection

## 6. Ranking and Selection (collector.py)

### 6.1 Input

The accumulated `dict[Path, FileAccumulator]`, keyed by canonical pool-space path.

### 6.2 Per-file enrichment

- **File size**: one `os.stat(acc.path).st_size` per path per session, cached.
- **Branch**: `acc.branch_idx`, resolved when the read was tracked.
- **Writes**: `acc.write_count` (W events seen for this file).
- **Effective size**: `effective_size_bytes(size)`, the size rounded up to the
  128 KiB SSD block floor with a minimum of one block.
- **iowait per MB**: `iowait_debt / (effective_size / 1_000_000)`.

### 6.3 Ranking and eligibility

Files with `write_count > 0` are excluded from the list: they were written during
observation and must not be moved. Everything else is listed, sorted descending
by the active column. The columns are `iowait_per_mb` (default), `iowait` and
`reads`; `←` / `→` rotates the active column for the focused panel.

Byte-weighting via the effective size is what lets a large, slow file outrank a
tiny one with equal raw iowait.

### 6.4 Branch summary

Per-branch totals (reads, writes, iowait) are summed on demand from the
accumulators for the tier line. No branch-level counters are maintained
separately, so the summary cannot drift from the file table.

### 6.5 Selection

The user marks a target branch per file: `Space` rotates the highlighted file's
target forward through the branch list, and `-` clears it. Nothing is selected
automatically; a file moves only because the user marked it. `Enter` turns the
marks into one `MovePlan` per file and opens PREVIEW.

### 6.6 Projection

For the marked set the TUI shows estimated savings and the space each target
needs:

```
avg_eff  = mean(effective_size) over marked files        # 1 when none marked
weight   = effective_size / avg_eff
saved   += iowait_debt * (1 - src_weight / tgt_weight) * weight
time    += size / (tgt_weight * 100_000_000)
space[t] += size
```

The projection is display only. It rescales estimated savings by physical size
and never changes which files are marked. The observation session ends with the
real numbers from `execute_move_plan()`, so the projection is not persisted.

## 7. TUI Interface (collector.py, Rich TUI)

### 7.1 Display

A full-screen Rich TUI using `Live(screen=True, get_renderable=)` at 4 Hz.
Keyboard input on a separate daemon thread via `readchar`, isolated from Rich I/O.

A single merged **BROWSE** screen replaces the old MONITOR/SELECT split. fatrace
sampling runs continuously for the whole session, so there is no separate
"monitor mode" to switch into and no fatrace respawn (and thus no reader-thread
desync). The screen shows the live process table when monitoring is active, the
navigable file table with FROM/TO target columns, tier stats, and the marking
estimate. `Enter` opens the **PREVIEW** panel.

### 7.2 BROWSE mode (live observation + selection)

Shows live-updating file table, process stats, and tier summary:

```
╭─ dimergio — <pool> — BROWSE ───────────────────────────────────────────────────────────────────────────╮
│ 0:05:32  reads 4,812  writes 17  files 87  marked 0  sample 10ms(100Hz)                                 │
│ Tiers:  0:nvme(10x)  1:ssd(4x)  2:r1(1x)   M:nand:ON                                                     │
│ PROCESS              READS     MMAP  IOWAIT  STATUS                                                       │
│ llama-server            0    6.8GB   2.012  watch                                                         │
│ everspace2.exe        3,923        -  12.348  run                                                          │
│ wineserver              877        -   2.104  run                                                          │
│ signal-desktop           12        -   0.032  exited                                                       │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ # READS   IOWAIT    FROM     TO     FILE                                                                 │
│ 1 4,521  22.600    ssd   ──→  nvme   Data/textures/grass.dds                                            │
│ 2 3,892  19.500    r1    ──→  ssd    Data/meshes/rock.nif                                               │
│ 3 2,891  14.500    r1    ──→  ssd    Data/main.ba2                                                       │
│ 4 2,100   8.200    ssd   ──→  nvme   Data/sounds/music.pak                                              │
│ 5 1,800   7.100    r1    ──→  ssd    Data/shaders/compiled.glsl                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Tier: reads  nvme    0 (0%)  ssd 2,400 (50%)  r1 2,412 (50%)                                              │
│       iowait  nvme 0.0s(0%)  ssd  7.2s(28%)  r1 18.1s(72%)                                              │
│ est. iowait saved 25.3s   est. move time 2.1s   space required  nvme 2.1GB  ssd 566MB                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Observation and selection happen on one screen. Press `q` twice within 4s to quit.
Press `Enter` to open the PREVIEW panel and review the planned moves. While
monitoring is active the process table is shown; for preloaded/saved sessions it
is omitted.

### 7.3 PREVIEW panel

Pressing `Enter` (with ≥1 marked file) opens a PREVIEW panel. Moves are listed
in iowait-debt order, with FROM/TO columns color-coded by speed class and a
per-file size plus a total. `Enter` confirms and calls `execute_move_plan()`;
`Esc` returns to BROWSE; `q` (twice within 4s) quits without executing.

File selection with branch targeting. Each file gets a FROM (current) and TO (target) column.
Color-coded by speed class: blue=HDD, teal=SSD, green=NVMe.

```
╭─ dimergio — <pool> — PREVIEW ──────────────────────────────────────────────────────────────────────────╮
│ 5 move(s)  total 3.2GB                                                                                      │
│ # FROM        TO     FILE                                                                 SIZE             │
│ 1  ssd   ──→  nvme   Data/textures/grass.dds                                       12.5MB              │
│ 2  r1    ──→  ssd    Data/meshes/rock.nif                                          438MB               │
│ 3  r1    ──→  ssd    Data/main.ba2                                               2.1GB                │
│ 4  ssd   ──→  nvme   Data/sounds/music.pak                                       128MB                │
│ 5  r1    ──→  ssd    Data/shaders/compiled.glsl                                    64MB                │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Tier: reads  nvme    0 (0%)  ssd 2,400 (50%)  r1 2,412 (50%)                                              │
│       iowait  nvme 0.0s(0%)  ssd  7.2s(28%)  r1 18.1s(72%)                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

FROM shows `...` if file exists on multiple branches (prefixed). Files with writes > 0
are shown in dim text (shift+# skips them).

### 7.4 User actions (BROWSE mode)

Navigation is consistent across the whole TUI: **`Enter`** always advances one step
(opens PREVIEW), **`Esc`** in PREVIEW returns to BROWSE, **`q`** always quits the
program (press twice within 4s; the first press shows a warning, any other key
cancels).

- **↑ / ↓**: Move the highlight cursor to a file
- **← / →**: Rotate the active sort column (IOW/MB → IOWAIT → READS, wraps). The active column is highlighted in the table header.
- **Space**: Rotate the highlighted file's target branch forward (cycles through branches)
- **Tab**: Switch focus between the file list and the process list (`Home` /
  `End` are aliases). Arrow keys move within the focused list and `←` / `→`
  re-sort it.
- **m**: Scan for mmap-reading candidates and focus the process list.
- **`-`**: Clear mark on selected file
- **`Enter`**: Open PREVIEW panel (ordered, branch-color-coded list + total bytes)
- **`q`**: Quit (press twice within 4s to confirm)
- **PageUp/PageDown**: Scroll the focused list by visible rows
- **Home / End**: Switch focus — `Home` to the process list, `End` back to the file list (they no longer scroll within a list)
- **`[`**: Decrease sample interval by 5ms (faster, min 5ms)
- **`]`**: Increase sample interval by 5ms (slower)
- **`s`**: Toggle showing exited processes in the process table
- **`M`**: Toggle NAND-source warning
- **`m`**: Scan for mmap-reading candidate PIDs and focus the process list

### 7.4.0.1 mmap read detection

fanotify (and therefore fatrace) does not report accesses that happen through
`mmap`, so a llama.cpp model load or an emulator loading assets leaves no trace
in dimergio. A CO-RE eBPF tracer (`dimergio-mmap`, a kprobe on
`filemap_fault`) counts page faults per `(pid, inode)` and streams aggregated
windows (`<pid> <ino> <count>`) over stdin/stdout to the collector, which maps
the inode back to a path via `/proc/<pid>/maps` (cached per pid) and folds each
fault into the normal accumulation pipeline (per-file reads + iowait share,
sampler event counts, PID stats).

The tracer is built from `src/dimergio/bpf` (C sources, Makefile) and installed
on `PATH` as `dimergio-mmap`. `DIMERGIO_MMAP_BIN` overrides the lookup. Build
products are not tracked in git.

- Press **`m`** to scan `/proc/<pid>/io` `read_bytes` for processes whose file
  mappings fall under the watched data path. Candidates are sorted by that
  monotonic counter (stable across scans), not by a rate. `m` also switches
  focus to the process list.
- With the process list focused, **↑/↓** moves the cursor and **`Enter`**
  toggles mmap watching for the highlighted process. Status column values:
  `run` (fatrace reads), `exited`, `cand` (mmap candidate), `watch` (currently
  traced). The MMAP column shows the candidate's total `read_bytes`.
- Start PIDs from the command line with `--mmap-pid <pid>` (repeatable), e.g.
  `dimergio watch --mmap-pid $(pgrep llama-server)`.
- The tracer is a single statically linked x86-64 binary that works on any
  kernel with BTF (CO-RE). When it is absent, `m` still lists candidates and
  `Enter` explains why it cannot watch them; mmap detection is then a no-op and
  the tool works on fatrace reads alone. The tracer starts at the configured
  `iowait_interval_ms`, and `[` / `]` move both intervals together.

The file list is ranked by **iowait cost per physical MB** (the default sort
column), not raw iowait seconds. Each file's size is rounded up to the SSD
block size (`SSD_BLOCK_BYTES = 128 KiB`, the assumed minimum physical read
cost) to derive an effective size; `iowait_per_mb = iowait_debt / effective_MB`.
This byte-weights the ranking so a large, slow file outranks a tiny one with
equal raw iowait. Files written during the observation run are excluded from
the list and can never be marked.

### 7.4.1 PREVIEW panel

Pressing `Enter` in BROWSE (with ≥1 marked file) opens a PREVIEW panel. Moves are
listed in iowait-debt order, with FROM/TO columns color-coded by speed class and a
per-file size plus a total. `Enter` confirms and calls `execute_move_plan()`;
`Esc` returns to BROWSE; `q` (twice within 4s) quits without executing.

### 7.4.2 Post-move summary and redundant-originals note

After `execute_move_plan()` returns, `cmd_watch` prints a color-coded operations
table and the total bytes copied — shown in **MB**, or **GB** when over 10GB. No
free-originals prompt is shown; the redundant renamed originals (`_dimergio_`
prefix) are kept so the user can test their program first. `_note_free_originals`
reports how many redundant originals were left behind and names the command to
verify & free them later: `dimergio cleanup --pool <mount> -d` (or `dimergio
undo` to revert). The note appears whenever at least one redundant original was
left behind, including for empty files where zero bytes were copied.

### 7.5 Scrolling

Visible rows clamped to 8-25 based on terminal height. Scroll position tracked
via `_file_scroll` offset. Arrow keys and PageUp/PageDown scroll the focused
list; the process list uses the same `tui_keys.apply_nav` helper with an 8-row
window.

### 7.6 NAND source warning

If source branch is NAND (SSD or NVMe) and `nand_warn` is ON, pressing `M`
shows a confirmation panel before allowing moves. Toggle with `M` key.

### 7.7 Rich markup and highlighting

- `Table.add_row(..., style="reverse")` for selected row
- Rich markup like `[bold]`, `[dim]`, `[reverse]` for inline styling
- `style` kwarg on individual cells for per-column coloring
- Color-coded FROM/TO columns by speed class

## 8. Migration (mover.py)

### 8.1 MovePlan execution

The TUI returns a list of `MovePlan` objects (file path + target branch + is_rename_only).
`execute_move_plan()` processes each plan and returns
`(succeeded, failed, failed_list, operations, total_bytes)`, where `operations` is a
per-file record (`pool_path`, `src`, `dst`, `bytes`, `ok`) and `total_bytes` counts
only copied (non-rename) bytes:

```
For each MovePlan:
  1. Reject a plan whose `branch_idx` or `target_branch_idx` no longer resolves
  2. Source = pool.branches[branch_idx], target = pool.branches[target_branch_idx]
  3. Resolve the real source file and its canonical pool-space relative path
  4. COPY + VERIFY + RENAME + RECORD (bytes = file size)
```

Every move copies. There is no rename-only fast path: a plan whose real file is
already on the target branch is still copied, so the reported byte total is
always what landed on the target.

### 8.2 Path resolution at move time

A move never touches a symlink. `pool.canonical_pool_path` resolves the tracked
path to the real file, and the relative path is recomputed from that real
location, so a plan recorded through a symlink or a raw btrfs volume path still
moves the file itself. When the canonical path escapes the pool, or the source
file no longer exists, the plan fails with a message that says to re-run
`watch`, and no partial copy is left behind.

### 8.3 Copy and rename pipeline

```
1. COPY  → shutil.copy2(src_path, dst_path) on branch
2. VERIFY → os.stat(src) vs os.stat(dst): size must match
3. VERIFY (optional) → SHA256 source, SHA256 destination, compare (--verify flag)
4. RENAME → os.rename(src, src.with_name(f"{prefix}{src.name}"))
5. RECORD → state.add(MoveEntry)
```

### 8.4 Directory creation

On the target branch: `os.makedirs(target_dir, exist_ok=True)`
Copy permission bits + ownership from the source directory (`shutil.copymode`,
`shutil.copystat` on the directory).

### 8.5 Copy

`shutil.copy2(src_path, dst_path)` preserves:
- Permission bits
- Last access/modification times
- Extended attributes
- Ownership (if running as root, otherwise best-effort)

### 8.6 Verification

```python
src_stat = os.stat(src_path)
dst_stat = os.stat(dst_path)
assert src_stat.st_size == dst_stat.st_size
# Optional: pass --verify for full SHA256 verification
```

### 8.7 Rename

```python
parent = src_path.parent
renamed = parent / f"{prefix}{src_path.name}"
os.rename(src_path, renamed)
```

Default prefix: `_dimergio_`. Configurable in config.

Result: mergerfs sees only the fast-branch copy (the slow-branch original
has a different name).

### 8.8 Rollback on failure

If COPY fails (disk full, permission error, I/O error):
- Print error, skip this file, continue to next
- Record as `failed` in state
- The user can retry later

If VERIFY fails (size mismatch):
- Delete the copy from fast branch
- Print error, continue
- Original is untouched

If SHA256 verification fails (--verify):
- Print error with both paths
- Stop the migration; both copies are preserved for investigation
- Original is untouched

If RENAME fails (shouldn't happen on same FS):
- Delete the copy, report error, continue

### 8.9 Progress output

```
Moving 38 files...
 1/38 ✓ Data/textures/grass.dds                 12.5MB   → ssd_branch
 2/38 ✓ Data/meshes/rock.nif                   438.0MB   → ssd_branch
 3/38 ✓ Data/main.ba2                            2.1GB   → ssd_branch
 …
 37/38 ✓ Data/terrain/chunk_05.bin              891.0MB   → ssd_branch
 38/38 ✗ Data/terrain/chunk_06.bin              891.0MB   → FAILED (disk full)

Done: 37 moved, 1 failed. 5.2GB copied to ssd_branch.
Original files renamed with prefix _dimergio_.

Restart your program and test. If everything works, run:
  dimergio cleanup --pool /mnt/pool -d
to verify & delete the originals.
```

## 9. State Management (state.py)

### 9.1 State directory

`config.state_dir`, default `$XDG_STATE_HOME/dimergio` (usually
`~/.local/state/dimergio`). It holds the state file, the per-pool stats YAML,
and nothing else.

```
~/.local/state/dimergio/
├── state.pool.json
└── POOL.yaml        # accumulated stats, written on clean exit
```

### 9.2 State file format

```json
{
  "pool": "POOL",
  "pool_mount": "/mnt/pool",
  "created": "2026-05-30T14:00:00Z",
  "moved_files": [
    {
      "pool_path": "Data/textures/grass.dds",
      "source_branch": "/mnt/slow",
      "target_branch": "/mnt/fast",
      "original_basename": "grass.dds",
      "renamed_basename": "_dimergio_grass.dds",
      "moved_at": "2026-05-30T14:30:00Z",
      "file_size": 12500000,
      "verified_working": null
    }
  ]
}
```

`verified_working`:
- `null` = not yet checked
- `true` = user confirmed program works
- `false` = user reported problem (trigger undo)

### 9.3 Crash behavior

There is no crash checkpoint. Accumulated stats reach disk when the session
ends cleanly (the TUI quits, or a passive run finishes), so a kill or a crash
loses that session's observations. Use the same pool and `--data` path on the
next run to keep collecting.

### 9.4 Undo

`dimergio undo` reads the state file and, for each entry:

1. If `verified_working == false` or user requests undo:
   - Rename original back: `_dimergio_grass.dds` → `grass.dds` (on source branch)
   - Delete copy from target branch
   - Clean up empty directories
   - Remove entry from state (or mark `undone: true`)

## 10. CLI Reference (cli.py)

```
usage: dimergio <command> [options]

Commands:
  watch      Run fatrace, collect, analyze, and move files
  status     Show migration state
  cleanup    Verify old migrations and clean up originals
  undo       Undo migrations
```

### 10.1 `dimergio watch`

```
dimergio watch [options]

Options:
  --pool PATH           Mergerfs pool mount point [default: /mnt/games]
  --data PATH           Restrict to reads under this path (default: CWD inside pool, else pool root)
  --process NAME        Auto-quit when this process exits (matches /proc/<pid>/cmdline)
  --pid N               Auto-quit when this PID exits
  --mmap-pid N          Watch mmap page-fault reads of this PID (repeatable)
  --sudo                Run fatrace via sudo. With --no-sudo the fatrace binary
                        needs cap_sys_admin (fanotify) and cap_dac_read_search
                        (open_by_handle_at, without it every event resolves to
                        "(deleted)" and is dropped)
  --iowait-interval N   I/O wait sampling interval in ms [default: 10]
  --verify              SHA256-verify each file after copy
  --no-interactive      Disable interactive PID monitor (headless mode)
  --offline             Skip collection; analyze saved stats or --from-log instead
  --from-log PATH       Replay a captured fatrace log (requires --offline)
  --debug-log [PATH]    Write sampler debug output (default /tmp/dimergio_<pool>.log)
  --verbose, -v         Log raw fatrace lines + parsing decisions to stderr
  --version             Show version and exit
```

`--from-log` runs the same tracked-path resolver as a live session, so a replayed
log attributes reads to the same files a live watch would.

### 10.2 `dimergio status`

```
dimergio status [--pool PATH]
```

Shows all migrations with status (verified/pending/problem), moved date, and file size.

### 10.3 `dimergio cleanup`

```
dimergio cleanup [--pool PATH] [-a|--all] [-d|--days [N]]
```

Walks through unverified migrations and asks you to confirm each one.
`-d` narrows to migrations at least N days old; omitting N means 0 days, so
fresh moves are included. `-a` accepts every eligible migration without
prompting.

### 10.4 `dimergio undo`

```
dimergio undo [--pool PATH] [--all]
```

Undo migrations. Without `--all`, shows a numbered list and lets you pick.

## 11. Configuration (config.py)

```
~/.config/dimergio/config.json
```

```json
{
  "prefix": "_dimergio_",
  "state_dir": "~/.local/state/dimergio",
  "cleanup_days": 14,
  "default_pool": "/mnt/games",
  "iowait_interval_ms": 10,
  "symlink_depth": 3
}
```

Created on first run with defaults if absent. Unknown keys are ignored and
missing keys are filled from these defaults.

`symlink_depth` is the directory depth walked once at startup from the data
path to build the display-name map, so a file can be labelled by the symlink a
user actually opens. `0` disables the walk, which also disables symlink naming.

## 12. Python 3.10+ Usage

- `match/case` for CLI dispatch and fatrace line parsing (3.10)
- `Path` from `pathlib` for all path manipulation
- `timezone.utc` for timestamps
- `@dataclass(slots=True)` for model objects (3.10)
- `|` union types in annotations (3.10)
- `Rich` for TUI: `Live`, `Table`, `Panel`, `Console`, `Prompt`, `Confirm`
- `threading.Thread(daemon=True)` for keyboard reader and iowait sampler
- `termios`/`tty` for raw keyboard input (isolated from Rich I/O)
- `pyyaml` for stats persistence (`<POOL>.yaml` under `state_dir`)

## 13. Non-goals

- No daemon or persistent service
- No web UI or API
- No support for non-Linux systems
- No automatic migration without user confirmation
- No support for mergerfs pools the tool did not discover at startup
- No file integrity verification beyond size comparison (optional SHA256 with --verify)
- No byte-range tracking (fatrace does not output byte counts)
- No actions during monitoring — TUI is read-only during observation
