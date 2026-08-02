from __future__ import annotations

import unittest

from sense_roll.combo_router import ComboRouter
from sense_roll.config import ComboConfig, ComboMember


def make_combo(name: str, strategy: str, members: list[tuple[str, str]]) -> ComboConfig:
    return ComboConfig(
        name=name,
        api_formats=["openai"],
        strategy=strategy,
        members=[ComboMember(provider=p, model=m) for p, m in members],
    )


class ComboRouterTests(unittest.TestCase):
    def test_is_combo_returns_true_for_known_name(self) -> None:
        router = ComboRouter([make_combo("fast", "fill-first", [("sn", "flash")])])
        self.assertTrue(router.is_combo("fast"))
        self.assertFalse(router.is_combo("unknown"))

    def test_fill_first_returns_first_member(self) -> None:
        router = ComboRouter([
            make_combo("fast", "fill-first", [("sn", "flash"), ("ds", "chat")])
        ])
        pair = router.next_member("fast", set())
        self.assertEqual(pair, ("sn", "flash"))

    def test_fill_first_skips_attempted(self) -> None:
        router = ComboRouter([
            make_combo("fast", "fill-first", [("sn", "flash"), ("ds", "chat")])
        ])
        pair = router.next_member("fast", {("sn", "flash")})
        self.assertEqual(pair, ("ds", "chat"))

    def test_fill_first_returns_none_when_all_attempted(self) -> None:
        router = ComboRouter([
            make_combo("fast", "fill-first", [("sn", "flash"), ("ds", "chat")])
        ])
        pair = router.next_member("fast", {("sn", "flash"), ("ds", "chat")})
        self.assertIsNone(pair)

    def test_round_robin_distributes_across_requests(self) -> None:
        router = ComboRouter([
            make_combo("fast", "round-robin", [("sn", "flash"), ("ds", "chat")])
        ])
        first = router.next_member("fast", set())
        second = router.next_member("fast", set())
        self.assertNotEqual(first, second)

    def test_round_robin_skips_attempted(self) -> None:
        router = ComboRouter([
            make_combo("fast", "round-robin", [("sn", "flash"), ("ds", "chat")])
        ])
        # Force index to ("ds", "chat") first, then attempted it → must fall back to ("sn", "flash")
        first = router.next_member("fast", set())
        # second call with first result attempted
        second = router.next_member("fast", {first})
        remaining = {("sn", "flash"), ("ds", "chat")} - {first}
        self.assertEqual(second, list(remaining)[0])

    def test_list_combos(self) -> None:
        router = ComboRouter([
            make_combo("fast", "fill-first", [("sn", "flash")]),
            make_combo("slow", "fill-first", [("sn", "r1")]),
        ])
        self.assertEqual(set(router.list_combos()), {"fast", "slow"})

    def test_returns_none_for_unknown_combo(self) -> None:
        router = ComboRouter([make_combo("fast", "fill-first", [("sn", "flash")])])
        self.assertIsNone(router.next_member("ghost", set()))
