from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import (
    Augment,
    AugmentTrio,
    BuildVariant,
    ChampionSummary,
    FullBuildRoute,
    ItemPerformance,
    ItemRef,
    LoadoutOption,
    OpggChampionData,
    RelatedArticle,
    SkillOrderOption,
)


COMMUNITYDRAGON_ZH_URL = "https://raw.communitydragon.org/latest/cdragon/arena/zh_cn.json"
COMMUNITYDRAGON_EN_URL = "https://raw.communitydragon.org/latest/cdragon/arena/en_us.json"
OPGG_BASE_URL = "https://op.gg/zh-cn/lol/modes/aram-mayhem"
ARAMGG_BASE_URL = "https://data.dtodo.cn/api/v1/zh-CN"
MAYHEMPEDIA_BUILD_BASE = (
    "https://cdn.jsdelivr.net/gh/boxsbraindump/Mayhempedia@main/data/builds"
)

_ITEM_ID_RE = re.compile(r"(?:item[/_-]|/)(\d{4,6})(?:[._/?-]|$)", re.IGNORECASE)
_PERCENT_RE = r"([0-9]+(?:\.[0-9]+)?%)"
_RARITY_NAMES = {0: "Silver", 1: "Gold", 2: "Prismatic", 3: "Prismatic", 4: "Prismatic"}


def normalize_lookup(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value or "").casefold())


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ").split())


def parse_communitydragon_augments(zh_payload: Any, en_payload: Any) -> list[Augment]:
    zh_items = zh_payload.get("augments", []) if isinstance(zh_payload, dict) else []
    en_items = en_payload.get("augments", []) if isinstance(en_payload, dict) else []
    english: dict[str, dict[str, Any]] = {}
    for raw in en_items:
        if not isinstance(raw, dict):
            continue
        for key in (str(raw.get("id") or ""), str(raw.get("apiName") or "")):
            if key:
                english[key] = raw
    result: list[Augment] = []
    for raw in zh_items:
        if not isinstance(raw, dict):
            continue
        augment_id = str(raw.get("id") or "").strip()
        api_name = str(raw.get("apiName") or "").strip()
        name = clean_text(raw.get("name"))
        if not augment_id or not name:
            continue
        peer = english.get(augment_id) or english.get(api_name) or {}
        try:
            rarity = int(raw.get("rarity"))
        except (TypeError, ValueError):
            rarity = -1
        icon_path = str(raw.get("iconLarge") or raw.get("iconSmall") or "").strip()
        result.append(
            Augment(
                id=augment_id,
                tier=_RARITY_NAMES.get(rarity, "Unknown"),
                name_zh=name,
                name_en=clean_text(peer.get("name")),
                description=clean_text(raw.get("desc") or raw.get("tooltip") or "暂无描述"),
                icon_url=communitydragon_asset_url(icon_path),
                api_name=api_name,
            )
        )
    return result


