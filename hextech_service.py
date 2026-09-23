from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
from dataclasses import replace
from io import BytesIO
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import aiohttp
import certifi
from PIL import Image as PILImage
from yarl import URL

from .models import (
    Augment,
    AugmentRecommendation,
    CacheEntry,
    ChampionDetail,
    ChampionSummary,
    DataResult,
    FullBuildRoute,
    HeroReport,
    ItemRef,
    LoadoutOption,
    OpggChampionData,
    ServiceConfig,
)
from .sources import (
    COMMUNITYDRAGON_EN_URL,
    COMMUNITYDRAGON_ZH_URL,
    GTIMG_HERO_LIST_URL,
    MAYHEMPEDIA_BUILD_BASE,
    OPGG_BASE_URL,
    WIKI_AUGMENTS_URL,
    attach_wiki_note,
    clean_text,
    is_access_challenge,
    mayhempedia_slugs,
    merge_opgg_data,
    normalize_lookup,
    parse_communitydragon_augments,
    parse_gtimg_aliases,
    parse_mayhempedia_routes,
    parse_opgg_augments,
    parse_opgg_build_pages,
    parse_wiki_notes,
)


DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CDN = "https://ddragon.leagueoflegends.com/cdn"

_TEXT_LIMIT = 12 * 1024 * 1024
_IMAGE_LIMIT = 5 * 1024 * 1024
_TRUSTED_IMAGE_HOSTS = {
    "ddragon.leagueoflegends.com",
    "raw.communitydragon.org",
    "opgg-static.akamaized.net",
    "s-opgg-kit.op.gg",
    "game.gtimg.cn",
}
_SOURCE_LABELS = {
    "data_dragon": "Data Dragon",
    "communitydragon": "CommunityDragon",
    "opgg": "OP.GG",
    "wiki": "League Wiki",
    "gtimg": "gtimg",
    "mayhempedia": "Mayhempedia",
}


class HextechError(RuntimeError):
    """Base error exposed by the data service."""


class UpstreamUnavailable(HextechError):
    """Raised when no current or stale upstream data is available."""


class ChampionNotFound(HextechError):
    """Raised when a hero query cannot be resolved locally."""


class AmbiguousChampion(HextechError):
    def __init__(self, query: str, candidates: list[ChampionSummary]) -> None:
        names = "、".join(item.name for item in candidates[:5])
        super().__init__(f"“{query}”匹配到多个英雄：{names}，请提供更完整的名称")
        self.candidates = candidates


