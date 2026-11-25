# dsh-virtualcell · 工程契约（CONTRACT）

> 本文件是**唯一事实来源**：Host 路由、Agent 工具、Python worker、Client UI 都按这里的形状实现。
> 改形状必须同时改这里 + 两侧实现。v0.1 契约版本：`1`。

## 0. 一句话定位

虚拟细胞（AI Virtual Cell, AIVC）工作台：把单细胞表达谱变成**可被扰动推演的状态空间模型**——
细胞状态表征 → 状态注释 → 基因调控网络（GRN）→ 虚拟敲除/过表达/药物扰动 → 扰动响应预测 →
状态转移/命运 → 虚拟筛选 → 自检与智能分析。每个产物可回溯、可重跑、原始数据不可变。

## 1. 进程与路由

| 项 | 值 |
|---|---|
| 包名 / 插件 id / Client loader id | `dsh-virtualcell` |
| HTTP 前缀 | `/virtualcell` |
| Agent 工具名 | `virtualcell` |
| Client 槽位 | `main`（key=`virtualcell`）、`sidebar.panellist`（id=`virtualcell`, order=5） |
| 默认工作目录 | `~/.dsh/virtualcell`（可用插件配置 `workdir` 或 env `DSH_VIRTUALCELL_WORKDIR` 覆盖） |
| Python 解释器 | 插件配置 `python` > `DSH_VIRTUALCELL_PYTHON` > `DSH_VC_PYTHON` > PATH/框架版/brew 探测 |

路由（Host `lib/impl.mjs` → Python worker cmd）：

| Method + Path | cmd | 说明 |
|---|---|---|
| GET `/health` | `ping` | 引擎存活 + 版本 |
| GET `/state` | `state` | 全量工作台状态 |
| GET `/formats` | `formats` | 支持的输入格式与已安装可选后端 |
| GET `/steps` | `steps` | 流程步骤元数据（含参数 schema + 推荐逻辑） |
| GET `/runs` | `runs` | 运行记录（分页：`?limit=&offset=`） |
| GET `/figure?id=` | — | Host 直接读文件返回 PNG（不经过 worker） |
| POST `/dataset/open` | `open` | `{path\|url, name, data_format, member, transpose, object, sheet, gene_col}` |
| POST `/dataset/demo` | `demo` | `{seed?, n_cells?, n_genes?}` |
| POST `/dataset/upload` | `open` | 原始 body + 头 `x-dsh-filename` |
| POST `/dataset/select` | `select` | `{dataset}` |
| POST `/dataset/close` | `close` | `{dataset}` |
| POST `/inspect` | `inspect` | 只探测不导入 |
| POST `/preview` | `preview` | `{dataset?, n_genes?, n_cells?}` 基因/细胞列预览 |
| POST `/suggest` | `suggest` | `{step, params?}` 参数推荐（参数辅助调整核心） |
| POST `/run` | `run` | `{step, params?}` 运行流程步骤 |
| POST `/sweep` | `sweep` | `{step, param, values?, params?}` 参数扫描 |
| POST `/plot` | `plot` | `{kind, params?}` |
| POST `/attach` | `attach` | `{kind, path, name?, ...}` 挂载扰动表/基因集/GRN 先验/药物签名 |
| POST `/export` | `export` | `{what, out_path?}` |
| POST `/audit` | `audit` | 确定性自检 |
| POST `/insight` | `insight` | 智能分析（结构化结论 + 叙述） |
| POST `/recipe` | `recipe` | 预设流程定义 |
| POST `/recipe/run` | `recipe_run` | 跑预设流程 |
| POST `/reset` | `reset` | 清空工作台 |
| POST `/engine/restart` | — | Host 侧重启 worker |

响应信封：成功 `{ok:true, ...payload}`；失败 HTTP 4xx/5xx + `{ok:false, code, error, details?}`。
错误码：`bad_request` `not_found` `prerequisite` `unsupported` `ambiguous` `timeout` `busy`
`no_python` `engine_exited` `internal` `disposed`。

## 2. Python worker 协议

`python -u lib/vc_engine.py --workdir <abs>`，stdin/stdout 各一行 JSON：

```jsonc
// 请求
{"id":"r1","cmd":"run","args":{"step":"state","params":{}}}
// 进度（可选，stdout）
{"id":"r1","event":"progress","message":"计算 PCA…","pct":40}
// 成功
{"id":"r1","ok":true,"result":{...}}
// 失败
{"id":"r1","ok":false,"error":{"code":"prerequisite","message":"请先运行 state","details":{}}}
```

