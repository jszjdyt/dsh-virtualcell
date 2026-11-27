#!/usr/bin/env node
/**
 * 离线测试运行器（CI 用）：按 package.json 的 `ci` 清单逐个跑，任何非 0 退出即失败。
 *
 * package.json 示例：
 *   "ci": {
 *     "node": ["test/client-logic.test.mjs", "test/ui_audit.test.mjs"],
 *     "python": ["test/test_p0_4_ora_family_growth.py"]
 *   }
 *
 * 设计取舍：清单显式列出「在干净 CI 环境里确实能过」的测试，而不是 glob 全跑——
 * 需要 R / 大模型 / 私有数据的测试留在本地，避免 CI 长期红着没人看。
 */
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const root = process.cwd()
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'))
const ci = pkg.ci && typeof pkg.ci === 'object' ? pkg.ci : {}
const nodeTests = Array.isArray(ci.node) ? ci.node : []
const pythonTests = Array.isArray(ci.python) ? ci.python : []
const failures = []

function runOne(kind, relative) {
  const full = path.join(root, relative)
  if (!fs.existsSync(full)) {
    console.log(`  ✗ ${kind} ${relative}（文件不存在）`)
    failures.push({ kind, relative, reason: 'missing' })
    return
  }
  const started = Date.now()
  const command = kind === 'node'
    ? { cmd: process.execPath, args: ['--test', relative] }
    : { cmd: process.env.PYTHON || 'python3', args: [relative] }
  const result = spawnSync(command.cmd, command.args, { cwd: root, stdio: 'inherit', env: process.env })
  const ms = Date.now() - started
  if (result.status === 0) {
    console.log(`  ✓ ${kind} ${relative}（${ms}ms）`)
  } else {
    console.log(`  ✗ ${kind} ${relative}（exit ${result.status}, ${ms}ms）`)
    failures.push({ kind, relative, reason: `exit ${result.status}` })
  }
}

console.log(`离线测试：${nodeTests.length} 个 node + ${pythonTests.length} 个 python`)
for (const relative of nodeTests) runOne('node', relative)
for (const relative of pythonTests) runOne('python', relative)

if (!nodeTests.length && !pythonTests.length) {
  console.log('  （没有声明 ci 清单：这个仓库只跑语法与契约检查）')
}
if (failures.length) {
  console.error(`\n失败 ${failures.length} 个：${failures.map((item) => `${item.kind}:${item.relative}`).join('、')}`)
  process.exit(1)
}
console.log(`\n全部通过：${nodeTests.length + pythonTests.length} 个测试`)
