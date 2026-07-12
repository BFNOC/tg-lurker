# 实施计划

## 目标

允许用户在设置页维护按优先级排序的多个完整 LLM 上游；每个上游有一个有序模型列表。一次摘要请求会先依次尝试当前上游的全部模型，在异常或空结果后才进入下一个上游。旧环境变量和旧数据库设置仍是默认首项。

## 共享约定

- 数据库使用单个 `llm_providers` JSON 设置项，元素为 `id`、`base_url`、`api_key`、`models`、`api_format`；旧 `model` 单值自动视为仅含一个元素的 `models`。
- 旧 `llm_*` 设置不存在 `llm_providers` 时继续生效；无效 JSON 或无有效上游同样回退至旧设置。
- 页面返回上游清单时，API key 只返回掩码；提交掩码值时按稳定 `id` 保留原密钥。
- 仅接受 `chat` 与 `responses` 两种 API 格式；保存时至少保留一个完整上游。
- 每次上游调用都独立关闭 HTTP/OpenAI 客户端。失败日志只含序号、模型和异常类型/摘要，不含 API key。

## Layer 1

1. `config.py`、`summarizer.py`、`tests/test_llm_failover.py`
   - 定义和加载上游配置，保留旧配置兼容路径。
   - 在每个上游内按模型列表调用；异常或空输出时继续尝试，并在全部失败后抛出汇总异常。
   - 覆盖顺序、失败切换、空输出切换、旧设置回退和不泄露密钥的日志行为。

2. `web/routes.py`、`web/templates/settings.html`、`README.md`、`tests/test_llm_provider_settings.py`
   - 用可新增、删除和上下移动的上游卡片替换单配置表单；每张卡用每行一个模型的列表维护模型优先级，所有密码字段保持掩码。
   - 保存、校验、脱敏展示及调试 curl 使用第一可用上游。
   - 记录用户配置方法和切换规则，并覆盖设置页持久化、排序和密钥保留。

## Layer 2

1. 汇合后运行完整 pytest、编译检查和人工 diff 审核。
2. 运行 antigravity 与 Claude 对完整 diff 的并行审查；修复 Critical 问题后复审。
3. 写入 `review.md`，归档 CCG task；仅提交本任务变更，绝不包含用户已有的 `.gitignore` 和 `AGENTS.md` 改动。
