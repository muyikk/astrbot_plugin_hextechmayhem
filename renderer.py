from __future__ import annotations

import base64
import binascii
import html
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import unquote, urlparse

from PIL import Image as PILImage

from .models import (
    Augment,
    AugmentRecommendation,
    FullBuildRoute,
    HeroReport,
    ItemRef,
    LoadoutOption,
)

RenderCallable = Callable[..., Awaitable[Any]]

_TIER_LABELS = {
    "Prismatic": "棱彩阶",
    "Gold": "黄金阶",
    "Silver": "白银阶",
    "Bronze": "青铜阶",
}


class ReportRenderer:
    width = 1120

    async def hero_report(
        self,
        render: RenderCallable,
        report: HeroReport,
        images: dict[str, str],
    ) -> bytes:
        return await self._render(render, self.hero_html(report, images))

    async def augment_report(
        self,
        render: RenderCallable,
        augments: list[Augment],
        images: dict[str, str],
        *,
        query: str,
        total: int,
        stale: bool,
    ) -> bytes:
        return await self._render(
            render,
            self.augment_html(
                augments,
                images,
                query=query,
                total=total,
                stale=stale,
            ),
        )

    def hero_html(self, report: HeroReport, images: dict[str, str]) -> str:
        champion = report.champion
        splash = _first_image(images, champion.splash_url, champion.splash_fallback_url)
        splash_style = (
            "background-image:linear-gradient(90deg,rgba(5,10,20,.16),"
            f"rgba(5,10,20,.96)),url('{html.escape(splash, quote=True)}')"
            if splash.startswith("data:image/")
            else ""
        )
        badges = []
        if report.patch:
            badges.append(f'<span class="badge">PATCH {_safe(report.patch)}</span>')
        if report.tier:
            badges.append(f'<span class="badge tier-rank">{_safe(report.tier)}</span>')
        for source in report.stale_sources:
            badges.append(f'<span class="badge stale">{_safe(source)} 旧缓存</span>')

        unavailable = ""
        if report.unavailable_sources:
            unavailable = (
                '<div class="source-warning">暂不可用：'
                + _safe("、".join(report.unavailable_sources))
                + "；其余模块仍为可用数据。</div>"
            )

        augment_columns = []
        for tier in ("Prismatic", "Gold", "Silver"):
            items = report.augments.get(tier, ())
            cards = "".join(_recommendation_card(item, images) for item in items)
            if not cards:
                cards = '<div class="mini-empty">暂无统计推荐</div>'
            augment_columns.append(
                f'<div class="augment-column tier-{_tier_class(tier)}">'
                f'<h3>{_safe(_tier_label(tier))}<span>{len(items)}</span></h3>{cards}</div>'
            )

        mechanisms = []
        ranked = sorted(
            (item for values in report.augments.values() for item in values),
            key=lambda item: item.rank,
        )
        for item in ranked[:3]:
            if item.augment.mechanism:
                mechanisms.append(
                    '<div class="mechanism-card">'
                    f'<b>{_safe(item.augment.name_zh)}</b>'
                    f'<span>{_safe(item.augment.mechanism)}</span></div>'
                )
        mechanism_section = (
            '<section><div class="section-title"><h2>机制补充</h2>'
            '<span>League Wiki · LLM 忠实翻译</span></div>'
            f'<div class="mechanism-grid">{"".join(mechanisms)}</div></section>'
            if mechanisms
            else ""
        )

        overview_cards = [
            _option_module("召唤师技能", report.summoner_spells, images, "最多 2 套"),
            _skill_order_module(report.skill_order),
            _option_module("出门装", report.starter_items, images, "最多 2 套"),
            _items_module("鞋子", report.boots, images, "最多 2 个"),
        ]
        core_cards = _option_module("核心装备", report.core_builds, images, "OP.GG 统计 · 最多 3 套", wide=True)
        routes = "".join(_route_card(route, images) for route in report.full_builds)
        if not routes:
            routes = '<div class="empty">暂无社区六件套；不会使用推导组合补齐。</div>'

        return self._document(
            title=f"{champion.name} · 海克斯乱斗",
            body=f"""
<section class="hero" style="{splash_style}">
  <div class="eyebrow">HEXTECH MAYHEM</div>
  <h1>{_safe(champion.name)}</h1>
  <div class="subtitle">{_safe(champion.title)} · {_safe(champion.id)}</div>
  <div class="badges">{''.join(badges)}</div>
  <p class="lore">{_safe(champion.lore)}</p>
</section>
{unavailable}
<section>
  <div class="section-title"><h2>海克斯推荐</h2><span>OP.GG 排序 · CommunityDragon 资料</span></div>
  <div class="augment-grid">{''.join(augment_columns)}</div>
</section>
{mechanism_section}
<section>
  <div class="section-title"><h2>对局配置</h2><span>OP.GG 公开统计</span></div>
  <div class="module-grid">{''.join(overview_cards)}</div>
  {core_cards}
</section>
<section>
  <div class="section-title"><h2>完整六件套</h2><span>Mayhempedia 社区攻略 · 最多 3 套</span></div>
  <div class="route-list">{routes}</div>
</section>
<footer>英雄：Riot Data Dragon · 海克斯：CommunityDragon / League Wiki · 统计：OP.GG · 社区路线：Mayhempedia · 非官方插件</footer>
""",
        )

    def augment_html(
        self,
        augments: list[Augment],
        images: dict[str, str],
        *,
        query: str,
        total: int,
        stale: bool,
    ) -> str:
        cards = []
        for item in augments:
            image = _image_tag(images.get(item.icon_url, ""), item.name_zh, "large-icon")
            mechanism = (
                '<div class="mechanism"><b>机制补充</b>'
                f"{_safe(item.mechanism)}</div>"
                if item.mechanism
                else ""
            )
            cards.append(
                f'<article class="search-card tier-{_tier_class(item.tier)}">'
                f'{image}<div class="search-copy"><div class="augment-head">'
                f'<span class="augment-name big">{_safe(item.name_zh)}</span>'
                f'<span class="tier-pill">{_safe(_tier_label(item.tier))}</span></div>'
                f'<div class="en-name">{_safe(item.name_en or item.api_name)}</div>'
                f'<div class="description">{_safe(item.description)}</div>{mechanism}'
                "</div></article>"
            )
        stale_badge = '<span class="badge stale">旧缓存</span>' if stale else ""
        return self._document(
            title=f"{query} · 海克斯搜索",
            body=f"""
<header class="search-header">
  <div class="eyebrow">HEXTECH AUGMENT SEARCH {stale_badge}</div>
  <h1>“{_safe(query)}”</h1>
  <div class="subtitle">找到 {total} 条，展示前 {len(augments)} 条</div>
</header>
<main class="search-list">{''.join(cards)}</main>
<footer>海克斯资料：CommunityDragon · 机制补充：League Wiki / LLM · 非官方插件</footer>
""",
        )

    async def _render(self, render: RenderCallable, source_html: str) -> bytes:
        options = {
            "full_page": True,
            "type": "png",
            "scale": "device",
            "device_scale_factor_level": "normal",
        }
        result = await render(source_html, {}, False, options)
        return normalize_render_result(result)

    def _document(self, *, title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width={self.width}"><title>{_safe(title)}</title>
<style>
* {{ box-sizing:border-box; }}
html,body {{ margin:0;width:{self.width}px;background:#050914;color:#eef5ff; }}
body {{ font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif;padding:32px; }}
body::before {{ content:"";position:fixed;inset:0;pointer-events:none;background:
radial-gradient(circle at 86% 4%,rgba(33,181,255,.17),transparent 29%),
radial-gradient(circle at 3% 42%,rgba(141,72,255,.13),transparent 28%); }}
section,.search-header {{ position:relative;background:linear-gradient(145deg,rgba(17,28,48,.97),rgba(8,14,27,.98));border:1px solid #203858;border-radius:22px;padding:26px;margin-bottom:20px;box-shadow:0 18px 50px rgba(0,0,0,.34);overflow:hidden; }}
.hero {{ min-height:330px;background-size:cover;background-position:center;display:flex;flex-direction:column;justify-content:flex-end; }}
.eyebrow {{ color:#65d8ff;letter-spacing:2.1px;font-size:14px;font-weight:800; }}
h1 {{ margin:9px 0 2px;font-size:54px;line-height:1.04; }}
h2 {{ margin:0;color:#d7eaff;font-size:24px; }}
h3 {{ margin:0 0 12px;font-size:18px;display:flex;justify-content:space-between; }}
h3 span {{ color:#6e88a8;font-size:12px; }}
.subtitle,.en-name {{ color:#91a9c5; }}
.lore {{ max-width:800px;color:#d0d9e7;line-height:1.65;font-size:15px; }}
.badges {{ display:flex;gap:8px;flex-wrap:wrap;margin-top:12px; }}
.badge,.tier-pill {{ display:inline-block;padding:4px 9px;border-radius:999px;color:#d3e5f7;background:#17283c;border:1px solid #294662;font-size:12px;font-weight:800; }}
.tier-rank {{ color:#75e6ff; }} .stale {{ color:#ffd47b;background:rgba(255,180,40,.12);border-color:rgba(255,190,70,.35); }}
.source-warning {{ position:relative;margin:-4px 0 20px;padding:13px 18px;border-radius:13px;color:#ffd99a;background:#2b2112;border:1px solid #684b1f; }}
.section-title {{ display:flex;align-items:baseline;justify-content:space-between;margin-bottom:18px; }}
.section-title>span {{ color:#6683a5;font-size:13px; }}
.augment-grid {{ display:grid;grid-template-columns:repeat(3,1fr);gap:13px; }}
.augment-column {{ padding:15px;border:1px solid #263c59;border-top-width:4px;border-radius:16px;background:rgba(7,14,27,.72); }}
.tier-prismatic {{ border-top-color:#b47cff!important; }} .tier-gold {{ border-top-color:#ffca55!important; }} .tier-silver {{ border-top-color:#a9bfd4!important; }}
.recommendation {{ display:flex;gap:10px;align-items:center;padding:9px 0;border-top:1px solid rgba(76,103,136,.25); }}
.recommendation:first-of-type {{ border-top:0; }}
.rank {{ width:22px;color:#6f8dab;font-size:12px;font-weight:800; }}
.augment-icon,.item-icon {{ width:42px;height:42px;border-radius:10px;object-fit:cover;background:#101b2c;flex:0 0 auto; }}
.rec-copy {{ min-width:0;flex:1; }} .rec-name {{ font-size:14px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }}
.stats {{ color:#7790aa;font-size:11px;margin-top:3px; }}
.mini-empty,.empty {{ color:#8198b2;padding:22px;text-align:center;border:1px dashed #29435f;border-radius:12px; }}
.mechanism-grid {{ display:grid;gap:10px; }} .mechanism-card {{ display:grid;grid-template-columns:170px 1fr;gap:14px;padding:14px;border-radius:13px;background:rgba(255,184,60,.07);border:1px solid rgba(255,184,60,.2); }}
.mechanism-card b {{ color:#ffd079; }} .mechanism-card span {{ color:#d9c8aa;line-height:1.5; }}
.module-grid {{ display:grid;grid-template-columns:repeat(2,1fr);gap:13px;margin-bottom:13px; }}
.module {{ border:1px solid #223a59;background:rgba(8,15,28,.7);border-radius:15px;padding:15px; }}
.module.wide {{ margin-top:13px; }} .module-head {{ display:flex;justify-content:space-between;margin-bottom:11px; }} .module-head b {{ color:#dcecff; }} .module-head span {{ color:#68809d;font-size:12px; }}
.option {{ display:flex;align-items:center;gap:8px;padding:9px 0;border-top:1px solid rgba(70,97,130,.25); }} .option:first-of-type {{ border-top:0; }}
.items {{ display:flex;align-items:center;gap:7px;flex-wrap:wrap; }} .item {{ display:grid;justify-items:center;gap:4px;max-width:76px; }} .item-name {{ color:#aebed0;font-size:10px;text-align:center;line-height:1.2; }}
.arrow {{ color:#49627f;font-weight:900; }} .skill-order {{ color:#a8e8ff;font-size:22px;font-weight:900;letter-spacing:3px;padding:13px 3px; }}
.route-list {{ display:grid;gap:12px; }} .route {{ border:1px solid #2a405d;background:rgba(8,15,28,.72);border-radius:15px;padding:16px; }}
.route-head {{ display:flex;justify-content:space-between;align-items:center;margin-bottom:12px; }} .route-name {{ font-weight:900;color:#f3d58b;font-size:17px; }} .route-source {{ color:#6d86a4;font-size:11px; }} .route-note {{ color:#9fb1c5;line-height:1.45;font-size:13px;margin-top:11px; }}
.image-placeholder {{ display:grid;place-items:center;color:#55718e;border:1px dashed #29435f;background:#0b1423;font-weight:800; }}
.augment-icon.image-placeholder,.item-icon.image-placeholder {{ width:42px;height:42px;border-radius:10px; }}
.large-icon {{ width:84px;height:84px;border-radius:18px;object-fit:cover;background:#101b2c; }} .large-icon.image-placeholder {{ width:84px;height:84px;border-radius:18px; }}
.search-list {{ position:relative;display:grid;gap:13px; }} .search-card {{ display:flex;gap:16px;align-items:flex-start;border:1px solid #223a59;border-left-width:5px;background:rgba(8,15,28,.75);border-radius:16px;padding:20px; }}
.search-copy {{ min-width:0;flex:1; }} .augment-head {{ display:flex;align-items:center;gap:10px;flex-wrap:wrap; }} .augment-name.big {{ font-size:23px;font-weight:900; }}
.description {{ color:#d5deeb;font-size:16px;line-height:1.55;margin-top:10px; }} .en-name {{ margin-top:4px;font-size:13px; }}
.mechanism {{ margin-top:13px;padding:12px 14px;border-radius:12px;line-height:1.55;color:#f4d9a2;background:rgba(255,184,60,.08);border:1px solid rgba(255,184,60,.22); }} .mechanism b {{ display:block;color:#ffc967;margin-bottom:4px; }}
footer {{ position:relative;color:#647b95;text-align:center;font-size:12px;padding:7px 0 2px; }}
</style></head><body>{body}</body></html>"""


def _recommendation_card(item: AugmentRecommendation, images: dict[str, str]) -> str:
    augment = item.augment
    image = _image_tag(images.get(augment.icon_url, ""), augment.name_zh, "augment-icon")
    stats = []
    if item.win_rate:
        stats.append(f"胜率 {item.win_rate}")
    if item.pick_rate:
        stats.append(f"选择率 {item.pick_rate}")
    if item.games:
        stats.append(f"{item.games} 场")
    stat_html = f'<div class="stats">{_safe(" · ".join(stats))}</div>' if stats else ""
    return (
        '<div class="recommendation">'
        f'<div class="rank">#{item.rank}</div>{image}<div class="rec-copy">'
        f'<div class="rec-name">{_safe(augment.name_zh)}</div>{stat_html}</div></div>'
    )


def _option_module(
    title: str,
    options: Iterable[LoadoutOption],
    images: dict[str, str],
    hint: str,
    *,
    wide: bool = False,
) -> str:
    option_list = list(options)
    rows = "".join(f'<div class="option">{_items_html(option.items, images)}</div>' for option in option_list)
    if not rows:
        rows = '<div class="mini-empty">暂无数据</div>'
    classes = "module wide" if wide else "module"
    return f'<div class="{classes}"><div class="module-head"><b>{_safe(title)}</b><span>{_safe(hint)}</span></div>{rows}</div>'


def _items_module(title: str, items: Iterable[ItemRef], images: dict[str, str], hint: str) -> str:
    values = list(items)
    body = _items_html(values, images) if values else '<div class="mini-empty">暂无数据</div>'
    return f'<div class="module"><div class="module-head"><b>{_safe(title)}</b><span>{_safe(hint)}</span></div>{body}</div>'


def _skill_order_module(order: tuple[str, ...]) -> str:
    body = " → ".join(order) if order else "暂无数据"
    return '<div class="module"><div class="module-head"><b>技能加点顺序</b><span>OP.GG</span></div>' f'<div class="skill-order">{_safe(body)}</div></div>'


def _route_card(route: FullBuildRoute, images: dict[str, str]) -> str:
    note = f'<div class="route-note">{_safe(route.note)}</div>' if route.note else ""
    return (
        '<article class="route"><div class="route-head">'
        f'<span class="route-name">{_safe(route.name)}</span>'
        f'<span class="route-source">{_safe(route.source)}</span></div>'
        f'{_items_html(route.items, images)}{note}</article>'
    )


def _items_html(items: Iterable[ItemRef], images: dict[str, str]) -> str:
    values = list(items)
    parts = []
    for index, item in enumerate(values):
        data = _first_image(
            images,
            item.icon_url,
            item.fallback_icon_url,
            item.extra_icon_url,
        )
        image = _image_tag(data, item.name, "item-icon")
        parts.append(f'<div class="item">{image}<span class="item-name">{_safe(item.name)}</span></div>')
        if index < len(values) - 1:
            parts.append('<span class="arrow">›</span>')
    return f'<div class="items">{"".join(parts)}</div>'


def _first_image(images: dict[str, str], *urls: str) -> str:
    for url in urls:
        value = images.get(url, "")
        if value.startswith("data:image/"):
            return value
    return ""


def normalize_render_result(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif hasattr(value, "getvalue"):
        raw = bytes(value.getvalue())
    elif isinstance(value, str):
        raw = _decode_render_string(value)
    else:
        raise ValueError("html_render 返回了不支持的图片类型")
    if not raw:
        raise ValueError("html_render 返回了空图片")
    try:
        with PILImage.open(BytesIO(raw)) as image:
            image.load()
            output = BytesIO()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.mode else "RGB")
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception as error:
        raise ValueError("html_render 返回的内容不是有效图片") from error


def _decode_render_string(value: str) -> bytes:
    value = value.strip()
    if value.startswith("base64://"):
        return _decode_base64(value[9:])
    if value.startswith("data:image/") and "," in value:
        return _decode_base64(value.split(",", 1)[1])
    if value.startswith("file:"):
        return Path(unquote(urlparse(value).path)).read_bytes()
    path = Path(value)
    if path.is_file():
        return path.read_bytes()
    return _decode_base64(value)


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("无效的 Base64 图片") from error


def _safe(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _tier_label(value: str) -> str:
    return _TIER_LABELS.get(str(value or "Unknown"), str(value or "Unknown"))


def _tier_class(value: str) -> str:
    text = str(value or "").casefold()
    for name in ("prismatic", "gold", "silver", "bronze"):
        if name in text:
            return name
    return "unknown"


def _image_tag(data_uri: str, alt: str, class_name: str) -> str:
    if data_uri.startswith("data:image/"):
        return f'<img class="{class_name}" src="{html.escape(data_uri, quote=True)}" alt="{_safe(alt)}">'
    return f'<div class="{class_name} image-placeholder">HEX</div>'
