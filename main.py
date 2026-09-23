from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import replace
from typing import Any, Iterable

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.star.filter.command import GreedyStr as QueryText
except ImportError:  # AstrBot 早期 4.x 兼容
    QueryText = str  # type: ignore[misc,assignment]

from .command_utils import recover_command_query
from .hextech_service import (
    AmbiguousChampion,
    ChampionNotFound,
    HextechError,
    HextechService,
)
from .models import (
    Augment,
    AugmentRecommendation,
    ChampionSummary,
    HeroReport,
    ItemRef,
    ServiceConfig,
)
from .renderer import ReportRenderer

PLUGIN_NAME = "astrbot_plugin_hextechmayhem"
_ROOT_COMMANDS = {"hextech", "海克斯科技"}
_HERO_COMMANDS = {"hero", "英雄", "海斗"}
_AUGMENT_COMMANDS = {"augment", "海克斯", "强化"}


@register(PLUGIN_NAME, "muyikk", "英雄联盟大乱斗与海克斯强化查询", "1.0.0")
class HextechMayhemPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config: dict[str, Any] = config or {}
        self.service = HextechService(ServiceConfig.from_mapping(self.config))
        self.renderer = ReportRenderer()
        self._translation_cache: dict[str, str] = {}
        if "max_interactions" in self.config and "max_augments_per_rarity" not in self.config:
            logger.warning(
                "配置项 max_interactions 已弃用；当前按 max_augments_per_rarity=5 展示每档海克斯"
            )

    async def initialize(self) -> None:
        await self.service.initialize()
        logger.info("Hextech Mayhem 插件初始化完成")

    @filter.command_group("hextech", alias={"海克斯科技"})
    def hextech():
        """英雄联盟大乱斗和海克斯强化查询。"""
        pass

    @hextech.command("help", alias={"帮助"})
    async def hextech_help(self, event: AstrMessageEvent):
        """显示插件帮助。"""
        yield event.plain_result(
            "海克斯乱斗插件命令\n"
            "/hextech hero <英雄> - 生成英雄大乱斗完整报告\n"
            "/hextech augment <关键词> - 搜索海克斯强化\n"
            "/hextech status - 查看各数据源和内存缓存状态\n\n"
            "支持中文混用：\n"
            "/海克斯科技 海斗 亚索\n"
            "/hextech 英雄 Miss Fortune\n"
            "/海克斯科技 海克斯 珠光护手\n"
            "/hextech 状态"
        )

    @hextech.command("hero", alias={"英雄", "海斗"})
    async def hextech_hero(self, event: AstrMessageEvent, query: QueryText = ""):
        """查询英雄，例如：/hextech hero 亚索。"""
        hero_query = extract_command_query(event, str(query), _HERO_COMMANDS)
        if not hero_query:
            yield event.plain_result("请输入英雄名，例如：/hextech hero 亚索")
            return
        try:
            champion, list_stale = await self._resolve_champion(hero_query)
            report = await self.service.hero_report(champion)
            if list_stale and "Data Dragon" not in report.stale_sources:
                report = replace(
                    report,
                    stale_sources=("Data Dragon", *report.stale_sources),
                )
            report = await self._translate_hero_wiki_notes(report)
            images = await self._hero_images(report)
            try:
                image_bytes = await self.renderer.hero_report(
                    self.html_render,
                    report,
                    images,
                )
            except Exception as error:
                logger.exception("英雄报告渲染失败，降级为文字：%s", error)
                yield event.plain_result(format_hero_text(report))
                return
            yield event.chain_result([image_from_bytes(image_bytes)])
        except (ChampionNotFound, AmbiguousChampion, HextechError) as error:
            yield event.plain_result(str(error))
        except asyncio.TimeoutError:
            yield event.plain_result("查询超时，请稍后重试或调大请求超时配置。")
        except Exception as error:
            logger.exception("英雄查询失败：%s", error)
            yield event.plain_result("英雄查询失败，详情请查看 AstrBot 日志。")

    @hextech.command("augment", alias={"海克斯", "强化"})
    async def hextech_augment(self, event: AstrMessageEvent, query: QueryText = ""):
        """搜索海克斯，例如：/hextech augment 珠光。"""
        augment_query = extract_command_query(event, str(query), _AUGMENT_COMMANDS)
        if not augment_query:
            yield event.plain_result("请输入海克斯名称，例如：/hextech augment 珠光护手")
            return
        try:
            augments, total, stale = await self.service.search_augments(augment_query)
            if not augments:
                yield event.plain_result(f"没有找到与“{augment_query}”相关的海克斯强化。")
                return
            augments = await self._translate_augment_wiki_notes(augments)
            images = await self._augment_images(augments)
            try:
                image_bytes = await self.renderer.augment_report(
                    self.html_render,
                    augments,
                    images,
                    query=augment_query,
                    total=total,
                    stale=stale,
                )
            except Exception as error:
                logger.exception("海克斯卡片渲染失败，降级为文字：%s", error)
                yield event.plain_result(
                    format_augment_text(
                        augments,
                        query=augment_query,
                        total=total,
                        stale=stale,
                    )
                )
                return
            yield event.chain_result([image_from_bytes(image_bytes)])
        except HextechError as error:
            yield event.plain_result(str(error))
        except asyncio.TimeoutError:
            yield event.plain_result("查询超时，请稍后重试或调大请求超时配置。")
        except Exception as error:
            logger.exception("海克斯查询失败：%s", error)
            yield event.plain_result("海克斯查询失败，详情请查看 AstrBot 日志。")

    @hextech.command("status", alias={"状态"})
    async def hextech_status(self, event: AstrMessageEvent):
        """查看实时数据与内存缓存状态。"""
        status = self.service.status()
        lines = [
            "海克斯乱斗插件运行正常",
            f"Data Dragon 版本：{status['champion_version']}",
            f"英雄列表：{status['champions']} 个｜英雄详情缓存：{status['details']} 个",
            "数据源：",
        ]
        for name, source in status["sources"].items():
            age = format_age(source["age"])
            suffix = f"｜{source['error']}" if source["error"] else ""
            lines.append(f"- {name}：{source['state']}（{age}）{suffix}")
        lines.extend(
            [
                f"图片内存缓存：{status['images']} 个",
                f"缓存有效期：{status['cache_ttl']} 秒",
                "所有缓存仅保存在当前进程内，插件重启后会清空。",
            ]
        )
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        await self.service.close()
        self._translation_cache.clear()
        logger.info("Hextech Mayhem 插件已停止")

    async def _resolve_champion(self, query: str) -> tuple[ChampionSummary, bool]:
        try:
            return await self.service.find_champion(query)
        except ChampionNotFound as original_error:
            if not str(self.config.get("llm_provider_id") or "").strip():
                raise
            normalized = await self._normalize_hero_name(query)
            if not normalized:
                raise original_error
            for candidate in normalized:
                try:
                    return await self.service.find_champion(candidate)
                except (ChampionNotFound, AmbiguousChampion):
                    continue
            raise original_error

    async def _normalize_hero_name(self, query: str) -> list[str]:
        provider = self._llm_provider()
        if provider is None:
            logger.warning("没有可用的 LLM Provider，跳过英雄别名识别")
            return []
        safe_query = str(query)[:100]
        prompt = (
            "识别下面的《英雄联盟》英雄别名、玩家黑话或数字代称。"
            "只返回严格 JSON，格式为："
            '{"names":["标准中文英雄名","官方英文名","英雄内部ID"]}。'
            "不要输出解释、Markdown 或 URL。\n"
            f"用户输入：{safe_query}"
        )
        try:
            response = await provider.text_chat(prompt=prompt, contexts=[])
            payload = parse_llm_json(str(getattr(response, "completion_text", "") or ""))
            names = payload.get("names", [])
            if not isinstance(names, list):
                return []
            return [str(item).strip() for item in names[:6] if str(item).strip()]
        except Exception as error:
            logger.warning("LLM 英雄别名识别失败：%s", error)
            return []

    async def _translate_hero_wiki_notes(self, report: HeroReport) -> HeroReport:
        ranked = sorted(
            (item for values in report.augments.values() for item in values),
            key=lambda item: item.rank,
        )[:3]
        notes = {item.augment.id: item.augment.wiki_note_en for item in ranked if item.augment.wiki_note_en}
        translated = await self._translate_notes(notes)
        if not translated:
            return report
        groups: dict[str, tuple[AugmentRecommendation, ...]] = {}
        for tier, values in report.augments.items():
            groups[tier] = tuple(
                replace(item, augment=replace(item.augment, mechanism=translated[item.augment.id]))
                if item.augment.id in translated
                else item
                for item in values
            )
        return replace(report, augments=groups)

    async def _translate_augment_wiki_notes(self, augments: list[Augment]) -> list[Augment]:
        notes = {item.id: item.wiki_note_en for item in augments if item.wiki_note_en}
        translated = await self._translate_notes(notes)
        return [
            replace(item, mechanism=translated[item.id]) if item.id in translated else item
            for item in augments
        ]

    async def _translate_notes(self, notes: dict[str, str]) -> dict[str, str]:
        if not notes:
            return {}
        result = {
            key: self._translation_cache[value]
            for key, value in notes.items()
            if value in self._translation_cache
        }
        pending = {key: value for key, value in notes.items() if value not in self._translation_cache}
        if not pending:
            return result
        provider = self._llm_provider()
        if provider is None:
            logger.warning("没有可用的 LLM Provider，跳过 Wiki 机制翻译")
            return result
        payload = [{"key": key, "text": text[:1800]} for key, text in pending.items()]
        prompt = (
            "将以下 League of Legends Wiki 机制说明忠实翻译为简洁中文。"
            "不得添加原文没有的数值、结论或攻略。只返回严格 JSON："
            '{"items":[{"key":"原 key","text":"中文翻译"}]}。'
            "不要输出 Markdown 或解释。\n输入："
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            response = await provider.text_chat(prompt=prompt, contexts=[])
            parsed = parse_llm_json(str(getattr(response, "completion_text", "") or ""))
            items = parsed.get("items", [])
            if not isinstance(items, list):
                return result
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "")
                text = str(item.get("text") or "").strip()[:1200]
                if key in pending and text:
                    self._translation_cache[pending[key]] = text
                    result[key] = text
        except Exception as error:
            logger.warning("LLM Wiki 机制翻译失败：%s", error)
        return result

    def _llm_provider(self):
        provider_id = str(self.config.get("llm_provider_id") or "").strip()
        if not provider_id or not hasattr(self.context, "get_provider_by_id"):
            return None
        return self.context.get_provider_by_id(provider_id)

    async def _hero_images(self, report: HeroReport) -> dict[str, str]:
        champion = report.champion
        urls = [
            champion.splash_url,
            champion.splash_fallback_url,
            champion.icon_url,
            champion.icon_fallback_url,
        ]
        urls.extend(
            recommendation.augment.icon_url
            for values in report.augments.values()
            for recommendation in values
        )
        for option in (*report.summoner_spells, *report.starter_items, *report.core_builds):
            urls.extend(_item_urls(option.items))
        urls.extend(_item_urls(report.boots))
        for route in report.full_builds:
            urls.extend(_item_urls(route.items))
        return await self._load_images(urls)

    async def _augment_images(self, augments: list[Augment]) -> dict[str, str]:
        return await self._load_images([item.icon_url for item in augments])

    async def _load_images(self, urls: Iterable[str]) -> dict[str, str]:
        unique = [url for url in dict.fromkeys(urls) if url]
        values = await asyncio.gather(
            *(self.service.image_data_uri(url) for url in unique),
            return_exceptions=True,
        )
        return {
            url: value
            for url, value in zip(unique, values)
            if isinstance(value, str) and value
        }


