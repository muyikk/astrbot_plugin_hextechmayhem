from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

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
_HERO_COMMANDS = {"海斗"}
_AUGMENT_COMMANDS = {"海克斯"}


@register(PLUGIN_NAME, "muyikk", "英雄联盟大乱斗与海克斯强化查询", "1.1.0")
class HextechMayhemPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config: dict[str, Any] = config or {}
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.service = HextechService(ServiceConfig.from_mapping(self.config), data_dir)
        self.renderer = ReportRenderer()
        self._translation_cache: dict[str, str] = {}
        if "max_interactions" in self.config and "max_augments_per_rarity" not in self.config:
            logger.warning(
                "配置项 max_interactions 已弃用；当前按 max_augments_per_rarity=5 展示每档海克斯"
            )

    async def initialize(self) -> None:
        await self.service.initialize()
        logger.info("Hextech Mayhem 插件初始化完成，数据目录：%s", self.service.data_dir)

    @filter.command("海斗")
    async def hero_query(self, event: AstrMessageEvent, query: QueryText = ""):
        """查询英雄，例如：/海斗 亚索。"""
        hero_query = extract_command_query(event, str(query), _HERO_COMMANDS)
        if not hero_query:
            yield event.plain_result("请输入英雄名，例如：/海斗 亚索")
            return
        try:
            champion, list_stale = await self._resolve_champion(hero_query)
            report = await self.service.hero_report(champion)
            if list_stale and "Data Dragon" not in report.stale_sources:
                report = replace(
                    report,
                    stale_sources=("Data Dragon", *report.stale_sources),
                )
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

    @filter.command("海克斯")
    async def augment_query(self, event: AstrMessageEvent, query: QueryText = ""):
        """搜索海克斯，例如：/海克斯 珠光护手。"""
        augment_query = extract_command_query(event, str(query), _AUGMENT_COMMANDS)
        if not augment_query:
            yield event.plain_result("请输入海克斯名称，例如：/海克斯 珠光护手")
            return
        try:
            augments, total, stale = await self.service.search_augments(augment_query)
            if not augments:
                yield event.plain_result(f"没有找到与“{augment_query}”相关的海克斯强化。")
                return
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

    @filter.command("海斗排名")
    async def hero_rankings(self, event: AstrMessageEvent):
        """查询海克斯大乱斗英雄胜率排名。"""
        try:
            rankings, stale = await self.service.champion_rankings(10)
            source = self.service.config.mayhem_source
            images = await self._load_images([item.icon_url for item in rankings])
            try:
                image_bytes = await self.renderer.ranking_report(
                    self.html_render,
                    rankings,
                    images,
                    source=source,
                    stale=stale,
                )
            except Exception as error:
                logger.exception("排行榜卡片渲染失败，降级为文字：%s", error)
                yield event.plain_result(
                    format_champion_rankings(rankings, stale=stale, source=source)
                )
                return
            yield event.chain_result([image_from_bytes(image_bytes)])
        except HextechError as error:
            yield event.plain_result(str(error))
        except asyncio.TimeoutError:
            yield event.plain_result("查询超时，请稍后重试或调大请求超时配置。")
        except Exception as error:
            logger.exception("英雄胜率排名查询失败：%s", error)
            yield event.plain_result("英雄胜率排名查询失败，详情请查看 AstrBot 日志。")

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
        for variant in report.build_variants:
            for option in (
                *variant.summoner_spells,
                *variant.starter_items,
                *variant.core_items,
                *variant.situational_items,
            ):
                urls.extend(_item_urls(option.items))
        urls.extend(
            augment.icon_url
            for trio in report.augment_trios
            for augment in trio.augments
            if augment.icon_url
        )
        urls.extend(_item_urls(tuple(item.item for item in report.item_performance)))
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
    return recover_command_query(raw, fallback, set(), subcommands)


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
        f"英文 ID：{detail.id}｜Riot 版本：{detail.version}｜海斗版本：{report.patch or '未知'}｜{report.tier or '段位未知'}{suffix}",
        "",
        "海克斯推荐（海斗信息源排序，ARAMGG 资料）：",
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
    if report.build_variants:
        lines.append("流派与完整技能方案：")
        for variant in report.build_variants:
            lines.append(f"- {variant.name}{_stats_values(variant.win_rate, variant.pick_rate, variant.games)}")
            for skill in variant.skill_orders:
                lines.append(
                    "  技能："
                    + " → ".join(skill.order)
                    + _stats_values(skill.win_rate, skill.pick_rate, skill.games)
                )
            if variant.summoner_spells:
                lines.append("  召唤师技能：" + "；".join(_items_text(option.items) for option in variant.summoner_spells))
            if variant.starter_items:
                lines.append("  出门装：" + "；".join(_items_text(option.items) for option in variant.starter_items))
            if variant.core_items:
                lines.append("  核心装备：" + "；".join(_items_text(option.items) for option in variant.core_items))
            if variant.situational_items:
                lines.append("  情境装备：" + "；".join(_items_text(option.items) for option in variant.situational_items))
    if report.augment_trios:
        lines.append("三海克斯组合：")
        for trio in report.augment_trios:
            lines.append(
                "- "
                + " + ".join(item.name_zh for item in trio.augments)
                + _stats_values(trio.win_rate, trio.pick_rate, trio.games)
            )
    if report.item_performance:
        lines.append("热门单件表现：")
        for item in report.item_performance:
            lines.append(
                f"- {item.item.name}"
                + _stats_values(item.win_rate, item.pick_rate, item.games)
            )
    lines.append("完整六件套（Mayhempedia 社区攻略）：")
    if report.full_builds:
        for route in report.full_builds:
            lines.append(f"- {route.name}：{_items_text(route.items)}")
            if route.note:
                lines.append(f"  {route.note}")
    else:
        lines.append("- 暂无社区六件套；未使用推导组合补齐。")
    if report.provenance:
        lines.append("数据说明：" + "；".join(report.provenance))
    if report.related_articles:
        lines.append("相关文章：" + "；".join(item.title for item in report.related_articles))
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