stdout 只允许协议行，日志走 stderr。所有 result 必须可 JSON 化（numpy → list/标量）。

## 3. 数据模型

### 3.1 工作台状态（`state.json` + `/state`）

```jsonc
{
  "ok": true,
  "version": "0.1.0",
  "engine": {
    "python": "/usr/bin/python3",
    "pythonVersion": "3.14.2",
    "backend": "builtin",              // builtin | scanpy | scvi
    "packages": {"numpy":"2.5.1","scanpy":null,"torch":null,"scvi":null,"celloracle":null,"gears":null},
    "adapters": [                        // 可选模型/后端（本机是否可用，可用则说明能力）
      {"id":"scanpy","available":false,"label":"scanpy","capability":"Leiden/UMAP","hint":"pip install scanpy"},
      {"id":"scvi","available":false,"label":"scVI","capability":"概率隐空间/差异","hint":"pip install scvi-tools"},
      {"id":"celloracle","available":false,"label":"CellOracle","capability":"官方 GRN 扰动","hint":"pip install celloracle"},
      {"id":"gears","available":false,"label":"GEARS","capability":"扰动响应预测","hint":"pip install gears"},
      {"id":"scgpt","available":false,"label":"scGPT","capability":"单细胞基础模型嵌入","hint":"pip install scgpt + 权重"},
      {"id":"geneformer","available":false,"label":"Geneformer","capability":"基础模型 in-silico 扰动","hint":"pip install geneformer + 权重"},
      {"id":"state","available":false,"label":"State (Arc)","capability":"ST/SE 扰动响应","hint":"pip install arc-state + 权重"}
    ],
    "workdir": "/Users/you/.dsh/virtualcell",
    "startedAt": "ISO"
  },
  "datasets": [Dataset],
  "active": "ds1",
  "runs": [{"id":"run-0007","dataset":"ds1","step":"perturb","label":"KO:KLF4","status":"ok","startedAt":"ISO","durationMs":1200,"figures":2,"tables":1}],
  "busy": null                      // 或 {"step":"grn","pct":30}
}
```

### 3.2 Dataset

```jsonc
{
  "id": "ds1", "name": "demo-virtualcell",
  "n_cells": 2000, "n_genes": 1200, "n_tfs": 180,
  "source": "demo|path|url|upload",
  "sha256": "…", "bytes": 1234567, "rawCopy": true,
  "rawPath": "~/.dsh/virtualcell/datasets/ds1/raw/demo.json",
  "createdAt": "ISO",
  "steps": {"qc": {"status":"ok","at":"ISO","ms":120}, "state": {...}},
  "stepOrder": ["qc","state"],
  "warnings": ["…"],
  "attachments": [{"id":"at1","kind":"perturb_table","name":"replogle.csv","path":"…","sha256":"…"}],
  "truth": {"has_grn_truth": true, "has_perturb_truth": true}   // demo 才有
}
```

`/state` 里 dataset 不内联大表；steps 只放摘要（`summary` 字段）。

### 3.3 Run 记录（原始数据保留）

每个步骤/出图/导出都产生一个 run：

```
<workdir>/datasets/<dsid>/runs/run-0007/
  params.json        # 入参（含默认值展开）
  result.json        # 返回给 UI 的 result（去掉大数组）
  env.json           # python/包版本/种子/后端/耗时/输入 sha256
  log.txt            # 文本日志
  tables/*.csv       # 所有表（UTF-8，含表头）
  figures/*.png      # 所有图（200 dpi）
  inputs.json        # 本次用到的上游产物（state.npz/grn.npz 的 sha256）
```

- 目录只增不改；`runs/index.json` 维护 `[{id, step, label, status, startedAt, durationMs, files:[...]}]`。
- `/export what=run_zip&run=run-0007` 可打包单次运行。
- 原始输入：`datasets/<dsid>/raw/` 保存副本 + `manifest.json{sha256,bytes,mtime,source,format,copy}`；
  文件 > 2 GiB 时 `copy:false` 只记路径与 sha256（不复制，避免占空间），并在 UI 标注。

## 4. 流程步骤（STEPS，顺序即左栏卡片顺序）

