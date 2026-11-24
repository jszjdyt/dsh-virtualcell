# dsh-virtualcell · 设计说明（DESIGN）

> 一句话：**把单细胞表达谱变成一个可以在电脑里“做实验”的模型**——改一个基因，看细胞状态往哪边走。
> 本文件说明「为什么这么设计」「每一步用什么方法」「哪些话不能乱说」。

## 1. 背景：虚拟细胞是什么，为什么现在能做

2024–2026 年，虚拟细胞（AI Virtual Cell / AIVC）从概念走向可执行的工作流。两条主线：

1. **单细胞基础模型**（scGPT、Geneformer、scFoundation、Arc Institute 的 State/SE-ST 等）：
   在海量单细胞上预训练，把细胞压成表示，然后在表示空间里做 in-silico 扰动。
2. **调控网络 + 动力学模拟**（CellOracle、SCENIC+、GEARS 等）：
   不依赖大模型权重，而是先推断基因调控网络（GRN），再沿网络做扰动传播，看状态位移。

中文生信社区（生信技能树、华哥生信等）对虚拟细胞的拆解基本是同一套骨架：
**状态表征 → 调控网络 → 虚拟扰动（KO/OE/药物）→ 响应预测 → 状态转移/命运 → 虚拟筛选**，
并且反复强调一句泼冷水的话：**模型给的只是假说，方向性必须用 ATAC/ChIP 或实验验证**。
本插件按这个骨架实现，同时把"冷水"写进每个结果（`validity` / `caveats` / audit）。

## 2. 目标与边界

### 3 个必须做到

| 要求 | 实现 |
|---|---|
| **输入文件** | h5ad / 10x h5 / 10x mtx 三件套 / loom / 文本矩阵 / GEO series matrix / 压缩包 / URL；另外可挂载扰动表、基因集、GRN 先验、药物签名 |
| **参数辅助调整** | 每步每个参数都有：默认值、**基于当前数据维度的推荐值**、推荐理由、效应方向、风险等级；外加 `sweep` 参数扫描 + 最优值挑选 |
| **结果出图 + 原始数据保留** | 每步自动出 PNG（200 dpi，16 种图）；输入立即登记 sha256 并复制/登记原始文件；每次运行产生不可变 `runs/run-XXXX/`（params/result/env/log/tables/figures/inputs）；可导出 protocol/methods/report/script/run_zip |
| **智能分析** | `insight`：结构化结论（状态/调控/扰动/局限）+ 候选靶点排名 + 下一步；`audit`：0–100 分确定性自检 |

### 明确不做（v0.1）

- **不训练/不下发基础模型权重**：scGPT/Geneformer/State 只做适配器探测；本机没装就如实标注"未安装 + 安装提示"，不偷偷下载。
- **不宣称因果**：共表达推断的 GRN 方向只是假说；有用户先验（ATAC/ChIP）时自动升级证据等级。
- **不做实验结论**：任何"靶点"都写成候选，并要求实验验证（写进报告与 caveats）。
- **不碰空间转录组/多组学联合**：留给后续版本（CONTRACT 已预留 `attach` 扩展位）。

## 3. 流程与算法

```
qc → state → annotate → grn → validate → perturb → screen → response → transition → insight
```

### 3.1 `qc` 质控
每细胞：检出基因数、总 counts、线粒体占比（`MT-` 基因）；每基因：表达细胞数。
阈值过滤后返回掩码。**不修改原始矩阵**，只记 mask（可回溯"过滤掉了哪些细胞"）。

### 3.2 `state` 状态表征
1. 库大小归一化（target sum=1e4）→ log1p
2. HVG：seurat 风格（均值分箱后按标准化离散度取 top-N；`n_hvg` 自动 ≤ 基因数-1）
3. z-score（clip ±10）→ PCA（随机 SVD）
4. kNN 图（NearestNeighbors）
5. 聚类：有 scanpy 用 Leiden；没有则 k-means + kNN 两轮多数投票平滑，`cluster='auto'` 时按 silhouette 扫 k∈[2..12]
6. 2D 嵌入：scanpy 在→UMAP；否则 t-SNE（大样本降级 PCA 前两维并 warning）
7. 标记基因：每簇 vs 其余，logFC + 表达比例 + Wilcoxon + BH

