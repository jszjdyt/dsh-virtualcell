#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vc_io 自测：demo 确定性 / 统计合理 / h5ad round-trip / 文本矩阵 round-trip / inspect。"""
import hashlib
import os
import sys
import tempfile
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_io  # noqa: E402

FAIL = []
def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)

t0 = time.time()
b1 = vc_io.load_demo(seed=0, n_cells=1200, n_genes=800)
b2 = vc_io.load_demo(seed=0, n_cells=1200, n_genes=800)
elapsed = time.time() - t0
h1 = hashlib.sha256(b1.X.tocsr().astype(np.float32).data.tobytes()).hexdigest()
h2 = hashlib.sha256(b2.X.tocsr().astype(np.float32).data.tobytes()).hexdigest()
check("demo 形状", b1.X.shape == (1200, 800), str(b1.X.shape))
check("demo 确定性", h1 == h2, h1[:16])
check("demo 生成耗时 <10s", elapsed < 10, f"{elapsed:.1f}s (两次)")
totals = np.asarray(b1.X.sum(axis=1)).ravel()
check("每个细胞 counts > 50", totals.min() > 50, f"min={totals.min():.0f} median={np.median(totals):.0f}")
check("每个基因有表达", (np.asarray(b1.X.sum(axis=0)).ravel() > 0).all(), "")
truth = b1.truth or {}
check("truth grn 边数 > 200", len(truth.get("grn", [])) > 200, f"{len(truth.get('grn', []))} 边")
check("truth 留出扰动 = 5", len(truth.get("perturb", {})) == 5, str(list(truth.get("perturb", {}).keys())))
pert = truth["perturb"][list(truth["perturb"])[0]]
check("留出扰动 Δ 非零", float(np.abs(pert).max()) > 0.1, f"max|Δ|={np.abs(pert).max():.3f}")
tab = truth.get("perturb_table")
check("perturb_table 形状", tab is not None and tab.shape[0] == 800 and "perturbation" in tab.columns,
      f"{None if tab is None else tab.shape}; 扰动={None if tab is None else sorted(tab['perturbation'].unique())}")
check("demo 有 6 个状态", len(truth.get("states", [])) == 6, str(truth.get("states")))
check("obs 有 cell_state", "cell_state" in list(b1.obs.columns), str(list(b1.obs.columns)))
check("meta 标记 counts", b1.meta.get("value_type") == "counts", b1.meta.get("value_type"))

# h5ad round-trip
tmp = tempfile.mkdtemp(prefix="vc-io-test-")
h5 = os.path.join(tmp, "demo.h5ad")
vc_io.write_h5ad(b1, h5)
b3 = vc_io.load_dataset(h5)
check("h5ad round-trip 形状", b3.X.shape == b1.X.shape, str(b3.X.shape))
check("h5ad 保留 obs", "cell_state" in list(b3.obs.columns), str(list(b3.obs.columns))[:60])
check("h5ad 基因名一致", b3.genes[:30] == b1.genes[:30], b3.genes[0])
info = vc_io.inspect_path(h5)
check("inspect h5ad", info.get("n_cells") == 1200 and info.get("n_genes") == 800,
      f"{info.get('n_cells')}x{info.get('n_genes')} value_type={info.get('value_type')}")

# 文本矩阵 round-trip（基因 × 细胞，测方向自动判断）
df = pd.DataFrame(np.asarray(b1.X[:80].todense()).T, index=b1.genes, columns=b1.cells[:80])
tsv = os.path.join(tmp, "expr.tsv")
df.to_csv(tsv, sep="\t")
b4 = vc_io.load_dataset(tsv)
check("文本矩阵方向自动判断", b4.X.shape == (80, len(b1.genes)), str(b4.X.shape))
check("文本矩阵基因名", set(b4.genes[:50]) == set(b1.genes[:50]), "")
info2 = vc_io.inspect_path(tsv)
check("inspect 文本矩阵", info2.get("format") == "matrix_text", str(info2.get("format")))

# 10x mtx 目录（手工造）
import scipy.io as sio
import scipy.sparse as sp
mtx_dir = os.path.join(tmp, "10x")
os.makedirs(mtx_dir)
mat = sp.csr_matrix(np.asarray(b1.X[:60].todense()).T.astype(int))  # genes × cells
sio.mmwrite(os.path.join(mtx_dir, "matrix.mtx"), mat)
with open(os.path.join(mtx_dir, "barcodes.tsv"), "w") as fh:
    fh.write("\n".join(b1.cells[:60]) + "\n")
with open(os.path.join(mtx_dir, "features.tsv"), "w") as fh:
    fh.write("\n".join(f"ENSG{i:06d}\t{gene}" for i, gene in enumerate(b1.genes)) + "\n")
b5 = vc_io.load_dataset(mtx_dir)
check("10x mtx 目录", b5.X.shape == (60, len(b1.genes)), str(b5.X.shape))

# 压缩包
zip_path = os.path.join(tmp, "pack.zip")
import zipfile
with zipfile.ZipFile(zip_path, "w") as zf:
    zf.write(tsv, "expr.tsv")
b6 = vc_io.load_dataset(zip_path)
check("zip 自动解包导入", b6.X.shape[0] == 80, str(b6.X.shape))

print()
print(f"结果：{15 - len(FAIL)} 通过，{len(FAIL)} 失败；失败项：{FAIL}")
sys.exit(1 if FAIL else 0)
