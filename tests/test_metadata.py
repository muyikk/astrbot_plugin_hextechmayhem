from __future__ import annotations

import json
import unittest
from pathlib import Path

from astrbot_plugin_hextechmayhem.models import ServiceConfig
from astrbot_plugin_hextechmayhem.command_utils import recover_command_query

ROOT = Path(__file__).parents[1]


class MetadataTest(unittest.TestCase):
    def test_config_defaults_and_bounds(self) -> None:
        config = ServiceConfig.from_mapping({})
        self.assertEqual(config.request_timeout_seconds, 20)
        self.assertEqual(config.cache_ttl_seconds, 3600)
        self.assertEqual(config.max_results, 5)
        self.assertEqual(config.max_augments_per_rarity, 5)
        self.assertTrue(config.enable_opgg_source)
        self.assertTrue(config.enable_wiki_enrichment)
        self.assertTrue(config.enable_gtimg_fallback)
        self.assertTrue(config.enable_mayhempedia_source)

        bounded = ServiceConfig.from_mapping(
            {"request_timeout_seconds": 1, "max_concurrent_requests": 99}
        )
        self.assertEqual(bounded.request_timeout_seconds, 5)
        self.assertEqual(bounded.max_concurrent_requests, 10)

    def test_schema_contains_all_public_settings(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(schema),
            {
                "llm_provider_id",
                "enable_opgg_source",
                "enable_wiki_enrichment",
                "enable_gtimg_fallback",
                "enable_mayhempedia_source",
                "request_timeout_seconds",
                "cache_ttl_seconds",
                "max_concurrent_requests",
                "max_results",
                "max_augments_per_rarity",
                "proxy",
            },
        )

    def test_command_group_has_expected_aliases(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command_group("hextech", alias={"海克斯科技"})', source)
        self.assertIn('@hextech.command("hero", alias={"英雄", "海斗"})', source)
        self.assertIn('@hextech.command("augment", alias={"海克斯", "强化"})', source)

    def test_mixed_language_and_multiword_query_recovery(self) -> None:
        roots = {"hextech", "海克斯科技"}
        heroes = {"hero", "英雄", "海斗"}
        self.assertEqual(
            recover_command_query(
                "/hextech 英雄 Miss Fortune",
                "Miss",
                roots,
                heroes,
            ),
            "Miss Fortune",
        )
        self.assertEqual(
            recover_command_query(
                "/海克斯科技 hero 暗裔剑魔",
                "暗裔剑魔",
                roots,
                heroes,
            ),
            "暗裔剑魔",
        )


if __name__ == "__main__":
    unittest.main()
