# 数据准备与下载指引

> 虚拟细胞工作台只需要**一份表达矩阵**就能跑完整条流程；如果能再配上细胞注释、ATAC/GRN 先验或 Perturb-seq 表，结果会更可信。
> 本文件说清：要什么文件、从哪下、怎么导入、哪些坑。

## 一、最小要求（一个文件即可）

**一张「细胞 × 基因」的表达矩阵 + 基因名。**

| 要求 | 说明 |
|---|---|
| 行/列 | 细胞 × 基因（**方向反了也没关系**：导入时可勾「转置」或自动判断） |
| 数值 | 原始 counts 最好（会自动 library-size 归一化 + log1p）；TPM/FPKM 也能用 |
| 基因名 | **必须能对上基因符号**（如 `CD3D`、`MS4A1`），Ensembl ID（`ENSG...`）需要先转成 symbol |
| 规模 | 下限约 500 细胞 × 500 基因；**推荐 2,000–50,000 细胞**。细胞越多，GRN 与扰动估计越稳 |
| 物种 | 参考 marker 面板与 TF 池是人类（内置 260 个 TF + 20 类状态 marker）；小鼠数据可跑，但注释会大量 Unknown |

只有矩阵也能跑：质控 → 状态表征 → 聚类 → 调控网络 → 虚拟扰动 → 筛选 → 状态转移 → 智能分析。
**注意**：没有细胞注释也能聚类，但"状态"只有编号 `0,1,2…`；自动注释依赖内置 marker 面板。

## 二、支持的格式（页面「导入数据」里选，或 auto 自动识别）

| 格式 | 需要哪些文件 | 说明 |
|---|---|---|
| **AnnData `.h5ad`** | 1 个 | 最省事，obs（细胞注释）会一起带进来；推荐优先用它 |
| **10x CellRanger `.h5`** | 1 个 | `filtered_feature_bc_matrix.h5`，自动只取 Gene Expression |
| **10x mtx 三件套** | `matrix.mtx(.gz)` + `barcodes.tsv(.gz)` + `features.tsv(.gz)`（或 `genes.tsv`） | 放同一目录，导入目录即可；缺 sidecar 会自动命名占位 |
| **Loom `.loom`** | 1 个 | GEO 常见 |
| **文本矩阵** | `.csv/.tsv/.txt`（可 `.gz`） | 第一行表头、第一列基因/细胞名；分隔符自动嗅探 |
| **GEO series matrix** | `*_series_matrix.txt.gz` | 自动解析数据表段（bulk 矩阵：每个样本当一个"细胞"，只适合粗粒度推演） |
| **R 的 `.rds`** | 1 个 | 直接读 Seurat v3/v4/v5、SingleCellExperiment、matrix；obs/metadata 会保留（需要本机 Rscript + Seurat） |
| **R 的 `.RData` / `.rda`** | 1 个 | 同上；文件里有多个对象时用 `object` 指定对象名 |
| **压缩包** | `.zip/.tar/.tar.gz` | 里面有一套可识别数据即可（10x 三件套/单个矩阵）；多候选中报错让你用 `member` 指定 |
| **URL 直链** | `http(s)://…` | 下载到本地后按上面规则识别（≤8 GB） |

**可选附件**（不是必需，但对应步骤会用到）：

| 附件 | 用途 | 格式 |
|---|---|---|
| 细胞 metadata（obs） | 着色、分组、按细胞类型比较 | CSV/TSV：第一列细胞名，其余列任意 |
| **Perturb-seq 扰动表** | 「响应预测」步骤 | CSV：至少 `cell_id, perturbation` 两列 + 基因列；或 h5ad 的 `obs['perturbation']` |
| **GRN 先验** | 把网络方向性证据从 `weak` 提到 `moderate` | CSV：`tf,target,sign,weight`（sign=±1，weight 可空） |
| 基因集 / 药物签名 | 签名投影、富集 | CSV：`gene,logFC`；或 GMT |

## 三、下载渠道

### 1) 只想试功能：不用下载
页面顶栏「**载入示例数据**」→ 2,000 细胞 × 1,000 基因、147 个 TF、**自带真值网络与留出扰动**，2 秒可用。

### 1.5) 自己的 R 分析结果（.rds / .RData）

**可以，直接导入，不用转 h5ad。** 工作台内置 R 桥（`lib/vc_rbridge.R`），会调用本机 `Rscript` 读对象：

- 支持 **Seurat v3/v4/v5**（默认取 `DefaultAssay` 的 counts；obs 里的 `seurat_clusters`、`cell_type`、`group`、`percent.mt` 等全部保留）、
  **SingleCellExperiment / SummarizedExperiment**、**matrix / dgCMatrix**、**数值 data.frame**。
