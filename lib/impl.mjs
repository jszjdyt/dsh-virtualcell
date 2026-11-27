/**
 * dsh-virtualcell — Host 业务实现（唯一操作实现）
 *
 * 一条操作只写一次：浏览器 UI 走 `/virtualcell/*` 路由，Agent 走 `virtualcell` 工具，
 * 两边入口都调这里的 op* 函数，最终都落到 Python worker 的同一个 cmd 上。
 *
 * 本文件按需动态 import（见 index.js 的 loadImpl），因此不要在 lib 模块之间写静态 import；
 * 需要 engine.mjs 时用 loadEngineMod()。
 */

import { createReadStream, existsSync, mkdirSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { pipeline } from 'node:stream/promises'
import { createWriteStream } from 'node:fs'

const PREFIX = '/virtualcell'
const MAX_JSON_BODY = 2 * 1024 * 1024
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
const ACTIVITY_LIMIT = 200

/** 每类操作的超时（毫秒）。重的步骤放宽，避免长任务被误杀。 */
const TIMEOUTS = {
  run: 15 * 60 * 1000,
  run_grn: 25 * 60 * 1000,
  run_screen: 20 * 60 * 1000,
  sweep: 40 * 60 * 1000,
  recipe_run: 80 * 60 * 1000,
  open: 20 * 60 * 1000,
  demo: 10 * 60 * 1000,
  plot: 3 * 60 * 1000,
  export: 5 * 60 * 1000,
  attach: 5 * 60 * 1000,
  default: 60 * 1000,
}

// ---------------------------------------------------------------------------
// HTTP 小工具
// ---------------------------------------------------------------------------

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1', '0.0.0.0'])

/** 插件版本（与 package.json 同步；契约测试会比对，防止漂移）。 */
export const PLUGIN_VERSION = '0.1.0'

/** 本工作台所有推演类产物的证据类型：模型推演，不是实验结论。 */
export const EVIDENCE_KIND = 'model_prediction'

/** 统计口径契约 schema（与 dsh-geobulk / dsh-metabolomics / bioflow 共用同一套编号）。 */
const CONTRACT_SCHEMA = 'dsh-workbench.stats_contract/1'

/** 状态机顺序：口径卡用它判断「重跑上游后下游结果是否可能过期」。
 *  与 lib/vc_engine.py 的 STEP_ORDER 逐字一致。 */
const CONTRACT_STEP_ORDER = ['qc', 'state', 'annotate', 'grn', 'validate', 'perturb', 'screen', 'response', 'transition', 'insight']

function contractNumber(value) {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function contractText(value) {
  return value === null || value === undefined ? '' : String(value)
}

/**
 * 工作台统计口径契约（口径卡的数据源，UI / Agent / 导出共用）。
 *
 * 与 bioflow 的 run 级契约同一套哲学：只搬运引擎真实回传的字段，
 * 取不到就写 null 并进 caveats——不补默认值、不猜方法名。
 * 虚拟细胞的「口径」= 数据规模 + 状态表征（HVG/PC/状态数/分离度）
 * + 调控网络（alpha/先验/边数）+ 最近一次虚拟扰动（基因/方式/强度/状态位移）。
 * **本工作台的全部推演产物 evidence_kind=model_prediction**，这条必须出现在 caveats 里。
 */
export function buildWorkbenchContract(dataset, extra = {}) {
  const d = dataset && typeof dataset === 'object' ? dataset : {}
  const plugin = String(extra.plugin || 'dsh-virtualcell')
  const version = String(extra.version || PLUGIN_VERSION)
  const stepMap = d.steps && typeof d.steps === 'object' ? d.steps : {}
  const stepOrderRaw = Array.isArray(d.stepOrder) ? d.stepOrder : (Array.isArray(d.steps_done) ? d.steps_done : [])
  const stepsDone = stepOrderRaw.map(String)
  const truth = d.truth && typeof d.truth === 'object' ? d.truth : {}
  const stepOf = (id) => {
    const entry = stepMap[id]
    return entry && typeof entry === 'object' ? entry : null
  }
  const metricsOf = (id) => {
    const entry = stepOf(id)
    return entry && entry.metrics && typeof entry.metrics === 'object' ? entry.metrics : {}
  }
  const paramsOf = (id) => {
    const entry = stepOf(id)
    return entry && entry.params && typeof entry.params === 'object' ? entry.params : {}
  }
  const atOf = (id) => {
    const entry = stepOf(id)
    return entry && entry.at ? entry.at : null
  }

  const stateEntry = stepOf('state')
  const stateMetrics = metricsOf('state')
  const stateParams = paramsOf('state')
  const firstNumber = (...values) => {
    for (const value of values) {
      const number = contractNumber(value)
      if (number !== null) return number
    }
    return null
  }

  const nCells = contractNumber(d.n_cells)
  const nGenes = contractNumber(d.n_genes)
  const nClusters = firstNumber(stateMetrics.n_clusters, stateParams.n_clusters)
  const silhouette = contractNumber(stateMetrics.silhouette)
  const annotateMetrics = metricsOf('annotate')
  const grnEntry = stepOf('grn')
  const grnMetrics = metricsOf('grn')
  const grnParams = paramsOf('grn')
  const perturbEntry = stepOf('perturb')
  const perturbMetrics = metricsOf('perturb')
  const perturbParams = paramsOf('perturb')
  const priorMode = contractText(grnMetrics.prior || grnParams.prior)
  const lastStep = stepsDone.length ? stepsDone[stepsDone.length - 1] : null
  const lastEntry = lastStep ? stepOf(lastStep) : null
  const lastStatus = lastEntry
    ? contractText(lastEntry.status || (lastEntry.ok === false ? 'error' : 'ok'))
    : ''

  const modeling = {
    state: stateEntry ? {
      n_hvg: firstNumber(stateMetrics.n_hvg, stateParams.n_hvg),
      n_pcs: firstNumber(stateMetrics.n_pcs, stateParams.n_pcs),
      n_clusters: nClusters,
      silhouette,
      variance_explained: contractNumber(stateMetrics.variance_explained),
      backend: contractText(stateMetrics.backend),
      at: atOf('state'),
    } : null,
    grn: grnEntry ? {
      method: contractText(grnParams.method),
      alpha: firstNumber(grnMetrics.alpha, grnParams.alpha),
      prior: priorMode,
      n_edges: contractNumber(grnMetrics.n_edges),
      n_tfs: contractNumber(grnMetrics.n_tfs),
      n_targets: contractNumber(grnMetrics.n_targets),
      cv_r2: contractNumber(grnMetrics.cv_r2),
      // 与 vc_engine 的判据一致：只有挂用户先验才升到 moderate，否则 weak。
      directional_evidence: (priorMode === 'user' || priorMode === 'mixed') ? 'moderate' : 'weak',
      at: atOf('grn'),
    } : null,
    perturb: perturbEntry ? {
      gene: contractText(perturbMetrics.gene || perturbParams.gene),
      mode: contractText(perturbMetrics.mode || perturbParams.mode),
      strength: contractNumber(perturbParams.strength),
      cluster: contractText(perturbParams.cluster),
      n_prop: contractNumber(perturbParams.n_prop),
      state_shift: contractNumber(perturbMetrics.state_shift),
      source_state: contractText(perturbMetrics.source_state),
      target_state: contractText(perturbMetrics.target_state),
      confidence: contractText(perturbMetrics.confidence),
      n_affected: contractNumber(perturbMetrics.n_affected),
      at: atOf('perturb'),
    } : null,
  }

  const inference = lastEntry
    ? {
      last_step: lastStep,
      status: lastStatus,
      ok: lastStatus === 'ok',
      at: lastEntry.at || null,
      ms: contractNumber(lastEntry.ms),
      summary: contractText(lastEntry.summary).slice(0, 400),
      params: lastEntry.params && typeof lastEntry.params === 'object' ? lastEntry.params : {},
      metrics: lastEntry.metrics && typeof lastEntry.metrics === 'object' ? lastEntry.metrics : {},
    }
    : null

  // 依赖顺序扫描：重跑上游（例如 qc）后 stepOrder 会出现逆序，提示下游可能是旧参数版本。
  let ordered = true
  let cursor = -1
  for (const id of stepsDone) {
    const index = CONTRACT_STEP_ORDER.indexOf(id)
    if (index < 0) continue
    if (index < cursor) { ordered = false; break }
    cursor = index
  }

  const caveats = []
  const add = (code, level, text) => caveats.push({ code, level, text })
  // 这条永远在：本工作台的任何一个数字都不是实验结论。
  add('model_prediction', 'warn', '本工作台的 GRN 方向、虚拟敲除/过表达、状态转移与靶点排序都是模型推演（evidence_kind=model_prediction），不是实验结论；引用前需 Perturb-seq / CRISPR 等实验验证。')
  if (!stateEntry) add('no_state', 'warn', '还没有做状态表征：细胞状态空间未建立，任何状态/扰动/靶点结论都不成立。')
  if (nCells === null || nGenes === null) {
    add('no_data', 'warn', '细胞×基因矩阵的规模取不到：口径无法成立，先确认数据集已正确导入。')
  }
  if (stateEntry && nClusters !== null && nClusters < 2) {
    add('fewer_than_2_states', 'warn', `状态表征只得到 ${nClusters} 个状态：没有可比较的状态结构，扰动位移与状态转移不可解释。`)
  }
  if (silhouette !== null && silhouette < 0.15) {
    add('low_state_separation', 'warn', `状态分离度低（silhouette=${silhouette}）：状态边界可能是人为切分，扰动方向需谨慎解读。`)
  }
  if (nCells !== null && nCells < 1000) {
    add('few_cells', 'warn', `细胞数偏少（n=${nCells} < 1000）：隐空间与网络估计不稳定，结论置信度低。`)
  }
  if (!ordered) {
    add('stale_downstream_steps', 'warn', `步骤记录顺序为 ${stepsDone.join(' → ')}，与依赖顺序不一致：重跑上游后下游结果可能来自旧参数版本，引用前按当前参数重跑。`)
  }
  if (lastEntry && lastStatus !== 'ok') {
    add('last_step_failed', 'warn', `最近一步 ${lastStep} 状态 ${lastStatus}：该步结果不可用，先修数据/参数。`)
  }
  if (stateEntry && !grnEntry) add('no_grn', 'info', '还没有推断调控网络：虚拟扰动/筛选没有可传播的网络，靶点结论无从谈起。')
  if (grnEntry && !perturbEntry) add('no_perturb', 'info', '还没有做虚拟扰动：目前只有网络结构，没有「敲掉某基因后细胞往哪偏」的预测。')
  if (grnEntry && !stepOf('validate')) add('no_grn_validation', 'info', 'GRN 没有做留出校验：边的方向性证据弱，引用前建议先用真值或先验做校验。')
  if (stateEntry && !stepOf('annotate')) add('no_annotation', 'info', '状态只有簇编号、没有细胞类型注释：状态解释只能停在 marker 层面。')
  if (truth.has_grn_truth === true || truth.has_perturb_truth === true) {
    add('demo_truth', 'info', '当前数据集带模拟真值：GRN/扰动指标是相对模拟真值算的，不能当作真实生物学准确性。')
  }
  if (!contractText(d.sha256)) add('no_fingerprint', 'info', '没有记录原始文件内容指纹（sha256）：口径对应哪一份文件无法回查。')

  const contract = {
    schema: CONTRACT_SCHEMA,
    plugin,
    plugin_version: version,
    evidence_kind: EVIDENCE_KIND,
    dataset: {
      id: contractText(d.id),
      name: contractText(d.name),
      n_cells: nCells,
      n_genes: nGenes,
      n_tfs: contractNumber(d.n_tfs),
      source: contractText(d.source),
      sha256: contractText(d.sha256),
      bytes: contractNumber(d.bytes),
      created_at: d.createdAt || null,
      demo: d.demo === true,
    },
    design: {
      steps_done: stepsDone,
      n_clusters: nClusters,
      n_reference_states: contractNumber(annotateMetrics.n_states),
      n_assigned_states: contractNumber(annotateMetrics.assigned),
      n_unknown_states: contractNumber(annotateMetrics.unknown),
      annotation_method: contractText(annotateMetrics.method),
      n_grn_edges: contractNumber(grnMetrics.n_edges),
      n_modeled_tfs: contractNumber(grnMetrics.n_tfs),
      n_hvg: modeling.state ? modeling.state.n_hvg : null,
      n_pcs: modeling.state ? modeling.state.n_pcs : null,
      silhouette,
      variance_explained: modeling.state ? modeling.state.variance_explained : null,
    },
    inference,
    modeling,
    recent_steps: stepsDone.slice(-5).map((id) => ({
      step: id,
      status: (stepOf(id) || {}).status || '',
      at: (stepOf(id) || {}).at || null,
    })),
    identity: {
      sha256: contractText(d.sha256),
      created_at: d.createdAt || null,
      source: contractText(d.source),
      n_steps_done: stepsDone.length,
    },
    caveats,
    ok_to_cite: !caveats.some((item) => item.level === 'warn'),
    generated_at: new Date().toISOString(),
  }
  return contract
}

export function hostIsLoopback(req) {
  const raw = String((req && req.headers && req.headers.host) || '')
  if (!raw) return true
  const name = raw.startsWith('[') ? raw.slice(0, raw.indexOf(']') + 1) : raw.split(':')[0]
  return LOOPBACK_HOSTS.has(name.toLowerCase())
}

function sendJson(res, status, value) {
  const body = Buffer.from(JSON.stringify(value), 'utf8')
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': String(body.length),
  })
  res.end(body)
}