class HextechService:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._champions_lock = asyncio.Lock()
        self._augments_lock = asyncio.Lock()
        self._wiki_lock = asyncio.Lock()
        self._gtimg_lock = asyncio.Lock()
        self._detail_locks: dict[str, asyncio.Lock] = {}
        self._opgg_locks: dict[str, asyncio.Lock] = {}
        self._route_locks: dict[str, asyncio.Lock] = {}
        self._champions: CacheEntry | None = None
        self._champion_version = ""
        self._details: dict[str, CacheEntry] = {}
        self._augments: CacheEntry | None = None
        self._wiki_notes: CacheEntry | None = None
        self._gtimg_aliases: CacheEntry | None = None
        self._opgg: dict[str, CacheEntry] = {}
        self._routes: dict[str, CacheEntry] = {}
        self._image_cache: dict[str, str] = {}
        self._source_errors: dict[str, str] = {}

    async def initialize(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=aiohttp.TCPConnector(ssl=ssl_context),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "Chrome/126.0 Safari/537.36 HextechMayhem/1.0"
                    ),
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                },
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._champions = None
        self._details.clear()
        self._augments = None
        self._wiki_notes = None
        self._gtimg_aliases = None
        self._opgg.clear()
        self._routes.clear()
        self._image_cache.clear()
        self._source_errors.clear()

    async def champions(self) -> DataResult:
        return await self._cached_single(
            entry_getter=lambda: self._champions,
            entry_setter=self._set_champions,
            lock=self._champions_lock,
            loader=self._load_champions,
            label="Riot 英雄列表",
            source="data_dragon",
        )

    async def find_champion(self, query: str) -> tuple[ChampionSummary, bool]:
        query = str(query or "").strip()
        if not query:
            raise ChampionNotFound("英雄名不能为空")
        result = await self.champions()
        items: list[ChampionSummary] = result.value
        needle = normalize_lookup(query)
        matches = _match_champions(items, needle)
        stale = result.stale
        if not matches and self.config.enable_gtimg_fallback:
            try:
                alias_result = await self.gtimg_aliases()
                stale = stale or alias_result.stale
                enriched = [
                    replace(item, aliases=alias_result.value.get(item.key, ()))
                    for item in items
                ]
                matches = _match_champions(enriched, needle)
            except HextechError:
                pass
        exact, partial = matches
        if len(exact) == 1:
            return exact[0], stale
        if len(exact) > 1:
            raise AmbiguousChampion(query, exact)
        if len(partial) == 1:
            return partial[0], stale
        if len(partial) > 1:
            raise AmbiguousChampion(query, partial)
        raise ChampionNotFound(f"未找到英雄：{query}")

    async def champion_detail(self, champion: ChampionSummary) -> DataResult:
        await self.champions()
        version = self._champion_version
        cache_key = f"{version}:{champion.id}"
        lock = self._detail_locks.setdefault(cache_key, asyncio.Lock())

        async def load() -> ChampionDetail:
            url = f"{DDRAGON_CDN}/{version}/data/zh_CN/champion/{champion.id}.json"
            payload = await self._get_json(url)
            data = payload.get("data", {}).get(champion.id)
            if not isinstance(data, dict):
                raise UpstreamUnavailable(f"Riot 未返回 {champion.id} 的英雄详情")
            fallback_splash = ""
            fallback_icon = ""
            if self.config.enable_gtimg_fallback:
                fallback_splash = (
                    "https://game.gtimg.cn/images/lol/act/img/skin/"
                    f"big{champion.key}000.jpg"
                )
                fallback_icon = (
                    "https://game.gtimg.cn/images/lol/act/img/champion/"
                    f"{champion.id}.png"
                )
            return ChampionDetail(
                id=champion.id,
                key=champion.key,
                name=str(data.get("name") or champion.name),
                title=str(data.get("title") or champion.title),
                lore=clean_text(data.get("lore") or data.get("blurb") or "暂无英雄介绍"),
                tags=tuple(str(tag) for tag in data.get("tags", []) if tag),
                version=version,
                splash_url=(
                    "https://ddragon.leagueoflegends.com/cdn/img/champion/splash/"
                    f"{champion.id}_0.jpg"
                ),
                icon_url=f"{DDRAGON_CDN}/{version}/img/champion/{champion.id}.png",
                splash_fallback_url=fallback_splash,
                icon_fallback_url=fallback_icon,
            )

        return await self._cached_mapping(
            self._details,
            cache_key,
            lock,
            load,
            f"{champion.name} 英雄详情",
            "data_dragon",
        )

    async def augments(self) -> DataResult:
        return await self._cached_single(
            entry_getter=lambda: self._augments,
            entry_setter=self._set_augments,
            lock=self._augments_lock,
            loader=self._load_augments,
            label="CommunityDragon 海克斯数据",
            source="communitydragon",
        )

    async def gtimg_aliases(self) -> DataResult:
        if not self.config.enable_gtimg_fallback:
            return DataResult({}, False, 0.0)
        return await self._cached_single(
            entry_getter=lambda: self._gtimg_aliases,
            entry_setter=self._set_gtimg_aliases,
            lock=self._gtimg_lock,
            loader=self._load_gtimg_aliases,
            label="gtimg 英雄别名",
            source="gtimg",
        )

    async def wiki_notes(self) -> DataResult:
        if not self.config.enable_wiki_enrichment:
            return DataResult({}, False, 0.0)
        return await self._cached_single(
            entry_getter=lambda: self._wiki_notes,
            entry_setter=self._set_wiki_notes,
            lock=self._wiki_lock,
            loader=self._load_wiki_notes,
            label="League Wiki 海克斯机制",
            source="wiki",
        )

    async def opgg_data(
        self,
        champion: ChampionSummary,
        catalog: list[Augment],
    ) -> DataResult:
        if not self.config.enable_opgg_source:
            return DataResult(OpggChampionData(), False, 0.0)
        key = champion.id
        lock = self._opgg_locks.setdefault(key, asyncio.Lock())

        async def load() -> OpggChampionData:
            slug = _opgg_slug(champion.id)
            urls = [f"{OPGG_BASE_URL}/{slug}/{page}" for page in ("build", "augments", "skills", "items")]
            values = await asyncio.gather(
                *(self._get_html(url) for url in urls),
                return_exceptions=True,
            )
            pages = [value for value in values if isinstance(value, str) and not is_access_challenge(value)]
            if not pages:
                errors = [str(value) for value in values if isinstance(value, BaseException)]
                reason = errors[0] if errors else "返回了访问验证页或空页面"
                raise UpstreamUnavailable(f"OP.GG 页面不可用：{reason}")
            base = parse_opgg_build_pages(*pages, page_url=urls[0])
            augment_page = values[1] if len(values) > 1 and isinstance(values[1], str) else ""
            recommendations = parse_opgg_augments(
                augment_page,
                catalog,
                self.config.max_augments_per_rarity,
            ) if augment_page else ()
            return merge_opgg_data(base, recommendations)

        return await self._cached_mapping(
            self._opgg,
            key,
            lock,
            load,
            f"{champion.name} OP.GG 统计",
            "opgg",
        )

    async def mayhempedia_routes(self, champion: ChampionSummary) -> DataResult:
        if not self.config.enable_mayhempedia_source:
            return DataResult((), False, 0.0)
        key = champion.id
        lock = self._route_locks.setdefault(key, asyncio.Lock())

        async def load() -> tuple[FullBuildRoute, ...]:
            last_error: Exception | None = None
            for slug in mayhempedia_slugs(champion.id):
                try:
                    payload = await self._get_json(f"{MAYHEMPEDIA_BUILD_BASE}/{slug}.json")
                except Exception as error:
                    last_error = error
                    continue
                routes = parse_mayhempedia_routes(payload, champion.key)
                if routes:
                    return self._version_route_icons(routes)
            if last_error is not None:
                raise UpstreamUnavailable(f"没有可用社区路线：{last_error}") from last_error
            return ()

        return await self._cached_mapping(
            self._routes,
            key,
            lock,
            load,
            f"{champion.name} Mayhempedia 路线",
            "mayhempedia",
        )

    async def hero_report(self, champion: ChampionSummary) -> HeroReport:
        detail_result = await self.champion_detail(champion)
        unavailable: list[str] = []
        stale: list[str] = ["Data Dragon"] if detail_result.stale else []

        catalog: list[Augment] = []
        catalog_result: DataResult | None = None
        try:
            catalog_result = await self.augments()
            catalog = catalog_result.value
            if catalog_result.stale:
                stale.append("CommunityDragon")
        except Exception:
            unavailable.append("CommunityDragon")

        opgg_result, route_result = await asyncio.gather(
            self.opgg_data(champion, catalog),
            self.mayhempedia_routes(champion),
            return_exceptions=True,
        )
        opgg = OpggChampionData()
        routes: tuple[FullBuildRoute, ...] = ()
        if isinstance(opgg_result, BaseException):
            if self.config.enable_opgg_source:
                unavailable.append("OP.GG")
        else:
            opgg = opgg_result.value
            if opgg_result.stale:
                stale.append("OP.GG")
        if isinstance(route_result, BaseException):
            if self.config.enable_mayhempedia_source:
                unavailable.append("Mayhempedia")
        else:
            routes = route_result.value
            if route_result.stale:
                stale.append("Mayhempedia")

        recommendations = _map_recommendations(opgg.augment_names, catalog)
        if recommendations and self.config.enable_wiki_enrichment:
            try:
                wiki_result = await self.wiki_notes()
                recommendations = _attach_top_wiki_notes(recommendations, wiki_result.value)
                if wiki_result.stale:
                    stale.append("League Wiki")
            except Exception:
                unavailable.append("League Wiki")

        return HeroReport(
            champion=detail_result.value,
            patch=opgg.patch,
            tier=opgg.tier,
            augments=recommendations,
            summoner_spells=self._with_fallbacks(opgg.summoner_spells),
            skill_order=opgg.skill_order,
            starter_items=self._with_fallbacks(opgg.starter_items),
            boots=self._items_with_fallbacks(opgg.boots),
            core_builds=self._with_fallbacks(opgg.core_builds),
            full_builds=routes,
            unavailable_sources=tuple(dict.fromkeys(unavailable)),
            stale_sources=tuple(dict.fromkeys(stale)),
        )

    async def search_augments(self, query: str) -> tuple[list[Augment], int, bool]:
        query = str(query or "").strip()
        if not query:
            return [], 0, False
        result = await self.augments()
        needle = normalize_lookup(query)
        matches = [
            item
            for item in result.value
            if any(
                needle in normalize_lookup(value)
                for value in (item.name_zh, item.name_en, item.api_name, item.id)
                if value
            )
        ]
        matches.sort(
            key=lambda item: (
                0
                if needle
                in {
                    normalize_lookup(item.name_zh),
                    normalize_lookup(item.name_en),
                    normalize_lookup(item.api_name),
                    normalize_lookup(item.id),
                }
                else 1,
                item.name_zh,
            )
        )
        selected = matches[: self.config.max_results]
        stale = result.stale
        if selected and self.config.enable_wiki_enrichment:
            try:
                wiki_result = await self.wiki_notes()
                selected = [attach_wiki_note(item, wiki_result.value) for item in selected]
                stale = stale or wiki_result.stale
            except HextechError:
                pass
        return selected, len(matches), stale

    async def image_data_uri(self, url: str) -> str:
        url = str(url or "").strip()
        if not url or not is_trusted_image_url(url):
            return ""
        if url in self._image_cache:
            return self._image_cache[url]
        try:
            raw = await self._request_bytes(url, _IMAGE_LIMIT, ("image/",), require_trusted_final=True)
            data_uri = normalize_image_data_uri(raw)
        except Exception:
            return ""
        if len(self._image_cache) >= 384:
            self._image_cache.pop(next(iter(self._image_cache)))
        self._image_cache[url] = data_uri
        return data_uri

    def status(self) -> dict[str, Any]:
        return {
            "champion_version": self._champion_version or "未加载",
            "champions": len(self._champions.value) if self._champions else 0,
            "details": len(self._details),
            "images": len(self._image_cache),
            "cache_ttl": self.config.cache_ttl_seconds,
            "sources": {
                "Data Dragon": self._source_status("data_dragon", self._champions, True),
                "CommunityDragon": self._source_status("communitydragon", self._augments, True),
                "OP.GG": self._source_status("opgg", self._newest(self._opgg), self.config.enable_opgg_source),
                "League Wiki": self._source_status("wiki", self._wiki_notes, self.config.enable_wiki_enrichment),
                "gtimg": self._source_status("gtimg", self._gtimg_aliases, self.config.enable_gtimg_fallback),
                "Mayhempedia": self._source_status("mayhempedia", self._newest(self._routes), self.config.enable_mayhempedia_source),
            },
        }

    async def _load_champions(self) -> list[ChampionSummary]:
        versions = await self._get_json(DDRAGON_VERSIONS_URL)
        if not isinstance(versions, list) or not versions:
            raise UpstreamUnavailable("Riot Data Dragon 没有返回有效版本")
        version = str(versions[0])
        payload = await self._get_json(f"{DDRAGON_CDN}/{version}/data/zh_CN/champion.json")
        raw_items = payload.get("data", {}) if isinstance(payload, dict) else {}
        items = [
            ChampionSummary(
                id=str(raw.get("id")),
                key=str(raw.get("key") or ""),
                name=str(raw.get("name") or raw.get("id")),
                title=str(raw.get("title") or ""),
                tags=tuple(str(tag) for tag in raw.get("tags", []) if tag),
            )
            for raw in raw_items.values()
            if isinstance(raw, dict) and raw.get("id")
        ]
        if not items:
            raise UpstreamUnavailable("Riot Data Dragon 英雄列表为空")
        self._champion_version = version
        return sorted(items, key=lambda item: int(item.key or 0))

    async def _load_augments(self) -> list[Augment]:
        zh_payload, en_payload = await asyncio.gather(
            self._get_json(COMMUNITYDRAGON_ZH_URL),
            self._get_json(COMMUNITYDRAGON_EN_URL),
        )
        items = parse_communitydragon_augments(zh_payload, en_payload)
        if not items:
            raise UpstreamUnavailable("CommunityDragon 海克斯列表为空")
        return items

    async def _load_wiki_notes(self) -> dict[str, str]:
        payload = await self._get_json(WIKI_AUGMENTS_URL)
        parsed = payload.get("parse", {}) if isinstance(payload, dict) else {}
        html_text = parsed.get("text", {}).get("*") if isinstance(parsed.get("text"), dict) else ""
        notes = parse_wiki_notes(str(html_text or ""))
        if not notes:
            raise UpstreamUnavailable("League Wiki 没有可用的机制说明")
        return notes

    async def _load_gtimg_aliases(self) -> dict[str, tuple[str, ...]]:
        aliases = parse_gtimg_aliases(await self._get_json(GTIMG_HERO_LIST_URL))
        if not aliases:
            raise UpstreamUnavailable("gtimg 没有返回英雄别名")
        return aliases

    def _version_route_icons(self, routes: tuple[FullBuildRoute, ...]) -> tuple[FullBuildRoute, ...]:
        output = []
        for route in routes:
            items = tuple(
                replace(
                    item,
                    icon_url=f"{DDRAGON_CDN}/{self._champion_version}/img/item/{item.id}.png",
                    extra_icon_url=(item.extra_icon_url if self.config.enable_gtimg_fallback else ""),
                )
                for item in route.items
            )
            output.append(replace(route, items=items))
        return tuple(output)

    def _with_fallbacks(self, options: tuple[LoadoutOption, ...]) -> tuple[LoadoutOption, ...]:
        return tuple(replace(option, items=self._items_with_fallbacks(option.items)) for option in options)

    def _items_with_fallbacks(self, items: tuple[ItemRef, ...]) -> tuple[ItemRef, ...]:
        if self.config.enable_gtimg_fallback:
            return items
        return tuple(replace(item, extra_icon_url="") for item in items)

    async def _cached_single(
        self,
        *,
        entry_getter: Callable[[], CacheEntry | None],
        entry_setter: Callable[[CacheEntry], None],
        lock: asyncio.Lock,
        loader: Callable[[], Awaitable[Any]],
        label: str,
        source: str,
    ) -> DataResult:
        entry = entry_getter()
        if self._is_fresh(entry):
            return DataResult(entry.value, False, entry.fetched_at)
        async with lock:
            entry = entry_getter()
            if self._is_fresh(entry):
                return DataResult(entry.value, False, entry.fetched_at)
            try:
                value = await loader()
            except Exception as error:
                self._source_errors[source] = str(error)[:240]
                if entry is not None:
                    return DataResult(entry.value, True, entry.fetched_at)
                if isinstance(error, HextechError):
                    raise
                raise UpstreamUnavailable(f"{label}获取失败：{error}") from error
            new_entry = CacheEntry(value=value, fetched_at=time.time())
            entry_setter(new_entry)
            self._source_errors.pop(source, None)
            return DataResult(value, False, new_entry.fetched_at)

    async def _cached_mapping(
        self,
        mapping: dict[str, CacheEntry],
        key: str,
        lock: asyncio.Lock,
        loader: Callable[[], Awaitable[Any]],
        label: str,
        source: str,
    ) -> DataResult:
        entry = mapping.get(key)
        if self._is_fresh(entry):
            return DataResult(entry.value, False, entry.fetched_at)
        async with lock:
            entry = mapping.get(key)
            if self._is_fresh(entry):
                return DataResult(entry.value, False, entry.fetched_at)
            try:
                value = await loader()
            except Exception as error:
                self._source_errors[source] = str(error)[:240]
                if entry is not None:
                    return DataResult(entry.value, True, entry.fetched_at)
                if isinstance(error, HextechError):
                    raise
                raise UpstreamUnavailable(f"{label}获取失败：{error}") from error
            new_entry = CacheEntry(value=value, fetched_at=time.time())
            mapping[key] = new_entry
            self._source_errors.pop(source, None)
            return DataResult(value, False, new_entry.fetched_at)

    def _is_fresh(self, entry: CacheEntry | None) -> bool:
        return bool(entry and time.time() - entry.fetched_at < self.config.cache_ttl_seconds)

    def _set_champions(self, entry: CacheEntry) -> None:
        self._champions = entry

    def _set_augments(self, entry: CacheEntry) -> None:
        self._augments = entry

    def _set_wiki_notes(self, entry: CacheEntry) -> None:
        self._wiki_notes = entry

    def _set_gtimg_aliases(self, entry: CacheEntry) -> None:
        self._gtimg_aliases = entry

    async def _get_json(self, url: str) -> Any:
        text = await self._get_text(
            url,
            (
                "application/json",
                "text/plain",
                "text/javascript",
                "application/javascript",
                "application/x-javascript",
            ),
        )
        try:
            return json.loads(text.lstrip("\ufeff"))
        except json.JSONDecodeError as error:
            raise UpstreamUnavailable(f"上游返回了无效 JSON：{url}") from error

    async def _get_html(self, url: str) -> str:
        text = await self._get_text(url, ("text/html", "application/xhtml+xml"))
        if is_access_challenge(text):
            raise UpstreamUnavailable(f"上游返回访问验证页：{urlparse(url).hostname}")
        return text

    async def _get_text(self, url: str, content_types: tuple[str, ...] = ()) -> str:
        raw = await self._request_bytes(url, _TEXT_LIMIT, content_types)
        return raw.decode("utf-8", errors="replace")

    async def _request_bytes(
        self,
        url: str,
        max_bytes: int,
        content_types: tuple[str, ...] = (),
        *,
        require_trusted_final: bool = False,
    ) -> bytes:
        await self.initialize()
        if self._session is None:
            raise UpstreamUnavailable("HTTP 会话未初始化")
        target: str | URL = url
        if (urlparse(url).hostname or "").casefold() == "wiki.leagueoflegends.com":
            # Preserve MediaWiki's encoded ':' and '/' page title. Requoting it
            # triggers the site's edge protection on some server networks.
            target = URL(url, encoded=True)
        async with self._semaphore:
            async with self._session.get(target, proxy=self.config.proxy or None, allow_redirects=True) as response:
                response.raise_for_status()
                final_url = str(response.url)
                if require_trusted_final and not is_trusted_image_url(final_url):
                    raise UpstreamUnavailable("图片重定向到了非可信域名")
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
                if content_types and content_type and not any(content_type.startswith(item) for item in content_types):
                    raise UpstreamUnavailable(f"上游 Content-Type 异常：{content_type}")
                if response.content_length and response.content_length > max_bytes:
                    raise UpstreamUnavailable(f"上游响应超过 {max_bytes} 字节限制")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UpstreamUnavailable(f"上游响应超过 {max_bytes} 字节限制")
                    chunks.append(chunk)
                return b"".join(chunks)

    def _newest(self, mapping: dict[str, CacheEntry]) -> CacheEntry | None:
        return max(mapping.values(), key=lambda item: item.fetched_at, default=None)

    def _source_status(self, source: str, entry: CacheEntry | None, enabled: bool) -> dict[str, Any]:
        if not enabled:
            return {"enabled": False, "state": "已关闭", "age": None, "error": ""}
        error = self._source_errors.get(source, "")
        if entry is not None:
            state = "旧缓存" if error else "正常"
            age = int(max(0, time.time() - entry.fetched_at))
        else:
            state = "失败" if error else "未加载"
            age = None
        return {"enabled": True, "state": state, "age": age, "error": error}