def communitydragon_asset_url(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        return ""
    if value.casefold().startswith("http"):
        return value
    return "https://raw.communitydragon.org/latest/game/" + quote(value.casefold(), safe="/._-")


def communitydragon_item_url(item_id: str) -> str:
    if not str(item_id or "").isdigit():
        return ""
    return (
        "https://raw.communitydragon.org/latest/plugins/"
        "rcp-be-lol-game-data/global/default/assets/items/icons2d/"
        f"{item_id}.png"
    )


def parse_opgg_augments(
    html_text: str,
    catalog: Iterable[Augment],
    max_per_rarity: int = 5,
) -> tuple[tuple[str, str, str, str], ...]:
    """Return ordered (name, win rate, pick rate, games) tuples.

    OP.GG exposes the recommendations in server-rendered image lists.  We only
    accept names that map back to the selected augment catalog, so navigation/skill images
    cannot accidentally become augment recommendations.
    """
    if is_access_challenge(html_text):
        raise ValueError("OP.GG 返回了访问验证页")
    by_name: dict[str, Augment] = {}
    for item in catalog:
        for name in (item.name_zh, item.name_en, item.api_name):
            key = normalize_lookup(name)
            if key:
                by_name[key] = item

    soup = BeautifulSoup(html_text, "html.parser")
    counts = {"Prismatic": 0, "Gold": 0, "Silver": 0}
    found: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for image in soup.select("img[alt]"):
        name = _image_name(image)
        item = by_name.get(normalize_lookup(name))
        if item is None or item.id in seen or item.tier not in counts:
            continue
        if counts[item.tier] >= max(1, max_per_rarity):
            continue
        context = image.find_parent("tr") or image.find_parent("li") or image.parent
        context_text = clean_text(context.get_text(" ") if context else "")
        found.append(
            (
                item.name_zh,
                _labeled_stat(context_text, ("胜率", "win rate")),
                _labeled_stat(context_text, ("选择率", "登场率", "pick rate")),
                _labeled_games(context_text),
            )
        )
        counts[item.tier] += 1
        seen.add(item.id)
        if all(value >= max_per_rarity for value in counts.values()):
            break
    return tuple(found)


def parse_opgg_build_pages(*html_pages: str, page_url: str = OPGG_BASE_URL) -> OpggChampionData:
    valid_pages = [page for page in html_pages if page and not is_access_challenge(page)]
    if not valid_pages:
        raise ValueError("OP.GG 没有返回可解析的页面")
    combined_text = " ".join(clean_text(BeautifulSoup(page, "html.parser").get_text(" ")) for page in valid_pages)
    patch_match = re.search(r"(?<!\d)(\d{1,2}\.\d{1,2})(?:\.\d+)?\s*版本", combined_text)
    tier_match = re.search(r"(?:^|\s)([0-9]+|S\+?|A\+?|B\+?|C\+?|D)\s*段位", combined_text, re.IGNORECASE)

    tables: dict[str, list[list[ItemRef]]] = {}
    for label in ("召唤师技能", "出门装", "鞋子", "核心装备"):
        rows: list[list[ItemRef]] = []
        for page in valid_pages:
            rows.extend(_extract_labeled_item_rows(page, label, page_url))
        tables[label] = _dedupe_item_rows(rows)

    skill_order: tuple[str, ...] = ()
    for page in valid_pages:
        text = _extract_labeled_text(page, "技能加点")
        slots = re.findall(r"(?<![A-Z])[QWER](?![A-Z])", text.upper())
        unique: list[str] = []
        for slot in slots:
            if slot not in unique:
                unique.append(slot)
            if len(unique) == 3:
                break
        if unique:
            skill_order = tuple(unique)
            break

    summoners = tuple(LoadoutOption(tuple(row), "召唤师技能") for row in tables["召唤师技能"][:2] if row)
    starters = tuple(LoadoutOption(tuple(row), "出门装") for row in tables["出门装"][:2] if row)
    boots = tuple(row[0] for row in tables["鞋子"][:2] if row)
    cores = tuple(LoadoutOption(tuple(row), "核心装备") for row in tables["核心装备"][:3] if row)
    return OpggChampionData(
        patch=patch_match.group(1) if patch_match else "",
        tier=(f"{tier_match.group(1)} 段位" if tier_match else ""),
        summoner_spells=summoners,
        skill_order=skill_order,
        starter_items=starters,
        boots=boots,
        core_builds=cores,
    )


def merge_opgg_data(
    base: OpggChampionData,
    augment_names: tuple[tuple[str, str, str, str], ...],
) -> OpggChampionData:
    return replace(base, augment_names=augment_names)


def parse_aramgg_champion(payload: Any, max_per_rarity: int = 5) -> OpggChampionData:
    """Convert ARAMGG's expanded champion endpoint into the shared report model."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("ARAMGG 没有返回有效英雄详情")
    data = payload["data"]
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    champion = data.get("champion") if isinstance(data.get("champion"), dict) else {}
    champion_stats = champion.get("stats") if isinstance(champion.get("stats"), dict) else {}

    rarity_counts = {"Prismatic": 0, "Gold": 0, "Silver": 0}
    rarity_map = {"prismatic": "Prismatic", "gold": "Gold", "silver": "Silver", 2: "Prismatic", 1: "Gold", 0: "Silver"}
    augments: list[tuple[str, str, str, str]] = []
    raw_augments = data.get("augments") if isinstance(data.get("augments"), list) else []
    ranked = sorted(
        (item for item in raw_augments if isinstance(item, dict)),
        key=lambda item: _safe_int((item.get("stats") or {}).get("rank"), 9999),
    )


    for item in ranked:
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        rarity_key = item.get("rarityName") or item.get("rarity")
        tier = rarity_map.get(rarity_key, rarity_map.get(str(rarity_key).casefold(), ""))
        name = clean_text(item.get("name"))
        if not name or tier not in rarity_counts or rarity_counts[tier] >= max(1, max_per_rarity):
            continue
        augments.append((name, _percent(stats.get("winRate")), _percent(stats.get("pickRate")), _games(stats.get("games"))))
        rarity_counts[tier] += 1

    builds = data.get("builds") if isinstance(data.get("builds"), list) else []
    build = builds[0] if builds and isinstance(builds[0], dict) else {}
    summoners = _aramgg_loadouts(build.get("summonerSpells"), "召唤师技能", 2)
    starters = _aramgg_loadouts(build.get("startingItems"), "出门装", 2)
    cores = _aramgg_loadouts(build.get("coreItems"), "核心装备", 3)
    boots = tuple(option.items[0] for option in _aramgg_loadouts(build.get("boots"), "鞋子", 2) if option.items)
    if not boots:
        boots = _aramgg_boots(data.get("items"))

    skill_order: tuple[str, ...] = ()
    skill_rows = build.get("skillOrders") if isinstance(build.get("skillOrders"), list) else []
    if skill_rows and isinstance(skill_rows[0], dict):
        keys = skill_rows[0].get("skillKeys")
        if not isinstance(keys, list):
            keys = ({1: "Q", 2: "W", 3: "E", 4: "R"}.get(_safe_int(value), "") for value in skill_rows[0].get("skillOrder", []))
        ordered: list[str] = []
        for value in keys:
            key = str(value).upper()
            if key in {"Q", "W", "E"} and key not in ordered:
                ordered.append(key)
        skill_order = tuple(ordered[:3])

    tier_value = champion_stats.get("tier")
    tier_parts = [f"T{tier_value}"] if tier_value not in (None, "") else []
    if champion_stats.get("winRate") is not None:
        tier_parts.append(f"胜率 {_percent(champion_stats.get('winRate'))}")
    if champion_stats.get("pickRate") is not None:
        tier_parts.append(f"登场率 {_percent(champion_stats.get('pickRate'))}")
    if champion_stats.get("games") is not None:
        tier_parts.append(f"{_games(champion_stats.get('games'))} 场")
    tier = " · ".join(tier_parts)
    build_variants = _aramgg_build_variants(builds)
    augment_trios = _aramgg_augment_trios(data.get("augmentTrios"), raw_augments)
    item_performance = _aramgg_item_performance(data.get("items"))
    provenance = _aramgg_provenance(meta, champion_stats, raw_augments)
    related_articles = _aramgg_related_articles(data.get("relatedBlogs"))
    return OpggChampionData(
        patch=str(meta.get("gamePatch") or champion_stats.get("gamePatch") or ""),
        tier=tier,
        augment_names=tuple(augments),
        summoner_spells=summoners,
        skill_order=skill_order,
        starter_items=starters,
        boots=boots,
        core_builds=cores,
        build_variants=build_variants,
        augment_trios=augment_trios,
        item_performance=item_performance,
        provenance=provenance,
        related_articles=related_articles,
    )


def parse_aramgg_champions(payload: Any) -> list[ChampionSummary]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ARAMGG 没有返回有效英雄列表")
    result: list[ChampionSummary] = []
    for list_rank, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("id") or "").strip()
        alias = str(raw.get("alias") or "").strip()
        name = clean_text(raw.get("name"))
        if not key or not alias or not name:
            continue
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        result.append(
            ChampionSummary(
                id=alias,
                key=key,
                name=name,
                title=clean_text(raw.get("title")),
                tags=tuple(str(value) for value in raw.get("roles", []) if value),
                aliases=(alias,),
                icon_url=str(raw.get("iconUrl") or ""),
                win_rate=_optional_float(stats.get("winRate")),
                pick_rate=_optional_float(stats.get("pickRate")),
                games=_optional_int(stats.get("games")),
                stats_tier=str(stats.get("tier") if stats.get("tier") is not None else ""),
                stats_patch=str(stats.get("gamePatch") or ""),
                stats_rank=_optional_int(raw.get("rank") or stats.get("rank")) or list_rank,
                stats_date=clean_text(stats.get("date")),
                stats_source=clean_text(stats.get("source")),
                stats_region=clean_text(stats.get("region")),
                rank_delta=clean_text(raw.get("rankDelta") or stats.get("rankDelta")),
            )
        )
    return result


def parse_opgg_champion_rankings(
    html_text: str, catalog: Iterable[ChampionSummary]
) -> list[ChampionSummary]:
    """Parse the public ranking embedded in OP.GG's server-rendered page."""
    if is_access_challenge(html_text):
        raise ValueError("OP.GG 返回了访问验证页")
    by_key = {str(item.key): item for item in catalog if item.key}
    by_id = {normalize_lookup(item.id): item for item in catalog if item.id}
    result: list[ChampionSummary] = []
    seen: set[str] = set()
    patch_match = re.search(r"(?<!\d)(\d{1,2}\.\d{1,2})(?:\.\d+)?\s*版本", html_text)
    page_patch = patch_match.group(1) if patch_match else ""
    for match in re.finditer(r"\{[^{}]{0,1200}\}", html_text):
        fragment = match.group(0)
        if "champion_id" not in fragment or "rank" not in fragment or "tier" not in fragment:
            continue
        normalized = fragment.replace('\\"', '"')
        try:
            raw = json.loads(normalized)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        champion_key = str(raw.get("champion_id") or raw.get("id") or "")
        champion = by_key.get(champion_key) or by_id.get(normalize_lookup(raw.get("key")))
        rank = _optional_int(raw.get("rank"))
        if champion is None or rank is None or rank < 1 or champion.id in seen:
            continue
        result.append(
            replace(
                champion,
                win_rate=_optional_ratio(
                    raw.get("win_rate") if "win_rate" in raw else raw.get("winRate")
                ),
                pick_rate=_optional_ratio(
                    raw.get("pick_rate") if "pick_rate" in raw else raw.get("pickRate")
                ),
                games=_optional_int(raw.get("games")),
                stats_tier=str(raw.get("tier") if raw.get("tier") is not None else ""),
                stats_rank=rank,
                stats_patch=clean_text(raw.get("patch") or page_patch),
                stats_date=clean_text(raw.get("date")),
                stats_source="opgg",
                stats_region=clean_text(raw.get("region") or "WORLD"),
                rank_delta=clean_text(raw.get("rank_delta") or raw.get("rankDelta")),
                icon_url=str(raw.get("image_url") or champion.icon_url),
            )
        )
        seen.add(champion.id)
    result.sort(key=lambda item: item.stats_rank or 9999)
    return result


def parse_aramgg_augments(payload: Any) -> list[Augment]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ARAMGG 没有返回有效海克斯列表")
    rarity_map = {"prismatic": "Prismatic", "gold": "Gold", "silver": "Silver", 2: "Prismatic", 1: "Gold", 0: "Silver"}
    result: list[Augment] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        augment_id = str(raw.get("id") or "").strip()
        name = clean_text(raw.get("name"))
        rarity = raw.get("rarityName") or raw.get("rarity")
        tier = rarity_map.get(rarity, rarity_map.get(str(rarity).casefold(), "Unknown"))
        if not augment_id or not name:
            continue
        result.append(
            Augment(
                id=augment_id,
                tier=tier,
                name_zh=name,
                name_en="",
                description=clean_text(raw.get("description") or raw.get("tooltip") or "暂无描述"),
                icon_url=str(raw.get("iconUrl") or ""),
                api_name=str(raw.get("key") or ""),
            )
        )
    return result


def _aramgg_loadouts(value: Any, label: str, limit: int) -> tuple[LoadoutOption, ...]:
    rows = value if isinstance(value, list) else []
    result: list[LoadoutOption] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        expanded = raw.get("items") or raw.get("spells") or []
        if not expanded and isinstance(raw.get("item"), dict):
            expanded = [raw["item"]]
        if not expanded and (raw.get("id") is not None or raw.get("itemId") is not None):
            expanded = [raw]
        ids = raw.get("itemIds") or raw.get("summonerSpellIds") or []
        items: list[ItemRef] = []
        if isinstance(expanded, list):
            for item in expanded:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or item.get("itemId") or "")
                items.append(ItemRef(item_id, clean_text(item.get("name")) or item_id, str(item.get("iconUrl") or "")))
        if not items and isinstance(ids, list):
            items = [ItemRef(str(item_id), str(item_id), _aramgg_asset_url(label, item_id)) for item_id in ids]
        if items:
            result.append(LoadoutOption(tuple(items), label, _percent(raw.get("winRate")), _percent(raw.get("pickRate")), _games(raw.get("games"))))
        if len(result) >= limit:
            break
    return tuple(result)


