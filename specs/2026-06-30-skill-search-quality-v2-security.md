# Spec 增补 v2: OpenSSF Scorecard + OSV 漏洞库

- **Date:** 2026-06-30
- **Status:** Approved, going straight to implementation
- **Parent spec:** [2026-06-30-skill-search-quality.md](2026-06-30-skill-search-quality.md)

## 1. 增补动机

v1 的 T 治理分基于"仓库元数据启发式"(LICENSE/活跃度/单一维护者...),覆盖**项目自治**但**没碰代码层面 / 包层面的真实安全**:
- 没看代码有没有 SAST/CodeQL 跑过
- 没看 release 制品是不是签名的
- 没看 CI workflow 有没有过权限暴露
- 没查这个**包**有没有已知 CVE

本增补接入两个**业界标准、公开免认证**的数据源补这两个缺口。

## 2. 接入的数据源

### 2.1 OpenSSF Scorecard

Google + OpenSSF 维护的自动代码/治理审计,**0-10 分** + 18 项详细 checks。

- **API**: `GET https://api.securityscorecards.dev/projects/github.com/{owner}/{repo}`
- **认证**: 无
- **失败**: 仓库未被收录 → 404,直接 return None
- **延迟**: ~200-500ms / 调用

返回结构(节选):
```json
{
  "score": 6.5,
  "checks": [
    {"name": "Binary-Artifacts",       "score": 10, "reason": "no binaries found"},
    {"name": "Branch-Protection",      "score": 0,  "reason": "branch protection not enabled"},
    {"name": "Dependency-Update-Tool", "score": 10, "reason": "Update tool detected"},
    {"name": "Dangerous-Workflow",     "score": 10, "reason": "no dangerous workflow patterns"},
    {"name": "License",                "score": 10, "reason": "license file detected"},
    {"name": "Maintained",             "score": 9,  "reason": "30 commits last 90 days"},
    {"name": "Token-Permissions",      "score": 0,  "reason": "no top-level write perms"},
    ...
  ]
}
```

### 2.2 OSV (Open Source Vulnerabilities)

Google 维护的统一漏洞数据库,覆盖 GHSA / PyPA / RustSec / npm Advisories。

- **API**: `POST https://api.osv.dev/v1/query` body `{"package": {"name": "rembg", "ecosystem": "PyPI"}}`
- **认证**: 无
- **失败/无漏洞**: 返回 `{"vulns": []}` 或 `{}`
- **延迟**: ~200-400ms / 调用

返回 `vulns` 数组,每个 vuln 含 `id` / `summary` / `severity[]` / `database_specific.severity`。**Severity** 取 `database_specific.severity` 优先,否则取 `severity[0].score`(CVSS)。我们把 HIGH / CRITICAL 算"高危",其它算"中低危"。

## 3. 接入到 T 分 + 风险标签

### T 分公式变化

```
T_v1(0-100) = positives(≤90) - penalties           # 现有
T_v2(0-100) = clamp(T_v1 + scorecard_bonus - osv_penalty, 0, 100)
  where
    scorecard_bonus = scorecard_score × 1.0    # 0-10 分直接 +0..+10
    osv_penalty    = min(50, 30 × n_critical)  # 每个未修复 CRITICAL 扣 30,封顶 50
```

**理由**: 不破坏 v1 的"健康仓库 ≥80" 约定(scorecard 缺失时 T_v2 == T_v1),只奖励真有 scorecard 的高分项目,只惩罚有真实 CVE 的项目。

### 新增风险标签

