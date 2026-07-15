# dimergio — Specification

## 1. Overview

dimergio watches a program's file reads through a mergerfs pool,
identifies files that contribute most to I/O wait, and migrates them to the
fastest branch in the pool. Reads are observed via fatrace; I/O device
contention is correlated via kernel block-device stats. No write-based
benchmarking is performed. No assumptions are made about hardware latency.

## 2. Terminology

| Term | Definition |
|---|---|
| **pool** | A mergerfs mount (e.g. `/mnt/games`), composed of multiple **branches** |
| **branch** | A filesystem path that is a member of a mergerfs pool. Each branch has a **speed class** and a **speed weight**. |
| **speed class** | Auto-detected tier from `rotational` flag + device name prefix. One of: `nvme`, `ssd`, `hdd`. |
| **speed weight** | Relative throughput estimate. Computed as `bytes_read / iowait_sec` per branch during observation. Used for target distribution. Defaults: hdd=1, ssd=4, nvme=10. |
| **branch stats** | Per-branch cumulative totals: reads, writes, iowait_sec. Computed on-demand from per-file data, not maintained separately. |
| **target distribution** | Ideal iowait split: `weight[i] / sum(weights)`. The goal for balanced IO across tiers. |
| **gap** | `target - actual` per branch. Positive = spare capacity (can receive files). Negative = overloaded (can give away files). |
| **candidate** | A file on an overloaded branch whose migration reduces iowait toward the target distribution. |
| **iowait debt** | Per-file score: sum of iowait_sec at each read event. Higher = more I/O impact. Primary ranking metric. |
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
         ├── pool.py          Mergerfs discovery from /proc, branch speed class auto-detection
         ├── collector.py     fatrace subprocess, IOWait sampling, Rich TUI (MONITOR + SELECT modes),
         │                    process tracking, branch marking, file selection
         ├── model.py         Data classes (Pool, Branch, ReadEvent, FileAccumulator,
         │                    PidStat, Candidate, MovePlan, MoveEntry)
         ├── analyze.py       Multi-tier move algorithm, gap computation
         ├── mover.py         Smart rename, copy, execute_move_plan()
         ├── state.py         StateManager (MoveEntry tracking, checkpoints)
         ├── stats.py         YAML persistence (.dimergio.yaml on largest branch)
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
    ├── collector._sample_iowait() ─── iowait_sec (delta, fairness-weighted among events in window)
    │
    ├── collector._accumulate()    ─── FileAccumulator.total_reads++  (R events)
    │                                  FileAccumulator.write_count++   (W events)
    │                                  FileAccumulator.iowait_debt += iowait_sec
    │
    ├── collector._update_pid_stats() ── PidStat.read_count++, pid_iowait_sec[pid] += iowait_sec
    │                                    PidStat.write_count++ on W events
    │
    └── TUI (MONITOR mode) ────── Read-only observation: process table, file table, tier stats
                                    │
                                    ├── Space ──── Stop monitoring → SELECT mode
                                    └── Enter ──── Switch to SELECT mode

SELECT mode ────────────────────── File selection with branch targeting
    │
    ├── ↑/↓ ────── Highlight file (cursor)
    ├── Space ──── Rotate highlighted file's target branch forward
    ├── m ──────── Back to MONITOR mode (restarts fatrace)
    ├── 0-9 ────── Mark file's target branch
    ├── Shift+0-9  Mark all files above (higher iowait) → skip files with writes
    ├── - ──────── Clear mark
    ├── Enter ──── Preview moves (PREVIEW panel) → confirm → execute_move_plan()
    ├── q ──────── Quit (confirmation if files marked)
    └── PageUp/Down/Home/End ── Scroll
```

## 4. Pool Discovery (pool.py)

### 4.1 Find mergerfs pools

Parse `/proc/mounts` for filesystems of type `fuse.mergerfs`. Extract:

- Mount point (e.g. `/mnt/games`)
- `fsname=` from super options (pool alias)
- Branch paths from `device` field (colon-separated)

### 4.2 Map branches to block devices

For each branch path (e.g. `/mnt/slow`):

1. Look up its mount in `/proc/mounts` → device path (e.g. `/dev/dm-3`)
2. Extract major:minor via `os.stat(branch)` → `os.major()`, `os.minor()`
   (fallback: read `/sys/block` by iterating, comparing major:minor from mountinfo)
3. Identify `dm-X` from the device path
4. Store mapping: `branch → {device: dm-X, rotational: from /sys/block/dm-X/queue/rotational}`

### 4.3 Speed class auto-detection

Each branch is classified into a speed tier based on two signals:

1. **`rotational` flag** (`/sys/block/<dev>/queue/rotational`): 0 = NAND, 1 = platter
2. **Device name prefix**: `nvme` = NVMe, `sd`/`vd` = SSD/HDD, `md` = RAID

Decision matrix:

| rotational | device prefix | speed class | default weight |
|---|---|---|---|
| 0 | `nvme` | `nvme` | 10 |
| 0 | `sd`/`vd`/`dm` | `ssd` | 4 |
| 1 | `sd`/`vd`/`dm` | `hdd` | 1 |
| 0/1 | `md`/`dm` (RAID) | `hdd` (conservative) | 1 |

Weight defaults are overridable by the user. Live speed weights are computed
from `bytes_read / iowait_sec` per branch during observation.

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
/usr/sbin/fatrace -f RW -u -t -p <pid>
```