| # | id | 标题 | requires | 关键参数（默认） |
|---|---|---|---|---|
| 1 | `qc` | 质控 | — | `min_genes=200,max_genes=0,max_mito=20,min_cells=3` |
| 2 | `state` | 状态表征 | qc | `n_hvg=1500,n_pcs=30,latent='pca',n_neighbors=15,cluster='auto',resolution=0.8,n_clusters=0,embedding='auto'` |
| 3 | `annotate` | 状态注释 | state | `panel='builtin',cluster_key='clusters',min_score=0.3,column='cell_state'` |
| 4 | `grn` | 调控网络 | state | `tf_list='builtin',method='ridge',alpha=0.1,min_cells=30,prior='coexpression',top_targets=2000,n_boot=0` |
| 5 | `validate` | GRN 校验 | grn | `truth='auto',min_edges=20`（仅在 demo 或挂了先验时有真值） |
| 6 | `perturb` | 虚拟扰动 | grn | `gene='',mode='knockout',strength=1.0,cluster='all',n_prop=3,top_n=30` |
| 7 | `screen` | 虚拟筛选 | grn | `mode='knockout',top_n=20,cluster='all',specificity=true` |
| 8 | `response` | 响应预测 | state | `data='auto',method='additive',nfold=5,latent_dim=0`（无扰动表时降级为签名投影） |
| 9 | `transition` | 状态转移 | state | `method='knn',temperature=0.1,k=10` |
| 10 | `insight` | 智能分析 | state | `depth='standard'` |

每步返回统一形状：

```jsonc
{
  "ok": true,
  "step": "state",
  "run": "run-0003",
  "summary": "2,000 细胞 → 6 个状态；PC1–PC30 解释 41.2% 方差；轮廓系数 0.38",
  "metrics": {"n_cells":2000,"n_clusters":6,"silhouette":0.38,"variance_explained":0.412},
  "tables": [{"id":"cluster_sizes","title":"状态规模","path":"…/tables/cluster_sizes.csv","rows":6,"preview":[["clusters","n_cells"],["0",312]]}],
  "figures": [{"id":"fig-1","kind":"state_embedding","title":"细胞状态空间","url":"/virtualcell/figure?id=…png","path":"…","width":1200,"height":900}],
  "warnings": ["…"],
  "suggest": {"next": "annotate", "why": "…", "params": {"…": "…"}}
}
```

### 4.0 步骤元数据（`GET /steps`，UI 左栏卡片用它渲染）

```jsonc
{"ok":true,"steps":[
  {"id":"state","title":"状态表征","icon":"state","requires":["qc"],
   "why":"把表达谱压到低维状态空间，得到每个细胞的状态向量与相似细胞图；后续所有扰动都在这个空间里推演。",
   "cost":"约 5–30 s","outputs":["latent","clusters","embedding"],
   "params":[
     {"key":"n_hvg","label":"高变基因数","type":"number","default":1500,"min":200,"max":5000,"step":100,
      "effect":"过少丢失状态信号，过多引入噪声","risk":"low","advanced":false},
     {"key":"latent","label":"隐空间方法","type":"select","default":"pca",
      "options":[{"value":"pca","label":"PCA（推荐）"},{"value":"nmf","label":"NMF"}],"risk":"low"}
   ]}
]}
```
参数 `type`：`number|select|boolean|column|text|genes|multiselect`；`advanced=true` 的收进"高级参数"折叠区。

### 4.3 UI 需要的最小状态字段（`GET /state`）

除 §3.1 外，UI 还需要：
```jsonc
{"stepsDone":["qc","state"],"suggestedStep":"annotate",
 "results":{"state":{"summary":"…","metrics":{…},"figures":[…],"tables":[…],"at":"ISO"},
            "grn":{…},"perturb":{…},"screen":{…},"response":{…},"transition":{…},
            "audit":{…},"insight":{…}},
 "attachments":[…], "lastRun":"run-0009"}
```
`results` 放**最近一次**每步的 result 摘要（大数组不进来），供 tab 直接渲染。

### 4.4 预设流程（`/recipe` + `/recipe/run`）

- `quick`：demo → qc → state → annotate → grn → validate（有真值时）→ perturb（第一个 hub TF）→ insight
- `standard`：quick + screen + transition + audit
- `full`：standard + response（有扰动表时）+ export report

返回 `{ok:true, recipe:{id,name,steps:[{step,params}],desc}, ...}`；`/recipe/run` 逐步执行并汇总
`{ok:true, done:[…], failed:[{step,code,error}], runIds:[…], insight:{…}}`。

### 4.1 `perturb` 返回（核心）

