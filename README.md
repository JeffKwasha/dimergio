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

- **Linux** 6.1 or later (tested on 7.0)
- `fatrace` — file access tracing (full paths, UID filtering)
- `mergerfs` — FUSE pool with at least two branches
- Python **≥3.10** with `rich` library
- `clang` + `bpftool` — only to build the optional mmap tracer

## Install

```bash
uv pip install -e .
```

The optional mmap tracer is built from source and installed on `PATH`. Skip it
if you only need fatrace reads; dimergio runs without it.

```bash
make -C src/dimergio/bpf
sudo make -C src/dimergio/bpf install
```

Set `DIMERGIO_MMAP_BIN` to use a tracer that is somewhere else on disk.

## Workflow

**1. Observe** — run your program through dimergio:

```bash
dimergio watch --pool /mnt/pool --data /mnt/pool/Data --pid 12345 --sudo
```

dimergio spawns `fatrace -f RW -u -t -t`, samples `io_ticks` from
`/proc/diskstats` at 100Hz, and charges each read event its share of the
device-busy time from the previous sampling window.

The Rich TUI shows live process stats, file table, and tier summary on a
single BROWSE screen. Observation and selection happen together — sampling
runs continuously, so there is no separate monitor step. Press `Enter` to open
the PREVIEW panel (or `q` to quit).

**2. Select** — on the BROWSE screen, mark files for migration:

```
# FROM        TO     WRITES  IOWAIT    SIZE  FILE
1  r1    ──→  ssd        0  22.600  12.5MB  Data/textures/grass.dds
2  r1    ──→  ssd        0  19.500   438MB  Data/meshes/rock.nif
```

- Press `↑` / `↓` to highlight a file (cursor)
- Press `←` / `→` to rotate the sort column (IOW/MB → IOWAIT → READS; the active column is highlighted)
- Press `Space` to rotate the highlighted file's target branch forward
- Press `Tab` (or `Home` / `End`) to switch focus between the file list and the process list
- Press `-` to clear a mark, `c` to clear session stats
- Press `Enter` to preview the moves (ordered, color-coded by branch)
- Press `q` twice within 4s to quit (first press shows a warning; any other key cancels)
- Press `[` / `]` to speed up / slow down the iowait sampling rate (5ms steps)

**Watching mmap readers.** Programs that load files with `mmap` (llama.cpp
model loaders, many emulators, data loaders) never show up in fatrace — the
kernel's fanotify API does not report mmap accesses. dimergio detects them
with a small eBPF tracer (`dimergio-mmap`, a CO-RE `filemap_fault` kprobe that
counts page faults per PID/inode). Press `m` to scan for candidate PIDs; the
scan also moves focus to the process list. `Enter` on a process toggles
watching its mmap faults; watched faults are folded into the same per-file
iowait/read ranking as fatrace reads. `Tab` or `End` returns focus to the file
list. You can start a PID under watch directly with `--mmap-pid N` (repeatable).
Without the tracer, mmap detection degrades to a no-op: `m` still lists
candidates and `Enter` reports why it cannot watch them.

One limit applies today. Faults are keyed by the inode the kernel faulted on.
A file mapped through the mergerfs mount faults on the backing file, so the
tracer reports the backing inode while the mapping in `/proc/<pid>/maps` shows
a FUSE inode. Such faults cannot be named yet, so dimergio reports the mismatch
once instead of counting them. Files mapped directly from a branch or through
the btrfs volume do resolve and count.

Files are ranked by **iowait cost per MB** (byte-weighted: file size rounded
up to the 128 KiB SSD block floor), so large slow files outrank tiny ones with
equal raw iowait. Files written during the run are excluded.

On `Enter` a **PREVIEW** panel lists every planned move, ordered by iowait
debt and color-coded by destination branch, with the total bytes to copy.
`Enter` applies the moves; `Esc` returns to BROWSE; `q` (twice) quits without applying.

