/**
 * dsh-* — P0 任务队列（跨插件共享模块，六个插件各存一份**逐字相同**的副本）
 *
 * 这个模块只做一件事：把「排队 → 派发 → 超时/取消 → 隔离」四件事做对。
 *
 * 背景（P0 第 1 条）：六个插件的 engine.mjs 原来是同一条 `state.queue.then(run, run)`
 * 串行链，外面套一层 `Promise.race` 兜底超时。兜底超时只 reject 调用方，**排队中的
 * `run` 仍然会执行并写入结果**：GeoBulk 实测调用超时后，前序请求释放时那份被取消的
 * 写入照样落盘。本模块把「取消/过期」记录在队列条目上，派发前检查：
 *   - 条目已被取消（waiting）→ 直接抛 cancelled，不执行 fn；
 *   - 条目已过期（deadline 已过）→ 抛 deadline，不执行 fn；
 *   - 条目运行中被取消 → 调用方立刻收到 cancelled，fn 之后返回的结果被**丢弃**
 *     （不交给调用方，因此上层不会 commit），其他任务完全不受影响。
 *
 * 不依赖任何第三方包，Node 18+ 可跑，可被 test/*.mjs 直接 import。
 */

/** 取消类错误（调用方主动取消 / 等待项被放弃）。 */
export function cancelledError(message = '任务已取消', details = null) {
  const error = Object.assign(new Error(message), { code: 'cancelled' })
  if (details && typeof details === 'object') error.details = details
  return error
}

/** 超时/过期类错误（deadline 已过，任务不会执行）。 */
export function deadlineError(message = '任务已超时', details = null) {
  const error = Object.assign(new Error(message), { code: 'timeout' })
  if (details && typeof details === 'object') error.details = details
  return error
}

const noop = () => {}

function defaultIdGen() {
  let n = 0
  return () => `t${++n}`
}

/**
 * 单 worker 串行队列 + 每任务 deadline / 取消。
 *
 * 语义（P0 回归用例依赖这些语义）：
 *   1. 同一时刻只有一个 fn 在跑（首版单 worker，后续要扩并发再改这里）；
 *   2. `submit` 的 deadline 在**派发前**检查；过期/取消的条目不会调用 fn；
 *   3. 调用方超时（guard）只影响自己那一条；运行中的 fn 结果被丢弃；
 *   4. `cancel(taskId)` 等待中 → 永不执行；运行中 → 只拒绝该任务，不波及其他任务。
 */
export class TaskQueue {
  constructor({ now = Date.now, maxQueue = 256, onLog = null, idGen = defaultIdGen(), historyLimit = 200 } = {}) {
    this.now = now
    this.maxQueue = Math.max(1, Number(maxQueue) || 256)
    this.onLog = typeof onLog === 'function' ? onLog : null
    this.idGen = idGen
    this.entries = new Map()
    this.chain = Promise.resolve()
    this.seq = 0
    this.historyLimit = Math.max(8, Number(historyLimit) || 200)
  }

  get size() {
    let n = 0
    for (const entry of this.entries.values()) {
      if (entry.state === 'waiting' || entry.state === 'running') n += 1
    }
    return n
  }

  get running() {
    for (const entry of this.entries.values()) if (entry.state === 'running') return true
    return false
  }

  get waiting() {
    let n = 0
    for (const entry of this.entries.values()) if (entry.state === 'waiting') n += 1
    return n
  }

  /** 某任务是否还在队列/运行中（已结算的任务仍在 entries 里，用 state 区分）。 */
  has(taskId) {
    const entry = this.entries.get(String(taskId || ''))
    return Boolean(entry && (entry.state === 'waiting' || entry.state === 'running'))
  }

  status(taskId) {
    const entry = this.entries.get(String(taskId || ''))
    if (!entry) return null
    return {
      taskId: entry.id,
      label: entry.label,
      state: entry.state,
      cancelled: Boolean(entry.cancelled),
      abandoned: Boolean(entry.abandoned),
      submittedAt: entry.submittedAt,
      startedAt: entry.startedAt,
      finishedAt: entry.finishedAt,
      deadline: entry.deadline,
      error: entry.error ? String(entry.error.message || entry.error) : null,
    }
  }

  /** 所有非终态任务 id（用于 dispose / 诊断）。 */
  pendingTasks() {
    const out = []
    for (const entry of this.entries.values()) {
      if (entry.state === 'waiting' || entry.state === 'running') out.push(entry.id)
    }
    return out
  }

