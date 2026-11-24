#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-virtualcell — 内置生物学先验。

三块内容：
  1. STATE_MARKERS：细胞状态参考 marker 面板（注释用，20 个状态，每类 6–12 个真实基因）；
  2. HUMAN_TFS：常用人类转录因子（GRN 推断的 TF 候选池，约 200 个真实符号）；
  3. PATHWAY_SETS：内置通路基因集（扰动 Δ 的富集分析用，22 组真实基因）；
  4. DEMO_STATES：合成 demo 的 6 个状态定义（每个状态有主控 TF + marker），
     保证 demo 上"注释 / GRN / 虚拟扰动 / 校验"都能形成闭环。

这些是"先验"，不是结论：GRN 的方向性仅靠共表达无法证明，需要 ATAC 或实验验证，
任何用到这些先验的步骤都必须在结果里写明来源（见 CONTRACT §4.1 validity）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. 细胞状态参考 marker 面板
# ---------------------------------------------------------------------------

STATE_MARKERS: dict[str, list[str]] = {
    "T cell": ["CD3D", "CD3E", "CD3G", "CD2", "CD247", "LCK", "TRAC", "LAT", "ZAP70", "LTB"],
    "CD4 T": ["CD4", "IL7R", "CCR7", "LEF1", "TCF7", "ICOS", "CD40LG", "CD28", "MAL", "TRAT1"],
    "CD8 T": ["CD8A", "CD8B", "GZMK", "CCL5", "GZMH", "CST7", "KLRD1", "EOMES", "PRF1", "NKG7"],
    "Treg": ["FOXP3", "IL2RA", "CTLA4", "IKZF2", "TNFRSF4", "TNFRSF18", "IL10", "LRRC32", "ENTPD1"],
    "NK cell": ["NKG7", "GNLY", "GZMB", "PRF1", "KLRD1", "KLRF1", "NCAM1", "FCGR3A", "TYROBP", "CTSW", "GZMA"],
    "B cell": ["CD79A", "CD79B", "MS4A1", "CD19", "BANK1", "CD37", "CD22", "TNFRSF13C", "CD74", "HLA-DRA"],
    "Plasma cell": ["MZB1", "JCHAIN", "SDC1", "XBP1", "IGHG1", "IGKC", "DERL3", "PRDM1", "TNFRSF17", "SSR4"],
    "Monocyte": ["LYZ", "CD14", "FCN1", "S100A8", "S100A9", "CST3", "VCAN", "CD68", "CSF1R", "FCGR3A"],
    "Macrophage": ["CD68", "CD163", "MRC1", "MSR1", "C1QA", "C1QB", "APOE", "CTSB", "CTSL", "FABP5"],
    "Dendritic cell": ["FCER1A", "CD1C", "CLEC10A", "CD74", "HLA-DPA1", "ITGAX", "LAMP3", "IRF8", "CD1E", "CST3"],
    "Neutrophil": ["FCGR3B", "CSF3R", "CXCR2", "S100A8", "S100A9", "MNDA", "FGR", "IFITM2", "LILRB2", "SLC25A37"],
    "Mast cell": ["TPSAB1", "TPSB2", "CPA3", "MS4A2", "KIT", "HDC", "CMA1", "GATA2", "HPGDS", "SLC18A2"],
    "Platelet": ["PPBP", "PF4", "ITGA2B", "GP9", "TUBB1", "SELP", "NRGN", "GP1BA", "TREML1", "RGS18"],
    "Erythrocyte": ["HBB", "HBA1", "HBA2", "ALAS2", "AHSP", "GYPA", "SLC4A1", "CA1", "BPGM", "EPB42"],
    "Endothelial": ["PECAM1", "VWF", "CLDN5", "KDR", "EMCN", "CDH5", "ESAM", "RAMP2", "ENG", "PLVAP"],
    "Fibroblast": ["COL1A1", "COL1A2", "COL3A1", "DCN", "LUM", "PDGFRB", "FBLN1", "SPARC", "FN1", "MMP2"],
    "Epithelial": ["EPCAM", "KRT18", "KRT8", "KRT19", "KRT7", "CDH1", "CLDN4", "MUC1", "TACSTD2", "SFN"],
    "Proliferating": ["MKI67", "TOP2A", "CCNB1", "CDK1", "BIRC5", "UBE2C", "CENPF", "NUSAP1", "TPX2", "AURKB"],
    "Interferon-stimulated": ["ISG15", "IFIT1", "IFIT3", "MX1", "OAS1", "STAT1", "IRF7", "IFI6", "IFI44L", "RSAD2"],
    "Stress / heat shock": ["HSPA1A", "HSPA1B", "HSP90AA1", "DNAJB1", "HSPH1", "FOS", "JUN", "JUNB", "EGR1", "ATF3"],
}

