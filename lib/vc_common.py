#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — Python 侧公共设施。

只放：错误类型、JSON 化、进度输出、目录/文件小工具、运行记录（RunStore）、
绘图通用设置。业务计算在各 vc_*.py 模块里。

依赖：numpy / pandas / scipy（装不上就别跑引擎，engine_info 会报缺包）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# 调色板（数据色，与 Client 端 PALETTE 对齐）
# ---------------------------------------------------------------------------

PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948",
    "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac", "#1f77b4", "#ff7f0e",
    "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    "#bcbd22", "#17becf",
]

CONTINUOUS_CMAP = "cividis"
DIVERGING_CMAP = "RdBu_r"

#: 中文标题字体回退链（macOS / Windows / Linux）
CJK_FONTS = [
    "PingFang SC", "Hiragino Sans GB", "Heiti SC", "Songti SC",
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC",
    "WenQuanYi Micro Hei", "Arial Unicode MS", "DejaVu Sans",
]


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class EngineError(Exception):
    """带错误码的业务异常；vc_engine 会原样转成 JSON 错误。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = jsonable(self.details)
        return out


def err(code: str, message: str, **details) -> EngineError:
    return EngineError(code, message, details)


# ---------------------------------------------------------------------------
# JSON 化
# ---------------------------------------------------------------------------

def _jsonable_scalar(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.str_,)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def jsonable(value, _depth: int = 0):
    """把 numpy / pandas / 稀疏矩阵变成可 json.dumps 的结构（有深度上限）。"""
    if _depth > 8:
        return str(type(value).__name__)
    value = _jsonable_scalar(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v, _depth + 1) for v in value]
    # numpy 数组
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return jsonable(value.item(), _depth + 1)
        if value.ndim == 1:
            return [jsonable(v, _depth + 1) for v in value.tolist()]
        return {"shape": list(value.shape), "dtype": str(value.dtype)}
    # pandas
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return {"rows": int(value.shape[0]), "columns": [str(c) for c in value.columns]}
        if isinstance(value, pd.Series):
            return [jsonable(v, _depth + 1) for v in value.tolist()]
        if isinstance(value, pd.Index):
            return [str(v) for v in value.tolist()]
    except Exception:  # noqa: BLE001
        pass
    # scipy 稀疏
    try:
        import scipy.sparse as sp

        if sp.issparse(value):
            return {"shape": list(value.shape), "nnz": int(value.nnz), "sparse": True}
    except Exception:  # noqa: BLE001
        pass
    if hasattr(value, "tolist"):
        try:
            return jsonable(value.tolist(), _depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return str(type(value).__name__)


def fnum(value, digits: int = 4):
    """安全数值：NaN/Inf → None，保留 digits 位。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def fvec(values, digits: int = 4, limit: int = 5000):
    out = []
    for value in np.asarray(values).ravel()[:limit]:
        out.append(fnum(value, digits))
    return out


# ---------------------------------------------------------------------------
# 进度（stdout 协议行；由 vc_engine 的 emit 注册回调）
# ---------------------------------------------------------------------------

_PROGRESS = {"fn": None}


def bind_progress(fn) -> None:
    """vc_engine 启动时注册：fn(message, pct)。"""
    _PROGRESS["fn"] = fn


def progress(message: str, pct: float | None = None) -> None:
    fn = _PROGRESS.get("fn")
    if not fn:
        return
    try:
        fn(str(message), None if pct is None else max(0.0, min(100.0, float(pct))))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 文件工具
# ---------------------------------------------------------------------------

