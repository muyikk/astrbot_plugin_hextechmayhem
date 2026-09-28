from __future__ import annotations

import asyncio
import json
import tempfile
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
    FullBuildRoute,
    ItemRef,
    ServiceConfig,
)
from astrbot_plugin_hextechmayhem.sources import (
    COMMUNITYDRAGON_EN_URL,
    COMMUNITYDRAGON_ZH_URL,
    parse_aramgg_champion,
    parse_aramgg_augments,
    parse_aramgg_champions,
    parse_mayhempedia_routes,
    parse_opgg_augments,
    parse_opgg_build_pages,
    parse_opgg_champion_rankings,
)

FIXTURES = Path(__file__).parent / "fixtures"


def catalog() -> list[Augment]:
    return [
        Augment("1", "Prismatic", "珠光护手", "Jeweled Gauntlet", "技能可以暴击。", icon_url="https://cdn.dtodo.cn/hextech/augment-icons/a.png", api_name="JeweledGauntlet"),
        Augment("2", "Gold", "灵魂虹吸", "Soul Siphon", "获得吸血。", api_name="SoulSiphon"),
        Augment("3", "Silver", "大力", "Blunt Force", "获得攻击力。", api_name="BluntForce"),
        Augment("4", "Prismatic", "秘术冲拳", "Mystic Punch", "普攻缩短冷却。", api_name="MysticPunch"),
    ]