# ---------------------------------------------------------------------------
# 2. 常用人类转录因子（GRN 的 TF 候选池）
# ---------------------------------------------------------------------------

HUMAN_TFS: list[str] = [
    "AR", "ARNT", "ASCL1", "ATF1", "ATF2", "ATF3", "ATF4", "ATF6", "BACH1", "BACH2",
    "BATF", "BATF3", "BCL3", "BCL6", "BCL11A", "BCL11B", "BHLHE40", "BHLHE41",
    "CEBPA", "CEBPB", "CEBPD", "CEBPE", "CREB1", "CREB3", "CREM", "CUX1", "DBP", "DDIT3",
    "E2F1", "E2F3", "E2F4", "EBF1", "EGR1", "EGR2", "EGR3", "ELF1", "ELF3", "ELF4",
    "ELK1", "ELK4", "ERF", "ERG", "ESR1", "ESRRA", "ESRRB", "ETS1", "ETS2", "ETV1",
    "ETV4", "ETV5", "ETV6", "FOS", "FOSB", "FOSL1", "FOSL2", "FOXA1", "FOXA2", "FOXC1",
    "FOXD1", "FOXF1", "FOXJ1", "FOXK1", "FOXM1", "FOXN1", "FOXO1", "FOXO3", "FOXO4",
    "FOXP1", "FOXP3", "GATA1", "GATA2", "GATA3", "GATA4", "GATA6", "GLI1", "GLI2", "GLI3",
    "GRHL2", "HES1", "HES4", "HES6", "HEY1", "HEYL", "HIC1", "HIF1A", "HIF3A", "HLF",
    "HMGA1", "HNF1A", "HNF4A", "HOXA9", "HOXB4", "HOXC6", "HSF1", "ID1", "ID2", "ID3",
    "ID4", "IKZF1", "IKZF2", "IKZF3", "IRF1", "IRF2", "IRF3", "IRF4", "IRF5", "IRF6",
    "IRF7", "IRF8", "IRF9", "JUN", "JUNB", "JUND", "KLF2", "KLF4", "KLF5", "KLF6",
    "KLF10", "LEF1", "LITAF", "LYL1", "MAF", "MAFB", "MAFF", "MAFK", "MAX", "MEF2A",
    "MEF2C", "MEIS1", "MITF", "MLX", "MXD1", "MYB", "MYBL2", "MYC", "MYCN", "MYOD1",
    "MZF1", "NANOG", "NFAT5", "NFATC1", "NFATC2", "NFE2", "NFE2L1", "NFE2L2", "NFE2L3",
    "NFIB", "NFIC", "NFIL3", "NFKB1", "NFKB2", "NFYA", "NFYB", "NFYC", "NKX2-1",
    "NR0B1", "NR1H3", "NR2F1", "NR2F2", "NR3C1", "NR4A1", "NR4A2", "NR4A3", "NR5A1",
    "ONECUT1", "ONECUT2", "POU2F1", "POU2F2", "POU2F3", "POU3F2", "POU5F1", "PPARA",
    "PPARD", "PPARG", "PRDM1", "PRDM14", "PRDM16", "PRRX1", "RARA", "RARB", "RARG",
    "REL", "RELA", "RELB", "REST", "RFX1", "RFX5", "RORA", "RORB", "RORC", "RUNX1",
    "RUNX2", "RUNX3", "RXRA", "RXRB", "SALL4", "SATB1", "SATB2", "SIX1", "SMAD1",
    "SMAD2", "SMAD3", "SMAD4", "SMAD5", "SMAD7", "SNAI1", "SNAI2", "SOX2", "SOX4",
    "SOX9", "SOX10", "SOX17", "SP1", "SP3", "SPDEF", "SPI1", "SPIB", "SREBF1", "SREBF2",
    "SRF", "STAT1", "STAT2", "STAT3", "STAT4", "STAT5A", "STAT5B", "STAT6", "TAL1",
    "TCF3", "TCF4", "TCF7", "TCF7L1", "TCF7L2", "TEAD1", "TEAD4", "TFAP2A", "TFAP2C",
    "TFDP1", "TFEB", "TFE3", "THRB", "TLX1", "USF1", "USF2", "VDR", "XBP1", "YY1",
    "ZBTB16", "ZBTB17", "ZBTB20", "ZBTB32", "ZBTB33", "ZEB1", "ZEB2", "ZNF143",
    "ZNF281", "ZNF750",
]

