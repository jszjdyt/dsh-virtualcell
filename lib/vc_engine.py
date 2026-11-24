#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — Python worker（长驻子进程，唯一计算入口）

协议（CONTRACT §2）：
  stdin  : 一行一个 JSON 请求 {"id":"..","cmd":"..","args":{..}}
  stdout : 一行一个 JSON 响应 {"id":"..","ok":true,"result":{..}} /
           {"id":"..","ok":false,"error":{"code":"..","message":".."}}
           可选 {"id":"..","event":"progress","message":"..","pct":0-100}
  stdout 只允许协议行；日志一律 stderr。

启动：python3 -u lib/vc_engine.py --workdir <abs_dir>

结构：
  - Worker：状态机 + 数据集注册 + 步骤调度 + run 留存 + 自检/智能分析/参数推荐/预设流程/导出；
  - 各计算模块（vc_io / vc_state / vc_grn / vc_response / vc_plot）按 CONTRACT 的签名实现，
    本文件只做编排：调模块 → 落盘（npz/csv/png）→ 写 run 记录 → 返回可 JSON 的结果。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import traceback

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VERSION = "0.1.0"
STEP_ORDER = ["qc", "state", "annotate", "grn", "validate", "perturb", "screen", "response", "transition", "insight"]

# STEP_REQUIRES 与 CONTRACT §4 一致
STEP_REQUIRES = {
    "qc": [],
    "state": ["qc"],
    "annotate": ["state"],
    "grn": ["state"],
    "validate": ["grn"],
    "perturb": ["grn"],
    "screen": ["grn"],
    "response": ["state"],
    "transition": ["state"],
    "insight": ["state"],
}

# ---------------------------------------------------------------------------
# 步骤元数据（UI 左栏卡片 / Agent 工具说明的唯一来源）
# ---------------------------------------------------------------------------

def _p(key, label, type_, default, **extra):
    item = {"key": key, "label": label, "type": type_, "default": default}
    item.update(extra)
    return item


STEPS = [
    {
        "id": "qc",
        "title": "质控",
        "icon": "qc",
        "requires": [],
        "why": "去掉低质量细胞（检出基因太少、线粒体占比过高），避免噪声细胞把状态空间拉歪。",
        "cost": "约 1–5 秒",
        "outputs": ["细胞/基因过滤掩码", "质控分布图"],
        "params": [
            _p("min_genes", "最少检出基因数", "number", 200, min=0, max=20000, step=50,
               effect="太低会留下空液滴，太高会误杀小细胞", risk="medium"),
            _p("max_genes", "最多检出基因数（0=不限）", "number", 0, min=0, max=20000, step=100,
               effect="用来去掉双细胞/多细胞", risk="low", advanced=True),
            _p("max_mito", "线粒体占比上限 (%)", "number", 20, min=0, max=100, step=1,
               effect="肿瘤/代谢样本可放宽到 25–30", risk="medium"),
            _p("min_cells", "基因至少在多少细胞中表达", "number", 3, min=0, max=1000, step=1,
               effect="过滤低表达基因", risk="low", advanced=True),
        ],
    },
    {
        "id": "state",
        "title": "状态表征",
        "icon": "state",
        "requires": ["qc"],
        "why": "把表达谱压到低维状态空间（每个细胞一个坐标），并建相似细胞图。之后所有虚拟扰动都在这个空间里推演。",
        "cost": "约 5–40 秒",
        "outputs": ["状态隐向量", "细胞聚类", "二维状态图", "标记基因"],
        "params": [
            _p("n_hvg", "高变基因数", "number", 1500, min=200, max=8000, step=100,
               effect="太少丢失状态信号，太多引入噪声", risk="medium"),
            _p("n_pcs", "主成分数", "number", 30, min=5, max=200, step=5,
               effect="决定状态空间的维度", risk="medium"),
            _p("n_neighbors", "近邻数", "number", 15, min=3, max=100, step=1,
               effect="影响聚类颗粒度与转移矩阵", risk="low"),
            _p("cluster", "聚类方式", "select", "auto",
               options=[{"value": "auto", "label": "自动（推荐）"}, {"value": "kmeans", "label": "k-means"},
                        {"value": "leiden", "label": "Leiden（需 scanpy）"}], risk="low"),
            _p("resolution", "聚类分辨率", "number", 0.8, min=0.1, max=3.0, step=0.1,
               effect="越大簇越多；内置引擎会映射为簇数", risk="medium"),
            _p("n_clusters", "指定簇数（0=自动）", "number", 0, min=0, max=50, step=1,
               effect="知道状态数时直接指定", risk="low", advanced=True),
            _p("latent", "隐空间方法", "select", "pca",
               options=[{"value": "pca", "label": "PCA（推荐）"}, {"value": "nmf", "label": "NMF"}], risk="low",
               advanced=True),
            _p("embedding", "二维嵌入", "select", "auto",
               options=[{"value": "auto", "label": "自动（推荐）"}, {"value": "pca", "label": "PCA"},
                        {"value": "tsne", "label": "t-SNE"}, {"value": "umap", "label": "UMAP（需 scanpy）"}],
               risk="low", advanced=True),
        ],
    },
    {
        "id": "annotate",
        "title": "状态注释",
        "icon": "annotate",
        "requires": ["state"],
        "why": "用内置参考 marker 面板给每个簇一个可读的名字，后面说“细胞从 T 细胞偏向单核”才有意义。",
        "cost": "约 1–5 秒",
        "outputs": ["每簇状态标签", "置信度与证据基因"],
        "params": [
            _p("panel", "参考面板", "select", "builtin",
               options=[{"value": "builtin", "label": "内置 20 类状态面板"}], risk="low"),
            _p("min_score", "最低置信度", "number", 0.3, min=0.0, max=2.0, step=0.05,
               effect="低于阈值标为 Unknown", risk="low"),
            _p("method", "打分方式", "select", "zscore",
               options=[{"value": "zscore", "label": "z-score 均值"}, {"value": "ucell", "label": "UCell 秩和"}],
               risk="low", advanced=True),
            _p("column", "写入的列名", "text", "cell_state", risk="low", advanced=True),
        ],
    },
    {
        "id": "grn",
        "title": "调控网络",
        "icon": "grn",
        "requires": ["state"],
        "why": "推断“谁调控谁”：转录因子 → 靶基因的有向网络。虚拟敲除就是沿这个网络把扰动传播出去。",
        "cost": "约 10–120 秒",
        "outputs": ["有向调控边", "每个状态的网络权重", "hub 因子"],
        "params": [
            _p("method", "推断方法", "select", "ridge",
               options=[{"value": "ridge", "label": "每簇 Ridge 回归（推荐）"}], risk="low"),
            _p("prior", "候选边先验", "select", "coexpression",
               options=[{"value": "coexpression", "label": "共表达筛选（无需额外数据）"},
                        {"value": "user", "label": "上传的 GRN 先验"},
                        {"value": "mixed", "label": "两者合并"}], risk="medium"),
            _p("alpha", "正则强度", "number", 0.1, min=0.001, max=10, step=0.01,
               effect="越大网络越稀疏、越保守", risk="medium"),
            _p("top_targets", "最多靶基因数", "number", 2000, min=100, max=20000, step=100,
               effect="越大越全，也越慢", risk="low", advanced=True),
            _p("min_cells", "每簇最少细胞数", "number", 30, min=5, max=500, step=5,
               effect="不足的簇改用全局模型", risk="low", advanced=True),
            _p("n_boot", "Bootstrap 次数（稳定性）", "number", 0, min=0, max=20, step=1,
               effect=">0 时额外估计边的稳定度，耗时线性增加", risk="low", advanced=True),
        ],
    },
    {
        "id": "validate",
        "title": "网络校验",
        "icon": "validate",
        "requires": ["grn"],
        "why": "用已知真值（demo 数据自带）检查推断的网有多准、虚拟扰动方向对不对。没有真值时会提示改用 Bootstrap 稳定性。",
        "cost": "约 5–30 秒",
        "outputs": ["GRN AUROC/AUPRC", "扰动 Δ 相关性", "通过/不通过结论"],
        "params": [
            _p("min_edges", "最少边数", "number", 20, min=1, max=1000, step=1, risk="low", advanced=True),
            _p("top_k", "Top-K 精准率", "number", 100, min=10, max=1000, step=10, risk="low", advanced=True),
        ],
    },
    {
        "id": "perturb",
        "title": "虚拟扰动",
        "icon": "perturb",
        "requires": ["grn"],
        "why": "在模型里“敲掉/过表达”一个基因，看细胞状态往哪边偏、哪些下游基因跟着变。这是虚拟细胞的核心动作。",
        "cost": "约 3–30 秒",
        "outputs": ["受影响基因排名", "每簇状态位移箭头", "目标状态预测", "通路富集"],
        "params": [
            _p("gene", "扰动基因", "text", "", effect="留空会自动选网络里的 hub 转录因子", risk="low"),
            _p("mode", "扰动方式", "select", "knockout",
               options=[{"value": "knockout", "label": "敲除（表达置 0）"},
                        {"value": "knockdown", "label": "敲低（按强度下调）"},
                        {"value": "overexpression", "label": "过表达（上调）"}], risk="low"),
            _p("strength", "扰动强度", "number", 1.0, min=0.1, max=3.0, step=0.1,
               effect="敲低/过表达时才有意义", risk="low"),
            _p("cluster", "作用范围", "select", "all",
               options=[{"value": "all", "label": "全部细胞"}], risk="low"),
            _p("n_prop", "网络传播轮数", "number", 3, min=1, max=8, step=1,
               effect="越大传播越远，也越容易放大噪声", risk="medium", advanced=True),
            _p("top_n", "展示受影响基因数", "number", 30, min=5, max=200, step=5, risk="low", advanced=True),
        ],
    },
    {
        "id": "screen",
        "title": "虚拟筛选",
        "icon": "screen",
        "requires": ["grn"],
        "why": "把一批候选 TF 挨个虚拟敲除，按“能把细胞推多远、推得多专一”排序，产出可验证的靶点候选。",
        "cost": "约 10–60 秒",
        "outputs": ["靶点排名", "每个靶点的目标状态"],
        "params": [
            _p("genes", "候选基因", "genes", [], effect="留空=内置全部人类 TF", risk="low"),
            _p("mode", "扰动方式", "select", "knockout",
               options=[{"value": "knockout", "label": "敲除"}, {"value": "overexpression", "label": "过表达"}], risk="low"),
            _p("top_n", "展示前 N 个", "number", 20, min=5, max=100, step=5, risk="low"),
            _p("specificity", "偏好专一性", "boolean", True, effect="开启后只推一个方向的靶点加分", risk="low"),
        ],
    },
    {
        "id": "response",
        "title": "响应预测",
        "icon": "response",
        "requires": ["state"],
        "why": "如果你有真实的 Perturb-seq 扰动数据，就在隐空间里学“扰动→状态变化”的映射并做留一验证；没有就退化成药物/签名投影。",
        "cost": "约 5–40 秒",
        "outputs": ["预测 vs 真实散点", "每个扰动的 R²", "未见扰动预测"],
        "params": [
            _p("method", "预测方法", "select", "additive",
               options=[{"value": "additive", "label": "隐空间加性模型（GEARS-lite）"}], risk="low"),
            _p("nfold", "交叉验证折数", "number", 5, min=2, max=10, step=1, risk="low"),
            _p("latent_dim", "隐空间维度（0=全用）", "number", 0, min=0, max=100, step=5, risk="low", advanced=True),
        ],
    },
    {
        "id": "transition",
        "title": "状态转移",
        "icon": "transition",
        "requires": ["state"],
        "why": "估计细胞状态之间的转移倾向（谁流向谁），给“细胞命运”一个可量化的方向。",
        "cost": "约 1–10 秒",
        "outputs": ["转移矩阵", "源/汇状态", "转移稳定性"],
        "params": [
            _p("method", "估计方法", "select", "knn",
               options=[{"value": "knn", "label": "kNN 邻接 + Sinkhorn（推荐）"},
                        {"value": "sinkhorn", "label": "质心距离 + Sinkhorn"}], risk="low"),
            _p("temperature", "平滑强度", "number", 0.1, min=0.0, max=1.0, step=0.01,
               effect="越大越平滑、越保守", risk="low"),
            _p("k", "近邻数", "number", 10, min=3, max=50, step=1, risk="low", advanced=True),
        ],
    },
    {
        "id": "insight",
        "title": "智能分析",
        "icon": "insight",
        "requires": ["state"],
        "why": "把前面所有结果翻译成人能读的结论：状态结构、调控枢纽、扰动与靶点、以及必须写进讨论的方法学局限。",
        "cost": "约 1–3 秒",
        "outputs": ["结论摘要", "靶点排名", "下一步建议", "局限清单"],
        "params": [
            _p("depth", "分析深度", "select", "standard",
               options=[{"value": "brief", "label": "简版"}, {"value": "standard", "label": "标准"},
                        {"value": "deep", "label": "深度"}], risk="low"),
        ],
    },
]