def _aramgg_build_variants(value: Any) -> tuple[BuildVariant, ...]:
    rows = value if isinstance(value, list) else []
    result: list[BuildVariant] = []
    for index, raw in enumerate(rows[:4], start=1):
        if not isinstance(raw, dict):
            continue
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else raw
        name = clean_text(raw.get("label") or raw.get("name") or raw.get("title"))
        if not name:
            tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
            tag_names = [
                clean_text(tag.get("name") or tag.get("label")) if isinstance(tag, dict) else clean_text(tag)
                for tag in tags
            ]
            name = " / ".join(value for value in tag_names if value) or f"流派 {index}"
        result.append(
            BuildVariant(
                name=name,
                win_rate=_percent(stats.get("winRate")),
                pick_rate=_percent(stats.get("pickRate")),
                games=_games(stats.get("games")),
                summoner_spells=_aramgg_loadouts(raw.get("summonerSpells"), "召唤师技能", 2),
                skill_orders=_aramgg_skill_orders(raw.get("skillOrders"), 3),
                starter_items=_aramgg_loadouts(raw.get("startingItems"), "出门装", 3),
                core_items=_aramgg_loadouts(raw.get("coreItems"), "核心装备", 3),
                situational_items=_aramgg_loadouts(raw.get("situationalItems"), "情境装备", 6),
            )
        )
    return tuple(result)


