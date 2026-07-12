# 审查记录

## 本地验证

- `python -m pytest -q`：56 passed
- `python -m py_compile config.py summarizer.py web/routes.py`：通过
- `git diff --check`：通过
- 新增测试覆盖调用顺序、同一上游的多模型耗尽后才切换下一上游、异常与空响应切换、旧设置回退、日志不含 API key、设置持久化、模型列表排序和掩码密钥保留。

## 人工审查

- 未发现 Critical 问题。
- 同一上游的模型列表按填写顺序尝试，且仅在其全部模型异常或返回空内容后才进入下一上游。
- 旧的单上游环境变量、SQLite 设置和 JSON 的 `model` 字段仍可作为单元素模型列表使用；程序化提交不含上游字段的既有设置请求不会清空配置。
- 上游调用只记录序号、模型与异常类型，且设置页/调试 curl 均使用掩码 Key。

## 外部审查状态

Antigravity 复审：无 Critical 或 Warning，确认同一上游内模型耗尽后才进入下一上游、旧单模型兼容、掩码密钥、提前校验和测试覆盖，`VERDICT: APPROVE`。

Claude 复审：无 Critical；指出过上游验证失败可能造成其它设置部分保存，现已通过在所有写入前执行 `_serialize_llm_providers` 修复，并有回归测试。其余建议均为不阻塞的信息项，`VERDICT: APPROVE`。

## 浏览器验证状态

本地 Mock 服务可启动；Playwright 自动化脚本因当前 `.venv` 缺少 `playwright` 包而未执行。页面渲染与保存路径由 FastAPI 测试覆盖。
