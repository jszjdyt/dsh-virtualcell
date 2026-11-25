/**
 * dsh-virtualcell — Python worker 管理器（Host 侧）
 *
 * 职责：
 *   1. 探测可用的 Python 解释器（宿主 Electron 进程的 PATH 往往很窄，不能只靠 `python3`）；
 *   2. 以 `python -u vc_engine.py --workdir <dir>` 拉起长驻 worker，JSON-lines over stdio；
 *   3. 请求排队、超时、进度缓存、stderr 日志、崩溃/超时后自动重启；
 *   4. 向 UI 暴露 status()：解释器、引擎模式（builtin/scanpy）、可选模型适配器、包版本、错误原因。
 *
 * 本文件只依赖 node 内置模块，可以被 `test/engine.test.mjs` 直接 import 测试。
 */

import { spawn } from 'node:child_process'
import { createWriteStream, mkdirSync, readdirSync, realpathSync, unlinkSync } from 'node:fs'
import { homedir as osHomedir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import readline from 'node:readline'
import * as fsSync from 'node:fs'
import { createTaskQueue } from './q_engine_queue.mjs'
import { openRunStore, openDownloadCache, cleanStaging, stableStringify, sha256, fileSha256 } from './q_run_store.mjs'

const ENGINE_SCRIPT = fileURLToPath(new URL('./vc_engine.py', import.meta.url))
const DEFAULT_TIMEOUT = 240_000
const PROBE_TIMEOUT = 12_000
const STDERR_TAIL = 16_000
const MAX_QUEUE = 64
const QUEUE_GRACE = 30_000
const TASK_GRACE = QUEUE_GRACE
const STUCK_UNRESPONSIVE_MS = 10 * 60_000
const PLUGIN_ID = 'virtualcell'
const RUN_PREFIX = 'virtualcell'

/** 探测用的 import 检查：这些包是 builtin 引擎的硬需求。 */
const PROBE_CODE =
  'import sys,numpy,pandas,scipy,sklearn,matplotlib,anndata,h5py;' +
  'print("PROBE_OK", sys.version.split()[0])'

function tail(text, n = 800) {
  const s = String(text || '')
  return s.length <= n ? s : '…' + s.slice(-n)
}

function uniq(list) {
  const seen = new Set()
  const out = []
  for (const item of list) {
    if (!item) continue
    let key = item
    try { key = realpathSync(item) } catch { /* 不存在的候选保留原值 */ }
    if (seen.has(key)) continue
    seen.add(key)
    out.push(item)
  }
  return out
}

/**
 * 候选解释器列表。顺序即优先级：
 *   显式配置 / 环境变量 > PATH 上的 python3 > macOS 框架版 > Homebrew > 系统版。
 */
export function pythonCandidates(opts = {}) {
  const list = []
  if (opts.python) list.push(opts.python)
  if (process.env.DSH_VIRTUALCELL_PYTHON) list.push(process.env.DSH_VIRTUALCELL_PYTHON)
  if (process.env.DSH_VC_PYTHON) list.push(process.env.DSH_VC_PYTHON)
  list.push('python3')
  if (process.platform === 'win32') {
    // Windows 上通常没有 python3 这个别名，用 python / py launcher
    list.push('python', 'py')
  }

  // macOS 官方框架版：把已装版本按版本号从高到低排进去
  const frameworkRoot = '/Library/Frameworks/Python.framework/Versions'
  try {
    const versions = readdirSync(frameworkRoot)
      .filter((v) => /^\d+\.\d+$/.test(v))
      .sort((a, b) => {
        const [a1, a2] = a.split('.').map(Number)
        const [b1, b2] = b.split('.').map(Number)
        return b1 - a1 || b2 - a2
      })
    for (const v of versions) list.push(path.join(frameworkRoot, v, 'bin', 'python3'))
  } catch { /* 非 macOS 或没有框架版 */ }

  list.push('/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3')
  return uniq(list)
}

/** 跑一次 `python -c PROBE_CODE`，判断解释器是否带齐依赖。 */
export function probePython(cmd, timeout = PROBE_TIMEOUT) {
  return new Promise((resolve) => {
    let settled = false
    let out = ''
    let err = ''
    let child
    const finish = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      try { child && child.kill('SIGKILL') } catch { /* ignore */ }
      resolve(result)
    }
    const timer = setTimeout(() => finish({ ok: false, reason: '探测超时' }), timeout)
    try {
      child = spawn(cmd, ['-u', '-c', PROBE_CODE], { stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      return finish({ ok: false, reason: String(error && error.message ? error.message : error) })
    }
    child.stdout.on('data', (b) => { out += b.toString('utf8') })
    child.stderr.on('data', (b) => { err += b.toString('utf8') })
    child.on('error', (error) => finish({ ok: false, reason: String(error && error.message ? error.message : error) }))
    child.on('close', (code) => {
      if (code === 0 && /PROBE_OK\s+(\S+)/.test(out)) {
        const version = out.match(/PROBE_OK\s+(\S+)/)[1]
        finish({ ok: true, python: cmd, version })
      } else {
        const detail = tail(err || out, 300).replace(/\s+/g, ' ').trim()
        finish({ ok: false, reason: detail || `退出码 ${code}` })
      }
    })
  })
}

/** 依次探测候选项，返回第一个可用的；失败时带上每个候选的原因。 */
export async function findPython(opts = {}) {
  const candidates = pythonCandidates(opts)
  const failures = []
  for (const cmd of candidates) {
    const result = await probePython(cmd, opts.probeTimeout ?? PROBE_TIMEOUT)
    if (result.ok) return { ok: true, python: cmd, version: result.version, candidates }
    failures.push(`${cmd}: ${result.reason}`)
  }
  return { ok: false, candidates, failures }
}

/** 确保工作目录骨架存在，并清掉上一轮残留的图。 */
export function prepareWorkdir(root) {
  const dirs = {
    root,
    figures: path.join(root, 'figures'),
    uploads: path.join(root, 'uploads'),
    exports: path.join(root, 'exports'),
    mpl: path.join(root, 'mpl-cache'),
    runs: path.join(root, 'runs'),
    staging: path.join(root, 'staging'),
    log: path.join(root, 'engine.log'),
  }
  for (const key of ['root', 'figures', 'uploads', 'exports', 'mpl', 'runs', 'staging']) mkdirSync(dirs[key], { recursive: true })
  // P0-3：已提交产物在 runs/<run_id>/ 下，重启不再删除；这里只清未提交的 staging。
  // 旧行为是启动时删掉 figures/*.png，导致「旧产物不可回查」。
  try { cleanStaging(root) } catch { /* ignore */ }
  return dirs
}

export function defaultWorkdir() {
  return path.join(osHomedir(), '.dsh', 'virtualcell')
}

/**
 * 创建一个引擎实例。惰性启动：第一次 request 时才探测 + spawn。
 *
 * @param {{python?:string, workdir?:string, engineScript?:string, timeout?:number, onLog?:(line:string)=>void}} opts
 */
export function createEngine(opts = {}) {
  const workdir = opts.workdir || defaultWorkdir()
  const dirs = prepareWorkdir(workdir)
  const engineScript = opts.engineScript || ENGINE_SCRIPT
  const defaultTimeout = opts.timeout || DEFAULT_TIMEOUT
  // 排队兜底宽限（默认 30s；测试传 queueGrace 调小，用于验收「排队超时后不执行」）
  const queueGrace = Number.isFinite(Number(opts.queueGrace)) && Number(opts.queueGrace) >= 0
    ? Number(opts.queueGrace)
    : QUEUE_GRACE

  const state = {
    child: null,
    rl: null,
    starting: null,       // Promise<{python,version,mode}>
    pythonInfo: null,     // {python, version, mode, packages, layouts, warnings}
    stderrTail: '',
    pending: new Map(),   // id -> {resolve, reject, timer, cmd, progress}
    seq: 0,
    tasks: null,
    store: null,
    downloads: null,
    taskSeq: 0,
    lastActivityAt: Date.now(),
    disposed: false,
    logStream: null,
    lastProgress: null,
    lastError: null,
    startedAt: null,
  }

  const logStream = () => {
    if (state.logStream) return state.logStream
    try {
      state.logStream = createWriteStream(dirs.log, { flags: 'a' })
      state.logStream.on('error', () => { state.logStream = null })
    } catch { state.logStream = null }
    return state.logStream
  }
  const log = (scope, message) => {
    const line = `[${new Date().toISOString()}] [${scope}] ${message}`
    try { logStream() && state.logStream.write(line + '\n') } catch { /* ignore */ }
    if (typeof opts.onLog === 'function') {
      try { opts.onLog(line) } catch { /* ignore */ }
    }
  }

  // P0-1 / P0-3：任务队列负责 deadline 与取消；run store 负责持久化与不可变产物。
  const tasks = createTaskQueue({ onLog: (scope, message) => log(scope, message) })
  const store = openRunStore({
    root: workdir,
    plugin: PLUGIN_ID,
    prefix: RUN_PREFIX,
    log: (scope, message) => log(scope, message),
  })
  state.tasks = tasks
  state.store = store
  const downloads = openDownloadCache({ root: workdir, log: (scope, message) => log(scope, message) })
  state.downloads = downloads

  /** 卡死治理：worker 长时间没有任何输出才重启（取消单个任务不重启）。 */
  const reapIfStuck = () => {
    if (!state.child || !state.pending.size) { state.lastActivityAt = Date.now(); return }
    const now = Date.now()
    if (!state.lastActivityAt) state.lastActivityAt = now
    if (now - state.lastActivityAt < STUCK_UNRESPONSIVE_MS) return
    log('engine', `worker 超过 ${Math.round(STUCK_UNRESPONSIVE_MS / 60000)} 分钟无输出，判定卡死并重启`)
    state.lastActivityAt = now
    killChild('worker 长时间无响应')
  }

  const rejectAll = (error) => {
    for (const [, entry] of state.pending) {
      clearTimeout(entry.timer)
      try { entry.reject(error) } catch { /* ignore */ }
    }
    state.pending.clear()
  }

  /** 结束整个 worker 进程组（HOST-05：只 kill 直接子进程会留下 R/Python 孙进程）。 */
  const killTree = (child, signal) => {
    if (!child || !child.pid) return false
    if (process.platform === 'win32') {
      try {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
        return true
      } catch { /* 回退到 child.kill */ }
    } else {
      try {
        // spawn 时 detached=true ⇒ worker 是进程组组长，负 pid 表示整组
        process.kill(-child.pid, signal)
        return true
      } catch { /* 组不存在时回退 */ }
    }
    try { child.kill(signal) } catch { /* ignore */ }
    return false
  }

  const killChild = (reason) => {
    const child = state.child
    state.child = null
    // kill 之后必须让下一次 request() 的 start() 重新探测/拉起 worker：
    // 否则 start() 会直接返回已 resolve 的旧 starting（此时 child 已是 null），
    // request() 就报 "引擎未运行（engine_exited）"，看起来像杀一次 worker 后引擎就废了。
    state.pythonInfo = null
    state.starting = null
    if (state.rl) { try { state.rl.close() } catch { /* ignore */ } state.rl = null }
    if (!child) return
    try { child.stdin.end() } catch { /* ignore */ }
    killTree(child, 'SIGTERM')
    setTimeout(() => { killTree(child, 'SIGKILL') }, 800).unref?.()
    if (reason) log('engine', `worker 关闭：${reason}`)
  }

  const spawnWorker = (python) => {
    log('engine', `spawn ${python} -u ${engineScript} --workdir ${workdir}`)
    let configJson = '{}'
    try { configJson = JSON.stringify(opts.config && typeof opts.config === 'object' ? opts.config : {}) } catch { configJson = '{}' }
    const child = spawn(python, ['-u', engineScript, '--workdir', workdir], {
      cwd: path.dirname(engineScript),
      env: {
        ...process.env,
        MPLBACKEND: 'Agg',
        MPLCONFIGDIR: dirs.mpl,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        DS_VC_WORKDIR: workdir,
        DS_VC_CONFIG: configJson,
      },
      stdio: ['pipe', 'pipe', 'pipe'],
      // POSIX：独立进程组，超时/取消时可以连同 R/Python 子进程一起清理
      detached: process.platform !== 'win32',
    })
    state.child = child
    state.lastError = null

    state.rl = readline.createInterface({ input: child.stdout })
    state.rl.on('line', (line) => {
      const text = String(line || '').trim()
      if (!text) return
      state.lastActivityAt = Date.now()
      let msg
      try { msg = JSON.parse(text) } catch {
        log('stdout', `非 JSON 行：${tail(text, 300)}`)
        return
      }
      if (msg && msg.event === 'progress') {
        const entry = state.pending.get(String(msg.id))
        if (entry) entry.progress = { message: msg.message || '', pct: msg.pct }
        state.lastProgress = { at: Date.now(), message: msg.message || '', pct: msg.pct }
        return
      }
      const id = String(msg && msg.id)
      const entry = state.pending.get(id)
      if (!entry) return
      state.pending.delete(id)
      clearTimeout(entry.timer)
      if (msg.ok) entry.resolve(msg.result)
      else {
        const error = new Error((msg.error && msg.error.message) || '引擎返回失败')
        error.code = (msg.error && msg.error.code) || 'internal'
        if (msg.error && msg.error.trace) error.trace = msg.error.trace
        if (msg.error && msg.error.details) error.details = msg.error.details
        entry.reject(error)
      }
    })

    child.stderr.on('data', (buf) => {
      const text = buf.toString('utf8')
      state.stderrTail = (state.stderrTail + text).slice(-STDERR_TAIL)
      log('stderr', tail(text, 2000).replace(/\s+$/, ''))
    })

    child.on('error', (error) => {
      state.lastError = `worker 启动失败：${error && error.message ? error.message : error}`
      rejectAll(Object.assign(new Error(state.lastError), { code: 'engine_exited' }))
    })

    child.on('exit', (code, signal) => {
      // 只有「当前活跃 worker」的退出才结算 pending / 清状态；
      // 旧 worker 的迟到 exit（kill 后已重启新 worker）绝不能把新 worker 的 child/starting 清掉，
      // 也不能 rejectAll 把新 worker 的握手请求一起打死。
      const wasActive = state.child === child
      if (wasActive) state.child = null
      if (state.disposed) return
      const detail = `worker 退出 code=${code} signal=${signal} :: ${tail(state.stderrTail, 500).replace(/\s+/g, ' ')}`
      log('engine', detail)
      if (!wasActive) return
      const error = Object.assign(new Error(`虚拟细胞引擎异常退出（${code ?? signal}）：${tail(state.stderrTail, 400).trim()}`), {
        code: 'engine_exited',
      })
      rejectAll(error)
      // 引擎崩溃意味着内存里的数据集没了，下一次请求会重新启动
      state.pythonInfo = null
      state.starting = null
    })
    return child
  }

  /** 启动（幂等）：探测 Python → spawn → ping 握手。 */
  const start = async () => {
    if (state.disposed) throw Object.assign(new Error('插件已卸载'), { code: 'disposed' })
    if (state.starting) return state.starting
    state.starting = (async () => {
      const found = await findPython({ python: opts.python })
      if (!found.ok) {
        const detail = found.failures.slice(0, 4).join('；')
        throw Object.assign(
          new Error(
            `找不到可用的 Python 解释器（需要 numpy/pandas/scipy/sklearn/anndata/matplotlib）。` +
              `已尝试：${detail}`,
          ),
          { code: 'no_python' },
        )
      }
      log('engine', `使用 Python：${found.python} (${found.version})`)
      spawnWorker(found.python)
      // 握手必须走「不排队」的底层发送：以前这里调用 request('ping') 会排到业务队列末尾，
      // 而队列里的普通请求又在等 start() 完成 —— worker 崩溃后整条队列会互相等待（HOST-02）。
      const info = await sendRequest('ping', {}, 30_000)
      state.pythonInfo = {
        python: found.python,
        version: found.version,
        mode: info.mode || 'builtin',
        packages: info.packages || {},
        layouts: info.layouts || ['pca', 'kmeans'],
        warnings: info.warnings || [],
        platform: info.platform || process.platform,
      }
      state.startedAt = Date.now()
      log('engine', `握手成功：mode=${state.pythonInfo.mode} python=${found.version}`)
      return state.pythonInfo
    })().catch((error) => {
      state.starting = null
      state.lastError = String(error && error.message ? error.message : error)
      throw error
    })
    return state.starting
  }

  /**
   * 底层发送：只负责「写一条命令 + 等回复」，不排队、不启动。握手与业务请求分开。
   *
   * P0-1：单任务超时只结算这一条（pending 条目删除、该任务拒绝），
   * 不再 `rejectAll` 把其他任务一起打死 —— 取消/超时按任务隔离。
   * 只有显式要求（握手）或 watchdog 判定卡死时才 kill worker。
   */
  const sendRequest = (cmd, args = {}, timeout = defaultTimeout, opts = {}) => {
    if (!state.child) {
      return Promise.reject(Object.assign(new Error('引擎未运行'), { code: 'engine_exited' }))
    }
    if (state.pending.size >= MAX_QUEUE) {
      return Promise.reject(Object.assign(new Error('引擎请求过多，请稍后再试'), { code: 'busy' }))
    }
    return new Promise((resolve, reject) => {
      const id = String(++state.seq)
      const limit = Number.isFinite(timeout) && timeout > 0 ? timeout : defaultTimeout
      const entry = { resolve: null, reject: null, timer: null, cmd, progress: null, taskId: opts.taskId || null, settled: false }
      const finish = (ok, value) => {
        if (entry.settled) return
        entry.settled = true
        clearTimeout(entry.timer)
        state.pending.delete(id)
        // 2026-11-25 接手修复：这里必须结算 Promise 执行器的原生 resolve/reject。
        // 上一轮误写成 entry.resolve(value) / entry.reject(value)，而 entry.resolve 本身
        // 只是 finish 的包装（finish → entry.resolve → finish（settled 已置位）→ return），
        // 原生 resolve 永远不会被调用 ⇒ 所有引擎请求（含 start() 的 ping 握手）永久挂起。
        if (ok) resolve(value)
        else reject(value)
      }
      entry.resolve = (value) => finish(true, value)
      entry.reject = (value) => finish(false, value)
      entry.timer = setTimeout(() => {
        const detail = tail(state.stderrTail, 400).replace(/\s+/g, ' ').trim()
        finish(false, Object.assign(
          new Error(`引擎超时（${cmd}，${Math.round(limit / 1000)}s）${detail ? '：' + detail : ''}`),
          { code: 'timeout', taskId: entry.taskId || undefined },
        ))
        state.lastActivityAt = Date.now()
        // killOnTimeout 允许是函数：取消（entry.cancelled）时返回 false → 只废该任务、保留 worker。
        const shouldKill = typeof opts.killOnTimeout === 'function'
          ? (() => { try { return Boolean(opts.killOnTimeout()) } catch { return false } })()
          : Boolean(opts.killOnTimeout)
        if (shouldKill) killChild(`请求超时 ${cmd}（清理整棵进程树）`)
        else log('engine', `请求超时（仅该任务失败，worker 与其他任务保留）：${cmd}`)
      }, limit)
      state.pending.set(id, entry)
      try {
        state.child.stdin.write(JSON.stringify({ id, cmd, args }) + '\n')
      } catch (error) {
        finish(false, Object.assign(new Error(`写入引擎失败：${error && error.message ? error.message : error}`), { code: 'engine_exited' }))
      }
    })
  }

  /**
   * 发一条命令：排队 + 启动 + deadline。
   *
   * P0-1 的核心修复：排队条目带 deadline，**派发前**再查一次；被取消/过期的条目
   * 不会执行 sendRequest，因此不会再出现「调用方已超时，排队写入稍后照样执行」。
   * 运行中取消只影响本任务（结果丢弃），其他任务继续。
   */
  const request = (cmd, args = {}, reqOpts = {}) => {
    if (state.disposed) return Promise.reject(Object.assign(new Error('插件已卸载'), { code: 'disposed' }))
    const timeout = reqOpts.timeout || defaultTimeout
    const deadline = Number(reqOpts.deadline) > 0 ? Number(reqOpts.deadline) : Date.now() + timeout
    const taskId = String(reqOpts.taskId || `cmd-${++state.taskSeq}-${cmd}`)
    const run = async (entry) => {
      if (state.disposed) throw Object.assign(new Error('插件已卸载'), { code: 'disposed' })
      if (!reqOpts.skipStart) await start()
      if (state.disposed) throw Object.assign(new Error('插件已卸载'), { code: 'disposed' })
      if (!state.child) throw Object.assign(new Error('引擎未运行'), { code: 'engine_exited' })
      // 启动/握手可能已经吃掉了全部时间：派发前再查一次，绝不在过期后写入。
      if (Date.now() >= deadline) {
        throw Object.assign(new Error(`引擎请求在派发前已过期（${cmd}），未执行`), { code: 'timeout' })
      }
      state.lastActivityAt = Date.now()
      reapIfStuck()
      const remaining = Math.max(1, deadline - Date.now())
      return sendRequest(cmd, args, remaining, {
        taskId: entry.id,
        // 运行中超时：worker 可能真卡死（HOST-05：要连它拉起的 R/Python 孙进程一起清理），
        // 所以默认杀整棵进程树；但「调用方主动取消」必须只废掉该任务、不杀 worker（任务隔离），
        // 因此把是否杀树的判断交给队列条目的实时 cancelled 状态。
        killOnTimeout: reqOpts.killOnTimeout === false ? false : () => !entry.cancelled,
      })
    }
    return tasks.submit(run, {
      taskId,
      deadline,
      timeoutMs: timeout,
      graceMs: reqOpts.queueTimeout === false ? Infinity : (reqOpts.graceMs === undefined ? queueGrace : reqOpts.graceMs),
      signal: reqOpts.signal,
      label: cmd,
    })
  }


  const dispose = async () => {
    state.disposed = true
    if (state.child) {
      try {
        state.child.stdin.write(JSON.stringify({ id: 'shutdown', cmd: 'shutdown', args: {} }) + '\n')
      } catch { /* ignore */ }
      await new Promise((resolve) => setTimeout(resolve, 150))
    }
    try { tasks.cancelAll('插件已卸载') } catch { /* ignore */ }
    killChild('插件卸载')
    rejectAll(Object.assign(new Error('插件已卸载'), { code: 'disposed' }))
    try { state.logStream && state.logStream.end() } catch { /* ignore */ }
    state.logStream = null
    try { store.close() } catch { /* ignore */ }
    try { downloads.close() } catch { /* ignore */ }
  }

  const status = () => ({
    ready: Boolean(state.child),
    mode: state.pythonInfo ? state.pythonInfo.mode : state.lastError ? 'none' : 'unknown',
    python: state.pythonInfo ? state.pythonInfo.python : null,
    version: state.pythonInfo ? state.pythonInfo.version : null,
    packages: state.pythonInfo ? state.pythonInfo.packages : null,
    layouts: state.pythonInfo ? state.pythonInfo.layouts : null,
    warnings: state.pythonInfo ? state.pythonInfo.warnings : [],
    error: state.lastError,
    startedAt: state.startedAt,
    lastProgress: state.lastProgress,
    stderrTail: tail(state.stderrTail, 1200),
    queue: { waiting: tasks.waiting, running: tasks.running, pending: tasks.pendingTasks() },
    runs: { kind: store.kind, dir: store.dirs.runs },
    downloads: { dir: downloads.dirs.blobs, entries: downloads.size },
  })

  // -------------------------------------------------------------------------
  // 任务 / 数据版本冻结 / 不可变运行记录（P0-2、P0-3 的公共入口）
  // -------------------------------------------------------------------------

  /** 数据集摘要 → 稳定 revision 令牌（内容或步骤一变就变）。 */
  const revisionOf = (summary) => {
    if (!summary || typeof summary !== 'object') return 'unknown'
    return sha256(stableStringify({
      id: summary.id || null,
      path: summary.path || summary.source_path || null,
      n_samples: summary.n_samples ?? summary.n_obs ?? summary.n_spots ?? summary.n_cells ?? null,
      n_genes: summary.n_genes ?? summary.n_features ?? summary.n_vars ?? null,
      steps: summary.steps || [],
      groups: summary.groups || null,
      history: Array.isArray(summary.history) ? summary.history.slice(-2) : (summary.history || null),
    }))
  }

  // 源文件哈希缓存：大矩阵只算一次
  const hashCache = new Map()
  const cachedFileHash = (file) => {
    try {
      const stat = fsSync.statSync(file)
      if (!stat.isFile()) return null
      const stamp = `${stat.size}:${stat.mtimeMs}`
      const hit = hashCache.get(file)
      if (hit && hit.stamp === stamp) return hit.sha256
      const digest = fileSha256(file)
      hashCache.set(file, { stamp, sha256: digest })
      return digest
    } catch { return null }
  }

  /**
   * 冻结数据版本：任务开始前解析出**明确的数据集 id + revision**，
   * 后续每一步都显式带上这个 id，UI 切活动数据集不会漂移。
   */
  const freezeDataset = async (selector, { timeout = 60_000 } = {}) => {
    const list = await request('list', {}, { timeout })
    const datasets = Array.isArray(list && list.datasets) ? list.datasets : []
    const explicit = selector === undefined || selector === null ? '' : String(selector).trim()
    let summary = null
    if (explicit) {
      summary = datasets.find((item) => item && item.id === explicit) || null
      if (!summary) {
        throw Object.assign(new Error(
          `数据集 ${explicit} 不存在（可能已关闭或引擎重启过）；可用：${datasets.map((d) => d && d.id).filter(Boolean).join('、') || '无'}`,
        ), { code: 'not_found' })
      }
    } else {
      summary = datasets.find((item) => item && item.id === (list && list.active)) || datasets[0] || null
      if (!summary) throw Object.assign(new Error('还没有打开任何数据集，请先导入数据或载入示例'), { code: 'bad_request' })
    }
    const revision = revisionOf(summary)
    const sourcePath = summary.path || summary.source_path || null
    const source = sourcePath && typeof sourcePath === 'string' && fsSync.existsSync(sourcePath)
      ? { path: sourcePath, sha256: cachedFileHash(sourcePath) }
      : null
    const artifactId = `dataset:${summary.id}:${revision.slice(0, 16)}`
    return {
      id: summary.id,
      name: summary.name || summary.id,
      revision,
      artifactId,
      active: (list && list.active) || null,
      source,
      dataset: { id: summary.id, revision, artifact: artifactId, source },
      summary,
      at: new Date().toISOString(),
    }
  }

  /**
   * 参考对象解析（P0-2）：优先接受 artifact ID（`dataset:<id>:<revision>`），
   * 裸数据集 id 也兼容，但会**当场冻结**成 artifact ID。
   * 用 artifact ID 引用时，若引擎里同名数据集已被切换/重载/改步骤（revision 变了），
   * 直接报 stale_artifact —— 绝不把「现在的 ds1」当成任务开始时的 ds1。
   */
  const resolveDatasetRef = async (ref) => {
    const text = String(ref === undefined || ref === null ? '' : ref).trim()
    // 未显式给引用：当场冻结「活动数据集」，之后 UI 再切换也不会漂移；
    // 显式给引用时优先按 artifact ID 校验（裸 id 也兼容，但会被冻结成 artifact ID）。
    if (!text) return freezeDataset(undefined)
    const parts = text.split(':')
    if (parts.length === 3 && parts[0] === 'dataset') {
      const id = parts[1]
      const expected = String(parts[2] || '')
      const frozen = await freezeDataset(id)
      if (expected && frozen.revision !== expected && frozen.revision.slice(0, expected.length) !== expected) {
        throw Object.assign(
          new Error(`数据集 ${id} 的数据版本已变化（引用 ${expected.slice(0, 12)}… ≠ 当前 ${frozen.revision.slice(0, 12)}…），拒绝沿用旧引用的结果`),
          { code: 'stale_artifact', details: { dataset: id, expected, actual: frozen.revision, artifactId: frozen.artifactId } },
        )
      }
      return frozen
    }
    return freezeDataset(text)
  }

  /**
   * 下载/输入文件按内容哈希复用（P0-2）：同一文件第二次进入时不再复制/下载。
   * @returns {{sha256:string, path:string, bytes:number, reused:boolean}}
   */
  const acquireFile = (sourcePath, extra = {}) => downloads.put(sourcePath, extra)

  /** 按 sha256 取缓存文件（未命中返回 null）。 */
  const cachedFile = (hash) => downloads.get(hash)

  /**
   * 执行一个「有运行记录」的任务。
   *   freezeDataset / resolveDatasetRef → 幂等键 → begin → 产物/步骤登记 → commit/abort
   * - 同键且已提交 → 直接复用（reused: true），不重复跑（同参数幂等）；
   * - 参数/数据版本/父产物/输入清单变化 → 新 key → 新 run_id（同键重跑则是 -v2 新版本）；
   * - 任一步骤 failed/error/cancelled → 整个任务失败（**不会**以 ok:true 提交），staging 删除；
   * - fn 失败/取消 → abort：staging 删除，**不产生任何已提交产物**。
   */
  const withTask = async (options, fn) => {
    const op = String(options.op || 'op')
    const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : null
    const frozen = options.frozen || await resolveDatasetRef(
      options.datasetRef !== undefined ? options.datasetRef : options.dataset,
    )
    const params = options.params && typeof options.params === 'object' ? options.params : {}
    const key = store.keyFor({
      op,
      dataset: frozen.dataset,
      params,
      extra: options.extraKey || null,
      parent: options.parentRun || null,
      version: options.version || null,
      // 输入清单（下载/参考对象的 artifact ID + sha256）也进幂等键：
      // 换输入版本必须产生新 run，绝不能复用旧缓存。
      inputs: Array.isArray(options.inputs) ? options.inputs : null,
    })
    const begin = store.begin({
      op,
      label: options.label || op,
      key,
      params,
      frozen: {
        dataset: frozen.dataset,
        at: frozen.at,
        engine: state.pythonInfo ? { python: state.pythonInfo.python, version: state.pythonInfo.version } : null,
        deadline: null,
      },
      datasetId: frozen.id,
      datasetRevision: frozen.revision,
      parentRun: options.parentRun || null,
      reuse: options.reuse !== false,
      revise: Boolean(options.revise),
    })
    if (begin.reused) {
      return { runId: begin.runId, reused: true, run: begin.reused, frozen, result: begin.reused.result }
    }
    const run = begin
    const deadline = timeoutMs ? Date.now() + timeoutMs : null
    run.deadline = deadline
    const ctx = {
      run,
      frozen,
      deadline,
      params,
      remaining: () => (deadline ? Math.max(1, deadline - Date.now()) : null),
      request: (cmd, args = {}, reqOpts = {}) => request(cmd, args, {
        ...reqOpts,
        deadline: Number(reqOpts.deadline) > 0 ? Number(reqOpts.deadline) : (deadline || undefined),
      }),
      /** 记录一步状态（running/ok/failed/skipped 都记进 store，供 UI 与回查）。 */
      step: (step, status = 'ok', extra = {}) => store.step(run, step, status, extra),
      /** 登记产物：先落 staging，commit 后才成为 runs/<run_id>/ 下的不可变文件。 */
      artifact: (item) => store.addArtifact(run, item),
      log: (name, text) => store.addLog(run, name, text),
      /** 输入/下载文件按内容哈希复用（同一文件不重复复制）。 */
      acquire: (sourcePath, extra = {}) => acquireFile(sourcePath, extra),
    }
    try {
      const outcome = await fn(ctx)
      const result = outcome && outcome.result !== undefined ? outcome.result : outcome
      const artifacts = outcome && Array.isArray(outcome.artifacts) ? outcome.artifacts : []
      for (const item of artifacts) {
        if (!item) continue
        const source = item.source || item.file || item.path
        if (!source) continue
        store.addArtifact(run, { kind: item.kind || 'file', source, name: item.name })
      }
      if (Array.isArray(outcome && outcome.steps)) {
        for (const step of outcome.steps) {
          if (!step || !step.step) continue
          store.step(run, step.step, step.status || 'ok', step)
        }
      }
      // 单步失败不得整体 ok:true：任何失败/取消步骤都让整个 run 失败，绝不 commit 成成功。
      const failedStep = (run.steps || []).find(
        (item) => ['failed', 'error', 'cancelled', 'aborted'].includes(String(item.status)),
      )
      if (failedStep) {
        const stepError = Object.assign(
          new Error(`任务步骤失败（${failedStep.step}），拒绝提交为成功：${failedStep.error || failedStep.status}`),
          { code: 'step_failed', runId: run.runId, details: { step: failedStep.step, status: failedStep.status } },
        )
        store.abort(run, { status: 'failed', error: stepError.message })
        throw stepError
      }
      const record = store.commit(run, { status: 'committed', result })
      return { runId: record.runId, reused: false, run: record, frozen, result }
    } catch (error) {
      const cancelled = Boolean(error && (error.code === 'cancelled' || error.code === 'timeout'))
      if (error && error.runId === undefined) error.runId = run.runId
      try {
        store.abort(run, {
          status: cancelled ? 'cancelled' : 'failed',
          error: error && error.message ? error.message : String(error),
        })
      } catch { /* abort 必须不掩盖原始错误 */ }
      throw error
    }
  }

  return {
    workdir,
    dirs,
    request,
    start,
    dispose,
    status,
    get progress() { return state.lastProgress },
    get info() { return state.pythonInfo },
    get running() { return Boolean(state.child) },
    /** 引擎脚本是否存在（测试/诊断用） */
    scriptPath: engineScript,
    // 任务队列（deadline / 取消 / 隔离）
    queue: tasks,
    cancel: (taskId, reason) => tasks.cancel(taskId, reason),
    taskStatus: (taskId) => tasks.status(taskId),
    pendingTasks: () => tasks.pendingTasks(),
    // 冻结数据 + 不可变运行记录（P0-2 / P0-3）
    freezeDataset,
    withTask,
    store,
    beginTask: (opts) => store.begin(opts),
    commitTask: (run, opts) => store.commit(run, opts),
    abortTask: (run, opts) => store.abort(run, opts),
    listRuns: (opts) => store.list(opts),
    latestRun: (opts) => store.latest(opts),
    getRun: (runId) => store.get(runId),
    runArtifacts: (runId) => store.artifacts(runId),
    resolveArtifact: (name, kind) => store.findArtifactByName(name, kind ? { kind } : undefined),
    // 参考对象 / 输入复用 / 运行产物校验（P0-2、P0-3）
    resolveDatasetRef,
    acquireFile,
    cachedFile,
    downloadCache: downloads,
    verifyRun: (runId) => store.verify(runId),
    _internal: state,
  }
}
