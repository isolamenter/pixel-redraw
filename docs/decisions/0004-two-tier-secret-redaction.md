# 4. 双层密钥脱敏与客户端零持久化设计

- **状态**: Accepted
- **日期**: 2026-09-20 (从凭据架构与 `tests/test_core.py` 确认)
- **决定者**: 安全与运行时设计

## 背景与问题

由于应用采用纯前端架构，用户的 Google Gemini API Key 由用户在浏览器输入并在前端内存中驻留。这带来了泄露风险：
1. **报错与异常泄露**：网络请求失败或上游返回错误详情时，错误消息、请求头或 traceback 可能包含明文 API Key。
2. **UI 渲染与截图泄露**：页面展示状态详情、历史信封或提供「复制/下载诊断 JSON」时，若未严格过滤，会将 Key 暴露在界面或导出文件中。
3. **共享设备安全**：若默认将 Key 持久化到 `localStorage`，在公用机器上会导致凭据遗留。

## 决策

1. **临时存储优先**：
   - 默认将 API Key 保存在 `sessionStorage` 中，标签页关闭即销毁。
   - 仅在用户显式勾选「在此设备记住」时才存入 `localStorage`。
   - 提供「清除」按钮，可一键清空所有存储介质中的凭据。
2. **双层强制脱敏机制 (Two-Tier Secret Redaction)**：
   - **第一层 (Python 边界层)**：在 `pixel_pipeline.py` 的 `to_envelope()` 中，对所有返回给 JavaScript 的请求包体、响应包体、错误描述和 traceback 进行递归扫描与 Key 掩码替换。
   - **第二层 (JavaScript 渲染层)**：在 `static/js/settings.js` 的 `redactSecrets()` 中，所有将被挂载到 DOM、插入错误徽章或提供下载的字符串再次进行二次脱敏过滤。
3. **安全测试断言保障**：
   - 在 `tests/test_core.py` 中编写 `EnvelopeTest.test_redaction_removes_the_key_in_both_forms`，断言无论正常或异常信封中均不得出现原始 Key 字符串。

## 结果与影响

### 正面影响
- 杜绝因复制日志、调试报错、截图分享或下载调试 JSON 文件而导致的凭据泄露。
- 即使某一层脱敏规则有疏漏，另一层仍能起到兜底防御作用（纵深防御）。

### 负面影响 / 权衡
- 所有的错误展示和日志导出逻辑在修改时都必须遵守并调用脱敏处理链路。
