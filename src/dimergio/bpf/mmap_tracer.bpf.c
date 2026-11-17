// SPDX-License-Identifier: GPL-2.0
/* dimergio mmap tracer — BPF half.
 *
 * Counts mmap(2) page-fault reads per (pid, mapping start) so dimergio can
 * notice model / data loads that fanotify (and therefore fatrace) never
 * reports: the kernel does not emit fanotify events for accesses that happen
 * through mmap page faults.
 *
 * The hot path is deliberately tiny: one hash lookup on the enabled-pid
 * filter map plus one counter increment. No path string resolution happens
 * in-kernel; the mapping start is looked up in /proc/<pid>/maps by userspace.
 *
 * The key is the mapping's start address rather than the file's inode because
 * inodes are per-filesystem: a file mapped through mergerfs faults on its
 * backing file, whose inode never matches the FUSE inode userspace can see.
 */
#include "vmlinux.h"

#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

/* Pids currently being traced.  A fault from any other pid is ignored, so
 * the kernel-side filter keeps the userspace dump small and cheap. */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u32);
    __type(value, __u8);
} enabled_pids SEC(".maps");

/* Per (pid, mapping start) page-fault counters, drained by the userspace loop
 * each sampling window. */
struct fault_key {
    __u32 pid;
    __u64 addr;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 131072);
    __type(key, struct fault_key);
    __type(value, __u64);
} faults SEC(".maps");

SEC("kprobe/filemap_fault")
int BPF_KPROBE(trace_filemap_fault, struct vm_fault *vmf)
{
    __u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (!bpf_map_lookup_elem(&enabled_pids, &pid))
        return 0;

    struct vm_area_struct *vma = NULL;
    unsigned long addr = 0;

    bpf_probe_read_kernel(&vma, sizeof(vma), &vmf->vma);
    if (!vma)
        return 0;
    bpf_probe_read_kernel(&addr, sizeof(addr), &vma->vm_start);
    if (!addr)
        return 0;

    struct fault_key key = { .pid = pid, .addr = addr };
    __u64 *count = bpf_map_lookup_elem(&faults, &key);
    if (count) {
        __sync_fetch_and_add(count, 1);
    } else {
        __u64 one = 1;
        bpf_map_update_elem(&faults, &key, &one, BPF_ANY);
    }
    return 0;
}