function opError(code, message, status = 400, details = null) {
  const body = { ok: false, code, error: message }
  if (details && typeof details === 'object') Object.assign(body, details)
  return { status, body }
}

function toFailure(error) {
  const code = (error && error.code) || 'internal'
  const message = String((error && error.message) || error)
  const details = error && error.details && typeof error.details === 'object' ? error.details : null
  if (code === 'prerequisite' || code === 'bad_request' || code === 'not_found' || code === 'unsupported' || code === 'ambiguous') {
    return opError(code, message, 400, details)
  }
  if (code === 'timeout') return opError(code, message, 504, details)
  if (code === 'busy') return opError(code, message, 429, details)
  return opError(code, message, 500, details)
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(Object.assign(new Error(`请求体超过上限（${Math.round(limit / 1024)} KB）`), { code: 'bad_request' }))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', (error) => reject(Object.assign(error, { code: 'bad_request' })))
  })
}

async function readJson(req) {
  const buffer = await readBody(req, MAX_JSON_BODY)
  if (!buffer.length) return {}
  try {
    const value = JSON.parse(buffer.toString('utf8'))
    return value && typeof value === 'object' ? value : {}
  } catch (error) {
    throw Object.assign(new Error(`请求 JSON 解析失败：${error.message}`), { code: 'bad_request' })
  }
}