def _aramgg_skill_orders(value: Any, limit: int) -> tuple[SkillOrderOption, ...]:
    rows = value if isinstance(value, list) else []
    result: list[SkillOrderOption] = []
    key_map = {1: "Q", 2: "W", 3: "E", 4: "R"}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        values = raw.get("skillKeys") if isinstance(raw.get("skillKeys"), list) else raw.get("skillOrder")
        if not isinstance(values, list):
            continue
        order = tuple(
            key
            for value in values[:18]
            if (key := (str(value).upper() if str(value).upper() in {"Q", "W", "E", "R"} else key_map.get(_safe_int(value), "")))
        )
        if not order:
            continue
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else raw
        result.append(
            SkillOrderOption(
                order,
                _percent(stats.get("winRate")),
                _percent(stats.get("pickRate")),
                _games(stats.get("games")),
            )
        )
        if len(result) >= limit:
            break
    return tuple(result)


def _aramgg_augment_trios(value: Any, raw_augments: list[Any]) -> tuple[AugmentTrio, ...]:
    rows = value if isinstance(value, list) else []
    by_id = {
        str(item.get("id")): item
        for item in raw_augments
        if isinstance(item, dict) and item.get("id") is not None
    }
    result: list[AugmentTrio] = []
    for raw in rows[:5]:
        if not isinstance(raw, dict):
            continue
        values = raw.get("augments") if isinstance(raw.get("augments"), list) else []
        if not values and isinstance(raw.get("augmentIds"), list):
            values = [by_id.get(str(item_id), {"id": item_id}) for item_id in raw["augmentIds"]]
        augments: list[Augment] = []
        for item in values[:3]:
            if not isinstance(item, dict):
                continue
            peer = by_id.get(str(item.get("id")), {})
            merged = {**peer, **item}
            name = clean_text(merged.get("name") or merged.get("displayName"))
            if not name:
                continue
            rarity = merged.get("rarityName") or merged.get("rarity")
            tier = {"prismatic": "Prismatic", "gold": "Gold", "silver": "Silver", 2: "Prismatic", 1: "Gold", 0: "Silver"}.get(
                rarity, "Unknown"
            )
            augments.append(
                Augment(
                    str(merged.get("id") or ""),
                    tier,
                    name,
                    "",
                    clean_text(merged.get("description") or ""),
                    icon_url=str(merged.get("iconUrl") or ""),
                    api_name=str(merged.get("key") or ""),
                )
            )
        if len(augments) != 3:
            continue
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else raw
        result.append(
            AugmentTrio(
                tuple(augments),
                _percent(stats.get("winRate")),
                _percent(stats.get("pickRate")),
                _games(stats.get("games")),
            )
        )
    return tuple(result)


