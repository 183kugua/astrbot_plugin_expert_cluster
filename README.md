# 专家集群 astrbot_plugin_expert_cluster

为主 Agent 配备一支**可随时召集的专家团队**。每位专家拥有独立的系统提示词，可指定独立的模型提供商（Provider）与模型；主 Agent 在对话中可以自主判断，向单个专家咨询，或召开多专家并行会诊后汇总结论。

## 环境要求

- AstrBot **>= v4.10.4**（`llm_generate` 需 v4.5.7，`template_list` 配置需 v4.10.4）
- 已在 WebUI 配置至少一个对话模型（Provider）

## 与 subagent_worktogether 的区别

本项目灵感来源于 [astrbot_plugin_subagent_worktogether](https://github.com/ScarletAugus/astrbot_plugin_subagent_worktogether)（AGPL-3.0），借鉴了其"多 Agent 协作 + 结构化错误前缀 + 熔断防护"的设计思路，但实现方式不同，**未复制其代码**：

| | subagent_worktogether | 本插件 |
| --- | --- | --- |
| 专家来源 | AstrBot WebUI 中配置的 Handoff 子代理 | 插件配置中的 template_list 专家条目 |
| 执行方式 | `tool_loop_agent` 完整工具循环 | 默认带只读工具的完整工具循环（可关，回退 `llm_generate` 纯文本） |
| 递归风险 | 存在，需五层防护 | 委派类工具被硬性排除出专家工具名单，防护聚焦熔断/超时/限流 |
| 上手成本 | 需先配置子代理 | 开箱即用，WebUI 可视化添加专家 |
| 会诊能力 | 逐个串行委派 | `convene_expert_panel` 并行会诊 |

## LLM 工具

| 工具名 | 说明 |
| --- | --- |
| `list_experts` | 列出所有专家的名称、擅长领域与本次对话的配额使用情况 |
| `consult_expert` | 向指定专家提出一个自包含问题，返回专家的专业回答 |
| `convene_expert_panel` | 召开专家会议：并行咨询多位（默认全体）专家，返回各方独立意见，由主 Agent 汇总最终答案 |
| `search_experts` | 按关键词搜索专家（匹配名称/擅长领域/标签），专家较多时快速定位合适人选 |
| `get_expert_usage` | 查询配额与用量统计：本次对话剩余额度、各专家累计咨询次数 |

所有系统级失败均以 `[EXPERT_ERROR]` 前缀返回，工具描述中已引导 LLM 将其与专家正常回答区分，触发自我修复逻辑（换专家 / 自行作答）。

## 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/experts`（别名：专家列表、专家团队） | 列出专家团队及各专家累计咨询次数 |
| `/panel <问题>`（别名：专家会议、会诊） | 直接召开专家会议：并行咨询全体专家 → 主持人汇总 → 输出会议纪要 |

## 配置项

在 WebUI 插件配置页修改（修改专家定义后需重载插件生效）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `experts` | template_list | 预置 4 个模板 | 专家团队成员列表，在 WebUI 中可视化增删改；字段见下 |
| `max_consults_per_event` | int | 12 | 单次对话所有咨询总次数上限（全局熔断） |
| `max_calls_per_expert` | int | 4 | 同一专家在单次对话中的最大被咨询次数 |
| `expert_timeout` | float | 90.0 | 单次咨询超时（秒） |
| `max_question_length` | int | 4000 | 问题长度上限，超长直接拒绝 |
| `panel_max_parallel` | int | 3 | 会诊最大并发数（信号量限流，防 API 限流） |
| `panel_style` | string | balanced | 会议汇总风格：balanced / critical / concise |
| `panel_render_image` | bool | false | 会议纪要是否渲染为图片（失败自动回退纯文本） |
| `expert_temperature` | float | 1.0 | 专家生成温度：0=严谨 1=均衡 2=发散；-1 表示不向模型传递该参数 |
| `retry_on_failure` | int | 1 | 专家调用失败自动重试次数（0-3）；超时不重试，避免等待翻倍 |
| `include_context_count` | int | 0 | 咨询时自动附带最近 N 条会话记录供专家理解背景（0=关闭） |
| `persist_stats` | bool | true | 累计统计写入 data/expert_cluster_stats.json，重启不丢失 |
| `panel_default_experts` | string | 空 | /panel 默认参会名单（name 或 tag，逗号分隔）；留空为全体 |
| `max_panel_size` | int | 8 | 单场会议参会上限，防止 token 失控 |
| `panel_timeout_multiplier` | float | 2.0 | 汇总阶段超时 = 单次咨询超时 × 该倍率 |
| `summary_provider_id` | string | 空 | 主持人汇总使用的独立对话模型 ID；留空跟随当前会话 |
| `summary_model` | string | 空 | 汇总强制指定的模型名；配合上一项使用 |
| `expert_tools_enabled` | bool | true | 专家是否可在回答中调用函数工具（完整工具循环，不可用时回退纯文本） |
| `expert_tool_names` | string | 三个只读工具 | 专家可用全局工具名（逗号分隔）；委派类工具强制排除 |
| `expert_max_tool_steps` | int | 5 | 单次专家咨询的最大工具调用轮数（1-20） |

### 专家字段

在 WebUI「专家团队成员列表」中添加条目即可：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | ✅ | 唯一标识，LLM 工具调用时使用（建议英文小写） |
| `display_name` | - | 展示名，留空用 name |
| `description` | 建议 | 擅长领域描述，主模型据此决定咨询谁 |
| `system_prompt` | ✅ | 专家的系统提示词 |
| `provider_id` | - | 指定对话模型 ID（WebUI 模型服务页可见），留空用当前会话模型 |
| `model` | - | 强制指定模型名，留空用 Provider 默认 |
| `tags` | - | 分组标签（逗号分隔），如 `dev,quality`；convene_expert_panel 与 /panel 可按标签召集整组专家 |

> 兼容说明：若直接编辑配置文件，`experts` 也接受 JSON 数组字符串形式。

## 安全机制

- **全局熔断**：单次对话咨询总次数达到 `max_consults_per_event` 后拒绝新咨询
- **单专家限额**：同一专家单次对话最多被咨询 `max_calls_per_expert` 次
- **超时控制**：单次咨询超时自动取消；会议汇总超时可按 `panel_timeout_multiplier` 倍率调节（默认 2 倍）
- **长度限制**：超长问题直接拒绝
- **并发限流**：信号量限制同时向模型发起的请求数
- **配额预检**：先校验目标存在性与配额，再扣减计数，避免无效消耗
- **防递归设计**：`consult_expert`/`convene_expert_panel` 被硬性排除在专家工具名单之外（误配自动剔除并告警），从结构上杜绝专家互相委派的无限循环
- **专家工具白名单**：默认仅下发三个只读信息工具，未注册或已在面板停用的工具不会传给专家

## 使用示例

```
用户：帮我把这段需求翻译成英文，并评估技术可行性
[LLM 调用 list_experts]
[LLM 调用 consult_expert(expert_name="translator", question="翻译：……")]
[LLM 调用 consult_expert(expert_name="coder", question="评估该需求的技术可行性：……")]
[LLM 汇总两份意见，回复用户]

用户：/panel 应该给新项目选 monorepo 还是 multirepo？
[并行咨询全体专家 → 会议纪要]
```

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- 灵感来源：[astrbot_plugin_subagent_worktogether](https://github.com/ScarletAugus/astrbot_plugin_subagent_worktogether)