function sanitizeName(name) {
  const base = String(name || 'upload')
  return path.basename(base).replace(/[^\w.\-\u4e00-\u9fa5]+/g, '_').slice(0, 160) || 'upload'
}

function nowIso() {
  return new Date().toISOString()
}

async function loadEngineMod() {
  return import(new URL('./engine.mjs', import.meta.url).href)
}

// ---------------------------------------------------------------------------
// 引擎
// ---------------------------------------------------------------------------

async function ensureEngine(runtime, { allowStart = true } = {}) {
  if (runtime.engine) {
    if (runtime.engine.running) return runtime.engine
    if (!allowStart) return runtime.engine
  } else {
    const mod = await loadEngineMod()
    const config = runtime.config && typeof runtime.config === 'object' ? runtime.config : {}
    runtime.engine = mod.createEngine({
      python: typeof config.python === 'string' && config.python.trim() ? config.python.trim() : undefined,
      workdir: typeof config.workdir === 'string' && config.workdir.trim() ? config.workdir.trim() : undefined,
      timeout: Number(config.timeoutMs) > 0 ? Number(config.timeoutMs) : undefined,
      config,
    })
    if (typeof runtime.ctx?.effect === 'function') {
      const engine = runtime.engine
      runtime.ctx.effect(() => () => { engine.dispose().catch(() => {}) }, 'dsh-virtualcell: engine')
    }
  }
  if (!allowStart) return runtime.engine
  const now = Date.now()
  if (runtime.engineFail && now - runtime.engineFail.at < 60_000) throw runtime.engineFail.error
  try {
    await runtime.engine.start()
    runtime.engineFail = null
  } catch (error) {
    runtime.engineFail = { at: now, error }
    throw error
  }
  return runtime.engine
}

export async function engineStatus(runtime) {
  let engine = runtime.engine
  if (!engine) {
    try {
      await ensureEngine(runtime, { allowStart: false })
      engine = runtime.engine
    } catch (error) {
      return { ready: false, mode: 'none', error: String(error && error.message ? error.message : error), python: null, packages: {} }
    }
  }
  if (!engine) return { ready: false, mode: 'none', error: 'Python 引擎未初始化', python: null, packages: {} }
  const status = typeof engine.status === 'function' ? engine.status() : { ready: false }
  if (!status.ready && !status.error && runtime.engineFail) {
    status.error = String(runtime.engineFail.error && runtime.engineFail.error.message ? runtime.engineFail.error.message : runtime.engineFail.error)
  }
  return status
}

async function engineRequest(runtime, cmd, args, timeout) {
  const engine = await ensureEngine(runtime)
  return engine.request(cmd, args, timeout ? { timeout } : {})
}

/** 按操作选择超时。 */
function timeoutFor(cmd, args) {
  if (cmd === 'run' && args && args.step === 'grn') return TIMEOUTS.run_grn
  if (cmd === 'run' && args && args.step === 'screen') return TIMEOUTS.run_screen
  return TIMEOUTS[cmd] || TIMEOUTS.default
}

// ---------------------------------------------------------------------------
// 活动日志（UI 右栏日志 + Agent 追溯）
// ---------------------------------------------------------------------------

