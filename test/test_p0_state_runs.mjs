/**
 * P0-3 回归测试（VirtualCell）：不可变 run_id 产物、重启后可回查、
 * 同参数幂等、不同参数新 run_id、worker 初始化不再清理已提交产物。
 *
 * VirtualCell 的 impl.mjs 是通用命令路由（没有逐 op 的 Node 函数），数据集冻结与
 * 不可变运行目录由 Python 侧 RunStore 负责；这里验证 Node 侧共享引擎的：
 *   - engine.withTask 的 run 目录 / manifest / sha256；
 *   - prepareWorkdir 只清 staging，不删 runs/（旧行为会删 figures/*.png）；
 *   - 重启后同参数复用、不同参数新 run。
 *
 * 运行：node test/test_p0_state_runs.mjs
 */

import { mkdtempSync, existsSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createEngine, prepareWorkdir } from '../lib/engine.mjs'

const PLUGIN = 'virtualcell'
const TMP = mkdtempSync(path.join(tmpdir(), `dsh-p0-state-${PLUGIN}-`))
const workerPath = path.join(TMP, 'fake_worker.py')

const FAKE_WORKER = `#!/usr/bin/env python3
import json, os, sys, time

def arg(name, default=None):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except Exception:
        return default

WORKDIR = arg('--workdir', '.')
DATASETS = [
    {'id': 'ds1', 'name': 'A', 'n_obs': 100, 'n_vars': 50, 'steps': ['qc', 'state']},
    {'id': 'ds2', 'name': 'B', 'n_obs': 200, 'n_vars': 60, 'steps': []},
]

def reply(msg_id, result=None, error=None):
    payload = {'id': msg_id, 'ok': error is None}
    if error is None:
        payload['result'] = result if result is not None else {}
    else:
        payload['error'] = {'code': 'internal', 'message': error}
    sys.stdout.write(json.dumps(payload) + '\\n')
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    cmd = msg.get('cmd')
    args = msg.get('args') or {}
    if cmd == 'ping':
        reply(msg.get('id'), {'mode': 'builtin', 'packages': {}, 'layouts': ['pca'], 'platform': 'test'})
    elif cmd in ('list', 'state'):
        reply(msg.get('id'), {'datasets': DATASETS, 'active': 'ds1'})
    elif cmd == 'plot':
        kind = str(args.get('kind') or 'plot')
        name = 'f_%s_%s.png' % (kind, str(int(time.time() * 1000))[-6:])
        target = os.path.join(WORKDIR, 'figures', name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as handle:
            handle.write(b'\\x89PNG\\r\\n\\x1a\\n fake')
        reply(msg.get('id'), {'id': name, 'file': target, 'filename': name, 'width': 100, 'height': 80})
    elif cmd == 'shutdown':
        reply(msg.get('id'), {'bye': True})
        break
    else:
        reply(msg.get('id'), {'echo': cmd})
`

writeFileSync(workerPath, FAKE_WORKER, { mode: 0o755 })

let passed = 0
let failed = 0
const failures = []

function check(name, condition, detail = '') {
  if (condition) {
    passed += 1
    console.log(`  PASS ${name}${detail ? `  [${detail}]` : ''}`)
  } else {
    failed += 1
    failures.push(`${name}${detail ? `：${detail}` : ''}`)
    console.log(`  FAIL ${name}${detail ? `  [${detail}]` : ''}`)
  }
}