class ParserTest(unittest.TestCase):
    def test_aramgg_champion_parser_reads_expanded_endpoint(self) -> None:
        payload = {
            "meta": {"gamePatch": "16.19"},
            "data": {
                "champion": {"stats": {"tier": 1}},
                "augments": [
                    {"id": 1, "name": "珠光护手", "rarityName": "prismatic", "iconUrl": "https://cdn.dtodo.cn/hextech/augment-icons/1.png", "stats": {"rank": 1, "winRate": 0.552, "pickRate": 0.123, "games": 1234, "winRateSource": "aramgg-client-upload", "winRateRegion": "WORLD", "winRateMinimumGames": 255}}
                ],
                "builds": [{
                    "label": "暴击流",
                    "games": 5000,
                    "winRate": 0.54,
                    "pickRate": 0.20,
                    "summonerSpells": [{"spells": [{"id": 4, "name": "闪现", "iconUrl": "https://cdn.dtodo.cn/hextech/summoner-spell-icons/4.png"}], "winRate": 0.51}],
                    "skillOrders": [{"skillKeys": ["Q", "E", "Q", "W", "Q", "R"], "games": 4000, "winRate": 0.55}],
                    "startingItems": [{"items": [{"id": 1001, "name": "速度之靴", "iconUrl": "https://cdn.dtodo.cn/hextech/item-icons/1001.png"}]}],
                    "coreItems": [{"items": [{"id": 3153, "name": "破败王者之刃", "iconUrl": "https://cdn.dtodo.cn/hextech/item-icons/3153.png"}]}],
                    "situationalItems": [{"item": {"id": 3036, "name": "多米尼克领主的致意", "iconUrl": "https://cdn.dtodo.cn/hextech/item-icons/3036.png"}, "pickRate": 0.12}],
                }],
                "augmentTrios": [{"augmentIds": [1, 1, 1], "stats": {"winRate": 0.61, "games": 200}}],
                "items": [{"item": {"id": 3153, "name": "破败王者之刃", "iconUrl": "https://cdn.dtodo.cn/hextech/item-icons/3153.png"}, "stats": {"winRate": 0.53, "pickRate": 0.22, "games": 999}}],
                "relatedBlogs": [{"title": "亚托克斯海斗攻略", "url": "https://example.invalid/article"}],
            },
        }
        data = parse_aramgg_champion(payload)
        self.assertEqual(data.patch, "16.19")
        self.assertEqual(data.tier, "T1")
        self.assertEqual(data.augment_names[0], ("珠光护手", "55.20%", "12.30%", "1,234"))
        self.assertEqual(data.skill_order, ("Q", "E", "W"))
        self.assertEqual(data.core_builds[0].items[0].id, "3153")
        self.assertEqual(data.build_variants[0].name, "暴击流")
        self.assertEqual(len(data.build_variants[0].skill_orders[0].order), 6)
        self.assertEqual(data.build_variants[0].situational_items[0].items[0].id, "3036")
        self.assertEqual(data.augment_trios[0].win_rate, "61.00%")
        self.assertEqual(data.item_performance[0].item.id, "3153")
        self.assertIn("最低 255 场", data.provenance[-1])
        self.assertEqual(data.related_articles[0].title, "亚托克斯海斗攻略")

    def test_aramgg_catalogs_supply_aliases_augments_and_icons(self) -> None:
        champions = parse_aramgg_champions({"data": [{"id": 266, "alias": "Aatrox", "name": "暗裔剑魔", "title": "亚托克斯", "roles": ["fighter"], "iconUrl": "https://cdn.dtodo.cn/hextech/champion-icons/266.png", "rankDelta": "+2", "stats": {"tier": 1, "winRate": 0.5312, "pickRate": 0.041, "games": None, "gamePatch": "16.19", "date": "2026-09-27", "source": "tencent", "region": "CN"}}]})
        self.assertEqual(champions[0].id, "Aatrox")
        self.assertIn("Aatrox", champions[0].aliases)
        self.assertEqual(champions[0].win_rate, 0.5312)
        self.assertIsNone(champions[0].games)
        self.assertEqual(champions[0].stats_date, "2026-09-27")
        self.assertEqual(champions[0].stats_source, "tencent")
        self.assertEqual(champions[0].stats_region, "CN")
        self.assertEqual(champions[0].rank_delta, "+2")
        items = parse_aramgg_augments({"data": [{"id": 1, "key": "JeweledGauntlet", "name": "珠光护手", "rarityName": "prismatic", "description": "<b>技能</b>可以暴击。", "iconUrl": "https://cdn.dtodo.cn/hextech/augment-icons/a.png"}]})
        self.assertEqual(items[0].tier, "Prismatic")
        self.assertEqual(items[0].description, "技能 可以暴击。")
        self.assertEqual(items[0].icon_url, "https://cdn.dtodo.cn/hextech/augment-icons/a.png")

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

    def test_opgg_ranking_parser_uses_rank_and_tier_without_inventing_win_rate(self) -> None:
        catalog_items = [
            ChampionSummary("Yasuo", "157", "亚索", "疾风剑豪"),
            ChampionSummary("Aatrox", "266", "亚托克斯", "暗裔剑魔"),
        ]
        html = (
            r'<script>self.__next_f.push([1,"champions\":[{\"key\":\"aatrox\",'
            r'\"name\":\"暗裔剑魔\",\"champion_id\":266,\"id\":266,\"tier\":2,\"rank\":8},'
            r'{\"key\":\"yasuo\",\"name\":\"疾风剑豪\",\"champion_id\":157,'
            r'\"id\":157,\"tier\":1,\"rank\":3}]"])</script>'
        )
        rankings = parse_opgg_champion_rankings(html, catalog_items)
        self.assertEqual([item.id for item in rankings], ["Yasuo", "Aatrox"])
        self.assertEqual(rankings[0].stats_rank, 3)
        self.assertEqual(rankings[0].stats_tier, "1")
        self.assertIsNone(rankings[0].win_rate)

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
            "https://cdn.dtodo.cn/hextech/augment-icons/a.png",
            "https://opgg-static.akamaized.net/item/3153.png",
        ):
            self.assertTrue(is_trusted_image_url(url))
        self.assertFalse(is_trusted_image_url("http://cdn.dtodo.cn/a.png"))
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

    async def test_aramgg_alias_matching(self) -> None:
        service = self.make_service()
        service._champions.value[1] = ChampionSummary("Aatrox", "266", "暗裔剑魔", "亚托克斯", aliases=("剑魔",))
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
        self.assertEqual(service.status()["sources"]["ARAMGG"]["state"], "旧缓存")

    async def test_aramgg_catalog_uses_versioned_disk_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            service = HextechService(ServiceConfig(), Path(temporary_dir))
            service.cache_dir.mkdir(parents=True)
            payload = {"meta": {"dataVersion": "16.19.3"}, "data": [{"id": 266}]}
            (service.cache_dir / "aramgg_champions.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            service._aramgg_remote_version = AsyncMock(return_value="16.19.3")
            service._get_json = AsyncMock()
            loaded = await service._load_aramgg_catalog("champions")
            self.assertEqual(loaded, payload)
            service._get_json.assert_not_awaited()

    async def test_opgg_mode_never_loads_aramgg_catalogs(self) -> None:
        service = HextechService(ServiceConfig(mayhem_source="opgg"))

        async def get_json(url: str, **_: object):
            self.assertNotIn("data.dtodo.cn", url)
            if url.endswith("/api/versions.json"):
                return ["16.19.1"]
            if url.endswith("/data/zh_CN/champion.json"):
                return {
                    "data": {
                        "Aatrox": {
                            "id": "Aatrox",
                            "key": "266",
                            "name": "暗裔剑魔",
                            "title": "亚托克斯",
                            "tags": ["Fighter"],
                        }
                    }
                }
            if url in {COMMUNITYDRAGON_ZH_URL, COMMUNITYDRAGON_EN_URL}:
                name = "珠光护手" if url == COMMUNITYDRAGON_ZH_URL else "Jeweled Gauntlet"
                return {
                    "augments": [
                        {
                            "id": 1,
                            "apiName": "JeweledGauntlet",
                            "name": name,
                            "rarity": 2,
                            "desc": "技能可以暴击",
                        }
                    ]
                }
            self.fail(f"意外请求：{url}")

        service._get_json = AsyncMock(side_effect=get_json)
        champions = await service._load_champions()
        augments = await service._load_augments()
        self.assertEqual(champions[0].id, "Aatrox")
        self.assertEqual(augments[0].name_zh, "珠光护手")
        self.assertTrue(
            all("data.dtodo.cn" not in call.args[0] for call in service._get_json.await_args_list)
        )
        opgg_item = service._items_with_fallbacks(
            (ItemRef("3153", "破败王者之刃", "https://opgg-static.akamaized.net/item/3153.png"),)
        )[0]
        route_item = service._version_route_icons(
            (FullBuildRoute("测试", "", (ItemRef("3153", "破败王者之刃"),)),)
        )[0].items[0]
        self.assertNotIn("dtodo.cn", opgg_item.icon_url + opgg_item.fallback_icon_url)
        self.assertNotIn("dtodo.cn", route_item.icon_url + route_item.fallback_icon_url)

    async def test_aramgg_win_rate_rankings_reuse_champion_cache(self) -> None:
        service = HextechService(ServiceConfig(mayhem_source="aramgg"))
        service._champions = CacheEntry(
            [
                ChampionSummary("Aatrox", "266", "暗裔剑魔", "亚托克斯", win_rate=0.51, pick_rate=0.04, stats_rank=2),
                ChampionSummary("Yasuo", "157", "亚索", "疾风剑豪", win_rate=0.55, pick_rate=0.08, stats_rank=1),
                ChampionSummary("Test", "999", "无统计", "", win_rate=None),
            ],
            time.time(),
        )
        service._load_champions = AsyncMock()
        rankings, stale = await service.champion_rankings(10)
        self.assertEqual([item.id for item in rankings], ["Yasuo", "Aatrox"])
        self.assertFalse(stale)
        service._load_champions.assert_not_awaited()

    async def test_opgg_rankings_use_public_page_without_aramgg(self) -> None:
        service = HextechService(ServiceConfig(mayhem_source="opgg"))
        service._champions = CacheEntry(
            [ChampionSummary("Yasuo", "157", "亚索", "疾风剑豪")], time.time()
        )
        service._get_html = AsyncMock(
            return_value=(
                r'{\"key\":\"yasuo\",\"name\":\"疾风剑豪\",'
                r'\"champion_id\":157,\"id\":157,\"tier\":1,\"rank\":3}'
            )
        )
        rankings, stale = await service.champion_rankings()
        self.assertEqual(rankings[0].stats_rank, 3)
        self.assertIsNone(rankings[0].win_rate)
        self.assertFalse(stale)
        self.assertTrue(all("dtodo.cn" not in call.args[0] for call in service._get_html.await_args_list))

    def test_lookup_normalization(self) -> None:
        self.assertEqual(normalize_lookup("Miss Fortune"), "missfortune")
        self.assertEqual(normalize_lookup("K'Sante"), "ksante")


if __name__ == "__main__":
    unittest.main()