export function logActivity(runtime, entry) {
  if (!runtime.activity) runtime.activity = []
  const item = {
    at: nowIso(),
    actor: entry.actor || 'system',
    action: String(entry.action || ''),
    dataset: entry.dataset || null,
    detail: entry.detail == null ? '' : String(entry.detail).slice(0, 400),
    ok: entry.ok !== false,
    ms: Number.isFinite(entry.ms) ? entry.ms : null,
  }
  runtime.activity.push(item)
  if (runtime.activity.length > ACTIVITY_LIMIT) runtime.activity = runtime.activity.slice(-ACTIVITY_LIMIT)
  return item
}

export function activityOf(runtime, since = 0) {
  const list = runtime.activity || []
  return { ok: true, entries: list.slice(Math.max(0, Number(since) || 0)) }
}

// ---------------------------------------------------------------------------
// 操作（HTTP 与 Agent 共用）
// ---------------------------------------------------------------------------

async function opState(runtime) {
  const state = await engineRequest(runtime, 'state', {}, 60_000)
  // 口径卡数据源：随数据集一起回传，UI 与 Agent 看到的是同一份
  const datasets = (Array.isArray(state.datasets) ? state.datasets : []).map((item) => ({
    ...item,
    stats_contract: buildWorkbenchContract(item),
  }))
  const activeDataset = datasets.find((item) => item && item.id === state.active) || null
  const decorated = {
    ...state,
    datasets,
    stats_contract: activeDataset ? activeDataset.stats_contract : null,
  }
  runtime.lastState = decorated
  return decorated
}

async function opHealth(runtime) {
  const status = await engineStatus(runtime)
  if (!status.ready) return { ok: true, ready: false, engine: status }
  try {
    const info = await engineRequest(runtime, 'ping', {}, 60_000)
    return { ok: true, ready: true, engine: { ...status, ...info } }
  } catch (error) {
    return { ok: true, ready: false, engine: { ...status, error: String(error && error.message ? error.message : error) } }
  }
}

async function opRun(runtime, args) {
  const step = String(args.step || '')
  if (!step) throw Object.assign(new Error('run 需要 step'), { code: 'bad_request' })
  const timeout = args.step === 'grn' ? TIMEOUTS.run_grn
    : args.step === 'screen' ? TIMEOUTS.run_screen
      : TIMEOUTS.run
  const started = Date.now()
  try {
    const result = await engineRequest(runtime, 'run', args, timeout)
    logActivity(runtime, { actor: args.actor || 'user', action: `run:${step}`, dataset: args.dataset, detail: result && result.summary, ok: true, ms: Date.now() - started })
    return result
  } catch (error) {
    logActivity(runtime, { actor: args.actor || 'user', action: `run:${step}`, dataset: args.dataset, detail: error && error.message, ok: false, ms: Date.now() - started })
    throw error
  }
}

async function opUpload(runtime, req, res) {
  const filename = sanitizeName(req.headers['x-dsh-filename'] || 'upload.h5ad')
  const engine = await ensureEngine(runtime)
  const dest = path.join(engine.dirs.uploads, `${Date.now().toString(36)}-${randomUUID().slice(0, 8)}-${filename}`)
  mkdirSync(engine.dirs.uploads, { recursive: true })
  let size = 0
  await pipeline(
    req,
    async function* count(source) {
      for await (const chunk of source) {
        size += chunk.length
        if (size > MAX_UPLOAD_BYTES) throw Object.assign(new Error('文件超过 8 GiB 上限'), { code: 'bad_request' })
        yield chunk
      }
    },
    createWriteStream(dest),
  )
  const result = await engineRequest(runtime, 'open', {
    path: dest,
    name: filename,
    data_format: req.headers['x-dsh-format'] || undefined,
    transpose: String(req.headers['x-dsh-transpose'] || '') === '1',
    actor: 'user',
  }, TIMEOUTS.open)
  logActivity(runtime, { actor: 'user', action: 'open:upload', detail: `${filename} (${size} B)`, ok: true })
  return { ok: true, ...result, uploaded: { name: filename, bytes: size } }
}

// ---------------------------------------------------------------------------
// 图片服务 / 文件下载
// ---------------------------------------------------------------------------

const REL_SEGMENT = /^[\w.\-\u4e00-\u9fa5]{1,120}$/

/** `/virtualcell/figure?id=<ds>/<run>/<file>.png` → 绝对路径（严格限制在工作目录内）。
 *  兼容只有文件名的旧 id：在 datasets/<ds>/runs/<run>/figures/ 里找唯一同名 PNG。 */
export function resolveFigurePath(engine, id) {
  const text = String(id || '').trim()
  if (!text) return null
  const parts = text.split('/')
  if (parts.length === 3) {
    if (!parts.every((part) => REL_SEGMENT.test(part))) return null
    if (!/\.png$/i.test(parts[2])) return null
    const base = path.resolve(engine.workdir, 'datasets', parts[0], 'runs', parts[1], 'figures')
    const file = path.resolve(base, parts[2])
    if (!file.startsWith(base + path.sep)) return null
    return file
  }
  // 单段：只允许 png 文件名，且必须在工作目录内唯一命中
  if (parts.length !== 1 || !REL_SEGMENT.test(parts[0]) || !/\.png$/i.test(parts[0])) return null
  const name = parts[0]
  const found = []
  const datasetsDir = path.resolve(engine.workdir, 'datasets')
  try {
    for (const dsid of readdirSync(datasetsDir)) {
      if (!REL_SEGMENT.test(dsid)) continue
      const runsDir = path.join(datasetsDir, dsid, 'runs')
      let runs = []
      try { runs = readdirSync(runsDir) } catch { continue }
      for (const runId of runs) {
        if (!REL_SEGMENT.test(runId)) continue
        const file = path.join(runsDir, runId, 'figures', name)
        const base = path.join(runsDir, runId, 'figures')
        try {
          const stat = statSync(file)
          if (stat.isFile() && path.resolve(file).startsWith(base + path.sep)) found.push(file)
        } catch { /* 不存在 */ }
      }
    }
  } catch { /* 工作目录还没有 datasets */ }
  return found.length === 1 ? found[0] : (found.length > 1 ? found[found.length - 1] : null)
}

