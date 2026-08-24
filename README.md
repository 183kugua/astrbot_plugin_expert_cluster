# 专家集群 astrbot_plugin_expert_cluster

为主 Agent 配备一支**可随时召集的专家团队**。每位专家拥有独立的系统提示词，可指定独立的模型提供商（Provider）与模型；主 Agent 在对话中可以自主判断，向单个专家咨询，或召开多专家并行会诊后汇总结论。

**功能亮点**

- 🔧 **开箱即用**：预置翻译/代码/审稿三个模板专家，装好就能用
- 🧠 **主 Agent 自主决策**：以 LLM 函数工具（function-calling）形式接入，无需固定触发词，主模型自行判断何时咨询谁
- ⚡ **并行会诊**：`convene_expert_panel` 并行咨询多位专家，信号量限流防 API 过载
- 🛠️ **专家也能用工具**（v1.2.0）：专家回答前可调用函数工具查证信息，白名单机制防递归
- 👥 **可视化配置**：WebUI 表格化增删改专家，支持独立模型、温度与分组标签
- 🛡️ **多层防护**：全局熔断、单专家限额、超时控制、并发限流、配额预检
- 📊 **用量统计**：本次对话配额 + 累计咨询次数持久化，重启不丢失
- 🖼️ **会议纪要渲染**：可选将纪要渲染为图片输出（失败自动回退纯文本）

## 环境要求

- AstrBot **>= v4.10.4**（`llm_generate` 需 v4.5.7，`template_list` 配置需 v4.10.4）
- 已在 WebUI 配置至少一个对话模型（Provider）
- （可选）如需专家使用函数工具，AstrBot 版本需支持 `tool_loop_agent`

## 安装

**方式一：WebUI 安装（推荐）**

1. 打开 AstrBot WebUI → 插件管理 → 从仓库安装 / 从 URL 安装
2. 填入本仓库地址 `https://github.com/183kugua/astrbot_plugin_expert_cluster`
3. 安装完成后在插件列表点击「启用」

**方式二：手动安装**

```bash
cd <AstrBot数据目录>/plugins
git clone https://github.com/183kugua/astrbot_plugin_expert_cluster
# 重启 AstrBot 或在 WebUI 重载插件
```

安装后无需额外配置即可工作；建议先到 WebUI → 插件管理 → 本插件 → 配置页，确认预置专家符合需求。

## 快速上手

1. **看团队**：聊天中发送 `/experts` 查看当前专家名单
2. **直接问**：像平时一样聊天，当问题需要专业视角时（如"帮我审查这段代码的逻辑"），主 Agent 会自动咨询对应专家并汇总回复
3. **开会诊**：发送 `/panel 应该选 monorepo 还是 multirepo？` 强制召开全体专家会议

## 工作原理

```
用户消息 ──▶ 主 Agent（带 5 个 LLM 工具）
              │
              ├─ list_experts      不确定找谁时先看名单
              ├─ search_experts    专家较多时按关键词定位
              ├─ get_expert_usage  开会前查剩余配额
              ├─ consult_expert    单专家深度咨询
              └─ convene_expert_panel  多专家并行会诊
                       │
                       ▼
              各专家（独立系统提示词 + 可选工具循环）并行作答
                       │
                       ▼
              主持人按 panel_style 风格汇总 → 回复用户
```

所有系统级失败均以 `[EXPERT_ERROR]` 前缀返回，工具描述已引导主模型将其与专家正常回答区分，从而触发自我修复（换专家 / 自行作答），而不是把错误信息当作答案复述给用户。

## 与 subagent_worktogether 的区别

