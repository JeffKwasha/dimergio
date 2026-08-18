// SPDX-License-Identifier: GPL-2.0
/* dimergio mmap tracer — BPF half.
 *
 * Counts mmap(2) page-fault reads per (pid, inode) so dimergio can notice
 * model / data loads that fanotify (and therefore fatrace) never reports:
 * the kernel does not emit fanotify events for accesses that happen through
 * mmap page faults.
 *
 * The hot path is deliberately tiny: one hash lookup on the enabled-pid
 * filter map plus one counter increment. No path string resolution happens
 * in-kernel; paths are resolved in userspace from /proc/<pid>/maps.
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

/* Per (pid, inode) page-fault counters, drained by the userspace loop each
 * sampling window. */
struct fault_key {
    __u32 pid;
    __u64 ino;
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
    struct file *file = NULL;
    struct inode *inode = NULL;
    __u64 ino = 0;

    bpf_probe_read_kernel(&vma, sizeof(vma), &vmf->vma);
    if (!vma)
        return 0;
    bpf_probe_read_kernel(&file, sizeof(file), &vma->vm_file);
    if (!file)
        return 0;
    bpf_probe_read_kernel(&inode, sizeof(inode), &file->f_inode);
    if (!inode)
        return 0;
    bpf_probe_read_kernel(&ino, sizeof(ino), &inode->i_ino);

    struct fault_key key = { .pid = pid, .ino = ino };
    __u64 *count = bpf_map_lookup_elem(&faults, &key);
    if (count) {
        __sync_fetch_and_add(count, 1);
    } else {
        __u64 one = 1;
        bpf_map_update_elem(&faults, &key, &one, BPF_ANY);
    }
    return 0;
}