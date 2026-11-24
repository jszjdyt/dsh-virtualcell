#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — GRN 推断 / 虚拟扰动 / 虚拟筛选 / GRN 校验（CONTRACT §7.3，另见 §6.5.2 / §6.5.3 / §4.1）。

四个入口：
    run_grn(bundle, state, params)           共表达先验 + 每簇标准化 Ridge → 有向边
    run_validate(bundle, state, grn, params) 有真值时：边 AUROC/AUPRC + 留出扰动 Δ 相关
    run_perturb(bundle, state, grn, params)  CellOracle 风格：初始 Δ → 沿边传播 → 投到 latent
    run_screen(bundle, state, grn, params)   批量虚拟敲除打分（state_shift × 特异度）

环境里没有 torch / celloracle，全部用 numpy / scipy / sklearn 的闭式解实现。

诚实性约束：共表达只能给关联，不能给因果。所有方向性结论都写进 validity.notes
（"GRN 来自共表达，方向性证据弱"）；只有挂了用户先验（prior='user'）才升到 moderate。
虚拟扰动是假说，不是结论。
"""

from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd
import scipy.sparse as sp
from scipy import stats

import vc_common as C
import vc_priors as P

__all__ = ["run_grn", "run_perturb", "run_screen", "run_validate"]

_EPS = 1e-9


def _opts(params, defaults: dict) -> dict:
    """合并参数：用户显式给的非 None 值覆盖默认值。"""
    out = dict(defaults)
    for key, value in (params or {}).items():
        if value is not None:
            out[key] = value
    return out


def _cluster_of(raw, labels, n_cells: int):
    """cluster 列 → (0..k-1 整数索引, 簇名列表)。兼容数字 id 与字符串标签。"""
    if raw is None:
        return np.zeros(int(n_cells), dtype=np.int64), ["all"]
    values = np.asarray(raw).ravel()
    if values.size != int(n_cells):
        raise C.err("prerequisite", f"clusters 长度 {values.size} 与细胞数 {n_cells} 不一致",
                    hint="重新运行「状态表征」以重建 clusters")
    if np.issubdtype(values.dtype, np.number):
        order = np.unique(values.astype(np.int64))
        mapping = {int(v): i for i, v in enumerate(order)}
        ids = np.array([mapping[int(v)] for v in values], dtype=np.int64)
        names = [str(v) for v in order]
    else:
        order, ids = np.unique(values.astype(str), return_inverse=True)
        names = [str(v) for v in order]
    label_list = [str(x) for x in (labels or [])]
    if len(label_list) == len(names) and len(names) > 0:
        names = label_list
    return ids.astype(np.int64), names


def _stage_arrays(state: dict) -> dict:
    """抽 state 里的表达矩阵 / HVG / latent / 分簇；缺关键字段时抛中文 prerequisite。"""
    state = state or {}
    arrays = state.get("arrays") or {}
    raw = arrays.get("X_log")
    if raw is None:
        raise C.err("prerequisite", "缺少状态表征结果（state.arrays['X_log']）",
                    hint="先运行「状态表征」一步，再跑 GRN / 扰动 / 筛选 / 校验")
    X = sp.csr_matrix(raw if sp.issparse(raw) else np.asarray(raw, dtype=np.float32),
                      dtype=np.float32)
    genes = [str(g) for g in (state.get("genes") or [])]
    if not genes or len(genes) != int(X.shape[1]):
        raise C.err("prerequisite", "state.genes 与 X_log 的列数不一致",
                    hint="检查 state 结果是否完整（引擎应回放 genes 字段）")
    pos = {gene: index for index, gene in enumerate(genes)}
    hvg = [str(g) for g in (state.get("hvg") or genes)]
    hvg = [g for g in dict.fromkeys(hvg) if g in pos] or list(genes)
    hvg_idx = np.array([pos[g] for g in hvg], dtype=np.int64)
    latent = arrays.get("latent")
    latent = None if latent is None else np.asarray(latent, dtype=np.float32)
    cluster_ids, cluster_names = _cluster_of(
        arrays.get("clusters"), state.get("cluster_labels"), int(X.shape[0]))
    return {
        "X": X, "genes": genes, "gene_pos": pos, "hvg": hvg, "hvg_idx": hvg_idx,
        "latent": latent, "cluster_ids": cluster_ids, "cluster_names": cluster_names,
        "n_cells": int(X.shape[0]), "n_genes": int(X.shape[1]), "k": len(cluster_names),
    }


def _standardize(matrix):
    """逐列 z-score；返回 (z, mean, std)，常数列 std 置 1。"""
    array = np.asarray(matrix, dtype=np.float64)
    if array.size == 0:
        return array, np.zeros((1, array.shape[1] if array.ndim == 2 else 0)), np.ones_like(array)
    center = array.mean(axis=0, keepdims=True)
    scale = array.std(axis=0, keepdims=True)
    scale[scale < 1e-8] = 1.0
    return (array - center) / scale, center, scale


def _ridge_fit(design, response, alpha: float):
    """闭式解 Ridge：(XᵀX+αI)⁻¹Xᵀy。design (n×p)、response (n,) 或 (n×m)。"""
    Xd = np.asarray(design, dtype=np.float64)
    Y = np.asarray(response, dtype=np.float64)
    p = Xd.shape[1]
    if p == 0:
        return np.zeros((0,) if Y.ndim == 1 else (0, Y.shape[1]))
    gram = Xd.T @ Xd
    gram[np.diag_indices_from(gram)] += max(float(alpha), 0.0)
    rhs = Xd.T @ Y
    try:
        return np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(gram, rhs, rcond=None)[0]


def _spearman_block(A, B):
    """列对列 Spearman：A (n×p) 与 B (n×q) → (p×q)。秩变换后一次矩阵乘。"""
    def _rank_z(M):
        M = np.asarray(M, dtype=np.float64)
        if M.ndim != 2 or M.shape[0] < 3 or M.size == 0:
            return np.zeros_like(M if M.ndim == 2 else M.reshape(-1, 1))
        R = stats.rankdata(M, axis=0)
        R = R - R.mean(axis=0, keepdims=True)
        sd = R.std(axis=0, keepdims=True)
        sd[sd < 1e-8] = 1.0
        return R / sd

    RA, RB = _rank_z(A), _rank_z(B)
    if RA.size == 0 or RB.size == 0:
        return np.zeros((RA.shape[1], RB.shape[1]))
    return (RA.T @ RB) / max(1, RA.shape[0])


def _hypergeom_ora(hits, universe, gene_sets) -> list:
    """超几何 ORA（未做多重校正）：返回 [{set,n_hits,n_set,fold,p,genes}]。"""
    uni = {str(g) for g in universe}
    hit = {str(g) for g in hits} & uni
    total, n = len(uni), len(hit)
    rows = []
    if total == 0 or n == 0:
        return rows
    for name, genes in (gene_sets or {}).items():
        members = {str(g) for g in genes} & uni
        m = len(members)
        if m == 0:
            continue
        overlap = hit & members
        k = len(overlap)
        p = 1.0 if k == 0 else float(stats.hypergeom.sf(k - 1, total, m, n))
        fold = (k / n) / (m / total) if n and m else 0.0
        rows.append({"set": str(name), "n_hits": int(k), "n_set": int(m),
                     "fold": float(fold), "p": float(p), "genes": sorted(overlap)})
    return rows


def _bh(pvalues):
    """Benjamini-Hochberg FDR；返回与输入等长的 q 值数组。"""
    p = np.asarray(pvalues, dtype=float).ravel()
    if p.size == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * p.size / (np.arange(p.size) + 1.0)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty_like(p)
    out[order] = np.clip(q, 0.0, 1.0)
    return out


# ---------------------------------------------------------------------------
# 候选边与每簇拟合（run_grn 的内部零件）
# ---------------------------------------------------------------------------

def _column_variance(X, cols):
    """稀疏矩阵指定列的方差（E[x²]-E[x]²），避免整体致密化。"""
    sub = X[:, np.asarray(cols, dtype=np.int64)]
    mean = np.asarray(sub.mean(axis=0)).ravel()
    mean_sq = np.asarray(sub.multiply(sub).mean(axis=0)).ravel()
    return np.maximum(mean_sq - mean ** 2, 0.0)


def _coexpression_scores(X, tf_idx, tg_idx, rng, max_cells: int = 4000):
    """TF × target 的 Spearman 相关矩阵（细胞 > max_cells 时随机下采样，可复现）。"""
    n_cells = int(X.shape[0])
    rows = np.arange(n_cells)
    if n_cells > int(max_cells):
        rows = np.sort(rng.choice(n_cells, size=int(max_cells), replace=False))
    Xt = np.asarray(X[rows][:, tf_idx].todense(), dtype=np.float32)
    Xg = np.asarray(X[rows][:, tg_idx].todense(), dtype=np.float32)
    return _spearman_block(Xt, Xg)


def _coexpression_scores_grouped(X, tf_idx, tg_idx, cluster_ids, k, rng, max_per_cluster: int = 2000):
    """**簇内** Spearman 相关，按簇细胞数加权平均。

    为什么不用全局相关：细胞状态本身会同时驱动 TF 与它的靶基因，全局相关会被"状态结构"
    主导，导致候选边全是状态程序基因（真实靶点被挤出去）。先在各簇内部算相关再平均，
    等价于去掉簇均值后的相关，更接近"同一状态下谁跟着谁变"。
    符号取加权平均相关的符号。
    """
    n_tf, n_tg = len(tf_idx), len(tg_idx)
    total = np.zeros((n_tf, n_tg), dtype=np.float64)
    weight_sum = 0.0
    cluster_ids = np.asarray(cluster_ids).ravel()
    for ci in range(int(k)):
        rows = np.where(cluster_ids == ci)[0]
        if rows.size < 8:
            continue
        if rows.size > int(max_per_cluster):
            rows = np.sort(rng.choice(rows, size=int(max_per_cluster), replace=False))
        Xt = np.asarray(X[rows][:, tf_idx].todense(), dtype=np.float32)
        Xg = np.asarray(X[rows][:, tg_idx].todense(), dtype=np.float32)
        block = _spearman_block(Xt, Xg)
        weight = float(rows.size)
        total += weight * block
        weight_sum += weight
    if weight_sum <= 0:
        return np.zeros((n_tf, n_tg), dtype=np.float32)
    return (total / weight_sum).astype(np.float32)


def _user_prior_edges(edges, tf_names, tg_names) -> dict:
    """用户 GRN 先验 [{tf,target,sign?,weight?}] → {(tf_local,tg_local): signed_weight}。"""
    tf_pos = {str(name).upper(): i for i, name in enumerate(tf_names)}
    tg_pos = {str(name).upper(): i for i, name in enumerate(tg_names)}
    out = {}
    for item in edges or []:
        if not isinstance(item, dict):
            continue
        tf = str(item.get("tf") or "").strip().upper()
        tg = str(item.get("target") or "").strip().upper()
        if tf not in tf_pos or tg not in tg_pos:
            continue
        weight = item.get("weight")
        try:
            weight = 1.0 if weight is None else float(weight)
        except (TypeError, ValueError):
            weight = 1.0
        sign = item.get("sign")
        if sign is not None:
            try:
                positive = float(sign) >= 0
            except (TypeError, ValueError):
                positive = not str(sign).strip().startswith("-")
            weight = abs(weight) * (1.0 if positive else -1.0)
        out[(tf_pos[tf], tg_pos[tg])] = float(weight)
    return out


def _fit_cluster_coef(X, tf_idx, tg_idx, cand_lists, rows, alpha, chunk: int = 400):
    """单簇 Ridge：逐 target 用其候选 TF 做标准化闭式解，返回 (n_tf×n_tg)。"""
    n_tf, n_tg = len(tf_idx), len(tg_idx)
    coef = np.zeros((n_tf, n_tg), dtype=np.float32)
    if n_tf == 0 or n_tg == 0 or len(rows) == 0:
        return coef
    sub = X[rows]
    Ztf = _standardize(np.asarray(sub[:, tf_idx].todense(), dtype=np.float32))[0]
    for start in range(0, n_tg, max(1, int(chunk))):
        stop = min(n_tg, start + chunk)
        Ztg = _standardize(np.asarray(sub[:, tg_idx[start:stop]].todense(),
                                      dtype=np.float32))[0]
        for local, tj in enumerate(range(start, stop)):
            cand = cand_lists[tj]
            if cand.size == 0:
                continue
            coef[cand, tj] = _ridge_fit(Ztf[:, cand], Ztg[:, local], alpha)
    return coef


def _cv_r2(X, tf_idx, tg_idx, cand_lists, cells, alpha, rng, n_targets: int = 200,
           folds: int = 5):
    """5 折 CV 的平均 R²（只抽 n_targets 个 target 控制耗时）；返回 (mean_r2, n_used)。"""
    usable = [tj for tj in range(len(tg_idx)) if cand_lists[tj].size >= 2]
    rows = np.asarray(cells, dtype=np.int64)
    if not usable or rows.size < folds * 4:
        return None, 0
    pick = usable
    if len(pick) > n_targets:
        pick = list(rng.choice(np.asarray(pick), size=int(n_targets), replace=False))
    if rows.size > 1500:
        rows = np.sort(rng.choice(rows, size=1500, replace=False))
    sub = X[rows]
    Ztf = _standardize(np.asarray(sub[:, tf_idx].todense(), dtype=np.float32))[0]
    Ztg = _standardize(np.asarray(sub[:, tg_idx].todense(), dtype=np.float32))[0]
    fold_id = rng.integers(0, folds, size=rows.size)
    r2s = []
    for tj in pick:
        cand = cand_lists[tj]
        y = Ztg[:, tj]
        pred = np.zeros_like(y)
        for fold in range(folds):
            test = fold_id == fold
            if not test.any() or test.all():
                continue
            beta = _ridge_fit(Ztf[~test][:, cand], y[~test], alpha)
            pred[test] = Ztf[test][:, cand] @ beta
        ss_res = float(((y - pred) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        if ss_tot > _EPS:
            r2s.append(1.0 - ss_res / ss_tot)
    return (float(np.mean(r2s)) if r2s else None), len(pick)


# ---------------------------------------------------------------------------
# GRN 推断
# ---------------------------------------------------------------------------

def run_grn(bundle, state, params: dict | None = None) -> dict:
    """推断有向调控网络：共表达先验（可混用户先验）+ 每簇标准化 Ridge 闭式解。

    TF 候选 = vc_priors.tf_universe() ∩ 表达基因；target = 非 TF 基因（超 top_targets 按方差取 top）。
    每个 target 只拿自己的候选 TF 做 Ridge，系数矩阵形状 (n_clusters × n_tf × n_target)。
    """
    t0 = time.time()
    p = _opts(params, {
        "method": "ridge", "alpha": 0.1, "prior": "coexpression",
        "top_targets": 2000, "min_cells": 30, "n_boot": 0,
        "tf_list": "builtin", "max_candidates_per_tf": 60, "seed": 0,
        "tfs": None, "prior_edges": None, "sparse_threshold": 0.10,
    })
    method = str(p.get("method") or "ridge").lower()
    if method != "ridge":
        raise C.err("bad_request", f"不支持的 GRN 方法：{method}",
                    hint="当前版本只内置 ridge 闭式解（环境无 torch / celloracle）")
    warnings: list[str] = []
    C.progress("准备 GRN 输入…", 5)
    data = _stage_arrays(state)
    X, genes, pos = data["X"], data["genes"], data["gene_pos"]
    n_cells, n_genes = data["n_cells"], data["n_genes"]
    cluster_ids, cluster_names = data["cluster_ids"], data["cluster_names"]
    k = len(cluster_names)
    rng = C.rng_for(p.get("seed"))

    # ---- 1) TF 候选 --------------------------------------------------------
    raw_tfs = p.get("tfs")
    if raw_tfs is None:
        tf_list = p.get("tf_list")
        if isinstance(tf_list, (list, tuple)) and tf_list:
            raw_tfs = list(tf_list)
        elif isinstance(tf_list, str) and tf_list.strip().lower() not in ("builtin", ""):
            raw_tfs = tf_list
    if isinstance(raw_tfs, str):
        raw_tfs = [x for x in raw_tfs.replace(";", ",").replace(" ", ",").split(",") if x]
    pool = [str(t) for t in (raw_tfs or P.tf_universe())]
    tf_names = [g for g in dict.fromkeys(pool) if g in pos]
    if len(tf_names) < len(pool):
        warnings.append(f"{len(pool) - len(tf_names)} 个 TF 候选不在表达基因里，已跳过（保留 {len(tf_names)} 个）")
    if not tf_names:
        raise C.err("prerequisite", "基因列表里没有任何 TF 候选",
                    hint="用 params['tfs'] 指定候选 TF，或检查基因符号是否与数据一致")
    tf_idx = np.array([pos[g] for g in tf_names], dtype=np.int64)
    tf_set = set(tf_names)

    # ---- 2) target 候选（非 TF；超量按方差取 top）---------------------------
    target_all = np.array([i for i, g in enumerate(genes) if g not in tf_set], dtype=np.int64)
    top_targets = int(p.get("top_targets") or 0)
    if top_targets > 0 and target_all.size > top_targets:
        variance = _column_variance(X, target_all)
        target_all = np.sort(target_all[np.argsort(-variance)[:top_targets]])
    tg_idx = target_all
    tg_names = [genes[int(i)] for i in tg_idx]
    n_tf, n_tg = len(tf_names), len(tg_names)
    if n_tg == 0:
        raise C.err("prerequisite", "没有可用的 target 基因（除 TF 之外为空）")
    C.progress(f"候选：{n_tf} 个 TF × {n_tg} 个 target", 15)

    # ---- 3) 候选边 --------------------------------------------------------
    prior_mode = str(p.get("prior") or "coexpression").lower()
    if prior_mode not in ("coexpression", "user", "mixed"):
        raise C.err("bad_request", f"未知 prior：{prior_mode}",
                    hint="prior 支持 coexpression | user | mixed")
    cand_sets = [set() for _ in range(n_tg)]
    prior_weights = {}
    if prior_mode in ("user", "mixed"):
        prior_weights = _user_prior_edges(p.get("prior_edges"), tf_names, tg_names)
        if prior_mode == "user" and not prior_weights:
            raise C.err("bad_request", "prior='user' 但 prior_edges 里没有落在候选集内的边",
                        hint="先挂载 GRN 先验（kind=grn_prior，列 tf,target,sign,weight）")
        for ti, tj in prior_weights:
            cand_sets[tj].add(ti)
    maxc = max(1, int(p.get("max_candidates_per_tf") or 60))
    n_coexp_candidates = 0
    if prior_mode in ("coexpression", "mixed"):
        # 候选边 = 全局共表达 top-k ∪ 簇内平均共表达 top-(k/2)。
        # 全局相关能抓住"状态程序驱动"的调控（TF 与靶基因随状态同增同减）；
        # 簇内相关能抓住"同一状态下细胞间"的调控（不受状态结构混杂）。
        # 只取其中一种都会系统性漏掉另一类真实调控，所以取并集。
        t0_coexp = time.time()
        corr_global = _coexpression_scores(X, tf_idx, tg_idx, rng)
        corr_within = (_coexpression_scores_grouped(X, tf_idx, tg_idx, cluster_ids, k, rng)
                       if k > 1 else corr_global)
        n_within = max(3, maxc // 2)
        for ti in range(n_tf):
            for corr, limit in ((corr_global, maxc), (corr_within, n_within)):
                strength = np.abs(corr[ti])
                for tj in np.argsort(-strength)[:limit]:
                    if strength[tj] > 1e-3:
                        cand_sets[int(tj)].add(ti)
                        n_coexp_candidates += 1
        warnings.append(f"候选边：全局共表达 top{maxc} ∪ 簇内共表达 top{n_within}，去重后 "
                        f"{sum(len(item) for item in cand_sets):,} 对（{time.time() - t0_coexp:.1f}s）")
    cand_lists = [np.array(sorted(s), dtype=np.int64) for s in cand_sets]
    candidate_mask = np.zeros((n_tf, n_tg), dtype=bool)
    for tj, items in enumerate(cand_lists):
        if items.size:
            candidate_mask[items, tj] = True
    if not any(item.size for item in cand_lists):
        raise C.err("prerequisite", "候选边为空：共表达信号不足或先验为空",
                    hint="放宽 max_candidates_per_tf / top_targets，或挂 GRN 先验")
    C.progress("拟合每簇 Ridge 系数…", 25)

    # ---- 4) alpha（auto 时 3 候选 × 5 折 CV）+ 每簇拟合 --------------------
    alpha = p.get("alpha", 0.1)
    cv_r2 = None
    all_rows = np.arange(n_cells)
    if isinstance(alpha, str) and alpha.strip().lower() in ("auto", ""):
        best = None
        for candidate_alpha in (0.01, 0.1, 1.0):
            score, _ = _cv_r2(X, tf_idx, tg_idx, cand_lists, all_rows, candidate_alpha, rng)
            if score is not None and (best is None or score > best[0]):
                best = (score, candidate_alpha)
        alpha, cv_r2 = (best[1], best[0]) if best else (0.1, None)
    else:
        try:
            alpha = float(alpha)
        except (TypeError, ValueError):
            alpha = 0.1
        alpha = min(max(alpha, 1e-4), 100.0)
        cv_r2, _ = _cv_r2(X, tf_idx, tg_idx, cand_lists, all_rows, alpha, rng)
    min_cells = max(3, int(p.get("min_cells") or 30))
    coef = np.zeros((k, n_tf, n_tg), dtype=np.float32)
    fallbacks: list[str] = []
    cluster_sizes = np.bincount(cluster_ids, minlength=k).astype(np.int64)
    for ci in range(k):
        rows = np.where(cluster_ids == ci)[0]
        if rows.size < min_cells:
            fallbacks.append(cluster_names[ci])
            rows = all_rows
        coef[ci] = _fit_cluster_coef(X, tf_idx, tg_idx, cand_lists, rows, alpha)
    C.progress("稀疏化并汇总边表…", 70)

    # ---- 5) 稀疏化：|coef| ≥ 阈值 或 每 target top3 -----------------------
    threshold = max(0.0, float(p.get("sparse_threshold") or 0.10))
    abs_coef = np.abs(coef)
    keep = abs_coef >= threshold
    top3 = np.argsort(-abs_coef, axis=1)[:, :3, :]
    for ci in range(k):
        keep[ci][top3[ci], np.arange(n_tg)] = True
    coef = np.where(keep, coef, 0.0).astype(np.float32)
    del abs_coef, keep, top3

    # ---- 6) hub / 边表 / 密度（逐簇累计，避免再开一份全局 abs）------------
    out_degree = np.zeros(n_tf, dtype=np.int64)
    weight_sum = np.zeros(n_tf, dtype=np.float64)
    parts_ci, parts_ti, parts_tj, parts_w = [], [], [], []
    for ci in range(k):
        block = coef[ci]
        mag = np.abs(block)
        out_degree += (mag > 0).sum(axis=1)
        weight_sum += mag.sum(axis=1)
        ti, tj = np.nonzero(block)
        if ti.size:
            parts_ci.append(np.full(ti.size, ci, dtype=np.int64))
            parts_ti.append(ti.astype(np.int64))
            parts_tj.append(tj.astype(np.int64))
            parts_w.append(block[ti, tj].astype(np.float64))
    if parts_w:
        all_ci = np.concatenate(parts_ci); all_ti = np.concatenate(parts_ti)
        all_tj = np.concatenate(parts_tj); all_w = np.concatenate(parts_w)
    else:
        all_ci = all_ti = all_tj = np.zeros(0, dtype=np.int64)
        all_w = np.zeros(0, dtype=np.float64)
    n_edges = int(all_w.size)
    n_pairs = int(np.unique(all_ti * max(1, n_tg) + all_tj).size) if n_edges else 0
    density = n_pairs / max(1, n_tf * n_tg)
    order = np.argsort(-np.abs(all_w))[:5000] if n_edges else np.zeros(0, dtype=np.int64)
    edges = [{
        "tf": tf_names[int(all_ti[i])], "target": tg_names[int(all_tj[i])],
        "score": C.fnum(float(abs(all_w[i]))), "sign": int(1 if all_w[i] >= 0 else -1),
        "weight": C.fnum(float(all_w[i])), "cluster": cluster_names[int(all_ci[i])],
    } for i in order]
    # hub 排序用"总绝对权重"（出度在 top3 稀疏化后几乎恒定，不能区分强弱）
    order_hub = np.argsort(-weight_sum)[:10]
    hub_tfs = [{"tf": tf_names[int(i)], "out_degree": int(out_degree[i]),
                "mean_abs_weight": C.fnum(weight_sum[i] / max(1, int(out_degree[i]))),
                "total_weight": C.fnum(weight_sum[i])}
               for i in order_hub]

    # ---- 7) 存储（>2e7 个系数时逐簇存 CSR 三件套，metrics.blocked=True）-----
    blocked = bool(k * n_tf * n_tg > 20_000_000)
    arrays = {"tf_idx": tf_idx, "tg_idx": tg_idx, "candidate_mask": candidate_mask}
    coef_blocks_meta = None
    if blocked:
        # 逐簇存 CSR 的 data/indices/indptr/shape（纯 ndarray，engine 的 npz 能安全回读）
        arrays["coef_blocked"] = np.asarray([1], dtype=np.int8)
        coef_blocks_meta = []
        for ci in range(k):
            csr = sp.csr_matrix(coef[ci])
            arrays[f"coef_block_{ci}_data"] = np.asarray(csr.data, dtype=np.float32)
            arrays[f"coef_block_{ci}_indices"] = np.asarray(csr.indices, dtype=np.int32)
            arrays[f"coef_block_{ci}_indptr"] = np.asarray(csr.indptr, dtype=np.int32)
            arrays[f"coef_block_{ci}_shape"] = np.asarray(csr.shape, dtype=np.int64)
            coef_blocks_meta.append({"cluster": cluster_names[ci], "shape": [int(n_tf), int(n_tg)],
                                     "nnz": int(csr.nnz)})
        del coef
    else:
        arrays["coef"] = coef

    # ---- 8) 可选 Bootstrap：候选边集合的重采样 Jaccard 稳定性 ---------------
    bootstrap = None
    n_boot = max(0, min(int(p.get("n_boot") or 0), 10))
    if n_boot > 0 and n_edges:
        original = set((int(a), int(b)) for a, b in zip(all_ti, all_tj))
        values = []
        for _ in range(n_boot):
            idx = np.sort(rng.choice(n_cells, size=n_cells, replace=True))
            corr_b = _coexpression_scores(X[idx], tf_idx, tg_idx, rng)
            boot_edges = set()
            for ti in range(n_tf):
                strength = np.abs(corr_b[ti])
                for tj in np.argsort(-strength)[:maxc]:
                    if strength[tj] > 1e-3:
                        boot_edges.add((ti, int(tj)))
            union = original | boot_edges
            values.append(len(original & boot_edges) / max(1, len(union)))
        bootstrap = float(np.mean(values))
        warnings.append("n_boot>0：稳定性只在共表达候选边集合上做重采样 Jaccard，不是完整系数 bootstrap")
    C.progress("汇总 GRN 指标…", 90)

    # ---- 9) 组装返回值 -----------------------------------------------------
    runtime = float(time.time() - t0)
    grn_source = {"coexpression": "coexpression_ridge", "user": "user_prior",
                  "mixed": "mixed"}[prior_mode]
    metrics = {
        "n_edges": n_edges, "n_tfs": n_tf, "n_targets": n_tg,
        "density": C.fnum(density, 6), "hub_tfs": hub_tfs,
        "cv_r2": C.fnum(cv_r2, 4), "runtime_s": C.fnum(runtime, 2),
        "n_cells": n_cells, "n_clusters": k, "alpha": alpha,
        "n_candidate_pairs": int(candidate_mask.sum()),
        "n_scored_pairs": n_pairs, "n_user_edges": len(prior_weights),
        "n_coexpression_candidates": n_coexp_candidates,
        "blocked": blocked, "prior": prior_mode, "cluster_sizes": cluster_sizes.tolist(),
    }
    if bootstrap is not None:
        metrics["bootstrap_stability"] = C.fnum(bootstrap)
    notes = ["GRN 由共表达（Spearman 先验）+ 每簇标准化 Ridge 推断：只能说明关联，不能证明因果；"
             "边的方向来自回归系数符号，不是 ATAC/ChIP 证据。"]
    if prior_mode in ("user", "mixed"):
        notes.insert(0, f"并入了 {len(prior_weights)} 条用户 GRN 先验边，方向性证据提升到 moderate。")
    if fallbacks:
        notes.append(f"{len(fallbacks)} 个状态细胞数 < min_cells={min_cells}，该簇系数回退到全局模型。")
    evidence = "moderate" if prior_mode in ("user", "mixed") else "weak"
    validity = {"directional_evidence": evidence, "notes": notes}

    biggest = hub_tfs[0] if hub_tfs else None
    summary = (f"在 {n_cells:,} 个细胞 × {n_genes:,} 个基因上推断出 {n_edges:,} 条有向边"
               f"（{n_tf} 个 TF × {n_tg} 个 target，{k} 个状态；来源：{grn_source}）")
    if biggest:
        summary += f"；最大 hub 是 {biggest['tf']}（出度 {biggest['out_degree']}，平均 |w|={biggest['mean_abs_weight']}）"
    if cv_r2 is not None:
        summary += f"；5 折交叉验证 R²={cv_r2:.2f}"
    summary += "。共表达推断，方向性证据弱，需实验验证。"

    if fallbacks:
        warnings.append("状态细胞数不足，以下状态回退到全局模型：" + "、".join(fallbacks))
    if blocked:
        warnings.append(f"系数张量 {k}×{n_tf}×{n_tg} 超过 2e7，已改按簇稀疏存储（metrics.blocked=True）")
    if cv_r2 is not None and cv_r2 < 0.05:
        warnings.append(f"交叉验证 R² 偏低（{cv_r2:.3f}）：线性 Ridge 解释力弱，下游扰动结论需谨慎")
    if n_edges == 0:
        warnings.append("没有推断出任何边：检查候选 TF/target 或增大 max_candidates_per_tf")
    if n_tf < 5:
        warnings.append(f"只有 {n_tf} 个 TF 候选，网络覆盖有限")

    result = {
        "arrays": arrays,
        "tf_names": tf_names, "target_names": tg_names, "cluster_names": cluster_names,
        "edges": edges, "metrics": metrics, "grn_source": grn_source,
        "validity": validity, "tables": [
            {"id": "grn_edges", "title": "调控边（按 |权重| 排序）",
             "columns": ["tf", "target", "score", "sign", "weight", "cluster"], "rows": edges},
            {"id": "hub_tfs", "title": "高连接 TF（hub）",
             "columns": ["tf", "out_degree", "mean_abs_weight"], "rows": hub_tfs},
        ],
        "figures": [
            {"kind": "grn_hub_bar", "title": "TF 出度（hub）"},
            {"kind": "grn_network", "title": "调控网络（top 边）"},
            {"kind": "grn_heatmap", "title": "TF×target 权重热图"},
        ],
        "summary": summary, "warnings": warnings, "params": p,
    }
    if coef_blocks_meta is not None:
        result["coef_blocks"] = coef_blocks_meta
    C.progress("GRN 完成", 100)
    return result


# ---------------------------------------------------------------------------
# GRN 数组访问 + 扰动模拟内核（perturb / screen / validate 共用）
# ---------------------------------------------------------------------------

def _resolve_gene(genes, name):
    """按原名或忽略大小写找到基因名；找不到返回 None。"""
    if not name:
        return None
    text = str(name).strip()
    if text in genes:
        return text
    upper = text.upper()
    for gene in genes:
        if str(gene).upper() == upper:
            return str(gene)
    return None


def _coef_for_cluster(grn, ci: int, n_tf: int | None = None, n_tg: int | None = None):
    """取某簇 (n_tf×n_tg) 系数矩阵。

    兼容：稠密 3D、扁平 2D（(k*n_tf)×n_tg）、逐簇列表、以及 blocked 模式的
    ``coef_block_<i>_{data,indices,indptr,shape}`` CSR 三件套（npz 安全）。
    """
    grn = grn or {}
    arrays = grn.get("arrays") or {}
    n_tf = max(int(n_tf if n_tf is not None else len(grn.get("tf_names") or [])), 0)
    n_tg = max(int(n_tg if n_tg is not None else len(grn.get("target_names") or [])), 0)
    coef = arrays.get("coef")
    if coef is not None and not isinstance(coef, list):
        dense = (np.asarray(coef.todense(), dtype=np.float32) if sp.issparse(coef)
                 else np.asarray(coef))
        if dense.ndim == 3:
            return np.asarray(dense[min(max(int(ci), 0), dense.shape[0] - 1)], dtype=np.float32)
        if dense.ndim == 2:
            if n_tf == 0 or dense.shape == (n_tf, n_tg):
                return np.asarray(dense, dtype=np.float32)
            start = max(int(ci), 0) * n_tf
            block = dense[start:start + n_tf, :]
            if block.shape[0] == n_tf:
                return np.asarray(block, dtype=np.float32)
    # blocked 模式：逐簇 CSR 三件套
    prefix = f"coef_block_{int(ci)}_"
    data = arrays.get(prefix + "data")
    if data is not None:
        shape = arrays.get(prefix + "shape")
        shape = (tuple(int(v) for v in np.asarray(shape).ravel()) if shape is not None
                 else (n_tf, n_tg))
        block = sp.csr_matrix((np.asarray(data, dtype=np.float32),
                               np.asarray(arrays[prefix + "indices"], dtype=np.int64),
                               np.asarray(arrays[prefix + "indptr"], dtype=np.int64)),
                              shape=shape)
        return np.asarray(block.todense(), dtype=np.float32)
    blocks = arrays.get("coef_blocks")
    if blocks is None and isinstance(arrays.get("coef"), list):
        blocks = arrays.get("coef")
    if blocks is not None and len(blocks) > int(ci):
        block = blocks[int(ci)]
        if isinstance(block, dict):
            block = block.get("coef")
        return np.asarray(block, dtype=np.float32)
    return np.zeros((n_tf, n_tg), dtype=np.float32)


def _latent_pack(state, data):
    """取 latent / 质心 / 载荷 / 2D 映射并校验维度；缺失时给中文 prerequisite。"""
    state = state or {}
    arrays = state.get("arrays") or {}
    latent = data.get("latent")
    if latent is None:
        latent = arrays.get("latent")
    if latent is not None:
        latent = np.asarray(latent, dtype=np.float32)
    centroids = state.get("cluster_centroids")
    if centroids is None:
        centroids = arrays.get("cluster_centroids")
    loadings = state.get("latent_loadings")
    if loadings is None:
        loadings = arrays.get("latent_loadings")
    emb = state.get("embedding_loadings")
    if emb is None:
        emb = arrays.get("embedding_loadings")
    missing = []
    if latent is None:
        missing.append("arrays['latent']")
    if centroids is None:
        missing.append("cluster_centroids")
    if loadings is None:
        missing.append("latent_loadings")
    if missing:
        raise C.err("prerequisite", "状态空间不完整：缺少 " + "、".join(missing),
                    hint="重新运行「状态表征」；扰动箭头需要 latent / cluster_centroids / latent_loadings")
    centroids = np.asarray(centroids, dtype=np.float32)
    loadings = np.asarray(loadings, dtype=np.float32)
    n_pcs = int(latent.shape[1])
    if centroids.ndim != 2 or centroids.shape[1] != n_pcs:
        raise C.err("prerequisite", f"cluster_centroids 形状 {centroids.shape} 与 latent 维数 {n_pcs} 不匹配")
    if loadings.ndim != 2 or loadings.shape[1] != n_pcs:
        raise C.err("prerequisite", f"latent_loadings 形状 {loadings.shape} 与 latent 维数 {n_pcs} 不匹配")
    emb = None if emb is None else np.asarray(emb, dtype=np.float32)
    if emb is not None and (emb.ndim != 2 or emb.shape[0] != n_pcs or emb.shape[1] != 2):
        emb = None
    return {"latent": latent, "centroids": centroids, "loadings": loadings,
            "emb": emb, "n_pcs": n_pcs, "k": int(centroids.shape[0])}


def _one_minus_cos(a, b) -> float:
    """数值稳定的 1-cos（小角度不丢精度），裁剪到 [0, 2]。"""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na < _EPS or nb < _EPS:
        return 0.0
    diff = va / na - vb / nb
    return float(min(2.0, max(0.0, 0.5 * float(diff @ diff))))


def _simulate(state, data, grn, gene, mode: str = "knockout", strength: float = 1.0,
              n_prop: int = 3, clip: float = 3.0, per_cell: bool = True,
              focus: int | None = None) -> dict:
    """沿 GRN 传播一次虚拟扰动。

    返回全基因平均 Δ、每簇平均 Δ、每簇 latent 位移、2D 箭头与状态位移。
    per_cell=False 时用"每簇平均 Δ"做线性传播（screen 用，快一个数量级）。
    """
    warnings: list[str] = []
    genes, pos = data["genes"], data["gene_pos"]
    n_cells, n_genes, k = data["n_cells"], data["n_genes"], data["k"]
    X, cluster_ids, names = data["X"], data["cluster_ids"], data["cluster_names"]
    mode_key = str(mode or "knockout").strip().lower()
    if mode_key in ("ko", "knockout", "knock-out"):
        mode_key = "knockout"
    elif mode_key in ("kd", "knockdown", "knock-down"):
        mode_key = "knockdown"
    elif mode_key in ("oe", "overexpression", "over-expression", "over_expression"):
        mode_key = "overexpression"
    else:
        raise C.err("bad_request", f"未知扰动模式：{mode}",
                    hint="mode 支持 knockout | knockdown | overexpression")
    resolved = _resolve_gene(genes, gene)
    if resolved is None:
        raise C.err("bad_request", f"基因 {gene} 不在当前数据集的基因列表里",
                    hint="检查拼写；用 action=preview 可查看基因名")
    g_global = pos[resolved]
    pack = _latent_pack(state, data)
    centroids, loadings, emb, n_pcs = pack["centroids"], pack["loadings"], pack["emb"], pack["n_pcs"]
    if centroids.shape[0] != k:
        warnings.append(f"cluster_centroids 行数 {centroids.shape[0]} 与状态数 {k} 不一致，按前 {k} 个使用")

    # ---- GRN 侧索引：只保留数据里存在的 TF / target ----
    grn_tf, grn_tg = list(grn.get("tf_names") or []), list(grn.get("target_names") or [])
    tf_rows = [i for i, g in enumerate(grn_tf) if g in pos]
    tg_cols = [j for j, g in enumerate(grn_tg) if g in pos]
    tf_genes = [grn_tf[i] for i in tf_rows]
    tg_genes = [grn_tg[j] for j in tg_cols]
    tf_global = np.array([pos[g] for g in tf_genes], dtype=np.int64)
    tg_global = np.array([pos[g] for g in tg_genes], dtype=np.int64)
    if resolved not in grn_tf:
        warnings.append(f"{resolved} 不在推断网络的 TF 列表里：没有它的出边，模拟只反映初始 Δ（下游传播为空）")

    # ---- 活跃基因：HVG ∪ TF ∪ target ∪ 被扰动基因 ----
    hvg_global = data["hvg_idx"]
    active_mask = np.zeros(n_genes, dtype=bool)
    active_mask[hvg_global] = True
    if tf_global.size:
        active_mask[tf_global] = True
    if tg_global.size:
        active_mask[tg_global] = True
    active_mask[g_global] = True
    active = np.where(active_mask)[0]
    local_of = {int(g): i for i, g in enumerate(active)}
    m = int(active.size)
    tf_local = np.array([local_of[int(g)] for g in tf_global], dtype=np.int64)
    tg_local = np.array([local_of[int(g)] for g in tg_global], dtype=np.int64)
    g_local = local_of[int(g_global)]

    # ---- 每个簇的系数块与限幅 std ----
    coef_blocks = []
    std_lim = np.ones((k, max(1, tg_global.size)), dtype=np.float32)
    cluster_cells = [np.where(cluster_ids == ci)[0] for ci in range(k)]
    for ci in range(k):
        block = _coef_for_cluster(grn, ci, n_tf=len(grn_tf), n_tg=len(grn_tg))
        if tf_rows and tg_cols:
            coef_blocks.append(np.asarray(block[np.ix_(tf_rows, tg_cols)], dtype=np.float64))
        else:
            coef_blocks.append(np.zeros((len(tf_rows), len(tg_cols)), dtype=np.float64))
        cells = cluster_cells[ci]
        if cells.size and tg_global.size:
            sub = X[cells][:, tg_global]
            mean = np.asarray(sub.mean(axis=0)).ravel()
            var = np.asarray(sub.multiply(sub).mean(axis=0)).ravel() - mean ** 2
            std_lim[ci] = np.sqrt(np.maximum(var, 1e-8)).astype(np.float32)
    lim = float(max(0.1, clip)) * std_lim

    # ---- 初始 Δ（逐细胞，或逐簇平均）----
    use_cells = bool(per_cell) and n_cells * max(1, m) <= 40_000_000
    if not use_cells and per_cell:
        warnings.append("细胞×活跃基因过大，已降级为每簇平均 Δ 的线性传播")
    delta = np.zeros((n_cells if use_cells else k, m), dtype=np.float32)
    for ci in range(k):
        cells = cluster_cells[ci]
        if cells.size == 0:
            continue
        xgene = np.asarray(X[cells][:, [g_global]].todense()).ravel().astype(np.float64)
        if mode_key == "knockout":
            d0 = -xgene
        elif mode_key == "knockdown":
            d0 = -float(strength) * xgene
        else:
            p95 = float(np.percentile(xgene, 95))
            d0 = float(strength) * np.maximum(0.0, p95 - xgene)
        if use_cells:
            delta[cells, g_local] = d0
        else:
            delta[ci, g_local] = float(d0.mean())

    # ---- 沿边传播（每簇各自的 coef，每轮按该基因 std 限幅）----
    rounds = max(0, int(n_prop))
    for _ in range(rounds):
        for ci in range(k):
            block, limit = coef_blocks[ci], lim[ci]
            if block.size == 0:
                continue
            if use_cells:
                cells = cluster_cells[ci]
                if cells.size == 0:
                    continue
                d_tf = delta[np.ix_(cells, tf_local)] if tf_local.size else None
                if d_tf is None:
                    continue
                prop = d_tf.astype(np.float64) @ block
                prop = np.clip(prop, -limit, limit)
                delta[np.ix_(cells, tg_local)] += prop.astype(np.float32)
            else:
                if not tf_local.size:
                    continue
                prop = np.clip(delta[ci, tf_local].astype(np.float64) @ block, -limit, limit)
                delta[ci, tg_local] += prop.astype(np.float32)

    # ---- 汇总：每簇平均 Δ / 平均 |Δ|（活跃基因）→ 全基因 ----
    means_active = np.zeros((k, m), dtype=np.float32)
    abs_means = np.zeros((k, m), dtype=np.float32)
    sizes = np.bincount(cluster_ids, minlength=k).astype(np.float64)
    if use_cells:
        for ci in range(k):
            cells = cluster_cells[ci]
            if cells.size:
                means_active[ci] = delta[cells].mean(axis=0)
                abs_means[ci] = np.abs(delta[cells]).mean(axis=0)
    else:
        means_active = delta
        abs_means = np.abs(delta)
    weights = sizes / max(1.0, sizes.sum())
    delta_gene = np.zeros(n_genes, dtype=np.float32)
    impact_gene = np.zeros(n_genes, dtype=np.float32)
    delta_gene[active] = (weights[:, None] * means_active).sum(axis=0)
    impact_gene[active] = (weights[:, None] * abs_means).sum(axis=0)
    if focus is not None and 0 <= int(focus) < k:
        delta_gene = np.zeros(n_genes, dtype=np.float32)
        impact_gene = np.zeros(n_genes, dtype=np.float32)
        delta_gene[active] = means_active[int(focus)]
        impact_gene[active] = abs_means[int(focus)]

    # ---- 投到 latent：Δ_latent = 每簇平均 Δ[hvg] · latent_loadings ----
    if loadings.shape[0] == n_genes:
        row_gene = np.arange(n_genes, dtype=np.int64)
    elif loadings.shape[0] == hvg_global.size:
        row_gene = hvg_global.astype(np.int64)
    else:
        raise C.err("prerequisite",
                    f"latent_loadings 行数 {loadings.shape[0]} 无法对应基因（{n_genes} 或 HVG {hvg_global.size}）")
    row_pos = np.array([r for r, g in enumerate(row_gene) if int(g) in local_of], dtype=np.int64)
    row_loc = np.array([local_of[int(row_gene[r])] for r in row_pos], dtype=np.int64)
    delta_latent = np.zeros((k, n_pcs), dtype=np.float32)
    if row_pos.size:
        delta_latent = (means_active[:, row_loc] @ loadings[row_pos, :]).astype(np.float32)
    arrows = None
    if emb is not None:
        arrows = (delta_latent @ emb).astype(np.float32)

    # ---- 状态位移：new = centroid + Δ_latent；target = 位移方向最接近的其它质心 ----
    per_cluster, cos_self, delta_norm, target_of = [], np.ones(k), np.zeros(k), np.zeros(k, int)
    for ci in range(k):
        shifted = centroids[ci] + delta_latent[ci]
        shift = _one_minus_cos(centroids[ci], shifted)
        cos_self[ci] = 1.0 - shift
        direction = np.asarray(delta_latent[ci], dtype=np.float64)
        best_j, best_cos = ci, 0.0
        if float(np.linalg.norm(direction)) > _EPS:
            for cj in range(k):
                if cj == ci:
                    continue
                align = 1.0 - _one_minus_cos(direction, centroids[cj] - centroids[ci])
                if align > best_cos:
                    best_j, best_cos = cj, align
        target_of[ci] = int(best_j)
        delta_norm[ci] = float(np.linalg.norm(direction))
        per_cluster.append({
            "cluster": names[ci], "delta_norm": C.fnum(delta_norm[ci]),
            "target_cluster": names[int(target_of[ci])], "cos": C.fnum(best_cos),
            "self_cos": C.fnum(cos_self[ci]),
        })
    if focus is not None and 0 <= int(focus) < k:
        state_shift = float(1.0 - cos_self[int(focus)])
    else:
        state_shift = float(np.sum(weights * (1.0 - cos_self)))
    if focus is not None and 0 <= int(focus) < k:
        source_index = int(focus)
    elif delta_norm.max() > 0:
        source_index = int(np.argmax(delta_norm))
    else:
        source_index = 0
        warnings.append("Δ_latent 全为 0（该基因没有被扰动或网络无出边），状态位移无意义")
    return {
        "gene": resolved, "mode": mode_key, "delta_gene": delta_gene,
        "impact_gene": impact_gene, "delta_by_cluster": means_active,
        "active": active, "delta_latent": delta_latent,
        "arrows": arrows, "per_cluster": per_cluster, "state_shift": state_shift,
        "source_index": source_index, "source_state": names[source_index],
        "target_state": names[int(target_of[source_index])],
        "cluster_weights": weights, "n_active": m, "warnings": warnings,
        "used_cells": bool(use_cells), "n_prop": rounds, "strength": float(strength),
    }


# ---------------------------------------------------------------------------
# 虚拟扰动
# ---------------------------------------------------------------------------

_MODE_CN = {"knockout": "敲除（KO）", "knockdown": "敲低（KD）", "overexpression": "过表达（OE）"}


def run_perturb(bundle, state, grn, params: dict | None = None) -> dict:
    """CellOracle 风格虚拟扰动：初始 Δ → 沿 GRN 传播 → 投到 latent → 状态位移。

    返回 arrays.delta_gene（全基因平均 Δ）、delta_latent（k×n_pcs）、arrows（k×2）、
    affected（≤200 行）、per_cluster、state_shift / target_state / source_state、通路富集。
    """
    t0 = time.time()
    p = _opts(params, {"gene": "", "mode": "knockout", "strength": 1.0,
                       "cluster": "all", "n_prop": 3, "top_n": 30, "clip": 3.0, "seed": 0})
    warnings: list[str] = []
    data = _stage_arrays(state)
    genes = data["genes"]
    grn = grn or {}
    if not (grn.get("tf_names") or grn.get("target_names")):
        raise C.err("prerequisite", "缺少 GRN 结果，无法做虚拟扰动",
                    hint="先运行「调控网络」一步（grn）")
    grn_metrics = grn.get("metrics") or {}
    gene = _resolve_gene(genes, p.get("gene"))
    if gene is None:
        hubs = grn_metrics.get("hub_tfs") or []
        gene = _resolve_gene(genes, (hubs[0] or {}).get("tf") if hubs else "")
        if gene is None:
            raise C.err("bad_request", "没有指定要扰动的基因（params['gene']）",
                        hint="传 gene=某个 TF/基因；或先跑 GRN 让系统自动选第一 hub")
        warnings.append(f"未指定 gene，自动选择 GRN 第一 hub：{gene}")
    strength = float(p.get("strength") if p.get("strength") is not None else 1.0)
    n_prop = max(0, min(int(p.get("n_prop") if p.get("n_prop") is not None else 3), 10))
    clip = float(p.get("clip") or 3.0)
    names = data["cluster_names"]
    cluster_key = str(p.get("cluster") or "all").strip()
    focus = None
    if cluster_key.lower() not in ("all", "", "全部"):
        if cluster_key in names:
            focus = names.index(cluster_key)
        else:
            try:
                index = int(float(cluster_key))
            except (TypeError, ValueError):
                index = -1
            if 0 <= index < data["k"]:
                focus = index
            else:
                raise C.err("bad_request", f"未知 cluster：{cluster_key}",
                            hint=f"可选 all 或 {names}")
    C.progress(f"模拟 {gene} 扰动…", 20)
    sim = _simulate(state, data, grn, gene, mode=p.get("mode"), strength=strength,
                    n_prop=n_prop, clip=clip, per_cell=True, focus=focus)
    warnings.extend(sim["warnings"])
    tf_set = set(grn.get("tf_names") or [])

    # ---- 受影响基因：按全细胞平均 |Δ| 排序（delta 保留方向）----
    top_n = max(1, min(int(p.get("top_n") or 30), 200))
    delta, impact = sim["delta_gene"], sim["impact_gene"]
    order = np.argsort(-impact)[:top_n]
    affected = [{"gene": genes[int(i)], "delta": C.fnum(delta[i]), "impact": C.fnum(impact[i]),
                 "rank": rank + 1, "is_tf": genes[int(i)] in tf_set}
                for rank, i in enumerate(order)]
    n_affected = int((impact > 1e-6).sum())

    # ---- top100 up / top100 down 对内置通路做超几何 ORA + BH ----
    up = [genes[int(i)] for i in np.argsort(-delta)[:100] if delta[int(i)] > 1e-6]
    down = [genes[int(i)] for i in np.argsort(delta)[:100] if delta[int(i)] < -1e-6]
    tests = []
    for direction, hits in (("up", up), ("down", down)):
        for row in _hypergeom_ora(hits, genes, P.PATHWAY_SETS):
            if row["n_hits"] > 0:
                tests.append({**row, "direction": direction})
    if tests:
        for row, qvalue in zip(tests, _bh([row["p"] for row in tests])):
            row["q"] = C.fnum(qvalue)
        tests.sort(key=lambda row: (row["q"] if row["q"] is not None else 1.0, row["p"]))
        tests = tests[:20]
    enrichment = [{"pathway": row["set"], "direction": row["direction"],
                   "n_hits": row["n_hits"], "n_set": row["n_set"],
                   "fold": C.fnum(row["fold"]), "p": C.fnum(row["p"]), "q": row["q"],
                   "genes": ",".join(row["genes"])} for row in tests]
    if not enrichment:
        warnings.append("Δ 没有富集到显著通路（幅度太小，或通路基因不在数据里）")

    # ---- 指标 / 有效性说明 / 图 / 表 / 摘要 ----
    state_shift = float(sim["state_shift"])
    n_edges_used = int(grn_metrics.get("n_edges") or 0)
    confidence = "medium" if (gene in tf_set and state_shift > _EPS) else "low"
    metrics = {
        "n_affected": n_affected,
        "max_abs_delta": C.fnum(float(np.max(np.abs(delta))) if delta.size else 0.0),
        "state_shift": C.fnum(state_shift), "target_state": sim["target_state"],
        "source_state": sim["source_state"], "confidence": confidence,
        "backend": "builtin", "seed": p.get("seed"), "n_prop": n_prop,
        "n_edges_used": n_edges_used, "gene": gene, "mode": sim["mode"],
        "runtime_s": C.fnum(time.time() - t0, 2),
    }
    validity = {
        "grn_source": grn.get("grn_source") or "coexpression_ridge",
        "n_edges_used": n_edges_used, "propagation": n_prop,
        "notes": [
            "这是假说，不是结论：GRN 来自共表达推断，传播只保证数据内部自洽，需 Perturb-seq / CRISPR 实验验证。",
            "初始 Δ：KO=-x、KD=-strength·x、OE=+strength·max(0, p95-x)；沿边迭代传播 n_prop 轮，每轮按该基因 std 限幅。",
            "状态位移 = 1 - cos(旧质心, 新质心)，按各状态细胞数加权；共表达方向性证据弱。",
        ],
    }
    tables = [
        {"id": "perturb_affected", "title": f"{gene} 受影响基因（按平均 |Δ| 排序）",
         "columns": ["gene", "delta", "impact", "rank", "is_tf"], "rows": affected},
        {"id": "perturb_enrichment", "title": "扰动 Δ 通路富集（超几何 ORA + BH）",
         "columns": ["pathway", "direction", "n_hits", "n_set", "fold", "p", "q", "genes"],
         "rows": enrichment},
        {"id": "perturb_per_cluster", "title": "每簇状态位移",
         "columns": ["cluster", "delta_norm", "target_cluster", "cos", "self_cos"],
         "rows": sim["per_cluster"]},
    ]
    figures = [
        {"kind": "perturb_waterfall", "title": f"{gene} 受影响基因瀑布图"},
        {"kind": "perturb_field", "title": f"{gene} 扰动的状态场箭头"},
        {"kind": "perturb_heatmap", "title": f"{gene} 扰动 × 状态 Δ 热图"},
    ]
    mode_cn = _MODE_CN.get(sim["mode"], sim["mode"])
    top_genes = "、".join(row["gene"] for row in affected[:3]) or "（无）"
    summary = (f"{mode_cn} {gene}（strength={strength:g}，传播 {n_prop} 轮）：预测细胞状态从 "
               f"{sim['source_state']} 向 {sim['target_state']} 偏移（state_shift={state_shift:.3f}）；"
               f"受影响基因 {n_affected} 个，top3：{top_genes}")
    if enrichment:
        best = enrichment[0]
        summary += f"；最显著通路：{best['pathway']}（{best['direction']}，q={best['q']}）"
    summary += "。这是基于共表达 GRN 的假说，需实验验证。"
    if focus is None and data["k"] > 1:
        warnings.append("状态位移按各状态细胞数加权；如需单状态解释，用 cluster 参数指定")
    if sim["arrows"] is None:
        warnings.append("缺少 embedding_loadings（n_pcs×2）：无法计算 2D 扰动箭头")
    arrays = {
        "delta_gene": np.asarray(delta, dtype=np.float32),
        "impact_gene": np.asarray(impact, dtype=np.float32),
        "delta_latent": np.asarray(sim["delta_latent"], dtype=np.float32),
        "arrows": None if sim["arrows"] is None else np.asarray(sim["arrows"], dtype=np.float32),
        "delta_gene_matrix": None,
    }
    C.progress("扰动模拟完成", 100)
    return {
        "arrays": arrays, "gene": gene, "mode": sim["mode"], "strength": strength,
        "affected": affected, "per_cluster": sim["per_cluster"],
        "state_shift": C.fnum(state_shift), "target_state": sim["target_state"],
        "source_state": sim["source_state"], "enrichment": enrichment,
        "metrics": metrics, "validity": validity, "tables": tables, "figures": figures,
        "summary": summary, "warnings": warnings,
        "params": {"gene": gene, "mode": sim["mode"], "strength": strength,
                   "cluster": cluster_key, "n_prop": n_prop, "top_n": top_n, "clip": clip},
    }


# ---------------------------------------------------------------------------
# 虚拟筛选
# ---------------------------------------------------------------------------

def _screen_candidates(data, grn, requested, max_genes: int) -> list:
    """候选 TF：用户指定优先；否则内置 TF ∩ 表达基因（按 GRN hub 排序）取前 max_genes 个。"""
    genes, pos = data["genes"], data["gene_pos"]
    grn_tf = set(g for g in (grn.get("tf_names") or []) if g in pos)
    if requested:
        out = []
        for name in requested:
            resolved = _resolve_gene(genes, name)
            if resolved is None:
                raise C.err("bad_request", f"基因 {name} 不在当前数据集的基因列表里",
                            hint="检查拼写，或用 action=preview 查看基因名")
            if resolved not in out:
                out.append(resolved)
        return out[:max(1, int(max_genes))]
    hubs = [str(row.get("tf")) for row in ((grn.get("metrics") or {}).get("hub_tfs") or [])
            if row.get("tf")]
    universe = [g for g in P.tf_universe() if g in pos and g in grn_tf]
    ordered = [g for g in dict.fromkeys(hubs) if g in grn_tf]
    ordered += [g for g in universe if g not in ordered]
    if not ordered:
        ordered = [g for g in P.tf_universe() if g in pos]
    return ordered[:max(1, int(max_genes))]


def run_screen(bundle, state, grn, params: dict | None = None) -> dict:
    """批量虚拟敲除：对候选 TF 逐个轻量模拟，打分 = state_shift × (1 + 状态特异度)。"""
    t0 = time.time()
    p = _opts(params, {"genes": [], "mode": "knockout", "top_n": 20, "specificity": True,
                       "max_genes": 40, "strength": 1.0, "n_prop": 3, "clip": 3.0, "seed": 0})
    warnings: list[str] = []
    data = _stage_arrays(state)
    grn = grn or {}
    if not (grn.get("tf_names") or grn.get("target_names")):
        raise C.err("prerequisite", "缺少 GRN 结果，无法做虚拟筛选",
                    hint="先运行「调控网络」一步（grn）")
    requested = p.get("genes") or []
    if isinstance(requested, str):
        requested = [x for x in requested.replace(";", ",").replace(" ", ",").split(",") if x]
    candidates = _screen_candidates(data, grn, requested, p.get("max_genes") or 40)
    if not candidates:
        raise C.err("prerequisite", "没有可筛选的候选基因",
                    hint="用 params['genes'] 指定 TF 列表，或先跑 GRN 得到候选 TF")
    k, names = data["k"], data["cluster_names"]
    specificity_on = bool(p.get("specificity")) and k > 1
    n_prop = max(0, min(int(p.get("n_prop") if p.get("n_prop") is not None else 3), 10))
    strength = float(p.get("strength") if p.get("strength") is not None else 1.0)
    C.progress(f"批量模拟 {len(candidates)} 个 TF…", 10)
    rows = []
    first_error = None
    for position, gene in enumerate(candidates):
        try:
            sim = _simulate(state, data, grn, gene, mode=p.get("mode"), strength=strength,
                            n_prop=n_prop, clip=float(p.get("clip") or 3.0), per_cell=False)
        except C.EngineError as error:
            warnings.append(f"{gene} 模拟跳过：{error.message}")
            first_error = first_error or error
            continue
        norms = np.array([row["delta_norm"] or 0.0 for row in sim["per_cluster"]],
                         dtype=np.float64)
        specificity = 0.0
        if specificity_on and norms.sum() > _EPS:
            prob = norms / norms.sum()
            entropy = -float(np.sum(prob * np.log(np.maximum(prob, 1e-12))))
            specificity = float(max(0.0, 1.0 - entropy / math.log(k)))
        state_shift = float(sim["state_shift"])
        score = state_shift * (1.0 + (specificity if specificity_on else 0.0))
        rows.append({"gene": sim["gene"], "score": score, "state_shift": state_shift,
                     "specificity": specificity, "source_state": sim["source_state"],
                     "target_state": sim["target_state"],
                     "delta_norm": float(np.linalg.norm(sim["delta_latent"]))})
        C.progress(f"模拟 {position + 1}/{len(candidates)}：{gene}", 10 + 80.0 * (position + 1) / len(candidates))
    if not rows:
        if isinstance(first_error, C.EngineError):
            raise first_error
        raise C.err("internal", "所有候选基因都模拟失败，无法排名",
                    hint="检查 GRN 的 tf_names 与表达矩阵基因是否一致")
    rows.sort(key=lambda row: -row["score"])
    top_n = max(1, min(int(p.get("top_n") or 20), len(rows), 200))
    ranked = [{"gene": row["gene"], "score": C.fnum(row["score"]),
               "state_shift": C.fnum(row["state_shift"]),
               "specificity": C.fnum(row["specificity"]),
               "target_state": row["target_state"],
               "why": (f"扰动后状态 {row['source_state']}→{row['target_state']}"
                       f"（位移 {row['state_shift']:.3f}，状态特异度 {row['specificity']:.2f}；"
                       f"score = 位移 ×(1+特异度) = {row['score']:.3f}）")}
              for row in rows[:top_n]]
    mode_key = str(p.get("mode") or "knockout").lower()
    mode_cn = _MODE_CN.get(mode_key, {"ko": "敲除（KO）", "kd": "敲低（KD）", "oe": "过表达（OE）"}.get(mode_key, mode_key))
    metrics = {"n_screened": len(rows), "n_requested": len(candidates),
               "top1": ranked[0]["gene"], "top1_score": ranked[0]["score"],
               "mode": mode_key, "specificity": specificity_on,
               "runtime_s": C.fnum(time.time() - t0, 2)}
    if not specificity_on and k == 1:
        warnings.append("只有 1 个状态，特异度无效（score = state_shift）")
    elif not specificity_on:
        warnings.append("specificity=False：score 只用状态位移，不奖励状态特异性")
    if len(rows) < len(candidates):
        warnings.append(f"{len(candidates) - len(rows)} 个候选模拟失败被跳过")
    if metrics["top1_score"] <= _EPS:
        warnings.append("所有候选的 state_shift 都接近 0：GRN 对这些 TF 没有出边，筛选不可信")
    summary = (f"对 {len(rows)} 个 TF 做虚拟{mode_cn}筛选：top1 是 {ranked[0]['gene']}"
               f"（score={ranked[0]['score']:.3f}，状态位移 {ranked[0]['state_shift']:.3f}，"
               f"特异度 {ranked[0]['specificity']:.2f}）")
    if ranked[0]["target_state"] != rows[0]["source_state"]:
        summary += f"，预测细胞状态向 {ranked[0]['target_state']} 偏移"
    summary += "。排序是假说，需要实验验证。"
    tables = [{"id": "screen_ranked", "title": f"虚拟{mode_cn}筛选排名",
               "columns": ["gene", "score", "state_shift", "specificity", "target_state", "why"],
               "rows": ranked}]
    figures = [{"kind": "screen_rank", "title": f"虚拟{mode_cn}筛选：top {len(ranked)} TF"}]
    C.progress("筛选完成", 100)
    return {
        "arrays": {"scores": np.asarray([row["score"] for row in rows], dtype=np.float32),
                   "tf_names": [row["gene"] for row in rows]},
        "ranked": ranked, "metrics": metrics, "tables": tables, "figures": figures,
        "summary": summary, "warnings": warnings,
        "params": {"genes": candidates, "mode": mode_key, "top_n": top_n,
                   "specificity": specificity_on, "max_genes": int(p.get("max_genes") or 40)},
    }


# ---------------------------------------------------------------------------
# GRN 准确性校验（只有 demo / 挂了真值时可用）
# ---------------------------------------------------------------------------

def _safe_spearman(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.size < 3 or float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
        return None
    value = float(stats.spearmanr(a, b).statistic)
    return value if math.isfinite(value) else None


def _safe_pearson(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.size < 3 or float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
        return None
    value = float(np.corrcoef(a, b)[0, 1])
    return value if math.isfinite(value) else None


def _truth_edges(truth_grn):
    """真值边 → [(tf, target, sign)]，兼容 DataFrame 与 list[dict]。"""
    rows = []
    if isinstance(truth_grn, pd.DataFrame):
        frame = truth_grn
        tf_col = "tf" if "tf" in frame.columns else frame.columns[0]
        tg_col = "target" if "target" in frame.columns else frame.columns[1]
        sign_col = "sign" if "sign" in frame.columns else None
        for tf, target, sign in zip(frame[tf_col].astype(str), frame[tg_col].astype(str),
                                    frame[sign_col].tolist() if sign_col else [None] * len(frame)):
            rows.append((tf, target, sign))
    elif isinstance(truth_grn, list):
        for item in truth_grn:
            if isinstance(item, dict) and item.get("tf") and item.get("target"):
                rows.append((str(item["tf"]), str(item["target"]), item.get("sign")))
    return rows


def run_validate(bundle, state, grn, params: dict | None = None) -> dict:
    """用 demo 真值校验 GRN：边存在性 AUROC/AUPRC/top100 精准率 + 留出扰动 Δ 的方向相关。"""
    t0 = time.time()
    p = _opts(params, {"truth": "auto", "min_edges": 20, "mode": "knockout",
                       "strength": 1.0, "n_prop": 3, "clip": 3.0, "seed": 0})
    truth = getattr(bundle, "truth", None) or {}
    if not isinstance(truth, dict) or truth.get("grn") is None:
        raise C.err("prerequisite", "当前数据集没有 GRN 真值，无法做准确性校验",
                    hint="用 demo 数据，或改用 n_boot>0 的 Bootstrap 稳定性")
    warnings: list[str] = []
    data = _stage_arrays(state)
    pos = data["gene_pos"]
    grn = grn or {}
    grn_tf_all = list(grn.get("tf_names") or [])
    grn_tg_all = list(grn.get("target_names") or [])
    tf_names = [g for g in grn_tf_all if g in pos]
    tg_names = [g for g in grn_tg_all if g in pos]
    if not tf_names or not tg_names:
        raise C.err("prerequisite", "GRN 的 TF / target 与当前表达矩阵对不上，无法校验")
    n_tf, n_tg = len(tf_names), len(tg_names)
    rows_keep = [i for i, g in enumerate(grn_tf_all) if g in pos]
    cols_keep = [j for j, g in enumerate(grn_tg_all) if g in pos]
    score = np.zeros((n_tf, n_tg), dtype=np.float32)
    sign = np.zeros((n_tf, n_tg), dtype=np.int8)
    for ci in range(data["k"]):
        block = _coef_for_cluster(grn, ci, n_tf=len(grn_tf_all), n_tg=len(grn_tg_all))
        sub = np.asarray(block[np.ix_(rows_keep, cols_keep)], dtype=np.float32)
        better = np.abs(sub) > np.abs(score)
        score = np.where(better, np.abs(sub), score).astype(np.float32)
        sign = np.where(better, np.sign(sub).astype(np.int8), sign).astype(np.int8)

    # ---- 边存在性：推断边 vs 真值边 ----
    tf_pos = {g: i for i, g in enumerate(tf_names)}
    tg_pos = {g: j for j, g in enumerate(tg_names)}
    labels = np.zeros((n_tf, n_tg), dtype=np.int8)
    truth_sign = np.zeros((n_tf, n_tg), dtype=np.int8)
    n_truth_in_universe = 0
    for tf, target, edge_sign in _truth_edges(truth.get("grn")):
        if tf in tf_pos and target in tg_pos:
            labels[tf_pos[tf], tg_pos[target]] = 1
            n_truth_in_universe += 1
            try:
                truth_sign[tf_pos[tf], tg_pos[target]] = 1 if float(edge_sign) >= 0 else -1
            except (TypeError, ValueError):
                pass
    flat_score, flat_label = score.ravel(), labels.ravel()
    # 评估空间：优先限定在「共表达/先验筛选出的候选边」内（本方法只在这些边上拟合系数），
    # 全空间 AUROC 会因大量 0 分负样本被 tie 拉回 0.5，属于评估口径问题而非模型问题。
    raw_mask = (grn.get("arrays") or {}).get("candidate_mask")
    eval_mask = None
    if raw_mask is not None:
        try:
            mask = np.asarray(raw_mask, dtype=bool)
            if mask.shape == (len(grn_tf_all), len(grn_tg_all)):
                eval_mask = mask[np.ix_(rows_keep, cols_keep)].ravel()
        except Exception:  # noqa: BLE001
            eval_mask = None
    auroc = auprc = None
    fpr, tpr = np.zeros(0), np.zeros(0)
    eval_score, eval_label = flat_score, flat_label
    if eval_mask is not None and eval_mask.sum() > 0 and 0 < int((eval_label & eval_mask).sum()) < int(eval_mask.sum()):
        eval_score, eval_label = flat_score[eval_mask], flat_label[eval_mask]
        warnings.append(f"校验口径：限定在共表达候选边空间（{int(eval_mask.sum()):,} 对，阳性 "
                        f"{int(eval_label.sum())} 条）内计算 AUROC/AUPRC")
    if 0 < int(eval_label.sum()) < eval_label.size:
        try:
            from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve
            auroc = float(roc_auc_score(eval_label, eval_score))
            auprc = float(average_precision_score(eval_label, eval_score))
            fpr, tpr, _ = roc_curve(eval_label, eval_score)
        except Exception as error:  # noqa: BLE001
            warnings.append(f"sklearn 评估失败：{error}")
    else:
        warnings.append("真值边在候选空间里全是阳性或全无：AUROC 无定义")
    top_k = min(100, eval_score.size)
    order_top = np.argsort(-eval_score)[:top_k]
    top_hits = float(eval_label[order_top].mean()) if top_k else 0.0
    pos_mask = flat_label.astype(bool) & (flat_score > 0)
    sign_agreement = None
    if pos_mask.sum():
        inferred_sign = np.sign(flat_score[pos_mask]).astype(np.int8)
        true_sign = truth_sign.ravel()[pos_mask]
        usable = true_sign != 0
        if usable.any():
            sign_agreement = float((inferred_sign[usable] == true_sign[usable]).mean())

    # ---- 留出扰动 Δ：同一 _simulate 模拟 vs 真值 ----
    truth_pert = truth.get("perturb") or {}
    if not isinstance(truth_pert, dict):
        truth_pert = {}
        warnings.append("truth['perturb'] 不是 {tf: Δ} 结构，跳过扰动方向校验")
    grn_tf_set = set(grn_tf_all)
    perturb_rows, spearman_top, pearson_top, spearman_all, jaccards, compare = [], [], [], [], [], []
    for tf, delta in truth_pert.items():
        tf = str(tf)
        true_delta = np.asarray(delta, dtype=np.float64).ravel()
        if true_delta.size != data["n_genes"]:
            warnings.append(f"真值扰动 {tf} 维度 {true_delta.size} ≠ 基因数 {data['n_genes']}，跳过")
            continue
        try:
            sim = _simulate(state, data, grn, tf, mode=p.get("mode"),
                            strength=float(p.get("strength") or 1.0),
                            n_prop=int(p.get("n_prop") or 3), clip=float(p.get("clip") or 3.0))
        except C.EngineError as error:
            warnings.append(f"真值扰动 {tf} 模拟失败：{error.message}")
            continue
        sim_delta = np.asarray(sim["delta_gene"], dtype=np.float64)
        if tf not in grn_tf_set:
            warnings.append(f"留出 TF {tf} 不在推断网络的 TF 列表里：没有下游传播，相关性会偏低")
        rho_all = _safe_spearman(true_delta, sim_delta)
        pear_all = _safe_pearson(true_delta, sim_delta)
        mask = np.zeros(true_delta.size, dtype=bool)
        mask[np.argsort(-np.abs(true_delta))[:200]] = True
        mask[np.argsort(-np.abs(sim_delta))[:200]] = True
        mask &= (np.abs(true_delta) > 1e-12) | (np.abs(sim_delta) > 1e-12)
        rho_top = _safe_spearman(true_delta[mask], sim_delta[mask])
        pear_top = _safe_pearson(true_delta[mask], sim_delta[mask])
        top_true = set(np.argsort(-np.abs(true_delta))[:20].tolist())
        top_sim = set(np.argsort(-np.abs(sim_delta))[:20].tolist())
        jaccard = len(top_true & top_sim) / max(1, len(top_true | top_sim))
        perturb_rows.append({"tf": tf, "spearman_top200": C.fnum(rho_top),
                             "pearson_top200": C.fnum(pear_top),
                             "spearman_all": C.fnum(rho_all),
                             "pearson_all": C.fnum(pear_all),
                             "jaccard_top20": C.fnum(jaccard),
                             "in_grn": bool(tf in grn_tf_set)})
        if rho_top is not None:
            spearman_top.append(rho_top)
        if pear_top is not None:
            pearson_top.append(pear_top)
        if rho_all is not None:
            spearman_all.append(rho_all)
        jaccards.append(jaccard)
        if len(compare) < 5:
            compare.append(sim_delta.astype(np.float32))

    def _mean(values):
        return C.fnum(float(np.mean(values))) if values else None

    metrics = {
        "grn_auroc": C.fnum(auroc), "grn_auprc": C.fnum(auprc),
        "grn_top100_precision": C.fnum(top_hits),
        "delta_spearman_mean": _mean(spearman_top),
        "delta_pearson_mean": _mean(pearson_top),
        "jaccard_top20_mean": _mean(jaccards),
        "n_heldout": len(perturb_rows),
        "delta_spearman_all_mean": _mean(spearman_all),
        "n_truth_edges": int(flat_label.sum()), "n_pairs": int(flat_label.size),
        "runtime_s": C.fnum(time.time() - t0, 2), "sign_agreement": C.fnum(sign_agreement),
    }
    passed = bool(auroc is not None and auroc >= 0.65
                  and metrics["delta_spearman_mean"] is not None
                  and metrics["delta_spearman_mean"] >= 0.3)
    if passed:
        why = (f"边存在性 AUROC={auroc:.2f}（≥0.65），留出扰动 Δ 的方向相关 Spearman="
               f"{metrics['delta_spearman_mean']:.2f}（≥0.3）：推断网络与真值结构一致。")
    else:
        reasons = []
        if auroc is None or auroc < 0.65:
            reasons.append(f"边 AUROC={C.fnum(auroc) if auroc is not None else '无定义'}（需 ≥0.65）")
        if metrics["delta_spearman_mean"] is None or metrics["delta_spearman_mean"] < 0.3:
            reasons.append(f"扰动 Spearman={metrics['delta_spearman_mean']}（需 ≥0.3）")
        why = "未通过：" + "；".join(reasons) + "。可能是候选 TF/target 太少、数据状态结构弱，或真值留出 TF 不在网络里。"
        if n_truth_in_universe < int(p.get("min_edges") or 20):
            why += f" 真值边仅 {n_truth_in_universe} 条落在候选空间内（<min_edges），评估不稳定。"
    summary = (f"边存在性：AUROC={metrics['grn_auroc']}、AUPRC={metrics['grn_auprc']}、"
               f"top100 精准率={metrics['grn_top100_precision']}（真值边 {n_truth_in_universe} 条 / "
               f"{flat_label.size} 对候选）；留出扰动 {len(perturb_rows)} 个：Δ 的 top200 Spearman 均值="
               f"{metrics['delta_spearman_mean']}、Pearson 均值={metrics['delta_pearson_mean']}、"
               f"top20 Jaccard={metrics['jaccard_top20_mean']}。判定：{'通过' if passed else '未通过'}。")
    if not passed:
        warnings.append(why)
    if metrics["n_heldout"] == 0:
        warnings.append("truth['perturb'] 为空：只评估了边结构，没有扰动方向校验")
    tables = [
        {"id": "validate_grn", "title": "GRN 边存在性校验",
         "columns": ["metric", "value", "detail"], "rows": [
             {"metric": "AUROC", "value": metrics["grn_auroc"], "detail": "0.5=随机猜"},
             {"metric": "AUPRC", "value": metrics["grn_auprc"], "detail": "正例稀少时更严格"},
             {"metric": "top100_precision", "value": metrics["grn_top100_precision"],
              "detail": "推断 top100 边里真值边比例"},
             {"metric": "sign_agreement", "value": metrics["sign_agreement"],
              "detail": "推断边与真值边符号一致率"},
             {"metric": "n_truth_edges", "value": n_truth_in_universe, "detail": "落在候选空间的真值边"},
             {"metric": "n_pairs", "value": int(flat_label.size), "detail": "候选空间 TF×target 对数"},
         ]},
        {"id": "validate_perturb", "title": "留出扰动 Δ 方向校验（模拟 vs 真值）",
         "columns": ["tf", "spearman_top200", "pearson_top200", "spearman_all", "pearson_all",
                     "jaccard_top20", "in_grn"],
         "rows": perturb_rows},
    ]
    figures = [
        {"kind": "validate_roc", "title": "GRN 边存在性 ROC"},
        {"kind": "compare_bar", "title": "边 AUROC vs 扰动 Spearman",
         "params": {"items": ["grn_auroc", "delta_spearman_mean"]}},
    ]
    arrays = {"edge_score": score, "edge_label": labels.astype(np.int8),
              "roc": np.stack([fpr, tpr], axis=1).astype(np.float32) if fpr.size else np.zeros((0, 2), np.float32),
              "perturb_spearman": np.asarray(spearman_top, dtype=np.float32)}
    if compare and len(compare) * data["n_genes"] <= 5_000_000:
        arrays["perturb_sim"] = np.stack(compare, axis=0)
    C.progress("校验完成", 100)
    return {
        "arrays": arrays, "metrics": metrics,
        "verdict": {"passed": passed, "why": why},
        "tables": tables, "figures": figures, "summary": summary, "warnings": warnings,
        "params": {"truth": "auto", "mode": str(p.get("mode") or "knockout"),
                   "n_prop": int(p.get("n_prop") or 3), "min_edges": int(p.get("min_edges") or 20)},
    }
