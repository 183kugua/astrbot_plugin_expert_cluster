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
- 专家咨询默认通过 tool_loop_agent 携带一组函数工具（白名单可在配置中
  调整或整体关闭，不可用时自动回退纯文本）；委派类工具被硬性排除，
  结构上杜绝专家间无限互相委派的递归。防护重点为：单事件咨询熔断、
  单专家调用上限、超时、问题长度与并发限流。
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

try:  # v4.5.7+ 提供的工具集类型；缺失时专家工具能力自动降级关闭
    from astrbot.core.agent.tool import ToolSet
except ImportError:  # pragma: no cover
    ToolSet = None  # type: ignore[assignment]

# 单次事件内的熔断计数存放在 event extra 中，事件结束随对象回收
_CONSULT_COUNTS_KEY = "_expert_cluster_consult_counts"
_TOTAL_CONSULTS_KEY = "_expert_cluster_total_consults"

# 结构化错误前缀：让 LLM 能区分"系统错误"与"专家的正常回答"
_ERROR_PREFIX = "[EXPERT_ERROR]"

# 无论配置如何都不允许下发给专家的工具：会造成专家间递归咨询
_EXPERT_FORBIDDEN_TOOLS = frozenset({"consult_expert", "convene_expert_panel"})

# 工具模式下追加到专家系统提示词的简短说明
_EXPERT_TOOL_HINT = (
    "\n\n[系统提示] 本次咨询已为你启用函数工具，需要查证信息时可主动调用。"
    "调用失败或不需要时，直接基于自身知识回答即可。"
)

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
    tags: tuple[str, ...] = ()  # 可选：分组标签，供 /panel 按组召集
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
    "1.3.0",
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

        # ---- 新增可配置项 ----
        # 专家生成温度；负数表示不向模型注入该参数（跟随服务商默认值）
        self.expert_temperature: float = _safe_float(
            cfg.get("expert_temperature", 1.0), 1.0
        )
        # 单次咨询失败后的自动重试次数（超时不重试，避免等待翻倍）
        self.retry_on_failure: int = min(
            _safe_int(cfg.get("retry_on_failure", 1), 1, min_val=0), 3
        )
        # 咨询时附带最近 N 条会话上下文，0 为关闭
        self.include_context_count: int = min(
            _safe_int(cfg.get("include_context_count", 0), 0, min_val=0), 20
        )
        # 是否把累计统计写入 data 目录，重启不丢失
        self.persist_stats: bool = bool(cfg.get("persist_stats", True))
        # /panel 未显式点名时的默认参会名单（逗号分隔 name 或 tag），留空为全体
        self.panel_default_experts: str = str(
            cfg.get("panel_default_experts", "")
        ).strip()
        # 单场会议参会人数上限（含 /panel 与 convene_expert_panel）
        self.max_panel_size: int = _safe_int(cfg.get("max_panel_size", 8), 8, min_val=1)
        # 主持人汇总阶段相对单次咨询的超时倍率
        self.panel_timeout_multiplier: float = _safe_float(
            cfg.get("panel_timeout_multiplier", 2.0), 2.0, min_val=1.0
        )
        # 主持人汇总可使用独立的对话模型
        self.summary_provider_id: str = str(cfg.get("summary_provider_id", "")).strip()
        self.summary_model: str = str(cfg.get("summary_model", "")).strip()

        # ---- 专家函数工具（v1.2.0）----
        # 是否允许专家在回答时通过完整工具循环调用函数工具
        self.expert_tools_enabled: bool = bool(cfg.get("expert_tools_enabled", True))
        # 专家可用的全局函数工具名（逗号分隔）；委派类工具恒被排除
        self.expert_tool_names_raw: str = str(
            cfg.get(
                "expert_tool_names",
                "list_experts, search_experts, get_expert_usage",
            )
        ).strip()
        # 单次专家咨询内的最大工具调用轮数
        self.expert_max_tool_steps: int = min(
            _safe_int(cfg.get("expert_max_tool_steps", 5), 5, min_val=1), 20
        )

        # 会诊并发限流信号量（插件级共享）
        self._semaphore = asyncio.Semaphore(self.panel_max_parallel)
        # 各会话最近一次会议纪要（预留调试查看）
        self._last_panels: dict[str, dict] = {}
        # 专家工具集缓存：首次构建后复用，避免每次咨询重复扫描全局工具
        self._tool_set_built: bool = False
        self._cached_tool_set: ToolSet | None = None

        self.experts: dict[str, Expert] = {}
        self._load_experts(cfg.get("experts", []))
        self._load_persisted_stats()

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
            raw_tags = str(item.get("tags", "")).strip()
            tags = tuple(
                dict.fromkeys(
                    t.strip().casefold() for t in raw_tags.split(",") if t.strip()
                )
            )
            self.experts[key] = Expert(
                name=name,
                display_name=str(item.get("display_name", "")).strip() or name,
                description=str(item.get("description", "")).strip() or "(未填写)",
                system_prompt=system_prompt,
                provider_id=str(item.get("provider_id", "")).strip(),
                model=str(item.get("model", "")).strip(),
                tags=tags,
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

    # ------------------------------------------------------------------ #
    # 统计持久化 / 会话上下文 / 参会名单
    # ------------------------------------------------------------------ #

    @property
    def _stats_path(self):
        try:
            from astrbot.core.star.star_tools import StarTools

            return StarTools.get_data_dir("astrbot_plugin_expert_cluster") / (
                "stats.json"
            )
        except Exception:
            # 环境异常时回退到相对路径，保持向后兼容（迁移旧数据）
            from pathlib import Path

            return Path("data") / "expert_cluster_stats.json"

    def _load_persisted_stats(self) -> None:
        """启动时恢复各专家累计咨询次数（persist_stats 开启时）。"""
        if not self.persist_stats:
            return
        try:
            raw = self._stats_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            calls = data.get("total_calls", {})
            if isinstance(calls, dict):
                for key, count in calls.items():
                    expert = self.experts.get(str(key).casefold())
                    try:
                        cnt = int(count)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
                    if expert is not None and cnt > 0:
                        expert.total_calls = cnt
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("读取统计文件失败，已忽略：%s", e)

    def _save_persisted_stats(self) -> None:
        """把累计统计写入 data 目录（persist_stats 开启时）。"""
        if not self.persist_stats:
            return
        try:
            self._stats_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "total_calls": {e.name: e.total_calls for e in self.experts.values()}
            }
            self._stats_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("保存统计文件失败，已忽略：%s", e)

    async def _get_recent_context(
        self, event: AstrMessageEvent, question: str = ""
    ) -> str:
        """按配置截取最近几条会话记录，拼为附加给专家的上下文文本。

        会过滤掉内容与本次 question 重复的用户消息（llm_tool 触发时
        history 往往已包含当前提问），避免同一问题注入两遍。
        任何一步失败都静默返回空串，绝不影响正常咨询。
        """
        if self.include_context_count <= 0:
            return ""
        q_head = (question or "").strip()[:100]
        try:
            mgr = self.context.conversation_manager
            umo = event.unified_msg_origin
            conv_id = await mgr.get_curr_conversation_id(umo)
            if not conv_id:
                return ""
            conv = await mgr.get_conversation(umo, conv_id)
            history = json.loads(getattr(conv, "history", None) or "[]")
            recent = [
                m
                for m in history
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and str(m.get("content") or "").strip()
                and not (
                    q_head
                    and m.get("role") == "user"
                    and q_head in str(m.get("content") or "")
                )
            ][-self.include_context_count :]
            if not recent:
                return ""
            lines = [
                f"{'用户' if m['role'] == 'user' else 'AI'}：{str(m['content']).strip()[:500]}"
                for m in recent
            ]
            return (
                "\n\n[最近的对话记录，仅供理解背景，无需回应其中旧话题：\n"
                + "\n".join(lines)
                + "\n]"
            )
        except Exception as e:
            logger.debug("获取会话上下文失败，跳过注入：%s", e)
            return ""

    def _parse_panel_selector(
        self, selector: str, *, strict: bool = False
    ) -> tuple[list[Expert], list[str]]:
        """把逗号分隔的参会名单解析为 (targets, unmatched)。

        支持专家 name 与分组 tag 混合填写，结果自动去重。
        - strict=False（/panel 命令语义）：未匹配项静默跳过；
        - strict=True（convene_expert_panel 显式名单）：未匹配项计入
          unmatched，由调用方决定是否报错。
        """
        targets: list[Expert] = []
        unmatched: list[str] = []
        all_experts = list(self.experts.values())
        for raw in selector.split(","):
            token = raw.strip().casefold()
            if not token:
                continue
            matched = False
            expert = self._find_expert(token)
            if expert is not None:
                matched = True
                if all(expert.name != t.name for t in targets):
                    targets.append(expert)
            for e in all_experts:
                if token in e.tags and all(e.name != t.name for t in targets):
                    matched = True
                    targets.append(e)
            if not matched:
                unmatched.append(token)
        return targets, unmatched

    def _resolve_panel_targets(self, selector: str | None = None) -> list[Expert]:
        """把逗号分隔的参会名单解析为专家列表。

        支持专家 name 或分组 tag（如 "coder" 或 "dev"）；
        无法匹配的项静默跳过。selector 留空时使用 panel_default_experts，
        再留空则为全体专家。
        """
        selector = (selector or self.panel_default_experts or "").strip()
        if not selector:
            return list(self.experts.values())
        targets, _unmatched = self._parse_panel_selector(selector)
        for token in _unmatched:
            logger.warning("参会名单项 '%s' 未匹配到任何专家或标签，已跳过", token)
        return targets

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

    def _build_expert_tool_set(self) -> ToolSet | None:
        """按配置构建专家可用的函数工具集；返回 None 表示回退纯文本咨询。

        防护规则：
        - 委派类工具（consult/convene）恒被排除，避免专家互相委派递归；
        - 已在 WebUI 停用（active=False）的工具不下发；
        - 全局工具管理器不可用时自动降级为纯文本模式。
        """
        if ToolSet is None or not self.expert_tools_enabled:
            return None
        # 惰性缓存：本插件的 llm_tool 在插件加载完成前可能尚未注册，
        # 延迟到首次真实咨询时构建最稳妥，之后直接复用
        if self._tool_set_built:
            return self._cached_tool_set
        names = [n.strip() for n in self.expert_tool_names_raw.split(",") if n.strip()]
        if not names:
            return None
        blocked = sorted(set(names) & _EXPERT_FORBIDDEN_TOOLS)
        if blocked:
            logger.warning(
                "expert_tool_names 含有会引发专家递归咨询的工具（%s），已自动排除",
                "、".join(blocked),
            )
            names = [n for n in names if n not in _EXPERT_FORBIDDEN_TOOLS]
            if not names:
                return None
        try:
            full_set = self.context.get_llm_tool_manager().get_full_tool_set()
        except Exception as e:
            logger.warning("获取全局函数工具失败，专家咨询回退为纯文本：%s", e)
            return None
        tool_set = ToolSet()
        missing: list[str] = []
        for name in names:
            tool = full_set.get_tool(name)  # 已含权限守卫包装
            if tool is None:
                missing.append(name)
                continue
            if not getattr(tool, "active", True):
                continue  # 面板中停用的工具静默跳过
            tool_set.add_tool(tool)
        if missing:
            logger.warning(
                "expert_tool_names 中未注册的工具已跳过：%s", "、".join(missing)
            )
        result = None if tool_set.empty() else tool_set
        self._cached_tool_set = result
        self._tool_set_built = True
        return result

    async def _ask_expert(
        self, event: AstrMessageEvent, expert: Expert, question: str
    ) -> str:
        """向单个专家发起一次纯文本咨询，带超时、并发限流与可选重试。"""
        try:
            provider_id = await self._resolve_provider_id(expert, event)
        except ValueError as e:
            return f"{_ERROR_PREFIX} {e}"

        extra_kwargs: dict = {}
        if expert.model:
            # llm_generate 的 **kwargs 会透传给 Provider.text_chat(model=...)
            extra_kwargs["model"] = expert.model
        if self.expert_temperature >= 0:
            extra_kwargs["temperature"] = self.expert_temperature

        # 可选：附带最近会话记录，让专家了解对话背景
        prompt = question + await self._get_recent_context(event, question)

        # 专家工具：可用时走 tool_loop_agent 完整工具循环
        tool_set = self._build_expert_tool_set()
        system_prompt = expert.system_prompt + (_EXPERT_TOOL_HINT if tool_set else "")
        if tool_set and extra_kwargs:
            # 框架的 agent 工具循环暂不透传 model/temperature（见 README）
            logger.debug(
                "工具模式下专家 '%s' 的 model/temperature 覆盖暂不生效",
                expert.display_name,
            )

        attempts = self.retry_on_failure + 1
        async with self._semaphore:
            for attempt in range(1, attempts + 1):
                try:
                    if tool_set is not None:
                        resp = await asyncio.wait_for(
                            self.context.tool_loop_agent(
                                event=event,
                                chat_provider_id=provider_id,
                                prompt=prompt,
                                system_prompt=system_prompt,
                                tools=tool_set,
                                max_steps=self.expert_max_tool_steps,
                            ),
                            timeout=self.expert_timeout,
                        )
                    else:
                        resp = await asyncio.wait_for(
                            self.context.llm_generate(
                                chat_provider_id=provider_id,
                                prompt=prompt,
                                system_prompt=system_prompt,
                                **extra_kwargs,
                            ),
                            timeout=self.expert_timeout,
                        )
                    break
                except asyncio.TimeoutError:
                    # 超时不重试：等待时间会成倍放大，直接取消
                    return (
                        f"{_ERROR_PREFIX} 专家 '{expert.display_name}' 超时"
                        f"（>{self.expert_timeout}s），本次咨询被取消。"
                    )
                except Exception as e:
                    if attempt < attempts:
                        logger.warning(
                            "咨询专家 '%s' 第 %d 次失败，准备重试：%s",
                            expert.display_name,
                            attempt,
                            e,
                        )
                        continue
                    logger.error(
                        "咨询专家 '%s' 失败（已重试 %d 次）",
                        expert.display_name,
                        attempt - 1,
                        exc_info=True,
                    )
                    return (
                        f"{_ERROR_PREFIX} 咨询专家 '{expert.display_name}' 时出错：{e}"
                    )
            else:  # pragma: no cover - 循环耗尽理论上不可达
                return f"{_ERROR_PREFIX} 咨询专家 '{expert.display_name}' 时出错。"

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
        if self.summary_provider_id:
            provider_id = self.summary_provider_id
        else:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        extra_kwargs: dict = {}
        if self.summary_model:
            extra_kwargs["model"] = self.summary_model
        resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=(
                    f"用户的问题：\n{question}\n\n以下是各位专家的独立意见：\n\n"
                    f"{opinions_text}"
                ),
                system_prompt=_PANEL_STYLES[self.panel_style],
                **extra_kwargs,
            ),
            timeout=self.expert_timeout * self.panel_timeout_multiplier,
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
        """List all experts in the cluster with their name, expertise and quota usage.
        Use this before consult_expert if unsure which expert fits the request."""
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
            tags_desc = f", tags: {','.join(e.tags)}" if e.tags else ""
            lines.append(
                f"- {e.name}（{e.display_name}）：{e.description} "
                f"[model: {model_desc}{tags_desc}, 本次对话已调用: "
                f"{used}/{self.max_calls_per_expert}]"
            )
        lines.append(
            f"\n本次对话咨询总配额：{self._get_total(event)}/"
            f"{self.max_consults_per_event}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # LLM 工具：search_experts / get_expert_usage
    # ------------------------------------------------------------------ #

    @llm_tool(name="search_experts")
    async def search_experts(self, event: AstrMessageEvent, keyword: str) -> str:
        """Search experts by keyword, matching expert name, display name, expertise
        description and tags. Use this to locate the right expert(s) when the cluster
        is large; convene_expert_panel accepts tags as participants too."""
        if not self.experts:
            return (
                f"{_ERROR_PREFIX} 专家集群为空。"
                "请提醒用户在 WebUI 插件配置中添加专家后再试。"
            )
        token = (keyword or "").strip().casefold()
        if not token:
            return f"{_ERROR_PREFIX} 关键词为空，请提供一个搜索词。"

        matched: list[str] = []
        for e in self.experts.values():
            haystack = " ".join(
                (e.name, e.display_name, e.description, e.model, " ".join(e.tags))
            ).casefold()
            if token in haystack:
                tags_desc = f"，标签 {','.join(e.tags)}" if e.tags else ""
                matched.append(
                    f"- {e.name}（{e.display_name}）：{e.description}{tags_desc}"
                )
        if not matched:
            all_names = ", ".join(e.name for e in self.experts.values())
            return f"没有专家匹配关键词 '{keyword}'。当前全部专家：{all_names}"

        tip = ""
        if any(e.tags for e in self.experts.values()):
            tip = "\n提示：convene_expert_panel 的参会名单可直接填写标签实现按组召集。"
        return (
            f"匹配 '{keyword}' 的专家共 {len(matched)} 位：\n"
            + "\n".join(matched)
            + tip
        )

    @llm_tool(name="get_expert_usage")
    async def get_expert_usage(self, event: AstrMessageEvent) -> str:
        """Get usage statistics for the expert cluster: overall conversation quota and
        per-expert call counts. Use this to check remaining quota before consult_expert
        or convene_expert_panel, especially before long panels."""
        counts = self._get_counts(event)
        lines = [
            f"本次对话总配额：{self._get_total(event)}/{self.max_consults_per_event}",
            "各专家用量：",
        ]
        for e in self.experts.values():
            used = counts.get(e.name.casefold(), 0)
            lines.append(
                f"- {e.name}: 本次对话 {used}/{self.max_calls_per_expert}"
                f"，累计 {e.total_calls} 次"
            )
        if not self.persist_stats:
            lines.append("（统计持久化已关闭，累计次数仅本次运行有效）")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # LLM 工具：consult_expert
    # ------------------------------------------------------------------ #

    @llm_tool(name="consult_expert")
    async def consult_expert(
        self, event: AstrMessageEvent, expert_name: str, question: str
    ) -> str:
        """Consult a specific expert from the cluster and get the expert's professional
        answer. Use this when the user's request needs specialized knowledge (legal,
        medical, coding, translation, etc.). Use list_experts first if unsure which
        expert to ask.

        IMPORTANT: If the result starts with "[EXPERT_ERROR]", the consultation failed at
        system level (quota, timeout, config...). Do NOT treat it as the expert's answer;
        answer by yourself or try another expert.

        Args:
            expert_name(string): The expert's name, exactly as listed by list_experts.
            question(string): A SELF-CONTAINED question. Include all necessary context and
            background - the expert cannot see this conversation.
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
        result = await self._ask_expert(event, expert, question)
        self._save_persisted_stats()
        return result

    # ------------------------------------------------------------------ #
    # LLM 工具：convene_expert_panel
    # ------------------------------------------------------------------ #

    @llm_tool(name="convene_expert_panel")
    async def convene_expert_panel(
        self, event: AstrMessageEvent, question: str, expert_names: str = ""
    ) -> str:
        """Convene an expert panel: consult several experts IN PARALLEL with the same
        question and return all their opinions. Then synthesize a final answer yourself
        from those opinions. Use this for complex questions that benefit from multiple
        professional perspectives.

        Note: each consulted expert counts toward per-expert and total quota. If some
        results start with "[EXPERT_ERROR]", synthesize based on the successful ones
        or answer by yourself.

        Args:
            question(string): A SELF-CONTAINED question with all necessary context.
            expert_names(string): Comma-separated expert names to convene,
            e.g. "coder,reviewer". Leave EMPTY to convene ALL experts.
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
            targets, unmatched = self._parse_panel_selector(expert_names, strict=True)
            if unmatched:
                available = ", ".join(e.name for e in self.experts.values())
                return (
                    f"{_ERROR_PREFIX} 以下名称既不是专家名也不是分组标签："
                    f"{', '.join(unmatched)}。可用专家：{available}"
                )
        else:
            # 留空时按 panel_default_experts 配置，再留空则为全体
            targets = self._resolve_panel_targets()

        if not targets:
            return f"{_ERROR_PREFIX} 参会专家名单为空。"
        if len(targets) > self.max_panel_size:
            targets = targets[: self.max_panel_size]

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

        results_raw = await asyncio.gather(
            *(_consult(e) for e in targets), return_exceptions=True
        )
        results = [r for r in results_raw if isinstance(r, tuple)]
        failed_n = len(results_raw) - len(results)
        self._save_persisted_stats()
        if not results:
            return (
                f"{_ERROR_PREFIX} 会议失败：{failed_n} 位专家全部因系统异常未能作答，"
                "请稍后重试或直接自行回答。"
            )
        logger.info(
            "专家会议结束：%s（事件累计 %d/%d）",
            "、".join(e.display_name for e, _ in results),
            self._get_total(event),
            self.max_consults_per_event,
        )
        header = (
            f"以下是 {len(results)} 位专家对同一问题的独立意见，"
            "请你综合后给出最终回答：\n\n"
        )
        if failed_n:
            header += (
                f"（另有 {failed_n} 位专家因系统异常未能给出意见，"
                "请仅基于以下成功意见综合。）\n\n"
            )
        return header + self._format_opinions(list(results))

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
            tags_desc = f"\n   标签：{', '.join(e.tags)}" if e.tags else ""
            lines.append(
                f"{i}. {e.display_name}（{e.name}）\n"
                f"   擅长：{e.description}\n"
                f"   模型：{model_desc}{tags_desc}\n"
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
        targets = self._resolve_panel_targets()[:quota_left]
        if len(targets) > self.max_panel_size:
            targets = targets[: self.max_panel_size]

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

        results_raw = await asyncio.gather(
            *(_consult(e) for e in targets), return_exceptions=True
        )
        results = [r for r in results_raw if isinstance(r, tuple)]
        failed_n = len(results_raw) - len(results)
        self._save_persisted_stats()
        if not results:
            yield event.plain_result(
                f"会议失败：{failed_n} 位专家全部因系统异常未能作答，请稍后重试喵。"
            )
            return
        failed_note = (
            f"\n（另有 {failed_n} 位专家因系统异常未能发言）" if failed_n else ""
        )
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
            f"参会：{'、'.join(e.display_name for e, _ in results)}{failed_note}"
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
        self._save_persisted_stats()
        total = sum(e.total_calls for e in self.experts.values())
        logger.info("专家集群插件已卸载，运行期间累计咨询 %d 次", total)
