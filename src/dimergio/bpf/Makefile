# Build the dimergio mmap tracer (CO-RE eBPF).
#
# Requirements: clang, bpftool, libbpf-dev, and a kernel with BTF
# (CONFIG_DEBUG_INFO_BTF) at /sys/kernel/btf/vmlinux.
#
# The resulting binary is CO-RE: it is compiled once and runs on any
# x86_64 kernel with BTF, regardless of kernel version.
#
# Override for a custom libbpf prefix (e.g. extracted libbpf-dev .deb):
#   make CFLAGS="-g -O2 -Wall -I/path/to/usr/include" \
#        LIBS="-L/path/to/usr/lib/x86_64-linux-gnu -lbpf -lelf -lz"
#
# For a fully static binary:
#   make CFLAGS="... -static" LIBS="-L... -lbpf -lelf -lz -lpthread"

CLANG ?= clang
BPFTOOL ?= bpftool
CC ?= cc
PKG_CONFIG ?= pkg-config

BINDIR ?= ../bin
VMLINUX ?= /sys/kernel/btf/vmlinux

ARCH := $(shell uname -m)
ifeq ($(ARCH),x86_64)
TARGET_ARCH := x86
else ifeq ($(ARCH),aarch64)
TARGET_ARCH := arm64
endif

BPF_CFLAGS ?= -g -O2 -Wall -target bpf -D__TARGET_ARCH_$(TARGET_ARCH)
CFLAGS ?= -g -O2 -Wall
CFLAGS += $(shell $(PKG_CONFIG) --cflags libbpf 2>/dev/null)
# --static pulls in libbpf's private deps (libelf, zlib, zstd, lzma), which
# keeps the same LIBS correct for both dynamic and fully-static linking.
LIBS ?= $(shell $(PKG_CONFIG) --static --libs libbpf 2>/dev/null)

# Set STATIC=1 for a fully static binary (no shared runtime deps).
ifeq ($(STATIC),1)
CFLAGS += -static
endif

.PHONY: all clean install

all: $(BINDIR)/dimergio-mmap

vmlinux.h: $(VMLINUX)
	$(BPFTOOL) btf dump file $(VMLINUX) format c > $@

mmap_tracer.bpf.o: mmap_tracer.bpf.c vmlinux.h
	$(CLANG) $(BPF_CFLAGS) -c mmap_tracer.bpf.c -o $@

mmap_tracer.skel.h: mmap_tracer.bpf.o
	$(BPFTOOL) gen skeleton mmap_tracer.bpf.o > $@

mmap_tracer: mmap_tracer.c mmap_tracer.skel.h
	$(CC) $(CFLAGS) -o $@ mmap_tracer.c $(LIBS)

$(BINDIR)/dimergio-mmap: mmap_tracer
	mkdir -p $(BINDIR)
	cp mmap_tracer $@

install: $(BINDIR)/dimergio-mmap

clean:
	rm -f mmap_tracer mmap_tracer.bpf.o mmap_tracer.skel.h vmlinux.h