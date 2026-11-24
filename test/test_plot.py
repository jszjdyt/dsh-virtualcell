#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vc_plot 自测：16 种图各出一张，断言文件存在/尺寸/notes；再测缺数据时降级不抛异常。"""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_plot  # noqa: E402

rng = np.random.default_rng(0)
n_cells, n_genes, n_states, n_pcs = 240, 60, 4, 8
clusters = rng.integers(0, n_states, size=n_cells)
latent = rng.normal(size=(n_cells, n_pcs)) + clusters[:, None] * 0.8
embedding = latent[:, :2] + rng.normal(scale=0.08, size=(n_cells, 2))
genes = [f"G{i:03d}" for i in range(n_genes)] + ["MYC", "KLF4"]
X_log = rng.poisson(2.0, size=(n_cells, len(genes))).astype(np.float32)

state = {
    "arrays": {"latent": latent.astype(np.float32), "embedding": embedding.astype(np.float32),
               "clusters": clusters.astype(np.int32), "X_log": X_log, "T": None},
    "genes": genes, "hvg": genes[:40], "cluster_labels": [f"S{i}" for i in range(n_states)],
    "obs_columns": {"n_genes": rng.integers(500, 3000, n_cells).tolist(),
                    "n_counts": rng.integers(2000, 9000, n_cells).tolist(),
                    "mito_pct": rng.uniform(0, 15, n_cells).tolist()},
    "cells_columns": {"cell_state": [f"S{int(c)}" for c in clusters]},
    "latent_loadings": rng.normal(size=(40, n_pcs)).astype(np.float32),
    "cluster_centroids": np.vstack([latent[clusters == i].mean(0) for i in range(n_states)]),
    "embedding_loadings": np.eye(n_pcs, 2, dtype=np.float32),
    "metrics": {"silhouette": 0.42, "variance_explained": 0.61, "n_clusters": n_states},
}
edges = [{"tf": f"TF{i}", "target": f"G{j}", "score": float(rng.normal()), "sign": 1,
          "cluster": str(int(rng.integers(0, n_states)))} for i in range(8) for j in range(8)]
grn = {
    "edges": edges,
    "metrics": {"n_edges": len(edges), "n_tfs": 8, "n_targets": 30,
                "hub_tfs": [{"tf": f"TF{i}", "out_degree": 8 - i, "mean_abs_weight": 0.4 - 0.03 * i} for i in range(8)],
                "cv_r2": 0.35},
    "validity": {"directional_evidence": "weak", "notes": ["共表达先验"]},
}
affected = [{"gene": genes[i], "delta": float(rng.normal()), "rank": i + 1, "is_tf": i < 3} for i in range(25)]
perturb = {
    "arrays": {"delta_gene": rng.normal(size=len(genes)).astype(np.float32),
               "delta_latent": rng.normal(size=(n_states, n_pcs)).astype(np.float32),
               "arrows": rng.normal(scale=0.2, size=(n_states, 2)).astype(np.float32)},
    "gene": "MYC", "mode": "knockout", "affected": affected,
    "per_cluster": [{"cluster": f"S{i}", "delta_norm": 0.3, "target_cluster": "S0", "cos": 0.8}
                    for i in range(n_states)],
    "state_shift": 0.31, "target_state": "S0", "source_state": "S1",
    "metrics": {"n_affected": 25, "max_abs_delta": 1.2, "state_shift": 0.31},
}
screen = {"ranked": [{"gene": f"TF{i}", "score": 0.9 - i * 0.06, "state_shift": 0.4 - i * 0.02,
                      "specificity": 0.5, "target_state": "S0", "why": "推动状态转移"} for i in range(10)],
          "arrays": {"scores": np.linspace(0.9, 0.3, 10), "tf_names": [f"TF{i}" for i in range(10)]},
          "metrics": {"n_screened": 10}}
response = {"arrays": {"pred": rng.normal(size=(6, 4)), "true": rng.normal(size=(6, 4))},
            "metrics": {"r2": 0.42, "spearman": 0.55, "beats_baseline": True}}
T = rng.random((n_states, n_states))
T = T / T.sum(axis=1, keepdims=True)
transition = {"arrays": {"T": T.astype(np.float32)},
              "sources": [{"cluster": "S1", "flow": 0.2}], "sinks": [{"cluster": "S0", "flow": -0.1}],
              "net_flow": [{"from": "S1", "to": "S0", "flow": 0.2}],
              "metrics": {"n_states": n_states, "stability": 0.88, "diag_mean": 0.7}}
validate = {"metrics": {"grn_auroc": 0.78, "grn_auprc": 0.42, "grn_top100_precision": 0.55,
                        "delta_spearman_mean": 0.41, "jaccard_top20_mean": 0.3},
            "verdict": {"passed": True, "why": "达标"}, "arrays": {"roc_fpr": [0, .1, .3, .6, 1],
                                                                  "roc_tpr": [0, .4, .7, .85, 1]}}
result = {"metrics": {**state["metrics"], **grn["metrics"], **transition["metrics"]},
          "tables": [], "figures": []}

ctx = {"state": state, "grn": grn, "perturb": perturb, "screen": screen, "response": response,
       "transition": transition, "validate": validate, "result": result, "bundle": None, "title": "测试"}
tmp = tempfile.mkdtemp(prefix="vc-plot-test-")
print("输出目录：", tmp)

fails = []
for kind in vc_plot.kinds():
    path = os.path.join(tmp, f"{kind}.png")
    info = vc_plot.plot(kind, ctx, path, {})
    size = os.path.getsize(path) if os.path.exists(path) else 0
    ok = (size > 8000 and info["width"] >= 400 and info["height"] >= 400 and bool(info["notes"]))
    print(f"[{'PASS' if ok else 'FAIL'}] {kind:24s} {info['width']:4d}x{info['height']:4d} {size/1024:6.1f} KB")
    if not ok:
        fails.append(kind)

# 缺数据时不能抛异常
empty_ctx = {"title": "空"}
for kind in ("state_embedding", "perturb_waterfall", "transition_matrix", "validate_roc", "not_a_kind"):
    path = os.path.join(tmp, f"empty-{kind}.png")
    info = vc_plot.plot(kind, empty_ctx, path, {})
    ok = os.path.exists(path) and os.path.getsize(path) > 3000
    print(f"[{'PASS' if ok else 'FAIL'}] 缺数据降级 {kind}")
    if not ok:
        fails.append("empty-" + kind)

print()
print(f"16 种图 + 5 个降级用例；失败：{fails}")
sys.exit(1 if fails else 0)