async function serveFigure(runtime, url, res) {
  const engine = await ensureEngine(runtime)
  const file = resolveFigurePath(engine, url.searchParams.get('id'))
  if (!file) throw Object.assign(new Error('图片 id 非法'), { code: 'bad_request' })
  let stat
  try { stat = statSync(file) } catch {
    throw Object.assign(new Error('图片不存在或已被清理，请重新出图'), { code: 'not_found' })
  }
  res.writeHead(200, {
    'content-type': 'image/png',
    'cache-control': 'private, max-age=3600',
    'content-length': String(stat.size),
  })
  await pipeline(createReadStream(file), res)
}

/** 导出产物下载：只允许工作目录 exports/ 下的文件。 */
async function serveDownload(runtime, url, res) {
  const engine = await ensureEngine(runtime)
  const id = String(url.searchParams.get('id') || '')
  if (!REL_SEGMENT.test(path.basename(id))) throw Object.assign(new Error('文件名非法'), { code: 'bad_request' })
  const base = path.resolve(engine.workdir, 'exports')
  const file = path.resolve(base, path.basename(id))
  if (!file.startsWith(base + path.sep)) throw Object.assign(new Error('路径非法'), { code: 'bad_request' })
  let stat
  try { stat = statSync(file) } catch {
    throw Object.assign(new Error('导出文件不存在'), { code: 'not_found' })
  }
  const name = path.basename(file)
  res.writeHead(200, {
    'content-type': 'application/octet-stream',
    'content-disposition': `attachment; filename*=UTF-8''${encodeURIComponent(name)}`,
    'content-length': String(stat.size),
    'cache-control': 'no-store',
  })
  await pipeline(createReadStream(file), res)
}

// ---------------------------------------------------------------------------
// 路由
// ---------------------------------------------------------------------------

export async function handle(runtime, req, res) {
  ensureToolRegistered(runtime)
  ensurePromptSection(runtime)
  if (!hostIsLoopback(req)) {
    return sendJson(res, 403, { ok: false, code: 'forbidden', error: '拒绝：只接受本机（回环地址）请求' })
  }
  const url = new URL(req.url || '/', 'http://127.0.0.1')
  if (!url.pathname.startsWith(PREFIX)) {
    return sendJson(res, 404, { ok: false, code: 'not_found', error: '未知路由' })
  }
  const route = url.pathname.slice(PREFIX.length) || '/'
  const method = (req.method || 'GET').toUpperCase()
  const key = `${method} ${route}`
  try {
    switch (key) {
      case 'GET /health':
        return sendJson(res, 200, await opHealth(runtime))
      case 'GET /state':
        return sendJson(res, 200, await opState(runtime))
      case 'GET /formats':
        return sendJson(res, 200, await engineRequest(runtime, 'formats', {}, 60_000))
      case 'GET /steps':
        return sendJson(res, 200, await engineRequest(runtime, 'steps', {}, 60_000))
      case 'GET /runs':
        return sendJson(res, 200, await engineRequest(runtime, 'runs', {
          dataset: url.searchParams.get('dataset') || undefined,
          limit: Number(url.searchParams.get('limit') || 20),
          offset: Number(url.searchParams.get('offset') || 0),
        }, 60_000))
      case 'GET /activity':
        return sendJson(res, 200, activityOf(runtime, Number(url.searchParams.get('since') || 0)))
      case 'GET /figure':
        return await serveFigure(runtime, url, res)
      case 'GET /download':
        return await serveDownload(runtime, url, res)
      case 'POST /dataset/open':
        return sendJson(res, 200, await engineRequest(runtime, 'open', { ...(await readJson(req)), actor: 'user' }, TIMEOUTS.open))
      case 'POST /dataset/demo':
        return sendJson(res, 200, await engineRequest(runtime, 'demo', { ...(await readJson(req)), actor: 'user' }, TIMEOUTS.demo))
      case 'POST /dataset/upload':
        return await opUpload(runtime, req, res)
      case 'POST /dataset/select':
        return sendJson(res, 200, await engineRequest(runtime, 'select', await readJson(req), 30_000))
      case 'POST /dataset/close':
        return sendJson(res, 200, await engineRequest(runtime, 'close', await readJson(req), 30_000))
      case 'POST /inspect':
        return sendJson(res, 200, await engineRequest(runtime, 'inspect', await readJson(req), 3 * 60_000))
      case 'POST /preview':
        return sendJson(res, 200, await engineRequest(runtime, 'preview', await readJson(req), 60_000))
      case 'POST /suggest':
        return sendJson(res, 200, await engineRequest(runtime, 'suggest', await readJson(req), 60_000))
      case 'POST /run':
        return sendJson(res, 200, await opRun(runtime, { ...(await readJson(req)) }))
      case 'POST /sweep':
        return sendJson(res, 200, await engineRequest(runtime, 'sweep', { ...(await readJson(req)), actor: 'user' }, TIMEOUTS.sweep))
      case 'POST /plot':
        return sendJson(res, 200, await engineRequest(runtime, 'plot', await readJson(req), TIMEOUTS.plot))
      case 'POST /attach':
        return sendJson(res, 200, await engineRequest(runtime, 'attach', { ...(await readJson(req)), actor: 'user' }, TIMEOUTS.attach))
      case 'POST /export':
        return sendJson(res, 200, await engineRequest(runtime, 'export', { ...(await readJson(req)), actor: 'user' }, TIMEOUTS.export))
      case 'POST /audit':
        return sendJson(res, 200, await engineRequest(runtime, 'audit', await readJson(req), 2 * 60_000))
      case 'POST /insight':
        return sendJson(res, 200, await engineRequest(runtime, 'insight', await readJson(req), 2 * 60_000))
      case 'POST /recipe':
        return sendJson(res, 200, await engineRequest(runtime, 'recipe', await readJson(req), 30_000))
      case 'POST /recipe/run':
        return sendJson(res, 200, await engineRequest(runtime, 'recipe_run', { ...(await readJson(req)), actor: 'user' }, TIMEOUTS.recipe_run))
      case 'POST /reset':
        return sendJson(res, 200, await engineRequest(runtime, 'reset', await readJson(req), 30_000))
      case 'POST /engine/restart': {
        const engine = await ensureEngine(runtime)
        await engine.dispose().catch(() => {})
        runtime.engine = null
        const status = await engineStatus(runtime)
        return sendJson(res, 200, { ok: true, engine: status })
      }
      default:
        return sendJson(res, 404, { ok: false, code: 'not_found', error: `未知路由 ${key}` })
    }
  } catch (error) {
    const failure = toFailure(error)
    if (failure && failure.body) return sendJson(res, failure.status, failure.body)
    return sendJson(res, 500, { ok: false, code: 'internal', error: String(error && error.message ? error.message : error) })
  }
}

