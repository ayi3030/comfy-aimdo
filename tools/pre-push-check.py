# -*- coding: utf-8 -*-
"""
pre-push 强制审查钩子（ComfyUI Intel XPU 适配团 / 解耦林）
==================================================================
在 `git push` 真正推送之前运行，对仓库做三道门禁：

  1) Action 存在性：扫描 .github/workflows 下所有 workflow 的每个 `uses: owner/repo@ref`，
     调用 GitHub API 确认仓库与 ref 真实存在。不存在 / ref 无法解析 => 【阻断 push】。
     （这正是之前 M1 首次 CI 失败的根因：写了一个不存在的第三方 action ref。）

  2) 浮动引用警告：ref 若不是 40 位 commit SHA（即 tag/branch 这类会漂移的引用），
     => 【警告】，建议用 tools/pin-actions.py 钉到 SHA。

  3) Python 语法门禁：对所有 .py 文件跑 py_compile，任一语法错误 => 【阻断 push】。

  4)（辅助）workflow 内出现的 http(s) 下载 URL 做可达性 HEAD 探测，不可达 => 【警告】。

退出码：存在任一「阻断」项 => 1（push 被拒绝）；否则 0。

设计取舍：网络/凭证不可用导致「无法校验」时，只警告不阻断，避免把用户锁死；
但「能连上 API 且引用确实不存在」时坚决阻断——这才是真正的硬机制。
"""

import os
import re
import sys
import json
import time
import glob
import subprocess
import urllib.request
import urllib.error

API_BASE = "https://api.github.com"

SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(.+?)\s*$", re.MULTILINE)
URL_RE = re.compile(r"https?://[^\s'\")\]]+")
# 匹配的 uses 值：可选引号包裹的 owner/repo@ref
USES_VAL_RE = re.compile(r"^['\"]?([^'\"]+?)['\"]?$")


def get_token():
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=20)
        t = r.stdout.strip()
        if t:
            return t
    except Exception:
        pass
    try:
        inp = "protocol=https\nhost=github.com\n\n"
        r = subprocess.run(["git", "credential", "fill"], input=inp,
                           capture_output=True, text=True, timeout=20)
        for ln in r.stdout.splitlines():
            if ln.startswith("password="):
                return ln[len("password="):].strip()
    except Exception:
        pass
    return None


def api_get(url, token):
    """返回 (status, json_or_None)。网络/限流异常返回 (0, None)。"""
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def repo_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    # 兜底：脚本所在目录的上两级（tools/ -> repo root）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def url_head(url):
    """对 URL 做一次 HEAD，返回 (ok, status_or_0)。"""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return True, resp.status
    except urllib.error.HTTPError as e:
        return e.code < 400, e.code
    except Exception:
        return False, 0


def main():
    root = repo_root()
    token = get_token()
    blocks = []      # 阻断项（列表 of 字符串）
    warns = []       # 警告项
    notes = []       # 正常提示

    wf_dir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        print("[pre-push] 未找到 .github/workflows，跳过 action 校验。")
    else:
        wf_files = sorted(glob.glob(os.path.join(wf_dir, "*.yml")) +
                          glob.glob(os.path.join(wf_dir, "*.yaml")))
        for wf in wf_files:
            try:
                with open(wf, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception as e:
                warns.append(f"{os.path.basename(wf)}: 无法读取 ({e})")
                continue
            name = os.path.basename(wf)

            # ---- 1) uses: 引用校验 ----
            for m in USES_RE.finditer(text):
                val = m.group(1).strip()
                val = val.split("#")[0].strip()  # 去掉行内注释，如 `... # v2.6.1`
                q = USES_VAL_RE.match(val)
                value = q.group(1) if q else val
                # 跳过本地 action 与 docker
                if value.startswith("./") or "://" in value:
                    continue
                if "@" not in value or "/" not in value:
                    continue
                owner_repo, ref = value.rsplit("@", 1)
                if SHA_RE.match(ref):
                    notes.append(f"{name}: {owner_repo}@{ref[:12]}… 已 pin SHA ✓")
                    continue
                # 浮动引用：先查仓库是否存在
                st_repo, _ = api_get(f"{API_BASE}/repos/{owner_repo}", token)
                if st_repo == 404:
                    blocks.append(f"[阻断] {name}: action 仓库不存在 -> {owner_repo}（uses: {value}）")
                    continue
                if st_repo not in (200,) and st_repo != 0:
                    warns.append(f"{name}: 无法校验仓库 {owner_repo}（HTTP {st_repo}）")
                    continue
                if st_repo == 0:
                    warns.append(f"{name}: 无网络/凭证，跳过校验 {value}")
                    continue
                # 仓库存在，解析 ref
                st_ref, _ = api_get(f"{API_BASE}/repos/{owner_repo}/commits/{ref}", token)
                if st_ref == 200:
                    warns.append(f"[警告] {name}: {value} 是浮动引用（建议 pin SHA，见 tools/pin-actions.py）")
                else:
                    blocks.append(f"[阻断] {name}: ref 无法解析 -> {value}（HTTP {st_ref}）")

            # ---- 4) URL 可达性（辅助） ----
            for u in sorted(set(URL_RE.findall(text))):
                if "github.com" in u and u.rstrip("/").endswith("git"):
                    continue  # git clone 地址，HEAD 无意义
                ok, st = url_head(u)
                if not ok:
                    warns.append(f"[警告] {name}: URL 可能不可达 -> {u} (status={st})")

    # ---- 3) Python 语法门禁 ----
    py_files = []
    for dirpath, dirs, files in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")
        for fn in files:
            if fn.endswith(".py"):
                py_files.append(os.path.join(dirpath, fn))
    if py_files:
        import py_compile
        bad = []
        for pf in py_files:
            try:
                py_compile.compile(pf, doraise=True)
            except py_compile.PyCompileError as e:
                rel = os.path.relpath(pf, root)
                bad.append(f"{rel}: {e}")
        if bad:
            blocks.append("[阻断] Python 语法错误：\n    " + "\n    ".join(bad))

    # ---- 汇总 ----
    print("=" * 64)
    print(" pre-push 强制审查（ComfyUI Intel XPU 适配团）")
    print("=" * 64)
    if notes:
        print("\n[已 pin / 正常]")
        for n in notes:
            print("  " + n)
    if warns:
        print("\n[警告 - 不阻断]")
        for w in warns:
            print("  " + w)
    if blocks:
        print("\n[阻断 - push 被拒绝]")
        for b in blocks:
            print("  " + b)
        print("\n" + "=" * 64)
        print(f" 共 {len(blocks)} 个阻断项。请修复后再 push。")
        print("=" * 64)
        return 1
    print("\n✅ 审查通过，允许 push。")
    if warns:
        print(f"（有 {len(warns)} 条警告，建议后续处理）")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
