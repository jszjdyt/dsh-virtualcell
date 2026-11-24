#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — 扰动响应预测 + 药物/基因签名投影（GEARS-lite）。

模块边界（CONTRACT §7.4，禁止跨模块改签名）：

    run_response(bundle, state, table, params) -> dict
    project_signature(bundle, state, signature, params) -> dict

本机没有 torch / gears，因此用 ``numpy`` + ``sklearn.linear_model.RidgeCV``
实现"加性隐空间模型"（GEARS 的最简线性版），而不是图神经网络：

    Δ_latent[pert] ≈ W · f(control 基线) + μ_Δ

其中 ``μ_Δ`` 是训练扰动的平均位移（均值位移基线），``f(·)`` 是逐扰动可变的
基线特征。**关于 CONTRACT 里 ``W · x_ctrl`` 的落地解释**：如果直接用全基因
``x_ctrl`` 矩阵（所有扰动共享同一个 control 均值）当作特征，设计矩阵会退化为
常数矩阵（秩 1），无法产生扰动之间的差异，因此这里取"被扰动基因在 control 中的
基线表达"及其派生量作为逐扰动特征：

    f0 = x_ctrl[被扰动基因]                 （基线表达水平）
    f1 = f0 的稳健 z 分数（median / IQR）   （相对表达位置）
    f2 = f0 在数据集基因里的经验分位数      （表达秩）
    f3 = 扰动类型符号（OE=+1 / KO,KD=-1 / 未知=0，从扰动名解析）

特征数按样本量自动截断（``min(4, max(1, n_pert - 3))``），并对特征做
中位数/IQR 稳健标准化后喂给 RidgeCV（内层 GCV 选 alpha）；残差在训练折内中心化，
等价于"均值位移基线 + 线性校正"，保证与基线可比。

流程（``run_response``）
------------------------
1. 规范化扰动表：宽表 ``cell_id, perturbation, <gene...>`` 或长表
   ``gene, perturbation, value``；control 用 control/WT/NC/non-targeting 等识别
   （大小写、下划线、连字符不敏感）。
2. 对齐到 ``bundle.genes``（精确 + 大小写不敏感；未命中的基因列忽略并 warning，
   数据集里缺的基因 Δ 记 0 并 warning）。
3. 逐扰动细胞均值 ``x_pert`` − control 均值 ``x_ctrl`` → ``Δ_gene``（基因空间）。
4. ``Δ_latent = Δ_gene[hvg] · latent_loadings``（PCA 载荷投影）；``latent_dim>0`` 截断。
5. ``method='additive'``：RidgeCV 学 ``Δ_latent ≈ μ + W·f``；交叉验证默认
   "扰动数 ≤10 用留一（LOO），否则 nfold 折 k-fold"，同时算均值位移基线。
6. 输出 ``arrays={'pred','true','per_gene_err',...}``、``per_pert``、
   ``metrics={method,n_pert,n_cells,r2,spearman,baseline_r2,beats_baseline,latent_dim,...}``、
   ``tables=[response_per_pert, response_pred_true]``、``figures=[{'kind':'response_scatter'}]``、
   中文 ``summary``、``warnings``。

降级规则
--------
* 有效扰动数 < 5：warning「不足以训练，仅给描述性结果」，method 降级为
  ``mean_shift``（LOO 均值基线），不再训练线性模型。
* 无 control 行：用全表总均值当伪 control，warning。
* ``table is None``：若 ``params['signature']`` 存在 → 自动转 ``project_signature``
  （``metrics['mode']='signature_projection'``）；否则抛 ``prerequisite``，
  details 说明"需先 attach kind=perturb_table 或 drug_signature"。

``project_signature``
---------------------
签名 ``{genes, logfc}`` → bundle 基因空间向量 ``s`` → ``s_latent = s[hvg]·loadings``
→ 归一化 → 与各簇质心余弦（``cluster_cos``，即 CONTRACT 里的 connectivity score：
正值=该簇沿签名激活方向，负值=被抑制；符号翻转对照 ``cs_flip = -cs``）→
签名 top100 up/down 对 ``vc_priors.PATHWAY_SETS`` 做超几何 ORA + BH → 返回
``arrays={'signature_latent','cluster_cos'}``、``per_cluster``、``enrichment``、
``metrics={n_genes_matched,top_cluster,max_abs_cos}``、两张表、``compare_bar`` 图。

方法学局限（写进 ``warnings`` / ``notes``，不要当作结论）
----------------------------------------------------------
* 隐空间是 PC 空间，线性 PCA 无法表达非线性调控；未见扰动的泛化只在"沿隐空间
  线性"的假设下成立。
* 扰动表若每扰动只有少量细胞，均值本身噪声很大；< ``min_pert_cells`` 的扰动被剔除。
* ``per_gene_err`` 由隐空间最小范数逆投影 + 全局尺度对齐得到，是诊断量
  （模型只在 HVG 子空间有表达能力，非 HVG 基因误差记为 0）。
