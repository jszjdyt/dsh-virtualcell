#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — Python worker 端到端冒烟测试。

直接以子进程方式拉起 `lib/vc_engine.py`，用 JSON-lines 协议走完整条流水线：
  ping → demo → steps → suggest → qc → state → annotate → grn → validate →
  perturb → screen → transition → insight → audit → plot → export → state → shutdown

用法：
    python3 test/smoke_e2e.py              # 默认在临时目录跑，结束后清理
    python3 test/smoke_e2e.py --keep       # 保留工作目录便于排查
    python3 test/smoke_e2e.py --verbose    # 打印每条响应的摘要

退出码：0 = 全部通过；1 = 有断言失败；2 = 引擎启动失败。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "lib", "vc_engine.py")

RESULTS = []


class Engine:
    def __init__(self, workdir: str, verbose: bool = False):
        self.workdir = workdir
        self.verbose = verbose
        self.seq = 0
        self.proc = subprocess.Popen(
            [sys.executable, "-u", ENGINE, "--workdir", workdir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.events = []

    def request(self, cmd: str, args: dict | None = None, timeout: float = 600.0) -> dict:
        self.seq += 1
        request_id = str(self.seq)
        payload = json.dumps({"id": request_id, "cmd": cmd, "args": args or {}}, ensure_ascii=False)
        started = time.time()
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        while True:
            if time.time() - started > timeout:
                raise TimeoutError(f"{cmd} 超过 {timeout}s")
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read()
                raise RuntimeError(f"引擎退出（{cmd}）：{stderr[-3000:]}")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.events.append({"raw": line[:400]})
                continue
            if message.get("event") == "progress":
                self.events.append(message)
                continue
            if str(message.get("id")) != request_id:
                continue
            if self.verbose:
                elapsed = time.time() - started
                status = "ok" if message.get("ok") else "FAIL"
                summary = ""
                if message.get("ok"):
                    summary = str((message.get("result") or {}).get("summary") or "")[:120]
                else:
                    summary = str((message.get("error") or {}).get("message") or "")[:200]
                print(f"  [{elapsed:6.1f}s] {cmd:12s} {status} {summary}")
            if not message.get("ok"):
                error = message.get("error") or {}
                raise EngineFailure(cmd, error.get("code") or "internal", error.get("message") or "未知错误", error)
            return message.get("result") or {}

    def close(self):
        try:
            self.proc.stdin.write(json.dumps({"id": "shutdown", "cmd": "shutdown", "args": {}}) + "\n")
            self.proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class EngineFailure(Exception):
    def __init__(self, cmd, code, message, payload=None):
        super().__init__(f"{cmd}: [{code}] {message}")
        self.cmd = cmd
        self.code = code
        self.message = message
        self.payload = payload or {}


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(condition), "detail": detail})
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--cells", type=int, default=1200)
    parser.add_argument("--genes", type=int, default=800)
    parsed = parser.parse_args()

    workdir = os.path.abspath(parsed.workdir or tempfile.mkdtemp(prefix="dsh-vc-e2e-"))
    os.makedirs(workdir, exist_ok=True)
    print(f"工作目录：{workdir}")
    print(f"解释器：{sys.version.split()[0]}  ({sys.executable})")
    print("-" * 78)

    engine = Engine(workdir, verbose=parsed.verbose)
    failures = 0
    try:
        # 0. 握手
        ping = engine.request("ping", {}, timeout=120)
        check("ping 握手", bool(ping.get("packages")), f"backend={ping.get('mode')} python={ping.get('platform')}")
        modules = ping.get("modules") or {}
        for name in ("vc_io", "vc_state", "vc_grn", "vc_response", "vc_plot"):
            if not check(f"模块 {name} 可导入", bool(modules.get(name)), json.dumps(ping.get("moduleErrors") or {}, ensure_ascii=False)[:200]):
                failures += 1

        # 1. demo
        demo = engine.request("demo", {"seed": 0, "n_cells": parsed.cells, "n_genes": parsed.genes})
        dataset = demo.get("dataset") or {}
        check("demo 载入", dataset.get("n_cells") == parsed.cells and dataset.get("n_genes") == parsed.genes,
              f"{dataset.get('n_cells')}×{dataset.get('n_genes')}，TF {dataset.get('n_tfs')}，真值 {dataset.get('truth')}")
        ds = dataset.get("id") or "ds1"

        # 2. 元数据
        steps = engine.request("steps", {})
        ids = [item["id"] for item in steps.get("steps") or []]
        check("steps 元数据", ids == ["qc", "state", "annotate", "grn", "validate", "perturb", "screen",
                                      "response", "transition", "insight"], ",".join(ids))
        formats = engine.request("formats", {})
        check("formats", len(formats.get("formats") or []) >= 5, f"{len(formats.get('formats') or [])} 种")

        # 3. 参数推荐
        suggest = engine.request("suggest", {"dataset": ds, "step": "state"})
        rec = {item["key"]: item.get("recommended") for item in suggest.get("params") or []}
        check("suggest(state)", rec.get("n_hvg") and rec.get("n_pcs"), json.dumps(rec, ensure_ascii=False))

        # 4. 流程
        stage_results = {}
        for step, params in (
            ("qc", {}),
            ("state", {}),
            ("annotate", {}),
            ("grn", {"n_boot": 0}),
            ("validate", {}),
            ("perturb", {"gene": "", "mode": "knockout"}),
            ("screen", {"top_n": 5}),
            ("transition", {}),
            ("insight", {}),
        ):
            try:
                result = engine.request("run", {"dataset": ds, "step": step, "params": params})
                stage_results[step] = result
                ok = check(f"run {step}", bool(result.get("summary")), str(result.get("summary"))[:160])
                if not ok:
                    failures += 1
                # 断言每步都有 run 记录
                if not result.get("run"):
                    check(f"run {step} 有 run id", False, "缺少 run")
                    failures += 1
            except EngineFailure as error:
                stage_results[step] = {"error": str(error), "code": error.code}
                check(f"run {step}", False, str(error))
                failures += 1

        # 5. 结果结构断言
        state = stage_results.get("state") or {}
        check("state 有 latent/clusters", bool((state.get("arrays") or {}).get("latent") or state.get("n_clusters")),
              f"n_clusters={state.get('n_clusters')} metrics={json.dumps(state.get('metrics') or {}, ensure_ascii=False)[:160]}")
        annotate = stage_results.get("annotate") or {}
        check("annotate 有标签", bool(annotate.get("cluster_labels") or annotate.get("metrics")),
              f"labels={annotate.get('cluster_labels')}")
        grn = stage_results.get("grn") or {}
        check("grn 有边", bool((grn.get("metrics") or {}).get("n_edges")),
              f"n_edges={(grn.get('metrics') or {}).get('n_edges')}")
        validate = stage_results.get("validate") or {}
        vm = validate.get("metrics") or {}
        check("validate 有 AUC", isinstance(vm.get("grn_auroc"), (int, float)),
              f"AUROC={vm.get('grn_auroc')} verdict={(validate.get('verdict') or {}).get('passed')}")
        perturb = stage_results.get("perturb") or {}
        pm = perturb.get("metrics") or {}
        check("perturb 有受影响基因", bool(perturb.get("affected")), f"n={len(perturb.get('affected') or [])} gene={perturb.get('gene')} target={perturb.get('target_state')}")
        screen = stage_results.get("screen") or {}
        check("screen 有排名", bool(screen.get("ranked")), f"top={[(row.get('gene')) for row in (screen.get('ranked') or [])[:3]]}")
        transition = stage_results.get("transition") or {}
        check("transition 有矩阵", bool((transition.get("arrays") or {}).get("T") or transition.get("metrics")),
              f"metrics={json.dumps(transition.get('metrics') or {}, ensure_ascii=False)[:120]}")
        insight = stage_results.get("insight") or {}
        check("insight 有分节", bool(insight.get("sections")), f"headline={str(insight.get('headline'))[:80]}")

        # 6. 出图（每步自动出 + 手动再出一张）
        figs = []
        for step in ("state", "grn", "perturb", "screen", "transition", "validate"):
            figs.extend((stage_results.get(step) or {}).get("figures") or [])
        check("自动出图", len(figs) >= 6, f"{len(figs)} 张：" + ",".join(sorted({item.get('kind', '') for item in figs})))
        try:
            plotted = engine.request("plot", {"dataset": ds, "kind": "perturb_field"})
            figure = plotted.get("figure") or {}
            check("手动出图 perturb_field", os.path.exists(figure.get("path") or ""), figure.get("path"))
        except EngineFailure as error:
            check("手动出图 perturb_field", False, str(error))
            failures += 1

        # 7. 自检 + 智能分析 API
        audit = engine.request("audit", {"dataset": ds})
        check("audit", isinstance(audit.get("score"), int), f"score={audit.get('score')} items={len(audit.get('items') or [])}")

        # 8. 参数扫描
        try:
            sweep = engine.request("sweep", {"dataset": ds, "step": "state", "param": "n_pcs",
                                             "values": [10, 20]}, timeout=900)
            check("sweep(state.n_pcs)", len(sweep.get("rows") or []) == 2,
                  json.dumps([{k: row.get(k) for k in ("value", "status", "n_clusters", "silhouette")}
                              for row in sweep.get("rows") or []], ensure_ascii=False))
        except EngineFailure as error:
            check("sweep(state.n_pcs)", False, str(error))
            failures += 1

        # 9. 导出
        for what, suffix in (("report", ".md"), ("methods", ".md"), ("protocol", ".json"),
                             ("summary_json", ".json"), ("script", ".py"), ("grn_csv", ".csv")):
            try:
                exported = engine.request("export", {"dataset": ds, "what": what})
                path = exported.get("path") or ""
                ok = os.path.exists(path) and path.endswith(suffix) and os.path.getsize(path) > 0
                check(f"export {what}", ok, f"{os.path.basename(path)} {os.path.getsize(path) if ok else 0} B")
                if not ok:
                    failures += 1
            except EngineFailure as error:
                check(f"export {what}", False, str(error))
                failures += 1

        # 10. runs / state
        runs = engine.request("runs", {"dataset": ds, "limit": 100})
        check("runs 有记录", len(runs.get("runs") or []) >= 8, f"{len(runs.get('runs') or [])} 条")
        state_payload = engine.request("state", {})
        check("state 载荷完整",
              bool(state_payload.get("datasets")) and bool(state_payload.get("results"))
              and bool(state_payload.get("steps")),
              f"results={sorted((state_payload.get('results') or {}).keys())}")

        # 11. worker 重启后数据仍在（持久化）
        engine.close()
        engine = Engine(workdir, verbose=parsed.verbose)
        ping2 = engine.request("ping", {})
        check("重启后握手", bool(ping2.get("packages")))
        state2 = engine.request("state", {})
        check("重启后 state 恢复", bool(state2.get("datasets")),
              f"datasets={len(state2.get('datasets') or [])}")
        restored = False
        try:
            restored = bool(engine.request("runs", {"dataset": ds, "limit": 1}).get("runs"))
        except EngineFailure:
            restored = False
        check("重启后 runs 可读", restored)

    except EngineFailure as error:
        check(f"致命失败：{error.cmd}", False, f"[{error.code}] {error.message}")
        failures += 1
    except Exception as error:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("未预期异常", False, str(error))
        failures += 1
    finally:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass

    print("-" * 78)
    passed = len([item for item in RESULTS if item["ok"]])
    total = len(RESULTS)
    print(f"结果：{passed}/{total} 通过，{total - passed} 失败")
    if total - passed:
        print("失败项：")
        for item in RESULTS:
            if not item["ok"]:
                print(f"  - {item['name']}：{item['detail']}")
        print(f"工作目录保留在：{workdir}")
        return 1
    if parsed.keep:
        print(f"工作目录保留在：{workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
