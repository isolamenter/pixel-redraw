/* Pyodide 宿主（module Worker）。
 *
 * 为什么必须是 Worker：像素化是同步的 CPU 密集计算（Pillow 解码、量化、
 * nearest_palette），跑在主线程会把页面连同进度条一起冻住 —— 而进度正是这段代码
 * 存在的理由。放到 Worker 里，主线程只负责画。
 *
 * 为什么必须独立成文件：Worker 只能从 URL 构造，不能内联（除非用 Blob URL，
 * 那又会丢掉 module 语义与相对路径）。
 *
 * 这个文件不认识模型、不认识 key、也不认识业务规则：它只做三件事 ——
 * 加载运行时、把 Python 源码写进 WASM 文件系统、把主线程传来的请求转交给
 * pixel_pipeline 并把结果原样送回去。
 */

var pyodide = null;
var bridge = null;          // Python 侧的 run_pipeline，绑定一次
var numpyAvailable = false;

async function boot(indexURL, pythonFiles) {
  /* 每一步都回报给主线程：冷启动要拉约 7.5MB，一个不动的「加载中」会让用户
     以为页面挂了，也让「卡在哪一步」变成一个只能靠猜的问题。 */
  const step = (name) => self.postMessage({ type: 'boot-progress', step: name });

  step('interpreter');
  /* 跨源 module import 需要 CORS：jsDelivr 返回 access-control-allow-origin: *。
     把这行换成本地路径即可完全自托管（见 README「离线部署」）。 */
  const module = await import(indexURL + 'pyodide.mjs');
  pyodide = await module.loadPyodide({ indexURL: indexURL });

  step('pillow');
  await pyodide.loadPackage(['pillow']);

  /* numpy 不是可选加速项。纯 Python 的 nearest_palette 在 64 色下约 33µs/像素，
     一次两轮运行要做三次约 400 万像素的映射 —— 没有 numpy 就是几十秒到几分钟的
     卡死。所以它随 Pillow 一起加载；万一加载失败，这里如实上报，由页面明确警告，
     而不是让用户在一次「卡住」里自己猜。 */
  step('numpy');
  try {
    await pyodide.loadPackage(['numpy']);
    numpyAvailable = true;
  } catch (error) {
    numpyAvailable = false;
  }

  step('python');
  for (const [name, text] of Object.entries(pythonFiles)) {
    pyodide.FS.writeFile('/' + name, text);
  }
  pyodide.runPython('import sys\nif "/" not in sys.path: sys.path.insert(0, "/")');

  /* 唯一的桥：JS 调它、它 await 内部的协程、把结果当字符串还回来。
     返回 JSON 字符串而不是对象，是因为 str 是原生类型、跨边界即隐式转换，
     不会留下需要 destroy() 的 PyProxy。 */
  pyodide.runPython([
    'import pixel_pipeline',
    'async def __pr_run(source_b64, request_json, on_progress):',
    '    return await pixel_pipeline.run_pipeline(source_b64, request_json, on_progress)',
  ].join('\n'));
  bridge = pyodide.globals.get('__pr_run');

  return JSON.parse(pyodide.runPython('import pixel_pipeline; pixel_pipeline.web_meta()'));
}

self.onmessage = async function (event) {
  const message = event.data || {};

  if (message.type === 'boot') {
    try {
      const meta = await boot(message.indexURL, message.pythonFiles);
      self.postMessage({ type: 'ready', meta: meta, numpy: numpyAvailable });
    } catch (error) {
      /* 运行时起不来，整个应用就没有意义了 —— 必须说清楚是哪一步失败，
         而不是让页面停在一个永远不动的「加载中」。 */
      self.postMessage({
        type: 'boot-failed',
        message: String((error && error.message) || error),
        detail: String((error && error.stack) || ''),
        indexURL: message.indexURL,
        numpy: numpyAvailable,
      });
    }
    return;
  }

  if (message.type === 'run') {
    if (!bridge) {
      self.postMessage({ type: 'done', id: message.id, envelope: {
        ok: false,
        error: { kind: 'internal', where: 'worker', message: '运行时尚未就绪。' },
      } });
      return;
    }
    const post = (phase, label, detail) => {
      self.postMessage({ type: 'progress', id: message.id, phase: phase, label: label, detail: detail });
    };
    try {
      const envelopeJson = await bridge(message.sourceB64, message.requestJson, post);
      self.postMessage({ type: 'done', id: message.id, envelope: JSON.parse(envelopeJson) });
    } catch (error) {
      /* 走到这里说明失败发生在 Python 边界之外（例如代理被提前回收）。
         Python 内部的一切失败都会以 ok:false 的信封正常返回。 */
      self.postMessage({ type: 'done', id: message.id, envelope: {
        ok: false,
        error: {
          kind: 'frontend',
          where: 'worker 调用 pixel_pipeline',
          message: String((error && error.message) || error),
          detail: String((error && error.stack) || ''),
        },
      } });
    }
  }
};
