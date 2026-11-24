#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vc_grn.py 自检：500 细胞 × 300 基因（20 TF + 280 target）× 3 状态。

覆盖 run_grn（边/系数/npz 落盘）→ run_validate（AUROC>0.6、verdict）→
run_perturb（top 受影响基因含真实下游、state_shift>0）→ run_screen（3 行）+ 无真值 prerequisite。
运行：cd plugins/dsh-virtualcell && python3 test/test_grn.py
"""
import os, sys, tempfile

import numpy as np
import pandas as pd
import scipy.sparse as sp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "lib")))
import vc_grn as G
import vc_priors as P


class Duck:
    """鸭子类型容器：bundle / state 只用约定字段。"""


def make_fixture(seed=0, n_cells=500, n_tf=20, n_targets=280, k=3, n_pcs=15):
    """真值 A：20 TF × 280 target 稀疏 ± 权重；表达 = 基线 + A 传播 + 状态程序 + 噪声。"""
    rng = np.random.default_rng(seed)
    tfs = list(P.HUMAN_TFS[:n_tf])
    tf_universe = set(P.tf_universe())
    pool = [g for markers in P.STATE_MARKERS.values() for g in markers]
    pool += list(P.DEMO_EXTRA_TARGETS)
    pool += [g for genes in P.PATHWAY_SETS.values() for g in genes]
    targets = []
    for gene in pool:
        if gene not in tf_universe and gene not in tfs and gene not in targets:
            targets.append(gene)
            if len(targets) == n_targets:
                break
    assert len(targets) == n_targets, f"可用于 target 的真实基因只有 {len(targets)}"
    genes, n_genes = tfs + targets, len(tfs) + len(targets)

    A = np.zeros((n_tf, n_targets))
    for j in range(n_targets):
        first = j % n_tf
        A[first, j] = rng.uniform(0.3, 0.9) * (1 if rng.random() < 0.6 else -1)
        if rng.random() < 0.35:
            second = (j * 7 + 3) % n_tf
            if second != first:
                A[second, j] += rng.uniform(0.2, 0.7) * (1 if rng.random() < 0.5 else -1)

    cluster = np.repeat(np.arange(k), int(np.ceil(n_cells / k)))[:n_cells]
    rng.shuffle(cluster)
    activity = rng.gamma(shape=2.0, scale=0.6, size=(n_cells, n_tf)) * \
        rng.uniform(0.7, 1.5, size=(k, n_tf))[cluster]
    raw = 1.0 + 0.8 * (activity @ A) + rng.normal(0, 0.25, size=(k, n_targets))[cluster] \
        + rng.normal(0, 0.35, size=(n_cells, n_targets))
    X_tg = np.log1p(np.clip(raw, 0.01, None))
    X = np.hstack([np.log1p(activity), X_tg]).astype(np.float32)
    X[rng.random(X.shape) < 0.05] = 0.0

    U, S, Vt = np.linalg.svd(X - X.mean(axis=0, keepdims=True), full_matrices=False)
    latent = (U[:, :n_pcs] * S[:n_pcs]).astype(np.float32)
    loadings = Vt[:n_pcs].T.astype(np.float32)
    _, _, V2 = np.linalg.svd(latent - latent.mean(axis=0), full_matrices=False)
    centroids = np.stack([latent[cluster == c].mean(axis=0) for c in range(k)]).astype(np.float32)
    state = {"arrays": {"X_log": sp.csr_matrix(X), "latent": latent, "clusters": cluster},
             "genes": genes, "hvg": list(genes), "cluster_centroids": centroids,
             "latent_loadings": loadings, "embedding_loadings": V2[:2].T.astype(np.float32),
             "cluster_labels": [f"S{c}" for c in range(k)]}
    truth_grn = pd.DataFrame([{"tf": tfs[a], "target": targets[j], "sign": int(np.sign(A[a, j])),
                               "weight": float(A[a, j])} for a, j in zip(*np.nonzero(A))])
    truth_perturb = {}
    for tf in (tfs[0], tfs[5], tfs[10]):          # 3 个留出 TF 的解析传播 Δ（与推断无关）
        ti = tfs.index(tf)
        delta = np.zeros(n_genes)
        delta[ti] = -float(np.asarray(X[:, ti]).mean())
        for _ in range(3):                          # Δ_target = Σ_r (Aᵀ)^r Δ_tf
            delta[n_tf:] += np.clip(A.T @ delta[:n_tf], -3 * X_tg.std(axis=0), 3 * X_tg.std(axis=0))
        truth_perturb[tf] = delta.astype(np.float32)
    bundle = Duck()
    bundle.genes, bundle.meta, bundle.truth = genes, {"format": "synthetic"}, None
    return bundle, state, A, tfs, targets, truth_grn, truth_perturb


def main():
    bundle, state, A, tfs, targets, truth_grn, truth_perturb = make_fixture()
    # ---- 1) GRN 推断 ----
    grn = G.run_grn(bundle, state, {"seed": 0})
    assert len(grn["edges"]) > 0, "run_grn 没有产出任何边"
    assert grn["arrays"]["coef"].shape == (3, 20, 280), grn["arrays"]["coef"].shape
    assert len(grn["tf_names"]) == 20 and len(grn["target_names"]) == 280
    assert grn["cluster_names"] == ["S0", "S1", "S2"]
    assert grn["metrics"]["n_edges"] > 0 and len(grn["metrics"]["hub_tfs"]) > 0
    assert grn["grn_source"] == "coexpression_ridge"
    assert grn["validity"]["directional_evidence"] == "weak"
    assert {f["kind"] for f in grn["figures"]} >= {"grn_hub_bar", "grn_network", "grn_heatmap"}
    with tempfile.TemporaryDirectory() as tmp:  # 模拟 engine 的 npz 落盘/回读
        path = os.path.join(tmp, "grn.npz")
        np.savez_compressed(path, **{k: np.asarray(v) for k, v in grn["arrays"].items()
                                     if v is not None})
        with np.load(path, allow_pickle=False) as data:
            assert data["coef"].shape == (3, 20, 280)
    print(f"[grn] edges={len(grn['edges'])} hub={grn['metrics']['hub_tfs'][0]['tf']} "
          f"cv_r2={grn['metrics']['cv_r2']}")
    # ---- 2) 有真值校验：AUROC > 0.6 ----
    bundle.truth = {"grn": truth_grn, "perturb": truth_perturb,
                    "perturb_meta": {"mode": "knockout", "n_heldout": 3}}
    report = G.run_validate(bundle, state, grn, {})
    auroc = report["metrics"]["grn_auroc"]
    assert auroc is not None and auroc > 0.6, f"AUROC={auroc} 未达到 0.6"
    assert report["metrics"]["n_heldout"] == 3 and report["verdict"]["passed"] is True
    print(f"[validate] AUROC={auroc:.3f} AUPRC={report['metrics']['grn_auprc']} "
          f"delta_spearman={report['metrics']['delta_spearman_mean']}")
    # ---- 3) 虚拟扰动：top 受影响基因含真实下游、state_shift > 0 ----
    perturbed = tfs[0]
    pert = G.run_perturb(bundle, state, grn, {"gene": perturbed})
    assert pert["state_shift"] is not None and pert["state_shift"] > 0, pert["state_shift"]
    top_affected = {row["gene"] for row in pert["affected"]}
    true_downstream = {targets[j] for j in np.where(A[tfs.index(perturbed)] != 0)[0]}
    hits = top_affected & true_downstream
    assert hits, f"{perturbed} 的 top 受影响基因里没有真实下游"
    assert pert["arrays"]["delta_gene"].shape == (len(bundle.genes),)
    assert np.asarray(pert["arrays"]["delta_latent"]).shape == (3, 15)
    assert np.asarray(pert["arrays"]["arrows"]).shape == (3, 2)
    assert len(pert["per_cluster"]) == 3
    assert any("假说" in note for note in pert["validity"]["notes"])
    print(f"[perturb] {perturbed} state_shift={pert['state_shift']:.4f} "
          f"{pert['source_state']}->{pert['target_state']} 真实下游命中 "
          f"{len(hits)}/{len(true_downstream)}")
    # ---- 4) 虚拟筛选：3 个 TF → 3 行 ----
    screened = G.run_screen(bundle, state, grn, {"genes": [tfs[0], tfs[1], tfs[2]]})
    assert len(screened["ranked"]) == 3, screened["ranked"]
    assert np.asarray(screened["arrays"]["scores"]).shape == (3,)
    assert screened["metrics"]["n_screened"] == 3
    print("[screen]", [(row["gene"], row["score"]) for row in screened["ranked"]])
    # ---- 5) 无真值时必须抛 prerequisite ----
    bare = Duck(); bare.genes, bare.meta, bare.truth = bundle.genes, {}, None
    try:
        G.run_validate(bare, state, grn, {})
    except Exception as error:  # noqa: BLE001
        assert getattr(error, "code", None) == "prerequisite", error
        print("[validate] 无真值 → prerequisite ✓")
    else:
        raise AssertionError("无真值时 run_validate 应该抛 prerequisite")
    print("all tests passed")


if __name__ == "__main__":
    main()