def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def write_json(path: str, value, *, indent: int = 2) -> str:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(jsonable(value), handle, ensure_ascii=False, indent=indent, allow_nan=False)
    os.replace(tmp, path)
    return path


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def sha256_of(path: str, limit_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = limit_bytes
    with open(path, "rb") as handle:
        while True:
            size = 1 << 20
            if remaining is not None:
                if remaining <= 0:
                    break
                size = min(size, remaining)
            chunk = handle.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


_SAFE = re.compile(r"[^\w.\-\u4e00-\u9fa5]+")


def safe_name(name: str, fallback: str = "item", limit: int = 80) -> str:
    base = os.path.basename(str(name or "")).strip()
    base = _SAFE.sub("_", base)
    base = base.strip("._") or fallback
    return base[:limit]


def fmt_bytes(size: int | float) -> str:
    number = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


# ---------------------------------------------------------------------------
# 表格落盘
# ---------------------------------------------------------------------------

def write_table(path: str, rows, columns=None) -> str:
    """rows: list[dict] 或 list[list] 或 pandas.DataFrame；写 UTF-8 CSV。"""
    import pandas as pd

    ensure_dir(os.path.dirname(os.path.abspath(path)))
    if isinstance(rows, pd.DataFrame):
        frame = rows
    elif rows and isinstance(rows[0], dict):
        frame = pd.DataFrame(rows, columns=columns)
    else:
        frame = pd.DataFrame(rows, columns=columns)
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def table_preview(rows, columns=None, limit: int = 8):
    """给 UI 的小预览：{'columns':[...], 'rows':[[...]]}"""
    import pandas as pd

    try:
        if isinstance(rows, pd.DataFrame):
            frame = rows
        elif rows and isinstance(rows[0], dict):
            frame = pd.DataFrame(rows, columns=columns)
        elif rows and isinstance(rows[0], (list, tuple)):
            frame = pd.DataFrame(rows, columns=columns)
        else:
            return {"columns": [], "rows": []}
        head = frame.head(limit)
        return {
            "columns": [str(c) for c in head.columns],
            "rows": [[jsonable(v) for v in record] for record in head.itertuples(index=False)],
            "total": int(frame.shape[0]),
        }
    except Exception:  # noqa: BLE001
        return {"columns": [], "rows": []}


# ---------------------------------------------------------------------------
# 运行记录（原始数据保留的核心）
# ---------------------------------------------------------------------------

@dataclass
class RunStore:
    """每个数据集一个 runs/ 目录，只增不改。

    目录结构见 CONTRACT §3.3。任何步骤都通过 store.begin() 建目录，
    再 store.finish() 落 result/env/index。
    """

    dataset_dir: str
    dataset_id: str
    _counter: int = field(default=0, init=False)

    @property
    def root(self) -> str:
        return os.path.join(self.dataset_dir, "runs")

    @property
    def index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def _next_id(self) -> str:
        rows = list_runs(self.root)
        used = set()
        for item in rows:
            rid = str(item.get("id") or "")
            match = re.match(r"run-(\d+)$", rid)
            if match:
                used.add(int(match.group(1)))
        number = 1
        while number in used:
            number += 1
        return f"run-{number:04d}"

    def begin(self, step: str, label: str = "", params: dict | None = None) -> dict:
        run_id = self._next_id()
        run_dir = os.path.join(self.root, run_id)
        for sub in ("tables", "figures"):
            ensure_dir(os.path.join(run_dir, sub))
        core = {
            "id": run_id,
            "dataset": self.dataset_id,
            "step": str(step),
            "label": str(label or step),
            "status": "running",
            "startedAt": now_iso(),
            "paramsPath": os.path.join(run_dir, "params.json"),
            "dir": run_dir,
        }
        write_json(os.path.join(run_dir, "params.json"), params or {})
        return {**core, "runDir": run_dir}

    def finish(self, run: dict, *, status: str = "ok", result: dict | None = None,
               env: dict | None = None, log: str = "", inputs: dict | None = None) -> dict:
        run_dir = run["runDir"]
        duration = int(round((time.time() - _parse_iso(run.get("startedAt"))) * 1000))
        payload = dict(result or {})
        # 大数组不进 result.json（CONTRACT §3.3）
        payload.pop("arrays", None)
        payload.pop("vectors", None)
        write_json(os.path.join(run_dir, "result.json"), payload)
        write_json(os.path.join(run_dir, "env.json"), {
            "python": sys.version,
            "executable": sys.executable,
            "backend": (env or {}).get("backend"),
            "packages": (env or {}).get("packages"),
            "seed": (env or {}).get("seed"),
            "durationMs": duration,
            "finishedAt": now_iso(),
            **(env or {}),
        })
        if log:
            with open(os.path.join(run_dir, "log.txt"), "w", encoding="utf-8") as handle:
                handle.write(log[-200_000:])
        if inputs:
            write_json(os.path.join(run_dir, "inputs.json"), inputs)
        entry = {
            "id": run["id"],
            "dataset": self.dataset_id,
            "step": run["step"],
            "label": run["label"],
            "status": status,
            "startedAt": run["startedAt"],
            "durationMs": duration,
            "figures": len(list_dir(os.path.join(run_dir, "figures"))),
            "tables": len(list_dir(os.path.join(run_dir, "tables"))),
            "dir": run_dir,
            "summary": str((result or {}).get("summary") or "")[:400],
        }
        _append_index(self.root, entry)
        return entry


def _parse_iso(text: str) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return time.time()


def _append_index(root: str, entry: dict) -> None:
    ensure_dir(root)
    path = os.path.join(root, "index.json")
    rows = read_json(path, []) or []
    rows.append(entry)
    write_json(path, rows[-500:])


def list_runs(root: str) -> list:
    return read_json(os.path.join(root, "index.json"), []) or []


def list_dir(path: str) -> list:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


# ---------------------------------------------------------------------------
# 绘图通用
# ---------------------------------------------------------------------------

def setup_matplotlib():
    """配置 Agg + 中文字体；返回 pyplot。失败时抛 EngineError。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        available = {font.name for font in font_manager.fontManager.ttflist}
        chosen = [name for name in CJK_FONTS if name in available]
        if not chosen:
            chosen = ["DejaVu Sans"]
        plt.rcParams["font.sans-serif"] = chosen + ["DejaVu Sans"]
        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["axes.spines.top"] = False
        plt.rcParams["axes.spines.right"] = False
        plt.rcParams["figure.dpi"] = 200
        plt.rcParams["savefig.dpi"] = 200
        plt.rcParams["savefig.bbox"] = "tight"
        plt.rcParams["axes.grid"] = False
        plt.rcParams["legend.frameon"] = False
        return plt
    except Exception as exc:  # noqa: BLE001
        raise err("internal", f"matplotlib 不可用：{exc}") from exc


def save_figure(fig, out_path: str, *, dpi: int = 200) -> dict:
    ensure_dir(os.path.dirname(os.path.abspath(out_path)))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    import matplotlib.pyplot as plt

    plt.close(fig)
    width = height = 0
    try:
        from PIL import Image

        with Image.open(out_path) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001
        try:
            width = int(fig.get_figwidth() * dpi)
            height = int(fig.get_figheight() * dpi)
        except Exception:  # noqa: BLE001
            width = height = 0
    return {
        "file": out_path,
        "width": width,
        "height": height,
        "bytes": os.path.getsize(out_path) if os.path.exists(out_path) else 0,
    }


def palette_color(index: int) -> str:
    return PALETTE[int(index) % len(PALETTE)]


def color_map(labels) -> dict:
    """把任意标签数组映射成稳定的颜色表。"""
    colors = {}
    order = []
    for label in labels:
        key = str(label)
        if key not in colors:
            colors[key] = palette_color(len(order))
            order.append(key)
    return colors


# ---------------------------------------------------------------------------
# 数组小工具
# ---------------------------------------------------------------------------

def as_dense(matrix):
    try:
        import scipy.sparse as sp

        if sp.issparse(matrix):
            return np.asarray(matrix.todense(), dtype=np.float32)
    except Exception:  # noqa: BLE001
        pass
    return np.asarray(matrix, dtype=np.float32)


def robust_scale(matrix, axis: int = 0):
    """逐列（基因）中位数/IQR 稳健标准化，返回 (scaled, center, scale)。"""
    array = as_dense(matrix)
    center = np.median(array, axis=axis, keepdims=True)
    q1 = np.percentile(array, 25, axis=axis, keepdims=True)
    q3 = np.percentile(array, 75, axis=axis, keepdims=True)
    scale = q3 - q1
    scale[scale < 1e-8] = 1.0
    return (array - center) / scale, center, scale


def top_indices(values, n: int, *, largest: bool = True, abs_value: bool = True):
    array = np.asarray(values, dtype=float)
    if abs_value:
        array = np.abs(array)
    order = np.argsort(-array if largest else array)
    return order[: max(1, min(int(n), order.size))]


def rng_for(seed) -> np.random.Generator:
    return np.random.default_rng(int(seed) if seed is not None else 0)


def stable_seed(seed, *parts) -> int:
    text = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(f"{seed}|{text}".encode("utf-8")).hexdigest()[:8], 16)
