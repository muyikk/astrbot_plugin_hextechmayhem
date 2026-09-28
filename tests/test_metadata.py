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
        self.assertEqual(config.mayhem_source, "aramgg")
        self.assertFalse(config.enable_mayhempedia_source)

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
                "mayhem_source",
                "aramgg_api_key",
                "enable_mayhempedia_source",
                "request_timeout_seconds",
                "cache_ttl_seconds",
                "max_concurrent_requests",
                "max_results",
                "max_augments_per_rarity",
                "proxy",
            },
        )

    def test_only_standalone_chinese_commands_are_registered(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command("海斗")', source)
        self.assertIn('@filter.command("海克斯")', source)
        self.assertIn('@filter.command("海斗排名")', source)
        self.assertNotIn("command_group", source)
        self.assertNotIn('@filter.command("hextech")', source)

    def test_mixed_language_and_multiword_query_recovery(self) -> None:
        roots: set[str] = set()
        heroes = {"海斗"}
        self.assertEqual(
            recover_command_query(
                "/海斗 Miss Fortune",
                "Miss",
                roots,
                heroes,
            ),
            "Miss Fortune",
        )
        self.assertEqual(
            recover_command_query(
                "/海斗 暗裔剑魔",
                "暗裔剑魔",
                roots,
                heroes,
            ),
            "暗裔剑魔",
        )


if __name__ == "__main__":
    unittest.main()
