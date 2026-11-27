#!/usr/bin/env node
/**
 * 插件契约检查（CI 用）——不依赖第三方包，只看仓库事实。
 *
 * 检查项：
 *  1. package.json 可解析，name 以 dsh- 开头，version 是 semver；
 *  2. README.md / cordis.patch.yml 存在（插件入口约定）；
 *  3. files 清单非空、不夹带 __pycache__ / .bak，且能覆盖入口与 lib；
 *  4. package.json 与 cordis.patch.yml 里不出现本机绝对路径（/Users/、/home/、C:\）；
 *  5. 如果 lib 里声明了 PLUGIN_VERSION，必须与 package.json version 一致（防止版本漂移）；
 *  6. license 字段缺失给 warning（不阻断，但会在 CI 日志里显眼）。
 *
 * 退出码：0 通过；1 有 issue。
 */
import fs from 'node:fs'
import path from 'node:path'

const root = process.cwd()
const issues = []
const warnings = []
const ok = []

function fail(code, detail) { issues.push(`✗ ${code}：${detail}`) }
function warn(code, detail) { warnings.push(`! ${code}：${detail}`) }
function pass(detail) { ok.push(`✓ ${detail}`) }

function readText(relative) {
  const full = path.join(root, relative)
  if (!fs.existsSync(full)) return null
  return fs.readFileSync(full, 'utf8')
}

// 1) package.json
let pkg = null
try {
  pkg = JSON.parse(readText('package.json') || 'null')
} catch (error) {
  fail('package.json 解析失败', String(error && error.message ? error.message : error))
}
if (pkg) {
  if (!/^dsh-[a-z0-9-]+$/.test(String(pkg.name || ''))) fail('name', `应形如 dsh-xxx，实际 ${pkg.name}`)
  else pass(`name=${pkg.name}`)
  if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(String(pkg.version || ''))) fail('version', `不是 semver：${pkg.version}`)
  else pass(`version=${pkg.version}`)
  if (!pkg.license) warn('license', 'package.json 缺 license 字段（发布前需补）')
  else pass(`license=${pkg.license}`)

  const files = Array.isArray(pkg.files) ? pkg.files : []
  if (!files.length) fail('files', 'files 清单为空：npm 包会缺文件')
  else pass(`files=${files.length} 项`)
  for (const pattern of files) {
    const text = String(pattern)
    // '!' 开头是 npm files 的排除模式（例如 !lib/__pycache__/**）：这正是想要的，不该报错
    if (text.startsWith('!')) continue
    if (text.includes('__pycache__')) fail('files', `不应打包 __pycache__：${text}`)
    if (text.includes('.bak')) fail('files', `不应打包备份文件：${text}`)
  }
  const coversEntry = files.some((item) => /(^|\/)(index\.js|cordis\.patch\.yml)$/.test(String(item)))
  if (!coversEntry) warn('files', 'files 清单没显式包含 index.js / cordis.patch.yml（确认入口会被打包）')
}

// 2) 插件入口约定
const readme = readText('README.md')
if (!readme) fail('README.md', '缺失（插件需要一个可读的说明）')
else if (readme.trim().length < 200) warn('README.md', '内容过短（<200 字符）')
else pass('README.md')
const manifest = readText('cordis.patch.yml')
if (!manifest) fail('cordis.patch.yml', '缺失（DSH 插件入口清单）')
else pass('cordis.patch.yml')

// 3) 本机绝对路径不得进包
const ABSOLUTE = /(\/Users\/|\/home\/[a-z]|[A-Za-z]:\\\\)/
for (const [relative, text] of [['package.json', readText('package.json')], ['cordis.patch.yml', manifest]]) {
  if (!text) continue
  if (ABSOLUTE.test(text)) fail('绝对路径', `${relative} 里出现本机绝对路径`)
  else pass(`${relative} 无绝对路径`)
}

// 4) 版本一致性：lib 里声明 PLUGIN_VERSION 时必须等于 package.json.version
const versionSources = []
function scanDir(dir, depth = 0) {
  if (depth > 2) return
  let entries = []
  try { entries = fs.readdirSync(dir, { withFileTypes: true }) } catch { return }
  for (const entry of entries) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      if (['node_modules', '__pycache__', '.git', 'test', 'examples'].includes(entry.name)) continue
      scanDir(full, depth + 1)
    } else if (/\.(mjs|js)$/.test(entry.name)) {
      const text = fs.readFileSync(full, 'utf8')
      const match = text.match(/export\s+const\s+PLUGIN_VERSION\s*=\s*['"]([^'"]+)['"]/)
      if (match) versionSources.push({ file: path.relative(root, full), version: match[1] })
    }
  }
}
scanDir(path.join(root, 'lib'))
if (versionSources.length && pkg) {
  for (const source of versionSources) {
    if (source.version !== String(pkg.version)) {
      fail('PLUGIN_VERSION', `${source.file} 里是 ${source.version}，package.json 是 ${pkg.version}（版本漂移）`)
    } else {
      pass(`PLUGIN_VERSION 与 package.json 一致（${source.file}）`)
    }
  }
}

// 5) 输出
console.log(`插件契约检查：${pkg ? pkg.name : '(未知)'} @ ${pkg ? pkg.version : '?'}`)
for (const line of ok) console.log('  ' + line)
for (const line of warnings) console.log('  ' + line)
for (const line of issues) console.log('  ' + line)
parting()
function parting() {
  if (issues.length) {
    console.error(`\n契约检查失败：${issues.length} 个问题、${warnings.length} 个警告`)
    process.exit(1)
  }
  console.log(`\n契约检查通过：${ok.length} 项通过、${warnings.length} 个警告`)
}
