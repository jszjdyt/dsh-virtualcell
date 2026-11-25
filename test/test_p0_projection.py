#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #4 回归：state 保存 HVG mean/std，grn/response 用同一 z-score 变换投影。

修复前 `vc_grn._simulate()` 与 `vc_response.run_response()` 都用
`Δ_gene[hvg] @ latent_loadings`；但 PCA 是在 **z-score 后的 HVG** 上拟合的，
Δ 却在 log 空间，量纲不一致。修复后 state 保存 `hvg_mean`/`hvg_std`，
投影改成 `(Δ / hvg_std) @ latent_loadings`。
"""
import copy
import os
import sys

import numpy as np
import scipy.sparse as sp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_grn  # noqa: E402
import vc_io  # noqa: E402
import vc_response  # noqa: E402
import vc_state  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def synthetic_state():
    """4 细胞 × 4 基因；HVG=['G1','G2']，G1 的 HVG std=2。"""
    genes = ["G1", "G2", "G3", "G4"]
    X = np.zeros((4, 4), dtype=np.float32)
    X[:, 0] = [1.0, 2.0, 3.0, 4.0]      # G1（HVG，mean=2.5，std=2.0）
    X[:, 1] = [0.5, 0.5, 0.5, 0.5]      # G2（HVG，常数列）
    state = {
        "arrays": {
            "X_log": sp.csr_matrix(X),
            "clusters": np.zeros(4, dtype=np.int64),
            "latent": np.zeros((4, 2), dtype=np.float32),
            "hvg_mean": np.array([2.5, 0.5], dtype=np.float32),
            "hvg_std": np.array([2.0, 1.0], dtype=np.float32),
        },
        "genes": genes,
        "hvg": ["G1", "G2"],
        "cluster_centroids": np.zeros((1, 2), dtype=np.float32),
        "latent_loadings": np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        "embedding_loadings": np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        "cluster_labels": ["S0"],
    }
    return state


def main() -> int:
    # ---- A. run_state 必须落盘 HVG mean/std ----
    bundle = vc_io.load_demo(seed=0, n_cells=120, n_genes=200)
    state = vc_state.run_state(bundle, {"n_hvg": 100, "n_pcs": 10, "n_neighbors": 5,
                                        "cluster": 2, "embedding": "pca"})
    arrays = state.get("arrays") or {}
    hvg = list(state.get("hvg") or [])
    check("state 保存 hvg_mean", "hvg_mean" in arrays, f"arrays keys={sorted(arrays)}")
    check("state 保存 hvg_std", "hvg_std" in arrays, f"arrays keys={sorted(arrays)}")
    if "hvg_std" in arrays and "hvg_mean" in arrays:
        std = np.asarray(arrays["hvg_std"]).ravel()
        mean = np.asarray(arrays["hvg_mean"]).ravel()
        check("hvg_mean/std 长度与 hvg 一致", std.size == len(hvg) and mean.size == len(hvg),
              f"{mean.size}/{std.size} vs {len(hvg)}")
        check("hvg_std 全为正有限值", bool(np.all(np.isfinite(std)) and np.all(std > 0)),
              f"min={float(np.min(std)):.4g}")
    else:
        check("hvg_mean/std 长度与 hvg 一致", False, "字段缺失")
        check("hvg_std 全为正有限值", False, "字段缺失")

    # ---- B. grn._simulate 投影按 z-score 尺度（Δ_log / std）----
    syn = synthetic_state()
    data = vc_grn._stage_arrays(syn)
    grn = {"tf_names": [], "target_names": [], "arrays": {"coef_blocks": []}}
    sim = vc_grn._simulate(syn, data, grn, "G1", mode="knockdown", strength=1.0, n_prop=0)
    expected = -2.5 / 2.0     # 每细胞 Δ_log 均值 = -2.5；z 空间除以 std=2.0
    got = float(np.asarray(sim["delta_latent"])[0, 0])
    check("simulate Δ_latent 用 hvg_std 变换", abs(got - expected) < 1e-5,
          f"Δ_latent[0,0]={got:.6f} 期望={expected:.6f}")
    projection = sim.get("projection") or {}
    check("simulate 记录投影尺度来源",
          isinstance(projection, dict) and projection.get("space") == "zscore_hvg",
          f"projection={projection}")

    # ---- C. response 投影同样使用 hvg_std（翻倍 std → 隐空间 Δ 减半）----
    table = (bundle.truth or {}).get("perturb_table")
    state = vc_state.run_state(bundle, {"n_hvg": 100, "n_pcs": 10, "n_neighbors": 5,
                                        "cluster": 2, "embedding": "pca"})
    if "hvg_std" not in (state.get("arrays") or {}):
        check("response Δ_latent 随 hvg_std 缩放（std×2 → Δ×0.5）", False, "state 缺 hvg_std")
        check("response metrics 记录标准化投影", False, "state 缺 hvg_std")
    else:
        std_a = np.asarray(state["arrays"]["hvg_std"], dtype=np.float64).copy()
        state_x2 = copy.deepcopy(state)
        state_x2["arrays"]["hvg_std"] = (std_a * 2.0).astype(np.float32)
        if state_x2.get("hvg_std") is not None:
            state_x2["hvg_std"] = (std_a * 2.0).astype(np.float32)
        res_a = vc_response.run_response(bundle, state, table, {})
        res_b = vc_response.run_response(bundle, state_x2, table, {})
        true_a = np.asarray(res_a["arrays"]["true"], dtype=np.float64)
        true_b = np.asarray(res_b["arrays"]["true"], dtype=np.float64)
        ratio = float(np.linalg.norm(true_b) / max(1e-12, np.linalg.norm(true_a)))
        check("response Δ_latent 随 hvg_std 缩放（std×2 → Δ×0.5）", abs(ratio - 0.5) < 0.02,
              f"norm ratio={ratio:.4f}")
        check("response metrics 记录标准化投影",
              res_a["metrics"].get("delta_projection") == "zscore_hvg",
              f"delta_projection={res_a['metrics'].get('delta_projection')}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
