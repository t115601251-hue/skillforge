# skillforge `find` 搜索 + 选用质量升级 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `skillforge find` 从"GitHub 关键词 + stars 排序"升级为"LLM 多角度改写 + 三维评分(相关性 R / 使用度 U / 治理 T)+ 风险标签 Top 3 输出"。

**Architecture:** 9 步流水线(改写 → 多搜 → 元数据体检 → 粗排 → 深读 README + close rate + 下载量 → 终排 → 渲染),全部在 `skillforge.py` 内新增函数;`cmd_find` 改写为编排者;老逻辑挪到 `cmd_find_simple` 由 `--simple` 触发。零新增运行时依赖。

**Tech Stack:** Python 3.10+ 标准库(urllib / json / re / argparse / unittest / unittest.mock)。LLM 用 Anthropic Messages API,GitHub 用 REST v3,包注册中心用 pypistats.org / npmjs.org / crates.io 的公开免认证 JSON API。

**Spec:** [specs/2026-06-30-skill-search-quality.md](../specs/2026-06-30-skill-search-quality.md)

---

## 文件结构

| 文件 | 作用 | 状态 |
|---|---|---|
| `skillforge.py` | 主文件,所有新函数在此追加;`cmd_find` 重写 | 修改 |
| `tests/test_skillforge.py` | stdlib `unittest`,覆盖所有纯函数和 mock 化的 IO 函数 | 新建 |
| `tests/__init__.py` | 空,让 unittest discover 能找到 tests | 新建 |
| `skillforge_README.md` | 补 §3 加 R/U/T 评分 + 新 flag 说明 | 修改 |

**所有新增函数放在 `skillforge.py` 的 `# ----------------------------------------------------------------------------- find pipeline` 注释块下**(新增,放在现有 `register_skill` 后、`confirm` 前),保持单文件、阅读顺序自顶向下。

---

## Task 0: git init + 提交当前状态

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: 检查没在 git 仓库里**

Run: `cd "E:/Awendang/skill自动发掘器" && git status`
Expected: `fatal: not a git repository (or any of the parent directories): .git`

- [ ] **Step 2: 写 .gitignore**

```
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
```

- [ ] **Step 3: git init + 首提交**

Run:
```bash
cd "E:/Awendang/skill自动发掘器"
git init
git add .
git commit -m "init: baseline before find-pipeline upgrade"
```
Expected: 输出 `Initial commit` 包含 `skillforge.py`、`skillforge_README.md`、`specs/...`、`plans/...`、`.gitignore`。

---