# ---------------------------------------------------------------------------
# 3. 内置通路基因集（对扰动 Δ 做 ORA/富集用）
# ---------------------------------------------------------------------------

PATHWAY_SETS: dict[str, list[str]] = {
    "细胞凋亡": ["BAX", "BAK1", "BCL2", "BCL2L1", "CASP3", "CASP8", "CASP9", "APAF1", "BID", "PMAIP1", "BBC3", "TNFRSF10B", "DIABLO", "XIAP"],
    "细胞周期": ["CCNA2", "CCNB1", "CCND1", "CCNE1", "CDK1", "CDK2", "CDK4", "CDK6", "CDKN1A", "CDKN1B", "E2F1", "MKI67", "TOP2A", "AURKB", "PLK1"],
    "DNA 损伤修复": ["ATM", "ATR", "BRCA1", "BRCA2", "RAD51", "CHEK1", "CHEK2", "TP53", "H2AX", "MDC1", "PARP1", "XRCC1", "LIG4", "FANCD2"],
    "上皮-间质转化": ["CDH1", "CDH2", "VIM", "FN1", "SNAI1", "SNAI2", "ZEB1", "ZEB2", "TWIST1", "MMP2", "MMP9", "TGFB1", "COL1A1", "CTNNB1"],
    "炎症反应": ["IL1B", "IL6", "IL8", "TNF", "CXCL8", "CCL2", "PTGS2", "NFKB1", "RELA", "NLRP3", "IL18", "S100A8", "S100A9", "TLR4"],
    "干扰素应答": ["ISG15", "IFIT1", "IFIT3", "MX1", "OAS1", "STAT1", "STAT2", "IRF7", "IRF9", "IFI6", "IFI44L", "RSAD2", "DDX58", "IFIH1"],
    "缺氧应答": ["HIF1A", "VEGFA", "LDHA", "PGK1", "SLC2A1", "ENO1", "HK2", "PDK1", "BNIP3", "ADM", "EGLN1", "ANGPTL4"],
    "氧化应激": ["NFE2L2", "HMOX1", "NQO1", "GCLC", "GCLM", "SOD1", "SOD2", "CAT", "TXN", "TXNRD1", "GPX1", "PRDX1"],
    "TNF-NFKB 信号": ["TNF", "TNFRSF1A", "TRAF2", "RIPK1", "NFKB1", "RELA", "NFKBIA", "IKBKB", "MAP3K7", "BIRC2", "BIRC3", "CXCL8"],
    "JAK-STAT 信号": ["JAK1", "JAK2", "JAK3", "STAT1", "STAT3", "STAT5A", "STAT5B", "STAT6", "SOCS1", "SOCS3", "PIAS1", "IL2RG"],
    "MAPK-ERK 信号": ["MAPK1", "MAPK3", "MAP2K1", "MAP2K2", "RAF1", "BRAF", "KRAS", "NRAS", "DUSP1", "DUSP6", "ELK1", "FOS", "JUN"],
    "PI3K-AKT 信号": ["PIK3CA", "PIK3CB", "AKT1", "AKT2", "PTEN", "MTOR", "RPTOR", "RICTOR", "GSK3B", "FOXO1", "FOXO3", "TSC1", "TSC2"],
    "TGF-beta 信号": ["TGFB1", "TGFBR1", "TGFBR2", "SMAD2", "SMAD3", "SMAD4", "SMAD7", "SKIL", "SERPINE1", "JUNB", "CTGF"],
    "WNT 信号": ["WNT3A", "WNT5A", "FZD1", "FZD5", "LRP5", "LRP6", "CTNNB1", "AXIN1", "APC", "TCF7", "LEF1", "DKK1", "MYC"],
    "NOTCH 信号": ["NOTCH1", "NOTCH2", "NOTCH3", "DLL1", "DLL4", "JAG1", "JAG2", "HES1", "HEY1", "RBPJ", "MAML1", "NRARP"],
    "未折叠蛋白反应": ["HSPA5", "DDIT3", "ATF4", "ATF6", "ERN1", "XBP1", "EDEM1", "HERPUD1", "CALR", "CANX", "HSP90B1"],
    "核糖体生物合成": ["RPL3", "RPL5", "RPL11", "RPL13A", "RPS3", "RPS6", "RPS19", "RPL23A", "NCL", "NPM1", "FBL", "UTP3", "MYC"],
    "糖酵解": ["HK1", "HK2", "GPI", "PFKL", "ALDOA", "GAPDH", "PGK1", "PGAM1", "ENO1", "PKM", "LDHA", "SLC2A1", "SLC2A3"],
    "脂肪酸氧化": ["CPT1A", "CPT2", "ACADM", "ACADVL", "HADHA", "HADHB", "ACAA2", "ECHS1", "PPARA", "PPARD", "UCP2", "FABP3"],
    "抗原提呈": ["HLA-A", "HLA-B", "HLA-C", "HLA-DRA", "HLA-DRB1", "HLA-DPA1", "CD74", "B2M", "TAP1", "TAP2", "PSMB8", "PSMB9", "CIITA"],
    "趋化因子信号": ["CCL2", "CCL3", "CCL4", "CCL5", "CXCL8", "CXCL9", "CXCL10", "CXCR4", "CCR7", "CCR5", "CXCR3", "ACKR1"],
    "补体系统": ["C1QA", "C1QB", "C1QC", "C2", "C3", "C3AR1", "C5AR1", "CFB", "CFD", "CFH", "SERPING1", "CD55", "CD59"],
}

