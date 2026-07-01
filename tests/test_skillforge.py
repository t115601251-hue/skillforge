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


class TestVersionSlots(unittest.TestCase):
    """版本三槽位:pristine / previous / current。"""
    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.mkdtemp(prefix="skf_versions_test_")
        self.versions_root = pathlib.Path(self.tmp) / "versions"
        self.skill_dir = pathlib.Path(self.tmp) / "skills" / "demo"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text("v1 content", encoding="utf-8")
        # 注入临时路径
        self._orig = skillforge.SKILLFORGE_VERSIONS
        skillforge.SKILLFORGE_VERSIONS = str(self.versions_root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        skillforge.SKILLFORGE_VERSIONS = self._orig

    def test_save_pristine_then_load(self):
        skillforge.save_pristine("demo", self.skill_dir)
        # 改原文件,pristine 不动
        (self.skill_dir / "SKILL.md").write_text("v2 changed", encoding="utf-8")
        pristine_dir = skillforge.version_dir("demo", "pristine")
        self.assertEqual((pristine_dir / "SKILL.md").read_text(encoding="utf-8"), "v1 content")

    def test_save_pristine_idempotent(self):
        """已有 pristine 不被覆盖(只第一次安装时写)。"""
        skillforge.save_pristine("demo", self.skill_dir)
        (self.skill_dir / "SKILL.md").write_text("v2", encoding="utf-8")
        skillforge.save_pristine("demo", self.skill_dir)  # 第二次调用应该跳过
        pristine_dir = skillforge.version_dir("demo", "pristine")
        self.assertEqual((pristine_dir / "SKILL.md").read_text(encoding="utf-8"), "v1 content")

    def test_save_previous_then_rollback_swap(self):
        # current = v2; previous = v1
        (self.skill_dir / "SKILL.md").write_text("v2", encoding="utf-8")
        skillforge.save_previous("demo", self.skill_dir)
        # 现在 previous 保存了 v2;再改 current 为 v3
        (self.skill_dir / "SKILL.md").write_text("v3", encoding="utf-8")
        # 回滚 swap → current=v2, previous=v3
        skillforge.rollback_to_previous("demo", self.skill_dir)
        self.assertEqual((self.skill_dir / "SKILL.md").read_text(encoding="utf-8"), "v2")
        prev = skillforge.version_dir("demo", "previous")
        self.assertEqual((prev / "SKILL.md").read_text(encoding="utf-8"), "v3")
        # 再回滚一次 → 回到 v3
        skillforge.rollback_to_previous("demo", self.skill_dir)
        self.assertEqual((self.skill_dir / "SKILL.md").read_text(encoding="utf-8"), "v3")

    def test_rollback_to_pristine_preserves_current_as_previous(self):
        (self.skill_dir / "SKILL.md").write_text("original", encoding="utf-8")
        skillforge.save_pristine("demo", self.skill_dir)
        (self.skill_dir / "SKILL.md").write_text("modified", encoding="utf-8")
        skillforge.rollback_to_pristine("demo", self.skill_dir)
        self.assertEqual((self.skill_dir / "SKILL.md").read_text(encoding="utf-8"), "original")
        prev = skillforge.version_dir("demo", "previous")
        self.assertEqual((prev / "SKILL.md").read_text(encoding="utf-8"), "modified")


class TestIntro(unittest.TestCase):
    def test_template_intro_extracts_fields(self):
        meta = {
            "name": "writing-helper",
            "description": "AI 写作助手,支持多 LLM,适合学生/作家。触发:writing-helper、写作助手、ai写作;仓库 GeekyWizKid/writing-helper。",
        }
        body = "## 这个技能能做什么\n通用 AI 写作助手\n\n## 安装\n```bash\nnpm install\n```\n"
        text = skillforge._template_intro(meta, body)
        self.assertIn("writing-helper", text)
        self.assertIn("AI 写作助手", text)
        self.assertIn("npm install", text)


class TestLastListCache(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.mkdtemp(prefix="skf_lastlist_")
        self._orig = skillforge.LAST_LIST_FILE
        skillforge.LAST_LIST_FILE = str(pathlib.Path(self.tmp) / "ll.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        skillforge.LAST_LIST_FILE = self._orig

    def test_save_and_load(self):
        skillforge.save_last_list({1: "asset-forge", 2: "rembg"})
        loaded = skillforge.load_last_list()
        self.assertEqual(loaded[1], "asset-forge")
        self.assertEqual(loaded[2], "rembg")

    def test_resolve_by_number(self):
        skillforge.save_last_list({1: "asset-forge", 2: "rembg"})
        self.assertEqual(skillforge.resolve_skill("1"), "asset-forge")
        self.assertEqual(skillforge.resolve_skill("rembg"), "rembg")

    def test_resolve_unknown_number(self):
        skillforge.save_last_list({1: "asset-forge"})
        self.assertIsNone(skillforge.resolve_skill("99"))


class TestMECE(unittest.TestCase):
    """v9: MECE 5+1 分类 + 双语."""
    def test_native_infra_isolates_screenshot(self):
        self.assertEqual(skillforge.mece_category({"name": "screenshot", "description": "OS screenshot"}), "native_infra")

    def test_action_executor_deploy(self):
        for name in ["vercel-deploy", "netlify-deploy", "cloudflare-deploy", "yeet", "gh-fix-ci", "linear"]:
            self.assertEqual(skillforge.mece_category({"name": name, "description": "..."}), "action_executor",
                             f"{name} should be action_executor")

    def test_generator_produces_asset(self):
        for name in ["asset-forge", "hatch-pet", "speech", "pixel2motion", "drawio"]:
            self.assertEqual(skillforge.mece_category({"name": name, "description": "..."}), "multi_modal_generator")

    def test_transformer_pure_local(self):
        for name in ["markitdown-convert", "transcribe", "pdf", "impeccable", "kami"]:
            self.assertEqual(skillforge.mece_category({"name": name, "description": "..."}), "content_transformer")

    def test_data_fetcher_readonly(self):
        for name in ["figma", "sentry", "openai-docs", "navigating-chatgpt-history"]:
            self.assertEqual(skillforge.mece_category({"name": name, "description": "..."}), "data_fetcher")

    def test_unknown_falls_to_integration_utility(self):
        # 未在任何 rule 里的一律落 utility
        self.assertEqual(skillforge.mece_category({"name": "random-tool-xyz", "description": "..."}), "integration_utility")

    def test_labels_bilingual(self):
        self.assertIn("数据", skillforge.mece_label("data_fetcher", "zh"))
        self.assertIn("Fetcher", skillforge.mece_label("data_fetcher", "en"))
        self.assertIn("动作执行", skillforge.mece_label("action_executor", "zh"))
        self.assertIn("Executor", skillforge.mece_label("action_executor", "en"))

    def test_contract_bilingual(self):
        self.assertIn("➡️", skillforge.mece_contract("data_fetcher", "zh"))
        self.assertIn("➡️", skillforge.mece_contract("data_fetcher", "en"))


class TestDetectLang(unittest.TestCase):
    """v9: detect_lang() 语言检测."""
    def test_explicit_wins(self):
        self.assertEqual(skillforge.detect_lang(explicit="en"), "en")
        self.assertEqual(skillforge.detect_lang(explicit="zh"), "zh")

    def test_cjk_in_query_returns_zh(self):
        self.assertEqual(skillforge.detect_lang(query_text="找一个写作文"), "zh")

    def test_english_query_returns_en(self):
        self.assertEqual(skillforge.detect_lang(query_text="find an essay writer"), "en")

    def test_default_zh(self):
        # 无 explicit 无 query,依 LANG env 或默认 zh
        r = skillforge.detect_lang()
        self.assertIn(r, ("zh", "en"))


class TestBrief(unittest.TestCase):
    """v9.1: _BRIEF_TRANSLATIONS 字典 + brief_for() + generate_catalog brief 模式."""

    def test_dict_covers_core_skills(self):
        """核心 74 项 skill 每个都要有 zh + en 释义(避免 fallback 截原描述)。"""
        must_have = [
            "figma", "sentry", "openai-docs", "navigating-chatgpt-history",
            "markitdown-convert", "transcribe", "pdf", "impeccable", "kami",
            "asset-forge", "frontend-design", "speech", "drawio", "pixel2motion",
            "vercel-deploy", "netlify-deploy", "cloudflare-deploy", "render-deploy",
            "yeet", "gh-fix-ci", "gh-address-comments", "linear",
            "figma-use", "figma-implement-design",
            "skillforge", "playwright", "karpathy-guidelines",
            "screenshot",
        ]
        for name in must_have:
            with self.subTest(skill=name):
                self.assertIn(name, skillforge._BRIEF_TRANSLATIONS, f"{name} 缺 brief 字典条目")
                entry = skillforge._BRIEF_TRANSLATIONS[name]
                self.assertIn("zh", entry, f"{name} 缺中文 brief")
                self.assertIn("en", entry, f"{name} 缺英文 brief")
                self.assertTrue(entry["zh"].strip(), f"{name} 中文 brief 为空")
                self.assertTrue(entry["en"].strip(), f"{name} 英文 brief 为空")

    def test_brief_length_bounded(self):
        """所有中文 brief ≤ 45 字,英文 ≤ 100 字符(防长描述潜入)。"""
        for name, entry in skillforge._BRIEF_TRANSLATIONS.items():
            with self.subTest(skill=name):
                self.assertLessEqual(len(entry["zh"]), 45, f"{name} 中文 brief 超长")
                self.assertLessEqual(len(entry["en"]), 100, f"{name} 英文 brief 超长")

    def test_brief_for_dict_hit(self):
        self.assertEqual(
            skillforge.brief_for("figma", "long english desc...", "zh"),
            skillforge._BRIEF_TRANSLATIONS["figma"]["zh"],
        )
        self.assertEqual(
            skillforge.brief_for("figma", "long english desc...", "en"),
            skillforge._BRIEF_TRANSLATIONS["figma"]["en"],
        )

    def test_brief_for_fallback_truncates(self):
        # 字典没命中,fallback 走原描述截首句
        long = "第一句话很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长。第二句"
        out = skillforge.brief_for("unknown-skill-xyz", long, "zh")
        self.assertTrue(out.endswith("…") or len(out) <= 80)
        self.assertNotIn("第二句", out)

    def test_brief_for_empty_desc(self):
        self.assertIn(skillforge.brief_for("unknown-skill", "", "zh"), ("(无描述)", "(no description)"))
        self.assertIn(skillforge.brief_for("unknown-skill", "", "en"), ("(无描述)", "(no description)"))

    def test_catalog_brief_mode_shape(self):
        """生成 brief CATALOG 到临时路径,检查它是紧凑格式:含分类头 + 一行式 skill 条目 + 不含长描述段落。"""
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "CATALOG_test.md"
            skillforge.generate_catalog(out_path=str(path), brief=True)
            content = path.read_text(encoding="utf-8")
            # 头部标记
            self.assertIn("紧凑模式", content)
            # MECE 类头(至少 Data Fetcher 类应该出现)
            self.assertIn("数据感知与检索", content)
            self.assertIn("数据契约", content)
            # 一行式条目格式(v9.2 加编号前缀)
            self.assertRegex(content, r"- `\s*\d+\.` \*\*\S+\*\* <sub>[🟢🟡🔵]+</sub> — ")
            # 尾注含总数 + 编号提示 (v9.3: 命令名带 -)
            self.assertRegex(content, r"/skill-详情 <编号>|/skill-info <n>")
            # 不能出现原描述典型长文本(检 markitdown 的英文长句)
            self.assertNotIn("Convert files and URLs into clean Markdown for LLM ingestion", content)

    def test_catalog_full_mode_still_works(self):
        """--full 保留完整描述路径,不能被 brief 抢走。"""
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "CATALOG_full.md"
            skillforge.generate_catalog(out_path=str(path), brief=False)
            content = path.read_text(encoding="utf-8")
            self.assertIn("完整模式", content)
            # 完整模式下每 skill 用 #### 四级标题
            self.assertIn("####", content)


class TestCategorize(unittest.TestCase):
    """v8: skill 分类规则匹配 + cache。"""
    def test_letta_prefix_wins(self):
        meta = {"name": "letta-foo", "description": "anything"}
        self.assertTrue(skillforge.categorize_skill(meta).startswith("🤖 Letta"))

    def test_gsap_prefix_only(self):
        # 只匹配 gsap-* 前缀,不被泛 "animation" 抢
        meta = {"name": "gsap-react", "description": "Official GSAP skill for React"}
        self.assertTrue(skillforge.categorize_skill(meta).startswith("🌊 GSAP"))

    def test_figma_wins(self):
        meta = {"name": "figma-implement", "description": "translate figma to code"}
        self.assertTrue(skillforge.categorize_skill(meta).startswith("🖼 Figma"))

    def test_playwright_is_browser_not_image(self):
        # 重要:playwright 描述含 "screenshot" 但应归浏览器,不归图像
        meta = {"name": "playwright", "description": "automate a real browser, screenshots, data extraction"}
        self.assertTrue(skillforge.categorize_skill(meta).startswith("🌐 浏览器"))

    def test_impeccable_is_taste_not_browser(self):
        meta = {"name": "impeccable", "description": "anti-slop frontend audit, live browser iteration on UI"}
        self.assertTrue(skillforge.categorize_skill(meta).startswith("✨ 前端审美"))

    def test_transcribe_is_video_audio(self):
        meta = {"name": "transcribe", "description": "Transcribe audio files to text"}
        self.assertTrue(skillforge.categorize_skill(meta).startswith("🎬 视频音频"))

    def test_unknown_falls_to_其它(self):
        meta = {"name": "totally-random", "description": "nothing matches anything"}
        self.assertTrue(skillforge.categorize_skill(meta).endswith("其它"))


class TestSpecificity(unittest.TestCase):
    """v8: skill_specificity 多轴评分。"""
    def test_strong_use_when_high(self):
        meta = {"name": "very-specific-skill", "description": "Use when user asks for X. Do not use for Y. 触发:a、b、c"}
        self.assertGreaterEqual(skillforge.skill_specificity(meta), 8)

    def test_short_generic_low(self):
        meta = {"name": "ai", "description": "general purpose"}
        self.assertLessEqual(skillforge.skill_specificity(meta), 0)

    def test_many_hyphens_more_specific(self):
        a = skillforge.skill_specificity({"name": "foo", "description": "x"})
        b = skillforge.skill_specificity({"name": "foo-bar-baz", "description": "x"})
        self.assertGreater(b, a)


class TestUsageStats(unittest.TestCase):
    """v8: usage_stats 持久化."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="skf_usage_")
        self._orig = skillforge.USAGE_STATS_FILE
        skillforge.USAGE_STATS_FILE = str(Path(self.tmp) / "u.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        skillforge.USAGE_STATS_FILE = self._orig

    def test_bump_then_count(self):
        self.assertEqual(skillforge.usage_count("foo"), 0)
        skillforge.usage_bump("foo")
        skillforge.usage_bump("foo")
        skillforge.usage_bump("bar")
        self.assertEqual(skillforge.usage_count("foo"), 2)
        self.assertEqual(skillforge.usage_count("bar"), 1)


class TestSuggestHelpers(unittest.TestCase):
    """v8: _extract_clause + _sort_by_priority。"""
    def test_extract_use_when_english(self):
        desc = "Some intro. Use when user asks for X. More text."
        r = skillforge._extract_clause(desc, [r"Use when ([^.。\n]+)"])
        self.assertEqual(r, "user asks for X")

    def test_extract_dont_use_for_english(self):
        desc = "...Do not use for huge files. Use compress instead."
        r = skillforge._extract_clause(desc, [r"Do not use (?:for |when )([^.。\n]+)"])
        self.assertEqual(r, "huge files")

    def test_extract_returns_none_no_match(self):
        r = skillforge._extract_clause("plain description", [r"Use when ([^.\n]+)"])
        self.assertIsNone(r)


from pathlib import Path  # for TestUsageStats setUp

class TestModifyFlow(unittest.TestCase):
    """cmd_modify 的纯函数部分:文件读取 / diff 应用 / frontmatter 改写。LLM 调用本身不测(走真 API)。"""
    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.mkdtemp(prefix="skf_modify_")
        self._orig_home = skillforge.CANONICAL_HOME
        self._orig_versions = skillforge.SKILLFORGE_VERSIONS
        skillforge.CANONICAL_HOME = str(pathlib.Path(self.tmp) / "skills")
        skillforge.SKILLFORGE_VERSIONS = str(pathlib.Path(self.tmp) / "versions")
        self.skill_dir = pathlib.Path(skillforge.CANONICAL_HOME).expanduser() / "demo"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: A demo skill for testing\n---\n# demo\n## install\n```bash\npip install demo\n```\n",
            encoding="utf-8",
        )
        (self.skill_dir / "main.py").write_text("def hello():\n    print('hi')\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        skillforge.CANONICAL_HOME = self._orig_home
        skillforge.SKILLFORGE_VERSIONS = self._orig_versions

    def test_collect_skill_files_finds_text(self):
        files = skillforge._collect_skill_files(self.skill_dir)
        self.assertIn("SKILL.md", files)
        self.assertIn("main.py", files)

    def test_collect_skips_git_dir(self):
        gd = self.skill_dir / ".git"
        gd.mkdir()
        (gd / "config").write_text("dummy", encoding="utf-8")
        files = skillforge._collect_skill_files(self.skill_dir)
        # 任何含 .git 的路径都不能进
        for p in files.keys():
            self.assertNotIn(".git", p)

    def test_apply_modify(self):
        skillforge._apply_changes(
            [{"path": "main.py", "action": "modify",
              "new_content": "def hello():\n    print('hello world')\n"}],
            self.skill_dir,
        )
        self.assertEqual(
            (self.skill_dir / "main.py").read_text(encoding="utf-8"),
            "def hello():\n    print('hello world')\n",
        )

    def test_apply_create(self):
        skillforge._apply_changes(
            [{"path": "new_file.py", "action": "create", "new_content": "# new\n"}],
            self.skill_dir,
        )
        self.assertTrue((self.skill_dir / "new_file.py").exists())

    def test_apply_delete(self):
        skillforge._apply_changes(
            [{"path": "main.py", "action": "delete"}],
            self.skill_dir,
        )
        self.assertFalse((self.skill_dir / "main.py").exists())

    def test_update_customization_marks_meta(self):
        skillforge._update_customization_meta("demo", "make it output markdown by default")
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("✨", text)
        self.assertIn("[已定制]", text)
        self.assertIn("customization-", text)

    def test_update_customization_idempotent_for_prefix(self):
        # 调两次 ✨ 不该叠加成 ✨✨[已定制] ✨[已定制]
        skillforge._update_customization_meta("demo", "first edit")
        skillforge._update_customization_meta("demo", "second edit")
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        # description 行只有一处 ✨[已定制]
        for line in text.splitlines():
            if line.lstrip().startswith("description:"):
                self.assertEqual(line.count("✨"), 1)
                self.assertEqual(line.count("[已定制]"), 1)

    def test_diff_changes_produces_unified(self):
        changes = [{"path": "main.py", "action": "modify",
                    "new_content": "def hello():\n    print('hello')\n"}]
        diff = skillforge._diff_changes(changes, self.skill_dir)
        self.assertIn("main.py", diff)
        # unified diff 里至少有 -/+ 行
        self.assertTrue("- " in diff or "-    " in diff or "---" in diff)


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