* 签名富集的 universe 取数据集全部基因（保守），不是"全部通路基因"。
"""

from __future__ import annotations

import os
import re

import numpy as np
import pandas as pd

try:  # 作为包导入（lib 是包）时走相对导入
    from . import vc_common as common
    from . import vc_priors as priors
except ImportError:  # 直接以 lib/ 为 sys.path 时（vc_engine / 自测脚本）
    import vc_common as common  # type: ignore
    import vc_priors as priors  # type: ignore

__all__ = ["run_response", "project_signature"]

err = common.err
fnum = common.fnum
progress = common.progress
EngineError = common.EngineError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 参数默认值（CONTRACT §4 step=response）
DEFAULTS: dict = {
    "data": "auto",
    "method": "additive",
    "nfold": 5,
    "latent_dim": 0,
    "min_pert_cells": 20,
    "seed": 0,
    "signature": None,
    "signature_name": None,
}

#: 支持的 method（additive 为默认；其余两个是显式降级）
SUPPORTED_METHODS = ("additive", "mean_shift", "mean")

#: 训练所需最少扰动数（< 此值只给描述性结果，CONTRACT §7.4「数据不足优雅降级」）
MIN_TRAIN_PERT = 5

#: 列名别名（统一小写比较）
_CELL_KEYS = (
    "cell_id", "cellid", "cell", "cell_name", "cellname", "barcode",
    "cell_barcode", "cell_index", "index",
)
_PERT_KEYS = (
    "perturbation", "perturbations", "perturb", "condition", "pert_gene",
    "perturbation_name", "target", "target_gene", "guide_gene", "sgrna_gene",
)
_GENE_KEYS = ("gene", "gene_symbol", "gene_name", "symbol", "genes")
_VALUE_KEYS = (
    "value", "expr", "expression", "mean_expr", "mean_expression", "lognorm",
    "logfc", "log2fc", "lfc", "score", "delta", "effect",
)

#: control 标签（先做 "去非字母数字 + 小写" 归一，再匹配；末尾数字会被剥掉）
_CONTROL_KEYS = frozenset({
    "control", "ctrl", "wt", "wildtype", "nc", "nontargeting", "nontarget",
    "nonessential", "scramble", "scrambled", "mock", "vehicle", "untreated",
    "baseline", "negativecontrol", "negctrl", "safeharbor",
})

#: 扰动名到基因名：常见修饰后缀（反复剥离）
_SUFFIX_PATTERN = re.compile(
    r"(?i)(?:[_\-\s.]+)(?:ko|kd|oe|ki|ov|over|overexpression|knockout|knockdown|"
    r"crispri|crispra|crispr|rnai|shrna|sirna|rep|replicate|guide|sg|g|clone|"
    r"pool|lib|day|d)\s*\d*$"
)
_TRAILING_INDEX = re.compile(r"(?i)(?:[_\-\s.]+)\d+$")

#: 扰动类型符号
_OE_PATTERN = re.compile(
    r"(?i)(?:^|[_\-\s+])(?:oe|ov|over|overexpression|over_expression|gain|"
    r"activate|activation|up)(?:$|[_\-\s+\d])"
)
_KO_PATTERN = re.compile(
    r"(?i)(?:^|[_\-\s])(?:ko|kd|ki|crispri|crispr|rnai|shrna|sirna|knockout|"
    r"knockdown|loss|inhibit|down|del|deletion)(?:$|[_\-\s\d])"
)

#: BH / ORA 参数
_TOP_SIGNATURE_GENES = 100
_MAX_ENRICH_ROWS = 60


# ---------------------------------------------------------------------------
# 参数与轻量工具
# ---------------------------------------------------------------------------

def _param_dict(params) -> dict:
    """把外部 params 收敛成 dict（None / 非 dict 都能吃）。"""
    return dict(params) if isinstance(params, dict) else {}


def _expanded_params(params) -> dict:
    out = dict(DEFAULTS)
    out.update(_param_dict(params))
    return out


def _int_param(params: dict, key: str, default: int, lo: int | None = None,
               hi: int | None = None) -> int:
    try:
        value = int(round(float(params.get(key, default))))
    except (TypeError, ValueError):
        value = int(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _norm_key(value) -> str:
    """标签归一：只留小写字母数字（用于 control 判定）。"""
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _is_control(label) -> bool:
    """control / WT / NC / non-targeting（含 control_1、Ctrl-2 之类写法）判定。"""
    key = _norm_key(label)
    if not key:
        return False
    if key in _CONTROL_KEYS:
        return True
    stripped = re.sub(r"\d+$", "", key)
    return stripped in _CONTROL_KEYS


def _is_numeric(series) -> bool:
    try:
        return bool(pd.api.types.is_numeric_dtype(series))
    except Exception:  # noqa: BLE001
        return False


def _unit(vector) -> np.ndarray:
    """L2 归一化（零向量原样返回）。"""
    array = np.asarray(vector, dtype=float)
    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm < 1e-12:
        return np.zeros_like(array)
    return array / norm


def _finite_array(array, fill: float = 0.0) -> np.ndarray:
    out = np.asarray(array, dtype=float)
    return np.nan_to_num(out, nan=fill, posinf=fill, neginf=fill)


def _gene_lookup(genes: list[str]):
    """返回 name -> index 的查找函数（精确优先，其次大小写不敏感）。"""
    exact = {str(g): i for i, g in enumerate(genes)}
    upper: dict[str, int] = {}
    for index, gene in enumerate(genes):
        key = str(gene).upper()
        if key not in upper:
            upper[key] = index

    def lookup(name) -> int | None:
        if name is None:
            return None
        text = str(name).strip()
        if not text:
            return None
        if text in exact:
            return exact[text]
        key = text.upper()
        if key in exact:
            return exact[key]
        return upper.get(key)

    return lookup


def _state_get(state, key, default=None):
    """从 state dict 取字段：先顶层，再 state['arrays']（兼容 engine 两种形态）。"""
    if not isinstance(state, dict):
        return default
    value = state.get(key)
    if value is not None:
        return value
    arrays = state.get("arrays")
    if isinstance(arrays, dict):
        value = arrays.get(key)
        if value is not None:
            return value
    return default


def _genes_of(bundle, state=None) -> list[str]:
    """基因列表：优先 state['genes']，退化到 bundle.genes。"""
    genes = _state_get(state, "genes") if state is not None else None
    if genes is None and bundle is not None:
        genes = getattr(bundle, "genes", None)
    if genes is None or len(genes) == 0:
        raise err(
            "prerequisite",
            "缺少基因列表：请先加载数据并运行 state 步骤",
            how=["打开数据集（open / demo）", "运行流程步骤 state（状态表征）"],
        )
    return [str(gene) for gene in genes]


def _load_latent_frame(state, genes: list[str], latent_dim: int, warnings: list[str]):
    """读出 PCA 载荷 + HVG，返回 (loadings[:, :n_latent], hvg, hvg_bundle_idx, hvg_rows, n_latent)。

    loadings 的行顺序 = state['hvg']；行数必须与之匹配。缺失或形状不符抛 prerequisite。
    """
    raw = _state_get(state, "latent_loadings")
    if raw is None:
        raise err(
            "prerequisite",
            "缺少 state 的 PCA 载荷（latent_loadings）：请先运行 state 步骤",
            how=["运行 state（状态表征）", "确认结果中 hvg / latent_loadings / cluster_centroids 齐全"],
        )
    loadings = np.asarray(raw, dtype=float)
    if loadings.ndim != 2 or loadings.shape[0] == 0:
        raise err("prerequisite", "latent_loadings 形状非法",
                  shape=list(np.shape(raw)))

    n_rows = loadings.shape[0]
    hvg = _state_get(state, "hvg")
    if hvg is not None and len(hvg) != n_rows:
        raise err(
            "prerequisite",
            f"latent_loadings 行数({n_rows})与 hvg 长度({len(hvg)})不一致",
            how=["重跑 state 步骤以生成一致的 hvg / 载荷"],
        )
    if hvg is None:
        if n_rows == len(genes):
            hvg = genes
            warnings.append("state 未提供 hvg：latent_loadings 行数等于基因数，按全基因 PCA 处理")
        else:
            raise err(
                "prerequisite",
                f"缺少 hvg，且 latent_loadings 行数({n_rows})不等于基因数({len(genes)})",
            )
    hvg = [str(gene) for gene in hvg]

    lookup = _gene_lookup(genes)
    hvg_bundle_idx: list[int] = []
    hvg_rows: list[int] = []
    missing: list[str] = []
    for row, gene in enumerate(hvg):
        index = lookup(gene)
        if index is None:
            missing.append(gene)
        else:
            hvg_bundle_idx.append(index)
            hvg_rows.append(row)
    if missing:
        warnings.append(
            f"{len(missing)} 个 hvg 基因不在数据集基因列中，已忽略其载荷行："
            + ", ".join(missing[:6])
        )
    if not hvg_rows:
        raise err("prerequisite", "hvg 与数据集基因完全没有交集，无法投影到隐空间")

    n_latent = loadings.shape[1]
    if latent_dim and int(latent_dim) > 0:
        if int(latent_dim) > n_latent:
            warnings.append(
                f"latent_dim={int(latent_dim)} 超过 state 的 PC 数({n_latent})，按 {n_latent} 处理"
            )
        n_latent = min(int(latent_dim), n_latent)
    return loadings[:, :n_latent], hvg, np.asarray(hvg_bundle_idx, int), np.asarray(hvg_rows, int), n_latent


def _centroids_and_labels(state, n_latent: int, warnings: list[str]):
    """簇质心 + 簇名；缺失时用 latent + clusters 现算。"""
    raw = _state_get(state, "cluster_centroids")
    labels = _state_get(state, "cluster_labels")
    if raw is not None:
        centroids = np.asarray(raw, dtype=float)
        if centroids.ndim != 2:
            raise err("prerequisite", "cluster_centroids 形状非法",
                      shape=list(centroids.shape))
        if centroids.shape[1] < n_latent:
            raise err(
                "prerequisite",
                f"cluster_centroids 的维度({centroids.shape[1]})小于隐空间维度({n_latent})",
            )
        centroids = centroids[:, :n_latent]
    else:
        latent = _state_get(state, "latent")
        clusters = _state_get(state, "clusters")
        if latent is None or clusters is None:
            raise err("prerequisite", "缺少 cluster_centroids，且无法由 latent/clusters 现算",
                      how=["重跑 state 步骤"])
        latent = np.asarray(latent, dtype=float)
        clusters = np.asarray(clusters).astype(int).ravel()
        if latent.shape[0] != clusters.size:
            raise err("prerequisite", "latent 与 clusters 长度不一致")
        keys = np.unique(clusters)
        centroids = np.vstack([latent[clusters == key].mean(axis=0) for key in keys])
        centroids = centroids[:, :n_latent]
        if labels is None:
            labels = [str(key) for key in keys]
        warnings.append("state 未提供 cluster_centroids：已由 latent + clusters 现算均值质心")

    labels = [str(label) for label in labels] if labels is not None else []
    if len(labels) != centroids.shape[0]:
        if labels:
            warnings.append(
                f"cluster_labels 长度({len(labels)})与质心行数({centroids.shape[0]})不一致，已按索引重命名"
            )
        labels = [str(i) for i in range(centroids.shape[0])]
    return centroids, labels


# ---------------------------------------------------------------------------
# 扰动表规范化
# ---------------------------------------------------------------------------

def _coerce_table_frame(table) -> pd.DataFrame:
    """把 table 收敛成 DataFrame：DataFrame / CSV-TSV 路径 / dict / list[dict]。"""
    if isinstance(table, pd.DataFrame):
        return table.copy()
    if isinstance(table, str):
        if not os.path.exists(table):
            raise err("bad_request", f"扰动表路径不存在：{table}",
                      how=["检查路径", "或改用 attach kind=perturb_table 挂载"])
        try:
            if table.lower().endswith((".tsv", ".tab")):
                return pd.read_csv(table, sep="\t")
            return pd.read_csv(table, sep=None, engine="python")
        except Exception as exc:  # noqa: BLE001
            raise err("bad_request", f"扰动表读取失败：{exc}", path=table) from exc
    if isinstance(table, dict):
        try:
            return pd.DataFrame(table)
        except Exception as exc:  # noqa: BLE001
            raise err("bad_request", f"扰动表 dict 无法转成表：{exc}") from exc
    if isinstance(table, (list, tuple)):
        try:
            return pd.DataFrame(list(table))
        except Exception as exc:  # noqa: BLE001
            raise err("bad_request", f"扰动表列表无法转成表：{exc}") from exc
    raise err("bad_request", f"不支持的扰动表类型：{type(table).__name__}",
              how=["attach kind=perturb_table（CSV/TSV）", "或直接传 pandas.DataFrame"])


def _find_column(columns, names) -> str | None:
    lower = {}
    for column in columns:
        lower.setdefault(str(column).strip().lower(), column)
    for name in names:
        if name in lower:
            return lower[name]
    return None


def _prepare_table(table) -> tuple[pd.DataFrame, dict]:
    """把扰动表整理成「宽表 + 标准扰动列」。

    返回 (frame, meta)：
      frame: 含 ``__pert__`` 列（str），可能含 ``__cell__`` 列，其余为基因表达列；
      meta:  {format:'wide'|'long', gene_cols, cell_col, n_rows, notes, warnings}
    长表（gene, perturbation, value）会按 (perturbation, gene) 求均值透视成宽表。
    """
    frame = _coerce_table_frame(table)
    if frame.shape[0] == 0:
        return frame, {"format": "empty", "gene_cols": [], "cell_col": None,
                       "n_rows": 0, "notes": [], "warnings": ["扰动表为空（0 行）"]}

    notes: list[str] = []
    warns: list[str] = []

    pert_col = _find_column(frame.columns, _PERT_KEYS)
    gene_col = _find_column(frame.columns, _GENE_KEYS)
    value_col = _find_column(frame.columns, _VALUE_KEYS)
    cell_col = _find_column(frame.columns, _CELL_KEYS)

    is_long = (
        pert_col is not None
        and gene_col is not None
        and value_col is not None
        and str(gene_col) != str(pert_col)
        and str(value_col) != str(pert_col)
        and _is_numeric(frame[value_col])
    )
    if pert_col is None:
        raise err(
            "bad_request",
            "扰动表缺少 perturbation 列（或 condition/target 别名）",
            columns=[str(c) for c in frame.columns[:40]],
            how=["宽表：cell_id,perturbation,<gene 列...>", "长表：gene,perturbation,value"],
        )

    if is_long:
        work = frame[[pert_col, gene_col, value_col]].copy()
        work[gene_col] = work[gene_col].astype(str).str.strip()
        work = work.dropna(subset=[pert_col, gene_col, value_col])
        wide = (work.groupby([pert_col, gene_col], sort=False)[value_col]
                .mean().unstack())
        wide.index = wide.index.astype(str).str.strip()
        wide = wide.reset_index()
        wide = wide.rename(columns={pert_col: "__pert__"})
        gene_cols = [c for c in wide.columns if c != "__pert__"]
        n_gene_cols = max(1, len(gene_cols))
        counts = pd.Series(int(round(frame.shape[0] / n_gene_cols)),
                           index=wide["__pert__"].unique())
        notes.append(
            "识别为长表（gene/perturbation/value），已按 (perturbation, gene) 求均值透视成宽表"
        )
        if cell_col is not None and str(cell_col) in frame.columns:
            cell_counts = frame.dropna(subset=[pert_col]).groupby(pert_col).size()
            counts = pd.Series(
                {str(k).strip(): int(v) for k, v in cell_counts.items()}
            )
            notes.append("长表含 cell_id 列：按 cell 计数")
        else:
            notes.append(
                f"长表无 cell_id：按 行数/基因数 估算每扰动细胞数(≈{int(counts.iloc[0]) if len(counts) else 0})，"
                "min_pert_cells 过滤被跳过"
            )
        return wide, {"format": "long", "gene_cols": gene_cols,
                      "cell_col": None, "n_rows": int(frame.shape[0]),
                      "counts": counts, "notes": notes, "warnings": warns}

    # 宽表
    pert_series = frame[pert_col].astype(str).str.strip()
    keep = pert_series.notna() & (pert_series != "") & (pert_series.str.lower() != "nan")
    if not bool(keep.all()):
        warns.append(f"扰动表有 {int((~keep).sum())} 行扰动名为空，已丢弃")
    work = frame.loc[keep].copy()
    work["__pert__"] = pert_series[keep].to_numpy()
    if cell_col is not None:
        work["__cell__"] = work[cell_col].astype(str)
    excluded = {str(pert_col), "__pert__", "__cell__"}
    gene_cols = [c for c in work.columns if c not in excluded and _is_numeric(work[c])]
    if not gene_cols:
        raise err(
            "bad_request",
            "扰动表没有数值型基因表达列",
            columns=[str(c) for c in work.columns[:40]],
            how=["宽表需：cell_id,perturbation,<基因列...>", "长表需：gene,perturbation,value"],
        )
    counts = work.groupby("__pert__", sort=False).size()
    return work, {"format": "wide", "gene_cols": gene_cols,
                  "cell_col": "__cell__" if "__cell__" in work.columns else None,
                  "n_rows": int(work.shape[0]), "counts": counts,
                  "notes": notes, "warnings": warns}


# ---------------------------------------------------------------------------
# 扰动名 → 基因名 / 扰动类型
# ---------------------------------------------------------------------------

def _pert_gene_candidates(label) -> list[str]:
    """从扰动名枚举可能的基因名：KLF4 / KLF4_KO / KLF4-KD / gene:KLF4 / KLF4_KO_1。"""
    raw = str(label).strip()
    out: list[str] = []

    def push(value: str) -> None:
        value = value.strip().strip("+").strip()
        if value and value not in out:
            out.append(value)

    push(raw)
    if "+" in raw:
        push(raw.replace("+", ""))
    if ":" in raw:  # gene:KLF4 / KO:KLF4
        push(raw.split(":", 1)[1])
        push(raw.split(":", 1)[0]) if raw.split(":", 1)[0].upper() not in {"KO", "KD", "OE", "GENE", "SGRNA"} else None

    current = raw
    for _ in range(5):
        trimmed = _SUFFIX_PATTERN.sub("", current)
        trimmed = _TRAILING_INDEX.sub("", trimmed)
        trimmed = trimmed.strip("_- .+")
        if not trimmed or trimmed == current:
            break
        current = trimmed
        push(current)
    # "KLF4_KO_rep1" 这类多段后缀：逐段剥
    parts = re.split(r"[_\s]+", raw)
    while len(parts) > 1:
        parts = parts[:-1]
        push("_".join(parts))
    return out


def _resolve_pert_gene(label, lookup):
    """扰动名解析成 bundle 基因索引，解析不到返回 None。"""
    for candidate in _pert_gene_candidates(label):
        index = lookup(candidate)
        if index is not None:
            return int(index)
    return None


def _pert_mode(label) -> float:
    """扰动类型符号：过表达 +1，敲低/敲除 -1，未知 0。"""
    text = str(label)
    if "+" in text or _OE_PATTERN.search(text):
        return 1.0
    if _KO_PATTERN.search(text):
        return -1.0
    return 0.0


def _build_features(pert_names, lookup, x_ctrl_full, table_gene_idx):
    """逐扰动特征矩阵（n_pert × k），以及解析到的基因索引。"""
    n = len(pert_names)
    values = np.asarray(x_ctrl_full, dtype=float)
    present = sorted(table_gene_idx)
    present_vals = values[present] if present else values
    if present_vals.size:
        median = float(np.median(present_vals))
        iqr = float(np.percentile(present_vals, 75) - np.percentile(present_vals, 25))
        if not np.isfinite(iqr) or iqr < 1e-9:
            iqr = float(np.std(present_vals))
        if not np.isfinite(iqr) or iqr < 1e-9:
            iqr = 1.0
        order = np.sort(present_vals)
    else:
        median, iqr = 0.0, 1.0
        order = np.zeros(0)

    features = np.zeros((n, 4), dtype=float)
    resolved: list[int | None] = []
    for row, name in enumerate(pert_names):
        gene = _resolve_pert_gene(name, lookup)
        resolved.append(gene)
        if gene is not None and gene in table_gene_idx:
            value = float(values[gene])
            rank = float(np.searchsorted(order, value, side="right") / max(1, order.size))
        else:
            value, rank = median, 0.5
        z = (value - median) / iqr
        features[row] = (value, z, rank, _pert_mode(name))
    return features, resolved


# ---------------------------------------------------------------------------
# 统计小工具
# ---------------------------------------------------------------------------

def _r2_matrix(true, pred) -> float | None:
    """整体 R²（按矩阵展开算）：1 - SS_res / SS_tot；SS_tot≈0 返回 None。"""
    true = np.asarray(true, dtype=float)
    pred = np.asarray(pred, dtype=float)
    resid = float(np.sum((true - pred) ** 2))
    total = float(np.sum((true - true.mean()) ** 2))
    if not np.isfinite(total) or total <= 1e-12:
        return None
    value = 1.0 - resid / total
    return float(value) if np.isfinite(value) else None


def _r2_row(true_row, pred_row) -> float | None:
    true_row = np.asarray(true_row, dtype=float)
    pred_row = np.asarray(pred_row, dtype=float)
    resid = float(np.sum((true_row - pred_row) ** 2))
    total = float(np.sum((true_row - true_row.mean()) ** 2))
    if not np.isfinite(total) or total <= 1e-12:
        return None
    return float(1.0 - resid / total)


def _spearman(a, b) -> float | None:
    """秩相关（避免 scipy 常数输入告警；秩用平均秩）。"""
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    if a.size < 3 or a.size != b.size:
        return None
    if np.allclose(a, a[0]) or np.allclose(b, b[0]):
        return None
    rank_a = _rankdata(a)
    rank_b = _rankdata(b)
    da = rank_a - rank_a.mean()
    db = rank_b - rank_b.mean()
    denom = float(np.sqrt(np.sum(da ** 2) * np.sum(db ** 2)))
    if denom < 1e-12:
        return None
    value = float(np.sum(da * db) / denom)
    return value if np.isfinite(value) else None


def _rankdata(values: np.ndarray) -> np.ndarray:
    """平均秩（ties 取均值），与 scipy.stats.rankdata 一致。"""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    sorted_vals = values[order]
    i = 0
    while i < values.size:
        j = i + 1
        while j < values.size and sorted_vals[j] == sorted_vals[i]:
            j += 1
        ranks[order[i:j]] = 0.5 * (i + j - 1) + 1.0
        i = j
    return ranks


def _bh_adjust(pvalues) -> np.ndarray:
    """Benjamini-Hochberg FDR。"""
    p = np.asarray(pvalues, dtype=float)
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order]
    q = ranked * n / (np.arange(n) + 1.0)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(n, dtype=float)
    out[order] = np.minimum(q, 1.0)
    return out


def _hypergeom_ora(query_genes, universe_size: int, pathway_sets: dict,
                   universe_set: set, direction: str) -> list[dict]:
    """单方向超几何 ORA；返回未做 BH 的原始行。"""
    from scipy.stats import hypergeom

    query = [gene for gene in query_genes if gene in universe_set]
    query = list(dict.fromkeys(query))
    if not query or universe_size <= 0:
        return []
    query_set = set(query)
    rows: list[dict] = []
    for term, members in pathway_sets.items():
        term_set = {str(gene) for gene in members} & universe_set
        if not term_set:
            continue
        overlap = query_set & term_set
        if not overlap:
            continue
        k = len(overlap)
        try:
            pvalue = float(hypergeom.sf(k - 1, universe_size, len(term_set), len(query_set)))
        except Exception:  # noqa: BLE001
            continue
        if not np.isfinite(pvalue):
            continue
        rows.append({
            "term": str(term),
            "direction": direction,
            "query_size": len(query_set),
            "term_size": len(term_set),
            "overlap": k,
            "genes": sorted(overlap)[:10],
            "p": pvalue,
        })
    return rows


# ---------------------------------------------------------------------------
# Ridge 回归（sklearn RidgeCV，缺库时 numpy 闭式解兜底）
# ---------------------------------------------------------------------------

_SKLEARN_STATE = {"tried": False, "ridge": None}


def _ridge_class():
    if _SKLEARN_STATE["tried"]:
        return _SKLEARN_STATE["ridge"]
    _SKLEARN_STATE["tried"] = True
    try:
        from sklearn.linear_model import RidgeCV

        _SKLEARN_STATE["ridge"] = RidgeCV
    except Exception:  # noqa: BLE001
        _SKLEARN_STATE["ridge"] = None
    return _SKLEARN_STATE["ridge"]


def _standardize_features(features: np.ndarray):
    """中位数/IQR 稳健标准化（复用 vc_common.robust_scale），返回 (scaled, center, scale)。"""
    scaled, center, scale = common.robust_scale(np.asarray(features, dtype=float), axis=0)
    return np.asarray(scaled, dtype=float), np.ravel(center).astype(float), np.ravel(scale).astype(float)


def _ridge_fit(features: np.ndarray, targets: np.ndarray):
    """拟合多输出 ridge；返回 (w, b, alpha, backend)。features 已标准化。"""
    X = np.asarray(features, dtype=float)
    Y = np.asarray(targets, dtype=float)
    RidgeCV = _ridge_class()
    if RidgeCV is not None:
        try:
            model = RidgeCV(alphas=np.logspace(-3, 2, 10), fit_intercept=True)
            model.fit(X, Y)
            coef = np.asarray(model.coef_, dtype=float)
            if coef.ndim == 1:
                coef = coef.reshape(1, -1)
            w = coef.T
            intercept = np.asarray(model.intercept_, dtype=float)
            b = np.atleast_1d(intercept).ravel()
            alpha = np.asarray(model.alpha_, dtype=float).ravel()
            return w, b, float(alpha[0]) if alpha.size else 1.0, "sklearn"
        except Exception:  # noqa: BLE001  兜底到闭式解
            pass

    alpha = 1.0
    Xc = X - X.mean(axis=0, keepdims=True)
    Yc = Y - Y.mean(axis=0, keepdims=True)
    dim = Xc.shape[1]
    gram = Xc.T @ Xc + alpha * np.eye(dim)
    try:
        w = np.linalg.solve(gram, Xc.T @ Yc)
    except np.linalg.LinAlgError:
        w = np.linalg.pinv(gram) @ (Xc.T @ Yc)
    b = Y.mean(axis=0) - X.mean(axis=0) @ w
    return w, np.atleast_1d(b).ravel(), alpha, "numpy"


def _make_folds(n_pert: int, nfold: int, seed: int) -> tuple[list[tuple[np.ndarray, np.ndarray]], str]:
    """交叉验证折：扰动少(≤10)用留一，否则 nfold 折 k-fold（固定种子）。"""
    if n_pert <= 10 or nfold < 2 or nfold >= n_pert:
        folds = [
            (np.array([j for j in range(n_pert) if j != i], dtype=int), np.array([i], dtype=int))
            for i in range(n_pert)
        ]
        return folds, "loo"
    rng = np.random.default_rng(int(seed))
    order = rng.permutation(n_pert)
    folds = []
    for k in range(nfold):
        test = order[k::nfold]
        train = np.setdiff1d(order, test, assume_unique=True)
        if test.size == 0 or train.size == 0:
            continue
        folds.append((train.astype(int), test.astype(int)))
    if not folds:
        return _make_folds(n_pert, 0, seed)
    return folds, f"{len(folds)}fold"


# ---------------------------------------------------------------------------
# run_response
# ---------------------------------------------------------------------------

def run_response(bundle, state: dict, table, params: dict) -> dict:
    """扰动响应预测（CONTRACT §7.4）。

    参数
    ----
    bundle : duck object
        数据集鸭子对象（需要 ``.genes``；``.cells/.obs/.truth`` 可选）。
    state : dict
        ``run_state`` 的返回：``arrays.latent``、``arrays.clusters``、
        ``latent_loadings``(n_hvg×n_pcs)、``hvg``、``genes``、``cluster_labels``、
        ``cluster_centroids``（缺 centroid 时可由 latent+clusters 现算）。
    table : pandas.DataFrame | str | dict | None
        扰动表。宽表 ``cell_id, perturbation, <gene 列...>``；长表
        ``gene, perturbation, value``。为 None 时按参数尝试签名投影。
    params : dict
        ``data='auto'``、``method='additive'``、``nfold=5``、``latent_dim=0``、
        ``min_pert_cells=20``、``seed=0``、``signature=None``。

    返回
    ----
    dict（CONTRACT §6.5 统一形状）::

        arrays: pred(n_pert×n_latent), true(n_pert×n_latent), baseline(同形状),
                per_gene_err(n_genes,), delta_gene(n_pert×n_genes),
                hvg_bundle_idx(n_hvg_used,)
        per_pert: [{perturbation, r2, spearman, top_shift_genes, ...}]
        metrics: {method, n_pert, n_cells, r2, spearman, baseline_r2,
                  beats_baseline, latent_dim, ...}
        tables: response_per_pert, response_pred_true
        figures: [{'kind':'response_scatter'}]

    说明
    ----
    * 方法：隐空间加性模型 ``Δ_latent ≈ μ + W·f(control 基线)``，RidgeCV 内层选 alpha；
      与"均值位移基线"对比，``beats_baseline`` 为严格大于（真实 CV R²）。
    * 逐扰动 top 基因：``top_shift_genes`` 是**观测** Δ_gene 的 top5（真实值）；
      ``top_overlap`` 是模型预测 top5（HVG 子空间内）与观测 top5 的重合比例。
    * 样本不足（有效扰动 <5）时降级为均值位移 + 描述性结果，并写 warning。
    """
    params = _expanded_params(params)
    method = str(params.get("method") or "additive").strip().lower()
    if method not in SUPPORTED_METHODS:
        raise err(
            "bad_request",
            f"未知 method：{method}",
            supported=list(SUPPORTED_METHODS),
            how=["method='additive'（默认，岭回归）", "method='mean_shift'（只给均值位移基线）"],
        )
    if method == "mean":
        method = "mean_shift"
    latent_dim = _int_param(params, "latent_dim", 0, lo=0)
    nfold = _int_param(params, "nfold", 5, lo=2)
    min_pert_cells = _int_param(params, "min_pert_cells", 20, lo=0)
    seed = _int_param(params, "seed", 0)

    progress("响应预测：读取扰动表…", 6)

    # ---- 0. 扰动表解析 + 无表降级 ---------------------------------------
    resolved_table = table
    resolve_notes: list[str] = []
    if resolved_table is None:
        resolved_table = _table_from_auto_source(bundle, params, resolve_notes)
    if resolved_table is None:
        return _degrade_to_signature(bundle, state, params, resolve_notes)

    frame, meta = _prepare_table(resolved_table)
    if frame.shape[0] == 0:
        return _degrade_to_signature(bundle, state, params, ["扰动表为空（0 行），已尝试签名投影"])

    warnings: list[str] = list(resolve_notes) + list(meta.get("warnings") or [])
    notes: list[str] = list(meta.get("notes") or [])

    genes = _genes_of(bundle, state)
    lookup = _gene_lookup(genes)
    n_genes = len(genes)

    # ---- 1. 基因列对齐 ------------------------------------------------
    progress("响应预测：对齐基因与分组…", 18)
    gene_cols = list(meta["gene_cols"])
    mapped: list[tuple[str, int]] = []
    ignored: list[str] = []
    for column in gene_cols:
        index = lookup(column)
        if index is None:
            ignored.append(str(column))
        else:
            mapped.append((column, index))
    if not mapped:
        raise err(
            "prerequisite",
            "扰动表的基因列与数据集基因没有交集，无法计算扰动响应",
            n_table_columns=len(gene_cols),
            n_dataset_genes=n_genes,
            examples_table=[str(c) for c in gene_cols[:6]],
            examples_dataset=genes[:6],
            how=["确认扰动表的基因列名与数据集一致（大小写不敏感）",
                 "或先用 attach 提供与数据集同源的扰动表"],
        )
    if ignored:
        warnings.append(
            f"{len(ignored)} 个数值列不是数据集基因，已忽略："
            + ", ".join(ignored[:8]) + ("…" if len(ignored) > 8 else "")
        )
    used_cols = [column for column, _ in mapped]
    bundle_idx = np.asarray([index for _, index in mapped], dtype=int)
    table_gene_idx = set(int(i) for i in bundle_idx)

    # ---- 2. 逐扰动均值 + Δ --------------------------------------------
    progress("响应预测：计算 control / 扰动均值…", 30)
    try:
        grouped = frame.groupby("__pert__", sort=False)[used_cols].mean()
        counts = frame.groupby("__pert__", sort=False).size()
    except Exception as exc:  # noqa: BLE001
        raise err("internal", f"扰动表分组失败：{exc}") from exc

    n_rows = int(frame.shape[0])
    control_labels = [label for label in grouped.index if _is_control(label)]
    if control_labels:
        control_rows = grouped.loc[control_labels]
        control_weights = counts.loc[control_labels].to_numpy(dtype=float)
        if control_weights.sum() > 0:
            ctrl_values = np.average(control_rows.to_numpy(dtype=float),
                                     axis=0, weights=control_weights)
        else:  # pragma: no cover - 权重异常兜底
            ctrl_values = control_rows.to_numpy(dtype=float).mean(axis=0)
        n_ctrl_cells = int(counts.loc[control_labels].sum())
    else:
        ctrl_values = grouped.to_numpy(dtype=float).mean(axis=0)
        n_ctrl_cells = n_rows
        warnings.append(
            "扰动表没有可识别的 control 行（control/WT/NC/non-targeting）："
            "已用全表总均值当伪 control，Δ 的绝对幅度会被低估"
        )
        notes.append("伪 control = 全表总均值；建议在扰动表中保留 control 组再重跑")

    pert_labels = sorted(str(label) for label in grouped.index if not _is_control(label))
    if not pert_labels:
        raise err(
            "prerequisite",
            "扰动表只有 control 组，没有可预测的扰动",
            n_control_cells=n_ctrl_cells,
            how=["扰动表至少需要 1 个非 control 的 perturbation 组",
                 "或改用 attach kind=drug_signature + 签名投影"],
        )

    # min_pert_cells 过滤（长表无 cell_id 时跳过，只有估算值）
    counts_known = meta.get("format") == "wide" or meta.get("cell_col") is not None
    if min_pert_cells > 0 and counts_known:
        kept, dropped = [], []
        for label in pert_labels:
            n_cells_label = int(counts.get(label, meta.get("counts", pd.Series()).get(label, 0)) or 0)
            (kept if n_cells_label >= min_pert_cells else dropped).append((label, n_cells_label))
        if dropped and len(kept) >= 2:
            warnings.append(
                f"{len(dropped)} 个扰动细胞数 < min_pert_cells={min_pert_cells} 已剔除："
                + ", ".join(f"{name}({n})" for name, n in dropped[:8])
            )
            pert_labels = [label for label, _ in kept]
        elif dropped:
            warnings.append(
                f"{len(dropped)} 个扰动细胞数 < min_pert_cells={min_pert_cells}，"
                "但剔除后不足 2 个扰动，已保留全部（均值噪声可能较大）"
            )
    elif min_pert_cells > 0:
        notes.append("长表无 cell_id：跳过 min_pert_cells 过滤（细胞数只能估算）")

    n_pert = len(pert_labels)
    x_pert = grouped.loc[pert_labels, used_cols].to_numpy(dtype=float)
    delta_table = x_pert - ctrl_values.reshape(1, -1)

    # ---- 3. 基因空间 Δ（bundle.genes 顺序，缺失填 0）-------------------
    progress("响应预测：构建基因空间 Δ…", 42)
    delta_gene = np.zeros((n_pert, n_genes), dtype=float)
    x_ctrl_full = np.zeros(n_genes, dtype=float)
    if np.unique(bundle_idx).size == bundle_idx.size:
        delta_gene[:, bundle_idx] = delta_table
        x_ctrl_full[bundle_idx] = ctrl_values
    else:  # 罕见：两个列名映射到同一基因（大小写变体）
        counts_dup = np.zeros(n_genes, dtype=float)
        for j, gidx in enumerate(bundle_idx):
            delta_gene[:, gidx] += delta_table[:, j]
            x_ctrl_full[gidx] += ctrl_values[j]
            counts_dup[gidx] += 1
        has = counts_dup > 0
        delta_gene[:, has] /= counts_dup[has].reshape(1, -1)
        x_ctrl_full[has] /= counts_dup[has]
        warnings.append("扰动表存在大小写重复的基因列，已按均值合并")

    # ---- 4. 投影到隐空间 ----------------------------------------------
    progress("响应预测：投影到隐空间…", 54)
    loadings, hvg, hvg_bundle_idx, hvg_rows, n_latent = _load_latent_frame(
        state, genes, latent_dim, warnings
    )
    missing_hvg_in_table = int(np.sum(~np.isin(hvg_bundle_idx, bundle_idx)))
    if missing_hvg_in_table:
        warnings.append(
            f"{missing_hvg_in_table}/{len(hvg_bundle_idx)} 个 hvg 基因不在扰动表中，"
            "其 Δ 按 0 投影（隐空间响应幅度会被低估）"
        )
    # Δ_gene_hvg (n_pert × n_hvg_used) · loadings (n_hvg_used × n_latent)
    delta_latent = delta_gene[:, hvg_bundle_idx] @ loadings[hvg_rows]

    # ---- 5. 特征 + 交叉验证 -------------------------------------------
    progress("响应预测：训练加性模型…", 66)
    n_cells_used = n_ctrl_cells + int(
        sum(int(counts.get(label, 0)) for label in pert_labels)
    ) if counts_known else n_ctrl_cells

    raw_features, resolved_genes = _build_features(
        pert_labels, lookup, x_ctrl_full, table_gene_idx
    )
    n_features = min(4, max(1, n_pert - 3))
    features = raw_features[:, :n_features]
    feature_names = ["ctrl_expr", "ctrl_expr_z", "ctrl_expr_rank", "pert_mode"][:n_features]
    n_resolved = int(sum(1 for gene in resolved_genes if gene is not None))

    cv_pred = None
    cv_base = None
    cv_name = "descriptive"
    ridge_alpha = None
    ridge_backend = None
    beat_possible = False

    effective_method = "mean_shift" if method == "mean_shift" else "additive"
    if effective_method == "additive" and n_pert < MIN_TRAIN_PERT:
        warnings.append(
            f"有效扰动数 {n_pert} < {MIN_TRAIN_PERT}：不足以训练，仅给描述性结果"
            "（method 降级为 mean_shift，只报告均值位移与逐扰动观测）"
        )
        notes.append("CONTRACT §7.4：数据不足（<3 扰动）时只做均值位移 + unsupported 提示；"
                     "本实现把训练门槛设为 5 个扰动以降低过拟合风险")
        effective_method = "mean_shift"
    if n_resolved == 0 and effective_method == "additive":
        warnings.append(
            "扰动名未能解析到任何数据集基因（支持 KLF4 / KLF4_KO / KLF4-OE 等写法）："
            "基线特征无变异，模型退化为均值位移"
        )
        effective_method = "mean_shift"

    if n_pert >= 2 and effective_method == "additive":
        folds, cv_name = _make_folds(n_pert, nfold, seed)
        scaled_features, _, _ = _standardize_features(features)
        if float(np.var(scaled_features)) < 1e-12:
            warnings.append("基线特征方差≈0（扰动名未解析到基因？）：模型退化为均值位移")
            effective_method = "mean_shift"
        else:
            cv_pred = np.zeros_like(delta_latent)
            cv_base = np.zeros_like(delta_latent)
            for train, test in folds:
                train_mean = delta_latent[train].mean(axis=0)
                cv_base[test] = train_mean
                try:
                    weights, intercept, alpha, backend = _ridge_fit(
                        scaled_features[train], delta_latent[train] - train_mean
                    )
                except Exception as exc:  # noqa: BLE001  拟合失败 → 该折退化为均值
                    warnings.append(f"某一折 Ridge 拟合失败，已退化为均值位移：{exc}")
                    cv_pred[test] = train_mean
                    continue
                ridge_alpha = alpha if ridge_alpha is None else ridge_alpha
                ridge_backend = backend
                cv_pred[test] = train_mean + scaled_features[test] @ weights + intercept
            beat_possible = True

    if cv_pred is None:
        # 均值位移（或单扰动描述性）：LOO 均值基线 = 预测
        cv_name = "loo_mean_shift" if n_pert >= 2 else "descriptive"
        cv_pred = np.zeros_like(delta_latent)
        cv_base = np.zeros_like(delta_latent)
        if n_pert >= 2:
            for index in range(n_pert):
                mask = np.arange(n_pert) != index
                cv_pred[index] = delta_latent[mask].mean(axis=0)
                cv_base[index] = cv_pred[index]
        else:  # 只有一个扰动：没有折，直接报告观测本身（描述性，不参与打分）
            cv_pred[0] = delta_latent[0]
            cv_base[0] = delta_latent[0]
            notes.append("只有 1 个扰动：无交叉验证，pred 即观测值，R² 不报告")
        if effective_method == "additive":
            effective_method = "mean_shift"

    cv_pred = _finite_array(cv_pred)
    cv_base = _finite_array(cv_base)
    delta_latent = _finite_array(delta_latent)

    r2 = _r2_matrix(delta_latent, cv_pred) if n_pert >= 2 else None
    baseline_r2 = _r2_matrix(delta_latent, cv_base) if n_pert >= 2 else None
    beats_baseline = bool(
        r2 is not None and baseline_r2 is not None and r2 > baseline_r2 + 1e-9
    )
    spearman_values = [
        _spearman(delta_latent[i], cv_pred[i]) for i in range(n_pert)
    ]
    spearman_values = [value for value in spearman_values if value is not None]
    spearman = float(np.mean(spearman_values)) if spearman_values else None
    baseline_spearman = [
        _spearman(delta_latent[i], cv_base[i]) for i in range(n_pert)
    ]
    baseline_spearman = [value for value in baseline_spearman if value is not None]
    baseline_spearman = float(np.mean(baseline_spearman)) if baseline_spearman else None

    # ---- 6. 基因空间逆投影 + 误差诊断 ----------------------------------
    progress("响应预测：逆投影与误差统计…", 80)
    per_gene_err, inverse_ratio = _gene_space_diagnostics(
        delta_gene, cv_pred, loadings, hvg_bundle_idx, hvg_rows, n_genes
    )

    # ---- 7. 每扰动明细 ------------------------------------------------
    top_shift_genes: list[list[str]] = []
    top_overlaps: list[float | None] = []
    per_pert: list[dict] = []
    for index, label in enumerate(pert_labels):
        observed_top = common.top_indices(delta_gene[index], 5)
        top_genes = [genes[int(g)] for g in observed_top]
        top_shift_genes.append(top_genes)
        predicted_top = common.top_indices(_inverse_gene_prediction(
            cv_pred[index], loadings, hvg_rows, 5
        )[0], 5)
        overlap = _top_overlap(observed_top, predicted_top)
        top_overlaps.append(overlap)
        row_spearman = _spearman(delta_latent[index], cv_pred[index])
        row_cos = _cosine(delta_latent[index], cv_pred[index])
        per_pert.append({
            "perturbation": str(label),
            "r2": fnum(_r2_row(delta_latent[index], cv_pred[index]), 4),
            "spearman": fnum(row_spearman, 4),
            "cosine": fnum(row_cos, 4),
            "top_shift_genes": top_genes,
            "predicted_top_genes": _predicted_top_genes(
                cv_pred[index], loadings, hvg, hvg_rows, 5
            ),
            "top_overlap": fnum(overlap, 4),
            "n_cells": int(counts.get(label, 0)) if counts_known else None,
            "delta_norm": fnum(float(np.linalg.norm(delta_latent[index])), 4),
            "resolved_gene": None if resolved_genes[index] is None else genes[resolved_genes[index]],
        })
    valid_overlaps = [value for value in top_overlaps if value is not None]
    overlap_mean = float(np.mean(valid_overlaps)) if valid_overlaps else None

    # ---- 8. 组装返回 --------------------------------------------------
    progress("响应预测：整理表与结论…", 92)
    per_pert_table = pd.DataFrame(per_pert).drop(columns=["predicted_top_genes"], errors="ignore")
    per_pert_table["top_shift_genes"] = [",".join(item) for item in top_shift_genes]
    pred_true_table = _pred_true_table(
        pert_labels, delta_latent, cv_pred, cv_base
    )

    r2_text = "不可计算" if r2 is None else f"{r2:.3f}"
    base_text = "不可计算" if baseline_r2 is None else f"{baseline_r2:.3f}"
    beat_text = "是" if beats_baseline else ("否" if r2 is not None else "无法比较")
    sp_text = "不可计算" if spearman is None else f"{spearman:.3f}"
    ov_text = "不可计算" if overlap_mean is None else f"{overlap_mean:.0%}"
    summary = (
        f"{n_pert} 个扰动（control {n_ctrl_cells} 细胞，共 {n_cells_used} 细胞）："
        f"{cv_name} 交叉验证 R²={r2_text}（均值位移基线 {base_text}，优于基线：{beat_text}）；"
        f"Spearman={sp_text}；HVG 空间 top-5 基因重合度 {ov_text}；"
        f"方法={effective_method}，隐空间维度={n_latent}"
    )

    if effective_method == "mean_shift" and n_pert >= MIN_TRAIN_PERT:
        warnings.append("已显式选择 mean_shift：只报告均值位移，不训练线性模型")
    if r2 is not None and r2 < 0:
        warnings.append(f"R²={r2:.3f} 为负：模型预测不如直接取均值，扰动间响应模式差异无法由 control 基线解释")
    if n_pert >= MIN_TRAIN_PERT and not beats_baseline and r2 is not None:
        warnings.append("加性模型未超过均值位移基线：未见扰动的响应更可能由「共享平均位移」主导，"
                        "不要据此宣称可泛化预测")
    notes.append(
        "隐空间 Δ 由 Δ_gene[hvg]·latent_loadings 投影得到；若 state 的 PCA 基于标准化表达，"
        "隐空间量纲与原始表达不同（pred/true 的 R²/Spearman 仍在同一空间内自洽）"
    )
    notes.append(
        "per_gene_err 由隐空间最小范数逆投影 + 全局尺度对齐得到（仅诊断用；"
        "模型只在 HVG 子空间有表达能力，非 HVG 基因的误差记为 0）"
    )
    notes.append("top-5 重合度在 HVG 子空间内计算，因为线性隐空间模型只预测 HVG 基因")
    if ignored:
        notes.append(f"被忽略的非基因数值列（前 8）：{ignored[:8]}")

    metrics = {
        "method": str(effective_method),
        "requested_method": str(method),
        "n_pert": int(n_pert),
        "n_cells": int(n_cells_used),
        "n_control_cells": int(n_ctrl_cells),
        "r2": fnum(r2, 4),
        "spearman": fnum(spearman, 4),
        "baseline_r2": fnum(baseline_r2, 4),
        "baseline_spearman": fnum(baseline_spearman, 4),
        "beats_baseline": beats_baseline,
        "latent_dim": int(n_latent),
        "cv": str(cv_name),
        "n_features": int(n_features),
        "feature_names": feature_names,
        "n_pert_genes_resolved": int(n_resolved),
        "ridge_alpha": fnum(ridge_alpha, 6),
        "ridge_backend": ridge_backend,
        "top_overlap": fnum(overlap_mean, 4),
        "n_genes": int(n_genes),
        "n_hvg_used": int(len(hvg_bundle_idx)),
        "n_genes_matched": int(len(bundle_idx)),
        "n_ignored_columns": int(len(ignored)),
        "inverse_scale": fnum(inverse_ratio, 6),
        "per_gene_err_mean": fnum(float(np.mean(per_gene_err)) if per_gene_err.size else None, 6),
        "data_format": str(meta.get("format")),
        "mode": "perturb_table",
    }

    arrays = {
        "pred": np.asarray(cv_pred, dtype=np.float32),
        "true": np.asarray(delta_latent, dtype=np.float32),
        "baseline": np.asarray(cv_base, dtype=np.float32),
        "per_gene_err": np.asarray(per_gene_err, dtype=np.float32),
        "delta_gene": np.asarray(delta_gene, dtype=np.float32),
        "hvg_bundle_idx": np.asarray(hvg_bundle_idx, dtype=np.int32),
        "feature_matrix": np.asarray(features, dtype=np.float32),
    }

    result = {
        "ok": True,
        "step": "response",
        "mode": "perturb_table",
        "params": params,
        "arrays": arrays,
        "per_pert": per_pert,
        "metrics": metrics,
        "tables": [
            {"id": "response_per_pert", "title": "逐扰动响应预测质量", "rows": per_pert_table},
            {"id": "response_pred_true", "title": "预测 vs 真实（隐空间 Δ）", "rows": pred_true_table},
        ],
        "figures": [
            {"kind": "response_scatter", "title": "扰动响应：预测 vs 真实（隐空间 Δ）",
             "params": {"x": "true", "y": "pred"}},
        ],
        "summary": summary,
        "warnings": warnings,
        "notes": notes,
    }
    progress("响应预测完成", 100)
    return result


def _table_from_auto_source(bundle, params: dict, notes: list[str]):
    """data=auto 时兜底：bundle.truth['perturb_table'] 或 params['data'] 里的表。"""
    data = params.get("data")
    if isinstance(data, (pd.DataFrame, str, dict, list, tuple)):
        notes.append("table 未直接传入，已使用 params['data'] 提供的扰动表")
        return data
    truth = getattr(bundle, "truth", None) if bundle is not None else None
    if isinstance(truth, dict) and truth.get("perturb_table") is not None:
        notes.append("table 未直接传入，已使用 bundle.truth['perturb_table']（demo 真值扰动表）")
        return truth["perturb_table"]
    return None


def _degrade_to_signature(bundle, state: dict, params: dict, notes: list[str]) -> dict:
    """无扰动表：用 params['signature'] 做签名投影，否则抛 prerequisite。"""
    signature = None
    for key in ("signature", "drug_signature", "gene_signature", "sig"):
        if params.get(key) is not None:
            signature = params[key]
            break
    if signature is None:
        raise err(
            "prerequisite",
            "没有扰动表：需先 attach kind=perturb_table 或 kind=drug_signature",
            data_param=params.get("data"),
            how=[
                "attach kind=perturb_table（CSV/TSV：cell_id,perturbation,<基因列>）后重跑 response",
                "或 attach kind=drug_signature（CSV：gene,logFC）后重跑 response",
                "或直接传 params: {'signature': {'name':…, 'genes':[…], 'logfc':[…]}}",
            ],
        )
    result = project_signature(bundle, state, signature, params)
    result["step"] = "response"
    result["mode"] = "signature_projection"
    result["degraded"] = True
    result["metrics"]["mode"] = "signature_projection"
    result["metrics"]["degraded"] = True
    result["warnings"] = list(result.get("warnings") or []) + [
        "未提供扰动表：response 已自动降级为签名投影（CONTRACT §7.4 降级路径）",
    ]
    result["notes"] = list(result.get("notes") or []) + list(notes)
    result["summary"] = "未提供扰动表，已降级为签名投影。" + str(result.get("summary") or "")
    return result


def _inverse_gene_prediction(latent_vector, loadings: np.ndarray, hvg_rows: np.ndarray,
                             top_n: int):
    """隐空间向量 → HVG 基因空间（最小范数逆投影），返回 (gene_values, hvg_rows)。"""
    latent = np.asarray(latent_vector, dtype=float).reshape(1, -1)
    used = loadings[hvg_rows]
    try:
        pinv = np.linalg.pinv(used)
        genes = (latent @ pinv).ravel()
    except Exception:  # noqa: BLE001
        genes = np.zeros(used.shape[0], dtype=float)
    return genes, hvg_rows


def _predicted_top_genes(latent_vector, loadings, hvg_names, hvg_rows, top_n: int):
    values, _ = _inverse_gene_prediction(latent_vector, loadings, hvg_rows, top_n)
    order = common.top_indices(values, top_n)
    return [str(hvg_names[int(hvg_rows[int(i)])]) if int(hvg_rows[int(i)]) < len(hvg_names)
            else "" for i in order]


def _top_overlap(observed_top_idx, predicted_top_idx) -> float | None:
    """HVG 子空间 top-5 重合度（两者都是 HVG 行内索引时才有意义）。"""
    if observed_top_idx is None or predicted_top_idx is None:
        return None
    a = set(int(v) for v in np.ravel(observed_top_idx))
    b = set(int(v) for v in np.ravel(predicted_top_idx))
    if len(a) != 5 or len(b) != 5:
        return None
    return len(a & b) / 5.0


def _gene_space_diagnostics(delta_gene, cv_pred, loadings, hvg_bundle_idx, hvg_rows,
                            n_genes):
    """逆投影到基因空间，返回 (per_gene_err, ratio)。"""
    per_gene_err = np.zeros(n_genes, dtype=float)
    used = loadings[hvg_rows]
    try:
        pinv = np.linalg.pinv(used)  # (n_latent, n_hvg_used)
        pred_hvg = np.asarray(cv_pred, dtype=float) @ pinv  # (n_pert, n_hvg_used)
    except Exception:  # noqa: BLE001
        return per_gene_err, 0.0
    observed = np.asarray(delta_gene, dtype=float)[:, hvg_bundle_idx]
    num = float(np.linalg.norm(observed))
    den = float(np.linalg.norm(pred_hvg))
    ratio = num / den if den > 1e-12 else 0.0
    if not np.isfinite(ratio):
        ratio = 0.0
    aligned = pred_hvg * ratio
    errors = np.abs(aligned - observed).mean(axis=0)
    per_gene_err[hvg_bundle_idx] = errors
    return per_gene_err, ratio


def _cosine(a, b) -> float | None:
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-12:
        return None
    value = float(np.dot(a, b) / denom)
    return value if np.isfinite(value) else None


def _pred_true_table(pert_labels, delta_latent, cv_pred, cv_base) -> pd.DataFrame:
    rows = []
    for index, label in enumerate(pert_labels):
        for dim in range(delta_latent.shape[1]):
            rows.append({
                "perturbation": str(label),
                "dim": int(dim),
                "true": float(delta_latent[index, dim]),
                "pred": float(cv_pred[index, dim]),
                "baseline": float(cv_base[index, dim]),
            })
    return pd.DataFrame(rows, columns=["perturbation", "dim", "true", "pred", "baseline"])


# ---------------------------------------------------------------------------
# 签名投影
# ---------------------------------------------------------------------------

def project_signature(bundle, state: dict, signature, params: dict) -> dict:
    """把药物/基因签名（gene, logFC）投到状态空间并打分（CONTRACT §7.4）。

    参数
    ----
    bundle : duck object
        需要 ``.genes``。
    state : dict
        ``run_state`` 的返回（latent_loadings / hvg / cluster_centroids / cluster_labels）。
    signature : dict | DataFrame | str
        ``{'name':str, 'genes':[str], 'logfc':[float]}``、DataFrame[gene, logFC]、
        ``{gene: logfc}`` 映射、``[[gene, logfc], …]``、或 CSV 路径。
    params : dict
        可含 ``latent_dim``（默认 0 = 用 state 全部 PC）。

    返回
    ----
    dict::

        arrays: signature_latent(n_pcs,), cluster_cos(k,), cell_score(n_cells,)  # cell_score 可选
        per_cluster: [{cluster, cos, direction}]
        enrichment: [{term, direction, query_size, term_size, overlap, genes, p, fdr}]
        metrics: {n_genes_matched, top_cluster, max_abs_cos, ...}
        tables: signature_clusters, signature_enrichment
        figures: [{'kind':'compare_bar','params':{'items':['cluster_cos']}}]

    打分口径
    --------
    * ``cos = cosine(s_latent, 簇质心)``：CONTRACT 里的 connectivity score。
      正值 = 该簇沿签名方向被"激活"（签名 up 基因在该簇更高），负值 = 被抑制；
      符号翻转对照为 ``-cos``，因此 connectivity 与余弦等价，``direction`` 直接取符号。
    * 富集：签名 top100 up / down 基因分别对 ``vc_priors.PATHWAY_SETS`` 做超几何 ORA，
      universe = 数据集全部基因（保守），BH 校正。
    """
    params = dict(DEFAULTS, **(_param_dict(params)))
    latent_dim = _int_param(params, "latent_dim", 0, lo=0)
    warnings: list[str] = []
    notes: list[str] = []

    progress("签名投影：解析签名…", 10)
    genes = _genes_of(bundle, state)
    n_genes = len(genes)
    lookup = _gene_lookup(genes)
    sig = _normalize_signature(signature)
    sig_name = str(sig.get("name") or params.get("signature_name") or "signature")

    # ---- 1. 基因空间向量 ----------------------------------------------
    logfc = np.asarray(sig["logfc"], dtype=float)
    sig_vec = np.zeros(n_genes, dtype=float)
    matched = 0
    missing: list[str] = []
    matched_genes: list[str] = []
    for gene, value in zip(sig["genes"], logfc):
        index = lookup(gene)
        if index is None:
            missing.append(str(gene))
            continue
        sig_vec[index] += float(value)  # 重复基因累加
        matched += 1
        matched_genes.append(genes[index])
    if matched == 0:
        raise err(
            "prerequisite",
            "签名基因与数据集基因没有交集，无法投影",
            n_signature_genes=len(sig["genes"]),
            examples_signature=[str(g) for g in sig["genes"][:6]],
            examples_dataset=genes[:6],
            how=["确认签名使用与数据集一致的基因符号（大小写不敏感）",
                 "或换用覆盖当前数据集的签名"],
        )
    if missing:
        warnings.append(
            f"签名有 {len(missing)}/{len(sig['genes'])} 个基因不在数据集中，按 0 处理："
            + ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        )
    if not np.any(np.abs(sig_vec) > 1e-12):
        raise err("bad_request", "签名 logFC 全为 0，无法定义方向")

    # ---- 2. 投影到隐空间 ----------------------------------------------
    progress("签名投影：投影到隐空间…", 30)
    loadings, hvg, hvg_bundle_idx, hvg_rows, n_latent = _load_latent_frame(
        state, genes, latent_dim, warnings
    )
    s_hvg = sig_vec[hvg_bundle_idx]
    if not np.any(np.abs(s_hvg) > 1e-12):
        raise err(
            "prerequisite",
            "签名基因全部不在高变基因（hvg）内，无法投影到隐空间",
            n_genes_matched=int(matched),
            n_hvg=int(len(hvg_bundle_idx)),
            how=["换用覆盖当前 HVG 的签名", "或重跑 state 提高 n_hvg（需要 ≥1 个签名基因进入 HVG）"],
        )
    n_hvg_matched = int(np.sum(np.abs(s_hvg) > 1e-12))
    s_latent = s_hvg @ loadings[hvg_rows]
    s_hat = _unit(s_latent)
    if not np.any(np.abs(s_hat) > 1e-12):
        raise err("bad_request", "签名在隐空间投影为 0，无法计算方向")

    # ---- 3. 与簇质心的余弦 --------------------------------------------
    progress("签名投影：计算簇相似度…", 55)
    centroids, cluster_labels = _centroids_and_labels(state, n_latent, warnings)
    centroid_hats = np.vstack([_unit(row) for row in centroids])
    cluster_cos = np.clip(centroid_hats @ s_hat, -1.0, 1.0)
    per_cluster = []
    for index, label in enumerate(cluster_labels):
        cos = float(cluster_cos[index])
        per_cluster.append({
            "cluster": str(label),
            "cos": fnum(cos, 4),
            "direction": "activated" if cos > 0 else ("suppressed" if cos < 0 else "neutral"),
        })
    order = np.argsort(-np.abs(cluster_cos))
    top_index = int(order[0]) if order.size else 0
    max_abs_cos = float(np.abs(cluster_cos[top_index])) if cluster_cos.size else 0.0
    top_cluster = str(cluster_labels[top_index]) if cluster_labels else "0"
    top_cos = float(cluster_cos[top_index]) if cluster_cos.size else 0.0

    # 单细胞投影分数（可选，用于分布/画图）
    cell_score = None
    latent = _state_get(state, "latent")
    clusters = _state_get(state, "clusters")
    if latent is not None:
        latent_array = np.asarray(latent, dtype=float)
        if latent_array.ndim == 2 and latent_array.shape[1] >= n_latent:
            cell_score = latent_array[:, :n_latent] @ s_hat
            notes.append("arrays['cell_score'] 是每个细胞在签名方向上的投影（PC 空间线性分数）")
    cluster_n_cells: dict[str, int] = {}
    if clusters is not None:
        clusters_array = np.asarray(clusters).astype(int).ravel()
        for index, label in enumerate(cluster_labels):
            key = index
            count = int(np.sum(clusters_array == key))
            if count == 0:  # 簇名可能不是 0..k-1 的索引
                try:
                    key = int(str(label))
                    count = int(np.sum(clusters_array == key))
                except (TypeError, ValueError):
                    count = 0
            cluster_n_cells[str(label)] = count

    # ---- 4. 通路富集 --------------------------------------------------
    progress("签名投影：通路富集…", 75)
    enrichment = _signature_enrichment(sig["genes"], logfc, genes, warnings)
    enrichment_table = pd.DataFrame(
        enrichment,
        columns=["term", "direction", "query_size", "term_size", "overlap", "genes", "p", "fdr"],
    )
    if enrichment:
        for row in enrichment:
            row["genes"] = ",".join(row["genes"])
        enrichment_table = pd.DataFrame(enrichment)
    else:
        warnings.append("签名基因与内置通路基因集没有重合，未得到富集结果")

    # ---- 5. 表与结论 --------------------------------------------------
    progress("签名投影：整理结果…", 90)
    cluster_rows = []
    for index, label in enumerate(cluster_labels):
        cluster_rows.append({
            "cluster": str(label),
            "n_cells": int(cluster_n_cells.get(str(label), 0)),
            "cos": float(cluster_cos[index]),
            "abs_cos": float(abs(cluster_cos[index])),
            "direction": "activated" if cluster_cos[index] > 0 else "suppressed",
            "rank": int(np.where(order == index)[0][0]) + 1,
        })
    cluster_table = pd.DataFrame(cluster_rows).sort_values("rank").reset_index(drop=True)

    n_up = int(np.sum(logfc > 0))
    n_down = int(np.sum(logfc < 0))
    top_terms = [row for row in enrichment[:3]]
    term_text = "；".join(
        f"{row['term']}({row['direction']}, q={row['fdr']:.2e}, 重合 {row['overlap']}/{row['term_size']})"
        for row in top_terms
    ) or "无显著富集"
    summary = (
        f"签名 {sig_name}（{len(sig['genes'])} 基因，up {n_up} / down {n_down}，"
        f"命中数据集 {matched} 个、HVG 内 {n_hvg_matched} 个）"
        f"最匹配状态 {top_cluster}（cos={top_cos:+.3f}，"
        f"{'激活' if top_cos > 0 else '抑制'}方向，connectivity={top_cos:+.3f}）；"
        f"富集：{term_text}"
    )

    metrics = {
        "n_genes_matched": int(matched),
        "n_signature_genes": int(len(sig["genes"])),
        "n_hvg_matched": int(n_hvg_matched),
        "top_cluster": top_cluster,
        "max_abs_cos": fnum(max_abs_cos, 4),
        "top_cos": fnum(top_cos, 4),
        "top_direction": "activated" if top_cos > 0 else "suppressed",
        "n_clusters": int(len(cluster_labels)),
        "latent_dim": int(n_latent),
        "n_enrichment_terms": int(len(enrichment)),
        "name": sig_name,
        "mode": "signature",
    }
    arrays = {
        "signature_latent": np.asarray(s_hat, dtype=np.float32),
        "cluster_cos": np.asarray(cluster_cos, dtype=np.float32),
        "signature_gene": np.asarray(sig_vec, dtype=np.float32),
    }
    if cell_score is not None:
        arrays["cell_score"] = np.asarray(cell_score, dtype=np.float32)

    result = {
        "ok": True,
        "step": "response",
        "mode": "signature",
        "params": params,
        "arrays": arrays,
        "per_cluster": per_cluster,
        "enrichment": enrichment,
        "metrics": metrics,
        "tables": [
            {"id": "signature_clusters", "title": "签名与簇的相似度（connectivity）",
             "rows": cluster_table},
            {"id": "signature_enrichment", "title": "签名通路富集（超几何 ORA + BH）",
             "rows": enrichment_table},
        ],
        "figures": [
            {"kind": "compare_bar", "title": "签名与各状态的余弦相似度",
             "params": {"items": ["cluster_cos"]}},
        ],
        "summary": summary,
        "warnings": warnings,
        "notes": notes + [
            "connectivity score = cos(s_latent, 簇质心)；符号翻转对照为 -cos，"
            "因此 direction 由余弦符号直接给出",
            "富集 universe 为数据集全部基因（保守口径），p 值为超几何检验，fdr 为 BH",
            "签名投影是线性 PCA 空间的方向匹配，不能据此断言因果调控或药物有效性",
        ],
    }
    progress("签名投影完成", 100)
    return result


def _normalize_signature(signature) -> dict:
    """把各种签名输入统一成 {'name','genes','logfc'}。"""
    name = ""
    genes = None
    logfc = None

    if isinstance(signature, pd.DataFrame):
        frame = signature.copy()
        frame.columns = [str(column) for column in frame.columns]
        gene_col = _find_column(frame.columns, _GENE_KEYS + ("gene_id", "id"))
        value_col = _find_column(
            frame.columns, _VALUE_KEYS + ("log2foldchange", "log_fc", "lfc", "stat", "weight")
        )
        if gene_col is None or value_col is None:
            if frame.shape[1] < 2:
                raise err("bad_request", "签名表至少需要 gene 与 logFC 两列",
                          columns=list(frame.columns))
            gene_col, value_col = frame.columns[0], frame.columns[1]
        genes = frame[gene_col].astype(str).str.strip().tolist()
        logfc = pd.to_numeric(frame[value_col], errors="coerce").to_numpy(dtype=float)
    elif isinstance(signature, dict):
        name = str(signature.get("name") or signature.get("id") or
                   signature.get("label") or "")
        gene_key = next((key for key in _GENE_KEYS if key in signature), None)
        if gene_key is None and "gene" in {str(k).lower() for k in signature}:
            gene_key = next((k for k in signature if str(k).lower() == "gene"), None)
        value_key = next(
            (key for key in (
                "logfc", "logFC", "log_fc", "lfc", "log2fc", "log2FoldChange",
                "weights", "values", "scores", "effects", "value",
            ) if key in signature),
            None,
        )
        if gene_key is not None and value_key is not None:
            genes = [str(g).strip() for g in signature[gene_key]]
            logfc = pd.to_numeric(pd.Series(list(signature[value_key])),
                                  errors="coerce").to_numpy(dtype=float)
        else:
            # {gene: logfc} 映射形式
            pairs = []
            for key, value in signature.items():
                if str(key).lower() in {"name", "id", "label", "source", "description"}:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                pairs.append((str(key).strip(), numeric))
            if not pairs:
                raise err(
                    "bad_request",
                    "签名 dict 无法解析：需要 {'genes':[...], 'logfc':[...]} 或 {gene: logfc} 映射",
                    keys=[str(k) for k in list(signature.keys())[:10]],
                )
            genes = [pair[0] for pair in pairs]
            logfc = np.asarray([pair[1] for pair in pairs], dtype=float)
    elif isinstance(signature, str):
        if not os.path.exists(signature):
            raise err("bad_request", f"签名文件不存在：{signature}",
                      how=["传入 dict/DataFrame，或提供 CSV（gene,logFC）绝对路径"])
        try:
            frame = pd.read_csv(signature, sep=None, engine="python")
        except Exception as exc:  # noqa: BLE001
            raise err("bad_request", f"签名文件读取失败：{exc}", path=signature) from exc
        return _normalize_signature(frame)
    elif isinstance(signature, (list, tuple, np.ndarray)):
        array = np.asarray(signature)
        if array.ndim == 2 and array.shape[1] >= 2:
            genes = [str(item).strip() for item in array[:, 0]]
            logfc = pd.to_numeric(pd.Series(array[:, 1]), errors="coerce").to_numpy(dtype=float)
        elif isinstance(signature, (list, tuple)) and signature and isinstance(signature[0], dict):
            genes = [str(row.get("gene") or row.get("symbol") or "").strip() for row in signature]
            logfc = pd.to_numeric(pd.Series([row.get("logFC", row.get("logfc", row.get("value")))
                                            for row in signature]),
                                  errors="coerce").to_numpy(dtype=float)
        else:
            raise err("bad_request", "签名列表无法解析：需要 [[gene, logfc], …] 或 [{'gene','logFC'}]")
    else:
        raise err(
            "bad_request",
            f"不支持的签名类型：{type(signature).__name__}",
            how=["dict {'name','genes','logfc'}", "DataFrame[gene, logFC]", "CSV 路径"],
        )

    genes = [str(gene).strip() for gene in genes]
    logfc = np.asarray(logfc, dtype=float)
    if len(genes) != logfc.size:
        raise err("bad_request",
                  f"签名 genes 长度({len(genes)})与 logfc 长度({logfc.size})不一致")
    keep = np.array([bool(str(gene)) and np.isfinite(value)
                     for gene, value in zip(genes, logfc)], dtype=bool)
    if not bool(np.all(keep)):
        dropped = int((~keep).sum())
        genes = [gene for gene, flag in zip(genes, keep) if flag]
        logfc = logfc[keep]
    else:
        dropped = 0
    if not genes:
        raise err("bad_request", "签名没有有效基因（genes/logfc 为空或全为 NaN）")
    # 重复基因按均值合并（保持首次出现顺序）
    merged: dict[str, list[float]] = {}
    order: list[str] = []
    for gene, value in zip(genes, logfc):
        if gene not in merged:
            merged[gene] = []
            order.append(gene)
        merged[gene].append(float(value))
    genes = order
    logfc = np.asarray([float(np.mean(merged[gene])) for gene in genes], dtype=float)
    out = {"name": name, "genes": genes, "logfc": logfc}
    if dropped:
        out["dropped"] = dropped
    return out


def _signature_enrichment(sig_genes, logfc, dataset_genes, warnings: list[str]) -> list[dict]:
    """签名 top-up / top-down 基因的超几何 ORA + BH。"""
    gene_array = np.asarray(sig_genes, dtype=object)
    lfc = np.asarray(logfc, dtype=float)
    universe_set = {str(gene) for gene in dataset_genes}
    universe_size = len(universe_set)
    if universe_size == 0:
        return []

    rows: list[dict] = []
    up_idx = np.where(lfc > 0)[0]
    down_idx = np.where(lfc < 0)[0]
    if up_idx.size:
        order = up_idx[np.argsort(-lfc[up_idx])][:_TOP_SIGNATURE_GENES]
        rows.extend(_hypergeom_ora(
            [str(gene_array[i]) for i in order], universe_size,
            priors.PATHWAY_SETS, universe_set, "up",
        ))
    if down_idx.size:
        order = down_idx[np.argsort(lfc[down_idx])][:_TOP_SIGNATURE_GENES]
        rows.extend(_hypergeom_ora(
            [str(gene_array[i]) for i in order], universe_size,
            priors.PATHWAY_SETS, universe_set, "down",
        ))
    if not rows:
        return []
    qvalues = _bh_adjust([row["p"] for row in rows])
    for row, qvalue in zip(rows, qvalues):
        row["p"] = fnum(row["p"], 6)
        row["fdr"] = fnum(float(qvalue), 6)
    rows.sort(key=lambda item: (item["p"] if item["p"] is not None else 1.0))
    kept = rows[:_MAX_ENRICH_ROWS]
    if len(rows) > len(kept):
        warnings.append(f"富集命中 {len(rows)} 条，已截断为 {len(kept)} 条（按 p 排序保留）")
    return kept
