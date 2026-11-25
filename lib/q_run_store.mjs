/**
 * dsh-* — P0 不可变运行记录 / 产物登记（跨插件共享模块，六个插件各存一份**逐字相同**的副本）
 *
 * 背景（P0 第 2、3 条）：
 *   - 各插件把 figure/表格写在 `workdir/figures/<kind>.png`、`analyses/<dataset>/<step>/`，
 *     同参数重跑会覆盖同名文件；`prepareWorkdir()` 启动时还会删掉 `figures/*.png`，
 *     旧产物重启即失；
 *   - 任务状态只存在内存（runtime.jobs / Map），单步失败后 `opRecipeRun` 仍返回 ok:true；
 *   - 步骤之间用「活动数据集」定位数据，用户切 dataset 会漂移。
 *
 * 本模块提供：
 *   1. 每个任务一个**不可变** run 目录：`<root>/runs/<run_id>/{figures,tables,logs}/`；
 *      先在 `<root>/staging/` 里写，`commit()` 时原子 rename 成 runs/<run_id>；
 *   2. SQLite（node:sqlite，Node ≥ 22.5）持久化 runs / attempts / steps / artifacts；
 *      老 Node 没有 node:sqlite 时自动退化成 `runs.json` 原子重写（同一套 API）；
 *   3. 幂等键：输入（数据集 id + revision + 源文件哈希）、参数、版本、父产物一起哈希；
 *      同键重复提交 → 复用已提交 run（`reused: true`）；同键再次修订 → `-v2` 新 run。
 *
 * 目录结构：
 *   <root>/runs/<run_id>/manifest.json      任务清单（schema/版本/哈希/步骤/产物）
 *   <root>/runs/<run_id>/params.json        参数快照
 *   <root>/runs/<run_id>/frozen.json        冻结的数据版本（数据集 id/revision/源哈希）
 *   <root>/runs/<run_id>/result.json        结果摘要（大数组不落盘）
 *   <root>/runs/<run_id>/figures/*.png      图（sha256 登记在 manifest）
 *   <root>/runs/<run_id>/tables/*           表
 *   <root>/runs/<run_id>/logs/*.log         日志
 */

import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import {
  copyFileSync, existsSync, mkdirSync, openSync, readSync, closeSync, readdirSync, readFileSync,
  renameSync, rmSync, statSync, writeFileSync,
} from 'node:fs'
import path from 'node:path'

const require = createRequire(import.meta.url)
const SCHEMA_VERSION = 1

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

