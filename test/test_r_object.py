#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R 对象导入自测：.rds / .RData / 多对象歧义 / inspect。

需要本机 Rscript + Seurat；缺 R 时打印 [SKIP] 并以 0 退出（不算失败）。
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_io  # noqa: E402
import vc_common as C  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    rscript = vc_io._find_rscript()
    if not rscript:
        print("[SKIP] 没有 Rscript，跳过 R 对象测试")
        return 0
    tmp = tempfile.mkdtemp(prefix="vc-rtest-")
    script = os.path.join(tmp, "make.R")
    r_code = "\n".join([
        'suppressPackageStartupMessages({library(Seurat); library(Matrix)})',
        'genes <- c("CD3D","CD3E","CD2","MS4A1","CD79A","LYZ","S100A8","NKG7","GNLY","PPBP","PF4",',
        '           sprintf("G%03d", 1:40))',
        'cells <- sprintf("cell%03d", 1:60)',
        'set.seed(0)',
        'counts <- matrix(rpois(length(genes)*length(cells), 2), nrow = length(genes),',
        '                 dimnames = list(genes, cells))',
        'obj <- CreateSeuratObject(counts = Matrix(counts, sparse = TRUE), project = "t")',
        'obj$cell_type <- rep(c("T","B","Mono"), length.out = ncol(obj))',
        'obj$group <- rep(c("ctrl","case"), length.out = ncol(obj))',
        f'saveRDS(obj, file = "{tmp}/seurat.rds")',
        f'save(obj, file = "{tmp}/seurat.RData")',
        'a <- obj; b <- obj',
        f'save(a, b, file = "{tmp}/two.RData")',
        'cat("ok\\n")',
        "",
    ])
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(r_code)
    done = subprocess.run([rscript, "--vanilla", script], capture_output=True, text=True)
    if done.returncode != 0:
        print("[SKIP] R 测试对象生成失败：", (done.stderr or "")[-400:])
        return 0

    rds_path = os.path.join(tmp, "seurat.rds")
    rdata_path = os.path.join(tmp, "seurat.RData")
    multi_path = os.path.join(tmp, "two.RData")

    bundle = vc_io.load_dataset(rds_path)
    check("R .rds 导入（Seurat）", bundle.n_cells == 60 and bundle.n_genes >= 50,
          f"{bundle.n_cells}×{bundle.n_genes} class={bundle.meta.get('r_class')} assay={bundle.meta.get('r_assay')}")
    check("R .rds 保留 obs", "cell_type" in list(bundle.obs.columns) and "group" in list(bundle.obs.columns),
          str(list(bundle.obs.columns)))
    check("R .rds 矩阵与基因匹配",
          abs(float(bundle.X.sum()) - 0.0) > 0 and bundle.genes[0] == "CD3D",
          f"sum={float(bundle.X.sum()):.0f} 首基因={bundle.genes[0]}")

    rdata_bundle = vc_io.load_dataset(rdata_path)
    check("R .RData 导入", rdata_bundle.n_cells == 60, f"{rdata_bundle.n_cells}×{rdata_bundle.n_genes}")

    info = vc_io.inspect_path(rds_path)
    check("R inspect", info.get("n_cells") == 60 and info.get("r_class") == "Seurat",
          f"{info.get('n_cells')}×{info.get('n_genes')} {info.get('r_class')}")

    ambiguous_ok = False
    candidates = []
    try:
        vc_io.load_dataset(multi_path)
    except C.EngineError as error:
        candidates = list((error.details or {}).get("candidates") or [])
        ambiguous_ok = error.code == "ambiguous" and bool(candidates)
    check("R 多对象报 ambiguous", ambiguous_ok, str([item.get("object") for item in candidates]))

    picked = vc_io.load_dataset(multi_path, object="b")
    check("R 多对象用 object 指定", picked.n_cells == 60, f"{picked.n_cells}×{picked.n_genes}")

    print()
    print(f"结果：{8 - len(FAIL)} 通过，{len(FAIL)} 失败；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