def _aramgg_item_performance(value: Any) -> tuple[ItemPerformance, ...]:
    rows = value if isinstance(value, list) else []
    ranked = sorted(
        (raw for raw in rows if isinstance(raw, dict)),
        key=lambda raw: (-(_optional_float((raw.get("stats") or raw).get("pickRate")) or 0.0), str(raw.get("id") or "")),
    )
    result: list[ItemPerformance] = []
    for raw in ranked:
        item = raw.get("item") if isinstance(raw.get("item"), dict) else raw
        item_id = str(item.get("id") or raw.get("id") or "")
        name = clean_text(item.get("name") or raw.get("name"))
        if not item_id or not name:
            continue
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else raw
        result.append(
            ItemPerformance(
                ItemRef(item_id, name, str(item.get("iconUrl") or raw.get("iconUrl") or "")),
                _percent(stats.get("winRate")),
                _percent(stats.get("pickRate")),
                _games(stats.get("games")),
            )
        )
        if len(result) >= 6:
            break
    return tuple(result)


def _aramgg_provenance(meta: dict[str, Any], champion_stats: dict[str, Any], raw_augments: list[Any]) -> tuple[str, ...]:
    values: list[str] = []
    source_raw = clean_text(champion_stats.get("source"))
    region_raw = clean_text(champion_stats.get("region"))
    source = {
        "tencent": "腾讯公开快照",
        "iesdev": "ARAMGG Build 统计",
        "aramgg-client-upload": "ARAMGG 客户端匿名上传",
    }.get(source_raw, source_raw)
    region = {"CN": "国服", "WORLD": "全球"}.get(region_raw, region_raw)
    date = clean_text(champion_stats.get("date"))
    if source or region:
        values.append("英雄统计：" + " / ".join(value for value in (source, region) if value))
    if date:
        values.append(f"统计日期：{date}")
    if meta.get("generatedAt"):
        values.append(f"数据生成：{clean_text(meta.get('generatedAt'))}")
    upload_stats = next(
        (
            item.get("stats")
            for item in raw_augments
            if isinstance(item, dict)
            and isinstance(item.get("stats"), dict)
            and item["stats"].get("winRateSource")
        ),
        None,
    )
    if isinstance(upload_stats, dict):
        upload_source_raw = clean_text(upload_stats.get("winRateSource"))
        upload_region_raw = clean_text(upload_stats.get("winRateRegion"))
        upload_source = {
            "aramgg-client-upload": "ARAMGG 客户端匿名上传"
        }.get(upload_source_raw, upload_source_raw)
        upload_region = {"CN": "国服", "WORLD": "全球"}.get(
            upload_region_raw, upload_region_raw
        )
        note = "海克斯胜率：" + " / ".join(
            value for value in (upload_source, upload_region) if value
        )
        if upload_stats.get("winRateMinimumGames") is not None:
            note += f"，最低 {upload_stats['winRateMinimumGames']} 场"
        values.append(note)
    return tuple(values)


