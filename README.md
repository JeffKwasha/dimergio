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
- `fatrace` — file access tracing (full paths, mount scoping, UID filtering)
- `mergerfs` — FUSE pool with at least two branches
- Python **≥3.10** (stdlib only; installed via `uv`)

## Install

```bash
uv tool install .
```

## Workflow

**1. Observe** — run your program through dimergio:

```bash
dimergio watch --pool /mnt/pool --data /mnt/pool/Data
```

dimergio spawns `fatrace` (scoped to the pool mount with `-c`, filtered to
read events with `-f R`, and your UID), samples `io_ticks` from
`/sys/block/*/stat` at 100 Hz, and merges each read event with the device
busy% at that moment.

Hit Ctrl C when done (or use `--pid` for auto-stop when the process exits).

**2. Review** — an interactive table sorted by I/O debt:

```
=== dimergio — pool: POOL (3m 14s observed) ===

Branches:
  ssd_branch      943G / 712G free   dm-7  SSD  [FAST]
  hdd_branch       15T /  4.5T free  dm-3  HDD  [SLOW]

  #  Reads  %R    IOWait  %IO    Cum%IO   Size    File
─── ────── ───── ──────── ────── ──────── ─────── ────────────────
  1  4,521  9.9%  22.6    9.1%   9.1%    12.5MB  Data/textures/...
  2  3,892  8.5%  19.5    7.9%  17.0%    438MB   Data/meshes/...
 38  1,204  2.6%   6.0    2.4%  80.3%    891MB   Data/terrain/...
...

  5 files cover 80.3% of IOWait (248 units)
  187 candidate files on slow branch(es), 45832 total reads

  [I]owait  [R]eads  [D]ensity  number=N  [q]uit > 38
  Move top 38 files (80.3% of IOWait, ~1.8GB to copy). Continue? [Y/n]:
```

| Input | Action |
|---|---|
| `N` (number) | Move top N files |
| `I` | Re-sort by I/O debt (default) |
| `R` | Re-sort by read count |
| `D` | Re-sort by read density (reads/s) |
| `q` | Quit without moving |

**3. Migrate** — files are copied to the fast branch, verified (size), and
the originals are renamed with a `_dimergio_` prefix. mergerfs now sees
only the fast-branch copy. Pass `--verify` for SHA256 comparison.

