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


class TestTScoreWithScorecard(unittest.TestCase):
    def test_scorecard_adds_bonus(self):
        base = skillforge.compute_t_score(_healthy_meta())
        with_sc = skillforge.compute_t_score(_healthy_meta(),
                                              scorecard={"score": 8.5, "checks": []})
        self.assertGreater(with_sc, base)
        # bonus ≈ 8.5 → +9 (rounded), clamped at 100
        self.assertLessEqual(with_sc, 100)

    def test_scorecard_none_equals_v1(self):
        v1 = skillforge.compute_t_score(_healthy_meta())
        v2 = skillforge.compute_t_score(_healthy_meta(), scorecard=None, osv_vulns=None)
        self.assertEqual(v1, v2)

    def test_osv_critical_penalty(self):
        base = skillforge.compute_t_score(_healthy_meta())
        with_vulns = skillforge.compute_t_score(
            _healthy_meta(),
            osv_vulns=[{"id": "GHSA-x", "severity": "CRITICAL", "summary": ""}] * 2,
        )
        self.assertLessEqual(with_vulns, base - 50)  # 2 crits → -60 → 封顶 -50

    def test_osv_low_no_penalty(self):
        base = skillforge.compute_t_score(_healthy_meta())
        with_low = skillforge.compute_t_score(
            _healthy_meta(),
            osv_vulns=[{"id": "x", "severity": "LOW", "summary": ""}],
        )
        self.assertEqual(with_low, base)


class TestRiskFlagsExtended(unittest.TestCase):
    def test_osv_red_flag(self):
        flags = skillforge.compute_risk_flags(
            _healthy_meta(),
            osv_vulns=[{"id": "GHSA-x", "severity": "CRITICAL", "summary": "RCE"}],
        )
        self.assertTrue(any("OSV" in f and "CRITICAL" in f.upper() for f in flags))

    def test_scorecard_low_red(self):
        flags = skillforge.compute_risk_flags(
            _healthy_meta(),
            scorecard={"score": 2.5, "checks": []},
        )
        self.assertTrue(any("Scorecard" in f and "2.5" in f for f in flags))

    def test_branch_protection_yellow(self):
        flags = skillforge.compute_risk_flags(
            _healthy_meta(),
            scorecard={"score": 8.0, "checks": [
                {"name": "Branch-Protection", "score": 0, "reason": "off"},
            ]},
        )
        self.assertTrue(any("branch protection" in f.lower() for f in flags))

    def test_binary_artifacts_yellow(self):
        flags = skillforge.compute_risk_flags(
            _healthy_meta(),
            scorecard={"score": 8.0, "checks": [
                {"name": "Binary-Artifacts", "score": 5, "reason": "found"},
            ]},
        )
        self.assertTrue(any("binary" in f.lower() for f in flags))


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


class TestFetchMetadata(unittest.TestCase):
    def test_combines_repo_contributors_releases(self):
        repo_payload = {
            "full_name": "danielgatis/rembg",
            "description": "Background remover",
            "archived": False,
            "license": {"spdx_id": "MIT"},
            "default_branch": "main",
            "stargazers_count": 18000, "subscribers_count": 320, "forks_count": 1900,
            "topics": ["bg", "ai"], "has_issues": True,
            "pushed_at": "2026-06-15T00:00:00Z",
            "created_at": "2020-01-01T00:00:00Z",
            "owner": {"type": "User", "login": "danielgatis"},
            "language": "Python",
            "clone_url": "https://github.com/danielgatis/rembg.git",
            "html_url": "https://github.com/danielgatis/rembg",
        }
        contribs = [{"login": "a"}, {"login": "b"}, {"login": "c"}, {"login": "d"}]
        releases_link = {"link": '<https://api.github.com/x?page=22>; rel="last"'}

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                _mock_response(repo_payload),
                _mock_response(contribs),
                _mock_response([{"tag_name": "v1.0"}], headers=releases_link),
            ]
            meta = skillforge.fetch_metadata("danielgatis/rembg", token="xxx")

        self.assertEqual(meta["full_name"], "danielgatis/rembg")
        self.assertGreaterEqual(meta["contributors_count"], 3)
        self.assertEqual(meta["release_count"], 22)
        self.assertGreaterEqual(meta["T"], 70)
        self.assertNotIn("🔴 已归档", meta["risk_flags"])


class TestCloseRate(unittest.TestCase):
    def test_normal(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                _mock_response({"total_count": 90}),
                _mock_response({"total_count": 10}),
            ]
            rate = skillforge.fetch_close_rate("a/b", token="x")
        self.assertAlmostEqual(rate, 0.9)

    def test_no_history(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                _mock_response({"total_count": 0}),
                _mock_response({"total_count": 0}),
            ]
            self.assertIsNone(skillforge.fetch_close_rate("a/b", token="x"))

    def test_api_error_returns_none(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 403, "x", {}, None)):
            self.assertIsNone(skillforge.fetch_close_rate("a/b", token="x"))


