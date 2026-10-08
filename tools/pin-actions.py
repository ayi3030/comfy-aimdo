# -*- coding: utf-8 -*-
"""
把 workflow 里所有「浮动」的 uses: 引用钉到 commit SHA（供应链加固）。
==================================================================
用法：
    python tools/pin-actions.py [--check]

不带 --check：就地改写 .github/workflows/*.yml，把 `owner/repo@tag|branch`
替换为 `owner/repo@<40位commit SHA>`（已为 SHA 的跳过）。
带   --check：只打印将要做的改动，不写文件。

需要 GitHub token（环境变量 GITHUB_TOKEN / gh / git credential fill）。
"""

import os
import re
import sys
import json
import glob
import subprocess
import urllib.request
import urllib.error

API_BASE = "https://api.github.com"
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
# 匹配一整行 uses:，捕获引号与值，便于原样回填
LINE_RE = re.compile(r"^(?P<indent>\s*(?:-\s*)?uses:\s*)(?P<q>['\"]?)(?P<val>[^\s'\"#]+)(?P<q2>['\"]?)(?P<rest>.*)$")


def get_token():
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["git", "credential", "fill"],
                           input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, timeout=20)
        for ln in r.stdout.splitlines():
            if ln.startswith("password="):
                return ln[len("password="):].strip()
    except Exception:
        pass
    return None


def api_commit_sha(owner_repo, ref, token):
    """返回 ref 对应的 commit SHA；失败返回 None。"""
    url = f"{API_BASE}/repos/{owner_repo}/commits/{ref}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")).get("sha")
    except Exception:
        return None


def repo_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    check_only = "--check" in sys.argv[1:]
    token = get_token()
    if not token:
        print("[ERROR] 未能获取 GitHub token，无法解析 SHA。请设置 GITHUB_TOKEN 或配置 git credential。")
        sys.exit(1)
    root = repo_root()
    wf_dir = os.path.join(root, ".github", "workflows")
    wf_files = sorted(glob.glob(os.path.join(wf_dir, "*.yml")) +
                      glob.glob(os.path.join(wf_dir, "*.yaml")))
    total = 0
    for wf in wf_files:
        with open(wf, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        changed = False
        for ln in lines:
            m = LINE_RE.match(ln.rstrip("\n"))
            if not m:
                new_lines.append(ln)
                continue
            val = m.group("val")
            if val.startswith("./") or "://" in val or "@" not in val or "/" not in val:
                new_lines.append(ln)
                continue
            owner_repo, ref = val.rsplit("@", 1)
            if SHA_RE.match(ref):
                new_lines.append(ln)
                continue
            sha = api_commit_sha(owner_repo, ref, token)
            if not sha:
                print(f"  [跳过] {os.path.basename(wf)}: 无法解析 {val}")
                new_lines.append(ln)
                continue
            new_val = f"{owner_repo}@{sha}"
            # 保留原引号风格；无引号时也不加（避免改动缩进/格式）
            q = m.group("q")
            new_ln = f"{m.group('indent')}{q}{new_val}{q}{m.group('rest')}\n"
            total += 1
            changed = True
            print(f"  [pin]  {os.path.basename(wf)}: {val}  ->  {new_val}")
            new_lines.append(new_ln)
        if changed and not check_only:
            with open(wf, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
    print(f"\n共处理 {total} 处浮动引用。" +
          ("（--check 模式，未写入文件）" if check_only else "（已写入文件，请 git add 后提交）"))


if __name__ == "__main__":
    main()
