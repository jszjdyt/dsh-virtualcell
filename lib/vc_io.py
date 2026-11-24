#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — 输入解析 + 示例数据生成（CONTRACT §5 / §7.1）。

支持的输入
----------
- AnnData `.h5ad`（anndata；X 优先，obs 全保留）
- 10x CellRanger `.h5`（h5py 手工解析 /matrix，自动只取 Gene Expression）
- 10x Matrix Market 三件套（目录或单个 `.mtx(.gz)`；缺 sidecar 自动命名）
- Loom（h5py 手工解析，不依赖 loompy）
- 文本矩阵 csv/tsv/txt（可 .gz/.bz2；分隔符嗅探、方向自动判断、可强制转置）
- GEO series matrix `*_series_matrix.txt(.gz)`
- zip / tar(.gz) 压缩包（安全解包后递归嗅探）
- http(s) URL（下载到 cache_dir 后按上面规则识别）

示例数据（load_demo）
--------------------
合成 6 个细胞状态、TF 驱动的表达谱，并**自带真值**：
`truth['grn']`（真实有向边）、`truth['perturb']`（5 个 TF 的解析扰动 Δ）、
`truth['perturb_table']`（control + 5 个敲低的 Perturb-seq 样表）、`truth['labels']`。
真值让「网络校验」可以量化，也让整条链路可离线自测。随机种子固定 ⇒ 完全可复现。
"""

from __future__ import annotations

import bz2
import gzip
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp

import vc_common as C
import vc_priors as P

MAX_DOWNLOAD_BYTES = 8 * 1024 ** 3
MAX_ARCHIVE_BYTES = 20 * 1024 ** 3
MAX_ARCHIVE_ENTRIES = 400
TEXT_EXT = (".csv", ".tsv", ".txt", ".tab", ".mtx", ".data")
ARCHIVE_EXT = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".tar.gz")


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

@dataclass
class Bundle:
    X: sp.csr_matrix
    genes: list[str]
    cells: list[str]
    obs: object = None
    meta: dict = field(default_factory=dict)
    truth: dict | None = None

    @property
    def n_cells(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_genes(self) -> int:
        return int(self.X.shape[1])

    def summary(self) -> dict:
        return {"n_cells": self.n_cells, "n_genes": self.n_genes,
                "value_type": (self.meta or {}).get("value_type"),
                "format": (self.meta or {}).get("format")}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _strip_gz(name: str) -> str:
    lower = name.lower()
    for suffix in (".gz", ".bz2"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _is_archive(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in (".zip", ".tar", ".tar.gz", ".tgz"))


def _open_text(path: str):
    lower = path.lower()
    if lower.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    if lower.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _squeeze_names(names, prefix: str, count: int) -> list[str]:
    """把基因/细胞名清理成唯一、非空的字符串列表。"""
    out = []
    seen = {}
    source = list(names) if names is not None else []
    for index in range(count):
        raw = str(source[index]) if index < len(source) else ""
        name = raw.strip().strip('"').strip("'")
        if not name or name.lower() in ("nan", "none"):
            name = f"{prefix}{index + 1:05d}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _to_csr(matrix, dtype=np.float32) -> sp.csr_matrix:
    if sp.issparse(matrix):
        out = matrix.tocsr().astype(dtype)
    else:
        out = sp.csr_matrix(np.asarray(matrix, dtype=dtype))
    out.sum_duplicates()
    return out


def _looks_like_gene(names) -> float:
    """返回“像基因符号”的比例：大写字母/数字/连接符开头，长度 2–15。"""
    pattern = re.compile(r"^[A-Za-z][A-Za-z0-9\-\._]{1,14}$")
    sample = [str(name) for name in list(names)[:200] if str(name).strip()]
    if not sample:
        return 0.0
    hits = 0
    for name in sample:
        cleaned = name.strip().strip('"')
        if not cleaned:
            continue
        upper_ratio = sum(1 for ch in cleaned if ch.isupper()) / max(1, len(cleaned))
        if pattern.match(cleaned) and upper_ratio >= 0.5:
            hits += 1
    return hits / len(sample)


def _detect_delimiter(sample: str) -> str:
    first = sample.split("\n", 1)[0]
    counts = {sep: first.count(sep) for sep in ("\t", ",", ";")}
    best = max(counts, key=lambda sep: counts[sep])
    return best if counts[best] > 0 else "\t"


def _value_type(matrix) -> str:
    """粗判 counts / lognorm / scaled。"""
    try:
        data = matrix.data if sp.issparse(matrix) else np.asarray(matrix).ravel()
        sample = np.asarray(data[: min(20000, data.size)], dtype=np.float64)
        if sample.size == 0:
            return "unknown"
        if sample.min() < -0.01:
            return "scaled"
        rounded = np.allclose(sample, np.round(sample), atol=1e-6)
        return "counts" if rounded else "lognorm"
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# 嗅探
# ---------------------------------------------------------------------------

def sniff(path: str, *, data_format: str = "auto", member=None, transpose: bool = False) -> dict:
    """识别格式，返回 {format, path, member?, notes[]}。"""
    target = str(path or "").strip()
    if not target:
        raise C.err("bad_request", "缺少输入路径")
    if data_format and data_format != "auto":
        return {"format": data_format, "path": target, "member": member, "notes": ["用户指定格式"]}

    url = target
    if url.startswith(("http://", "https://")):
        return {"format": "url", "path": url, "member": None, "notes": ["将下载后识别"]}

    if os.path.isdir(target):
        names = [name.lower() for name in os.listdir(target)]
        has_matrix = any(name.startswith("matrix.mtx") for name in names)
        has_bc = any("barcode" in name for name in names)
        if has_matrix and (has_bc or any("feature" in name or "gene" in name for name in names)):
            return {"format": "10x_mtx", "path": target, "member": None, "notes": ["10x 三件套目录"]}
        # 目录里找唯一数据文件
        candidates = [os.path.join(target, name) for name in sorted(os.listdir(target))
                      if os.path.isfile(os.path.join(target, name))]
        found = [item for item in candidates if _is_data_file(item)]
        if len(found) == 1:
            return sniff(found[0], data_format="auto", transpose=transpose)
        raise C.err("ambiguous", "目录里有多个候选数据文件，请直接指定文件路径",
                    {"candidates": [os.path.basename(item) for item in found[:30]]})

    if not os.path.exists(target):
        raise C.err("not_found", f"路径不存在：{target}")

    base = os.path.basename(target)
    lower = base.lower()

    # 压缩包（.mtx.gz 不算）
    if _is_archive(lower) and not lower.endswith(".mtx.gz"):
        return {"format": "archive", "path": target, "member": member, "notes": []}

    if lower.endswith((".h5ad", ".h5ad.gz")):
        return {"format": "h5ad", "path": target, "member": None, "notes": []}
    if lower.endswith(".loom"):
        return {"format": "loom", "path": target, "member": None, "notes": []}
    if lower.endswith(".h5") or lower.endswith(".hdf5"):
        # 10x h5 还是普通 h5：打开看有没有 /matrix
        try:
            import h5py

            with h5py.File(target, "r") as handle:
                if "matrix" in handle:
                    return {"format": "10x_h5", "path": target, "member": None, "notes": []}
                keys = list(handle.keys())[:8]
        except Exception:  # noqa: BLE001
            keys = []
        raise C.err("unsupported", f"无法识别的 HDF5 结构（顶层：{keys or '空'}）；"
                                   "如果是 10x 输出，请确认包含 /matrix 组")
    if lower.endswith(".mtx") or lower.endswith(".mtx.gz"):
        return {"format": "10x_mtx", "path": target, "member": None, "notes": ["单个 mtx，将与同目录 sidecar 配对"]}
    if "series_matrix" in lower:
        return {"format": "geo_series", "path": target, "member": None, "notes": []}
    if any(lower.endswith(ext) or lower.endswith(ext + ".gz") or lower.endswith(ext + ".bz2") for ext in TEXT_EXT):
        return {"format": "matrix_text", "path": target, "member": None, "notes": []}
    raise C.err("unsupported", f"无法识别的文件类型：{base}",
                {"hint": "支持 h5ad / 10x h5 / 10x mtx / loom / csv-tsv-txt / GEO series matrix / zip-tar / URL"})


def _is_data_file(path: str) -> bool:
    lower = os.path.basename(path).lower()
    if lower.startswith((".", "__")):
        return False
    return any(lower.endswith(ext) or lower.endswith(ext + ".gz") or lower.endswith(ext + ".bz2")
               for ext in (".h5ad", ".h5", ".loom", ".mtx", ".csv", ".tsv", ".txt", ".tab", ".data"))


# ---------------------------------------------------------------------------
# 各格式读取
# ---------------------------------------------------------------------------

def _read_h5ad(path: str) -> Bundle:
    import anndata as ad
    import pandas as pd

    try:
        adata = ad.read_h5ad(path)
    except Exception as error:  # noqa: BLE001
        raise C.err("bad_request", f"h5ad 读取失败：{error}") from error
    matrix = adata.X
    if matrix is None:
        raise C.err("bad_request", "h5ad 里没有 X 矩阵")
    X = _to_csr(matrix)
    genes = _squeeze_names(list(adata.var_names), "GENE", X.shape[1])
    cells = _squeeze_names(list(adata.obs_names), "CELL", X.shape[0])
    obs = adata.obs.copy()
    obs.index = cells
    meta = {"format": "h5ad", "source": path, "value_type": _value_type(X), "transpose": False,
            "obs_columns": [str(c) for c in obs.columns]}
    return Bundle(X=X, genes=genes, cells=cells, obs=obs, meta=meta)


def _read_10x_h5(path: str) -> Bundle:
    import h5py

    with h5py.File(path, "r") as handle:
        if "matrix" not in handle:
            raise C.err("bad_request", "不是 10x h5（缺少 /matrix）")
        group = handle["matrix"]
        shape = tuple(int(v) for v in group["shape"][:])  # (genes, cells)
        data = group["data"][:]
        indices = group["indices"][:]
        indptr = group["indptr"][:]
        matrix = sp.csc_matrix((data, indices, indptr), shape=shape)
        if "features" in group:
            features = group["features"]
            names = [item.decode("utf-8", "replace") for item in features["name"][:]]
            types = None
            if "feature_type" in features:
                types = [item.decode("utf-8", "replace") for item in features["feature_type"][:]]
        else:
            names = [item.decode("utf-8", "replace") for item in group["genes"][:]]
            types = None
        barcodes = [item.decode("utf-8", "replace") for item in group["barcodes"][:]]
    if types is not None and any(kind == "Gene Expression" for kind in types):
        keep = [index for index, kind in enumerate(types) if kind == "Gene Expression"]
        matrix = matrix[keep, :]
        names = [names[index] for index in keep]
    X = _to_csr(matrix.T)  # cells × genes
    meta = {"format": "10x_h5", "source": path, "value_type": "counts", "transpose": True,
            "obs_columns": []}
    return Bundle(X=X, genes=_squeeze_names(names, "GENE", X.shape[1]),
                  cells=_squeeze_names(barcodes, "CELL", X.shape[0]),
                  obs=_empty_obs(X.shape[0]), meta=meta)


def _read_10x_mtx(path: str) -> Bundle:
    import pandas as pd
    from scipy.io import mmread

    if os.path.isdir(path):
        folder = path
        mtx = _find_in_dir(folder, ("matrix.mtx", "matrix.mtx.gz"))
        if not mtx:
            raise C.err("bad_request", f"目录里找不到 matrix.mtx：{path}")
    else:
        folder = os.path.dirname(path) or "."
        mtx = path
    barcodes_path = _find_in_dir(folder, ("barcodes.tsv", "barcodes.tsv.gz"))
    features_path = _find_in_dir(folder, ("features.tsv", "features.tsv.gz", "genes.tsv", "genes.tsv.gz"))
    matrix = mmread(mtx)
    matrix = sp.csc_matrix(matrix) if sp.issparse(matrix) else sp.csc_matrix(np.asarray(matrix))
    genes_count, cells_count = matrix.shape
    names = []
    if features_path:
        frame = pd.read_csv(features_path, sep="\t", header=None, dtype=str)
        if frame.shape[1] >= 2:
            names = frame.iloc[:, 1].astype(str).tolist()
            kinds = frame.iloc[:, 2].astype(str).tolist() if frame.shape[1] >= 3 else None
            if kinds and any(kind == "Gene Expression" for kind in kinds):
                keep = [index for index, kind in enumerate(kinds) if kind == "Gene Expression"]
                not_observed = [index for index in range(len(names)) if index not in set(keep)]
                if not_observed:
                    raise C.err("unsupported", "10x features 含非基因特征（CRISPR/抗体），当前版本只取 Gene Expression",
                                {"hint": "用 scanpy/anndata 预处理成 h5ad 再导入"})
        else:
            names = frame.iloc[:, 0].astype(str).tolist()
    barcodes = []
    if barcodes_path:
        frame = pd.read_csv(barcodes_path, sep="\t", header=None, dtype=str)
        barcodes = frame.iloc[:, 0].astype(str).tolist()
    X = _to_csr(matrix.T)
    meta = {"format": "10x_mtx", "source": path, "value_type": "counts", "transpose": True,
            "obs_columns": []}
    return Bundle(X=X, genes=_squeeze_names(names, "GENE", X.shape[1]),
                  cells=_squeeze_names(barcodes, "CELL", X.shape[0]),
                  obs=_empty_obs(X.shape[0]), meta=meta)


def _find_in_dir(folder: str, names) -> str | None:
    try:
        entries = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        return None
    for name in names:
        if name.lower() in entries:
            return os.path.join(folder, entries[name.lower()])
    return None


def _empty_obs(count: int):
    import pandas as pd

    return pd.DataFrame(index=[f"cell-{index}" for index in range(count)])


def _read_loom(path: str) -> Bundle:
    import h5py

    with h5py.File(path, "r") as handle:
        if "layers" in handle and "matrix" in handle["layers"]:
            matrix = handle["layers/matrix"][:]
        elif "matrix" in handle:
            matrix = handle["matrix"][:]
        else:
            raise C.err("bad_request", "loom 文件里找不到 matrix")
        genes = []
        cells = []
        for group_name in ("row_attr", "col_attr"):
            group = handle.get(group_name)
            if group is None:
                continue
            for key in ("gene_names", "Gene", "Accession", "CellID", "cell_names"):
                try:
                    if key in group:
                        values = [item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                                  for item in group[key][:]]
                        if key in ("gene_names", "Gene", "Accession") and not genes:
                            genes = values
                        elif key in ("CellID", "cell_names") and not cells:
                            cells = values
                except Exception:  # noqa: BLE001
                    continue
    matrix = sp.csc_matrix(np.asarray(matrix))
    # loom 约定：行=基因，列=细胞
    X = _to_csr(matrix.T) if matrix.shape[0] >= matrix.shape[1] else _to_csr(matrix)
    meta = {"format": "loom", "source": path, "value_type": _value_type(X),
            "transpose": X.shape == matrix.shape, "obs_columns": []}
    return Bundle(X=X, genes=_squeeze_names(genes, "GENE", X.shape[1]),
                  cells=_squeeze_names(cells, "CELL", X.shape[0]),
                  obs=_empty_obs(X.shape[0]), meta=meta)


def _read_text(path: str, *, transpose: bool = False, gene_col: str | None = None) -> Bundle:
    import pandas as pd

    from io import StringIO

    with _open_text(path) as handle:
        raw = handle.read()
    lines = raw.split("\n")
    lines = [line for line in lines if not line.startswith(("!", "#"))]
    if not lines:
        raise C.err("bad_request", "文本文件是空的")
    delimiter = _detect_delimiter(lines[0] + "\n" + (lines[1] if len(lines) > 1 else ""))
    text = "\n".join(lines)
    try:
        frame = pd.read_csv(StringIO(text), sep=delimiter,
                            index_col=0 if gene_col is None else None)
    except Exception as error:  # noqa: BLE001
        raise C.err("bad_request", f"文本矩阵读取失败：{error}") from error
    if gene_col is not None and gene_col in frame.columns:
        frame = frame.set_index(gene_col)
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(axis=0, how="all").fillna(0.0)
    matrix = sp.csc_matrix(frame.to_numpy(dtype=np.float64))
    row_names = [str(item) for item in frame.index]
    col_names = [str(item) for item in frame.columns]
    # 方向：默认 行=基因、列=细胞；若行列看起来反过来则转置
    row_gene_score = _looks_like_gene(row_names)
    col_gene_score = _looks_like_gene(col_names)
    # 行名更像基因符号 ⇒ 行=基因（R/经典矩阵约定），否则行=细胞
    rows_are_genes = row_gene_score >= col_gene_score
    if transpose:
        rows_are_genes = not rows_are_genes
    if rows_are_genes:
        X = _to_csr(matrix.T)
        genes, cells = row_names, col_names
    else:
        X = _to_csr(matrix)
        genes, cells = col_names, row_names
    meta = {"format": "matrix_text", "source": path, "value_type": _value_type(X),
            "transpose": bool(rows_are_genes), "obs_columns": [],
            "heuristic": {"rows_gene_score": round(row_gene_score, 3),
                          "cols_gene_score": round(col_gene_score, 3)}}
    return Bundle(X=X, genes=_squeeze_names(genes, "GENE", X.shape[1]),
                  cells=_squeeze_names(cells, "CELL", X.shape[0]),
                  obs=_empty_obs(X.shape[0]), meta=meta)


def _read_geo_series(path: str) -> Bundle:
    import pandas as pd

    rows = []
    inside = False
    with _open_text(path) as handle:
        for line in handle:
            if line.startswith("!series_matrix_table_begin"):
                inside = True
                continue
            if line.startswith("!series_matrix_table_end"):
                break
            if inside:
                rows.append(line)
    if len(rows) < 2:
        raise C.err("bad_request", "GEO series matrix 里没有数据表段")
    from io import StringIO

    frame = pd.read_csv(StringIO("".join(rows)), sep="\t", index_col=0)
    frame = frame.apply(pd.to_numeric, errors="coerce").dropna(axis=0, how="all").fillna(0.0)
    X = _to_csr(frame.to_numpy(dtype=np.float64).T)  # 样本(cells) × 探针/基因
    meta = {"format": "geo_series", "source": path, "value_type": _value_type(X),
            "transpose": True, "obs_columns": [],
            "note": "GEO series matrix 是 bulk 矩阵；这里把每个样本当作一个“细胞”，只适合做粗粒度虚拟推演"}
    return Bundle(X=X, genes=_squeeze_names([str(item) for item in frame.index], "GENE", X.shape[1]),
                  cells=_squeeze_names([str(item) for item in frame.columns], "GSM", X.shape[0]),
                  obs=_empty_obs(X.shape[0]), meta=meta)


def _extract_archive(path: str, cache_dir: str) -> str:
    cache_dir = C.ensure_dir(cache_dir or tempfile.mkdtemp(prefix="vc-unpack-"))
    folder = C.ensure_dir(os.path.join(cache_dir, f"unpack-{int(time.time() * 1000)}"))
    lower = path.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            total = 0
            for index, info in enumerate(archive.infolist()):
                if index > MAX_ARCHIVE_ENTRIES:
                    raise C.err("bad_request", "压缩包条目过多")
                total += max(0, info.file_size)
                if total > MAX_ARCHIVE_BYTES:
                    raise C.err("bad_request", "解压后体积超过 20 GB")
                target = os.path.join(folder, info.filename)
                if not os.path.abspath(target).startswith(os.path.abspath(folder) + os.sep):
                    raise C.err("bad_request", f"压缩包内路径非法：{info.filename}")
            archive.extractall(folder)
    else:
        try:
            with tarfile.open(path, "r:*") as archive:
                members = archive.getmembers()[:MAX_ARCHIVE_ENTRIES]
                for member in members:
                    target = os.path.join(folder, member.name)
                    if not os.path.abspath(target).startswith(os.path.abspath(folder) + os.sep):
                        raise C.err("bad_request", f"压缩包内路径非法：{member.name}")
                archive.extractall(folder, members=members)
        except tarfile.TarError as error:
            raise C.err("bad_request", f"压缩包解压失败：{error}") from error
    return folder


def _find_data_files(folder: str) -> list[str]:
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            full = os.path.join(root, name)
            if _is_data_file(full):
                found.append(full)
    return sorted(found)


def _download(url: str, cache_dir: str) -> str:
    cache_dir = C.ensure_dir(cache_dir or tempfile.mkdtemp(prefix="vc-download-"))
    name = os.path.basename(url.split("?")[0]) or "download"
    target = os.path.join(cache_dir, C.safe_name(name, "download", limit=120))
    request = urllib.request.Request(url, headers={"User-Agent": "dsh-virtualcell/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as handle:
            size = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise C.err("bad_request", "下载超过 8 GB 上限")
                handle.write(chunk)
    except C.EngineError:
        raise
    except Exception as error:  # noqa: BLE001
        raise C.err("bad_request", f"下载失败：{error}") from error
    return target


# ---------------------------------------------------------------------------
# 对外：inspect / load
# ---------------------------------------------------------------------------

def inspect_path(path: str, *, data_format: str = "auto", member=None, transpose: bool = False,
                 object=None, sheet=None, gene_col=None, max_cells: int = 40000,
                 cache_dir: str | None = None) -> dict:
    info = sniff(path, data_format=data_format, transpose=transpose)
    fmt = info["format"]
    source = info["path"]
    notes = list(info.get("notes") or [])
    started = time.time()
    detail = {"format": fmt, "path": source, "member": info.get("member")}
    try:
        if fmt == "url":
            local = _download(source, cache_dir or tempfile.mkdtemp(prefix="vc-download-"))
            nested = inspect_path(local, data_format="auto", transpose=transpose, cache_dir=cache_dir)
            nested["path"] = source
            nested["downloadedTo"] = local
            nested.setdefault("notes", []).append("已下载到本地缓存")
            return nested
        if fmt == "archive":
            folder = _extract_archive(source, cache_dir or tempfile.mkdtemp(prefix="vc-unpack-"))
            candidates = _find_data_files(folder)
            candidates = [item for item in candidates if not _is_archive(item)]
            if not candidates:
                raise C.err("bad_request", "压缩包里没找到可识别的数据文件")
            if len(candidates) > 1:
                tenx = [item for item in candidates if os.path.basename(item).lower().startswith("matrix.mtx")]
                if tenx:
                    candidates = tenx
            if len(candidates) > 1:
                raise C.err("ambiguous", "压缩包里有多份候选数据文件，请用 member 指定或解压后单独导入",
                            {"candidates": [os.path.relpath(item, folder) for item in candidates[:30]],
                             "extractedTo": folder})
            nested = inspect_path(candidates[0], transpose=transpose, cache_dir=cache_dir)
            nested["path"] = source
            nested["member"] = os.path.relpath(candidates[0], folder)
            nested["extractedTo"] = folder
            nested.setdefault("notes", []).append("已解包")
            return nested
        if fmt == "h5ad":
            import anndata as ad

            adata = ad.read_h5ad(source, backed="r")
            shape = adata.shape
            genes = [str(item) for item in adata.var_names[:20]]
            cells = [str(item) for item in adata.obs_names[:10]]
            obs_cols = [str(item) for item in adata.obs.columns]
            try:
                value_type = _value_type(adata.X)
            except Exception:  # noqa: BLE001
                value_type = "unknown"
            detail.update({"n_cells": int(shape[0]), "n_genes": int(shape[1]),
                           "value_type": value_type, "sample_genes": genes, "sample_cells": cells,
                           "obs_columns": obs_cols})
        elif fmt == "10x_h5":
            import h5py

            with h5py.File(source, "r") as handle:
                shape = tuple(int(v) for v in handle["matrix/shape"][:])
                genes_name = "features" if "features" in handle["matrix"] else "genes"
                genes = [item.decode("utf-8", "replace") for item in handle[f"matrix/{genes_name}/name"][:20]]
            detail.update({"n_cells": shape[1], "n_genes": shape[0], "value_type": "counts",
                           "sample_genes": genes, "obs_columns": []})
        elif fmt == "10x_mtx":
            bundle = _read_10x_mtx(source)
            detail.update({"n_cells": bundle.n_cells, "n_genes": bundle.n_genes,
                           "value_type": "counts", "sample_genes": bundle.genes[:20],
                           "sample_cells": bundle.cells[:10], "obs_columns": []})
        elif fmt == "loom":
            import h5py

            with h5py.File(source, "r") as handle:
                shape = tuple(int(v) for v in handle["matrix"].shape)
            detail.update({"n_cells": max(shape), "n_genes": min(shape), "value_type": "unknown",
                           "obs_columns": []})
        elif fmt == "geo_series":
            bundle = _read_geo_series(source)
            detail.update({"n_cells": bundle.n_cells, "n_genes": bundle.n_genes,
                           "value_type": bundle.meta.get("value_type"),
                           "sample_genes": bundle.genes[:20], "sample_cells": bundle.cells[:10],
                           "obs_columns": []})
        else:
            with _open_text(source) as handle:
                head = [next(handle) for _ in range(min(6, 6))]
            delimiter = _detect_delimiter("".join(head))
            columns = head[0].rstrip("\n").split(delimiter)
            detail.update({"n_columns": len(columns), "delimiter": delimiter,
                           "sample_columns": [item.strip() for item in columns[:12]],
                           "value_type": "unknown", "obs_columns": []})
    except C.EngineError:
        raise
    except Exception as error:  # noqa: BLE001
        raise C.err("bad_request", f"探测失败：{error}") from error
    detail["notes"] = notes
    detail["elapsedMs"] = int((time.time() - started) * 1000)
    return C.jsonable(detail)


def load_dataset(path: str, *, data_format: str = "auto", member=None, transpose: bool = False,
                 object=None, sheet=None, gene_col=None, max_cells: int = 40000,
                 n_cells: int | None = None, n_genes: int | None = None,
                 cache_dir: str | None = None) -> Bundle:
    info = sniff(path, data_format=data_format, transpose=transpose)
    fmt = info["format"]
    source = info["path"]
    if fmt == "url":
        source = _download(source, cache_dir or tempfile.mkdtemp(prefix="vc-download-"))
        info = sniff(source)
        fmt = info["format"]
    if fmt == "archive":
        folder = _extract_archive(source, cache_dir or tempfile.mkdtemp(prefix="vc-unpack-"))
        candidates = _find_data_files(folder)
        if member:
            candidates = [os.path.join(folder, member)]
        tenx = [item for item in candidates if os.path.basename(item).lower().startswith("matrix.mtx")]
        if len(candidates) > 1 and tenx:
            candidates = tenx
        if not candidates:
            raise C.err("bad_request", "压缩包里没找到可识别的数据文件")
        if len(candidates) > 1:
            raise C.err("ambiguous", "压缩包里有多份候选数据文件，请用 member 指定",
                        {"candidates": [os.path.relpath(item, folder) for item in candidates[:30]],
                         "extractedTo": folder})
        source = candidates[0]
        info = sniff(source)
        fmt = info["format"]

    starters = {"h5ad": _read_h5ad, "10x_h5": _read_10x_h5, "10x_mtx": _read_10x_mtx,
                "loom": _read_loom, "geo_series": _read_geo_series}
    if fmt in starters:
        bundle = starters[fmt](source)
    elif fmt == "matrix_text":
        bundle = _read_text(source, transpose=transpose, gene_col=gene_col)
    else:
        raise C.err("unsupported", f"不支持的格式：{fmt}")
    bundle.meta.setdefault("format", fmt)
    bundle.meta.setdefault("source", source)
    if transpose and fmt not in ("matrix_text",):
        bundle.meta["transposeForced"] = True

    # 统一后处理：稀疏、有限值、去空行列、规模上限
    X = bundle.X.tocsr().astype(np.float32)
    if X.nnz and not np.all(np.isfinite(np.asarray(X.data[: min(200000, X.nnz)]))):
        X.data[~np.isfinite(X.data)] = 0.0
    keep_cells = np.asarray(X.sum(axis=1)).ravel() > 0
    keep_genes = np.asarray(X.sum(axis=0)).ravel() > 0
    if keep_cells.sum() < X.shape[0] or keep_genes.sum() < X.shape[1]:
        bundle.meta.setdefault("dropped", {})["empty_cells"] = int(X.shape[0] - keep_cells.sum())
        bundle.meta["dropped"]["empty_genes"] = int(X.shape[1] - keep_genes.sum())
        X = X[keep_cells][:, keep_genes]
        bundle.genes = [gene for gene, keep in zip(bundle.genes, keep_genes) if keep]
        bundle.cells = [cell for cell, keep in zip(bundle.cells, keep_cells) if keep]
        if getattr(bundle, "obs", None) is not None and len(bundle.obs) == len(keep_cells):
            bundle.obs = bundle.obs.loc[np.asarray(bundle.obs.index)[keep_cells]]
    limit = int(max_cells or 40000)
    if n_cells:
        limit = min(limit, int(n_cells))
    dropped_cells = int(X.shape[0])
    if X.shape[0] > limit:
        rng = np.random.default_rng(0)
        index = np.sort(rng.choice(X.shape[0], size=limit, replace=False))
        X = X[index]
        bundle.cells = [bundle.cells[item] for item in index]
        obs = getattr(bundle, "obs", None)
        if obs is not None and len(obs) == dropped_cells:
            bundle.obs = obs.iloc[index]
        bundle.meta["subsample"] = {"from": dropped_cells, "to": int(limit)}
    # obs 索引统一对齐到细胞名（10x/文本导入时 obs 只是占位）
    obs = getattr(bundle, "obs", None)
    if obs is not None and len(obs) == X.shape[0]:
        obs = obs.copy()
        obs.index = bundle.cells
        bundle.obs = obs
    elif obs is not None:
        bundle.obs = _empty_obs(X.shape[0])
        bundle.obs.index = bundle.cells
    if X.shape[0] == 0 or X.shape[1] == 0:
        raise C.err("bad_request", "读取后矩阵为空（全 0 或过滤过度）")
    bundle.X = X
    bundle.meta["n_cells"] = int(X.shape[0])
    bundle.meta["n_genes"] = int(X.shape[1])
    bundle.meta["value_type"] = bundle.meta.get("value_type") or _value_type(X)
    if bundle.meta["value_type"] == "scaled":
        bundle.meta.setdefault("warnings", []).append("矩阵含负值（可能已标准化/scale），质控与状态表征会按 log 空间处理")
    return bundle


def write_h5ad(bundle: Bundle, path: str) -> None:
    import anndata as ad
    import pandas as pd

    obs = getattr(bundle, "obs", None)
    if obs is None or len(obs) != bundle.n_cells:
        obs = pd.DataFrame(index=bundle.cells)
    var = pd.DataFrame(index=bundle.genes)
    adata = ad.AnnData(X=bundle.X.tocsr().astype(np.float32), obs=obs, var=var)
    adata.uns["dsh_virtualcell_meta"] = {key: value for key, value in (bundle.meta or {}).items()
                                        if isinstance(value, (str, int, float, bool, list))}
    C.ensure_dir(os.path.dirname(os.path.abspath(path)))
    adata.write_h5ad(path)


def sha256_of(path: str) -> str:
    return C.sha256_of(path)


# ---------------------------------------------------------------------------
# 示例数据（带真值）
# ---------------------------------------------------------------------------

def load_demo(seed: int = 0, n_cells: int = 2000, n_genes: int = 1000) -> Bundle:
    """生成可控的虚拟细胞示例数据（见模块 docstring）。

    模型：`log_mu[g, c] = base[g] + Σ_t A[g, t]·activity[t, c] + marker_boost[g, state(c)]`
    计数 ~ Poisson(mu·depth)，再按基因做 25% dropout。
    真值 A 即 `truth['grn']`；`truth['perturb']` 用 A 解析传播（与推断器无关）。
    """
    import pandas as pd

    rng = C.rng_for(seed)
    n_cells = int(max(200, n_cells))
    n_genes = int(max(240, n_genes))

    tf_pool_all = [gene for gene in P.tf_universe()]
    mandatory_tfs = []
    for state in P.DEMO_STATES:
        mandatory_tfs.extend(state["master_tfs"])
    mandatory_tfs.extend(P.DEMO_HELDOUT_TFS)
    tf_budget = int(min(120, max(40, n_genes // 8), len(tf_pool_all)))
    tf_pool = list(dict.fromkeys([gene for gene in mandatory_tfs if gene in tf_pool_all]))
    rest_pool = [gene for gene in tf_pool_all if gene not in set(tf_pool)]
    extra_tfs = int(max(0, tf_budget - len(tf_pool)))
    if extra_tfs and rest_pool:
        tf_pool.extend(str(gene) for gene in rng.choice(np.asarray(rest_pool, dtype=object),
                                                       size=min(extra_tfs, len(rest_pool)), replace=False))
    marker_genes = []
    for genes in P.STATE_MARKERS.values():
        marker_genes.extend(genes)
    target_pool = []
    for genes in P.PATHWAY_SETS.values():
        target_pool.extend(genes)
    target_pool.extend(marker_genes)
    target_pool.extend(P.DEMO_EXTRA_TARGETS)
    target_pool.extend(P.DEMO_COMMON_GENES)
    tf_set = set(tf_pool)
    target_pool = [gene for gene in dict.fromkeys(target_pool) if gene not in tf_set]

    genes = list(dict.fromkeys(tf_pool + target_pool))
    simulated = 0
    if len(genes) < n_genes:
        extra = n_genes - len(genes)
        genes.extend(f"SIMG{index + 1:04d}" for index in range(extra))
        simulated = extra
    genes = genes[:n_genes]
    n_actual = len(genes)
    gene_index = {gene: index for index, gene in enumerate(genes)}
    tf_names = [gene for gene in genes if gene in set(tf_pool)]
    tf_index = {gene: index for index, gene in enumerate(tf_names)}
    target_names = [gene for gene in genes if gene not in set(tf_names)]
    heldout = [gene for gene in P.DEMO_HELDOUT_TFS if gene in gene_index]
    state_names = [state["name"] for state in P.DEMO_STATES]
    n_states = len(state_names)

    # 1) 真值网络 A（genes × tfs）
    A = np.zeros((n_actual, len(tf_names)), dtype=np.float32)
    edges = []
    marker_owner = {}
    for state in P.DEMO_STATES:
        for gene in state["markers"]:
            marker_owner.setdefault(gene, state["name"])
    for target in target_names:
        owner = marker_owner.get(target)
        if owner and rng.random() < 0.55:
            candidates = [tf for tf in P.DEMO_STATES[state_names.index(owner)]["master_tfs"]
                          if tf in tf_index]
        else:
            candidates = [tf for tf in tf_names if tf not in heldout]
        if not candidates:
            continue
        k = int(rng.integers(1, 4))
        chosen = rng.choice(np.asarray(candidates, dtype=object), size=min(k, len(candidates)), replace=False)
        for tf in chosen:
            sign = 1.0 if rng.random() < 0.7 else -1.0
            weight = float(abs(rng.normal(0.60, 0.15)))
            A[gene_index[target], tf_index[tf]] = sign * weight
            edges.append({"tf": str(tf), "target": str(target), "sign": int(sign), "weight": round(weight, 4)})
    # 留出 TF 的专属下游（保证敲除有可观效应）
    for tf in heldout:
        own = [gene for gene in target_names if rng.random() < 0.03][:28]
        for target in own:
            sign = 1.0 if rng.random() < 0.75 else -1.0
            weight = float(abs(rng.normal(0.70, 0.15)))
            A[gene_index[target], tf_index[tf]] = sign * weight
            edges.append({"tf": tf, "target": target, "sign": int(sign), "weight": round(weight, 4)})
    # TF-TF 少量互调
    for _ in range(int(len(tf_names) * 0.08)):
        source = str(rng.choice(np.asarray(tf_names, dtype=object)))
        target = str(rng.choice(np.asarray(tf_names, dtype=object)))
        if source == target:
            continue
        weight = float(abs(rng.normal(0.2, 0.08)))
        A[tf_index[target], tf_index[source]] = weight
        edges.append({"tf": source, "target": target, "sign": 1, "weight": round(weight, 4)})

    # 1.5) TF 自调控项：让 TF 的 mRNA 能反映自身活性（共表达推断的前提）。
    #      真实数据里 TF 表达与活性高度相关；不加这一项，TF mRNA 与其靶基因无关联，
    #      任何 co-expression 方法都不可能恢复真值网络（demo 会系统性"校验失败"）。
    for tf_name in tf_names:
        A[tf_index[tf_name], tf_index[tf_name]] = 1.0

    # 2) 细胞状态
    proportions = np.asarray([0.24, 0.20, 0.16, 0.12, 0.10, 0.18], dtype=float)
    proportions = proportions[:n_states]
    proportions = proportions / proportions.sum()
    counts = rng.multinomial(n_cells, proportions)
    labels = np.concatenate([np.full(int(count), index, dtype=np.int32) for index, count in enumerate(counts)])
    rng.shuffle(labels)

    # 3) TF 活性：**细胞级连续波动**（模拟调控噪声/梯度）+ 状态主控 TF 的状态加成
    #    只做"状态开关"会让状态结构主导共表达，任何按簇拟合的方法都难以恢复真实调控边；
    #    真实单细胞里 TF 活性在同一状态内也是连续波动的，这里按后者生成。
    activity = rng.lognormal(0.0, 0.7, size=(len(tf_names), n_cells)).astype(np.float32)
    for state_index, state in enumerate(P.DEMO_STATES):
        mask = labels == state_index
        for tf in state["master_tfs"]:
            if tf in tf_index:
                activity[tf_index[tf], mask] += 1.0

    # 4) 表达均值
    base = rng.normal(0.15, 0.45, size=n_actual).astype(np.float32)
    scale = rng.lognormal(0.0, 0.35, size=n_actual).astype(np.float32)
    signal = (A * scale[:, None]) @ activity  # genes × cells
    log_mu = base[:, None] + signal
    # 状态程序：每个状态额外上调一批 target（模拟状态特异程序，增强状态可分性）
    state_programs = {}
    for state_index, state in enumerate(P.DEMO_STATES):
        picks = rng.choice(np.asarray(target_names, dtype=object),
                           size=min(40, len(target_names)), replace=False)
        state_programs[state_index] = [str(item) for item in picks]
        mask = labels == state_index
        if not mask.any():
            continue
        for gene in state["markers"]:
            if gene in gene_index:
                log_mu[gene_index[gene], mask] += float(rng.uniform(2.4, 3.4))
        for tf in state["master_tfs"]:
            if tf in gene_index:
                log_mu[gene_index[tf], mask] += float(rng.uniform(1.8, 2.4))
        for gene in state_programs[state_index]:
            if gene in gene_index:
                log_mu[gene_index[gene], mask] += float(rng.uniform(0.7, 1.2))
    log_mu += rng.normal(0.0, 0.20, size=log_mu.shape).astype(np.float32)
    mu = np.exp(np.clip(log_mu, -6.0, 6.0))
    depth = rng.lognormal(np.log(2.2), 0.2, size=n_cells).astype(np.float32)
    counts = rng.poisson(mu * depth[None, :]).astype(np.float32)
    # dropout（按基因）
    dropout = rng.random(size=counts.shape) < 0.10
    counts[dropout] = 0.0
    # 保证每细胞有足够 counts
    totals = counts.sum(axis=0)
    low = totals < 60
    if low.any():
        counts[:, low] += rng.poisson(mu[:, low] * 2.0).astype(np.float32)
    X = sp.csr_matrix(counts.T.astype(np.float32))  # cells × genes
    mean_expr = np.asarray(X.mean(axis=0)).ravel()

    cells = [f"demo-cell-{index + 1:05d}" for index in range(n_cells)]
    obs = pd.DataFrame({"cell_state": [state_names[index] for index in labels],
                        "state_index": labels.astype(int)}, index=cells)

    # 5) 真值扰动 Δ（解析传播，与推断器无关）
    truth_perturb = {}
    truth_perturb_meta = {}
    tf_mean = {tf: float(np.asarray(X[:, gene_index[tf]].mean())) for tf in tf_names}
    tf_rows = np.asarray([gene_index[name] for name in tf_names], dtype=int)
    for tf in heldout:
        delta = np.zeros(n_actual, dtype=np.float32)
        delta += -A[:, tf_index[tf]] * tf_mean[tf]
        for _ in range(3):
            delta_tf = delta[tf_rows].copy()
            delta_tf[tf_index[tf]] = 0.0  # 被扰动 TF 本身的 Δ 已计入首项，不重复传播
            delta += A @ delta_tf
        truth_perturb[tf] = delta.astype(np.float32)
        order = np.argsort(-np.abs(delta))[:20]
        truth_perturb_meta[tf] = {"top_genes": [genes[index] for index in order],
                                  "max_abs_delta": float(np.abs(delta).max())}

    # 6) Perturb-seq 样表（control + 5 个敲低）
    table_rows = {}
    def simulate_block(labels_block, active_mask, n_block):
        signal_block = (A * scale[:, None]) @ (activity_block(active_mask, n_block, rng))
        log_mu_block = base[:, None] + signal_block
        for state_index, state in enumerate(P.DEMO_STATES):
            mask = labels_block == state_index
            if not mask.any():
                continue
            for gene in state["markers"]:
                if gene in gene_index:
                    log_mu_block[gene_index[gene], mask] += 1.2
        log_mu_block += rng.normal(0.0, 0.35, size=log_mu_block.shape).astype(np.float32)
        mu_block = np.exp(np.clip(log_mu_block, -6.0, 6.0))
        depth_block = rng.lognormal(0.0, 0.3, size=n_block).astype(np.float32)
        block = rng.poisson(mu_block * depth_block[None, :]).astype(np.float32)
        block[rng.random(size=block.shape) < 0.3] = 0.0
        return block.T

    def activity_block(active_tfs, n_block, generator):
        block = np.zeros((len(tf_names), n_block), dtype=np.float32)
        for tf in active_tfs:
            if tf in tf_index:
                block[tf_index[tf], :] = 1.0
        block *= generator.lognormal(0.0, 0.2, size=block.shape).astype(np.float32)
        return block

    control_labels = rng.choice(n_states, size=200, replace=True)
    control_active = [tf for state in P.DEMO_STATES for tf in state["master_tfs"]]
    table_blocks = [simulate_block(control_labels, control_active, 200)]
    pert_labels = ["control"] + list(heldout)
    for tf in heldout:
        block_labels = rng.choice(n_states, size=120, replace=True)
        active = [name for name in control_active if name != tf]
        table_blocks.append(simulate_block(block_labels, active, 120))
    matrix = np.vstack(table_blocks)
    frame = pd.DataFrame(matrix, columns=genes)
    frame.insert(0, "perturbation", sum(([label] * 200 if index == 0 else [label] * 120
                                         for index, label in enumerate(pert_labels)), []))
    frame.insert(0, "cell_id", [f"ps-{index + 1:05d}" for index in range(len(frame))])

    truth = {
        "grn": pd.DataFrame(edges),
        "perturb": truth_perturb,
        "perturb_meta": truth_perturb_meta,
        "perturb_table": frame,
        "labels": np.asarray([state_names[index] for index in labels]),
        "states": state_names,
        "heldout_tfs": heldout,
        "gene_role": {"tfs": tf_names, "markers": [gene for gene in marker_genes if gene in gene_index]},
        "state_programs": {P.DEMO_STATES[index]["name"]: genes_program
                           for index, genes_program in state_programs.items()},
        "pathways": P.PATHWAY_SETS,
    }
    meta = {"format": "demo", "source": "demo", "value_type": "counts", "transpose": False,
            "seed": int(seed), "n_cells": int(n_cells), "n_genes": int(n_actual),
            "n_simulated_genes": int(simulated), "n_states": n_states,
            "state_names": state_names, "heldout_tfs": heldout,
            "obs_columns": ["cell_state", "state_index"],
            "note": "合成数据：真值网络 + 留出扰动，用于校验流程与出图；不含任何真实病人信息"}
    return Bundle(X=X, genes=genes, cells=cells, obs=obs, meta=meta, truth=truth)


def tf_mean_vec(tf_names, tf_mean) -> np.ndarray:
    return np.asarray([tf_mean[name] for name in tf_names], dtype=np.float32)