def _aramgg_related_articles(value: Any) -> tuple[RelatedArticle, ...]:
    rows = value if isinstance(value, list) else []
    result = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        title = clean_text(raw.get("title"))
        if title:
            result.append(RelatedArticle(title, str(raw.get("url") or "")))
        if len(result) >= 3:
            break
    return tuple(result)


def _aramgg_asset_url(label: str, item_id: Any) -> str:
    folder = "summoner-spell-icons" if label == "召唤师技能" else "item-icons"
    return f"https://cdn.dtodo.cn/hextech/{folder}/{item_id}.png"


def _aramgg_boots(value: Any) -> tuple[ItemRef, ...]:
    rows = value if isinstance(value, list) else []
    result: list[ItemRef] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = raw.get("item") if isinstance(raw.get("item"), dict) else raw
        name = clean_text(item.get("name"))
        if not name or not any(marker in name for marker in ("靴", "鞋")):
            continue
        item_id = str(item.get("id") or raw.get("id") or "")
        result.append(ItemRef(item_id, name, str(item.get("iconUrl") or raw.get("iconUrl") or _aramgg_asset_url("鞋子", item_id))))
        if len(result) >= 2:
            break
    return tuple(result)


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%" if value is not None else ""
    except (TypeError, ValueError):
        return ""