// ---------------------------------------------------------------------------
// Agent 工具
// ---------------------------------------------------------------------------

const ACTIONS = ['status', 'formats', 'steps', 'demo', 'open', 'inspect', 'preview', 'suggest', 'run',
  'perturb', 'screen', 'sweep', 'plot', 'attach', 'export', 'audit', 'insight', 'recipe', 'recipe_run', 'runs', 'reset']
const STEP_IDS = ['qc', 'state', 'annotate', 'grn', 'validate', 'perturb', 'screen', 'response', 'transition', 'insight']
const PLOT_KINDS = ['qc_violin', 'state_embedding', 'state_embedding_perturb', 'cluster_bar', 'grn_hub_bar',
  'grn_network', 'grn_heatmap', 'perturb_waterfall', 'perturb_field', 'perturb_heatmap', 'screen_rank',
  'response_scatter', 'transition_matrix', 'transition_graph', 'validate_roc', 'compare_bar']
const EXPORT_KINDS = ['state_csv', 'clusters_csv', 'markers_csv', 'grn_csv', 'perturb_csv', 'screen_csv',
  'response_csv', 'transition_csv', 'summary_json', 'protocol', 'methods', 'report', 'script', 'run_zip']
const ATTACH_KINDS = ['perturb_table', 'gene_set', 'grn_prior', 'drug_signature']

function buildToolDefinition(hostRuntime) {
  return {
    name: 'virtualcell',
    description:
      '虚拟细胞（AI Virtual Cell / AIVC）工作台（本机 Host 插件，带可视化页面 + Python 计算引擎）。把单细胞表达谱变成可被扰动推演的模型：' +
      '导入数据 → 质控 → 状态表征（隐空间/聚类/标记基因）→ 状态注释 → 基因调控网络（GRN）→ 网络校验 → 虚拟扰动（敲除/敲低/过表达）→ 虚拟筛选 → 扰动响应预测 → 状态转移 → 智能分析。' +
      '每个结果都保留原始数据（sha256 + 不可变 runs 目录）、出 PNG 图、可导出 protocol/methods/report/script。' +
      'action: status=看工作台状态与建议下一步; formats=支持的输入格式与可选模型适配器; steps=流程步骤与参数 schema; demo=载入带真值的示例数据; ' +
      'open=导入本地/URL 数据（path 或 url；data_format 可指定 h5ad/10x_h5/10x_mtx/loom/matrix_text/geo_series/archive）; inspect=只探测格式; preview=看基因/细胞列; ' +
      'suggest=某一步的参数推荐（含推荐值+为什么，参数辅助调整的核心）; run=跑步骤（step 必填：qc/state/annotate/grn/validate/perturb/screen/response/transition/insight；params 可选）; ' +
      'perturb=直接做一次虚拟扰动（gene+mode+strength，等价 run step=perturb）; screen=虚拟筛选候选 TF; sweep=参数扫描（step+param+values）; ' +
      'plot=出图（kind 见 PLOT_KINDS）; attach=挂载扰动表/基因集/GRN 先验/药物签名; export=导出（what 见 EXPORT_KINDS）; audit=确定性自检; insight=智能分析（结论+靶点+局限）; ' +
      'recipe=查看预设流程定义（quick/standard/full）; recipe_run=直接执行预设流程（id=quick/standard/full）; runs=运行记录; reset=清空工作台。' +
      '用户提到虚拟细胞、AIVC、虚拟敲除、in-silico perturbation、GRN、扰动响应、Perturb-seq 预测、细胞状态转移、靶点筛选时优先用本工具驱动这个工作台（与页面按钮同一套实现），不要自己写脚本重跑。' +
      '给结论前建议先 audit + insight；图的绝对路径可用 read_image 查看。',
    parameters: {
      action: { type: 'string', enum: ACTIONS, description: '要执行的操作' },
      dataset: { type: 'string', description: '数据集 id（如 ds1）；省略则用当前活动数据集' },
      path: { type: 'string', description: '本地文件/目录绝对路径（open/inspect/attach）' },
      url: { type: 'string', description: 'http(s) 直链（open）' },
      data_format: {
        type: 'string',
        enum: ['auto', 'h5ad', '10x_h5', '10x_mtx', 'loom', 'matrix_text', 'geo_series', 'archive'],
        description: '强制输入格式（默认 auto）',
      },
      member: { type: 'string', description: '压缩包内相对路径（或 RData 里的对象名，见 object）' },
      object: { type: 'string', description: 'RData 里有多个对象时指定对象名（.rds/.RData 导入用）' },
      transpose: { type: 'boolean', description: '矩阵方向（true=基因×细胞，自动转置）' },
      gene_col: { type: 'string', description: '文本矩阵里基因名的列' },
      sheet: { type: 'string', description: 'Excel sheet 名' },
      name: { type: 'string', description: '数据集显示名' },
      step: { type: 'string', enum: STEP_IDS, description: 'run/sweep 的步骤' },
      params: { type: 'object', description: '步骤参数（各步参数见 action=steps；如 state: n_hvg/n_pcs/cluster/resolution；grn: alpha/prior/top_targets；perturb: gene/mode/strength）', additionalProperties: true },
      gene: { type: 'string', description: 'action=perturb 的扰动基因（留空自动选 hub TF）' },
      mode: { type: 'string', enum: ['knockout', 'knockdown', 'overexpression'], description: 'action=perturb/screen 的扰动方式' },
      strength: { type: 'number', description: '扰动强度（knockdown/overexpression，默认 1.0）' },
      cluster: { type: 'string', description: '作用范围（all 或簇名）' },
      genes: { type: 'array', description: 'action=screen 的候选基因列表', items: { type: 'string' } },
      param: { type: 'string', description: 'action=sweep 要扫描的参数名' },
      values: { type: 'array', description: 'action=sweep 的取值列表（省略=自动）', items: { type: 'number' } },
      kind: { type: 'string', description: 'action=plot 的图类型（或 attach 的附件类型）', enum: [...PLOT_KINDS, ...ATTACH_KINDS] },
      what: { type: 'string', enum: EXPORT_KINDS, description: 'action=export 的导出类型' },
      out_path: { type: 'string', description: 'action=export 的目标文件绝对路径（可选）' },
      run: { type: 'string', description: 'action=export what=run_zip 时的运行 id' },
      id: { type: 'string', description: 'action=recipe 的预设 id：quick/standard/full' },
      recipe: { type: 'string', description: 'action=recipe 的预设 id：quick/standard/full' },
      seed: { type: 'number', description: 'action=demo 的随机种子' },
      n_cells: { type: 'number', description: 'action=demo 的细胞数' },
      n_genes: { type: 'number', description: 'action=demo 的基因数' },
      limit: { type: 'number', description: 'action=runs 的条数' },
      offset: { type: 'number', description: 'action=runs 的偏移' },
    },
    output: {
      schema: { type: 'object' },
      render: (_args, value) => {
        const text = value && typeof value.message === 'string' ? value.message : JSON.stringify(value)
        return [{ type: 'text', text: String(text) }]
      },
    },
    execute: async (args) => {
      return sanitizeValue(await runTool(hostRuntime, args || {}))
    },
  }
}

