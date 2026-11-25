#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GRN 符号回归测试（P0 已修项的守卫）。

`lib/vc_grn.py` 的符号一致性必须来自**带符号的 Ridge 系数**；曾出现的 bug 是
从 `|coef|` 再取 sign，导致恒为 +1，符号一致率退化到「恒猜 +1」基线
（任务单记录的 0.5457 就是这个基线）。本测试用合成真值网络（正负边混合）
断言符号一致率显著高于基线，且正/负边分别都高。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import vc_grn as G  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def load_fixture_module():
    path = os.path.join(HERE, "test_grn.py")
    spec = importlib.util.spec_from_file_location("vc_grn_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    fixture = load_fixture_module()
    bundle, state, A, tfs, targets, truth_grn, truth_perturb = fixture.make_fixture()
    bundle.truth = {"grn": truth_grn, "perturb": truth_perturb,
                    "perturb_meta": {"mode": "knockout", "n_heldout": 3}}
    grn = G.run_grn(bundle, state, {"seed": 0})
    report = G.run_validate(bundle, state, grn, {})
    metrics = report["metrics"]

    agree = metrics.get("sign_agreement")
    baseline = metrics.get("sign_agreement_baseline")
    pos = metrics.get("sign_agreement_positive")
    neg = metrics.get("sign_agreement_negative")
    n_pos = int(metrics.get("sign_agreement_n_positive") or 0)
    n_neg = int(metrics.get("sign_agreement_n_negative") or 0)
    check("真值边含正负两类", n_pos > 0 and n_neg > 0, f"正={n_pos} 负={n_neg}")
    check("符号一致率 ≥ 0.90", agree is not None and float(agree) >= 0.90,
          f"sign_agreement={agree}")
    check("符号一致率高于恒猜 +1 基线", agree is not None and baseline is not None
          and float(agree) > float(baseline) + 0.05,
          f"agree={agree} baseline(+1)={baseline}")
    check("正边符号一致率 ≥ 0.90", pos is not None and float(pos) >= 0.90, f"positive={pos}")
    check("负边符号一致率 ≥ 0.90", neg is not None and float(neg) >= 0.90, f"negative={neg}")

    print()
    print(f"结果：{len(FAIL) == 0}；失败项：{FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