# ---------------------------------------------------------------------------
# 4. demo 的状态定义（每个状态有主控 TF + 真实 marker）
# ---------------------------------------------------------------------------

#: 6 个状态；`master_tfs` 是驱动该状态的 TF（demo 真值 GRN 的核心边来源），
#: `markers` 用真实基因符号，让内置 marker 面板能注释出正确标签。
DEMO_STATES: list[dict] = [
    {
        "name": "T cell",
        "master_tfs": ["TCF7", "LEF1", "RUNX1", "GATA3"],
        "markers": STATE_MARKERS["T cell"][:8] + STATE_MARKERS["CD4 T"][:4],
    },
    {
        "name": "B cell",
        "master_tfs": ["PAX5", "EBF1", "IRF4", "SPIB"],
        "markers": STATE_MARKERS["B cell"][:8] + ["CD74", "HLA-DRA"],
    },
    {
        "name": "Monocyte",
        "master_tfs": ["CEBPB", "SPI1", "KLF4", "JUNB"],
        "markers": STATE_MARKERS["Monocyte"][:8] + ["CD68", "CSF1R"],
    },
    {
        "name": "NK cell",
        "master_tfs": ["TBX21", "EOMES", "STAT4", "IRF2"],
        "markers": STATE_MARKERS["NK cell"][:8] + ["GNLY", "PRF1"],
    },
    {
        "name": "Dendritic cell",
        "master_tfs": ["IRF8", "BATF3", "RELB", "SPI1"],
        "markers": STATE_MARKERS["Dendritic cell"][:8] + ["CD74", "HLA-DPA1"],
    },
    {
        "name": "Proliferating",
        "master_tfs": ["MYC", "E2F1", "FOXM1", "TFDP1"],
        "markers": STATE_MARKERS["Proliferating"][:9],
    },
]