class TestGuessPackageName(unittest.TestCase):
    def test_pypi_setup_py(self):
        setup_py = "from setuptools import setup\nsetup(name='rembg', version='2.0')\n"
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [_raw_bytes(setup_py)]
            r = skillforge.guess_package_name("danielgatis/rembg", "main", "Python")
        self.assertEqual(r, {"ecosystem": "pypi", "name": "rembg"})

    def test_pypi_pyproject(self):
        pyproject = '[project]\nname = "rembg-cli"\nversion = "0.1"\n'
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                urllib.error.HTTPError("u", 404, "nope", {}, None),
                _raw_bytes(pyproject),
            ]
            r = skillforge.guess_package_name("a/b", "main", "Python")
        self.assertEqual(r, {"ecosystem": "pypi", "name": "rembg-cli"})

    def test_npm_package_json(self):
        pkg = '{"name": "my-tool", "version": "1.0.0"}'
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [_raw_bytes(pkg)]
            r = skillforge.guess_package_name("a/b", "main", "JavaScript")
        self.assertEqual(r, {"ecosystem": "npm", "name": "my-tool"})

    def test_cargo_toml(self):
        cargo = '[package]\nname = "ripgrep"\nversion = "1.0"\n'
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [_raw_bytes(cargo)]
            r = skillforge.guess_package_name("a/b", "main", "Rust")
        self.assertEqual(r, {"ecosystem": "cargo", "name": "ripgrep"})

    def test_unsupported_language(self):
        r = skillforge.guess_package_name("a/b", "main", "Go")
        self.assertIsNone(r)

    def test_all_files_404(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
            r = skillforge.guess_package_name("foo/bar-baz", "main", "Python")
        self.assertEqual(r, {"ecosystem": "pypi", "name": "bar-baz"})


class TestFetchDownloads(unittest.TestCase):
    def test_pypi(self):
        payload = {"data": {"last_day": 80000, "last_week": 560000, "last_month": 2400000}}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_response(payload)
            n = skillforge.fetch_downloads("pypi", "rembg")
        self.assertEqual(n, 2400000)

    def test_npm(self):
        payload = {"downloads": 150000, "package": "left-pad"}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_response(payload)
            n = skillforge.fetch_downloads("npm", "left-pad")
        self.assertEqual(n, 150000)

    def test_cargo_sums_last_30(self):
        payload = {"version_downloads": [
            {"date": "2026-06-29", "downloads": 100, "version": 1},
            {"date": "2026-06-28", "downloads": 200, "version": 1},
        ]}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_response(payload)
            n = skillforge.fetch_downloads("cargo", "ripgrep")
        self.assertEqual(n, 300)

    def test_unknown_eco_returns_none(self):
        self.assertIsNone(skillforge.fetch_downloads("conda", "anything"))

    def test_404_returns_none(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
            self.assertIsNone(skillforge.fetch_downloads("pypi", "this-pkg-doesnt-exist-9999"))


class TestLLMRewrite(unittest.TestCase):
    def test_no_key_returns_original_only(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            r = skillforge.llm_rewrite_query("批量去图片背景")
        self.assertEqual(r, ["批量去图片背景"])

    def test_with_key_parses_json(self):
        api_payload = {"content": [{"text": '["remove image background", "image background removal cli", "rembg python ai"]'}]}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = _mock_response(api_payload)
                r = skillforge.llm_rewrite_query("批量去图片背景")
        self.assertEqual(len(r), 3)
        self.assertIn("rembg python ai", r)

    def test_bad_json_returns_original(self):
        api_payload = {"content": [{"text": "对不起我不会"}]}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = _mock_response(api_payload)
                r = skillforge.llm_rewrite_query("批量去图片背景")
        self.assertEqual(r, ["批量去图片背景"])


class TestCoarseRerank(unittest.TestCase):
    def test_no_key_falls_back_to_score(self):
        cands = [
            {"full_name": "a/x", "description": "x", "language": "Py", "stargazers_count": 100,
             "subscribers_count": 0, "forks_count": 0, "U": 50, "T": 80, "risk_flags": []},
            {"full_name": "a/y", "description": "y", "language": "Py", "stargazers_count": 100,
             "subscribers_count": 0, "forks_count": 0, "U": 80, "T": 30, "risk_flags": []},
            {"full_name": "a/z", "description": "z", "language": "Py", "stargazers_count": 100,
             "subscribers_count": 0, "forks_count": 0, "U": 90, "T": 90, "risk_flags": []},
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            top = skillforge.llm_coarse_rerank("q", cands)
        self.assertEqual(top[0]["full_name"], "a/z")

    def test_with_key_parses(self):
        api_payload = {"content": [{"text": '[{"full_name": "a/y", "reason": "更相关"}, {"full_name": "a/z", "reason": "次之"}]'}]}
        cands = [{"full_name": f"a/{n}", "description": n, "language": "Py",
                  "stargazers_count": 1, "subscribers_count": 1, "forks_count": 1,
                  "U": 1, "T": 1, "risk_flags": []} for n in ["x", "y", "z"]]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = _mock_response(api_payload)
                top = skillforge.llm_coarse_rerank("q", cands)
        self.assertEqual([t["full_name"] for t in top], ["a/y", "a/z"])


class TestFinalRank(unittest.TestCase):
    def test_no_key_falls_back(self):
        cands = [
            {"full_name": "a/x", "U": 10, "T": 10, "risk_flags": [], "readme_excerpt": ""},
            {"full_name": "a/y", "U": 90, "T": 90, "risk_flags": [], "readme_excerpt": ""},
            {"full_name": "a/z", "U": 50, "T": 50, "risk_flags": [], "readme_excerpt": ""},
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            top = skillforge.llm_final_rank("q", cands)
        self.assertEqual(top[0]["full_name"], "a/y")
        for t in top:
            self.assertIn("R", t)
            self.assertIn("recommend_level", t)

    def test_with_key_parses(self):
        api_payload = {"content": [{"text": '[{"full_name": "a/x", "R": 9, "recommend_level": "强推", "why": "test", "risks": []}]'}]}
        cands = [{"full_name": "a/x", "U": 1, "T": 1, "risk_flags": [], "readme_excerpt": "..."}]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = _mock_response(api_payload)
                top = skillforge.llm_final_rank("q", cands)
        self.assertEqual(top[0]["R"], 9)
        self.assertEqual(top[0]["recommend_level"], "强推")


class TestRenderTop3(unittest.TestCase):
    def test_contains_key_fields(self):
        ranked = [{
            "full_name": "danielgatis/rembg", "R": 9,
            "recommend_level": "强推", "why": "行业标准",
            "risks": [],
        }]
        meta = {"danielgatis/rembg": {
            "full_name": "danielgatis/rembg", "language": "Python",
            "stargazers_count": 18000, "subscribers_count": 320, "forks_count": 1900,
            "monthly_downloads": 2400000, "release_count": 22, "close_rate": 0.92,
            "U": 92, "T": 85, "install_cmds": ["pip install rembg"],
        }}
        text = skillforge.render_top3("批量去图片背景", ranked, meta, trusted_set=set())
        self.assertIn("批量去图片背景", text)
        self.assertIn("danielgatis/rembg", text)
        self.assertIn("R 相关 9/10", text)
        self.assertIn("U 使用 92/100", text)
        self.assertIn("T 治理 85/100", text)
        self.assertIn("强推", text)
        self.assertIn("行业标准", text)
        self.assertIn("pip install rembg", text)

    def test_renders_risks(self):
        ranked = [{"full_name": "x/y", "R": 7, "recommend_level": "推荐",
                   "why": "...", "risks": ["仓库太新", "单一维护者"]}]
        meta = {"x/y": {"full_name": "x/y", "language": "Python",
                        "stargazers_count": 1, "subscribers_count": 1, "forks_count": 0,
                        "monthly_downloads": None, "release_count": 0, "close_rate": None,
                        "U": 5, "T": 50, "install_cmds": []}}
        text = skillforge.render_top3("q", ranked, meta, trusted_set=set())
        self.assertIn("仓库太新", text)
        self.assertIn("单一维护者", text)


class TestFetchScorecard(unittest.TestCase):
    def test_returns_score_and_checks(self):
        payload = {
            "score": 6.5,
            "checks": [
                {"name": "Binary-Artifacts", "score": 10, "reason": "no binaries"},
                {"name": "Branch-Protection", "score": 0, "reason": "not enabled"},
            ],
        }
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_response(payload)
            r = skillforge.fetch_scorecard("danielgatis/rembg")
        self.assertEqual(r["score"], 6.5)
        self.assertEqual(len(r["checks"]), 2)

    def test_404_returns_none(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
            self.assertIsNone(skillforge.fetch_scorecard("no/such-repo"))

    def test_network_error_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("dns fail")):
            self.assertIsNone(skillforge.fetch_scorecard("a/b"))


class TestFetchOSV(unittest.TestCase):
    def test_no_vulns(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_response({"vulns": []})
            r = skillforge.fetch_osv_vulns("PyPI", "rembg")
        self.assertEqual(r, [])

    def test_returns_critical_vulns(self):
        payload = {"vulns": [
            {"id": "GHSA-aaa", "summary": "RCE", "database_specific": {"severity": "CRITICAL"}},
            {"id": "GHSA-bbb", "summary": "info leak", "database_specific": {"severity": "MODERATE"}},
        ]}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_response(payload)
            r = skillforge.fetch_osv_vulns("PyPI", "vulnerable-pkg")
        self.assertEqual(len(r), 2)
        crits = [v for v in r if v["severity"] == "CRITICAL"]
        self.assertEqual(len(crits), 1)
        self.assertEqual(crits[0]["id"], "GHSA-aaa")

    def test_unknown_ecosystem_returns_empty(self):
        self.assertEqual(skillforge.fetch_osv_vulns("conda", "x"), [])

    def test_network_error_returns_empty(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("fail")):
            self.assertEqual(skillforge.fetch_osv_vulns("PyPI", "x"), [])


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