```jsonc
{
  "ok": true, "step": "perturb", "run": "run-0009",
  "summary": "KO KLF4：预测细胞从 S1/S2 向 S4 偏移（Δ 0.42），下游 MYC/CCND1 下调，凋亡通路激活",
  "params": {"gene":"KLF4","mode":"knockout","strength":1.0,"cluster":"all","n_prop":3},
  "metrics": {"n_affected": 87, "max_abs_delta": 1.83, "state_shift": 0.42,
              "target_state": "S4", "source_state":"S1", "confidence":"medium", "backend":"builtin"},
  "affected": [{"gene":"MYC","delta":-0.82,"rank":1,"is_tf":true}, …],           // 最多 200 行
  "per_cluster": [{"cluster":"0","delta_norm":0.42,"target_cluster":"3","cos":0.81}, …],
  "figures": [...],
  "tables": [...],
  "validity": {"grn_source":"coexpression_ridge","n_edges_used":3120,"propagation":3,
               "notes":["GRN 来自共表达先验，非 ATAC；方向性证据弱"]}
}
```

### 4.2 参数推荐（`/suggest` + `run` 的 `dry`）

```jsonc
{
  "ok": true, "step": "state",
  "params": [
    {"key":"n_hvg","label":"高变基因数","type":"number","default":1500,"recommended":2000,
     "range":[500,5000],"why":"细胞数 2000、基因数 1200：HVG 上限受基因数限制，建议 1200→取 min",
     "effect":"过少丢失状态信号，过多引入噪声","risk":"low"},
    {"key":"n_pcs","label":"主成分数","type":"number","default":30,"recommended":25,"range":[10,100],
     "why":"前 25 个 PC 累计解释 85% 方差","effect":"…","risk":"medium"}
  ],
  "notes": ["当前数据为 demo 合成数据，推荐值同时用于校验流程正确性"]
}
```

## 5. 支持的输入

| 格式 | 判定 | 说明 |
|---|---|---|
| `.h5ad`（含 `.gz`? 不支持 gz） | anndata | AnnData，自动取 X / raw.X，obs 全保留 |
| 10x `.h5` | h5py 手工解析 | `filtered_feature_bc_matrix.h5` |
| 10x mtx 目录/单 `.mtx(.gz)` | 三件套 | matrix + barcodes + features/genes |
| `.loom` | h5py 手工解析 | GEO 常见 |
| `.csv/.tsv/.txt(.gz)` | 分隔符嗅探 + 方向判断 | 表达矩阵（细胞×基因 或 基因×细胞，`transpose` 可强制） |
| `.zip/.tar(.gz)` | 解包后递归嗅探 | 找 10x 三件套或唯一数据文件 |
| GEO series matrix | `*_series_matrix.txt(.gz)` | 兼容老数据 |
| URL | http(s) | 下载后按上表识别（≤8 GB） |
| R 单细胞对象 | `.rds` / `.RData` / `.rda` | Seurat v3–v5 / SingleCellExperiment / matrix / data.frame；由 `lib/vc_rbridge.R` 调本机 Rscript 导出中立文件；多对象用 `object` 指定（`ambiguous` + candidates） |
| 扰动表 `attach kind=perturb_table` | CSV/TSV/H5AD | 列：`cell_id,perturbation[,gene columns…]` 或 h5ad `obs['perturbation']` |
| 基因集 `kind=gene_set` | CSV/GMT/TXT | 用于签名投影/富集 |
| GRN 先验 `kind=grn_prior` | CSV（`tf,target[,sign,weight]`） | 有先验时 `prior='user'`，方向性证据更强 |
| 药物签名 `kind=drug_signature` | CSV（`gene,logFC`） | 投影到状态空间，算 connectivity |

## 6. 输出（`/export`）

`what` ∈ `state_csv, clusters_csv, markers_csv, grn_csv, perturb_csv, screen_csv, response_csv,
transition_csv, summary_json, protocol, methods, report, script, run_zip`。
- `report`：Markdown 智能分析报告（中文，含方法、结果、图、局限）。
- `methods`：英文 Methods 段（可贴论文）。
- `script`：可复现 Python 脚本（读取工作目录产物 + 重算关键步骤）。
- 全部写 `<workdir>/exports/`，返回绝对路径。

## 6.5 统一返回与中间结构（所有步骤函数必须遵守）

步骤函数（`run_*`）返回 **纯 dict**，由 `vc_engine` 负责落盘、出图、写 run 记录：