本项目灵感来源于 [astrbot_plugin_subagent_worktogether](https://github.com/ScarletAugus/astrbot_plugin_subagent_worktogether)（AGPL-3.0），借鉴了其"多 Agent 协作 + 结构化错误前缀 + 熔断防护"的设计思路，但实现方式不同，**未复制其代码**：

| | subagent_worktogether | 本插件 |
| --- | --- | --- |
| 专家来源 | AstrBot WebUI 中配置的 Handoff 子代理 | 插件配置中的 template_list 专家条目 |
| 执行方式 | `tool_loop_agent` 完整工具循环 | 默认带只读工具的完整工具循环（可关，回退 `llm_generate` 纯文本） |
| 递归风险 | 存在，需五层防护 | 委派类工具被硬性排除出专家工具名单，防护聚焦熔断/超时/限流 |
| 上手成本 | 需先配置子代理 | 开箱即用，WebUI 可视化添加专家 |
| 会诊能力 | 逐个串行委派 | `convene_expert_panel` 并行会诊 |

## 预置模板专家

WebUI 配置页「专家团队成员列表」下方下拉框可选择模板一键添加：

| name | 展示名 | 定位 | 分组标签 |
| --- | --- | --- | --- |
| `translator` | 翻译专家 | 精通多语种互译，熟悉本地化与文化差异 | - |
| `coder` | 代码专家 | 擅长编程、调试、架构设计与代码审查 | `dev` |
| `reviewer` | 审稿专家 | 擅长事实核查、逻辑漏洞与表达问题审查 | `dev,quality` |
| `custom` | - | 空白模板，从零定义自己的专家 | - |

标签的用途：`convene_expert_panel` 的参会名单和 `/panel` 默认名单都接受 tag，填 `dev` 即可召集整组开发类专家。

## LLM 工具详解

主 Agent 通过以下函数工具使用专家团队（均为自主调用，无需指令触发）：

### consult_expert

向指定专家提出一个自包含问题，返回专家的专业回答。

- 参数：`expert_name`（专家标识）、`question`（自包含问题——专家看不到聊天上下文，问题必须写清背景）
- 适用场景：问题只需要单一专业视角（法律、医学、编程、翻译……）

### convene_expert_panel

并行咨询多位专家后返回各方独立意见，由主 Agent 汇总最终答案。

- 参数：`question`（自包含问题）、`expert_names`（可选，逗号分隔的专家 name 或分组 tag，留空为全体）
- 适用场景：复杂问题需要多专业视角交叉验证（方案评审、技术选型、争议裁决）
- 参会人数受 `max_panel_size` 上限保护，并发受 `panel_max_parallel` 限流

### list_experts

列出所有专家的名称、擅长领域与本次对话的配额使用情况。不确定该咨询谁时先调用。

### search_experts

按关键词搜索专家，匹配名称/展示名/擅长领域描述/标签。专家较多时快速定位合适人选。

### get_expert_usage

查询配额与用量统计：本次对话剩余额度、各专家累计咨询次数。召开大型会议前建议先查余量。

## 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/experts`（别名：专家列表、专家团队） | 列出专家团队及各专家累计咨询次数 |
| `/panel <问题>`（别名：专家会议、会诊） | 直接召开专家会议：按 `panel_default_experts` 名单（留空为全体）并行咨询 → 按 `panel_style` 汇总 → 输出纪要 |

## 专家函数工具（v1.2.0）

开启 `expert_tools_enabled` 后，每位专家在回答前可通过 `tool_loop_agent` 完整工具循环调用函数工具查证信息。

**工作机制**

- 白名单：只有 `expert_tool_names` 中列出的工具会下发给专家，默认为本插件的三个只读信息工具 `list_experts, search_experts, get_expert_usage`
- 防递归硬保护：`consult_expert` 与 `convene_expert_panel` 被强制排除，即使误配进白名单也会自动剔除并记录警告日志
- 停用联动：在 WebUI 中停用的全局工具不会下发给专家
- 自动降级：工具集为空或获取失败时自动回退原纯文本咨询模式，不影响可用性
- 步数上限：每位专家单次咨询最多进行 `expert_max_tool_steps` 轮工具调用（1-20）

**扩展玩法**：想让代码专家联网搜资料？在 `expert_tool_names` 里加上你环境中已有的搜索类工具名即可（逗号分隔）。工具名需与本插件或其他插件注册的 LLM 工具一致。

**注意事项**：框架的 agent 工具循环暂不支持透传专家的独立模型名与温度覆盖，`provider_id` 选择仍生效，但 `model` / `expert_temperature` 仅在关闭专家工具的纯文本模式下生效。

## 配置项

在 WebUI 插件配置页修改（修改专家定义后需重载插件生效）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `experts` | template_list | 空（含 4 个模板） | 专家团队成员列表，在 WebUI 中可视化增删改；字段见下 |
| `max_consults_per_event` | int | 12 | 单次对话所有咨询总次数上限（全局熔断） |
| `max_calls_per_expert` | int | 4 | 同一专家在单次对话中的最大被咨询次数 |
| `expert_timeout` | float | 90.0 | 单次咨询超时（秒）；会诊时覆盖所有并行专家调用 |
| `max_question_length` | int | 4000 | 问题长度上限，超长直接拒绝 |
| `panel_max_parallel` | int | 3 | 会诊最大并发数（信号量限流，防 API 限流） |
| `panel_style` | string | balanced | 会议汇总风格，三种风格见下表 |
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
| `expert_tool_names` | string | 见上文 | 专家可用全局工具名（逗号分隔）；委派类工具强制排除 |
| `expert_max_tool_steps` | int | 5 | 单次专家咨询的最大工具调用轮数（1-20） |

### panel_style 三种风格

| 取值 | 汇总行为 | 适合场景 |
| --- | --- | --- |
| `balanced` | 吸收共识，剔除明显错误，对分歧点给出裁决并简述理由 | 日常大多数问题 |
| `critical` | 交叉比对观点，明确指出互相矛盾的意见与不可靠论据，给出经质询后站得住脚的结论 | 方案评审、风险论证 |
| `concise` | 提炼成尽可能精简的最终结论，只保留可靠且必要的信息 | 只要答案不要过程的快问快答 |

> 配置了非法值时自动回退 `balanced` 并记录警告。

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
>
> 写好 description 是让主模型"选对人"的关键：描述越具体（如"擅长 Python 异步编程与爬虫反爬"而非"程序员"），路由越准。

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

**单专家咨询**（主 Agent 自动路由）：

```
用户：帮我把这段需求翻译成英文，并评估技术可行性
[LLM 调用 consult_expert(expert_name="translator", question="翻译：……")]
[LLM 调用 consult_expert(expert_name="coder", question="评估该需求的技术可行性：……")]
[LLM 汇总两份意见，回复用户]
```

**多专家会诊**：

```
用户：/panel 应该给新项目选 monorepo 还是 multirepo？
[并行咨询全体专家 → 会议纪要]
```

**按标签召集**（LLM 自主调用）：

```
用户：从工程角度评估一下这个方案
[LLM 调用 convene_expert_panel(question="评估该方案", expert_names="dev")]
[仅 dev 标签组的 coder/reviewer 参会 → 汇总结论]
```

## FAQ

**Q：怎么知道主模型有没有真的去咨询专家？**
开启 AstrBot 日志（日志等级 INFO）观察函数调用记录；也可发 `/experts` 对比各专家累计咨询次数的变化。

**Q：专家的回答里带着"我是 AI"之类的开场白？**
这是专家 system_prompt 的问题，编辑对应专家的系统提示词，明确角色定位即可。

**Q：想换掉某个预置专家？**
直接在 WebUI 列表中删除该条目，或在「专家团队成员列表」下拉框选择模板重新定制；修改后重载插件生效。

**Q：会议很慢 / 报 API 限流？**
调低 `panel_max_parallel`（默认 3），或缩小 `panel_default_experts` 参会名单；必要时提高 `expert_timeout`。

**Q：统计数据在哪？**
`data/expert_cluster_stats.json`（相对 AstrBot 数据目录）；设 `persist_stats=false` 可关闭持久化。

**Q：如何让专家联网搜索？**
在 `expert_tool_names` 中加入环境中已注册的搜索工具名，详见上文「专家函数工具」一节。

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- 灵感来源：[astrbot_plugin_subagent_worktogether](https://github.com/ScarletAugus/astrbot_plugin_subagent_worktogether)
