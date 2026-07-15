# dimergio

## Purpose

Linux has some great options for IO caching... At the high end.
LVM2 cache is good, but if the drive dies you're in for some pain.
ZFS is also great... if you have plenty of memory...
Bcachefs is very promising, but not ready yet.

So if you want to turbo charge a pool of spinning rust with a bit of NAND what can you do?

For many mergerfs fills this gap... But there's no system for distributing files.

dimergio was written (poorly) to perform the analysis in realtime on a live system and
distribute files between mergerfs component filesystems in a bid to squeeze the most
user experience out of your precious NAND.

The motivating usecase is game data files which can easily consume 50GB per title and expect
random access to be nearly instant.  Devs assume everyone has their library on a Gen4 nvme.
But the majority of game data is rarely used, while a few files are indispensable.
So dimergio aims to watch and notice which files have the most bang for your bytes.
Keeping writable and rarely read files on a second disk can increase IOPS and throughput 
if file distribution is ideal, and keeping unchanging files on NAND preserves lifespan.

## Method

Watch file reads via `fatrace`, correlate with device I/O wait, and
migrate heavy-read files from slow → fast [mergerfs](https://github.com/trapexit/mergerfs)
branches. No write benchmarking, no hardware assumptions — all heuristics
from actual observation.

## Requirements

- **Linux** (kernel 6.x)
- `fatrace` — file access tracing (full paths, UID filtering)
- `mergerfs` — FUSE pool with at least two branches
- Python **≥3.10** with `rich` library

## Install

```bash
uv pip install -e .
```

## Workflow

**1. Observe** — run your program through dimergio:

```bash
dimergio watch --pool /mnt/pool --data /mnt/pool/Data --pid 12345 --sudo
```

dimergio spawns `fatrace -f RW -u -t -p <pid>`, samples `io_ticks` from
`/sys/block/*/stat` at 100Hz, and merges each read/write event
with the device busy% at that moment.

The Rich TUI shows live process stats, file table, and tier summary.
Hit `Space` or `Enter` when done to switch to SELECT mode (or `q` to quit).

**2. Select** — in SELECT mode, mark files for migration:

```
# FROM        TO     WRITES  IOWAIT    SIZE  FILE
1  r1    ──→  ssd        0  22.600  12.5MB  Data/textures/grass.dds
2  r1    ──→  ssd        0  19.500   438MB  Data/meshes/rock.nif
```

- Press `↑` / `↓` to highlight a file (cursor)
- Press `Space` to rotate the highlighted file's target branch forward
- Press `m` to return to MONITOR mode (restarts fatrace)
- Press `0-9` to mark a file's target branch (color-coded by speed class)
- Press `Shift+0-9` to mark all files above (higher iowait)
- Press `-` to clear a mark, `c` to clear session stats
- Press `Enter` to preview the moves (ordered, color-coded by branch)
- Press `q` to quit (with confirmation if files are marked)

On `Enter` a **PREVIEW** panel lists every planned move, ordered by iowait
debt and color-coded by destination branch, with the total bytes to copy.
`Enter` applies the moves; `Esc` returns to SELECT.

**3. Migrate** — files are copied to the target branch, verified (size), and
the originals are renamed with a `_dimergio_` prefix. mergerfs now sees
only the fast-branch copy. Smart rename: if file already on target branch,
just swaps prefix (no copy needed).

After the operations complete, a color-coded operations list and the total
bytes copied (MB, or GB if over 10GB) are shown. You are then asked whether
to free the redundant renamed originals — say `y` to delete them (this also
removes them from `undo`), or `N` to keep them for later `cleanup`/`undo`.

```
Executing 2 moves...
  1/2 ✓ Data/textures/grass.dds                    r1 → ssd
  2/2 ✓ Data/meshes/rock.nif                       r1 → ssd

Done: 2 moved, 0 failed.
Original files renamed with prefix '_dimergio_'.
```

**4. Verify** — restart your program and test. If everything works:

```bash
dimergio cleanup --pool /mnt/pool
```

Walks through unverified migrations older than 14 days and asks you to
confirm each one. If something breaks:

```bash
dimergio undo --pool /mnt/pool
```

Stats are saved to `.dimergio.yaml` on the largest branch root when the TUI exits.
They persist across sessions and track read/write counts and iowait per file.

## Commands

```
dimergio <command> [options]

Commands:
  watch      Run fatrace, collect, analyze, and select files to move
  analyze    Analyze existing fatrace log
  status     Show migration state
  cleanup    Verify old migrations and clean up originals
  undo       Undo migrations
```

### watch options

| Flag | Default | Description |
|---|---|---|
| `--pool PATH` | `/mnt/games` | Mergerfs pool mount point |
| `--data PATH` | pool root | Restrict tracking to files under this directory |
| `--pid N` | — | Auto-stop when PID N exits |
| `--process NAME` | — | Auto-stop when process NAME stops reading |
| `--sudo` | — | Run fatrace via sudo (needs CAP_SYS_ADMIN) |
| `--iowait-interval N` | 10 | I/O wait sampling interval in ms |
| `--verify` | — | SHA256-verify each file after copy |
| `--no-interactive` | — | Headless mode (no TUI) |
| `--verbose`, `-v` | — | Log raw fatrace lines to stderr |
| `--version` | — | Show version and exit |

## Performance

### Fatrace overhead

`fatrace` hooks the kernel's `fanotify` API — it receives a message per
file operation but does not poll or intercept I/O. CPU cost is negligible.
The primary bottleneck is parsing its output stream, which dimergio does
line-by-line in a dedicated thread.

### I/O wait sampling

`io_ticks` (field 9 in `/sys/block/*/stat`) is sampled at 100Hz (10ms) by
default. The sample interval is adjustable at runtime via `[` and `]` keys
(5ms increments, minimum 5ms = 200Hz max). Each sample reads one file per
branch device — cost is ~microseconds.

### Memory

dimergio accumulates one `FileAccumulator` object (~200 bytes) per unique
file path observed. A program touching 200,000 files uses ~40 MB of RAM.
No file contents are ever read into memory during collection.

### `--verify` overhead

The verify pipeline:
```
hash(src) → copy2 → hash(dst) → compare
```

- `hash(src)` pulls the source into the kernel page cache (first read
  from disk)
- `copy2` reads from the page cache (hot, no disk I/O) and writes to
  destination
- `hash(dst)` reads from the page cache (just written)

Result: **no extra disk I/O** beyond the copy itself. CPU cost is
SHA256 at ~1 GB/s/core. For a 50 GB migration, expect ~50 s of
additional CPU time (per-file sequential hashing).

### Ranking

All ranking is derived from the accumulator dict — O(*n* log *n*) sort
on candidate count.

## Configuration

On first run dimergio creates `~/.config/dimergio/config.json` with
defaults:

```json
{
  "prefix": "_dimergio_",
  "state_dir": "~/.local/share/dimergio",
  "cleanup_days": 14,
  "default_pool": "/mnt/games"
}
```

| Key | Default | Description |
|---|---|---|
| `prefix` | `_dimergio_` | Prefix for renamed originals on the slow branch |
| `state_dir` | `~/.local/share/dimergio` | Where state and checkpoint files live |
| `cleanup_days` | `14` | Age threshold (days) for `dimergio cleanup` |
| `default_pool` | `/mnt/games` | Pool used when `--pool` is omitted |

Missing keys are filled from defaults — you only need to list overrides.

## State & Crash Recovery

- **State file:** `~/.local/share/dimergio/state.<pool>.json` — list of
  every migration with source, target, timestamps, and verification status
- **Checkpoint:** `~/.local/share/dimergio/checkpoint.<pool>.json` —
  accumulator snapshot written every 60 s during collection. If dimergio
  crashes mid-session, the checkpoint is loaded on restart and new data
  is merged transparently.
- Both files use atomic writes (write to `.tmp`, then `rename`).

## Safety

- Originals are **renamed** (prefixed), **never deleted**, until you
  explicitly confirm via `dimergio cleanup`.
- Every copy is verified by **size** before the original is renamed.
- Pass `--verify` for full SHA256 verification — stops the process and
  preserves both copies on mismatch.
- Non-verify errors (disk full, permission denied) skip the file and
  continue; the partial copy is cleaned up.
- Full undo is always available via `dimergio undo`.

## Permissions

dimergio runs as your user by default. Fatrace requires `CAP_SYS_ADMIN`
for fanotify access. Use `--sudo` to run fatrace via sudo. The tool
checks for cached sudo credentials before starting the TUI.

The **target branch** must be writable by your user (or by root if
using `--sudo`).

## Pool Discovery

dimergio discovers mergerfs pools by scanning `/proc/*/cmdline` for
mergerfs processes (not via `/proc/mounts`, because FUSE filesystems
don't expose branch paths in mountinfo). Each branch is mapped to its
block device (`dm-X`) via `/proc/mounts` and checked for the
`rotational` flag in `/sys/block/*/queue/rotational`.

Branches are classified into speed tiers (nvme, ssd, hdd) based on
the rotational flag and device name prefix. Short labels are derived
from the last path component with common suffixes stripped.

Fatrace runs against raw btrfs volume mounts (not FUSE), and paths
are remapped to pool-relative via the volume map. This handles
symlinks, bind mounts, and mergerfs mount aliases transparently.

## Architecture

```
fatrace -f RW -u -t -p <pid>
    │
    ▼
collector._parse_line()          ─── ReadEvent (timestamp, proc, pid, uid, event, path)
    │                                PidStat (process_name from /proc/<pid>/comm)
    │
    ├── collector._sample_iowait() ─── iowait_sec (delta, fairness-weighted)
    │
    ├── collector._accumulate()    ─── FileAccumulator.total_reads++  (R events)
    │                                  FileAccumulator.write_count++   (W events)
    │                                  FileAccumulator.iowait_debt += iowait_sec
    │
    ├── collector._update_pid_stats() ── PidStat.read_count++, pid_iowait_sec[pid] += iowait_sec
    │                                    PidStat.write_count++ on W events
    │
    └── TUI (MONITOR mode) ────── Read-only observation
                                    │
                                    ├── Space ──── Stop → SELECT mode
                                    └── Enter ──── Switch to SELECT mode

SELECT mode ────────────────────── Branch marking + file selection
    │
    ├── ↑/↓ ────── Highlight file (cursor)
    ├── Space ──── Rotate highlighted file's target branch
    ├── m ──────── Back to MONITOR mode (restarts fatrace)
    ├── 0-9 ────── Mark target branch (color-coded)
    ├── Shift+0-9  Mark above (skip writes)
    ├── Enter ──── Preview → confirm → execute_move_plan()
    └── q ──────── Quit (confirmation if marked)
```

## Key Concepts

- **I/O debt** — per-file score: sum of iowait_sec at each read event.
  Higher = more I/O impact. Default ranking metric.
- **Speed class** — auto-detected tier: `nvme`, `ssd`, or `hdd`.
- **Speed weight** — relative throughput: nvme=10, ssd=4, hdd=1.
- **Target distribution** — ideal iowait split: weight[i] / sum(weights).
- **Gap** — target - actual per branch. Positive = spare capacity.
- **Multi-tier moves** — files move from overloaded branches to branches with
  positive gap, proportional to their capacity.
- **Smart rename** — if file already on target branch, just swap prefix (no copy).
- **Stats persistence** — `.dimergio.yaml` on largest branch root, additive merge across sessions.

## Why not...

- **fio / hdparm / dd?** — Synthetic benchmarks don't measure real I/O
  patterns. dimergio measures actual reads during actual usage.
- **lsof / inotify?** — `fatrace` provides full file paths,
  per-UID filtering out of the box with less overhead than recursive
  inotify watches.
- **automatic balancing?** — The user always reviews and confirms
  migrations. dimergio is an assistant, not an autonomous balancer.