**3. Migrate** — the source is resolved to the real file (a symlink is never
moved, and a path recorded through one is remapped to the file's branch),
copied to the target branch, verified by size, and the original is renamed
with a `_dimergio_` prefix. mergerfs now sees only the fast-branch copy.
Every move copies, so the reported byte total is what lands on the target.

After the operations complete, a color-coded operations list and the total
bytes copied (MB, or GB if over 10GB) are shown. The redundant renamed
originals are kept so you can test your program first; you're told how to
verify & free them later with `dimergio cleanup --pool <pool> -d` (or revert
with `dimergio undo`).

```
Executing 2 moves...
  1/2 ✓ Data/textures/grass.dds                    r1 → ssd
  2/2 ✓ Data/meshes/rock.nif                       r1 → ssd

Done: 2 moved, 0 failed.
Original files renamed with prefix '_dimergio_'.
```

**4. Verify** — restart your program and test. If everything works:

```bash
dimergio cleanup --pool /mnt/pool -d
```

Walks through unverified migrations and asks you to
confirm each one. Use `-a` to skip prompting or `-d` to change the day filter
(omitting the number means 0 days, so fresh moves are included).

```bash
dimergio undo --pool /mnt/pool
```

Stats are saved to `state_dir` when the TUI exits. They persist across sessions
and track read/write counts and iowait per file.

## Commands

```
dimergio <command> [options]

Commands:
  watch      Run fatrace, collect, analyze, and move files
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
| `--sudo` | on | Run fatrace via sudo; `--no-sudo` needs capabilities on the fatrace binary |
| `--iowait-interval N` | 10 | I/O wait sampling interval in ms |
| `--verify` | — | SHA256-verify each file after copy |
| `--no-interactive` | — | Headless mode (no TUI) |
| `--mmap-pid N` | — | Watch mmap page-fault reads of PID N (repeatable) |
| `--offline` | — | Skip collection and analyze saved stats or a log |
| `--from-log PATH` | — | Replay a captured fatrace log (with `--offline`) |
| `--verbose`, `-v` | — | Log raw fatrace lines to stderr |
| `--version` | — | Show version and exit |

## Performance

### Fatrace overhead

`fatrace` hooks the kernel's `fanotify` API — it receives a message per
file operation but does not poll or intercept I/O. CPU cost is negligible.
The primary bottleneck is parsing its output stream, which dimergio does
line-by-line in a dedicated thread.

### I/O wait sampling

`io_ticks` is read from `/proc/diskstats` at 100Hz (10ms) by default. The
sample interval is adjustable at runtime via `[` and `]` keys (5ms increments,
minimum 5ms = 200Hz max). Each sample is one read of `/proc/diskstats` for all
devices, and devices backing the same branch set are summed into one IO domain.

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
  "state_dir": "~/.local/state/dimergio",
  "cleanup_days": 14,
  "default_pool": "/mnt/games",
  "iowait_interval_ms": 10,
  "symlink_depth": 3
}
```

| Key | Default | Description |
|---|---|---|
| `prefix` | `_dimergio_` | Prefix for renamed originals on the slow branch |
| `state_dir` | `$XDG_STATE_HOME/dimergio` | Where the state file and the per-pool stats YAML live |
| `cleanup_days` | `14` | Default age threshold for `dimergio cleanup`; `-d` overrides it |
| `default_pool` | `/mnt/games` | Pool used when `--pool` is omitted |
| `iowait_interval_ms` | `10` | Initial iowait sampling interval (`--iowait-interval` overrides) |
| `symlink_depth` | `3` | Directory depth walked at startup to label files by symlink name; `0` disables the walk |

Missing keys are filled from defaults — you only need to list overrides.

## State & Persistence

- **State file:** `state.<pool>.json` under `state_dir`, a list of every
  migration with source, target, timestamps, and verification status.
- **Stats:** `<POOL>.yaml` under `state_dir`, written when a session ends
  cleanly and merged into the next session on load.
- Both use atomic writes (write to `.tmp`, then `rename`).
- A crash loses the current session's observations. Nothing is replayed from a
  partial write.

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

fatrace needs privilege. Run dimergio with `--sudo` and a sudoers rule, or
give the fatrace binary two file capabilities and pass `--no-sudo`:

```bash
sudo setcap cap_sys_admin,cap_dac_read_search+ep /usr/local/bin/fatrace
```

`cap_sys_admin` lets fanotify start. `cap_dac_read_search` lets modern fatrace
turn an event handle back into a path through `open_by_handle_at`. With only
the first, every event prints `(deleted)` and dimergio drops it, because the
parser needs a path.

The mmap tracer needs less: `sudo setcap cap_bpf,cap_perfmon+ep /usr/local/bin/dimergio-mmap`.
`cap_sys_admin` is unnecessary on 6.1 and later. Keep both binaries at mode
`0750` so the capability is not offered to every user on the machine, and
re-apply the capability after any copy, `chown`, or rebuild: the kernel strips
`security.capability` from a new inode by design.

The tool checks for cached sudo credentials before starting the TUI.

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
are remapped to pool-relative via the volume map. One resolver in
`pool.py` does this for the live TUI and for `--from-log`, so both track the
same files. Symlinks are resolved to the real file, and a file is tracked when
it lives under the data path or is reachable through a symlink there.

Branches that share a set of block devices (one btrfs pool) also share an
**IO domain**: their `io_ticks` are summed before the per-event share is
computed, because they contend for the same device queue.

## Architecture

```
fatrace -f RW -u -t -p <pid>
    │
    ▼
collector._parse_line()          ─── ReadEvent (timestamp, proc, pid, uid, event, path)
    │                                PidStat (process_name from /proc/<pid>/comm)
    │
    ├── collector.IOWaitSampler    ─── bills each window's busy time to the
    │                                  (file, pid) reads seen in that window
    │
    ├── collector._accumulate()    ─── FileAccumulator.total_reads++  (R events)
    │                                  FileAccumulator.write_count++   (W events)
    │
    ├── collector._charge()        ─── FileAccumulator.iowait_debt += billed seconds
    │
    ├── collector._update_pid_stats() ── PidStat.read_count++
    │                                    PidStat.write_count++ on W events
    │
    └── TUI (BROWSE mode) ────── Live observation + selection on one screen
                                    │
                                    ├── Enter ──── Open PREVIEW panel
                                    └── Esc/q ──── Quit

BROWSE mode ────────────────────── Branch marking + file selection
    │
    ├── ↑/↓ ────── Highlight row in the focused list
    ├── ←/→ ────── Re-sort the focused list
    ├── Tab ────── Switch focus (files ↔ processes)
    ├── Space ─── Rotate highlighted file's target branch
    ├── m ─────── Scan mmap candidates, focus processes
    ├── Enter ──── Preview → confirm → execute_move_plan()
    └── q ──────── Quit (press twice within 4s)
```

## Key Concepts

- **I/O debt** — per-file score: the device busy time billed to this
  file's reads, split by how many reads shared each window. Higher = more
  I/O impact. Default ranking metric.
- **Speed class** — auto-detected tier: `nvme`, `ssd`, or `hdd`.
- **Speed weight** — relative throughput: nvme=10, ssd=4, hdd=1.
- **Effective size** — file size rounded up to the 128 KiB SSD block floor;
  the denominator of the iowait-per-MB ranking.
- **Selection is manual** — you mark which files move where; nothing is
  selected automatically.
- **IO domain** — branches backed by the same block devices; their busy
  time is summed, so two branches on one btrfs pool cannot look independent.
- **Stats persistence** — `<POOL>.yaml` under `state_dir`, additive merge across sessions.

## Why not...

- **fio / hdparm / dd?** — Synthetic benchmarks don't measure real I/O
  patterns. dimergio measures actual reads during actual usage.
- **lsof / inotify?** — `fatrace` provides full file paths,
  per-UID filtering out of the box with less overhead than recursive
  inotify watches.
- **automatic balancing?** — The user always reviews and confirms
  migrations. dimergio is an assistant, not an autonomous balancer.