/** 把大对象裁剪到 Agent 可读的规模（图/表路径保留，原始大数组不进上下文）。 */
function sanitizeValue(value) {
  const json = JSON.stringify(value, (_key, item) => {
    if (typeof item === 'number' && !Number.isFinite(item)) return null
    if (Array.isArray(item) && item.length > 500) return item.slice(0, 500)
    if (item && typeof item === 'object' && item.type === 'Buffer') return undefined
    return item
  })
  return json === undefined ? value : JSON.parse(json)
}

const TOOL_SCHEMA_VERSION = 2

export function ensureToolRegistered(runtime) {
  if (runtime.toolRegistered && runtime.toolSchemaVersion === TOOL_SCHEMA_VERSION) return runtime.toolRegistered
  try {
    const ctx = runtime.ctx
    const tools = ctx && typeof ctx.get === 'function' ? ctx.get('tools') : ctx && ctx.tools
    if (!tools || typeof tools.register !== 'function') {
      runtime.toolRegistered = false
      return false
    }
    if (runtime.toolRegistered) {
      if (typeof runtime.toolRegistered !== 'function') return runtime.toolRegistered
      try { runtime.toolRegistered() } catch { /* ignore */ }
      runtime.toolRegistered = false
    }
    const dispose = tools.register(buildToolDefinition(runtime))
    runtime.toolRegistered = typeof dispose === 'function' ? dispose : true
    runtime.toolSchemaVersion = TOOL_SCHEMA_VERSION
    return true
  } catch (error) {
    runtime.toolRegistered = false
    try { console.warn('[dsh-virtualcell] agent tool unavailable:', error && error.message ? error.message : error) } catch { /* ignore */ }
    return false
  }
}

// ---------------------------------------------------------------------------
// tool → cmd 映射
// ---------------------------------------------------------------------------

export async function runTool(runtime, args) {
  const action = String(args.action || 'status')
  const dataset = args.dataset || undefined
  const withDataset = (payload) => (dataset ? { dataset, ...payload } : { ...payload })

  switch (action) {
    case 'status': {
      const state = await opState(runtime)
      return summarizeState(state)
    }
    case 'formats':
      return engineRequest(runtime, 'formats', {}, 60_000)
    case 'steps':
      return engineRequest(runtime, 'steps', {}, 60_000)
    case 'demo': {
      const result = await engineRequest(runtime, 'demo', withDataset({
        seed: args.seed, n_cells: args.n_cells, n_genes: args.n_genes, actor: 'agent',
      }), TIMEOUTS.demo)
      return { message: result.summary, dataset: result.dataset }
    }
    case 'open': {
      const result = await engineRequest(runtime, 'open', withDataset({
        path: args.path, url: args.url, name: args.name, data_format: args.data_format,
        member: args.member, object: args.object, transpose: args.transpose, gene_col: args.gene_col,
        sheet: args.sheet, actor: 'agent',
      }), TIMEOUTS.open)
      return { message: result.summary, dataset: result.dataset }
    }
    case 'inspect':
      return engineRequest(runtime, 'inspect', {
        path: args.path, url: args.url, data_format: args.data_format, member: args.member,
        object: args.object, transpose: args.transpose, sheet: args.sheet, gene_col: args.gene_col,
      }, 3 * 60_000)
    case 'preview':
      return engineRequest(runtime, 'preview', withDataset({}), 60_000)
    case 'suggest':
      return engineRequest(runtime, 'suggest', withDataset({ step: args.step, params: args.params }), 60_000)
    case 'run': {
      const result = await opRun(runtime, withDataset({
        step: args.step, params: args.params, actor: 'agent',
      }))
      return { message: result.summary || `${args.step} 完成`, run: result.run, result }
    }
    case 'perturb': {
      const result = await opRun(runtime, withDataset({
        step: 'perturb',
        params: { gene: args.gene, mode: args.mode, strength: args.strength, cluster: args.cluster },
        actor: 'agent',
      }))
      return { message: result.summary || `虚拟扰动完成`, run: result.run, result }
    }
    case 'screen':
      return engineRequest(runtime, 'run', withDataset({
        step: 'screen',
        params: { genes: args.genes, mode: args.mode, top_n: args.limit },
        actor: 'agent',
      }), TIMEOUTS.run_screen)
    case 'sweep':
      return engineRequest(runtime, 'sweep', withDataset({
        step: args.step, param: args.param, values: args.values, params: args.params,
      }), TIMEOUTS.sweep)
    case 'plot':
      return engineRequest(runtime, 'plot', withDataset({ kind: args.kind, params: args.params }), TIMEOUTS.plot)
    case 'attach':
      return engineRequest(runtime, 'attach', withDataset({
        kind: args.kind && ATTACH_KINDS.includes(args.kind) ? args.kind : 'perturb_table',
        path: args.path, name: args.name,
      }), TIMEOUTS.attach)
    case 'export':
      return engineRequest(runtime, 'export', withDataset({
        what: args.what, run: args.run, out_path: args.out_path,
      }), TIMEOUTS.export)
    case 'audit':
      return engineRequest(runtime, 'audit', withDataset({}), 2 * 60_000)
    case 'insight':
      return engineRequest(runtime, 'insight', withDataset({ params: args.params }), 2 * 60_000)
    case 'recipe': {
      const id = args.recipe || args.id || 'quick'
      return engineRequest(runtime, 'recipe', { id }, 30_000)
    }
    case 'recipe_run': {
      const id = args.recipe || args.id || 'quick'
      return engineRequest(runtime, 'recipe_run', withDataset({ id }), TIMEOUTS.recipe_run)
    }
    case 'runs':
      return engineRequest(runtime, 'runs', withDataset({
        limit: args.limit || 20, offset: args.offset || 0,
      }), 60_000)
    case 'reset':
      return engineRequest(runtime, 'reset', {}, 30_000)
    default:
      throw Object.assign(new Error(`未知 action：${action}`), { code: 'bad_request' })
  }
}

