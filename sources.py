from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import Augment, FullBuildRoute, ItemRef, LoadoutOption, OpggChampionData


COMMUNITYDRAGON_ZH_URL = (
    "https://raw.communitydragon.org/latest/cdragon/arena/zh_cn.json"
)
COMMUNITYDRAGON_EN_URL = (
    "https://raw.communitydragon.org/latest/cdragon/arena/en_us.json"
)
OPGG_BASE_URL = "https://op.gg/zh-cn/lol/modes/aram-mayhem"
WIKI_AUGMENTS_URL = (
    "https://wiki.leagueoflegends.com/en-us/api.php?"
    "action=parse&page=ARAM%3A_Mayhem%2FAugments&prop=text&format=json"
)
GTIMG_HERO_LIST_URL = (
    "https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js"
)
MAYHEMPEDIA_BUILD_BASE = (
    "https://cdn.jsdelivr.net/gh/boxsbraindump/Mayhempedia@main/data/builds"
)

_RARITY_NAMES = {0: "Silver", 1: "Gold", 2: "Prismatic", 3: "Prismatic", 4: "Prismatic"}
_ITEM_ID_RE = re.compile(r"(?:item[/_-]|/)(\d{4,6})(?:[._/?-]|$)", re.IGNORECASE)
_PERCENT_RE = r"([0-9]+(?:\.[0-9]+)?%)"


def normalize_lookup(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value or "").casefold())


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ").split())


def parse_communitydragon_augments(
    zh_payload: Any,
    en_payload: Any,
) -> list[Augment]:
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
    seen: set[str] = set()
    for raw in zh_items:
        if not isinstance(raw, dict):
            continue
        augment_id = str(raw.get("id") or "").strip()
        api_name = str(raw.get("apiName") or "").strip()
        name_zh = clean_text(raw.get("name"))
        if not augment_id or not name_zh or augment_id in seen:
            continue
        peer = english.get(augment_id) or english.get(api_name) or {}
        icon_path = str(raw.get("iconLarge") or raw.get("iconSmall") or "").strip()
        rarity = raw.get("rarity")
        try:
            rarity_value = int(rarity)
        except (TypeError, ValueError):
            rarity_value = -1
        result.append(
            Augment(
                id=augment_id,
                tier=_RARITY_NAMES.get(rarity_value, "Unknown"),
                name_zh=name_zh,
                name_en=clean_text(peer.get("name")),
                description=clean_text(raw.get("desc") or raw.get("tooltip") or "暂无描述"),
                icon_url=communitydragon_asset_url(icon_path),
                api_name=api_name,
            )
        )
        seen.add(augment_id)
    return result


def communitydragon_asset_url(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        return ""
    if value.casefold().startswith("http"):
        return value
    return "https://raw.communitydragon.org/latest/game/" + quote(
        value.casefold(), safe="/._-"
    )


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
    accept names that map back to CommunityDragon, so navigation/skill images
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


def parse_wiki_notes(html_text: str) -> dict[str, str]:
    if is_access_challenge(html_text):
        raise ValueError("League Wiki 返回了访问验证页")
    soup = BeautifulSoup(html_text, "html.parser")
    result: dict[str, str] = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        name = _wiki_row_name(cells[0])
        if not name:
            continue
        notes = row.select_one('[data-title="Notes"], [data-title="notes"]')
        if notes is None:
            notes = row.find(
                lambda tag: isinstance(tag, Tag)
                and str(tag.get("title") or "").casefold() == "notes"
            )
        note_text = clean_text(notes.get_text(" ") if notes else "")
        if not note_text and len(cells) >= 2:
            # Some revisions expose the mechanics in the effect column only.
            note_text = clean_text(cells[-1].get_text(" "))
        if not note_text or note_text == name:
            continue
        for candidate in (name, str(cells[0].get("data-api-name") or "")):
            key = normalize_lookup(candidate)
            if key:
                result[key] = note_text[:2000]
    return result


def attach_wiki_note(item: Augment, notes: dict[str, str]) -> Augment:
    for candidate in (item.name_en, item.api_name, item.name_zh):
        note = notes.get(normalize_lookup(candidate))
        if note:
            return replace(item, wiki_note_en=note)
    return item


def parse_gtimg_aliases(payload: Any) -> dict[str, tuple[str, ...]]:
    heroes = payload.get("hero", []) if isinstance(payload, dict) else []
    result: dict[str, tuple[str, ...]] = {}
    for raw in heroes:
        if not isinstance(raw, dict):
            continue
        hero_id = str(raw.get("heroId") or raw.get("id") or "").strip()
        if not hero_id:
            continue
        values: list[str] = []
        for field in ("name", "title", "alias", "keywords", "label"):
            value = str(raw.get(field) or "")
            values.extend(part.strip() for part in re.split(r"[,，/|]", value) if part.strip())
        result[hero_id] = tuple(dict.fromkeys(values))
    return result


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
    fallback = communitydragon_item_url(item_id)
    extra = (
        f"https://game.gtimg.cn/images/lol/act/img/item/{item_id}.png"
        if item_id
        else ""
    )
    return ItemRef(item_id, name, url, fallback, extra)


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


def _wiki_row_name(cell: Tag) -> str:
    candidate = cell.select_one("[data-to-string], a[title], b, strong")
    if candidate is not None:
        value = str(candidate.get("data-to-string") or candidate.get("title") or candidate.get_text(" "))
        value = clean_text(value)
        if value:
            return value
    return clean_text(cell.get_text(" ")).split("[")[0].strip()


def _mayhempedia_item(raw: dict[str, Any]) -> ItemRef:
    item_id = str(raw.get("id") or "").strip()
    name = clean_text(raw.get("name"))
    icon = (
        f"https://ddragon.leagueoflegends.com/cdn/15.1.1/img/item/{item_id}.png"
        if item_id
        else ""
    )
    fallback = communitydragon_item_url(item_id)
    extra = (
        f"https://game.gtimg.cn/images/lol/act/img/item/{item_id}.png"
        if item_id
        else ""
    )
    return ItemRef(item_id, name, icon, fallback, extra)
