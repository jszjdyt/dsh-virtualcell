/**
 * P0-1 回归测试：超时/取消之后**不执行写入**，运行中取消按任务隔离。
 *
 * 复现的旧 bug：engine.mjs 的串行队列外面套了一层 Promise.race 兜底超时，兜底只拒绝
 * 调用方；排队中的 `run` 稍后照旧执行 → 「调用方已超时，写入仍然发生」。
 *
 * 这里用真实的 lib/engine.mjs + 一个只讲 JSON-lines 的假 worker 驱动：
 *   1. 等待中的任务超时后，绝不执行（写日志里查不到 write）；
 *   2. 等待中的任务被 cancel 后，绝不执行；
 *   3. 运行中的任务被 cancel 时只影响它自己，后面的任务照常执行（隔离）；
 *   4. deadline 已过（派发前）的任务直接拒绝，不执行；
 *   5. 取消之后队列继续可用，没有残留 pending。
 *
 * 运行：node test/test_p0_queue_timeout.mjs
 */

import { mkdtempSync, readFileSync, existsSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createEngine } from '../lib/engine.mjs'

const TMP = mkdtempSync(path.join(tmpdir(), 'dsh-p0-queue-'))
const workerPath = path.join(TMP, 'fake_worker.py')

const FAKE_WORKER = `#!/usr/bin/env python3
import json, os, sys, time

def arg(name, default=None):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except Exception:
        return default

WORKDIR = arg('--workdir', '.')

def note(cmd, args):
    try:
        with open(os.path.join(WORKDIR, 'calls.jsonl'), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'cmd': cmd, 'args': args}, ensure_ascii=False) + '\\n')
    except Exception:
        pass

def append(name, text):
    try:
        with open(os.path.join(WORKDIR, 'writes.log'), 'a', encoding='utf-8') as handle:
            handle.write(str(name) + ':' + str(text) + '\\n')
    except Exception:
        pass

def reply(msg_id, result=None, error=None):
    payload = {'id': msg_id, 'ok': error is None}
    if error is None:
        payload['result'] = result if result is not None else {}
    else:
        payload['error'] = {'code': 'internal', 'message': error}
    sys.stdout.write(json.dumps(payload) + '\\n')
    sys.stdout.flush()

DATASETS = [
    {'id': 'ds1', 'name': 'A', 'n_samples': 4, 'n_genes': 10, 'steps': [], 'path': None},
    {'id': 'ds2', 'name': 'B', 'n_samples': 6, 'n_genes': 12, 'steps': [], 'path': None},
]
ACTIVE = 'ds1'

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
    note(cmd, args)
    if cmd == 'ping':
        reply(msg.get('id'), {'mode': 'builtin', 'packages': {}, 'layouts': ['pca'], 'platform': 'test'})
    elif cmd == 'list':
        reply(msg.get('id'), {'datasets': DATASETS, 'active': ACTIVE})
    elif cmd == 'state':
        reply(msg.get('id'), {'datasets': DATASETS, 'active': ACTIVE})
    elif cmd == 'select':
        ACTIVE = str(args.get('dataset') or ACTIVE)
        reply(msg.get('id'), {'active': ACTIVE})
    elif cmd == 'slow':
        time.sleep(float(args.get('seconds') or 0.5))
        reply(msg.get('id'), {'slept': True})
    elif cmd == 'write':
        append(args.get('name') or 'unnamed', 'written')
        reply(msg.get('id'), {'written': args.get('name')})
    elif cmd == 'fail':
        reply(msg.get('id'), error=str(args.get('message') or 'boom'))
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function readCalls(workdir) {
  const file = path.join(workdir, 'calls.jsonl')
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8').split('\n').filter(Boolean).map((line) => {
    try { return JSON.parse(line) } catch { return { cmd: '?', args: {} } }
  })
}

function readWrites(workdir) {
  const file = path.join(workdir, 'writes.log')
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8').split('\n').filter(Boolean)
}

function settled(promise) {
  return promise.then((value) => ({ ok: true, value }), (error) => ({ ok: false, error }))
}

async function main() {
  const workdir = path.join(TMP, 'work')
  const engine = createEngine({
    workdir,
    engineScript: workerPath,
    python: process.env.DSH_TEST_PYTHON || 'python3',
  })

  // -------------------------------------------------------------------------
  // 1. 等待中的任务超时 → 不执行
  // -------------------------------------------------------------------------
  const slow = engine.request('slow', { seconds: 0.6 }, { timeout: 10_000 })
  const impatient = await settled(engine.request('write', { name: 'w1' }, { timeout: 120, graceMs: 0 }))
  await slow
  await sleep(400)
  const writesAfter1 = readWrites(workdir)
  check('1a 等待中的写入超时后被拒绝', impatient.ok === false && impatient.error.code === 'timeout',
    impatient.ok ? 'resolved' : `code=${impatient.error.code}`)
  check('1b 超时后不写入（旧 bug：稍后照样执行）', !writesAfter1.some((line) => line.startsWith('w1:')),
    `writes=${JSON.stringify(writesAfter1)}`)
  check('1c 队列状态里该任务已取消', ['cancelled', 'expired'].includes((engine.taskStatus('cmd-2-write') || {}).state) ||
    engine.status().queue.waiting === 0, `waiting=${engine.status().queue.waiting}`)

  // -------------------------------------------------------------------------
  // 2. 取消等待中的任务 → 不执行
  // -------------------------------------------------------------------------
  const slow2 = engine.request('slow', { seconds: 0.5 }, { timeout: 10_000 })
  const queued = settled(engine.request('write', { name: 'w2' }, { timeout: 10_000, taskId: 'p0-w2' }))
  await sleep(50)
  const cancelled = engine.cancel('p0-w2')
  const queuedResult = await queued
  await slow2
  await sleep(400)
  const writesAfter2 = readWrites(workdir)
  check('2a 取消等待项返回 true', cancelled === true)
  check('2b 被取消的等待项收到 cancelled', queuedResult.ok === false && queuedResult.error.code === 'cancelled',
    queuedResult.ok ? 'resolved' : `code=${queuedResult.error.code}`)
  check('2c 取消后不写入', !writesAfter2.some((line) => line.startsWith('w2:')),
    `writes=${JSON.stringify(writesAfter2)}`)
  check('2d 任务状态可查', (engine.taskStatus('p0-w2') || {}).state === 'cancelled',
    JSON.stringify(engine.taskStatus('p0-w2')))

  // -------------------------------------------------------------------------
  // 3. 运行中取消 → 只影响该任务，后面的任务照常执行
  // -------------------------------------------------------------------------
  const slow3 = settled(engine.request('slow', { seconds: 0.5 }, { timeout: 10_000, taskId: 'p0-slow3' }))
  const follow = settled(engine.request('write', { name: 'w3' }, { timeout: 10_000, taskId: 'p0-w3' }))
  await sleep(120)
  const runningCancelled = engine.cancel('p0-slow3')
  const slow3Result = await slow3
  const followResult = await follow
  await sleep(200)
  const writesAfter3 = readWrites(workdir)
  check('3a 运行中取消只拒绝该任务', runningCancelled === true && slow3Result.ok === false &&
    slow3Result.error.code === 'cancelled', slow3Result.ok ? 'resolved' : `code=${slow3Result.error.code}`)
  check('3b 后续任务不受影响（按任务隔离）', followResult.ok === true && writesAfter3.some((line) => line.startsWith('w3:')),
    followResult.ok ? 'ok' : `code=${followResult.error && followResult.error.code}`)

  // -------------------------------------------------------------------------
  // 4. deadline 已过（派发前）→ 直接拒绝，不执行
  // -------------------------------------------------------------------------
  const expired = await settled(engine.request('write', { name: 'w4' }, { deadline: Date.now() - 500, timeout: 5_000 }))
  await sleep(200)
  const writesAfter4 = readWrites(workdir)
  check('4a 派发前 deadline 已过 → 拒绝', expired.ok === false && expired.error.code === 'timeout',
    expired.ok ? 'resolved' : `code=${expired.error.code}`)
  check('4b 过期任务不执行', !writesAfter4.some((line) => line.startsWith('w4:')),
    `writes=${JSON.stringify(writesAfter4)}`)

  // -------------------------------------------------------------------------
  // 5. 取消之后队列仍可用，且没有残留 pending
  // -------------------------------------------------------------------------
  const after = await settled(engine.request('list', {}, { timeout: 10_000 }))
  check('5a 取消/超时后队列继续可用', after.ok === true && Array.isArray(after.value.datasets) && after.value.datasets.length === 2)
  const pending = engine._internal.pending.size
  check('5b 没有残留 pending 请求', pending === 0, `pending=${pending}`)

  await engine.dispose()
  try { rmSync(TMP, { recursive: true, force: true }) } catch { /* ignore */ }

  console.log(`\n== P0-1 队列回归：${passed} 通过 / ${failed} 失败 ==`)
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
