from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ChampionSummary:
    id: str
    key: str
    name: str
    title: str
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    icon_url: str = ""
    win_rate: float | None = None
    pick_rate: float | None = None
    games: int | None = None
    stats_tier: str = ""
    stats_patch: str = ""
    stats_rank: int | None = None
    stats_date: str = ""
    stats_source: str = ""
    stats_region: str = ""
    rank_delta: str = ""


@dataclass(frozen=True, slots=True)
class ChampionDetail:
    id: str
    key: str
    name: str
    title: str
    tags: tuple[str, ...]
    version: str
    splash_url: str
    icon_url: str
    splash_fallback_url: str = ""
    icon_fallback_url: str = ""


@dataclass(frozen=True, slots=True)
class Augment:
    id: str
    tier: str
    name_zh: str
    name_en: str
    description: str
    mechanism: str = ""
    icon_url: str = ""
    api_name: str = ""


@dataclass(frozen=True, slots=True)
class AugmentRecommendation:
    augment: Augment
    rank: int
    win_rate: str = ""
    pick_rate: str = ""
    games: str = ""


@dataclass(frozen=True, slots=True)
class ItemRef:
    id: str
    name: str
    icon_url: str = ""
    fallback_icon_url: str = ""
    extra_icon_url: str = ""


@dataclass(frozen=True, slots=True)
class LoadoutOption:
    items: tuple[ItemRef, ...]
    label: str = ""
    win_rate: str = ""
    pick_rate: str = ""
    games: str = ""


@dataclass(frozen=True, slots=True)
class SkillOrderOption:
    order: tuple[str, ...]
    win_rate: str = ""
    pick_rate: str = ""
    games: str = ""


@dataclass(frozen=True, slots=True)
class BuildVariant:
    name: str
    win_rate: str = ""
    pick_rate: str = ""
    games: str = ""
    summoner_spells: tuple[LoadoutOption, ...] = ()
    skill_orders: tuple[SkillOrderOption, ...] = ()
    starter_items: tuple[LoadoutOption, ...] = ()
    core_items: tuple[LoadoutOption, ...] = ()
    situational_items: tuple[LoadoutOption, ...] = ()


@dataclass(frozen=True, slots=True)
class AugmentTrio:
    augments: tuple[Augment, ...]
    win_rate: str = ""
    pick_rate: str = ""
    games: str = ""


@dataclass(frozen=True, slots=True)
class ItemPerformance:
    item: ItemRef
    win_rate: str = ""
    pick_rate: str = ""
    games: str = ""


@dataclass(frozen=True, slots=True)
class RelatedArticle:
    title: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class FullBuildRoute:
    name: str
    note: str
    items: tuple[ItemRef, ...]
    source: str = "Mayhempedia 社区攻略"


@dataclass(frozen=True, slots=True)
class OpggChampionData:
    patch: str = ""
    tier: str = ""
    augment_names: tuple[tuple[str, str, str, str], ...] = ()
    summoner_spells: tuple[LoadoutOption, ...] = ()
    skill_order: tuple[str, ...] = ()
    starter_items: tuple[LoadoutOption, ...] = ()
    boots: tuple[ItemRef, ...] = ()
    core_builds: tuple[LoadoutOption, ...] = ()
    build_variants: tuple[BuildVariant, ...] = ()
    augment_trios: tuple[AugmentTrio, ...] = ()
    item_performance: tuple[ItemPerformance, ...] = ()
    provenance: tuple[str, ...] = ()
    related_articles: tuple[RelatedArticle, ...] = ()


@dataclass(frozen=True, slots=True)
class HeroReport:
    champion: ChampionDetail
    patch: str = ""
    tier: str = ""
    augments: dict[str, tuple[AugmentRecommendation, ...]] = field(default_factory=dict)
    summoner_spells: tuple[LoadoutOption, ...] = ()
    skill_order: tuple[str, ...] = ()
    starter_items: tuple[LoadoutOption, ...] = ()
    boots: tuple[ItemRef, ...] = ()
    core_builds: tuple[LoadoutOption, ...] = ()
    build_variants: tuple[BuildVariant, ...] = ()
    augment_trios: tuple[AugmentTrio, ...] = ()
    item_performance: tuple[ItemPerformance, ...] = ()
    provenance: tuple[str, ...] = ()
    related_articles: tuple[RelatedArticle, ...] = ()
    full_builds: tuple[FullBuildRoute, ...] = ()
    unavailable_sources: tuple[str, ...] = ()
    stale_sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DataResult:
    value: Any
    stale: bool = False
    fetched_at: float = 0.0


@dataclass(slots=True)
class CacheEntry:
    value: Any
    fetched_at: float


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    mayhem_source: str = "aramgg"
    aramgg_api_key: str = ""
    enable_mayhempedia_source: bool = False
    request_timeout_seconds: int = 20
    cache_ttl_seconds: int = 3600
    max_concurrent_requests: int = 3
    max_results: int = 5
    max_augments_per_rarity: int = 5
    proxy: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "ServiceConfig":
        data = raw or {}
        return cls(
            mayhem_source=(str(data.get("mayhem_source") or "aramgg").strip().lower()
                           if str(data.get("mayhem_source") or "aramgg").strip().lower() in {"opgg", "aramgg"}
                           else "aramgg"),
            aramgg_api_key=str(data.get("aramgg_api_key") or "").strip(),
            enable_mayhempedia_source=_as_bool(
                data.get("enable_mayhempedia_source"), False
            ),
            request_timeout_seconds=_bounded_int(
                data.get("request_timeout_seconds"), 20, 5, 120
            ),
            cache_ttl_seconds=_bounded_int(
                data.get("cache_ttl_seconds"), 3600, 60, 86400
            ),
            max_concurrent_requests=_bounded_int(
                data.get("max_concurrent_requests"), 3, 1, 10
            ),
            max_results=_bounded_int(data.get("max_results"), 5, 1, 10),
            max_augments_per_rarity=_bounded_int(
                data.get("max_augments_per_rarity"), 5, 1, 10
            ),
            proxy=str(data.get("proxy") or "").strip(),
        )


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