| Flag | Purpose |
|---|---|
| `-f RW` | Read and write events (write tracking for move candidates) |
| `-u` | Include `[uid:gid]` for user filtering |
| `-t` | Timestamp in epoch seconds |
| `-p <pid>` | Only events from this PID |

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

Every **10ms** (100Hz default), read `/sys/block/<dm-X>/stat` for each branch device.
The sample interval is adjustable at runtime via `[` and `]` keys (5ms increments,
minimum 5ms = 200Hz max).

Format (17 fields):
```
read_io read_merges read_sectors read_ticks
write_io write_merges write_sectors write_ticks
in_flight io_ticks time_in_queue
... (discard_*, flush_*)
```

Derived per branch per sample:

```
io_busy_delta = io_ticks[t] - io_ticks[t-1]   # ms device was busy since last sample
io_busy_pct   = io_busy_delta / 50 * 100        # percent of interval busy
num_reads_delta = read_io[t] - read_io[t-1]     # reads completed since last sample
read_ticks_delta = read_ticks[t] - read_ticks[t-1]  # ms spent reading
```

**Fairness weighting**: When multiple reads are observed within a single 50ms
sampling window, the iowait delta for that window is divided equally among them.
This prevents inflating debt when many reads complete simultaneously.

Store a **rolling window** (last 3 samples, 150ms) of busy_pct for smoothing.
Maintain a thread-safe reference to the latest smoothed busy_pct per branch.

### 5.5 Event Capture

On each fatrace line (from the main thread):

1. Parse: timestamp, proc, pid, uid, event_type, path
2. **UID filter**: skip if uid ≠ current user (unless `--sudo`)
3. **Path filter**: convert to Path, skip if not under `data_path`
4. **Volume remap**: if path is through btrfs volume mount, remap to pool-relative
5. **Write detection**: if event is `W`, add path to `_written_paths` and increment `write_count`
6. **Branch resolution**: match path prefix against branch paths to identify branch
7. **IOWait**: read latest smoothed `io_busy_pct` for that branch
8. **Accumulate**: update the FileAccumulator for this path
9. **PID tracking**: update PidStat (read_count, write_count, process_name from `/proc/<pid>/comm`)
10. **Return** ReadEvent for TUI consumption

### 5.6 FileAccumulator

```python
@dataclass
class FileAccumulator:
    path: Path
    branch: str              # which mergerfs branch (identifies the device)
    total_reads: int = 0
    write_count: int = 0     # W events on this file
    first_seen: float = 0.0   # epoch seconds
    last_seen: float = 0.0
    iowait_debt: float = 0.0  # sum of iowait_sec at each read event
    _peak_read_rate: float = 0.0  # for secondary heuristic

    def observe(self, ts: float, iowait_sec: float) -> None:
        self.total_reads += 1
        self.iowait_debt += iowait_sec
        if not self.first_seen:
            self.first_seen = ts
        self.last_seen = ts
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

### 5.8 Auto-quit

Default in interactive mode: OFF. User toggles with `a` key on monitor screen.

If `--pid PID` is given (passive mode): auto-quit ON by default.
If `--process NAME` is given (passive mode): auto-quit ON by default.
`--auto-quit` CLI flag explicitly enables it for interactive mode.

On stop:
1. TERM the fatrace subprocess
2. Read any remaining buffered output
3. Proceed to analysis/selection

## 6. Analysis (model.py)

### 6.1 Input

The accumulated `dict[Path, FileAccumulator]` from the event log.

### 6.2 Per-file enrichment

For each accumulator, look up:

- **File size**: `os.stat(accum.path).st_size` (cached in the accumulator)
- **Branch**: already recorded
- **Writes**: `accum.write_count` (W events)

### 6.3 Branch stats (derived)

Per-branch totals are computed on-demand from per-file accumulators:

```python
def branch_stats(accumulators: dict[Path, FileAccumulator], branches: list[Branch]):
    stats = {b: {"reads": 0, "writes": 0, "iowait": 0.0} for b in branches}
    for acc in accumulators.values():
        b = branch_by_path[acc.branch]
        stats[b]["reads"] += acc.total_reads
        stats[b]["writes"] += acc.write_count
        stats[b]["iowait"] += acc.iowait_debt
    return stats
