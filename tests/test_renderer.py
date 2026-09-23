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
    ChampionDetail,
    FullBuildRoute,
    HeroReport,
    ItemRef,
    LoadoutOption,
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
            lore="<script>bad()</script> 测试英雄",
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
            full_builds=(FullBuildRoute("测试路线", "<img src=x>", items),),
            unavailable_sources=("OP.GG<script>",),
            stale_sources=("League Wiki",),
        )

    def test_hero_html_has_all_modules_and_no_skill_archive(self) -> None:
        source = self.renderer.hero_html(self.report, {})
        self.assertNotIn("<script>bad()", source)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", source)
        self.assertIn("&lt;b&gt;珠光&lt;/b&gt;", source)
        self.assertIn("海克斯推荐", source)
        self.assertIn("召唤师技能", source)
        self.assertIn("技能加点顺序", source)
        self.assertIn("完整六件套", source)
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
        self.assertIn("CommunityDragon", source)


if __name__ == "__main__":
    unittest.main()
