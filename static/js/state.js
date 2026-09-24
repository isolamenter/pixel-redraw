/* 页面唯一的可变状态。其他模块只改它的字段，不重新赋值 S 本身。
 *
 * 服务端消失后这个对象瘦了一圈：run_id、SSE 连接、重连计数、服务端 elapsed 回填、
 * 轮询定时器、去重表 —— 那些都是「任务活在服务端」的产物。现在一次运行就是一次
 * 函数调用，进度由 Worker 直接推过来，所以只留下真正属于页面的东西。 */

export var S = {
  meta: null,              // web_meta() 的载荷；控件与上限都由它驱动
  metaFailed: false,
  numpyOk: null,           // null = 未知；false = numpy 没装（固定调色板会非常慢）

  worker: null,
  bootState: "loading",    // loading | ready | failed
  bootStep: null,          // 冷启动走到哪一步了，用于把 7.5MB 的等待说清楚
  bootError: null,

  uploadBlob: null,        // 保留原比例的规范化 PNG
  uploadInfo: null,        // {ow, oh, w, h, srcBytes, encBytes}
  uploadName: "",
  size: 32,
  paletteMode: "auto",     // preset | auto | custom
  preset: null,
  maxColors: 16,
  maxColorsMode: "all",    // "all" | "custom"
  maxColorsLimit: 8,
  pixelizeOnly: false,

  running: false,
  runSeq: 0,               // 当前运行的编号；旧运行的回包据此丢弃
  runStartedAt: 0,
  elapsedMs: 0,
  phasesSeen: {},          // phase -> {elapsed_ms}
  currentPhase: null,
  failedPhase: null,

  lastResultRawB64: null,  // 模型原始输出，供「改尺寸/换调色板」零成本重渲染
  lastResultDraftB64: null, // 首轮量化中间草稿，重渲染时保留展示
  lastResultSource: null,  // 'generate' | 'repixelize'
  result: null,

  blobs: { pixel: null, preview: null },
  objectUrls: [],          // 结果图的 blob: URL，换新结果时统一 revoke
  uploadThumbUrl: null,
  recoveredReason: null,

  copyTarget: "preview",
  zoom: 8,
  oneToOne: false,
  errorCount: 0,

  uiTimer: null,
  repixelTimer: null
};
