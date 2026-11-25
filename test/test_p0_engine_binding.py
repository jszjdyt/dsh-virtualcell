#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #7/#8 回归：run 记录绑定不可变父 run ID；复现脚本用真实参数 + QC mask。

修复前 `vc_engine.run_step()` 的 `meta.steps[step]` 只有 status/summary，没有 run id
与参数；`_inputs_of()` 只登记 stage npz 哈希，没有 `parentRuns`；
`_script_text()` 用 `_default_params(step)`，既不是实际参数也不把 QC mask 传给
run_state。修复后：step entry 写 `run` + `params`；inputs.json 带 `parentRuns`
（上游每一步的不可变 run id）；脚本嵌入实际参数并在 state 前传入
`cell_mask`/`gene_mask`。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_engine  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="vc-p0-engine-")
    engine = vc_engine.Worker(workdir)
    demo = engine.demo({"n_cells": 160, "n_genes": 220})
    dsid = demo["dataset"]["id"]

    qc = engine.run_step("qc", {"params": {"min_genes": 30, "min_cells": 2}})
    state = engine.run_step("state", {"params": {"n_hvg": 120, "n_pcs": 10,
                                                 "n_neighbors": 5, "cluster": 2,
                                                 "embedding": "pca"}})
    grn = engine.run_step("grn", {"params": {"alpha": 0.1}})

    meta = engine._meta(dsid)
    steps = meta.get("steps") or {}
    qc_entry, state_entry, grn_entry = steps.get("qc") or {}, steps.get("state") or {}, steps.get("grn") or {}

    check("qc step entry 含 run id", str(qc_entry.get("run") or "").startswith("run-"),
          f"run={qc_entry.get('run')!r}")
    check("state step entry 含 run id", str(state_entry.get("run") or "").startswith("run-"),
          f"run={state_entry.get('run')!r}")
    check("state step entry 含实际参数", state_entry.get("params", {}).get("n_hvg") == 120,
          f"params={state_entry.get('params')}")
    check("qc step entry 含实际参数", qc_entry.get("params", {}).get("min_genes") == 30,
          f"min_genes={qc_entry.get('params', {}).get('min_genes')}")
    check("run id 与返回一致", qc_entry.get("run") == qc.get("run")
          and state_entry.get("run") == state.get("run")
          and grn_entry.get("run") == grn.get("run"),
          f"{qc_entry.get('run')}/{qc.get('run')} {state_entry.get('run')}/{state.get('run')}")

    def inputs_of(run_id):
        path = os.path.join(engine._dataset_dir(dsid), "runs", str(run_id), "inputs.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    grn_inputs = inputs_of(grn.get("run"))
    check("grn run 有 inputs.json", grn_inputs is not None, f"run={grn.get('run')}")
    parents = (grn_inputs or {}).get("parentRuns") or {}
    check("parentRuns 绑定 state 父 run", parents.get("state") == state_entry.get("run"),
          f"parentRuns={parents}")
    check("parentRuns 绑定 qc 父 run", parents.get("qc") == qc_entry.get("run"),
          f"parentRuns={parents}")
    state_inputs = inputs_of(state.get("run")) or {}
    check("state 的 parentRuns 只有 qc（不含自己）",
          (state_inputs.get("parentRuns") or {}).get("state") is None
          and (state_inputs.get("parentRuns") or {}).get("qc") == qc_entry.get("run"),
          f"parentRuns={state_inputs.get('parentRuns')}")

    meta = engine._meta(dsid)
    script = engine._script_text(meta)
    check("脚本不再调用 _default_params", "_default_params" not in script,
          f"len={len(script)}")
    check("脚本嵌入实际 state 参数 n_hvg=120", "n_hvg" in script and "120" in script,
          "未找到 n_hvg=120")
    check("脚本嵌入实际 qc 参数 min_genes=30",
          ("'min_genes': 30" in script) or ('"min_genes": 30' in script),
          "未找到 min_genes=30")
    check("脚本把 QC mask 传给 run_state", "cell_mask" in script and "gene_mask" in script,
          "脚本未出现 cell_mask/gene_mask")
    check("脚本先跑 qc 再跑 state", script.find("run_qc") >= 0
          and script.find("run_qc") < script.find("run_state"),
          f"qc@{script.find('run_qc')} state@{script.find('run_state')}")
    check("脚本只包含已运行步骤（无 perturb）", "run_perturb" not in script,
          "不应出现未运行步骤")
    compiled = True
    error_text = ""
    try:
        compile(script, "reproduce.py", "exec")
    except SyntaxError as exc:  # noqa: PERF203
        compiled = False
        error_text = str(exc)
    check("脚本语法合法（可直接执行）", compiled, error_text)

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
