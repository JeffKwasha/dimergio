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
| **branch** | A filesystem path that is a member of a mergerfs pool |
| **fast branch** | The branch with the lowest device I/O busy-time — empirically measured during the observation session |
| **slow branch** | Any branch that is not the fast branch |
| **candidate** | A file on a slow branch whose migration could reduce observed I/O wait |
| **iowait debt** | A per-file score; the sum of device-busy percentage at the time each read of that file occurred. Higher = more I/O impact. |
| **observation session** | The period during which fatrace is running and the program is active |

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       dimergio CLI                           │
│  watch │ analyze │ status │ cleanup │ undo                    │
└─────────────────────────────────────────────────────────────┘
         │
         ├── pool.py         Mergerfs discovery, branch mapping
         ├── collector.py    fatrace subprocess + iowait sampler
         ├── eventlog.py     ReadEvent stream, FileAccumulator
         ├── analyze.py      Aggregation, ranking, 80/20
         ├── selector.py     Interactive table + prompt
         ├── mover.py        Copy → verify → rename → state
         ├── state.py        JSON state per pool
         └── config.py       ~/.config/dimergio/config.json
```

### 3.1 Data Flow

```
FATRACE (subprocess)                IOWAIT SAMPLER (thread)
  stdout line-by-line                /proc/diskstats every 50ms
         │                                    │
         ▼                                    ▼
  ┌─────────────────────────────┐
  │       eventlog.py           │
  │  merges into per-file       │
  │  FileAccumulator records    │
  └──────────┬──────────────────┘
             │ Ctrl+C / process exit
             ▼
  ┌─────────────────────────────┐
  │       analyze.py            │
  │  80/20 by iowait debt       │
  │  rank candidates             │
  └──────────┬──────────────────┘
             │ interactive
             ▼
  ┌─────────────────────────────┐
  │       selector.py           │
  │  table + prompt → selections │
  └──────────┬──────────────────┘
             │ user confirms
             ▼
  ┌─────────────────────────────┐
  │       mover.py              │
  │  copy → verify → rename     │
  │  record in state            │
  └─────────────────────────────┘
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

### 4.3 Identify fast branch

No write benchmark. The fast branch is the one with the LOWEST `io_ticks`
accumulation per read-byte during the observation session. This is determined
**after** collection, by comparing per-branch `total_read_io_time / total_bytes_read`.

Pre-session hint: the branch where `queue/rotational == 0` (SSD/NVMe) is
presumed fast. The empirical data may override this.

## 5. Data Collection (collector.py, eventlog.py)

### 5.1 Subprocess: fatrace

```
fatrace -c -f R -u -t -t
```

| Flag | Purpose |
|---|---|
| `-c` | Only events on the current mount (CWD set to pool root) |
| `-f R` | Read events only |
| `-u` | Include `[uid:gid]` for user filtering |
| `-t -t` | Two timestamps → epoch seconds.microseconds |

Output format (per line):

```
TIMESTAMP PROCESS(PID)[UID:GID]: EVENT PATH

Example:
  1748573021.456789 myapp(12345)[1000:1000]: R /mnt/pool/Data/file.ba2
```

Lines for non-read events, different UIDs, or paths outside the pool are
discarded. A line is parsed as:

```python
match line.split():
    case [ts_str, rest]:
        ts = float(ts_str.removesuffix('.'))
        # rest: "process(pid)[uid:gid]: EVENT /path"
        # regex or split parse
```

### 5.2 I/O Wait Sampler (background thread)

Every **50ms**, read `/sys/block/<dm-X>/stat` for each branch device.

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

Store a **rolling window** (last 3 samples, 150ms) of busy_pct for smoothing.
Maintain a thread-safe reference to the latest smoothed busy_pct per branch.

### 5.3 Event Capture

On each fatrace line (from the main thread):

1. Parse: timestamp, pid, uid, event_type, path
2. **Filter**: skip unless event_type is `R`, uid matches current user, path starts with data root
3. **Determine branch**: which branch path is a prefix of this file's path?
4. **Look up current iowait**: read the latest smoothed `io_busy_pct` for that branch
5. **Accumulate**: update the FileAccumulator for this path

### 5.4 FileAccumulator

