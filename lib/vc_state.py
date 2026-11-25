#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — 状态表征模块（qc / state / annotate / transition）。

  run_qc         质控：细胞/基因过滤掩码、QC 指标、质控分布图；
  run_state      状态表征：库归一化+log1p → HVG(seurat) → PCA → kNN → 聚类 → 2D 嵌入 → marker；
  run_annotate   用 vc_priors.STATE_MARKERS 对每簇打分（z-score / UCell 秩和），top1/top2/margin；
  run_transition kNN 簇间转移矩阵 + Sinkhorn 平滑 + bootstrap 稳定性。

只用 numpy/scipy/sklearn；scanpy 仅在 _try_scanpy() 探测，缺省 backend='builtin' 并写 warnings；
不 import vc_io；bundle 是鸭子对象（.X/.genes/.cells/.obs/.truth/.meta）；大数组放 'arrays'。
"""
from __future__ import annotations

import time
import numpy as np
import pandas as pd
import scipy.sparse as sp

try:  # 作为包导入（lib 是包）时走相对导入
    from . import vc_common as common
    from . import vc_priors as priors
except ImportError:  # 直接以 lib/ 为 sys.path 时（vc_engine / 自测脚本）
    import vc_common as common  # type: ignore
    import vc_priors as priors  # type: ignore

__all__ = ["run_qc", "run_state", "run_annotate", "run_transition"]
err, fnum, progress = common.err, common.fnum, common.progress

EPS = 1e-9

def _as_float_csr(matrix, dtype=np.float32):
    """任意稠密/稀疏矩阵 → 干净的 csr（float32，NaN/Inf 归零）。"""
    if sp.issparse(matrix):
        out = matrix.tocsr().astype(dtype, copy=False)
    else:
        out = sp.csr_matrix(np.asarray(matrix, dtype=dtype))
    if out.nnz:
        out.data = np.nan_to_num(out.data, nan=0.0, posinf=0.0, neginf=0.0).astype(dtype)
    return out

def _log_normalize(matrix, target_sum: float = 1e4):
    """库归一化到 target_sum（每细胞）后 log1p；返回 csr float32。"""
    X = _as_float_csr(matrix, np.float32)
    totals = np.asarray(X.sum(axis=1)).ravel().astype(np.float64)
    scale = np.zeros_like(totals)
    np.divide(target_sum, totals, out=scale, where=totals > 0)
    scale = np.clip(scale, 0.0, 1e6).astype(np.float32)
    normalized = X.multiply(scale[:, None]).tocsr().astype(np.float32)
    if normalized.nnz:
        normalized.data = np.log1p(normalized.data)
    return normalized

def _mask_from(value, n: int):
    """params 里的布尔掩码 list → np.bool_ 数组；缺省/长度不符返回 None。"""
    if value is None:
        return None
    try:
        arr = np.asarray(value)
    except Exception:  # noqa: BLE001
        return None
    if arr.size != n:
        return None
    if arr.dtype == np.bool_:
        return arr.astype(bool, copy=False)
    if arr.dtype.kind in "iuf":
        return arr.astype(np.float64).astype(bool)
    truthy = ("1", "true", "yes", "y", "t", "keep", "pass")
    return np.asarray([str(v).strip().lower() in truthy for v in arr.ravel()], dtype=bool)

def _mito_gene_mask(genes):
    return np.asarray([str(g).strip().upper().startswith("MT-") for g in genes], dtype=bool)

def _mito_pct(matrix, genes):
    """线粒体 UMIs 占比（%）。无 MT- 基因时返回全 0 + flag。"""
    X = _as_float_csr(matrix, np.float32)
    flag = _mito_gene_mask(genes)
    total = np.asarray(X.sum(axis=1)).ravel().astype(np.float64)
    pct = np.zeros(total.size, dtype=np.float32)
    if flag.any() and total.size:
        mito = np.asarray(X[:, np.flatnonzero(flag)].sum(axis=1)).ravel().astype(np.float64)
        np.divide(mito * 100.0, total, out=pct, where=total > 0)
    return pct.astype(np.float32), flag

def _hvg_seurat(X_log, n_hvg: int, n_bins: int = 20):
    """Seurat 风格 HVG：log 均值等频分箱 → 箱内稳健 z(离散度) → clip ±10 → topN。"""
    n_cells, n_genes = X_log.shape
    if n_genes <= 2:
        return np.arange(n_genes, dtype=np.int64)
    mean = np.asarray(X_log.mean(axis=0)).ravel().astype(np.float64)
    mean_sq = np.asarray(X_log.multiply(X_log).mean(axis=0)).ravel().astype(np.float64)
    var = np.maximum(mean_sq - mean * mean, 0.0)
    disp = var / np.maximum(mean, 1e-12)
    n_bins = int(max(2, min(int(n_bins), max(2, n_genes // 5))))
    order = np.argsort(mean, kind="stable")
    ranks = np.empty(n_genes, dtype=np.int64)
    ranks[order] = np.arange(n_genes)
    bins = np.minimum((ranks * n_bins) // n_genes, n_bins - 1)
    z = np.zeros(n_genes, dtype=np.float64)
    for b in range(n_bins):
        mask = bins == b
        if int(mask.sum()) < 3:
            continue
        vals = disp[mask]
        med = float(np.median(vals))
        mad = float(np.median(np.abs(vals - med)))
        if mad < 1e-12:
            mad = float(np.std(vals)) or 1.0
        z[mask] = (vals - med) / (1.4826 * mad)
    z = np.clip(z, -10.0, 10.0)
    n_take = int(max(2, min(int(n_hvg), n_genes - 1)))
    return np.sort(np.argsort(-z, kind="stable")[:n_take].astype(np.int64))

def _zscore_dense(matrix, clip: float = 10.0):
    """按基因（列）z-score 并 clip ±clip；返回 (scaled, mean, std)。

    mean/std 必须随 state 一起保存：PCA 是在 **z-score 后的 HVG** 上拟合的，
    下游 grn/response 在 log 空间算出的 Δ 只有除以同一 std 才能投到隐空间。
    """
    arr = np.asarray(matrix, dtype=np.float32)
    mean = arr.mean(axis=0, keepdims=True)
    std = arr.std(axis=0, keepdims=True)
    std = np.where(std < 1e-8, 1.0, std)
    scaled = np.clip((arr - mean) / std, -clip, clip).astype(np.float32)
    return scaled, mean.ravel().astype(np.float32), std.ravel().astype(np.float32)

def _knn(latent, n_neighbors: int):
    """kNN 图：返回 (idx, dist)，自身邻居已剔除。"""
    from sklearn.neighbors import NearestNeighbors

    n = int(latent.shape[0])
    k = int(max(1, min(int(n_neighbors), n - 1)))
    model = NearestNeighbors(n_neighbors=k + 1, algorithm="auto", metric="euclidean")
    model.fit(latent)
    dist, idx = model.kneighbors(latent)
    return idx[:, 1:].astype(np.int32), np.maximum(dist[:, 1:], 0.0).astype(np.float32)

def _kmeans_smooth(clusters, knn_idx, rounds: int = 2):
    """kNN 多数投票平滑（含自身一票），2 轮；返回连续标签 (labels, k)。"""
    labels = np.asarray(clusters, dtype=np.int32)
    n = labels.size
    neigh = np.asarray(knn_idx, dtype=np.int64)
    rows = np.arange(n)
    for _ in range(int(max(0, rounds))):
        n_lab = int(labels.max()) + 1 if n else 1
        counts = np.zeros((n, max(n_lab, 1)), dtype=np.int32)
        for j in range(neigh.shape[1]):
            counts[rows, labels[neigh[:, j]]] += 1
        counts[rows, labels] += 1
        new = np.argmax(counts, axis=1).astype(np.int32)
        if np.array_equal(new, labels):
            break
        labels = new
    _, inv = np.unique(labels, return_inverse=True)
    inv = inv.astype(np.int32)
    return inv, int(inv.max()) + 1

def _silhouette_safe(latent, labels, seed: int = 0, max_n: int = 3000):
    """轮廓系数；细胞 > max_n 时固定 seed 子采样。失败返回 0.0。"""
    from sklearn.metrics import silhouette_score

    labels = np.asarray(labels, dtype=np.int32)
    n = labels.size
    if np.unique(labels).size < 2 or np.unique(labels).size >= n:
        return 0.0
    try:
        if n > max_n:
            rng = np.random.default_rng(int(seed))
            pick = np.sort(rng.choice(n, size=int(max_n), replace=False))
            sub_labels = labels[pick]
            if np.unique(sub_labels).size < 2:
                return 0.0
            return float(silhouette_score(np.asarray(latent)[pick], sub_labels))
        return float(silhouette_score(np.asarray(latent), labels))
    except Exception:  # noqa: BLE001
        return 0.0

def _try_scanpy():
    """探测 scanpy；没有就返回 None（builtin 后端始终可跑）。"""
    try:
        import scanpy as sc  # type: ignore

        return sc
    except Exception:  # noqa: BLE001
        return None


def run_qc(bundle, params: dict) -> dict:
    """质控：细胞/基因过滤掩码 + QC 指标 + 质控分布图（CONTRACT §4 step=qc）。

    返回 arrays={"cell_mask":bool[],"gene_mask":bool[]}，其余细胞/基因下游步骤通过
    params 里的 cell_mask/gene_mask 传入。筛选规则：
      * 细胞：n_genes >= min_genes，max_genes>0 时 n_genes <= max_genes，
        有 MT- 基因时 mito_pct <= max_mito；
      * 基因：在 >= min_cells 个细胞中检出。
    极端情况（全被滤掉 / 无 MT- 基因）自动降级并写 warnings。
    """
    t0 = time.time()
    p = dict(params or {})
    min_genes = max(0, int(p.get("min_genes", 200) or 0))
    max_genes = max(0, int(p.get("max_genes", 0) or 0))
    max_mito = float(p.get("max_mito", 20) if p.get("max_mito") is not None else 20.0)
    min_cells = max(1, int(p.get("min_cells", 3) or 1))
    warnings: list[str] = []

    X = _as_float_csr(getattr(bundle, "X", None))
    if X.shape[0] == 0 or X.shape[1] == 0:
        raise err("empty_input", "表达矩阵为空（0 细胞或 0 基因），无法做质控")
    X = X.copy()
    X.eliminate_zeros()
    n_cells, n_genes = X.shape

    genes = [str(g) for g in (getattr(bundle, "genes", None) or [])]
    if len(genes) != n_genes:
        if genes:
            warnings.append(f"基因名数量（{len(genes)}）与矩阵列数（{n_genes}）不一致，已按位置补全")
        genes = genes[:n_genes] + [f"gene_{i}" for i in range(len(genes), n_genes)]
    if max_genes and max_genes < min_genes:
        warnings.append(f"max_genes={max_genes} < min_genes={min_genes}，已忽略上限")
        max_genes = 0

    n_genes_cell = np.asarray(X.getnnz(axis=1)).ravel().astype(np.int32)
    n_counts = np.asarray(X.sum(axis=1)).ravel().astype(np.float64)
    mito_pct, mito_flag = _mito_pct(X, genes)
    n_mito = int(mito_flag.sum())
    if n_mito == 0:
        warnings.append("未找到 MT- 开头的线粒体基因，线粒体占比全部记 0，mito 过滤已跳过")
        mito_pct = np.zeros(n_cells, dtype=np.float32)

    cell_mask = n_genes_cell >= min_genes
    if max_genes:
        cell_mask &= n_genes_cell <= max_genes
    if n_mito:
        cell_mask &= mito_pct <= max_mito
    n_cells_keep = int(cell_mask.sum())
    # 严格阈值：不足 3 个细胞时**不再**静默回退成全 True（旧 bug：下游拿到假 mask），
    # 只把这次质控标成 preview_only，mask 保持真实筛选结果，让 state 明确拒绝。
    preview_only = bool(n_cells_keep < 3)
    fallback_cell = False
    if preview_only:
        warnings.append(
            f"按当前阈值仅剩 {n_cells_keep}/{n_cells} 个细胞（<3）：本次质控结果仅预览"
            f"（preview_only=True），cell_mask 保持严格筛选、不回退为全 True；"
            f"请放宽 min_genes/max_mito 等阈值后重跑质控，下游「状态表征」会因有效细胞不足而拒绝运行"
        )

    gene_counts = np.asarray(X.getnnz(axis=0)).ravel().astype(np.int32)
    gene_mask = gene_counts >= min_cells
    fallback_gene = False
    if int(gene_mask.sum()) < 2:
        warnings.append(f"至少 {min_cells} 个细胞检出的基因不足 2 个，已回退为全部基因通过")
        gene_mask = np.ones(n_genes, dtype=bool)
        fallback_gene = True

    pct_kept = float(100.0 * cell_mask.sum() / max(1, n_cells))
    kept_cells = int(cell_mask.sum())
    kept_genes = int(gene_mask.sum())
    if pct_kept < 50.0 and not fallback_cell and not preview_only:
        warnings.append(f"质控滤掉了 {100.0 - pct_kept:.1f}% 的细胞，阈值可能过严，请核对 min_genes/max_mito")
    if n_mito:
        med_mito = float(np.median(mito_pct[cell_mask])) if kept_cells else 0.0
        if med_mito > 15.0:
            warnings.append(f"保留细胞中线粒体占比中位数 {med_mito:.1f}%，提示样本可能降解")
    else:
        med_mito = 0.0

    metrics = {
        "n_cells_before": int(n_cells),
        "n_cells_after": kept_cells,
        "n_genes_before": int(n_genes),
        "n_genes_after": kept_genes,
        "n_cells_removed": int(n_cells - kept_cells),
        "n_genes_removed": int(n_genes - kept_genes),
        "pct_cells_kept": fnum(pct_kept, 2),
        "median_n_genes": fnum(np.median(n_genes_cell[cell_mask]) if kept_cells else 0.0, 2),
        "median_n_counts": fnum(np.median(n_counts[cell_mask]) if kept_cells else 0.0, 2),
        "median_mito_pct": fnum(med_mito, 3),
        "n_mito_genes": n_mito,
        "min_genes": min_genes,
        "max_genes": max_genes,
        "max_mito": fnum(max_mito, 2),
        "min_cells": min_cells,
        "fallback": bool(fallback_gene),
        "fallback_cell": False,
        "fallback_gene": bool(fallback_gene),
        "preview_only": bool(preview_only),
        "runtime_s": fnum(time.time() - t0, 3),
    }

    try:
        cells = [str(c) for c in (getattr(bundle, "cells", None) or [])]
        if len(cells) != n_cells:
            cells = [f"cell_{i}" for i in range(n_cells)]
        qc_rows = pd.DataFrame({
            "cell": cells,
            "n_genes": n_genes_cell.astype(np.int64),
            "n_counts": np.round(n_counts, 2),
            "mito_pct": np.round(mito_pct, 3),
            "kept": cell_mask.astype(int),
        })
    except Exception:  # noqa: BLE001
        qc_rows = pd.DataFrame({
            "n_genes": n_genes_cell.astype(np.int64),
            "n_counts": np.round(n_counts, 2),
            "mito_pct": np.round(mito_pct, 3),
            "kept": cell_mask.astype(int),
        })
    genes_rows = pd.DataFrame({
        "gene": genes,
        "n_cells_detected": gene_counts.astype(np.int64),
        "kept": gene_mask.astype(int),
    })

    if n_mito:
        mito_note = f"线粒体中位 {med_mito:.1f}%"
    else:
        mito_note = "无 MT- 基因（跳过线粒体过滤）"
    summary = (
        f"质控：{n_cells} → {kept_cells} 个细胞（保留 {pct_kept:.1f}%），"
        f"{n_genes} → {kept_genes} 个基因；每细胞中位 {metrics['median_n_genes']:.0f} 个基因 / "
        f"{metrics['median_n_counts']:.0f} UMIs，{mito_note}；去除 {n_cells - kept_cells} 个细胞。"
    )
    if preview_only:
        summary += "【preview_only】有效细胞 <3，本次仅为预览，下游状态表征会拒绝运行；请放宽阈值重跑。"

    return {
        "arrays": {
            "cell_mask": np.asarray(cell_mask, dtype=bool),
            "gene_mask": np.asarray(gene_mask, dtype=bool),
        },
        "obs_columns": {
            "n_genes": [int(v) for v in n_genes_cell],
            "n_counts": [fnum(v, 3) for v in n_counts],
            "mito_pct": [fnum(v, 4) for v in mito_pct],
        },
        "metrics": metrics,
        "tables": [
            {"id": "qc_metrics", "title": "质控指标", "rows": qc_rows},
            {"id": "qc_genes", "title": "基因检出", "rows": genes_rows},
        ],
        "figures": [{"kind": "qc_violin", "title": "质控分布"}],
        "summary": summary,
        "warnings": warnings,
    }


def _tsne(latent, seed: int, n_cells: int):
    """sklearn t-SNE（init='pca'，固定 seed）；perplexity 随细胞数自适应。"""
    from sklearn.manifold import TSNE

    perplexity = min(30.0, max(5.0, n_cells / 5.0))
    perplexity = min(perplexity, max(2.0, (n_cells - 1) / 3.0))
    model = TSNE(n_components=2, perplexity=perplexity, random_state=int(seed),
                 init="pca", learning_rate="auto")
    return np.asarray(model.fit_transform(np.asarray(latent, dtype=np.float32)), dtype=np.float32)


def _relabel_by_size(labels):
    """按簇大小降序重排标签（0=最大簇），保证跨次运行稳定。"""
    labels = np.asarray(labels, dtype=np.int32)
    uniq, counts = np.unique(labels, return_counts=True)
    order = np.lexsort((np.arange(uniq.size), -counts))
    out = np.zeros(labels.size, dtype=np.int32)
    for new_label, pos in enumerate(order):
        out[labels == uniq[pos]] = new_label
    return out


def run_state(bundle, params: dict) -> dict:
    """状态表征主流程（CONTRACT §6.5.1/§7.2）：归一化 → HVG → PCA → kNN → 聚类 → 2D 嵌入 → marker。

    params: n_hvg=1500, n_pcs=30, latent='pca', n_neighbors=15, cluster='auto',
            resolution=0.8, n_clusters=0, embedding='auto', seed=0,
            以及可选的 cell_mask / gene_mask（run_qc 的 arrays 直接回传即可）。
    返回的 arrays 含 X_log/latent/embedding/clusters/knn_idx/knn_dist，由 engine 落 npz。
    """
    t0 = time.time()
    p = dict(params or {})
    seed = int(p.get("seed", 0) or 0)
    n_hvg_param = int(p.get("n_hvg", 1500) or 1500)
    n_pcs_param = int(p.get("n_pcs", 30) or 30)
    n_neighbors = int(p.get("n_neighbors", 15) or 15)
    latent_kind = str(p.get("latent", "pca") or "pca").lower()
    n_clusters_param = int(p.get("n_clusters", 0) or 0)
    cluster_mode = p.get("cluster", "auto")
    embedding_kind = str(p.get("embedding", "auto") or "auto").lower()
    warnings: list[str] = []
    notes: list[str] = []

    X = _as_float_csr(getattr(bundle, "X", None))
    if X.shape[0] == 0 or X.shape[1] == 0:
        raise err("empty_input", "表达矩阵为空，无法做状态表征")
    X = X.copy()
    X.eliminate_zeros()
    n_cells_all, n_genes_all = X.shape

    genes_all = [str(g) for g in (getattr(bundle, "genes", None) or [])]
    if len(genes_all) != n_genes_all:
        genes_all = genes_all[:n_genes_all] + [f"gene_{i}" for i in range(len(genes_all), n_genes_all)]
        warnings.append("基因名数量与矩阵列数不一致，已按位置补全")

    cell_mask = _mask_from(p.get("cell_mask"), n_cells_all)
    gene_mask = _mask_from(p.get("gene_mask"), n_genes_all)
    if p.get("cell_mask") is not None and cell_mask is None:
        warnings.append("cell_mask 长度与细胞数不符，已忽略并全部保留")
    if p.get("gene_mask") is not None and gene_mask is None:
        warnings.append("gene_mask 长度与基因数不符，已忽略并全部保留")
    if cell_mask is None:
        cell_mask = np.ones(n_cells_all, dtype=bool)
    if gene_mask is None:
        gene_mask = np.ones(n_genes_all, dtype=bool)
    if int(cell_mask.sum()) < n_cells_all:
        notes.append(f"按 cell_mask 保留 {int(cell_mask.sum())}/{n_cells_all} 个细胞")
    if int(gene_mask.sum()) < n_genes_all:
        notes.append(f"按 gene_mask 保留 {int(gene_mask.sum())}/{n_genes_all} 个基因")
    X_sub = X[cell_mask][:, gene_mask].tocsr()
    genes = [g for g, keep in zip(genes_all, gene_mask) if keep]
    n_cells, n_genes = X_sub.shape
    if n_cells < 3:
        raise err("too_few_cells", f"状态表征至少需要 3 个细胞，当前 {n_cells} 个")
    if n_genes < 3:
        raise err("too_few_genes", f"状态表征至少需要 3 个基因，当前 {n_genes} 个")

    progress(f"state: 归一化 {n_cells} 细胞 × {n_genes} 基因")
    X_log = _log_normalize(X_sub)
    n_hvg = int(min(max(int(n_hvg_param), 200), max(2, n_genes - 1)))
    hvg_idx = _hvg_seurat(X_log, n_hvg)
    hvg = [genes[int(i)] for i in hvg_idx]
    X_hvg = np.asarray(X_log[:, hvg_idx].todense(), dtype=np.float32)
    if n_cells * hvg_idx.size >= 2e8:
        warnings.append(f"HVG 稠密矩阵 {n_cells}×{hvg_idx.size} 较大，PCA 内存占用较高")
    X_hvg, hvg_mean, hvg_std = _zscore_dense(X_hvg, clip=10.0)

    from sklearn.decomposition import PCA
    from sklearn.cluster import KMeans

    n_pcs = int(max(2, min(int(n_pcs_param), hvg_idx.size - 1, n_cells - 1)))
    pca = PCA(n_components=n_pcs, random_state=seed)
    latent = pca.fit_transform(X_hvg).astype(np.float32)
    variance_explained = float(np.sum(pca.explained_variance_ratio_))
    progress(f"state: PCA {n_pcs} 维，解释 {variance_explained * 100:.1f}% 方差")
    knn_idx, knn_dist = _knn(latent, n_neighbors)

    try:
        fixed_k = int(cluster_mode)
    except (TypeError, ValueError):
        fixed_k = 0
    if n_clusters_param > 0:
        fixed_k = n_clusters_param
    cluster_key = str(cluster_mode).lower()
    if fixed_k <= 0 and cluster_key not in ("auto", "", "none", "silhouette", "kmeans"):
        warnings.append(f"cluster={cluster_mode!r} 在 builtin 后端不可用（leiden 需 scanpy），"
                        f"已回退为 k-means + silhouette 自动选 k")
    base_labels = None
    if fixed_k > 0:
        n_take = int(min(max(2, fixed_k), max(2, n_cells - 1)))
        base_labels = KMeans(n_clusters=n_take, n_init=10, random_state=seed).fit_predict(latent).astype(np.int32)
        if n_take != fixed_k:
            warnings.append(f"n_clusters={fixed_k} 超出可用范围，已收敛到 {n_take} 簇")
    else:
        max_k = int(min(12, max(2, n_cells // 20))) if n_cells >= 40 else 2
        best_sil = -1.0
        for k_try in range(2, max_k + 1):
            labels_try = KMeans(n_clusters=k_try, n_init=10, random_state=seed).fit_predict(latent)
            sil_try = _silhouette_safe(latent, labels_try, seed)
            if sil_try > best_sil:
                best_sil = sil_try
                base_labels = labels_try.astype(np.int32)
        if base_labels is None:
            n_take = int(min(3, max(2, n_cells - 1)))
            base_labels = KMeans(n_clusters=n_take, n_init=10, random_state=seed).fit_predict(latent).astype(np.int32)
    if latent_kind not in ("pca", "builtin", ""):
        notes.append(f"latent={latent_kind} 在 builtin 后端不可用，已使用 PCA")

    labels_smooth, k_smooth = _kmeans_smooth(base_labels, knn_idx, rounds=2)
    if k_smooth < 2:
        warnings.append("kNN 平滑后只剩 1 个状态，已回退为平滑前聚类（细胞可能缺乏明显状态结构）")
        labels_smooth = _relabel_by_size(base_labels)
        k_smooth = int(labels_smooth.max()) + 1
    else:
        labels_smooth = _relabel_by_size(labels_smooth)
    clusters = labels_smooth.astype(np.int32)
    n_clusters = int(clusters.max()) + 1
    cluster_labels = [str(i) for i in range(n_clusters)]
    sil = _silhouette_safe(latent, clusters, seed)
    progress(f"state: {n_clusters} 个状态，轮廓系数 {sil:.3f}")

    if embedding_kind == "umap":
        warnings.append("embedding=umap 需要 scanpy，builtin 后端已回退为 t-SNE/PCA 二维嵌入")
    use_pca_embedding = embedding_kind == "pca" or n_cells > 3000 or n_pcs < 2
    embedding = None
    if not use_pca_embedding:
        try:
            embedding = _tsne(latent, seed, n_cells)
            emb_kind = "tsne"
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"t-SNE 失败（{type(exc).__name__}: {exc}），降级为 PCA 前两维")
            use_pca_embedding = True
    if use_pca_embedding:
        embedding = np.asarray(latent[:, :2], dtype=np.float32)
        emb_kind = "pca"
    embedding = np.asarray(embedding, dtype=np.float32).reshape(n_cells, 2)
    embedding_loadings = np.zeros((n_pcs, 2), dtype=np.float32)
    embedding_loadings[0, 0] = 1.0
    embedding_loadings[1, 1] = 1.0

    centroids = np.zeros((n_clusters, n_pcs), dtype=np.float32)
    sizes = np.zeros(n_clusters, dtype=np.int64)
    for c in range(n_clusters):
        idx = np.flatnonzero(clusters == c)
        sizes[c] = idx.size
        if idx.size:
            centroids[c] = latent[idx].mean(axis=0)

    markers = _cluster_markers(X_log, clusters, n_clusters, genes, hvg_idx, seed)
    pc_rows = []
    loadings = np.asarray(pca.components_, dtype=np.float64)
    for i in range(min(n_pcs, 10)):
        order = np.argsort(-np.abs(loadings[i]), kind="stable")[:5]
        pc_rows.append({
            "PC": f"PC{i + 1}",
            "variance_pct": fnum(float(pca.explained_variance_ratio_[i]) * 100.0, 2),
            "top_genes": ", ".join(f"{hvg[int(j)]}({loadings[i, int(j)]:+.2f})" for j in order),
        })
    cluster_rows = pd.DataFrame({
        "clusters": cluster_labels,
        "n_cells": sizes.astype(np.int64),
        "pct": [fnum(100.0 * s / max(1, n_cells), 2) for s in sizes],
    })
    marker_rows = markers.copy()
    if not marker_rows.empty:
        marker_rows["cluster"] = marker_rows["cluster"].astype(str)
    top_pcs = pd.DataFrame(pc_rows, columns=["PC", "variance_pct", "top_genes"])
    latent_loadings = np.asarray(pca.components_.T, dtype=np.float32)

    n_genes_cell = np.asarray(X_sub.getnnz(axis=1)).ravel().astype(np.int32)
    mito_pct, _ = _mito_pct(X_sub, genes)
    obs_columns = {
        "clusters": [int(v) for v in clusters],
        "n_genes": [int(v) for v in n_genes_cell],
        "mito_pct": [fnum(v, 4) for v in mito_pct],
    }

    runtime = time.time() - t0
    metrics = {
        "n_cells": int(n_cells), "n_genes": int(n_genes), "n_hvg": int(hvg_idx.size),
        "n_pcs": int(n_pcs), "n_clusters": int(n_clusters), "silhouette": fnum(sil, 4),
        "variance_explained": fnum(variance_explained, 4),
        "knn_k": int(knn_idx.shape[1]), "embedding": emb_kind,
        "backend": "builtin", "seed": seed, "runtime_s": fnum(runtime, 3),
    }
    if sil < 0.05:
        warnings.append(f"轮廓系数仅 {sil:.3f}，细胞状态结构较弱，聚类结果需谨慎解读")
    if n_clusters >= max(2, n_cells // 10):
        warnings.append(f"状态数 {n_clusters} 相对细胞数 {n_cells} 偏多，建议调大 min_cells 或减少 n_clusters")
    if _try_scanpy() is not None:
        notes.append("backend=builtin（numpy/scipy/sklearn）；scanpy 已安装但本实现统一走 builtin，未用 Leiden/UMAP")
    else:
        notes.append("backend=builtin（numpy/scipy/sklearn）；scanpy 未安装，聚类/PCA/t-SNE 全部内置实现")
    summary = (
        f"状态表征：{n_cells} 个细胞 × {n_genes} 个基因（HVG {hvg_idx.size}）→ {n_clusters} 个状态；"
        f"PC1–PC{n_pcs} 解释 {variance_explained * 100:.1f}% 方差；轮廓系数 {sil:.2f}；2D 嵌入用 {emb_kind.upper()}。"
    )
    return {
        "arrays": {  # 大数组；载荷/质心同时放入 arrays 以便 engine npz 落盘后可恢复
            "X_log": X_log, "latent": latent, "embedding": embedding,
            "clusters": clusters, "knn_idx": np.asarray(knn_idx, dtype=np.int32),
            "knn_dist": np.asarray(knn_dist, dtype=np.float32),
            "latent_loadings": latent_loadings, "cluster_centroids": centroids,
            "embedding_loadings": embedding_loadings,
            "hvg_mean": hvg_mean, "hvg_std": hvg_std,
        },
        "genes": genes,
        "hvg": hvg,
        "hvg_mean": hvg_mean,
        "hvg_std": hvg_std,
        "latent_loadings": latent_loadings,
        "cluster_centroids": centroids,
        "embedding_loadings": embedding_loadings,
        "cluster_labels": cluster_labels,
        "cluster_sizes": [int(s) for s in sizes],
        "obs_columns": obs_columns,
        "backend": "builtin",
        "n_clusters": int(n_clusters),
        "n_pcs": int(n_pcs),
        "metrics": metrics,
        "tables": [
            {"id": "cluster_sizes", "title": "状态规模", "rows": cluster_rows},
            {"id": "markers", "title": "状态标记基因", "rows": marker_rows},
            {"id": "top_pcs", "title": "主成分载荷", "rows": top_pcs},
        ],
        "figures": [
            {"kind": "state_embedding", "title": "细胞状态空间"},
            {"kind": "cluster_bar", "title": "状态规模"},
        ],
        "summary": summary,
        "warnings": warnings,
        "notes": notes,
    }

def _bh_adjust(pvals):
    """Benjamini–Hochberg FDR（NaN 视为 1）。"""
    p = np.nan_to_num(np.asarray(pvals, dtype=np.float64), nan=1.0)
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p, kind="stable")
    ranked = p[order] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n, dtype=np.float64)
    out[order] = np.clip(q, 0.0, 1.0)
    return out

def _cluster_markers(X_log, clusters, n_clusters, genes, hvg_idx, seed, top_n: int = 8):
    """每簇 vs 其余：logFC + pct_in/out + Mann–Whitney U + BH；候选=HVG 内簇均值 top300。"""
    from scipy.stats import mannwhitneyu

    n = X_log.shape[0]
    rng = np.random.default_rng(int(seed))
    sub = np.sort(rng.choice(n, size=min(n, 6000), replace=False)) if n > 6000 else np.arange(n)
    Xs, cl = X_log[sub], np.asarray(clusters, dtype=np.int64)[sub]
    n_sub = Xs.shape[0]
    onehot = sp.csr_matrix((np.ones(n_sub, np.float32), (np.arange(n_sub), cl)), shape=(n_sub, n_clusters))
    sizes = np.asarray(onehot.sum(axis=0)).ravel()
    means = np.asarray((onehot.T @ Xs).todense(), dtype=np.float64) / np.maximum(sizes[:, None], 1.0)
    universe = np.unique(np.asarray(hvg_idx if hvg_idx is not None else np.arange(len(genes))))
    universe = universe[universe < len(genes)]
    rows = []
    for c in range(n_clusters):
        mask, out_mask = cl == c, cl != c
        if int(mask.sum()) < 2 or int(out_mask.sum()) < 2 or universe.size == 0:
            continue
        cand = universe[np.argsort(-means[c, universe], kind="stable")[:300]]
        dense = np.asarray(Xs[:, cand].todense(), dtype=np.float32)
        pvals = np.ones(cand.size)
        for t in range(cand.size):
            a, b = dense[mask, t], dense[out_mask, t]
            if float(a.max()) <= 0.0 and float(b.max()) <= 0.0:
                continue
            try:
                pvals[t] = float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
            except Exception:  # noqa: BLE001
                pvals[t] = 1.0
        qvals = _bh_adjust(pvals)
        in_mean, out_mean = dense[mask].mean(axis=0), dense[out_mask].mean(axis=0)
        logfc = np.log2((in_mean + 1e-9) / (out_mean + 1e-9))
        pct_in, pct_out = (dense[mask] > 0).mean(axis=0), (dense[out_mask] > 0).mean(axis=0)
        for t in np.lexsort((-np.abs(logfc), qvals))[: max(1, int(top_n))]:
            rows.append({"cluster": str(c), "gene": genes[int(cand[t])], "logFC": fnum(logfc[t], 3),
                         "pct_in": fnum(pct_in[t], 3), "pct_out": fnum(pct_out[t], 3),
                         "p_value": fnum(pvals[t], 6), "q_value": fnum(qvals[t], 6)})
    return pd.DataFrame(rows, columns=["cluster", "gene", "logFC", "pct_in", "pct_out", "p_value", "q_value"])

def run_annotate(bundle, state: dict, params: dict) -> dict:
    """用 vc_priors.STATE_MARKERS 给每个 state 簇打参考状态分（CONTRACT §7.2）。

    method='zscore'：簇均值相对全细胞的基因 z-score，marker 取均值；
    method='ucell'：簇均值在基因间的秩百分位（UCell 风格秩和的簇级近似）；
    每簇取 top1/top2，margin<min_score 标 Unknown 并写 warning。
    """
    t0 = time.time()
    p = dict(params or {})
    panel = p.get("panel", "builtin")
    min_score = float(p.get("min_score", 0.3) if p.get("min_score") is not None else 0.3)
    column = str(p.get("column", "cell_state") or "cell_state")
    method = str(p.get("method", "zscore") or "zscore").lower()
    warnings: list[str] = []
    notes: list[str] = []
    state = state if isinstance(state, dict) else {}
    arrays = state.get("arrays") if isinstance(state.get("arrays"), dict) else state
    if not isinstance(arrays, dict):
        arrays = {}
    clusters = arrays.get("clusters")
    if clusters is None:
        raise err("missing_state", "run_annotate 需要先运行 state（缺少 clusters）")
    clusters = np.asarray(clusters, dtype=np.int32)
    n_cells = int(clusters.size)
    genes = [str(g) for g in (state.get("genes") or getattr(bundle, "genes", None) or [])]
    X_log = arrays.get("X_log")
    if X_log is None:
        X_log = _log_normalize(getattr(bundle, "X", None))
        if X_log.shape[0] != n_cells:
            raise err("shape_mismatch", "state 的 clusters 与 bundle.X 细胞数不一致，请先重跑 state")
        warnings.append("state 未携带 X_log，已从 bundle.X 重新归一化（可能与 state 的过滤掩码不一致）")
    X_log = _as_float_csr(X_log)
    n_genes = int(X_log.shape[1])
    if len(genes) != n_genes:
        genes = genes[:n_genes] + [f"gene_{i}" for i in range(len(genes), n_genes)]
    if X_log.shape[0] != n_cells:
        raise err("shape_mismatch", "state 的 clusters 长度与 X_log 行数不一致，请先重跑 state")
    if isinstance(panel, dict):
        panel_markers = {str(k): [str(g) for g in v] for k, v in panel.items()}
    elif str(panel).lower() == "demo":
        panel_markers = {str(s.get("name")): list(s.get("markers") or []) for s in priors.DEMO_STATES}
    else:
        if str(panel).lower() not in ("builtin", "", "default", "state_markers"):
            warnings.append(f"未知 panel={panel!r}，已回退为 builtin STATE_MARKERS")
        panel_markers = {k: list(v) for k, v in priors.STATE_MARKERS.items()}
    if method not in ("zscore", "ucell"):
        warnings.append(f"未知 method={method!r}，已回退为 zscore")
        method = "zscore"
    gene_pos = {g: i for i, g in enumerate(genes)}
    states: list[str] = []
    state_genes: dict[str, np.ndarray] = {}
    for name, markers in panel_markers.items():
        hits = np.unique([gene_pos[g] for g in markers if g in gene_pos])
        if hits.size:
            states.append(str(name))
            state_genes[str(name)] = hits
        else:
            warnings.append(f"参考状态 {name} 的 marker 在表达矩阵中一个都没有，已跳过")
    if not states:
        raise err("bad_panel", "参考 marker 面板与表达矩阵无交集，无法注释；请检查基因名或 panel")
    n_states = len(states)
    k = int(clusters.max()) + 1
    sizes = np.bincount(clusters, minlength=k).astype(np.float64)
    onehot = sp.csr_matrix((np.ones(n_cells, np.float32), (np.arange(n_cells), clusters)), shape=(n_cells, k))
    sums = np.asarray((onehot.T @ X_log).todense(), dtype=np.float64)
    means = sums / np.maximum(sizes[:, None], 1.0)
    gmean = np.asarray(X_log.mean(axis=0)).ravel().astype(np.float64)
    gsq = np.asarray(X_log.multiply(X_log).mean(axis=0)).ravel().astype(np.float64)
    gstd = np.sqrt(np.maximum(gsq - gmean * gmean, 0.0))
    gstd = np.where(gstd < 1e-8, 1.0, gstd)
    z = (means - gmean) / gstd
    if method == "ucell":
        ranks = np.argsort(np.argsort(means, axis=1), axis=1).astype(np.float64)
        pct_rank = ranks / max(1, n_genes - 1)
        score_matrix = np.column_stack([pct_rank[:, state_genes[s]].mean(axis=1) for s in states])
    else:
        score_matrix = np.column_stack([z[:, state_genes[s]].mean(axis=1) for s in states])
    score_matrix = np.asarray(score_matrix, dtype=np.float64).reshape(k, n_states)
    order = np.argsort(-score_matrix, axis=1)
    labels_per_cluster = np.zeros(k, dtype=np.int32)
    cluster_labels: list[str] = []
    margins = np.zeros(k, dtype=np.float64)
    rows = []
    for c in range(k):
        o = order[c]
        top1 = states[int(o[0])]
        s1 = float(score_matrix[c, o[0]])
        s2 = float(score_matrix[c, o[1]]) if n_states > 1 else 0.0
        top2 = states[int(o[1])] if n_states > 1 else "Unknown"
        margin = s1 - s2
        # 归属规则（真实数据比 demo 更混杂）：
        #   - 证据分 s1 必须达到 min_score（默认 0.3，代表"高于全细胞平均水平"）；
        #   - margin 很小时（<0.05）只有 s1 很强（>=1.0）才敢归属，否则 Unknown；
        #   - 归属了但 margin < 0.3 → 标 ambiguous（保留 top1，但提示 top2 也在同一量级）。
        assigned = bool(s1 >= min_score and (margin >= 0.05 or s1 >= 1.0))
        ambiguous = bool(assigned and margin < 0.3)
        label = top1 if assigned else "Unknown"
        margins[c] = margin
        labels_per_cluster[c] = int(o[0]) if assigned else n_states
        cluster_labels.append(label)
        marker_idx = state_genes[top1]
        rank_src = pct_rank if method == "ucell" else z
        top_marker = marker_idx[np.argsort(-rank_src[c, marker_idx], kind="stable")[:3]]
        evidence = ", ".join(genes[int(i)] for i in top_marker)
        rows.append({"cluster": str(c), "n_cells": int(sizes[c]), "label": label, "top_state": top1,
                     "top_score": fnum(s1, 3), "second_state": top2, "second_score": fnum(s2, 3),
                     "margin": fnum(margin, 4), "ambiguous": ambiguous,
                     "n_markers": int(marker_idx.size), "evidence": evidence})
    labels_per_cell = labels_per_cluster[clusters].astype(np.int32)
    cell_state = [cluster_labels[int(c)] for c in clusters]
    if isinstance(state, dict):  # 同步给 state stage，vc_plot 的 color_by=cell_state 才能着色
        state_columns = state.get("cells_columns")
        if not isinstance(state_columns, dict):
            state_columns = {}
            state["cells_columns"] = state_columns
        state_columns[column] = cell_state
    assigned = int(sum(1 for lab in cluster_labels if lab != "Unknown"))
    unknown = int(k - assigned)
    ambiguous_count = int(sum(1 for row in rows if row.get("ambiguous")))
    assigned_cells = int(sum(int(sizes[c]) for c in range(k) if cluster_labels[c] != "Unknown"))
    mean_margin = float(np.mean(margins)) if k else 0.0
    if unknown:
        warnings.append(f"{unknown}/{k} 个状态簇证据分 < {min_score} 或与第二名几乎并列，已标为 Unknown"
                        "（参考面板覆盖不足或状态混合；可上传自定义 marker 面板）")
    if ambiguous_count:
        warnings.append(f"{ambiguous_count} 个状态簇虽然归属成功，但与第二名得分接近（margin < 0.3），"
                        "标签是 top1 推断，请结合 marker 表核对")
    if n_states <= 2:
        warnings.append(f"面板只有 {n_states} 个参考状态可用，注释特异性有限")
    notes.append(f"注释面板：{len(panel_markers)} 个参考状态，其中 {n_states} 个在矩阵中有 marker；method={method}")
    runtime = time.time() - t0
    best_pos = int(np.argmax(score_matrix.max(axis=0)))
    best = float(score_matrix[:, best_pos].max())
    summary = (
        f"状态注释：{k} 个状态簇中 {assigned} 个匹配参考面板"
        + (f"（其中 {ambiguous_count} 个与第二名接近）" if ambiguous_count else "")
        + f"、{unknown} 个 Unknown（覆盖 {assigned_cells}/{n_cells} 个细胞）；"
        f"平均 margin {mean_margin:.2f}；最高分 {best:.2f} 对应 {states[best_pos]}（{method} 打分）。"
    )
    return {
        "arrays": {"labels_per_cluster": labels_per_cluster.astype(np.int32), "labels_per_cell": labels_per_cell},
        "states": states + ["Unknown"],
        "cluster_labels": cluster_labels,
        "cells_columns": {column: cell_state},
        "column": column,
        "metrics": {
            "assigned": assigned, "unknown": unknown, "ambiguous": ambiguous_count,
            "mean_margin": fnum(mean_margin, 4),
            "assigned_cells": assigned_cells, "unknown_cells": int(n_cells - assigned_cells),
            "n_states": n_states, "n_clusters": k, "method": method, "panel": str(panel),
            "runtime_s": fnum(runtime, 3),
        },
        "tables": [
            {"id": "annotate_scores", "title": "状态打分", "rows": pd.DataFrame(rows)},
            {"id": "annotate_assignments", "title": "状态归属", "rows": pd.DataFrame({
                "cluster": [str(c) for c in range(k)],
                "label": cluster_labels,
                "n_cells": np.bincount(clusters, minlength=k).astype(np.int64),
                "pct": [fnum(100.0 * sizes[c] / max(1, n_cells), 2) for c in range(k)],
                "ambiguous": [bool(rows[c].get("ambiguous")) for c in range(k)],
            })},
        ],
        "figures": [{"kind": "state_embedding", "title": "状态注释", "params": {"color_by": column}}],
        "summary": summary,
        "warnings": warnings,
        "notes": notes,
    }


def _row_normalize(matrix):
    """行归一；全零行改均匀分布，避免 NaN。"""
    mat = np.asarray(matrix, dtype=np.float64)
    rows = mat.sum(axis=1, keepdims=True)
    return mat / np.where(rows > 0, rows, 1.0)


def _sinkhorn(matrix, n_iter: int = 20):
    """交替行/列归一（Sinkhorn），末步再行归一保证每一行和为 1。"""
    T = np.asarray(matrix, dtype=np.float64).copy()
    for _ in range(int(max(1, n_iter))):
        rows = T.sum(axis=1, keepdims=True)
        T = T / np.where(rows > 0, rows, 1.0)
        cols = T.sum(axis=0, keepdims=True)
        T = T / np.where(cols > 0, cols, 1.0)
    return _row_normalize(T)


def run_transition(bundle, state: dict, params: dict) -> dict:
    """kNN 簇间转移矩阵：边计数 → 行归一 T0 → Sinkhorn 平滑 → 净流/稳定性（CONTRACT §6.5.3）。

    params: method='knn', temperature=0.1, k=10（每细胞取前 k 个邻居边）。
    T=(1-r)*T0 + r/k 后做 20 步 Sinkhorn；stability 用 n_boot 次细胞 bootstrap
    重采样 kNN 边的 T 的 1 - 相对 Frobenius 差（100 次默认）。
    """
    t0 = time.time()
    p = dict(params or {})
    method = str(p.get("method", "knn") or "knn").lower()
    temperature = float(p.get("temperature", 0.1) if p.get("temperature") is not None else 0.1)
    k_edges = int(p.get("k", 10) or 10)
    seed = int(p.get("seed", 0) or 0)
    n_boot = int(p.get("n_boot", 100) if p.get("n_boot") is not None else 100)
    warnings: list[str] = []
    notes: list[str] = []

    state = state if isinstance(state, dict) else {}
    arrays = state.get("arrays") if isinstance(state.get("arrays"), dict) else state
    if not isinstance(arrays, dict):
        arrays = {}
    knn_idx = arrays.get("knn_idx")
    clusters = arrays.get("clusters")
    if knn_idx is None or clusters is None:
        raise err("missing_state", "run_transition 需要先运行 state（缺少 knn_idx/clusters）")
    knn_idx = np.asarray(knn_idx, dtype=np.int64)
    clusters = np.asarray(clusters, dtype=np.int64)
    if knn_idx.ndim != 2 or knn_idx.shape[0] != clusters.size:
        raise err("shape_mismatch", "state 的 knn_idx 与 clusters 形状不一致，请先重跑 state")
    if method != "knn":
        warnings.append(f"method={method} 在 builtin 后端不可用，已回退到 kNN 簇间转移")
    n_cells = int(clusters.size)
    labels = np.unique(clusters)
    k_states = int(labels.size)
    if k_states < 2:
        raise err("too_few_states", f"状态数 {k_states} < 2，无法构建状态转移；请先跑 state 得到多个状态")
    if n_cells < 2:
        raise err("too_few_cells", f"细胞数 {n_cells} < 2，无法构建状态转移")
    remap = np.full(int(clusters.max()) + 1, -1, dtype=np.int64)
    remap[labels] = np.arange(k_states)
    ci = remap[clusters]
    kk = int(min(max(1, k_edges), knn_idx.shape[1]))
    ni = knn_idx[:, :kk]
    nj = remap[clusters[ni]]
    flat = ci[:, None] * k_states + nj
    counts = np.bincount(flat.ravel(), minlength=k_states * k_states).reshape(k_states, k_states).astype(np.float64)
    T0 = _row_normalize(counts)
    r = float(min(max(temperature, 0.0), 1.0))
    if r != temperature:
        warnings.append(f"temperature={temperature} 超出 [0,1]，已截断为 {r}")
    T = _sinkhorn((1.0 - r) * T0 + r / k_states, n_iter=20)
    diag_mean = float(np.mean(np.diag(T)))

    rng = np.random.default_rng(seed)
    rels: list[float] = []
    base_norm = max(float(np.linalg.norm(T)), 1e-12)
    if n_boot > 0:
        for _ in range(int(n_boot)):
            idx = rng.integers(0, n_cells, size=n_cells)
            flat_b = ci[idx, None] * k_states + nj[idx]
            cnt_b = np.bincount(flat_b.ravel(), minlength=k_states * k_states).reshape(k_states, k_states)
            T0_b = _row_normalize(cnt_b.astype(np.float64))
            T_b = _sinkhorn((1.0 - r) * T0_b + r / k_states, n_iter=20)
            rels.append(float(np.linalg.norm(T_b - T)) / base_norm)
        stability = float(max(0.0, min(1.0, 1.0 - float(np.mean(rels)))))
    else:
        # 不做 bootstrap 就没有稳定性估计——不要用 1.0 伪装成"完美稳定"。
        stability = None
        warnings.append("n_boot=0，未做 bootstrap 稳定性估计（stability 记为 null，不可当作稳定）")

    names = [str(x) for x in (state.get("cluster_labels") or [])]
    if len(names) != k_states:
        names = [str(int(v)) for v in labels]
    net_struct = 1.0 - T0.sum(axis=0)  # 正=更多流出（源），负=更多流入（汇）
    src_order = np.argsort(-net_struct)[:3]
    sink_order = np.argsort(net_struct)[:3]
    sources = [{"cluster": names[int(i)], "flow": fnum(float(net_struct[i]), 6)}
               for i in src_order if net_struct[i] > 1e-12]
    sinks = [{"cluster": names[int(i)], "flow": fnum(float(net_struct[i]), 6)}
             for i in sink_order if net_struct[i] < -1e-12]
    pair = T - T.T
    iu = np.triu_indices(k_states, k=1)
    vals = pair[iu]
    net_flow = []
    for pos in np.argsort(-np.abs(vals))[:20]:
        i, j = int(iu[0][pos]), int(iu[1][pos])
        v = float(vals[pos])
        if abs(v) <= 1e-12:
            continue
        if v >= 0:
            net_flow.append({"from": names[i], "to": names[j], "flow": fnum(v, 6)})
        else:
            net_flow.append({"from": names[j], "to": names[i], "flow": fnum(-v, 6)})
    if diag_mean < 1.0 / max(2, k_states) + 1e-6:
        warnings.append(f"转移矩阵对角均值 {diag_mean:.3f} 接近均匀，状态间没有明显方向性")
    if not net_flow:
        warnings.append("状态间净流几乎为 0，转移矩阵接近对称")
    direction_note = (
        "源/汇与净流方向由**静态** kNN 图（无时间信息、无拟时序）导出，只能当作候选方向假说；"
        "要谈命运方向需要 RNA velocity / 拟时序 / 谱系追踪等独立证据。"
    )
    warnings.append(direction_note)
    matrix_rows = pd.DataFrame(T, columns=names)
    matrix_rows.insert(0, "from", names)
    runtime = time.time() - t0
    metrics = {
        "n_states": int(k_states), "stability": fnum(stability, 4), "diag_mean": fnum(diag_mean, 4),
        "n_cells": int(n_cells), "n_edges": int(n_cells * kk), "k": int(kk),
        "temperature": fnum(r, 4), "method": "knn", "n_boot": int(max(0, n_boot)),
        "runtime_s": fnum(runtime, 3),
    }
    if net_flow:
        top_pair = net_flow[0]
        top_txt = f"{top_pair['from']}→{top_pair['to']}（{top_pair['flow']:.3f}）"
    else:
        top_txt = "无明显净转移"
    src_txt = f"{sources[0]['cluster']}（{sources[0]['flow']:+.3f}）" if sources else "无明显源"
    sink_txt = f"{sinks[0]['cluster']}（{sinks[0]['flow']:+.3f}）" if sinks else "无明显汇"
    stability_txt = ("未做 bootstrap" if stability is None
                     else f"bootstrap 稳定度 {stability:.2f}（{int(max(0, n_boot))} 次）")
    summary = (
        f"状态转移（候选方向假说）：{k_states} 个状态、{n_cells} 个细胞、{int(n_cells * kk)} 条 kNN 边；"
        f"对角均值 {diag_mean:.2f}，最强净转移 {top_txt}（静态图近似，非真实动力学）；"
        f"主净流出 {src_txt}、主净流入 {sink_txt}；{stability_txt}。"
    )
    validity = {
        "direction_evidence": "candidate_hypothesis",
        "notes": [direction_note,
                  "转移矩阵是 kNN 细胞相似度的静态近似；源/汇标签与 net_flow 不构成命运证据。"],
    }
    notes = list(notes or []) + ["方向性仅供提出假说，需 velocity/拟时序/谱系追踪或实验验证。"]
    return {
        "arrays": {"T": np.asarray(T, dtype=np.float64)},
        "sources": sources,
        "sinks": sinks,
        "net_flow": net_flow,
        "cluster_names": names,
        "metrics": metrics,
        "direction_evidence": "candidate_hypothesis",
        "validity": validity,
        "notes": notes,
        "tables": [{"id": "transition_matrix", "title": "状态转移矩阵", "rows": matrix_rows}],
        "figures": [
            {"kind": "transition_matrix", "title": "状态转移矩阵"},
            {"kind": "transition_graph", "title": "状态转移图"},
        ],
        "summary": summary,
        "warnings": warnings,
    }
