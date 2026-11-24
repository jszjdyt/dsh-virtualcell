// dsh-virtualcell 浏览器端纯函数单测：node test/client-logic.test.mjs
// client.js 在 node（无 window）里只把纯函数挂到 globalThis.__VC_TEST__，直接 import 断言。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
const CLIENT_URL = new URL('../client.js', import.meta.url)
await import(CLIENT_URL.href)
const VC = globalThis.__VC_TEST__
assert.ok(VC, 'node 环境应通过 globalThis.__VC_TEST__ 暴露纯函数')
const SOURCE = await readFile(CLIENT_URL, 'utf8')
const hasKey = (object, key) => Object.prototype.hasOwnProperty.call(object, key)
test('加载：无 window 不炸，NS/API/轮询/字典/步骤齐全', () => {
  assert.equal(typeof globalThis.window, 'undefined')
  assert.equal(VC.NS, 'dsh-virtualcell')
  assert.equal(VC.API, '/virtualcell')
  assert.ok(VC.POLL_MS > 0 && VC.POLL_MS <= 5000)
  assert.ok(VC.PALETTE.length >= 12)
  assert.ok(Object.keys(VC.DICT_ZH).length > 400, '中文词典应来自 locale/zh.json')
  assert.deepEqual(Object.keys(VC.DICT_ZH).filter((key) => !hasKey(VC.DICT_EN, key)), [])
  assert.deepEqual(Object.keys(VC.DICT_EN).filter((key) => !hasKey(VC.DICT_ZH, key)), [])
  assert.equal(VC.STEP_ORDER.length, 10)
  assert.equal(VC.TAB_KEYS.length, 8)
})
test('格式化：千分位 / 百分号 / 空值 —', () => {
  assert.equal(VC.fmtNumber(1234567), '1.23M')
  assert.equal(VC.fmtNumber(12345.6), '12.35K')
  assert.equal(VC.fmtNumber(null), '—')
  assert.equal(VC.fmtInt(2000), '2,000')
  assert.equal(VC.fmtPct(0.412), '41.2%')
  assert.equal(VC.fmtPct(41.2), '41.2%')
  assert.equal(VC.fmtSigned(0.5), '+0.5')
})
test('颜色映射：索引/名字/连续值稳定', () => {
  assert.equal(VC.colorForIndex(0), VC.PALETTE[0])
  assert.equal(VC.colorForName('KLF4'), VC.colorForName('KLF4'))
  assert.match(VC.colorForName('T cell'), /^#[0-9a-f]{6}$/i)
  assert.equal(VC.colorForValue(null, 0, 1), '#bab0ac')
  assert.notEqual(VC.colorForValue(0, 0, 1), VC.colorForValue(1, 0, 1))
})
test('视口变换：project/unproject 往返可逆，缩放锚点不漂移', () => {
  const points = [{ x: -2, y: 1 }, { x: 3, y: 5 }, { x: 0, y: 0 }]
  const view = VC.buildViewport(points, 640, 400, 20)
  assert.ok(view.scale > 0)
  for (const point of points) {
    const screen = VC.projectPoint(view, point.x, point.y)
    const back = VC.unprojectPoint(view, screen.x, screen.y)
    assert.ok(Math.abs(back.x - point.x) < 1e-9 && Math.abs(back.y - point.y) < 1e-9)
  }
  const anchor = { px: 200, py: 120 }
  const before = VC.unprojectPoint(view, anchor.px, anchor.py)
  const zoomed = VC.zoomViewport(view, anchor.px, anchor.py, 2)
  const after = VC.unprojectPoint(zoomed, anchor.px, anchor.py)
  assert.ok(Math.abs(before.x - after.x) < 1e-9 && Math.abs(before.y - after.y) < 1e-9)
  const moved = VC.projectPoint(VC.panViewport(view, 10, -6), 0, 0)
  const origin = VC.projectPoint(view, 0, 0)
  assert.ok(Math.abs(moved.x - origin.x - 10) < 1e-9 && Math.abs(moved.y - origin.y + 6) < 1e-9)
})
test('state 归一化：缺字段安全，busy/stepsDone/results 都拿到', () => {
  const normalized = VC.normalizeState({
    ok: true,
    engine: { python: '/usr/bin/python3', pythonVersion: '3.14.2', backend: 'builtin', packages: { numpy: '2.0' }, adapters: [{ id: 'scanpy', label: 'scanpy', available: false, hint: 'pip install scanpy' }] },
    datasets: [{ id: 'ds1', name: 'demo', n_cells: 2000, n_genes: 1200, stepOrder: ['qc'], steps: { qc: { status: 'ok', ms: 120, summary: '质控完成' } } }],
    active: 'ds1',
    busy: { step: 'state', pct: 30, message: 'PCA' },
    results: { qc: { summary: '质控完成', metrics: { n_cells: 2000 } } },
    stepsDone: ['qc'], suggestedStep: 'state',
    runs: [{ id: 'run-0001', step: 'qc', status: 'ok', durationMs: 120 }], lastRun: 'run-0001',
  })
  assert.equal(normalized.active, 'ds1')
  assert.equal(normalized.activeDataset.name, 'demo')
  assert.equal(normalized.engine.adapters[0].available, false)
  assert.equal(normalized.busy.pct, 30)
  assert.deepEqual(normalized.stepsDone, ['qc'])
  assert.equal(VC.doneStepSet(normalized.activeDataset).qc, true)
  assert.equal(VC.stepStatusOf({ id: 'state', requires: ['qc'] }, VC.doneStepSet(normalized.activeDataset), ''), 'ready')
  assert.equal(VC.stepStatusOf({ id: 'state', requires: ['qc'] }, {}, ''), 'blocked')
  const empty = VC.normalizeState(null)
  assert.equal(empty.activeDataset, null)
  assert.deepEqual(empty.datasets, [])
  assert.equal(empty.busy, null)
})
test('建议指令：无数据/有数据都给出 2–5 条可复制中文指令', () => {
  const empty = VC.buildAgentInstructions(VC.normalizeState(null), {})
  assert.ok(empty.length >= 2 && empty.length <= 5)
  assert.ok(empty.every((item) => item.text && item.text.length > 10))
  const state = VC.normalizeState({
    active: 'ds1',
    datasets: [{ id: 'ds1', name: 'demo', stepOrder: ['qc', 'state'], steps: { qc: { status: 'ok' }, state: { status: 'ok' } } }],
    results: { grn: { metrics: { hub_tfs: [{ tf: 'KLF4' }], n_edges: 120 } } },
    suggestedStep: 'annotate', stepsDone: ['qc', 'state'],
  })
  const instructions = VC.buildAgentInstructions(state, {})
  assert.ok(instructions.length >= 3 && instructions.length <= 5)
  assert.ok(instructions.every((item) => item.title && item.text))
  const context = VC.buildAgentContext(state)
  assert.match(context, /demo/)
  assert.match(context, /ds1/)
})
test('错误映射：prerequisite/bad_request/timeout/no_python 各自给出可操作建议', () => {
  assert.match(VC.errorAdvice('prerequisite', { step: 'grn' }), /请先运行上一步/)
  assert.match(VC.errorAdvice('bad_request', { error: 'n_pcs 超范围' }), /参数不对/)
  assert.match(VC.errorAdvice('timeout'), /计算超时/)
  assert.match(VC.errorAdvice('no_python'), /Python 环境未就绪/)
  const described = VC.describeError({ code: 'prerequisite', error: '需要先跑 grn', details: { step: 'grn' } })
  assert.equal(described.code, 'prerequisite')
  assert.match(described.advice, /网络|grn|请先运行上一步/)
  assert.equal(VC.describeError({ code: 'weird' }).code, 'weird')
})
test('静态约束：单文件 IIFE、只 require react、槽位与端点齐全', () => {
  assert.match(SOURCE, /^\/\*\*/)
  assert.ok(SOURCE.includes("require('react')"))
  assert.ok(!/^\s*import\s/m.test(SOURCE), '不应出现 ESM import')
  assert.ok(!/^\s*export\s/m.test(SOURCE), '不应出现 ESM export')
  assert.ok(SOURCE.includes("slots.inject('main'"))
  assert.ok(SOURCE.includes("slots.inject('sidebar.panellist'"))
  assert.ok(SOURCE.includes("id: 'virtualcell'") && SOURCE.includes("key: 'virtualcell'") && SOURCE.includes('order: 5'))
  assert.ok(SOURCE.includes('locale.register(NS, { zh: DICT_ZH, en: DICT_EN })'))
  assert.ok(SOURCE.includes('window.__ModuleLoader__.load({ id: NS, factory: factory })'))
  for (const route of ['/state', '/steps', '/run', '/suggest', '/plot', '/attach', '/export', '/audit', '/insight', '/recipe/run', '/dataset/demo', '/dataset/open', '/dataset/upload', '/dataset/select', '/dataset/close', '/preview', '/figure', '/runs']) assert.ok(SOURCE.includes(route), '客户端应调用 ' + route)
})