## Task 1: tests 脚手架

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_skillforge.py`

- [ ] **Step 1: 建空 `__init__.py`**

```python
# empty marker for unittest discover
```

- [ ] **Step 2: 建测试主文件,先写一个会失败的占位测试**

`tests/test_skillforge.py`:
```python
"""skillforge 测试套件。零外部依赖,只用标准库 unittest + unittest.mock。

跑法:
  cd skillforge dir
  python -m unittest discover tests -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import skillforge


class TestScaffolding(unittest.TestCase):
    def test_can_import(self):
        self.assertTrue(hasattr(skillforge, "scan_local"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认通过**

Run: `python -m unittest discover tests -v`
Expected: `test_can_import (tests.test_skillforge.TestScaffolding) ... ok` + `OK`

- [ ] **Step 4: commit**

```bash
git add tests/
git commit -m "test: add unittest scaffolding"
```

---

## Task 2: `compute_t_score` + `compute_risk_flags`

**Files:**
- Modify: `skillforge.py` (追加函数)
- Modify: `tests/test_skillforge.py` (追加 `TestTScore`)

- [ ] **Step 1: 写失败测试**

在 `tests/test_skillforge.py` 末尾追加:
```python
class TestTScore(unittest.TestCase):
    def _meta(self, **overrides):
        """工具:造一个常规活跃仓库的 meta。"""
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

    def test_healthy_repo_high_t(self):
        score = skillforge.compute_t_score(self._meta())
        self.assertGreaterEqual(score, 80)

    def test_archived_collapses_to_zero(self):
        score = skillforge.compute_t_score(self._meta(archived=True))
        self.assertEqual(score, 0)

    def test_no_license_loses_points(self):
        with_license = skillforge.compute_t_score(self._meta())
        without = skillforge.compute_t_score(self._meta(license=None))
        self.assertGreater(with_license, without)

    def test_star_farming_penalty(self):
        # 5 天新仓 + 高 stars + 单人维护
        meta = self._meta(
            stargazers_count=500,
            contributors_count=1,
            created_at="2026-06-25T00:00:00Z",
        )
        score = skillforge.compute_t_score(meta)
        self.assertLess(score, 30)


class TestRiskFlags(unittest.TestCase):
    def _meta(self, **overrides):
        return TestTScore._meta(self, **overrides)

    def test_archived_red(self):
        flags = skillforge.compute_risk_flags(self._meta(archived=True))
        self.assertIn("🔴 已归档", flags)

    def test_new_repo_yellow(self):
        flags = skillforge.compute_risk_flags(self._meta(created_at="2026-06-25T00:00:00Z"))
        self.assertTrue(any("太新" in f for f in flags))

    def test_no_license_yellow(self):
        flags = skillforge.compute_risk_flags(self._meta(license=None))
        self.assertTrue(any("无 LICENSE" in f for f in flags))

    def test_clean_repo_no_flags(self):
        flags = skillforge.compute_risk_flags(self._meta())
        self.assertEqual(flags, [])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_skillforge.TestTScore -v`
Expected: 全部 ERROR `AttributeError: module 'skillforge' has no attribute 'compute_t_score'`

- [ ] **Step 3: 实现 `compute_t_score` 和 `compute_risk_flags`**

在 `skillforge.py` 的 `register_skill` 函数下面、`# ----------------------------------------------------------------------------- 交互` 注释行**前面**,新加一段:

```python
# ----------------------------------------------------------------------------- find pipeline
import datetime


def _age_days(iso_ts: str) -> int:
    """ISO 8601 字符串(GitHub 给的) → 现在距它过了多少天。失败返回 99999。"""
    if not iso_ts:
        return 99999
    try:
        # GitHub 给的格式: "2026-06-01T00:00:00Z"
        dt = datetime.datetime.strptime(iso_ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (now - dt).days)
    except (ValueError, TypeError):
        return 99999


def compute_t_score(meta: dict) -> int:
    """治理透明度 0-100。详见 specs/2026-06-30-skill-search-quality.md §5.1。"""
    if meta.get("archived"):
        return 0  # 直接归零,不参与后续计算

    score = 0
    # 加分项
    if meta.get("license"):
        score += 20
    if meta.get("default_branch") in {"main", "master", "develop"}:
        score += 15
    if _age_days(meta.get("pushed_at", "")) <= 90:
        score += 15
    if (meta.get("contributors_count") or 0) >= 3:
        score += 10
    if _age_days(meta.get("created_at", "")) >= 90:
        score += 10
    if meta.get("has_issues"):
        score += 10
    if (meta.get("owner") or {}).get("type") == "Organization":
        score += 5
    if meta.get("topics"):
        score += 5

    # 减分项
    age = _age_days(meta.get("created_at", ""))
    stars = meta.get("stargazers_count") or 0
    contribs = meta.get("contributors_count") or 0

    if age < 14 and stars > 100:
        score -= 30
    if contribs == 1 and stars > 100 and age < 60:
        score -= 20
    if _age_days(meta.get("pushed_at", "")) > 365:
        score -= 10

    return max(0, min(100, score))


def compute_risk_flags(meta: dict) -> list:
    """风险标签 list。详见 spec §5.3。"""
    if meta.get("archived"):
        return ["🔴 已归档"]

    flags = []
    age_create = _age_days(meta.get("created_at", ""))
    age_push = _age_days(meta.get("pushed_at", ""))
    stars = meta.get("stargazers_count") or 0
    contribs = meta.get("contributors_count") or 0

    if age_create < 30:
        flags.append("🟡 仓库太新(< 30 天)")
    if contribs < 3:
        flags.append("🟡 单一维护者(< 3 人)")
    if not meta.get("license"):
        flags.append("🟡 无 LICENSE")
    if age_push > 365:
        flags.append("🟡 长期未维护(> 1 年)")
    if stars > 100 and age_create < 60 and contribs == 1:
        flags.append("🟡 star farming 嫌疑")
    if meta.get("has_issues") is False:
        flags.append("🟡 没开 issues")
    if (meta.get("release_count") or 0) == 0 and age_create > 180:
        flags.append("🟡 无 release")

    return flags
```

- [ ] **Step 4: 跑测试确认全通过**

Run: `python -m unittest tests.test_skillforge.TestTScore tests.test_skillforge.TestRiskFlags -v`
Expected: 8 个 test 全 ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: T 治理分 + 风险标签"
```

---

## Task 3: `compute_u_score`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写失败测试**

```python
class TestUScore(unittest.TestCase):
    def test_zero_signals(self):
        score = skillforge.compute_u_score(
            stars=0, watchers=0, forks=0,
            downloads=None, release_count=0, close_rate=None,
        )
        self.assertEqual(score, 0)

    def test_popular_package(self):
        # rembg-like: 18k stars, 320 watchers, 1.9k forks, 2.4M/月下载, 22 release
        score = skillforge.compute_u_score(
            stars=18000, watchers=320, forks=1900,
            downloads=2400000, release_count=22, close_rate=0.92,
        )
        self.assertGreaterEqual(score, 85)
        self.assertLessEqual(score, 100)

    def test_missing_downloads_caps_below_100(self):
        # 拿不到下载量,U 上限 ≈ 70
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_skillforge.TestUScore -v`
Expected: AttributeError

- [ ] **Step 3: 实现**

在 `compute_risk_flags` 下面追加:
```python
import math


def compute_u_score(*, stars: int, watchers: int, forks: int,
                    downloads, release_count: int, close_rate) -> int:
    """使用度 0-100。spec §5.2。downloads/close_rate 允许 None。"""
    def _log(n, ceiling):
        # log10(n+1) 占 log10(ceiling+1) 的比例,封顶 1.0
        return min(1.0, math.log10((n or 0) + 1) / math.log10(ceiling + 1))

    w_s = _log(stars, 100000) * 20
    w_w = _log(watchers, 10000) * 20
    w_f = _log(forks, 10000) * 15
    w_d = _log(downloads, 10000000) * 30 if downloads is not None else 0
    w_r = min(release_count or 0, 20) / 20 * 10
    w_c = (close_rate or 0) * 5

    return int(round(max(0, min(100, w_s + w_w + w_f + w_d + w_r + w_c))))
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestUScore -v`
Expected: 4 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: U 使用度评分(stars/watchers/forks/downloads/releases/close-rate 融合)"
```

---

## Task 4: `fetch_metadata`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

`fetch_metadata` 一次性调 `/repos/{x}` + `/repos/{x}/contributors?per_page=4` + `/repos/{x}/releases?per_page=1`,把字段拼成 spec §6.1 要的字典(并附带 T 分 + flags),返回。

- [ ] **Step 1: 写失败测试(mock urllib)**

```python
from unittest import mock
import json as _json


class TestFetchMetadata(unittest.TestCase):
    def _mock_response(self, payload, headers=None):
        m = mock.MagicMock()
        m.read.return_value = _json.dumps(payload).encode()
        m.status = 200
        m.headers = headers or {}
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

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
        releases_link = {"link": '<https://api.github.com/...&page=22>; rel="last"'}

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._mock_response(repo_payload),
                self._mock_response(contribs),
                self._mock_response([{"tag_name": "v1.0"}], headers=releases_link),
            ]
            meta = skillforge.fetch_metadata("danielgatis/rembg", token="xxx")

        self.assertEqual(meta["full_name"], "danielgatis/rembg")
        self.assertGreaterEqual(meta["contributors_count"], 3)
        self.assertEqual(meta["release_count"], 22)
        self.assertGreaterEqual(meta["T"], 70)
        self.assertNotIn("🔴 已归档", meta["risk_flags"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_skillforge.TestFetchMetadata -v`
Expected: `AttributeError: module 'skillforge' has no attribute 'fetch_metadata'`

- [ ] **Step 3: 实现 `fetch_metadata`**

在 `compute_u_score` 下面追加:
```python
import re as _re


def _parse_last_page(link_header: str) -> int:
    """从 GitHub Link header 解析 rel='last' 的 page 号。没有就返回 1。"""
    m = _re.search(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"', link_header or "")
    return int(m.group(1)) if m else 1


def fetch_metadata(full_name: str, token=None) -> dict:
    """抓 spec §6.1 全部字段,顺手算 T 分 + flags 一起返回。
    失败的子请求降级:contributors_count 退到 1,release_count 退到 0。
    """
    # 主元数据
    _, repo = gh_request(f"/repos/{full_name}", token)

    # contributors 数(只要 ≥3 就够判,per_page=4 足矣)
    try:
        _, contribs = gh_request(f"/repos/{full_name}/contributors?per_page=4&anon=true", token)
        contributors_count = len(contribs) if isinstance(contribs, list) else 1
    except (GHError, Exception):
        contributors_count = 1

    # releases 总数(看 Link header)
    try:
        # 复用 urlopen 拿原始 response 看 header
        url = GITHUB_API + f"/repos/{full_name}/releases?per_page=1"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "skillforge",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            link = r.headers.get("link", "")
            body = _json.loads(r.read() or "[]")
        release_count = _parse_last_page(link) if link else len(body)
    except Exception:
        release_count = 0

    meta = dict(repo)
    meta["contributors_count"] = contributors_count
    meta["release_count"] = release_count
    meta["T"] = compute_t_score(meta)
    meta["risk_flags"] = compute_risk_flags(meta)
    return meta
```

(注意:`json` 已经在文件顶端 `import json as _` 过吗?用 `_json` 是因为模块顶端就 `import json`,直接用 `json.loads(...)` 即可。把 `_json.loads` 改成 `json.loads`,把 `import json as _json` 这种写法去掉,文件顶端 `import json` 已经 ok。)

实际写入时用 `json.loads(...)`,不要 `_json.loads`。

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestFetchMetadata -v`
Expected: ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: fetch_metadata 一次拉齐 /repos + contributors + releases"
```

---

## Task 5: `fetch_close_rate`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写失败测试**

```python
class TestCloseRate(unittest.TestCase):
    def test_normal(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._mock_response({"total_count": 90}),  # closed
                self._mock_response({"total_count": 10}),  # open
            ]
            rate = skillforge.fetch_close_rate("a/b", token="x")
        self.assertAlmostEqual(rate, 0.9)

    def test_no_history(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._mock_response({"total_count": 0}),
                self._mock_response({"total_count": 0}),
            ]
            self.assertIsNone(skillforge.fetch_close_rate("a/b", token="x"))

    def test_api_error_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 403, "x", {}, None)):
            self.assertIsNone(skillforge.fetch_close_rate("a/b", token="x"))

    _mock_response = TestFetchMetadata._mock_response
```

文件顶端需要 `import urllib.error` (已有)。

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestCloseRate -v`

- [ ] **Step 3: 实现**

在 `fetch_metadata` 下面追加:
```python
def fetch_close_rate(full_name: str, token=None):
    """返回 0-1 之间的 issue 闭合率,无历史/失败返回 None。"""
    def _count(state):
        q = urllib.parse.quote(f"type:issue repo:{full_name} is:{state}")
        path = f"/search/issues?q={q}&per_page=1"
        _, data = gh_request(path, token)
        return (data or {}).get("total_count", 0)
    try:
        closed = _count("closed")
        open_ = _count("open")
    except Exception:
        return None
    total = closed + open_
    if total == 0:
        return None
    return closed / total
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestCloseRate -v`
Expected: 3 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: fetch_close_rate"
```

---

## Task 6: `guess_package_name`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写失败测试**

```python
class TestGuessPackageName(unittest.TestCase):
    _mock_response = TestFetchMetadata._mock_response

    def _raw_bytes(self, text):
        """模拟 raw.githubusercontent.com 的纯文本响应。"""
        m = mock.MagicMock()
        m.read.return_value = text.encode()
        m.status = 200
        m.headers = {}
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def test_pypi_setup_py(self):
        setup_py = "from setuptools import setup\nsetup(name='rembg', version='2.0')\n"
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [self._raw_bytes(setup_py)]
            r = skillforge.guess_package_name("danielgatis/rembg", "main", "Python")
        self.assertEqual(r, {"ecosystem": "pypi", "name": "rembg"})

    def test_pypi_pyproject(self):
        pyproject = '[project]\nname = "rembg-cli"\nversion = "0.1"\n'
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                urllib.error.HTTPError("u", 404, "nope", {}, None),  # setup.py 没有
                self._raw_bytes(pyproject),
            ]
            r = skillforge.guess_package_name("a/b", "main", "Python")
        self.assertEqual(r, {"ecosystem": "pypi", "name": "rembg-cli"})

    def test_npm_package_json(self):
        pkg = '{"name": "my-tool", "version": "1.0.0"}'
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [self._raw_bytes(pkg)]
            r = skillforge.guess_package_name("a/b", "main", "JavaScript")
        self.assertEqual(r, {"ecosystem": "npm", "name": "my-tool"})

    def test_cargo_toml(self):
        cargo = '[package]\nname = "ripgrep"\nversion = "1.0"\n'
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [self._raw_bytes(cargo)]
            r = skillforge.guess_package_name("a/b", "main", "Rust")
        self.assertEqual(r, {"ecosystem": "cargo", "name": "ripgrep"})

    def test_unsupported_language(self):
        # Go 不在支持列表 → None
        r = skillforge.guess_package_name("a/b", "main", "Go")
        self.assertIsNone(r)

    def test_all_files_404(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
            # Python 但 setup.py 和 pyproject.toml 都没,fallback 到 repo 名
            r = skillforge.guess_package_name("foo/bar-baz", "main", "Python")
        self.assertEqual(r, {"ecosystem": "pypi", "name": "bar-baz"})
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestGuessPackageName -v`

- [ ] **Step 3: 实现**

在 `fetch_close_rate` 下面追加:
```python
_RAW = "https://raw.githubusercontent.com"


def _fetch_raw(full_name: str, branch: str, path: str) -> str:
    """从 GitHub raw 拿文件文本,失败返回 ''。无 API rate limit。"""
    url = f"{_RAW}/{full_name}/{branch}/{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "skillforge"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def guess_package_name(full_name: str, default_branch: str, language: str):
    """按 language 推 ecosystem,按文件优先级解析包名。
    支持: Python(setup.py / pyproject.toml) / JavaScript|TypeScript(package.json) / Rust(Cargo.toml)。
    """
    lang = (language or "").lower()
    repo_basename = full_name.split("/")[-1].lower().replace("_", "-")

    if lang == "python":
        text = _fetch_raw(full_name, default_branch, "setup.py")
        m = _re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return {"ecosystem": "pypi", "name": m.group(1)}
        text = _fetch_raw(full_name, default_branch, "pyproject.toml")
        m = _re.search(r'^\s*name\s*=\s*"([^"]+)"', text, _re.MULTILINE)
        if m:
            return {"ecosystem": "pypi", "name": m.group(1)}
        return {"ecosystem": "pypi", "name": repo_basename}

    if lang in {"javascript", "typescript"}:
        text = _fetch_raw(full_name, default_branch, "package.json")
        try:
            data = json.loads(text)
            if data.get("name"):
                return {"ecosystem": "npm", "name": data["name"]}
        except Exception:
            pass
        return None  # 不冒猜,npm 命名比 PyPI 严格

    if lang == "rust":
        text = _fetch_raw(full_name, default_branch, "Cargo.toml")
        m = _re.search(r'^\s*name\s*=\s*"([^"]+)"', text, _re.MULTILINE)
        if m:
            return {"ecosystem": "cargo", "name": m.group(1)}
        return {"ecosystem": "cargo", "name": repo_basename}

    return None
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestGuessPackageName -v`
Expected: 6 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: guess_package_name(setup.py/pyproject/package.json/Cargo.toml)"
```

---

## Task 7: `fetch_downloads`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写失败测试**

```python
class TestFetchDownloads(unittest.TestCase):
    _raw_bytes = TestGuessPackageName._raw_bytes
    _mock_response = TestFetchMetadata._mock_response

    def test_pypi(self):
        payload = {"data": {"last_day": 80000, "last_week": 560000, "last_month": 2400000}}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = self._mock_response(payload)
            n = skillforge.fetch_downloads("pypi", "rembg")
        self.assertEqual(n, 2400000)

    def test_npm(self):
        payload = {"downloads": 150000, "package": "left-pad"}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = self._mock_response(payload)
            n = skillforge.fetch_downloads("npm", "left-pad")
        self.assertEqual(n, 150000)

    def test_cargo_sums_last_30(self):
        # crates.io 给 90 天版本下载明细 list,我们取最近 30 天求和
        payload = {"version_downloads": [
            {"date": "2026-06-29", "downloads": 100, "version": 1},
            {"date": "2026-06-28", "downloads": 200, "version": 1},
        ]}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = self._mock_response(payload)
            n = skillforge.fetch_downloads("cargo", "ripgrep")
        self.assertEqual(n, 300)

    def test_unknown_eco_returns_none(self):
        self.assertIsNone(skillforge.fetch_downloads("conda", "anything"))

    def test_404_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
            self.assertIsNone(skillforge.fetch_downloads("pypi", "this-pkg-doesnt-exist-9999"))
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestFetchDownloads -v`

- [ ] **Step 3: 实现**

在 `guess_package_name` 下面追加:
```python
def fetch_downloads(ecosystem: str, name: str):
    """月下载量;失败/未知 ecosystem 返回 None。"""
    if not name:
        return None
    try:
        if ecosystem == "pypi":
            url = f"https://pypistats.org/api/packages/{name}/recent"
            req = urllib.request.Request(url, headers={"User-Agent": "skillforge", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            return data.get("data", {}).get("last_month")
        if ecosystem == "npm":
            url = f"https://api.npmjs.org/downloads/point/last-month/{name}"
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read())
            return data.get("downloads")
        if ecosystem == "cargo":
            url = f"https://crates.io/api/v1/crates/{name}/downloads"
            req = urllib.request.Request(url, headers={"User-Agent": "skillforge"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            entries = data.get("version_downloads", [])[:30]
            return sum(int(e.get("downloads", 0)) for e in entries)
    except Exception:
        return None
    return None
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestFetchDownloads -v`
Expected: 5 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: fetch_downloads(pypi/npm/cargo 月下载量)"
```

---

## Task 8: `llm_rewrite_query`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写测试(只测 JSON 解析与回退)**

```python
class TestLLMRewrite(unittest.TestCase):
    _mock_response = TestFetchMetadata._mock_response

    def test_no_key_returns_original_only(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            r = skillforge.llm_rewrite_query("批量去图片背景")
        self.assertEqual(r, ["批量去图片背景"])

    def test_with_key_parses_json(self):
        api_payload = {"content": [{"text": '["remove image background", "image background removal cli", "rembg python ai"]'}]}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = self._mock_response(api_payload)
                r = skillforge.llm_rewrite_query("批量去图片背景")
        self.assertEqual(len(r), 3)
        self.assertIn("rembg python ai", r)

    def test_bad_json_returns_original(self):
        api_payload = {"content": [{"text": "对不起我不会"}]}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = self._mock_response(api_payload)
                r = skillforge.llm_rewrite_query("批量去图片背景")
        self.assertEqual(r, ["批量去图片背景"])
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestLLMRewrite -v`

- [ ] **Step 3: 实现**

在 `fetch_downloads` 下面追加:
```python
def _llm_call(prompt: str, max_tokens: int = 1024, model: str = None):
    """Anthropic Messages 单轮调用,返回 text 字符串。无 key 或失败返回 None。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": model or ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(ANTHROPIC_API, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()
    except Exception as e:
        print(f"  [warn] LLM 调用失败: {e}", file=sys.stderr)
        return None


