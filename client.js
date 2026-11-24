/**
 * dsh-virtualcell — 虚拟细胞工作台 · 浏览器端整页 UI（Client 半边）
 *
 * 形态：window.__ModuleLoader__.load({ id:'dsh-virtualcell', factory(require){...} })，
 * 只 require('react')，用 React.createElement（不写 JSX、不 import 宿主 Client 包）。
 *
 * 数据面：全部走同源相对路径 /virtualcell/*（见 CONTRACT.md §1 / §3 / §4 / §8）。
 * 两层：纯函数层（顶部到 VC_TEST，不碰 DOM、不 require react，node 下挂
 * globalThis.__VC_TEST__ 供单测）+ 浏览器层（组件 / 样式 / 槽注册，factory 内装配）。
 * 约定：类名一律 vc- 前缀；主题色只用 --dsw-alias-*；数据色用 PALETTE。
 */
;(function () {
  'use strict'

  // ===== 常量 =====
  var NS = 'dsh-virtualcell'
  var API = '/virtualcell'
  var POLL_MS = 1500
  var API_TIMEOUT_MS = 60000
  var HEAVY_API_TIMEOUT_MS = 300000
  var HEAVY_TIMEOUT_MS = 300000
  var MAX_LOG = 50
  var MAX_TABLE_ROWS = 200
  var TAB_KEYS = ['state', 'network', 'perturb', 'screen', 'response', 'transition', 'insight', 'records']
  var STEP_ORDER = ['qc', 'state', 'annotate', 'grn', 'validate', 'perturb', 'screen', 'response', 'transition', 'insight']
  var STEP_TITLES = {
    qc: '质控', state: '状态表征', annotate: '状态注释', grn: '调控网络', validate: '网络校验',
    perturb: '虚拟扰动', screen: '虚拟筛选', response: '响应预测', transition: '状态转移', insight: '智能分析',
  }
  /** 分组/状态调色板（数据色，不参与主题）。 */
  var PALETTE = [
    '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7',
    '#9c755f', '#bab0ac', '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b',
    '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
  ]
  /** 连续色标：低 → 高（基因表达 / Δ 数值）。 */
  var CONTINUOUS_STOPS = ['#3b4cc0', '#7b9ff9', '#dddddd', '#f6a582', '#b40426']

  // 浏览器运行时槽位：factory() 里注入 react，apply() 里注入 locale 翻译器。
  var React = null
  var translator = null

  // ===== 小工具 =====
  function isNil(value) { return value === null || value === undefined }

  function clamp(value, low, high) {
    var number = Number(value)
    if (!isFinite(number)) number = Number(low) || 0
    return Math.min(Number(high), Math.max(Number(low), number))
  }

  function asNumber(value) {
    if (isNil(value) || value === '') return null
    var number = Number(value)
    return isFinite(number) ? number : null
  }

  function plainText(value, limit) {
    if (isNil(value)) return ''
    if (typeof value === 'string') return limit ? value.slice(0, limit) : value
    if (typeof value === 'number' || typeof value === 'boolean') return String(value)
    try { return JSON.stringify(value).slice(0, limit || 2000) } catch (ignored) { return String(value) }
  }

  function trimFixed(number, digits) {
    var text = Number(number).toFixed(digits)
    if (text.indexOf('.') >= 0) text = text.replace(/0+$/, '').replace(/\.$/, '')
    return text
  }

  // ===== 数字 / 时间格式化（千分位；空值一律 —） =====
  function fmtNumber(value, digits) {
    if (isNil(value) || value === '') return '—'
    var number = Number(value)
    if (!isFinite(number)) return '—'
    var fixed = isFinite(Number(digits)) ? clamp(digits, 0, 6) : 2
    var abs = Math.abs(number)
    if (abs >= 1e9) return trimFixed(number / 1e9, fixed) + 'B'
    if (abs >= 1e6) return trimFixed(number / 1e6, fixed) + 'M'
    if (abs >= 1e4) return trimFixed(number / 1e3, fixed) + 'K'
    var parts = trimFixed(number, fixed).split('.')
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',')
    return parts.join('.')
  }

  function fmtInt(value) {
    var number = asNumber(value)
    if (number === null) return '—'
    return String(Math.round(number)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  }

  /** 输入可以是 0–1 的比例，也可以是已乘 100 的百分数。 */
  function fmtPct(value, digits) {
    var number = asNumber(value)
    if (number === null) return '—'
    var percent = Math.abs(number) <= 1.5 ? number * 100 : number
    return trimFixed(percent, isFinite(Number(digits)) ? clamp(digits, 0, 3) : 1) + '%'
  }

  function fmtSigned(value, digits) {
    var number = asNumber(value)
    if (number === null) return '—'
    return (number > 0 ? '+' : '') + fmtNumber(number, digits)
  }

  function formatDuration(ms) {
    var number = asNumber(ms)
    if (number === null) return '—'
    if (number < 1000) return Math.round(number) + ' ms'
    if (number < 60000) return trimFixed(number / 1000, 1) + ' s'
    return Math.floor(number / 60000) + ' 分 ' + Math.round((number % 60000) / 1000) + ' 秒'
  }

  function formatClock(value) {
    if (!value) return '—'
    var date = value instanceof Date ? value : new Date(value)
    if (isNaN(date.getTime())) return plainText(value, 40)
    function pad(number) { return number < 10 ? '0' + number : String(number) }
    return pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' +
      pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds())
  }

  function formatBytes(bytes) {
    var number = asNumber(bytes)
    if (number === null) return '—'
    if (number < 1024) return number + ' B'
    if (number < 1024 * 1024) return trimFixed(number / 1024, 1) + ' KB'
    if (number < 1024 * 1024 * 1024) return trimFixed(number / 1024 / 1024, 1) + ' MB'
    return trimFixed(number / 1024 / 1024 / 1024, 2) + ' GB'
  }

  // ===== 配色 =====
  function hexToRgb(hex) {
    var text = String(hex || '').replace('#', '')
    if (text.length === 3) text = text.charAt(0) + text.charAt(0) + text.charAt(1) + text.charAt(1) + text.charAt(2) + text.charAt(2)
    if (!/^[0-9a-fA-F]{6}$/.test(text)) return { r: 128, g: 128, b: 128 }
    return { r: parseInt(text.slice(0, 2), 16), g: parseInt(text.slice(2, 4), 16), b: parseInt(text.slice(4, 6), 16) }
  }

  function rgbToHex(rgb) {
    function hex(number) {
      var text = Math.round(clamp(number, 0, 255)).toString(16)
      return text.length < 2 ? '0' + text : text
    }
    return '#' + hex(rgb.r) + hex(rgb.g) + hex(rgb.b)
  }

  function mixHex(fromHex, toHex, fraction) {
    var t = clamp(fraction, 0, 1)
    var from = hexToRgb(fromHex)
    var to = hexToRgb(toHex)
    return rgbToHex({
      r: from.r + (to.r - from.r) * t,
      g: from.g + (to.g - from.g) * t,
      b: from.b + (to.b - from.b) * t,
    })
  }

  function withAlpha(hex, alpha) {
    var rgb = hexToRgb(hex)
    return 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + clamp(alpha, 0, 1) + ')'
  }

  function colorForIndex(index) {
    var number = Math.floor(asNumber(index) === null ? 0 : asNumber(index))
    return PALETTE[Math.abs(number) % PALETTE.length]
  }

  function hashString(text) {
    var value = String(isNil(text) ? '' : text)
    var hash = 2166136261
    for (var index = 0; index < value.length; index++) {
      hash ^= value.charCodeAt(index)
      hash = Math.imul(hash, 16777619)
    }
    return hash >>> 0
  }

  /** 分类名 → 稳定颜色：同一簇/状态名永远同色（与后端出图顺序无关）。 */
  function colorForName(name, fallbackIndex) {
    if (isNil(name) || name === '') return colorForIndex(fallbackIndex || 0)
    var text = String(name)
    if (/^\d+$/.test(text) && text.length <= 3) return colorForIndex(Number(text))
    return PALETTE[hashString(text) % PALETTE.length]
  }

  /** 连续值 → 色标颜色（min≥max / 缺值返回中间色）。 */
  function colorForValue(value, min, max) {
    var number = asNumber(value)
    if (number === null) return '#bab0ac'
    var low = asNumber(min)
    var high = asNumber(max)
    if (low === null || high === null || high <= low) return CONTINUOUS_STOPS[2]
    var t = clamp((number - low) / (high - low), 0, 1) * (CONTINUOUS_STOPS.length - 1)
    var slot = Math.min(CONTINUOUS_STOPS.length - 2, Math.floor(t))
    return mixHex(CONTINUOUS_STOPS[slot], CONTINUOUS_STOPS[slot + 1], t - slot)
  }

  // @@CHUNK1B@@
  // ==========================================================================
  // 二维视口变换（canvas 缩放/拖拽）：数据坐标 ↔ 屏幕像素，往返可逆
  // ==========================================================================

  function buildViewport(points, width, height, padding) {
    var w = Math.max(1, asNumber(width) || 1)
    var hgt = Math.max(1, asNumber(height) || 1)
    var pad = asNumber(padding) === null ? 24 : Math.max(0, Number(padding))
    var minX = Infinity
    var maxX = -Infinity
    var minY = Infinity
    var maxY = -Infinity
    var list = Array.isArray(points) ? points : []
    for (var index = 0; index < list.length; index++) {
      var point = list[index]
      var rawX = point && typeof point === 'object' ? (Array.isArray(point) ? point[0] : point.x) : point
      var rawY = point && typeof point === 'object' ? (Array.isArray(point) ? point[1] : point.y) : NaN
      var x = asNumber(rawX)
      var y = asNumber(rawY)
      if (x === null || y === null) continue
      if (x < minX) minX = x
      if (x > maxX) maxX = x
      if (y < minY) minY = y
      if (y > maxY) maxY = y
    }
    if (!isFinite(minX) || !isFinite(minY)) { minX = 0; maxX = 1; minY = 0; maxY = 1 }
    var spanX = maxX - minX
    var spanY = maxY - minY
    if (spanX < 1e-9) { spanX = 1; maxX = minX + 1 }
    if (spanY < 1e-9) { spanY = 1; maxY = minY + 1 }
    var scale = Math.min((w - 2 * pad) / spanX, (hgt - 2 * pad) / spanY)
    if (!isFinite(scale) || scale <= 0) scale = 1
    var centerX = (minX + maxX) / 2
    var centerY = (minY + maxY) / 2
    return {
      scale: scale,
      tx: w / 2 - centerX * scale,
      ty: hgt / 2 - centerY * scale,
      width: w,
      height: hgt,
      minX: minX, maxX: maxX, minY: minY, maxY: maxY,
    }
  }

  function projectPoint(viewport, x, y) {
    var v = viewport || {}
    var scale = asNumber(v.scale) || 1
    return {
      x: (asNumber(x) || 0) * scale + (asNumber(v.tx) || 0),
      y: (asNumber(v.height) || 1) - ((asNumber(y) || 0) * scale + (asNumber(v.ty) || 0)),
    }
  }

  function unprojectPoint(viewport, px, py) {
    var v = viewport || {}
    var scale = asNumber(v.scale) || 1
    return {
      x: ((asNumber(px) || 0) - (asNumber(v.tx) || 0)) / scale,
      y: ((asNumber(v.height) || 1) - (asNumber(py) || 0) - (asNumber(v.ty) || 0)) / scale,
    }
  }

  /** 以屏幕像素 (px,py) 为锚点缩放：锚点下的数据坐标保持不动。 */
  function zoomViewport(viewport, px, py, factor) {
    var v = viewport || {}
    var data = unprojectPoint(v, px, py)
    var scale = clamp((asNumber(v.scale) || 1) * (asNumber(factor) || 1), 1e-6, 1e9)
    return {
      scale: scale,
      tx: (asNumber(px) || 0) - data.x * scale,
      ty: (asNumber(v.height) || 1) - (asNumber(py) || 0) - data.y * scale,
      width: asNumber(v.width) || 1,
      height: asNumber(v.height) || 1,
      minX: v.minX, maxX: v.maxX, minY: v.minY, maxY: v.maxY,
    }
  }

  function panViewport(viewport, dx, dy) {
    var v = viewport || {}
    return {
      scale: asNumber(v.scale) || 1,
      tx: (asNumber(v.tx) || 0) + (asNumber(dx) || 0),
      ty: (asNumber(v.ty) || 0) - (asNumber(dy) || 0),
      width: asNumber(v.width) || 1,
      height: asNumber(v.height) || 1,
      minX: v.minX, maxX: v.maxX, minY: v.minY, maxY: v.maxY,
    }
  }

  // ==========================================================================
  // 响应归一化（/state、/steps、/suggest、/runs …）：缺字段给安全默认值
  // ==========================================================================

  function normalizeAdapter(raw, index) {
    var item = raw && typeof raw === 'object' ? raw : {}
    return {
      id: plainText(item.id || 'adapter-' + index, 60),
      label: plainText(item.label || item.id || 'adapter', 80),
      capability: plainText(item.capability || '', 160),
      hint: plainText(item.hint || '', 240),
      available: item.available === true,
    }
  }

  function normalizeEngine(raw) {
    var item = raw && typeof raw === 'object' ? raw : {}
    var packages = {}
    var rawPackages = item.packages && typeof item.packages === 'object' ? item.packages : {}
    var keys = Object.keys(rawPackages)
    for (var index = 0; index < keys.length; index++) packages[keys[index]] = rawPackages[keys[index]]
    var adapters = []
    if (Array.isArray(item.adapters)) {
      for (var adapterIndex = 0; adapterIndex < item.adapters.length; adapterIndex++) {
        adapters.push(normalizeAdapter(item.adapters[adapterIndex], adapterIndex))
      }
    }
    return {
      python: plainText(item.python || '', 300),
      pythonVersion: plainText(item.pythonVersion || '', 60),
      backend: plainText(item.backend || '', 60),
      packages: packages,
      adapters: adapters,
      workdir: plainText(item.workdir || '', 400),
      startedAt: plainText(item.startedAt || '', 60),
      available: adapters.some(function (adapter) { return adapter.available }),
    }
  }

  function normalizeStepRecord(raw) {
    var item = raw && typeof raw === 'object' ? raw : {}
    return {
      status: plainText(item.status || '', 30),
      at: plainText(item.at || '', 60),
      ms: asNumber(item.ms),
      summary: plainText(item.summary || '', 800),
      params: item.params && typeof item.params === 'object' ? item.params : {},
      warnings: Array.isArray(item.warnings) ? item.warnings.map(function (text) { return plainText(text, 400) }) : [],
      metrics: item.metrics && typeof item.metrics === 'object' ? item.metrics : {},
    }
  }

  function normalizeAttachment(raw, index) {
    var item = raw && typeof raw === 'object' ? raw : {}
    return {
      id: plainText(item.id || 'at-' + index, 80),
      kind: plainText(item.kind || '', 60),
      name: plainText(item.name || item.id || '', 200),
      path: plainText(item.path || '', 500),
      sha256: plainText(item.sha256 || '', 100),
    }
  }

  function normalizeDataset(raw) {
    var item = raw && typeof raw === 'object' ? raw : {}
    var rawSteps = item.steps && typeof item.steps === 'object' ? item.steps : {}
    var steps = {}
    var stepKeys = Object.keys(rawSteps)
    for (var keyIndex = 0; keyIndex < stepKeys.length; keyIndex++) {
      steps[stepKeys[keyIndex]] = normalizeStepRecord(rawSteps[stepKeys[keyIndex]])
    }
    var stepOrder = []
    if (Array.isArray(item.stepOrder)) {
      stepOrder = item.stepOrder.map(function (id) { return plainText(id, 40) })
    } else if (Array.isArray(item.steps_done)) {
      stepOrder = item.steps_done.map(function (id) { return plainText(id, 40) })
    }
    var warnings = []
    if (Array.isArray(item.warnings)) {
      for (var warnIndex = 0; warnIndex < item.warnings.length; warnIndex++) warnings.push(plainText(item.warnings[warnIndex], 500))
    }
    var attachments = []
    if (Array.isArray(item.attachments)) {
      for (var attachIndex = 0; attachIndex < item.attachments.length; attachIndex++) {
        attachments.push(normalizeAttachment(item.attachments[attachIndex], attachIndex))
      }
    }
    var truth = item.truth && typeof item.truth === 'object' ? item.truth : {}
    return {
      id: plainText(item.id || '', 80),
      name: plainText(item.name || item.id || 'dataset', 200),
      nCells: asNumber(item.n_cells),
      nGenes: asNumber(item.n_genes),
      nTfs: asNumber(item.n_tfs),
      source: plainText(item.source || '', 40),
      bytes: asNumber(item.bytes),
      sha256: plainText(item.sha256 || '', 100),
      rawPath: plainText(item.rawPath || '', 600),
      rawCopy: item.rawCopy !== false,
      createdAt: plainText(item.createdAt || '', 60),
      steps: steps,
      stepOrder: stepOrder,
      warnings: warnings,
      attachments: attachments,
      truth: {
        hasGrnTruth: truth.has_grn_truth === true,
        hasPerturbTruth: truth.has_perturb_truth === true,
      },
    }
  }

  function findDataset(datasets, id) {
    if (!Array.isArray(datasets) || !id) return null
    for (var index = 0; index < datasets.length; index++) {
      if (datasets[index] && datasets[index].id === id) return datasets[index]
    }
    return null
  }

  /** /state → 归一化工作台状态（容错字段命名差异，缺字段给安全默认值）。 */
  function normalizeState(payload) {
    var raw = payload && typeof payload === 'object' ? payload : {}
    var datasets = []
    if (Array.isArray(raw.datasets)) {
      for (var index = 0; index < raw.datasets.length; index++) datasets.push(normalizeDataset(raw.datasets[index]))
    }
    var active = plainText(raw.active || '', 80)
    if (!active && datasets.length) active = datasets[0].id
    var results = raw.results && typeof raw.results === 'object' ? raw.results : {}
    var activeDataset = findDataset(datasets, active)
    var stepsDone = Array.isArray(raw.stepsDone)
      ? raw.stepsDone.map(function (id) { return plainText(id, 40) })
      : (activeDataset ? activeDataset.stepOrder.slice() : [])
    var busy = null
    if (raw.busy && typeof raw.busy === 'object') {
      busy = {
        step: plainText(raw.busy.step || '', 40),
        run: plainText(raw.busy.run || '', 60),
        pct: asNumber(raw.busy.pct),
        message: plainText(raw.busy.message || '', 300),
      }
    }
    return {
      ok: raw.ok !== false,
      version: plainText(raw.version || '', 40),
      engine: normalizeEngine(raw.engine),
      datasets: datasets,
      active: active,
      activeDataset: activeDataset,
      results: results,
      stepsDone: stepsDone,
      suggestedStep: plainText(raw.suggestedStep || '', 40),
      busy: busy,
      runs: Array.isArray(raw.runs) ? raw.runs : [],
      lastRun: plainText(raw.lastRun || '', 80),
      steps: Array.isArray(raw.steps) ? raw.steps : [],
      raw: raw,
    }
  }

  // @@CHUNK2@@
  // ==========================================================================
  // 步骤元数据 / 参数推荐 / 图 / 表 / run / audit / insight 归一化
  // ==========================================================================

  function normalizeOption(raw) {
    if (raw && typeof raw === 'object') return { value: plainText(raw.value, 200), label: plainText(raw.label || raw.value, 200) }
    return { value: plainText(raw, 200), label: plainText(raw, 200) }
  }

  /** 参数描述符（/steps 与 /suggest 共用形状）。 */
  function normalizeParamSpec(raw, index) {
    var item = raw && typeof raw === 'object' ? raw : {}
    var options = []
    if (Array.isArray(item.options)) {
      for (var optionIndex = 0; optionIndex < item.options.length; optionIndex++) options.push(normalizeOption(item.options[optionIndex]))
    }
    return {
      key: plainText(item.key || 'param-' + index, 80),
      label: plainText(item.label || item.key || '', 160),
      type: plainText(item.type || 'text', 30),
      defaultValue: item.default,
      recommended: item.recommended,
      min: asNumber(item.min),
      max: asNumber(item.max),
      step: asNumber(item.step),
      options: options,
      advanced: item.advanced === true,
      effect: plainText(item.effect || '', 300),
      risk: plainText(item.risk || '', 20),
      why: plainText(item.why || '', 500),
      range: Array.isArray(item.range) ? item.range.map(function (value) { return asNumber(value) }) : null,
    }
  }

  /** /steps → 左栏 10 张步骤卡元数据（顺序即 STEP_ORDER）。 */
  function normalizeSteps(payload) {
    var raw = payload && typeof payload === 'object' ? payload : {}
    var list = Array.isArray(raw.steps) ? raw.steps : (Array.isArray(raw) ? raw : [])
    var steps = []
    for (var index = 0; index < list.length; index++) {
      var item = list[index] && typeof list[index] === 'object' ? list[index] : {}
      var params = []
      if (Array.isArray(item.params)) {
        for (var paramIndex = 0; paramIndex < item.params.length; paramIndex++) {
          params.push(normalizeParamSpec(item.params[paramIndex], paramIndex))
        }
      }
      steps.push({
        id: plainText(item.id || stepIdAt(index), 40),
        title: plainText(item.title || STEP_TITLES[item.id] || item.id || '', 120),
        icon: plainText(item.icon || item.id || '', 40),
        requires: Array.isArray(item.requires) ? item.requires.map(function (id) { return plainText(id, 40) }) : [],
        why: plainText(item.why || '', 600),
        cost: plainText(item.cost || '', 120),
        outputs: Array.isArray(item.outputs) ? item.outputs.map(function (text) { return plainText(text, 160) }) : [],
        params: params,
      })
    }
    return { ok: raw.ok !== false, steps: steps }
  }

  function stepIdAt(index) {
    return STEP_ORDER[index] || ('step-' + index)
  }

  /** /suggest → 每步的参数推荐（推荐值徽章 + 为什么）。 */
  function normalizeSuggestion(payload) {
    var raw = payload && typeof payload === 'object' ? payload : {}
    var params = []
    var list = Array.isArray(raw.params) ? raw.params : []
    for (var index = 0; index < list.length; index++) params.push(normalizeParamSpec(list[index], index))
    return {
      ok: raw.ok !== false,
      step: plainText(raw.step || '', 40),
      params: params,
      notes: Array.isArray(raw.notes) ? raw.notes.map(function (text) { return plainText(text, 400) }) : [],
    }
  }

  /** 出图 URL：优先用服务端给的 url（相对/绝对都直接用），没有才构造 ?id=（保留完整 ds/run/file）。 */
  function figureSrc(raw) {
    if (typeof raw === 'string' && raw) {
      if (raw.indexOf('data:') === 0 || raw.indexOf('http://') === 0 || raw.indexOf('https://') === 0) return raw
      if (raw.charAt(0) === '/') return raw
      if (/^[A-Za-z0-9._%-]+$/.test(raw) && /\.png$/i.test(raw)) return API + '/figure?id=' + encodeURIComponent(raw)
      return ''
    }
    if (!raw || typeof raw !== 'object') return ''
    if (typeof raw.url === 'string' && raw.url) return raw.url
    if (!isNil(raw.id) && String(raw.id)) return API + '/figure?id=' + encodeURIComponent(String(raw.id))
    if (typeof raw.file === 'string' && raw.file) return API + '/figure?id=' + encodeURIComponent(raw.file)
    return ''
  }

  function normalizeFigure(raw) {
    if (!raw || typeof raw !== 'object') return null
    var url = figureSrc(raw)
    if (!url) return null
    return {
      id: plainText(raw.id || '', 200),
      title: plainText(raw.title || raw.kind || '', 200),
      url: url,
      kind: plainText(raw.kind || '', 60),
      path: plainText(raw.path || '', 500),
      width: asNumber(raw.width) || 0,
      height: asNumber(raw.height) || 0,
    }
  }

  function normalizeFigureList(list) {
    var out = []
    if (!Array.isArray(list)) return out
    for (var index = 0; index < list.length; index++) {
      var figure = normalizeFigure(list[index])
      if (figure) out.push(figure)
    }
    return out
  }

  /** 表格：columns + rows（后端给 preview.columns/rows）→ 渲染友好的结构。 */
  function normalizeTable(raw, index) {
    var item = raw && typeof raw === 'object' ? raw : {}
    var preview = item.preview && typeof item.preview === 'object' ? item.preview : {}
    var columns = []
    if (Array.isArray(preview.columns)) {
      columns = preview.columns.map(function (name) { return plainText(name, 120) })
    } else if (Array.isArray(item.columns)) {
      columns = item.columns.map(function (column) {
        return plainText(column && typeof column === 'object' ? (column.label || column.key) : column, 120)
      })
    }
    var rows = []
    if (Array.isArray(preview.rows)) {
      for (var rowIndex = 0; rowIndex < preview.rows.length; rowIndex++) {
        var row = preview.rows[rowIndex]
        rows.push(Array.isArray(row) ? row.map(function (cell) { return cell }) : Object.keys(row || {}).map(function (key) { return row[key] }))
      }
    }
    return {
      id: plainText(item.id || 'table-' + index, 80),
      title: plainText(item.title || item.id || 'table', 120),
      path: plainText(item.path || '', 500),
      file: plainText(item.file || '', 200),
      total: asNumber(item.rows !== undefined ? item.rows : preview.total) || rows.length,
      columns: columns,
      rows: rows,
    }
  }

  function normalizeTables(list) {
    var out = []
    if (!Array.isArray(list)) return out
    for (var index = 0; index < list.length; index++) out.push(normalizeTable(list[index], index))
    return out
  }

  function normalizeRun(raw, index) {
    var item = raw && typeof raw === 'object' ? raw : {}
    return {
      id: plainText(item.id || 'run-' + index, 80),
      step: plainText(item.step || '', 40),
      label: plainText(item.label || item.step || '', 200),
      status: plainText(item.status || '', 30),
      startedAt: plainText(item.startedAt || '', 60),
      durationMs: asNumber(item.durationMs),
      figures: asNumber(item.figures) || 0,
      tables: asNumber(item.tables) || 0,
      summary: plainText(item.summary || '', 600),
      dir: plainText(item.dir || '', 600),
    }
  }

  function normalizeRuns(list) {
    var out = []
    if (!Array.isArray(list)) return out
    for (var index = 0; index < list.length; index++) out.push(normalizeRun(list[index], index))
    return out
  }

  /** /audit：0–100 分 + 逐项检查。 */
  function normalizeAudit(payload) {
    var raw = payload && typeof payload === 'object' ? payload : {}
    var items = []
    if (Array.isArray(raw.items)) {
      for (var index = 0; index < raw.items.length; index++) {
        var item = raw.items[index] && typeof raw.items[index] === 'object' ? raw.items[index] : {}
        items.push({
          id: plainText(item.id || 'item-' + index, 80),
          level: plainText(item.level || 'warn', 20),
          title: plainText(item.title || '', 200),
          detail: plainText(item.detail || '', 600),
          hint: plainText(item.hint || '', 300),
        })
      }
    }
    return {
      ok: raw.ok !== false,
      score: asNumber(raw.score),
      level: plainText(raw.level || '', 20),
      summary: plainText(raw.summary || '', 800),
      items: items,
      caveats: Array.isArray(raw.caveats) ? raw.caveats.map(function (text) { return plainText(text, 500) }) : [],
    }
  }

  /** /insight：结构化结论 + 靶点排名 + 下一步 + caveats。 */
  function normalizeInsight(payload) {
    var raw = payload && typeof payload === 'object' ? payload : {}
    var sections = []
    if (Array.isArray(raw.sections)) {
      for (var index = 0; index < raw.sections.length; index++) {
        var section = raw.sections[index] && typeof raw.sections[index] === 'object' ? raw.sections[index] : {}
        sections.push({
          title: plainText(section.title || '', 160),
          bullets: Array.isArray(section.bullets) ? section.bullets.map(function (text) { return plainText(text, 600) }) : [],
        })
      }
    }
    return {
      ok: raw.ok !== false,
      headline: plainText(raw.headline || raw.summary || '', 1200),
      summary: plainText(raw.summary || raw.headline || '', 1200),
      sections: sections,
      rankedTargets: normalizeRankedTargets(raw.ranked_targets || raw.rankedTargets),
      nextActions: normalizeNextActions(raw.next_actions || raw.nextActions),
      caveats: Array.isArray(raw.caveats) ? raw.caveats.map(function (text) { return plainText(text, 600) }) : [],
      level: plainText(raw.level || '', 20),
      at: plainText(raw.at || '', 60),
    }
  }

  function normalizeRankedTargets(list) {
    var out = []
    if (!Array.isArray(list)) return out
    for (var index = 0; index < list.length; index++) {
      var item = list[index] && typeof list[index] === 'object' ? list[index] : { gene: list[index] }
      out.push({
        gene: plainText(item.gene || item.id || '', 80),
        score: asNumber(item.score),
        why: plainText(item.why || '', 400),
      })
    }
    return out
  }

  function normalizeNextActions(list) {
    var out = []
    if (!Array.isArray(list)) return out
    for (var index = 0; index < list.length; index++) {
      var item = list[index] && typeof list[index] === 'object' ? list[index] : { step: list[index] }
      out.push({
        step: plainText(item.step || '', 40),
        why: plainText(item.why || '', 500),
      })
    }
    return out
  }

  /** /preview：基因/细胞列；若后端附带嵌入坐标则一并提取（供交互散点）。 */
  function normalizePreview(payload) {
    var raw = payload && typeof payload === 'object' ? payload : {}
    var obsColumns = Array.isArray(raw.obs_columns) ? raw.obs_columns.map(function (name) { return plainText(name, 120) }) : []
    var clusters = Array.isArray(raw.clusters) ? raw.clusters.map(function (value) { return plainText(value, 80) }) : []
    return {
      ok: raw.ok !== false,
      genes: Array.isArray(raw.genes) ? raw.genes.map(function (gene) { return plainText(gene, 120) }) : [],
      cells: Array.isArray(raw.cells) ? raw.cells.map(function (cell) { return plainText(cell, 120) }) : [],
      obsColumns: obsColumns,
      clusters: clusters,
      embedding: extractEmbedding(raw),
      nGenes: asNumber(raw.n_genes),
      nCells: asNumber(raw.n_cells),
    }
  }

  /** 从任意响应里找二维细胞坐标：兼容 [[x,y],…] / [{x,y}] / {embedding:…} 等形状。 */
  function extractEmbedding(raw) {
    if (!raw || typeof raw !== 'object') return []
    var candidates = [raw.embedding, raw.embedding_2d, raw.points, raw.coords, raw.umap, raw.tsne, raw.scatter]
    for (var index = 0; index < candidates.length; index++) {
      var candidate = candidates[index]
      if (!Array.isArray(candidate) || !candidate.length) continue
      var points = []
      for (var pointIndex = 0; pointIndex < candidate.length; pointIndex++) {
        var point = candidate[pointIndex]
        var x = null
        var y = null
        if (Array.isArray(point)) { x = asNumber(point[0]); y = asNumber(point[1]) } else if (point && typeof point === 'object') {
          x = asNumber(point.x); y = asNumber(point.y)
          if (x === null) { x = asNumber(point[0]); y = asNumber(point[1]) }
        }
        if (x === null || y === null) continue
        points.push({ x: x, y: y, cluster: plainText((point && point.cluster) || (point && point.group) || '', 80) })
      }
      if (points.length) return points
    }
    return []
  }

  // @@CHUNK2B@@
  // ==========================================================================
  // 步骤状态机 / 参数默认值与收集
  // ==========================================================================

  function doneStepSet(dataset) {
    var done = {}
    if (!dataset) return done
    var order = dataset.stepOrder || []
    for (var index = 0; index < order.length; index++) done[order[index]] = true
    var steps = dataset.steps || {}
    var keys = Object.keys(steps)
    for (var keyIndex = 0; keyIndex < keys.length; keyIndex++) {
      var record = steps[keys[keyIndex]]
      if (record && (record.status === 'ok' || record.status === 'done')) done[keys[keyIndex]] = true
    }
    return done
  }

  function isStepReady(stepMeta, doneSet) {
    var requires = (stepMeta && stepMeta.requires) || []
    for (var index = 0; index < requires.length; index++) if (!doneSet[requires[index]]) return false
    return true
  }

  /** ready | done | running | failed | blocked */
  function stepStatusOf(stepMeta, doneSet, busyStep) {
    var id = stepMeta && stepMeta.id
    if (busyStep && busyStep === id) return 'running'
    if (doneSet && doneSet[id]) return 'done'
    return isStepReady(stepMeta, doneSet || {}) ? 'ready' : 'blocked'
  }

  function defaultParams(paramSpecs) {
    var values = {}
    var list = Array.isArray(paramSpecs) ? paramSpecs : []
    for (var index = 0; index < list.length; index++) {
      var spec = list[index]
      if (!spec || !spec.key) continue
      values[spec.key] = spec.defaultValue
    }
    return values
  }

  /** 按参数类型把用户输入合回参数对象（数字空值不传）。 */
  function coerceParamValue(spec, value) {
    if (!spec) return value
    var type = spec.type || 'text'
    if (type === 'number') return asNumber(value)
    if (type === 'boolean') return value === true || value === 'true'
    if (type === 'genes' || type === 'multiselect') return parseTokenList(Array.isArray(value) ? value.join(',') : value)
    return isNil(value) ? '' : value
  }

  function collectStepParams(paramSpecs, values) {
    var out = {}
    var list = Array.isArray(paramSpecs) ? paramSpecs : []
    for (var index = 0; index < list.length; index++) {
      var spec = list[index]
      if (!spec || !spec.key) continue
      var value = values ? values[spec.key] : spec.defaultValue
      var coerced = coerceParamValue(spec, value)
      if (coerced === null || coerced === '' || (Array.isArray(coerced) && !coerced.length)) continue
      out[spec.key] = coerced
    }
    if (values && values.__extra && typeof values.__extra === 'object') {
      var extraKeys = Object.keys(values.__extra)
      for (var extraIndex = 0; extraIndex < extraKeys.length; extraIndex++) out[extraKeys[extraIndex]] = values.__extra[extraKeys[extraIndex]]
    }
    return out
  }

  function formatParamValue(spec, value) {
    if (isNil(value) || value === '') return '—'
    var type = spec && spec.type
    if (type === 'number') return fmtNumber(value, Math.abs(Number(value)) < 10 ? 2 : 1)
    if (type === 'boolean') return value ? '是' : '否'
    if (Array.isArray(value)) return value.join('、')
    return plainText(value, 200)
  }

  /** 逗号/空格/分号分隔的基因列表。 */
  function parseTokenList(text) {
    if (Array.isArray(text)) return text.map(function (item) { return plainText(item, 80) }).filter(Boolean)
    if (isNil(text)) return []
    return String(text).split(/[\s,;，；]+/).map(function (item) { return item.trim() }).filter(Boolean)
  }

  // ==========================================================================
  // 错误码 → 可操作中文建议（CONTRACT §1 错误码表）
  // ==========================================================================

  var ERROR_META = {
    prerequisite: { key: 'errPrerequisite', title: '缺少前置步骤' },
    bad_request: { key: 'errBadRequest', title: '参数不对' },
    timeout: { key: 'errTimeout', title: '计算超时' },
    no_python: { key: 'errNoPython', title: 'Python 环境未就绪' },
    busy: { key: 'errBusy', title: '引擎正忙' },
    engine_exited: { key: 'errEngineExited', title: '引擎已退出' },
    not_found: { key: 'errNotFound', title: '找不到对象' },
    unsupported: { key: 'errUnsupported', title: '当前引擎不支持' },
    ambiguous: { key: 'errAmbiguous', title: '输入有歧义' },
    internal: { key: 'errInternal', title: '引擎内部错误' },
    disposed: { key: 'errDisposed', title: '引擎已关闭' },
    auth: { key: 'errAuth', title: '没有权限' },
    forbidden: { key: 'errAuth', title: '没有权限' },
    network: { key: 'errNetwork', title: '网络/宿主连接失败' },
    unknown: { key: 'errUnknown', title: '未知错误' },
  }

  /** 错误码 → 中文建议（纯函数；UI 再用 T() 覆盖标题文案）。 */
  function errorAdvice(code, detail, hint) {
    var normalized = plainText(code || 'internal', 60)
    var extra = plainText(hint || (detail && detail.hint) || '', 400)
    if (normalized === 'prerequisite') {
      var stepId = plainText((detail && (detail.step || detail.requires)) || '', 40)
      var stepTitle = STEP_TITLES[stepId] || stepId
      return '请先运行上一步' + (stepTitle ? '（' + stepTitle + '）' : '') + (extra ? '：' + extra : '')
    }
    if (normalized === 'bad_request') return '参数不对' + (extra || detail && detail.error ? '：' + plainText((detail && detail.error) || extra, 400) : '，请检查参数类型与取值范围')
    if (normalized === 'timeout') return '计算超时，可换小一点的参数重试' + (extra ? '（' + extra + '）' : '')
    if (normalized === 'no_python') return 'Python 环境未就绪，点引擎徽章查看'
    if (normalized === 'busy') return '引擎正在跑另一步，等它结束再试' + (extra ? '：' + extra : '')
    if (normalized === 'unsupported') return '当前引擎不支持这个能力：装可选后端（scanpy/scvi/celloracle/gears）后重试' + (extra ? '（' + extra + '）' : '')
    if (normalized === 'ambiguous') return '输入有歧义，请补充明确参数' + (extra ? '：' + extra : '')
    if (normalized === 'network') return '连不上宿主：确认页面与 /virtualcell 同源、宿主未退出'
    return (extra || '请重试；持续失败可重启引擎（点引擎徽章 → 重启）')
  }

  /** 统一解析失败信封 / 网络异常 → 展示用结构。 */
  function describeError(error) {
    var payload = error && typeof error === 'object' ? error : { error: String(isNil(error) ? '' : error) }
    var code = plainText(payload.code || 'internal', 60)
    var meta = ERROR_META[code] || ERROR_META.internal
    var message = plainText(payload.error || payload.message || '', 800)
    var hint = plainText((payload.details && payload.details.hint) || payload.hint || '', 400)
    return {
      code: code,
      title: meta.title,
      localeKey: meta.key,
      message: message,
      hint: hint,
      advice: errorAdvice(code, payload.details || payload, hint),
    }
  }

  // ==========================================================================
  // API 客户端：同源 /virtualcell/*，带超时 + 统一错误信封
  // ==========================================================================

  function normalizeApiError(status, body) {
    var payload = body && typeof body === 'object' ? body : {}
    return {
      __vc: true,
      code: plainText(payload.code || (status === 404 ? 'not_found' : 'internal'), 60),
      error: plainText(payload.error || ('HTTP ' + status), 800),
      details: payload.details && typeof payload.details === 'object' ? payload.details : {},
      status: status,
    }
  }

  function createApi(fetchImpl, baseUrl) {
    var base = baseUrl || API
    var fetcher = fetchImpl || (typeof fetch === 'function' ? fetch : null)

    function request(method, path, body, options) {
      var opts = options || {}
      var timeoutMs = asNumber(opts.timeoutMs) || API_TIMEOUT_MS
      var controller = null
      try { if (typeof AbortController !== 'undefined') controller = new AbortController() } catch (ignored) { controller = null }
      var timer = null
      if (controller && timeoutMs > 0) timer = setTimeout(function () { try { controller.abort() } catch (ignored) { /* ignore */ } }, timeoutMs)
      var init = { method: method, headers: {} }
      if (body !== undefined && body !== null) {
        init.headers['content-type'] = 'application/json'
        init.body = JSON.stringify(body)
      }
      if (controller) init.signal = controller.signal
      return Promise.resolve()
        .then(function () {
          if (!fetcher) throw { __vc: true, code: 'network', error: '当前环境没有 fetch', status: 0 }
          return fetcher(base + path, init)
        })
        .then(function (response) {
          return response.text().then(function (text) {
            var payload = null
            try { payload = text ? JSON.parse(text) : {} } catch (ignored) { payload = { ok: false, code: 'internal', error: '返回不是 JSON：' + String(text).slice(0, 200) } }
            if (response.ok && payload && payload.ok !== false) return payload
            throw normalizeApiError(response.status, payload)
          })
        })
        .catch(function (error) {
          if (timer) { clearTimeout(timer); timer = null }
          if (error && error.__vc) throw error
          var aborted = error && (error.name === 'AbortError' || /abort/i.test(String(error.message || '')))
          throw {
            __vc: true,
            code: aborted ? 'timeout' : 'network',
            error: aborted ? ('请求超过 ' + Math.round(timeoutMs / 1000) + ' 秒未返回') : plainText((error && error.message) || error, 600),
            details: {},
            status: 0,
          }
        })
        .then(function (value) {
          if (timer) { clearTimeout(timer); timer = null }
          return value
        }, function (error) {
          if (timer) { clearTimeout(timer); timer = null }
          throw error
        })
    }

    return {
      base: base,
      request: request,
      get: function (path, options) { return request('GET', path, undefined, options) },
      post: function (path, body, options) { return request('POST', path, body || {}, options) },
      figureUrl: function (id) { return base + '/figure?id=' + encodeURIComponent(id) },
    }
  }

  var defaultApi = null
  function api() {
    if (!defaultApi) defaultApi = createApi(null, API)
    return defaultApi
  }

  // ==========================================================================
  // locale 翻译器：宿主 locale 服务可用就绑定，否则回落中文词典
  // ==========================================================================

  function createTranslator(locale) {
    var bound = null
    try {
      if (locale && typeof locale.bind === 'function') bound = locale.bind(NS)
    } catch (ignored) { bound = null }
    return function translate(key) {
      if (typeof bound === 'function') {
        try {
          var value = bound(key)
          if (typeof value === 'string' && value && value !== key) return value
        } catch (ignored) { /* 宿主 locale 不可用：静默回落 */ }
      }
      var fallback = DICT_ZH[key]
      return typeof fallback === 'string' ? fallback : String(key)
    }
  }

  /** 全局取词：apply() 里替换 translator。 */
  function T(key) {
    if (typeof translator === 'function') return translator(key)
    return typeof key === 'string' ? key : String(key)
  }

  /** 带 {v} 占位的模板取词（避免依赖宿主插值实现）。 */
  function tf(key, value) {
    var template = T(key)
    if (!template || template === key) template = DICT_ZH[key] || ''
    return String(template).replace(/\{v\}/g, isNil(value) ? '' : String(value))
  }

  // @@CHUNK2C@@
  // ==========================================================================
  // 日志 / 运行态文本
  // ==========================================================================

  function nowStamp() {
    var date = new Date()
    function pad(number) { return number < 10 ? '0' + number : String(number) }
    return pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds())
  }

  function makeLogEntry(level, text, extra) {
    return {
      id: 'log-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7),
      at: nowStamp(),
      level: plainText(level || 'info', 20),
      text: plainText(text || '', 1200),
      extra: plainText(extra || '', 600),
    }
  }

  function appendLog(list, entry, max) {
    var out = Array.isArray(list) ? list.slice() : []
    if (!entry) return out
    out.push(entry)
    var limit = asNumber(max) || MAX_LOG
    if (out.length > limit) out = out.slice(out.length - limit)
    return out
  }

  function stepTitle(stepId) {
    return STEP_TITLES[stepId] || plainText(stepId || '', 40) || '步骤'
  }

  function resultOf(stateObj, step) {
    if (!stateObj || !stateObj.results) return null
    var value = stateObj.results[step]
    return value && typeof value === 'object' ? value : null
  }

  function busyText(stateObj) {
    if (!stateObj || !stateObj.busy) return ''
    var busy = stateObj.busy
    var title = stepTitle(busy.step)
    var pct = asNumber(busy.pct)
    return title + '运行中' + (pct === null ? '' : '（' + trimFixed(pct, 0) + '%）') + (busy.message ? '：' + busy.message : '')
  }

  /** 从 grn 结果里取 hub TF 列表（metrics.hub_tfs 可能是字符串或对象）。 */
  function hubTfsOf(stateObj) {
    var grn = resultOf(stateObj, 'grn')
    var out = []
    if (!grn) return out
    var metrics = grn.metrics && typeof grn.metrics === 'object' ? grn.metrics : {}
    var hubs = metrics.hub_tfs
    if (Array.isArray(hubs)) {
      for (var index = 0; index < hubs.length; index++) {
        var item = hubs[index]
        var name = typeof item === 'string' ? item : plainText((item && (item.tf || item.gene || item.id)) || '', 80)
        if (name) out.push(name)
      }
    }
    var edges = Array.isArray(grn.edges) ? grn.edges : []
    if (!out.length) {
      var seen = {}
      for (var edgeIndex = 0; edgeIndex < edges.length; edgeIndex++) {
        var edge = edges[edgeIndex] || {}
        var tf = plainText(edge.tf || '', 80)
        if (tf && !seen[tf]) { seen[tf] = true; out.push(tf) }
        if (out.length >= 12) break
      }
    }
    return out.slice(0, 20)
  }

  function clusterLabelsOf(stateObj) {
    var state = resultOf(stateObj, 'state')
    if (state && Array.isArray(state.cluster_labels)) return state.cluster_labels.map(function (name) { return plainText(name, 80) })
    return []
  }

  // ==========================================================================
  // 状态图兜底布局：后端暂未提供单细胞坐标时，按真实簇规模画"簇级示意图"
  // （半径 ∝ sqrt(细胞数)，位置为确定的黄金角螺旋；只用于展示真实簇摘要）
  // ==========================================================================

  function buildClusterLayout(labels, sizes) {
    var names = Array.isArray(labels) ? labels : []
    var counts = Array.isArray(sizes) ? sizes : []
    var out = []
    var golden = Math.PI * (3 - Math.sqrt(5))
    var maxCount = 0
    for (var index = 0; index < counts.length; index++) {
      var count = asNumber(counts[index])
      if (count !== null && count > maxCount) maxCount = count
    }
    for (var itemIndex = 0; itemIndex < names.length; itemIndex++) {
      var label = plainText(names[itemIndex], 80)
      if (!label) continue
      var count2 = asNumber(counts[itemIndex])
      var radius = Math.sqrt(itemIndex + 0.6) * 1.35
      var angle = itemIndex * golden
      out.push({
        cluster: label,
        label: label,
        size: count2,
        share: count2 !== null && maxCount > 0 ? count2 / maxCount : null,
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
      })
    }
    return out
  }

  // ==========================================================================
  // 给 AI 的下一步中文指令（可复制）与完整上下文
  // ==========================================================================

  function stepMetaOf(stateObj, stepId) {
    var steps = stateObj && Array.isArray(stateObj.steps) ? stateObj.steps : []
    for (var index = 0; index < steps.length; index++) {
      if (steps[index] && steps[index].id === stepId) return steps[index]
    }
    return null
  }

  function buildStepInstruction(stepId, params) {
    var payload = { action: 'run', step: stepId }
    if (params && Object.keys(params).length) payload.params = params
    return '用 virtualcell 工具执行 ' + JSON.stringify(payload) + '（这是「' + stepTitle(stepId) + '」步骤）'
  }

  /**
   * 右栏「给 AI 的下一步」：3–5 条可复制中文指令。
   * options.suggest = 已缓存的 /suggest 结果（可选）；options.insight = 已缓存的 insight。
   */
  function buildAgentInstructions(stateObj, options) {
    var opts = options || {}
    var state = stateObj && stateObj.activeDataset !== undefined ? stateObj : normalizeState(stateObj)
    var list = []
    var dataset = state.activeDataset
    if (!dataset) {
      list.push({
        id: 'load-demo',
        title: '载入示例数据',
        text: '用 virtualcell 工具 action=demo 载入示例数据；然后 action=recipe/run, recipe=quick 跑通「质控→状态→注释→网络→校验→扰动→智能分析」，最后把 insight 的一句话结论发给我。',
      })
      list.push({
        id: 'import-own',
        title: '导入我的数据',
        text: '用 virtualcell 工具 action=open 导入我的单细胞表达矩阵（路径我随后给），格式 h5ad/10x/CSV 都可以；导完先 action=steps 看参数、action=suggest 看每一步的推荐值。',
      })
      return list.slice(0, 3)
    }
    var done = doneStepSet(dataset)
    var next = state.suggestedStep || ''
    var nextMeta = stepMetaOf(state, next)
    if (next && next !== 'open') {
      var params = null
      var suggest = opts.suggest
      if (suggest && Array.isArray(suggest.params) && suggest.params.length) {
        params = {}
        for (var paramIndex = 0; paramIndex < suggest.params.length; paramIndex++) {
          var spec = suggest.params[paramIndex]
          if (spec && spec.key && !isNil(spec.recommended)) params[spec.key] = spec.recommended
        }
      } else if (nextMeta) {
        params = defaultParams(nextMeta.params)
      }
      list.push({
        id: 'next-step',
        title: '继续下一步：' + stepTitle(next),
        text: buildStepInstruction(next, params) + (nextMeta && nextMeta.why ? '。目的：' + plainText(nextMeta.why, 160) : '') + '；跑完把 summary 与 metrics 给我。',
      })
    }
    var hubs = hubTfsOf(state)
    var perturb = resultOf(state, 'perturb')
    if (hubs.length && !perturb) {
      list.push({
        id: 'perturb-hub',
        title: '敲除 hub 转录因子',
        text: '用 virtualcell 工具执行 ' + JSON.stringify({ action: 'run', step: 'perturb', params: { gene: hubs[0], mode: 'knockout', strength: 1 } }) +
          '，然后解释 metrics.state_shift / target_state 与 affected 表里 top 基因（hub 候选：' + hubs.slice(0, 5).join('、') + '）。',
      })
    }
    if (perturb) {
      var affected = Array.isArray(perturb.affected) ? perturb.affected.slice(0, 5) : []
      var genes = affected.map(function (row) { return plainText((row && row.gene) || '', 40) }).filter(Boolean)
      list.push({
        id: 'explain-perturb',
        title: '解读扰动结果',
        text: '解读 virtualcell 的扰动结果：基因 ' + plainText(perturb.gene || '', 60) + '，模式 ' + plainText(perturb.mode || '', 30) +
          '，state_shift=' + fmtNumber(perturb.state_shift, 2) + '，目标状态 ' + plainText(perturb.target_state || '—', 40) +
          (genes.length ? '，受影响基因 top：' + genes.join('、') : '') + '。请说明方向性证据有多强、需要哪些实验验证。',
      })
    }
    var insight = opts.insight || resultOf(state, 'insight')
    if (insight) {
      list.push({
        id: 'report',
        title: '导出报告与方法',
        text: '用 virtualcell 工具 action=export, what=report 导出中文智能分析报告，再 action=export, what=methods 导出英文 Methods；把报告路径和 caveats 一起列给我。',
      })
    } else {
      list.push({
        id: 'insight',
        title: '生成智能分析',
        text: '用 virtualcell 工具执行 ' + JSON.stringify({ action: 'insight', params: { depth: 'standard' } }) + '，然后按 headline / ranked_targets / caveats 三块给我结论，并指出哪一条最值得做实验。',
      })
    }
    if (!done.audit) {
      list.push({
        id: 'audit',
        title: '跑自检',
        text: '用 virtualcell 工具 action=audit 做确定性自检，把 0–100 分、未通过项和修复建议列出来；顺便 action=export, what=protocol 存档本次参数。',
      })
    }
    return list.slice(0, 5)
  }

  /** 复制完整上下文：数据集 + 引擎 + 每步 summary + 最近 run。 */
  function buildAgentContext(stateObj) {
    var state = stateObj && stateObj.activeDataset !== undefined ? stateObj : normalizeState(stateObj)
    var lines = []
    lines.push('【dsh-virtualcell 工作台上下文】')
    lines.push('引擎：' + (state.engine.backend || '—') + ' / Python ' + (state.engine.pythonVersion || '—') +
      (state.engine.workdir ? ' / workdir=' + state.engine.workdir : ''))
    var dataset = state.activeDataset
    if (!dataset) {
      lines.push('数据集：未载入')
      lines.push('建议：先用 virtualcell 工具 action=demo 或 action=open 导入数据。')
      return lines.join('\n')
    }
    lines.push('数据集：' + dataset.name + '（' + dataset.id + '），' + fmtInt(dataset.nCells) + ' 细胞 × ' +
      fmtInt(dataset.nGenes) + ' 基因' + (dataset.nTfs ? '，' + fmtInt(dataset.nTfs) + ' 个 TF' : ''))
    if (dataset.rawPath) lines.push('原始数据：' + dataset.rawPath + (dataset.sha256 ? '（sha256 ' + dataset.sha256.slice(0, 16) + '…）' : ''))
    lines.push('已完成步骤：' + (state.stepsDone.length ? state.stepsDone.map(stepTitle).join(' → ') : '无'))
    if (state.suggestedStep) lines.push('建议下一步：' + stepTitle(state.suggestedStep))
    var stepIds = STEP_ORDER.slice()
    for (var index = 0; index < stepIds.length; index++) {
      var result = resultOf(state, stepIds[index])
      if (!result || !result.summary) continue
      lines.push('- ' + stepTitle(stepIds[index]) + '：' + plainText(result.summary, 400))
    }
    if (state.lastRun) lines.push('最近 run：' + state.lastRun)
    if (state.busy) lines.push('正在运行：' + busyText(state))
    return lines.join('\n')
  }

  // ==========================================================================
  // 预设流程 / 导出选项 / tab 元数据
  // ==========================================================================

  var RECIPES = [
    { id: 'quick', titleKey: 'recipeQuick', descKey: 'recipeQuickDesc', timeKey: 'recipeQuickTime' },
    { id: 'standard', titleKey: 'recipeStandard', descKey: 'recipeStandardDesc', timeKey: 'recipeStandardTime' },
    { id: 'full', titleKey: 'recipeFull', descKey: 'recipeFullDesc', timeKey: 'recipeFullTime' },
  ]

  var EXPORT_OPTIONS = [
    { what: 'report', labelKey: 'insightExportReport', suffix: '.md' },
    { what: 'methods', labelKey: 'insightExportMethods', suffix: '.md' },
    { what: 'summary_json', labelKey: 'exportJson', suffix: '.json' },
    { what: 'state_csv', labelKey: 'exportCsv', suffix: '.csv' },
    { what: 'perturb_csv', labelKey: 'exportCsv', suffix: '.csv' },
    { what: 'script', labelKey: 'exportPython', suffix: '.py' },
    { what: 'protocol', labelKey: 'exportJson', suffix: '.json' },
  ]

  var TAB_META = [
    { key: 'state', labelKey: 'tabState', requires: ['state'] },
    { key: 'network', labelKey: 'tabNetwork', requires: ['grn'] },
    { key: 'perturb', labelKey: 'tabPerturb', requires: ['grn'] },
    { key: 'screen', labelKey: 'tabScreen', requires: ['grn'] },
    { key: 'response', labelKey: 'tabResponse', requires: ['state'] },
    { key: 'transition', labelKey: 'tabTransition', requires: ['state'] },
    { key: 'insight', labelKey: 'tabInsight', requires: ['state'] },
    { key: 'records', labelKey: 'tabRecords', requires: [] },
  ]

  // ==========================================================================
  // 浏览器层：样式（一个 CSS 字符串，<style> 生命周期跟随组件；类名 vc-）
  // ==========================================================================

  var CSS = [
    '.vc-root{display:flex;flex-direction:column;height:100%;min-height:78vh;overflow:hidden;position:relative;isolation:isolate;',
    'color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-base);border-radius:12px;font-size:13px;line-height:1.5;',
    'font-variant-numeric:tabular-nums}',
    '.vc-root *,.vc-root *::before,.vc-root *::after{box-sizing:border-box}',
    '.vc-root button,.vc-root input,.vc-root select,.vc-root textarea{font:inherit;color:inherit}',
    '.vc-root ::-webkit-scrollbar{width:8px;height:8px}',
    '.vc-root ::-webkit-scrollbar-thumb{background:var(--dsw-alias-border-l2);border-radius:8px}',
    // 顶栏
    '.vc-topbar{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--dsw-alias-border-l1);',
    'background:var(--dsw-alias-bg-layer-1);flex:0 0 auto;flex-wrap:wrap}',
    '.vc-title{font-size:15px;font-weight:650;letter-spacing:.2px;display:flex;align-items:center;gap:8px}',
    '.vc-logo{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,var(--dsw-alias-brand-primary),var(--dsw-alias-state-success-primary));',
    'display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:700}',
    '.vc-subtitle{font-size:11px;color:var(--dsw-alias-label-tertiary);margin-top:1px}',
    '.vc-topbar-spacer{flex:1 1 auto}',
    '.vc-topbar-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap}',
    // 按钮
    '.vc-btn{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);',
    'color:var(--dsw-alias-label-primary);border-radius:8px;padding:5px 10px;font-size:12px;cursor:pointer;white-space:nowrap;transition:filter .12s}',
    '.vc-btn:hover{filter:brightness(1.06);border-color:var(--dsw-alias-border-l2)}',
    '.vc-btn:active{transform:translateY(.5px)}',
    '.vc-btn[disabled]{opacity:.45;cursor:not-allowed}',
    '.vc-btn.vc-primary{background:var(--dsw-alias-brand-primary);border-color:transparent;color:#fff;font-weight:600}',
    '.vc-btn.vc-danger{border-color:color-mix(in srgb,var(--dsw-alias-state-error-primary) 45%,transparent);color:var(--dsw-alias-state-error-primary)}',
    '.vc-btn.vc-ghost{background:transparent;border-color:transparent;color:var(--dsw-alias-label-secondary)}',
    '.vc-btn.vc-small{padding:3px 8px;font-size:11px;border-radius:7px}',
    '.vc-btn.vc-icon{padding:4px 6px}',
    // 三栏
    '.vc-body{display:grid;grid-template-columns:244px minmax(0,1fr) 312px;gap:10px;flex:1 1 auto;min-height:0;padding:10px}',
    '.vc-col{display:flex;flex-direction:column;gap:8px;min-height:0;overflow:auto;padding-right:2px}',
    '.vc-center{display:flex;flex-direction:column;gap:8px;min-height:0;overflow:hidden}',
    '@media (max-width:1180px){.vc-body{grid-template-columns:220px minmax(0,1fr)}.vc-right{display:none}.vc-right.vc-mobile-show{display:flex;position:absolute;right:10px;top:64px;bottom:10px;width:312px;z-index:30;background:var(--dsw-alias-bg-base);border:1px solid var(--dsw-alias-border-l1);border-radius:10px;padding:8px;box-shadow:0 10px 30px rgba(0,0,0,.18)}}',
    '@media (max-width:860px){.vc-body{grid-template-columns:minmax(0,1fr)}.vc-left{max-height:42vh}}',
    // 卡片
    '.vc-card{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:10px;overflow:hidden;flex:0 0 auto}',
    '.vc-card-head{display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-2)}',
    '.vc-card-title{font-size:12px;font-weight:650;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.vc-card-body{padding:9px 10px}',
    '.vc-card-body.vc-tight{padding:6px 8px}',
    '.vc-kv{display:flex;align-items:baseline;justify-content:space-between;gap:8px;padding:2px 0;font-size:11.5px;cursor:default}',
    '.vc-k{color:var(--dsw-alias-label-tertiary);flex:0 0 auto}',
    '.vc-v{color:var(--dsw-alias-label-primary);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:550}',
    '.vc-muted{color:var(--dsw-alias-label-tertiary);font-size:11.5px}',
    '.vc-sub{color:var(--dsw-alias-label-secondary);font-size:12px}',
    '.vc-warn-text{color:var(--dsw-alias-state-warn-primary)}',
    '.vc-err-text{color:var(--dsw-alias-state-error-primary)}',
    '.vc-ok-text{color:var(--dsw-alias-state-success-primary)}',
    // 状态徽章 / chip
    '.vc-dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto;background:var(--dsw-alias-label-tertiary)}',
    '.vc-dot.done{background:var(--dsw-alias-state-success-primary)}',
    '.vc-dot.ready{background:var(--dsw-alias-brand-primary)}',
    '.vc-dot.running{background:var(--dsw-alias-state-warn-primary);animation:vc-pulse 1s infinite}',
    '.vc-dot.blocked{background:var(--dsw-alias-border-l2)}',
    '.vc-dot.failed{background:var(--dsw-alias-state-error-primary)}',
    '@keyframes vc-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
    '.vc-chip{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--dsw-alias-border-l1);border-radius:999px;',
    'padding:1px 7px;font-size:11px;background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary);white-space:nowrap}',
    '.vc-chip.vc-chip-reco{border-color:color-mix(in srgb,var(--dsw-alias-brand-primary) 50%,transparent);color:var(--dsw-alias-brand-primary);cursor:pointer;background:color-mix(in srgb,var(--dsw-alias-brand-primary) 8%,transparent)}',
    '.vc-chip.vc-chip-live{cursor:pointer}',
    '.vc-chip.vc-chip-live.active{background:var(--dsw-alias-brand-primary);color:#fff;border-color:transparent}',
    '.vc-chip.vc-chip-gene{border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}',
    // 步骤卡
    '.vc-step{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:10px;flex:0 0 auto;overflow:hidden}',
    '.vc-step.vc-step-done{border-color:color-mix(in srgb,var(--dsw-alias-state-success-primary) 30%,var(--dsw-alias-border-l1))}',
    '.vc-step.vc-step-running{border-color:color-mix(in srgb,var(--dsw-alias-state-warn-primary) 55%,transparent)}',
    '.vc-step.vc-step-blocked{opacity:.66}',
    '.vc-step-head{display:flex;align-items:center;gap:6px;padding:7px 9px;cursor:pointer;user-select:none}',
    '.vc-step-head:hover{background:var(--dsw-alias-bg-layer-2)}',
    '.vc-step-idx{width:18px;height:18px;border-radius:6px;background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);',
    'display:inline-flex;align-items:center;justify-content:center;font-size:10.5px;color:var(--dsw-alias-label-tertiary);flex:0 0 auto}',
    '.vc-step-title{font-size:12.5px;font-weight:640;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.vc-step-caret{color:var(--dsw-alias-label-tertiary);font-size:10px;flex:0 0 auto}',
    '.vc-step-body{padding:0 9px 9px;display:flex;flex-direction:column;gap:7px}',
    '.vc-step-why{font-size:11px;color:var(--dsw-alias-label-tertiary);line-height:1.5}',
    '.vc-step-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}',
    '.vc-step-summary{font-size:11.5px;color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-bg-layer-2);border-radius:7px;padding:5px 7px}',
    '.vc-progress{height:4px;border-radius:4px;background:var(--dsw-alias-bg-layer-2);overflow:hidden}',
    '.vc-progress > i{display:block;height:100%;background:var(--dsw-alias-brand-primary);transition:width .25s}',
    // 表单
    '.vc-field{display:flex;flex-direction:column;gap:3px}',
    '.vc-field-label{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--dsw-alias-label-secondary);flex-wrap:wrap}',
    '.vc-input,.vc-select,.vc-textarea{width:100%;background:var(--dsw-alias-bg-base);border:1px solid var(--dsw-alias-border-l1);border-radius:7px;',
    'padding:4px 7px;font-size:12px;outline:none;min-width:0}',
    '.vc-input:focus,.vc-select:focus,.vc-textarea:focus{border-color:var(--dsw-alias-brand-primary)}',
    '.vc-textarea{resize:vertical;min-height:50px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}',
    '.vc-check{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--dsw-alias-label-secondary);cursor:pointer}',
    '.vc-advanced{border-top:1px dashed var(--dsw-alias-border-l1);padding-top:5px}',
    '.vc-advanced > summary{cursor:pointer;font-size:11px;color:var(--dsw-alias-label-tertiary);user-select:none}',
    '.vc-advanced-grid{display:flex;flex-direction:column;gap:7px;padding-top:7px}',
    // tabs
    '.vc-tabs{display:flex;gap:2px;align-items:center;border-bottom:1px solid var(--dsw-alias-border-l1);flex:0 0 auto;overflow-x:auto;padding-bottom:0}',
    '.vc-tab{border:none;background:transparent;border-bottom:2px solid transparent;padding:6px 10px;font-size:12px;color:var(--dsw-alias-label-tertiary);',
    'cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;gap:5px}',
    '.vc-tab:hover{color:var(--dsw-alias-label-primary)}',
    '.vc-tab.active{color:var(--dsw-alias-brand-primary);border-bottom-color:var(--dsw-alias-brand-primary);font-weight:600}',
    '.vc-tab .vc-tab-need{font-size:9px;color:var(--dsw-alias-state-warn-primary)}',
    // 中栏内容
    '.vc-panel{flex:1 1 auto;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:9px;padding:2px}',
    '.vc-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:0 0 auto}',
    '.vc-toolbar-label{font-size:11.5px;color:var(--dsw-alias-label-tertiary)}',
    '.vc-row-between{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}',
    '.vc-empty{border:1px dashed var(--dsw-alias-border-l2);border-radius:10px;padding:14px;color:var(--dsw-alias-label-tertiary);font-size:12px;',
    'display:flex;flex-direction:column;gap:8px;align-items:flex-start;background:var(--dsw-alias-bg-layer-1)}',
    '.vc-onboard{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}',
    '.vc-onboard-card{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;padding:10px;background:var(--dsw-alias-bg-layer-1);',
    'display:flex;flex-direction:column;gap:6px}',
    '.vc-onboard-num{width:20px;height:20px;border-radius:50%;background:var(--dsw-alias-brand-primary);color:#fff;font-size:11px;font-weight:700;',
    'display:inline-flex;align-items:center;justify-content:center}',
    // canvas
    '.vc-canvas-wrap{position:relative;border:1px solid var(--dsw-alias-border-l1);border-radius:10px;overflow:hidden;background:var(--dsw-alias-bg-layer-1);flex:1 1 auto;min-height:320px}',
    '.vc-canvas{display:block;width:100%;height:100%;cursor:grab;touch-action:none}',
    '.vc-canvas.vc-grabbing{cursor:grabbing}',
    '.vc-canvas-hud{position:absolute;left:8px;top:8px;display:flex;gap:5px;flex-wrap:wrap;max-width:calc(100% - 16px);pointer-events:none}',
    '.vc-canvas-hud > *{pointer-events:auto}',
    '.vc-canvas-tip{position:absolute;pointer-events:none;background:var(--dsw-alias-bg-overlay,#0b1220);color:#fff;border-radius:7px;',
    'padding:4px 7px;font-size:11px;line-height:1.45;max-width:240px;z-index:5;box-shadow:0 4px 14px rgba(0,0,0,.28)}',
    '.vc-legend{display:flex;flex-wrap:wrap;gap:4px;align-items:center;flex:0 0 auto}',
    '.vc-legend-item{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--dsw-alias-label-secondary);cursor:pointer;',
    'border:1px solid var(--dsw-alias-border-l1);border-radius:999px;padding:1px 7px;background:var(--dsw-alias-bg-layer-1);user-select:none}',
    '.vc-legend-item.off{opacity:.4;text-decoration:line-through}',
    '.vc-legend-swatch{width:8px;height:8px;border-radius:50%;flex:0 0 auto}',
    // 指标 / 图 / 表
    '.vc-metrics{display:flex;flex-wrap:wrap;gap:6px;flex:0 0 auto}',
    '.vc-metric{border:1px solid var(--dsw-alias-border-l1);border-radius:9px;padding:5px 9px;background:var(--dsw-alias-bg-layer-1);min-width:84px}',
    '.vc-metric-k{font-size:10.5px;color:var(--dsw-alias-label-tertiary);white-space:nowrap}',
    '.vc-metric-v{font-size:14px;font-weight:660;white-space:nowrap}',
    '.vc-figure{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;overflow:hidden;background:var(--dsw-alias-bg-layer-1);flex:0 0 auto}',
    '.vc-figure img{display:block;width:100%;height:auto;background:#fff}',
    '.vc-figure-head{display:flex;align-items:center;gap:6px;padding:6px 9px;border-bottom:1px solid var(--dsw-alias-border-l1);font-size:11.5px;color:var(--dsw-alias-label-secondary)}',
    '.vc-figure-empty{padding:16px;text-align:center;color:var(--dsw-alias-label-tertiary);font-size:12px}',
    '.vc-table-wrap{overflow:auto;border:1px solid var(--dsw-alias-border-l1);border-radius:9px;max-height:340px;background:var(--dsw-alias-bg-layer-1)}',
    '.vc-table{width:100%;border-collapse:collapse;font-size:11.5px}',
    '.vc-table th{position:sticky;top:0;background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary);text-align:left;',
    'padding:5px 8px;font-weight:600;white-space:nowrap;border-bottom:1px solid var(--dsw-alias-border-l1);z-index:1}',
    '.vc-table td{padding:4px 8px;border-bottom:1px solid color-mix(in srgb,var(--dsw-alias-border-l1) 55%,transparent);',
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:280px}',
    '.vc-table tr:hover td{background:var(--dsw-alias-bg-layer-2)}',
    '.vc-table td.num{text-align:right;font-variant-numeric:tabular-nums}',
    // 结果列表 / 行
    '.vc-rows{display:flex;flex-direction:column;gap:3px}',
    '.vc-row{display:grid;grid-template-columns:minmax(70px,1.1fr) minmax(0,1fr) auto;gap:7px;align-items:center;padding:3px 6px;border-radius:6px;font-size:11.5px}',
    '.vc-row:hover{background:var(--dsw-alias-bg-layer-2)}',
    '.vc-row-gene{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.vc-delta{font-variant-numeric:tabular-nums;font-weight:600}',
    '.vc-delta.up{color:var(--dsw-alias-state-error-primary)}',
    '.vc-delta.down{color:var(--dsw-alias-state-success-primary)}',
    '.vc-bar{height:5px;border-radius:4px;background:var(--dsw-alias-bg-layer-2);overflow:hidden}',
    '.vc-bar > i{display:block;height:100%;border-radius:4px}',
    // 热图（转移矩阵）
    '.vc-heat{display:grid;gap:2px;font-size:10.5px}',
    '.vc-heat-cell{border-radius:4px;padding:3px 4px;text-align:center;color:#fff;min-width:34px;overflow:hidden;text-overflow:ellipsis}',
    '.vc-heat-head{color:var(--dsw-alias-label-tertiary);text-align:center;padding:2px}',
    // 智能分析
    '.vc-section{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;background:var(--dsw-alias-bg-layer-1);padding:9px 11px;flex:0 0 auto}',
    '.vc-section-title{font-size:12.5px;font-weight:650;margin-bottom:5px}',
    '.vc-bullets{margin:0;padding-left:17px;display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--dsw-alias-label-secondary)}',
    '.vc-caveats{border:1px solid color-mix(in srgb,var(--dsw-alias-state-warn-primary) 55%,transparent);border-left-width:4px;border-radius:9px;',
    'background:color-mix(in srgb,var(--dsw-alias-state-warn-primary) 9%,transparent);padding:9px 11px;font-size:11.5px;color:var(--dsw-alias-label-secondary)}',
    '.vc-caveats b{color:var(--dsw-alias-state-warn-primary)}',
    // 右栏 Agent
    '.vc-agent{display:flex;flex-direction:column;gap:8px;min-height:0}',
    '.vc-summary-lg{font-size:14px;font-weight:620;line-height:1.55}',
    '.vc-command{display:flex;gap:6px;align-items:flex-start;border:1px solid var(--dsw-alias-border-l1);border-radius:9px;padding:6px 8px;background:var(--dsw-alias-bg-layer-1)}',
    '.vc-command-text{flex:1 1 auto;font-size:11.5px;color:var(--dsw-alias-label-secondary);white-space:pre-wrap;word-break:break-word;max-height:120px;overflow:auto}',
    '.vc-log{border:1px solid var(--dsw-alias-border-l1);border-radius:9px;background:var(--dsw-alias-bg-layer-1);max-height:260px;overflow:auto;padding:5px 7px;',
    'display:flex;flex-direction:column;gap:2px;font-size:10.8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}',
    '.vc-log-line{display:flex;gap:5px;color:var(--dsw-alias-label-secondary);line-height:1.45}',
    '.vc-log-time{color:var(--dsw-alias-label-tertiary);flex:0 0 auto}',
    '.vc-log-line.error{color:var(--dsw-alias-state-error-primary)}',
    '.vc-log-line.warn{color:var(--dsw-alias-state-warn-primary)}',
    '.vc-log-line.ok{color:var(--dsw-alias-state-success-primary)}',
    // 弹层 / 抽屉 / toast
    '.vc-mask{position:absolute;inset:0;background:rgba(4,8,16,.42);z-index:40;display:flex;align-items:center;justify-content:center;padding:18px}',
    '.vc-modal{width:min(620px,94%);max-height:86%;display:flex;flex-direction:column;background:var(--dsw-alias-bg-base);border:1px solid var(--dsw-alias-border-l1);',
    'border-radius:12px;overflow:hidden;box-shadow:0 18px 50px rgba(0,0,0,.35)}',
    '.vc-modal-head{display:flex;align-items:center;gap:8px;padding:10px 13px;border-bottom:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1)}',
    '.vc-modal-title{font-size:13px;font-weight:650;flex:1 1 auto}',
    '.vc-modal-body{padding:12px 13px;overflow:auto;display:flex;flex-direction:column;gap:9px}',
    '.vc-modal-foot{display:flex;justify-content:flex-end;gap:7px;padding:9px 13px;border-top:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1)}',
    '.vc-drawer-mask{position:absolute;inset:0;background:rgba(4,8,16,.32);z-index:38}',
    '.vc-drawer{position:absolute;right:0;top:0;bottom:0;width:min(560px,94%);background:var(--dsw-alias-bg-base);border-left:1px solid var(--dsw-alias-border-l1);',
    'z-index:39;display:flex;flex-direction:column;box-shadow:-12px 0 38px rgba(0,0,0,.28)}',
    '.vc-drawer-head{display:flex;align-items:center;gap:8px;padding:10px 13px;border-bottom:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1)}',
    '.vc-drawer-body{padding:11px 13px;overflow:auto;display:flex;flex-direction:column;gap:9px}',
    '.vc-toasts{position:absolute;right:12px;bottom:12px;display:flex;flex-direction:column;gap:6px;z-index:60;max-width:360px}',
    '.vc-toast{border-radius:9px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);padding:7px 10px;font-size:11.5px;',
    'box-shadow:0 8px 24px rgba(0,0,0,.22);display:flex;flex-direction:column;gap:2px}',
    '.vc-toast.error{border-color:color-mix(in srgb,var(--dsw-alias-state-error-primary) 60%,transparent)}',
    '.vc-toast.ok{border-color:color-mix(in srgb,var(--dsw-alias-state-success-primary) 55%,transparent)}',
    '.vc-toast-title{font-weight:650;font-size:12px}',
    '.vc-toast-advice{color:var(--dsw-alias-label-secondary)}',
    '.vc-toast-close{align-self:flex-end;font-size:10px;color:var(--dsw-alias-label-tertiary);cursor:pointer}',
    // 杂项
    '.vc-sep{height:1px;background:var(--dsw-alias-border-l1);margin:2px 0}',
    '.vc-scroll{overflow:auto;min-height:0}',
    '.vc-grow{flex:1 1 auto;min-height:0}',
    '.vc-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}',
    '.vc-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}',
    '.vc-badge-engine{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--dsw-alias-border-l1);border-radius:999px;',
    'padding:3px 9px;font-size:11.5px;background:var(--dsw-alias-bg-layer-2);cursor:pointer;user-select:none}',
    '.vc-badge-engine:hover{border-color:var(--dsw-alias-border-l2)}',
    '.vc-popover{position:absolute;top:52px;right:14px;width:min(440px,92vw);max-height:70vh;overflow:auto;z-index:45;background:var(--dsw-alias-bg-base);',
    'border:1px solid var(--dsw-alias-border-l2);border-radius:11px;box-shadow:0 16px 44px rgba(0,0,0,.3);padding:11px 12px;display:flex;flex-direction:column;gap:8px}',
    '.vc-adapter{display:flex;align-items:flex-start;gap:7px;padding:4px 0;border-bottom:1px dashed var(--dsw-alias-border-l1);font-size:11.5px}',
    '.vc-adapter:last-child{border-bottom:none}',
    '.vc-adapter-name{font-weight:640;min-width:96px}',
    '.vc-adapter-cap{color:var(--dsw-alias-label-tertiary);flex:1 1 auto}',
    '.vc-nowrap{white-space:nowrap}',
    '.vc-right{overflow:auto}',
  ].join('')

  // ==========================================================================
  // 浏览器运行时：React 槽位 + createElement 简写 + hooks 包装
  // ==========================================================================

  function h(type, props) {
    var children = Array.prototype.slice.call(arguments, 2)
    return React.createElement.apply(React, [type, props].concat(children))
  }

  function useS(initial) { return React.useState(initial) }
  function useE(effect, deps) { return React.useEffect(effect, deps) }
  function useR(initial) { return React.useRef(initial) }
  function useM(factory, deps) { return React.useMemo(factory, deps) }
  function useC(callback, deps) { return React.useCallback(callback, deps) }

  function warnPlugin(message, error) {
    try { console.warn('[dsh-virtualcell] ' + message, error || '') } catch (ignored) { /* ignore */ }
  }

  function copyText(text) {
    var value = String(isNil(text) ? '' : text)
    try {
      if (navigator && navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(value)
    } catch (ignored) { /* 回退到 execCommand */ }
    try {
      var area = document.createElement('textarea')
      area.value = value
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      document.body.removeChild(area)
      return Promise.resolve()
    } catch (error) {
      return Promise.reject(error)
    }
  }

  // ==========================================================================
  // 图标（内联 SVG，避免依赖宿主图标包）
  // ==========================================================================

  var ICON_PATHS = {
    play: 'M5 3l12 7-12 7z',
    refresh: 'M4 9a6 6 0 0 1 10-3l2 2M18 15a6 6 0 0 1-10 3l-2-2M16 4v4h-4M6 20v-4h4',
    upload: 'M12 3v10M8 7l4-4 4 4M4 15v4h16v-4',
    sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z',
    close: 'M6 6l12 12M18 6L6 18',
    copy: 'M8 8h10v12H8zM5 16V4h11',
    chevron: 'M8 10l4 4 4-4',
    download: 'M12 4v10M8 10l4 4 4-4M5 19h14',
    info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 8v.5M12 11v6',
    check: 'M4 12l5 5L20 6',
    warn: 'M12 3l9 17H3zM12 9v5M12 17v.5',
    plus: 'M12 5v14M5 12h14',
    gene: 'M8 3c0 6 8 12 8 18M16 3c0 6-8 12-8 18',
    network: 'M5 6h4M15 6h4M7 6v6h10V6M12 12v6M9 20h6',
    screen: 'M4 5h16v10H4zM9 20h6M12 15v5',
    response: 'M4 17l5-6 4 3 6-8M15 6h4v4',
    transition: 'M4 7h9a4 4 0 0 1 0 8H7M9 11l-3 3 3 3',
    chart: 'M4 20V8M10 20V4M16 20v-7M22 20H2',
    dataset: 'M12 3c5 0 8 1.5 8 3s-3 3-8 3-8-1.5-8-3 3-3 8-3zM4 6v6c0 1.5 3 3 8 3s8-1.5 8-3V6M4 12v6c0 1.5 3 3 8 3s8-1.5 8-3v-6',
    folder: 'M3 6h6l2 2h10v11H3z',
    trash: 'M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13',
    reset: 'M4 12a8 8 0 1 0 2-5M4 4v5h5',
    arrow: 'M5 12h14M13 6l6 6-6 6',
  }

  function Glyph(props) {
    var name = props && props.name ? props.name : 'info'
    var size = props && props.size ? props.size : 14
    var path = ICON_PATHS[name] || ICON_PATHS.info
    return h('svg', {
      width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': 'true',
      style: { flex: '0 0 auto' },
    }, h('path', { d: path }))
  }

  function SidebarGlyph(props) {
    var size = props && props.size ? props.size : 18
    return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': 'true' },
      h('circle', { cx: 12, cy: 12, r: 9.2, stroke: 'currentColor', strokeWidth: 1.5 }),
      h('circle', { cx: 9.2, cy: 9.6, r: 2.6, fill: 'var(--dsw-alias-brand-primary)' }),
      h('circle', { cx: 15.2, cy: 13.6, r: 2.1, fill: 'var(--dsw-alias-state-success-primary)' }),
      h('circle', { cx: 9.8, cy: 16, r: 1.5, fill: 'var(--dsw-alias-state-warn-primary)' }),
      h('path', { d: 'M11.2 11.1l2.4 1.4M10.6 11.9l-.5 2.3', stroke: 'currentColor', strokeWidth: 1.1 }))
  }

  // ==========================================================================
  // 基础组件
  // ==========================================================================

  function Btn(props) {
    var p = props || {}
    var className = 'vc-btn' + (p.variant ? ' vc-' + p.variant : '') + (p.small ? ' vc-small' : '') + (p.className ? ' ' + p.className : '')
    return h('button', {
      type: 'button',
      className: className,
      title: p.title || undefined,
      disabled: p.disabled === true,
      onClick: p.onClick || undefined,
    }, p.icon ? h(Glyph, { name: p.icon, size: p.small ? 12 : 13 }) : null, p.children)
  }

  function Chip(props) {
    var p = props || {}
    var className = 'vc-chip' + (p.tone ? ' vc-chip-' + p.tone : '') + (p.active ? ' active' : '') + (p.live ? ' vc-chip-live' : '')
    return h('span', {
      className: className,
      title: p.title || undefined,
      onClick: p.onClick || undefined,
      style: p.onClick ? { cursor: 'pointer' } : undefined,
    }, p.swatch ? h('i', { style: { width: 8, height: 8, borderRadius: '50%', background: p.swatch, display: 'inline-block' } }) : null, p.children)
  }

  function Dot(props) {
    var status = props && props.status ? props.status : 'blocked'
    return h('span', { className: 'vc-dot ' + status, title: props && props.title ? props.title : undefined })
  }

  function KV(props) {
    var p = props || {}
    return h('div', { className: 'vc-kv', title: p.title || (typeof p.value === 'string' ? p.value : undefined) },
      h('span', { className: 'vc-k' }, p.label),
      h('span', { className: 'vc-v' }, isNil(p.value) || p.value === '' ? '—' : p.value))
  }

  function Field(props) {
    var p = props || {}
    return h('label', { className: 'vc-field', title: p.title || undefined },
      h('span', { className: 'vc-field-label' }, p.label,
        p.badge ? h(Chip, { tone: 'reco', onClick: p.onBadgeClick, title: p.badgeTitle || '' }, p.badge) : null),
      p.children)
  }

  function Section(props) {
    var p = props || {}
    return h('div', { className: 'vc-section' + (p.className ? ' ' + p.className : '') },
      p.title || p.extra ? h('div', { className: 'vc-row-between' },
        h('div', { className: 'vc-section-title' }, p.title || ''),
        p.extra ? h('div', { className: 'vc-muted' }, p.extra) : null) : null,
      p.children)
  }

  function Metric(props) {
    var p = props || {}
    return h('div', { className: 'vc-metric', title: p.hint || undefined },
      h('div', { className: 'vc-metric-k' }, p.label),
      h('div', { className: 'vc-metric-v' }, isNil(p.value) || p.value === '' ? '—' : p.value,
        p.unit ? h('small', { style: { fontWeight: 500, fontSize: 10.5, opacity: 0.7 } }, ' ' + p.unit) : null))
  }

  function Progress(props) {
    var pct = asNumber(props && props.pct)
    return h('div', { className: 'vc-progress', title: props && props.title ? props.title : undefined },
      h('i', { style: { width: (pct === null ? 45 : clamp(pct, 0, 100)) + '%' } }))
  }

  function Loading(props) {
    var p = props || {}
    return h('div', { className: 'vc-muted', style: { padding: '8px 2px' } },
      p.text || T('loading'), h('span', { className: 'vc-dot running', style: { marginLeft: 6 } }))
  }

  // @@COMPONENTS2@@
  function ErrorNote(props) {
    var p = props || {}
    return h('div', { className: 'vc-err-text', style: { fontSize: 11.5, display: 'flex', gap: 6, alignItems: 'flex-start' } },
      h(Glyph, { name: 'warn', size: 13 }),
      h('span', null, p.text || ''),
      p.onRetry ? h('button', { type: 'button', className: 'vc-btn vc-small', onClick: p.onRetry }, T('retry')) : null)
  }

  function EmptyBlock(props) {
    var p = props || {}
    return h('div', { className: 'vc-empty' },
      p.title ? h('div', { style: { fontWeight: 640, fontSize: 12.5, color: 'var(--dsw-alias-label-secondary)' } }, p.title) : null,
      p.hint ? h('div', null, p.hint) : null,
      p.children || null,
      p.actions ? h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } }, p.actions) : null)
  }

  function FigureBlock(props) {
    var p = props || {}
    var failedState = useS(false)
    var failed = failedState[0]
    var setFailed = failedState[1]
    var key = p.figure && p.figure.url ? p.figure.url : ''
    useE(function () { setFailed(false) }, [key])
    if (p.loading) {
      return h('div', { className: 'vc-figure' },
        h('div', { className: 'vc-figure-head' }, p.title || T('plotLoading')),
        h('div', { className: 'vc-figure-empty' }, h(Loading, { text: p.loadingText || T('plotLoading') })))
    }
    if (p.error) {
      return h('div', { className: 'vc-figure' },
        h('div', { className: 'vc-figure-head' }, p.title || ''),
        h('div', { className: 'vc-figure-empty' }, h(ErrorNote, { text: p.error, onRetry: p.onReload })))
    }
    if (!p.figure || !p.figure.url) {
      return h('div', { className: 'vc-figure' },
        h('div', { className: 'vc-figure-head' }, p.title || ''),
        h('div', { className: 'vc-figure-empty' }, p.emptyText || T('noFigure')))
    }
    return h('div', { className: 'vc-figure' },
      h('div', { className: 'vc-figure-head' },
        h('span', { style: { flex: '1 1 auto' } }, p.title || p.figure.title || ''),
        h('a', { className: 'vc-muted', href: p.figure.url, target: '_blank', rel: 'noreferrer' }, '原图')),
      failed
        ? h('div', { className: 'vc-figure-empty' }, T('figureFail'))
        : h('img', {
            src: p.figure.url,
            alt: p.figure.title || p.title || 'figure',
            loading: 'lazy',
            onError: function () { setFailed(true) },
          }))
  }

  function TableView(props) {
    var p = props || {}
    var table = p.table
    if (!table || !Array.isArray(table.rows) || !table.rows.length) {
      return h('div', { className: 'vc-muted', style: { padding: '6px 2px' } }, p.emptyText || T('noData'))
    }
    var columns = table.columns.length ? table.columns : table.rows[0].map(function (cell, index) { return 'v' + index })
    var limit = asNumber(p.maxRows) || MAX_TABLE_ROWS
    var rows = table.rows.slice(0, limit)
    function isNumericColumn(index) {
      for (var rowIndex = 0; rowIndex < Math.min(rows.length, 12); rowIndex++) {
        var cell = rows[rowIndex][index]
        if (isNil(cell) || cell === '') continue
        if (asNumber(cell) === null || typeof cell === 'boolean') return false
      }
      return true
    }
    return h('div', { className: 'vc-table-wrap' },
      h('table', { className: 'vc-table' },
        h('thead', null, h('tr', null, columns.map(function (name, index) {
          return h('th', { key: 'h' + index, title: name }, plainText(name, 80))
        }))),
        h('tbody', null, rows.map(function (row, rowIndex) {
          return h('tr', { key: 'r' + rowIndex }, columns.map(function (name, cellIndex) {
            var cell = row[cellIndex]
            var numeric = isNumericColumn(cellIndex)
            var display = isNil(cell) || cell === '' ? '—' : (numeric ? fmtNumber(cell, 3) : plainText(cell, 120))
            return h('td', { key: 'c' + cellIndex, className: numeric ? 'num' : '', title: plainText(cell, 200) }, display)
          }))
        }))),
      table.total > rows.length ? h('div', { className: 'vc-muted', style: { padding: '4px 8px' } }, '显示前 ' + rows.length + ' / ' + fmtInt(table.total) + ' 行') : null)
  }

  function Modal(props) {
    var p = props || {}
    return h('div', { className: 'vc-mask', onMouseDown: function (event) { if (event.target === event.currentTarget && p.onClose) p.onClose() } },
      h('div', { className: 'vc-modal', role: 'dialog', 'aria-modal': 'true' },
        h('div', { className: 'vc-modal-head' },
          h('div', { className: 'vc-modal-title' }, p.title || ''),
          p.onClose ? h('button', { type: 'button', className: 'vc-btn vc-icon vc-ghost', onClick: p.onClose, title: T('close') }, h(Glyph, { name: 'close', size: 13 })) : null),
        h('div', { className: 'vc-modal-body' }, p.children),
        p.footer ? h('div', { className: 'vc-modal-foot' }, p.footer) : null))
  }

  function Drawer(props) {
    var p = props || {}
    return h('div', null,
      h('div', { className: 'vc-drawer-mask', onClick: p.onClose || undefined }),
      h('div', { className: 'vc-drawer', role: 'dialog', 'aria-modal': 'true' },
        h('div', { className: 'vc-drawer-head' },
          h('div', { className: 'vc-modal-title' }, p.title || ''),
          p.extra || null,
          h('button', { type: 'button', className: 'vc-btn vc-icon vc-ghost', onClick: p.onClose, title: T('close') }, h(Glyph, { name: 'close', size: 13 }))),
        h('div', { className: 'vc-drawer-body' }, p.children)))
  }

  function ToastHost(props) {
    var p = props || {}
    var list = Array.isArray(p.toasts) ? p.toasts : []
    if (!list.length) return null
    return h('div', { className: 'vc-toasts' }, list.map(function (toast) {
      return h('div', { key: toast.id, className: 'vc-toast ' + (toast.tone || '') },
        h('div', { className: 'vc-toast-title' }, toast.title || ''),
        toast.advice ? h('div', { className: 'vc-toast-advice' }, toast.advice) : null,
        toast.message ? h('div', { className: 'vc-toast-advice' }, plainText(toast.message, 400)) : null,
        h('div', { className: 'vc-toast-close', onClick: function () { if (p.onClose) p.onClose(toast.id) } }, T('closeToast') || '关闭'))
    }))
  }

  /** 错误边界类：延迟到 factory() 里造（IIFE 求值时 React 还没注入）。 */
  var ErrorBoundaryImpl = null

  function makeErrorBoundary() {
    return class extends React.Component {
      constructor(props) {
        super(props)
        this.state = { error: null }
      }
      static getDerivedStateFromError(error) {
        return { error: error }
      }
      componentDidCatch(error, info) {
        warnPlugin('component crashed', error)
        try { console.error('[dsh-virtualcell] UI 组件异常', error, info) } catch (ignored) { /* ignore */ }
      }
      render() {
        if (this.state.error) {
          if (this.props.mini) {
            return h('span', { title: 'UI 组件异常：' + String(this.state.error && this.state.error.message || this.state.error) },
              h(SidebarGlyph, { size: 16 }))
          }
          return h('div', { style: { padding: 18, display: 'flex', flexDirection: 'column', gap: 8 } },
            h('div', { style: { fontWeight: 650 } }, T('crashTitle')),
            h('div', { className: 'vc-muted' }, T('crashHint')),
            h('pre', { className: 'vc-textarea', style: { maxHeight: 160, overflow: 'auto' } }, String(this.state.error && this.state.error.stack || this.state.error)))
        }
        return this.props.children
      }
    }
  }

  /** 整页错误边界：单个组件炸了不黑整页。 */
  function PluginErrorBoundary(props) {
    var p = props || {}
    return h(ErrorBoundaryImpl, { mini: p.mini === true }, p.children)
  }

  /** 空状态 / 缺少前置步骤时的统一引导。 */
  function NeedStep(props) {
    var p = props || {}
    return h(EmptyBlock, {
      title: p.title || T('stepNeedPrereq'),
      hint: p.hint || '',
      actions: p.children || null,
    })
  }

  // @@COMPONENTS3@@
  // ==========================================================================
  // 参数表单（number/select/boolean/text/genes/column；advanced 收进折叠）
  // ==========================================================================

  function ParamField(props) {
    var p = props || {}
    var spec = p.spec
    var value = p.value
    var reco = p.recommended
    var hasReco = !isNil(reco) && String(reco) !== String(value)
    var badge = hasReco ? tf('stepRecommended', formatParamValue(spec, reco)) : null
    var control = null
    var type = spec.type || 'text'
    if (type === 'number') {
      control = h('input', {
        className: 'vc-input', type: 'number',
        min: isNil(spec.min) ? undefined : spec.min,
        max: isNil(spec.max) ? undefined : spec.max,
        step: isNil(spec.step) ? 'any' : spec.step,
        value: isNil(value) ? '' : String(value),
        onChange: function (event) { p.onChange(event.target.value === '' ? '' : Number(event.target.value)) },
        disabled: p.disabled === true,
      })
    } else if (type === 'select') {
      var options = spec.options || []
      control = h('select', {
        className: 'vc-select',
        value: isNil(value) ? '' : String(value),
        onChange: function (event) { p.onChange(event.target.value) },
        disabled: p.disabled === true,
      }, options.map(function (option) {
        return h('option', { key: option.value, value: option.value }, option.label || option.value)
      }))
    } else if (type === 'boolean') {
      control = h('label', { className: 'vc-check' },
        h('input', {
          type: 'checkbox',
          checked: value === true || value === 'true',
          onChange: function (event) { p.onChange(event.target.checked) },
          disabled: p.disabled === true,
        }), value ? T('yes') : T('no'))
      return h('div', { className: 'vc-field' },
        h('div', { className: 'vc-field-label' }, spec.label),
        control,
        spec.effect ? h('div', { className: 'vc-muted', style: { fontSize: 10.5 } }, spec.effect) : null)
    } else if (type === 'genes') {
      control = h('textarea', {
        className: 'vc-textarea',
        value: Array.isArray(value) ? value.join(', ') : (isNil(value) ? '' : String(value)),
        placeholder: T('perturbGenePlaceholder') || 'KLF4, MYC',
        onChange: function (event) { p.onChange(event.target.value) },
        disabled: p.disabled === true,
        rows: 2,
      })
    } else if (type === 'column') {
      control = h('input', {
        className: 'vc-input', type: 'text', value: isNil(value) ? '' : String(value),
        onChange: function (event) { p.onChange(event.target.value) },
        disabled: p.disabled === true,
      })
    } else {
      control = h('input', {
        className: 'vc-input', type: 'text', value: isNil(value) ? '' : String(value),
        onChange: function (event) { p.onChange(event.target.value) },
        disabled: p.disabled === true,
      })
    }
    return h(Field, {
      label: spec.label || spec.key,
      badge: badge,
      badgeTitle: p.recommendWhy || tf('stepFillRecommended', ''),
      onBadgeClick: hasReco ? function () { p.onChange(reco) } : undefined,
    }, control, spec.effect ? h('div', { className: 'vc-muted', style: { fontSize: 10.5 } }, spec.effect) : null)
  }

  function ParamForm(props) {
    var p = props || {}
    var specs = Array.isArray(p.specs) ? p.specs : []
    var values = p.values || {}
    var recoMap = p.recommended || {}
    var basic = specs.filter(function (spec) { return spec.advanced !== true })
    var advanced = specs.filter(function (spec) { return spec.advanced === true })
    if (!specs.length) return h('div', { className: 'vc-muted' }, T('stepNoParams'))
    return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 7 } },
      basic.map(function (spec) {
        return h(ParamField, {
          key: spec.key, spec: spec, value: hasOwn(values, spec.key) ? values[spec.key] : spec.defaultValue,
          recommended: recoMap[spec.key], recommendWhy: (p.reasons || {})[spec.key],
          disabled: p.disabled === true, onChange: function (next) { p.onChange(spec.key, next) },
        })
      }),
      advanced.length ? h('details', { className: 'vc-advanced', open: p.showAdvanced === true },
        h('summary', null, T('stepAdvanced') + '（' + advanced.length + '）'),
        h('div', { className: 'vc-advanced-grid' }, advanced.map(function (spec) {
          return h(ParamField, {
            key: spec.key, spec: spec, value: hasOwn(values, spec.key) ? values[spec.key] : spec.defaultValue,
            recommended: recoMap[spec.key], disabled: p.disabled === true,
            onChange: function (next) { p.onChange(spec.key, next) },
          })
        }))) : null)
  }

  function hasOwn(object, key) {
    return Object.prototype.hasOwnProperty.call(object || {}, key)
  }

  // ==========================================================================
  // 顶栏：标题 / 引擎徽章（点开详情）/ demo / 导入 / 一键全流程 / 刷新
  // ==========================================================================

  function EnginePopover(props) {
    var p = props || {}
    var engine = p.engine || normalizeEngine(null)
    var packages = engine.packages || {}
    var packageNames = Object.keys(packages)
    return h('div', { className: 'vc-popover', onClick: function (event) { event.stopPropagation() } },
      h('div', { className: 'vc-row-between' },
        h('div', { style: { fontWeight: 650, fontSize: 12.5 } }, T('engineTitle')),
        h('div', { style: { display: 'flex', gap: 6 } },
          h(Btn, { small: true, icon: 'reset', onClick: p.onRestart, title: T('engineRestartConfirm') }, T('engineRestart')),
          h(Btn, { small: true, variant: 'ghost', icon: 'close', onClick: p.onClose }, T('close')))),
      h('div', { className: 'vc-card-body vc-tight' },
        h(KV, { label: T('enginePython'), value: engine.python || '—', title: engine.python }),
        h(KV, { label: T('engineVersion'), value: engine.pythonVersion || '—' }),
        h(KV, { label: T('engineBackend'), value: engine.backend || '—' }),
        h(KV, { label: T('engineWorkdir'), value: engine.workdir || '—', title: engine.workdir }),
        packageNames.length ? h('div', { style: { marginTop: 6 } },
          h('div', { className: 'vc-muted' }, T('enginePackages')),
          h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 } }, packageNames.map(function (name) {
            var version = packages[name]
            var ok = !isNil(version) && version !== ''
            return h(Chip, { key: name, swatch: ok ? 'var(--dsw-alias-state-success-primary)' : 'var(--dsw-alias-label-tertiary)', title: ok ? String(version) : T('engineMissing') },
              name + (ok ? ' ' + plainText(version, 20) : ' —'))
          }))) : null,
        h('div', { style: { marginTop: 8 } },
          h('div', { className: 'vc-muted' }, T('engineAdapters')),
          engine.adapters.length
            ? h('div', { style: { marginTop: 3 } }, engine.adapters.map(function (adapter) {
                return h('div', { className: 'vc-adapter', key: adapter.id },
                  h(Dot, { status: adapter.available ? 'done' : 'blocked', title: adapter.available ? T('engineAvailable') : T('engineMissing') }),
                  h('span', { className: 'vc-adapter-name' }, adapter.label),
                  h('span', { className: 'vc-adapter-cap' }, adapter.capability + (adapter.available ? '' : '（' + (adapter.hint || T('engineInstallHint')) + '）')))
              }))
            : h('div', { className: 'vc-muted' }, T('engineNoAdapters')))))
  }

  function RecipeMenu(props) {
    var p = props || {}
    return h('div', { className: 'vc-popover', style: { right: 14, width: 'min(380px,92vw)' }, onClick: function (event) { event.stopPropagation() } },
      h('div', { className: 'vc-row-between' },
        h('div', { style: { fontWeight: 650, fontSize: 12.5 } }, T('recipeMenuHint')),
        h(Btn, { small: true, variant: 'ghost', icon: 'close', onClick: p.onClose }, T('close'))),
      RECIPES.map(function (recipe) {
        return h('button', {
          key: recipe.id, type: 'button', className: 'vc-btn',
          style: { justifyContent: 'flex-start', textAlign: 'left', display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 2, padding: '7px 9px' },
          onClick: function () { if (p.onRun) p.onRun(recipe.id) },
        },
          h('span', { style: { fontWeight: 640 } }, T(recipe.titleKey) + ' · ' + T(recipe.timeKey)),
          h('span', { className: 'vc-muted', style: { whiteSpace: 'normal', fontSize: 11 } }, T(recipe.descKey)))
      }),
      h('div', { className: 'vc-muted', style: { fontSize: 10.5 } }, T('recipeHint')))
  }

  function TopBar(props) {
    var p = props || {}
    var state = p.state
    var engine = state && state.engine ? state.engine : normalizeEngine(null)
    var busy = state && state.busy ? state.busy : null
    return h('div', { className: 'vc-topbar' },
      h('div', { className: 'vc-logo' }, 'V'),
      h('div', null,
        h('div', { className: 'vc-title' }, T('appTitle')),
        h('div', { className: 'vc-subtitle' }, T('appSubtitle'))),
      h('div', { className: 'vc-topbar-spacer' }),
      h('div', { className: 'vc-topbar-actions' },
        h('button', {
          type: 'button', className: 'vc-badge-engine', title: T('engineBadgeHint'),
          onClick: function () { if (p.onToggleEngine) p.onToggleEngine() },
        },
          h(Dot, { status: engine.backend ? 'done' : 'blocked' }),
          h('span', null, (engine.backend || T('engineBad')) + (engine.pythonVersion ? ' · py' + engine.pythonVersion : '')),
          h(Glyph, { name: 'info', size: 13 }),
          busy ? h('span', { className: 'vc-chip vc-chip-live' }, T('busyChip')) : null),
        h(Btn, { icon: 'sparkle', onClick: p.onDemo, disabled: p.busy === true, title: T('demoHint') }, p.busy ? T('demoBusy') : T('demo')),
        h(Btn, { icon: 'upload', onClick: p.onImport }, T('importData')),
        h(Btn, { icon: 'play', variant: 'primary', onClick: function () { if (p.onToggleRecipe) p.onToggleRecipe() }, disabled: p.busy === true }, T('recipeRun')),
        h(Btn, { icon: 'refresh', onClick: p.onRefresh, disabled: p.refreshing === true, title: T('refreshHint') },
          p.refreshing ? T('refreshBusy') : T('refresh')))
    )
  }

  // @@COMPONENTS4@@
  // ==========================================================================
  // 导入弹窗：本地路径 / 上传文件 + 格式下拉 + 转置 + 高级（member/sheet/gene_col）
  // ==========================================================================

  var DATA_FORMATS = [
    { value: 'auto', labelKey: 'formatAuto' },
    { value: 'h5ad', labelKey: 'formatH5ad' },
    { value: '10x_h5', labelKey: 'format10xH5' },
    { value: '10x_mtx', labelKey: 'format10xMtx' },
    { value: 'loom', labelKey: 'formatLoom' },
    { value: 'matrix_text', labelKey: 'formatMatrix' },
    { value: 'geo_series', labelKey: 'formatGeo' },
    { value: 'archive', labelKey: 'formatArchive' },
    { value: 'xlsx', labelKey: 'formatXlsx' },
  ]

  function ImportDialog(props) {
    var p = props || {}
    var modeState = useS('path')
    var pathState = useS('')
    var formatState = useS('auto')
    var transposeState = useS(false)
    var memberState = useS('')
    var sheetState = useS('')
    var geneColState = useS('')
    var nameState = useS('')
    var inspectState = useS(null)
    var errorState = useS('')
    var busyState = useS(false)
    var fileState = useS(null)
    var mode = modeState[0]
    var path = pathState[0]
    var format = formatState[0]
    var busy = busyState[0] || p.busy === true

    function meta() {
      return {
        name: nameState[0] || undefined,
        data_format: format,
        transpose: transposeState[0],
        member: memberState[0] || undefined,
        sheet: sheetState[0] || undefined,
        gene_col: geneColState[0] || undefined,
      }
    }

    function runInspect() {
      if (!path) { errorState[1](T('importNeedSource')); return }
      busyState[1](true)
      errorState[1]('')
      inspectState[1](null)
      api().post('/inspect', { path: path, data_format: format, member: meta().member, transpose: meta().transpose, sheet: meta().sheet, gene_col: meta().gene_col }, { timeoutMs: HEAVY_API_TIMEOUT_MS })
        .then(function (result) {
          busyState[1](false)
          inspectState[1](result)
        })
        .catch(function (error) {
          busyState[1](false)
          errorState[1](describeError(error).advice)
        })
    }

    function submit() {
      if (mode === 'path' && !path) { errorState[1](T('importNeedSource')); return }
      if (mode === 'upload' && !fileState[0]) { errorState[1](T('importNeedSource')); return }
      if (p.onSubmit) p.onSubmit(mode === 'path' ? { kind: 'path', payload: Object.assign({ path: path }, meta()) } : { kind: 'upload', file: fileState[0], payload: meta() })
    }

    var inspect = inspectState[0]
    return h(Modal, {
      title: T('importTitle'),
      onClose: function () { if (p.onClose) p.onClose() },
      footer: [
        h(Btn, { key: 'cancel', onClick: function () { if (p.onClose) p.onClose() } }, T('cancel')),
        mode === 'path' ? h(Btn, { key: 'inspect', onClick: runInspect, disabled: busy || !path }, busy ? T('inspectBusy') : T('importInspect')) : null,
        h(Btn, { key: 'go', variant: 'primary', onClick: submit, disabled: busy }, busy ? T('importBusy') : T('importStart')),
      ],
    },
      h('div', { className: 'vc-muted' }, T('importSubtitle')),
      h('div', { style: { display: 'flex', gap: 6 } },
        h(Btn, { small: true, variant: mode === 'path' ? 'primary' : undefined, onClick: function () { modeState[1]('path') } }, T('importPath')),
        h(Btn, { small: true, variant: mode === 'upload' ? 'primary' : undefined, onClick: function () { modeState[1]('upload') } }, T('importUpload'))),
      mode === 'path'
        ? h(Field, { label: T('importPath'), title: T('importPathHint') },
            h('input', { className: 'vc-input', type: 'text', value: path, placeholder: T('importPathPlaceholder'), onChange: function (event) { pathState[1](event.target.value) } }))
        : h(Field, { label: T('importUpload'), title: T('importUploadHint') },
            h('input', { className: 'vc-input', type: 'file', onChange: function (event) { fileState[1](event.target.files && event.target.files[0] ? event.target.files[0] : null) } })),
      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 } },
        h(Field, { label: T('importFormat'), title: T('formatHint') },
          h('select', { className: 'vc-select', value: format, onChange: function (event) { formatState[1](event.target.value) } },
            DATA_FORMATS.map(function (item) { return h('option', { key: item.value, value: item.value }, T(item.labelKey)) }))),
        h(Field, { label: '数据集名（可选）' },
          h('input', { className: 'vc-input', type: 'text', value: nameState[0], placeholder: 'my-dataset', onChange: function (event) { nameState[1](event.target.value) } }))),
      h('label', { className: 'vc-check' },
        h('input', { type: 'checkbox', checked: transposeState[0], onChange: function (event) { transposeState[1](event.target.checked) } }),
        T('importTranspose'), h('span', { className: 'vc-muted' }, '— ' + T('importTransposeHint'))),
      h('details', { className: 'vc-advanced' },
        h('summary', null, T('importAdvanced')),
        h('div', { className: 'vc-advanced-grid' },
          h(Field, { label: T('importMember'), title: T('importMemberHint') }, h('input', { className: 'vc-input', type: 'text', value: memberState[0], onChange: function (event) { memberState[1](event.target.value) } })),
          h(Field, { label: T('importSheet'), title: T('importSheetHint') }, h('input', { className: 'vc-input', type: 'text', value: sheetState[0], onChange: function (event) { sheetState[1](event.target.value) } })),
          h(Field, { label: T('importGeneCol'), title: T('importGeneColHint') }, h('input', { className: 'vc-input', type: 'text', value: geneColState[0], onChange: function (event) { geneColState[1](event.target.value) } })))),
      errorState[0] ? h(ErrorNote, { text: errorState[0] }) : null,
      inspect ? h(Section, { title: T('inspectOk') },
        h(KV, { label: T('inspectCells'), value: fmtInt(inspect.n_cells) }),
        h(KV, { label: T('inspectGenes'), value: fmtInt(inspect.n_genes) }),
        h(KV, { label: T('inspectFormat'), value: plainText(inspect.format || inspect.data_format || '—', 60) }),
        Array.isArray(inspect.warnings) && inspect.warnings.length
          ? h('div', { className: 'vc-warn-text', style: { fontSize: 11 } }, T('inspectWarnings') + '：' + inspect.warnings.map(function (text) { return plainText(text, 160) }).join('；'))
          : null) : null)
  }

  // ==========================================================================
  // 左栏：数据集卡 + 10 张步骤卡 + tabs
  // ==========================================================================

  function DatasetCard(props) {
    var p = props || {}
    var state = p.state
    var dataset = state && state.activeDataset
    var others = state && Array.isArray(state.datasets) ? state.datasets.filter(function (item) { return item.id !== state.active }) : []
    if (!dataset) {
      return h('div', { className: 'vc-card' },
        h('div', { className: 'vc-card-head' }, h('div', { className: 'vc-card-title' }, T('datasetNone'))),
        h('div', { className: 'vc-card-body' },
          h('div', { className: 'vc-muted', style: { marginBottom: 7 } }, T('datasetNoneHint')),
          h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
            h(Btn, { small: true, icon: 'sparkle', onClick: p.onDemo }, T('demo')),
            h(Btn, { small: true, icon: 'upload', onClick: p.onImport }, T('importData')))))
    }
    var doneCount = state.stepsDone ? state.stepsDone.length : 0
    return h('div', { className: 'vc-card' },
      h('div', { className: 'vc-card-head' },
        h(Dot, { status: 'done' }),
        h('div', { className: 'vc-card-title', title: dataset.name }, dataset.name),
        others.length ? h('select', {
          className: 'vc-select', style: { width: 'auto', maxWidth: 96, fontSize: 11 },
          value: dataset.id,
          onChange: function (event) { if (p.onSelect) p.onSelect(event.target.value) },
          title: T('datasetSwitchHint'),
        }, [dataset].concat(others).map(function (item) { return h('option', { key: item.id, value: item.id }, item.name) })) : null,
        h(Btn, { small: true, variant: 'ghost', icon: 'close', title: T('datasetClose'), onClick: p.onCloseDataset })),
      h('div', { className: 'vc-card-body vc-tight' },
        h(KV, { label: T('datasetCells'), value: fmtInt(dataset.nCells) }),
        h(KV, { label: T('datasetGenes'), value: fmtInt(dataset.nGenes) }),
        h(KV, { label: T('datasetTfs'), value: fmtInt(dataset.nTfs) }),
        h(KV, { label: tf('datasetStepsDone', doneCount), value: doneCount + ' / ' + STEP_ORDER.length }),
        h(KV, { label: T('datasetSource'), value: dataset.source || '—' }),
        dataset.truth && (dataset.truth.hasGrnTruth || dataset.truth.hasPerturbTruth)
          ? h('div', { className: 'vc-chip', style: { marginTop: 3 } }, T('datasetTruth'))
          : null,
        dataset.warnings && dataset.warnings.length
          ? h('div', { className: 'vc-warn-text', style: { fontSize: 10.5, marginTop: 4 }, title: dataset.warnings.join('\n') },
              '⚠ ' + plainText(dataset.warnings[0], 90) + (dataset.warnings.length > 1 ? '（+' + (dataset.warnings.length - 1) + '）' : ''))
          : null,
        dataset.attachments && dataset.attachments.length
          ? h('div', { className: 'vc-muted', style: { fontSize: 10.5, marginTop: 3 } }, '附件 ' + dataset.attachments.length + ' 个：' + dataset.attachments.slice(0, 3).map(function (item) { return item.name }).join('、'))
          : null))
  }

  function StepCard(props) {
    var p = props || {}
    var meta = p.meta || { id: '', title: '', params: [], requires: [] }
    var status = p.status || 'blocked'
    var record = p.record || null
    var result = p.result || null
    var busy = p.busy === true
    var showWhy = p.expanded === true
    var missing = (meta.requires || []).filter(function (id) { return !(p.doneSet || {})[id] })
    return h('div', { className: 'vc-step vc-step-' + status },
      h('div', { className: 'vc-step-head', onClick: p.onToggle, title: meta.why || '' },
        h(Dot, { status: status }),
        h('span', { className: 'vc-step-idx' }, String((p.index || 0) + 1)),
        h('span', { className: 'vc-step-title' }, meta.title || stepTitle(meta.id)),
        p.recommendedCount ? h(Chip, { tone: 'reco', title: T('stepRecommended') }, '推荐 ' + p.recommendedCount) : null,
        record && record.ms ? h(Chip, { title: T('stepLastRun') }, formatDuration(record.ms)) : null,
        busy ? h(Chip, { tone: 'live' }, T('stepStatusRunning')) : null,
        h('span', { className: 'vc-step-caret' }, showWhy ? '▾' : '▸')),
      showWhy
        ? h('div', { className: 'vc-step-body' },
            meta.why ? h('div', { className: 'vc-step-why' }, plainText(meta.why, 220)) : null,
            status === 'blocked' && missing.length
              ? h('div', { className: 'vc-warn-text', style: { fontSize: 11 } }, tf('stepNeedPrereq', missing.map(stepTitle).join('、')))
              : null,
            busy ? h(Progress, { pct: p.busyPct, title: T('stepRunning') }) : null,
            h(ParamForm, {
              specs: meta.params || [],
              values: p.values || {},
              recommended: p.recommended || {},
              reasons: p.reasons || {},
              showAdvanced: p.showAdvanced === true,
              disabled: busy,
              onChange: p.onChange || function () {},
            }),
            h('div', { className: 'vc-step-actions' },
              h(Btn, {
                small: true, icon: 'play', variant: status === 'done' ? undefined : 'primary',
                disabled: busy || status === 'blocked',
                onClick: p.onRun,
                title: status === 'blocked' ? tf('stepNeedPrereq', missing.map(stepTitle).join('、')) : undefined,
              }, busy ? T('stepRunning') : (status === 'done' ? T('stepRerun') : T('stepRun'))),
              h(Btn, { small: true, icon: 'sparkle', disabled: busy, onClick: p.onSuggest, title: T('stepFillRecommended') }, '推荐值'),
              meta.cost ? h('span', { className: 'vc-muted', style: { fontSize: 10, marginLeft: 'auto' } }, meta.cost) : null),
            record && record.summary ? h('div', { className: 'vc-step-summary' }, plainText(record.summary, 260)) : null,
            record && record.ms ? h('div', { className: 'vc-muted', style: { fontSize: 10.5 } }, T('stepLastRun') + '：' + formatDuration(record.ms) + ' · ' + formatClock(record.at)) : null,
            record && record.warnings && record.warnings.length
              ? h('div', { className: 'vc-warn-text', style: { fontSize: 10.5 } }, '⚠ ' + plainText(record.warnings[0], 160))
              : null,
            result && result.figures && result.figures.length
              ? h('a', {
                  className: 'vc-muted', style: { fontSize: 10.5, cursor: 'pointer' },
                  onClick: function (event) { event.preventDefault(); if (p.onOpenTab) p.onOpenTab(p.tabForStep || 'state') },
                }, T('stepPlotDone'))
              : null)
        : null)
  }

  function TabsBar(props) {
    var p = props || {}
    return h('div', { className: 'vc-tabs' }, TAB_META.map(function (tab) {
      var missing = (tab.requires || []).filter(function (id) { return !(p.doneSet || {})[id] })
      var className = 'vc-tab' + (p.active === tab.key ? ' active' : '')
      return h('button', {
        key: tab.key, type: 'button', className: className,
        title: missing.length ? tf('stepNeedPrereq', missing.map(stepTitle).join('、')) : undefined,
        onClick: function () { if (p.onChange) p.onChange(tab.key) },
      }, T(tab.labelKey), missing.length ? h('span', { className: 'vc-tab-need' }, '·') : null)
    }))
  }

  function EmptyGuide(props) {
    var p = props || {}
    var dataset = p.state ? p.state.activeDataset : null
    var doneSet = p.doneSet || {}
    var hasState = doneSet.state === true
    var hasPerturb = doneSet.perturb === true
    return h('div', { className: 'vc-panel' },
      h('div', { className: 'vc-empty', style: { borderStyle: 'solid' } },
        h('div', { style: { fontWeight: 650, fontSize: 13 } }, T('onboardTitle')),
        h('div', null, T('onboardIntro'))),
      h('div', { className: 'vc-onboard' },
        h('div', { className: 'vc-onboard-card' },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 6 } },
            h('span', { className: 'vc-onboard-num' }, '1'),
            h('strong', null, T('onboardStep1'))),
          h('div', { className: 'vc-muted' }, T('onboardStep1Desc')),
          h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
            h(Btn, { small: true, variant: 'primary', icon: 'sparkle', disabled: p.busy === true, onClick: p.onDemo }, dataset ? T('onboardDemo') : T('onboardDemo')),
            h(Btn, { small: true, icon: 'upload', onClick: p.onImport }, T('onboardImport')))),
        h('div', { className: 'vc-onboard-card' },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 6 } },
            h('span', { className: 'vc-onboard-num' }, '2'),
            h('strong', null, T('onboardStep2'))),
          h('div', { className: 'vc-muted' }, T('onboardStep2Desc')),
          h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
            h(Btn, { small: true, variant: dataset ? 'primary' : undefined, icon: 'play', disabled: p.busy === true || !dataset, onClick: p.onQuick }, T('onboardQuick')),
            hasState ? h(Chip, { swatch: 'var(--dsw-alias-state-success-primary)' }, '✓ ' + T('stepStatusDone')) : null)),
        h('div', { className: 'vc-onboard-card' },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 6 } },
            h('span', { className: 'vc-onboard-num' }, '3'),
            h('strong', null, T('onboardStep3'))),
          h('div', { className: 'vc-muted' }, T('onboardStep3Desc')),
          h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
            h(Btn, { small: true, icon: 'arrow', disabled: !hasPerturb, onClick: p.onOpenPerturb }, T('onboardOpenPerturb')),
            h(Chip, { swatch: hasPerturb ? 'var(--dsw-alias-state-success-primary)' : 'var(--dsw-alias-label-tertiary)' },
              hasPerturb ? '✓ ' + T('stepStatusDone') : T('pending'))))),
      h('div', { className: 'vc-muted', style: { fontSize: 11 } }, T('onboardTip')))
  }

  // ==========================================================================
  // 中栏 · 状态图 canvas 散点（缩放/拖拽/双击复位/hover/图例点选/扰动箭头）
  // ==========================================================================

  function StateCanvas(props) {
    var p = props || {}
    var points = Array.isArray(p.points) ? p.points : []
    var wrapRef = useR(null)
    var canvasRef = useR(null)
    var sizeState = useS({ width: 640, height: 380 })
    var viewState = useS(null)
    var hoverState = useS(null)
    var dragRef = useR(null)
    var size = sizeState[0]
    var viewport = viewState[0]
    var hover = hoverState[0]
    var hidden = p.hidden || {}

    function measure() {
      var wrapper = wrapRef.current
      if (!wrapper) return
      var width = Math.max(220, wrapper.clientWidth || 0)
      var height = Math.max(220, wrapper.clientHeight || 0)
      sizeState[1](function (previous) {
        if (previous.width === width && previous.height === height) return previous
        return { width: width, height: height }
      })
    }

    function fit() {
      var next = buildViewport(points, size.width, size.height, 34)
      viewState[1](next)
      return next
    }

    useE(function () {
      measure()
      var wrapper = wrapRef.current
      if (!wrapper || typeof ResizeObserver === 'undefined') {
        window.addEventListener('resize', measure)
        return function () { window.removeEventListener('resize', measure) }
      }
      var observer = new ResizeObserver(measure)
      observer.observe(wrapper)
      return function () { observer.disconnect() }
    }, [])

    useE(function () {
      viewState[1](buildViewport(points, size.width, size.height, 34))
    }, [size.width, size.height, points.length, p.fitKey])

    // 画布：每次数据/视口/悬停变化重绘（devicePixelRatio 适配）
    useE(function () {
      var canvas = canvasRef.current
      if (!canvas) return
      var ratio = Math.max(1, Math.min(3, (typeof window !== 'undefined' && window.devicePixelRatio) || 1))
      canvas.width = Math.round(size.width * ratio)
      canvas.height = Math.round(size.height * ratio)
      var ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
      ctx.clearRect(0, 0, size.width, size.height)
      var current = viewport || buildViewport(points, size.width, size.height, 34)
      var textColor = '#8a94a6'
      try { textColor = getComputedStyle(canvas).color || textColor } catch (ignored) { /* ignore */ }
      var isClusterMode = points.some(function (point) { return point.kind === 'cluster' })
      var visible = points.filter(function (point) { return !hidden[String(point.cluster)] })

      // 扰动箭头（画在点下面）
      var overlay = p.overlay
      if (overlay && overlay.active) {
        var byCluster = {}
        for (var pointIndex = 0; pointIndex < points.length; pointIndex++) byCluster[String(points[pointIndex].cluster)] = points[pointIndex]
        var arrows = overlay.perCluster || []
        for (var arrowIndex = 0; arrowIndex < arrows.length; arrowIndex++) {
          var arrow = arrows[arrowIndex]
          var from = byCluster[String(arrow.cluster)]
          var to = byCluster[String(arrow.targetCluster)]
          if (!from || !to) continue
          var fromPoint = projectPoint(current, from.x, from.y)
          var toPoint = projectPoint(current, to.x, to.y)
          var dx = toPoint.x - fromPoint.x
          var dy = toPoint.y - fromPoint.y
          var length = Math.sqrt(dx * dx + dy * dy) || 1
          var ux = dx / length
          var uy = dy / length
          var startX = fromPoint.x + ux * 6
          var startY = fromPoint.y + uy * 6
          var endX = toPoint.x - ux * 9
          var endY = toPoint.y - uy * 9
          ctx.strokeStyle = 'rgba(225,87,89,.85)'
          ctx.fillStyle = 'rgba(225,87,89,.85)'
          ctx.lineWidth = 1.6
          ctx.beginPath()
          ctx.moveTo(startX, startY)
          ctx.lineTo(endX, endY)
          ctx.stroke()
          ctx.beginPath()
          ctx.moveTo(endX, endY)
          ctx.lineTo(endX - ux * 7 - uy * 3.4, endY - uy * 7 + ux * 3.4)
          ctx.lineTo(endX - ux * 7 + uy * 3.4, endY - uy * 7 - ux * 3.4)
          ctx.closePath()
          ctx.fill()
          if (arrow.deltaNorm !== null && arrow.deltaNorm !== undefined) {
            ctx.fillStyle = 'rgba(225,87,89,.95)'
            ctx.font = '10px ui-monospace,monospace'
            ctx.fillText('Δ' + fmtNumber(arrow.deltaNorm, 2), (startX + endX) / 2 + 4, (startY + endY) / 2 - 4)
          }
        }
      }

      for (var index = 0; index < visible.length; index++) {
        var point = visible[index]
        var screen = projectPoint(current, point.x, point.y)
        if (screen.x < -30 || screen.y < -30 || screen.x > size.width + 30 || screen.y > size.height + 30) continue
        var clusterKey = String(point.cluster)
        var color = p.colorOf ? p.colorOf(point, index) : colorForName(clusterKey, index)
        var radius = isClusterMode ? 6 + 13 * Math.sqrt(clamp(point.share === null || point.share === undefined ? 0.4 : point.share, 0.05, 1)) : 2.6
        ctx.beginPath()
        ctx.arc(screen.x, screen.y, radius, 0, Math.PI * 2)
        ctx.fillStyle = withAlpha(color, isClusterMode ? 0.62 : 0.72)
        ctx.fill()
        ctx.lineWidth = 1
        ctx.strokeStyle = withAlpha(color, 0.95)
        ctx.stroke()
        if (isClusterMode) {
          ctx.fillStyle = textColor
          ctx.font = '11px ui-monospace,monospace'
          ctx.textAlign = 'center'
          ctx.fillText(plainText(point.label || clusterKey, 14), screen.x, screen.y + radius + 12)
        }
      }

      if (hover && hover.point) {
        var hoveredScreen = projectPoint(current, hover.point.x, hover.point.y)
        ctx.beginPath()
        ctx.arc(hoveredScreen.x, hoveredScreen.y, (isClusterMode ? 16 : 6), 0, Math.PI * 2)
        ctx.strokeStyle = 'rgba(255,255,255,.9)'
        ctx.lineWidth = 2
        ctx.stroke()
      }
    }, [points, viewport, size.width, size.height, hidden, hover, p.overlay, p.colorOf])

    function pointAt(px, py) {
      var current = viewport
      if (!current) return null
      var isClusterMode = points.some(function (point) { return point.kind === 'cluster' })
      var best = null
      var bestDistance = isClusterMode ? 26 : 9
      for (var index = 0; index < points.length; index++) {
        var point = points[index]
        if (hidden[String(point.cluster)]) continue
        var screen = projectPoint(current, point.x, point.y)
        var distance = Math.sqrt((screen.x - px) * (screen.x - px) + (screen.y - py) * (screen.y - py))
        if (distance <= bestDistance) { best = { point: point, screen: screen }; bestDistance = distance }
      }
      return best
    }

    function localPosition(event) {
      var canvas = canvasRef.current
      if (!canvas) return { x: 0, y: 0 }
      var rect = canvas.getBoundingClientRect()
      return { x: event.clientX - rect.left, y: event.clientY - rect.top }
    }

    // wheel 必须非 passive 才能 preventDefault
    useE(function () {
      var canvas = canvasRef.current
      if (!canvas) return
      function onWheel(event) {
        event.preventDefault()
        var position = localPosition(event)
        var factor = Math.pow(1.0016, -event.deltaY)
        viewState[1](function (previous) { return zoomViewport(previous, position.x, position.y, factor) })
      }
      canvas.addEventListener('wheel', onWheel, { passive: false })
      return function () { canvas.removeEventListener('wheel', onWheel) }
    }, [viewport])

    function onMouseDown(event) {
      if (event.button !== 0) return
      var position = localPosition(event)
      dragRef.current = { startX: position.x, startY: position.y, viewport: viewport, moved: false }
      try { event.currentTarget.setPointerCapture(event.pointerId) } catch (ignored) { /* ignore */ }
    }

    function onMouseMove(event) {
      var position = localPosition(event)
      var drag = dragRef.current
      if (drag) {
        var dx = position.x - drag.startX
        var dy = position.y - drag.startY
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true
        if (drag.moved) viewState[1](panViewport(drag.viewport, dx, dy))
      }
      if (!drag || !drag.moved) {
        var hit = pointAt(position.x, position.y)
        hoverState[1](hit)
      }
    }

    function onMouseUp() {
      dragRef.current = null
    }

    function onMouseLeave() {
      dragRef.current = null
      hoverState[1](null)
    }

    var tip = null
    if (hover && hover.point) {
      var tipX = clamp(hover.screen.x + 12, 6, Math.max(6, size.width - 180))
      var tipY = clamp(hover.screen.y + 12, 6, Math.max(6, size.height - 60))
      tip = h('div', { className: 'vc-canvas-tip', style: { left: tipX, top: tipY } }, p.tooltipFor ? p.tooltipFor(hover.point) : plainText(hover.point.label || hover.point.cluster, 100))
    }

    return h('div', { className: 'vc-canvas-wrap', ref: wrapRef },
      h('canvas', {
        className: 'vc-canvas' + (dragRef.current && dragRef.current.moved ? ' vc-grabbing' : ''),
        ref: canvasRef,
        style: { width: size.width + 'px', height: size.height + 'px' },
        onMouseDown: onMouseDown,
        onMouseMove: onMouseMove,
        onMouseUp: onMouseUp,
        onMouseLeave: onMouseLeave,
        onDoubleClick: function () { fit() },
      }),
      h('div', { className: 'vc-canvas-hud' }, (p.hud || []).filter(Boolean).map(function (item, hudIndex) {
        return h('span', { key: 'hud-' + hudIndex, style: { display: 'contents' } }, item)
      })),
      tip,
      !points.length ? h('div', { className: 'vc-figure-empty', style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' } },
        T('stateNoData')) : null)
  }

  // ==========================================================================
  // 中栏 · 状态图 tab（着色切换 / 图例点选 / 扰动叠加 / 静态图）
  // ==========================================================================

  function normalizePerturbResult(raw, fallbackGene) {
    var result = raw && typeof raw === 'object' ? raw : {}
    var metrics = result.metrics && typeof result.metrics === 'object' ? result.metrics : {}
    var affected = []
    if (Array.isArray(result.affected)) {
      for (var index = 0; index < result.affected.length; index++) {
        var row = result.affected[index] || {}
        affected.push({
          gene: plainText(row.gene || '', 80),
          delta: asNumber(row.delta),
          rank: asNumber(row.rank) || index + 1,
          isTf: row.is_tf === true,
        })
      }
    }
    var perCluster = []
    if (Array.isArray(result.per_cluster)) {
      for (var clusterIndex = 0; clusterIndex < result.per_cluster.length; clusterIndex++) {
        var item = result.per_cluster[clusterIndex] || {}
        perCluster.push({
          cluster: plainText(item.cluster || '', 40),
          deltaNorm: asNumber(item.delta_norm),
          targetCluster: plainText(item.target_cluster || '', 40),
          cos: asNumber(item.cos),
        })
      }
    }
    var validity = result.validity && typeof result.validity === 'object' ? result.validity : {}
    return {
      active: !!(result.gene || result.summary),
      gene: plainText(result.gene || fallbackGene || '', 80),
      mode: plainText(result.mode || '', 30),
      strength: asNumber(result.strength),
      summary: plainText(result.summary || '', 1000),
      stateShift: asNumber(result.state_shift !== undefined ? result.state_shift : metrics.state_shift),
      targetState: plainText(result.target_state || metrics.target_state || '', 40),
      sourceState: plainText(result.source_state || metrics.source_state || '', 40),
      confidence: plainText(metrics.confidence || '', 30),
      backend: plainText(metrics.backend || result.backend || '', 40),
      metrics: metrics,
      affected: affected,
      perCluster: perCluster,
      validity: {
        grnSource: plainText(validity.grn_source || '', 120),
        nEdgesUsed: asNumber(validity.n_edges_used),
        propagation: asNumber(validity.propagation),
        notes: Array.isArray(validity.notes) ? validity.notes.map(function (text) { return plainText(text, 400) }) : [],
      },
    }
  }

  /** annotate 结果表 → 簇号 → 状态名 映射（表头列名不固定，按关键词猜）。 */
  function cellStateMapOf(stateObj) {
    var annotate = resultOf(stateObj, 'annotate')
    var map = {}
    if (!annotate || !Array.isArray(annotate.tables)) return map
    for (var tableIndex = 0; tableIndex < annotate.tables.length; tableIndex++) {
      var table = normalizeTable(annotate.tables[tableIndex], tableIndex)
      var headers = table.columns.map(function (name) { return String(name).toLowerCase() })
      var clusterCol = -1
      var stateCol = -1
      for (var columnIndex = 0; columnIndex < headers.length; columnIndex++) {
        if (clusterCol < 0 && /cluster|簇/.test(headers[columnIndex])) clusterCol = columnIndex
        if (stateCol < 0 && /state|label|name|状态|类型|cell_type/.test(headers[columnIndex])) stateCol = columnIndex
      }
      if (clusterCol < 0 || stateCol < 0 || clusterCol === stateCol) continue
      for (var rowIndex = 0; rowIndex < table.rows.length; rowIndex++) {
        var row = table.rows[rowIndex]
        var clusterName = plainText(row[clusterCol], 40)
        var stateName = plainText(row[stateCol], 60)
        if (clusterName && stateName) map[clusterName] = stateName
      }
    }
    return map
  }

  function stateTabPoints(stateObj, embedding, colorBy, gene) {
    var stateResult = resultOf(stateObj, 'state')
    var stateMap = cellStateMapOf(stateObj)
    if (Array.isArray(embedding) && embedding.length) {
      return embedding.map(function (point, index) {
        var cluster = plainText(point.cluster || '', 60)
        return { x: point.x, y: point.y, cluster: cluster, label: cluster, kind: 'cell', index: index }
      })
    }
    var labels = stateResult && Array.isArray(stateResult.cluster_labels) ? stateResult.cluster_labels : []
    var sizes = stateResult && Array.isArray(stateResult.cluster_sizes) ? stateResult.cluster_sizes : []
    if (!labels.length) return []
    var layout = buildClusterLayout(labels.map(function (label) {
      var name = stateMap[String(label)]
      return name ? String(label) + ' · ' + name : String(label)
    }), sizes)
    return layout.map(function (item) {
      return { x: item.x, y: item.y, cluster: item.cluster, label: item.label, size: item.size, share: item.share, kind: 'cluster' }
    })
  }

  function StateTab(props) {
    var p = props || {}
    var state = p.state
    var stateResult = resultOf(state, 'state')
    var colorByState = useS('cluster')
    var hiddenState = useS({})
    var geneState = useS('')
    var fitState = useS(0)
    var colorBy = colorByState[0]
    var hidden = hiddenState[0]
    var embedding = p.embedding || []
    var points = useM(function () { return stateTabPoints(state, embedding, colorBy, geneState[0]) }, [state, embedding, colorBy])
    var overlayRaw = resultOf(state, 'perturb')
    var overlay = useM(function () {
      var normalized = normalizePerturbResult(overlayRaw, '')
      return normalized.affected.length || normalized.perCluster.length ? normalized : null
    }, [overlayRaw])
    var metrics = stateResult && stateResult.metrics ? stateResult.metrics : {}
    var figures = useM(function () { return stateResult ? normalizeFigureList(stateResult.figures) : [] }, [stateResult])
    var clusters = useM(function () {
      var seen = {}
      var out = []
      for (var index = 0; index < points.length; index++) {
        var key = String(points[index].cluster)
        if (seen[key]) continue
        seen[key] = true
        var base = points[index].label || key
        out.push({ key: key, label: base })
      }
      return out
    }, [points])

    if (!stateResult) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('tabNeedState'), hint: T('stateNoDataHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('state') } }, T('stepRun') + '「' + stepTitle('state') + '」')))
    }

    var schematic = !embedding.length
    var shownFigure = p.plotFigure || (figures.length ? figures[0] : null)
    return h('div', { className: 'vc-panel' },
      h('div', { className: 'vc-toolbar' },
        h('span', { className: 'vc-toolbar-label' }, T('stateColorBy')),
        h('select', {
          className: 'vc-select', style: { width: 130 },
          value: colorBy, onChange: function (event) { colorByState[1](event.target.value) },
        },
          h('option', { value: 'cluster' }, T('stateColorCluster')),
          h('option', { value: 'cell_state' }, T('stateColorCellState')),
          h('option', { value: 'gene' }, T('stateColorGene'))),
        colorBy === 'gene' ? h('input', {
          className: 'vc-input', style: { width: 120 }, placeholder: T('stateGenePlaceholder') || 'KLF4',
          value: geneState[0], onChange: function (event) { geneState[1](event.target.value) },
        }) : null,
        h(Btn, { small: true, icon: 'reset', onClick: function () { fitState[1](fitState[0] + 1) } }, T('stateResetView')),
        h(Btn, {
          small: true, icon: 'chart', disabled: p.plotting === true,
          onClick: function () {
            if (p.onPlot) p.onPlot(overlay ? 'state_embedding_perturb' : 'state_embedding', overlay ? { gene: overlay.gene } : {})
          },
        }, p.plotting ? T('plotLoading') : T('stateShowStatic')),
        overlay ? h(Chip, { tone: 'live', title: T('perturbArrowNote') }, T('stateOverlayPerturb') + '：' + (overlay.gene || '—')) : null,
        h('span', { className: 'vc-muted', style: { marginLeft: 'auto', fontSize: 10.5 } },
          schematic ? '簇级示意：' + fmtInt(points.length) + ' 个状态（后端未提供单细胞坐标）' : tf('statePoints', points.length))),
      h('div', { className: 'vc-metrics' },
        h(Metric, { label: T('metricCells'), value: fmtInt(metrics.n_cells) }),
        h(Metric, { label: T('metricClusters'), value: fmtInt(metrics.n_clusters) }),
        h(Metric, { label: T('metricSilhouette'), value: fmtNumber(metrics.silhouette, 3) }),
        h(Metric, { label: T('metricVariance'), value: fmtPct(metrics.variance_explained) }),
        h(Metric, { label: T('metricHvg'), value: fmtInt(metrics.n_hvg) }),
        h(Metric, { label: T('metricPcs'), value: fmtInt(metrics.n_pcs) })),
      h(StateCanvas, {
        points: points,
        overlay: overlay,
        hidden: hidden,
        fitKey: fitState[0],
        colorOf: function (point) {
          if (colorBy === 'cell_state') return colorForName(point.label || point.cluster)
          return colorForName(point.cluster)
        },
        tooltipFor: function (point) {
          var lines = []
          var stateName = cellStateMapOf(state)[String(point.cluster)]
          lines.push('簇 ' + point.cluster + (stateName ? ' · ' + stateName : ''))
          if (point.size !== null && point.size !== undefined) lines.push('细胞数 ' + fmtInt(point.size) + (point.share ? '（相对最大簇 ' + fmtPct(point.share) + '）' : ''))
          if (overlay) {
            var row = null
            for (var index = 0; index < overlay.perCluster.length; index++) {
              if (String(overlay.perCluster[index].cluster) === String(point.cluster)) { row = overlay.perCluster[index]; break }
            }
            if (row) lines.push('Δ 位移 ' + fmtNumber(row.deltaNorm, 3) + (row.targetCluster ? ' → 簇 ' + row.targetCluster : ''))
            lines.push('扰动：' + overlay.gene + ' ' + overlay.mode)
          }
          return lines.join('；')
        },
        hud: [
          h('span', { className: 'vc-chip', title: T('stateHoverHint') }, T('stateInteractive')),
          schematic ? h('span', { className: 'vc-chip', title: '单细胞坐标接口暂未提供；当前按真实簇规模画簇级示意图，交互、图例与扰动箭头都基于真实结果' }, '簇级示意') : null,
        ],
      }),
      h('div', { className: 'vc-legend' },
        clusters.map(function (cluster) {
          var off = hidden[cluster.key] === true
          return h('span', {
            key: cluster.key, className: 'vc-legend-item' + (off ? ' off' : ''),
            title: (off ? T('stateHidden') : T('stateLegend')) + '：' + cluster.label,
            onClick: function () {
              hiddenState[1](function (previous) {
                var next = Object.assign({}, previous)
                next[cluster.key] = !next[cluster.key]
                return next
              })
            },
          }, h('i', { className: 'vc-legend-swatch', style: { background: colorForName(cluster.key) } }),
            plainText(cluster.label, 24))
        })),
      colorBy === 'gene'
        ? h('div', { className: 'vc-warn-text', style: { fontSize: 11 } }, '基因着色需要逐细胞表达接口（/preview 目前只返回基因名）；已回退为按簇着色，可点「出静态图」看后端按基因着色的版本。')
        : null,
      shownFigure ? h(FigureBlock, { figure: shownFigure, title: T('stateStatic') }) : null,
      stateResult.tables && stateResult.tables.length
        ? h(Section, { title: T('tables'), extra: T('stateClusters') },
            stateResult.tables.slice(0, 2).map(function (table, index) {
              return h('div', { key: index, style: { marginBottom: 6 } },
                h(TableView, { table: normalizeTable(table, index), maxRows: 12 }))
            }))
        : null,
      stateResult.summary ? h('div', { className: 'vc-muted', style: { fontSize: 11.5 } }, plainText(stateResult.summary, 500)) : null)
  }

  // ==========================================================================
  // 指标标签 / 结果合并（/state digest + 本次会话完整 /run 结果）
  // ==========================================================================

  var METRIC_LABEL_KEYS = {
    n_cells: 'metricCells', n_genes: 'metricGenes', n_clusters: 'metricClusters', n_states: 'metricStates',
    silhouette: 'metricSilhouette', variance_explained: 'metricVariance', n_hvg: 'metricHvg', n_pcs: 'metricPcs',
    n_tfs: 'metricNtf', n_edges: 'metricEdges', density: 'metricDensity', cv_r2: 'metricCvR2',
    grn_auroc: 'metricAuroc', auprc: 'metricAuprc', grn_auprc: 'metricAuprc',
    delta_corr: 'metricSpearman', spearman: 'metricSpearman', pearson: 'metricPearson', r2: 'metricR2',
    n_affected: 'metricAffected', max_abs_delta: 'metricMaxDelta', state_shift: 'metricStateShift',
    target_state: 'metricTargetState', source_state: 'metricSourceState', confidence: 'metricConfidence',
    backend: 'metricBackend', stability: 'metricStability', n_pert: 'metricNPert', method: 'metricMethod',
    score: 'metricScore', n_targets: 'metricTargets',
  }

  function metricLabel(key) {
    var localeKey = METRIC_LABEL_KEYS[key]
    return localeKey ? T(localeKey) : plainText(key, 40)
  }

  function formatMetricValue(key, value) {
    if (isNil(value) || value === '') return '—'
    if (typeof value === 'string') return plainText(value, 60)
    var number = asNumber(value)
    if (number === null) return plainText(value, 60)
    if (key === 'variance_explained') return fmtPct(number)
    if (/state|confidence|backend|method|target|source/.test(key)) return plainText(value, 40)
    if (Math.abs(number) < 10) return fmtNumber(number, 3)
    return fmtNumber(number, 1)
  }

  function metricEntryList(metrics, keys) {
    var source = metrics && typeof metrics === 'object' ? metrics : {}
    var order = Array.isArray(keys) ? keys : Object.keys(source)
    var out = []
    for (var index = 0; index < order.length; index++) {
      var key = order[index]
      if (!hasOwn(source, key)) continue
      var value = source[key]
      if (value && typeof value === 'object') continue
      out.push({ key: key, label: metricLabel(key), value: formatMetricValue(key, value) })
    }
    return out
  }

  function mergedResultOf(stateObj, step, fullResults) {
    var digest = resultOf(stateObj, step)
    var fullResult = fullResults && fullResults[step] && typeof fullResults[step] === 'object' ? fullResults[step] : null
    if (!fullResult) return digest
    return Object.assign({}, digest || {}, fullResult)
  }

  // ==========================================================================
  // 中栏 · 网络 tab（hub 柱 + 边表 + 校验）
  // ==========================================================================

  function normalizeEdges(list) {
    var out = []
    if (!Array.isArray(list)) return out
    for (var index = 0; index < list.length; index++) {
      var item = list[index] && typeof list[index] === 'object' ? list[index] : {}
      out.push({
        tf: plainText(item.tf || '', 80),
        target: plainText(item.target || '', 80),
        score: asNumber(item.score),
        sign: plainText(item.sign || (asNumber(item.score) !== null && asNumber(item.score) < 0 ? '-' : '+'), 10),
        cluster: plainText(item.cluster === undefined ? '' : item.cluster, 40),
      })
    }
    return out
  }

  function networkHubWeights(stateObj, edges) {
    var grn = resultOf(stateObj, 'grn')
    var metrics = grn && grn.metrics ? grn.metrics : {}
    var names = hubTfsOf(stateObj)
    var weights = {}
    for (var index = 0; index < names.length; index++) weights[names[index]] = 0
    for (var edgeIndex = 0; edgeIndex < edges.length; edgeIndex++) {
      var edge = edges[edgeIndex]
      weights[edge.tf] = (weights[edge.tf] || 0) + Math.abs(edge.score === null ? 0 : edge.score)
    }
    return names.map(function (name) { return { tf: name, weight: weights[name] || 0 } }).sort(function (left, right) { return right.weight - left.weight })
  }

  function NetworkTab(props) {
    var p = props || {}
    var state = p.state
    var grn = mergedResultOf(state, 'grn', p.fullResults)
    if (!grn) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('tabNeedGrn'), hint: T('networkNoResultHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('grn') } }, T('stepRun') + '「' + stepTitle('grn') + '」'),
          h(Btn, { small: true, icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunRecipe) p.onRunRecipe('quick') } }, T('recipeQuick'))))
    }
    var metrics = grn.metrics && typeof grn.metrics === 'object' ? grn.metrics : {}
    var edges = normalizeEdges(grn.edges)
    var hubs = networkHubWeights(state, edges)
    var validate = mergedResultOf(state, 'validate', p.fullResults)
    var figures = normalizeFigureList(grn.figures)
    var maxWeight = hubs.reduce(function (max, item) { return Math.max(max, item.weight) }, 0)
    return h('div', { className: 'vc-panel' },
      h('div', { className: 'vc-toolbar' },
        h(Btn, { small: true, icon: 'play', variant: 'primary', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('grn') } }, T('stepRerun')),
        h(Btn, { small: true, icon: 'chart', disabled: p.plotting === true, onClick: function () { if (p.onPlot) p.onPlot('grn_hub_bar', {}) } }, p.plotting ? T('plotLoading') : T('networkStatic')),
        validate ? h(Chip, { swatch: validate.metrics && validate.metrics.grn_auroc >= 0.7 ? 'var(--dsw-alias-state-success-primary)' : 'var(--dsw-alias-state-warn-primary)', title: T('networkValidateHint') },
          T('networkValidateTitle') + '：' + (validate.summary ? plainText(validate.summary, 80) : '已运行')) : null,
        h('span', { className: 'vc-muted', style: { marginLeft: 'auto' } }, T('networkFilterHint'))),
      h('div', { className: 'vc-metrics' }, metricEntryList(metrics, ['n_edges', 'n_tfs', 'n_targets', 'density', 'cv_r2']).map(function (item) {
        return h(Metric, { key: item.key, label: item.label, value: item.value })
      })),
      h(Section, { title: T('networkHubTitle'), extra: hubs.length ? hubs.length + ' 个' : '' },
        hubs.length
          ? h('div', { className: 'vc-rows' }, hubs.slice(0, 12).map(function (hub) {
              var ratio = maxWeight > 0 ? hub.weight / maxWeight : 0
              return h('div', { className: 'vc-row', key: hub.tf, style: { gridTemplateColumns: 'minmax(70px,1.1fr) minmax(0,1.6fr) auto' } },
                h('span', { className: 'vc-row-gene' }, hub.tf),
                h('div', { className: 'vc-bar' }, h('i', { style: { width: Math.max(3, ratio * 100) + '%', background: colorForName(hub.tf) } })),
                h('span', { className: 'vc-muted' }, fmtNumber(hub.weight, 2)))
            }))
          : h('div', { className: 'vc-muted' }, T('networkHubEmpty'))),
      h(Section, { title: T('networkEdgesTitle'), extra: fmtInt(metrics.n_edges) + ' 条（显示前 ' + edges.length + '）' },
        edges.length
          ? h(TableView, {
              table: {
                columns: [T('networkEdgeTf'), T('networkEdgeTarget'), T('networkEdgeScore'), T('networkEdgeSign'), T('networkEdgeCluster')],
                rows: edges.map(function (edge) { return [edge.tf, edge.target, edge.score, edge.sign === '-' ? '↓ 抑制' : '↑ 激活', edge.cluster] }),
                total: metrics.n_edges || edges.length,
              },
              maxRows: 60,
            })
          : h('div', { className: 'vc-muted' }, T('networkNoResult'))),
      validate
        ? h(Section, { title: T('networkValidateTitle'), extra: validate.metrics ? metricEntryList(validate.metrics, ['grn_auroc', 'grn_auprc', 'delta_corr', 'n_edges']).map(function (item) { return item.label + ' ' + item.value }).join(' · ') : '' },
            h('div', { className: 'vc-sub' }, plainText(validate.summary || '', 500)),
            validate.tables && validate.tables.length ? h(TableView, { table: normalizeTable(validate.tables[0], 0), maxRows: 8 }) : null)
        : h('div', { className: 'vc-muted', style: { fontSize: 11 } }, T('networkValidateNeedTruth')),
      figures.length ? h(FigureBlock, { figure: figures[0], title: figures[0].title || T('networkStatic') }) : null,
      grn.summary ? h('div', { className: 'vc-muted', style: { fontSize: 11.5 } }, plainText(grn.summary, 500)) : null)
  }

  // ==========================================================================
  // 中栏 · 扰动 tab（基因输入 + hub TF chips + 模式/强度 → 结果 + 指标 + 表 + 图）
  // ==========================================================================

  function PerturbTab(props) {
    var p = props || {}
    var state = p.state
    var grn = resultOf(state, 'grn')
    var result = mergedResultOf(state, 'perturb', p.fullResults)
    var overlay = normalizePerturbResult(result, p.pendingGene || '')
    var geneState = useS(p.pendingGene || '')
    var modeState = useS('knockout')
    var strengthState = useS(1)
    var gene = geneState[0]
    var hubs = hubTfsOf(state)
    var figures = normalizeFigureList(result ? result.figures : null)

    useE(function () {
      if (p.pendingGene && p.pendingGene !== geneState[0]) geneState[1](p.pendingGene)
    }, [p.pendingGene])

    if (!grn) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('tabNeedGrn'), hint: T('perturbNeedGrn') || T('perturbNoResultHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('grn') } }, T('stepRun') + '「' + stepTitle('grn') + '」')))
    }

    function run() {
      var params = { gene: gene, mode: modeState[0], strength: Number(strengthState[0]) || 1, cluster: 'all' }
      if (p.onRunStep) p.onRunStep('perturb', params)
    }

    return h('div', { className: 'vc-panel' },
      h(Section, { title: T('perturbTitle') },
        h('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0,1.4fr) minmax(0,1fr) minmax(80px,.8fr) auto', gap: 8, alignItems: 'end' } },
          h(Field, { label: T('perturbGene'), title: T('perturbGeneHint') },
            h('input', {
              className: 'vc-input', type: 'text', value: gene, placeholder: T('perturbGenePlaceholder') || '留空=自动选 hub TF',
              onChange: function (event) { geneState[1](event.target.value) },
            })),
          h(Field, { label: T('perturbMode'), title: T('perturbModeHint') },
            h('select', { className: 'vc-select', value: modeState[0], onChange: function (event) { modeState[1](event.target.value) } },
              h('option', { value: 'knockout' }, T('perturbModeKo')),
              h('option', { value: 'knockdown' }, T('perturbModeKd')),
              h('option', { value: 'overexpression' }, T('perturbModeOe')))),
          h(Field, { label: T('perturbStrength'), title: T('perturbStrengthHint') },
            h('input', {
              className: 'vc-input', type: 'number', min: 0.1, max: 3, step: 0.1, value: String(strengthState[0]),
              onChange: function (event) { strengthState[1](event.target.value) },
            })),
          h(Btn, { variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: run }, p.busy === true ? T('stepRunning') : T('perturbRun'))),
        hubs.length
          ? h('div', { style: { display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 7, alignItems: 'center' } },
              h('span', { className: 'vc-muted', style: { fontSize: 10.5 } }, T('perturbHubChips') + '：'),
              hubs.slice(0, 12).map(function (hub) {
                return h(Chip, {
                  key: hub, className: 'vc-chip-gene', tone: 'live',
                  active: gene === hub, title: '点击填入 ' + hub,
                  onClick: function () { geneState[1](hub) },
                }, hub)
              }))
          : h('div', { className: 'vc-muted', style: { fontSize: 10.5, marginTop: 5 } }, T('networkHubEmpty'))),
      !overlay.active
        ? h(EmptyBlock, {
            title: T('perturbNoResult'),
            hint: T('perturbNoResultHint'),
            actions: [
              h(Btn, { key: 'run', small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: run }, T('perturbRun')),
              hubs.length ? h(Btn, { key: 'hub', small: true, icon: 'sparkle', onClick: function () { geneState[1](hubs[0]) } }, '用 hub TF：' + hubs[0]) : null,
            ],
          })
        : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9 } },
            h('div', { className: 'vc-summary-lg' }, overlay.summary || (overlay.gene ? 'KO ' + overlay.gene : T('perturbSummaryTitle'))),
            h('div', { className: 'vc-metrics' },
              h(Metric, { label: T('metricAffected'), value: fmtInt(overlay.metrics.n_affected) }),
              h(Metric, { label: T('metricMaxDelta'), value: fmtNumber(overlay.metrics.max_abs_delta, 2) }),
              h(Metric, { label: T('metricStateShift'), value: fmtNumber(overlay.stateShift, 3) }),
              h(Metric, { label: T('metricSourceState'), value: overlay.sourceState || '—' }),
              h(Metric, { label: T('metricTargetState'), value: overlay.targetState || '—' }),
              h(Metric, { label: T('metricConfidence'), value: overlay.confidence || '—' }),
              h(Metric, { label: T('metricBackend'), value: overlay.backend || '—' })),
            overlay.validity.notes.length || overlay.validity.grnSource
              ? h('div', { className: 'vc-caveats' },
                  h('b', null, T('perturbValidity') + '：'),
                  (overlay.validity.grnSource ? '来源 ' + overlay.validity.grnSource + '；' : '') +
                  (overlay.validity.nEdgesUsed ? fmtInt(overlay.validity.nEdgesUsed) + ' 条边参与传播；' : '') +
                  (overlay.validity.propagation ? '传播 ' + overlay.validity.propagation + ' 轮；' : '') +
                  overlay.validity.notes.map(function (text) { return plainText(text, 260) }).join('；'))
              : h('div', { className: 'vc-muted', style: { fontSize: 11 } }, T('perturbArrowNote')),
            h(Section, { title: T('perturbAffectedTitle'), extra: overlay.affected.length ? '前 ' + overlay.affected.length + ' 个' : '' },
              overlay.affected.length
                ? h('div', { className: 'vc-rows' }, overlay.affected.slice(0, 30).map(function (row) {
                    var maxAbs = overlay.affected.reduce(function (max, item) { return Math.max(max, Math.abs(item.delta === null ? 0 : item.delta)) }, 0)
                    var ratio = maxAbs > 0 ? Math.abs(row.delta === null ? 0 : row.delta) / maxAbs : 0
                    var color = row.delta !== null && row.delta > 0 ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-state-success-primary)'
                    return h('div', {
                      className: 'vc-row', key: row.gene + '-' + row.rank, title: '点击填入扰动基因',
                      style: { cursor: 'pointer', gridTemplateColumns: 'minmax(64px,1fr) minmax(0,1.6fr) auto' },
                      onClick: function () { geneState[1](row.gene) },
                    },
                      h('span', { className: 'vc-row-gene' }, row.gene, row.isTf ? h(Chip, { style: { marginLeft: 4 } }, 'TF') : null),
                      h('div', { className: 'vc-bar' }, h('i', { style: { width: Math.max(2, ratio * 100) + '%', background: color } })),
                      h('span', { className: 'vc-delta ' + (row.delta !== null && row.delta > 0 ? 'up' : 'down') }, fmtSigned(row.delta, 2)))
                  }))
                : h('div', { className: 'vc-muted' }, T('perturbNoAffected'))),
            overlay.perCluster.length
              ? h(Section, { title: T('perturbPerCluster') },
                  h(TableView, {
                    table: {
                      columns: ['簇', T('perturbDeltaNorm'), T('perturbTargetCluster'), T('perturbCos')],
                      rows: overlay.perCluster.map(function (row) { return [row.cluster, row.deltaNorm, row.targetCluster, row.cos] }),
                      total: overlay.perCluster.length,
                    },
                    maxRows: 20,
                  }))
              : null,
            h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
              h(Btn, { small: true, icon: 'play', disabled: p.busy === true, onClick: run }, T('perturbRunAgain')),
              h(Btn, { small: true, icon: 'chart', disabled: p.plotting === true, onClick: function () { if (p.onPlot) p.onPlot('perturb_waterfall', { gene: overlay.gene }) } }, p.plotting ? T('plotLoading') : T('perturbAffected')),
              h(Btn, { small: true, icon: 'chart', disabled: p.plotting === true, onClick: function () { if (p.onPlot) p.onPlot('state_embedding_perturb', { gene: overlay.gene }) } }, T('stateOverlayPerturb'))),
            figures.length ? h(FigureBlock, { figure: figures[0], title: figures[0].title || T('perturbSummaryTitle') }) : null))
  }

  // ==========================================================================
  // 中栏 · 筛选 tab（候选 genes + 运行 → 排名表，每行"对 TA 做扰动"）
  // ==========================================================================

  function ScreenTab(props) {
    var p = props || {}
    var state = p.state
    var grn = resultOf(state, 'grn')
    var result = mergedResultOf(state, 'screen', p.fullResults)
    var selectedState = useS([])
    var modeState = useS('knockout')
    var topNState = useS(20)
    var specificityState = useS(true)
    var selected = selectedState[0]
    var hubs = hubTfsOf(state)
    var perturb = normalizePerturbResult(resultOf(state, 'perturb'), '')
    var candidateGenes = []
    for (var index = 0; index < hubs.length; index++) if (candidateGenes.indexOf(hubs[index]) < 0) candidateGenes.push(hubs[index])
    for (var affectedIndex = 0; affectedIndex < Math.min(perturb.affected.length, 10); affectedIndex++) {
      var gene = perturb.affected[affectedIndex].gene
      if (gene && candidateGenes.indexOf(gene) < 0) candidateGenes.push(gene)
    }
    var ranked = []
    if (result && Array.isArray(result.ranked)) {
      for (var rankIndex = 0; rankIndex < result.ranked.length; rankIndex++) {
        var row = result.ranked[rankIndex] && typeof result.ranked[rankIndex] === 'object' ? result.ranked[rankIndex] : {}
        ranked.push({
          gene: plainText(row.gene || '', 80),
          score: asNumber(row.score),
          stateShift: asNumber(row.state_shift),
          specificity: asNumber(row.specificity),
          targetState: plainText(row.target_state || '', 40),
          why: plainText(row.why || '', 300),
        })
      }
    }
    var figures = normalizeFigureList(result ? result.figures : null)
    var maxScore = ranked.reduce(function (max, row) { return Math.max(max, row.score === null ? 0 : Math.abs(row.score)) }, 0)

    if (!grn) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('tabNeedGrn'), hint: T('screenCandidatesHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('grn') } }, T('stepRun') + '「' + stepTitle('grn') + '」')))
    }

    function toggleGene(name) {
      selectedState[1](function (previous) {
        var next = previous.slice()
        var position = next.indexOf(name)
        if (position >= 0) next.splice(position, 1)
        else if (next.length < 10) next.push(name)
        return next
      })
    }

    function run() {
      var params = { mode: modeState[0], top_n: Number(topNState[0]) || 20, specificity: specificityState[0] === true }
      if (selected.length) params.genes = selected
      if (p.onRunStep) p.onRunStep('screen', params)
    }

    return h('div', { className: 'vc-panel' },
      h(Section, { title: T('screenTitle'), extra: ranked.length ? fmtInt(ranked.length) + ' 个靶点' : '' },
        h('div', { className: 'vc-muted', style: { fontSize: 11, marginBottom: 5 } }, T('screenCandidatesHint')),
        h('div', { style: { display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 7, alignItems: 'center' } },
          h('span', { className: 'vc-muted', style: { fontSize: 10.5 } }, T('screenCandidates') + '（' + (candidateGenes.length ? candidateGenes.length + ' 个' : T('screenCandidatesEmpty')) + '）：'),
          candidateGenes.slice(0, 24).map(function (name) {
            return h(Chip, {
              key: name, className: 'vc-chip-gene', tone: 'live', active: selected.indexOf(name) >= 0,
              onClick: function () { toggleGene(name) },
            }, name)
          })),
        h('div', { style: { display: 'flex', gap: 8, alignItems: 'end', flexWrap: 'wrap' } },
          h(Field, { label: T('screenMode') },
            h('select', { className: 'vc-select', value: modeState[0], onChange: function (event) { modeState[1](event.target.value) } },
              h('option', { value: 'knockout' }, T('perturbModeKo')),
              h('option', { value: 'overexpression' }, T('perturbModeOe')))),
          h(Field, { label: T('screenTopN') },
            h('input', { className: 'vc-input', type: 'number', min: 5, max: 100, step: 5, value: String(topNState[0]), onChange: function (event) { topNState[1](event.target.value) } })),
          h('label', { className: 'vc-check', title: T('screenSpecificityHint'), style: { paddingBottom: 5 } },
            h('input', { type: 'checkbox', checked: specificityState[0], onChange: function (event) { specificityState[1](event.target.checked) } }),
            T('screenSpecificity')),
          h(Btn, { variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: run }, p.busy === true ? T('stepRunning') : T('screenRun')))),
      !ranked.length
        ? h(EmptyBlock, {
            title: T('screenNoResult'),
            hint: T('screenNoResultHint'),
            actions: [h(Btn, { key: 'run', small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: run }, T('screenRun'))],
          })
        : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9 } },
            h(Section, { title: T('screenTopChart') },
              h('div', { className: 'vc-rows' }, ranked.slice(0, 10).map(function (row, rowIndex) {
                var ratio = maxScore > 0 ? Math.abs(row.score === null ? 0 : row.score) / maxScore : 0
                return h('div', { className: 'vc-row', key: row.gene + '-' + rowIndex, style: { gridTemplateColumns: '34px minmax(64px,1fr) minmax(0,1.6fr) auto' } },
                  h('span', { className: 'vc-muted' }, '#' + (rowIndex + 1)),
                  h('span', { className: 'vc-row-gene' }, row.gene),
                  h('div', { className: 'vc-bar' }, h('i', { style: { width: Math.max(2, ratio * 100) + '%', background: colorForName(row.gene) } })),
                  h('span', { className: 'vc-muted' }, fmtNumber(row.score, 3)))
              }))),
            h(Section, { title: T('screenRankTitle'), extra: T('screenPerturbThis') },
              h(TableView, {
                table: {
                  columns: [T('screenRank'), T('screenGene'), T('screenScore'), T('screenShift'), T('screenSpecificityCol'), T('screenTargetState'), T('screenWhy')],
                  rows: ranked.map(function (row, rowIndex) {
                    return [rowIndex + 1, row.gene, row.score, row.stateShift, row.specificity, row.targetState, row.why]
                  }),
                  total: ranked.length,
                },
                maxRows: 40,
              }),
              h('div', { className: 'vc-rows', style: { marginTop: 6 } }, ranked.slice(0, 30).map(function (row, rowIndex) {
                return h('div', {
                  className: 'vc-row', key: row.gene + '-act-' + rowIndex,
                  style: { gridTemplateColumns: '32px minmax(64px,1fr) minmax(0,1.4fr) auto auto' },
                },
                  h('span', { className: 'vc-muted' }, '#' + (rowIndex + 1)),
                  h('span', { className: 'vc-row-gene' }, row.gene),
                  h('span', { className: 'vc-muted', style: { overflow: 'hidden', textOverflow: 'ellipsis' } }, plainText(row.why || row.targetState || '', 80)),
                  h('span', { className: 'vc-muted' }, fmtNumber(row.score, 3)),
                  h(Btn, {
                    small: true, icon: 'arrow', title: T('screenPerturbThis') + ' ' + row.gene,
                    onClick: function () { if (p.onPerturbGene) p.onPerturbGene(row.gene) },
                  }, T('screenPerturbThis')))
              }))),
            figures.length ? h(FigureBlock, { figure: figures[0], title: figures[0].title || T('screenTopChart') }) : null,
            result && result.summary ? h('div', { className: 'vc-muted', style: { fontSize: 11.5 } }, plainText(result.summary, 400)) : null)
    )
  }

  // ==========================================================================
  // 中栏 · 转移 tab（矩阵热图 + 源/汇 + net flow）
  // ==========================================================================

  /** 从 /run 或 digest 结果里取转移矩阵（可能是 T / matrix / transition_matrix）。 */
  function transitionMatrixOf(result) {
    if (!result || typeof result !== 'object') return null
    var candidates = [result.T, result.matrix, result.transition_matrix]
    for (var index = 0; index < candidates.length; index++) {
      var candidate = candidates[index]
      if (Array.isArray(candidate) && candidate.length && Array.isArray(candidate[0])) return candidate
    }
    return null
  }

  function TransitionTab(props) {
    var p = props || {}
    var state = p.state
    var result = mergedResultOf(state, 'transition', p.fullResults)
    if (!resultOf(state, 'state')) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('transitionNeedState'), hint: T('transitionNoResultHint') || T('stateNoDataHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('state') } }, T('stepRun') + '「' + stepTitle('state') + '」')))
    }
    var metrics = result && result.metrics && typeof result.metrics === 'object' ? result.metrics : {}
    var matrix = transitionMatrixOf(result)
    var sources = result && Array.isArray(result.sources) ? result.sources : []
    var sinks = result && Array.isArray(result.sinks) ? result.sinks : []
    var netFlow = result && Array.isArray(result.net_flow) ? result.net_flow : []
    var figures = normalizeFigureList(result ? result.figures : null)
    var labels = []
    for (var labelIndex = 0; labelIndex < (matrix ? matrix.length : 0); labelIndex++) labels.push('S' + labelIndex)
    var columnCount = matrix && matrix[0] ? matrix[0].length : 0
    var heatCells = []
    if (matrix) {
      heatCells.push(h('div', { key: 'corner', className: 'vc-heat-head' }, ''))
      for (var headIndex = 0; headIndex < labels.length; headIndex++) {
        heatCells.push(h('div', { key: 'h-' + headIndex, className: 'vc-heat-head' }, labels[headIndex]))
      }
      for (var rowIndex = 0; rowIndex < matrix.length; rowIndex++) {
        heatCells.push(h('div', { key: 'r-' + rowIndex, className: 'vc-heat-head', style: { textAlign: 'right', paddingRight: 4 } }, labels[rowIndex]))
        for (var columnIndex = 0; columnIndex < matrix[rowIndex].length; columnIndex++) {
          var number = asNumber(matrix[rowIndex][columnIndex])
          heatCells.push(h('div', {
            key: 'c-' + rowIndex + '-' + columnIndex,
            className: 'vc-heat-cell',
            style: {
              background: number === null ? 'var(--dsw-alias-bg-layer-2)' : colorForValue(number, 0, 0.5),
              color: number !== null && number > 0.28 ? '#fff' : 'var(--dsw-alias-label-primary)',
            },
            title: labels[rowIndex] + ' → ' + labels[columnIndex] + '：' + fmtNumber(number, 3),
          }, number === null ? '—' : fmtNumber(number, 2)))
        }
      }
    }
    var sourceChips = sources.map(function (item, sourceIndex) {
      return h(Chip, { key: 's' + sourceIndex, swatch: colorForName(String(item), sourceIndex) }, 'S' + plainText(item, 20))
    })
    var sinkChips = sinks.map(function (item, sinkIndex) {
      return h(Chip, { key: 'k' + sinkIndex, swatch: colorForName(String(item), sinkIndex) }, 'S' + plainText(item, 20))
    })
    return h('div', { className: 'vc-panel' },
      h('div', { className: 'vc-toolbar' },
        h(Btn, { variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('transition', { method: 'knn', temperature: 0.1, k: 10 }) } }, p.busy === true ? T('stepRunning') : T('transitionRun')),
        h(Btn, { small: true, icon: 'chart', disabled: p.plotting === true, onClick: function () { if (p.onPlot) p.onPlot('transition_matrix', {}) } }, p.plotting ? T('plotLoading') : T('transitionMatrix')),
        h('span', { className: 'vc-muted', style: { marginLeft: 'auto', fontSize: 10.5 } }, T('transitionHeatHint'))),
      h('div', { className: 'vc-metrics' },
        h(Metric, { label: T('transitionStability'), value: fmtNumber(metrics.stability, 3) }),
        h(Metric, { label: T('transitionNStates'), value: fmtInt(metrics.n_states) }),
        h(Metric, { label: T('metricMethod'), value: plainText(metrics.method || '—', 40) })),
      !result
        ? h(EmptyBlock, { title: T('transitionNoResult'), hint: T('transitionNoResultHint') })
        : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9 } },
            heatCells.length
              ? h(Section, { title: T('transitionMatrix'), extra: columnCount + ' × ' + labels.length },
                  h('div', { className: 'vc-heat', style: { gridTemplateColumns: 'auto repeat(' + columnCount + ',minmax(34px,1fr))' } }, heatCells))
              : h('div', { className: 'vc-muted', style: { fontSize: 11 } }, '转移矩阵未随本次响应返回；点「状态转移矩阵」出静态图，或重跑「状态转移」后查看完整矩阵。'),
            sourceChips.length || sinkChips.length
              ? h(Section, { title: T('transitionSources') + ' / ' + T('transitionSinks') },
                  h('div', { style: { display: 'flex', gap: 14, flexWrap: 'wrap' } },
                    h('div', null,
                      h('div', { className: 'vc-muted' }, T('transitionSources')),
                      h('div', { style: { display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 3 } }, sourceChips)),
                    h('div', null,
                      h('div', { className: 'vc-muted' }, T('transitionSinks')),
                      h('div', { style: { display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 3 } }, sinkChips))))
              : null,
            netFlow.length
              ? h(Section, { title: T('transitionNetFlow') },
                  h(TableView, {
                    table: {
                      columns: [T('transitionFrom'), T('transitionTo'), T('transitionFlow')],
                      rows: netFlow.map(function (edge) {
                        var from = Array.isArray(edge) ? edge[0] : (edge && edge.from)
                        var to = Array.isArray(edge) ? edge[1] : (edge && edge.to)
                        var flow = Array.isArray(edge) ? edge[2] : (edge && edge.flow)
                        return ['S' + plainText(from, 20), 'S' + plainText(to, 20), flow]
                      }),
                      total: netFlow.length,
                    },
                    maxRows: 24,
                  }))
              : null,
            figures.length ? h(FigureBlock, { figure: figures[0], title: figures[0].title || T('transitionMatrix') }) : null,
            result.summary ? h('div', { className: 'vc-muted', style: { fontSize: 11.5 } }, plainText(result.summary, 500)) : null))
  }

  // ==========================================================================
  // 中栏 · 响应 tab（挂载扰动表引导 + 预测/真实散点 + 每扰动指标）
  // ==========================================================================

  function ResponseTab(props) {
    var p = props || {}
    var state = p.state
    var dataset = state ? state.activeDataset : null
    var attachments = dataset && dataset.attachments ? dataset.attachments : []
    var perturbTables = attachments.filter(function (item) { return item.kind === 'perturb_table' || /perturb|扰动/i.test(item.kind + ' ' + item.name) })
    var pathState = useS('')
    var result = mergedResultOf(state, 'response', p.fullResults)
    var metrics = result && result.metrics ? result.metrics : {}
    var figures = normalizeFigureList(result ? result.figures : null)
    var perPert = []
    if (result && Array.isArray(result.per_pert)) {
      for (var index = 0; index < result.per_pert.length; index++) {
        var row = result.per_pert[index] && typeof result.per_pert[index] === 'object' ? result.per_pert[index] : {}
        perPert.push({ perturbation: plainText(row.perturbation || '', 100), r2: asNumber(row.r2), genes: Array.isArray(row.top_shift_genes) ? row.top_shift_genes : [] })
      }
    }
    if (!resultOf(state, 'state')) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('responseNeedState'), hint: T('stateNoDataHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('state') } }, T('stepRun') + '「' + stepTitle('state') + '」')))
    }
    return h('div', { className: 'vc-panel' },
      h(Section, { title: T('responseAttach'), extra: perturbTables.length ? perturbTables.length + ' 个已挂载' : T('responseNeedData') },
        h('div', { className: 'vc-muted', style: { fontSize: 11, marginBottom: 6 } }, T('responseNeedDataHint')),
        perturbTables.length
          ? h('div', { style: { display: 'flex', gap: 4, flexWrap: 'wrap' } }, perturbTables.map(function (item) {
              return h(Chip, { key: item.id, swatch: 'var(--dsw-alias-state-success-primary)', title: item.path }, item.name)
            }))
          : null,
        h('div', { style: { display: 'flex', gap: 7, alignItems: 'end', marginTop: 6 } },
          h(Field, { label: T('responsePath'), title: T('responseNoAttachHint') },
            h('input', {
              className: 'vc-input', type: 'text', placeholder: '/path/to/perturb_table.csv',
              value: pathState[0], onChange: function (event) { pathState[1](event.target.value) },
            })),
          h(Btn, {
            small: true, icon: 'upload', disabled: p.attaching === true || !pathState[0],
            onClick: function () { if (p.onAttach) p.onAttach('perturb_table', pathState[0]) },
          }, p.attaching === true ? T('responseAttaching') : T('responseAttachBtn'))),
        h('div', { className: 'vc-muted', style: { fontSize: 10.5, marginTop: 4 } }, T('responseUseDemo') + '：' + T('demoHint'))),
      h('div', { className: 'vc-toolbar' },
        h(Btn, { variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('response', { data: 'auto', method: 'additive', nfold: 5 }) } }, p.busy === true ? T('stepRunning') : T('responseRun')),
        h(Btn, { small: true, icon: 'chart', disabled: p.plotting === true, onClick: function () { if (p.onPlot) p.onPlot('response_scatter', {}) } }, p.plotting ? T('plotLoading') : T('responsePredVsTrue'))),
      !result
        ? h(EmptyBlock, { title: T('responseNoResult'), hint: T('responseNoResultHint') })
        : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9 } },
            h('div', { className: 'vc-metrics' },
              h(Metric, { label: T('responseR2'), value: fmtNumber(metrics.r2, 3) }),
              h(Metric, { label: T('responseSpearman'), value: fmtNumber(metrics.spearman, 3) }),
              h(Metric, { label: T('responseNPert'), value: fmtInt(metrics.n_pert) }),
              h(Metric, { label: T('responseMethod'), value: plainText(metrics.method || '—', 40) })),
            figures.length ? h(FigureBlock, { figure: figures[0], title: T('responsePredVsTrue') }) : null,
            perPert.length
              ? h(Section, { title: T('responsePerPert'), extra: T('responsePredVsTrue') },
                  h(TableView, {
                    table: {
                      columns: [T('responsePerturbation'), T('responseR2'), 'top 偏移基因'],
                      rows: perPert.map(function (row) { return [row.perturbation, row.r2, row.genes.slice(0, 5).join('、')] }),
                      total: perPert.length,
                    },
                    maxRows: 30,
                  }))
              : null,
            result.summary ? h('div', { className: 'vc-muted', style: { fontSize: 11.5 } }, plainText(result.summary, 500)) : null))
  }

  // ==========================================================================
  // 中栏 · 智能分析 tab（insight 分节卡 + 靶点表 + 下一步 + caveats + audit）
  // ==========================================================================

  function InsightTab(props) {
    var p = props || {}
    var state = p.state
    var rawInsight = p.insightResult || mergedResultOf(state, 'insight', p.fullResults)
    var insight = rawInsight ? normalizeInsight(rawInsight) : null
    var audit = p.auditResult ? normalizeAudit(p.auditResult) : normalizeAudit(resultOf(state, 'audit'))
    var hasInsight = insight && (insight.headline || insight.sections.length)
    function generate() {
      if (p.onInsight) p.onInsight()
      else if (p.onRunStep) p.onRunStep('insight', { depth: 'standard' })
    }
    if (!resultOf(state, 'state')) {
      return h('div', { className: 'vc-panel' },
        h(NeedStep, { title: T('insightNeedState'), hint: T('insightNoResultHint') },
          h(Btn, { small: true, variant: 'primary', icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('state') } }, T('stepRun') + '「' + stepTitle('state') + '」')))
    }
    return h('div', { className: 'vc-panel' },
      h('div', { className: 'vc-toolbar' },
        h(Btn, { variant: 'primary', icon: 'sparkle', disabled: p.busy === true, onClick: generate },
          p.busy === true ? T('insightRunning') : (hasInsight ? T('insightRegenerate') : T('insightGenerate'))),
        h(Btn, { small: true, icon: 'check', disabled: p.auditing === true, onClick: p.onAudit }, p.auditing === true ? T('insightAuditRunning') : T('insightAudit')),
        h(Btn, { small: true, icon: 'download', disabled: p.exporting === true, onClick: function () { if (p.onExport) p.onExport('report') } }, T('insightExportReport')),
        h(Btn, { small: true, icon: 'download', disabled: p.exporting === true, onClick: function () { if (p.onExport) p.onExport('methods') } }, T('insightExportMethods')),
        h('span', { className: 'vc-muted', style: { marginLeft: 'auto', fontSize: 10.5 } }, T('insightDisclaimer'))),
      hasInsight
        ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9 } },
            h('div', { className: 'vc-summary-lg' }, insight.headline),
            insight.sections.map(function (section, index) {
              return h(Section, { key: 'section-' + index, title: section.title },
                h('ul', { className: 'vc-bullets' }, section.bullets.map(function (text, bulletIndex) {
                  return h('li', { key: bulletIndex }, text)
                })))
            }),
            insight.rankedTargets.length
              ? h(Section, { title: T('insightRankedTargets'), extra: T('screenPerturbThis') },
                  h(TableView, {
                    table: {
                      columns: [T('insightRankedGene'), T('insightRankedScore'), T('insightRankedWhy')],
                      rows: insight.rankedTargets.map(function (row) { return [row.gene, row.score, row.why] }),
                      total: insight.rankedTargets.length,
                    },
                    maxRows: 20,
                  }),
                  h('div', { style: { display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 6 } }, insight.rankedTargets.slice(0, 8).map(function (row) {
                    return h(Btn, {
                      key: row.gene, small: true, icon: 'arrow',
                      onClick: function () { if (p.onPerturbGene) p.onPerturbGene(row.gene) },
                    }, '扰动 ' + row.gene)
                  })))
              : null,
            insight.nextActions.length
              ? h(Section, { title: T('insightNextActions') },
                  h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } }, insight.nextActions.map(function (action, index) {
                    return h(Btn, {
                      key: 'action-' + index, small: true, icon: 'play', disabled: p.busy === true || !action.step,
                      title: action.why,
                      onClick: function () { if (p.onRunStep) p.onRunStep(action.step) },
                    }, stepTitle(action.step) + (action.why ? '：' + plainText(action.why, 30) : ''))
                  })))
              : null,
            insight.caveats.length
              ? h('div', { className: 'vc-caveats' },
                  h('div', { style: { fontWeight: 650, marginBottom: 4 } }, '⚠ ' + T('insightCaveats')),
                  h('ul', { className: 'vc-bullets' }, insight.caveats.map(function (text, index) {
                    return h('li', { key: index, style: { color: 'var(--dsw-alias-label-secondary)' } }, text)
                  })))
              : null)
        : h(EmptyBlock, {
            title: T('insightNoResult'),
            hint: T('insightNoResultHint'),
            actions: [
              h(Btn, { key: 'run', small: true, variant: 'primary', icon: 'sparkle', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep('insight', { depth: 'standard' }) } }, T('insightGenerate')),
              h(Btn, { key: 'quick', small: true, icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunRecipe) p.onRunRecipe('quick') } }, T('recipeQuick')),
            ],
          }),
      h(Section, { title: T('insightAudit'), extra: audit.score !== null ? T('insightAuditScore') + ' ' + fmtInt(audit.score) + ' / 100' : '' },
        audit.score !== null
          ? h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 7 } },
              h('div', {
                style: {
                  width: 56, height: 56, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 17, fontWeight: 700,
                  border: '4px solid ' + (audit.level === 'ok' ? 'var(--dsw-alias-state-success-primary)' : audit.level === 'fail' ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-state-warn-primary)'),
                  color: audit.level === 'ok' ? 'var(--dsw-alias-state-success-primary)' : audit.level === 'fail' ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-state-warn-primary)',
                },
              }, fmtInt(audit.score)),
              h('div', { className: 'vc-sub', style: { flex: '1 1 auto' } }, audit.summary || ''))
          : null,
        audit.items.length
          ? h('div', { className: 'vc-rows' }, audit.items.map(function (item, index) {
              return h('div', { className: 'vc-row', key: item.id + '-' + index, style: { gridTemplateColumns: '10px minmax(80px,.9fr) minmax(0,2fr)' }, title: item.hint },
                h(Dot, { status: item.level === 'ok' ? 'done' : item.level === 'fail' ? 'failed' : 'running', title: item.level }),
                h('span', { className: 'vc-row-gene' }, item.title),
                h('span', { className: 'vc-muted', style: { whiteSpace: 'normal' } }, item.detail + (item.hint ? '（' + item.hint + '）' : '')))
            }))
          : h('div', { className: 'vc-muted', style: { fontSize: 11 } }, audit.score !== null ? T('insightNoChecks') : T('insightAuditHint')),
        audit.caveats.length ? h('div', { className: 'vc-muted', style: { fontSize: 10.5, marginTop: 5 } }, audit.caveats.join('；')) : null))
  }

  // ==========================================================================
  // 中栏 · 记录 tab（runs 列表 → 抽屉：参数/产物/导出/重跑/复制复现指令）
  // ==========================================================================

  /** 参数扫描（/sweep）：选 step + 数值参数 + values → 表格 + 最优值一键填回。 */
  function SweepPanel(props) {
    var p = props || {}
    var steps = Array.isArray(p.steps) ? p.steps : []
    var openState = useS(false)
    var stepState = useS('')
    var paramState = useS('')
    var valuesState = useS('')
    var running = p.running === true
    var open = openState[0]
    var stepId = stepState[0]
    var stepMeta = null
    for (var index = 0; index < steps.length; index++) {
      if (steps[index].id === stepId) { stepMeta = steps[index]; break }
    }
    if (!stepId) {
      for (var fallbackIndex = 0; fallbackIndex < steps.length; fallbackIndex++) {
        var candidate = steps[fallbackIndex]
        var hasNumeric = (candidate.params || []).some(function (spec) { return spec.type === 'number' })
        if (hasNumeric) { stepMeta = candidate; stepId = candidate.id; break }
      }
    }
    var numericParams = stepMeta ? (stepMeta.params || []).filter(function (spec) { return spec.type === 'number' }) : []
    var paramKey = paramState[0]
    if (!paramKey && numericParams.length) paramKey = numericParams[0].key
    var result = p.result && typeof p.result === 'object' ? p.result : null
    var rows = result && Array.isArray(result.rows) ? result.rows : []
    var metricKeys = []
    if (rows.length && rows[0] && typeof rows[0] === 'object') {
      var rowKeys = Object.keys(rows[0])
      for (var keyIndex = 0; keyIndex < rowKeys.length && metricKeys.length < 4; keyIndex++) {
        var key = rowKeys[keyIndex]
        if (key === 'value' || key === 'status') continue
        if (asNumber(rows[0][key]) !== null) metricKeys.push(key)
      }
    }
    var best = result && result.best && typeof result.best === 'object' ? result.best : null
    var primary = result && result.primary ? plainText(result.primary, 60) : (metricKeys.length ? metricKeys[0] : '')
    if (!open) {
      return h(Section, { title: '参数扫描（sweep）', extra: result ? '已有结果' : '扫描一个数值参数看指标怎么变' },
        h('div', { style: { display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' } },
          h(Btn, { small: true, icon: 'chart', onClick: function () { openState[1](true) } }, result ? '查看扫描结果' : T('expandParams')),
          result && result.summary ? h('span', { className: 'vc-muted', style: { fontSize: 11 } }, plainText(result.summary, 150)) : null))
    }
    return h(Section, { title: '参数扫描（sweep）', extra: 'CONTRACT §4.2' },
      h('div', { style: { display: 'flex', gap: 7, alignItems: 'end', flexWrap: 'wrap' } },
        h(Field, { label: T('stepWord') || '步骤' },
          h('select', {
            className: 'vc-select', style: { width: 120 },
            value: stepId,
            onChange: function (event) { stepState[1](event.target.value); paramState[1]('') },
          }, steps.map(function (step) { return h('option', { key: step.id, value: step.id }, step.title || step.id) }))),
        h(Field, { label: T('stepParams') || '参数' },
          h('select', {
            className: 'vc-select', style: { width: 150 },
            value: paramKey || '',
            onChange: function (event) { paramState[1](event.target.value) },
          }, numericParams.length
            ? numericParams.map(function (spec) { return h('option', { key: spec.key, value: spec.key }, spec.label || spec.key) })
            : h('option', { value: '' }, '（该步骤没有数值参数）'))),
        h(Field, { label: 'values（逗号分隔，留空=自动）' },
          h('input', {
            className: 'vc-input', style: { width: 180 }, type: 'text', placeholder: '200,500,1000',
            value: valuesState[0], onChange: function (event) { valuesState[1](event.target.value) },
          })),
        h(Btn, {
          variant: 'primary', icon: 'play', disabled: running || !stepId || !paramKey,
          onClick: function () {
            if (p.onRun) p.onRun(stepId, paramKey, valuesState[0])
          },
        }, running ? T('stepRunning') : '开始扫描'),
        h(Btn, { small: true, variant: 'ghost', icon: 'close', onClick: function () { openState[1](false) } }, T('collapse'))),
      result
        ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6, marginTop: 7 } },
            rows.length
              ? h(TableView, {
                  table: {
                    columns: ['value'].concat(metricKeys),
                    rows: rows.map(function (row) { return [row.value].concat(metricKeys.map(function (key) { return row[key] })) }),
                    total: rows.length,
                  },
                  maxRows: 20,
                })
              : h('div', { className: 'vc-muted' }, '扫描没有返回 rows'),
            best
              ? h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
                  h('span', { className: 'vc-chip vc-chip-reco', style: { cursor: 'default' } },
                    '最优 ' + primary + '=' + fmtNumber(best[primary], 3) + ' @ ' + plainText(result.param || paramKey, 40) + '=' + fmtNumber(best.value, 3)),
                  h(Btn, {
                    small: true, icon: 'arrow',
                    onClick: function () {
                      if (p.onFillValue) p.onFillValue(plainText(result.step || stepId, 40), plainText(result.param || paramKey, 40), best.value)
                    },
                  }, '填回该步骤参数'))
              : null,
            result.summary ? h('div', { className: 'vc-muted', style: { fontSize: 11 } }, plainText(result.summary, 300)) : null)
        : null)
  }

  function RecordsTab(props) {
    var p = props || {}
    var state = p.state
    var runs = normalizeRuns(p.runs && p.runs.length ? p.runs : (state ? state.runs : []))
    var selectedState = useS(null)
    var selected = selectedState[0]
    return h('div', { className: 'vc-panel' },
      h(SweepPanel, {
        steps: p.steps || [],
        result: p.sweep || null,
        running: p.sweeping === true,
        onRun: p.onSweep,
        onFillValue: p.onFillValue,
      }),
      h('div', { className: 'vc-toolbar' },
        h('span', { className: 'vc-muted' }, T('recordsHint')),
        h('div', { style: { marginLeft: 'auto' } },
          h(Btn, { small: true, icon: 'refresh', onClick: p.onRefreshRuns, disabled: p.refreshingRuns === true }, p.refreshingRuns ? T('refreshBusy') : T('recordsRefresh')))),
      h('div', { className: 'vc-metrics' },
        h(Metric, { label: T('recordsStatus'), value: fmtInt(runs.length) }),
        h(Metric, { label: T('metrics'), value: fmtInt(runs.filter(function (run) { return run.status === 'ok' }).length) + ' ok' }),
        h(Metric, { label: T('figures'), value: fmtInt(runs.reduce(function (sum, run) { return sum + run.figures }, 0)) }),
        h(Metric, { label: T('tables'), value: fmtInt(runs.reduce(function (sum, run) { return sum + run.tables }, 0)) })),
      !runs.length
        ? h(EmptyBlock, {
            title: T('recordsNoRuns'),
            hint: T('recordsNoRunsHint'),
            actions: [h(Btn, { key: 'demo', small: true, variant: 'primary', icon: 'sparkle', onClick: p.onDemo }, T('demo'))],
          })
        : h('div', { className: 'vc-table-wrap' },
            h('table', { className: 'vc-table' },
              h('thead', null, h('tr', null,
                h('th', null, T('recordsId')),
                h('th', null, T('recordsStep')),
                h('th', null, T('recordsTime')),
                h('th', null, T('recordsDuration')),
                h('th', null, T('recordsFigures')),
                h('th', null, T('recordsTables')),
                h('th', null, T('recordsStatus')),
                h('th', null, ''))),
              h('tbody', null, runs.map(function (run) {
                return h('tr', { key: run.id, onClick: function () { selectedState[1](run) }, style: { cursor: 'pointer' } },
                  h('td', { className: 'vc-mono' }, run.id),
                  h('td', null, stepTitle(run.step) + (run.label && run.label !== run.step ? ' · ' + plainText(run.label, 30) : '')),
                  h('td', null, formatClock(run.startedAt)),
                  h('td', { className: 'num' }, formatDuration(run.durationMs)),
                  h('td', { className: 'num' }, fmtInt(run.figures)),
                  h('td', { className: 'num' }, fmtInt(run.tables)),
                  h('td', null, h(Chip, { swatch: run.status === 'ok' ? 'var(--dsw-alias-state-success-primary)' : 'var(--dsw-alias-state-error-primary)' }, run.status === 'ok' ? T('recordsStatusOk') : T('recordsStatusBad'))),
                  h('td', null, h(Btn, { small: true, icon: 'chevron' }, T('recordsDetail'))))
              })))),
      selected
        ? h(Drawer, {
            title: T('recordsDetailTitle') + ' · ' + selected.id,
            onClose: function () { selectedState[1](null) },
            children: h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9 } },
              h(Section, { title: T('recordsStep') },
                h(KV, { label: T('recordsId'), value: selected.id }),
                h(KV, { label: T('recordsStep'), value: stepTitle(selected.step) }),
                h(KV, { label: T('recordsTime'), value: formatClock(selected.startedAt) }),
                h(KV, { label: T('recordsDuration'), value: formatDuration(selected.durationMs) }),
                h(KV, { label: T('recordsStatus'), value: selected.status }),
                h(KV, { label: T('recordsParams'), value: T('recordsParamsEmpty') }),
                selected.summary ? h('div', { className: 'vc-step-summary' }, plainText(selected.summary, 500)) : null),
              h(Section, { title: T('recordsArtifacts') },
                h(KV, { label: T('figures'), value: fmtInt(selected.figures) }),
                h(KV, { label: T('tables'), value: fmtInt(selected.tables) }),
                selected.dir ? h('div', { className: 'vc-muted vc-mono', style: { fontSize: 10.5, wordBreak: 'break-all', marginTop: 4 } }, selected.dir) : h('div', { className: 'vc-muted' }, T('recordsNoFiles'))),
              h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
                h(Btn, { small: true, icon: 'download', disabled: p.exporting === true, onClick: function () { if (p.onExport) p.onExport('run_zip', { run: selected.id }) } }, T('recordsExportZip')),
                h(Btn, { small: true, icon: 'download', onClick: function () { if (p.onExport) p.onExport('report') } }, T('exportMarkdown')),
                h(Btn, { small: true, icon: 'play', disabled: p.busy === true, onClick: function () { if (p.onRunStep) p.onRunStep(selected.step) } }, T('recordsRerun')),
                h(Btn, { small: true, icon: 'copy', onClick: function () {
                  var text = '用 virtualcell 工具重跑：' + JSON.stringify({ action: 'run', step: selected.step }) + '（原始 run=' + selected.id + '）'
                  if (p.onCopy) p.onCopy(text, T('recordsCopyRepro'))
                } }, T('recordsCopyRepro'))))
          })
        : null)
  }

  // ==========================================================================
  // 右栏 AgentPanel：当前结论 / 给 AI 的下一步（可复制）/ 状态速览 / 日志
  // ==========================================================================

  function AgentPanel(props) {
    var p = props || {}
    var state = p.state
    var logs = Array.isArray(p.logs) ? p.logs : []
    var detailState = useS(false)
    var detail = detailState[0]
    var doneSet = state ? doneStepSet(state.activeDataset) : {}
    var insight = p.insightResult ? normalizeInsight(p.insightResult) : normalizeInsight(resultOf(state, 'insight'))

    var conclusion = ''
    var conclusionFrom = ''
    var metricList = []
    if (insight.headline) {
      conclusion = insight.headline
      conclusionFrom = T('tabInsight')
    } else if (state && state.activeDataset) {
      var doneSteps = (state.stepsDone || []).slice().reverse()
      for (var index = 0; index < doneSteps.length; index++) {
        var result = resultOf(state, doneSteps[index])
        if (result && result.summary) {
          conclusion = result.summary
          conclusionFrom = stepTitle(doneSteps[index])
          metricList = metricEntryList(result.metrics, ['n_clusters', 'silhouette', 'n_edges', 'state_shift']).slice(0, 3)
          break
        }
      }
    }
    var instructions = state
      ? buildAgentInstructions(state, { suggest: p.suggest, insight: p.insightResult || resultOf(state, 'insight') })
      : buildAgentInstructions({ datasets: [], active: '' }, {})
    var contextText = state ? buildAgentContext(state) : ''

    return h('div', { className: 'vc-col vc-right vc-agent' },
      h(Section, { title: T('agentConclusions'), extra: conclusionFrom },
        !state || !state.activeDataset
          ? h('div', { className: 'vc-muted' }, T('agentNoDataset'))
          : h('div', null,
              h('div', { className: 'vc-summary-lg' }, conclusion || h('span', { className: 'vc-muted' }, T('agentNoConclusions'))),
              metricList.length
                ? h('div', { className: 'vc-metrics', style: { marginTop: 6 } }, metricList.map(function (item) {
                    return h(Metric, { key: item.key, label: item.label, value: item.value })
                  }))
                : null,
              h('div', { style: { display: 'flex', gap: 6, marginTop: 7, flexWrap: 'wrap' } },
                h(Btn, { small: true, icon: 'arrow', disabled: !doneSet.insight, onClick: function () { if (p.onOpenTab) p.onOpenTab('insight') } }, T('tabInsight')),
                h(Btn, { small: true, icon: 'chart', onClick: function () { if (p.onOpenTab) p.onOpenTab('state') } }, T('tabState'))))),
      h(Section, { title: T('agentNext'), extra: T('agentNextHint') },
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } }, instructions.map(function (instruction) {
          return h('div', { className: 'vc-command', key: instruction.id },
            h('div', { className: 'vc-command-text' }, instruction.text),
            h(Btn, {
              small: true, variant: 'ghost', icon: 'copy', title: T('agentCopy'),
              onClick: function () { if (p.onCopy) p.onCopy(instruction.text, instruction.title) },
            }, T('copy')))
        }))),
      h(Section, { title: T('agentContext'), extra: T('agentContextCopied') },
        h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
          h(Btn, {
            small: true, icon: 'copy',
            onClick: function () { if (p.onCopy) p.onCopy(contextText, T('agentContextCopied')) },
          }, T('agentCopyAll')),
          h(Btn, { small: true, icon: 'info', onClick: function () { detailState[1](!detail) } }, detail ? T('collapse') : T('expand')),
          state && state.busy ? h(Chip, { tone: 'live' }, busyText(state)) : null),
        detail ? h('pre', { className: 'vc-textarea', style: { marginTop: 6, maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap' } }, contextText) : null,
        state && state.activeDataset
          ? h('div', { style: { marginTop: 5 } },
              h(KV, { label: T('agentContextDataset'), value: state.activeDataset.name }),
              h(KV, { label: T('agentContextSteps'), value: (state.stepsDone || []).length + ' / ' + STEP_ORDER.length }),
              h(KV, { label: T('agentContextLastRun'), value: state.lastRun || '—' }),
              h(KV, { label: T('agentWorkdir'), value: state.engine.workdir || '—', title: state.engine.workdir }))
          : null),
      h(Section, { title: T('agentLog'), extra: logs.length ? String(logs.length) + ' 条' : '' },
        h('div', { style: { display: 'flex', gap: 6, marginBottom: 5 } },
          h(Btn, { small: true, icon: 'trash', onClick: p.onClearLogs }, T('agentLogClear'))),
        logs.length
          ? h('div', { className: 'vc-log' }, logs.slice(-MAX_LOG).map(function (entry) {
              return h('div', { className: 'vc-log-line ' + (entry.level || 'info'), key: entry.id },
                h('span', { className: 'vc-log-time' }, entry.at),
                h('span', null, entry.text))
            }))
          : h('div', { className: 'vc-muted', style: { fontSize: 11 } }, T('agentLogEmpty'))))
  }

  // ==========================================================================
  // 整页主体：状态管理 + 轮询 + 动作 + 三栏装配
  // ==========================================================================

  var TIMEOUT_BY_STEP = { qc: 120000, state: 300000, annotate: 120000, grn: 600000, validate: 300000, perturb: 300000, screen: 600000, response: 600000, transition: 300000, insight: 300000 }

  function WorkbenchBody() {
    var stateStore = useS(null)
    var stepsStore = useS([])
    var loadingStore = useS(true)
    var errorStore = useS('')
    var tabStore = useS('state')
    var expandedStore = useS({})
    var draftsStore = useS({})
    var suggestStore = useS({})
    var fullStore = useS({})
    var logsStore = useS([])
    var toastsStore = useS([])
    var engineOpenStore = useS(false)
    var recipeOpenStore = useS(false)
    var importOpenStore = useS(false)
    var plotFiguresStore = useS({})
    var plotLoadingStore = useS(false)
    var auditStore = useS(null)
    var insightStore = useS(null)
    var previewStore = useS(null)
    var attachingStore = useS(false)
    var exportingStore = useS(false)
    var auditingStore = useS(false)
    var refreshingStore = useS(false)
    var runsStore = useS([])
    var pendingGeneStore = useS('')
    var refreshingRunsStore = useS(false)
    var sweepStore = useS(null)
    var sweepingStore = useS(false)
    var plotLoading = plotLoadingStore[0]
    var attaching = attachingStore[0]
    var exporting = exportingStore[0]
    var auditing = auditingStore[0]
    var refreshing = refreshingStore[0]
    var refreshingRuns = refreshingRunsStore[0]
    var sweeping = sweepingStore[0]
    var engineOpen = engineOpenStore[0]
    var recipeOpen = recipeOpenStore[0]
    var importOpen = importOpenStore[0]

    var state = stateStore[0]
    var stepsMeta = stepsStore[0]
    var loading = loadingStore[0]
    var error = errorStore[0]
    var activeTab = tabStore[0]
    var expanded = expandedStore[0]
    var drafts = draftsStore[0]
    var suggestions = suggestStore[0]
    var fullResults = fullStore[0]
    var logs = logsStore[0]
    var toasts = toastsStore[0]
    var plotFigures = plotFiguresStore[0]
    var preview = previewStore[0]
    var busy = state && state.busy ? state.busy : null
    var doneSet = state && state.activeDataset ? doneStepSet(state.activeDataset) : {}
    var prevBusyRef = useR(null)
    var suggestedFetchedRef = useR({})
    var pollFailRef = useR(0)

    function pushLog(level, text) {
      logsStore[1](function (previous) { return appendLog(previous, makeLogEntry(level, text), MAX_LOG) })
    }

    function dismissToast(id) {
      toastsStore[1](function (previous) { return previous.filter(function (item) { return item.id !== id }) })
    }

    function pushToast(toast) {
      var entry = {
        id: 'toast-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6),
        tone: toast.tone || 'info',
        title: toast.title || '',
        advice: toast.advice || '',
        message: toast.message || '',
      }
      toastsStore[1](function (previous) { return previous.concat([entry]).slice(-4) })
      setTimeout(function () { dismissToast(entry.id) }, 7000)
    }

    function fail(errorValue, context) {
      var info = describeError(errorValue)
      pushToast({ tone: 'error', title: info.title, advice: info.advice, message: info.message })
      pushLog('error', (context ? context + '：' : '') + info.title + (info.message ? ' — ' + info.message : ''))
    }

    function refreshState(options) {
      var opts = options || {}
      if (!opts.silent) refreshingStore[1](true)
      return api().get('/state', { timeoutMs: API_TIMEOUT_MS }).then(function (raw) {
        refreshingStore[1](false)
        pollFailRef.current = 0
        errorStore[1]('')
        var next = normalizeState(raw)
        stateStore[1](next)
        return next
      }).catch(function (errorValue) {
        refreshingStore[1](false)
        pollFailRef.current += 1
        if (!opts.silent) {
          fail(errorValue, T('refresh'))
        } else if (pollFailRef.current === 3) {
          pushLog('warn', '轮询 /state 连续失败，正在重试…')
        }
        return null
      })
    }

    function loadSteps() {
      return api().get('/steps', { timeoutMs: API_TIMEOUT_MS }).then(function (raw) {
        var normalized = normalizeSteps(raw)
        stepsStore[1](normalized.steps)
        return normalized.steps
      }).catch(function (errorValue) {
        pushLog('warn', '加载 /steps 失败：' + describeError(errorValue).advice + '（回退用 /state 里的步骤元数据）')
        return []
      })
    }

    function loadPreview() {
      return api().post('/preview', { n_genes: 12, n_cells: 12, embedding: true }, { timeoutMs: HEAVY_API_TIMEOUT_MS })
        .then(function (raw) { previewStore[1](normalizePreview(raw)) })
        .catch(function () { previewStore[1](null) })
    }

    // 首屏：加载 steps + state，然后每 1.5s 轮询 /state
    useE(function () {
      var alive = true
      loadSteps()
      refreshState({ silent: true }).then(function (next) {
        if (!alive) return
        loadingStore[1](false)
        if (!next) {
          errorStore[1]('连不上宿主引擎（/virtualcell/state）：确认插件已启用、宿主未退出，然后点刷新重试。')
          return
        }
        if (next.active) loadPreview()
        if (!next.activeDataset) return
        var dataset = next.activeDataset
        var nextDrafts = {}
        // 用 /state 里的步骤元数据兜底
        stepsStore[1](function (previous) {
          return previous.length ? previous : normalizeSteps({ steps: next.raw && next.raw.steps ? next.raw.steps : [] }).steps
        })
        void dataset
        draftsStore[1](function (previous) { return Object.keys(previous).length ? previous : nextDrafts })
      })
      function tick() {
        if (!alive) return
        refreshState({ silent: true }).then(function (next) {
          if (!alive || !next) return
          loadingStore[1](false)
          var previousBusy = prevBusyRef.current
          if (next.busy && !previousBusy) {
            pushLog('info', '开始运行「' + stepTitle(next.busy.step) + '」' + (next.busy.pct === null ? '' : '（' + trimFixed(next.busy.pct, 0) + '%）'))
          } else if (!next.busy && previousBusy) {
            pushLog('ok', '「' + stepTitle(previousBusy.step) + '」运行结束')
            loadPreview()
          }
          prevBusyRef.current = next.busy
        })
      }
      var timer = setInterval(tick, POLL_MS)
      return function () { alive = false; clearInterval(timer) }
    }, [])

    // 数据集/状态变化后默认展开建议的下一步
    useE(function () {
      if (!state || !state.activeDataset) return
      var target = state.suggestedStep || ''
      if (target && expandedStore[0][target] !== true) {
        expandedStore[1](function (previous) {
          if (previous[target]) return previous
          var next = Object.assign({}, previous)
          next[target] = true
          return next
        })
      }
    }, [state && state.active, state && state.suggestedStep])

    // 为建议的下一步自动拉一次 /suggest（推荐值徽章）
    useE(function () {
      if (!state || !state.activeDataset) return
      var step = state.suggestedStep || ''
      if (!step || step === 'open' || suggestedFetchedRef.current[step]) return
      suggestedFetchedRef.current[step] = true
      api().post('/suggest', { step: step }, { timeoutMs: API_TIMEOUT_MS }).then(function (raw) {
        var suggestion = normalizeSuggestion(raw)
        suggestStore[1](function (previous) {
          var next = Object.assign({}, previous)
          next[step] = suggestion
          return next
        })
        var filled = {}
        for (var index = 0; index < suggestion.params.length; index++) {
          var spec = suggestion.params[index]
          if (spec && spec.key && !isNil(spec.recommended)) filled[spec.key] = spec.recommended
        }
        if (Object.keys(filled).length) {
          draftsStore[1](function (previous) {
            var next = Object.assign({}, previous)
            next[step] = Object.assign({}, previous[step] || {}, filled)
            return next
          })
        }
      }).catch(function () { /* 推荐值拉不到不阻塞页面 */ })
    }, [state && state.active, state && state.suggestedStep])

    // ---- 动作 -------------------------------------------------------------

    function handleDemo() {
      pushLog('info', '载入示例数据…')
      pushToast({ tone: 'info', title: T('demoBusy') })
      api().post('/dataset/demo', { seed: 42 }, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        pushToast({ tone: 'ok', title: T('demoDone'), advice: plainText(result.summary || '', 300) })
        pushLog('ok', T('demoDone') + '：' + plainText(result.summary || '', 300))
        return refreshState({ silent: true }).then(function (next) {
          if (next && next.active) return loadPreview()
          return null
        }).then(function () { return loadSteps() })
      }).catch(function (errorValue) { fail(errorValue, T('demo')) })
    }

    function handleRunStep(step, params) {
      var stepMeta = null
      for (var index = 0; index < stepsMeta.length; index++) if (stepsMeta[index].id === step) stepMeta = stepsMeta[index]
      var values = Object.assign({}, drafts[step] || {}, params || {})
      var payload = { step: step, params: collectStepParams(stepMeta ? stepMeta.params : [], values) }
      pushLog('info', '提交「' + stepTitle(step) + '」' + (Object.keys(payload.params).length ? '：' + JSON.stringify(payload.params) : ''))
      pushToast({ tone: 'info', title: stepTitle(step) + T('stepRunning') })
      api().post('/run', payload, { timeoutMs: TIMEOUT_BY_STEP[step] || HEAVY_API_TIMEOUT_MS }).then(function (result) {
        fullStore[1](function (previous) {
          var next = Object.assign({}, previous)
          next[step] = result
          return next
        })
        if (step === 'perturb') {
          pushToast({ tone: 'ok', title: T('perturbTitle') + '：' + plainText(result.summary || '', 200) })
        } else if (step === 'insight') {
          insightStore[1](result)
          auditStore[1](previousAudit())
          pushToast({ tone: 'ok', title: T('insightTitle'), advice: plainText(result.headline || result.summary || '', 300) })
        } else {
          pushToast({ tone: 'ok', title: stepTitle(step) + ' ✓', advice: plainText(result.summary || '', 300) })
        }
        pushLog('ok', stepTitle(step) + '：' + plainText(result.summary || '', 400))
        return refreshState({ silent: true }).then(function () { return loadPreview() })
      }).catch(function (errorValue) { fail(errorValue, stepTitle(step)) })
    }

    function previousAudit() {
      return auditStore[0]
    }

    function handleSuggest(step) {
      api().post('/suggest', { step: step }, { timeoutMs: API_TIMEOUT_MS }).then(function (raw) {
        var suggestion = normalizeSuggestion(raw)
        suggestStore[1](function (previous) {
          var next = Object.assign({}, previous)
          next[step] = suggestion
          return next
        })
        var filled = {}
        for (var index = 0; index < suggestion.params.length; index++) {
          var spec = suggestion.params[index]
          if (spec && spec.key && !isNil(spec.recommended)) filled[spec.key] = spec.recommended
        }
        draftsStore[1](function (previous) {
          var next = Object.assign({}, previous)
          next[step] = Object.assign({}, previous[step] || {}, filled)
          return next
        })
        pushToast({ tone: 'ok', title: stepTitle(step) + '：' + T('stepFillRecommended'), advice: suggestion.notes.join('；') })
      }).catch(function (errorValue) { fail(errorValue, T('stepRecommended')) })
    }

    function handleSweep(step, param, valuesText) {
      if (!step || !param) {
        pushToast({ tone: 'error', title: '参数扫描', advice: '先选步骤和数值参数' })
        return
      }
      var payload = { step: step, param: param }
      var values = parseTokenList(valuesText).map(function (item) { return asNumber(item) }).filter(function (item) { return item !== null })
      if (values.length) payload.values = values
      sweepingStore[1](true)
      pushLog('info', '参数扫描：' + stepTitle(step) + ' / ' + param + (values.length ? '：' + values.join('、') : '（自动取值）'))
      api().post('/sweep', payload, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        sweepingStore[1](false)
        sweepStore[1](result && typeof result === 'object' ? result : null)
        var best = result && result.best && typeof result.best === 'object' ? result.best : null
        pushToast({
          tone: 'ok', title: '参数扫描完成',
          advice: plainText((result && result.summary) || (best ? '最优 @' + param + '=' + fmtNumber(best.value, 3) : ''), 300),
        })
        pushLog('ok', '参数扫描完成：' + plainText((result && result.summary) || '', 300))
        return refreshState({ silent: true })
      }).catch(function (errorValue) {
        sweepingStore[1](false)
        fail(errorValue, '参数扫描')
      })
    }

    function handleSweepFill(step, param, value) {
      changeDraft(step, param, value)
      expandedStore[1](function (previous) {
        var next = Object.assign({}, previous)
        next[step] = true
        return next
      })
      pushToast({ tone: 'ok', title: '已填回参数', advice: stepTitle(step) + ' · ' + param + ' = ' + fmtNumber(value, 3) })
    }

    function handleRecipe(recipeId) {
      recipeOpenStore[1](false)
      pushToast({ tone: 'info', title: T('recipeRunning') + '：' + recipeId })
      pushLog('info', '开始一键全流程：' + recipeId)
      api().post('/recipe/run', { id: recipeId }, { timeoutMs: 90 * 60 * 1000 }).then(function (result) {
        var failed = Array.isArray(result.failed) ? result.failed : []
        pushToast({
          tone: failed.length ? 'error' : 'ok',
          title: failed.length ? T('recipePartial') : T('recipeComplete'),
          advice: plainText(result.summary || '', 400),
          message: failed.map(function (item) { return stepTitle(item.step) + '：' + plainText(item.error || '', 200) }).join('；'),
        })
        pushLog(failed.length ? 'warn' : 'ok', '一键全流程 ' + recipeId + '：' + plainText(result.summary || '', 400))
        if (result.insight) insightStore[1](result.insight)
        if (result.done) {
          var full = {}
          for (var index = 0; index < result.done.length; index++) {
            var item = result.done[index]
            if (item && item.step === 'insight' && result.insight) full.insight = result.insight
          }
          fullStore[1](function (previous) { return Object.assign({}, previous, full) })
        }
        return refreshState({ silent: true }).then(function () { return loadPreview() })
      }).catch(function (errorValue) { fail(errorValue, T('recipeRun')) })
    }

    function handlePlot(kind, params) {
      plotLoadingStore[1](true)
      pushLog('info', '出图：' + kind)
      api().post('/plot', { kind: kind, params: params || {} }, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        plotLoadingStore[1](false)
        var figure = normalizeFigure(result && result.figure ? result.figure : result)
        if (figure) {
          plotFiguresStore[1](function (previous) {
            var next = Object.assign({}, previous)
            next[kind] = figure
            return next
          })
          pushToast({ tone: 'ok', title: T('stepPlotDone') || '出图完成', advice: figure.title })
        }
      }).catch(function (errorValue) {
        plotLoadingStore[1](false)
        fail(errorValue, T('figureFail'))
      })
    }

    function handleAttach(kind, path) {
      if (!path) return
      attachingStore[1](true)
      pushLog('info', '挂载 ' + kind + '：' + path)
      api().post('/attach', { kind: kind, path: path }, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        attachingStore[1](false)
        pushToast({ tone: 'ok', title: T('responseAttachDone'), advice: plainText(result.name || result.path || path, 300) })
        pushLog('ok', '挂载成功：' + plainText(result.name || path, 300))
        return refreshState({ silent: true })
      }).catch(function (errorValue) {
        attachingStore[1](false)
        fail(errorValue, T('responseAttachBtn'))
      })
    }

    function handleExport(what, extra) {
      exportingStore[1](true)
      pushLog('info', '导出：' + what)
      api().post('/export', Object.assign({ what: what }, extra || {}), { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        exportingStore[1](false)
        var path = plainText(result.path || result.file || '', 600)
        var file = plainText(result.file || (path ? path.split('/').pop() : ''), 200)
        pushToast({
          tone: 'ok', title: T('exportDone'),
          advice: path,
          message: file ? '可在 /virtualcell/download?id=' + file + ' 下载' : '',
        })
        pushLog('ok', '导出 ' + what + ' → ' + path)
      }).catch(function (errorValue) {
        exportingStore[1](false)
        fail(errorValue, T('exportTitle'))
      })
    }

    function handleInsight() {
      auditingStore[1](true)
      pushLog('info', '生成智能分析…')
      api().post('/insight', { depth: 'standard' }, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        auditingStore[1](false)
        insightStore[1](result)
        pushToast({ tone: 'ok', title: T('insightTitle'), advice: plainText(result.headline || result.summary || '', 300) })
        pushLog('ok', '智能分析：' + plainText(result.headline || result.summary || '', 400))
        return refreshState({ silent: true })
      }).catch(function (errorValue) {
        auditingStore[1](false)
        fail(errorValue, T('insightGenerate'))
      })
    }

    function handleAudit() {
      auditingStore[1](true)
      pushLog('info', '运行自检…')
      api().post('/audit', {}, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function (result) {
        auditingStore[1](false)
        var audit = normalizeAudit(result)
        auditStore[1](audit)
        pushToast({ tone: audit.level === 'ok' ? 'ok' : 'error', title: T('insightAudit') + '：' + fmtInt(audit.score) + ' / 100', advice: plainText(audit.summary || '', 300) })
        pushLog('ok', '自检：' + plainText(audit.summary || '', 300))
        return refreshState({ silent: true })
      }).catch(function (errorValue) {
        auditingStore[1](false)
        fail(errorValue, T('insightAudit'))
      })
    }

    function handleSelectDataset(id) {
      api().post('/dataset/select', { dataset: id }, { timeoutMs: API_TIMEOUT_MS }).then(function () {
        fullStore[1]({})
        insightStore[1](null)
        auditStore[1](null)
        plotFiguresStore[1]({})
        pushLog('info', '切换数据集：' + id)
        return refreshState({ silent: true }).then(function () { return loadPreview() })
      }).catch(function (errorValue) { fail(errorValue, T('datasetSwitch')) })
    }

    function handleCloseDataset() {
      if (!state || !state.active) return
      var id = state.active
      api().post('/dataset/close', { dataset: id }, { timeoutMs: API_TIMEOUT_MS }).then(function () {
        fullStore[1]({})
        insightStore[1](null)
        auditStore[1](null)
        pushLog('info', '关闭数据集：' + id)
        return refreshState({ silent: true })
      }).catch(function (errorValue) { fail(errorValue, T('datasetClose')) })
    }

    function handleCopy(text, label) {
      copyText(text).then(function () {
        pushToast({ tone: 'ok', title: label || T('copied'), advice: plainText(text, 160) })
      }).catch(function () {
        pushToast({ tone: 'error', title: T('copyFailed'), advice: plainText(text, 200) })
      })
    }

    function handleRefresh() {
      refreshState().then(function () { return loadSteps() })
    }

    function handleRefreshRuns() {
      refreshingRunsStore[1](true)
      api().get('/runs?limit=50', { timeoutMs: API_TIMEOUT_MS }).then(function (raw) {
        refreshingRunsStore[1](false)
        var list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.runs) ? raw.runs : [])
        runsStore[1](normalizeRuns(list))
      }).catch(function (errorValue) {
        refreshingRunsStore[1](false)
        fail(errorValue, T('recordsLoadFail'))
      })
    }

    function handleToggleEngine() {
      engineOpenStore[1](!engineOpenStore[0])
    }

    function handleRestartEngine() {
      api().post('/engine/restart', {}, { timeoutMs: HEAVY_API_TIMEOUT_MS }).then(function () {
        pushToast({ tone: 'ok', title: T('engineRestartDone') })
        pushLog('ok', T('engineRestartDone'))
        return refreshState({ silent: true })
      }).catch(function (errorValue) { fail(errorValue, T('engineRestart')) })
    }

    function handleRunImport(payload) {
      if (!payload) return
      importOpenStore[1](false)
      if (payload.kind === 'path') {
        pushLog('info', '导入数据：' + payload.payload.path)
        pushToast({ tone: 'info', title: T('importBusy') })
        api().post('/dataset/open', payload.payload, { timeoutMs: 30 * 60 * 1000 }).then(function (result) {
          pushToast({ tone: 'ok', title: T('importSuccess'), advice: plainText(result.summary || result.name || '', 300) })
          pushLog('ok', T('importSuccess') + '：' + plainText(result.summary || '', 300))
          return refreshState({ silent: true }).then(function () { return loadPreview() }).then(function () { return loadSteps() })
        }).catch(function (errorValue) { fail(errorValue, T('importStart')) })
        return
      }
      var file = payload.file
      if (!file || typeof fetch !== 'function') {
        pushToast({ tone: 'error', title: T('importNeedSource') })
        return
      }
      pushToast({ tone: 'info', title: T('importBusy') })
      var headers = {
        'x-dsh-filename': payload.payload && payload.payload.name ? payload.payload.name : (file.name || 'upload.h5ad'),
        'x-dsh-format': (payload.payload && payload.payload.data_format) || 'auto',
        'x-dsh-transpose': payload.payload && payload.payload.transpose ? '1' : '0',
        'content-type': 'application/octet-stream',
      }
      fetch(API + '/dataset/upload', {
        method: 'POST',
        headers: headers,
        body: file,
      }).then(function (response) {
        return response.text().then(function (text) {
          var body = null
          try { body = text ? JSON.parse(text) : {} } catch (ignored) { body = { ok: false, code: 'internal', error: '返回不是 JSON' } }
          if (!response.ok || (body && body.ok === false)) throw normalizeApiError(response.status, body)
          return body
        })
      }).then(function (result) {
        pushToast({ tone: 'ok', title: T('importSuccess'), advice: plainText(result.summary || '', 300) })
        pushLog('ok', T('importSuccess') + '：' + plainText(result.summary || '', 300))
        return refreshState({ silent: true }).then(function () { return loadPreview() }).then(function () { return loadSteps() })
      }).catch(function (errorValue) { fail(errorValue, T('importStart')) })
    }

    function openPerturbGene(gene) {
      pendingGeneStore[1](gene)
      tabStore[1]('perturb')
    }

    function changeDraft(step, key, value) {
      var stepMeta = null
      for (var index = 0; index < effectiveSteps.length; index++) {
        if (effectiveSteps[index].id === step) { stepMeta = effectiveSteps[index]; break }
      }
      var defaults = stepMeta ? defaultParams(stepMeta.params) : {}
      draftsStore[1](function (previous) {
        var next = Object.assign({}, previous)
        next[step] = Object.assign({}, defaults, previous[step] || {})
        next[step][key] = value
        return next
      })
    }

    // ---- 派生数据 ---------------------------------------------------------

    var effectiveSteps = stepsMeta.length ? stepsMeta : normalizeSteps({ steps: state && state.raw && Array.isArray(state.raw.steps) ? state.raw.steps : [] }).steps
    var dataset = state ? state.activeDataset : null
    var stepCards = effectiveSteps.map(function (meta, index) {
      var record = dataset && dataset.steps ? dataset.steps[meta.id] : null
      var suggestion = suggestions[meta.id]
      var recommended = {}
      var reasons = {}
      if (suggestion) {
        for (var index2 = 0; index2 < suggestion.params.length; index2++) {
          var spec = suggestion.params[index2]
          if (!spec || !spec.key) continue
          if (!isNil(spec.recommended)) recommended[spec.key] = spec.recommended
          if (spec.why) reasons[spec.key] = spec.why
        }
      }
      return {
        meta: meta,
        index: index,
        status: stepStatusOf(meta, doneSet, busy ? busy.step : ''),
        record: record,
        result: resultOf(state, meta.id),
        recommendation: recommended,
        reasons: reasons,
      }
    })
    var expandedCount = Object.keys(expanded).filter(function (key) { return expanded[key] }).length
    void expandedCount

    function renderTab() {
      if (!state || !dataset) return h(EmptyGuide, {
        state: state, doneSet: doneSet, busy: busy !== null,
        onDemo: handleDemo,
        onImport: function () { importOpenStore[1](true) },
        onQuick: function () { handleRecipe('quick') },
        onOpenPerturb: function () { tabStore[1]('perturb') },
      })
      var common = {
        state: state,
        fullResults: fullResults,
        busy: busy !== null,
        plotting: plotLoading,
        preview: preview,
        onRunStep: handleRunStep,
        onRunRecipe: handleRecipe,
        onPlot: handlePlot,
        onPerturbGene: openPerturbGene,
      }
      if (activeTab === 'state') {
        if (!doneSet.state) {
          return h(EmptyGuide, {
            state: state, doneSet: doneSet, busy: busy !== null,
            onDemo: handleDemo,
            onImport: function () { importOpenStore[1](true) },
            onQuick: function () { handleRecipe('quick') },
            onOpenPerturb: function () { tabStore[1]('perturb') },
          })
        }
        return h(StateTab, Object.assign({}, common, {
          embedding: preview && preview.embedding ? preview.embedding : [],
          plotFigure: plotFigures.state_embedding || plotFigures.state_embedding_perturb || null,
        }))
      }
      if (activeTab === 'network') return h(NetworkTab, common)
      if (activeTab === 'perturb') return h(PerturbTab, Object.assign({}, common, { pendingGene: pendingGeneStore[0] }))
      if (activeTab === 'screen') return h(ScreenTab, common)
      if (activeTab === 'response') {
        return h(ResponseTab, Object.assign({}, common, { attaching: attaching, onAttach: handleAttach }))
      }
      if (activeTab === 'transition') return h(TransitionTab, common)
      if (activeTab === 'insight') {
        return h(InsightTab, Object.assign({}, common, {
          insightResult: insightStore[0],
          auditResult: auditStore[0],
          auditing: auditing,
          exporting: exporting,
          onInsight: handleInsight,
          onAudit: handleAudit,
          onExport: handleExport,
        }))
      }
      return h(RecordsTab, {
        state: state,
        steps: effectiveSteps,
        runs: runsStore[0],
        sweep: sweepStore[0],
        sweeping: sweeping,
        onSweep: handleSweep,
        onFillValue: handleSweepFill,
        busy: busy !== null,
        exporting: exporting,
        refreshingRuns: refreshingRuns,
        onRefreshRuns: handleRefreshRuns,
        onExport: handleExport,
        onRunStep: handleRunStep,
        onCopy: handleCopy,
        onDemo: handleDemo,
      })
    }

    var stepsColumn = h('div', { className: 'vc-col vc-left' },
      h(DatasetCard, {
        state: state,
        onDemo: handleDemo,
        onImport: function () { importOpenStore[1](true) },
        onSelect: handleSelectDataset,
        onCloseDataset: handleCloseDataset,
      }),
      h('div', { className: 'vc-row-between', style: { padding: '0 2px' } },
        h('div', { className: 'vc-card-title' }, T('leftTitle')),
        h('span', { className: 'vc-muted', style: { fontSize: 10.5 } }, T('leftHint'))),
      stepCards.map(function (item) {
        return h(StepCard, {
          key: item.meta.id,
          meta: item.meta,
          index: item.index,
          status: item.status,
          record: item.record,
          result: item.result,
          doneSet: doneSet,
          busy: busy !== null && busy.step === item.meta.id,
          busyPct: busy ? busy.pct : null,
          expanded: expanded[item.meta.id] === true,
          values: drafts[item.meta.id] || defaultParams(item.meta.params),
          recommended: item.recommendation,
          reasons: item.reasons,
          onToggle: function () {
            expandedStore[1](function (previous) {
              var next = Object.assign({}, previous)
              next[item.meta.id] = !next[item.meta.id]
              return next
            })
          },
          onChange: function (key, value) { changeDraft(item.meta.id, key, value) },
          onRun: function () { handleRunStep(item.meta.id) },
          onSuggest: function () { handleSuggest(item.meta.id) },
          tabForStep: item.meta.id === 'grn' ? 'network' : item.meta.id === 'perturb' ? 'perturb' : 'state',
        })
      }),
      h('div', { className: 'vc-muted', style: { fontSize: 10.5, padding: '2px 2px 10px' } }, T('stepReadyHint')))

    var centerColumn = h('div', { className: 'vc-center' },
      h(TabsBar, {
        active: activeTab,
        doneSet: doneSet,
        onChange: function (key) { tabStore[1](key) },
      }),
      loading && !state
        ? h('div', { className: 'vc-panel' }, h(Loading, { text: T('loadingData') }))
        : null,
      error && !state
        ? h('div', { className: 'vc-panel' }, h(ErrorNote, { text: error, onRetry: handleRefresh }))
        : null,
      state ? renderTab() : null,
      state && state.activeDataset && expandedCount === 0
        ? h('div', { className: 'vc-muted', style: { fontSize: 10.5 } }, T('leftHint'))
        : null)

    var rightColumn = h(AgentPanel, {
      state: state,
      suggest: state && state.suggestedStep ? suggestions[state.suggestedStep] : null,
      insightResult: insightStore[0],
      auditResult: auditStore[0],
      logs: logs,
      busy: busy !== null,
      onCopy: handleCopy,
      onOpenTab: function (key) { tabStore[1](key) },
      onClearLogs: function () { logsStore[1]([]) },
    })

    return h('div', { className: 'vc-root' },
      h('style', null, CSS),
      h(TopBar, {
        state: state,
        busy: busy !== null,
        refreshing: refreshing,
        onDemo: handleDemo,
        onImport: function () { importOpenStore[1](true) },
        onToggleEngine: handleToggleEngine,
        onToggleRecipe: function () { recipeOpenStore[1](!recipeOpenStore[0]) },
        onRefresh: handleRefresh,
      }),
      engineOpen
        ? h(EnginePopover, {
            engine: state ? state.engine : normalizeEngine(null),
            onClose: function () { engineOpenStore[1](false) },
            onRestart: handleRestartEngine,
          })
        : null,
      recipeOpen
        ? h(RecipeMenu, {
            onClose: function () { recipeOpenStore[1](false) },
            onRun: handleRecipe,
          })
        : null,
      h('div', { className: 'vc-body' }, stepsColumn, centerColumn, rightColumn),
      importOpen
        ? h(ImportDialog, {
            busy: false,
            onClose: function () { importOpenStore[1](false) },
            onSubmit: handleRunImport,
          })
        : null,
      h(ToastHost, { toasts: toasts, onClose: dismissToast }))
  }

  /** 整页入口：错误边界包住主体（单点异常不黑整页）。 */
  function WorkbenchPage() {
    return h(PluginErrorBoundary, null, h(WorkbenchBody))
  }

  // ==========================================================================
  // 侧边栏入口
  // ==========================================================================

  function SidebarIcon(props) {
    return h(PluginErrorBoundary, { mini: true }, h(SidebarGlyph, { size: props && props.size ? props.size : 18 }))
  }

  // ==========================================================================
  // 插件入口：locale 字典 + 两个槽（main 整页 + sidebar.panellist 入口）
  // ==========================================================================

  function apply(ctx) {
    var locale = ctx && ctx.locale
    if (locale && typeof locale.register === 'function') {
      var registerDictionaries = function () {
        try {
          return locale.register(NS, { zh: DICT_ZH, en: DICT_EN })
        } catch (error) {
          warnPlugin('failed to register locale dictionaries', error)
          return undefined
        }
      }
      try {
        if (typeof ctx.effect === 'function') ctx.effect(registerDictionaries, NS + ': locale')
        else registerDictionaries()
      } catch (error) {
        warnPlugin('failed to install locale effect', error)
      }
      translator = createTranslator(locale)
    }

    var slots = ctx && ctx.slots
    if (!slots && ctx && typeof ctx.get === 'function') {
      try { slots = ctx.get('slots') } catch (error) { slots = null }
    }
    if (!slots || typeof slots.register !== 'function' || typeof slots.inject !== 'function') {
      warnPlugin('slots service unavailable; UI not registered')
      return
    }

    var disposers = []
    function keep(handle) {
      if (typeof handle === 'function') disposers.push(handle)
      return handle
    }
    try {
      if (typeof ctx.effect === 'function') {
        ctx.effect(function () {
          return function () {
            while (disposers.length) {
              var dispose = disposers.pop()
              try { dispose() } catch (ignored) { /* 单个 disposer 失败不影响其它 */ }
            }
          }
        }, NS + ': slot disposers')
      }
    } catch (error) {
      warnPlugin('failed to install slot cleanup', error)
    }

    try {
      keep(slots.inject('main', function () {
        return keep(slots.register({ name: 'main', key: 'virtualcell', locale: NS }, WorkbenchPage))
      }))
    } catch (error) {
      warnPlugin('failed to register main slot', error)
    }

    try {
      keep(slots.inject('sidebar.panellist', function () {
        return keep(slots.register({
          name: 'sidebar.panellist',
          id: 'virtualcell',
          order: 5,
          label: function () { return T('nav') },
          locale: NS,
        }, SidebarIcon))
      }))
    } catch (error) {
      warnPlugin('failed to register sidebar.panellist slot', error)
    }
  }

  // ==========================================================================
  // Client 模块工厂：注入 react（只 require('react')）并返回插件对象
  // ==========================================================================

  function factory(require) {
    var react = null
    try { react = require('react') } catch (error) { warnPlugin('failed to require react', error) }
    if (!react) throw new Error('dsh-virtualcell client: react unavailable')
    React = react
    ErrorBoundaryImpl = makeErrorBoundary()
    return {
      name: NS,
      inject: ['slots', 'locale'],
      apply: apply,
      __test__: Object.assign({}, VC_TEST, {
        components: {
          WorkbenchPage: WorkbenchPage,
          WorkbenchBody: WorkbenchBody,
          TopBar: TopBar,
          EnginePopover: EnginePopover,
          DatasetCard: DatasetCard,
          StepCard: StepCard,
          ParamForm: ParamForm,
          TabsBar: TabsBar,
          EmptyGuide: EmptyGuide,
          StateCanvas: StateCanvas,
          StateTab: StateTab,
          NetworkTab: NetworkTab,
          PerturbTab: PerturbTab,
          ScreenTab: ScreenTab,
          ResponseTab: ResponseTab,
          InsightTab: InsightTab,
          RecordsTab: RecordsTab,
          SweepPanel: SweepPanel,
          AgentPanel: AgentPanel,
          ImportDialog: ImportDialog,
          SidebarIcon: SidebarIcon,
          PluginErrorBoundary: PluginErrorBoundary,
        },
      }),
    }
  }

  // ==========================================================================
  // locale 词典：直接从 locale/zh.json、locale/en.json 生成（不要手抄）
  // ==========================================================================

  var DICT_ZH = {"appTitle":"虚拟细胞工作台","appSubtitle":"单细胞 → 状态空间 → 虚拟扰动 → 靶点筛选","nav":"虚拟细胞","engineReady":"引擎就绪","engineBooting":"引擎启动中","engineBad":"引擎异常","engineBadgeHint":"查看引擎详情与可选模型","engineTitle":"引擎信息","enginePython":"Python","engineVersion":"版本","engineBackend":"计算后端","engineWorkdir":"工作目录","engineAdapters":"可选模型 / 后端","engineAvailable":"可用","engineMissing":"未安装","engineInstallHint":"安装提示","enginePackages":"依赖包","engineRestart":"重启引擎","engineRestartConfirm":"重启会中断正在运行的计算，确定继续？","engineRestarting":"正在重启…","engineNoAdapters":"没有可选后端可用，当前使用内置 numpy 实现","engineRestartDone":"引擎已重启","engineWorkdirHint":"原始数据、运行记录与导出都在这里","enginePopupClose":"关闭","close":"关闭","cancel":"取消","confirm":"确定","retry":"重试","refresh":"刷新","refreshHint":"重新拉取工作台状态","refreshBusy":"刷新中…","loading":"加载中…","loadingData":"正在读取工作台状态…","loadingHint":"请稍候","copy":"复制","copied":"已复制","copyFailed":"复制失败，请手动选中文本复制","closeToast":"关闭提示","copyHint":"复制后可以直接粘给 AI","demo":"载入示例数据","demoHint":"内置 2,000 细胞 × 1,200 基因模拟数据，约 3 秒","demoBusy":"正在生成示例数据…","demoDone":"示例数据已载入","importData":"导入数据","importTitle":"导入单细胞数据","importSubtitle":"支持 h5ad、10x、loom、csv/tsv、zip、GEO series matrix 与本机直链","importPath":"数据路径或下载链接","importPathPlaceholder":"/Users/you/data.h5ad 或 https://…","importPathHint":"本机文件填绝对路径；http(s) 链接会先下载再识别","importUpload":"从本机选择文件","importUploadHint":"大文件建议直接填路径，上传会复制一份原始数据留档","importFormat":"数据格式","formatAuto":"自动识别（推荐）","formatH5ad":"h5ad（AnnData）","format10xH5":"10x h5","format10xMtx":"10x mtx 三件套","formatLoom":"loom","formatMatrix":"csv/tsv/txt 表达矩阵","formatGeo":"GEO series matrix","formatArchive":"zip/tar 压缩包","formatXlsx":"Excel 表格","formatHint":"不确定就选自动识别；识别失败时会提示手动指定","importTranspose":"矩阵方向转置","importTransposeHint":"细胞×基因 与 基因×细胞 判断错误时才需要打开","importAdvanced":"高级选项","importMember":"压缩包内的文件","importMemberHint":"压缩包里有多个候选文件时填相对路径","importSheet":"Excel 的 sheet 名","importSheetHint":"留空使用第一个 sheet","importGeneCol":"基因名列","importGeneColHint":"矩阵首列是基因名时填列名，留空自动判断","importInspect":"先探测格式","inspectBusy":"正在探测…","inspectOk":"探测完成","inspectCells":"细胞数","inspectGenes":"基因数","inspectFormat":"识别格式","inspectWarnings":"探测提示","importStart":"导入并载入","importBusy":"正在导入，大文件需要几分钟…","importNeedSource":"请先填写路径、链接或选择文件","importSuccess":"数据已导入","importNoPreview":"未返回预览信息","transposeOn":"已转置","transposeOff":"未转置","recipeRun":"一键全流程","recipeHint":"自动按顺序跑完整条分析链","recipeQuick":"快速体验","recipeQuickDesc":"质控 → 状态表征 → 注释 → 调控网络 → 虚拟扰动 → 智能分析","recipeQuickTime":"约 1–2 分钟","recipeStandard":"标准分析","recipeStandardDesc":"快速流程 + 虚拟筛选 + 状态转移 + 自检","recipeStandardTime":"约 3–5 分钟","recipeFull":"完整流程","recipeFullDesc":"标准流程 + 扰动响应预测 + 报告导出（需要扰动表）","recipeFullTime":"约 5–10 分钟","recipeRunning":"正在执行一键流程","recipeCurrent":"当前步骤","recipeDone":"已完成","recipeFailed":"失败","recipeComplete":"一键流程完成","recipePartial":"流程部分完成","recipeGoInsight":"查看智能分析","recipeStart":"开始","recipeMenuHint":"选择流程规模","leftTitle":"分析流程","leftHint":"自上而下依次运行；绿勾表示已完成","datasetNone":"还没有数据集","datasetNoneHint":"先载入示例数据，或导入自己的表达矩阵","datasetSwitch":"切换数据集","datasetSwitchHint":"点击数据集卡片切换或关闭","datasetCells":"细胞","datasetGenes":"基因","datasetTfs":"TF","datasetStepsDone":"已完成 {v} 步","datasetCurrentStep":"当前步骤：{v}","datasetWarnings":"提示 {v} 条","datasetTruth":"含模拟真值","datasetSize":"大小","datasetSource":"来源","datasetClose":"关闭数据集","datasetNoOthers":"没有其他数据集","datasetActive":"使用中","datasetRawPath":"原始数据路径","datasetUse":"使用","datasetOpen":"打开","datasetCreated":"创建时间","stepParams":"参数","stepAdvanced":"高级参数","stepAdvancedCount":"高级参数（{v}）","stepRun":"运行","stepRerun":"重跑","stepPlotOnly":"只出图","stepRunning":"运行中…","stepStatusDone":"已完成","stepStatusRunning":"运行中","stepStatusReady":"待运行","stepStatusBlocked":"未就绪","stepStatusFailed":"失败","stepNeedPrereq":"需先运行：{v}","stepSummaryEmpty":"尚未运行","stepRecommended":"推荐 {v}","stepRecommendedEmpty":"暂无推荐","stepFillRecommended":"点击填入推荐值","stepWhy":"为什么","stepEffect":"影响","stepRisk":"风险","riskLow":"低风险","riskMedium":"中风险","riskHigh":"高风险","stepCost":"耗时","stepOutputs":"产出","stepLastRun":"上次 {v}","stepPlotDone":"图已生成","stepError":"这一步出错了","stepNoParams":"这一步没有可调参数","stepReadyHint":"依赖已满足，可以运行","stepsLoaded":"流程步骤已就绪","noSteps":"流程步骤加载失败，已使用内置步骤表","tabState":"状态图","tabNetwork":"网络","tabPerturb":"扰动","tabScreen":"筛选","tabResponse":"响应","tabTransition":"转移","tabInsight":"智能分析","tabRecords":"记录","tabNeedState":"先完成「状态表征」才能看这张图","tabNeedGrn":"先完成「调控网络」才有网络结果","tabNeedPerturb":"先做一次虚拟扰动","tabNeedScreen":"先跑一次虚拟筛选","tabNeedTransition":"先完成「状态转移」","tabNeedInsight":"先生成智能分析","tabNeedResponse":"先挂载扰动表或使用示例数据","stateColorBy":"着色方式","stateColorCluster":"细胞状态（聚类）","stateColorCellState":"注释状态","stateColorObs":"样本注释列","stateColorGene":"基因表达","stateGenePlaceholder":"输入基因名，如 KLF4","stateGeneApply":"着色","stateStatic":"静态图","stateInteractive":"交互图","stateOverlayPerturb":"叠加扰动箭头","stateLegend":"图例（点击可隐藏/显示）","stateHidden":"已隐藏","stateHoverHint":"滚轮缩放 · 拖拽平移 · 双击复位","stateResetView":"复位视图","statePoints":"显示 {v} 个细胞","stateDownsample":"（已下采样显示）","stateNoData":"还没有状态图数据","stateNoDataHint":"运行「状态表征」后，这里会显示可缩放的细胞状态空间","stateFetchFail":"交互数据加载失败，已切换为静态图","stateCells":"细胞数","stateClusters":"状态数","stateSilhouette":"轮廓系数","stateVariance":"解释方差","statePerturbOverlay":"箭头：起点状态 → 预测目标状态","stateArrowDelta":"位移 {v}","stateRunState":"运行状态表征","stateShowStatic":"查看静态图","stateHideStatic":"收起静态图","stateGeneValue":"表达","stateStaticHint":"静态图由后端 matplotlib 出图，可下载存档","stateLegendHiddenCount":"已隐藏 {v} 类","networkTitle":"基因调控网络","networkCluster":"按状态过滤","networkAllClusters":"全部状态","networkHubTitle":"Hub 转录因子（按调控强度）","networkHubEmpty":"还没有 hub 因子结果，先完成「调控网络」","networkEdgesTitle":"调控边","networkEdgeTf":"转录因子","networkEdgeTarget":"靶基因","networkEdgeScore":"权重","networkEdgeSign":"方向","networkEdgeCluster":"状态","networkSignUp":"激活","networkSignDown":"抑制","networkFilterHint":"点击表头排序，最多显示 200 行","networkStatic":"网络图","networkValidateTitle":"GRN 校验","networkValidateRun":"运行 GRN 校验","networkValidateNeedTruth":"demo 数据带模拟真值可直接校验；真实数据需先挂载 GRN 先验","networkValidatePass":"校验通过","networkValidateFail":"校验未通过","networkValidateHint":"仅 demo 或挂载 GRN 先验后可校验","networkNoResult":"还没有网络结果","networkNoResultHint":"运行「状态表征」后再运行「调控网络」","networkRunGrn":"运行调控网络","networkEdgeCount":"共 {v} 条边","networkHubCount":"共 {v} 个 hub 因子","perturbTitle":"虚拟扰动","perturbGene":"目标基因","perturbGenePlaceholder":"输入基因名，如 KLF4","perturbGeneHint":"建议先用网络里的 hub 转录因子","perturbHubChips":"Hub 因子（点击填入）","perturbMode":"扰动方式","perturbModeKo":"敲除","perturbModeKd":"敲低","perturbModeOe":"过表达","perturbModeHint":"敲除最彻底；敲低保留部分表达；过表达用于看增强方向","perturbStrength":"扰动强度","perturbStrengthHint":"1.0 表示完全敲除，或按最大表达量过表达","perturbCluster":"作用状态","perturbAllClusters":"全部状态","perturbRun":"运行虚拟扰动","perturbAffected":"受影响基因数","perturbMaxDelta":"最大绝对变化","perturbStateShift":"状态位移","perturbTargetState":"预测目标状态","perturbSourceState":"起点状态","perturbConfidence":"置信度","perturbAffectedTitle":"受影响最大的基因","perturbGeneCol":"基因","perturbDeltaCol":"变化量","perturbPerCluster":"每个状态的位移","perturbDeltaNorm":"位移强度","perturbTargetCluster":"指向状态","perturbCos":"余弦相似","perturbValidity":"方法学提示","perturbNoResult":"还没有扰动结果","perturbNoResultHint":"先跑「调控网络」，再挑一个 hub 因子做虚拟扰动","perturbNeedGrn":"需要先完成「调控网络」","perturbSummaryTitle":"本次扰动结论","perturbRunAgain":"再跑一次","perturbArrowNote":"状态图里已叠加箭头：起点状态 → 目标状态","perturbTopDelta":"前 {v} 个","perturbNoAffected":"这次扰动没有显著受影响的基因，试试加大强度或换一个 TF","screenTitle":"虚拟筛选","screenCandidates":"候选基因","screenCandidatesEmpty":"留空 = 使用内置人类 TF 池","screenCandidatesHint":"用逗号或空格分隔；留空时自动使用 GRN 的 TF 池","screenTopN":"展示前 N 个","screenSpecificity":"计算特异性","screenSpecificityHint":"特异性越高，影响越集中在一类状态，更适合做靶点","screenMode":"模拟扰动方式","screenRun":"开始虚拟筛选","screenRankTitle":"排名","screenRank":"排名","screenGene":"基因","screenScore":"综合分","screenShift":"状态位移","screenSpecificityCol":"特异性","screenTargetState":"指向状态","screenWhy":"理由","screenPerturbThis":"对它做扰动","screenNoResult":"还没有筛选结果","screenNoResultHint":"先跑「调控网络」，或直接点上面的开始筛选（会自动使用内置 TF 池）","screenTopChart":"综合分前 {v}","screenProgress":"正在逐个模拟候选基因…","screenCandidateCount":"本次候选 {v} 个基因","responseTitle":"扰动响应预测","responseNeedData":"需要一张 Perturb-seq 风格的扰动表","responseNeedDataHint":"表里至少要有 cell_id、perturbation 两列（或 h5ad 的 obs 里的 perturbation）。没有表也可以先用示例数据。","responseAttach":"挂载扰动表","responsePath":"扰动表路径","responseAttachBtn":"挂载","responseAttached":"已挂载：{v}","responseUseDemo":"用示例数据自带的扰动表","responseRun":"运行响应预测","responsePredVsTrue":"预测 vs 真实","responsePerPert":"每个扰动的误差","responseR2":"R²","responseSpearman":"Spearman","responseNPert":"扰动数","responseMethod":"方法","responseNoResult":"还没有响应预测结果","responseNoResultHint":"挂载扰动表后运行响应预测；没有扰动表时会把药物或基因签名投影到状态空间","responseNeedState":"需要先完成「状态表征」","responseAttaching":"正在挂载…","responseAttachDone":"扰动表已挂载","responsePerturbation":"扰动","responseNoAttachHint":"也可以在「导入数据」里挂载基因集、GRN 先验或药物签名","transitionTitle":"状态转移","transitionRun":"计算状态转移","transitionMatrix":"状态转移矩阵","transitionSources":"起点状态（流出）","transitionSinks":"汇点状态（流入）","transitionNetFlow":"净流向","transitionFrom":"从","transitionTo":"到","transitionFlow":"净流量","transitionStability":"稳定性","transitionNStates":"状态数","transitionNoResult":"还没有状态转移结果","transitionNoResultHint":"运行「状态表征」后即可计算状态转移","transitionNeedState":"需要先完成「状态表征」","transitionHeatHint":"行 = 起点状态，列 = 终点状态；数值为归一化转移概率","insightTitle":"智能分析","insightGenerate":"生成智能分析","insightAudit":"运行自检","insightExportReport":"导出中文报告","insightExportMethods":"导出 Methods","insightHeadline":"结论","insightRankedTargets":"推荐靶点","insightNextActions":"下一步建议","insightCaveats":"方法学提醒","insightNoResult":"还没有智能分析","insightNoResultHint":"点上面的按钮生成一份中文结论，会引用本次运行的真实数字","insightNeedState":"先完成「状态表征」再生成智能分析","insightAuditScore":"自检得分","insightAuditChecks":"检查项","insightAuditPass":"通过","insightAuditWarn":"注意","insightAuditFail":"不通过","insightAuditHint":"自检是确定性的规则检查，不替代科研判断","insightDisclaimer":"结论只引用本次运行的真实数字；低置信度会写进方法学提醒","insightRegenerate":"重新生成","insightRankedGene":"基因","insightRankedScore":"分数","insightRankedWhy":"推荐理由","insightNextStep":"去做","insightNoChecks":"自检没有返回检查项","insightAuditRunning":"正在自检…","insightRunning":"正在生成…","insightExportRunning":"正在导出…","insightExportDone":"已导出：{v}","recordsTitle":"运行记录","recordsHint":"每次运行都会留下参数、图、表与产物路径，可导出 zip","recordsId":"运行 ID","recordsStep":"步骤","recordsTime":"时间","recordsDuration":"耗时","recordsFigures":"图","recordsTables":"表","recordsStatus":"状态","recordsDetail":"详情","recordsParams":"参数","recordsArtifacts":"产物","recordsFiles":"文件","recordsExportZip":"导出 run_zip","recordsRerun":"重跑这一步","recordsCopyRepro":"复制复现指令","recordsNoRuns":"还没有运行记录","recordsNoRunsHint":"先跑一个步骤，这里会逐条记录参数与产物","recordsEmpty":"暂无","recordsParamsEmpty":"运行记录未包含参数明细，可在导出的 zip 里查看","recordsArtifactsEmpty":"运行记录未返回产物列表，可在导出目录查看","recordsExportDone":"已导出：{v}","recordsExportRunning":"正在打包…","recordsRefresh":"刷新记录","recordsStatusOk":"成功","recordsStatusBad":"失败","recordsDetailTitle":"运行详情","recordsNoFiles":"暂无文件","recordsOpenFolder":"产物目录","recordsLoadFail":"运行记录加载失败","agentTitle":"Agent 协作","agentHint":"把状态和结论一键变成可复制给 AI 的指令","agentConclusions":"当前结论","agentNoConclusions":"还没有结论，跑第一步之后这里会出现","agentConclusionsFrom":"来自：{v}","agentNext":"给 AI 的下一步","agentNextHint":"点「复制」，直接粘给任意 AI 助手","agentCopy":"复制","agentCopyAll":"复制完整上下文","agentContext":"状态速览","agentContextDataset":"数据集","agentContextSteps":"已完成步骤","agentContextLastRun":"最近运行","agentContextExport":"产物目录","agentContextPending":"待办","agentNoDataset":"还没有数据集","agentLog":"操作日志","agentLogEmpty":"暂无日志，操作后这里会记录每一步","agentLogClear":"清空","agentReadResult":"让 Agent 解读","agentReadResultHint":"复制一段带本次真实数字的解读指令","agentCopied":"指令已复制","agentContextCopied":"上下文已复制","agentWorkdir":"工作目录","agentEngine":"引擎","agentNeedDataset":"先载入数据后才有可复制的上下文","agentRunHint":"复制后可以直接发给任意 AI 助手","onboardTitle":"虚拟细胞能干什么？","onboardIntro":"把单细胞表达谱变成可推演的「细胞状态空间」：先看细胞分成哪些状态、谁在调控它们，再在没有湿实验的情况下推演「敲掉某个基因，细胞会往哪偏」，最后排出值得优先验证的靶点。","onboardStep1":"载入示例数据","onboardStep1Desc":"内置 2,000 个细胞 × 1,200 个基因的模拟数据，带真实 marker 与模拟真值，3 秒载入。","onboardDemo":"载入示例数据","onboardStep2":"一键跑 Quick","onboardStep2Desc":"自动跑完质控 → 状态表征 → 注释 → 调控网络 → 虚拟扰动 → 智能分析。","onboardQuick":"一键跑 Quick","onboardStep3":"看扰动结果","onboardStep3Desc":"在「扰动」标签里换基因、调强度，看预测的细胞状态偏移；右栏 Agent 面板能一键生成给 AI 的指令。","onboardOpenPerturb":"去扰动页","onboardImport":"我有自己的数据，去导入","onboardTip":"整个过程都在本机运行，原始数据只读留档；每一步运行都有记录可回溯。","onboardBusy":"正在准备…","errTitle":"操作失败","errRetry":"可重试","errPrerequisite":"请先运行上一步","errPrerequisiteStep":"请先运行上一步：{v}","errBusy":"引擎正在计算上一步，请稍候再试","errNoPython":"没有找到可用的 Python 解释器，请在插件设置里指定 python 路径","errEngineExited":"引擎进程已退出，点「刷新」或重启插件即可拉起","errTimeout":"计算超时了，可以减小数据量或稍后重试","errBadRequest":"参数不合法，请检查输入（数值范围、基因名等）","errUnsupported":"当前后端不支持这个操作，可以换参数或安装可选依赖","errAmbiguous":"数据格式无法唯一确定，请在导入时手动选择格式","errNotFound":"没有找到对应的数据或文件，请确认路径是否仍然存在","errAuth":"仅本机可访问：请在 DeepSeek Harness 客户端内打开","errNetwork":"无法连接工作台服务，请确认插件已启用并刷新","errInternal":"引擎内部错误，重试一次；仍失败请在「记录」里导出 run_zip 反馈","errDisposed":"工作台已关闭，刷新页面即可重新连接","errUnknown":"未知错误","errNeedDataset":"先载入一个数据集","errNoData":"暂无数据","crashTitle":"这个区块崩了，但其他功能还能用","crashHint":"可以点「重试」恢复；如果反复出现，请刷新页面","figureFail":"图片加载失败","noFigure":"暂无图片","noSelection":"不选择","yes":"是","no":"否","warnings":"提示","notes":"说明","metrics":"指标","tables":"表","figures":"图","moreMetrics":"更多指标","rawMetrics":"原始指标","metricAffected":"受影响基因数","metricMaxDelta":"最大绝对变化","metricStateShift":"状态位移","metricTargetState":"目标状态","metricSourceState":"起点状态","metricConfidence":"置信度","metricBackend":"计算后端","metricCells":"细胞数","metricGenes":"基因数","metricClusters":"状态数","metricStates":"状态数","metricSilhouette":"轮廓系数","metricVariance":"解释方差","metricNtf":"TF 数","metricEdges":"边数","metricDensity":"网络密度","metricCvR2":"交叉验证 R²","metricAuroc":"AUROC","metricAuprc":"AUPRC","metricSpearman":"Spearman","metricPearson":"Pearson","metricR2":"R²","metricNPert":"扰动数","metricStability":"稳定性","metricMethod":"方法","metricTargets":"靶点数","metricHvg":"高变基因数","metricPcs":"主成分数","metricScore":"分数","metricOther":"指标","exportTitle":"导出","exportBtn":"导出","exportRunning":"正在导出…","exportDone":"已导出","exportFailed":"导出失败","exportCsv":"CSV","exportJson":"JSON","exportMarkdown":"Markdown","exportPython":"Python","exportZip":"ZIP","plotLoading":"正在出图…","plotFailed":"出图失败","runFailTitle":"运行失败","runOkTitle":"运行完成","geneRequired":"请先填写基因名","strengthLabel":"强度","allStates":"全部状态","busyChip":"计算中：{v}","busyChipNoStep":"计算中","legend":"图例","pointsUnit":"个细胞","collapse":"收起","expand":"展开","expandParams":"展开参数","collapseParams":"收起参数","showMore":"显示更多","showLess":"收起","datasetWord":"数据集","stepWord":"步骤","timeWord":"时间","noData":"暂无数据","unknown":"未知","pending":"待运行","atTime":"时间","units":"个"}
  var DICT_EN = {"appTitle":"Virtual Cell Workbench","appSubtitle":"Single-cell → state space → virtual perturbation → target screen","nav":"Virtual Cell","engineReady":"Engine ready","engineBooting":"Engine starting","engineBad":"Engine error","engineBadgeHint":"Show engine details and optional backends","engineTitle":"Engine","enginePython":"Python","engineVersion":"Version","engineBackend":"Backend","engineWorkdir":"Work directory","engineAdapters":"Optional models / backends","engineAvailable":"available","engineMissing":"not installed","engineInstallHint":"Install hint","enginePackages":"Packages","engineRestart":"Restart engine","engineRestartConfirm":"Restarting interrupts any running computation. Continue?","engineRestarting":"Restarting…","engineNoAdapters":"No optional backend available; using the built-in numpy implementation","engineRestartDone":"Engine restarted","engineWorkdirHint":"Raw data, run records and exports live here","enginePopupClose":"Close","close":"Close","cancel":"Cancel","confirm":"OK","retry":"Retry","refresh":"Refresh","refreshHint":"Reload workbench state","refreshBusy":"Refreshing…","loading":"Loading…","loadingData":"Reading workbench state…","loadingHint":"Please wait","copy":"Copy","copied":"Copied","copyFailed":"Copy failed. Select the text and copy manually.","closeToast":"Dismiss","copyHint":"Paste it into any AI assistant","demo":"Load demo data","demoHint":"Built-in 2,000 cells × 1,200 genes, about 3 seconds","demoBusy":"Generating demo data…","demoDone":"Demo data loaded","importData":"Import data","importTitle":"Import single-cell data","importSubtitle":"h5ad, 10x, loom, csv/tsv, zip, GEO series matrix and URLs","importPath":"Path or download URL","importPathPlaceholder":"/Users/you/data.h5ad or https://…","importPathHint":"Use an absolute path for local files; http(s) URLs are downloaded first","importUpload":"Choose a local file","importUploadHint":"Large files are better imported by path; upload copies a raw archive","importFormat":"Data format","formatAuto":"Auto-detect (recommended)","formatH5ad":"h5ad (AnnData)","format10xH5":"10x h5","format10xMtx":"10x mtx triplet","formatLoom":"loom","formatMatrix":"csv/tsv/txt matrix","formatGeo":"GEO series matrix","formatArchive":"zip/tar archive","formatXlsx":"Excel workbook","formatHint":"Keep auto-detect unless it fails; then pick the format manually","importTranspose":"Transpose matrix","importTransposeHint":"Only needed when cells×genes was mis-detected","importAdvanced":"Advanced options","importMember":"File inside archive","importMemberHint":"Relative path when the archive has several candidates","importSheet":"Excel sheet name","importSheetHint":"Leave empty to use the first sheet","importGeneCol":"Gene name column","importGeneColHint":"Set when the first column holds gene names; empty auto-detects","importInspect":"Inspect first","inspectBusy":"Inspecting…","inspectOk":"Inspection done","inspectCells":"Cells","inspectGenes":"Genes","inspectFormat":"Detected format","inspectWarnings":"Inspection notes","importStart":"Import and load","importBusy":"Importing; large files can take minutes…","importNeedSource":"Enter a path or URL, or choose a file first","importSuccess":"Dataset imported","importNoPreview":"No preview returned","transposeOn":"Transposed","transposeOff":"Not transposed","recipeRun":"Run full pipeline","recipeHint":"Run the whole analysis chain in order","recipeQuick":"Quick tour","recipeQuickDesc":"QC → state → annotate → GRN → virtual perturbation → insight","recipeQuickTime":"~1–2 min","recipeStandard":"Standard analysis","recipeStandardDesc":"Quick flow + virtual screen + transition + audit","recipeStandardTime":"~3–5 min","recipeFull":"Full pipeline","recipeFullDesc":"Standard flow + response prediction + report export (needs a perturbation table)","recipeFullTime":"~5–10 min","recipeRunning":"Running pipeline","recipeCurrent":"Current step","recipeDone":"done","recipeFailed":"failed","recipeComplete":"Pipeline complete","recipePartial":"Pipeline partially complete","recipeGoInsight":"Open insight","recipeStart":"Start","recipeMenuHint":"Choose pipeline size","leftTitle":"Pipeline","leftHint":"Run top to bottom; a green check means done","datasetNone":"No dataset yet","datasetNoneHint":"Load demo data or import your own matrix","datasetSwitch":"Switch dataset","datasetSwitchHint":"Click the dataset card to switch or close","datasetCells":"cells","datasetGenes":"genes","datasetTfs":"TFs","datasetStepsDone":"{v} steps done","datasetCurrentStep":"Current: {v}","datasetWarnings":"{v} warnings","datasetTruth":"has simulated truth","datasetSize":"Size","datasetSource":"Source","datasetClose":"Close dataset","datasetNoOthers":"No other datasets","datasetActive":"active","datasetRawPath":"Raw data path","datasetUse":"Use","datasetOpen":"Open","datasetCreated":"Created","stepParams":"Parameters","stepAdvanced":"Advanced","stepAdvancedCount":"Advanced ({v})","stepRun":"Run","stepRerun":"Rerun","stepPlotOnly":"Plot only","stepRunning":"Running…","stepStatusDone":"done","stepStatusRunning":"running","stepStatusReady":"ready","stepStatusBlocked":"blocked","stepStatusFailed":"failed","stepNeedPrereq":"Run first: {v}","stepSummaryEmpty":"Not run yet","stepRecommended":"Rec. {v}","stepRecommendedEmpty":"No suggestion","stepFillRecommended":"Click to fill the recommended value","stepWhy":"Why","stepEffect":"Effect","stepRisk":"Risk","riskLow":"low risk","riskMedium":"medium risk","riskHigh":"high risk","stepCost":"Cost","stepOutputs":"Outputs","stepLastRun":"last {v}","stepPlotDone":"Figure created","stepError":"This step failed","stepNoParams":"This step has no parameters","stepReadyHint":"Prerequisites met, ready to run","stepsLoaded":"Step metadata loaded","noSteps":"Failed to load step metadata; using built-in steps","tabState":"States","tabNetwork":"Network","tabPerturb":"Perturb","tabScreen":"Screen","tabResponse":"Response","tabTransition":"Transition","tabInsight":"Insight","tabRecords":"Runs","tabNeedState":"Run \"State representation\" first","tabNeedGrn":"Run \"Regulatory network\" first","tabNeedPerturb":"Run a virtual perturbation first","tabNeedScreen":"Run a virtual screen first","tabNeedTransition":"Run \"State transition\" first","tabNeedInsight":"Generate the insight first","tabNeedResponse":"Attach a perturbation table or use the demo data","stateColorBy":"Color by","stateColorCluster":"Cell state (cluster)","stateColorCellState":"Annotated state","stateColorObs":"Metadata column","stateColorGene":"Gene expression","stateGenePlaceholder":"Gene name, e.g. KLF4","stateGeneApply":"Color","stateStatic":"Static figure","stateInteractive":"Interactive","stateOverlayPerturb":"Overlay perturbation arrows","stateLegend":"Legend (click to hide/show)","stateHidden":"hidden","stateHoverHint":"Wheel to zoom · drag to pan · double-click to reset","stateResetView":"Reset view","statePoints":"{v} cells shown","stateDownsample":" (downsampled)","stateNoData":"No state map yet","stateNoDataHint":"Run \"State representation\" to get a zoomable cell-state map","stateFetchFail":"Interactive data failed to load; showing the static figure","stateCells":"Cells","stateClusters":"States","stateSilhouette":"Silhouette","stateVariance":"Variance explained","statePerturbOverlay":"Arrows: source state → predicted target state","stateArrowDelta":"shift {v}","stateRunState":"Run state representation","stateShowStatic":"Show static figure","stateHideStatic":"Hide static figure","stateGeneValue":"expr","stateStaticHint":"Static figures are rendered by matplotlib on the host","stateLegendHiddenCount":"{v} hidden","networkTitle":"Gene regulatory network","networkCluster":"Filter by state","networkAllClusters":"All states","networkHubTitle":"Hub transcription factors (by strength)","networkHubEmpty":"No hub factors yet; run \"Regulatory network\"","networkEdgesTitle":"Regulatory edges","networkEdgeTf":"TF","networkEdgeTarget":"Target","networkEdgeScore":"Weight","networkEdgeSign":"Sign","networkEdgeCluster":"State","networkSignUp":"activate","networkSignDown":"repress","networkFilterHint":"Click a header to sort; up to 200 rows","networkStatic":"Network figure","networkValidateTitle":"GRN validation","networkValidateRun":"Run GRN validation","networkValidateNeedTruth":"Demo data carries simulated ground truth; real data needs a GRN prior","networkValidatePass":"Validation passed","networkValidateFail":"Validation failed","networkValidateHint":"Available for demo data or with an attached GRN prior","networkNoResult":"No network results yet","networkNoResultHint":"Run \"State representation\", then \"Regulatory network\"","networkRunGrn":"Run regulatory network","networkEdgeCount":"{v} edges","networkHubCount":"{v} hub factors","perturbTitle":"Virtual perturbation","perturbGene":"Target gene","perturbGenePlaceholder":"Gene name, e.g. KLF4","perturbGeneHint":"Start from a hub transcription factor","perturbHubChips":"Hub factors (click to fill)","perturbMode":"Perturbation","perturbModeKo":"Knockout","perturbModeKd":"Knockdown","perturbModeOe":"Overexpression","perturbModeHint":"Knockout is complete; knockdown keeps residual expression; overexpression tests the opposite direction","perturbStrength":"Strength","perturbStrengthHint":"1.0 means full knockout or maximum-expression overexpression","perturbCluster":"Target state","perturbAllClusters":"All states","perturbRun":"Run virtual perturbation","perturbAffected":"Affected genes","perturbMaxDelta":"Max |Δ|","perturbStateShift":"State shift","perturbTargetState":"Predicted target state","perturbSourceState":"Source state","perturbConfidence":"Confidence","perturbAffectedTitle":"Most affected genes","perturbGeneCol":"Gene","perturbDeltaCol":"Δ","perturbPerCluster":"Shift per state","perturbDeltaNorm":"Shift strength","perturbTargetCluster":"Target state","perturbCos":"Cosine","perturbValidity":"Method notes","perturbNoResult":"No perturbation result yet","perturbNoResultHint":"Run \"Regulatory network\", then perturb a hub factor","perturbNeedGrn":"Requires \"Regulatory network\"","perturbSummaryTitle":"Perturbation conclusion","perturbRunAgain":"Run again","perturbArrowNote":"Arrows overlaid on the state map: source → target","perturbTopDelta":"Top {v}","perturbNoAffected":"No gene is significantly affected; raise the strength or try another TF","screenTitle":"Virtual screen","screenCandidates":"Candidate genes","screenCandidatesEmpty":"Empty = built-in human TF pool","screenCandidatesHint":"Comma or space separated; empty uses the GRN TF pool","screenTopN":"Show top N","screenSpecificity":"Compute specificity","screenSpecificityHint":"Higher specificity means the effect concentrates on one state","screenMode":"Simulated perturbation","screenRun":"Start virtual screen","screenRankTitle":"Ranking","screenRank":"Rank","screenGene":"Gene","screenScore":"Score","screenShift":"State shift","screenSpecificityCol":"Specificity","screenTargetState":"Target state","screenWhy":"Why","screenPerturbThis":"Perturb this gene","screenNoResult":"No screen results yet","screenNoResultHint":"Run \"Regulatory network\", or start the screen now (uses the built-in TF pool)","screenTopChart":"Top {v} by score","screenProgress":"Simulating candidate genes…","screenCandidateCount":"{v} candidate genes","responseTitle":"Perturbation response","responseNeedData":"A Perturb-seq style perturbation table is required","responseNeedDataHint":"At least cell_id and perturbation columns (or h5ad obs[\"perturbation\"]). The demo data already carries one.","responseAttach":"Attach perturbation table","responsePath":"Perturbation table path","responseAttachBtn":"Attach","responseAttached":"Attached: {v}","responseUseDemo":"Use the demo perturbation table","responseRun":"Run response prediction","responsePredVsTrue":"Predicted vs true","responsePerPert":"Error per perturbation","responseR2":"R²","responseSpearman":"Spearman","responseNPert":"Perturbations","responseMethod":"Method","responseNoResult":"No response result yet","responseNoResultHint":"Attach a perturbation table and run; without one, drug/gene signatures are projected into the state space","responseNeedState":"Requires \"State representation\"","responseAttaching":"Attaching…","responseAttachDone":"Perturbation table attached","responsePerturbation":"Perturbation","responseNoAttachHint":"Gene sets, GRN priors and drug signatures can also be attached","transitionTitle":"State transition","transitionRun":"Compute state transition","transitionMatrix":"Transition matrix","transitionSources":"Source states (outflow)","transitionSinks":"Sink states (inflow)","transitionNetFlow":"Net flow","transitionFrom":"From","transitionTo":"To","transitionFlow":"Net flow","transitionStability":"Stability","transitionNStates":"States","transitionNoResult":"No transition result yet","transitionNoResultHint":"Run \"State representation\" first","transitionNeedState":"Requires \"State representation\"","transitionHeatHint":"Rows = source states, columns = target states; normalized transition probability","insightTitle":"Insight","insightGenerate":"Generate insight","insightAudit":"Run audit","insightExportReport":"Export report","insightExportMethods":"Export Methods","insightHeadline":"Conclusion","insightRankedTargets":"Ranked targets","insightNextActions":"Next actions","insightCaveats":"Method caveats","insightNoResult":"No insight yet","insightNoResultHint":"Generate a Chinese summary that only cites real numbers from this run","insightNeedState":"Requires \"State representation\"","insightAuditScore":"Audit score","insightAuditChecks":"Checks","insightAuditPass":"pass","insightAuditWarn":"warn","insightAuditFail":"fail","insightAuditHint":"The audit is a deterministic rule check, not a scientific verdict","insightDisclaimer":"Conclusions cite only this run’s real numbers; low confidence is reported as a caveat","insightRegenerate":"Regenerate","insightRankedGene":"Gene","insightRankedScore":"Score","insightRankedWhy":"Why","insightNextStep":"Do it","insightNoChecks":"The audit returned no checks","insightAuditRunning":"Auditing…","insightRunning":"Generating…","insightExportRunning":"Exporting…","insightExportDone":"Exported: {v}","recordsTitle":"Run records","recordsHint":"Every run keeps parameters, figures, tables and artifact paths; exported as zip","recordsId":"Run ID","recordsStep":"Step","recordsTime":"Time","recordsDuration":"Duration","recordsFigures":"Figures","recordsTables":"Tables","recordsStatus":"Status","recordsDetail":"Detail","recordsParams":"Parameters","recordsArtifacts":"Artifacts","recordsFiles":"Files","recordsExportZip":"Export run_zip","recordsRerun":"Rerun this step","recordsCopyRepro":"Copy reproduction instruction","recordsNoRuns":"No run records yet","recordsNoRunsHint":"Run a step and its parameters and artifacts appear here","recordsEmpty":"none","recordsParamsEmpty":"This run record has no parameter detail; check the exported zip","recordsArtifactsEmpty":"No artifact list returned; check the export directory","recordsExportDone":"Exported: {v}","recordsExportRunning":"Packaging…","recordsRefresh":"Refresh records","recordsStatusOk":"ok","recordsStatusBad":"failed","recordsDetailTitle":"Run detail","recordsNoFiles":"No files","recordsOpenFolder":"Export directory","recordsLoadFail":"Failed to load run records","agentTitle":"Agent collaboration","agentHint":"Turn state and conclusions into copy-ready AI instructions","agentConclusions":"Current conclusion","agentNoConclusions":"No conclusion yet; run the first step","agentConclusionsFrom":"From: {v}","agentNext":"Next for the AI","agentNextHint":"Press copy and paste into any AI assistant","agentCopy":"Copy","agentCopyAll":"Copy full context","agentContext":"Status snapshot","agentContextDataset":"Dataset","agentContextSteps":"Steps done","agentContextLastRun":"Last run","agentContextExport":"Export dir","agentContextPending":"Pending","agentNoDataset":"No dataset yet","agentLog":"Activity log","agentLogEmpty":"No log yet; every action is recorded here","agentLogClear":"Clear","agentReadResult":"Ask the Agent","agentReadResultHint":"Copy an interpretation request carrying this run’s real numbers","agentCopied":"Instruction copied","agentContextCopied":"Context copied","agentWorkdir":"Work dir","agentEngine":"Engine","agentNeedDataset":"Load data first to build copyable context","agentRunHint":"Paste it into any AI assistant","onboardTitle":"What can a virtual cell do?","onboardIntro":"Turn single-cell expression into a manipulable cell-state space: see which states exist and who regulates them, simulate what happens when a gene is knocked out, and rank targets worth validating first.","onboardStep1":"Load demo data","onboardStep1Desc":"Built-in 2,000 cells × 1,200 genes with real markers and simulated ground truth; loads in about 3 seconds.","onboardDemo":"Load demo data","onboardStep2":"Run the Quick pipeline","onboardStep2Desc":"QC → state → annotate → GRN → virtual perturbation → insight, automatically.","onboardQuick":"Run Quick","onboardStep3":"Look at perturbation results","onboardStep3Desc":"Switch genes and strength in the Perturb tab to see predicted state shifts; the Agent panel builds copy-ready AI instructions.","onboardOpenPerturb":"Open Perturb tab","onboardImport":"I have my own data","onboardTip":"Everything runs locally; raw data is kept read-only and every run is recorded.","onboardBusy":"Preparing…","errTitle":"Something failed","errRetry":"retryable","errPrerequisite":"Run the previous step first","errPrerequisiteStep":"Run the previous step first: {v}","errBusy":"The engine is still computing; try again shortly","errNoPython":"No usable Python interpreter; set the python path in plugin settings","errEngineExited":"The engine process exited; press Refresh or restart the plugin","errTimeout":"The computation timed out; use less data or retry later","errBadRequest":"Invalid parameters; check numeric ranges and gene names","errUnsupported":"The current backend does not support this; change parameters or install optional deps","errAmbiguous":"The data format is ambiguous; choose the format manually when importing","errNotFound":"Dataset or file not found; verify the path still exists","errAuth":"Local access only: open this inside the DeepSeek Harness desktop client","errNetwork":"Cannot reach the workbench service; make sure the plugin is enabled and refresh","errInternal":"Engine internal error; retry once, then export run_zip from Runs and report it","errDisposed":"The workbench was closed; refresh the page to reconnect","errUnknown":"Unknown error","errNeedDataset":"Load a dataset first","errNoData":"No data","crashTitle":"This block crashed; the rest still works","crashHint":"Press Retry; if it keeps happening, refresh the page","figureFail":"Image failed to load","noFigure":"No figure","noSelection":"none","yes":"yes","no":"no","warnings":"Warnings","notes":"Notes","metrics":"Metrics","tables":"tables","figures":"figures","moreMetrics":"More metrics","rawMetrics":"Raw metrics","metricAffected":"Affected genes","metricMaxDelta":"Max |Δ|","metricStateShift":"State shift","metricTargetState":"Target state","metricSourceState":"Source state","metricConfidence":"Confidence","metricBackend":"Backend","metricCells":"Cells","metricGenes":"Genes","metricClusters":"States","metricStates":"States","metricSilhouette":"Silhouette","metricVariance":"Variance explained","metricNtf":"TFs","metricEdges":"Edges","metricDensity":"Density","metricCvR2":"CV R²","metricAuroc":"AUROC","metricAuprc":"AUPRC","metricSpearman":"Spearman","metricPearson":"Pearson","metricR2":"R²","metricNPert":"Perturbations","metricStability":"Stability","metricMethod":"Method","metricTargets":"Targets","metricHvg":"HVGs","metricPcs":"PCs","metricScore":"Score","metricOther":"Metric","exportTitle":"Export","exportBtn":"Export","exportRunning":"Exporting…","exportDone":"Exported","exportFailed":"Export failed","exportCsv":"CSV","exportJson":"JSON","exportMarkdown":"Markdown","exportPython":"Python","exportZip":"ZIP","plotLoading":"Rendering…","plotFailed":"Plot failed","runFailTitle":"Run failed","runOkTitle":"Run complete","geneRequired":"Enter a gene name","strengthLabel":"strength","allStates":"All states","busyChip":"Computing: {v}","busyChipNoStep":"Computing","legend":"Legend","pointsUnit":"cells","collapse":"Collapse","expand":"Expand","expandParams":"Show parameters","collapseParams":"Hide parameters","showMore":"Show more","showLess":"Show less","datasetWord":"dataset","stepWord":"step","timeWord":"time","noData":"No data","unknown":"unknown","pending":"pending","atTime":"time","units":""}

  // ==========================================================================
  // 纯函数测试出口（node 无 window 时挂 globalThis.__VC_TEST__；浏览器里也挂在 __test__）
  // ==========================================================================

  var VC_TEST = {
    NS: NS, API: API, POLL_MS: POLL_MS, PALETTE: PALETTE, CONTINUOUS_STOPS: CONTINUOUS_STOPS,
    TAB_KEYS: TAB_KEYS, STEP_ORDER: STEP_ORDER, TAB_META: TAB_META, RECIPES: RECIPES,
    DICT_ZH: DICT_ZH, DICT_EN: DICT_EN,
    isNil: isNil, clamp: clamp, asNumber: asNumber, plainText: plainText,
    fmtNumber: fmtNumber, fmtInt: fmtInt, fmtPct: fmtPct, fmtSigned: fmtSigned,
    formatDuration: formatDuration, formatClock: formatClock, formatBytes: formatBytes,
    hexToRgb: hexToRgb, rgbToHex: rgbToHex, mixHex: mixHex, withAlpha: withAlpha,
    colorForIndex: colorForIndex, colorForName: colorForName, colorForValue: colorForValue,
    hashString: hashString,
    buildViewport: buildViewport, projectPoint: projectPoint, unprojectPoint: unprojectPoint,
    zoomViewport: zoomViewport, panViewport: panViewport,
    normalizeState: normalizeState, normalizeDataset: normalizeDataset, normalizeEngine: normalizeEngine,
    normalizeSteps: normalizeSteps, normalizeSuggestion: normalizeSuggestion, normalizeFigure: normalizeFigure,
    normalizeTable: normalizeTable, normalizeRun: normalizeRun, normalizeRuns: normalizeRuns,
    figureSrc: figureSrc, normalizeFigureList: normalizeFigureList,
    normalizeAudit: normalizeAudit, normalizeInsight: normalizeInsight, normalizePreview: normalizePreview,
    extractEmbedding: extractEmbedding, normalizePerturbResult: normalizePerturbResult,
    cellStateMapOf: cellStateMapOf, stateTabPoints: stateTabPoints, buildClusterLayout: buildClusterLayout,
    doneStepSet: doneStepSet, isStepReady: isStepReady, stepStatusOf: stepStatusOf,
    defaultParams: defaultParams, collectStepParams: collectStepParams, formatParamValue: formatParamValue,
    parseTokenList: parseTokenList, coerceParamValue: coerceParamValue,
    errorAdvice: errorAdvice, describeError: describeError, createApi: createApi,
    createTranslator: createTranslator, makeLogEntry: makeLogEntry, appendLog: appendLog,
    stepTitle: stepTitle, resultOf: resultOf, busyText: busyText, hubTfsOf: hubTfsOf,
    mergedResultOf: mergedResultOf, metricLabel: metricLabel, metricEntryList: metricEntryList,
    buildStepInstruction: buildStepInstruction, buildAgentInstructions: buildAgentInstructions,
    buildAgentContext: buildAgentContext, normalizeEdges: normalizeEdges, networkHubWeights: networkHubWeights,
  }

  // ==========================================================================
  // 顶层守卫：node 里只暴露纯函数给单测；浏览器里注册 Client 模块。
  // ==========================================================================

  if (typeof window === 'undefined') {
    globalThis.__VC_TEST__ = VC_TEST
  } else if (window.__ModuleLoader__ && typeof window.__ModuleLoader__.load === 'function') {
    window.__ModuleLoader__.load({ id: NS, factory: factory })
  } else {
    try { console.warn('[dsh-virtualcell] window.__ModuleLoader__ 不可用，Client 模块未注册') } catch (ignored) { /* ignore */ }
  }
})()
