#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #3 回归：QC 严格 mask + preview_only 闸门。

修复前 `lib/vc_state.py:run_qc()` 在 `n_cells_keep < 3` 时把 `cell_mask` 直接
改回「全 True」静默放行——下游 state 拿不到真实筛选结果、也不会拒绝。
修复后：保留严格 mask，标记 `metrics.preview_only=True` 并写 warnings；
下游 `run_state(cell_mask=...)` 因细胞数不足抛 EngineError（拒绝继续）。
"""
import os
import sys

import numpy as np
import scipy.sparse as sp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_common as C  # noqa: E402
import vc_io  # noqa: E402
import vc_state  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def make_bundle(n_cells=10, n_genes=20):
    X = np.zeros((n_cells, n_genes), dtype=np.float32)
    X[0, :] = 5.0
    X[1, :] = 5.0
    for row in range(2, n_cells):
        X[row, row % n_genes] = 1.0
    return vc_io.Bundle(
        X=sp.csr_matrix(X), genes=[f"G{i}" for i in range(n_genes)],
        cells=[f"C{i}" for i in range(n_cells)], obs=None,
        meta={"format": "synthetic", "value_type": "counts"},
    )


def main() -> int:
    bundle = make_bundle()
    qc = vc_state.run_qc(bundle, {"min_genes": 10, "min_cells": 1})
    mask = np.asarray(qc["arrays"]["cell_mask"], dtype=bool)
    check("严格 mask 只保留 2 个细胞", int(mask.sum()) == 2,
          f"keep={int(mask.sum())}/{mask.size} mask={mask.tolist()}")
    check("metrics.preview_only=True", qc["metrics"].get("preview_only") is True,
          f"preview_only={qc['metrics'].get('preview_only')} metrics_keys={sorted(qc['metrics'])}")
    check("warnings 明确标注仅预览", any(("预览" in w) or ("preview" in w.lower()) for w in qc["warnings"]),
          f"warnings={qc['warnings']}")
    check("fallback 不写回全 True", not qc["metrics"].get("fallback_cell", False),
          f"fallback={qc['metrics'].get('fallback')}")

    refused = False
    detail = ""
    try:
        vc_state.run_state(bundle, {"cell_mask": mask.tolist(), "n_hvg": 5, "n_pcs": 3,
                                    "cluster": 2, "embedding": "pca"})
    except C.EngineError as error:
        refused = error.code in ("too_few_cells", "prerequisite", "bad_request")
        detail = f"{error.code}: {error.message}"
    except Exception as error:  # noqa: BLE001
        refused = True
        detail = f"{type(error).__name__}: {error}"
    check("下游 state 拒绝继续", refused, detail)

    normal = vc_state.run_qc(bundle, {"min_genes": 1, "min_cells": 1})
    check("阈值正常时 preview_only=False",
          normal["metrics"].get("preview_only") is False,
          f"preview_only={normal['metrics'].get('preview_only')}")
    check("阈值正常时 mask 为真实结果", int(np.asarray(normal["arrays"]["cell_mask"]).sum()) == bundle.n_cells,
          f"keep={int(np.asarray(normal['arrays']['cell_mask']).sum())}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