- `.RData/.rda` 里有多个对象 → 报 `ambiguous` 并列出候选，用 `object` 指定（页面「导入数据 → 高级选项 → RData 对象名」，或对话里说"用 xxx 对象"）。
- 前提：本机有 `Rscript`（R 4.x）+ `Seurat`/`SeuratObject`/`Matrix`；`Rscript -e 'library(Seurat)'` 能过即可。
  找不到 Rscript 时工具会明确报 `no_r`，不会静默失败。

```text
# 页面：导入数据 → 填 /Users/you/obj.rds（多对象时在高级选项填对象名）
# 对话：用虚拟细胞工作台打开 /Users/you/obj.rds
# 工具：virtualcell action=open path=/Users/you/obj.rds data_format=auto
```

本机实测：把 10x PBMC3k 在 R 里做成 Seurat 对象（2700×32738）`saveRDS` → 直接导入 → 质控 → 状态表征，
结果与直接导 10x 原始文件**完全一致**（2699 细胞 × 13714 基因、5 个状态、轮廓系数 0.35），obs 里的
`orig.ident / nCount_RNA / nFeature_RNA / percent.mt` 都在。

> 如果对象是 **已 scale/已整合** 的（`scale.data`、`integrated` assay），工作台优先取 counts；没有 counts 时会标注
> `value_type=lognorm` 继续跑。建议在 R 里保留 counts（`CreateSeuratObject` 默认就有）。

### 2) 真实单细胞数据（推荐渠道）

| 渠道 | 适合 | 怎么拿 | 体积量级 |
|---|---|---|---|
| **10x Genomics 官网数据集** | 最快验证真实数据；PBMC/组织/多组学 | 打开 <https://www.10xgenomics.com/datasets> → 选数据集 → 下载 **Filtered Feature Matrix**（`.tar.gz` 或 `.h5`） | 10–500 MB |
| **GEO**（NCBI） | 任何已发表 scRNA-seq | 搜 `single cell RNA-seq <组织/疾病>`；在 GSE 页面下 `*_raw.tar` 或 `*_matrix.mtx.gz`；也可用**本机的单细胞工作台**：GEO 检索 → 下载 → 导入 → 导出 h5ad → 再交给虚拟细胞工作台 | 100 MB – 数 GB |
| **CELLxGENE**（CZI） | 现成 `.h5ad`、带细胞类型注释 | <https://cellxgene.cziscience.com> → 数据集页 → Download → **h5ad**（直链形如 `https://datasets.cellxgene.cziscience.com/<uuid>.h5ad`） | 0.5–20 GB |
| **Human Cell Atlas / Tabula Sapiens** | 多组织、多细胞类型图谱 | HCA Data Portal <https://data.humancellatlas.org>；Tabula Sapiens 门户 <https://tabula-sapiens-portal.ds.czbiohub.org>（可下 h5ad） | 数 GB（建议先取单个组织子集） |
| **单细胞工作台（你已装）** | 已下载/已有 rds、Seurat 对象 | `dsh-singlecell` 打开 → 清洗 → 导出 h5ad → 本工作台 `open` | — |

**已验证可用的一条直链（10x PBMC 3k，7.3 MB，2,700 细胞）：**

```bash
curl -L -o /tmp/pbmc3k.tar.gz \
  "https://cf.10xgenomics.com/samples/cell/pbmc3k/pbmc3k_filtered_gene_bc_matrices.tar.gz"
```

然后在页面「导入数据」里填 `/tmp/pbmc3k.tar.gz`（或对话里说"用虚拟细胞工作台打开 /tmp/pbmc3k.tar.gz"）。
本机实测：导入后 2,700 × 16,634 基因 → 质控 → 状态表征 2,699 细胞 × 13,714 基因、5 个状态（轮廓系数 0.35）→ 注释出
**T cell 54% / Monocyte 25% / B cell 13% / NK 7% / Platelet 0.5%**，与 PBMC3k 已知组成一致；GRN 的 hub 是 SPI1/JUNB/CEBPD/ID2/CEBPB。

### 3) 扰动数据（只有想做「响应预测」才需要）

| 数据集 | 内容 | 位置 |
|---|---|---|
| Replogle et al. 2022, genome-wide Perturb-seq | K562 全基因组 CRISPRi 扰动 | GEO **GSE146194**（processed 文件）；也可用 GWPS 门户 <https://gwps.wi.mit.edu> |
| Norman et al. 2019, Perturb-seq | K562 组合扰动（遗传互作） | GEO **GSE133344** |
| 你自己的 Perturb-seq | 最相关 | 整理成 `cell_id, perturbation, 基因列…` 的 CSV，或用 h5ad + `obs['perturbation']` |

