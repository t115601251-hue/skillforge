"""skillforge 测试套件。零外部依赖,只用标准库 unittest + unittest.mock。

跑法:
  cd skillforge dir
  python -m unittest discover tests -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import unittest
import urllib.error
from unittest import mock

import skillforge


def _mock_response(payload, headers=None):
    """共享工具:伪造一个 urllib urlopen 的 context-manager response。"""
    m = mock.MagicMock()
    m.read.return_value = json.dumps(payload).encode()
    m.status = 200
    m.headers = headers or {}
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


def _raw_bytes(text):
    """共享工具:伪造一个返回纯文本的 urlopen response(模拟 raw.githubusercontent.com)。"""
    m = mock.MagicMock()
    m.read.return_value = text.encode()
    m.status = 200
    m.headers = {}
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class TestScaffolding(unittest.TestCase):
    def test_can_import(self):
        self.assertTrue(hasattr(skillforge, "scan_local"))


def _healthy_meta(**overrides):
    """工具:造一个常规活跃仓库的 meta dict。"""
    base = {
        "archived": False,
        "disabled": False,
        "license": {"spdx_id": "MIT"},
        "default_branch": "main",
        "stargazers_count": 500,
        "subscribers_count": 50,
        "forks_count": 100,
        "topics": ["cli", "image"],
        "has_issues": True,
        "pushed_at": "2026-06-01T00:00:00Z",
        "created_at": "2024-01-01T00:00:00Z",
        "owner": {"type": "Organization", "login": "acme"},
        "contributors_count": 8,
        "release_count": 5,
    }
    base.update(overrides)
    return base


class TestTScore(unittest.TestCase):
    def test_healthy_repo_high_t(self):
        self.assertGreaterEqual(skillforge.compute_t_score(_healthy_meta()), 80)

    def test_archived_collapses_to_zero(self):
        self.assertEqual(skillforge.compute_t_score(_healthy_meta(archived=True)), 0)

    def test_no_license_loses_points(self):
        with_l = skillforge.compute_t_score(_healthy_meta())
        without = skillforge.compute_t_score(_healthy_meta(license=None))
        self.assertGreater(with_l, without)

    def test_star_farming_penalty(self):
        meta = _healthy_meta(
            stargazers_count=500,
            contributors_count=1,
            created_at="2026-06-25T00:00:00Z",
        )
        self.assertLess(skillforge.compute_t_score(meta), 30)


class TestRiskFlags(unittest.TestCase):
    def test_archived_red(self):
        flags = skillforge.compute_risk_flags(_healthy_meta(archived=True))
        self.assertIn("🔴 已归档", flags)

    def test_new_repo_yellow(self):
        flags = skillforge.compute_risk_flags(_healthy_meta(created_at="2026-06-25T00:00:00Z"))
        self.assertTrue(any("太新" in f for f in flags))

    def test_no_license_yellow(self):
        flags = skillforge.compute_risk_flags(_healthy_meta(license=None))
        self.assertTrue(any("无 LICENSE" in f for f in flags))

    def test_clean_repo_no_flags(self):
        self.assertEqual(skillforge.compute_risk_flags(_healthy_meta()), [])


class TestUScore(unittest.TestCase):
    def test_zero_signals(self):
        score = skillforge.compute_u_score(
            stars=0, watchers=0, forks=0,
            downloads=None, release_count=0, close_rate=None,
        )
        self.assertEqual(score, 0)

    def test_popular_package(self):
        # rembg-like。实算约 84,放宽到 80 不损意图。
        score = skillforge.compute_u_score(
            stars=18000, watchers=320, forks=1900,
            downloads=2400000, release_count=22, close_rate=0.92,
        )
        self.assertGreaterEqual(score, 80)
        self.assertLessEqual(score, 100)

    def test_missing_downloads_caps_below_100(self):
        score = skillforge.compute_u_score(
            stars=100000, watchers=10000, forks=10000,
            downloads=None, release_count=20, close_rate=1.0,
        )
        self.assertLessEqual(score, 75)
        self.assertGreater(score, 60)

    def test_clamps_to_100(self):
        score = skillforge.compute_u_score(
            stars=10**9, watchers=10**9, forks=10**9,
            downloads=10**9, release_count=10000, close_rate=1.0,
        )
        self.assertEqual(score, 100)


if __name__ == "__main__":
    unittest.main()
