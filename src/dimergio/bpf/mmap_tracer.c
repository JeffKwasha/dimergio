// SPDX-License-Identifier: (LGPL-2.1 OR BSD-2-Clause)
/* dimergio mmap tracer — userspace half.
 *
 * Loads the CO-RE BPF program, attaches a kprobe to filemap_fault, and
 * drains the per-(pid, inode) fault counters every sampling window, printing
 * one line per entry:
 *
 *     <pid> <ino> <count>
 *
 * Commands on stdin (no respawn needed):
 *     +<pid>      start tracing this pid
 *     -<pid>      stop tracing this pid (and clear its accumulated counts)
 *     i <ms>      change the sampling window in milliseconds
 *
 * Exits cleanly on SIGINT / SIGTERM.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "mmap_tracer.skel.h"

static volatile sig_atomic_t g_stop = 0;

/* Must match mmap_tracer.bpf.c — pid then ino, 16 bytes, no padding. */
struct fault_key {
    __u32 pid;
    __u64 ino;
};

static void handle_signal(int sig)
{
    g_stop = 1;
}

static void set_pid(struct mmap_tracer_bpf *skel, __u32 pid, int on)
{
    struct bpf_map *map = skel->maps.enabled_pids;
    if (on) {
        __u8 one = 1;
        bpf_map__update_elem(map, &pid, sizeof(pid), &one, sizeof(one), BPF_ANY);
    } else {
        bpf_map__delete_elem(map, &pid, sizeof(pid), 0);
    }
}

static void clear_pid(struct mmap_tracer_bpf *skel, __u32 pid)
{
    struct bpf_map *map = skel->maps.faults;
    struct fault_key key, next;
    int err = bpf_map__get_next_key(map, NULL, &key, sizeof(key));
    while (err == 0) {
        if (key.pid == pid)
            bpf_map__delete_elem(map, &key, sizeof(key), 0);
        err = bpf_map__get_next_key(map, &key, &next, sizeof(key));
        key = next;
    }
}

/* Read the aggregated counters, emit non-zero entries, reset them to zero. */
static void dump_faults(struct mmap_tracer_bpf *skel)
{
    struct bpf_map *map = skel->maps.faults;
    struct fault_key key, next;
    int err = bpf_map__get_next_key(map, NULL, &key, sizeof(key));
    while (err == 0) {
        __u64 value = 0;
        if (bpf_map__lookup_elem(map, &key, sizeof(key), &value, sizeof(value), 0) == 0 && value > 0) {
            __u64 zero = 0;
            bpf_map__update_elem(map, &key, sizeof(key), &zero, sizeof(zero), BPF_EXIST);
            printf("%u %llu %llu\n",
                   key.pid, (unsigned long long)key.ino, (unsigned long long)value);
        }
        err = bpf_map__get_next_key(map, &key, &next, sizeof(key));
        key = next;
    }
    fflush(stdout);
}

int main(int argc, char **argv)
{
    struct mmap_tracer_bpf *skel = NULL;
    long interval_ms = 10;
    __u32 initial_pids[1024];
    int n_initial = 0;
    int stdin_open = 1;
    int err;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--interval-ms") == 0 && i + 1 < argc) {
            interval_ms = atol(argv[++i]);
        } else if (strcmp(argv[i], "--pid") == 0 && i + 1 < argc) {
            if (n_initial < 1024)
                initial_pids[n_initial++] = (__u32)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--help") == 0) {
            fprintf(stderr,
                    "usage: mmap_tracer [--interval-ms N] [--pid N]...\n");
            return 0;
        }
    }
    if (interval_ms < 1)
        interval_ms = 1;

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    skel = mmap_tracer_bpf__open();
    if (!skel) {
        fprintf(stderr, "mmap_tracer: failed to open BPF skeleton\n");
        return 1;
    }
    err = mmap_tracer_bpf__load(skel);
    if (err) {
        fprintf(stderr, "mmap_tracer: failed to load BPF object: %s\n", strerror(err));
        goto cleanup;
    }

    for (int i = 0; i < n_initial; i++)
        set_pid(skel, initial_pids[i], 1);

    err = mmap_tracer_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "mmap_tracer: failed to attach kprobe: %s\n", strerror(err));
        goto cleanup;
    }

    fprintf(stderr, "mmap_tracer: attached filemap_fault (interval %ld ms)\n", interval_ms);
    fflush(stderr);

    while (!g_stop) {
        fd_set rfds;
        struct timeval tv;
        FD_ZERO(&rfds);
        FD_SET(STDIN_FILENO, &rfds);
        tv.tv_sec = interval_ms / 1000;
        tv.tv_usec = (interval_ms % 1000) * 1000;

        int r = select(STDIN_FILENO + 1, &rfds, NULL, NULL, &tv);
        if (r > 0 && stdin_open) {
            char buf[256];
            long ms;
            if (fgets(buf, sizeof(buf), stdin) == NULL) {
                stdin_open = 0; /* parent closed stdin; keep tracing */
            } else if (buf[0] == '+' || buf[0] == '-') {
                int pid = atoi(buf + 1);
                if (pid > 0)
                    set_pid(skel, (__u32)pid, buf[0] == '+');
                if (buf[0] == '-')
                    clear_pid(skel, (__u32)pid);
            } else if (sscanf(buf, "i %ld", &ms) == 1 && ms >= 1) {
                interval_ms = ms;
            }
        }
        dump_faults(skel);
    }

cleanup:
    mmap_tracer_bpf__destroy(skel);
    return err ? 1 : 0;
}