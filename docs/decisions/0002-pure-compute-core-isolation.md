# 2. 算法核心纯计算隔离与传输解耦

- **状态**: Accepted
- **日期**: 2026-09-20 (从架构与测试规范确认)
- **决定者**: 核心架构设计

## 背景与问题

算法逻辑需要在两个完全不同的环境中运行：
1. **本机开发与自动化测试**：使用标准 CPython 3.9+ 运行单元测试（无网络依赖、执行迅速）。
2. **浏览器生产环境**：使用 Pyodide 3.14+ (WASM) 运行在 Web Worker 内。

如果算法核心直接引入网络请求库（如 `requests`、`urllib` 或 `pyodide.http`）、文件系统 I/O 或操作系统环境变量，将导致代码无法在标准 CPython 和 Pyodide 之间无缝复用，并严重增加单元测试复杂度。

## 决策

1. 将 `pixel_redraw.py`、`pixel_reduce.py`、`pixel_color.py` 和 `pixel_palettes.py` 严格约束为**纯计算模块 (Pure Compute)**：
   - 严禁 `import os`, `sys`, `pathlib`, `urllib`, `socket` 等带 I/O 或环境依赖的模块。
   - 不读取环境变量，不读写磁盘文件，不直接持有网络套接字。
   - 上游模型网络调用通过可注入的异步回调函数（`call_upstream`）提供。
2. 独立划分 `pixel_pipeline.py` 作为**浏览器专属边界模块**：
   - 它是唯一允许引用 `pyodide` 的 Python 模块。
   - 负责通过 `pyodide.http.pyfetch` 执行网络请求，并处理 Python 与 JavaScript 之间的 JSON 字符串编解码与脱敏信封包装。
3. 在 `tests/test_core.py` 中增加**源码级纯度测试 (CorePurityTest)**，通过静态 AST/源码扫描自动化断言算法核心没有违规引入 I/O 模块。

## 结果与影响

### 正面影响
- **两端 100% 行为一致**：本地 CPython 测试通过即代表 Pyodide 逻辑正确，无需在 WASM 环境中调试纯逻辑问题。
- **高测试覆盖与零成本验证**：单元测试只需注入假的上游响应（Mock Upstream），毫秒级完成 40+ 项测试，不需要网络连接或 API 花费。
- **防止架构腐化**：自动化纯度测试严格阻断了在核心算法中随意加入副作用操作的可能。

### 负面影响 / 权衡
- 边界模块（`pixel_pipeline.py`）需要手动处理异步回调与 JSON 序列化转换。