```
Moving 38 files to ssd_branch...

  1/38 ✓ Data/textures/grass.dds                 12.5MB  → ssd_branch
  2/38 ✓ Data/meshes/rock.nif                   438.0MB  → ssd_branch
  3/38 ✗ Data/main.ba2                          2.1GB    disk full
  4/38 ✓ Data/terrain/chunk_05.bin             891.0MB   → ssd_branch
  5/38 ✓ Data/terrain/chunk_06.bin             891.0MB   → ssd_branch
  ...

Done: 37 moved, 1 failed.
Original files renamed with prefix '_dimergio_'.
Originals are NOT deleted. See `dimergio cleanup --pool /mnt/pool`.
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

| Command | Description |
|---|---|
| `watch` | Live trace → collect → analyze → move |
| `analyze` | Offline: parse a saved fatrace log |
| `status` | Show migration state for a pool |
| `cleanup` | Walk unverified migrations, confirm or undo |
| `undo` | Restore originals, remove copies |

### `dimergio watch [options]`

| Flag | Default | Description |
|---|---|---|
| `--pool PATH` | `/mnt/games` | Mergerfs pool mount point |
| `--data PATH` | pool root | Restrict tracking to files under this directory |
| `--pid N` | — | Auto-stop when PID N exits (polls `/proc/N/status`) |
| `--process NAME` | — | Auto-stop when process NAME stops reading for 30s |
| `--sudo` | — | Prepend `sudo` to the `fatrace` invocation |
| `--iowait-interval N` | `10` | I/O wait sampling interval in ms (default 100 Hz) |
| `--verify` | — | SHA256 each file after copy; stops on mismatch |

### `dimergio analyze --log PATH [options]`

| Flag | Default | Description |
|---|---|---|
| `--log PATH` | *required* | Path to a fatrace log file |
| `--pool PATH` | `/mnt/games` | Mergerfs pool mount point |
| `--data PATH` | pool root | Restrict tracking to files under this directory |
| `--verify` | — | SHA256 each file after copy |

Analyzes a previously-saved fatrace log. Since there is no live I/O wait
data, ranking falls back to **read count** automatically.

### `dimergio status --pool PATH`

List all moved files for a pool with status, move date, and size.

### `dimergio cleanup --pool PATH`

Walks through unverified migrations older than `cleanup_days` (default 14).
For each file, asks if the program still works:

| Answer | Action |
|---|---|
| `Y` or Enter | Delete the renamed original on the slow branch; mark verified |
| `n` | Undo: restore original, remove copy from fast branch |
| `skip` | Leave it for next time |

### `dimergio undo --pool PATH [--all]`

Restore renamed originals on the slow branch and remove copies on the
fast branch. Without `--all`, shows a numbered list and prompts for
selections (e.g. `1,3` or `all`). State entries are removed after undo.

## Performance

### Fatrace overhead

`fatrace` hooks the kernel's `fanotify` API — it receives a message per
file operation but does not poll or intercept I/O. CPU cost is negligible.
The primary bottleneck is parsing its output stream, which dimergio does
line-by-line in a dedicated thread.

### I/O wait sampling

`io_ticks` (field 9 in `/sys/block/*/stat`) is sampled at 100 Hz by
default. Each sample reads one file per branch device — cost is ~microseconds.
Tune with `--iowait-interval` if needed.

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
  "default_pool": "/mnt/games",
  "checkpoint_interval_s": 60,
  "iowait_interval_ms": 10
}
```

| Key | Default | Description |
|---|---|---|
| `prefix` | `_dimergio_` | Prefix for renamed originals on the slow branch |
| `state_dir` | `~/.local/share/dimergio` | Where state and checkpoint files live |
| `cleanup_days` | `14` | Age threshold (days) for `dimergio cleanup` |
| `default_pool` | `/mnt/games` | Pool used when `--pool` is omitted |
| `checkpoint_interval_s` | `60` | How often the accumulator is saved during collection |
| `iowait_interval_ms` | `10` | Sampling interval for `/sys/block/*/stat` |

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

dimergio runs as your user. It does not use `sudo` internally. If
`fatrace` requires root on your system, pass `--sudo`.

The **target branch** must be writable by your user. If you see
`Permission denied`, ensure group write permission on the branch mount:

```bash
chmod -R g+w /mnt/ssd_branch
```

## Pool Discovery

dimergio discovers mergerfs pools by scanning `/proc/*/cmdline` for
mergerfs processes (not via `/proc/mounts`, because FUSE filesystems
don't expose branch paths in mountinfo). Each branch is mapped to its
block device (`dm-X`) via `/proc/mounts` and checked for the
`rotational` flag in `/sys/block/*/queue/rotational`.

The fastest branch is always the first non-rotational (SSD/NVMe)
branch with the most free space. If all branches are rotational, the
one with the most free space is used.

## Architecture

```
FATRACE (subprocess)           IOWAIT SAMPLER (background thread)
  stdout line-by-line           /sys/block/*/stat every 10ms
         │                                    │
         ▼                                    ▼
   FileAccumulators ← merge path + device busy% at time of read
         │
         ▼
   Analysis: 80/20 ranking by I/O debt
         │
         ▼
   Interactive selection → copy → verify (size ± SHA256) → rename
```

## Key Concepts

- **I/O debt** — per-file score: sum of device busy% at each read event.
  Higher = more I/O impact. Default ranking metric.
- **Read rank** — simple event count. Useful for offline logs without
  I/O wait data (auto-fallback).
- **Density** — reads per second (total / span). Identifies files
  accessed in rapid bursts.
- **80/20 threshold** — the smallest set of top-ranked files whose
  cumulative I/O debt ≥ 80 % of the total.

## Why not...

- **fio / hdparm / dd?** — Synthetic benchmarks don't measure real I/O
  patterns. dimergio measures actual reads during actual usage.
- **lsof / inotify?** — `fatrace` provides full file paths,
  mount-scoped output, and per-UID filtering out of the box with less
  overhead than recursive inotify watches.
- **automatic balancing?** — The user always reviews and confirms
  migrations. dimergio is an assistant, not an autonomous balancer.
