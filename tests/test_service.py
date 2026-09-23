from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from astrbot_plugin_hextechmayhem.hextech_service import (
    HextechService,
    is_cloudflare_challenge,
    is_trusted_image_url,
    normalize_lookup,
)
from astrbot_plugin_hextechmayhem.models import (
    Augment,
    CacheEntry,
    ChampionSummary,
    ServiceConfig,
)
from astrbot_plugin_hextechmayhem.sources import (
    attach_wiki_note,
    parse_communitydragon_augments,
    parse_gtimg_aliases,
    parse_mayhempedia_routes,
    parse_opgg_augments,
    parse_opgg_build_pages,
    parse_wiki_notes,
)

FIXTURES = Path(__file__).parent / "fixtures"


def catalog() -> list[Augment]:
    return [
        Augment("1", "Prismatic", "珠光护手", "Jeweled Gauntlet", "技能可以暴击。", icon_url="https://raw.communitydragon.org/latest/game/a.png", api_name="JeweledGauntlet"),
        Augment("2", "Gold", "灵魂虹吸", "Soul Siphon", "获得吸血。", api_name="SoulSiphon"),
        Augment("3", "Silver", "大力", "Blunt Force", "获得攻击力。", api_name="BluntForce"),
        Augment("4", "Prismatic", "秘术冲拳", "Mystic Punch", "普攻缩短冷却。", api_name="MysticPunch"),
    ]


class ParserTest(unittest.TestCase):
    def test_communitydragon_parses_chinese_and_english_without_scripts(self) -> None:
        zh = {
            "augments": [
                {
                    "id": 1,
                    "apiName": "JeweledGauntlet",
                    "name": "珠光护手",
                    "rarity": 2,
                    "desc": "<b>技能</b>可以暴击。",
                    "iconLarge": "assets/ux/icon.png",
                }
            ]
        }
        en = {"augments": [{"id": 1, "apiName": "JeweledGauntlet", "name": "Jeweled Gauntlet"}]}
        items = parse_communitydragon_augments(zh, en)
        self.assertEqual(items[0].name_en, "Jeweled Gauntlet")
        self.assertEqual(items[0].tier, "Prismatic")
        self.assertEqual(items[0].description, "技能 可以暴击。")
        self.assertEqual(items[0].icon_url, "https://raw.communitydragon.org/latest/game/assets/ux/icon.png")

    def test_opgg_parser_groups_augments_and_limits_each_rarity(self) -> None:
        html = (FIXTURES / "opgg_augments.html").read_text(encoding="utf-8")
        result = parse_opgg_augments(html, catalog(), max_per_rarity=1)
        self.assertEqual([item[0] for item in result], ["珠光护手", "灵魂虹吸", "大力"])
        self.assertEqual(result[0][1], "55.2%")
        self.assertEqual(result[0][2], "12.3%")
        self.assertNotIn("英雄技能", [item[0] for item in result])

    def test_opgg_build_parser_reads_only_labeled_sections(self) -> None:
        html = (FIXTURES / "opgg_build.html").read_text(encoding="utf-8")
        data = parse_opgg_build_pages(html, page_url="https://op.gg/test/build")
        self.assertEqual(data.patch, "16.18")
        self.assertEqual(data.tier, "2 段位")
        self.assertEqual(len(data.summoner_spells), 2)
        self.assertEqual(data.skill_order, ("Q", "E", "W"))
        self.assertEqual(len(data.starter_items), 2)
        self.assertEqual(len(data.boots), 2)
        self.assertEqual(len(data.core_builds), 3)
        self.assertEqual(data.core_builds[0].items[0].id, "3153")

    def test_wiki_parser_and_attachment(self) -> None:
        html = (FIXTURES / "wiki_augments.html").read_text(encoding="utf-8")
        notes = parse_wiki_notes(html)
        item = attach_wiki_note(catalog()[0], notes)
        self.assertIn("critical", item.wiki_note_en.casefold())
        self.assertEqual(item.mechanism, "")

    def test_gtimg_aliases_are_keyed_by_numeric_hero_id(self) -> None:
        result = parse_gtimg_aliases(
            {"hero": [{"heroId": "266", "name": "暗裔剑魔", "alias": "Aatrox", "keywords": "剑魔,亚托克斯"}]}
        )
        self.assertIn("剑魔", result["266"])
        self.assertIn("Aatrox", result["266"])

    def test_mayhempedia_accepts_real_six_item_routes_only(self) -> None:
        payload = {
            "championId": 157,
            "archetypes": [
                {"name": "暴击流", "note": "测试", "items": [{"id": 3000 + i, "name": f"装备{i}"} for i in range(6)]},
                {"name": "残缺", "items": [{"id": 1, "name": "只有一件"}]},
            ],
        }
        routes = parse_mayhempedia_routes(payload, "157")
        self.assertEqual(len(routes), 1)
        self.assertEqual(len(routes[0].items), 6)
        self.assertEqual(parse_mayhempedia_routes(payload, "266"), ())

    def test_challenge_detection_and_image_allowlist(self) -> None:
        self.assertTrue(is_cloudflare_challenge('<title>Just a moment...</title><script src="https://challenges.cloudflare.com/x"></script>'))
        self.assertFalse(is_cloudflare_challenge("<html>normal page</html>"))
        for url in (
            "https://ddragon.leagueoflegends.com/cdn/img/champion/Aatrox.png",
            "https://raw.communitydragon.org/latest/game/a.png",
            "https://opgg-static.akamaized.net/item/3153.png",
            "https://game.gtimg.cn/images/lol/a.png",
        ):
            self.assertTrue(is_trusted_image_url(url))
        self.assertFalse(is_trusted_image_url("http://game.gtimg.cn/a.png"))
        self.assertFalse(is_trusted_image_url("https://example.com/image.png"))


class MatchingTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self) -> HextechService:
        service = HextechService(ServiceConfig(cache_ttl_seconds=60))
        service._champions = CacheEntry(
            value=[
                ChampionSummary("MissFortune", "21", "赏金猎人", "厄运小姐"),
                ChampionSummary("Aatrox", "266", "暗裔剑魔", "亚托克斯"),
            ],
            fetched_at=time.time(),
        )
        service._champion_version = "16.17.1"
        return service

    async def test_champion_matching_supports_chinese_english_id_and_spaces(self) -> None:
        service = self.make_service()
        for query in ("赏金猎人", "厄运小姐", "Miss Fortune", "missfortune", "21"):
            champion, stale = await service.find_champion(query)
            self.assertEqual(champion.id, "MissFortune")
            self.assertFalse(stale)

    async def test_gtimg_alias_matching_is_optional(self) -> None:
        service = self.make_service()
        service._gtimg_aliases = CacheEntry({"266": ("剑魔",)}, time.time())
        champion, _ = await service.find_champion("剑魔")
        self.assertEqual(champion.id, "Aatrox")

    async def test_singleflight_and_stale_fallback(self) -> None:
        service = HextechService(ServiceConfig(cache_ttl_seconds=60))
        expected = [ChampionSummary("Aatrox", "266", "暗裔剑魔", "亚托克斯")]

        async def delayed_load():
            await asyncio.sleep(0.01)
            service._champion_version = "16.17.1"
            return expected

        service._load_champions = AsyncMock(side_effect=delayed_load)
        first, second = await asyncio.gather(service.champions(), service.champions())
        self.assertEqual(first.value, expected)
        self.assertEqual(second.value, expected)
        self.assertEqual(service._load_champions.await_count, 1)

        service._champions.fetched_at = 0
        service._load_champions = AsyncMock(side_effect=RuntimeError("offline"))
        stale = await service.champions()
        self.assertTrue(stale.stale)
        self.assertEqual(stale.value, expected)
        self.assertEqual(service.status()["sources"]["Data Dragon"]["state"], "旧缓存")

    def test_lookup_normalization(self) -> None:
        self.assertEqual(normalize_lookup("Miss Fortune"), "missfortune")
        self.assertEqual(normalize_lookup("K'Sante"), "ksante")


if __name__ == "__main__":
    unittest.main()