async function main() {
  const workdir = path.join(TMP, 'work')
  const makeEngine = () => createEngine({
    workdir,
    engineScript: workerPath,
    python: process.env.DSH_TEST_PYTHON || 'python3',
  })

  let engine = makeEngine()
  const started = await engine.start()
  check('0 引擎与 run store 就绪', started.mode === 'builtin' && Boolean(engine.store))

  const task = await engine.withTask({
    op: 'plot:state_embedding',
    dataset: 'ds1',
    params: { kind: 'state_embedding', gene: null },
    label: 'state_embedding',
    timeoutMs: 60_000,
  }, async (ctx) => {
    const result = await ctx.request('plot', { dataset: ctx.frozen.id, kind: 'state_embedding' }, { timeout: 30_000 })
    return { result, artifacts: [{ kind: 'figure', source: result.file, name: result.filename }] }
  })
  check('1a run 提交成功并返回 run_id', Boolean(task.runId) && task.reused === false, `run=${task.runId}`)
  const runDir = path.join(workdir, 'runs', task.runId)
  check('1b runs/<run_id>/manifest.json 存在', existsSync(path.join(runDir, 'manifest.json')))
  const manifest = JSON.parse(readFileSync(path.join(runDir, 'manifest.json'), 'utf8'))
  check('1c manifest 记录数据版本与产物哈希', manifest.dataset && /^[0-9a-f]{64}$/.test(manifest.dataset.revision || '') &&
    (manifest.artifacts || []).every((item) => /^[0-9a-f]{64}$/.test(item.sha256 || '')),
    JSON.stringify(manifest.dataset))
  const figure = (manifest.artifacts || []).find((item) => item.kind === 'figure')
  check('1d 图在不可变目录内', Boolean(figure) && existsSync(path.join(runDir, figure.relPath)), figure && figure.relPath)

  // 同参数幂等
  const again = await engine.withTask({
    op: 'plot:state_embedding',
    dataset: 'ds1',
    params: { kind: 'state_embedding', gene: null },
    label: 'state_embedding',
    timeoutMs: 60_000,
  }, async () => { throw new Error('不应该执行') })
  check('2a 同参数重复提交幂等（不执行）', again.reused === true && again.runId === task.runId,
    `first=${task.runId} second=${again.runId}`)
  // 不同参数新 run
  const variant = await engine.withTask({
    op: 'plot:state_embedding',
    dataset: 'ds1',
    params: { kind: 'state_embedding', gene: 'CD36' },
    label: 'state_embedding',
    timeoutMs: 60_000,
  }, async (ctx) => {
    const result = await ctx.request('plot', { dataset: ctx.frozen.id, kind: 'state_embedding' }, { timeout: 30_000 })
    return { result, artifacts: [{ kind: 'figure', source: result.file, name: result.filename }] }
  })
  check('2b 不同参数产生新 run_id', variant.runId !== task.runId, `a=${task.runId} b=${variant.runId}`)

  // 模拟重启：staging 里放残留，确认 prepareWorkdir 只清 staging，runs/ 不动
  writeFileSync(path.join(workdir, 'staging', 'leftover.tmp'), 'x')
  await engine.dispose()
  const dirs = prepareWorkdir(workdir)
  check('3a 重启后已提交 run 目录仍存在', existsSync(runDir) && existsSync(path.join(runDir, figure.relPath)), runDir)
  check('3b 重启只清 staging', readdirSync(dirs.staging).length === 0, JSON.stringify(readdirSync(dirs.staging)))

  engine = makeEngine()
  await engine.start()
  const runsAfter = engine.listRuns({ limit: 10 })
  check('3c 重启后仍能列出旧 run', runsAfter.some((item) => item.runId === task.runId),
    `runs=${runsAfter.map((item) => item.runId).join(',')}`)
  const found = engine.resolveArtifact(figure.name, 'figure')
  check('3d 重启后按文件名可回查旧产物', Boolean(found) && existsSync(found.file), found ? found.file : 'missing')
  check('3e getRun/runArtifacts 可回查', Boolean(engine.getRun(task.runId)) && engine.runArtifacts(task.runId).length >= 1)

  await engine.dispose()
  try { rmSync(TMP, { recursive: true, force: true }) } catch { /* ignore */ }

  console.log(`\n== P0-3 状态回归（${PLUGIN}）：${passed} 通过 / ${failed} 失败 ==`)
  if (failed) {
    for (const item of failures) console.log(`  ✗ ${item}`)
    process.exit(1)
  }
}

main().catch((error) => {
  console.error('测试本身异常：', error)
  try { rmSync(TMP, { recursive: true, force: true }) } catch { /* ignore */ }
  process.exit(1)
})
