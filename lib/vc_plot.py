#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — 出图模块（CONTRACT §7.5）。

`plot(kind, ctx, out_path, params)` 画一张图并返回
`{'file','width','height','title','notes'}`。**绝不抛异常**：缺数据/出错时画占位图并写 notes。

16 种图：
qc_violin / state_embedding / state_embedding_perturb / cluster_bar /
grn_hub_bar / grn_network / grn_heatmap / perturb_waterfall / perturb_field /
perturb_heatmap / screen_rank / response_scatter / transition_matrix /
transition_graph / validate_roc / compare_bar
"""

from __future__ import annotations

import math
import os

import numpy as np

import vc_common as C

MAX_POINTS = 200_000
DEFAULT_SIZE = (7.2, 5.4)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _arr(ctx, *path, default=None):
    """按路径取嵌套字段：_arr(ctx,'state','arrays','latent')。"""
    node = ctx
    for key in path:
        if node is None:
            return default
        if isinstance(node, dict):
            node = node.get(key)
        else:
            node = getattr(node, key, None)
    return default if node is None else node


def _stage(ctx, name):
    value = ctx.get(name) if isinstance(ctx, dict) else None
    return value if isinstance(value, dict) else {}


def _labels(state):
    labels = state.get("cluster_labels")
    if isinstance(labels, (list, tuple)) and labels:
        return [str(item) for item in labels]
    clusters = _arr(state, "arrays", "clusters")
    if clusters is not None:
        unique = sorted({int(item) for item in np.asarray(clusters).ravel()})
        return [str(item) for item in unique]
    return []


def _clusters(state):
    value = _arr(state, "arrays", "clusters")
    if value is None:
        return None
    try:
        return np.asarray(value).ravel().astype(int)
    except Exception:  # noqa: BLE001
        return None


def _embedding(state):
    value = _arr(state, "arrays", "embedding")
    if value is None:
        return None
    array = np.asarray(value, dtype=float)
    if array.ndim != 2 or array.shape[1] < 2 or array.shape[0] < 3:
        return None
    return array[:, :2]


def _is_categorical(values) -> bool:
    try:
        sample = [item for item in list(values)[:50]]
    except TypeError:
        return False
    return bool(sample) and any(isinstance(item, (str, bytes, np.str_)) for item in sample)


def _colored(values, labels=None):
    """把一组着色值变成 (colors, legend_items)；字符串自动走分类配色。"""
    values = list(values)
    if labels is not None or _is_categorical(values):
        mapping = C.color_map([str(item) for item in values])
        return [mapping[str(item)] for item in values], [(str(k), v) for k, v in mapping.items()]
    array = np.asarray(values, dtype=float)
    finite = np.isfinite(array)
    if finite.sum() == 0:
        return ["#999999"] * len(values), []
    low, high = np.nanpercentile(array[finite], [2, 98])
    span = max(1e-9, float(high - low))
    import matplotlib

    cmap = matplotlib.colormaps[C.CONTINUOUS_CMAP]
    colors = []
    for item in array:
        if not np.isfinite(item):
            colors.append("#dddddd")
        else:
            colors.append(matplotlib.colors.to_hex(cmap(float(np.clip((item - low) / span, 0, 1)))))
    return colors, []


def _categorical_series(state, name):
    """从 state 里取一个“每细胞一个值”的分类列。"""
    columns = state.get("cells_columns") or {}
    if isinstance(columns, dict) and name in columns:
        return list(columns[name])
    arrays = state.get("arrays") or {}
    if name == "clusters" and "clusters" in arrays:
        return [str(int(item)) for item in np.asarray(arrays["clusters"]).ravel()]
    obs = _arr(state, "obs_columns")
    if isinstance(obs, dict) and name in obs:
        return list(obs[name])
    return None


def _gene_expression(state, gene):
    genes = state.get("genes") or []
    if gene not in genes:
        return None
    matrix = _arr(state, "arrays", "X_log")
    if matrix is None:
        return None
    index = genes.index(gene)
    try:
        column = matrix[:, index]
        values = column.toarray().ravel() if hasattr(column, "toarray") else np.asarray(column).ravel()
        return values.astype(float)
    except Exception:  # noqa: BLE001
        return None


def _deg_panel(plt, fig, message, title):
    """占位：白底 + 居中文案。"""
    ax = fig.add_axes([0.05, 0.05, 0.9, 0.9])
    ax.axis("off")
    ax.text(0.5, 0.55, str(title), ha="center", va="center", fontsize=14, color="#333333")
    ax.text(0.5, 0.38, str(message), ha="center", va="center", fontsize=10, color="#888888", wrap=True)
    return ax


def _finish(fig, out_path, title, notes, expected=None):
    info = C.save_figure(fig, out_path, dpi=200)
    return {"file": os.path.abspath(out_path), "width": info["width"], "height": info["height"],
            "title": title, "notes": list(notes)}


def _points(ax, xy, colors, size, alpha=0.75):
    xy = np.asarray(xy, dtype=float)
    if xy.shape[0] > MAX_POINTS:
        index = np.linspace(0, xy.shape[0] - 1, MAX_POINTS).astype(int)
        xy = xy[index]
        colors = [colors[item] for item in index]
    ax.scatter(xy[:, 0], xy[:, 1], c=colors, s=size, alpha=alpha, linewidths=0, rasterized=True)


def _plain_axes(ax):
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ("top", "right", "bottom", "left"):
        try:
            ax.spines[spine].set_visible(False)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def plot(kind: str, ctx: dict, out_path: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    title = str((ctx or {}).get("title") or params.get("title") or kind)
    try:
        plt = C.setup_matplotlib()
    except C.EngineError as error:
        return {"file": os.path.abspath(out_path), "width": 0, "height": 0, "title": title,
                "notes": [f"matplotlib 不可用，未出图：{error.message}"]}
    C.ensure_dir(os.path.dirname(os.path.abspath(out_path)))
    kind = str(kind or "").strip()
    try:
        handler = _HANDLERS.get(kind)
        if handler is None:
            fig = plt.figure(figsize=DEFAULT_SIZE)
            _deg_panel(plt, fig, f"未知图类型：{kind}", title)
            return _finish(fig, out_path, title, [f"不支持的图类型 {kind}"])
        return handler(plt, ctx or {}, out_path, params, title)
    except Exception as error:  # noqa: BLE001
        try:
            plt.close("all")
        except Exception:  # noqa: BLE001
            pass
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, f"出图失败：{type(error).__name__}: {error}", title)
        return _finish(fig, out_path, title, [f"出图异常：{error}"])


# ---------------------------------------------------------------------------
# 1. 质控小提琴
# ---------------------------------------------------------------------------

def _plot_qc_violin(plt, ctx, out_path, params, title):
    state = _stage(ctx, "state")
    result = _stage(ctx, "result")
    source = {}
    for container in (result.get("obs_columns") or {}, state.get("obs_columns") or {}):
        if isinstance(container, dict):
            source.update(container)
    metrics = {
        "检出基因数": source.get("n_genes"),
        "总 counts": source.get("n_counts"),
        "线粒体占比 (%)": source.get("mito_pct"),
    }
    available = [(name, np.asarray(values, dtype=float)) for name, values in metrics.items()
                 if values is not None and len(values) > 0]
    if not available:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「质控」步骤（生成每细胞 QC 指标）", title)
        return _finish(fig, out_path, title, ["缺少 QC 指标：先运行 qc"])
    fig, axes = plt.subplots(1, len(available), figsize=(3.2 * len(available), 4.2))
    if len(available) == 1:
        axes = [axes]
    for ax, (name, values) in zip(axes, available):
        finite = values[np.isfinite(values)]
        parts = ax.violinplot([finite] if finite.size else [[0.0]], showextrema=False, widths=0.7)
        for body in parts["bodies"]:
            body.set_facecolor(C.palette_color(0))
            body.set_alpha(0.45)
            body.set_edgecolor("#3d5a80")
        median = float(np.median(finite)) if finite.size else 0.0
        ax.scatter([1], [median], color="#c0392b", zorder=5, s=28, label=f"中位数 {median:.4g}")
        ax.set_xticks([])
        ax.set_title(name, fontsize=11)
        ax.legend(loc="upper right", fontsize=8)
    fig.suptitle(f"{title}（{len(available)} 项指标）", fontsize=13)
    notes = ["图：每细胞质控指标分布；红点为中位数。"]
    return _finish(fig, out_path, title, notes)


# ---------------------------------------------------------------------------
# 2/3. 状态图（含扰动箭头）
# ---------------------------------------------------------------------------

def _embedding_axes(plt, ctx, params, title, kind):
    state = _stage(ctx, "state")
    embedding = _embedding(state)
    if embedding is None:
        return None
    color_by = str(params.get("color_by") or "clusters")
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    notes = []
    if color_by in ("clusters", "cluster"):
        clusters = _clusters(state)
        labels = _labels(state)
        if clusters is None:
            return None
        names = [labels[item] if 0 <= item < len(labels) else str(item) for item in clusters]
        colors, legend = _colored(names)
        _points(ax, embedding, colors, params.get("point_size") or 6)
        for name, color in legend[:20]:
            ax.scatter([], [], c=color, s=22, label=str(name))
        ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8, title="状态", markerscale=1.4)
        notes.append(f"按状态着色，共 {len(legend)} 个状态。")
    elif color_by == "cell_state":
        values = _categorical_series(state, "cell_state")
        if values is None:
            return None
        colors, legend = _colored(values)
        _points(ax, embedding, colors, params.get("point_size") or 6)
        for name, color in legend[:20]:
            ax.scatter([], [], c=color, s=22, label=str(name))
        ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8, title="注释", markerscale=1.4)
        notes.append("按参考面板注释的细胞状态着色。")
    else:
        values = _categorical_series(state, color_by)
        if values is None:
            values = _gene_expression(state, color_by)
        if values is None:
            return None
        if _is_categorical(values):
            colors, legend = _colored(values)
            _points(ax, embedding, colors, params.get("point_size") or 6)
            for name, color in legend[:20]:
                ax.scatter([], [], c=color, s=22, label=str(name))
            ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8, markerscale=1.4)
        else:
            colors, _ = _colored(values)
            _points(ax, embedding, colors, params.get("point_size") or 6)
            finite = np.asarray(values, dtype=float)
            finite = finite[np.isfinite(finite)]
            if finite.size:
                sm = plt.cm.ScalarMappable(cmap=C.CONTINUOUS_CMAP,
                                           norm=plt.Normalize(float(np.percentile(finite, 2)),
                                                              float(np.percentile(finite, 98))))
                fig.colorbar(sm, ax=ax, shrink=0.7, pad=0.02, label=color_by)
                notes.append(f"按 {color_by} 表达着色。")
    ax.set_xlabel("嵌入维度 1", fontsize=10)
    ax.set_ylabel("嵌入维度 2", fontsize=10)
    ax.set_title(f"{title}（{color_by} 着色）", fontsize=13)
    return fig, ax, notes


def _plot_state_embedding(plt, ctx, out_path, params, title):
    built = _embedding_axes(plt, ctx, params, title, "state_embedding")
    if built is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「状态表征」得到二维嵌入", title)
        return _finish(fig, out_path, title, ["缺少状态嵌入：先运行 state"])
    fig, ax, notes = built
    notes.insert(0, "图：细胞状态二维嵌入（静态图；页面内为可交互 canvas）。")
    return _finish(fig, out_path, title, notes)


def _plot_state_embedding_perturb(plt, ctx, out_path, params, title):
    built = _embedding_axes(plt, ctx, params, title, "state_embedding_perturb")
    if built is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「状态表征」", title)
        return _finish(fig, out_path, title, ["缺少状态嵌入"])
    fig, ax, notes = built
    perturb = _stage(ctx, "perturb")
    arrows = _arr(perturb, "arrays", "arrows")
    if arrows is None:
        notes.append("没有扰动结果，只画了状态图；先运行虚拟扰动。")
        return _finish(fig, out_path, title, notes)
    arrows = np.asarray(arrows, dtype=float)
    clusters = _clusters(_stage(ctx, "state"))
    centroids = _arr(_stage(ctx, "state"), "arrays", "embedding")
    embedding = _embedding(_stage(ctx, "state"))
    labels = _labels(_stage(ctx, "state"))
    if clusters is not None and embedding is not None:
        centers = np.vstack([embedding[clusters == index].mean(axis=0)
                             if (clusters == index).any() else [np.nan, np.nan]
                             for index in range(arrows.shape[0])])
    else:
        centers = np.zeros((arrows.shape[0], 2))
    norms = np.linalg.norm(arrows, axis=1)
    # 箭头按视野自动缩放：中位箭头约占坐标跨度的 8%，避免箭头小到看不见/大到穿出画布
    span = float(np.nanmax(embedding) - np.nanmin(embedding)) if embedding is not None else 1.0
    median_norm = float(np.nanmedian(norms)) if np.isfinite(norms).any() else 0.0
    auto = (0.08 * span / median_norm) if median_norm > 1e-9 else 1.0
    scale = float(params.get("arrow_scale") or auto)
    for index, (center, arrow) in enumerate(zip(centers, arrows)):
        if not np.all(np.isfinite(center)) or not np.all(np.isfinite(arrow)):
            continue
        ax.annotate("", xy=center + arrow * scale, xytext=center,
                    arrowprops={"arrowstyle": "-|>", "lw": 1.6,
                                "color": "#c0392b" if norms[index] > np.median(norms) else "#e08b5a"})
    ax.scatter(centers[:, 0], centers[:, 1], marker="X", s=90, color="#222222", zorder=6, label="状态中心")
    for index, center in enumerate(centers):
        name = labels[index] if index < len(labels) else str(index)
        ax.annotate(name, (center[0], center[1]), fontsize=7, xytext=(3, 3), textcoords="offset points")
    gene = perturb.get("gene") or ""
    ax.set_title(f"{title}（{gene}）", fontsize=13)
    ax.legend(loc="best", fontsize=8)
    notes.insert(0, f"图：{gene} 扰动后每个状态中心在嵌入空间的预测位移（箭头方向=状态偏移方向，长度已按视野归一）。")
    notes.append("位移由线性传播模型给出，属于假说；见 validity 字段。")
    return _finish(fig, out_path, title, notes)


# ---------------------------------------------------------------------------
# 4. 簇规模柱状
# ---------------------------------------------------------------------------

def _plot_cluster_bar(plt, ctx, out_path, params, title):
    state = _stage(ctx, "state")
    clusters = _clusters(state)
    if clusters is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「状态表征」", title)
        return _finish(fig, out_path, title, ["缺少聚类结果"])
    labels = _labels(state)
    unique = sorted({int(item) for item in clusters})
    sizes = [int((clusters == item).sum()) for item in unique]
    names = [labels[item] if 0 <= item < len(labels) else str(item) for item in unique]
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    colors = [C.palette_color(index) for index in range(len(unique))]
    bars = ax.bar(range(len(unique)), sizes, color=colors, alpha=0.85)
    for bar_item, size in zip(bars, sizes):
        ax.annotate(f"{size:,}", (bar_item.get_x() + bar_item.get_width() / 2, size), ha="center",
                    va="bottom", fontsize=8)
    ax.set_xticks(range(len(unique)))
    ax.set_xticklabels(names, rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("细胞数")
    ax.set_title(f"{title}（{len(unique)} 个状态 / {sum(sizes):,} 细胞）", fontsize=13)
    notes = [f"图：每个状态的细胞数；最小 {min(sizes):,}，最大 {max(sizes):,}。"]
    return _finish(fig, out_path, title, notes)


# ---------------------------------------------------------------------------
# 5/6/7. GRN
# ---------------------------------------------------------------------------

def _hub_rows(grn):
    rows = _arr(grn, "metrics", "hub_tfs")
    if rows:
        return rows
    edges = grn.get("edges") or []
    stats = {}
    for edge in edges:
        tf = str(edge.get("tf") or "")
        if not tf:
            continue
        entry = stats.setdefault(tf, {"tf": tf, "out_degree": 0, "sum_weight": 0.0})
        entry["out_degree"] += 1
        entry["sum_weight"] += abs(float(edge.get("score") or 0))
    for entry in stats.values():
        entry["mean_abs_weight"] = entry["sum_weight"] / max(1, entry["out_degree"])
    return sorted(stats.values(), key=lambda item: -item["out_degree"])[:10]


def _plot_grn_hub_bar(plt, ctx, out_path, params, title):
    grn = _stage(ctx, "grn")
    hubs = _hub_rows(grn)
    if not hubs:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「调控网络」", title)
        return _finish(fig, out_path, title, ["缺少 GRN hub"])
    hubs = hubs[:15][::-1]
    names = [str(item.get("tf")) for item in hubs]
    degrees = [int(item.get("out_degree") or 0) for item in hubs]
    weights = [float(item.get("mean_abs_weight") or 0) for item in hubs]
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    bars = ax.barh(names, degrees, color=[C.palette_color(index) for index in range(len(names))], alpha=0.85)
    for bar_item, weight in zip(bars, weights):
        ax.annotate(f"|w|={weight:.2f}", (bar_item.get_width(), bar_item.get_y() + bar_item.get_height() / 2),
                    va="center", fontsize=8, xytext=(3, 0), textcoords="offset points")
    ax.set_xlabel("调控边数（出度）")
    ax.set_title(f"{title}（Top {len(names)}）", fontsize=13)
    notes = [f"图：按出度排序的枢纽 TF；最高 {names[-1]}（{degrees[-1]} 条边）。"]
    return _finish(fig, out_path, title, notes)


def _plot_grn_network(plt, ctx, out_path, params, title):
    grn = _stage(ctx, "grn")
    edges = grn.get("edges") or []
    if not edges:
        fig = plt.figure(figsize=(6.4, 6.4))
        _deg_panel(plt, fig, "需要先运行「调控网络」", title)
        return _finish(fig, out_path, title, ["缺少 GRN 边"])
    edges = sorted(edges, key=lambda item: -abs(float(item.get("score") or 0)))[: int(params.get("max_edges") or 150)]
    degree = {}
    for edge in edges:
        for key in ("tf", "target"):
            name = str(edge.get(key) or "")
            degree[name] = degree.get(name, 0) + 1
    names = sorted(degree, key=lambda item: -degree[item])[:60]
    index_of = {name: index for index, name in enumerate(names)}
    angles = np.linspace(0, 2 * np.pi, len(names), endpoint=False)
    pos = {name: (math.cos(angles[index]), math.sin(angles[index])) for name, index in index_of.items()}
    fig, ax = plt.subplots(figsize=(6.8, 6.8))
    for edge in edges:
        source, target = str(edge.get("tf") or ""), str(edge.get("target") or "")
        if source not in pos or target not in pos:
            continue
        color = "#c0392b" if float(edge.get("score") or 0) >= 0 else "#2e6f95"
        width = 0.4 + 2.6 * min(1.0, abs(float(edge.get("score") or 0)) / 1.5)
        ax.plot([pos[source][0], pos[target][0]], [pos[source][1], pos[target][1]],
                color=color, lw=width, alpha=0.55, zorder=1)
    xs = [pos[name][0] for name in names]
    ys = [pos[name][1] for name in names]
    sizes = [40 + 26 * min(12, degree.get(name, 1)) for name in names]
    ax.scatter(xs, ys, s=sizes, c=[C.palette_color(index) for index in range(len(names))],
               edgecolors="white", linewidths=0.8, zorder=3)
    for index, name in enumerate(names[:25]):
        ax.annotate(name, (xs[index], ys[index]), fontsize=7, xytext=(4, 4),
                    textcoords="offset points", zorder=4)
    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-1.25, 1.25)
    ax.set_aspect("equal")
    _plain_axes(ax)
    ax.set_title(f"{title}（Top {len(edges)} 边，红=激活 蓝=抑制）", fontsize=13)
    notes = ["图：环形布局的调控网络；节点大小=连接数，边颜色=调控方向。",
             "共表达推断只给假说，方向性需 ATAC/ChIP 或实验验证。"]
    return _finish(fig, out_path, title, notes)


def _plot_grn_heatmap(plt, ctx, out_path, params, title):
    grn = _stage(ctx, "grn")
    edges = grn.get("edges") or []
    if not edges:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「调控网络」", title)
        return _finish(fig, out_path, title, ["缺少 GRN 边"])
    tfs = []
    targets = []
    for edge in edges:
        if edge.get("tf") not in tfs:
            tfs.append(edge.get("tf"))
        if edge.get("target") not in targets:
            targets.append(edge.get("target"))
    tfs, targets = tfs[:25], targets[:25]
    grid = np.zeros((len(tfs), len(targets)))
    for edge in edges:
        if edge.get("tf") in tfs and edge.get("target") in targets:
            grid[tfs.index(edge["tf"]), targets.index(edge["target"])] = float(edge.get("score") or 0)
    fig, ax = plt.subplots(figsize=(7.2, 6.2))
    limit = float(np.abs(grid).max()) or 1.0
    image = ax.imshow(grid, cmap=C.DIVERGING_CMAP, vmin=-limit, vmax=limit, aspect="auto")
    ax.set_xticks(range(len(targets)))
    ax.set_xticklabels(targets, rotation=90, fontsize=7)
    ax.set_yticks(range(len(tfs)))
    ax.set_yticklabels(tfs, fontsize=7)
    fig.colorbar(image, ax=ax, shrink=0.7, pad=0.02, label="调控权重（正=激活 / 负=抑制）")
    ax.set_title(f"{title}（红=激活 蓝=抑制）", fontsize=13)
    return _finish(fig, out_path, title, ["图：高权重边的 TF×靶基因系数热图（发散色，0=无调控）。"])


# ---------------------------------------------------------------------------
# 8/9/10. 扰动
# ---------------------------------------------------------------------------

def _plot_perturb_waterfall(plt, ctx, out_path, params, title):
    perturb = _stage(ctx, "perturb")
    affected = perturb.get("affected") or []
    if not affected:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「虚拟扰动」", title)
        return _finish(fig, out_path, title, ["缺少扰动结果"])
    rows = sorted(affected, key=lambda item: -abs(float(item.get("delta") or 0)))[: int(params.get("top_n") or 25)]
    rows = rows[::-1]
    names = [str(item.get("gene")) for item in rows]
    values = [float(item.get("delta") or 0) for item in rows]
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    colors = ["#c0392b" if value >= 0 else "#2e6f95" for value in values]
    ax.barh(names, values, color=colors, alpha=0.9)
    for index, (name, value) in enumerate(zip(names, values)):
        ax.annotate(f"{value:+.2f}", (value, index), va="center", fontsize=7,
                    xytext=(4 if value >= 0 else -4, 0), textcoords="offset points",
                    ha="left" if value >= 0 else "right")
    ax.axvline(0, color="#555555", lw=0.8)
    gene = perturb.get("gene") or ""
    mode = perturb.get("mode") or ""
    ax.set_xlabel("预测表达变化 Δ（log 空间）")
    ax.set_title(f"{title}（{gene} {mode}，Top {len(names)}）", fontsize=13)
    notes = [f"图：{gene} {mode} 后受影响最大的 {len(names)} 个基因（红=上调，蓝=下调）。"]
    return _finish(fig, out_path, title, notes)


def _plot_perturb_field(plt, ctx, out_path, params, title):
    built = _embedding_axes(plt, ctx, params, title, "perturb_field")
    if built is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「状态表征」与「虚拟扰动」", title)
        return _finish(fig, out_path, title, ["缺少状态嵌入或扰动"])
    fig, ax, notes = built
    perturb = _stage(ctx, "perturb")
    arrows = _arr(perturb, "arrays", "arrows")
    state = _stage(ctx, "state")
    clusters = _clusters(state)
    embedding = _embedding(state)
    labels = _labels(state)
    if arrows is None or clusters is None or embedding is None:
        notes.append("缺少扰动向量，只画了状态图。")
        return _finish(fig, out_path, title, notes)
    arrows = np.asarray(arrows, dtype=float)
    centers = np.vstack([embedding[clusters == index].mean(axis=0) if (clusters == index).any()
                         else [np.nan, np.nan] for index in range(arrows.shape[0])])
    norms = np.linalg.norm(arrows, axis=1)
    span = float(np.nanmax(embedding) - np.nanmin(embedding))
    median_norm = float(np.nanmedian(norms)) if np.isfinite(norms).any() else 0.0
    auto = (0.10 * span / median_norm) if median_norm > 1e-9 else 1.0
    scale = float(params.get("arrow_scale") or auto)
    quiver = ax.quiver(centers[:, 0], centers[:, 1], arrows[:, 0] * scale, arrows[:, 1] * scale,
                       np.linalg.norm(arrows, axis=1), cmap="OrRd", angles="xy",
                       scale_units="xy", scale=1.0, width=0.006, zorder=6)
    fig.colorbar(quiver, ax=ax, shrink=0.7, pad=0.02, label="位移强度")
    ax.scatter(centers[:, 0], centers[:, 1], marker="X", s=70, color="#222222", zorder=7)
    for index, center in enumerate(centers):
        if np.all(np.isfinite(center)):
            ax.annotate(labels[index] if index < len(labels) else str(index), center,
                        fontsize=7, xytext=(3, 3), textcoords="offset points")
    gene = perturb.get("gene") or ""
    ax.set_title(f"{title}（{gene}）", fontsize=13)
    notes.insert(0, f"图：{gene} 扰动后各状态的预测位移（箭头=方向，颜色=强度）。")
    return _finish(fig, out_path, title, notes)


def _plot_perturb_heatmap(plt, ctx, out_path, params, title):
    perturb = _stage(ctx, "perturb")
    delta_latent = _arr(perturb, "arrays", "delta_latent")
    labels = _labels(_stage(ctx, "state"))
    if delta_latent is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「虚拟扰动」", title)
        return _finish(fig, out_path, title, ["缺少扰动向量"])
    grid = np.asarray(delta_latent, dtype=float)
    if grid.ndim == 1:
        grid = grid[None, :]
    fig, ax = plt.subplots(figsize=(7.2, max(2.8, 0.42 * grid.shape[0] + 1.6)))
    limit = float(np.abs(grid).max()) or 1.0
    image = ax.imshow(grid, cmap=C.DIVERGING_CMAP, vmin=-limit, vmax=limit, aspect="auto")
    ax.set_yticks(range(grid.shape[0]))
    names = [labels[index] if index < len(labels) else f"状态{index}" for index in range(grid.shape[0])]
    ax.set_yticklabels(names, fontsize=8)
    ax.set_xticks(range(grid.shape[1]))
    ax.set_xticklabels([f"PC{index + 1}" for index in range(grid.shape[1])], rotation=90, fontsize=6)
    fig.colorbar(image, ax=ax, shrink=0.7, pad=0.02, label="隐空间位移 Δ")
    gene = perturb.get("gene") or ""
    ax.set_title(f"{title}（{gene}，行=状态 列=PC）", fontsize=13)
    return _finish(fig, out_path, title,
                   ["图：每个状态中心的隐空间位移向量（行=状态，列=主成分）；不是基因级热图。"])


# ---------------------------------------------------------------------------
# 11. 虚拟筛选排名
# ---------------------------------------------------------------------------

def _plot_screen_rank(plt, ctx, out_path, params, title):
    screen = _stage(ctx, "screen")
    ranked = screen.get("ranked") or []
    if not ranked:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「虚拟筛选」", title)
        return _finish(fig, out_path, title, ["缺少筛选结果"])
    rows = ranked[: int(params.get("top_n") or 20)][::-1]
    names = [str(item.get("gene")) for item in rows]
    scores = [float(item.get("score") or 0) for item in rows]
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    bars = ax.barh(names, scores, color=[C.palette_color(index) for index in range(len(names))], alpha=0.88)
    for bar_item, row in zip(bars, rows):
        target = str(row.get("target_state") or "")
        ax.annotate(f"→{target}" if target else "", (bar_item.get_width(), bar_item.get_y() + bar_item.get_height() / 2),
                    va="center", fontsize=7, xytext=(3, 0), textcoords="offset points")
    ax.set_xlabel("筛选得分（位移 × 专一性）")
    ax.set_title(f"{title}（Top {len(names)}）", fontsize=13)
    return _finish(fig, out_path, title,
                   ["图：虚拟筛选得分排名；箭头为目标状态，得分越高越值得优先验证。"])


# ---------------------------------------------------------------------------
# 12. 响应预测散点
# ---------------------------------------------------------------------------

def _plot_response_scatter(plt, ctx, out_path, params, title):
    response = _stage(ctx, "response")
    pred = _arr(response, "arrays", "pred")
    true = _arr(response, "arrays", "true")
    if pred is None or true is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「响应预测」（需扰动表）", title)
        return _finish(fig, out_path, title, ["缺少预测结果"])
    pred = np.asarray(pred, dtype=float)
    true = np.asarray(true, dtype=float)
    if pred.shape != true.shape:
        pred = pred.ravel()
        true = true.ravel()
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    ax.scatter(true.ravel(), pred.ravel(), s=26, color=C.palette_color(0), alpha=0.75, edgecolors="white")
    low = float(min(true.min(), pred.min()))
    high = float(max(true.max(), pred.max()))
    ax.plot([low, high], [low, high], "--", color="#888888", lw=1, label="y = x")
    metrics = response.get("metrics") or {}
    r2 = metrics.get("r2")
    text = f"R² = {r2:.3f}" if isinstance(r2, (int, float)) else "R² = —"
    ax.annotate(text, (0.04, 0.92), xycoords="axes fraction", fontsize=11)
    ax.set_xlabel("真实隐空间响应 Δ")
    ax.set_ylabel("预测隐空间响应 Δ")
    ax.set_title(f"{title}（{text}）", fontsize=13)
    ax.legend(loc="lower right", fontsize=8)
    return _finish(fig, out_path, title,
                   ["图：留出扰动的预测 vs 真实响应（隐空间）；点越接近对角线越准。"])


# ---------------------------------------------------------------------------
# 13/14. 状态转移
# ---------------------------------------------------------------------------

def _plot_transition_matrix(plt, ctx, out_path, params, title):
    transition = _stage(ctx, "transition")
    matrix = _arr(transition, "arrays", "T")
    if matrix is None:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "需要先运行「状态转移」", title)
        return _finish(fig, out_path, title, ["缺少转移矩阵"])
    matrix = np.asarray(matrix, dtype=float)
    labels = _labels(_stage(ctx, "state"))
    names = [labels[index] if index < len(labels) else f"状态{index}" for index in range(matrix.shape[0])]
    fig, ax = plt.subplots(figsize=(6.8, 5.8))
    image = ax.imshow(matrix, cmap="Blues", aspect="auto")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names, fontsize=8)
    for row in range(matrix.shape[0]):
        for col in range(matrix.shape[1]):
            value = matrix[row, col]
            if value >= 0.01:
                ax.text(col, row, f"{value:.2f}", ha="center", va="center", fontsize=6,
                        color="white" if value > 0.5 else "#222222")
    fig.colorbar(image, ax=ax, shrink=0.75, pad=0.02, label="转移倾向（行归一）")
    ax.set_title(f"{title}（行归一）", fontsize=13)
    return _finish(fig, out_path, title, ["图：状态间转移倾向（行=起点，列=终点）；对角线=自我保持。"])


def _plot_transition_graph(plt, ctx, out_path, params, title):
    transition = _stage(ctx, "transition")
    matrix = _arr(transition, "arrays", "T")
    flows = transition.get("net_flow") or []
    if matrix is None and not flows:
        fig = plt.figure(figsize=(6.4, 6.4))
        _deg_panel(plt, fig, "需要先运行「状态转移」", title)
        return _finish(fig, out_path, title, ["缺少转移结果"])
    labels = _labels(_stage(ctx, "state"))
    count = int(np.asarray(matrix).shape[0]) if matrix is not None else 0
    if count == 0:
        count = len({row.get("from") for row in flows} | {row.get("to") for row in flows})
    names = [labels[index] if index < len(labels) else f"状态{index}" for index in range(count)]
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    pos = [(math.cos(angle), math.sin(angle)) for angle in angles]
    fig, ax = plt.subplots(figsize=(6.6, 6.6))
    if matrix is not None:
        grid = np.asarray(matrix, dtype=float)
        flows_sorted = [(row, col, grid[row, col]) for row in range(count) for col in range(count)
                        if row != col and grid[row, col] > 0.02]
    else:
        flows_sorted = [(int(item.get("from", 0)), int(item.get("to", 0)), float(item.get("flow") or 0))
                        for item in flows]
    for row, col, value in sorted(flows_sorted, key=lambda item: -item[2])[:30]:
        if row >= count or col >= count:
            continue
        ax.annotate("", xy=pos[col], xytext=pos[row],
                    arrowprops={"arrowstyle": "-|>", "lw": 0.8 + 6.0 * min(1.0, value),
                                "color": "#4e79a7", "alpha": 0.7,
                                "connectionstyle": "arc3,rad=0.18"})
    ax.scatter([item[0] for item in pos], [item[1] for item in pos], s=320,
               c=[C.palette_color(index) for index in range(count)], edgecolors="white", zorder=3)
    for index, name in enumerate(names):
        ax.annotate(name, pos[index], fontsize=8, ha="center", va="center", zorder=4)
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-1.3, 1.3)
    ax.set_aspect("equal")
    _plain_axes(ax)
    ax.set_title(f"{title}（Top 30 流向）", fontsize=13)
    return _finish(fig, out_path, title, ["图：状态间的净转移流向；箭头粗细=转移倾向。"])


# ---------------------------------------------------------------------------
# 15. 校验 ROC
# ---------------------------------------------------------------------------

def _plot_validate_roc(plt, ctx, out_path, params, title):
    result = _stage(ctx, "result")
    validate = _stage(ctx, "validate")
    metrics = validate.get("metrics") or result.get("metrics") or {}
    fpr = _arr(validate, "arrays", "roc_fpr") or _arr(result, "arrays", "roc_fpr")
    tpr = _arr(validate, "arrays", "roc_tpr") or _arr(result, "arrays", "roc_tpr")
    roc = _arr(validate, "arrays", "roc")
    if roc is None:
        roc = _arr(result, "arrays", "roc")
    if (fpr is None or tpr is None) and roc is not None:
        try:
            pairs = np.asarray(roc, dtype=float)
            if pairs.ndim == 2 and pairs.shape[0] > 2 and pairs.shape[1] >= 2:
                fpr, tpr = pairs[:, 0], pairs[:, 1]
        except Exception:  # noqa: BLE001
            pass
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    if fpr is not None and tpr is not None:
        ax.plot(np.asarray(fpr, dtype=float), np.asarray(tpr, dtype=float), lw=2,
                color=C.palette_color(0), label="GRN 推断")
        notes = ["图：GRN 边存在性的 ROC 曲线（对比真值网络）。"]
    else:
        names, values, baseline = [], [], []
        for key, label, threshold in (("grn_auroc", "GRN AUROC", 0.5), ("grn_auprc", "GRN AUPRC", 0.0),
                                      ("grn_top100_precision", "Top100 精准率", 0.0),
                                      ("delta_spearman_mean", "扰动 Δ Spearman", 0.3),
                                      ("jaccard_top20_mean", "Top20 基因 Jaccard", 0.0)):
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                names.append(label)
                values.append(float(value))
                baseline.append(threshold)
        if not names:
            _deg_panel(plt, fig, "需要先运行「网络校验」（demo 数据自带真值）", title)
            return _finish(fig, out_path, title, ["缺少校验指标"])
        colors = ["#59a14f" if value >= base else "#c0392b" for value, base in zip(values, baseline)]
        ax.barh(names[::-1], values[::-1], color=colors[::-1], alpha=0.9)
        for index, value in enumerate(values[::-1]):
            ax.annotate(f"{value:.3f}", (value, index), va="center", fontsize=8, xytext=(3, 0),
                        textcoords="offset points")
        ax.set_xlim(0, max(1.0, max(values) * 1.15))
        ax.set_title(title, fontsize=13)
        notes = ["图：GRN 与留出扰动的校验指标（绿=达到通过阈值，红=未达到）。"]
    ax.plot([0, 1], [0, 1], "--", color="#999999", lw=0.8)
    auroc = metrics.get("grn_auroc")
    if isinstance(auroc, (int, float)):
        ax.annotate(f"AUROC = {auroc:.3f}", (0.55, 0.12), fontsize=11)
    ax.set_xlabel("假阳性率")
    ax.set_ylabel("真阳性率")
    ax.set_title(title, fontsize=13)
    return _finish(fig, out_path, title, notes)


# ---------------------------------------------------------------------------
# 16. 通用对比柱
# ---------------------------------------------------------------------------

def _plot_compare_bar(plt, ctx, out_path, params, title):
    items = params.get("items") or []
    sources = []
    for name in ("result", "validate", "response", "screen", "transition", "state", "grn", "perturb"):
        stage = _stage(ctx, name)
        if stage:
            sources.append(stage)
    metrics = {}
    for source in sources:
        for key, value in (source.get("metrics") or {}).items():
            if isinstance(value, (int, float)) and key not in metrics:
                metrics[key] = float(value)
    if not items:
        preferred = ["silhouette", "variance_explained", "grn_auroc", "delta_spearman_mean",
                     "state_shift", "r2", "stability"]
        items = [key for key in preferred if key in metrics]
    names, values = [], []
    for item in items:
        if item in metrics:
            names.append(str(item))
            values.append(metrics[item])
    if not names:
        fig = plt.figure(figsize=DEFAULT_SIZE)
        _deg_panel(plt, fig, "没有可对比的指标：先运行相应步骤", title)
        return _finish(fig, out_path, title, ["缺少指标"])
    fig, ax = plt.subplots(figsize=DEFAULT_SIZE)
    ax.bar(names, values, color=[C.palette_color(index) for index in range(len(names))], alpha=0.88)
    for index, value in enumerate(values):
        ax.annotate(f"{value:.3g}", (index, value), ha="center", va="bottom", fontsize=8)
    ax.set_ylabel("指标值")
    ax.set_title(title, fontsize=13)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    return _finish(fig, out_path, title, ["图：多指标对比柱状图。"])


# ---------------------------------------------------------------------------
# 注册表（放最后，避免函数未定义）
# ---------------------------------------------------------------------------

_HANDLERS = {
    "qc_violin": _plot_qc_violin,
    "state_embedding": _plot_state_embedding,
    "state_embedding_perturb": _plot_state_embedding_perturb,
    "cluster_bar": _plot_cluster_bar,
    "grn_hub_bar": _plot_grn_hub_bar,
    "grn_network": _plot_grn_network,
    "grn_heatmap": _plot_grn_heatmap,
    "perturb_waterfall": _plot_perturb_waterfall,
    "perturb_field": _plot_perturb_field,
    "perturb_heatmap": _plot_perturb_heatmap,
    "screen_rank": _plot_screen_rank,
    "response_scatter": _plot_response_scatter,
    "transition_matrix": _plot_transition_matrix,
    "transition_graph": _plot_transition_graph,
    "validate_roc": _plot_validate_roc,
    "compare_bar": _plot_compare_bar,
}


def kinds() -> list[str]:
    return list(_HANDLERS)