```python
{
  "arrays": {...},            # 大数组（numpy/scipy），engine 存 npz 后从 result.json 剥离
  "tables": [                 # 表 → engine 写 tables/<id>.csv
      {"id":"cluster_sizes", "title":"状态规模", "rows": pandas.DataFrame | list[dict]}
  ],
  "figures": [                # 图 → engine 调 vc_plot.plot(kind, ctx, path, params)
      {"kind":"state_embedding", "title":"细胞状态空间", "params":{}}
  ],
  "metrics": {...},           # 关键数字（JSON 标量），UI/Agent 直接读
  "summary":  "一句话中文结论（必须含本次真实数字）",
  "warnings": ["…"],          # 降级、样本量不足、方法学风险
  "notes":    ["…"]           # 补充说明（可选）
}
```

`vc_engine` 组装的出图 ctx（各步骤据此取用，不得假设字段一定存在）：

```python
ctx = {
  "bundle": Bundle,           # 仅含 genes/cells/obs/truth（大矩阵不放进来）
  "state":  state_arrays,     # {'arrays':…, 'hvg':…, 'genes':…, 'latent_loadings':…, 'cluster_centroids':…, 'embedding_loadings':…, 'cluster_labels':…}
  "grn":    grn_arrays,       # {'arrays':{'coef','tf_idx','tg_idx'}, 'tf_names':…, 'target_names':…, 'cluster_names':…, 'edges':…}
  "perturb":perturb_arrays,   # {'arrays':{'delta_gene','delta_latent','arrows'}, 'gene':…, 'affected':…, 'per_cluster':…}
  "screen": screen_arrays,
  "response": response_arrays,
  "transition": transition_arrays,
  "result":   本次步骤返回的完整 dict（含 arrays）,
  "title": str,
}
```

### 6.5.1 `state` 步骤必须产出的字段

```python
{
 "arrays": {"X_log": csr(n_cells×n_genes), "latent": (n_cells×n_pcs), "embedding": (n_cells×2),
            "clusters": (n_cells,) int, "knn_idx": (n_cells×k) int, "knn_dist": (n_cells×k)},
 "genes": [str],              # X_log 列顺序（全基因）
 "hvg": [str],                # 高变基因名（latent_loadings 的行顺序）
 "latent_loadings": (n_hvg×n_pcs),      # knn 空间载荷（HVG 表达 → PC）
 "cluster_centroids": (n_clusters×n_pcs),
 "embedding_loadings": (n_pcs×2),       # PC → 2D 线性映射（扰动箭头用）
 "cluster_labels": [str],               # 每簇名称（"0","1",… 或注释后的名字）
 "obs_columns": {"clusters": [...]},    # 可直接着色的 obs 列
 "backend": "builtin|scanpy",
 "n_clusters": int, "n_pcs": int, "metrics": {...}
}
```

### 6.5.2 `grn` 步骤必须产出的字段

```python
{
 "arrays": {"coef": (n_clusters×n_tf×n_target) float32, "tf_idx": int[], "tg_idx": int[]},
 "tf_names": [str], "target_names": [str], "cluster_names": [str],   # n_clusters 与 coef 第 0 维一致
 "edges": [{"tf","target","score","sign","cluster"}],   # 最多 5000 行，按 |score| 排序
 "metrics": {"n_edges","n_tfs","n_targets","density","hub_tfs":[...],"cv_r2"},
 "grn_source": "coexpression_ridge|user_prior|mixed",
 "validity": {"directional_evidence":"weak|moderate|strong", "notes":[...]}
}
```

### 6.5.3 `perturb` / `screen` / `transition` 的数组字段

```python
# perturb
{"arrays": {"delta_gene": (n_genes,), "delta_latent": (n_cells×n_pcs) 或 (n_clusters×n_pcs),
            "arrows": (n_clusters×2)},
 "gene": str, "mode": str, "strength": float,
 "affected": [{"gene","delta","rank","is_tf"}],      # ≤200 行
 "per_cluster": [{"cluster","delta_norm","target_cluster","cos"}],
 "state_shift": float, "target_state": str, "source_state": str}

# screen
{"arrays": {"scores": (n_tf,), "tf_names": [str]},
 "ranked": [{"gene","score","state_shift","specificity","target_state","why"}]}

# transition
{"arrays": {"T": (k×k)}, "sources": [...], "sinks": [...], "net_flow": [[i,j,float]],
 "metrics": {"stability": float, "n_states": k}}

# response
{"arrays": {"pred": (n_pert×n_latent), "true": (n_pert×n_latent), "per_gene_err": (n_genes,)},
 "metrics": {"r2":…, "spearman":…, "n_pert":…, "method":…},
 "per_pert": [{"perturbation","r2","top_shift_genes":[...]}]}
```

## 7. Python 模块边界（**严格按此实现，禁止跨模块改签名**）