| 触发条件 | 标签 |
|---|---|
| OSV 查到 ≥1 个未修复 HIGH/CRITICAL | 🔴 OSV: {n} 个未修复的 HIGH/CRITICAL 漏洞 |
| Scorecard 总分 < 4 | 🔴 Scorecard 总分 {x}/10(安全实践薄弱) |
| Scorecard `Branch-Protection` < 5 | 🟡 未启用 branch protection |
| Scorecard `Binary-Artifacts` < 10 | 🟡 仓库内有 binary artifacts |
| Scorecard `Dangerous-Workflow` < 10 | 🟡 CI workflow 含危险 pattern |
| Scorecard `Dependency-Update-Tool` < 5 | 🟡 无依赖更新工具(Dependabot/Renovate) |

其它 Scorecard checks(SAST、Fuzzing、SBOM、Token-Permissions...)**不**单独标 flag,已经间接反映在总分 bonus 里;过多 flag 会噪音。

## 4. 接入到流水线

**位置**: 现 step ⑥ "包下载量 × Top 5" 同位置(包名推导 + 下载量 + close_rate + **Scorecard + OSV**)。

每个 Top 5 候选额外 2 次 HTTP 调用:
- 1× Scorecard(无关包名,只要 `owner/repo`)
- 1× OSV(需要包名,跟 fetch_downloads 共用 guess_package_name 结果)

**额外耗时**: 5 × (~300ms × 2) = ~3s 串行,**实际并行可压到 ~600ms**(v2 第二阶段优化)。
**额外成本**: 0 美金。

## 5. 渲染输出微调

`render_top3` 在原 `📥 ★ 👁 🔱 📦 💬` 那一行后面**加一行**:

```
      🛡 Scorecard 6.5/10  ·  OSV 0 vuln  ·  branch-protection ✗  dependency-update ✓
```

- `🛡 Scorecard X/10` — 总分
- `OSV {n} vuln` — n=0 显示绿色感, n>0 提示"X 个漏洞,详情见 risks"
- 最多列 3 个 scorecard 子检查的 √/✗,优先显示我们关心的(branch-protection、binary-artifacts、dependency-update)

如果 Scorecard 没收录(404),显示 `🛡 Scorecard 无收录`。
OSV 无包名识别时显示 `OSV 跳过(无法推 ecosystem)`.

## 6. 模块新增

| 函数 | 输入 | 输出 |
|---|---|---|
| `fetch_scorecard(full_name) -> Optional[dict]` | "owner/repo" | `{"score": 6.5, "checks": [{"name", "score", "reason"}, ...]}` 或 None |
| `fetch_osv_vulns(ecosystem, name) -> list` | "PyPI"/"npm"/"crates.io" + 包名 | `[{"id", "severity", "summary"}, ...]` 或 [] |
| `compute_t_score(meta, scorecard=None, osv_vulns=None) -> int` | 加两个可选参 | 同 v1 范围 |
| `compute_risk_flags(meta, scorecard=None, osv_vulns=None) -> list` | 加两个可选参 | 同 v1 范围 + 新增标签 |

`fetch_metadata` 不动(scorecard/osv 在 step ⑥ 才拉,不进早期 batch)。

## 7. 测试矩阵

| 场景 | 期望 |
|---|---|
| 知名仓库有 scorecard | T_v2 > T_v1(bonus 生效) |
| 仓库 404 not in scorecard | T_v2 == T_v1(没影响,no flag) |
| 包有 2 个 critical CVE | T_v2 == T_v1 - 50(罚分封顶);多 🔴 flag |
| Scorecard 总分 3 | 多 🔴 "Scorecard 总分 3/10" flag |
| Scorecard Branch-Protection = 0 | 多 🟡 "未启用 branch protection" flag |
| 网络超时 | scorecard/osv 返回 None/[];T/flags 退化为 v1 行为,不崩 |

## 8. 待 v3(本次不做)

- 并发 fetch(scorecard + osv + readme + close + downloads 并行)→ 单次 find 时延 -50%
- 同时查 deps.dev 统一拿(替代分两次)
- 把 scorecard 24h 缓存到 `~/.skillforge/.cache/scorecard/{owner}-{repo}.json`(同一仓库重复 find 不重复打 API)