/** status 的 Agent 友好摘要（不把整份 state 塞进上下文）。 */
function summarizeState(state) {
  const active = (state.datasets || []).find((item) => item.id === state.active) || null
  const lines = []
  lines.push(`虚拟细胞工作台：${state.datasets ? state.datasets.length : 0} 个数据集；引擎 ${state.engine ? state.engine.backend : '?'}（Python ${state.engine ? state.engine.pythonVersion : '?'}）`)
  if (active) {
    const done = (active.steps_done || []).map((id) => ({ qc: '质控', state: '状态表征', annotate: '状态注释', grn: '调控网络', validate: '网络校验', perturb: '虚拟扰动', screen: '虚拟筛选', response: '响应预测', transition: '状态转移', insight: '智能分析' }[id] || id))
    lines.push(`当前数据集：${active.name}（${active.n_cells} 细胞 × ${active.n_genes} 基因，${active.n_tfs || 0} 个 TF）`)
    lines.push(`已完成：${done.length ? done.join('、') : '无'}`)
    if (active.steps_summary) {
      const last = (state.results && state.results[Object.keys(state.results)[Object.keys(state.results).length - 1]]) || null
      if (last && last.summary) lines.push(`最近结果：${last.summary}`)
    }
    const contract = active.stats_contract || state.stats_contract || null
    if (contract) {
      const warns = (contract.caveats || []).filter((item) => item.level === 'warn')
      lines.push(`统计口径：evidence_kind=${contract.evidence_kind || '?'}（模型推演，不是实验结论）；`
        + (warns.length ? `${warns.length} 条引用前必须处理（${warns.map((item) => item.code).join('、')}）` : '没有 warn 项'))
    }
  } else {
    lines.push('当前没有数据集：可 action=demo 载入带真值的示例数据，或 action=open 导入自己的 h5ad。')
  }
  if (state.suggestedStep) {
    lines.push(`建议下一步：${state.suggestedStep}`)
  }
  if (state.busy) lines.push(`正在运行：${state.busy.message || state.busy.step}`)
  const adapters = (state.engine && state.engine.adapters) || []
  const missing = adapters.filter((item) => !item.available).map((item) => item.label)
  if (missing.length) lines.push(`可选后端未安装（不影响内置引擎）：${missing.join('、')}`)
  return {
    message: lines.join('\n'),
    active: state.active,
    stepsDone: state.stepsDone,
    suggestedStep: state.suggestedStep,
    datasets: state.datasets,
    stats_contract: state.stats_contract || null,
    engine: state.engine ? { backend: state.engine.backend, pythonVersion: state.engine.pythonVersion, packages: state.engine.packages } : null,
    results: state.results,
  }
}

// ---------------------------------------------------------------------------
// Prompt section
// ---------------------------------------------------------------------------

export function renderPromptSection(runtime) {
  const state = runtime.lastState
  if (!state || !state.datasets || state.datasets.length === 0) return ''
  const active = state.datasets.find((item) => item.id === state.active)
  const lines = ['虚拟细胞工作台（Host 插件 dsh-virtualcell）已在使用中。']
  if (active) {
    const done = (active.steps_done || []).length ? active.steps_done.join('、') : '无'
    lines.push(`当前数据集：${active.name || active.id}（${active.n_cells} 细胞 × ${active.n_genes} 基因），已完成步骤：${done}。`)
  }
  lines.push(
    '用户提到虚拟细胞/AIVC、虚拟敲除或过表达、GRN、扰动响应预测、Perturb-seq、状态转移、靶点筛选时，' +
    '优先调用 virtualcell 工具操作这个工作台（与页面按钮同一套实现），不要另写脚本重跑；' +
    '给结论前先 audit 与 insight，并把「模型推演不等于实验结论」写清楚。图的绝对路径可用 read_image 查看。',
  )
  return lines.join('\n')
}

export function ensurePromptSection(runtime) {
  if (runtime.promptRegistered) return runtime.promptRegistered
  try {
    const ctx = runtime.ctx
    const systemPrompt = ctx && typeof ctx.get === 'function' ? ctx.get('systemPrompt') : ctx && ctx.systemPrompt
    if (!systemPrompt || typeof systemPrompt.section !== 'function') {
      runtime.promptRegistered = false
      return false
    }
    const dispose = systemPrompt.section({
      name: 'dsh-virtualcell-workbench',
      order: 780,
      text: () => renderPromptSection(runtime),
    })
    runtime.promptRegistered = typeof dispose === 'function' ? dispose : true
    return true
  } catch (error) {
    runtime.promptRegistered = false
    try { console.warn('[dsh-virtualcell] prompt section unavailable:', error && error.message ? error.message : error) } catch { /* ignore */ }
    return false
  }
}

// 供单测直接 import
export const __internals = {
  resolveFigurePath, toFailure, timeoutFor, summarizeState, buildToolDefinition, sanitizeValue,
  REGISTRY: { PREFIX, ACTIONS, STEP_IDS, PLOT_KINDS, EXPORT_KINDS, ATTACH_KINDS },
}
