#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #1 回归：Seurat v5 多 layer 按基因 ID 对齐并集；严格区分 counts / log。

修复前 `lib/vc_rbridge.R` 的 `extract_seurat()` 在 counts/data 整层取不到时走
`count_layers` 回退，用 `do.call(cbind, mats)` **按列位置拼接**：3 基因的 counts.l1
与 3 基因的 counts.l2（并集 4 基因）会被导成 3 个基因、且第二层数值贴到第一层的
基因行上；data 层同理，且 scale.data 也可能被当成表达层导出。
修复后按基因 ID 取并集对齐，meta.value_type 明确写 counts / lognorm。
缺 Rscript/Seurat 时打印 SKIP 并返回 0（不算失败）。
"""
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_io  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


R_CODE = r'''
suppressPackageStartupMessages({library(Seurat); library(Matrix)})
out <- Sys.getenv("VC_R_OUT")
m1 <- Matrix(matrix(c(1,0,2, 0,3,0), nrow = 3,
                    dimnames = list(c("A","B","C"), c("c1","c2"))), sparse = TRUE)
m2 <- Matrix(matrix(c(20,30,40, 0,50,0), nrow = 3,
                    dimnames = list(c("B","C","D"), c("c3","c4"))), sparse = TRUE)
obj <- CreateSeuratObject(counts = list(l1 = as(m1, "CsparseMatrix"),
                                        l2 = as(m2, "CsparseMatrix")), project = "t")
saveRDS(obj, file.path(out, "counts_layers.rds"))
full <- Matrix(c(1,0,2,0, 0,3,0,1, 2,0,0,1, 0,0,1,0), nrow = 4,
               dimnames = list(c("A","B","C","D"), c("c1","c2","c3","c4")), sparse = TRUE)
base <- CreateSeuratObject(counts = full, project = "t")
base[["RNA"]] <- CreateAssay5Object(data = list(l1 = as(m1, "CsparseMatrix"),
                                                l2 = as(m2, "CsparseMatrix")))
saveRDS(base, file.path(out, "data_layers.rds"))
cat("ok\n")
'''

# 两层 3 基因的并集是 4 基因；下面这些值只能靠「按基因 ID 对齐」才对得上。
EXPECTED = {
    ("c1", "A"): 1.0, ("c2", "B"): 3.0, ("c2", "C"): 0.0,
    ("c3", "B"): 20.0, ("c3", "C"): 30.0, ("c3", "D"): 40.0,
    ("c4", "C"): 50.0, ("c4", "B"): 0.0,
}


def value_of(bundle, cell, gene):
    cells = [str(c) for c in bundle.cells]
    genes = [str(g) for g in bundle.genes]
    return float(bundle.X[cells.index(cell), genes.index(gene)])


def check_union(label, bundle, expect_value_type):
    genes = [str(g) for g in bundle.genes]
    cells = [str(c) for c in bundle.cells]
    check(f"{label}：基因并集 = 4（A,B,C,D）", bundle.n_genes == 4 and set(genes) == {"A", "B", "C", "D"},
          f"genes={genes}")
    check(f"{label}：细胞 = 4", bundle.n_cells == 4 and set(cells) == {"c1", "c2", "c3", "c4"},
          f"cells={cells}")
    if set(genes) == {"A", "B", "C", "D"} and set(cells) == {"c1", "c2", "c3", "c4"}:
        bad = [(cell, gene, value_of(bundle, cell, gene), want)
               for (cell, gene), want in EXPECTED.items()
               if abs(value_of(bundle, cell, gene) - want) > 1e-6]
        check(f"{label}：数值按基因 ID 对齐", not bad, f"错位={bad[:4]}")
    check(f"{label}：value_type = {expect_value_type}",
          bundle.meta.get("value_type") == expect_value_type,
          f"实际={bundle.meta.get('value_type')} meta={ {k: bundle.meta.get(k) for k in ('has_counts', 'r_layers')} }")


def main() -> int:
    rscript = vc_io._find_rscript()
    if not rscript:
        print("[SKIP] 没有 Rscript，跳过 R 多 layer 测试")
        return 0
    tmp = tempfile.mkdtemp(prefix="vc-p0-rlayers-")
    script = os.path.join(tmp, "make.R")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(R_CODE)
    env = dict(os.environ, VC_R_OUT=tmp)
    done = subprocess.run([rscript, "--vanilla", script], capture_output=True, text=True, env=env)
    if done.returncode != 0:
        print("[SKIP] R 测试对象生成失败（Seurat 不可用）：", (done.stderr or "")[-500:])
        return 0

    counts_path = os.path.join(tmp, "counts_layers.rds")
    data_path = os.path.join(tmp, "data_layers.rds")

    counts = vc_io.load_dataset(counts_path)
    check_union("counts 多 layer", counts, "counts")

    data = vc_io.load_dataset(data_path)
    check_union("data 多 layer", data, "lognorm")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