> 拿到的是 h5ad 时可直接导入本工作台跑「响应预测」；是 count matrix 时先取 control + 扰动细胞的矩阵，按上面格式整理。
> 实际只需要**至少 3–5 个扰动**才能训练；少于 3 个会如实降级为描述性位移（工具会写明）。

### 4) ATAC / GRN 先验（想让"虚拟敲除"更可信时才需要）

内置 GRN 来自共表达，方向性证据天然是 `weak`。三条提升路径，任选其一：

1. **自己的 scATAC**：用 ArchR/Signac 算 peak-gene 关联 → 取 TF 结合位点覆盖的 peak → 导出 `tf,target,sign,weight` CSV，
   页面「调控网络 → 候选边先验」选「上传的 GRN 先验」。
2. **公共先验**：ChIP-Atlas（<https://chip-atlas.org>）的 Target Genes、或 SCENIC 的 motif 数据库（cisTarget），
   导出成同样的 4 列 CSV。
3. **已有 h5ad 带 ATAC**：多组学对象可先在单细胞工作台里整理出 peak-gene 表，再挂载。

### 5) 药物 / 基因签名（可选）
LINCS/CMap（CLUE 平台 <https://clue.io>）或你自己的差异基因表 → 整理成 `gene,logFC` CSV → 页面「响应预测」里挂载。

## 四、怎么导入（三种方式，效果一致）

1. **页面**：顶栏「导入数据」→ 填路径或选文件 → 可先点「先探测格式」→「导入并载入」。
2. **对话**：
   ```
   用虚拟细胞工作台打开 /tmp/pbmc3k.tar.gz，命名为 pbmc3k
   用虚拟细胞工作台从 URL 导入 https://datasets.cellxgene.cziscience.com/<uuid>.h5ad
   ```
3. **工具调用**：`virtualcell action=open path=… data_format=auto`

导入后建议顺序：`qc → state → annotate → grn → validate(仅 demo) → perturb/screen → transition → insight`，
或直接点顶栏「一键全流程」。

## 五、常见坑（导入失败先看这里）

| 现象 | 原因 | 解决 |
|---|---|---|
| 报"无法识别格式" | 后缀不认识/压缩包里有多个候选 | 用 `inspect` 先探测；压缩包用 `member` 指定相对路径；或手动解压后给具体文件 |
| 报"ambiguous" | 目录/压缩包里有多份数据 | 指定具体文件路径（如 `.../filtered_gene_bc_matrices/hg19/matrix.mtx`） |
| 基因全是 `ENSG…` | 矩阵用 Ensembl ID | 先转成 gene symbol（10x `features.tsv` 第 2 列就是 symbol；GEO 可用 biomaRt/pybiomart） |
| 细胞/基因数比预期少很多 | 全零基因被自动丢掉（正常）；或误读了子集 | 看导入返回的 `dropped` 字段；必要时在单细胞工作台里先做过滤再导出 |
| 注释大量 Unknown | 物种不是人 / 组织没有对应 marker / 阈值太严 | 面板是 20 类常见人细胞状态；组织特殊时把 `min_score` 调低，或上传自定义 marker 面板 |
| 虚拟敲除几乎没位移 | GRN 太弱（共表达噪声、细胞少、基因被过滤） | 提高细胞数、调小 `alpha`、`n_prop` 加到 4–5；或挂 ATAC 先验；先看 `audit` 与 `validate` 再下结论 |
| 文件太大很慢 | >5 万细胞 | 先随机/分层下采样（导入时会自动提示），或先在单细胞工作台里做 QC + HVG 再导出 |
| 导入后没有分组/临床信息 | 文本矩阵没有 obs（R 对象一般都有） | 另给一份 `sample_sheet`（CSV，第一列细胞名 + 分组列），或在页面里按正则自动分组 |
| R 对象报 `no_r` / `ambiguous` | 没装 Rscript；或 `.RData` 里多个对象 | 装 R+Seurat；或页面高级选项/工具里用 `object` 指定对象名 |
| R 对象读进来是空/维度过小 | 对象只有 scale.data（如 SCTransform 后）没有 counts | 在 R 里 `saveRDS(GetAssayData(obj, layer="counts"))` 单独存矩阵，或保留 counts assay |

## 六、最稳的推荐做法

1. 想验证功能：**示例数据**（不用下载）。
2. 想验证真实数据：**10x PBMC 3k**（7 MB，一条 curl 命令）。
3. 想做自己的课题：**CELLxGENE 或 GEO 下载 h5ad**，或**直接给 R 的 `.rds/.RData`** → 本工作台导入（obs 带细胞类型时注释可直接对照）。
4. 想做扰动预测：**再补一份 Perturb-seq 表**（GSE146194 或自己的数据）。
5. 想把"虚拟敲除"讲成论文级证据：**补 GRN 先验（ATAC/ChIP）** + 实验验证，并把 `validity` 与 `caveats` 一起写进讨论。