def _games(value: Any) -> str:
    try:
        return f"{int(value):,}" if value is not None else ""
    except (TypeError, ValueError):
        return ""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_ratio(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    return parsed / 100 if parsed > 1 else parsed


def parse_mayhempedia_routes(payload: Any, champion_key: str) -> tuple[FullBuildRoute, ...]:
    if not isinstance(payload, dict) or str(payload.get("championId") or "") != str(champion_key):
        return ()
    result: list[FullBuildRoute] = []
    for raw in payload.get("archetypes", []):
        if not isinstance(raw, dict):
            continue
        items = tuple(_mayhempedia_item(item) for item in raw.get("items", []) if isinstance(item, dict))
        items = tuple(item for item in items if item.id and item.name)
        if len(items) < 6:
            continue
        result.append(
            FullBuildRoute(
                name=clean_text(raw.get("name")) or "社区路线",
                note=clean_text(raw.get("note")),
                items=items[:6],
            )
        )
        if len(result) == 3:
            break
    return tuple(result)


def mayhempedia_slugs(champion_id: str) -> tuple[str, ...]:
    normalized = normalize_lookup(champion_id)
    overrides = {
        "monkeyking": ("wukong", "monkeyking"),
        "nunu": ("nunu", "nunuandwillump"),
        "renata": ("renataglasc", "renata"),
    }
    return overrides.get(normalized, (normalized,))


def is_access_challenge(html_text: str) -> bool:
    lowered = str(html_text or "").casefold()
    markers = (
        "challenges.cloudflare.com",
        "cf_chl_",
        "just a moment...",
        "verify you are human",
        "access denied",
    )
    return sum(marker in lowered for marker in markers) >= 2 or (
        "challenges.cloudflare.com" in lowered and "just a moment" in lowered
    )


def _image_name(image: Tag) -> str:
    value = clean_text(image.get("alt") or image.get("title") or "")
    return re.sub(r"^(?:image|图片)\s*[:：]\s*", "", value, flags=re.IGNORECASE)


def _extract_labeled_item_rows(html_text: str, label: str, page_url: str) -> list[list[ItemRef]]:
    soup = BeautifulSoup(html_text, "html.parser")
    containers: list[Tag] = []
    for table in soup.select("table"):
        if label in clean_text(table.get_text(" ")):
            containers.append(table)
    if not containers:
        label_node = soup.find(
            lambda tag: isinstance(tag, Tag)
            and tag.name in {"h2", "h3", "h4", "caption", "div", "span"}
            and clean_text(tag.get_text(" ")) == label
        )
        if label_node is not None:
            table = label_node.find_parent("table") or label_node.find_next("table")
            if table is not None:
                containers.append(table)
            else:
                parent = label_node.parent
                if isinstance(parent, Tag):
                    containers.append(parent)

    rows: list[list[ItemRef]] = []
    for container in containers[:1]:
        row_nodes = container.select("tbody tr") or container.select("tr")
        if not row_nodes:
            row_nodes = [node for node in container.find_all(["li", "div"], recursive=False) if node.select("img[alt]")]
        for row in row_nodes:
            items = [_item_from_image(image, page_url) for image in row.select("img[alt]")]
            items = [item for item in items if item.name]
            if items:
                rows.append(items)
    return rows


def _extract_labeled_text(html_text: str, label: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for table in soup.select("table"):
        text = clean_text(table.get_text(" "))
        if label in text:
            return text
    label_node = soup.find(string=lambda value: label in clean_text(value))
    if label_node is not None:
        parent = label_node.parent
        if isinstance(parent, Tag):
            container = parent.find_parent("table") or parent.find_next("table") or parent.parent
            if isinstance(container, Tag):
                return clean_text(container.get_text(" "))
    return ""


def _item_from_image(image: Tag, page_url: str) -> ItemRef:
    name = _image_name(image)
    raw_url = str(
        image.get("src")
        or image.get("data-src")
        or image.get("data-original")
        or ""
    )
    url = urljoin(page_url, raw_url) if raw_url else ""
    match = _ITEM_ID_RE.search(urlparse(url).path)
    item_id = match.group(1) if match else ""
    return ItemRef(item_id, name, url, communitydragon_item_url(item_id), "")


def _dedupe_item_rows(rows: list[list[ItemRef]]) -> list[list[ItemRef]]:
    output: list[list[ItemRef]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = tuple(item.id or normalize_lookup(item.name) for item in row)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _labeled_stat(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        match = re.search(re.escape(label) + r"\s*[:：]?\s*" + _PERCENT_RE, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _labeled_games(text: str) -> str:
    match = re.search(r"(?:场次|样本|games?)\s*[:：]?\s*([0-9][0-9,]*)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _mayhempedia_item(raw: dict[str, Any]) -> ItemRef:
    item_id = str(raw.get("id") or "").strip()
    name = clean_text(raw.get("name"))
    return ItemRef(item_id, name, "")