  /**
   * 提交一个任务。
   *
   * @param {(entry:object)=>Promise<any>} fn 真正执行动作的函数（只有被派发才调用）
   * @param {object} opts
   *   taskId       任务 id（默认自动生成）
   *   timeoutMs    相对超时的毫秒数（deadline = now + timeoutMs）
   *   deadline     绝对 deadline（毫秒时间戳，优先于 timeoutMs）
   *   graceMs      调用方兜底的额外宽限（默认 0；给 Infinity 表示不加兜底）
   *   queueTimeout false = 不加调用方兜底（仍做派发前 deadline 检查）
   *   signal       AbortSignal（abort 等价于 cancel）
   *   label        诊断标签（一般是 cmd 名）
   * @returns {Promise<any>}
   */
  submit(fn, opts = {}) {
    if (typeof fn !== 'function') return Promise.reject(new TypeError('TaskQueue.submit 需要函数'))
    const now = this.now()
    const timeoutMs = Number.isFinite(Number(opts.timeoutMs)) && Number(opts.timeoutMs) > 0
      ? Number(opts.timeoutMs)
      : null
    let deadline = Number(opts.deadline) > 0 ? Number(opts.deadline) : null
    if (!deadline) deadline = timeoutMs ? now + timeoutMs : Infinity
    const taskId = String(opts.taskId || this.idGen() || `t${++this.seq}`)
    const grace = opts.queueTimeout === false || opts.graceMs === Infinity
      ? Infinity
      : (Number.isFinite(Number(opts.graceMs)) ? Math.max(0, Number(opts.graceMs)) : 0)

    if (this.size >= this.maxQueue) {
      const error = Object.assign(new Error('引擎请求过多，请稍后再试'), { code: 'busy' })
      return Promise.reject(error)
    }

    const entry = {
      id: taskId,
      label: String(opts.label || ''),
      state: 'waiting',
      cancelled: false,
      abandoned: false,
      settled: false,
      submittedAt: now,
      startedAt: null,
      finishedAt: null,
      deadline,
      error: null,
      timer: null,
      resolve: null,
      reject: null,
      promise: null,
      signalCleanup: null,
    }

    let resolveCaller = null
    let rejectCaller = null
    const caller = new Promise((resolve, reject) => {
      resolveCaller = resolve
      rejectCaller = reject
    })
    entry.resolve = resolveCaller
    entry.reject = rejectCaller
    entry.promise = caller
    this.entries.set(taskId, entry)

    const settle = (ok, value) => {
      if (entry.settled) return
      entry.settled = true
      if (entry.timer) { clearTimeout(entry.timer); entry.timer = null }
      // 结算后必须解绑 AbortSignal 监听，否则长生命周期的 signal 会不断累积 listener
      if (entry.signalCleanup) {
        try { entry.signalCleanup() } catch { /* ignore */ }
        entry.signalCleanup = null
      }
      if (ok) entry.resolve(value)
      else entry.reject(value)
    }
    entry.settle = settle

    // 串行链：只有轮到它、且没被取消/过期时才执行 fn
    const link = this.chain.then(() => this._dispatch(entry, fn))
    this.chain = link.then(noop, noop)
    // 调用方可能先被 guard 拒绝，这里兜住链上的 rejection，避免 unhandledRejection
    link.catch(noop)

    // 调用方兜底计时器：等待中到期 → 取消（永不执行）；运行中到期 → 丢弃结果（不波及其他任务）。
    // deadline=Infinity 且没有 grace 时不建计时器（不能 setTimeout(Infinity)：那会被压成 1ms）。
    const backstop = Number.isFinite(deadline)
      ? Math.max(1, deadline - this.now()) + grace
      : (Number.isFinite(grace) ? grace : Infinity)
    if (Number.isFinite(backstop)) {
      entry.timer = setTimeout(() => {
        entry.timer = null
        if (entry.settled) return
        if (entry.state === 'waiting') {
          // 关键修复：等待项被放弃后必须「不执行」
          entry.cancelled = true
          entry.state = 'cancelled'
          entry.error = deadlineError(`任务在排队阶段超时（${entry.label || entry.id}），已取消，不会执行`)
          this._log('queue', `等待项超时取消：${entry.id}${entry.label ? ` (${entry.label})` : ''}`)
          settle(false, entry.error)
          return
        }
        if (entry.state === 'running') {
          // 运行中：只放弃这一条的结果，worker 与其他任务不受影响
          entry.abandoned = true
          entry.error = deadlineError(`任务执行超时（${entry.label || entry.id}），结果已丢弃`)
          this._log('queue', `运行项超时：${entry.id}${entry.label ? ` (${entry.label})` : ''}（结果丢弃，不影响其他任务）`)
          settle(false, entry.error)
        }
      }, backstop)
      entry.timer.unref?.()
    }

    if (opts.signal) {
      const signal = opts.signal
      const onAbort = () => { this.cancel(taskId, 'aborted') }
      if (signal.aborted) onAbort()
      else {
        try { signal.addEventListener('abort', onAbort, { once: true }) } catch { /* ignore */ }
        entry.signalCleanup = () => { try { signal.removeEventListener('abort', onAbort) } catch { /* ignore */ } }
      }
    }

    this._prune()
    return caller
  }

