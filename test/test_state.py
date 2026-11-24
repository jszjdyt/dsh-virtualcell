#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vc_state 端到端自测：合成 600 细胞 × 400 基因 × 3 状态（真实 marker 面板 + 泊松噪声），
依次跑 qc→state→annotate→transition，断言字段/形状/聚类质量/注释准确率/转移矩阵/确定性/耗时。"""
import os, sys, time

import numpy as np
import pandas as pd
import scipy.sparse as sp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import vc_priors as priors  # noqa: E402
import vc_state as vs  # noqa: E402


class Bundle:  # 鸭子 bundle：只需要 .X/.genes/.cells/.obs/.truth/.meta
    def __init__(self, X, genes, cells, obs, truth=None, meta=None):
        self.X, self.genes, self.cells = X, genes, cells
        self.obs, self.truth = obs, truth
        self.meta = meta or {"value_type": "counts"}


def make_bundle(seed=7, n_cells=600, n_genes=400):
    rng = np.random.default_rng(seed)
    panel = sorted({g for genes in priors.STATE_MARKERS.values() for g in genes})
    states = {
        "T cell": priors.STATE_MARKERS["T cell"][:8],
        "B cell": priors.STATE_MARKERS["B cell"][:8],
        "Monocyte": priors.STATE_MARKERS["Monocyte"][:8],
    }
    genes = panel + [f"G{i:03d}" for i in range(n_genes - len(panel))]
    pos = {g: i for i, g in enumerate(genes)}
    truth = np.array([st for st in states for _ in range(n_cells // len(states))])
    X = rng.poisson(1.2, size=(truth.size, len(genes))).astype(np.float32)
    programs = np.array_split(rng.permutation(np.arange(len(panel), len(genes))), len(states))
    for si, (st, genes_st) in enumerate(states.items()):
        idx = np.flatnonzero(truth == st)
        for g in genes_st:  # 该状态的真实 marker 高表达
            X[idx, pos[g]] = rng.poisson(8.0, size=idx.size)
        for col in programs[si]:  # 额外的状态程序基因（让低维结构更真实）
            X[idx, col] = rng.poisson(5.0, size=idx.size)
    perm = rng.permutation(truth.size)
    X, truth = X[perm], truth[perm]
    cells = [f"cell{i:04d}" for i in range(truth.size)]
    obs = pd.DataFrame({"truth_state": truth}, index=cells)
    return Bundle(sp.csr_matrix(X), genes, cells, obs, truth={"labels": truth})


def main():
    t0 = time.time()
    bundle = make_bundle()
    n = bundle.X.shape[0]
    print(f"synthetic: {n} cells × {bundle.X.shape[1]} genes × 3 states")
    qc = vs.run_qc(bundle, {})
    assert qc["arrays"]["cell_mask"].shape == (n,), "cell_mask shape"
    assert qc["arrays"]["gene_mask"].shape == (len(bundle.genes),), "gene_mask shape"
    assert {"n_cells_before", "n_cells_after", "median_n_genes"} <= set(qc["metrics"]), "qc metrics"
    assert qc["tables"][0]["id"] == "qc_metrics" and qc["figures"][0]["kind"] == "qc_violin", "qc table/fig"
    params = {"n_hvg": 200, "n_pcs": 20, "seed": 3,
              "cell_mask": qc["arrays"]["cell_mask"].tolist(),
              "gene_mask": qc["arrays"]["gene_mask"].tolist()}
    state = vs.run_state(bundle, params)
    for key in ("arrays", "genes", "hvg", "latent_loadings", "cluster_centroids", "embedding_loadings",
                "cluster_labels", "cluster_sizes", "obs_columns", "backend", "n_clusters", "n_pcs",
                "metrics", "tables", "figures", "summary", "warnings"):
        assert key in state, f"state missing field: {key}"
    arrays, k = state["arrays"], state["n_clusters"]
    for key in ("X_log", "latent", "embedding", "clusters", "knn_idx", "knn_dist"):
        assert key in arrays, f"state arrays missing: {key}"
    assert arrays["clusters"].shape == (n,) and arrays["latent"].shape == (n, state["n_pcs"])
    assert arrays["embedding"].shape == (n, 2) and sp.issparse(arrays["X_log"])
    assert arrays["knn_idx"].shape[0] == n and arrays["knn_idx"].shape[1] >= 5
    assert state["latent_loadings"].shape == (len(state["hvg"]), state["n_pcs"])
    assert state["cluster_centroids"].shape == (k, state["n_pcs"])
    assert state["embedding_loadings"].shape == (state["n_pcs"], 2)
    assert len(state["cluster_labels"]) == k == len(state["cluster_sizes"])
    assert 2 <= k <= 6, f"n_clusters={k}"
    assert state["metrics"]["silhouette"] > 0.15, f"silhouette={state['metrics']['silhouette']}"
    assert state["metrics"]["variance_explained"] > 0 and "runtime_s" in state["metrics"]
    assert len(state["tables"]) >= 3 and len(state["figures"]) >= 2
    state2 = vs.run_state(bundle, params)
    assert np.array_equal(arrays["clusters"], state2["arrays"]["clusters"]), "deterministic clusters"
    assert np.allclose(arrays["latent"], state2["arrays"]["latent"], atol=1e-5), "deterministic latent"
    assert state["hvg"] == state2["hvg"], "deterministic hvg"
    annot = vs.run_annotate(bundle, state, {})
    assert annot["arrays"]["labels_per_cluster"].shape == (k,)
    assert annot["arrays"]["labels_per_cell"].shape == (n,)
    assert len(annot["cells_columns"]["cell_state"]) == n and len(annot["cluster_labels"]) == k
    assert {"assigned", "unknown", "mean_margin"} <= set(annot["metrics"])
    predicted, truth = np.asarray(annot["cells_columns"]["cell_state"]), bundle.truth["labels"]
    correct = 0
    for st in ("T cell", "B cell", "Monocyte"):
        vals, counts = np.unique(predicted[truth == st], return_counts=True)
        correct += int(vals[np.argmax(counts)] == st)
    assert correct >= 2, f"annotation correct {correct}/3"
    assert len(annot["tables"]) >= 2 and len(annot["figures"]) >= 1
    trans = vs.run_transition(bundle, state, {})
    T = np.asarray(trans["arrays"]["T"])
    assert T.shape == (k, k) and np.allclose(T.sum(axis=1), 1.0, atol=1e-6), "T shape/row sums"
    assert isinstance(trans["sources"], list) and isinstance(trans["sinks"], list)
    assert isinstance(trans["net_flow"], list) and len(trans["net_flow"]) <= 20
    assert {"n_states", "stability", "diag_mean"} <= set(trans["metrics"])
    assert trans["metrics"]["n_states"] == k and 0.0 <= trans["metrics"]["stability"] <= 1.0
    assert len(trans["tables"]) >= 1 and len(trans["figures"]) >= 2

    elapsed = time.time() - t0
    assert elapsed < 60.0, f"total runtime {elapsed:.1f}s >= 60s"
    print("state metrics:", {kk: state["metrics"][kk] for kk in ("n_cells", "n_genes", "n_hvg", "n_pcs",
                                                                  "n_clusters", "silhouette", "variance_explained", "runtime_s")})
    print("annotate metrics:", {kk: annot["metrics"][kk] for kk in ("assigned", "unknown", "mean_margin")})
    print("annotation accuracy:", f"{correct}/3")
    print("transition metrics:", trans["metrics"])
    print("T row sums:", np.round(T.sum(axis=1), 8).tolist())
    print(f"total elapsed: {elapsed:.1f}s")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
