# 更新日志 (Changelog)

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [v1.1.0] - 2026-08-24

### 新增 · 配置项

- **专家生成温度** (`expert_temperature`, 默认 1.0)：统一控制所有专家的回答发散度，-1 表示不注入该参数
- **失败自动重试** (`retry_on_failure`, 默认 1 次)：专家调用出错自动重试（上限 3 次），超时不重试以免等待翻倍
- **会话上下文注入** (`include_context_count`, 默认关闭)：咨询专家时自动附带最近 N 条对话记录，专家不再"失忆"，问题无需自包含
- **统计持久化** (`persist_stats`, 默认开启)：各专家累计咨询次数保存到 `data/expert_cluster_stats.json`，重启插件不丢失
- **会议默认名单** (`panel_default_experts`)：`/panel` 未点名时的默认参会专家（支持 name 与 tag），留空为全体
- **参会人数上限** (`max_panel_size`, 默认 8)：防止单场会议 token 失控
- **汇总超时倍率** (`panel_timeout_multiplier`, 默认 2.0)：主持人汇总的超时时间可调
- **独立汇总模型** (`summary_provider_id` / `summary_model`)：主持人可使用与专家不同的对话模型

### 新增 · 专家字段

- **分组标签** (`tags`)：逗号分隔的标签组（如 `dev,quality`），`convene_expert_panel` 参会名单与 `/panel` 默认名单均支持按标签整组召集

### 新增 · LLM 函数工具

- **search_experts**：按关键词模糊搜索专家（匹配名称、擅长领域、模型、标签），专家团队较大时帮助主 Agent 快速定位人选
- **get_expert_usage**：查询配额与用量统计（本次对话剩余额度、各专家累计咨询次数），便于主 Agent 在召开大型会议前评估余量

### 改进

- `/experts` 指令与 `list_experts` 工具的输出新增分组标签展示
- 统计口径升级：`/experts` 展示的累计咨询次数在开启 `persist_stats` 后跨重启累计

## [v1.0.0] - 2026-08-24

### 首个版本

- 专家团队成员 WebUI 可视化配置（预置 coder / reviewer / writer / analyst 四模板）
- LLM 函数工具：`list_experts` / `consult_expert` / `convene_expert_panel`
- 聊天指令：`/experts`、`/panel <问题>`
- 三种会议汇总风格：balanced / critical / concise，纪要可选渲染为图片
- 安全机制：全局熔断、单专家限额、超时控制、问题长度限制、信号量并发限流、无递归设计
