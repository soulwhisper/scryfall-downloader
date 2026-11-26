import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scryfall_downloader.__main__ import build_parser, env_option
from scryfall_downloader.protocol import default_port


class ConfigTest(unittest.TestCase):
    def test_prefixed_port_takes_precedence_and_legacy_port_still_works(self):
        with patch.dict(os.environ, {"DEFAULT_PORT": "18765", "DOWNLOADER_PORT": "18766"}, clear=True):
            self.assertEqual(default_port(), 18766)
        with patch.dict(os.environ, {"DEFAULT_PORT": "18765"}, clear=True):
            self.assertEqual(default_port(), 18765)
        with patch.dict(os.environ, {"DOWNLOADER_PORT": "not-a-port"}, clear=True):
            with self.assertRaisesRegex(ValueError, "DOWNLOADER_PORT"):
                default_port()

    def test_service_options_read_prefixed_environment_and_cli_wins(self):
        env = {
            "DOWNLOADER_BIND": "0.0.0.0",
            "DOWNLOADER_DATA": "/srv/cards",
            "DOWNLOADER_FORGE_EDITIONS": "/srv/forge-editions",
            "DOWNLOADER_WORKERS": "7",
            "DOWNLOADER_PROXY": "http://proxy.example:3128",
            "DOWNLOADER_UID": "10001",
            "DOWNLOADER_GID": "10001",
        }
        with patch.dict(os.environ, env, clear=True):
            args = build_parser().parse_args([])
            self.assertEqual((args.bind, args.data, args.forge_editions, args.workers),
                             ("0.0.0.0", Path("/srv/cards"), Path("/srv/forge-editions"), 7))
            self.assertEqual((args.proxy, args.uid, args.gid), (env["DOWNLOADER_PROXY"], 10001, 10001))
            args = build_parser().parse_args(["--bind", "127.0.0.1", "--workers", "2", "--data", "/tmp/other"])
            self.assertEqual((args.bind, args.workers, args.data), ("127.0.0.1", 2, Path("/tmp/other")))

    def test_one_shot_actions_are_not_implicitly_enabled_by_environment(self):
        with patch.dict(os.environ, {"DOWNLOADER_ARCHIVE_ALL": "1", "DOWNLOADER_REPAIR_OWNERSHIP": "1",
                                     "DOWNLOADER_DRY_RUN": "1"}, clear=True):
            args = build_parser().parse_args([])
            self.assertFalse(args.archive_all)
            self.assertFalse(args.repair_ownership)
            self.assertFalse(args.dry_run)

    def test_prefixed_option_lookup_does_not_read_unprefixed_values(self):
        with patch.dict(os.environ, {"DATA": "/wrong"}, clear=True):
            self.assertIsNone(env_option("DATA"))
