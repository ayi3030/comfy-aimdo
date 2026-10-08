#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 探针：对输出目录做 sha256 清单，用于“同 seed 出图哈希比对”。

用途（两条对照，任一不一致即说明 RAM 层改变了字节/数值）：
  1) RAM 缓存开：启动并出图 -> `--snapshot on.txt`
  2) RAM 缓存关：AIMDO_XPU_RAM_CACHE_GB=0 启动、同 seed 出图 -> `--snapshot off.txt`
  3) `--compare on.txt off.txt` —— 逐文件 sha256 必须完全一致。

覆盖 IMAGE/VIDEO/AUDIO 后缀；忽略目录与其它文件。只看文件内容，不依赖文件名。
用法：
    python <此文件> snapshot <dir> <out.txt>
    python <此文件> compare <a.txt> <b.txt>
退出码：0 一致 | 1 不一致 | 2 用法错
约束：不启动 ComfyUI；本文件为“预备工装”，reviewer 不代为运行。
"""
import hashlib
import os
import sys

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
        ".mp4", ".webm", ".mkv", ".mov", ".avi",
        ".flac", ".wav", ".mp3", ".ogg"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_snapshot(root, out):
    items = []
    for r, _d, files in os.walk(root):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in EXTS:
                p = os.path.join(r, f)
                items.append((os.path.relpath(p, root).replace(os.sep, "/"), sha256(p)))
    items.sort()
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# snapshot of {root}  files={len(items)}\n")
        for name, h in items:
            fh.write(f"{h}  {name}\n")
    print(f"snapshot: {len(items)} artifacts -> {out}")
    return 0


def _read(path):
    d = {}
    for ln in open(path, "r", encoding="utf-8"):
        if ln.startswith("#") or not ln.strip():
            continue
        h, name = ln.rstrip("\n").split("  ", 1)
        d[name] = h
    return d


def cmd_compare(a, b):
    A, B = _read(a), _read(b)
    only_a = sorted(set(A) - set(B))
    only_b = sorted(set(B) - set(A))
    diff = sorted(n for n in (set(A) & set(B)) if A[n] != B[n])
    if not only_a and not only_b and not diff:
        print(f"IDENTICAL: {len(A)} artifacts byte-identical")
        return 0
    print(f"MISMATCH: only_in_{os.path.basename(a)}={only_a} "
          f"only_in_{os.path.basename(b)}={only_b} hash_diff={diff}")
    return 1


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    mode = sys.argv[1]
    if mode == "snapshot":
        return cmd_snapshot(sys.argv[2], sys.argv[3])
    if mode == "compare":
        return cmd_compare(sys.argv[2], sys.argv[3])
    print(f"unknown mode {mode!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