  async _dispatch(entry, fn) {
    if (entry.settled && entry.state === 'cancelled') return
    if (entry.cancelled) {
      entry.state = 'cancelled'
      entry.finishedAt = this.now()
      this._log('queue', `派发前发现已取消，跳过执行：${entry.id}`)
      entry.settle?.(false, cancelledError(`任务已取消（${entry.label || entry.id}），未执行`))
      return
    }
    if (Number.isFinite(entry.deadline) && this.now() >= entry.deadline) {
      entry.state = 'expired'
      entry.finishedAt = this.now()
      this._log('queue', `派发前 deadline 已过，跳过执行：${entry.id}`)
      entry.settle?.(false, deadlineError(`任务在派发前已过期（${entry.label || entry.id}），未执行`))
      return
    }
    entry.state = 'running'
    entry.startedAt = this.now()
    if (entry.timer) { clearTimeout(entry.timer); entry.timer = null }
    try {
      const value = await fn(entry)
      entry.finishedAt = this.now()
      if (entry.cancelled || entry.abandoned) {
        entry.state = 'cancelled'
        this._log('queue', `任务结果被丢弃（已取消/超时）：${entry.id}`)
        entry.settle?.(false, cancelledError(`任务已取消，结果已丢弃（${entry.label || entry.id}）`))
        return
      }
      entry.state = 'ok'
      entry.settle?.(true, value)
    } catch (error) {
      entry.finishedAt = this.now()
      entry.state = entry.cancelled ? 'cancelled' : 'error'
      entry.error = error
      entry.settle?.(false, error)
    }
  }

  /**
   * 取消任务。
   * - waiting：从逻辑上移除（永不执行）；
   * - running：只让该任务的调用方失败，结果丢弃，**不波及其他任务**。
   * @returns {boolean} 是否找到了可取消的任务
   */
  cancel(taskId, reason = 'cancelled') {
    const entry = this.entries.get(String(taskId || ''))
    if (!entry) return false
    if (entry.state === 'ok' || entry.state === 'error' || entry.state === 'expired') return false
    if (entry.state === 'cancelled') return true
    entry.cancelled = true
    entry.cancelReason = String(reason || 'cancelled')
    if (entry.signalCleanup) { try { entry.signalCleanup() } catch { /* ignore */ } entry.signalCleanup = null }
    if (entry.state === 'waiting') {
      entry.state = 'cancelled'
      entry.finishedAt = this.now()
      this._log('queue', `取消等待项（不会执行）：${entry.id}`)
      entry.settle?.(false, cancelledError(`任务已取消（${entry.label || entry.id}），未执行`))
      return true
    }
    if (entry.state === 'running') {
      entry.abandoned = true
      this._log('queue', `取消运行项（结果丢弃，其他任务继续）：${entry.id}`)
      entry.settle?.(false, cancelledError(`任务已取消（${entry.label || entry.id}），结果已丢弃`))
      return true
    }
    return false
  }

  /** 取消全部（只在 dispose 时用）。 */
  cancelAll(reason = 'disposed') {
    let count = 0
    for (const entry of this.entries.values()) {
      if (this.cancel(entry.id, reason)) count += 1
    }
    return count
  }

  /** 等待当前链排空（测试/关停用）。 */
  async idle() {
    await this.chain
  }

  _prune() {
    if (this.entries.size <= this.historyLimit) return
    const finished = []
    for (const entry of this.entries.values()) {
      if (entry.state !== 'waiting' && entry.state !== 'running') finished.push(entry)
    }
    finished.sort((a, b) => (a.finishedAt || 0) - (b.finishedAt || 0))
    const excess = this.entries.size - this.historyLimit
    for (let i = 0; i < Math.min(excess, finished.length); i += 1) {
      const entry = finished[i]
      if (entry.timer) { clearTimeout(entry.timer); entry.timer = null }
      this.entries.delete(entry.id)
    }
  }

  _log(scope, message) {
    if (!this.onLog) return
    try { this.onLog(scope, message) } catch { /* ignore */ }
  }
}

/** 便捷工厂（engine.mjs 用）。 */
export function createTaskQueue(opts = {}) {
  return new TaskQueue(opts)
}