#: demo 里"留出"的 5 个 TF：GRN 拟合时看不见它们，用于校验虚拟扰动（CONTRACT §7.1）。
DEMO_HELDOUT_TFS: list[str] = ["KLF2", "NR4A1", "STAT3", "MAFB", "ZEB1"]

#: demo 的 6 个状态额外表达一些非 marker 基因，保证 GRN 有足够 target

#: 额外常见人类基因（让 demo 的基因池足够大，避免用占位名；均为真实 HGNC symbol）
DEMO_COMMON_GENES: list[str] = [
    "ACTB", "GAPDH", "TUBB", "TUBB4B", "ACTG1", "VIM", "FLNA", "MYH9", "CFL1", "PFN1",
    "EEF1A1", "EEF2", "RPLP0", "RPL13A", "RPS18", "RPS6", "RPL7", "RPL10A", "RPL23", "RPS3A",
    "HSP90AB1", "HSPA8", "HSPB1", "HSPH1", "DNAJA1", "DNAJB6", "BAG3", "HSPA2", "HSPE1", "HSPD1",
    "UBC", "UBB", "UBA52", "PSMA1", "PSMA2", "PSMB1", "PSMB4", "PSMD1", "UBE2D1", "USP7",
    "CTSD", "CTSB", "CTSL", "CTSS", "CTSK", "LAMP1", "LAMP2", "GBA", "HEXB", "NPC1",
    "GAPDH", "PKM", "PGK1", "PGAM1", "ENO1", "ENO2", "ALDOA", "TPI1", "LDHA", "LDHB",
    "HK2", "PFKL", "G6PD", "PGD", "TALDO1", "TKT", "GPI", "BPGM", "PGAM2", "GYS1",
    "ACLY", "ACACA", "FASN", "SCD", "ELOVL5", "FABP4", "FABP5", "CD36", "LPL", "LIPE",
    "CPT1A", "CPT2", "ACADM", "ACADVL", "HADHA", "HADHB", "ACAA2", "ECHS1", "IDH1", "IDH2",
    "SDHA", "SDHB", "SDHC", "SDHD", "FH", "MDH1", "MDH2", "CS", "ACO2", "OGDH",
    "NDUFA1", "NDUFB8", "NDUFS1", "UQCRB", "UQCRC2", "COX4I1", "COX5A", "COX6A1", "ATP5F1A", "ATP5MC1",
    "SLC2A1", "SLC2A3", "SLC3A2", "SLC7A5", "SLC7A11", "SLC16A1", "SLC16A3", "SLC1A5", "SLC38A2", "SLC43A1",
    "ATP1A1", "ATP1B1", "ATP1B3", "ATP2A2", "ATP2B1", "ATP6V0B", "ATP6V1A", "ATP13A3", "SLC9A1", "SLC12A2",
    "FOS", "FOSB", "JUN", "JUNB", "JUND", "EGR1", "EGR2", "ATF3", "ATF4", "DDIT3",
    "IER2", "IER3", "IER5", "DUSP1", "DUSP2", "DUSP5", "DUSP6", "GADD45B", "GADD45G", "TIPARP",
    "NFKB1", "NFKB2", "RELA", "RELB", "NFKBIA", "NFKBIB", "NFKBIZ", "TNFAIP3", "BIRC2", "BIRC3",
    "TNF", "TNFRSF1A", "TNFRSF1B", "LTB", "TRAF1", "TRAF3", "RIPK1", "TANK", "IKBKB", "CHUK",
    "IL1B", "IL1R1", "IL6", "IL6R", "IL6ST", "IL10", "IL10RA", "IL13", "IL4R", "IL17RA",
    "CXCL8", "CXCL1", "CXCL2", "CXCL3", "CXCL5", "CXCL9", "CXCL10", "CXCL11", "CXCL13", "CXCL16",
    "CCL2", "CCL3", "CCL3L1", "CCL4", "CCL4L2", "CCL5", "CCL7", "CCL8", "CCL13", "CCL18",
    "CCL19", "CCL20", "CCL21", "CCL22", "CCR2", "CCR5", "CCR7", "CXCR3", "CXCR4", "CXCR5",
    "CXCR6", "CX3CR1", "ACKR1", "SELL", "SELE", "SELP", "SELPLG", "ITGAL", "ITGAM", "ITGAX",
    "ITGB1", "ITGB2", "ITGB7", "ICAM1", "ICAM2", "ICAM3", "VCAM1", "PECAM1", "CD44", "CD47",
    "STAT1", "STAT2", "STAT3", "STAT4", "STAT5A", "STAT5B", "STAT6", "JAK1", "JAK2", "JAK3",
    "TYK2", "SOCS1", "SOCS2", "SOCS3", "SOCS4", "SOCS5", "PTPN1", "PTPN2", "PTPN6", "PTPN11",
    "PIK3CA", "PIK3CB", "PIK3CD", "PIK3R1", "PIK3R2", "AKT1", "AKT2", "AKT3", "PTEN", "MTOR",
    "RPTOR", "RICTOR", "RHEB", "TSC1", "TSC2", "RPS6KB1", "RPS6KB2", "EIF4EBP1", "GSK3B", "GSK3A",
    "MAPK1", "MAPK3", "MAPK6", "MAPK8", "MAPK9", "MAPK14", "MAP2K1", "MAP2K2", "MAP2K4", "MAP3K8",
    "RAF1", "BRAF", "KRAS", "NRAS", "HRAS", "RRAS2", "RASA1", "RASA2", "DUSP4", "DUSP16",
    "RHOA", "RHOB", "RHOC", "RAC1", "RAC2", "CDC42", "ARF1", "ARF6", "RAB5A", "RAB7A",
    "ACTN1", "ACTN4", "LIMA1", "EZR", "MSN", "RDX", "MARCKS", "GSN", "CAPZB", "TMSB4X",
    "COL1A1", "COL1A2", "COL3A1", "COL4A1", "COL4A2", "COL5A1", "COL6A1", "COL6A2", "COL6A3", "COL18A1",
    "FN1", "FBN1", "FBLN1", "FBLN2", "FBLN5", "LAMA4", "LAMB1", "LAMC1", "THBS1", "THBS2",
    "SPARC", "SPARCL1", "TIMP1", "TIMP2", "TIMP3", "MMP2", "MMP9", "MMP14", "MMP19", "LOXL1",
    "DCN", "LUM", "KERA", "PRELP", "OGN", "ASPN", "FMOD", "MFAP4", "MFAP5", "ELN",
    "PDGFRA", "PDGFRB", "PDGFA", "PDGFB", "PDGFC", "PDGFD", "IGF1", "IGF1R", "IGF2", "IGFBP3",
    "IGFBP4", "IGFBP5", "IGFBP7", "FGF2", "FGFR1", "FGFR2", "FGFR3", "FGF7", "FGF10", "EGF",
    "EGFR", "ERBB2", "ERBB3", "ERBB4", "AREG", "EREG", "TGFA", "HBEGF", "NRG1", "BTC",
    "VEGFA", "VEGFB", "VEGFC", "KDR", "FLT1", "FLT4", "ANGPT1", "ANGPT2", "TEK", "PGF",
    "NOTCH1", "NOTCH2", "NOTCH3", "NOTCH4", "DLL1", "DLL3", "DLL4", "JAG1", "JAG2", "HES1",
    "HEY1", "HEY2", "HEYL", "RBPJ", "MAML1", "MAML2", "NUMB", "NUMBL", "DTX1", "DTX3L",
    "WNT3A", "WNT5A", "WNT5B", "WNT2", "WNT4", "FZD1", "FZD2", "FZD4", "FZD5", "FZD7",
    "LRP5", "LRP6", "CTNNB1", "CTNNA1", "CTNNB1", "AXIN1", "AXIN2", "APC", "DKK1", "DKK3",
    "SFRP1", "SFRP2", "SFRP4", "SOX2", "SOX4", "SOX9", "SOX10", "SOX11", "SOX17", "SOX18",
    "BMP2", "BMP4", "BMP6", "BMP7", "BMPR1A", "BMPR1B", "BMPR2", "ACVR1", "ACVR2A", "ACVR2B",
    "TGFB1", "TGFB2", "TGFB3", "TGFBR1", "TGFBR2", "TGFBR3", "SMAD2", "SMAD3", "SMAD4", "SMAD7",
    "SMAD1", "SMAD5", "SMAD6", "SMAD9", "SKIL", "SKI", "SERPINE1", "SERPINE2", "SERPINB2", "SERPINB9",
    "HLA-A", "HLA-B", "HLA-C", "HLA-E", "HLA-F", "HLA-DRA", "HLA-DRB1", "HLA-DRB5", "HLA-DPA1", "HLA-DPB1",
    "HLA-DQA1", "HLA-DQB1", "HLA-DMA", "HLA-DMB", "HLA-DOA", "HLA-DOB", "B2M", "TAP1", "TAP2", "TAPBP",
    "PSMB8", "PSMB9", "PSMB10", "PSMB5", "PSME1", "PSME2", "CD74", "CIITA", "NLRC5", "ERAP1",
    "CD3D", "CD3E", "CD3G", "CD2", "CD4", "CD8A", "CD8B", "CD5", "CD6", "CD7",
    "CD19", "CD22", "CD24", "CD27", "CD28", "CD34", "CD38", "CD40", "CD48", "CD52",
    "CD53", "CD55", "CD59", "CD63", "CD69", "CD70", "CD79A", "CD79B", "CD80", "CD83",
    "CD86", "CD96", "CD160", "CD163", "CD177", "CD226", "CD244", "CD247", "CD274", "CD276",
    "CD300A", "CD302", "CD320", "CD36", "CD37", "CD300C", "CD300E", "CD300LF", "CD200", "CD200R1",
    "IL2RA", "IL2RB", "IL2RG", "IL7R", "IL21R", "IL23R", "IL27RA", "IL12RB1", "IL12RB2", "IL18R1",
    "IL1RAP", "IL1RL1", "IL4", "IL5", "IL7", "IL12A", "IL12B", "IL15", "IL16", "IL18",
    "IL21", "IL23A", "IL27", "IL32", "IL33", "IL36G", "IL37", "IL2", "IL3", "IL9",
    "IFNG", "IFNGR1", "IFNGR2", "IFNA1", "IFNA2", "IFNB1", "IFNL1", "IFNLR1", "IFNAR1", "IFNAR2",
    "ISG15", "ISG20", "IFIT1", "IFIT2", "IFIT3", "IFITM1", "IFITM2", "IFITM3", "IFI6", "IFI16",
    "IFI27", "IFI30", "IFI35", "IFI44", "IFI44L", "MX1", "MX2", "OAS1", "OAS2", "OAS3",
    "OASL", "RSAD2", "BST2", "DDX58", "DDX60", "IFIH1", "MAVS", "STING1", "TBK1", "IRF3",
    "IRF7", "IRF9", "STAT1", "XAF1", "TRAFD1", "SAMD9", "SAMD9L", "PARP9", "PARP10", "PARP12",
    "TNFSF10", "TNFRSF10A", "TNFRSF10B", "TNFRSF10D", "TNFRSF11A", "TNFRSF13B", "TNFRSF13C", "TNFRSF14", "TNFRSF17", "TNFRSF18",
    "BAX", "BAK1", "BAD", "BID", "BCL2", "BCL2L1", "BCL2L2", "BCL2L11", "MCL1", "BIRC2",
    "BIRC5", "XIAP", "CASP1", "CASP2", "CASP3", "CASP4", "CASP6", "CASP7", "CASP8", "CASP9",
    "CASP10", "APAF1", "CYCS", "DIABLO", "AIFM1", "ENDOG", "DFFA", "DFFB", "PARP1", "PARP2",
    "TP53", "TP53BP1", "CDKN1A", "CDKN1B", "CDKN1C", "CDKN2A", "CDKN2B", "CDKN2D", "MDM2", "MDM4",
    "E2F1", "E2F2", "E2F3", "E2F4", "E2F5", "E2F6", "E2F7", "E2F8", "RB1", "RBL1",
    "CCNA1", "CCNA2", "CCNB1", "CCNB2", "CCND1", "CCND2", "CCND3", "CCNE1", "CCNE2", "CCNF",
    "CDK1", "CDK2", "CDK4", "CDK6", "CDK7", "CDK9", "CDC25A", "CDC25B", "CDC25C", "CDC6",
    "CDC20", "CDC27", "CDC42", "CDCA8", "AURKA", "AURKB", "PLK1", "PLK2", "PLK3", "PLK4",
    "BUB1", "BUB1B", "BUB3", "MAD2L1", "MAD2L2", "NDC80", "NUF2", "SPC24", "SPC25", "NDEL1",
    "MKI67", "TOP2A", "TOP2B", "TOP1", "PCNA", "MCM2", "MCM3", "MCM4", "MCM5", "MCM6",
    "MCM7", "RFC1", "RFC2", "RFC3", "RFC4", "RFC5", "RPA1", "RPA2", "RPA3", "FEN1",
    "LIG1", "LIG3", "LIG4", "XRCC1", "XRCC5", "XRCC6", "PRKDC", "ATM", "ATR", "ATRX",
    "CHEK1", "CHEK2", "BRCA1", "BRCA2", "RAD50", "RAD51", "RAD51B", "RAD51C", "RAD51D", "RAD52",
    "H2AX", "H2AFZ", "H2AFV", "H1-0", "H1-2", "H1-4", "H2BC1", "H3-3A", "H4C1", "H2AC1",
    "HIST1H1C", "HIST1H2AC", "HIST1H3A", "HIST1H4C", "HIST2H2AC", "HIST3H2A", "HIST4H4", "H2BC5", "H3C1", "H4C3",
]

