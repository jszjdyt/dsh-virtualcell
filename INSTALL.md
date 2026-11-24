# 安装 dsh-virtualcell

## 前置：Python 依赖

```bash
python3 -c "import numpy, pandas, scipy, sklearn, anndata, h5py, matplotlib; print('OK')"
```

缺包时：

```bash
python3 -m pip install -r requirements.txt      # 在插件目录执行
```

可选增强（不装也能用，只是降级）：`scanpy`（Leiden/UMAP）、`scvi-tools`、`celloracle`、`gears`，
以及基础模型 `scgpt / geneformer / arc-state`（需另下权重）。工作台会在「引擎徽章」里如实显示装了什么。

## 方式一：在 Harness 里安装（推荐）

1. 设置 → 插件 → 安装本地插件包 → 选择本目录；
2. 或让 Agent 调：

```text
plugin_manager: action=install_bundle, target=/Users/you/plugins/dsh-virtualcell
```

3. 重载页面：侧边栏出现「虚拟细胞」，对话里出现 `virtualcell` 工具。

## 方式二：手动加入 profile（高级）

编辑 `~/.dsh/profiles/<profile>/package.json`：

```json
"dependencies": { "dsh-virtualcell": "link:/绝对路径/plugins/dsh-virtualcell" }
```

并在 `dsh.profile.bundles` 追加 `"dsh-virtualcell"`，然后重启宿主。一般不推荐手改。

## 首次使用（3 步）

1. 侧边栏打开「虚拟细胞」→ 顶栏点 **载入示例数据**（2000 细胞 × 1000 基因，自带真值网络与留出扰动，3 秒）。
2. 点 **一键全流程 → 快速体验**：质控 → 状态表征 → 状态注释 → 调控网络 → 网络校验 → 虚拟敲除一个 hub TF → 智能分析。
3. 到「扰动」tab 换基因再敲一次；或到「智能分析」看候选靶点与局限，再导出报告。

对话里也可以直接说：

```text
用虚拟细胞工作台载入示例数据，跑一遍标准流程，然后敲除 MYC 看看细胞状态往哪偏
用虚拟细胞工作台导入 /path/to/data.h5ad，先质控+状态表征，再推断 GRN 并虚拟筛选靶点
```

## 常见问题

**Q：引擎徽章显示「Python 未就绪」？**
点徽章看原因。常见是宿主 PATH 找不到 Python：在插件配置里指定 `python` 绝对路径，或设环境变量
`DSH_VIRTUALCELL_PYTHON=/opt/homebrew/bin/python3`。日志在 `~/.dsh/virtualcell/engine.log`。

**Q：数据/图/记录存在哪？**
默认 `~/.dsh/virtualcell/`（插件配置 `workdir` 可改）：
`datasets/<ds>/raw` 原始数据与 manifest、`datasets/<ds>/runs/run-XXXX/` 每次运行的参数/图/表/日志、
`exports/` 导出的报告与 CSV。**原始数据不会被修改**。

**Q：为什么「网络校验」说没有真值？**
只有示例数据自带真值网络。真实数据可用 `grn` 的 `n_boot>0` 看 Bootstrap 边稳定性，或挂载自己的
GRN 先验（`attach kind=grn_prior`）后把 `prior` 改成 `user`。

**Q：只有 5 个扰动也能量响应预测吗？**
会降级：样本不足时只给描述性位移，并明确提示"不足以训练、仅作参考"。

**Q：虚拟敲除的结论能直接写进论文吗？**
不能。它是模型推演。`insight` 与报告里都会强制附「方法学局限」；请用 ATAC/ChIP 先验或 qPCR/WB/功能实验验证方向。

**Q：和单细胞工作台什么关系？**
`dsh-singlecell` 负责数据清洗与标准单细胞分析；把它的数据导出成 `.h5ad` 后，本工作台可直接导入做虚拟细胞推演。
