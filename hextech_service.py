from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
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
    ARAMGG_BASE_URL,
    COMMUNITYDRAGON_EN_URL,
    COMMUNITYDRAGON_ZH_URL,
    MAYHEMPEDIA_BUILD_BASE,
    OPGG_BASE_URL,
    clean_text,
    communitydragon_item_url,
    is_access_challenge,
    mayhempedia_slugs,
    merge_opgg_data,
    normalize_lookup,
    parse_aramgg_champion,
    parse_aramgg_augments,
    parse_aramgg_champions,
    parse_communitydragon_augments,
    parse_mayhempedia_routes,
    parse_opgg_augments,
    parse_opgg_build_pages,
    parse_opgg_champion_rankings,
)


DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CDN = "https://ddragon.leagueoflegends.com/cdn"

_TEXT_LIMIT = 12 * 1024 * 1024
_IMAGE_LIMIT = 5 * 1024 * 1024
_TRUSTED_IMAGE_HOSTS = {
    "ddragon.leagueoflegends.com",
    "opgg-static.akamaized.net",
    "s-opgg-kit.op.gg",
    "cdn.dtodo.cn",
    "raw.communitydragon.org",
}
_SOURCE_LABELS = {
    "data_dragon": "Data Dragon",
    "opgg": "OP.GG",
    "aramgg": "ARAMGG",
    "communitydragon": "CommunityDragon",
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
    def __init__(self, config: ServiceConfig, data_dir: Path | None = None) -> None:
        self.config = config
        self.data_dir = data_dir.resolve() if data_dir is not None else None
        self.cache_dir = self.data_dir / "cache" if self.data_dir is not None else None
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._champions_lock = asyncio.Lock()
        self._augments_lock = asyncio.Lock()
        self._version_lock = asyncio.Lock()
        self._rankings_lock = asyncio.Lock()
        self._aramgg_config_lock = asyncio.Lock()
        self._detail_locks: dict[str, asyncio.Lock] = {}
        self._opgg_locks: dict[str, asyncio.Lock] = {}
        self._aramgg_locks: dict[str, asyncio.Lock] = {}
        self._route_locks: dict[str, asyncio.Lock] = {}
        self._champions: CacheEntry | None = None
        self._champion_version = ""
        self._details: dict[str, CacheEntry] = {}
        self._augments: CacheEntry | None = None
        self._opgg: dict[str, CacheEntry] = {}
        self._aramgg: dict[str, CacheEntry] = {}
        self._routes: dict[str, CacheEntry] = {}
        self._rankings: CacheEntry | None = None
        self._image_cache: dict[str, str] = {}
        self._source_errors: dict[str, str] = {}
        self._aramgg_data_version = ""
        self._aramgg_version_checked_at = 0.0

    async def initialize(self) -> None:
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
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
        self._opgg.clear()
        self._aramgg.clear()
        self._routes.clear()
        self._rankings = None
        self._image_cache.clear()
        self._source_errors.clear()

    async def champions(self) -> DataResult:
        use_aramgg = self.config.mayhem_source == "aramgg"
        return await self._cached_single(
            entry_getter=lambda: self._champions,
            entry_setter=self._set_champions,
            lock=self._champions_lock,
            loader=self._load_champions,
            label="ARAMGG 英雄列表" if use_aramgg else "Riot 英雄列表",
            source="aramgg" if use_aramgg else "data_dragon",
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
        version = await self._data_dragon_version()
        cache_key = f"{version}:{champion.id}"
        lock = self._detail_locks.setdefault(cache_key, asyncio.Lock())

        async def load() -> ChampionDetail:
            return ChampionDetail(
                id=champion.id,
                key=champion.key,
                name=champion.name,
                title=champion.title,
                tags=champion.tags,
                version=version,
                splash_url=(
                    "https://ddragon.leagueoflegends.com/cdn/img/champion/splash/"
                    f"{champion.id}_0.jpg"
                ),
                icon_url=champion.icon_url,
                splash_fallback_url="",
                icon_fallback_url="",
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
        use_aramgg = self.config.mayhem_source == "aramgg"
        return await self._cached_single(
            entry_getter=lambda: self._augments,
            entry_setter=self._set_augments,
            lock=self._augments_lock,
            loader=self._load_augments,
            label="ARAMGG 海克斯数据" if use_aramgg else "CommunityDragon 海克斯数据",
            source="aramgg" if use_aramgg else "communitydragon",
        )

    async def opgg_data(
        self,
        champion: ChampionSummary,
        catalog: list[Augment],
    ) -> DataResult:
        if self.config.mayhem_source != "opgg":
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

    async def aramgg_data(self, champion: ChampionSummary) -> DataResult:
        if self.config.mayhem_source != "aramgg":
            return DataResult(OpggChampionData(), False, 0.0)
        if not self.config.aramgg_api_key:
            raise UpstreamUnavailable("ARAMGG API Key 未配置")
        key = champion.key
        lock = self._aramgg_locks.setdefault(key, asyncio.Lock())

        async def load() -> OpggChampionData:
            try:
                payload = await self._get_json(
                    f"{ARAMGG_BASE_URL}/champions/{key}.json",
                    headers={"Authorization": f"Bearer {self.config.aramgg_api_key}"},
                )
            except aiohttp.ClientResponseError as error:
                if error.status == 401:
                    raise UpstreamUnavailable("ARAMGG API Key 无效或未授权") from error
                if error.status == 429:
                    raise UpstreamUnavailable("ARAMGG 请求频率或每日 credits 已达到上限") from error
                raise
            return parse_aramgg_champion(payload, self.config.max_augments_per_rarity)

        return await self._cached_mapping(
            self._aramgg,
            key,
            lock,
            load,
            f"{champion.name} ARAMGG 统计",
            "aramgg",
        )

    async def mayhem_data(self, champion: ChampionSummary, catalog: list[Augment]) -> DataResult:
        if self.config.mayhem_source == "aramgg":
            return await self.aramgg_data(champion)
        return await self.opgg_data(champion, catalog)

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
        catalog_label = "ARAMGG 海克斯目录" if self.config.mayhem_source == "aramgg" else "CommunityDragon"
        try:
            catalog_result = await self.augments()
            catalog = catalog_result.value
            if catalog_result.stale:
                stale.append(catalog_label)
        except Exception:
            unavailable.append(catalog_label)

        stats_result, route_result = await asyncio.gather(
            self.mayhem_data(champion, catalog),
            self.mayhempedia_routes(champion),
            return_exceptions=True,
        )
        opgg = OpggChampionData()
        routes: tuple[FullBuildRoute, ...] = ()
        source_label = "ARAMGG" if self.config.mayhem_source == "aramgg" else "OP.GG"
        if isinstance(stats_result, BaseException):
            unavailable.append(source_label)
        else:
            opgg = stats_result.value
            if stats_result.stale:
                stale.append(source_label)
        if isinstance(route_result, BaseException):
            if self.config.enable_mayhempedia_source:
                unavailable.append("Mayhempedia")
        else:
            routes = route_result.value
            if route_result.stale:
                stale.append("Mayhempedia")

        recommendations = _map_recommendations(opgg.augment_names, catalog)
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
            build_variants=self._build_variants_with_fallbacks(opgg.build_variants),
            augment_trios=opgg.augment_trios,
            item_performance=tuple(
                replace(item, item=self._items_with_fallbacks((item.item,))[0])
                for item in opgg.item_performance
            ),
            provenance=opgg.provenance,
            related_articles=opgg.related_articles,
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
        return selected, len(matches), stale

    async def champion_rankings(
        self, limit: int = 10
    ) -> tuple[list[ChampionSummary], bool]:
        champion_result = await self.champions()
        if self.config.mayhem_source == "aramgg":
            ranked = [
                item
                for item in champion_result.value
                if item.stats_rank is not None or item.stats_tier or item.win_rate is not None
            ]
            ranked.sort(key=lambda item: item.stats_rank or 9999)
            if not ranked:
                raise UpstreamUnavailable("ARAMGG 英雄榜单暂未提供排名数据")
            return ranked[: max(1, min(int(limit), 50))], champion_result.stale

        async def load() -> list[ChampionSummary]:
            html_text = await self._get_html(OPGG_BASE_URL)
            ranked = parse_opgg_champion_rankings(html_text, champion_result.value)
            if not ranked:
                raise UpstreamUnavailable("OP.GG 英雄排名页面暂未提供可解析的榜单数据")
            return ranked

        result = await self._cached_single(
            entry_getter=lambda: self._rankings,
            entry_setter=self._set_rankings,
            lock=self._rankings_lock,
            loader=load,
            label="OP.GG 英雄排名",
            source="opgg",
        )
        ranked = result.value
        if not ranked:
            raise UpstreamUnavailable("OP.GG 英雄榜单暂未提供排名数据")
        return ranked[: max(1, min(int(limit), 50))], champion_result.stale or result.stale

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
        use_aramgg = self.config.mayhem_source == "aramgg"
        return {
            "champion_version": self._champion_version or "未加载",
            "champions": len(self._champions.value) if self._champions else 0,
            "details": len(self._details),
            "images": len(self._image_cache),
            "cache_ttl": self.config.cache_ttl_seconds,
            "data_dir": str(self.data_dir) if self.data_dir is not None else "未启用",
            "disk_cache": {
                "champions": bool(
                    use_aramgg and self.cache_dir and (self.cache_dir / "aramgg_champions.json").is_file()
                ),
                "augments": bool(
                    use_aramgg and self.cache_dir and (self.cache_dir / "aramgg_augments.json").is_file()
                ),
            },
            "sources": {
                "Data Dragon": self._source_status(
                    "data_dragon",
                    self._newest_entries(self._champions if not use_aramgg else None, *self._details.values()),
                    True,
                ),
                "CommunityDragon": self._source_status(
                    "communitydragon", self._augments if not use_aramgg else None, not use_aramgg
                ),
                "OP.GG": self._source_status(
                    "opgg", self._newest_entries(self._rankings, *self._opgg.values()), not use_aramgg
                ),
                "ARAMGG": self._source_status(
                    "aramgg",
                    self._newest_entries(
                        self._champions if use_aramgg else None,
                        self._augments if use_aramgg else None,
                        *self._aramgg.values(),
                    ),
                    use_aramgg,
                ),
                "Mayhempedia": self._source_status("mayhempedia", self._newest(self._routes), self.config.enable_mayhempedia_source),
            },
        }

    async def _load_champions(self) -> list[ChampionSummary]:
        if self.config.mayhem_source == "aramgg":
            payload = await self._load_aramgg_catalog("champions")
            items = parse_aramgg_champions(payload)
            if not items:
                raise UpstreamUnavailable("ARAMGG 英雄列表为空")
            return sorted(items, key=lambda item: int(item.key or 0))

        version = await self._data_dragon_version()
        payload = await self._get_json(f"{DDRAGON_CDN}/{version}/data/zh_CN/champion.json")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise UpstreamUnavailable("Riot 英雄列表格式异常")
        items = [
            ChampionSummary(
                id=str(raw.get("id") or champion_id),
                key=str(raw.get("key") or ""),
                name=clean_text(raw.get("name")),
                title=clean_text(raw.get("title")),
                tags=tuple(str(tag) for tag in raw.get("tags", []) if tag),
                aliases=(str(raw.get("id") or champion_id),),
                icon_url=f"{DDRAGON_CDN}/{version}/img/champion/{raw.get('id') or champion_id}.png",
            )
            for champion_id, raw in data.items()
            if isinstance(raw, dict) and (raw.get("id") or champion_id) and raw.get("name")
        ]
        if not items:
            raise UpstreamUnavailable("Riot 英雄列表为空")
        return sorted(items, key=lambda item: int(item.key or 0))

    async def _load_augments(self) -> list[Augment]:
        if self.config.mayhem_source == "aramgg":
            payload = await self._load_aramgg_catalog("augments")
            items = parse_aramgg_augments(payload)
            if not items:
                raise UpstreamUnavailable("ARAMGG 海克斯列表为空")
            return items

        zh_payload, en_payload = await asyncio.gather(
            self._get_json(COMMUNITYDRAGON_ZH_URL),
            self._get_json(COMMUNITYDRAGON_EN_URL),
        )
        items = parse_communitydragon_augments(zh_payload, en_payload)
        if not items:
            raise UpstreamUnavailable("CommunityDragon 海克斯列表为空")
        return items

    async def _data_dragon_version(self) -> str:
        if self._champion_version:
            return self._champion_version
        async with self._version_lock:
            if self._champion_version:
                return self._champion_version
            versions = await self._get_json(DDRAGON_VERSIONS_URL)
            if not isinstance(versions, list) or not versions:
                raise UpstreamUnavailable("Riot Data Dragon 没有返回有效版本")
            self._champion_version = str(versions[0])
            return self._champion_version

    def _aramgg_headers(self) -> dict[str, str]:
        if not self.config.aramgg_api_key:
            raise UpstreamUnavailable("ARAMGG API Key 未配置")
        return {"Authorization": f"Bearer {self.config.aramgg_api_key}"}

    async def _load_aramgg_catalog(self, resource: str) -> dict[str, Any]:
        if resource not in {"champions", "augments"}:
            raise ValueError("不支持的 ARAMGG 本地缓存资源")
        path = self.cache_dir / f"aramgg_{resource}.json" if self.cache_dir is not None else None
        disk_payload = await asyncio.to_thread(_read_json_file, path) if path is not None else None
        try:
            remote_version = await self._aramgg_remote_version()
        except Exception:
            if disk_payload is not None:
                return disk_payload
            raise
        disk_meta = disk_payload.get("meta") if isinstance(disk_payload, dict) else None
        disk_version = str(disk_meta.get("dataVersion") or "") if isinstance(disk_meta, dict) else ""
        if disk_payload is not None and disk_version and disk_version == remote_version:
            return disk_payload
        try:
            payload = await self._get_json(
                f"{ARAMGG_BASE_URL}/{resource}.json",
                headers=self._aramgg_headers(),
            )
        except Exception:
            if disk_payload is not None:
                return disk_payload
            raise
        if not isinstance(payload, dict):
            raise UpstreamUnavailable(f"ARAMGG {resource} 返回格式异常")
        if path is not None:
            await asyncio.to_thread(_write_json_atomic, path, payload)
        return payload

    async def _aramgg_remote_version(self) -> str:
        if self._aramgg_data_version and time.time() - self._aramgg_version_checked_at < self.config.cache_ttl_seconds:
            return self._aramgg_data_version
        async with self._aramgg_config_lock:
            if self._aramgg_data_version and time.time() - self._aramgg_version_checked_at < self.config.cache_ttl_seconds:
                return self._aramgg_data_version
            payload = await self._get_json(f"{ARAMGG_BASE_URL}/config.json")
            version = str(payload.get("dataVersion") or "") if isinstance(payload, dict) else ""
            if not version:
                raise UpstreamUnavailable("ARAMGG config.json 缺少 dataVersion")
            self._aramgg_data_version = version
            self._aramgg_version_checked_at = time.time()
            return version

    def _version_route_icons(self, routes: tuple[FullBuildRoute, ...]) -> tuple[FullBuildRoute, ...]:
        output = []
        for route in routes:
            if self.config.mayhem_source == "aramgg":
                items = tuple(
                    replace(
                        item,
                        icon_url=f"https://cdn.dtodo.cn/hextech/item-icons/{item.id}.png",
                        fallback_icon_url="",
                        extra_icon_url="",
                    )
                    for item in route.items
                )
            else:
                items = tuple(
                    replace(
                        item,
                        icon_url=(
                            f"{DDRAGON_CDN}/{self._champion_version}/img/item/{item.id}.png"
                            if item.id and self._champion_version
                            else item.icon_url
                        ),
                        fallback_icon_url=communitydragon_item_url(item.id),
                        extra_icon_url="",
                    )
                    for item in route.items
                )
            output.append(replace(route, items=items))
        return tuple(output)

    def _with_fallbacks(self, options: tuple[LoadoutOption, ...]) -> tuple[LoadoutOption, ...]:
        return tuple(replace(option, items=self._items_with_fallbacks(option.items)) for option in options)

    def _build_variants_with_fallbacks(self, variants):
        return tuple(
            replace(
                variant,
                summoner_spells=self._with_fallbacks(variant.summoner_spells),
                starter_items=self._with_fallbacks(variant.starter_items),
                core_items=self._with_fallbacks(variant.core_items),
                situational_items=self._with_fallbacks(variant.situational_items),
            )
            for variant in variants
        )

    def _items_with_fallbacks(self, items: tuple[ItemRef, ...]) -> tuple[ItemRef, ...]:
        if self.config.mayhem_source == "opgg":
            return tuple(
                replace(
                    item,
                    fallback_icon_url=communitydragon_item_url(item.id),
                    extra_icon_url="",
                )
                for item in items
            )
        return tuple(
            replace(
                item,
                icon_url=(f"https://cdn.dtodo.cn/hextech/item-icons/{item.id}.png" if item.id else item.icon_url),
                fallback_icon_url="",
                extra_icon_url="",
            )
            for item in items
        )

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

    def _set_rankings(self, entry: CacheEntry) -> None:
        self._rankings = entry

    async def _get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        text = await self._get_text(
            url,
            (
                "application/json",
                "text/plain",
                "text/javascript",
                "application/javascript",
                "application/x-javascript",
            ),
            headers=headers,
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

    async def _get_text(self, url: str, content_types: tuple[str, ...] = (), *, headers: dict[str, str] | None = None) -> str:
        raw = await self._request_bytes(url, _TEXT_LIMIT, content_types, headers=headers)
        return raw.decode("utf-8", errors="replace")

    async def _request_bytes(
        self,
        url: str,
        max_bytes: int,
        content_types: tuple[str, ...] = (),
        *,
        require_trusted_final: bool = False,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        await self.initialize()
        if self._session is None:
            raise UpstreamUnavailable("HTTP 会话未初始化")
        target: str | URL = url
        async with self._semaphore:
            async with self._session.get(target, proxy=self.config.proxy or None, allow_redirects=True, headers=headers) as response:
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

    def _newest_entries(self, *entries: CacheEntry | None) -> CacheEntry | None:
        return max((entry for entry in entries if entry is not None), key=lambda item: item.fetched_at, default=None)

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


def _read_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


# Backwards-compatible name retained for callers/tests that imported it.
is_cloudflare_challenge = is_access_challenge