**为什么先做状态空间**：虚拟扰动的最小可靠单位是"状态"而不是"单个细胞"。所有扰动结果都投影回状态质心，
才能回答"细胞会往哪边走"。

### 3.3 `grn` 调控网络（CellOracle 风格）
1. **候选边先验**：TF 池（内置 ~225 个真实人类 TF，可换用户列表）× 靶基因；共表达 |Spearman| 每 TF 取 top-k
   （或用户上传的 `tf,target,sign,weight` 先验）。
2. **每状态 Ridge**：`X_target ~ X_TF(candidates)`，系数 = 有向权重；细胞不足的簇退化为全局模型。
3. 稀疏化（|coef|≥0.02 或每靶 top3），输出边表、hub TF、CV R²、方向性证据等级。

### 3.3.1 为什么 demo 能自证（v0.1 实测）

合成数据的关键设计：**TF 的 mRNA 必须能反映自身活性**（加自调控项），否则任何共表达方法都不可能恢复真值网络；
TF 活性在状态内连续波动（而不是"状态开关"），这样"按簇拟合"才能看到细胞级信号；
候选边取"全局 ∪ 簇内"两种共表达。默认参数（2000 细胞 × 1000 基因、147 TF）实测：
边 AUROC **0.985**、AUPRC 0.958、留出扰动 Δ 的 top200 Spearman **0.81**、Pearson 0.84、判定**通过**。
这些数字只说明"流程在信号明确的合成基准上自洽"，真实数据请以 `audit` 与 `validity` 为准。

### 3.4 `validate` 网络校验（demo 数据）
demo 数据自带真值网络与 5 个"留出" TF 的解析扰动 Δ。
- 边存在性：AUROC / AUPRC / top-k 精准率
- 扰动方向：模拟 Δ vs 真值 Δ 的 Spearman / Pearson / top20 Jaccard
- 判定：AUROC ≥ 0.65 且 Δ Spearman ≥ 0.3 才算通过；不通过给出调参建议。

> 这是本工作台"可自证"的部分：没有真值的真实数据上，用 Bootstrap 边稳定性替代，并如实降级。

### 3.5 `perturb` 虚拟扰动
1. 初始 Δ：KO → `Δx=-x_tf`；knockdown → `-strength·x_tf`；OE → `+strength·(p95-x_tf)`
2. 网络传播 `n_prop` 轮：`Δ ← Δ + coefᵀ·Δ_tf`（每状态独立，逐轮限幅）
3. 投到隐空间：`Δ_latent = Δ_X,hvg · latent_loadings`
4. 状态位移：新质心与各状态质心余弦相似度 → `target_state`、`state_shift`
5. 下游解释：受影响基因排名 + 通路 ORA（内置 22 组通路基因集）

> 这就是"虚拟敲除"的数学形式。它的可信度 **完全取决于 GRN**——所以 `validity` 必须随结果一起返回。

### 3.6 `screen` 虚拟筛选
对候选 TF 批量做轻量扰动（只算状态质心位移），打分
`score = state_shift × (1 + specificity)`，specificity = `1 - entropy(簇位移分布)/log(k)`。
含义：**能把细胞推得远、且推得专一的 TF 更值得做实验**。

### 3.7 `response` 扰动响应预测
有 Perturb-seq 样表（`cell_id, perturbation, genes…`）时：在隐空间学
`Δ_latent ≈ W·x_control`（Ridge，留一/多折验证），与"所有扰动平均位移"基线比较 R²/Spearman。
没有扰动表时退化为药物/签名投影：把 `gene,logFC` 签名投到隐空间，算与各状态质心的余弦 + 通路富集。

### 3.8 `transition` 状态转移
kNN 簇间边数 → 行归一 → 平滑（temperature）→ Sinkhorn 行列归一 → 转移矩阵；
输出源/汇状态、net flow、bootstrap 稳定性。给"命运方向"一个可量化、可反驳的估计。