```python
@dataclass
class FileAccumulator:
    path: Path
    branch: str              # which mergerfs branch (identifies the device)
    total_reads: int = 0
    first_seen: float = 0.0   # epoch seconds
    last_seen: float = 0.0
    iowait_debt: float = 0.0  # sum of io_busy_pct at each read event
    _peak_read_rate: float = 0.0  # for secondary heuristic

    def observe(self, ts: float, busy_pct: float) -> None:
        self.total_reads += 1
        self.iowait_debt += busy_pct
        if not self.first_seen:
            self.first_seen = ts
        self.last_seen = ts
```

Three ranking metrics are derived from this:

| Metric | Formula | Meaning |
|---|---|---|
| **iowait debt** | `accum.iowait_debt` | Reads × device busy % — the primary metric |
| **read weight** | `accum.total_reads` | Simple count of read events |
| **density** | `accum.total_reads / (accum.last_seen - accum.first_seen + ε)` | Read rate; high density suggests frequent, possibly cached, access |

### 5.5 Checkpoint

Every 60 seconds, the accumulator dict is serialized to a JSON checkpoint
in `~/.local/share/dimergio/checkpoint.<pool>.json` for crash recovery.
On restart/analyze, the checkpoint is loaded and new events are merged.

### 5.6 Auto-quit

If `--pid PID` is given:
- A separate thread polls `/proc/<pid>/status` every 1 second
- When the PID disappears → set `stop_flag = True`
- The main loop checks `stop_flag` after each fatrace line

If `--process NAME` is given:
- After each fatrace event, update `last_event_time[process_name]`
- When no events from `NAME` have arrived in 30 seconds → stop

Without either: let fatrace run until Ctrl+C or Ctrl+D is received.

On stop:
1. TERM the fatrace subprocess
2. Read any remaining buffered output
3. Write final checkpoint
4. Proceed to analysis

### 5.7 Phase tracking in output

While collecting, print a short periodic status (every 30s) to stderr:

```
[dimergio] collecting... 3m14s | 45,832 reads | 187 files tracked | hdd_branch busy: 34%
```

No progress bar. No TUI during collection. The TUI appears only during selection.

## 6. Analysis (analyze.py)

### 6.1 Input

The accumulated `dict[Path, FileAccumulator]` from the event log.

### 6.2 Per-file enrichment

For each accumulator, look up:

- **File size**: `os.stat(accum.path).st_size` (cached in the accumulator)
- **Branch**: already recorded
- **Is candidate**: `True` if the file is on a slow branch (not the fastest)

### 6.3 Sorting & Ranking

The user picks a ranking metric at analysis time. Default is **iowait debt**.

```python
candidates = [
    Candidate(
        path=acc.path,
        reads=acc.total_reads,
        iowait_debt=acc.iowait_debt,
        branch=acc.branch,
        file_size=acc.file_size,
    )
    for acc in accumulators.values()
    if acc.branch != fastest_branch
]
candidates.sort(key=lambda c: c.iowait_debt, reverse=True)
```

### 6.4 80/20 Analysis

```
let total_iowait = sum(c.iowait_debt for c in sorted_candidates)
let cumulative = 0
for each candidate c (in order):
    cumulative += c.iowait_debt
    c.cum_pct = cumulative / total_iowait * 100
    c.pct_of_total = c.iowait_debt / total_iowait * 100
```

The 80% threshold is: the minimum set of top-ranked files whose cumulative
iowait debt ≥ 80% of total iowait debt.

### 6.5 I/O Impact Projection

For each candidate:

```
iowait_share_pct = c.iowait_debt / total_iowait * 100
projected_improvement = iowait_share_pct  # if moved to fast branch
```

The "after" estimate: if these files are moved to the fast branch, their
reads won't contribute to the slow branch's iowait. The net improvement
is their share of total iowait. This is displayed as:

```
Moving 187 files would eliminate 80.3% of read-associated I/O wait on the slow branch.
```

This is not a wall-time guarantee — it says "this share of observed iowait
was coincident with reads to these files."

## 7. Selection Interface (selector.py)

### 7.1 Display

A single terminal table, not curses/rich — plain print with aligned columns.