```
lib/vc_common.py     # 我写：EngineError/_err/jsonable/进度/文件/画图登记/RunStore 工具
lib/vc_priors.py     # 我写：内置状态 marker 面板、人类 TF 列表、通路基因集、demo 真值说明
lib/vc_io.py         # A：输入解析 + demo 生成（含真值）
lib/vc_state.py      # B：qc / state / annotate / transition
lib/vc_grn.py        # C：grn / validate / perturb / screen
lib/vc_response.py   # E：response（扰动响应预测）+ 签名投影
lib/vc_plot.py       # D：全部图
lib/vc_engine.py     # 我写：worker + cmd dispatch + runs/audit/insight/suggest/recipe/export
```

### 7.1 `vc_io.py`

```python
class Bundle:                     # dataclass；数值一律 numpy/scipy
    X            # scipy.sparse.csr_matrix float32, n_cells × n_genes（原始 counts 或已标准化由 meta 标注）
    genes: list[str]
    cells: list[str]
    obs: pandas.DataFrame         # 可为空 DataFrame（index=cells）
    meta: dict                    # {format, source, transpose, value_type:'counts'|'lognorm'|'unknown', n_note}
    truth: dict | None            # demo: {'grn': DataFrame[tf,target,sign,weight],
                                  #        'programs': {state: {'up':[...],'down':[...]}},
                                  #        'perturb': {tf: np.ndarray(n_genes)},
                                  #        'labels': np.ndarray(str), 'perturb_table': DataFrame}

def sniff(path: str, *, data_format='auto', member=None, transpose=False) -> dict
def inspect_path(path, *, data_format='auto', member=None, transpose=False, object=None,
                 sheet=None, gene_col=None, max_cells=40000) -> dict
def load_dataset(path, *, data_format='auto', member=None, transpose=False, object=None,
                 sheet=None, gene_col=None, max_cells=40000, n_cells=None, n_genes=None) -> Bundle
def load_demo(seed=0, n_cells=2000, n_genes=1200) -> Bundle
def write_h5ad(bundle, path) -> None          # 便于与 scanpy/单细胞工作台互通
def sha256_of(path) -> str
```

`load_demo` 硬要求（决定 validate 是否有意义）：
- 约 2000 细胞 × 1200 基因，6 个状态（用 `vc_priors.STATE_MARKERS` 里的真实 marker 名字着色），
  其中 180 个是 `vc_priors.HUMAN_TFS` 里的真实 TF。
- 真值 GRN：每个 target 由 1–3 个 TF 调控（符号 ±），TF 间少量互调；状态特异权重。
- 表达 = 基线 + 状态程序 + GRN 传播项 + 负二项/泊松噪声 + 丢弃；固定 seed 完全可复现。
- `truth['perturb']`：对 5 个保留 TF（不参与 GRN 拟合的"留出扰动"）用真实 A 矩阵解析传播出 Δ（与模拟器无关）。
- `truth['perturb_table']`：control + 5 个 TF 的敲低 Perturb-seq 样表（细胞×基因，含噪声）。
- 生成 ≤ 5 s，内存 ≤ 300 MB。

### 7.2 `vc_state.py`

```python
# 输入统一是 Bundle + 上游 stage dict；返回结果既含 ndarray 也含可 JSON 部分
def run_qc(bundle, params: dict) -> dict
def run_state(bundle, params: dict) -> dict      # 含 'arrays': {...} 大数组（engine 落 npz 后剥离）
def run_annotate(bundle, state: dict, params: dict) -> dict
def run_transition(bundle, state: dict, params: dict) -> dict
```
- `run_state` 必须产出：`arrays={'X_log':csr,'latent':(n,pcs),'embedding':(n,2),'clusters':(n,),
  'knn_idx':(n,k),'knn_dist':(n,k)}`、`hvg: list[str]`、`metrics`、`tables`、
  `cluster_centroids`（latent 空间）、`embedding_loadings`（latent→2D 线性映射，供扰动箭头用）。
- 自动参数：`n_hvg=min(param, n_genes)`；`n_pcs` 自动 ≤ min(n_hvg, n_cells)-1；
  k 自动；`cluster='auto'` 时 silhouette 扫 k∈[2..12] 取最优（细胞 >3000 用子采样）。
- 可选后端：scanpy 在则用 Leiden/UMAP（`backend='scanpy'`），否则 kmeans+sklearn t-SNE/PCA。
  任何降级都要写进 `warnings` 且 `metrics.backend` 标注。