DEMO_EXTRA_TARGETS: list[str] = [
    "MYC", "CCND1", "BCL2", "BAX", "TP53", "CDKN1A", "IL6", "IL1B", "TNF", "CXCL8",
    "VEGFA", "HIF1A", "NFE2L2", "HMOX1", "STAT1", "ISG15", "IFIT1", "MX1", "SOCS3",
    "PIK3CA", "AKT1", "MTOR", "MAPK1", "MAPK3", "FOS", "JUN", "JUNB", "ATF3", "EGR1",
    "CDH1", "VIM", "FN1", "MMP2", "COL1A1", "COL1A2", "DCN", "LUM", "SPARC",
    "MKI67", "TOP2A", "CCNB1", "CDK1", "BIRC5", "UBE2C", "PCNA", "MCM2", "MCM3",
    "HSPA1A", "HSPA1B", "HSP90AA1", "DNAJB1", "HSPH1", "BAG3",
    "XBP1", "DDIT3", "ATF4", "HSPA5", "CALR", "CANX", "DERL1", "SEC61A1",
    "GAPDH", "PGK1", "ENO1", "HK2", "LDHA", "PKM", "SLC2A1", "SLC2A3",
    "HLA-A", "HLA-B", "HLA-C", "B2M", "TAP1", "PSMB8", "PSMB9", "CIITA",
    "CCL2", "CCL3", "CCL4", "CCL5", "CXCL9", "CXCL10", "CCR7", "CXCR4",
    "C1QA", "C1QB", "C3", "CFB", "CFD", "SERPING1",
    "TP63", "SOX4", "SOX9", "KLF6", "ID1", "ID2", "ID3", "SNAI1", "SNAI2", "ZEB1", "ZEB2",
]


def tf_universe() -> list[str]:
    """TF 候选池（去重、排序稳定）。"""
    seen = set(HUMAN_TFS) | set(DEMO_HELDOUT_TFS)
    for state in DEMO_STATES:
        seen.update(state["master_tfs"])
    return sorted(seen)


def marker_panel() -> dict[str, list[str]]:
    return {name: list(genes) for name, genes in STATE_MARKERS.items()}


def pathway_sets() -> dict[str, list[str]]:
    return {name: list(genes) for name, genes in PATHWAY_SETS.items()}