```

No separate branch-level counters — one pass over accumulators is sufficient.

### 6.4 Target distribution

```
total_weight = sum(branch.speed_weight for branch in branches)
target[i] = branch[i].speed_weight / total_weight
```

Example: nvme(10) + ssd(4) + hdd(1) → targets: nvme=67%, ssd=27%, hdd=7%

### 6.5 Gap computation

```
actual[i] = branch[i].iowait / total_iowait
gap[i] = target[i] - actual[i]
```

- `gap > 0`: branch has spare capacity — can receive files
- `gap < 0`: branch is overloaded — can give away files
- `gap == 0`: balanced

### 6.6 Multi-tier move algorithm

```
1. Compute branch_stats and gaps
2. Sort branches by gap ascending (most overloaded first)
3. Identify source branches: those with gap < 0 AND speed class ≤ target tier
4. Sort files on source branches by iowait_debt descending
5. For each source branch (most overloaded first):
     For each file on source branch (highest debt first):
       Find target branch with largest positive gap
       If target gap > 0:
         Add file to move list
         Update target gap (decrease by file's iowait contribution)
       Else:
         Break (no more capacity)
```

Default behavior: only source from the **lowest speed class** (e.g. HDD).
This prevents unnecessary SSD→NVMe moves unless explicitly requested.

### 6.7 I/O Impact Projection

For each candidate:

```
iowait_share_pct = c.iowait_debt / total_iowait * 100
improvement = c.iowait_debt * (1 - 1/speed_ratio)  # time saved by moving
```

Where `speed_ratio = target_weight / source_weight`.

Display:

```
Moving 187 files would eliminate 80.3% of read-associated I/O wait.
Estimated time saved: 45.2s per observation session.
```

## 7. TUI Interface (collector.py, Rich TUI)

### 7.1 Display

A full-screen Rich TUI using `Live(screen=True, get_renderable=)` at 1-2 Hz.
Keyboard input on a separate daemon thread (own termios), isolated from Rich I/O.

Two modes: **MONITOR** (during observation) and **SELECT** (after observation).

### 7.2 MONITOR mode (during observation)

Shows live-updating file table, process stats, and tier summary:

```
╭─ dimergio — <pool> — MONITOR ───────────────────────────────────────────────────────────────────────────╮
│ 0:05:32  reads 4,812  writes 17  files 87  iowait 25.3(18.1)s  active 2  sample 10ms(100Hz)            │
│ Tiers:  nvme=10x(ssd)  ssd=4x(ssd)  r1=1x(hdd)  total(ro) 25.3s                                       │
│ Watching: /mnt/@/ssd_games /mnt/@/r1_games                                                              │
│ [a] auto-quit:OFF  [Space] stop & select  [Enter] select  [q] quit                                      │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ PROCESS              READS  WRITES  IOWAIT  STATUS                                                       │
│ everspace2.exe        3,923      12  12.348  run                                                          │
│ wineserver              877       0   2.104  run                                                          │
│ signal-desktop           12       5   0.032  run                                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ # READS   IOWAIT    SIZE   BRANCH  FILE                                                                  │
│ 1 4,521  22.600  12.5MB   ssd     Data/textures/grass.dds                                                │
│ 2 3,892  19.500   438MB   r1      Data/meshes/rock.nif                                                   │
│ 3 2,891  14.500  2.1GB    r1      Data/main.ba2                                                          │
│ 4 2,100   8.200  128MB    ssd     Data/sounds/music.pak                                                  │
│ 5 1,800   7.100   64MB    r1      Data/shaders/compiled.glsl                                              │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Tier: reads  nvme    0 (0%)  ssd 2,400 (50%)  r1 2,412 (50%)                                              │
│       iowait  nvme 0.0s(0%)  ssd  7.2s(28%)  r1 18.1s(72%)                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Read-only during monitoring. No actions, no selection. Press `q` to quit (with
confirmation). Press `Space` or `Enter` to stop monitoring and switch to SELECT mode.

### 7.3 SELECT mode (after observation)

File selection with branch targeting. Each file gets a FROM (current) and TO (target) column.
Color-coded by speed class: blue=HDD, teal=SSD, green=NVMe.

```
╭─ dimergio — <pool> — SELECT ────────────────────────────────────────────────────────────────────────────╮
│ 0:05:32  reads 4,812  writes 17  files 87  iowait 25.3(18.1)s  active 2                                │
│ Est. iowait: 25.3s  Move time: ~2.1s  Space required: 0B                                               │
│ Tiers:  nvme=10x(ssd)  ssd=4x(ssd)  r1=1x(hdd)  total(ro) 25.3s                                       │
│ [↑↓] highlight  [Space] rotate  [m] monitor  [0-9] mark  [Enter] preview  [q] quit                       │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ # FROM        TO     WRITES  IOWAIT    SIZE  FILE                                                        │
│ 1  ssd   ──→  nvme       0  22.600  12.5MB  Data/textures/grass.dds                                      │
│ 2  r1    ──→  ssd        0  19.500   438MB  Data/meshes/rock.nif                                         │
│ 3  r1    ──→  ssd        3  14.500  2.1GB  Data/main.ba2                                                 │
│ 4  ssd   ──→  nvme       0   8.200  128MB  Data/sounds/music.pak                                          │
│ 5  r1    ──→  ssd        0   7.100   64MB  Data/shaders/compiled.glsl                                     │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Tier: reads  nvme    0 (0%)  ssd 2,400 (50%)  r1 2,412 (50%)                                              │
│       iowait  nvme 0.0s(0%)  ssd  7.2s(28%)  r1 18.1s(72%)                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

FROM shows `...` if file exists on multiple branches (prefixed). Files with writes > 0
are shown in dim text (shift+# skips them).

### 7.4 User actions (SELECT mode)

- **↑ / ↓**: Move the highlight cursor to a file
- **Space**: Rotate the highlighted file's target branch forward (cycles through branches)
- **`m`**: Return to MONITOR mode (restarts fatrace)
- **0-9**: Mark file's target branch (color-coded)
- **Shift+0-9**: Mark all files above (higher iowait) — files with writes skipped
- **`-`**: Clear mark on selected file
- **`Enter`**: Open PREVIEW panel (ordered, branch-color-coded list + total bytes)
- **`q`**: Quit (confirmation if files are marked)
- **PageUp/PageDown**: Scroll by visible rows
- **Home/End**: Jump to top/bottom
- **`[`**: Decrease sample interval by 5ms (faster, min 5ms)
- **`]`**: Increase sample interval by 5ms (slower)

### 7.4.1 PREVIEW panel

Pressing `Enter` in SELECT (with ≥1 marked file) opens a PREVIEW panel. Moves are
listed in iowait-debt order, with FROM/TO columns color-coded by speed class and a
per-file size plus a total. `Enter` confirms and calls `execute_move_plan()`;
`Esc`/`q` returns to SELECT.

### 7.4.2 Post-move summary and free prompt

After `execute_move_plan()` returns, `cmd_watch` prints a color-coded operations
table and the total bytes copied — shown in **MB**, or **GB** when over 10GB. It
then prompts whether to free the redundant renamed originals (`_dimergio_` prefix).
Answering `y` deletes them and removes the state entries (so they leave `undo`);
`N` keeps them for later `cleanup` or `undo`. The prompt appears whenever at least
one file was moved, even if zero bytes were copied (rename-only moves).

### 7.5 Scrolling

Visible rows clamped to 8-25 based on terminal height. Scroll position tracked
via `_file_scroll` offset. Arrow keys, PageUp/PageDown, Home/End navigate.

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
  1. Source branch = pool.branches[file.branch_idx]; target = pool.branches[target_branch_idx]
  2. If file already on target branch (StateManager lookup)
     → smart rename (swap prefix, no copy, 0 bytes)
  3. Otherwise: copy + verify + rename + record (bytes = file size)
```

### 8.2 Smart rename

If a file was previously moved to the target branch (per StateManager),
the copy is skipped. The prefixed original is renamed back:

```
_dimergio_grass.dds → grass.dds  (on target branch)
```

This handles the case where:
- User moved file HDD → SSD
- Later, file is on SSD and user wants to move SSD → NVMe
- File already exists on SSD (as `_dimergio_grass.dds`)
- Just rename it back — no copy needed

### 8.3 Copy and rename pipeline

For files NOT already on the target branch:

```
1. COPY  → shutil.copy2(src_path, dst_path) on branch
2. VERIFY → os.stat(src) vs os.stat(dst): size must match
3. VERIFY (optional) → SHA256 source, SHA256 destination, compare (--verify flag)
4. RENAME → os.rename(src, src.with_name(f"{prefix}{src.name}"))
5. RECORD → state.add(MoveEntry)
```

### 8.4 Path resolution

Given a file path through mergerfs (`/mnt/pool/Data/file.ba2`):

- **Source (slow branch)**: `slow_branch_path + relative_path`
  e.g. `/mnt/slow/Data/file.ba2`

- **Target (fast branch)**: `fast_branch_path + relative_path`
  e.g. `/mnt/fast/Data/file.ba2`

Relative path = path relative to the pool root.

### 8.5 Directory creation

On the target branch: `os.makedirs(target_dir, exist_ok=True)`
Copy permission bits + ownership from the source directory (`shutil.copymode`,
`shutil.copystat` on the directory).

### 8.4 Copy

`shutil.copy2(src_path, dst_path)` preserves:
- Permission bits
- Last access/modification times
- Extended attributes
- Ownership (if running as root, otherwise best-effort)

### 8.5 Verification

```python
src_stat = os.stat(src_path)
dst_stat = os.stat(dst_path)
assert src_stat.st_size == dst_stat.st_size
# Optional: pass --verify for full SHA256 verification
```

### 8.6 Rename

```python
parent = src_path.parent
renamed = parent / f"{prefix}{src_path.name}"
os.rename(src_path, renamed)
```

Default prefix: `_dimergio_`. Configurable in config.

Result: mergerfs sees only the fast-branch copy (the slow-branch original
has a different name).

### 8.7 Rollback on failure

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

### 8.8 Progress output

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
  dimergio cleanup --pool /mnt/pool
to delete the originals.
```

## 9. State Management (state.py)

### 9.1 State directory

```
~/.local/share/dimergio/
├── config.json
├── state.pool.json
└── checkpoint.pool.json    # crash recovery checkpoint
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

### 9.3 Checkpoint format

Same schema but written during collection (every 60s). The checkpoint saves
the accumulator state to survive crashes. On restart, the checkpoint is
loaded and new data is merged.

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
  watch      Run fatrace, collect, analyze, and select files to move
  analyze    Analyze existing fatrace log
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
  --sudo                Run fatrace via sudo (needs CAP_SYS_ADMIN for fanotify)
  --iowait-interval N   I/O wait sampling interval in ms [default: 10]
  --verify              SHA256-verify each file after copy
  --no-interactive      Disable interactive PID monitor (headless mode)
  --verbose, -v         Log raw fatrace lines + parsing decisions to stderr
  --version             Show version and exit
```

### 10.2 `dimergio analyze`

```
dimergio analyze --log PATH [options]

Options:
  --log PATH            Path to fatrace log file (required)
  --pool PATH           Mergerfs pool mount point [default: /mnt/games]
  --data PATH           Restrict to reads under this path
  --verify              SHA256-verify each file after copy
```

### 10.3 `dimergio status`

```
dimergio status [--pool PATH]
```

Shows all migrations with status (verified/pending/problem), moved date, and file size.

### 10.4 `dimergio cleanup`

```
dimergio cleanup [--pool PATH]
```

Walks through unverified migrations older than 14 days and asks you to
confirm each one.

### 10.5 `dimergio undo`

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
  "state_dir": "~/.local/share/dimergio",
  "cleanup_days": 14,
  "default_pool": "/mnt/games",
  "checkpoint_interval_s": 60
}
```

Created on first run with defaults if absent.

## 12. Python 3.10+ Usage

- `match/case` for CLI dispatch and fatrace line parsing (3.10)
- `Path` from `pathlib` for all path manipulation
- `timezone.utc` for timestamps
- `@dataclass(slots=True)` for model objects (3.10)
- `|` union types in annotations (3.10)
- `Rich` for TUI: `Live`, `Table`, `Panel`, `Console`, `Prompt`, `Confirm`
- `threading.Thread(daemon=True)` for keyboard reader and iowait sampler
- `termios`/`tty` for raw keyboard input (isolated from Rich I/O)
- `pyyaml` for stats persistence (`.dimergio.yaml` on largest branch)

## 13. Non-goals

- No daemon or persistent service
- No web UI or API
- No support for non-Linux systems
- No automatic migration without user confirmation
- No support for mergerfs pools the tool did not discover at startup
- No file integrity verification beyond size comparison (optional SHA256 with --verify)
- No byte-range tracking (fatrace does not output byte counts)
- No actions during monitoring — TUI is read-only during observation
