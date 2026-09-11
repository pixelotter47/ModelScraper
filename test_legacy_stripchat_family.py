"""Inheritance isolation fence between XHamsterLive and Stripchat."""

import unittest

from legacy_stripchat_family import (
    LegacyStripchatFamilyPaths,
    LegacyStripchatFamilyRunner,
)
from ctb_core import CTBRunner
from stripchat_core import StripchatRunner
from xhamsterlive_core import XHamsterLiveRunner


class LegacyFamilyIsolationTests(unittest.TestCase):
    def test_xhl_does_not_inherit_the_active_stripchat_runner(self):
        self.assertFalse(issubclass(XHamsterLiveRunner, StripchatRunner))

    def test_xhl_inherits_the_frozen_legacy_family(self):
        self.assertTrue(
            issubclass(XHamsterLiveRunner, LegacyStripchatFamilyRunner)
        )

    def test_legacy_family_is_a_ctb_runner(self):
        self.assertTrue(
            issubclass(LegacyStripchatFamilyRunner, CTBRunner)
        )

    def test_legacy_family_keeps_stripchat_defaults(self):
        self.assertEqual(
            LegacyStripchatFamilyRunner.platform_name, "Stripchat"
        )
        self.assertEqual(
            LegacyStripchatFamilyRunner.site_domain, "stripchat.com"
        )
        self.assertEqual(
            LegacyStripchatFamilyRunner.api_host, "go.stripchat.com"
        )
        paths = LegacyStripchatFamilyPaths("session")
        self.assertTrue(paths.vpn_list.endswith("sc_vpn_list.txt"))


if __name__ == "__main__":
    unittest.main()
