/**
 * dsh-virtualcell — Host 侧纯逻辑单测（不需要起宿主、不需要 Python）。
 *
 * 运行：node test/impl.test.mjs
 */

import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const impl = await import(new URL('../lib/impl.mjs', `file://${HERE}/`).href)

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed += 1
    console.log(`[PASS] ${name}`)
  } catch (error) {
    failed += 1
    console.log(`[FAIL] ${name} — ${error && error.message ? error.message : error}`)
  }
}

const { resolveFigurePath, toFailure, timeoutFor, summarizeState, buildToolDefinition, sanitizeValue } = impl.__internals

// --- resolveFigurePath：严格限制在工作目录 datasets/<ds>/runs/<run>/figures 下 ---
const engine = { workdir: '/tmp/vc-work' }

test('figure 路径正常解析', () => {
  const file = resolveFigurePath(engine, 'ds1/run-0003/state_embedding.png')
  assert.equal(file, '/tmp/vc-work/datasets/ds1/runs/run-0003/figures/state_embedding.png')
})

test('figure 拒绝路径穿越', () => {
  assert.equal(resolveFigurePath(engine, 'ds1/../../etc/passwd.png'), null)
  assert.equal(resolveFigurePath(engine, '../ds1/run/figures.png'), null)
})

test('figure 拒绝非 png 与段数不对', () => {
  assert.equal(resolveFigurePath(engine, 'ds1/run-0001/a.jpg'), null)
  assert.equal(resolveFigurePath(engine, 'ds1/run-0001.png'), null)
  assert.equal(resolveFigurePath(engine, ''), null)
})

test('figure 拒绝带斜杠的恶意段', () => {
  assert.equal(resolveFigurePath(engine, 'ds1/run-0001/../../x.png'), null)
})

// --- 错误映射 ---
test('prerequisite 映射 400 且保留 hint', () => {
  const error = Object.assign(new Error('请先运行质控'), { code: 'prerequisite', details: { hint: '第 1 步' } })
  const mapped = toFailure(error)
  assert.equal(mapped.status, 400)
  assert.equal(mapped.body.code, 'prerequisite')
  assert.equal(mapped.body.hint, '第 1 步')
})

test('timeout 映射 504，未知码映射 500', () => {
  assert.equal(toFailure(Object.assign(new Error('x'), { code: 'timeout' })).status, 504)
  assert.equal(toFailure(new Error('boom')).status, 500)
})

// --- 超时策略 ---
test('grn / screen 用更长超时', () => {
  assert.ok(timeoutFor('run', { step: 'grn' }) > timeoutFor('run', { step: 'qc' }))
  assert.ok(timeoutFor('run', { step: 'screen' }) > timeoutFor('run', { step: 'qc' }))
  assert.ok(timeoutFor('recipe_run') > timeoutFor('run', { step: 'qc' }))
})

// --- 工具定义 ---
test('工具定义合法且无 JSON-Schema 保留字冲突', () => {
  const def = buildToolDefinition({})
  assert.equal(def.name, 'virtualcell')
  assert.ok(def.description.length > 100)
  assert.ok(def.parameters.action.enum.includes('perturb'))
  assert.ok(def.parameters.action.enum.includes('insight'))
  const reserved = new Set(['type', 'enum', 'minimum', 'maximum', 'pattern', 'items'])
  for (const key of Object.keys(def.parameters)) assert.ok(!reserved.has(key), `参数名 ${key} 冲突`)
  assert.equal(typeof def.execute, 'function')
})

// --- status 摘要 ---
test('status 摘要有数据集与下一步', () => {
  const summary = summarizeState({
    datasets: [{ id: 'ds1', name: 'demo', n_cells: 2000, n_genes: 1000, n_tfs: 180, steps_done: ['qc', 'state'], steps_summary: {} }],
    active: 'ds1',
    engine: { backend: 'builtin', pythonVersion: '3.14.2', adapters: [{ label: 'scGPT', available: false }] },
    results: { state: { summary: '6 个状态' } },
    suggestedStep: 'annotate',
    stepsDone: ['qc', 'state'],
    busy: null,
  })
  assert.match(summary.message, /demo/)
  assert.match(summary.message, /质控、状态表征/)
  assert.match(summary.message, /建议下一步：annotate/)
  assert.match(summary.message, /scGPT/)
})

test('status 空工作台给引导', () => {
  const summary = summarizeState({ datasets: [], active: null })
  assert.match(summary.message, /没有数据集/)
  assert.match(summary.message, /demo/)
})

// --- 结果裁剪 ---
test('sanitizeValue 去掉 NaN 与超长数组', () => {
  const value = sanitizeValue({ a: Number.NaN, b: new Array(900).fill(1), c: { d: Number.POSITIVE_INFINITY } })
  assert.equal(value.a, null)
  assert.equal(value.b.length, 500)
  assert.equal(value.c.d, null)
})

console.log(`\n结果：${passed} 通过，${failed} 失败`)
process.exit(failed ? 1 : 0)
