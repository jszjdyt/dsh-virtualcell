#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 #6 回归：transition 的 stability 与静态 kNN 方向性标注。

修复前 `lib/vc_state.py:run_transition()`：`n_boot=0` 时把 `stability` 记成
`1.0`（伪装成"完美稳定"）；并且把静态 kNN 图直接输出成源/汇/净流"方向"，
容易被读成真实命运方向。修复后：`stability=None` + warnings；结果显式标
`direction_evidence='candidate_hypothesis'`，summary/notes 写明只是候选假说。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_io  # noqa: E402
import vc_state  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    bundle = vc_io.load_demo(seed=0, n_cells=300, n_genes=400)
    state = vc_state.run_state(bundle, {"n_hvg": 150, "n_pcs": 15, "n_neighbors": 8,
                                        "cluster": 4, "embedding": "pca"})

    trans = vc_state.run_transition(bundle, state, {"n_boot": 0})
    metrics = trans["metrics"]
    check("n_boot=0 → stability=None", metrics.get("stability") is None,
          f"stability={metrics.get('stability')!r}")
    check("warnings 说明未做 bootstrap", any(("bootstrap" in w) or ("n_boot" in w) for w in trans["warnings"]),
          f"warnings={trans['warnings']}")
    check("direction_evidence=candidate_hypothesis",
          trans.get("direction_evidence") == "candidate_hypothesis",
          f"direction_evidence={trans.get('direction_evidence')!r}")
    check("validity 说明方向只是假说",
          isinstance(trans.get("validity"), dict)
          and ("假说" in str(trans["validity"]) or "candidate" in str(trans["validity"]).lower()
               or "hypothesis" in str(trans["validity"]).lower()),
          f"validity={trans.get('validity')}")
    check("summary 标注候选方向假说",
          ("候选" in trans["summary"]) or ("假说" in trans["summary"]) or ("hypothesis" in trans["summary"]),
          f"summary={trans['summary'][:160]}")
    check("sources/sinks 仍保留（作为候选）", isinstance(trans.get("sources"), list)
          and isinstance(trans.get("sinks"), list), f"sources={len(trans.get('sources') or [])}")

    # 默认 n_boot>0 时 stability 必须是数值（回归保护，test_state.py 依赖）
    trans2 = vc_state.run_transition(bundle, state, {"n_boot": 20, "seed": 0})
    stability = trans2["metrics"].get("stability")
    check("n_boot>0 时 stability 为 [0,1] 数值", isinstance(stability, (int, float))
          and 0.0 <= float(stability) <= 1.0, f"stability={stability!r}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