def _match_champions(items: list[ChampionSummary], needle: str) -> tuple[list[ChampionSummary], list[ChampionSummary]]:
    exact: list[ChampionSummary] = []
    partial: list[ChampionSummary] = []
    for item in items:
        fields = (item.id, item.key, item.name, item.title, *item.aliases)
        normalized = [normalize_lookup(value) for value in fields if value]
        if needle in normalized:
            exact.append(item)
        elif needle and any(needle in value for value in normalized):
            partial.append(item)
    return exact, partial


def _map_recommendations(
    raw: tuple[tuple[str, str, str, str], ...],
    catalog: list[Augment],
) -> dict[str, tuple[AugmentRecommendation, ...]]:
    index: dict[str, Augment] = {}
    for item in catalog:
        for value in (item.name_zh, item.name_en, item.api_name, item.id):
            key = normalize_lookup(value)
            if key:
                index[key] = item
    grouped: dict[str, list[AugmentRecommendation]] = {"Prismatic": [], "Gold": [], "Silver": []}
    for rank, (name, win_rate, pick_rate, games) in enumerate(raw, start=1):
        item = index.get(normalize_lookup(name))
        if item is None or item.tier not in grouped:
            continue
        grouped[item.tier].append(AugmentRecommendation(item, rank, win_rate, pick_rate, games))
    return {key: tuple(value) for key, value in grouped.items()}