```
=== dimergio — pool: GAMMAS (3m 14s observed) ===

Branches:
  ssd_branch   943G / 712G free   dm-7  {rotational: 0}
  hdd_branch    15T / 4.5T free   dm-3  {rotational: 1}

Read events on slow branch(es): 45,832

Rank metric: [I]owait debt  [R]eads  [D]ensity

  #  Reads  %R    IOWait  %IO    Cum%IO   Size    Branch    File
─── ────── ───── ──────── ────── ──────── ─────── ───────── ────────────────
  1  4,521  9.9%  22.6     9.1%   9.1%   12.5MB   hdd_branch Data/textures/...
  2  3,892  8.5%  19.5     7.9%  17.0%   438MB    hdd_branch Data/meshes/...
  3  2,891  6.3%  14.5     5.8%  22.8%   2.1GB    hdd_branch Data/main.ba2
  …
 38  1,204  2.6%   6.0     2.4%  80.3%   891MB    hdd_branch Data/terrain/...
─── ────── ───── ──────── ────── ────────
     45,832       248.4

80% IOWait threshold: 38 files (cumulative 80.3%, 248.4 unit IOWait)
```

### 7.2 User actions

```
Rank metric: [I]owait debt  [R]eads  [D]ensity  [current: I]
>
```

User enters:
- A number: move top N files
- `I`, `R`, or `D`: switch ranking metric and redisplay
- `q`: quit without moving

```
> 38
Move top 38 files (80.3% of IOWait, ~1.8GB to copy). Continue? [Y/n]:
```

### 7.3 Behavior

- After selection, the caller (cli.py `watch` or `analyze`) passes the
  selected file list to mover.py
- The display function is stateless — it takes a list of Candidate and
  a pool reference, prints, and returns a selection

## 8. Migration (mover.py)

### 8.1 Pipeline

For each selected file, in rank order:

```
1. COPY  → shutil.copy2(src_path, dst_path) on branch
2. VERIFY → os.stat(src) vs os.stat(dst): size must match
3. VERIFY (optional) → SHA256 source, SHA256 destination, compare (--verify flag)
4. RENAME → os.rename(src, src.with_name(f"{prefix}{src.name}"))
5. RECORD → state.add(file, src_branch, dst_branch, moved_at)
```

### 8.2 Path resolution

Given a file path through mergerfs (`/mnt/pool/Data/file.ba2`):

- **Source (slow branch)**: `slow_branch_path + relative_path`
  e.g. `/mnt/slow/Data/file.ba2`

- **Target (fast branch)**: `fast_branch_path + relative_path`
  e.g. `/mnt/fast/Data/file.ba2`

Relative path = path relative to the pool root.

### 8.3 Directory creation

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

commands:
  watch         Run fatrace, collect, analyze, and move files
  analyze       Analyze existing fatrace log without live collection
  status        Show migration state for a pool
  cleanup       Ask about old migrations, delete originals or undo
  undo          Restore originals and remove copies

watch options:
  --pool PATH   Mergerfs pool mount point  [default: /mnt/games]
  --data PATH   Restrict to reads under this path (default: CWD inside pool, else pool root)
  --process NAME  Auto-quit when this process exits (matches /proc/<pid>/cmdline)
  --pid N       Auto-quit when this PID exits
  --sudo        Run fatrace via sudo (needs CAP_SYS_ADMIN for fanotify)
  --no-interactive  Disable interactive PID monitor (headless mode)

analyze options:
  --log PATH    Path to existing fatrace log file
  --pool PATH   Pool mount point  [default: /mnt/games]
  --data PATH   Restrict to reads under PATH  [default: pool root]
  --iowait PATH  Path to iowait sample log (if collected separately)

status options:
  --pool PATH   Pool to query  [default: /mnt/games]

cleanup options:
  --pool PATH   Pool to clean up  [default: /mnt/games]
  --all         Process all pending, not just those past threshold

undo options:
  --pool PATH   Pool to undo moves in  [default: /mnt/games]
  --all         Undo all migrations for this pool
```

### 10.1 analyze subcommand (offline mode)

Read a previously-saved fatrace log. Requires a companion iowait sample log
for I/O debt correlation (optional — without it, ranking falls back to read
count).

If the log was collected by dimergio (checkpoint available), load the
accumulated data directly.

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

## 13. Non-goals

- No daemon or persistent service
- No web UI or API
- No support for non-Linux systems
- No automatic migration without user confirmation
- No support for mergerfs pools the tool did not discover at startup
- No file integrity verification beyond size comparison (optional SHA256 with --verify)
