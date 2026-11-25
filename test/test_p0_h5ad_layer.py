#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #2 回归：h5ad 选层 + matrix_source / value_type 记录。

修复前 `lib/vc_io.py:_read_h5ad()` 固定取 `adata.X`，忽略 `layers['counts']` 与
`adata.raw`，`value_type` 只靠启发式猜。修复后按
`layers['counts']` → `X` → `raw.X`（X 为 scal e/缺失时优先 raw）的顺序选层，
并在 meta 里写 `matrix_source` 与 `value_type`。
"""
import os
import sys
import tempfile

import anndata as ad
import numpy as np
import scipy.sparse as sp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_io  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def make(n_cells=6, n_genes=4):
    rng = np.random.default_rng(0)
    counts = rng.integers(1, 20, size=(n_cells, n_genes)).astype(np.float32)
    norm = np.log1p(counts / counts.sum(axis=1, keepdims=True) * 1e4).astype(np.float32)
    obs = [f"cell{i}" for i in range(n_cells)]
    var = [f"G{i}" for i in range(n_genes)]
    return counts, norm, obs, var


def dense(X):
    return np.asarray(X.todense() if sp.issparse(X) else X, dtype=np.float64)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vc-p0-h5ad-")
    counts, norm, obs, var = make()

    # 1) X 是标准化值、layers['counts'] 是原始 counts → 必须选 counts 层
    adata = ad.AnnData(X=sp.csr_matrix(norm))
    adata.obs_names, adata.var_names = obs, var
    adata.layers["counts"] = sp.csr_matrix(counts)
    path1 = os.path.join(tmp, "with_counts_layer.h5ad")
    adata.write_h5ad(path1)
    bundle = vc_io.load_dataset(path1)
    check("layers/counts 被选中", bundle.meta.get("matrix_source") == "layers/counts",
          f"matrix_source={bundle.meta.get('matrix_source')}")
    check("counts 层 value_type=counts", bundle.meta.get("value_type") == "counts",
          f"value_type={bundle.meta.get('value_type')}")
    same = np.allclose(dense(bundle.X), counts)
    check("矩阵取自 counts 层（不是 X）", same,
          f"sum X={float(np.sum(dense(bundle.X))):.1f} counts={float(counts.sum()):.1f}")

    # 2) X 是标度数据（含负值）+ raw.X 是 counts → 优先 raw
    scaled = (counts - counts.mean()) / (counts.std() + 1e-6)
    adata2 = ad.AnnData(X=sp.csr_matrix(scaled.astype(np.float32)))
    adata2.obs_names, adata2.var_names = obs, var
    raw = ad.AnnData(X=sp.csr_matrix(counts))
    raw.obs_names, raw.var_names = obs, var
    adata2.raw = raw
    path2 = os.path.join(tmp, "scaled_with_raw.h5ad")
    adata2.write_h5ad(path2)
    bundle2 = None
    error2 = ""
    try:
        bundle2 = vc_io.load_dataset(path2)
    except Exception as exc:  # noqa: BLE001
        error2 = f"{type(exc).__name__}: {exc}"
    check("X 为 scaled 时选 raw", bundle2 is not None and bundle2.meta.get("matrix_source") == "raw/X",
          f"matrix_source={None if bundle2 is None else bundle2.meta.get('matrix_source')} err={error2}")
    same2 = (bundle2 is not None and bundle2.n_cells == 6 and bundle2.n_genes == 4
             and np.allclose(dense(bundle2.X), counts))
    check("raw 层矩阵生效", same2,
          "未取到 raw" if bundle2 is None or bundle2.n_cells != 6 or bundle2.n_genes != 4
          else f"sum={float(np.sum(dense(bundle2.X))):.1f} vs {float(counts.sum()):.1f}")

    # 3) X 缺失 + raw 存在 → 选 raw（修复前直接报「h5ad 里没有 X 矩阵」）
    adata3 = ad.AnnData(X=None, obs={"cell": obs}, var={"gene": var})
    adata3.raw = raw
    path3 = os.path.join(tmp, "no_x_with_raw.h5ad")
    adata3.write_h5ad(path3)
    bundle3 = None
    error3 = ""
    try:
        bundle3 = vc_io.load_dataset(path3)
    except Exception as exc:  # noqa: BLE001
        error3 = f"{type(exc).__name__}: {exc}"
    ok3 = (bundle3 is not None and bundle3.meta.get("matrix_source") == "raw/X"
           and bundle3.n_cells == 6 and bundle3.n_genes == 4
           and np.allclose(dense(bundle3.X), counts))
    check("X 缺失时选 raw", ok3,
          f"matrix_source={None if bundle3 is None else bundle3.meta.get('matrix_source')} err={error3}")

    # 3b) inspect 也要报告选层（不读全矩阵）
    info = vc_io.inspect_path(path1)
    check("inspect 报告 matrix_source/value_type",
          info.get("matrix_source") == "layers/counts" and info.get("value_type") == "counts",
          f"matrix_source={info.get('matrix_source')} value_type={info.get('value_type')}")

    # 4) 普通 lognorm X、无 counts/raw → 保持取 X（回归保护）
    adata4 = ad.AnnData(X=sp.csr_matrix(norm))
    adata4.obs_names, adata4.var_names = obs, var
    path4 = os.path.join(tmp, "plain_x.h5ad")
    adata4.write_h5ad(path4)
    bundle4 = vc_io.load_dataset(path4)
    check("普通 X 仍取 X", bundle4.meta.get("matrix_source") == "X"
          and bundle4.meta.get("value_type") == "lognorm",
          f"source={bundle4.meta.get('matrix_source')} value_type={bundle4.meta.get('value_type')}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