STEP_MAP = {step["id"]: step for step in STEPS}


def _default_params(step_id: str) -> dict:
    step = STEP_MAP.get(step_id) or {}
    out = {}
    for param in step.get("params", []):
        value = param.get("default")
        out[param["key"]] = list(value) if isinstance(value, list) else value
    return out


def _merge_params(step_id: str, given) -> dict:
    params = _default_params(step_id)
    if isinstance(given, dict):
        for key, value in given.items():
            if key in params and value is not None:
                params[key] = value
    return params


# ---------------------------------------------------------------------------
# 引擎基础设施
# ---------------------------------------------------------------------------

class EngineFailure(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


#: 当前正在执行的请求 id（progress 事件要带 id）
_CURRENT = {"id": None}

#: 可选后端/模型适配器（检测到就用，检测不到就如实标注 + 给安装提示）
ADAPTERS = [
    {"id": "scanpy", "module": "scanpy", "label": "scanpy", "capability": "Leiden 聚类 / UMAP / HVG",
     "hint": "pip install scanpy"},
    {"id": "scvi", "module": "scvi", "label": "scVI", "capability": "概率隐空间 / 差异表达",
     "hint": "pip install scvi-tools"},
    {"id": "celloracle", "module": "celloracle", "label": "CellOracle", "capability": "官方 GRN 扰动模拟",
     "hint": "pip install celloracle"},
    {"id": "gears", "module": "gears", "label": "GEARS", "capability": "扰动响应预测（GNN）",
     "hint": "pip install gears"},
    {"id": "scgpt", "module": "scgpt", "label": "scGPT", "capability": "单细胞基础模型嵌入",
     "hint": "pip install scgpt + 下载权重"},
    {"id": "geneformer", "module": "geneformer", "label": "Geneformer", "capability": "基础模型 in-silico 扰动",
     "hint": "pip install geneformer + 下载权重"},
    {"id": "state", "module": "arc_state", "label": "State (Arc)", "capability": "ST/SE 扰动响应预测",
     "hint": "pip install arc-state + 下载权重"},
]


class Worker:
    def __init__(self, workdir: str):
        self.workdir = workdir
        self.started = time.time()
        self.state = None          # 持久化的小状态
        self.bundles = {}          # dsid -> Bundle（内存缓存）
        self.stages = {}           # dsid -> {step: dict}（含 arrays）
        self.results = {}          # dsid -> {step: result 摘要}
        self.figures = {}          # figid -> {file, dsid, run}
        self.busy = None
        self._engine_info = None
        self._io = None
        self._state_mod = None
        self._grn_mod = None
        self._response_mod = None
        self._plot_mod = None
        self._modules_error = {}

    # -- 基础设施 ---------------------------------------------------------
    def _ensure_dirs(self):
        for sub in ("datasets", "exports", "uploads", "demo", "figures", "mpl-cache"):
            os.makedirs(os.path.join(self.workdir, sub), exist_ok=True)

    def _state_path(self) -> str:
        return os.path.join(self.workdir, "state.json")

    def load_state(self):
        if self.state is not None:
            return self.state
        import vc_common as C

        raw = C.read_json(self._state_path(), None)
        if not isinstance(raw, dict) or not isinstance(raw.get("datasets"), dict):
            raw = {"version": VERSION, "datasets": {}, "active": None, "createdAt": C.now_iso()}
        raw.setdefault("datasets", {})
        raw.setdefault("active", None)
        self.state = raw
        return self.state

    def save_state(self):
        import vc_common as C

        state = self.load_state()
        state["updatedAt"] = C.now_iso()
        state["version"] = VERSION
        C.write_json(self._state_path(), state)
        return state

    def _mod(self, name: str):
        """按需 import 计算模块；失败不致命（返回 None 并记录原因）。"""
        attr = {"vc_io": "_io", "vc_state": "_state_mod", "vc_grn": "_grn_mod",
                "vc_response": "_response_mod", "vc_plot": "_plot_mod"}[name]
        cached = getattr(self, attr)
        if cached is not None:
            return cached
        try:
            module = __import__(name)
            setattr(self, attr, module)
            return module
        except Exception as error:  # noqa: BLE001
            self._modules_error[name] = f"{type(error).__name__}: {error}"
            sys.stderr.write(f"[vc_engine] 模块 {name} 不可用：{self._modules_error[name]}\n")
            return None

    def engine_info(self) -> dict:
        if self._engine_info is not None:
            return self._engine_info
        packages = {}
        try:
            from importlib.metadata import version as _pkg_version
        except Exception:  # noqa: BLE001
            _pkg_version = None
        for name in ("numpy", "pandas", "scipy", "sklearn", "anndata", "h5py", "matplotlib", "PIL"):
            try:
                module = __import__(name)
                raw = getattr(module, "__version__", None)
                if raw is None and _pkg_version:
                    try:
                        raw = _pkg_version(name)
                    except Exception:  # noqa: BLE001
                        raw = "?"
                packages[name] = raw or "?"
            except Exception:  # noqa: BLE001
                packages[name] = None
        adapters = []
        for adapter in ADAPTERS:
            available = False
            version = None
            try:
                module = __import__(adapter["module"])
                available = True
                version = getattr(module, "__version__", "?")
            except Exception:  # noqa: BLE001
                available = False
            adapters.append({**adapter, "available": available, "version": version})
        backend = "builtin"
        if (packages.get("anndata") and self._mod("vc_io")):
            backend = "builtin"
        if any(item["id"] == "scanpy" and item["available"] for item in adapters):
            backend = "scanpy"
        info = {
            "python": sys.executable,
            "pythonVersion": platform.python_version(),
            "platform": platform.platform(),
            "backend": backend,
            "packages": packages,
            "adapters": adapters,
            "workdir": self.workdir,
            "startedAt": self.started,
            "modules": {"vc_io": bool(self._mod("vc_io")), "vc_state": bool(self._mod("vc_state")),
                        "vc_grn": bool(self._mod("vc_grn")), "vc_response": bool(self._mod("vc_response")),
                        "vc_plot": bool(self._mod("vc_plot"))},
            "moduleErrors": dict(self._modules_error),
        }
        self._engine_info = info
        return info

    # -- 数据集 -----------------------------------------------------------
    def _dataset_dir(self, dsid: str) -> str:
        return os.path.join(self.workdir, "datasets", dsid)

    def _meta(self, dsid: str | None = None) -> dict:
        state = self.load_state()
        key = dsid or state.get("active")
        if not key or key not in state["datasets"]:
            import vc_common as C

            raise C.err("not_found", "没有活动数据集，请先导入数据或载入示例数据")
        return state["datasets"][key]

    def _next_ds_id(self) -> str:
        state = self.load_state()
        number = 1
        while f"ds{number}" in state["datasets"]:
            number += 1
        return f"ds{number}"

    def _register_bundle(self, bundle, *, name: str, source: str, raw_path: str,
                         raw_copy: bool, demo: dict | None = None, sha256: str | None = None,
                         bytes_: int | None = None, warnings=None) -> dict:
        import vc_common as C

        state = self.load_state()
        dsid = self._next_ds_id()
        dsdir = C.ensure_dir(self._dataset_dir(dsid))
        for sub in ("raw", "stage", "runs"):
            C.ensure_dir(os.path.join(dsdir, sub))
        meta = {
            "id": dsid,
            "name": str(name or f"dataset-{dsid}"),
            "n_cells": int(bundle.X.shape[0]),
            "n_genes": int(bundle.X.shape[1]),
            "n_tfs": int(len(set(getattr(bundle, "genes", [])) & set(self._tf_universe()))),
            "source": source,
            "sha256": sha256,
            "bytes": int(bytes_ or 0),
            "rawCopy": bool(raw_copy),
            "rawPath": raw_path,
            "createdAt": C.now_iso(),
            "dir": dsdir,
            "demo": demo,
            "steps": {},
            "stepOrder": [],
            "warnings": list(warnings or []),
            "attachments": [],
            "truth": {
                "has_grn_truth": bool(bundle.truth and ("grn" in bundle.truth)),
                "has_perturb_truth": bool(bundle.truth and ("perturb" in bundle.truth)),
                "has_perturb_table": bool(bundle.truth and ("perturb_table" in bundle.truth)),
            },
            "meta": dict(getattr(bundle, "meta", {}) or {}),
        }
        # 落盘：原始计数矩阵（便于与 scanpy/单细胞工作台互通）
        self._write_bundle_h5ad(bundle, os.path.join(dsdir, "bundle.h5ad"))
        state["datasets"][dsid] = meta
        state["active"] = dsid
        self.save_state()
        self.bundles[dsid] = bundle
        self.stages[dsid] = {}
        self.results[dsid] = {}
        return meta

    def _write_bundle_h5ad(self, bundle, path: str):
        io = self._mod("vc_io")
        if not io:
            return
        try:
            io.write_h5ad(bundle, path)
        except Exception as error:  # noqa: BLE001
            sys.stderr.write(f"[vc_engine] 写 h5ad 失败：{error}\n")

    def _load_bundle(self, dsid: str):
        """内存里没有就从磁盘恢复（worker 重启后仍可用）。"""
        if dsid in self.bundles:
            return self.bundles[dsid]
        import vc_common as C

        meta = self._meta(dsid)
        io = self._mod("vc_io")
        if not io:
            raise C.err("internal", "vc_io 模块不可用，无法读取数据")
        demo = meta.get("demo")
        if demo and demo.get("seed") is not None:
            bundle = io.load_demo(seed=int(demo.get("seed") or 0),
                                  n_cells=int(demo.get("n_cells") or 2000),
                                  n_genes=int(demo.get("n_genes") or 1000))
        else:
            path = os.path.join(self._dataset_dir(dsid), "bundle.h5ad")
            if not os.path.exists(path):
                raise C.err("prerequisite", "数据集文件缺失，请重新导入")
            bundle = io.load_dataset(path, data_format="h5ad")
        self.bundles[dsid] = bundle
        self.stages.setdefault(dsid, {})
        return bundle

    def _tf_universe(self):
        try:
            import vc_priors as P

            return P.tf_universe()
        except Exception:  # noqa: BLE001
            return []

    def _stage(self, dsid: str, step: str):
        """取某步骤的 stage（含 arrays），内存没有就从 npz/json 恢复。"""
        stages = self.stages.setdefault(dsid, {})
        if step in stages:
            return stages[step]
        meta = self._meta(dsid)
        entry = (meta.get("steps") or {}).get(step)
        if not entry or entry.get("status") != "ok":
            import vc_common as C

            raise C.err("prerequisite", f"请先运行「{STEP_MAP[step]['title']}」这一步")
        dsdir = self._dataset_dir(dsid)
        stage_dir = os.path.join(dsdir, "stage")
        arrays = _load_npz(os.path.join(stage_dir, f"{step}.npz"))
        summary = _read_json_file(os.path.join(stage_dir, f"{step}.json")) or {}
        stage = dict(summary)
        stage["arrays"] = arrays
        stages[step] = stage
        return stage

    def _persist_stage(self, dsid: str, step: str, result: dict, *, arrays: bool = True):
        dsdir = self._dataset_dir(dsid)
        stage_dir = self._ensure_stage_dir(dsdir)
        arrays_payload = result.get("arrays") or {}
        if arrays and arrays_payload:
            _save_npz(os.path.join(stage_dir, f"{step}.npz"), arrays_payload)
        summary = {key: value for key, value in result.items() if key not in ("arrays", "tables")}
        summary["tables"] = [_table_summary(item) for item in (result.get("tables") or [])]
        _write_json_file(os.path.join(stage_dir, f"{step}.json"), summary)
        stages = self.stages.setdefault(dsid, {})
        stage = dict(summary)
        stage["arrays"] = arrays_payload
        stages[step] = stage

    def _ensure_stage_dir(self, dsdir: str) -> str:
        path = os.path.join(dsdir, "stage")
        os.makedirs(path, exist_ok=True)
        return path

    # -- run 执行 ---------------------------------------------------------
    def _run_recorder(self, dsid: str):
        import vc_common as C

        return C.RunStore(self._dataset_dir(dsid), dsid)

    def _ctx(self, dsid: str, *, include_bundle=True) -> dict:
        """组装出图 ctx（CONTRACT §6.5）。"""
        bundle = self._load_bundle(dsid) if include_bundle else None
        light = None
        if bundle is not None:
            import types

            light = types.SimpleNamespace(
                genes=list(bundle.genes), cells=list(bundle.cells),
                obs=getattr(bundle, "obs", None), truth=getattr(bundle, "truth", None),
                meta=getattr(bundle, "meta", {}),
            )
        ctx = {"bundle": light, "title": ""}
        for step in ("state", "grn", "perturb", "screen", "response", "transition"):
            try:
                ctx[step] = self._stage(dsid, step)
            except Exception:  # noqa: BLE001
                ctx[step] = None
        return ctx

    def run_step(self, step: str, args: dict, *, recipe_mode: bool = False) -> dict:
        import vc_common as C

        if step not in STEP_MAP:
            raise C.err("bad_request", f"未知步骤：{step}")
        dsid = args.get("dataset") or self.load_state().get("active")
        meta = self._meta(dsid)
        params = _merge_params(step, args.get("params"))
        # 依赖检查
        done = set(meta.get("stepOrder") or [])
        missing = [need for need in STEP_REQUIRES.get(step, []) if need not in done]
        if missing:
            titles = "、".join(STEP_MAP[need]["title"] for need in missing)
            raise C.err("prerequisite", f"请先运行「{titles}」", {"missing": missing})

        label = STEP_MAP[step]["title"]
        if step == "perturb":
            label = f"{label}：{params.get('gene') or '自动选 hub'}"
        store = self._run_recorder(dsid)
        run = store.begin(step, label, params)
        started = time.time()
        self.busy = {"step": step, "run": run["id"], "pct": 0, "message": f"{STEP_MAP[step]['title']}…"}
        C.bind_progress(lambda message, pct: self._progress(message, pct))
        try:
            result = self._dispatch_step(step, dsid, meta, params, run)
        except Exception as error:  # noqa: BLE001
            entry = store.finish(run, status="error", result={"summary": str(error), "error": str(error)},
                                 env=self._env_summary(), log=traceback.format_exc())
            self.busy = None
            self.save_state()
            if isinstance(error, C.EngineError):
                raise
            raise C.err("internal", f"{STEP_MAP[step]['title']}失败：{type(error).__name__}: {error}",
                        {"run": entry["id"]}) from error
        finally:
            self.busy = None

        # ---- 后处理：表 → CSV → stage 落盘 → 出图 → 更新 stage json → run 记录 ----
        # 顺序很重要：出图时 vc_plot 会从 ctx 里读 state/grn/perturb 等 stage，
        # 所以必须**先把本步 stage 落盘/入内存，再出图**，否则本步自己的图会画成"缺数据"占位图。
        result.setdefault("metrics", {})
        result.setdefault("warnings", [])
        if not result.get("summary"):
            result["summary"] = self._fallback_summary(step, meta, result)
        persistable = step in ("qc", "state", "annotate", "grn", "validate",
                               "perturb", "screen", "response", "transition")
        if persistable:
            self._persist_stage(dsid, step, result)
        tables = self._write_tables(run, result)
        figures = self._render_figures(dsid, run, result)
        result["tables"] = tables
        result["figures"] = figures
        result["run"] = run["id"]
        if persistable:
            self._persist_stage(dsid, step, result, arrays=False)

        meta = self._meta(dsid)
        entry = {
            "status": "ok",
            "at": C.now_iso(),
            "ms": int((time.time() - started) * 1000),
            "summary": result.get("summary"),
            "metrics": C.jsonable(result.get("metrics") or {}),
            "warnings": list(result.get("warnings") or [])[:20],
        }
        meta.setdefault("steps", {})[step] = entry
        order = [item for item in (meta.get("stepOrder") or []) if item != step]
        order.append(step)
        meta["stepOrder"] = order
        # results 摘要（UI 直接渲染；不含 arrays / 表行 / 全部边）
        self.results.setdefault(dsid, {})[step] = self._result_digest(step, result)
        self.save_state()

        finished = store.finish(run, status="ok", result=result, env=self._env_summary(),
                                log=result.get("log") or "", inputs=self._inputs_of(dsid))
        result["run"] = finished["id"]
        result["suggest"] = self._next_suggestion(dsid)
        return result

    def _progress(self, message: str, pct):
        request_id = _CURRENT.get("id")
        if request_id is None:
            return
        _emit({"id": request_id, "event": "progress", "message": str(message),
               **({"pct": float(pct)} if pct is not None else {})})
        if self.busy is not None:
            self.busy["message"] = str(message)
            if pct is not None:
                self.busy["pct"] = pct

    def _env_summary(self) -> dict:
        info = self.engine_info()
        return {"backend": info.get("backend"), "packages": info.get("packages"),
                "pythonVersion": info.get("pythonVersion"), "seed": 0}

    def _inputs_of(self, dsid: str) -> dict:
        import vc_common as C

        out = {}
        stage_dir = os.path.join(self._dataset_dir(dsid), "stage")
        for name in ("qc", "state", "grn"):
            path = os.path.join(stage_dir, f"{name}.npz")
            if os.path.exists(path):
                out[name] = C.sha256_of(path)
        return out

    def _dispatch_step(self, step: str, dsid: str, meta: dict, params: dict, run: dict) -> dict:
        import vc_common as C

        bundle = self._load_bundle(dsid)
        if step == "qc":
            module = self._require("vc_state")
            C.progress("质控指标…", 10)
            return module.run_qc(bundle, params)
        if step == "state":
            module = self._require("vc_state")
            C.progress("构建状态空间…", 5)
            result = module.run_state(bundle, params)
            result.setdefault("figures", [{"kind": "state_embedding", "title": "细胞状态空间"},
                                           {"kind": "cluster_bar", "title": "状态规模"}])
            return result
        if step == "annotate":
            module = self._require("vc_state")
            C.progress("打分参考 marker…", 10)
            return module.run_annotate(bundle, self._stage(dsid, "state"), params)
        if step == "grn":
            module = self._require("vc_grn")
            C.progress("推断调控网络…", 5)
            return module.run_grn(bundle, self._stage(dsid, "state"), params)
        if step == "validate":
            module = self._require("vc_grn")
            if not getattr(bundle, "truth", None) or "grn" not in (bundle.truth or {}):
                raise C.err("prerequisite",
                            "当前数据集没有 GRN 真值，无法做准确性校验",
                            {"hint": "用「载入示例数据」的 demo 数据可完成校验；真实数据请用 n_boot>0 的 Bootstrap 稳定性替代"})
            return module.run_validate(bundle, self._stage(dsid, "state"), self._stage(dsid, "grn"), params)
        if step == "perturb":
            module = self._require("vc_grn")
            gene = str(params.get("gene") or "").strip()
            if not gene:
                gene = self._auto_perturb_gene(dsid)
                params["gene"] = gene
                run["label"] = f"{STEP_MAP['perturb']['title']}：{gene}（自动选 hub）"
            else:
                genes = {str(g).upper() for g in bundle.genes}
                if gene.upper() not in genes:
                    raise C.err("bad_request", f"基因 {gene} 不在当前数据集的基因列表里",
                                {"hint": "检查拼写，或用 action=preview 看基因名"})
                params["gene"] = gene.upper()
            C.progress(f"模拟 {params['gene']} 扰动…", 20)
            return module.run_perturb(bundle, self._stage(dsid, "state"), self._stage(dsid, "grn"), params)
        if step == "screen":
            module = self._require("vc_grn")
            C.progress("批量虚拟敲除…", 5)
            return module.run_screen(bundle, self._stage(dsid, "state"), self._stage(dsid, "grn"), params)
        if step == "response":
            module = self._require("vc_response")
            table = self._response_table(dsid, bundle)
            return module.run_response(bundle, self._stage(dsid, "state"), table, params)
        if step == "transition":
            module = self._require("vc_state")
            return module.run_transition(bundle, self._stage(dsid, "state"), params)
        if step == "insight":
            return self._insight(dsid, params)
        raise C.err("bad_request", f"未知步骤：{step}")

    def _require(self, name: str):
        import vc_common as C

        module = self._mod(name)
        if module is None:
            detail = self._modules_error.get(name) or "未加载"
            raise C.err("internal", f"{name} 模块不可用：{detail}")
        return module

    def _auto_perturb_gene(self, dsid: str) -> str:
        """模糊模式：没有指定基因时，选 GRN hub 里出度最高、且在数据里有表达的 TF。"""
        try:
            grn = self._stage(dsid, "grn")
        except Exception:  # noqa: BLE001
            import vc_common as C

            raise C.err("prerequisite", "请先运行「调控网络」")
        edges = grn.get("edges") or []
        score = {}
        weights = {}
        for edge in edges:
            tf = str(edge.get("tf") or "")
            if not tf:
                continue
            score[tf] = score.get(tf, 0) + 1
            weights[tf] = weights.get(tf, 0.0) + abs(float(edge.get("score") or 0))
        if not score:
            raise vc_err("bad_request", "网络里没有可用 TF，请手动指定 gene")
        ranked = sorted(score.items(), key=lambda item: (-item[1], -weights.get(item[0], 0.0)))
        return str(ranked[0][0]).upper()

    def _response_table(self, dsid: str, bundle):
        meta = self._meta(dsid)
        # 1) 显式挂载的扰动表
        for item in meta.get("attachments") or []:
            if item.get("kind") == "perturb_table" and item.get("dataPath"):
                import pandas as pd

                path = item["dataPath"]
                try:
                    frame = pd.read_csv(path)
                    return frame
                except Exception:  # noqa: BLE001
                    continue
        # 2) demo 自带
        truth = getattr(bundle, "truth", None) or {}
        table = truth.get("perturb_table")
        if table is not None:
            return table
        return None

    def _result_digest(self, step: str, result: dict) -> dict:
        """给 /state 的轻量摘要：不带 arrays、edges 只留 top20、affected 只留 top20。"""
        import vc_common as C

        keep = ("summary", "metrics", "warnings", "notes", "gene", "mode", "strength",
                "state_shift", "target_state", "source_state", "backend", "n_clusters", "n_pcs",
                "validity", "verdict", "grn_source", "cluster_labels", "cluster_sizes",
                "affected", "per_cluster", "enrichment", "sources", "sinks", "net_flow")
        digest = {key: C.jsonable(result.get(key)) for key in keep if key in result}
        digest["figures"] = result.get("figures") or []
        digest["tables"] = result.get("tables") or []
        digest["run"] = result.get("run")
        digest["at"] = C.now_iso()
        if result.get("edges"):
            digest["edges"] = result["edges"][:20]
        if result.get("affected"):
            digest["affected"] = result["affected"][:20]
        if result.get("per_cluster"):
            digest["per_cluster"] = result["per_cluster"][:20]
        if result.get("ranked"):
            digest["ranked"] = result["ranked"][:20]
        if result.get("per_pert"):
            digest["per_pert"] = result["per_pert"][:20]
        if result.get("cluster_labels"):
            digest["cluster_labels"] = result["cluster_labels"]
        if result.get("cluster_sizes"):
            digest["cluster_sizes"] = result["cluster_sizes"]
        if result.get("enrichment"):
            digest["enrichment"] = result["enrichment"][:20]
        if result.get("sections"):
            digest["sections"] = result["sections"]
        if result.get("headline"):
            digest["headline"] = result["headline"]
        if result.get("ranked_targets"):
            digest["ranked_targets"] = result["ranked_targets"][:20]
        if result.get("next_actions"):
            digest["next_actions"] = result["next_actions"]
        if result.get("caveats"):
            digest["caveats"] = result["caveats"]
        return digest

    def _fallback_summary(self, step: str, meta: dict, result: dict) -> str:
        metrics = result.get("metrics") or {}
        parts = [f"{STEP_MAP[step]['title']}完成"]
        if metrics.get("n_clusters"):
            parts.append(f"{metrics['n_clusters']} 个状态")
        if metrics.get("n_edges"):
            parts.append(f"{metrics['n_edges']} 条调控边")
        if metrics.get("n_affected"):
            parts.append(f"{metrics['n_affected']} 个受影响基因")
        return "；".join(parts) + "。"

    # -- 表/图/导出 -------------------------------------------------------
    def _write_tables(self, run: dict, result: dict) -> list:
        import vc_common as C

        entries = []
        run_dir = run["runDir"]
        for item in result.get("tables") or []:
            if not isinstance(item, dict):
                continue
            table_id = C.safe_name(item.get("id") or "table", "table")
            rows = item.get("rows")
            if rows is None:
                continue
            path = os.path.join(run_dir, "tables", f"{table_id}.csv")
            try:
                C.write_table(path, rows, item.get("columns"))
            except Exception as error:  # noqa: BLE001
                sys.stderr.write(f"[vc_engine] 写表 {table_id} 失败：{error}\n")
                continue
            entries.append({
                "id": table_id,
                "title": item.get("title") or table_id,
                "path": path,
                "file": table_id + ".csv",
                "rows": int(C.table_preview(rows).get("total") or 0),
                "preview": C.table_preview(rows, limit=6),
            })
        return entries

    def _render_figures(self, dsid: str, run: dict, result: dict) -> list:
        import vc_common as C

        plot = self._mod("vc_plot")
        specs = result.get("figures") or []
        if not specs or not plot:
            if specs and not plot:
                result.setdefault("warnings", []).append("vc_plot 模块不可用，本次未出图")
            return []
        entries = []
        ctx = self._ctx(dsid)
        ctx["result"] = result
        seen = set()
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("kind") or "")
            if not kind or kind in seen:
                continue
            seen.add(kind)
            title = str(spec.get("title") or kind)
            filename = f"{C.safe_name(kind, 'figure')}.png"
            path = os.path.join(run["runDir"], "figures", filename)
            ctx["title"] = title
            try:
                C.progress(f"出图：{title}", 70 + min(25, index * 5))
                info = plot.plot(kind, ctx, path, spec.get("params") or {})
            except Exception as error:  # noqa: BLE001
                sys.stderr.write(f"[vc_engine] 出图 {kind} 失败：{error}\n")
                result.setdefault("warnings", []).append(f"图 {kind} 生成失败：{error}")
                continue
            rel = f"{dsid}/{run['id']}/{filename}"
            entry = {
                "id": rel,
                "kind": kind,
                "title": title,
                "path": path,
                "width": int(info.get("width") or 0),
                "height": int(info.get("height") or 0),
                "url": f"/virtualcell/figure?id={_quote(rel)}",
                "notes": info.get("notes") or [],
            }
            self.figures[rel] = {"file": path, "dsid": dsid, "run": run["id"]}
            entries.append(entry)
        return entries

    # -- 状态与 next 建议 --------------------------------------------------
    def state_payload(self) -> dict:
        import vc_common as C

        state = self.load_state()
        datasets = []
        for dsid, meta in state["datasets"].items():
            steps = meta.get("steps") or {}
            datasets.append({
                **{key: value for key, value in meta.items() if key not in ("meta",)},
                "steps_done": [item for item in (meta.get("stepOrder") or [])],
                "steps_summary": {key: value.get("summary") for key, value in steps.items()},
            })
        active = state.get("active")
        results = {}
        if active:
            results = self.results.get(active) or self._restore_results(active)
        return C.jsonable({
            "ok": True,
            "version": VERSION,
            "engine": self.engine_info(),
            "datasets": datasets,
            "active": active,
            "runs": self._recent_runs(active, 12) if active else [],
            "launchAt": self.started,
            "busy": self.busy,
            "results": results,
            "stepsDone": (self._meta(active).get("stepOrder") if active else []) or [],
            "suggestedStep": self._next_suggestion(active).get("next") if active else "qc",
            "lastRun": (self._recent_runs(active, 1) or [{}])[0].get("id") if active else None,
            "steps": STEPS,
            "adapters": self.engine_info().get("adapters"),
        })

    def _restore_results(self, dsid: str) -> dict:
        """worker 重启后从 stage/*.json 恢复 results 摘要。"""
        out = {}
        stage_dir = os.path.join(self._dataset_dir(dsid), "stage")
        for step in STEP_ORDER:
            path = os.path.join(stage_dir, f"{step}.json")
            if not os.path.exists(path):
                continue
            data = _read_json_file(path) or {}
            data.pop("arrays", None)
            out[step] = data
        self.results[dsid] = out
        return out

    def _recent_runs(self, dsid: str, limit: int = 12) -> list:
        if not dsid:
            return []
        print_root = os.path.join(self._dataset_dir(dsid), "runs")
        rows = _read_json_file(os.path.join(print_root, "index.json")) or []
        if not isinstance(rows, list):
            return []
        return rows[-int(limit):][::-1]

    def _next_suggestion(self, dsid: str | None) -> dict:
        import vc_common as C

        if not dsid:
            return {"next": "open", "why": "先导入数据或载入示例数据"}
        meta = self._meta(dsid)
        done = set(meta.get("stepOrder") or [])
        for step in STEP_ORDER:
            if step not in done and all(need in done for need in STEP_REQUIRES.get(step, [])):
                params = self._recommend(step, dsid).get("params") or []
                recommended = {item["key"]: item.get("recommended") for item in params
                               if item.get("recommended") not in (None, "")}
                return {"next": step, "why": f"下一步是「{STEP_MAP[step]['title']}」：{STEP_MAP[step]['why']}",
                        "params": recommended}
        return {"next": "insight", "why": "流程已跑完，可看「智能分析」或导出报告",
                "params": {}}

    # -- 参数推荐（参数辅助调整的核心） ----------------------------------
    def _recommend(self, step: str, dsid: str) -> dict:
        import vc_common as C

        meta = self._meta(dsid)
        n_cells = int(meta.get("n_cells") or 0)
        n_genes = int(meta.get("n_genes") or 0)
        notes = []
        out = []
        for param in STEP_MAP[step].get("params", []):
            item = dict(param)
            item["recommended"] = param.get("default")
            item["why"] = "默认值适用于多数数据"
            item["risk"] = param.get("risk") or "low"
            key = param["key"]
            if step == "qc":
                if key == "min_genes":
                    value = 200 if n_cells > 500 else 100
                    item.update(recommended=value, why=f"{n_cells:,} 个细胞规模下，200 个检出基因是常用下限")
                elif key == "max_mito":
                    value = 20
                    item.update(recommended=value, why="一般组织 20%；肿瘤/代谢样本可放宽到 25–30%")
            elif step == "state":
                if key == "n_hvg":
                    value = int(min(2000, max(300, n_genes - 1)))
                    item.update(recommended=value,
                                why=f"基因总数只有 {n_genes:,}，HVG 取 {value:,}（不超过基因数的 2/3）")
                elif key == "n_pcs":
                    value = 30 if n_cells > 1000 else 20
                    item.update(recommended=value, why=f"{n_cells:,} 个细胞建议 {value} 个 PC，兼顾分辨率与稳定性")
                elif key == "n_neighbors":
                    value = max(5, min(30, int(round(n_cells ** 0.5 / 2)) + 5))
                    item.update(recommended=value, why=f"按细胞数 {n_cells:,} 估算的邻域大小")
                elif key == "resolution":
                    value = 0.8
                    item.update(recommended=value, why="0.8 是常见起点；簇太多就调低")
            elif step == "grn":
                if key == "alpha":
                    value = 0.1 if n_cells < 2000 else 0.3
                    item.update(recommended=value,
                                why=f"{n_cells:,} 个细胞时用 {value} 的正则强度，避免每个状态拟合过拟合")
                elif key == "top_targets":
                    value = int(min(2000, max(200, n_genes)))
                    item.update(recommended=value, why=f"取 {value:,} 个靶基因，覆盖大部分表达基因")
            elif step == "perturb":
                if key == "gene":
                    try:
                        value = self._auto_perturb_gene(dsid)
                        item.update(recommended=value, why=f"网络里出度最高的 TF 是 {value}，适合作为默认扰动目标")
                    except Exception:  # noqa: BLE001
                        pass
                elif key == "n_prop":
                    value = 3
                    item.update(recommended=value, why="传播 3 轮通常覆盖直接 + 二级下游，过多会放大噪声")
            elif step == "response":
                if key == "nfold":
                    n_pert = 5
                    try:
                        table = self._response_table(dsid, self._load_bundle(dsid))
                        if table is not None:
                            n_pert = max(2, int(table["perturbation"].nunique()) - 1)
                    except Exception:  # noqa: BLE001
                        pass
                    value = max(2, min(5, n_pert))
                    item.update(recommended=value, why=f"只有 {n_pert} 个扰动，折数取 {value} 更稳（留一法）")
            elif step == "transition":
                if key == "temperature":
                    item.update(recommended=0.1, why="0.1 的平滑强度在保真与稳定之间较平衡")
            out.append(item)
        notes.append("推荐值是启发式建议（基于数据维度与规模），不是最优解；"
                     "可用 /sweep 做参数扫描再定。")
        return C.jsonable({"ok": True, "step": step, "params": out, "notes": notes,
                           "data": {"n_cells": n_cells, "n_genes": n_genes}})

    # -- 出图 / 扫描 -------------------------------------------------------
    def plot(self, args: dict) -> dict:
        import vc_common as C

        dsid = args.get("dataset") or self.load_state().get("active")
        meta = self._meta(dsid)
        kind = str(args.get("kind") or "").strip()
        if not kind:
            raise C.err("bad_request", "缺少 kind")
        plot = self._require("vc_plot")
        friendly = _plot_title(kind)
        run = self._run_recorder(dsid).begin("plot", f"出图：{friendly}", args.get("params") or {})
        ctx = self._ctx(dsid)
        need = _plot_step(kind)
        if need:
            try:
                ctx["result"] = self._stage(dsid, need)
            except Exception as error:  # noqa: BLE001
                sys.stderr.write(f"[vc_engine] 出图 {kind} 缺少上游 {need}：{error}\n")
        path = os.path.join(run["runDir"], "figures", f"{C.safe_name(kind, 'figure')}.png")
        ctx["title"] = str(args.get("title") or friendly)
        info = plot.plot(kind, ctx, path, args.get("params") or {})
        rel = f"{dsid}/{run['id']}/{os.path.basename(path)}"
        entry = {
            "id": rel, "kind": kind, "title": ctx["title"], "path": path,
            "width": int(info.get("width") or 0), "height": int(info.get("height") or 0),
            "url": f"/virtualcell/figure?id={_quote(rel)}", "notes": info.get("notes") or [],
        }
        self.figures[rel] = {"file": path, "dsid": dsid, "run": run["id"]}
        self._run_recorder(dsid).finish(run, status="ok", result={"summary": f"出图 {kind}", "figures": [entry]})
        return C.jsonable({"ok": True, "figure": entry, "run": run["id"]})

    def sweep(self, args: dict) -> dict:
        """参数扫描：对某参数的一组取值重复跑，比较指标（不影响已有 stage）。"""
        import vc_common as C

        step = str(args.get("step") or "")
        param = str(args.get("param") or "")
        if step not in STEP_MAP or not param:
            raise C.err("bad_request", "sweep 需要 step 与非空 param")
        values = args.get("values")
        if not values:
            spec = next((item for item in STEP_MAP[step]["params"] if item["key"] == param), None)
            if not spec:
                raise C.err("bad_request", f"步骤 {step} 没有参数 {param}")
            base = float(spec.get("default") or 1)
            low, high = spec.get("min"), spec.get("max")
            if low is not None and high is not None:
                values = [round(low + (high - low) * frac, 3) for frac in (0.15, 0.35, 0.55, 0.75)]
            else:
                values = [base * factor for factor in (0.5, 1.0, 2.0)]
        dsid = args.get("dataset") or self.load_state().get("active")
        base_params = _merge_params(step, args.get("params"))
        rows = []
        for value in values:
            params = dict(base_params)
            params[param] = value
            try:
                result = self._dispatch_step(step, dsid, self._meta(dsid), params, {"id": "sweep"})
            except Exception as error:  # noqa: BLE001
                rows.append({"value": value, "status": "error", "error": str(error)})
                continue
            metrics = C.jsonable(result.get("metrics") or {})
            rows.append({"value": value, "status": "ok", **metrics})
            C.progress(f"扫描 {param}={value} 完成", None)
        # 选最优：按 step 的主指标
        primary = {"state": "silhouette", "grn": "cv_r2", "validate": "grn_auroc",
                   "transition": "stability"}.get(step)
        best = None
        if primary:
            candidates = [row for row in rows if row.get("status") == "ok" and isinstance(row.get(primary), (int, float))]
            if candidates:
                best = max(candidates, key=lambda row: float(row[primary]))
        return C.jsonable({"ok": True, "step": step, "param": param, "rows": rows,
                           "primary": primary, "best": best,
                           "summary": (f"扫描 {param}：{len(rows)} 组；"
                                       + (f"最优 {primary}={best[primary]:.3f} @ {param}={best['value']}"
                                          if best else "各组指标不可比"))})

    # -- attach / preview / inspect ---------------------------------------
    def attach(self, args: dict) -> dict:
        import vc_common as C

        dsid = args.get("dataset") or self.load_state().get("active")
        meta = self._meta(dsid)
        kind = str(args.get("kind") or "").strip()
        if kind not in ("perturb_table", "gene_set", "grn_prior", "drug_signature"):
            raise C.err("bad_request", f"不支持的附件类型：{kind}")
        path = str(args.get("path") or "").strip()
        if not path or not os.path.exists(path):
            raise C.err("bad_request", "附件路径不存在", {"path": path})
        import pandas as pd

        attach_dir = C.ensure_dir(os.path.join(self._dataset_dir(dsid), "attachments"))
        target = os.path.join(attach_dir, f"{C.safe_name(os.path.basename(path), 'attachment')}")
        import shutil

        shutil.copyfile(path, target)
        summary = {"rows": 0, "columns": []}
        try:
            if kind == "gene_set" and path.lower().endswith((".gmt",)):
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    summary = {"sets": len([line for line in handle if line.strip()])}
            else:
                frame = pd.read_csv(target, sep=None, engine="python") if not path.lower().endswith(".h5ad") \
                    else pd.read_hdf(target) if False else pd.read_csv(target)
                summary = {"rows": int(frame.shape[0]), "columns": [str(c) for c in frame.columns[:30]]}
        except Exception as error:  # noqa: BLE001
            summary = {"rows": 0, "columns": [], "note": f"预览失败：{error}"}
        entry = {
            "id": f"at{len(meta.get('attachments') or []) + 1}",
            "kind": kind,
            "name": str(args.get("name") or os.path.basename(path)),
            "path": path,
            "dataPath": target,
            "sha256": C.sha256_of(target),
            "bytes": os.path.getsize(target),
            "createdAt": C.now_iso(),
            "summary": summary,
        }
        meta.setdefault("attachments", []).append(entry)
        self.save_state()
        hint = {"perturb_table": "现在可以运行「响应预测」",
                "gene_set": "可用于签名投影与富集",
                "grn_prior": "在「调控网络」参数里把先验改为「上传的 GRN 先验」",
                "drug_signature": "在「响应预测」里会用它做签名投影"}.get(kind, "")
        return C.jsonable({"ok": True, "attachment": entry, "hint": hint})

    def preview(self, args: dict) -> dict:
        """基因/细胞预览 +（有状态表征时）交互散点用的二维坐标。

        坐标走 /preview 而不是 /state：/state 会被 UI 每 1.5 s 轮询，塞 2–6 千个点会白刷流量。
        """
        import vc_common as C

        dsid = args.get("dataset") or self.load_state().get("active")
        bundle = self._load_bundle(dsid)
        n_genes = int(args.get("n_genes") or 30)
        n_cells = int(args.get("n_cells") or 12)
        genes = list(bundle.genes)[: max(1, n_genes)]
        cells = list(bundle.cells)[: max(1, n_cells)]
        columns = getattr(bundle.obs, "columns", None)
        obs_cols = [str(item) for item in columns] if columns is not None else []
        payload = {"ok": True, "genes": genes, "cells": cells, "obs_columns": obs_cols,
                   "n_genes": len(bundle.genes), "n_cells": len(bundle.cells)}
        try:
            state = self._stage(dsid, "state")
        except Exception:  # noqa: BLE001
            state = None
        if state:
            arrays = state.get("arrays") or {}
            embedding = arrays.get("embedding")
            clusters = arrays.get("clusters")
            labels = state.get("cluster_labels") or []
            if embedding is not None and clusters is not None:
                coords = np.asarray(embedding, dtype=float)
                ids = np.asarray(clusters).ravel().astype(int)
                limit = int(args.get("max_points") or 4000)
                index = np.arange(coords.shape[0])
                if coords.shape[0] > limit:
                    index = np.sort(np.random.default_rng(0).choice(coords.shape[0], size=limit, replace=False))
                names = [str(labels[item]) if 0 <= item < len(labels) else str(item) for item in ids]
                payload["embedding"] = [
                    {"x": C.fnum(coords[item, 0], 3), "y": C.fnum(coords[item, 1], 3),
                     "cluster": names[item]} for item in index
                ]
                payload["clusters"] = list(labels)
                payload["embeddingSampled"] = bool(coords.shape[0] > limit)
                payload["embeddingTotal"] = int(coords.shape[0])
                # 注释列（按细胞），供“按注释着色”
                cell_state = (state.get("cells_columns") or {}).get("cell_state")
                if cell_state is not None:
                    try:
                        payload["cell_state"] = [str(cell_state[item]) for item in index]
                    except Exception:  # noqa: BLE001
                        pass
        return payload

    def inspect(self, args: dict) -> dict:
        import vc_common as C

        io = self._require("vc_io")
        path = str(args.get("path") or "").strip()
        if not path:
            raise C.err("bad_request", "缺少 path")
        info = io.inspect_path(
            path, data_format=args.get("data_format") or "auto", member=args.get("member"),
            transpose=bool(args.get("transpose")), object=args.get("object"), sheet=args.get("sheet"),
            gene_col=args.get("gene_col"), cache_dir=os.path.join(self.workdir, "uploads"),
        )
        return C.jsonable({"ok": True, **info})

    # -- 自检 / 智能分析 --------------------------------------------------
    def audit(self, args: dict | None = None) -> dict:
        import vc_common as C

        args = args or {}
        dsid = args.get("dataset") or self.load_state().get("active")
        meta = self._meta(dsid)
        bundle = self._load_bundle(dsid)
        items = []

        def add(item_id, level, title, detail, hint=""):
            items.append({"id": item_id, "level": level, "title": title, "detail": detail, "hint": hint})

        # 1 数据完整性
        if meta.get("sha256"):
            add("data_hash", "ok", "原始数据已登记", f"sha256={str(meta['sha256'])[:16]}…；{meta.get('bytes', 0)} bytes")
        else:
            add("data_hash", "warn", "原始数据未登记哈希", "内存 demo 数据或来源未落盘", "入库真实数据时会自动登记")
        # 2 规模
        n_cells, n_genes = int(meta.get("n_cells") or 0), int(meta.get("n_genes") or 0)
        level = "ok" if n_cells >= 500 and n_genes >= 500 else "warn"
        add("size", level, "数据规模", f"{n_cells:,} 细胞 × {n_genes:,} 基因",
            "" if level == "ok" else "细胞或基因偏少，隐空间与 GRN 估计会不稳")
        # 3 质控后
        steps = meta.get("steps") or {}
        if "qc" in steps:
            metrics = steps["qc"].get("metrics") or {}
            removed = metrics.get("pct_removed")
            if isinstance(removed, (int, float)):
                level = "fail" if removed > 60 else ("warn" if removed > 30 else "ok")
                add("qc", level, "质控保留率", f"过滤掉 {removed:.1f}% 的细胞",
                    "" if level == "ok" else "过滤比例偏高，检查阈值或数据质量")
        else:
            add("qc", "warn", "未做质控", "建议先跑质控", "左栏第 1 步")
        # 4 状态
        state = None
        try:
            state = self._stage(dsid, "state")
        except Exception:  # noqa: BLE001
            pass
        if state:
            sil = (state.get("metrics") or {}).get("silhouette")
            if isinstance(sil, (int, float)):
                level = "ok" if sil >= 0.15 else "warn"
                add("clusters", level, "状态分离度", f"轮廓系数 {sil:.3f}",
                    "" if level == "ok" else "状态分离较弱：可能状态连续，或 n_pcs/分辨率不合适")
            labels = state.get("cluster_labels") or []
            sizes = state.get("cluster_sizes") or []
            if sizes:
                small = [size for size in sizes if isinstance(size, (int, float)) and size < 10]
                if small:
                    add("small_clusters", "warn", "存在小簇", f"{len(small)} 个簇细胞数 <10，扰动估计会不稳",
                        "可调低分辨率或提高 n_neighbors")
        else:
            add("clusters", "warn", "未做状态表征", "后续网络与扰动都依赖它", "第 2 步")
        # 5 注释
        if "annotate" in steps:
            metrics = steps["annotate"].get("metrics") or {}
            unknown = metrics.get("unknown")
            if isinstance(unknown, int) and unknown > 0:
                add("annotate", "warn" if unknown else "ok", "状态注释覆盖",
                    f"{unknown} 个簇未匹配到参考面板", "可上传自定义 marker 面板或手动改名")
            else:
                add("annotate", "ok", "状态注释覆盖", "全部簇已分配到参考状态")
        else:
            add("annotate", "warn", "未做状态注释", "结果会只有簇编号，难以解读", "第 3 步")
        # 6 GRN
        grn = None
        try:
            grn = self._stage(dsid, "grn")
        except Exception:  # noqa: BLE001
            pass
        if grn:
            metrics = grn.get("metrics") or {}
            n_edges = metrics.get("n_edges") or 0
            hub = metrics.get("hub_tfs") or []
            level = "ok" if n_edges >= 100 else "warn"
            add("grn", level, "调控网络规模", f"{n_edges} 条边、{metrics.get('n_tfs', 0)} 个 TF",
                "" if level == "ok" else "边太少：可能表达稀疏或 alpha 过大")
            validity = grn.get("validity") or {}
            add("grn_direction", "warn" if validity.get("directional_evidence") == "weak" else "ok",
                "网络方向性证据", f"证据强度：{validity.get('directional_evidence', '未知')}",
                "共表达推断只给假说；有 ATAC/ChIP 先验或实验验证才算方向确定")
            if hub:
                add("hub", "ok", "枢纽因子", "出度最高：" + "、".join(item.get("tf", "") for item in hub[:5]))
        else:
            add("grn", "warn", "未推断调控网络", "虚拟扰动需要网络", "第 4 步")
        # 7 校验
        if "validate" in steps:
            verdict = (self.results.get(dsid, {}).get("validate") or {}).get("verdict") or {}
            add("validate", "ok" if verdict.get("passed") else "warn", "网络/扰动校验",
                verdict.get("why") or "已运行", "" if verdict.get("passed") else "先不要用它做结论，换先验或调参后重跑")
        else:
            add("validate", "warn", "未做校验", "demo 数据可直接校验；真实数据建议 n_boot>0 看稳定性",
                "第 5 步")
        # 8 扰动
        if "perturb" in steps:
            digest = self.results.get(dsid, {}).get("perturb") or {}
            add("perturb", "ok", "虚拟扰动已完成",
                f"{digest.get('gene') or ''} {digest.get('mode') or ''} → 目标状态 {digest.get('target_state') or '—'}")
        else:
            add("perturb", "warn", "未做虚拟扰动", "这是虚拟细胞的核心动作", "第 6 步")
        # 9 参数
        notes = []
        for step in (meta.get("stepOrder") or []):
            try:
                recommendation = self._recommend(step, dsid)
                entries = (meta.get("steps") or {}).get(step) or {}
                used = entries.get("params") or {}
                for item in recommendation.get("params") or []:
                    key = item["key"]
                    if key in used and used[key] != item.get("recommended") and item.get("risk") in ("medium", "high"):
                        notes.append(f"{STEP_MAP[step]['title']} 的 {item['label']} 用了 {used[key]}，推荐 {item['recommended']}")
            except Exception:  # noqa: BLE001
                continue
        if notes:
            add("params", "warn", "参数与推荐值不同", "；".join(notes[:5]),
                "如果结果异常，先试推荐值")
        else:
            add("params", "ok", "参数合理性", "关键参数都在推荐范围内")
        # 打分
        weight = {"ok": 1.0, "warn": 0.5, "fail": 0.0}
        score = int(round(100 * sum(weight[item["level"]] for item in items) / max(1, len(items))))
        level = "ok" if score >= 80 else ("warn" if score >= 55 else "fail")
        summary = f"自检得分 {score}/100（{level}）：{len([i for i in items if i['level']=='ok'])} 项通过、" \
                  f"{len([i for i in items if i['level']=='warn'])} 项建议、" \
                  f"{len([i for i in items if i['level']=='fail'])} 项问题"
        result = {"ok": True, "score": score, "level": level, "items": items, "summary": summary,
                  "caveats": ["自检是确定性规则检查，不替代统计假设检验与实验验证"]}
        self.results.setdefault(dsid, {})["audit"] = result
        self.save_state()
        return C.jsonable(result)

    def insight(self, args: dict | None = None) -> dict:
        import vc_common as C

        args = args or {}
        dsid = args.get("dataset") or self.load_state().get("active")
        return C.jsonable(self._insight(dsid, _merge_params("insight", args.get("params"))))

    def _insight(self, dsid: str, params: dict) -> dict:
        """确定性智能分析：只读取本次真实结果，不编造数字。"""
        import vc_common as C

        meta = self._meta(dsid)
        results = self.results.get(dsid) or self._restore_results(dsid)
        sections = []
        caveats = []
        ranked_targets = []
        level = str(params.get("depth") or "standard")

        # 1) 数据与状态结构
        bullets = [f"{meta.get('n_cells'):,} 个细胞 × {meta.get('n_genes'):,} 个基因（来源：{meta.get('source')}）"]
        state_digest = results.get("state") or {}
        state_metrics = state_digest.get("metrics") or {}
        if state_metrics.get("n_clusters"):
            bullets.append(f"状态空间：{state_metrics['n_clusters']} 个状态，"
                           f"轮廓系数 {C.fnum(state_metrics.get('silhouette'), 3)}，"
                           f"PC 累积解释 {C.fnum((state_metrics.get('variance_explained') or 0) * 100, 1)}% 方差")
        annotate_digest = results.get("annotate") or {}
        labels = annotate_digest.get("cluster_labels") or state_digest.get("cluster_labels")
        if labels:
            bullets.append("状态标签：" + "、".join(str(item) for item in labels[:12]))
        if annotate_digest.get("summary"):
            bullets.append(annotate_digest["summary"])
        sections.append({"title": "细胞状态", "bullets": bullets})

        # 2) 调控结构
        grn_digest = results.get("grn") or {}
        grn_metrics = grn_digest.get("metrics") or {}
        bullets = []
        if grn_metrics.get("n_edges"):
            hubs = grn_metrics.get("hub_tfs") or []
            bullets.append(f"推断出 {grn_metrics['n_edges']:,} 条有向调控边（{grn_metrics.get('n_tfs', 0)} 个 TF、"
                           f"{grn_metrics.get('n_targets', 0)} 个靶基因）")
            if hubs:
                bullets.append("枢纽因子（按总调控权重排序）：" + "、".join(
                    f"{item.get('tf')}(总权重 {C.fnum(item.get('total_weight'), 1)}，出度 {item.get('out_degree')})"
                    for item in hubs[:6]))
                top_hub = hubs[0].get("tf")
                if top_hub:
                    ranked_targets.append({"gene": top_hub, "score": 1.0,
                                           "why": "网络出度最高，扰动影响面最大"})
            validity = grn_digest.get("validity") or {}
            bullets.append(f"方向性证据：{validity.get('directional_evidence', 'weak（共表达推断，未上传 ATAC/ChIP 先验）')}"
                           + (f"（{'; '.join(validity.get('notes') or [])}）" if validity.get("notes") else ""))
        else:
            bullets.append("尚未推断调控网络（第 4 步）")
        sections.append({"title": "调控结构", "bullets": bullets})

        # 3) 扰动与靶点
        bullets = []
        perturb_digest = results.get("perturb") or {}
        if perturb_digest:
            bullets.append(perturb_digest.get("summary") or "已运行虚拟扰动")
            target = perturb_digest.get("target_state")
            shift = C.fnum(perturb_digest.get("state_shift"), 3)
            if target:
                bullets.append(f"预测细胞向「{target}」方向偏移（位移指标 {shift}）")
            affected = perturb_digest.get("affected") or []
            if affected:
                up = [item for item in affected[:40] if (item.get("delta") or 0) > 0][:5]
                down = [item for item in affected[:40] if (item.get("delta") or 0) < 0][:5]
                if up:
                    bullets.append("上调最明显：" + "、".join(item["gene"] for item in up))
                if down:
                    bullets.append("下调最明显：" + "、".join(item["gene"] for item in down))
            ranked_targets.insert(0, {
                "gene": perturb_digest.get("gene"),
                "score": 0.9 if shift else 0.6,
                "why": f"{perturb_digest.get('mode')} 后预测偏移到 {target or '未定状态'}",
            })
        else:
            bullets.append("尚未做虚拟扰动（第 6 步）")
        screen_digest = results.get("screen") or {}
        if screen_digest.get("ranked"):
            for row in screen_digest["ranked"][:5]:
                ranked_targets.append({"gene": row.get("gene"), "score": row.get("score"),
                                       "why": row.get("why") or f"预测推向 {row.get('target_state')}"})
            bullets.append("虚拟筛选前列：" + "、".join(
                f"{row.get('gene')}({C.fnum(row.get('score'), 2)})" for row in screen_digest["ranked"][:5]))
        response_digest = results.get("response") or {}
        if response_digest.get("metrics"):
            metrics = response_digest["metrics"]
            bullets.append(f"扰动响应预测：R²={C.fnum(metrics.get('r2'), 3)}，"
                           f"Spearman={C.fnum(metrics.get('spearman'), 3)}"
                           f"（{'优于' if metrics.get('beats_baseline') else '未优于'}均值基线）")
        transition_digest = results.get("transition") or {}
        if transition_digest.get("sources"):
            bullets.append("状态转移：源状态 " + "、".join(str(item.get("cluster")) for item in transition_digest["sources"][:3])
                           + "；汇状态 " + "、".join(str(item.get("cluster")) for item in transition_digest["sinks"][:3]))
        sections.append({"title": "扰动与候选靶点", "bullets": bullets})

        # 4) 局限（必须写）
        caveats = ["虚拟扰动是模型推演，不是实验结论；任何靶点都需要 qPCR/WB/功能实验验证"]
        validity = grn_digest.get("validity") or {}
        if validity.get("directional_evidence") in ("weak", None):
            caveats.append("GRN 来自共表达先验，只有相关性没有因果方向；有 ATAC/ChIP 先验时应重新推断")
        if state_metrics.get("silhouette") is not None and state_metrics["silhouette"] < 0.15:
            caveats.append("状态分离度偏低，状态边界可能是人为切分；建议多分辨率对比后再解读扰动方向")
        if (meta.get("n_cells") or 0) < 1000:
            caveats.append("细胞数偏少（<1000），隐空间与网络估计不稳定；结论置信度低")
        validate_digest = results.get("validate") or {}
        if validate_digest.get("verdict"):
            caveats.append("留出校验结论：" + str(validate_digest["verdict"].get("why")))
        sections.append({"title": "方法学局限（必读）", "bullets": caveats})

        # 5) 下一步
        nxt = self._next_suggestion(dsid)
        next_actions = [{"step": nxt.get("next"), "why": nxt.get("why")}]
        pending = [("perturb", "选一个 hub TF 做虚拟敲除，看状态往哪边偏"),
                   ("screen", "批量筛选能推动状态转移的候选靶点"),
                   ("validate", "当前数据有真值，可量化检验网络与扰动的准确性"),
                   ("transition", "估计状态之间的转移倾向，给命运方向一个可量化的估计")]
        for step_id, why in pending:
            if step_id in (meta.get("stepOrder") or []):
                continue
            if step_id in ("perturb", "screen") and "grn" not in results:
                continue
            if step_id == "validate" and not meta.get("truth", {}).get("has_grn_truth"):
                continue
            if step_id == "transition" and "state" not in results:
                continue
            if any(item.get("step") == step_id for item in next_actions):
                continue
            next_actions.append({"step": step_id, "why": why})
        next_actions = next_actions[:4]

        headline_map = {
            "brief": "虚拟细胞模型已建立，可开始虚拟扰动。",
            "standard": (f"在 {meta.get('n_cells'):,} 个细胞上建立了状态空间与调控网络，"
                         + (f"虚拟扰动 {perturb_digest.get('gene')} 预测状态向 {perturb_digest.get('target_state')} 偏移。"
                            if perturb_digest else "虚拟扰动尚未运行。")),
            "deep": (f"虚拟细胞模型：{state_metrics.get('n_clusters')} 个状态、"
                     f"{grn_metrics.get('n_edges') or 0:,} 条调控边；"
                     + (f"扰动 {perturb_digest.get('gene')} → {perturb_digest.get('target_state')}；"
                        if perturb_digest else "")
                     + "结论置信度受 GRN 方向性与验证数据限制，需实验确认。"),
        }
        # 去重 + 排序
        seen = set()
        dedup = []
        for row in ranked_targets:
            gene = str(row.get("gene") or "")
            if gene and gene not in seen:
                seen.add(gene)
                dedup.append(row)
        result = {
            "ok": True,
            "headline": headline_map.get(level, headline_map["standard"]),
            "sections": sections,
            "ranked_targets": dedup[:10],
            "next_actions": next_actions,
            "caveats": caveats,
            "metrics": {"n_sections": len(sections), "n_targets": len(dedup)},
            "summary": headline_map.get(level, headline_map["standard"]),
            "warnings": [],
            "figures": [],
            "tables": [{"id": "insight_targets", "title": "候选靶点",
                        "rows": [{"gene": row.get("gene"), "score": row.get("score"), "why": row.get("why")}
                                 for row in dedup[:10]]}],
        }
        self.results.setdefault(dsid, {})["insight"] = {k: v for k, v in result.items() if k != "tables"}
        self.save_state()
        return result

    # -- 预设流程 ---------------------------------------------------------
    def recipe(self, args: dict | None = None) -> dict:
        args = args or {}
        recipe_id = str(args.get("id") or "quick")
        recipes = {
            "quick": {
                "id": "quick", "name": "快速体验（推荐首次使用）",
                "desc": "示例数据 → 质控 → 状态表征 → 状态注释 → 调控网络 → 校验 → 虚拟敲除一个 hub TF → 智能分析",
                "steps": ["qc", "state", "annotate", "grn", "validate", "perturb", "insight"],
            },
            "standard": {
                "id": "standard", "name": "标准分析",
                "desc": "在快速版基础上加虚拟筛选、状态转移与自检",
                "steps": ["qc", "state", "annotate", "grn", "validate", "perturb", "screen",
                          "transition", "insight", "audit"],
            },
            "full": {
                "id": "full", "name": "完整流程",
                "desc": "标准版 + 扰动响应预测（有扰动表时）+ 导出报告",
                "steps": ["qc", "state", "annotate", "grn", "validate", "perturb", "screen",
                          "transition", "response", "insight", "audit"],
            },
        }
        if recipe_id not in recipes:
            raise self._require("vc_common").err("bad_request", f"未知预设：{recipe_id}")
        return {"ok": True, "recipe": recipes[recipe_id], "recipes": list(recipes.values())}

    def recipe_run(self, args: dict) -> dict:
        import vc_common as C

        recipe_id = str(args.get("id") or "quick")
        definition = self.recipe({"id": recipe_id})["recipe"]
        dsid = args.get("dataset") or self.load_state().get("active")
        if not dsid and args.get("demo", True):
            self.demo({})
            dsid = self.load_state().get("active")
        done = []
        failed = []
        run_ids = []
        for step in definition["steps"]:
            if step == "audit":
                result = self.audit({"dataset": dsid})
                done.append({"step": "audit", "summary": result.get("summary")})
                continue
            try:
                params = args.get("params", {}).get(step) if isinstance(args.get("params"), dict) else None
                result = self.run_step(step, {"dataset": dsid, "params": params or {}}, recipe_mode=True)
                done.append({"step": step, "summary": result.get("summary"), "run": result.get("run")})
                if result.get("run"):
                    run_ids.append(result["run"])
            except C.EngineError as error:
                if error.code in ("prerequisite", "unsupported") and step in ("validate", "response"):
                    done.append({"step": step, "skipped": True, "reason": error.message})
                    continue
                failed.append({"step": step, "code": error.code, "error": error.message})
                break
            except Exception as error:  # noqa: BLE001
                failed.append({"step": step, "code": "internal", "error": str(error)})
                break
        return C.jsonable({"ok": True, "recipe": recipe_id, "done": done, "failed": failed,
                           "runIds": run_ids,
                           "summary": f"预设「{definition['name']}」完成 {len(done)} 步"
                                      + (f"，{len(failed)} 步失败" if failed else ""),
                           "insight": (self.results.get(dsid, {}) or {}).get("insight")})

    # -- 导出 -------------------------------------------------------------
    def export(self, args: dict) -> dict:
        import vc_common as C

        dsid = args.get("dataset") or self.load_state().get("active")
        meta = self._meta(dsid)
        what = str(args.get("what") or "").strip()
        exports_dir = C.ensure_dir(os.path.join(self.workdir, "exports"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = args.get("out_path")
        results = self.results.get(dsid) or self._restore_results(dsid)
        name = C.safe_name(meta.get("name") or dsid, dsid)

        def path_for(suffix: str) -> str:
            return os.path.abspath(out_path or os.path.join(exports_dir, f"{name}-{what}-{stamp}{suffix}"))

        if what in ("state_csv", "clusters_csv", "markers_csv", "grn_csv", "perturb_csv",
                    "screen_csv", "response_csv", "transition_csv"):
            step_map = {
                "state_csv": ("state", "latent"), "clusters_csv": ("state", "clusters"),
                "markers_csv": ("state", "markers"), "grn_csv": ("grn", "edges"),
                "perturb_csv": ("perturb", "affected"), "screen_csv": ("screen", "ranked"),
                "response_csv": ("response", "per_pert"), "transition_csv": ("transition", "T"),
            }
            step, key = step_map[what]
            digest = results.get(step) or {}
            rows = digest.get(key)
            if rows is None:
                stage = self._stage(dsid, step)
                rows = stage.get(key)
            if rows is None:
                raise C.err("prerequisite", f"没有可导出的 {what}，请先运行对应步骤")
            path = path_for(".csv")
            if key in ("latent", "clusters", "T") or (isinstance(rows, list) and rows and isinstance(rows[0], list)):
                C.write_table(path, [{"index": index, **{f"v{i}": value for i, value in enumerate(row)}}
                                     for index, row in enumerate(rows if isinstance(rows, list) else [])]
                              or [{"value": rows}])
            else:
                C.write_table(path, rows)
            return self._export_result(path, what)

        if what == "summary_json":
            path = path_for(".json")
            C.write_json(path, {"dataset": meta, "results": results,
                                "engine": self.engine_info(), "exportedAt": C.now_iso()})
            return self._export_result(path, what)

        if what == "protocol":
            path = path_for(".json")
            protocol = {
                "tool": {"name": "dsh-virtualcell", "version": VERSION},
                "createdAt": C.now_iso(),
                "dataset": {key: meta.get(key) for key in ("id", "name", "n_cells", "n_genes", "source", "sha256")},
                "steps": [{"step": step, **((meta.get("steps") or {}).get(step) or {})}
                          for step in (meta.get("stepOrder") or [])],
                "params": {step: _merge_params(step, ((meta.get("steps") or {}).get(step) or {}).get("params"))
                           for step in (meta.get("stepOrder") or [])},
                "runs": self._recent_runs(dsid, 50),
                "engine": self.engine_info(),
                "reproduce": "python3 lib/vc_engine.py --workdir <workdir> 后用相同 steps/params 依次调用 run",
            }
            C.write_json(path, protocol)
            return self._export_result(path, what)

        if what == "methods":
            path = path_for(".md")
            text = self._methods_text(meta, results)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return self._export_result(path, what)

        if what == "report":
            path = path_for(".md")
            text = self._report_text(dsid, meta, results)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return self._export_result(path, what)

        if what == "script":
            path = path_for(".py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._script_text(meta))
            return self._export_result(path, what)

        if what == "run_zip":
            run_id = str(args.get("run") or "")
            if not run_id:
                raise C.err("bad_request", "run_zip 需要 run 参数")
            run_dir = os.path.join(self._dataset_dir(dsid), "runs", run_id)
            if not os.path.isdir(run_dir):
                raise C.err("not_found", f"运行记录 {run_id} 不存在")
            import zipfile

            path = path_for(".zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for root, _dirs, files in os.walk(run_dir):
                    for filename in files:
                        full = os.path.join(root, filename)
                        archive.write(full, os.path.relpath(full, run_dir))
            return self._export_result(path, what)

        raise C.err("bad_request", f"不支持的导出类型：{what}")

    def _export_result(self, path: str, what: str) -> dict:
        return {"ok": True, "what": what, "path": path, "bytes": os.path.getsize(path),
                "name": os.path.basename(path)}

    def _methods_text(self, meta: dict, results: dict) -> str:
        state = results.get("state") or {}
        metrics = state.get("metrics") or {}
        grn = results.get("grn") or {}
        gm = grn.get("metrics") or {}
        lines = [
            "## Virtual-cell modelling",
            "",
            f"Single-cell expression profiles ({meta.get('n_cells'):,} cells × {meta.get('n_genes'):,} genes) "
            "were used to construct a virtual-cell state space. ",
            ("After quality control, counts were library-size normalised, log1p-transformed, and restricted to "
             f"{metrics.get('n_hvg', 'the')} highly variable genes. "
             if metrics else ""),
            ("A principal-component latent space was computed and cells were clustered; "
             f"a k-nearest-neighbour graph was used for state transition estimation. "
             if metrics else ""),
            ("A gene regulatory network was inferred by fitting per-state ridge regressions of candidate "
             "transcription factor-target pairs derived from co-expression priors "
             f"({gm.get('n_edges', 'n/a')} directed edges, "
             f"{gm.get('n_tfs', 'n/a')} transcription factors). "
             if gm else ""),
            ("In silico perturbations were simulated by clamping the perturbed gene's expression and "
             "propagating the resulting expression change through the inferred network for several iterations; "
             "the propagated change was projected into the latent space to estimate the shift of each cell state. "
             if results.get("perturb") else ""),
            "All results are model-based predictions and require experimental validation.",
        ]
        return "\n".join(line for line in lines if line)

    def _report_text(self, dsid: str, meta: dict, results: dict) -> str:
        import vc_common as C

        insight = results.get("insight") or self._insight(dsid, {})
        audit = self.audit({"dataset": dsid})
        lines = [f"# 虚拟细胞分析报告：{meta.get('name')}", "",
                 f"- 数据：{meta.get('n_cells'):,} 细胞 × {meta.get('n_genes'):,} 基因（来源 `{meta.get('source')}`）",
                 f"- 生成时间：{C.now_iso()}", f"- 工作目录：`{self.workdir}`",
                 f"- 原始数据：`{meta.get('rawPath')}`（sha256 `{str(meta.get('sha256') or '')[:16]}…`）", "",
                 "## 一句话结论", "", str(insight.get("headline") or ""), ""]
        for section in insight.get("sections") or []:
            lines.append(f"## {section.get('title')}")
            lines.append("")
            for bullet in section.get("bullets") or []:
                lines.append(f"- {bullet}")
            lines.append("")
        targets = insight.get("ranked_targets") or []
        if targets:
            lines.append("## 候选靶点")
            lines.append("")
            lines.append("| 基因 | 分数 | 理由 |")
            lines.append("|---|---|---|")
            for row in targets:
                lines.append(f"| {row.get('gene')} | {C.fnum(row.get('score'), 2)} | {row.get('why')} |")
            lines.append("")
        lines.append("## 自检")
        lines.append("")
        lines.append(f"- 得分：{audit.get('score')}/100（{audit.get('level')}）")
        for item in audit.get("items") or []:
            if item.get("level") != "ok":
                lines.append(f"- [{item.get('level')}] {item.get('title')}：{item.get('detail')}"
                             + (f"（建议：{item['hint']}）" if item.get("hint") else ""))
        lines.append("")
        lines.append("## 图")
        lines.append("")
        seen = set()
        for step in ("state", "grn", "perturb", "screen", "response", "transition", "validate"):
            for figure in (results.get(step) or {}).get("figures") or []:
                if figure.get("path") in seen:
                    continue
                seen.add(figure.get("path"))
                notes = "；".join(figure.get("notes") or [])
                lines.append(f"- **{figure.get('title')}**：`{figure.get('path')}`" + (f"（{notes}）" if notes else ""))
        lines.append("")
        lines.append("## 方法学局限")
        lines.append("")
        for item in insight.get("caveats") or []:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("## 下一步")
        lines.append("")
        for action in insight.get("next_actions") or []:
            lines.append(f"- `{action.get('step')}`：{action.get('why')}")
        lines.append("")
        return "\n".join(lines)

    def _script_text(self, meta: dict) -> str:
        return f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现脚本：{meta.get('name')} —— 由 dsh-virtualcell {VERSION} 生成。

运行方式：
    python3 reproduce.py            # 在插件 lib/ 目录旁运行
注意：脚本读取工作目录里的已处理数据，不重新下载输入。
"""

import os
import sys

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import vc_common as C
import vc_io
import vc_state
import vc_grn

WORKDIR = {self.workdir!r}
DATASET = {meta.get('id')!r}

bundle = vc_io.load_dataset(os.path.join(WORKDIR, "datasets", DATASET, "bundle.h5ad"), data_format="h5ad")
qc = vc_state.run_qc(bundle, {_default_params("qc")!r})
state = vc_state.run_state(bundle, {_default_params("state")!r})
annotate = vc_state.run_annotate(bundle, state, {_default_params("annotate")!r})
grn = vc_grn.run_grn(bundle, state, {_default_params("grn")!r})
perturb = vc_grn.run_perturb(bundle, state, grn, {_default_params("perturb")!r})
print("状态数：", state.get("n_clusters"))
print("调控边：", (grn.get("metrics") or {{}}).get("n_edges"))
print("扰动结论：", perturb.get("summary"))
'''

    # -- 打开/示例/选择/关闭/重置 ----------------------------------------
    def open_dataset(self, args: dict) -> dict:
        import vc_common as C

        io = self._require("vc_io")
        demo = bool(args.get("demo"))
        if demo:
            return self.demo(args)
        path = str(args.get("path") or "").strip()
        url = str(args.get("url") or "").strip()
        if not path and not url:
            raise C.err("bad_request", "需要 path 或 url")
        source = path or url
        cache_dir = os.path.join(self.workdir, "uploads")
        C.progress("识别输入格式…", 5)
        bundle = io.load_dataset(
            source, data_format=args.get("data_format") or "auto", member=args.get("member"),
            transpose=bool(args.get("transpose")), object=args.get("object"),
            sheet=args.get("sheet"), gene_col=args.get("gene_col"), cache_dir=cache_dir,
        )
        sha = None
        bytes_ = 0
        raw_copy = True
        raw_path = ""
        # 原始数据保留：小文件复制、大文件登记哈希
        if path and os.path.isfile(path):
            try:
                bytes_ = os.path.getsize(path)
                sha = io.sha256_of(path) if hasattr(io, "sha256_of") else C.sha256_of(path)
                raw_copy = bytes_ <= 2 * 1024 ** 3
                dsid_tmp = self._next_ds_id()
                raw_dir = C.ensure_dir(os.path.join(self.workdir, "datasets", dsid_tmp, "raw"))
                if raw_copy:
                    import shutil

                    target = os.path.join(raw_dir, C.safe_name(os.path.basename(path), "input"))
                    shutil.copyfile(path, target)
                    raw_path = target
                else:
                    raw_path = path
                C.write_json(os.path.join(raw_dir, "manifest.json"), {
                    "source": path, "sha256": sha, "bytes": bytes_, "copy": raw_copy,
                    "createdAt": C.now_iso(),
                })
            except Exception as error:  # noqa: BLE001
                sys.stderr.write(f"[vc_engine] 原始数据登记失败：{error}\n")
        meta = self._register_bundle(
            bundle, name=args.get("name") or os.path.basename(source.rstrip("/")) or "dataset",
            source="url" if url and not path else "path", raw_path=raw_path, raw_copy=raw_copy,
            sha256=sha, bytes_=bytes_,
        )
        return {"ok": True, "dataset": meta, "summary": self._open_summary(meta, bundle)}

    def demo(self, args: dict | None = None) -> dict:
        import vc_common as C

        args = args or {}
        io = self._require("vc_io")
        seed = int(args.get("seed") or 0)
        n_cells = int(args.get("n_cells") or 2000)
        n_genes = int(args.get("n_genes") or 1000)
        C.progress("生成示例虚拟细胞数据（含真值网络）…", 10)
        bundle = io.load_demo(seed=seed, n_cells=n_cells, n_genes=n_genes)
        demo_meta = {"seed": seed, "n_cells": n_cells, "n_genes": n_genes}
        meta = self._register_bundle(
            bundle, name=f"demo-virtualcell-{n_cells}x{n_genes}", source="demo",
            raw_path="", raw_copy=False, demo=demo_meta,
        )
        return {"ok": True, "dataset": meta, "summary": self._open_summary(meta, bundle)}

    def _open_summary(self, meta: dict, bundle) -> str:
        truth = getattr(bundle, "truth", None) or {}
        extra = ""
        if truth.get("grn") is not None:
            try:
                extra = f"，自带真值网络 {len(truth['grn'])} 条边与 {len(truth.get('perturb') or {})} 个留出扰动"
            except Exception:  # noqa: BLE001
                extra = "，自带真值网络"
        return (f"已载入 {meta['name']}：{meta['n_cells']:,} 细胞 × {meta['n_genes']:,} 基因"
                f"（其中 {meta['n_tfs']} 个转录因子）{extra}。建议下一步：质控 → 状态表征。")

    def select_dataset(self, args: dict) -> dict:
        import vc_common as C

        dsid = str(args.get("dataset") or "")
        state = self.load_state()
        if dsid not in state["datasets"]:
            raise C.err("not_found", f"数据集 {dsid} 不存在")
        state["active"] = dsid
        self.save_state()
        return {"ok": True, "active": dsid}

    def close_dataset(self, args: dict) -> dict:
        import vc_common as C

        dsid = str(args.get("dataset") or self.load_state().get("active") or "")
        state = self.load_state()
        if dsid in state["datasets"]:
            state["datasets"].pop(dsid, None)
        if state.get("active") == dsid:
            state["active"] = next(iter(state["datasets"]), None)
        self.bundles.pop(dsid, None)
        self.stages.pop(dsid, None)
        self.results.pop(dsid, None)
        self.save_state()
        return {"ok": True, "active": state.get("active"),
                "note": "已从工作台移除（磁盘上的数据与运行记录保留，便于事后核查）"}

    def reset(self, args: dict | None = None) -> dict:
        self.bundles.clear()
        self.stages.clear()
        self.results.clear()
        self.figures.clear()
        self.state = {"version": VERSION, "datasets": {}, "active": None,
                      "createdAt": self.load_state().get("createdAt")}
        self.save_state()
        return {"ok": True, "note": "工作台已清空（磁盘文件保留）"}


# ---------------------------------------------------------------------------
# 文件小工具
# ---------------------------------------------------------------------------

def _save_npz(path: str, arrays: dict) -> None:
    import numpy as np

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload = {}
    try:
        import scipy.sparse as sp

        for key, value in arrays.items():
            if value is None:
                continue
            if sp.issparse(value):
                payload[f"{key}__data"] = value.data
                payload[f"{key}__indices"] = value.indices
                payload[f"{key}__indptr"] = value.indptr
                payload[f"{key}__shape"] = np.asarray(value.shape)
                payload[f"{key}__sparse"] = np.asarray([1])
            elif isinstance(value, np.ndarray):
                payload[key] = value
            elif isinstance(value, (list, tuple)):
                payload[key] = np.asarray(value)
            else:
                try:
                    payload[key] = np.asarray(value)
                except Exception:  # noqa: BLE001
                    continue
    except Exception as error:  # noqa: BLE001
        sys.stderr.write(f"[vc_engine] 保存数组失败：{error}\n")
        return
    np.savez_compressed(path, **payload)


def _load_npz(path: str) -> dict:
    import numpy as np

    if not os.path.exists(path):
        return {}
    out = {}
    try:
        import scipy.sparse as sp

        with np.load(path, allow_pickle=False) as data:
            keys = set(data.files)
            sparse_bases = {key[: -len("__sparse")] for key in keys if key.endswith("__sparse")}
            for key in keys:
                if key.endswith(("__data", "__indices", "__indptr", "__shape", "__sparse")):
                    continue
                out[key] = data[key]
            for base in sparse_bases:
                if f"{base}__data" not in keys:
                    continue
                shape = tuple(int(v) for v in data[f"{base}__shape"])
                out[base] = sp.csr_matrix(
                    (data[f"{base}__data"], data[f"{base}__indices"], data[f"{base}__indptr"]), shape=shape)
    except Exception as error:  # noqa: BLE001
        sys.stderr.write(f"[vc_engine] 读取数组失败 {path}：{error}\n")
    return out


def _read_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json_file(path: str, value) -> None:
    import vc_common as C

    C.write_json(path, value)


def _table_summary(item: dict) -> dict:
    import vc_common as C

    return {"id": item.get("id"), "title": item.get("title"),
            "rows": int(C.table_preview(item.get("rows")).get("total") or 0)}


PLOT_TITLES = {
    "qc_violin": "质控指标分布",
    "state_embedding": "细胞状态空间",
    "state_embedding_perturb": "虚拟扰动后的状态位移",
    "cluster_bar": "状态规模",
    "grn_hub_bar": "枢纽转录因子",
    "grn_network": "调控网络",
    "grn_heatmap": "调控权重热图",
    "perturb_waterfall": "受影响基因",
    "perturb_field": "状态位移场",
    "perturb_heatmap": "各状态隐空间位移",
    "screen_rank": "虚拟筛选排名",
    "response_scatter": "扰动响应预测",
    "transition_matrix": "状态转移矩阵",
    "transition_graph": "状态转移流向",
    "validate_roc": "网络校验",
    "compare_bar": "指标对比",
}
PLOT_STEPS = {
    "qc_violin": "qc",
    "state_embedding": "state",
    "cluster_bar": "state",
    "state_embedding_perturb": "perturb",
    "perturb_waterfall": "perturb",
    "perturb_field": "perturb",
    "perturb_heatmap": "perturb",
    "grn_hub_bar": "grn",
    "grn_network": "grn",
    "grn_heatmap": "grn",
    "screen_rank": "screen",
    "response_scatter": "response",
    "transition_matrix": "transition",
    "transition_graph": "transition",
    "validate_roc": "validate",
}


def _plot_title(kind: str) -> str:
    return PLOT_TITLES.get(str(kind), str(kind))


def _plot_step(kind: str):
    return PLOT_STEPS.get(str(kind))


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(str(text), safe="")


def vc_err(code: str, message: str, **details):
    import vc_common as C

    return C.err(code, message, **details)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.path.join(os.path.expanduser("~"), ".dsh", "virtualcell"))
    parsed, _ = parser.parse_known_args()
    workdir = os.path.abspath(parsed.workdir)

    os.environ["MPLBACKEND"] = "Agg"
    os.environ.setdefault("MPLCONFIGDIR", os.path.join(workdir, "mpl-cache"))
    os.environ["DS_VC_WORKDIR"] = workdir
    try:
        os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
    except OSError:
        os.environ["MPLCONFIGDIR"] = "/tmp"

    try:
        import vc_common as C
    except Exception as error:  # noqa: BLE001
        sys.stderr.write(f"[vc_engine] 依赖缺失：{error}\n")
        _emit({"id": "boot", "ok": False, "error": {"code": "no_python",
               "message": f"Python 依赖缺失（numpy/pandas/scipy/sklearn/anndata/matplotlib）：{error}"}})
        return 1

    worker = Worker(workdir)
    worker._ensure_dirs()

    def handle(cmd: str, args: dict):
        if cmd in ("ping", "engine_info"):
            info = worker.engine_info()
            return {"mode": info["backend"], "packages": info["packages"], "adapters": info["adapters"],
                    "platform": info["platform"], "version": VERSION, "modules": info["modules"]}
        if cmd == "state":
            return worker.state_payload()
        if cmd == "formats":
            return {"ok": True, "formats": [
                {"id": "h5ad", "label": "AnnData .h5ad", "ext": ".h5ad", "note": "scverse/scanpy 标准，保留 obs"},
                {"id": "10x_h5", "label": "10x CellRanger .h5", "ext": ".h5", "note": "自动只取 Gene Expression"},
                {"id": "10x_mtx", "label": "10x 三件套目录/单 .mtx", "ext": ".mtx/.mtx.gz", "note": "缺 sidecar 自动命名"},
                {"id": "loom", "label": "Loom", "ext": ".loom", "note": "GEO 常见"},
                {"id": "matrix_text", "label": "表达矩阵 csv/tsv/txt", "ext": ".csv/.tsv/.txt(.gz)", "note": "方向自动判断，可强制转置"},
                {"id": "geo_series", "label": "GEO series matrix", "ext": "_series_matrix.txt(.gz)", "note": "自动解析表段"},
                {"id": "archive", "label": "压缩包", "ext": ".zip/.tar(.gz)", "note": "解包后自动找数据文件"},
                {"id": "url", "label": "URL 直链", "ext": "http(s)", "note": "下载后按格式识别，≤8 GB"},
            ], "attachKinds": ["perturb_table", "gene_set", "grn_prior", "drug_signature"],
                "adapters": worker.engine_info()["adapters"]}
        if cmd == "steps":
            return {"ok": True, "steps": STEPS, "order": STEP_ORDER,
                    "requires": STEP_REQUIRES}
        if cmd == "runs":
            dsid = args.get("dataset") or worker.load_state().get("active")
            limit = int(args.get("limit") or 20)
            offset = int(args.get("offset") or 0)
            rows = worker._recent_runs(dsid, 500)
            return {"ok": True, "runs": rows[offset:offset + limit], "total": len(rows)}
        if cmd in ("open",):
            return worker.open_dataset(args)
        if cmd == "demo":
            return worker.demo(args)
        if cmd == "select":
            return worker.select_dataset(args)
        if cmd == "close":
            return worker.close_dataset(args)
        if cmd == "inspect":
            return worker.inspect(args)
        if cmd == "preview":
            return worker.preview(args)
        if cmd == "suggest":
            dsid = args.get("dataset") or worker.load_state().get("active")
            step = str(args.get("step") or "")
            if step not in STEP_MAP:
                raise C.err("bad_request", f"未知步骤：{step}")
            return worker._recommend(step, dsid)
        if cmd == "run":
            step = str(args.get("step") or "")
            if not step:
                raise C.err("bad_request", "run 需要 step")
            return worker.run_step(step, args)
        if cmd == "sweep":
            return worker.sweep(args)
        if cmd == "plot":
            return worker.plot(args)
        if cmd == "attach":
            return worker.attach(args)
        if cmd == "export":
            return worker.export(args)
        if cmd == "audit":
            return worker.audit(args)
        if cmd == "insight":
            return worker.insight(args)
        if cmd == "recipe":
            return worker.recipe(args)
        if cmd == "recipe_run":
            return worker.recipe_run(args)
        if cmd == "reset":
            return worker.reset(args)
        if cmd == "shutdown":
            return {"ok": True}
        raise C.err("bad_request", f"未知命令：{cmd}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            payload = json.loads(line)
            request_id = payload.get("id")
            cmd = str(payload.get("cmd") or "")
            args = payload.get("args") or {}
            _CURRENT["id"] = request_id
            result = handle(cmd, args)
            _emit({"id": request_id, "ok": True, "result": C.jsonable(result)})
            if cmd == "shutdown":
                return 0
        except EngineFailure as error:
            _emit({"id": request_id, "ok": False,
                   "error": {"code": error.code, "message": error.message, **C.jsonable(error.details)}})
        except C.EngineError as error:
            _emit({"id": request_id, "ok": False, "error": error.to_dict()})
        except Exception as error:  # noqa: BLE001
            trace = traceback.format_exc()
            sys.stderr.write(trace + "\n")
            sys.stderr.flush()
            _emit({"id": request_id, "ok": False,
                   "error": {"code": "internal", "message": f"{type(error).__name__}: {error}",
                             "trace": trace[-2000:]}})
        finally:
            _CURRENT["id"] = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
