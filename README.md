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
dimergio --pool /mnt/pool --data /mnt/pool/Data --pid 12345 --sudo
```

dimergio spawns `fatrace -f RW -u -t -p <pid>`, samples `io_ticks` from
`/sys/block/*/stat` at 50ms intervals, and merges each read/write event
with the device busy% at that moment.

The Rich TUI shows live process stats, file table, and tier summary.
Hit `q` when done (or use `--pid` for auto-stop when the process exits).

**2. Review** — after observation stops, the Candidate screen shows:

```
╭─ dimergio — candidate files ─────────────────────────────────────────────────────╮
│ Tier: reads  nvme    0 (0%)  ssd 2,400 (50%)  r1 2,412 (50%)                     │
│       iowait  nvme 0.0s(0%)  ssd  7.2s(28%)  r1 18.1s(72%)                     │
├──────────────────────────────────────────────────────────────────────────────────┤
│ # READS   IOWAIT    SIZE  WRITES  BRANCH  FILE                                  │
│ 1 4,521  22.600  12.5MB       0   r1     Data/textures/grass.dds                │
│ 2 3,892  19.500   438MB       0   r1     Data/meshes/rock.nif                   │
│ …                                                                                │
│ Enter file number to move, range (e.g. 1-5), or 'all'.                           │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

Select files by number, range, or `all`. Press `m` to move.

**3. Migrate** — files are copied to the target branch, verified (size), and
the originals are renamed with a `_dimergio_` prefix. mergerfs now sees
only the fast-branch copy.

```
Moving 38 files...
  1/38 ✓ Data/textures/grass.dds                 12.5MB   → ssd
  2/38 ✓ Data/meshes/rock.nif                   438.0MB   → ssd
  3/38 ✗ Data/main.ba2                            2.1GB   disk full
  …

Done: 37 moved, 1 failed. 5.2GB copied to ssd.
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

## Commands

```
dimergio [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--pool PATH` | `/mnt/games` | Mergerfs pool mount point |
| `--data PATH` | pool root | Restrict tracking to files under this directory |
| `--pid N` | — | Auto-stop when PID N exits |
| `--process NAME` | — | Auto-stop when process NAME stops reading for 30s |
| `--sudo` | — | Run fatrace via sudo (needs CAP_SYS_ADMIN) |
| `--no-interactive` | — | Headless mode (no TUI) |
| `--verbose`, `-v` | — | Log raw fatrace lines to stderr |
| `--auto-quit` | OFF | Enable auto-quit in interactive mode |
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
    └── collector._update_pid_stats() ── PidStat.read_count++, pid_iowait_sec[pid] += iowait_sec
                                          PidStat.write_count++ on W events
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

## Why not...

- **fio / hdparm / dd?** — Synthetic benchmarks don't measure real I/O
  patterns. dimergio measures actual reads during actual usage.
- **lsof / inotify?** — `fatrace` provides full file paths,
  per-UID filtering out of the box with less overhead than recursive
  inotify watches.
- **automatic balancing?** — The user always reviews and confirms
  migrations. dimergio is an assistant, not an autonomous balancer.