- `run_transition`：kNN 转移矩阵（行归一）+ Sinkhorn 平滑（numpy），返回 `T`（k×k）、
  `sources/sinks`、`net_flow`、稳定性（bootstrap 重采样 ARI）。
- `run_annotate`：对 `vc_priors.STATE_MARKERS` 打分（均值 z-score / UCell 风格秩和），
  给 top1/top2 + margin + 证据基因 + 每簇表；score 低时保留 `Unknown` 并 warning。

### 7.3 `vc_grn.py`

```python
def run_grn(bundle, state: dict, params: dict) -> dict
def run_validate(bundle, state: dict, grn: dict, params: dict) -> dict
def run_perturb(bundle, state: dict, grn: dict, params: dict) -> dict
def run_screen(bundle, state: dict, grn: dict, params: dict) -> dict
```
- `run_grn`：候选对 = 共表达先验（每 TF 取 |Spearman| top-k，k 由 `top_targets` 均摊）∪ 用户先验；
  每簇（或全局）ridge：`X_target ~ X_tf_candidates`，标准化 + 交叉验证选 alpha（`alpha='auto'` 时），
  系数作为有向权重；输出 `arrays={'coef': (n_clusters, n_tf, n_target) 稀疏/稠密, 'tf_idx', 'target_idx'}`、
  `edges` 表、`metrics{hub_tfs, density, n_edges, cv_r2}`、`warnings`。
- `run_validate`：demo 真值时算 GRN AUROC/AUPRC（edge 存在性）与留出扰动 Δ 的
  Spearman/Pearson（模拟 vs 真值，5 个留出 TF），返回 `metrics`、`figures`（ROC/散点）、
  `verdict{passed:bool, why:str}`。无真值时 `code='prerequisite'` 并给替代建议（用 bootstrap 稳定性）。
- `run_perturb`：CellOracle 风格——
  1) 初始 Δ：`mode=knockout` → `Δx_tf = -x_tf`；`knockdown` → `-strength·x_tf`；`overexpression` → `+strength·(max-x_tf)`；
  2) 沿 GRN 迭代传播 `n_prop` 轮（`Δ ← Δ + coefᵀ·Δ_tf` 按簇），限幅；
  3) 投到 latent：`Δ_latent = Δ_X_hvg · loadingsᵀ`（用 state 的 PCA 载荷）；
  4) 每簇平均 Δ_latent → 与各簇质心余弦 → `state_shift`、`target_state`；
  5) 表格：受影响基因、每簇 Δ 范数、下游通路富集（用 priors 自带基因集）。
- `run_screen`：对候选 TF（默认内置 TF 列表 ∩ 表达基因，或指定 `genes`）批量模拟，
  打分 = `|state_shift| × specificity`（只影响单一方向的加分），返回排名表 + top 图数据。

### 7.4 `vc_response.py`

```python
def run_response(bundle, state: dict, table, params: dict) -> dict
def project_signature(bundle, state: dict, signature: dict, params: dict) -> dict
```
- `table` 为挂载的扰动表（DataFrame）。`method='additive'`：在 latent 空间学
  `Δ_latent[pert] ≈ Σ_gene w_gene·x_gene(control)`（岭回归 / 每扰动均值位移），
  留一扰动交叉验证；输出到真实/预测散点、每基因误差、未见扰动预测。
  数据不足（<3 扰动）时优雅降级：只做均值位移 + 报告 `unsupported` 提示。
- 无扰动表时 `run_response` 自动走 `project_signature`：把药物/扰动签名（gene,logFC）
  投到 latent 方向，算与各簇质心的余弦 + connectivity score。

### 7.5 `vc_plot.py`

```python
def plot(kind: str, ctx: dict, out_path: str, params: dict | None = None) -> dict
# ctx = {bundle?, state?, grn?, perturb?, result?, title?}
# 返回 {'file':out_path,'width':px,'height':px,'title':str,'notes':[...]}
```
kinds：`qc_violin state_embedding state_embedding_perturb cluster_bar grn_hub_bar grn_network
grn_heatmap perturb_waterfall perturb_field perturb_heatmap screen_rank response_scatter
transition_matrix transition_graph validate_roc compare_bar`
- 统一风格：`matplotlib Agg`、dpi=200、中文字体回退（PingFang SC / Heiti SC / Arial Unicode MS /
  Noto Sans CJK）、去顶部/右侧框线、cividis/viridis 或自定义 `PALETTE`、图注写进 `notes`。
- 每张图 ≤ 1200×900 px，≤ 400 KB。

