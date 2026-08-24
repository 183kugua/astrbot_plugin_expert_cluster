"""
astrbot_plugin_expert_cluster / 专家集群插件

为主 Agent 配备一支可随时召集的专家团队：
- 专家在 WebUI 插件配置中以 template_list 可视化管理，
  每位专家拥有独立的系统提示词，可指定独立的对话模型（Provider / model）
- 主 LLM 可通过 function-calling 向单个专家咨询（consult_expert）
- 也可召开多专家会诊（convene_expert_panel），并行收集意见后由主 LLM 汇总
- 提供 /experts、/panel 聊天指令，不依赖 function-calling 也能直接使用

实现说明：
- LLM 调用使用官方推荐的 Context.llm_generate()（>= v4.5.7），
  Provider 解析优先专家自带 provider_id，否则回退到会话当前对话模型。
- 专家为纯文本咨询、不携带任何工具，天然不存在专家间无限互相委派的
  递归风险；防护重点为：单事件咨询熔断、单专家调用上限、超时、
  问题长度与并发限流。
- 灵感来源：astrbot_plugin_subagent_worktogether（详见 README）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
from astrbot.api.star import Context, Star, register

# 单次事件内的熔断计数存放在 event extra 中，事件结束随对象回收
_CONSULT_COUNTS_KEY = "_expert_cluster_consult_counts"
_TOTAL_CONSULTS_KEY = "_expert_cluster_total_consults"

# 结构化错误前缀：让 LLM 能区分"系统错误"与"专家的正常回答"
_ERROR_PREFIX = "[EXPERT_ERROR]"

_PANEL_STYLES: dict[str, str] = {
    "balanced": (
        "你是专家会议的主持人。以下是多位专家针对同一问题给出的独立意见。"
        "请综合所有意见输出一份最终回答：吸收共识，剔除明显错误，"
        "对分歧点给出你的裁决并简述理由。直接输出最终答案。"
    ),
    "critical": (
        "你是专家会议的主持人，风格严谨挑剔。以下是多位专家的独立意见。"
        "请交叉比对各专家的观点，明确指出哪些意见互相矛盾、哪些论据不可靠，"
        "给出经过质询后站得住脚的最终结论。直接输出最终答案。"
    ),
    "concise": (
        "你是专家会议的主持人。以下是多位专家的独立意见。"
        "请提炼成一份尽可能精简的最终结论，只保留可靠且必要的信息。"
        "直接输出最终答案。"
    ),
}


@dataclass
class Expert:
    """一位专家的定义。"""

    name: str  # 唯一标识（用于工具调用，建议英文小写）
    display_name: str  # 展示名（可为中文）
    description: str  # 擅长领域描述，供主 LLM 判断该找谁
    system_prompt: str  # 专家的系统提示词
    provider_id: str = ""  # 可选：指定对话模型 ID，留空用当前会话模型
    model: str = ""  # 可选：强制指定模型名，留空用 Provider 默认模型
    # 运行时统计（跨事件累计，仅日志观察用）
    total_calls: int = field(default=0, repr=False)


def _safe_int(value: object, default: int, *, min_val: int | None = None) -> int:
    """从配置中安全解析 int，失败时回退默认值。"""
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        result = default
    if min_val is not None and result < min_val:
        result = min_val
    return result


def _safe_float(
    value: object, default: float, *, min_val: float | None = None
) -> float:
    """从配置中安全解析 float，失败时回退默认值。"""
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        result = default
    if min_val is not None and result < min_val:
        result = min_val
    return result


@register(
    "astrbot_plugin_expert_cluster",
    "kugua",
    "专家集群：为主 Agent 配备可随时咨询与会诊的专家团队",
    "1.0.0",
)
class ExpertClusterPlugin(Star):
    """专家集群插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        cfg = config or {}

        self.expert_timeout: float = _safe_float(
            cfg.get("expert_timeout", 90.0), 90.0, min_val=5.0
        )
        self.max_consults_per_event: int = _safe_int(
            cfg.get("max_consults_per_event", 12), 12, min_val=1
        )
        self.max_calls_per_expert: int = _safe_int(
            cfg.get("max_calls_per_expert", 4), 4, min_val=1
        )
        self.max_question_length: int = _safe_int(
            cfg.get("max_question_length", 4000), 4000, min_val=100
        )
        self.panel_max_parallel: int = _safe_int(
            cfg.get("panel_max_parallel", 3), 3, min_val=1
        )
        self.panel_style: str = cfg.get("panel_style", "balanced")
        if self.panel_style not in _PANEL_STYLES:
            logger.warning(
                "未知的 panel_style '%s'，已回退为 balanced", self.panel_style
            )
            self.panel_style = "balanced"
        self.panel_render_image: bool = bool(cfg.get("panel_render_image", False))

        # 会诊并发限流信号量（插件级共享）
        self._semaphore = asyncio.Semaphore(self.panel_max_parallel)
        # 各会话最近一次会议纪要（预留调试查看）
        self._last_panels: dict[str, dict] = {}

        self.experts: dict[str, Expert] = {}
        self._load_experts(cfg.get("experts", []))

        logger.info(
            "专家集群已加载：%d 位专家（%s）",
            len(self.experts),
            ", ".join(e.name for e in self.experts.values()) or "无",
        )

    # ------------------------------------------------------------------ #
    # 配置解析
    # ------------------------------------------------------------------ #

    def _load_experts(self, experts_raw: object) -> None:
        """解析配置中的专家列表。

        标准输入为 template_list 保存后的 list[dict]；
        同时兼容用户手写 JSON 字符串的情况（如直接编辑配置文件）。
        格式错误时保留为空而非崩溃。
        """
        self.experts = {}
        if not experts_raw:
            return

        raw: object
        if isinstance(experts_raw, str):
            try:
                raw = json.loads(experts_raw)
            except json.JSONDecodeError as e:
                logger.error("专家定义 JSON 解析失败，请检查 experts 配置：%s", e)
                return
        else:
            raw = experts_raw

        if not isinstance(raw, list):
            logger.error(
                "experts 应为列表（template_list），实际类型：%s",
                type(raw).__name__,
            )
            return

        seen: set[str] = set()
        for i, item in enumerate(raw):
            # template_list 条目中的 __template_key 是模板来源标记，跳过即可
            if not isinstance(item, dict):
                logger.warning("第 %d 个专家定义不是对象，已跳过", i + 1)
                continue
            name = str(item.get("name", "")).strip()
            system_prompt = str(item.get("system_prompt", "")).strip()
            if not name or not system_prompt:
                logger.warning("第 %d 个专家缺少 name 或 system_prompt，已跳过", i + 1)
                continue
            key = name.casefold()
            if key in seen:
                logger.warning("专家名重复：%s，后出现的定义已跳过", name)
                continue
            seen.add(key)
            self.experts[key] = Expert(
                name=name,
                display_name=str(item.get("display_name", "")).strip() or name,
                description=str(item.get("description", "")).strip() or "(未填写)",
                system_prompt=system_prompt,
                provider_id=str(item.get("provider_id", "")).strip(),
                model=str(item.get("model", "")).strip(),
            )

    # ------------------------------------------------------------------ #
    # 内部工具方法
    # ------------------------------------------------------------------ #

    def _find_expert(self, name: str) -> Expert | None:
        """按名称查找专家（大小写不敏感）。"""
        return self.experts.get(str(name).strip().casefold())

    def _get_counts(self, event: AstrMessageEvent) -> dict[str, int]:
        """获取（或初始化）本次事件中各专家被咨询的计数。"""
        counts = event.get_extra(_CONSULT_COUNTS_KEY)
        if counts is None:
            counts = {}
            event.set_extra(_CONSULT_COUNTS_KEY, counts)
        return counts

    def _get_total(self, event: AstrMessageEvent) -> int:
        return int(event.get_extra(_TOTAL_CONSULTS_KEY, 0) or 0)

    def _bump(self, event: AstrMessageEvent, expert: Expert) -> None:
        counts = self._get_counts(event)
        key = expert.name.casefold()
        counts[key] = counts.get(key, 0) + 1
        event.set_extra(_TOTAL_CONSULTS_KEY, self._get_total(event) + 1)
        expert.total_calls += 1

    async def _resolve_provider_id(
        self, expert: Expert, event: AstrMessageEvent
    ) -> str:
        """解析专家应使用的对话模型 ID。

        优先专家自带 provider_id（需存在且为文本生成类型），
        否则回退到当前会话正在使用的对话模型。
        失败时抛 ValueError，由调用方转换为 [EXPERT_ERROR]。
        """
        if expert.provider_id:
            prov = self.context.get_provider_by_id(provider_id=expert.provider_id)
            if prov is None:
                raise ValueError(
                    f"专家 '{expert.display_name}' 配置的 provider_id "
                    f"'{expert.provider_id}' 不存在，请检查插件配置"
                )
            if not hasattr(prov, "text_chat"):
                raise ValueError(
                    f"'{expert.provider_id}' 不是文本生成类型的模型服务，"
                    f"无法作为 '{expert.display_name}' 的对话模型"
                )
            return expert.provider_id

        try:
            return await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception as e:
            raise ValueError(f"当前会话没有可用的对话模型：{e}") from e

    async def _ask_expert(
        self, event: AstrMessageEvent, expert: Expert, question: str
    ) -> str:
        """向单个专家发起一次纯文本咨询，带超时与并发限流。"""
        try:
            provider_id = await self._resolve_provider_id(expert, event)
        except ValueError as e:
            return f"{_ERROR_PREFIX} {e}"

        extra_kwargs: dict = {}
        if expert.model:
            # llm_generate 的 **kwargs 会透传给 Provider.text_chat(model=...)
            extra_kwargs["model"] = expert.model

        async with self._semaphore:
            try:
                resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=question,
                        system_prompt=expert.system_prompt,
                        **extra_kwargs,
                    ),
                    timeout=self.expert_timeout,
                )
            except asyncio.TimeoutError:
                return (
                    f"{_ERROR_PREFIX} 专家 '{expert.display_name}' 超时"
                    f"（>{self.expert_timeout}s），本次咨询被取消。"
                )
            except Exception as e:
                logger.error("咨询专家 '%s' 失败", expert.display_name, exc_info=True)
                return f"{_ERROR_PREFIX} 咨询专家 '{expert.display_name}' 时出错：{e}"

        text = (resp.completion_text or "").strip()
        if not text:
            return f"{_ERROR_PREFIX} 专家 '{expert.display_name}' 返回了空回复。"
        return text

    def _validate_question(self, question: str) -> str | None:
        """校验问题文本，不通过时返回错误信息。"""
        if not question or not question.strip():
            return f"{_ERROR_PREFIX} question 不能为空。"
        if self.max_question_length > 0 and len(question) > self.max_question_length:
            return (
                f"{_ERROR_PREFIX} 问题长度 {len(question)} 超过上限 "
                f"{self.max_question_length}，请精简后重试。"
            )
        return None

    @staticmethod
    def _format_opinions(pairs: list[tuple[Expert, str]]) -> str:
        """把 (专家, 意见) 列表格式化为供主 LLM 汇总的文本。"""
        blocks = [
            f"【{expert.display_name}（{expert.name}）】的意见：\n{opinion}"
            for expert, opinion in pairs
        ]
        return "\n\n---\n\n".join(blocks)

    async def _summarize_panel(
        self, event: AstrMessageEvent, question: str, opinions_text: str
    ) -> str:
        """以主持人身份汇总各专家意见，失败时抛出异常由调用方兜底。"""
        provider_id = await self.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )
        resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=(
                    f"用户的问题：\n{question}\n\n以下是各位专家的独立意见：\n\n"
                    f"{opinions_text}"
                ),
                system_prompt=_PANEL_STYLES[self.panel_style],
            ),
            timeout=self.expert_timeout * 2,
        )
        summary = (resp.completion_text or "").strip()
        if not summary:
            raise ValueError("汇总模型返回空回复")
        return summary

    # ------------------------------------------------------------------ #
    # LLM 工具：list_experts
    # ------------------------------------------------------------------ #

    @llm_tool(name="list_experts")
    async def list_experts(self, event: AstrMessageEvent) -> str:
        """List all experts in the cluster with their name, expertise and quota usage. Use this before consult_expert if unsure which expert fits the request."""
        if not self.experts:
            return (
                f"{_ERROR_PREFIX} 专家集群为空。"
                "请提醒用户在 WebUI 插件配置中添加专家后再试。"
            )
        counts = self._get_counts(event)
        lines = ["当前可咨询的专家："]
        for e in self.experts.values():
            used = counts.get(e.name.casefold(), 0)
            model_desc = e.model or "(默认模型)"
            lines.append(
                f"- {e.name}（{e.display_name}）：{e.description} "
                f"[model: {model_desc}, 本次对话已调用: "
                f"{used}/{self.max_calls_per_expert}]"
            )
        lines.append(
            f"\n本次对话咨询总配额：{self._get_total(event)}/"
            f"{self.max_consults_per_event}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # LLM 工具：consult_expert
    # ------------------------------------------------------------------ #

    @llm_tool(name="consult_expert")
    async def consult_expert(
        self, event: AstrMessageEvent, expert_name: str, question: str
    ) -> str:
        """Consult a specific expert from the cluster and get the expert's professional answer. Use this when the user's request needs specialized knowledge (legal, medical, coding, translation, etc.). Use list_experts first if unsure which expert to ask.

        IMPORTANT: If the result starts with "[EXPERT_ERROR]", the consultation failed at system level (quota, timeout, config...). Do NOT treat it as the expert's answer; answer by yourself or try another expert.

        Args:
            expert_name(string): The expert's name, exactly as listed by list_experts.
            question(string): A SELF-CONTAINED question. Include all necessary context and background - the expert cannot see this conversation.
        """
        # 输入校验
        if not expert_name or not expert_name.strip():
            return f"{_ERROR_PREFIX} expert_name 不能为空。"
        if err := self._validate_question(question):
            return err

        # 目标存在性校验（先校验再扣配额，避免白白熔断）
        expert = self._find_expert(expert_name)
        if expert is None:
            available = ", ".join(e.name for e in self.experts.values()) or "(无)"
            return (
                f"{_ERROR_PREFIX} 找不到专家 '{expert_name}'。"
                f"可用专家：{available}。可用 list_experts 查看详情。"
            )

        # 熔断：单事件总配额
        if self._get_total(event) >= self.max_consults_per_event:
            return (
                f"{_ERROR_PREFIX} 本次对话的咨询总次数已达上限"
                f"（{self.max_consults_per_event}），请基于已有信息直接作答。"
            )
        # 熔断：单专家调用上限
        counts = self._get_counts(event)
        key = expert.name.casefold()
        if counts.get(key, 0) >= self.max_calls_per_expert:
            return (
                f"{_ERROR_PREFIX} 专家 '{expert.display_name}' 本次对话中已被咨询"
                f" {counts[key]} 次，达到上限（{self.max_calls_per_expert}）。"
                "请自行总结或咨询其他专家。"
            )

        self._bump(event, expert)
        logger.info(
            "咨询专家 %s（事件累计 %d/%d）",
            expert.display_name,
            self._get_total(event),
            self.max_consults_per_event,
        )
        return await self._ask_expert(event, expert, question)

    # ------------------------------------------------------------------ #
    # LLM 工具：convene_expert_panel
    # ------------------------------------------------------------------ #

    @llm_tool(name="convene_expert_panel")
    async def convene_expert_panel(
        self, event: AstrMessageEvent, question: str, expert_names: str = ""
    ) -> str:
        """Convene an expert panel: consult several experts IN PARALLEL with the same question and return all their opinions. Then synthesize a final answer yourself from those opinions. Use this for complex questions that benefit from multiple professional perspectives.

        Note: each consulted expert counts toward per-expert and total quota. If some results start with "[EXPERT_ERROR]", synthesize based on the successful ones or answer by yourself.

        Args:
            question(string): A SELF-CONTAINED question with all necessary context.
            expert_names(string): Comma-separated expert names to convene, e.g. "coder,reviewer". Leave EMPTY to convene ALL experts.
        """
        if err := self._validate_question(question):
            return err

        if not self.experts:
            return (
                f"{_ERROR_PREFIX} 专家集群为空，无法召开会议。"
                "请提醒用户在 WebUI 插件配置中添加专家。"
            )

        # 确定参会名单
        targets: list[Expert]
        if expert_names and expert_names.strip():
            targets = []
            missing: list[str] = []
            for raw_name in expert_names.split(","):
                name = raw_name.strip()
                if not name:
                    continue
                expert = self._find_expert(name)
                if expert is None:
                    missing.append(name)
                elif all(expert.name != t.name for t in targets):
                    targets.append(expert)
            if missing:
                available = ", ".join(e.name for e in self.experts.values())
                return (
                    f"{_ERROR_PREFIX} 以下专家不存在：{', '.join(missing)}。"
                    f"可用专家：{available}"
                )
        else:
            targets = list(self.experts.values())

        if not targets:
            return f"{_ERROR_PREFIX} 参会专家名单为空。"

        # 配额预检：总配额不足时按剩余配额裁剪参会人数
        quota_left = self.max_consults_per_event - self._get_total(event)
        if quota_left <= 0:
            return (
                f"{_ERROR_PREFIX} 本次对话的咨询总次数已达上限"
                f"（{self.max_consults_per_event}），无法召开会议。"
            )
        if len(targets) > quota_left:
            targets = targets[:quota_left]

        # 并行咨询
        async def _consult(expert: Expert) -> tuple[Expert, str]:
            counts = self._get_counts(event)
            key = expert.name.casefold()
            if counts.get(key, 0) >= self.max_calls_per_expert:
                return expert, (
                    f"{_ERROR_PREFIX} 已达该专家的单次对话调用上限，未参与本次会议。"
                )
            self._bump(event, expert)
            opinion = await self._ask_expert(event, expert, question)
            return expert, opinion

        results = await asyncio.gather(*(_consult(e) for e in targets))
        logger.info(
            "专家会议结束：%s（事件累计 %d/%d）",
            "、".join(e.display_name for e, _ in results),
            self._get_total(event),
            self.max_consults_per_event,
        )
        return (
            f"以下是 {len(results)} 位专家对同一问题的独立意见，"
            "请你综合后给出最终回答：\n\n" + self._format_opinions(list(results))
        )

    # ------------------------------------------------------------------ #
    # 聊天指令：/experts
    # ------------------------------------------------------------------ #

    @filter.command("experts", alias={"专家列表", "专家团队"})
    async def cmd_experts(self, event: AstrMessageEvent):
        """列出专家团队的所有成员及其擅长领域。"""
        if not self.experts:
            yield event.plain_result(
                "专家集群是空的喵…请在 WebUI 插件配置页的「专家团队成员列表」中添加专家。"
            )
            return
        lines = ["🎓 当前专家团队：\n"]
        for i, e in enumerate(self.experts.values(), 1):
            model_desc = e.model or "默认模型"
            lines.append(
                f"{i}. {e.display_name}（{e.name}）\n"
                f"   擅长：{e.description}\n"
                f"   模型：{model_desc}\n"
                f"   累计咨询：{e.total_calls} 次"
            )
        lines.append(
            "\n提示：对话中可直接让 AI 咨询专家，或用 /panel <问题> 召开专家会议。"
        )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 聊天指令：/panel
    # ------------------------------------------------------------------ #

    @filter.command("panel", alias={"专家会议", "会诊"})
    async def cmd_panel(self, event: AstrMessageEvent):
        """召开专家会议：并行咨询全体专家并汇总最终结论，用法 /panel <问题>。"""
        # 解析问题文本（兼容带/不带唤醒前缀的消息形式）
        text = (event.message_str or "").strip()
        for prefix in ("/panel", "panel", "/专家会议", "专家会议", "/会诊", "会诊"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix) :].strip()
                break
        if not text:
            yield event.plain_result(
                "用法：/panel <问题>\n例如：/panel 量子计算目前最大的工程瓶颈是什么？"
            )
            return

        if not self.experts:
            yield event.plain_result(
                "专家集群是空的，无法开会喵。请先在 WebUI 插件配置页添加专家。"
            )
            return
        if err := self._validate_question(text):
            yield event.plain_result(err)
            return

        quota_left = self.max_consults_per_event - self._get_total(event)
        if quota_left <= 0:
            yield event.plain_result(
                f"本次对话咨询次数已达上限（{self.max_consults_per_event}），会议无法召开。"
            )
            return
        targets = list(self.experts.values())[:quota_left]

        started = time.time()
        # 过程提示：钩子外的 handler 中可正常 send；最终回复仍用 yield
        await event.send(
            event.plain_result(
                f"🔔 正在召集 {len(targets)} 位专家会诊："
                f"{'、'.join(e.display_name for e in targets)} …"
            )
        )

        async def _consult(expert: Expert) -> tuple[Expert, str]:
            self._bump(event, expert)
            opinion = await self._ask_expert(event, expert, text)
            return expert, opinion

        results = await asyncio.gather(*(_consult(e) for e in targets))
        opinions_text = self._format_opinions(list(results))

        # 主持人汇总；失败时回退为原始意见拼接
        try:
            summary_text = await self._summarize_panel(event, text, opinions_text)
        except Exception:
            logger.warning("专家会议汇总失败，回退为原始意见拼接", exc_info=True)
            summary_text = (
                "（主持人汇总失败，以下为各位专家的原始意见）\n\n"
                + "\n\n".join(opinion for _, opinion in results)
            )

        elapsed = time.time() - started
        report = (
            f"📋 专家会议纪要\n"
            f"问题：{text}\n"
            f"———————\n"
            f"{summary_text}\n"
            f"———————\n"
            f"参会：{'、'.join(e.display_name for e, _ in results)}"
            f"（用时 {elapsed:.1f}s）"
        )

        # 可选：渲染为图片发送
        if self.panel_render_image:
            try:
                url = await self.text_to_image(report)
                yield event.image_result(url)
                self._last_panels[event.unified_msg_origin] = {
                    "question": text,
                    "report": report,
                    "timestamp": time.time(),
                }
                return
            except Exception:
                logger.warning("会议纪要文转图失败，回退纯文本", exc_info=True)

        self._last_panels[event.unified_msg_origin] = {
            "question": text,
            "report": report,
            "timestamp": time.time(),
        }
        yield event.plain_result(report)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def terminate(self):
        """插件卸载/停用时调用。"""
        total = sum(e.total_calls for e in self.experts.values())
        logger.info("专家集群插件已卸载，运行期间累计咨询 %d 次", total)
