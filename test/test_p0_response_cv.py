#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #5 回归：response 去伪 control / 折内标准化 / donor 留出 / 线性基线。

修复前 `lib/vc_response.py:run_response()`：
  * 没有 control 行时用「全表均值」当伪 control 静默继续；
  * `_standardize_features()` 在交叉验证之外对全量特征拟合（信息泄漏）；
  * 没有 donor/样本分组留出；
  * 只有均值位移基线，没有线性基线对照。
修复后：缺 control 抛 prerequisite；每折只用训练扰动的统计量标准化；
表里有 donor 列时按 donor 留出；metrics 增加 `baseline_linear_r2` 与 `donor_holdout`。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_common as C  # noqa: E402
import vc_io  # noqa: E402
import vc_response  # noqa: E402
import vc_state  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    bundle = vc_io.load_demo(seed=0, n_cells=300, n_genes=400)
    table = (bundle.truth or {}).get("perturb_table")
    state = vc_state.run_state(bundle, {"n_hvg": 150, "n_pcs": 15, "n_neighbors": 8,
                                        "cluster": 4, "embedding": "pca"})
    labels = [str(v) for v in table["perturbation"].unique()]
    control = [v for v in labels if v.strip().lower() in ("control", "ctrl", "wt", "nc")]
    check("demo 扰动表含 control 组", bool(control), f"labels={labels}")

    # ---- A. 没有 control → prerequisite，不再用全表均值当伪 control ----
    no_ctrl = table[~table["perturbation"].astype(str).str.strip().str.lower().isin(
        ("control", "ctrl", "wt", "nc", "non-targeting"))].copy()
    refused = False
    detail = ""
    try:
        vc_response.run_response(bundle, state, no_ctrl, {})
    except C.EngineError as error:
        refused = error.code == "prerequisite"
        detail = f"{error.code}: {error.message}"
    check("缺 control → prerequisite", refused, detail)

    # ---- B. 标准化必须在折内拟合（每次调用样本数 = 训练折大小）----
    calls = []
    original = vc_response._standardize_features

    def spy(features):
        calls.append(int(np.asarray(features).shape[0]))
        return original(features)

    vc_response._standardize_features = spy
    try:
        result = vc_response.run_response(bundle, state, table, {})
    finally:
        vc_response._standardize_features = original
    n_pert = int(result["metrics"]["n_pert"])
    check("折内标准化：调用次数 = 折数", len(calls) >= 2, f"calls={calls}")
    check("折内标准化：每次只用训练折", bool(calls) and all(count < n_pert for count in calls),
          f"calls={calls} n_pert={n_pert}")
    check("metrics 标记 in_fold", result["metrics"].get("standardization") == "in_fold",
          f"standardization={result['metrics'].get('standardization')}")

    # ---- C. donor 列 → 按 donor 留出，不再把同 donor 的扰动拆进训练/测试 ----
    donor_of = {}
    for index, label in enumerate(labels):
        if label in control:
            donor_of[label] = "d1"
        else:
            donor_of[label] = f"d{2 + (index % 3)}"
    with_donor = table.copy()
    with_donor["donor"] = with_donor["perturbation"].map(donor_of)
    donor_result = vc_response.run_response(bundle, state, with_donor, {})
    check("donor 分组留出生效", donor_result["metrics"].get("donor_holdout") is True,
          f"donor_holdout={donor_result['metrics'].get('donor_holdout')} cv={donor_result['metrics'].get('cv')}")
    check("cv 方案标记为按组", "group" in str(donor_result["metrics"].get("cv")).lower(),
          f"cv={donor_result['metrics'].get('cv')}")
    folds = donor_result["metrics"].get("fold_groups") or []
    grouped_ok = bool(folds) and all(len(set(fold)) == 1 for fold in folds)
    check("每个测试折只含同一 donor", grouped_ok, f"fold_groups={folds[:4]}")

    # ---- D. 线性基线存在且与均值基线区分 ----
    metrics = result["metrics"]
    check("有均值基线 baseline_r2", "baseline_r2" in metrics, f"keys={sorted(metrics)}")
    check("有线性基线 baseline_linear_r2", "baseline_linear_r2" in metrics,
          f"baseline_linear_r2={metrics.get('baseline_linear_r2')}")

    # ---- E. 无 donor 列时不误报 donor 留出（回归保护）----
    check("无 donor 列 → donor_holdout=False", result["metrics"].get("donor_holdout") is False,
          f"donor_holdout={result['metrics'].get('donor_holdout')}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