def extract_command_query(event: AstrMessageEvent, fallback: str, subcommands: set[str]) -> str:
    """Recover the complete query on older AstrBot versions without GreedyStr."""
    raw = ""
    if hasattr(event, "get_extra"):
        try:
            raw = str(event.get_extra("astrbot_original_message_str") or "")
        except Exception:
            raw = ""
    raw = raw or str(getattr(event, "message_str", "") or "")
    return recover_command_query(raw, fallback, _ROOT_COMMANDS, subcommands)


def parse_llm_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    elif "{" in text and "}" in text:
        text = text[text.find("{") : text.rfind("}") + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("LLM 返回值不是 JSON 对象")
    return value


def image_from_bytes(data: bytes):
    encoded = base64.b64encode(data).decode("ascii")
    if hasattr(Comp.Image, "fromBase64"):
        return Comp.Image.fromBase64(encoded)
    return Comp.Image(file=f"base64://{encoded}")


def format_hero_text(report: HeroReport) -> str:
    detail = report.champion
    suffix = f"｜旧缓存：{'、'.join(report.stale_sources)}" if report.stale_sources else ""
    lines = [
        f"{detail.name} · {detail.title}",
        f"英文 ID：{detail.id}｜Riot 版本：{detail.version}｜OP.GG 版本：{report.patch or '未知'}｜{report.tier or '段位未知'}{suffix}",
        detail.lore,
        "",
        "海克斯推荐（OP.GG 排序，CommunityDragon 资料）：",
    ]
    for tier in ("Prismatic", "Gold", "Silver"):
        values = report.augments.get(tier, ())
        lines.append(f"{_tier_text(tier)}：")
        if values:
            for item in values:
                stats = _stats_text(item)
                line = f"- #{item.rank} {item.augment.name_zh}{stats}：{item.augment.description}"
                if item.augment.mechanism:
                    line += f"｜机制：{item.augment.mechanism}"
                lines.append(line)
        else:
            lines.append("- 暂无统计推荐")
    lines.extend(["", "召唤师技能：", *_option_text(report.summoner_spells)])
    lines.append("技能加点顺序：" + (" → ".join(report.skill_order) if report.skill_order else "暂无数据"))
    lines.extend(["出门装：", *_option_text(report.starter_items)])
    lines.append("鞋子：" + (_items_text(report.boots) if report.boots else "暂无数据"))
    lines.extend(["核心装备：", *_option_text(report.core_builds)])
    lines.append("完整六件套（Mayhempedia 社区攻略）：")
    if report.full_builds:
        for route in report.full_builds:
            lines.append(f"- {route.name}：{_items_text(route.items)}")
            if route.note:
                lines.append(f"  {route.note}")
    else:
        lines.append("- 暂无社区六件套；未使用推导组合补齐。")
    if report.unavailable_sources:
        lines.append("暂不可用来源：" + "、".join(report.unavailable_sources))
    return "\n".join(lines)


def format_augment_text(
    augments: list[Augment],
    *,
    query: str,
    total: int,
    stale: bool,
) -> str:
    lines = [
        f"海克斯搜索：{query}（共 {total} 条，展示 {len(augments)} 条）"
        f"{'｜缓存数据' if stale else ''}"
    ]
    for index, item in enumerate(augments, start=1):
        line = f"{index}. {item.name_zh}（{item.name_en or item.api_name or '无英文名'}）｜{_tier_text(item.tier)}\n{item.description}"
        if item.mechanism:
            line += f"\n机制补充：{item.mechanism}"
        lines.append(line)
    return "\n\n".join(lines)


def format_age(value: int | None) -> str:
    if value is None:
        return "未加载"
    if value < 60:
        return f"{value} 秒前更新"
    return f"{value // 60} 分钟前更新"


def _item_urls(items: Iterable[ItemRef]) -> list[str]:
    return [
        url
        for item in items
        for url in (item.icon_url, item.fallback_icon_url, item.extra_icon_url)
        if url
    ]


def _items_text(items: Iterable[ItemRef]) -> str:
    return " → ".join(item.name for item in items)


def _option_text(options) -> list[str]:
    values = list(options)
    return [f"- {_items_text(option.items)}" for option in values] if values else ["- 暂无数据"]


def _stats_text(item: AugmentRecommendation) -> str:
    values = []
    if item.win_rate:
        values.append(f"胜率 {item.win_rate}")
    if item.pick_rate:
        values.append(f"选择率 {item.pick_rate}")
    if item.games:
        values.append(f"{item.games} 场")
    return f"（{'，'.join(values)}）" if values else ""


def _tier_text(value: str) -> str:
    return {"Prismatic": "棱彩阶", "Gold": "黄金阶", "Silver": "白银阶"}.get(value, value)