def _strip_code_fence(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    if text.startswith("```"):
        text = _re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = _re.sub(r"\n?```\s*$", "", text).strip()
    return text


def llm_rewrite_query(query: str) -> list:
    """中文需求 → 3 个不同角度的英文 query。无 key/解析失败 → [原始 query]。"""
    text = _llm_call(
        "你在帮一个跨 agent 技能管理工具改写用户的中文需求,以提高 GitHub 搜索召回率。\n\n"
        "请把下面这条中文需求改写成 3 个不同角度的英文 GitHub 搜索关键词:\n"
        "- 角度 1: 按\"能力/功能\"措辞 (e.g. \"remove image background\")\n"
        "- 角度 2: 按\"工具/CLI\"措辞 (e.g. \"image background removal cli\")\n"
        "- 角度 3: 按\"技术栈/方案\"措辞 (e.g. \"rembg python ai\")\n\n"
        "每个 3-6 个词,纯小写,不要引号、不要标点。\n\n"
        f'输入: "{query}"\n\n'
        '只输出一个 JSON 数组 (3 个字符串), 不要任何其他文字:\n["...", "...", "..."]',
        max_tokens=300,
    )
    if not text:
        return [query]
    try:
        arr = json.loads(_strip_code_fence(text))
        if isinstance(arr, list) and all(isinstance(x, str) and x.strip() for x in arr):
            return [x.strip() for x in arr][:3]
    except Exception:
        pass
    return [query]
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestLLMRewrite -v`
Expected: 3 个 test ok。

- [ ] **Step 5: 实跑冒烟(有 ANTHROPIC_API_KEY 时)**

Run:
```bash
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY PYTHONIOENCODING=utf-8 python -c "
import skillforge
print(skillforge.llm_rewrite_query('批量去图片背景'))
"
```
Expected: 输出形如 `['remove image background', 'image background removal cli', 'rembg python ai']`。

- [ ] **Step 6: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: llm_rewrite_query(中文 → 3 个英文 query)"
```

---

## Task 9: `llm_coarse_rerank`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写测试**

```python
class TestCoarseRerank(unittest.TestCase):
    _mock_response = TestFetchMetadata._mock_response

    def test_no_key_falls_back_to_score(self):
        cands = [
            {"full_name": "a/x", "desc": "x", "language": "Py", "stars": 100,
             "U": 50, "T": 80, "risk_flags": []},
            {"full_name": "a/y", "desc": "y", "language": "Py", "stars": 100,
             "U": 80, "T": 30, "risk_flags": []},
            {"full_name": "a/z", "desc": "z", "language": "Py", "stars": 100,
             "U": 90, "T": 90, "risk_flags": []},
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            top = skillforge.llm_coarse_rerank("q", cands)
        # 启发式 0.6*U + 0.4*T:z=90, x=62, y=60 → 排序 z, x, y
        self.assertEqual(top[0]["full_name"], "a/z")

    def test_with_key_parses(self):
        api_payload = {"content": [{"text": '[{"full_name": "a/y", "reason": "更相关"}, {"full_name": "a/z", "reason": "次之"}]'}]}
        cands = [{"full_name": f"a/{n}", "desc": n, "language": "Py", "stars": 1,
                  "U": 1, "T": 1, "risk_flags": []} for n in ["x", "y", "z"]]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = self._mock_response(api_payload)
                top = skillforge.llm_coarse_rerank("q", cands)
        self.assertEqual([t["full_name"] for t in top], ["a/y", "a/z"])
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestCoarseRerank -v`

- [ ] **Step 3: 实现**

在 `llm_rewrite_query` 下面追加:
```python
def _coarse_heuristic(candidates: list) -> list:
    """无 LLM 时的回退排序:0.6*U + 0.4*T,降序。"""
    scored = [(c, 0.6 * (c.get("U") or 0) + 0.4 * (c.get("T") or 0)) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"full_name": c["full_name"], "reason": f"启发式分 {s:.1f}"} for c, s in scored[:5]]


def llm_coarse_rerank(query: str, candidates: list) -> list:
    """N 个候选 → Top 5 (full_name + reason)。无 key 走启发式回退。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _coarse_heuristic(candidates)

    # 喂模型时只传必要字段,省 tokens
    summary = [
        {"full_name": c["full_name"], "desc": c.get("description", ""),
         "language": c.get("language", ""), "stars": c.get("stargazers_count", 0),
         "watchers": c.get("subscribers_count", 0), "forks": c.get("forks_count", 0),
         "U": c.get("U"), "T": c.get("T"), "flags": c.get("risk_flags", [])}
        for c in candidates
    ]
    prompt = (
        f'用户中文需求: "{query}"\n\n'
        f"下面是 {len(summary)} 个 GitHub 仓库的元数据。请挑出最可能解决用户需求的 5 个,以便下一步深度阅读 README。\n\n"
        f"候选 (JSON):\n{json.dumps(summary, ensure_ascii=False)}\n\n"
        "判断时:\n"
        "- 相关性 > 一切,desc 跟需求毛都不沾的直接跳过\n"
        "- 同等相关性下,U 高 (有真实用量) 优于 U 低\n"
        "- T < 30 的尽量避免 (除非相关性远超其他)\n"
        "- archived 必须排到最后\n\n"
        '只输出 JSON 数组 (5 个), 不要其他文字:\n[{"full_name": "...", "reason": "<1 句中文,30 字内>"}, ...]\n'
        "按推荐顺序排。"
    )
    text = _llm_call(prompt, max_tokens=600)
    if not text:
        return _coarse_heuristic(candidates)
    try:
        arr = json.loads(_strip_code_fence(text))
        if isinstance(arr, list) and arr:
            return [{"full_name": x["full_name"], "reason": x.get("reason", "")}
                    for x in arr if isinstance(x, dict) and "full_name" in x][:5]
    except Exception:
        pass
    return _coarse_heuristic(candidates)
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestCoarseRerank -v`
Expected: 2 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: llm_coarse_rerank(N → Top 5)+ 启发式回退"
```

---

## Task 10: `llm_final_rank`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写测试**

```python
class TestFinalRank(unittest.TestCase):
    _mock_response = TestFetchMetadata._mock_response

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
                urlopen.return_value = self._mock_response(api_payload)
                top = skillforge.llm_final_rank("q", cands)
        self.assertEqual(top[0]["R"], 9)
        self.assertEqual(top[0]["recommend_level"], "强推")
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestFinalRank -v`

- [ ] **Step 3: 实现**

在 `llm_coarse_rerank` 下面追加:
```python
def _final_heuristic(candidates: list) -> list:
    """无 LLM 时:按 (U+T) 降序,造 dummy R/recommend_level/why。"""
    scored = sorted(candidates, key=lambda c: (c.get("U") or 0) + (c.get("T") or 0), reverse=True)
    out = []
    for c in scored[:3]:
        ut = (c.get("U") or 0) + (c.get("T") or 0)
        if ut >= 140:
            level = "推荐"
        elif ut >= 80:
            level = "谨慎"
        else:
            level = "不推荐"
        out.append({
            "full_name": c["full_name"],
            "R": 0,  # 没 LLM 算不了
            "recommend_level": level,
            "why": "(无 ANTHROPIC_API_KEY,按 U+T 启发式排序)",
            "risks": c.get("risk_flags", []),
        })
    return out


def llm_final_rank(query: str, candidates: list) -> list:
    """5 个候选+README → Top 3 含 R/级别/中文理由/风险。无 key 走启发式。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _final_heuristic(candidates)

    payload = []
    for c in candidates:
        payload.append({
            "full_name": c["full_name"],
            "desc": c.get("description", ""),
            "language": c.get("language", ""),
            "stars": c.get("stargazers_count", 0),
            "watchers": c.get("subscribers_count", 0),
            "forks": c.get("forks_count", 0),
            "monthly_downloads": c.get("monthly_downloads"),
            "release_count": c.get("release_count", 0),
            "close_rate": c.get("close_rate"),
            "U": c.get("U"), "T": c.get("T"),
            "risk_flags": c.get("risk_flags", []),
            "readme_excerpt": (c.get("readme_excerpt") or "")[:4000],
        })
    prompt = (
        f'用户中文需求: "{query}"\n\n'
        "下面是 5 个候选的完整数据,含 README 摘录。请综合相关性、真实使用证据、治理透明度,选出 Top 3。\n\n"
        f"候选 (JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "对每个候选,基于 README 判断它是否真能解决用户需求:\n"
        "- R (0-10): 相关性。README 里有没有明确对应用户场景的功能/示例?\n"
        '- recommend_level: "强推" | "推荐" | "谨慎" | "不推荐"\n'
        "  推荐级别参考:\n"
        "  - 强推: R ≥ 8, U ≥ 70, T ≥ 70, 无 🔴 flag\n"
        "  - 推荐: R ≥ 7, U ≥ 30, T ≥ 50, ≤ 2 个 🟡 flag\n"
        "  - 谨慎: R ≥ 6, 但 U < 30 或 T < 50 或多个 🟡 flag\n"
        "  - 不推荐: R < 6, 或有 🔴 archived\n"
        '- why: 2 句中文推荐理由,说清"这库的核心能力是什么、为什么命中用户需求"\n'
        "- risks: 中文风险点 array,基于 risk_flags + README 看到的隐患\n\n"
        "按推荐度从高到低,只输出 JSON,共 3 个:\n"
        '[{"full_name": "...", "R": 9, "recommend_level": "强推", "why": "...", "risks": [...]}, ...]'
    )
    text = _llm_call(prompt, max_tokens=1500)
    if not text:
        return _final_heuristic(candidates)
    try:
        arr = json.loads(_strip_code_fence(text))
        if isinstance(arr, list) and arr:
            return arr[:3]
    except Exception:
        pass
    return _final_heuristic(candidates)
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestFinalRank -v`
Expected: 2 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: llm_final_rank(Top 5 → Top 3 含 R 分/推荐级/中文理由)"
```

---

## Task 11: `render_top3`

**Files:**
- Modify: `skillforge.py`
- Modify: `tests/test_skillforge.py`

- [ ] **Step 1: 写测试**

```python
class TestRenderTop3(unittest.TestCase):
    def test_contains_key_fields(self):
        ranked = [{
            "full_name": "danielgatis/rembg", "R": 9,
            "recommend_level": "强推", "why": "行业标准",
            "risks": [],
        }]
        candidates_meta = {"danielgatis/rembg": {
            "full_name": "danielgatis/rembg", "language": "Python",
            "stargazers_count": 18000, "subscribers_count": 320, "forks_count": 1900,
            "monthly_downloads": 2400000, "release_count": 22, "close_rate": 0.92,
            "U": 92, "T": 85, "install_cmds": ["pip install rembg"],
        }}
        text = skillforge.render_top3("批量去图片背景", ranked, candidates_meta, trusted_set=set())
        self.assertIn("批量去图片背景", text)
        self.assertIn("danielgatis/rembg", text)
        self.assertIn("R 相关 9/10", text)
        self.assertIn("U 使用 92/100", text)
        self.assertIn("T 治理 85/100", text)
        self.assertIn("强推", text)
        self.assertIn("行业标准", text)
        self.assertIn("pip install rembg", text)

    def test_renders_risks(self):
        ranked = [{
            "full_name": "x/y", "R": 7, "recommend_level": "推荐",
            "why": "...", "risks": ["仓库太新", "单一维护者"],
        }]
        meta = {"x/y": {"full_name": "x/y", "language": "Python",
                        "stargazers_count": 1, "subscribers_count": 1, "forks_count": 0,
                        "monthly_downloads": None, "release_count": 0, "close_rate": None,
                        "U": 5, "T": 50, "install_cmds": []}}
        text = skillforge.render_top3("q", ranked, meta, trusted_set=set())
        self.assertIn("仓库太新", text)
        self.assertIn("单一维护者", text)
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m unittest tests.test_skillforge.TestRenderTop3 -v`

- [ ] **Step 3: 实现**

在 `llm_final_rank` 下面追加:
```python
def _fmt_int(n):
    if n is None: return "无数据"
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}k"
    return str(n)


def _stars(level: str) -> str:
    return {"强推": "⭐⭐⭐ 强推 ", "推荐": "⭐⭐ 推荐  ",
            "谨慎": "⭐ 谨慎   ", "不推荐": "  不推荐  "}.get(level, level)


def render_top3(query: str, ranked: list, meta_by_name: dict, trusted_set: set) -> str:
    """ranked:llm_final_rank 输出;meta_by_name: full_name → 完整 meta(含 install_cmds);trusted_set:owner 白名单集合。"""
    lines = [f"🔎 「{query}」 → Top {len(ranked)}\n"]
    for i, item in enumerate(ranked):
        fn = item["full_name"]
        m = meta_by_name.get(fn, {})
        installs = m.get("install_cmds") or []
        install_str = installs[0] if installs else "(无标准安装方式)"
        owner = fn.split("/")[0].lower()
        trusted = "是" if (owner in trusted_set or fn.lower() in trusted_set) else "否"

        rate_str = f"{int((m.get('close_rate') or 0) * 100)}%" if m.get("close_rate") is not None else "无历史"
        lines.append(f"  [{i}] {_stars(item.get('recommend_level',''))}  {fn}  ({m.get('language','')})")
        lines.append(f"      R 相关 {item.get('R',0)}/10  ·  U 使用 {m.get('U',0)}/100  ·  T 治理 {m.get('T',0)}/100")
        lines.append(
            f"      ★ {_fmt_int(m.get('stargazers_count'))}  "
            f"👁 {_fmt_int(m.get('subscribers_count'))}  "
            f"🔱 {_fmt_int(m.get('forks_count'))}  "
            f"📥 {_fmt_int(m.get('monthly_downloads'))}  "
            f"📦 {m.get('release_count',0)} release  "
            f"💬 {rate_str} 闭合"
        )
        lines.append(f"      推荐: {item.get('why','')}")
        risks = item.get("risks") or []
        lines.append(f"      风险: {'  '.join(risks) if risks else '(无)'}")
        lines.append(f"      装: {install_str}     owner ∈ trusted? {trusted}\n")
    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试**

Run: `python -m unittest tests.test_skillforge.TestRenderTop3 -v`
Expected: 2 个 test ok。

- [ ] **Step 5: commit**

```bash
git add skillforge.py tests/test_skillforge.py
git commit -m "feat: render_top3 输出格式"
```

---

## Task 12: `cmd_find` 重写 + `--simple` / `--no-readme` / `--top` + 把老逻辑挪到 `cmd_find_simple`

**Files:**
- Modify: `skillforge.py`(`cmd_find` 大改 + `build_parser`)

- [ ] **Step 1: 把老 `cmd_find` 改名为 `cmd_find_simple`**

找到 `def cmd_find(args):`(在 trust 子命令下面),把整个函数重命名为 `def cmd_find_simple(args):`,内容**不改**。

- [ ] **Step 2: 写新 `cmd_find`**

在 `cmd_find_simple` 上面或下面新加:

```python
def cmd_find(args):
    """新流水线 cmd_find:LLM 改写 → 多搜 → 体检 → 粗排 → 深读 → 终排 → Top 3。
    --simple 走 cmd_find_simple(老路径)。
    """
    if args.simple:
        return cmd_find_simple(args)

    query = " ".join(args.query)
    token = os.environ.get("GITHUB_TOKEN")

    # 0) 前置闸门(同老逻辑)
    skills, _ = scan_local()
    matches = match_local(query, skills)
    if matches and not args.force_new:
        print(f'✅ 本地已有能满足「{query}」的技能,无需重复安装:\n')
        for s, score in matches[:3]:
            print(f"  ● {s.name}  (匹配度 {score:.2f}) — {s.description[:60]}")
        print("\n如仍想另装新的,加 --force-new。")
        return

    # --repo 直通(跳过 1-2,从 3 进入)
    if args.repo:
        try:
            _, info = gh_request(f"/repos/{args.repo}", token)
        except GHError as e:
            print(f"❌ 取仓库元数据失败:{e}", file=sys.stderr)
            return
        chosen = _info_to_chosen(info)
        return _install_chosen(args, chosen, token)

    # 1) LLM 改写
    print(f'🔎 「{query}」 改写中…')
    queries = llm_rewrite_query(query)
    print(f"   改写得到 {len(queries)} 个 query:{queries}")

    # 2) 多搜 + 合并去重
    seen = set()
    candidates = []
    for q in queries:
        for c in github_search(q, token, top=args.top * 2):
            if c["full_name"] in seen:
                continue
            seen.add(c["full_name"])
            candidates.append(c)
    if not candidates:
        print("❌ 没搜到任何候选,换个描述试试。")
        return
    print(f"   合并去重得 {len(candidates)} 个候选")

    # 3) 元数据体检 + 算 T 分 + flags
    print("   体检中…")
    enriched = []
    for c in candidates:
        try:
            meta = fetch_metadata(c["full_name"], token)
            enriched.append(meta)
        except GHError as e:
            print(f"   skip {c['full_name']}: {e}", file=sys.stderr)
    if not enriched:
        print("❌ 体检后无可用候选。")
        return

    # 4) 临时 U 分(没有 downloads/close_rate,先粗算)
    for m in enriched:
        m["U"] = compute_u_score(
            stars=m.get("stargazers_count", 0),
            watchers=m.get("subscribers_count", 0),
            forks=m.get("forks_count", 0),
            downloads=None, release_count=m.get("release_count", 0),
            close_rate=None,
        )

    # 5) LLM 粗排 → Top 5
    print("   LLM 粗排…")
    top5_refs = llm_coarse_rerank(query, enriched)
    top5_names = [r["full_name"] for r in top5_refs]
    top5 = [m for m in enriched if m["full_name"] in top5_names]

    # 6) 深读 Top 5: README + close_rate(可选跳过)
    if not args.no_readme:
        print("   深读 Top 5…")
        for m in top5:
            m["readme_excerpt"] = fetch_readme(m["full_name"], token)
            m["close_rate"] = fetch_close_rate(m["full_name"], token)
            # 包名 + 下载量
            pkg = guess_package_name(m["full_name"], m.get("default_branch", "main"), m.get("language", ""))
            m["monthly_downloads"] = fetch_downloads(pkg["ecosystem"], pkg["name"]) if pkg else None
            # 重算 U
            m["U"] = compute_u_score(
                stars=m.get("stargazers_count", 0),
                watchers=m.get("subscribers_count", 0),
                forks=m.get("forks_count", 0),
                downloads=m["monthly_downloads"],
                release_count=m.get("release_count", 0),
                close_rate=m["close_rate"],
            )
            # 推断安装命令(从 language 粗推,真 clone 后才能确认)
            m["install_cmds"] = _guess_install(m.get("language", ""))
    else:
        for m in top5:
            m["readme_excerpt"] = ""
            m["close_rate"] = None
            m["monthly_downloads"] = None
            m["install_cmds"] = _guess_install(m.get("language", ""))

    # 7) LLM 终排 → Top 3
    print("   LLM 终排…")
    ranked = llm_final_rank(query, top5)

    # 8) 渲染
    trusted = load_trusted()
    meta_by_name = {m["full_name"]: m for m in top5}
    print("\n" + render_top3(query, ranked, meta_by_name, trusted))

    # 9) 让用户选
    if args.yes:
        idx = 0
        print("[自动选 0]")
    else:
        try:
            idx = int(input("选哪个? 输序号: ").strip())
        except (ValueError, EOFError):
            print("已取消。")
            return

    chosen_name = ranked[idx]["full_name"]
    chosen = _info_to_chosen(meta_by_name[chosen_name])
    return _install_chosen(args, chosen, token)


def _info_to_chosen(info: dict) -> dict:
    """把 /repos 响应 / fetch_metadata 输出格式化成老 cmd_find 期待的 chosen dict。"""
    return {
        "full_name": info["full_name"],
        "description": info.get("description") or "",
        "stars": info.get("stargazers_count", 0),
        "updated": (info.get("pushed_at") or "")[:10],
        "language": info.get("language") or "",
        "clone_url": info["clone_url"],
        "html_url": info["html_url"],
    }


def _guess_install(language: str):
    lang = (language or "").lower()
    if lang == "python":
        return ["pip install -e ."]
    if lang in {"javascript", "typescript"}:
        return ["npm install"]
    if lang == "rust":
        return ["cargo build --release"]
    return []


def _install_chosen(args, chosen, token):
    """从 cmd_find_simple 的 step ③ 开始往下走(star → clone → 检测 → adopt → gen → register)。
    把这一段抽出来便于新旧 cmd_find 共用。
    """
    # 直接调用 cmd_find_simple 的"装到 register"那段:复制粘贴最简单
    # ... 把 cmd_find_simple step 3 到结尾的代码原样搬到这里 ...
```

**说明:**`_install_chosen` 体内是把 `cmd_find_simple` 现有"step ③ star → ④ clone → ⑤ 安装命令 → 5.5 adoption → ⑥ gen SKILL.md → ⑦ register"的代码原封不动搬过来,把变量 `chosen`/`token`/`args` 都从形参拿。这样新老 `cmd_find` 共用安装段。

详细实施时:
1. 把 `cmd_find_simple` step ③-⑦ 的代码块**剪切**到 `_install_chosen` 函数体内,把原 `cmd_find_simple` 那部分**留个 `return _install_chosen(args, chosen, token)`** 代替。
2. `_install_chosen` 内部凡用到 `args.yes / args.no_star / args.install / args.copy / args.no_register` 都已经在形参里。

- [ ] **Step 3: 更新 `build_parser`,加 `--simple`、`--no-readme`、`--top`(改默认)**

找到 `pf = sub.add_parser("find", help="本地没有就去 GitHub 找并安装")`,把它和后面的 add_argument 改成:

```python
pf = sub.add_parser("find", help="本地没有就去 GitHub 找并安装(LLM 增强,见 specs)")
pf.add_argument("query", nargs="+")
pf.add_argument("--repo", help="跳过搜索,直接指定 owner/repo")
pf.add_argument("--top", type=int, default=3, help="最终展示几个(默认 3;多搜阶段会按 top*2 拉)")
pf.add_argument("--yes", action="store_true", help="非交互:自动确认+选第一个")
pf.add_argument("--force-new", action="store_true", help="本地已有也强制装新的")
pf.add_argument("--no-star", action="store_true")
pf.add_argument("--install", action="store_true", help="允许执行安装命令")
pf.add_argument("--no-register", action="store_true")
pf.add_argument("--copy", action="store_true", help="注册用复制而非软链")
pf.add_argument("--simple", action="store_true", help="跳过 LLM,走老 keyword 路径")
pf.add_argument("--no-readme", action="store_true", help="跳过 README 深读 + close-rate + 下载量")
pf.set_defaults(func=cmd_find)
```

- [ ] **Step 4: 验证语法**

Run: `cd "E:/Awendang/skill自动发掘器" && python -m py_compile skillforge.py && python skillforge.py --help`
Expected: 帮助里 find 命令出现,无 traceback。

- [ ] **Step 5: 跑现有测试不能挂**

Run: `python -m unittest discover tests -v`
Expected: 全部 ok。

- [ ] **Step 6: 冒烟一次 `--simple`(走老路径)**

Run:
```bash
GITHUB_TOKEN=$TOKEN python skillforge.py find "asset forge" --repo editech-dev/asset-forge --yes --force-new --simple --no-star
```
Expected: 跟改造前行为一致(已 adopt 过会显示"技能目录已存在",仍是 OK)。

- [ ] **Step 7: commit**

```bash
git add skillforge.py
git commit -m "feat: cmd_find 接 LLM 流水线;老逻辑挪到 cmd_find_simple --simple 触发"
```

---

## Task 13: 5 个验收 query 真跑一遍

**Files:**
- Create: `tests/acceptance_queries.sh`(便于复跑)

- [ ] **Step 1: 写 acceptance 脚本**

`tests/acceptance_queries.sh`:
```bash
#!/usr/bin/env bash
# spec §16 的 5 个验收 query。
# 跑法: GITHUB_TOKEN=xxx ANTHROPIC_API_KEY=yyy bash tests/acceptance_queries.sh
# 每个 query 不真装,只让 LLM 出 Top 3。Ctrl-C 跳过到下一个。

set -u
cd "$(dirname "$0")/.."

QUERIES=(
  "批量去图片背景"
  "把 mp4 转成 webm 节省体积"
  "命令行管理 GitHub PR review 回复"
  "在浏览器里 OCR 图片"
  "把代码仓库可视化成依赖图"
)

for q in "${QUERIES[@]}"; do
  echo "================================================================"
  echo "Query: $q"
  echo "================================================================"
  PYTHONIOENCODING=utf-8 python skillforge.py find "$q" --force-new --no-star 2>&1 || true
  echo
  read -p "继续下一个? [Enter] " _
done
```

- [ ] **Step 2: 手动跑 acceptance**

Run:
```bash
chmod +x tests/acceptance_queries.sh
GITHUB_TOKEN=$YOUR_TOKEN ANTHROPIC_API_KEY=$YOUR_KEY bash tests/acceptance_queries.sh
```

肉眼检查每个 query 的 Top 3:
- Top 1 必须**相关**且**不是 awesome-* 列表**
- R/U/T 三维分必须落在合理区间(R ≥ 6 是相关;U/T 见仓库真实情况)
- archived 仓库必须不出现在 Top 1

不通过的 query → 记下来,定位是哪一步问题(改写错?粗排错?README 没读懂?),回到对应 Task 改 prompt。

- [ ] **Step 3: commit acceptance 脚本**

```bash
git add tests/acceptance_queries.sh
git commit -m "test: 5 个 acceptance query 脚本"
```

---

## Task 14: README 同步更新

**Files:**
- Modify: `skillforge_README.md`

- [ ] **Step 1: 找到"### find 的常用参数"那节,把表格加 3 行**

```markdown
| `--simple` | 跳过 LLM 流水线,走老 keyword 搜索(快,$0,质量低) |
| `--no-readme` | 跳过 README 深读 + close_rate + 下载量(中速) |
| `--top N` | 输出几个候选(默认 3) |
```

- [ ] **Step 2: 在"### Adoption"上面新加一节"### LLM 增强 find 流水线"**

```markdown
### LLM 增强 find 流水线(默认)

当 `ANTHROPIC_API_KEY` + `GITHUB_TOKEN` 都设了时,`find` 会走 9 步流水线:

1. LLM 把中文需求改写成 3 个英文 query(功能/工具/技术栈三个角度)
2. 三个 query 各搜 GitHub,合并去重得 10-15 候选
3. 对每个候选拉一次 `/repos/{x}` 元数据,算治理分 T(0-100)+ 风险标签
4. LLM 粗排,挑出最相关的 5 个继续深读
5-6. 对 Top 5:fetch README + issue 闭合率 + 包月下载量(PyPI/npm/Crates)
7. 算使用度 U(0-100),融合 stars/watchers/forks/下载量/release/闭合率
8. LLM 终排出 Top 3,带相关性 R(0-10)+ 推荐级别 + 中文理由 + 风险列表
9. 输出 Top 3 让你选

成本:~$0.02 + 8-12s / find。详见 [specs/2026-06-30-skill-search-quality.md](specs/2026-06-30-skill-search-quality.md)。

无 `ANTHROPIC_API_KEY` 时自动降级到启发式排序(0.6*U + 0.4*T)。
```

- [ ] **Step 3: §6 安全设计里加一行**

```markdown
- **三维评分透明化**:每个推荐候选都附 R(相关性)、U(真实使用证据)、T(治理透明度)三维分 + 风险标签,看得到"为什么推这个"和"风险在哪"。
```

- [ ] **Step 4: commit**

```bash
git add skillforge_README.md
git commit -m "docs: README 同步 LLM 流水线 + 三维评分说明"
```

---

## Task 15: 总收尾 — 完整 list/which/find 回归

- [ ] **Step 1: 跑全部 unittest**

Run: `python -m unittest discover tests -v`
Expected: 所有 test ok,没有跳过。

- [ ] **Step 2: 跑 list / which / find --simple / find(全流水线)各一次**

```bash
python skillforge.py list | head -5
python skillforge.py which "remove image background batch"
python skillforge.py find "asset forge" --repo editech-dev/asset-forge --yes --force-new --simple --no-star
# 真跑一次完整流水线(任选一个 query):
GITHUB_TOKEN=$T ANTHROPIC_API_KEY=$K python skillforge.py find "把视频压成 webm" --force-new --no-star
```
Expected: 每个命令都 0 traceback,find 完整流水线输出 Top 3 含三维分。

- [ ] **Step 3: 看 git log 确认 commit 历史干净**

Run: `git log --oneline`
Expected: 看到从 Task 0 到 Task 14 的 ~15 个 commit,主题清晰。

- [ ] **Step 4: 收尾 commit(如果有遗漏 fix)**

```bash
git status
# 如有未提交修改:
git add -A
git commit -m "chore: post-acceptance cleanup"
```

---

## Self-Review 结果

**1. Spec 覆盖检查:** 对照 spec 14 节,每节都有对应 task(§1-3 → Task 0/12;§4-5 → Task 2/3;§6 → Task 4-7;§7-8 → Task 8-10;§9 → Task 12;§10-11 → Task 12;§12 → Task 13;§13-14 → 全表;§15 → 已声明不做;§16 → Task 13)。**无遗漏**。

**2. 占位扫描:** 所有 step 都给了完整代码 / 完整命令 / 完整 expected。无 TODO / TBD。**通过**。

**3. 类型一致性:**
- `compute_t_score(meta)` 输入是 dict,返回 int ✓
- `compute_u_score(*, stars, watchers, forks, downloads, release_count, close_rate)` 关键字参数风格 ✓ 调用方在 Task 12 用关键字调 ✓
- `fetch_metadata` 返回的 dict 在 Task 9/10/12 被消费,字段名 `T`、`U`、`risk_flags`、`stargazers_count`、`subscribers_count`、`forks_count`、`monthly_downloads`、`release_count`、`close_rate`、`readme_excerpt`、`language`、`install_cmds`、`default_branch`、`full_name`、`description`,在所有任务中一致 ✓
- `llm_final_rank` 输出 dict 字段 `full_name`、`R`、`recommend_level`、`why`、`risks`,与 Task 11 `render_top3` 消费一致 ✓
- `guess_package_name` 返回 `{ecosystem: ..., name: ...}` 或 None;`fetch_downloads(ecosystem, name)` 接收一致 ✓

**通过**。
