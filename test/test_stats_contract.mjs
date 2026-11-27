/**
 * 工作台统计口径契约测试（node:test）——dsh-virtualcell
 *
 * 口径卡的职责是「把这次状态表征/网络/扰动用了什么参数」说清楚，且**取不到就写 null**。
 * 虚拟细胞的产物全部是模型推演：契约里 evidence_kind 必须是 model_prediction，
 * 且 model_prediction 的 caveat 必须始终存在——这是本工作台的统计底线。
 *
 * 运行：node --test test/test_stats_contract.mjs
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { buildWorkbenchContract, PLUGIN_VERSION, EVIDENCE_KIND } from '../lib/impl.mjs'

const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))

function step(overrides = {}) {
  return { status: 'ok', at: '2026-11-27T10:00:00Z', ms: 1200, summary: '', metrics: {}, params: {}, ...overrides }
}

function baseDataset(overrides = {}) {
  return {
    id: 'ds_demo',
    name: 'demo-virtualcell',
    n_cells: 2000,
    n_genes: 1200,
    n_tfs: 96,
    source: 'demo',
    sha256: 'abc123def456',
    bytes: 1048576,
    createdAt: '2026-11-27T09:00:00Z',
    demo: true,
    steps: {
      qc: step({ summary: '质控：2000 细胞 → 1900', metrics: { n_cells_before: 2000, n_cells_after: 1900 }, params: { min_genes: 200 } }),
      state: step({
        summary: '5 个状态；silhouette 0.32',
        metrics: { n_hvg: 1500, n_pcs: 30, n_clusters: 5, silhouette: 0.32, variance_explained: 0.55, backend: 'builtin' },
        params: { n_hvg: 1500, n_pcs: 30, cluster: 'auto', resolution: 0.8, seed: 0 },
      }),
      annotate: step({
        summary: '5/5 状态匹配参考面板',
        metrics: { n_states: 8, assigned: 5, unknown: 0, method: 'ucell' },
        params: { panel: 'builtin', method: 'ucell' },
      }),
      grn: step({
        summary: '推断出 320 条有向边',
        metrics: { n_edges: 320, n_tfs: 60, n_targets: 800, alpha: 0.1, prior: 'coexpression', cv_r2: 0.18 },
        params: { method: 'ridge', alpha: 0.1, prior: 'coexpression', top_targets: 2000 },
      }),
      validate: step({ summary: 'GRN 校验通过', metrics: { auroc: 0.71 }, params: {} }),
      perturb: step({
        summary: '敲除 KLF4：S1 → S3',
        metrics: {
          gene: 'KLF4', mode: 'knockout', state_shift: 0.21, source_state: 'S1',
          target_state: 'S3', confidence: 'medium', n_affected: 37,
        },
        params: { gene: 'KLF4', mode: 'knockout', strength: 1.0, cluster: 'all', n_prop: 3 },
      }),
    },
    stepOrder: ['qc', 'state', 'annotate', 'grn', 'validate', 'perturb'],
    truth: { has_grn_truth: false, has_perturb_truth: false, has_perturb_table: false },
    ...overrides,
  }
}

test('口径 1) 齐全：状态/网络/扰动参数真实回传，evidence 标 model_prediction', () => {
  const contract = buildWorkbenchContract(baseDataset())
  assert.equal(contract.schema, 'dsh-workbench.stats_contract/1')
  assert.equal(contract.plugin, 'dsh-virtualcell')
  assert.equal(contract.plugin_version, PLUGIN_VERSION)
  assert.equal(contract.plugin_version, pkg.version, 'PLUGIN_VERSION 必须与 package.json 一致')
  assert.equal(contract.evidence_kind, 'model_prediction')
  assert.equal(contract.evidence_kind, EVIDENCE_KIND)
  assert.equal(contract.dataset.n_cells, 2000)
  assert.equal(contract.dataset.n_genes, 1200)
  assert.equal(contract.dataset.n_tfs, 96)
  assert.equal(contract.design.n_clusters, 5)
  assert.equal(contract.design.n_hvg, 1500)
  assert.equal(contract.design.n_pcs, 30)
  assert.equal(contract.design.silhouette, 0.32)
  assert.equal(contract.design.n_grn_edges, 320)
  assert.equal(contract.design.n_modeled_tfs, 60)
  assert.equal(contract.design.n_reference_states, 8)
  assert.equal(contract.design.n_assigned_states, 5)
  // 最近一步 = perturb，参数逐字来自引擎
  assert.equal(contract.inference.last_step, 'perturb')
  assert.equal(contract.inference.params.gene, 'KLF4')
  assert.equal(contract.inference.params.strength, 1)
  // 三个建模步骤的真实参数
  assert.equal(contract.modeling.state.n_hvg, 1500)
  assert.equal(contract.modeling.state.n_clusters, 5)
  assert.equal(contract.modeling.grn.alpha, 0.1)
  assert.equal(contract.modeling.grn.prior, 'coexpression')
  assert.equal(contract.modeling.grn.directional_evidence, 'weak')
  assert.equal(contract.modeling.perturb.gene, 'KLF4')
  assert.equal(contract.modeling.perturb.mode, 'knockout')
  assert.equal(contract.modeling.perturb.strength, 1)
  assert.equal(contract.modeling.perturb.state_shift, 0.21)
  assert.equal(contract.modeling.perturb.target_state, 'S3')
  // 模型推演 caveat 必须存在，其余 warn 不应误报
  const prediction = contract.caveats.find((item) => item.code === 'model_prediction')
  assert.ok(prediction, '必须存在 model_prediction caveat')
  assert.equal(prediction.level, 'warn')
  assert.match(prediction.text, /model_prediction/)
  const warnCodes = contract.caveats.filter((item) => item.level === 'warn').map((item) => item.code)
  assert.deepEqual(warnCodes, ['model_prediction'], '除模型推演外不应有别的 warn')
  assert.equal(contract.ok_to_cite, false, '模型推演永远不能直接当实验结论引用')
  assert.doesNotThrow(() => JSON.stringify(contract), '契约必须可 JSON 序列化')
})

test('口径 2) 数据太少 / 状态分离度低 / 无网络 → caveats 逐条点名，模型推演 caveat 仍在', () => {
  const contract = buildWorkbenchContract(baseDataset({
    n_cells: 300,
    sha256: '',
    demo: true,
    truth: { has_grn_truth: true, has_perturb_truth: false, has_perturb_table: false },
    steps: {
      qc: step({ summary: '质控', metrics: { n_cells_after: 300 }, params: {} }),
      state: step({
        summary: '3 个状态；silhouette 0.05',
        metrics: { n_hvg: 1500, n_pcs: 30, n_clusters: 3, silhouette: 0.05, variance_explained: 0.3, backend: 'builtin' },
        params: { n_hvg: 1500, n_pcs: 30 },
      }),
    },
    stepOrder: ['qc', 'state'],
  }))
  const codes = contract.caveats.map((item) => item.code)
  assert.ok(codes.includes('model_prediction'), '即使没有扰动也要标 model_prediction')
  assert.ok(codes.includes('few_cells'))
  assert.ok(codes.includes('low_state_separation'))
  assert.ok(codes.includes('no_grn'))
  assert.ok(codes.includes('no_annotation'))
  assert.ok(codes.includes('demo_truth'))
  assert.ok(codes.includes('no_fingerprint'))
  assert.equal(contract.ok_to_cite, false)
  assert.equal(contract.modeling.grn, null)
  assert.equal(contract.modeling.perturb, null)
  assert.equal(contract.inference.last_step, 'state')
})

test('口径 3) 状态数 <2（等价 n<2）与重跑导致的下游过期都必须警告；空数据集不编造', () => {
  const singleState = buildWorkbenchContract({
    id: 'ds_single',
    n_cells: 500,
    n_genes: 800,
    steps: { state: step({ metrics: { n_clusters: 1, n_hvg: 500, n_pcs: 10 } }) },
    stepOrder: ['state'],
  })
  assert.ok(singleState.caveats.some((item) => item.code === 'fewer_than_2_states'))
  assert.equal(singleState.modeling.state.n_clusters, 1)

  const reranUpstream = buildWorkbenchContract({
    id: 'ds_stale',
    n_cells: 2000,
    n_genes: 1200,
    steps: { qc: step(), state: step({ metrics: { n_clusters: 4 } }), grn: step({ metrics: { n_edges: 10 } }) },
    stepOrder: ['state', 'grn', 'qc'],
  })
  assert.ok(reranUpstream.caveats.some((item) => item.code === 'stale_downstream_steps'))

  const empty = buildWorkbenchContract({})
  assert.equal(empty.dataset.n_cells, null)
  assert.equal(empty.dataset.n_genes, null)
  assert.deepEqual(empty.design.steps_done, [])
  assert.equal(empty.inference, null)
  assert.equal(empty.modeling.state, null)
  assert.equal(empty.modeling.grn, null)
  assert.equal(empty.modeling.perturb, null)
  assert.equal(empty.evidence_kind, 'model_prediction')
  assert.ok(empty.caveats.some((item) => item.code === 'no_state'))
  assert.ok(empty.caveats.some((item) => item.code === 'no_data'))
  assert.ok(empty.caveats.some((item) => item.code === 'model_prediction'))
  assert.equal(empty.ok_to_cite, false)
})

test('口径 4) 空数据集字段为 null/空而不是编造，且无 warn 之外的误报', () => {
  const contract = buildWorkbenchContract(null)
  assert.equal(contract.schema, 'dsh-workbench.stats_contract/1')
  assert.deepEqual(contract.design.steps_done, [])
  assert.equal(contract.design.n_clusters, null)
  assert.equal(contract.design.silhouette, null)
  assert.equal(contract.identity.sha256, '')
  assert.equal(contract.identity.n_steps_done, 0)
  assert.deepEqual(contract.recent_steps, [])
  const warnCodes = contract.caveats.filter((item) => item.level === 'warn').map((item) => item.code)
  assert.deepEqual(warnCodes, ['model_prediction', 'no_state', 'no_data'])
  assert.deepEqual(Object.keys(contract).sort(), [
    'caveats', 'dataset', 'design', 'evidence_kind', 'generated_at', 'identity',
    'inference', 'modeling', 'ok_to_cite', 'plugin', 'plugin_version', 'recent_steps', 'schema',
  ])
})