def _attach_top_wiki_notes(
    grouped: dict[str, tuple[AugmentRecommendation, ...]],
    notes: dict[str, str],
) -> dict[str, tuple[AugmentRecommendation, ...]]:
    ranked = sorted((item for values in grouped.values() for item in values), key=lambda item: item.rank)
    top_ids = {item.augment.id for item in ranked[:3]}
    return {
        tier: tuple(
            replace(item, augment=attach_wiki_note(item.augment, notes))
            if item.augment.id in top_ids
            else item
            for item in values
        )
        for tier, values in grouped.items()
    }


def _opgg_slug(champion_id: str) -> str:
    normalized = normalize_lookup(champion_id)
    return {"monkeyking": "wukong"}.get(normalized, normalized)


def is_trusted_image_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host in _TRUSTED_IMAGE_HOSTS or host.endswith(".riotcdn.net")
    )


def normalize_image_data_uri(raw: bytes) -> str:
    if not raw:
        return ""
    with PILImage.open(BytesIO(raw)) as image:
        image.load()
        image.thumbnail((1600, 1600), PILImage.Resampling.LANCZOS)
        has_alpha = image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        )
        output = BytesIO()
        if has_alpha:
            image.convert("RGBA").save(output, format="PNG", optimize=True)
            mime = "image/png"
        else:
            image.convert("RGB").save(output, format="JPEG", quality=84, optimize=True, progressive=True)
            mime = "image/jpeg"
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


# Backwards-compatible name retained for callers/tests that imported it.
is_cloudflare_challenge = is_access_challenge