/** 稳定序列化：对象键排序，保证同样输入得到同样哈希。 */
export function stableStringify(value, depth = 0) {
  if (depth > 24) return '"[depth]"'
  if (value === null || value === undefined) return 'null'
  const type = typeof value
  if (type === 'number') return Number.isFinite(value) ? String(value) : 'null'
  if (type === 'bigint') return JSON.stringify(String(value))
  if (type === 'boolean') return value ? 'true' : 'false'
  if (type === 'string') return JSON.stringify(value)
  if (type === 'function' || type === 'symbol') return 'null'
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item, depth + 1)).join(',')}]`
  if (type === 'object') {
    const keys = Object.keys(value).filter((key) => value[key] !== undefined).sort()
    return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(value[key], depth + 1)}`).join(',')}}`
  }
  return 'null'
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

/** 文件哈希（流式读，不整文件进内存）。大文件按 4 MiB 分块。 */
export function fileSha256(file, { maxBytes = 8 * 1024 * 1024 * 1024 } = {}) {
  let fd = null
  try {
    const stat = statSync(file)
    if (!stat.isFile()) return null
    if (stat.size > maxBytes) return null
    fd = openSync(file, 'r')
    const hash = createHash('sha256')
    const buffer = Buffer.allocUnsafe(4 * 1024 * 1024)
    let read = 0
    do {
      read = readSync(fd, buffer, 0, buffer.length, null)
      if (read > 0) hash.update(buffer.subarray(0, read))
    } while (read > 0)
    return hash.digest('hex')
  } catch {
    return null
  } finally {
    if (fd !== null) { try { closeSync(fd) } catch { /* ignore */ } }
  }
}

function nowIso(now) {
  return new Date(now()).toISOString()
}

function ensureDir(dir) {
  mkdirSync(dir, { recursive: true })
  return dir
}

function slug(text, fallback = 'run') {
  const value = String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return (value || fallback).slice(0, 40)
}

function safeName(name, fallback = 'file') {
  const base = path.basename(String(name || fallback)).replace(/[^\w.\-\u4e00-\u9fa5]+/g, '_')
  return (base || fallback).slice(0, 160)
}

function writeJson(file, value) {
  ensureDir(path.dirname(file))
  writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
}

function readJson(file, fallback = null) {
  try { return JSON.parse(readFileSync(file, 'utf8')) } catch { return fallback }
}

function listDir(dir) {
  try { return readdirSync(dir) } catch { return [] }
}

// ---------------------------------------------------------------------------
// 存储后端：SQLite（首选）/ JSON 原子重写（老 Node 兜底）
// ---------------------------------------------------------------------------

function openSqliteBackend(file, log) {
  let DatabaseSync = null
  try {
    ;({ DatabaseSync } = require('node:sqlite'))
  } catch {
    return null
  }
  if (!DatabaseSync) return null
  let db = null
  try {
    db = new DatabaseSync(file)
    db.exec('PRAGMA journal_mode = WAL')
    db.exec('PRAGMA synchronous = NORMAL')
    db.exec(`
      CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        plugin TEXT, op TEXT, label TEXT,
        idem_key TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL,
        dataset_id TEXT, dataset_revision TEXT, parent_run TEXT,
        params TEXT, frozen TEXT,
        created_at TEXT, started_at TEXT, finished_at TEXT,
        dir TEXT, error TEXT, result TEXT, reused INTEGER DEFAULT 0
      );
      CREATE INDEX IF NOT EXISTS runs_key_idx ON runs (idem_key, status);
      CREATE INDEX IF NOT EXISTS runs_created_idx ON runs (created_at);
      CREATE TABLE IF NOT EXISTS attempts (
        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, error TEXT
      );
      CREATE TABLE IF NOT EXISTS steps (
        run_id TEXT NOT NULL, seq INTEGER NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, error TEXT, result TEXT,
        PRIMARY KEY (run_id, seq)
      );
      CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT, name TEXT,
        rel_path TEXT, sha256 TEXT, bytes INTEGER, source TEXT, created_at TEXT
      );
      CREATE INDEX IF NOT EXISTS artifacts_run_idx ON artifacts (run_id);
    `)
  } catch (error) {
    log('store', `SQLite 初始化失败，退化到 JSON：${error && error.message ? error.message : error}`)
    try { db && db.close() } catch { /* ignore */ }
    return null
  }

  const stmt = {
    insertRun: db.prepare(`INSERT INTO runs
      (run_id, plugin, op, label, idem_key, version, status, dataset_id, dataset_revision,
       parent_run, params, frozen, created_at, started_at, finished_at, dir, error, result, reused)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`),
    updateRun: db.prepare(`UPDATE runs SET status = ?, finished_at = ?, dir = ?, error = ?, result = ?, reused = ? WHERE run_id = ?`),
    getRun: db.prepare('SELECT * FROM runs WHERE run_id = ?'),
    // created_at 可能同毫秒：再按 rowid 倒序，保证「最近一次提交」可确定（幂等复用/修订版本号）
    findByKey: db.prepare(`SELECT * FROM runs WHERE idem_key = ? ORDER BY created_at DESC, rowid DESC`),
    listRuns: db.prepare('SELECT * FROM runs ORDER BY created_at DESC LIMIT ?'),
    insertAttempt: db.prepare('INSERT INTO attempts (run_id, attempt, status, started_at, finished_at, error) VALUES (?,?,?,?,?,?)'),
    finishAttempt: db.prepare('UPDATE attempts SET status = ?, finished_at = ?, error = ? WHERE run_id = ? AND attempt = ?'),
    upsertStep: db.prepare(`INSERT INTO steps (run_id, seq, step, status, started_at, finished_at, error, result)
      VALUES (?,?,?,?,?,?,?,?)
      ON CONFLICT(run_id, seq) DO UPDATE SET status=excluded.status, finished_at=excluded.finished_at,
        error=excluded.error, result=excluded.result`),
    insertArtifact: db.prepare(`INSERT OR REPLACE INTO artifacts
      (artifact_id, run_id, kind, name, rel_path, sha256, bytes, source, created_at) VALUES (?,?,?,?,?,?,?,?,?)`),
    listArtifacts: db.prepare('SELECT * FROM artifacts WHERE run_id = ? ORDER BY kind, name'),
    listSteps: db.prepare('SELECT * FROM steps WHERE run_id = ? ORDER BY seq'),
    deleteRun: db.prepare('DELETE FROM runs WHERE run_id = ?'),
  }

  return {
    kind: 'sqlite',
    insertRun(row) {
      stmt.insertRun.run(
        row.run_id, row.plugin, row.op, row.label, row.idem_key, row.version, row.status,
        row.dataset_id || null, row.dataset_revision || null, row.parent_run || null,
        row.params || '{}', row.frozen || '{}', row.created_at, row.started_at || null,
        row.finished_at || null, row.dir || null, row.error || null, row.result || null,
        row.reused ? 1 : 0,
      )
    },
    updateRun(row) {
      stmt.updateRun.run(
        row.status, row.finished_at || null, row.dir || null, row.error || null,
        row.result || null, row.reused ? 1 : 0, row.run_id,
      )
    },
    getRun(runId) { return stmt.getRun.get(runId) || null },
    findByKey(key) { return stmt.findByKey.all(key) },
    listRuns(limit) { return stmt.listRuns.all(limit) },
    insertAttempt(row) {
      stmt.insertAttempt.run(row.run_id, row.attempt, row.status, row.started_at || null, row.finished_at || null, row.error || null)
    },
    finishAttempt(row) { stmt.finishAttempt.run(row.status, row.finished_at || null, row.error || null, row.run_id, row.attempt) },
    upsertStep(row) {
      stmt.upsertStep.run(row.run_id, row.seq, row.step, row.status, row.started_at || null, row.finished_at || null, row.error || null, row.result || null)
    },
    insertArtifact(row) {
      stmt.insertArtifact.run(row.artifact_id, row.run_id, row.kind, row.name, row.rel_path, row.sha256, row.bytes, row.source || null, row.created_at)
    },
    listArtifacts(runId) { return stmt.listArtifacts.all(runId) },
    listSteps(runId) { return stmt.listSteps.all(runId) },
    deleteRun(runId) { stmt.deleteRun.run(runId) },
    close() { try { db.close() } catch { /* ignore */ } },
  }
}

function openJsonBackend(file, log) {
  const empty = { runs: [], attempts: [], steps: [], artifacts: [] }
  let state = readJson(file, null)
  if (!state || !Array.isArray(state.runs)) state = { ...empty }
  for (const key of Object.keys(empty)) if (!Array.isArray(state[key])) state[key] = []
  const flush = () => {
    const tmp = `${file}.tmp-${process.pid}`
    writeJson(tmp, state)
    try { renameSync(tmp, file) } catch (error) {
      log('store', `JSON 索引写入失败：${error && error.message ? error.message : error}`)
      try { rmSync(tmp, { force: true }) } catch { /* ignore */ }
    }
  }
  flush()
  return {
    kind: 'json',
    insertRun(row) { state.runs.push({ ...row }); flush() },
    updateRun(row) {
      const hit = state.runs.find((item) => item.run_id === row.run_id)
      if (hit) Object.assign(hit, row)
      flush()
    },
    getRun(runId) {
      const hit = state.runs.find((item) => item.run_id === runId)
      return hit ? { ...hit } : null
    },
    findByKey(key) {
      // 同 created_at 时，后插入的（修订版/重试版）优先，保证「最近一次」可确定
      const hit = state.runs
        .map((item, index) => ({ item, index }))
        .filter((row) => row.item.idem_key === key)
        .sort((a, b) => String(b.item.created_at).localeCompare(String(a.item.created_at)) || b.index - a.index)
      return hit.map((row) => row.item)
    },
    listRuns(limit) {
      return [...state.runs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, limit)
    },
    insertAttempt(row) { state.attempts.push({ ...row }); flush() },
    finishAttempt(row) {
      for (let i = state.attempts.length - 1; i >= 0; i -= 1) {
        const item = state.attempts[i]
        if (item.run_id === row.run_id && item.attempt === row.attempt) {
          Object.assign(item, { status: row.status, finished_at: row.finished_at, error: row.error })
          break
        }
      }
      flush()
    },
    upsertStep(row) {
      const hit = state.steps.find((item) => item.run_id === row.run_id && item.seq === row.seq)
      if (hit) Object.assign(hit, row)
      else state.steps.push({ ...row })
      flush()
    },
    insertArtifact(row) {
      const hit = state.artifacts.find((item) => item.artifact_id === row.artifact_id)
      if (hit) Object.assign(hit, row)
      else state.artifacts.push({ ...row })
      flush()
    },
    listArtifacts(runId) { return state.artifacts.filter((item) => item.run_id === runId).sort((a, b) => `${a.kind}/${a.name}`.localeCompare(`${b.kind}/${b.name}`)) },
    listSteps(runId) { return state.steps.filter((item) => item.run_id === runId).sort((a, b) => a.seq - b.seq) },
    deleteRun(runId) {
      for (const key of Object.keys(state)) state[key] = state[key].filter((item) => item.run_id !== runId)
      flush()
    },
    close() { flush() },
  }
}

// ---------------------------------------------------------------------------
// RunStore
// ---------------------------------------------------------------------------

/**
 * 打开（或创建）一个 run store。
 *
 * @param {{root:string, plugin?:string, prefix?:string, now?:()=>number, log?:(scope:string,msg:string)=>void}} opts
 */
export function openRunStore(opts = {}) {
  const root = path.resolve(String(opts.root || '.'))
  const plugin = String(opts.plugin || 'plugin')
  const prefix = slug(opts.prefix || plugin, 'run')
  const now = typeof opts.now === 'function' ? opts.now : Date.now
  const log = typeof opts.log === 'function' ? opts.log : () => {}

  const dirs = {
    root,
    runs: path.join(root, 'runs'),
    staging: path.join(root, 'staging'),
    db: path.join(root, 'runs.db'),
    json: path.join(root, 'runs.json'),
  }
  ensureDir(dirs.runs)
  ensureDir(dirs.staging)

  let backend = openSqliteBackend(dirs.db, log)
  if (!backend) backend = openJsonBackend(dirs.json, log)
  log('store', `运行记录存储：${backend.kind}（${backend.kind === 'sqlite' ? dirs.db : dirs.json}）`)

  const byKeyCache = new Map()
  const openRuns = new Map()

  const toRecord = (row, extra = {}) => {
    if (!row) return null
    const artifacts = backend.listArtifacts(row.run_id).map((item) => ({
      id: item.artifact_id,
      kind: item.kind,
      name: item.name,
      relPath: item.rel_path,
      file: item.rel_path && row.dir ? path.join(row.dir, item.rel_path) : (item.source || null),
      sha256: item.sha256,
      bytes: item.bytes,
    }))
    const steps = backend.listSteps(row.run_id).map((item) => ({
      seq: item.seq,
      step: item.step,
      status: item.status,
      startedAt: item.started_at,
      finishedAt: item.finished_at,
      error: item.error,
      result: item.result ? readJsonLoose(item.result) : null,
    }))
    return {
      runId: row.run_id,
      run_id: row.run_id,
      plugin: row.plugin,
      op: row.op,
      label: row.label,
      key: row.idem_key,
      version: row.version,
      status: row.status,
      dataset: row.dataset_id ? { id: row.dataset_id, revision: row.dataset_revision } : null,
      parentRun: row.parent_run,
      params: readJsonLoose(row.params) || {},
      frozen: readJsonLoose(row.frozen) || {},
      createdAt: row.created_at,
      startedAt: row.started_at,
      finishedAt: row.finished_at,
      dir: row.dir,
      error: row.error,
      result: readJsonLoose(row.result),
      reused: Boolean(row.reused),
      steps,
      artifacts,
      ...extra,
    }
  }

  const store = {
    kind: backend.kind,
    plugin,
    dirs,
    root,
    schemaVersion: SCHEMA_VERSION,
    now,

    /**
     * 计算幂等键：输入（数据集 id/revision/源文件哈希）、参数、代码/存储 schema 版本、
     * 父产物与显式版本全参与。任何一项变化都会得到新 key（→ 新 run_id），
     * 从而保证「不同参数产生新 run_id」「数据版本变化不复用旧缓存」。
     */
    keyFor(parts = {}) {
      return sha256(stableStringify({
        plugin,
        schemaVersion: SCHEMA_VERSION,
        ...parts,
      }))
    },

    /** 按幂等键找已提交 run（用于幂等复用）。 */
    findCommitted(key) {
      if (!key) return null
      if (byKeyCache.has(key)) return byKeyCache.get(key)
      const rows = backend.findByKey(key) || []
      const hit = rows.find((row) => row.status === 'committed') || null
      const record = hit ? toRecord(hit) : null
      if (record) byKeyCache.set(key, record)
      return record
    },

    /**
     * 开始一个任务。
     * @returns {{runId:string, staging:string, dir:string, version:number, attempt:number, reused:object|null}}
     */
    begin(options = {}) {
      const op = String(options.op || 'op')
      const key = String(options.key || store.keyFor({ op, args: options.params || {} }))
      if (options.reuse !== false) {
        const existing = store.findCommitted(key)
        if (existing && options.revise !== true) {
          return { runId: existing.runId, staging: null, dir: existing.dir, version: existing.version, attempt: 0, reused: existing }
        }
      }
      const prior = backend.findByKey(key) || []
      const version = prior.length + 1
      const base = `${prefix}-${slug(op)}-${key.slice(0, 12)}`
      const runId = version > 1 ? `${base}-v${version}` : base
      const attempt = prior.filter((row) => row.run_id === runId).length + 1
      const staging = path.join(dirs.staging, `${runId}-a${attempt}-${Math.random().toString(36).slice(2, 8)}`)
      ensureDir(path.join(staging, 'figures'))
      ensureDir(path.join(staging, 'tables'))
      ensureDir(path.join(staging, 'logs'))
      const createdAt = nowIso(now)
      const frozen = options.frozen && typeof options.frozen === 'object' ? options.frozen : {}
      const params = options.params && typeof options.params === 'object' ? options.params : {}
      const datasetId = options.datasetId || (frozen.dataset && frozen.dataset.id) || null
      const datasetRevision = options.datasetRevision || (frozen.dataset && frozen.dataset.revision) || null
      const row = {
        run_id: runId,
        plugin,
        op,
        label: String(options.label || op),
        idem_key: key,
        version,
        status: 'running',
        dataset_id: datasetId,
        dataset_revision: datasetRevision,
        parent_run: options.parentRun || null,
        params: JSON.stringify(params),
        frozen: JSON.stringify(frozen),
        created_at: createdAt,
        started_at: createdAt,
        finished_at: null,
        dir: null,
        error: null,
        result: null,
        reused: 0,
      }
      backend.insertRun(row)
      backend.insertAttempt({ run_id: runId, attempt, status: 'running', started_at: createdAt })
      writeJson(path.join(staging, 'params.json'), params)
      writeJson(path.join(staging, 'frozen.json'), frozen)
      const run = {
        runId,
        run_id: runId,
        key,
        op,
        label: row.label,
        version,
        attempt,
        staging,
        dir: path.join(dirs.runs, runId),
        params,
        frozen,
        datasetId,
        datasetRevision,
        steps: [],
        artifacts: [],
        reused: null,
        startedAt: createdAt,
      }
      openRuns.set(runId, run)
      log('store', `开始任务 ${runId}（op=${op} version=${version}${datasetId ? ` dataset=${datasetId}` : ''}）`)
      return { ...run, reused: null }
    },

    /** 记录一步状态（成功/失败/跳过都记，供 UI 与回查）。 */
    step(run, stepName, status, extra = {}) {
      const target = openRuns.get(run.runId) || run
      const seq = target.steps.length
      const at = nowIso(now)
      const row = {
        run_id: target.runId,
        seq,
        step: String(stepName),
        status: String(status),
        started_at: extra.startedAt || at,
        finished_at: status === 'running' ? null : at,
        error: extra.error ? String(extra.error).slice(0, 4000) : null,
        result: extra.result ? JSON.stringify(extra.result).slice(0, 200000) : null,
      }
      backend.upsertStep(row)
      target.steps.push({ seq, step: row.step, status, startedAt: row.started_at, finishedAt: row.finished_at, error: row.error, result: extra.result || null, ms: extra.ms ?? null })
      return target.steps[target.steps.length - 1]
    },

    /**
     * 把一个产物从可变工作目录搬进 staging（commit 后成为不可变产物）。
     * @returns {{id:string, kind:string, name:string, relPath:string, file:string, sha256:string, bytes:number}|null}
     */
    addArtifact(run, { kind = 'file', source, path: sourcePath, name, link = false } = {}) {
      const target = openRuns.get(run.runId) || run
      const src = sourcePath || source
      if (!src) return null
      const kindDir = kind === 'figure' ? 'figures' : kind === 'table' ? 'tables' : kind === 'log' ? 'logs' : 'files'
      const baseName = safeName(name || path.basename(src), 'artifact')
      if (link) {
        // 大文件（导出包）只登记绝对路径 + 哈希，不复制：避免双倍占盘。
        let bytes = 0
        let hash = null
        try {
          bytes = statSync(src).size
          hash = fileSha256(src)
        } catch (error) {
          log('store', `产物登记失败 ${src}：${error && error.message ? error.message : error}`)
          return null
        }
        const artifact = {
          id: `${target.runId}:link:${baseName}`,
          artifactId: `${target.runId}:link:${baseName}`,
          kind,
          name: baseName,
          relPath: null,
          file: src,
          source: src,
          sha256: hash,
          bytes,
          external: true,
        }
        target.artifacts.push(artifact)
        return artifact
      }
      let relName = baseName
      const occupied = new Set(target.artifacts.filter((item) => item.kind === kind).map((item) => item.name))
      if (occupied.has(relName)) relName = `${sha256(baseName).slice(0, 8)}-${baseName}`
      let copySource = src
      // 同一个文件可能已经是 staging 里的（比如 python 直接写到了 staging）
      const stagingDir = path.resolve(target.staging || '')
      const resolved = path.resolve(src)
      const alreadyInside = stagingDir && resolved.startsWith(`${stagingDir}${path.sep}`)
      let bytes = 0
      let hash = null
      try {
        if (!alreadyInside) {
          ensureDir(path.join(target.staging, kindDir))
          const dest = path.join(target.staging, kindDir, relName)
          copyFileSync(src, dest)
          copySource = dest
        }
        bytes = statSync(copySource).size
        hash = fileSha256(copySource) || sha256(readFileSync(copySource))
      } catch (error) {
        log('store', `产物登记失败 ${src}：${error && error.message ? error.message : error}`)
        return null
      }
      const relPath = path.posix.join(kindDir, relName)
      const artifact = {
        id: `${target.runId}:${relPath}`,
        artifactId: `${target.runId}:${relPath}`,
        kind,
        name: relName,
        relPath,
        file: path.join(target.staging, kindDir, relName),
        stagingFile: copySource,
        sha256: hash,
        bytes,
      }
      target.artifacts.push(artifact)
      return artifact
    },

    /** 写一段文本日志到 staging/logs。 */
    addLog(run, name, text) {
      const target = openRuns.get(run.runId) || run
      const relName = safeName(name || 'run.log', 'run.log')
      try {
        ensureDir(path.join(target.staging, 'logs'))
        writeFileSync(path.join(target.staging, 'logs', relName), String(text || '').slice(-2_000_000), 'utf8')
        const artifact = {
          id: `${target.runId}:logs/${relName}`,
          kind: 'log',
          name: relName,
          relPath: `logs/${relName}`,
          file: path.join(target.staging, 'logs', relName),
          sha256: sha256(String(text || '')),
          bytes: Buffer.byteLength(String(text || '')),
        }
        target.artifacts.push(artifact)
        return artifact
      } catch { return null }
    },

    /**
     * 提交：写 manifest → 原子 rename(staging → runs/<run_id>) → 更新索引。
     * 失败/取消的 run 不要调用 commit（用 abort）。
     */
    commit(run, { status = 'committed', result = null, log: logText = '' } = {}) {
      const target = openRuns.get(run.runId) || run
      if (!target.staging) {
        // 复用已提交的 run：直接返回既有记录
        const existing = backend.getRun(target.runId)
        return toRecord(existing, { reused: true })
      }
      if (logText) store.addLog(target, 'run.log', logText)
      const resultPayload = sanitizeResult(result)
      writeJson(path.join(target.staging, 'result.json'), resultPayload || {})
      const manifest = {
        schemaVersion: SCHEMA_VERSION,
        run_id: target.runId,
        plugin,
        op: target.op,
        label: target.label,
        version: target.version,
        key: target.key,
        status,
        dataset: target.datasetId ? { id: target.datasetId, revision: target.datasetRevision } : null,
        frozen: target.frozen || {},
        params: target.params || {},
        startedAt: target.startedAt,
        finishedAt: nowIso(now),
        steps: target.steps,
        artifacts: target.artifacts.map((item) => ({
          kind: item.kind, name: item.name, relPath: item.relPath, sha256: item.sha256, bytes: item.bytes,
          external: Boolean(item.external), source: item.external ? item.source : undefined,
        })),
      }
      writeJson(path.join(target.staging, 'manifest.json'), manifest)
      let finalDir = target.dir
      try {
        if (existsSync(finalDir)) {
          // 极端并发/重试：已有同名目录时加后缀，绝不覆盖已提交产物
          finalDir = `${finalDir}-a${target.attempt}`
        }
        ensureDir(path.dirname(finalDir))
        renameSync(target.staging, finalDir)
      } catch (error) {
        const message = `产物提交失败：${error && error.message ? error.message : error}`
        log('store', message)
        store.abort(target, { status: 'failed', error: message })
        throw Object.assign(new Error(message), { code: 'internal' })
      }
      target.dir = finalDir
      openRuns.delete(target.runId)
      const finishedAt = manifest.finishedAt
      const row = {
        run_id: target.runId,
        status,
        finished_at: finishedAt,
        dir: finalDir,
        error: null,
        result: JSON.stringify(resultSummary(resultPayload)),
        reused: 0,
      }
      backend.updateRun(row)
      backend.finishAttempt({ run_id: target.runId, attempt: target.attempt, status, finished_at: finishedAt })
      for (const artifact of target.artifacts) {
        const relPath = artifact.external ? null : artifact.relPath
        backend.insertArtifact({
          artifact_id: `${target.runId}:${artifact.relPath || `link:${artifact.name}`}`,
          run_id: target.runId,
          kind: artifact.kind,
          name: artifact.name,
          rel_path: relPath,
          sha256: artifact.sha256,
          bytes: artifact.bytes,
          source: artifact.external ? artifact.source : null,
          created_at: finishedAt,
        })
        artifact.file = artifact.external ? artifact.source : path.join(finalDir, artifact.relPath)
      }
      byKeyCache.set(target.key, toRecord(backend.getRun(target.runId)))
      log('store', `提交任务 ${target.runId}（${target.artifacts.length} 个产物）`)
      return toRecord(backend.getRun(target.runId))
    },

    /** 终止任务：删除 staging（不产生任何已提交产物），记录失败/取消状态。 */
    abort(run, { status = 'failed', error = '', result = null } = {}) {
      const target = openRuns.get(run.runId) || run
      if (target.staging) {
        try { rmSync(target.staging, { recursive: true, force: true }) } catch { /* ignore */ }
      }
      openRuns.delete(target.runId)
      const finishedAt = nowIso(now)
      const payload = sanitizeResult(result)
      backend.updateRun({
        run_id: target.runId,
        status,
        finished_at: finishedAt,
        dir: null,
        error: String(error || status).slice(0, 4000),
        result: payload ? JSON.stringify(resultSummary(payload)) : null,
        reused: 0,
      })
      backend.finishAttempt({
        run_id: target.runId, attempt: target.attempt, status,
        finished_at: finishedAt, error: String(error || status).slice(0, 4000),
      })
      log('store', `终止任务 ${target.runId}（${status}）：${String(error || '').slice(0, 200)}`)
      return toRecord(backend.getRun(target.runId))
    },

    get(runId) { return toRecord(backend.getRun(String(runId || ''))) },

    list({ op = null, dataset = null, status = null, limit = 50 } = {}) {
      const rows = backend.listRuns(Math.max(1, Math.min(1000, Number(limit) || 50) * 4))
      const out = []
      for (const row of rows) {
        if (op && row.op !== op) continue
        if (status && row.status !== status) continue
        if (dataset && row.dataset_id !== dataset) continue
        out.push(toRecord(row))
        if (out.length >= (Number(limit) || 50)) break
      }
      return out
    },

    /** 某数据集/操作最近一次提交的 run。 */
    latest({ op = null, dataset = null } = {}) {
      return store.list({ op, dataset, status: 'committed', limit: 1 })[0] || null
    },

    artifacts(runId) {
      const record = store.get(runId)
      return record ? record.artifacts : []
    },

    /**
     * 按内容 hash 校验某个 run 的全部已提交产物（重启后回查/导出前校验用）。
     * @returns {{ok:boolean, runId:string, dir:string|null, checked:number, reason?:string, mismatches:Array}}
     */
    verify(runId) {
      const record = store.get(runId)
      if (!record) return { ok: false, runId: String(runId || ''), dir: null, checked: 0, reason: 'not_found', mismatches: [] }
      if (!record.dir) return { ok: false, runId: record.runId, dir: null, checked: 0, reason: 'not_committed', mismatches: [] }
      const mismatches = []
      for (const artifact of record.artifacts) {
        const file = path.join(record.dir, artifact.relPath)
        const digest = fileSha256(file)
        if (!digest || digest !== artifact.sha256) {
          mismatches.push({ name: artifact.name, relPath: artifact.relPath, expected: artifact.sha256, actual: digest })
        }
      }
      return { ok: mismatches.length === 0, runId: record.runId, dir: record.dir, checked: record.artifacts.length, mismatches }
    },

    /** 按文件名找已提交产物（旧 figure 路由的兜底查找，按 run 时间倒序）。 */
    findArtifactByName(name, { kind = null } = {}) {
      const wanted = safeName(name, '')
      if (!wanted) return null
      for (const run of store.list({ limit: 200 })) {
        if (!run.dir) continue
        for (const artifact of run.artifacts) {
          if (kind && artifact.kind !== kind) continue
          if (artifact.name === wanted) return { ...artifact, runId: run.runId }
        }
      }
      return null
    },

    close() {
      try { backend.close() } catch { /* ignore */ }
    },

    _backend: backend,
  }

  return store
}

function readJsonLoose(text) {
  if (text === null || text === undefined || text === '') return null
  if (typeof text === 'object') return text
  try { return JSON.parse(String(text)) } catch { return null }
}

function sanitizeResult(value, depth = 0) {
  if (depth > 12) return null
  if (value === undefined) return undefined
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'bigint') return Number(value)
  if (typeof value === 'function' || typeof value === 'symbol') return null
  if (Array.isArray(value)) {
    // 大数组不进 result.json（完整数据在图/表产物或引擎里）
    return value.slice(0, 200).map((item) => {
      const next = sanitizeResult(item, depth + 1)
      return next === undefined ? null : next
    })
  }
  if (value && typeof value === 'object') {
    const out = {}
    for (const [key, item] of Object.entries(value)) {
      const next = sanitizeResult(item, depth + 1)
      if (next !== undefined) out[key] = next
    }
    return out
  }
  return value
}

function resultSummary(payload) {
  if (!payload || typeof payload !== 'object') return payload
  const out = {}
  if (payload.summary !== undefined) out.summary = payload.summary
  if (payload.message !== undefined) out.message = String(payload.message).slice(0, 1000)
  if (payload.figures !== undefined) out.figures = Array.isArray(payload.figures) ? payload.figures.slice(0, 20) : payload.figures
  if (payload.figure !== undefined) out.figure = payload.figure
  if (payload.file !== undefined) out.file = payload.file
  if (payload.files !== undefined) out.files = Array.isArray(payload.files) ? payload.files.slice(0, 20) : payload.files
  if (payload.tables !== undefined) out.tables = Array.isArray(payload.tables) ? payload.tables.slice(0, 20).map((item) => (item && item.id ? { id: item.id, title: item.title } : item)) : payload.tables
  return out
}

/** 清理启动残留：只清 staging（已提交 runs/ 永不清理）。 */
export function cleanStaging(root) {
  const staging = path.join(path.resolve(String(root || '.')), 'staging')
  let removed = 0
  for (const name of listDir(staging)) {
    try {
      rmSync(path.join(staging, name), { recursive: true, force: true })
      removed += 1
    } catch { /* ignore */ }
  }
  return removed
}

// ---------------------------------------------------------------------------
// 内容寻址下载缓存（P0-2：下载路径已存在时按哈希复用，不重复下载/复制）
// ---------------------------------------------------------------------------

/**
 * 打开内容寻址缓存：`<root>/cache/downloads/<sha前2位>/<sha>-<文件名>`。
 *
 * 用途：同一个文件（GEO 附件、参考对象、h5ad/rds）无论被几个任务、几次重试请求，
 * 都按 **内容 sha256** 定位；已存在时直接复用（`reused: true`），不会重复下载，
 * 也不受「同名不同内容」污染。索引写 `<root>/cache/downloads.json`（原子重写）。
 *
 * @param {{root:string, log?:(scope:string,msg:string)=>void}} opts
 */
export function openDownloadCache(opts = {}) {
  const root = path.resolve(String(opts.root || '.'))
  const log = typeof opts.log === 'function' ? opts.log : () => {}
  const dirs = {
    root,
    blobs: path.join(root, 'cache', 'downloads'),
    index: path.join(root, 'cache', 'downloads.json'),
  }
  ensureDir(dirs.blobs)
  let state = readJson(dirs.index, null)
  if (!state || typeof state !== 'object' || !state.bySha || typeof state.bySha !== 'object') state = { bySha: {} }

  const flush = () => {
    const tmp = `${dirs.index}.tmp-${process.pid}`
    try {
      writeJson(tmp, state)
      renameSync(tmp, dirs.index)
    } catch (error) {
      log('cache', `下载缓存索引写入失败：${error && error.message ? error.message : error}`)
      try { rmSync(tmp, { force: true }) } catch { /* ignore */ }
    }
  }

  const cache = {
    dirs,
    kind: 'content-addressed',
    get size() { return Object.keys(state.bySha).length },

    /** 查一个哈希对应的缓存文件（存在且可读才返回）。 */
    get(sha256) {
      const key = String(sha256 || '').toLowerCase()
      const hit = state.bySha[key]
      if (!hit) return null
      const file = path.join(dirs.blobs, hit.rel)
      try {
        const stat = statSync(file)
        if (!stat.isFile()) return null
      } catch { return null }
      state.bySha[key] = { ...hit, hits: (Number(hit.hits) || 0) + 1, lastUsedAt: new Date().toISOString() }
      flush()
      return { sha256: key, path: file, bytes: statSync(file).size, name: hit.name, reused: true }
    },

    has(sha256) { return Boolean(cache.get(sha256)) },

    /**
     * 把一个本地文件放进缓存。同哈希已存在 → 直接复用（不复制）。
     * @param {string} sourcePath 本地文件路径
     * @param {{name?:string, sha256?:string}} [extra] 期望哈希（给了就校验，不一致报 hash_mismatch）
     */
    put(sourcePath, extra = {}) {
      const src = path.resolve(String(sourcePath || ''))
      let stat = null
      try { stat = statSync(src) } catch { /* below */ }
      if (!stat || !stat.isFile()) {
        throw Object.assign(new Error(`下载缓存：源文件不存在或不是文件：${src}`), { code: 'not_found' })
      }
      const digest = fileSha256(src)
      if (!digest) throw Object.assign(new Error(`下载缓存：无法计算哈希：${src}`), { code: 'internal' })
      const expected = extra.sha256 ? String(extra.sha256).toLowerCase() : null
      if (expected && expected !== digest) {
        throw Object.assign(
          new Error(`下载缓存：内容哈希不符（期望 ${expected.slice(0, 12)}…，实际 ${digest.slice(0, 12)}…）：${src}`),
          { code: 'hash_mismatch', details: { expected, actual: digest } },
        )
      }
      const existing = cache.get(digest)
      if (existing) {
        log('cache', `复用已缓存文件 ${digest.slice(0, 12)}…（${existing.bytes} B）`)
        return { ...existing, reused: true }
      }
      const name = safeName(extra.name || path.basename(src), 'download')
      const rel = path.posix.join(digest.slice(0, 2), `${digest}-${name}`)
      const dest = path.join(dirs.blobs, rel)
      ensureDir(path.dirname(dest))
      const tmp = `${dest}.tmp-${process.pid}-${Math.random().toString(36).slice(2, 8)}`
      try {
        copyFileSync(src, tmp)
        renameSync(tmp, dest)
      } catch (error) {
        try { rmSync(tmp, { force: true }) } catch { /* ignore */ }
        throw Object.assign(new Error(`下载缓存写入失败：${error && error.message ? error.message : error}`), { code: 'internal' })
      }
      state.bySha[digest] = {
        sha256: digest,
        name,
        rel,
        bytes: stat.size,
        hits: 1,
        firstSeenAt: new Date().toISOString(),
        lastUsedAt: new Date().toISOString(),
        source: src,
      }
      flush()
      log('cache', `缓存新文件 ${digest.slice(0, 12)}…（${stat.size} B，${name}）`)
      return { sha256: digest, path: dest, bytes: stat.size, name, reused: false }
    },

    /** 校验索引里的每个 blob 仍存在且哈希一致（重启后/导出前审计用）。 */
    verify() {
      const mismatches = []
      let checked = 0
      for (const [key, hit] of Object.entries(state.bySha)) {
        checked += 1
        const digest = fileSha256(path.join(dirs.blobs, hit.rel))
        if (!digest || digest !== key) mismatches.push({ sha256: key, rel: hit.rel, actual: digest })
      }
      return { ok: mismatches.length === 0, checked, mismatches }
    },

    stats() {
      const out = { count: 0, bytes: 0 }
      for (const hit of Object.values(state.bySha)) { out.count += 1; out.bytes += Number(hit.bytes) || 0 }
      return out
    },

    close() { flush() },
    _state: state,
  }
  return cache
}

