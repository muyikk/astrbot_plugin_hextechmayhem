from __future__ import annotations

import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from astrbot_plugin_hextechmayhem.models import (
    Augment,
    AugmentRecommendation,
    AugmentTrio,
    BuildVariant,
    ChampionSummary,
    ChampionDetail,
    FullBuildRoute,
    HeroReport,
    ItemRef,
    ItemPerformance,
    LoadoutOption,
    RelatedArticle,
    SkillOrderOption,
)
from astrbot_plugin_hextechmayhem.renderer import ReportRenderer, normalize_render_result


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "#123456").save(output, format="PNG")
    return output.getvalue()


class RendererTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.renderer = ReportRenderer()
        champion = ChampionDetail(
            id="Aatrox",
            key="266",
            name="暗裔剑魔",
            title="亚托克斯",
            tags=("Fighter",),
            version="16.17.1",
            splash_url="https://example.invalid/splash.jpg",
            icon_url="",
        )
        augment = Augment(
            "1",
            "Gold",
            "<b>珠光</b>",
            "Jeweled Gauntlet",
            "技能可以暴击。",
            "暴击伤害受修正。",
        )
        items = tuple(ItemRef(str(3000 + i), f"装备{i}") for i in range(6))
        self.report = HeroReport(
            champion=champion,
            patch="16.18",
            tier="2 段位",
            augments={"Prismatic": (), "Gold": (AugmentRecommendation(augment, 1),), "Silver": ()},
            summoner_spells=(LoadoutOption(items[:2]),),
            skill_order=("Q", "E", "W"),
            starter_items=(LoadoutOption(items[:2]),),
            boots=(items[2], items[3]),
            core_builds=(LoadoutOption(items[:3]),),
            build_variants=(
                BuildVariant(
                    "暴击流",
                    "54.00%",
                    "20.00%",
                    "5,000",
                    skill_orders=(SkillOrderOption(("Q", "E", "Q", "W", "R"), "55.00%"),),
                    situational_items=(LoadoutOption(items[3:5]),),
                ),
            ),
            augment_trios=(AugmentTrio((augment, augment, augment), "61.00%", games="200"),),
            item_performance=(ItemPerformance(items[0], "53.00%", "22.00%", "999"),),
            provenance=("英雄统计：tencent / CN",),
            related_articles=(RelatedArticle("测试攻略", "https://example.invalid"),),
            full_builds=(FullBuildRoute("测试路线", "<img src=x>", items),),
            unavailable_sources=("OP.GG<script>",),
            stale_sources=("League Wiki",),
        )

    def test_hero_html_has_all_modules_and_no_skill_archive(self) -> None:
        source = self.renderer.hero_html(self.report, {})
        self.assertNotIn("英雄简介", source)
        self.assertIn("&lt;b&gt;珠光&lt;/b&gt;", source)
        self.assertIn("海克斯推荐", source)
        self.assertIn("召唤师技能", source)
        self.assertIn("技能加点顺序", source)
        self.assertIn("完整六件套", source)
        self.assertIn("流派与完整技能方案", source)
        self.assertIn("三海克斯组合", source)
        self.assertIn("热门单件表现", source)
        self.assertIn("数据说明与相关攻略", source)
        self.assertIn("Mayhempedia 社区攻略", source)
        self.assertNotIn("技能档案", source)
        self.assertNotIn("被动", source)
        self.assertIn("League Wiki 旧缓存", source)

    async def test_renderer_normalizes_bytes_to_png(self) -> None:
        async def render(*_args, **_kwargs):
            return png_bytes()

        result = await self.renderer.hero_report(render, self.report, {})
        self.assertTrue(result.startswith(b"\x89PNG"))

    def test_normalize_supports_base64_data_uri_and_file(self) -> None:
        raw = png_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        self.assertTrue(normalize_render_result(f"base64://{encoded}").startswith(b"\x89PNG"))
        self.assertTrue(normalize_render_result(f"data:image/png;base64,{encoded}").startswith(b"\x89PNG"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "render.png"
            path.write_bytes(raw)
            self.assertTrue(normalize_render_result(str(path)).startswith(b"\x89PNG"))

    def test_augment_html_contains_card_and_translated_mechanism(self) -> None:
        item = Augment(
            "1",
            "Gold",
            "珠光护手",
            "Jeweled Gauntlet",
            "技能可以暴击。",
            "暴击伤害受修正。",
        )
        source = self.renderer.augment_html([item], {}, query="珠光", total=1, stale=False)
        self.assertIn("珠光护手", source)
        self.assertIn("机制补充", source)
        self.assertIn("黄金阶", source)

    def test_ranking_html_groups_tiers_and_shows_metadata(self) -> None:
        rankings = [
            ChampionSummary(
                "Aatrox",
                "266",
                "亚托克斯",
                "暗裔剑魔",
                tags=("fighter",),
                icon_url="https://example.invalid/aatrox.png",
                win_rate=0.5312,
                pick_rate=0.041,
                stats_tier="1",
                stats_patch="16.19",
                stats_rank=1,
                stats_date="2026-09-27",
                stats_source="tencent",
                stats_region="CN",
                rank_delta="+2",
            )
        ]
        source = self.renderer.ranking_html(rankings, {}, source="aramgg", stale=False)
        self.assertIn("英雄综合强度排名", source)
        self.assertIn("PATCH 16.19", source)
        self.assertIn("2026-09-27", source)
        self.assertIn("腾讯公开快照", source)
        self.assertIn("国服", source)
        self.assertIn("T1", source)
        self.assertIn("战士", source)
        self.assertIn("53.12%", source)
        self.assertIn("+2", source)
        self.assertIn("ARAMGG", source)


if __name__ == "__main__":
    unittest.main()