def format_champion_rankings(
    rankings: list[ChampionSummary], *, stale: bool, source: str
) -> str:
    patch = next((item.stats_patch for item in rankings if item.stats_patch), "未知")
    date = next((item.stats_date for item in rankings if item.stats_date), "")
    stats_source = next((item.stats_source for item in rankings if item.stats_source), source)
    region = next((item.stats_region for item in rankings if item.stats_region), "")
    source_label = "OP.GG" if source == "opgg" else "ARAMGG"
    patch_label = f"｜版本 {patch}" if patch != "未知" else ""
    metadata = "｜".join(
        value
        for value in (
            date,
            _ranking_source_label(stats_source),
            _ranking_region_label(region),
        )
        if value
    )
    lines = [
        f"海克斯大乱斗英雄综合强度排名｜{source_label}{patch_label}"
        f"{'｜' + metadata if metadata else ''}{'｜缓存数据' if stale else ''}"
    ]
    for index, item in enumerate(rankings, start=1):
        details = []
        if item.stats_tier:
            details.append(f"T{item.stats_tier}")
        if item.win_rate is not None:
            details.append(f"胜率 {item.win_rate * 100:.2f}%")
        if item.pick_rate is not None:
            details.append(f"登场率 {item.pick_rate * 100:.2f}%")
        if item.games is not None:
            details.append(f"{item.games:,} 场")
        if item.tags:
            details.append("/".join(_ranking_role_label(value) for value in item.tags))
        if item.rank_delta:
            details.append(f"排名变化 {item.rank_delta}")
        rank = item.stats_rank or index
        title = f"（{item.title}）" if item.title else ""
        suffix = f"｜{'｜'.join(details)}" if details else ""
        lines.append(f"{rank}. {item.name}{title}{suffix}")
    return "\n".join(lines)


def _ranking_role_label(value: str) -> str:
    return {
        "fighter": "战士",
        "mage": "法师",
        "tank": "坦克",
        "marksman": "射手",
        "support": "辅助",
        "assassin": "刺客",
    }.get(str(value).casefold(), str(value))


def _ranking_source_label(value: str) -> str:
    return {
        "tencent": "腾讯公开快照",
        "iesdev": "ARAMGG Build 统计",
        "aramgg-client-upload": "ARAMGG 客户端匿名上传",
        "opgg": "OP.GG",
    }.get(str(value).casefold(), str(value))


def _ranking_region_label(value: str) -> str:
    return {"cn": "国服", "world": "全球"}.get(str(value).casefold(), str(value))


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
    return [f"- {_items_text(option.items)}{_loadout_stats_text(option)}" for option in values] if values else ["- 暂无数据"]


def _loadout_stats_text(option) -> str:
    values = []
    if option.win_rate:
        values.append(f"胜率 {option.win_rate}")
    if option.pick_rate:
        values.append(f"登场率 {option.pick_rate}")
    if option.games:
        values.append(f"{option.games} 场")
    return f"（{'，'.join(values)}）" if values else ""


def _stats_values(win_rate: str, pick_rate: str, games: str) -> str:
    values = []
    if win_rate:
        values.append(f"胜率 {win_rate}")
    if pick_rate:
        values.append(f"登场率 {pick_rate}")
    if games:
        values.append(f"{games} 场")
    return f"（{'，'.join(values)}）" if values else ""


def _stats_text(item: AugmentRecommendation) -> str:
    values = []
    if item.win_rate:
        values.append(f"胜率 {item.win_rate}")
    if item.pick_rate:
        values.append(f"登场率 {item.pick_rate}")
    if item.games:
        values.append(f"{item.games} 场")
    return f"（{'，'.join(values)}）" if values else ""


def _tier_text(value: str) -> str:
    return {"Prismatic": "棱彩阶", "Gold": "黄金阶", "Silver": "白银阶"}.get(value, value)