## 7.6 实现口径补充（v0.1 落地后追加，均已在代码里生效）

1. **GRN 候选边 = 全局共表达 ∪ 簇内共表达**：只取一种会系统性漏掉另一类真实调控
   （状态程序驱动 ↔ 状态内细胞间调控）。`max_candidates_per_tf` 默认 60，簇内再取一半。
2. **网络校验的 AUROC/AUPRC 只在候选边空间内计算**（`arrays.candidate_mask`），并在 warnings 写明口径；
   全空间 AUROC 会被大量 0 分负样本的 tie 拉回 0.5，属于评估口径问题而非模型问题。
   旧 stage 没有 candidate_mask 时会退回全空间并在 warnings 说明。
3. **`/preview` 额外返回交互散点坐标**：`embedding:[{x,y,cluster}]`（默认最多 4000 点）、
   `clusters`、`cell_state`（按细胞）；`/state` 保持轻量（每 1.5 s 轮询）。坐标算法默认 t-SNE，
   线性嵌入（PCA）时扰动箭头严格成立，t-SNE 时箭头是"簇中心 + 线性近似"，图注会说明。
4. **`perturb` 的 `gene` 留空 = 自动选 hub TF**（按出度与总权重排序），结果里写 `params.gene` 与 `summary`，
   便于 Agent 直接引用；指定的基因不在表达矩阵里时返回 `bad_request` 并提示用 `preview` 查基因名。
5. **Agent 工具的 `recipe_run` 是一个独立 action**（与 `recipe` 分开：`recipe` 只看定义，`recipe_run` 执行）。
6. **出图顺序**：步骤跑完先把 stage 落盘/入内存，再渲染该步骤的图（否则本步自己的图会画成"缺数据"占位图）。
   `run_step` 的 postprocess 顺序固定为：指标/摘要 → stage 落盘 → 表格 → 出图 → 更新 stage json → run 记录。
7. **样式**：`vc_plot` 的标题统一为「业务名（括号内写细节）」，不重复名词；CJK 字体链
   PingFang SC → Hiragino Sans GB → … → DejaVu Sans。

## 8. Client UI 契约

- 布局：顶栏（引擎徽章 / 导入 / demo / 刷新）+ 三栏（左 220–260 px 流程卡；中 tab 内容；右 300 px Agent 面板/日志）。
- 左栏：数据集卡 + 10 张步骤卡（可展开参数表单、显示推荐值徽章与"为什么"、运行/重跑、依赖未满足时禁用并提示）。
- 中栏 tabs：`状态图`（canvas 散点：按 cluster/基因/注释着色，扰动箭头叠加，hover tooltip，缩放/拖拽/复位）、
  `网络`（GRN hub 柱 + 边表）、`扰动`（受影响基因瀑布 + 每簇 Δ + 热图）、`筛选`（排名表 + 柱状）、
  `响应`（预测 vs 真实散点）、`转移`（矩阵热图 + 流向）、`智能分析`（insight 卡片 + audit 分数）、
  `记录`（runs 列表：查看参数/图/表/导出/重跑）。
- 右栏：当前结果摘要 + 推荐下一步 + 可复制的 Agent 指令 + 运行日志尾部。
- 所有数据走 `/virtualcell/*`；轮询 `/state`（1.5 s）更新进度；`/figure` 图片直接用 `<img>`。
- 纯函数层（颜色/格式化/视口变换/响应归一化）放文件顶部并挂 `globalThis.__VC_TEST__` 供单测。

## 9. 自检（`/audit`）与智能分析（`/insight`）

- audit 检查项（0–100 分）：输入完整性/sha256、细胞数与基因数下限、线粒体比例、重复细胞、
  每簇细胞数（<10 警告）、GRN 边数与符号平衡、扰动稳定性（bootstrap，若 n_boot>0）、
  留出校验（若有真值）、参数是否在推荐范围、产物是否齐全。
- insight 输出：
  ```jsonc
  {"headline":"…","sections":[{"title":"细胞状态","bullets":["…"]},
     {"title":"调控结构","bullets":["…"]},{"title":"扰动与靶点","bullets":["…"]},
     {"title":"方法学局限","bullets":["…"]}],
   "ranked_targets":[{"gene":"KLF4","score":0.82,"why":"…"}],
   "next_actions":[{"step":"screen","why":"…"}],
   "caveats":["GRN 为共表达先验，方向性需 ATAC 或实验验证"]}
  ```
- 结论只引用本次运行的真实数字；不得编造。低置信度必须写进 caveats。
