# CHANGELOG

## v0.1.0（首个版本）

### 新增
- **Host 插件 + 整页工作台**：`/virtualcell` HTTP 路由、`virtualcell` Agent 工具、prompt section、
  侧边栏入口与整页 UI（左流程卡 / 中图表 tab / 右 Agent 面板）。
- **输入**：h5ad、10x `.h5`、10x mtx 三件套、loom、文本矩阵（csv/tsv/txt，可 gz）、GEO series matrix、
  zip/tar 压缩包、URL、**R 的 `.rds`/`.RData`/`.rda`**（Seurat v3–v5 / SingleCellExperiment / matrix / data.frame，
  内置 R 桥 `lib/vc_rbridge.R`，obs 保留；多对象用 `object` 指定）；原始数据登记 sha256 并保留副本（>2 GiB 只登记路径）。
- **10 个步骤**：qc / state / annotate / grn / validate / perturb / screen / response / transition / insight。
- **虚拟扰动**：knockout / knockdown / overexpression；沿 GRN 迭代传播；投影到状态空间；
  每簇位移箭头、目标状态、受影响基因排名、通路 ORA。
- **虚拟筛选**：批量轻量扰动 + 位移×专一性打分，输出候选靶点排名。
- **响应预测**：Perturb-seq 样表 → 隐空间加性模型（留一/多折），与均值基线对比；无扰动表时降级为签名投影。
- **状态转移**：kNN + Sinkhorn 转移矩阵、源/汇状态、稳定性。
- **网络校验**：demo 真值网络的 AUROC/AUPRC/top-k 精准率 + 留出扰动 Δ 相关性 + 通过判定。
- **参数辅助调整**：每参数推荐值 + 理由 + 风险等级（`/suggest`）；参数扫描与最优值（`/sweep`）。
- **出图**：16 种出版级 PNG（state_embedding / grn_network / perturb_waterfall / perturb_field /
  screen_rank / response_scatter / transition_matrix / validate_roc …）。
- **原始数据与运行留存**：不可变 `runs/run-XXXX/`（params/result/env/inputs/log/tables/figures）+
  `runs/index.json`；导出 `report / methods / protocol / script / summary_json / *.csv / run_zip`。
- **智能分析与自检**：`insight`（分节结论 + 候选靶点 + 下一步 + 强制局限）、`audit`（0–100 确定性自检）。
- **预设流程**：quick / standard / full 一键跑。
- **数据指引**：`DATA_GUIDE.md`（要什么文件、10x/GEO/CELLxGENE/R 对象/扰动表怎么拿、常见坑）。
- **示例数据**：内置合成数据（6 状态、TFs 驱动、真值 GRN、5 个留出扰动），可完全离线验证整条链。
- **可选后端探测**：scanpy / scVI / CellOracle / GEARS / scGPT / Geneformer / State，未安装时如实标注并降级。
