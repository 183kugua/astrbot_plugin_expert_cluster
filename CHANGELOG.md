# 更新日志 (Changelog)

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [v1.3.5] - 2026-08-25

### 新增

- **预设专家模板 ×4**：新增「写作专家」「学习辅导专家」「数据分析专家」「法律顾问」四套预置模板（含系统提示词、标签与 Provider 下拉选择），预置模板总数达 7 套

### 修复

- **修复 LLM 工具参数丢失**：`search_experts` / `consult_expert` / `convene_expert_panel` 的 Google 风格 docstring 存在两处格式问题——参数类型缺空格（`name(string):` → `name (string):`）以及参数描述续行缩进不足（与参数名平齐被解析器误判为新条目），导致 docstring 解析失败、注册到模型端的工具 schema 参数为空，模型无法正确传参；已按标准格式修正并补全 `search_experts` 缺失的 `keyword` 参数声明

## [v1.3.1] - 2026-08-25

### 变更

- **配置样式优化**：专家模板与汇总配置中的对话模型 Provider 由手动填写 ID 改为 WebUI 下拉框选择（官方 `_special: select_provider`，列出已配置的提供商），共 5 处；留空仍表示使用当前会话的对话模型

## [v1.3.0] - 2026-08-24

### 修复 · 深度排查专项

- **参会名单解析重构** (`_parse_panel_selector`)：统一解析入口，支持专家 name 与分组 tag 混合填写，结果自动去重；`convene_expert_panel` 显式名单此前不识别分组 tag，现已生效，未匹配项由调用方决定报错或跳过
- **`/panel` 命令同款对齐**：命令侧复用同一解析器，未匹配项记录警告日志后静默跳过
- **会话上下文去重**：llm_tool 触发时 history 往往已包含当前提问，现过滤与本次 question 重复的用户消息，避免同一问题注入两遍浪费 token
- **统计保存时机补齐**：单专家咨询、多专家会诊及成员管理操作完成后即时落盘累计次数，不再依赖卸载钩子，降低统计丢失风险

### 变更

- **数据路径标准化**：统计文件迁移至 AstrBot 标准插件数据目录（`StarTools.get_data_dir`），目录不存在时自动创建；环境异常时回退旧相对路径并兼容迁移历史数据
- **专家工具集缓存**：首次构建白名单工具集后复用，避免每次咨询重复扫描全局工具注册表
- 内部版本号统一为 `1.3.0`（`metadata.yaml` 与 `@register` 装饰器对齐）

## [v1.2.1] - 2026-08-24

### 变更 · 市场上架准备

- `metadata.yaml` 版本号去掉 `v` 前缀（`1.2.1`），对齐 AstrBot 插件市场 JSON 规范
- 全量通过 ruff check / ruff format：折行修复 10 处超长 docstring，统一代码格式；无功能改动

## [v1.2.0] - 2026-08-24

### 新增 · 专家函数工具

- **专家可携带工具作答** (`expert_tools_enabled`, 默认开启)：专家咨询升级为 `tool_loop_agent` 完整工具循环，可在回答前调用函数工具查证信息；工具集为空或获取失败时自动回退原纯文本模式
- **专家工具白名单** (`expert_tool_names`)：逗号分隔的专家可用全局工具名，默认为本插件三个只读信息工具（`list_experts` / `search_experts` / `get_expert_usage`）
- **循环步数上限** (`expert_max_tool_steps`, 默认 5)：限制每位专家单次咨询的工具调用轮数（1-20），防止 token 失控
- **防递归硬保护**：`consult_expert` 与 `convene_expert_panel` 强制排除在专家工具名单之外，即使误配也会自动剔除并记录警告；已在 WebUI 停用的工具同样不会下发

### 变更

- 工具模式下自动在专家系统提示词后附加简短的工具使用说明
- 兼容性说明：框架的 agent 工具循环暂不支持透传专家的独立模型名与温度覆盖（`model`/`temperature` 仅在纯文本模式下生效）

### 文档

- README 全面扩写：新增安装步骤、快速上手、工作原理流程图、预置模板专家表、LLM 工具参数详解、专家工具扩展玩法与注意事项、panel_style 风格对照、FAQ 常见问题与使用示例扩充

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