### 3.9 `insight` 智能分析
只引用本次运行的真实数字，按「细胞状态 / 调控结构 / 扰动与候选靶点 / 方法学局限 / 下一步」分节；
候选靶点排名合并：hub 出度、扰动位移、筛选得分；局限清单强制包含 GRN 方向性、状态分离度、
细胞数、留出校验结论。

## 4. 参数辅助调整（用户明确要的能力）

三条线：

1. **运行前推荐**（`/suggest`）：按 `n_cells / n_genes / n_perturbations` 等实际维度给每个参数推荐值与理由，
   UI 显示"推荐 2000"徽章，一键填入；`risk` 分三级提示。
2. **运行中提示**（`warnings`）：过滤比例过高、簇太小、GRN 边太少、扰动不稳定等，都带"怎么调"。
3. **运行后扫描**（`/sweep`）：对某一参数取值网格重跑，比较主指标（state→silhouette、grn→CV R²、
   validate→AUROC、transition→stability），挑最优值。扫描不改动已保存的 stage。

## 5. 原始数据保留与可复现

- **输入**：`datasets/<ds>/raw/manifest.json` 记录 sha256、字节数、来源、格式；≤2 GiB 时复制副本，
  更大时只登记路径 + 哈希（不复制，避免占盘），UI 标注 `copy:false`。
- **每步**：`datasets/<ds>/runs/run-0007/{params,result,env,inputs}.json + log.txt + tables/*.csv + figures/*.png`，
  只增不改；`runs/index.json` 是索引。
- **环境**：每次运行记录 Python 版本、包版本、后端（builtin/scanpy）、耗时、种子。
- **导出**：`protocol`（完整参数与运行链）、`methods`（英文 Methods 段）、`report`（中文报告含图与局限）、
  `script`（可复现 Python）、`run_zip`（单次运行打包）。

## 6. 可选后端与降级策略

| 后端/模型 | 装了会怎样 | 没装 |
|---|---|---|
| scanpy | 用 Leiden/UMAP/HVG，`backend='scanpy'` | 内置 k-means + t-SNE/PCA，warnings 标注 |
| scvi-tools | （v0.2 起）用 scVI 隐空间 | 不出现 |
| CellOracle | （v0.2 起）官方实现作对照 | 内置 CellOracle 风格实现 |
| GEARS / scGPT / Geneformer / State | 适配器探测 + 需要权重 | `/state` 显示"未安装 + 安装提示"，功能不缺失 |

原则：**功能不因缺可选包而消失，只降级并标注。**

## 7. 参考与致谢

- Arc Institute, **State** / Virtual Cell Challenge（2025–2026）：扰动响应预测的评测范式（留出扰动 + Turing test 思路）。
- Kamimoto et al., **CellOracle**（Nature 2023）：GRN + 扰动传播 + 状态位移的分析框架。
- Roohani et al., **GEARS**（Nat Biotech 2023）：扰动响应的基因级预测与评测方式。
- scGPT / Geneformer / scFoundation：单细胞基础模型路线（本插件只做适配器位）。
- Replogle et al. Perturb-seq 图谱：扰动表格式参考。
- 生信技能树等中文社区对 AIVC 的拆解（状态表征→GRN→虚拟扰动→响应预测→筛选），以及其一贯的方法学提醒。

## 8. 已知局限（写在前面，比写在讨论里更诚实）

1. **GRN 方向性**：共表达只能给相关性；没有 ATAC/ChIP 时 `directional_evidence='weak'`。
2. **线性外推**：传播模型是线性近似，强扰动/非线性阈值效应会低估或错判。
3. **状态定义依赖参数**：`n_pcs`/分辨率/簇数会改变"状态"的数量与边界，扰动方向也随之变化——所以提供 sweep 与多分辨率对比。
4. **单细胞稀疏性**：低表达基因的 Δ 不稳定；受影响基因排名对 dropout 敏感。
5. **响应预测样本量**：扰动数 < 5 时无法训练，只能给描述性结果（会明确降级）。
6. **不做批次校正的因果推断**：跨样本批次效应可能被误认为状态差异，需要在质控/状态步自行检查（audit 有提示）。
