#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #9 回归：package.json 的 files 必须打包 lib/vc_rbridge.R，并用实际 tarball 验证 RDS 可读。

修复前 `package.json.files` 只有 `lib/*.mjs`、`lib/*.py`，`npm pack` 出来的 tarball
不含 `lib/vc_rbridge.R`；安装后 `.rds/.RData` 导入直接报「R bridge 脚本缺失」。
本测试先 `npm pack`，解包后确认 R 桥存在，再用解包后的包读一个真实 Seurat RDS。
"""
import json
import os
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

import vc_io  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    with open(os.path.join(ROOT, "package.json"), "r", encoding="utf-8") as handle:
        pkg = json.load(handle)
    files = list(pkg.get("files") or [])
    check("package.json files 含 lib/*.R 或 vc_rbridge.R",
          any(str(item).strip().endswith("*.R") or str(item).strip().endswith("vc_rbridge.R")
              for item in files),
          f"files={files}")
    print("files 覆盖 R：", [item for item in files if "R" in str(item)])

    npm = None
    for candidate in ("npm", "/usr/local/bin/npm", "/opt/homebrew/bin/npm"):
        try:
            probe = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30)
            if probe.returncode == 0:
                npm = candidate
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    if npm is None:
        print("[SKIP] 没有 npm，无法验证 tarball")
        return 0

    rscript = vc_io._find_rscript()
    if not rscript:
        print("[SKIP] 没有 Rscript，无法验证 tarball 里的 R 桥")
        return 0

    tmp = tempfile.mkdtemp(prefix="vc-p0-pack-")
    packed = subprocess.run([npm, "pack", "--silent", "--pack-destination", tmp],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    if packed.returncode != 0:
        check("npm pack 成功", False, (packed.stderr or packed.stdout)[-500:])
        return 1
    names = [line.strip() for line in (packed.stdout or "").splitlines() if line.strip().endswith(".tgz")]
    check("npm pack 产出 tarball", bool(names), f"stdout={packed.stdout[-300:]}")
    if not names:
        return 1
    tarball = os.path.join(tmp, names[-1])

    extract_dir = os.path.join(tmp, "unpacked")
    os.makedirs(extract_dir, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        members = tar.getnames()
        tar.extractall(extract_dir)
    check("tarball 含 lib/vc_rbridge.R", any(name.endswith("lib/vc_rbridge.R") for name in members),
          f"匹配={[n for n in members if n.endswith('.R')][:5]}")
    check("tarball 含 vc_io.py 等 python 模块", any(name.endswith("lib/vc_io.py") for name in members),
          f"lib py 数={sum(1 for n in members if n.endswith('.py'))}")

    pkg_dir = os.path.join(extract_dir, "package")
    lib_dir = os.path.join(pkg_dir, "lib")
    check("解包后有 lib/vc_rbridge.R 文件", os.path.isfile(os.path.join(lib_dir, "vc_rbridge.R")),
          os.path.join(lib_dir, "vc_rbridge.R"))

    # 生成一个最小 Seurat 对象（3 基因 × 5 细胞）
    script = os.path.join(tmp, "make.R")
    r_code = "\n".join([
        'suppressPackageStartupMessages({library(Seurat); library(Matrix)})',
        'm <- Matrix(matrix(c(1,0,2, 0,3,0, 1,0,0, 1,0,0, 1,0,0), nrow = 3,',
        '             dimnames = list(c("CD3D","CD3E","LYZ"), sprintf("c%02d", 1:5))), sparse = TRUE)',
        'obj <- CreateSeuratObject(counts = m, project = "t")',
        f'saveRDS(obj, "{os.path.join(tmp, "mini.rds")}")',
        'cat("ok\\n")',
        "",
    ])
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(r_code)
    done = subprocess.run([rscript, "--vanilla", script], capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        check("生成测试用 Seurat RDS", False, (done.stderr or "")[-400:])
        return 1
    rds_path = os.path.join(tmp, "mini.rds")

    probe = "\n".join([
        "import sys, json",
        f"sys.path.insert(0, {lib_dir!r})",
        "import vc_io",
        f"bundle = vc_io.load_dataset({rds_path!r})",
        "print(json.dumps({'n_cells': bundle.n_cells, 'n_genes': bundle.n_genes,",
        "                  'genes': list(bundle.genes)[:3], 'bridge': vc_io.R_BRIDGE}))",
        "",
    ])
    env = dict(os.environ)
    env["MPLBACKEND"] = "Agg"
    env["PYTHONPATH"] = lib_dir
    shipped = subprocess.run([sys.executable, "-c", probe], cwd=pkg_dir, capture_output=True,
                             text=True, timeout=300, env=env)
    check("解包后的 tarball 能读 Seurat RDS", shipped.returncode == 0,
          (shipped.stderr or shipped.stdout)[-700:])
    loaded = {}
    if shipped.returncode == 0:
        try:
            loaded = json.loads((shipped.stdout or "{}").splitlines()[-1])
        except ValueError:
            loaded = {}
    check("RDS 维度正确（5 细胞 × 3 基因）",
          loaded.get("n_cells") == 5 and loaded.get("n_genes") == 3,
          f"loaded={loaded}")
    check("RDS 使用的 R 桥来自 tarball",
          str(loaded.get("bridge") or "").startswith(pkg_dir), f"bridge={loaded.get('bridge')}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
