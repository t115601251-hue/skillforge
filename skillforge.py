#!/usr/bin/env python3
"""
skillforge — 跨 agent 的技能闭环管理工具

闭环：用户描述 -> 扫本地技能 -> 命中?
  命中  : 告诉用户已经装了、怎么用
  未命中: GitHub 搜索 -> 确认 -> star -> clone -> 装 -> 生成 SKILL.md -> 注册到各 agent 目录

子命令:
  list            列出所有已装技能(跨 Claude Code / Codex / OpenClaw,按真实路径去重)
  which <描述>    只查本地:看有没有技能能满足这个需求
  find  <描述>    完整闭环:本地没有就去 GitHub 找、装、包装成技能

设计原则:零第三方依赖(仅标准库);有副作用的步骤(star / 装包)默认先确认。
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

# ----------------------------------------------------------------------------- 配置
GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get("SKILLFORGE_MODEL", "claude-sonnet-4-6")

# 各 agent 的用户级技能目录(项目级会在 scan 时按 cwd 另外加)
DEFAULT_AGENT_DIRS = [
    "~/.claude/skills",     # Claude Code
    "~/.codex/skills",      # Codex
    "~/.openclaw/skills",   # OpenClaw
]
# 本工具自己的"权威"技能目录;装新技能默认落在这里,再注册(软链)到各 agent
CANONICAL_HOME = os.environ.get("SKILLFORGE_HOME", "~/.skillforge/skills")
# 可信 owner 清单(每行一个 owner 或 owner/repo,#开头是注释);命中则在 find 时自动装
TRUSTED_FILE = os.environ.get("SKILLFORGE_TRUSTED", "~/.skillforge/trusted.txt")
# 备份目录:agent 扫描目录里不能留 .bak,否则会被 harness 当成新技能注册
BACKUP_HOME = os.environ.get("SKILLFORGE_BACKUPS", "~/.skillforge/backups")
# 版本快照目录(三槽位:pristine / previous / current):不在 agent 扫描路径里
SKILLFORGE_VERSIONS = os.environ.get("SKILLFORGE_VERSIONS", "~/.skillforge/versions")
# 编号缓存:/skill-列表 写入,/skill <n> 引用
LAST_LIST_FILE = os.environ.get("SKILLFORGE_LAST_LIST", "~/.skillforge/.last_list.json")


def save_last_list(mapping: dict):
    """{1: 'asset-forge', 2: 'rembg', ...} 写盘。"""
    p = Path(LAST_LIST_FILE).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    # JSON key 必须是字符串
    data = {"saved_at": int(_now()), "mapping": {str(k): v for k, v in mapping.items()}}
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_last_list() -> dict:
    """读盘,30 天过期。返回 {int: str}。"""
    p = Path(LAST_LIST_FILE).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if _now() - (data.get("saved_at") or 0) > 30 * 86400:
        return {}
    return {int(k): v for k, v in (data.get("mapping") or {}).items()}


def resolve_skill(name_or_num: str):
    """传入"asset-forge"或"1",返回 skill 名字。找不到返回 None。"""
    s = (name_or_num or "").strip()
    if not s:
        return None
    if s.isdigit():
        return load_last_list().get(int(s))
    return s  # 直接当 name 用


def _now() -> int:
    import time as _time
    return int(_time.time())


def version_dir(name: str, slot: str) -> Path:
    """返回 ~/.skillforge/versions/<name>/<slot>/ 的 Path,不创建。"""
    return Path(SKILLFORGE_VERSIONS).expanduser() / name / slot


def _rmtree_force(path: Path):
    """跨平台强删:Windows 上 git 的 .pack/.idx 文件带只读位,shutil.rmtree 默认 unlink 会拒绝。
    回调里给文件加写权限再重试,解决 PermissionError [WinError 5]。
    """
    import shutil, stat
    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        try:
            func(p)
        except OSError:
            pass  # 真删不掉就算了,不能因为单文件挡住整个流程
    # Python 3.12+ 用 onexc,老版本用 onerror
    try:
        shutil.rmtree(path, onexc=_onerror)
    except TypeError:
        shutil.rmtree(path, onerror=_onerror)


def _copy_skill_into(src: Path, dest: Path):
    """覆盖式复制 src 整个目录到 dest;dest 已存在先强删(处理 git 只读位)。"""
    import shutil
    if dest.exists():
        _rmtree_force(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=True)


def save_pristine(name: str, skill_dir: Path):
    """安装时调一次:把当前 skill_dir 整体存为 pristine。已存在则跳过(永不覆盖原版)。"""
    pristine = version_dir(name, "pristine")
    if pristine.exists():
        return  # 永不覆盖
    _copy_skill_into(skill_dir, pristine)


def save_previous(name: str, skill_dir: Path):
    """修改前调:把当前 skill_dir 存为 previous,覆盖旧 previous。"""
    _copy_skill_into(skill_dir, version_dir(name, "previous"))


def rollback_to_previous(name: str, skill_dir: Path):
    """swap 形式:current ↔ previous 互换。回滚后再回滚 = undo/redo。"""
    prev = version_dir(name, "previous")
    if not prev.exists():
        raise FileNotFoundError(f"没有 previous 版本可回滚:{prev}")
    # 三步走: current → tmp ; previous → current ; tmp → previous
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skf_swap_"))
    tmp_dir = tmp / "x"
    _copy_skill_into(skill_dir, tmp_dir)
    _copy_skill_into(prev, skill_dir)
    _copy_skill_into(tmp_dir, prev)
    shutil.rmtree(tmp, ignore_errors=True)


def rollback_to_pristine(name: str, skill_dir: Path):
    """先把当前 current 存为 previous(保留可逆),然后 pristine → current。"""
    pristine = version_dir(name, "pristine")
    if not pristine.exists():
        raise FileNotFoundError(f"没有 pristine 版本(可能不是 skillforge 装的):{pristine}")
    save_previous(name, skill_dir)
    _copy_skill_into(pristine, skill_dir)


def backup_skill_dir(source_dir: Path) -> Path:
    """把一个技能目录搬到 BACKUP_HOME,避免污染 agent 扫描路径。
    命名:<原名>.<agent 标签>.bak.<unix-ts>
    """
    import shutil, time as _time
    ts = int(_time.time())
    # 推 agent 标签:.claude/skills/xxx 的 parent.parent.name = '.claude'
    try:
        agent = source_dir.parent.parent.name.lstrip(".") or "misc"
    except Exception:
        agent = "misc"
    backup_root = Path(BACKUP_HOME).expanduser()
    backup_root.mkdir(parents=True, exist_ok=True)
    dest = backup_root / f"{source_dir.name}.{agent}.bak.{ts}"
    # 唯一性
    i = 1
    while dest.exists():
        dest = backup_root / f"{source_dir.name}.{agent}.bak.{ts}.{i}"
        i += 1
    shutil.move(str(source_dir), str(dest))
    return dest


def load_trusted():
    p = Path(TRUSTED_FILE).expanduser()
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.lower())
    return out


def is_trusted(full_name: str, trusted=None) -> bool:
    trusted = trusted if trusted is not None else load_trusted()
    fn = full_name.lower()
    owner = fn.split("/")[0]
    return fn in trusted or owner in trusted


def save_trusted(items):
    p = Path(TRUSTED_FILE).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if p.exists():
        existing = p.read_text(encoding="utf-8", errors="replace").splitlines()
    # 保留注释、空行、原顺序;只更新非注释条目
    body_comment = [l for l in existing if not l.strip() or l.strip().startswith("#")]
    new_lines = body_comment + sorted(items)
    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def agent_dirs():
    """返回要扫描/注册的 agent 技能目录列表(用户级 + 项目级 + 权威目录)。"""
    raw = os.environ.get("SKILLFORGE_SCAN_DIRS")
    # SKILLFORGE_HOME 排在最前面:realpath 去重时它先入,其他位置的同一文件就会被跳过,
    # 这样 list 显示的"来源"就是权威位置而不是路过的 agent dir。
    dirs = [CANONICAL_HOME] + (raw.split(os.pathsep) if raw else list(DEFAULT_AGENT_DIRS))
    # 项目级:当前目录往上找 .claude/skills、.codex/skills
    cur = Path.cwd()
    for parent in [cur, *cur.parents]:
        for sub in (".claude/skills", ".codex/skills", ".openclaw/skills"):
            p = parent / sub
            if p.is_dir():
                dirs.append(str(p))
        if (parent / ".git").exists():
            break  # 到仓库根为止
    # 展开 ~ 并去重(保序)
    seen, out = set(), []
    for d in dirs:
        p = str(Path(d).expanduser())
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ----------------------------------------------------------------------------- 数据结构
@dataclass
class Skill:
    name: str
    description: str
    path: str          # SKILL.md 的真实路径
    source: str        # 来自哪个目录(用于显示)


# ----------------------------------------------------------------------------- 本地扫描
def parse_frontmatter(skill_md: Path):
    """从 SKILL.md 头部 YAML 里提取 name 和 description(极简解析,不依赖 PyYAML)。"""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    front = text[3:end]
    name = desc = None
    for line in front.splitlines():
        m = re.match(r"\s*(name|description)\s*:\s*(.*)", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
        if key == "name":
            name = val
        else:
            desc = val
    if not name:
        return None
    return {"name": name, "description": desc or ""}


def _source_priority(source: str) -> int:
    """注册目录的优先级:小 = 高。SKILLFORGE_HOME > 用户级 agent 目录 > 其它(项目级等)。"""
    src = str(Path(source).expanduser())
    if src == str(Path(CANONICAL_HOME).expanduser()):
        return 0
    user_level = {str(Path(d).expanduser()) for d in DEFAULT_AGENT_DIRS}
    if src in user_level:
        return 1
    return 2


def scan_local(name_dedup: bool = True):
    """扫描所有 agent 目录。
    先按 SKILL.md 真实路径去重(软链指向同一文件只算一次)。
    再按 name 去重,优先级 SKILLFORGE_HOME > 用户级 > 项目级,
    被遮蔽的同名副本作为 shadowed 返回供 list 命令展示。
    返回 (winners, shadowed)。
    """
    found = {}  # realpath -> Skill
    for d in agent_dirs():
        base = Path(d)
        if not base.is_dir():
            continue
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir():
                continue
            if ".bak." in skill_dir.name:
                continue  # skillforge 自己生成的备份目录,不当作活的技能
            md = skill_dir / "SKILL.md"
            if not md.exists():
                # 兼容大小写
                alt = [p for p in skill_dir.iterdir() if p.name.lower() == "skill.md"]
                if not alt:
                    continue
                md = alt[0]
            real = os.path.realpath(md)
            if real in found:
                continue  # 软链指向同一文件,只算一次
            meta = parse_frontmatter(md)
            if not meta:
                continue
            found[real] = Skill(
                name=meta["name"],
                description=meta["description"],
                path=real,
                source=str(base),
            )
    skills = list(found.values())
    if not name_dedup:
        return skills, []
    by_name, shadowed = {}, []
    for s in sorted(skills, key=lambda x: (_source_priority(x.source), x.name)):
        if s.name in by_name:
            shadowed.append(s)
        else:
            by_name[s.name] = s
    return list(by_name.values()), shadowed


# ----------------------------------------------------------------------------- 匹配
_WORD = re.compile(r"[a-z0-9]+")

# 泛词降权:与领域无关的词不应主导匹配。命中只算 0.2 个实词。
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "in", "for", "with", "from", "on", "at", "by",
    "and", "or", "is", "it", "be", "as", "this", "that", "these", "those",
    "i", "me", "my", "you", "your", "we", "our",
    "do", "does", "make", "use", "using", "via", "want", "need", "please", "help",
    "file", "files", "text", "data", "app", "apps", "code", "project", "thing", "stuff",
})


def _tokens(s):
    return set(_WORD.findall(s.lower()))


def keyword_score(query, skill: Skill):
    """无 API key 时的回退匹配:实词全权重,泛词只算 0.2。
    score = (实词命中 + 0.2*泛词命中) / (实词总数 + 0.2*泛词总数)
    """
    q = _tokens(query)
    if not q:
        return 0.0
    strong = {t for t in q if t not in _STOPWORDS}
    weak = q - strong
    hay = _tokens(skill.name + " " + skill.description)
    s_hit, w_hit = strong & hay, weak & hay
    denom = len(strong) + 0.2 * len(weak)
    if denom == 0:  # 全是停用词的退化查询,fall back 到原始重叠比
        return len(q & hay) / len(q)
    return (len(s_hit) + 0.2 * len(w_hit)) / denom


def llm_match(query, skills):
    """有 ANTHROPIC_API_KEY 时,让模型判断哪个技能能满足需求。返回 name 列表或 None。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not skills:
        return None
    catalog = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    prompt = (
        "下面是本地已安装的技能清单(名字: 描述)。\n"
        f"{catalog}\n\n"
        f'用户的需求是:"{query}"\n\n'
        "哪些技能能满足这个需求?只回一个 JSON 数组,元素是技能名字,"
        "按相关度从高到低;都不匹配就回 []。不要任何其它文字。"
    )
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(ANTHROPIC_API, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        text = text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        names = json.loads(text)
        return [n for n in names if isinstance(n, str)]
    except Exception as e:
        print(f"[warn] LLM 匹配失败,回退关键词匹配: {e}", file=sys.stderr)
        return None


def match_local(query, skills, threshold=0.34):
    """返回 [(skill, score)] 按分数降序。优先用 LLM,失败回退关键词。"""
    names = llm_match(query, skills)
    if names is not None:
        order = {n: i for i, n in enumerate(names)}
        hits = [s for s in skills if s.name in order]
        hits.sort(key=lambda s: order[s.name])
        return [(s, 1.0 - 0.01 * order[s.name]) for s in hits]
    scored = [(s, keyword_score(query, s)) for s in skills]
    scored = [(s, sc) for s, sc in scored if sc >= threshold]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ----------------------------------------------------------------------------- GitHub
class GHError(RuntimeError):
    """带 status code 的 GitHub API 错误,方便上层分类处理。"""
    def __init__(self, code: int, message: str, raw: str = ""):
        super().__init__(message)
        self.code = code
        self.raw = raw


def gh_request(path, token=None, method="GET", data=None):
    url = path if path.startswith("http") else GITHUB_API + path
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "skillforge",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        msg = _friendly_gh_msg(e.code, path, token, body_text)
        raise GHError(e.code, msg, body_text) from e


def _friendly_gh_msg(code: int, path: str, token: str | None, body: str) -> str:
    if code == 401:
        return (
            "GitHub 拒绝认证 (401 Bad credentials)。GITHUB_TOKEN 已失效或拼写错误。"
            "重签:https://github.com/settings/tokens"
        )
    if code == 403:
        if "rate limit" in body.lower() or "API rate limit" in body:
            if not token:
                return (
                    "GitHub 匿名 rate limit 超出 (403,配额 60/小时)。三种解法:\n"
                    "  A) 已装 gh CLI 且登录过: GITHUB_TOKEN=$(gh auth token) python skillforge.py ...\n"
                    "  B) 手动生成 PAT: https://github.com/settings/tokens → classic → public_repo\n"
                    "  C) 不联网只用本地: python skillforge.py suggest \"<需求>\""
                )
            return "GitHub rate limit 超出 (403,配额 5000/小时)。等到 reset 窗口或换新 token。"
        if "/user/starred/" in path:
            return (
                "Star 失败 (403):token 缺少 starring 写权限。"
                "fine-grained PAT 需要勾 *Account permissions* → *Starring* → Read and write。"
                "classic PAT 需要勾 `public_repo`(或 `repo`)。"
            )
        return f"GitHub 403 Forbidden:{body[:200]}"
    if code == 404:
        return f"GitHub 找不到资源 (404):{path}。仓库名拼写正确?是否私有(需 token 权限)?"
    return f"GitHub HTTP {code}: {body[:200]}"


def github_search(query, token=None, top=5, search_readme=False):
    """按描述搜仓库,按 star 排序。默认只匹配 name+description(更相关)。"""
    scope = "name,description,readme" if search_readme else "name,description"
    q = urllib.parse.quote(f"{query} in:{scope}")
    path = f"/search/repositories?q={q}&sort=stars&order=desc&per_page={top}"
    try:
        _, data = gh_request(path, token)
    except GHError as e:
        print(f"  搜索失败:{e}", file=sys.stderr)
        return []
    out = []
    for item in (data or {}).get("items", []):
        out.append({
            "full_name": item["full_name"],
            "description": item.get("description") or "",
            "stars": item.get("stargazers_count", 0),
            "updated": (item.get("pushed_at") or "")[:10],
            "language": item.get("language") or "",
            "clone_url": item["clone_url"],
            "html_url": item["html_url"],
            "default_branch": item.get("default_branch", "main"),
        })
    return out


def fetch_readme(full_name, token=None):
    try:
        _, data = gh_request(f"/repos/{full_name}/readme", token)
        content = base64.b64decode(data["content"]).decode("utf-8", "replace")
        return content
    except Exception:
        return ""


def star_repo(full_name, token):
    """给仓库点 star。需要 token。这是改动用户账号的操作。"""
    if not token:
        raise RuntimeError("缺少 GITHUB_TOKEN,无法 star")
    status, _ = gh_request(f"/user/starred/{full_name}", token, method="PUT")
    return status in (204, 304)


# ----------------------------------------------------------------------------- 安装 / 生成 / 注册
def clone_repo(clone_url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(dest)],
        check=True, capture_output=True, text=True,
    )


def detect_install_cmds(repo_dir: Path):
    """检测包管理器,返回建议的安装命令(字符串列表),不执行。"""
    f = lambda n: (repo_dir / n).exists()
    if f("package.json"):
        if f("pnpm-lock.yaml"):
            return ["pnpm install"]
        if f("yarn.lock"):
            return ["yarn install"]
        return ["npm install"]
    if f("pyproject.toml") or f("setup.py"):
        return ["pip install -e ."]
    if f("requirements.txt"):
        return ["pip install -r requirements.txt"]
    if f("Cargo.toml"):
        return ["cargo build --release"]
    if f("go.mod"):
        return ["go build ./..."]
    if f("Makefile"):
        return ["make"]
    return []  # 没有可识别的安装方式,仅 clone


def _short(s: str, n: int) -> str:
    """单行化并截断到 n 字符。"""
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _build_template_description(meta, name: str) -> str:
    """模板兜底时的 description:短句 + 触发词 + 仓库名,目标 ≤240 字符,适合 agent 菜单展示。"""
    summary = _short(meta.get("description") or "", 110)
    triggers = {name}
    for seg in re.split(r"[/\-_]", meta["full_name"].lower()):
        if len(seg) >= 3:
            triggers.add(seg)
    trig_str = "、".join(sorted(triggers))
    head = summary if summary else f"使用 {meta['full_name']} 的能力"
    return _short(f"{head}。触发:{trig_str};仓库 {meta['full_name']}。", 240)


def gen_skill_md(meta, readme, install_cmds, run_hint=""):
    """生成 SKILL.md 内容。有 ANTHROPIC_API_KEY 用模型写,否则套模板。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    name = meta["full_name"].split("/")[-1].lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")

    if key:
        llm = _gen_skill_md_llm(meta, readme, install_cmds, name)
        if llm:
            return llm
        print("  [warn] LLM 返回内容不像 SKILL.md 格式,回退模板", file=sys.stderr)
    else:
        print("  [warn] 未设 ANTHROPIC_API_KEY,使用模板生成(description 质量较低)", file=sys.stderr)

    # ---- 模板回退 ----
    install_block = "\n".join(f"```bash\n{c}\n```" for c in install_cmds) or "_仓库未提供标准安装方式,clone 后参考其 README。_"
    readme_excerpt = (readme.strip()[:600] + " …") if readme.strip() else "（仓库无 README）"
    description = _build_template_description(meta, name)
    return f"""---
name: {name}
description: {description}
---

# {name}

来源仓库: {meta['full_name']}  ({meta['stars']}★, {meta['language']})
{meta['html_url']}

## 这个技能能做什么
{meta['description'] or '(见下方 README 摘录)'}

## 安装
{install_block}

## 运行 / 使用
{run_hint or '参考下方 README 摘录与仓库文档。'}

## 仓库 README 摘录
{readme_excerpt}

## 注意
- 本技能由 skillforge 自动从公开仓库生成,使用前请审阅仓库代码与依赖。
- 此 description 为模板兜底版;设置 ANTHROPIC_API_KEY 后重新生成或手工补全"何时该用 / 何时不用"会更好触发。
"""


def _gen_skill_md_llm(meta, readme, install_cmds, name):
    key = os.environ.get("ANTHROPIC_API_KEY")
    prompt = (
        "你在为一个 GitHub 项目编写 agent 技能文件 SKILL.md。要求:\n"
        "1) 第一段是 YAML frontmatter,含 name 和 description 两个字段,用 --- 包裹。\n"
        "2) description 要写得主动、包含触发词,说明『何时该用』和『能做什么』。\n"
        "3) 正文用中文 markdown,包含:能做什么、安装命令、最小可用示例、常见坑。简洁,<300 行。\n"
        f"项目: {meta['full_name']}  ({meta['stars']}★, 语言 {meta['language']})\n"
        f"项目描述: {meta['description']}\n"
        f"建议安装命令: {install_cmds}\n"
        f"建议技能名(name 字段用这个): {name}\n"
        f"README(节选):\n{readme[:4000]}\n\n"
        "只输出 SKILL.md 的完整内容,不要任何额外说明。"
    )
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2000,
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
        text = "".join(b.get("text", "") for b in data.get("content", []))
        text = text.strip()
        # 容忍 ```markdown ... ``` / ``` ... ``` 包裹
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
        return text if text.startswith("---") else None
    except Exception as e:
        print(f"[warn] LLM 生成 SKILL.md 失败,回退模板: {e}", file=sys.stderr)
        return None


def register_target_dirs():
    raw = os.environ.get("SKILLFORGE_REGISTER_DIRS")
    return raw.split(os.pathsep) if raw else list(DEFAULT_AGENT_DIRS)


def find_handcrafted_skill_md(name: str, our_skill_dir: Path):
    """在各注册目标里找已存在的、非软链、description 较长的 SKILL.md。
    返回 (existing_md_path, description_length, source_dir) 或 None。
    """
    best = None
    for d in register_target_dirs():
        base = Path(d).expanduser()
        cand_dir = base / name
        cand_md = cand_dir / "SKILL.md"
        if not cand_md.exists() or cand_md.is_symlink() or cand_dir.is_symlink():
            continue
        try:
            if os.path.realpath(cand_dir) == os.path.realpath(our_skill_dir):
                continue  # 是我们自己,跳过
        except OSError:
            pass
        meta_ex = parse_frontmatter(cand_md)
        if not meta_ex:
            continue
        length = len(meta_ex.get("description") or "")
        if length < 80:
            continue  # 太短,可能本来就是 stub
        if not best or length > best[1]:
            best = (cand_md, length, cand_dir)
    return best


def adopt_handcrafted_skill_md(source_dir: Path, dest_skill_dir: Path):
    """把手写版整目录搬到 SKILLFORGE_HOME 下,原目录搬到 BACKUP_HOME 留作备份。
    后续 register_skill 会自动在原位置建一条软链指回来。
    """
    import shutil
    dest_skill_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        target = dest_skill_dir / item.name
        if target.exists():
            continue  # 不覆盖 repo/、不覆盖刚 clone 出的内容
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    return backup_skill_dir(source_dir)


def register_skill(skill_dir: Path, link=True):
    """把技能注册到各 agent 目录:默认软链(一处更新处处生效),失败则复制。"""
    results = []
    for d in register_target_dirs():
        target_base = Path(d).expanduser()
        target_base.mkdir(parents=True, exist_ok=True)
        target = target_base / skill_dir.name
        if target.exists() or target.is_symlink():
            results.append((str(target), "已存在,跳过"))
            continue
        try:
            if link:
                target.symlink_to(skill_dir, target_is_directory=True)
                results.append((str(target), "软链"))
            else:
                raise OSError
        except OSError:
            import shutil
            shutil.copytree(skill_dir, target)
            results.append((str(target), "复制"))
    return results


# ----------------------------------------------------------------------------- find pipeline
import datetime
import math


def _age_days(iso_ts: str) -> int:
    """ISO 8601 字符串(GitHub 给的)→ 现在距它过了多少天。失败返回 99999。"""
    if not iso_ts:
        return 99999
    try:
        dt = datetime.datetime.strptime(iso_ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (now - dt).days)
    except (ValueError, TypeError):
        return 99999


def compute_t_score(meta: dict, scorecard=None, osv_vulns=None) -> int:
    """治理透明度 0-100。spec §5.1 + v2 增补 §3。
    scorecard: fetch_scorecard 返回的 dict 或 None
    osv_vulns: fetch_osv_vulns 返回的 list 或 None
    """
    if meta.get("archived"):
        return 0

    score = 0
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

    age = _age_days(meta.get("created_at", ""))
    stars = meta.get("stargazers_count") or 0
    contribs = meta.get("contributors_count") or 0

    if age < 14 and stars > 100:
        score -= 30
    if contribs == 1 and stars > 100 and age < 60:
        score -= 20
    if _age_days(meta.get("pushed_at", "")) > 365:
        score -= 10

    # v2 增补:Scorecard bonus + OSV 严重漏洞 penalty
    if scorecard and isinstance(scorecard.get("score"), (int, float)):
        score += int(round(scorecard["score"]))  # 0-10 直接加
    if osv_vulns:
        n_critical = sum(1 for v in osv_vulns
                         if (v.get("severity") or "").upper() in {"HIGH", "CRITICAL"})
        score -= min(50, 30 * n_critical)

    return max(0, min(100, score))


def _parse_last_page(link_header: str) -> int:
    """从 GitHub Link header 解析 rel='last' 的 page 号。没有就返回 1。"""
    m = re.search(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"', link_header or "")
    return int(m.group(1)) if m else 1


def fetch_metadata(full_name: str, token=None) -> dict:
    """抓 spec §6.1 全部字段,顺手算 T 分 + flags 一起返回。
    失败的子请求降级:contributors_count → 1,release_count → 0。
    """
    _, repo = gh_request(f"/repos/{full_name}", token)

    try:
        _, contribs = gh_request(f"/repos/{full_name}/contributors?per_page=4&anon=true", token)
        contributors_count = len(contribs) if isinstance(contribs, list) else 1
    except Exception:
        contributors_count = 1

    try:
        url = GITHUB_API + f"/repos/{full_name}/releases?per_page=1"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "skillforge",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            link = r.headers.get("link", "")
            body = json.loads(r.read() or b"[]")
        release_count = _parse_last_page(link) if link else len(body)
    except Exception:
        release_count = 0

    meta = dict(repo)
    meta["contributors_count"] = contributors_count
    meta["release_count"] = release_count
    meta["T"] = compute_t_score(meta)
    meta["risk_flags"] = compute_risk_flags(meta)
    return meta


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
    支持: Python(setup.py/pyproject.toml) / JavaScript|TypeScript(package.json) / Rust(Cargo.toml)。
    其他语言返回 None。
    """
    lang = (language or "").lower()
    repo_basename = full_name.split("/")[-1].lower().replace("_", "-")

    if lang == "python":
        text = _fetch_raw(full_name, default_branch, "setup.py")
        m = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return {"ecosystem": "pypi", "name": m.group(1)}
        text = _fetch_raw(full_name, default_branch, "pyproject.toml")
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.MULTILINE)
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
        return None  # npm 命名严格,不冒猜

    if lang == "rust":
        text = _fetch_raw(full_name, default_branch, "Cargo.toml")
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            return {"ecosystem": "cargo", "name": m.group(1)}
        return {"ecosystem": "cargo", "name": repo_basename}

    return None


def _llm_call(prompt: str, max_tokens: int = 1024, model: str = None):
    """Anthropic Messages 单轮调用,返回 text 字符串。无 key 或失败 → None。"""
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
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    return text


def llm_rewrite_query(query: str) -> list:
    """中文需求 → 3 个不同角度的英文 query。无 key / 解析失败 → [原始 query]。"""
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


def _coarse_heuristic(candidates: list) -> list:
    """无 LLM 时的回退:0.6*U + 0.4*T 降序。"""
    scored = [(c, 0.6 * (c.get("U") or 0) + 0.4 * (c.get("T") or 0)) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"full_name": c["full_name"], "reason": f"启发式分 {s:.1f}"} for c, s in scored[:5]]


def llm_coarse_rerank(query: str, candidates: list) -> list:
    """N 个候选 → Top 5 (full_name + reason)。无 key 走启发式回退。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _coarse_heuristic(candidates)

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


def _final_heuristic(candidates: list) -> list:
    """无 LLM 时:按 U+T 降序,造 dummy R/recommend_level/why。"""
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
            "R": None,  # 启发式算不了相关性
            "recommend_level": level,
            "why": "(无 ANTHROPIC_API_KEY,按 U+T 启发式排序)",
            "risks": c.get("risk_flags", []),
        })
    return out


def llm_final_rank(query: str, candidates: list) -> list:
    """5 个候选 + README → Top 3 含 R / 级别 / 中文理由 / 风险。无 key 走启发式。"""
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


def _fmt_int(n):
    if n is None: return "无数据"
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}k"
    return str(n)


def _stars(level: str) -> str:
    return {"强推": "⭐⭐⭐ 强推 ", "推荐": "⭐⭐ 推荐  ",
            "谨慎": "⭐ 谨慎   ", "不推荐": "❌ 不推荐 "}.get(level, level)


def _u_breakdown(m: dict) -> list:
    """把 U (使用度) 分拆成 6 项子指标,与 compute_u_score 逻辑一致。
    返回 [(label, points, max, note), ...],用于 render_top3 明细展示。"""
    def _log(n, ceiling):
        return min(1.0, math.log10((n or 0) + 1) / math.log10(ceiling + 1))
    s = m.get('stargazers_count') or 0
    w = m.get('subscribers_count') or 0
    f = m.get('forks_count') or 0
    d = m.get('monthly_downloads')
    r = m.get('release_count') or 0
    c = m.get('close_rate')
    return [
        ('star',      round(_log(s, 100000) * 20, 1),  20, f'★{s}'),
        ('watch',     round(_log(w, 10000)  * 20, 1),  20, f'👁{w}'),
        ('fork',      round(_log(f, 10000)  * 15, 1),  15, f'🔱{f}'),
        ('download',  round(_log(d, 10000000) * 30, 1) if d is not None else 0.0,  30,
                      f'📥{d}/月' if d is not None else '📥无'),
        ('release',   round(min(r, 20) / 20 * 10, 1),  10, f'📦{r}个'),
        ('close_rate',round((c or 0) * 5, 1),           5,
                      f'💬{int((c or 0)*100)}%' if c is not None else '💬无历史'),
    ]


def _t_breakdown(m: dict, scorecard=None, osv_vulns=None) -> list:
    """把 T (治理度) 分拆成 加分项 + 惩罚项,与 compute_t_score 逻辑一致。
    返回 [(label, points, max, note), ...]"""
    if m.get('archived'):
        return [('archived', 0, 100, '归档 → 直接 0')]
    items = []
    items.append(('LICENSE',    20 if m.get('license') else 0, 20, str(m.get('license') or '无')[:20]))
    items.append(('主分支名',   15 if m.get('default_branch') in {'main','master','develop'} else 0, 15, m.get('default_branch','?')))
    push_age = _age_days(m.get('pushed_at',''))
    items.append(('近期活跃',   15 if push_age <= 90 else 0, 15, f'{push_age}天前 push'))
    contribs = m.get('contributors_count') or 0
    items.append(('多维护者',   10 if contribs >= 3 else 0, 10, f'{contribs}人'))
    age = _age_days(m.get('created_at',''))
    items.append(('存活>90天',  10 if age >= 90 else 0, 10, f'仓龄{age}天'))
    items.append(('开 issues',  10 if m.get('has_issues') else 0, 10, '是' if m.get('has_issues') else '否'))
    items.append(('组织所有',    5 if (m.get('owner') or {}).get('type') == 'Organization' else 0, 5, (m.get('owner') or {}).get('type','?')))
    items.append(('有 topics',   5 if m.get('topics') else 0, 5, f'{len(m.get("topics") or [])}个'))
    stars = m.get('stargazers_count') or 0
    if age < 14 and stars > 100:
        items.append(('star farming惩罚', -30, 0, f'<14天却>{stars}★'))
    if contribs == 1 and stars > 100 and age < 60:
        items.append(('单人堆星惩罚', -20, 0, f'1人<60天{stars}★'))
    if push_age > 365:
        items.append(('长期不更新惩罚', -10, 0, f'>{push_age}天没 push'))
    if scorecard and isinstance(scorecard.get('score'), (int, float)):
        items.append(('OpenSSF Scorecard', int(round(scorecard['score'])), 10, f'{scorecard["score"]}/10'))
    if osv_vulns:
        n_crit = sum(1 for v in osv_vulns if (v.get('severity') or '').upper() in {'HIGH','CRITICAL'})
        if n_crit:
            items.append(('OSV高危惩罚', -min(50, 30*n_crit), 0, f'{n_crit}个HIGH/CRITICAL'))
    return items


def render_top3(query: str, ranked: list, meta_by_name: dict, trusted_set: set) -> str:
    """ranked:llm_final_rank 输出;meta_by_name: full_name → 完整 meta(含 install_cmds);trusted_set:owner 白名单集合。
    v9.5: 编号 1-based, U/T 展开子指标明细。"""
    lines = [f"🔎 「{query}」 → Top {len(ranked)}\n"]
    for i, item in enumerate(ranked, start=1):
        fn = item["full_name"]
        m = meta_by_name.get(fn, {})
        installs = m.get("install_cmds") or []
        install_str = installs[0] if installs else "(无标准安装方式)"
        owner = fn.split("/")[0].lower()
        trusted = "是" if (owner in trusted_set or fn.lower() in trusted_set) else "否"

        lines.append(f"  {i}. {_stars(item.get('recommend_level',''))}  {fn}  ({m.get('language','')})")
        r = item.get("R")
        r_str = f"{r}/10" if r is not None else "--"
        lines.append(f"      R 相关 {r_str}  (agent 主观判断:描述与需求匹配度)")

        # U 明细
        u_items = _u_breakdown(m)
        u_total = m.get('U', 0)
        u_parts = "  ".join(f"{lab}:{p}/{mx}({note})" for lab, p, mx, note in u_items)
        lines.append(f"      U 使用 {u_total}/100")
        lines.append(f"          = {u_parts}")

        # T 明细
        sc = m.get("scorecard")
        osv = m.get("osv_vulns") or []
        t_items = _t_breakdown(m, scorecard=sc, osv_vulns=osv)
        t_total = m.get('T', 0)
        # 加分 vs 惩罚分开
        pos = [it for it in t_items if it[1] >= 0]
        neg = [it for it in t_items if it[1] < 0]
        t_parts = "  ".join(f"{lab}:{p}/{mx}({note})" for lab, p, mx, note in pos)
        lines.append(f"      T 治理 {t_total}/100")
        lines.append(f"          = {t_parts}")
        if neg:
            neg_parts = "  ".join(f"{lab}:{p}({note})" for lab, p, _, note in neg)
            lines.append(f"          ⚠ 惩罚: {neg_parts}")

        # 安全汇总行
        sc_str = "🛡 Scorecard 无收录" if sc is None else f"🛡 Scorecard {sc.get('score','?')}/10"
        n_crit = sum(1 for v in osv if (v.get("severity") or "").upper() in {"HIGH", "CRITICAL"})
        if not osv:
            osv_str = "OSV 0 vuln"
        elif n_crit:
            osv_str = f"OSV ⚠️ {n_crit} HIGH/CRITICAL ({len(osv)} 总)"
        else:
            osv_str = f"OSV {len(osv)} 低中危"
        lines.append(f"      {sc_str}  ·  {osv_str}")

        lines.append(f"      推荐: {item.get('why','')}")
        risks = item.get("risks") or []
        lines.append(f"      风险: {'  '.join(risks) if risks else '(无)'}")

        suit = item.get("suitable") or _extract_clause(
            m.get("description",""),
            [r"Use when ([^.。\n]+)", r"使用[场]?景[:: ]([^.。\n]+)"]
        )
        not_suit = item.get("not_suitable") or _extract_clause(
            m.get("description",""),
            [r"Do not use (?:for |when )([^.。\n]+)", r"don't use ([^.。\n]+)", r"不[适]?用[于:: ]([^.。\n]+)"]
        )
        if suit:
            lines.append(f"      ✓ 适合: {suit[:90]}")
        if not_suit:
            lines.append(f"      ✗ 别用: {not_suit[:90]}")
        lines.append(f"      装: {install_str}     owner ∈ trusted? {trusted}\n")
    return "\n".join(lines)


_SCORECARD_API = "https://api.securityscorecards.dev/projects/github.com"
_OSV_API = "https://api.osv.dev/v1/query"
# 我们 ecosystem 命名 → OSV 期望命名
_OSV_ECO = {"pypi": "PyPI", "npm": "npm", "cargo": "crates.io"}


def fetch_osv_vulns(ecosystem: str, name: str) -> list:
    """OSV 查包漏洞。返回简化后的 list:[{id, severity, summary}, ...]。失败/无 hit → []。"""
    if not name:
        return []
    osv_eco = _OSV_ECO.get((ecosystem or "").lower(), ecosystem)
    if osv_eco not in {"PyPI", "npm", "crates.io"}:
        return []
    body = json.dumps({"package": {"name": name, "ecosystem": osv_eco}}).encode()
    req = urllib.request.Request(_OSV_API, data=body, method="POST",
                                 headers={"content-type": "application/json", "User-Agent": "skillforge"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return []

    out = []
    for v in (data.get("vulns") or []):
        sev = (v.get("database_specific") or {}).get("severity") or ""
        if not sev:
            # 退到 CVSS:取最高 severity 数字推断等级
            for s in (v.get("severity") or []):
                score = s.get("score") or ""
                if "/" in score:  # CVSS string
                    sev = "HIGH"  # 粗略归类
                    break
        out.append({"id": v.get("id", "?"), "severity": (sev or "UNKNOWN").upper(), "summary": (v.get("summary") or "")[:120]})
    return out


def fetch_scorecard(full_name: str):
    """拉 OpenSSF Scorecard 数据。仓库未收录/网络失败 → None。"""
    if not full_name or "/" not in full_name:
        return None
    url = f"{_SCORECARD_API}/{full_name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "skillforge", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_downloads(ecosystem: str, name: str):
    """月下载量;失败/未知 ecosystem 返回 None。免认证公开端点。"""
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


def fetch_close_rate(full_name: str, token=None):
    """返回 0-1 之间的 issue 闭合率,无历史/失败 → None。"""
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


def compute_u_score(*, stars: int, watchers: int, forks: int,
                    downloads, release_count: int, close_rate) -> int:
    """使用度 0-100。spec §5.2。downloads/close_rate 允许 None。"""
    def _log(n, ceiling):
        return min(1.0, math.log10((n or 0) + 1) / math.log10(ceiling + 1))

    w_s = _log(stars, 100000) * 20
    w_w = _log(watchers, 10000) * 20
    w_f = _log(forks, 10000) * 15
    w_d = _log(downloads, 10000000) * 30 if downloads is not None else 0
    w_r = min(release_count or 0, 20) / 20 * 10
    w_c = (close_rate or 0) * 5

    return int(round(max(0, min(100, w_s + w_w + w_f + w_d + w_r + w_c))))


def compute_risk_flags(meta: dict, scorecard=None, osv_vulns=None) -> list:
    """风险标签 list。spec §5.3 + v2 增补 §3。"""
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

    # v2 增补:OSV 漏洞
    if osv_vulns:
        crit = [v for v in osv_vulns
                if (v.get("severity") or "").upper() in {"HIGH", "CRITICAL"}]
        if crit:
            flags.append(f"🔴 OSV: {len(crit)} 个未修复的 HIGH/CRITICAL 漏洞")

    # v2 增补:Scorecard 总分 + 关键子项
    if scorecard and isinstance(scorecard.get("score"), (int, float)):
        if scorecard["score"] < 4:
            flags.append(f"🔴 Scorecard 总分 {scorecard['score']}/10(安全实践薄弱)")
        # 子项 check
        chk = {c.get("name"): c for c in (scorecard.get("checks") or [])}
        bp = chk.get("Branch-Protection")
        if bp and (bp.get("score") or 0) < 5:
            flags.append("🟡 未启用 branch protection")
        ba = chk.get("Binary-Artifacts")
        if ba and (ba.get("score") or 0) < 10:
            flags.append("🟡 仓库内有 binary artifacts")
        dw = chk.get("Dangerous-Workflow")
        if dw and (dw.get("score") or 0) < 10:
            flags.append("🟡 CI workflow 含危险 pattern")
        du = chk.get("Dependency-Update-Tool")
        if du and (du.get("score") or 0) < 5:
            flags.append("🟡 无依赖更新工具(Dependabot/Renovate)")

    return flags


# ----------------------------------------------------------------------------- 交互
def confirm(prompt, assume_yes=False):
    if assume_yes:
        print(f"{prompt} [自动 yes]")
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ----------------------------------------------------------------------------- 命令
CATALOG_FILE = os.environ.get("SKILLFORGE_CATALOG", "~/.skillforge/CATALOG.md")
CATEGORIES_CACHE = os.environ.get("SKILLFORGE_CATEGORIES", "~/.skillforge/.categories.json")
USAGE_STATS_FILE = os.environ.get("SKILLFORGE_USAGE", "~/.skillforge/.usage_stats.json")

# ============================================================================
# MECE 五大一级分类(v9,借鉴用户提供的零级红线 protocol)
# 5 个业务类 + 1 个"环境隔离区"(native infra 剥离)
# 双语标签:根据 --lang zh/en/auto 输出
# ============================================================================

# 语言检测
def detect_lang(query_text: str = None, explicit: str = None) -> str:
    """决定输出语言。优先级:explicit > query CJK > env LANG > 'zh' 默认。"""
    if explicit and explicit in ("zh", "en"):
        return explicit
    if query_text:
        if any("一" <= c <= "鿿" for c in query_text):
            return "zh"
        # 纯英文 query 且长度 >= 3 → en
        if len(query_text.strip()) >= 3:
            return "en"
    lang = os.environ.get("LANG", "") or os.environ.get("LC_ALL", "")
    if lang.startswith(("en_", "en-", "en.")):
        return "en"
    return "zh"  # 默认中文(会话主要语种)


# MECE 5+1 分类字典(每个 key 都有 zh/en/contract_zh/contract_en/hint_zh/hint_en)
_MECE_CATEGORIES = {
    "data_fetcher": {
        "zh": "🟢 数据感知与检索",
        "en": "🟢 Data Fetcher",
        "contract_zh": "外部查询/URL ➡️ 只读结构化数据",
        "contract_en": "External query/URL ➡️ read-only structured data",
        "hint_zh": "上游节点,输出通常流转给 Transformer 处理",
        "hint_en": "Upstream node; typically pipe output to a Transformer",
    },
    "content_transformer": {
        "zh": "🔵 内容转化与处理",
        "en": "🔵 Content Transformer",
        "contract_zh": "输入数据/文件 ➡️ 转化后的数据/文件",
        "contract_en": "Input data/file ➡️ transformed data/file",
        "hint_zh": "纯本地变换,可多个串联",
        "hint_en": "Pure local transform; chainable",
    },
    "multi_modal_generator": {
        "zh": "🔥 多模态创作",
        "en": "🔥 Multi-Modal Generator",
        "contract_zh": "需求描述 ➡️ 生成的资产(图/音/视/代码)",
        "contract_en": "Requirement spec ➡️ generated asset (image/audio/video/code)",
        "hint_zh": "算力密集,生成全新资产",
        "hint_en": "Compute-intensive; produces fresh assets",
    },
    "action_executor": {
        "zh": "⚡ 动作执行与控制",
        "en": "⚡ Action Executor",
        "contract_zh": "配置/命令 ➡️ 外部系统状态变更",
        "contract_en": "Config/command ➡️ external state change",
        "hint_zh": "危险操作,前置需 dry-run/confirm",
        "hint_en": "State-changing; dry-run/confirm advised",
    },
    "integration_utility": {
        "zh": "🛠 跨组件集成工具",
        "en": "🛠 Integration Utility",
        "contract_zh": "上下游 skill I/O ➡️ 路由/桥接/中间件",
        "contract_en": "Upstream/downstream skill I/O ➡️ routing/bridge/middleware",
        "hint_zh": "连接其它 skill 的胶水,常做前置/路由",
        "hint_en": "Glue between skills; often prereq/router",
    },
    "native_infra": {  # 隔离区
        "zh": "🚫 系统原生基建(隔离)",
        "en": "🚫 Native Infrastructure (isolated)",
        "contract_zh": "OS/IDE 内置能力",
        "contract_en": "OS/IDE built-in capability",
        "hint_zh": "不参与业务 skill 编排",
        "hint_en": "Not part of business skill orchestration",
    },
}


def mece_label(key: str, lang: str = "zh") -> str:
    """category key → 双语标签(如 '🟢 数据感知与检索' / '🟢 Data Fetcher')。"""
    entry = _MECE_CATEGORIES.get(key) or _MECE_CATEGORIES["integration_utility"]
    return entry[lang] if lang in ("zh", "en") else entry["zh"]


def mece_contract(key: str, lang: str = "zh") -> str:
    entry = _MECE_CATEGORIES.get(key) or _MECE_CATEGORIES["integration_utility"]
    return entry[f"contract_{lang}"] if f"contract_{lang}" in entry else entry["contract_zh"]


def mece_hint(key: str, lang: str = "zh") -> str:
    entry = _MECE_CATEGORIES.get(key) or _MECE_CATEGORIES["integration_utility"]
    return entry[f"hint_{lang}"] if f"hint_{lang}" in entry else entry["hint_zh"]


# 每个 skill 的紧凑双语释义(≤25 字中文 / ≤12 words 英文)
# 用户 v9.1 明确要求:/skill-列表 输出「英文名 + 一句中文释义」紧凑格式,不再贴原描述长文
# 未收录的 skill 走 brief_for 的 fallback(取原 description 首句截断)
_BRIEF_TRANSLATIONS = {
    # 🟢 Data Fetcher (6)
    "figma": {"zh": "拉 Figma 设计上下文/截图/变量/资产", "en": "Fetch Figma design context, screenshots, variables, assets"},
    "security-ownership-map": {"zh": "分析 git 仓库算安全所有权拓扑与 bus factor", "en": "Analyze git repo for security ownership topology and bus factor"},
    "navigating-chatgpt-history": {"zh": "检索归档的 ChatGPT/Claude 会话导出", "en": "Navigate archived ChatGPT/Claude conversation exports"},
    "notion-research-documentation": {"zh": "Notion 多源研究并合成带引用的文档", "en": "Research across Notion, synthesize cited documentation"},
    "openai-docs": {"zh": "查 OpenAI 官方最新文档与模型选型", "en": "Query official OpenAI docs and model selection guidance"},
    "sentry": {"zh": "只读查 Sentry issue/事件/生产错误", "en": "Read-only inspect Sentry issues, events, production errors"},

    # 🔵 Content Transformer (18)
    "knight-imagetopptx-skill": {"zh": "图片/PDF页/截图重建为可编辑 PPTX", "en": "Rebuild slide images / PDF pages into editable PPTX"},
    "gsap-scrolltrigger": {"zh": "GSAP 滚动动画/pinning", "en": "GSAP ScrollTrigger — scroll-linked animation and pinning"},
    "markitdown-convert": {"zh": "PDF/DOCX/PPTX/图片/音频/YouTube 转 Markdown", "en": "Convert PDF/DOCX/PPTX/image/audio/YouTube to Markdown"},
    "security-threat-model": {"zh": "仓库级威胁建模,输出 Markdown", "en": "Repo-grounded threat modeling, writes Markdown model"},
    "figma-code-connect-components": {"zh": "Figma 组件与代码组件做 Code Connect 映射", "en": "Map Figma components to code via Code Connect"},
    "figma-implement-design": {"zh": "Figma 设计 1:1 转生产代码", "en": "Translate Figma designs into 1:1 production code"},
    "gsap-core": {"zh": "GSAP 核心 API (tween/easing/stagger)", "en": "GSAP core API — tween/easing/stagger/matchMedia"},
    "gsap-frameworks": {"zh": "GSAP 在 Vue/Svelte 等非 React 框架里的用法", "en": "GSAP for Vue, Svelte and non-React frameworks"},
    "gsap-performance": {"zh": "GSAP 动画性能优化 (transform/避免抖动)", "en": "GSAP animation performance — transforms, avoid jank"},
    "gsap-plugins": {"zh": "GSAP 插件集 (ScrollTo/Flip/Draggable/SplitText…)", "en": "GSAP plugins — ScrollTo/Flip/Draggable/SplitText etc"},
    "gsap-react": {"zh": "GSAP 在 React/Next.js 里的用法 (useGSAP)", "en": "GSAP for React/Next.js — useGSAP hook and cleanup"},
    "gsap-timeline": {"zh": "GSAP 时间线编排/关键帧序列", "en": "GSAP timelines — sequencing and keyframe choreography"},
    "gsap-utils": {"zh": "gsap.utils 辅助函数 (clamp/mapRange/random)", "en": "gsap.utils helpers — clamp/mapRange/random/snap"},
    "impeccable": {"zh": "前端审查/精修/打磨 (audit/critique/polish/harden)", "en": "Frontend audit/critique/polish/harden/animate suite"},
    "kami": {"zh": "精排 PDF/落地页/简历/白皮书/幻灯片", "en": "Typeset premium PDF/landing/resume/white paper/slides"},
    "transcribe": {"zh": "音视频转文字,支持说话人分离", "en": "Transcribe audio/video with optional speaker diarization"},
    "pdf": {"zh": "PDF 读取/生成/审阅 (reportlab/pdfplumber/pypdf)", "en": "PDF read/generate/review via reportlab/pdfplumber/pypdf"},
    "redesign-existing-projects": {"zh": "现有网站/应用高端化改造", "en": "Redesign existing sites/apps to premium quality"},

    # 🔥 Multi-Modal Generator (8)
    "asset-forge": {"zh": "本地批量素材处理 (去背景/矢量化/WebP/WebM)", "en": "Local batch asset processing — bg removal/SVG/WebP/WebM"},
    "notion-meeting-intelligence": {"zh": "结合 Notion 上下文准备会议材料", "en": "Prepare meeting materials from Notion context"},
    "frontend-design": {"zh": "从零生成高质感前端页面/组件", "en": "Generate distinctive production-grade frontend UI"},
    "hatch-pet": {"zh": "从美术稿生成 Codex 动画宠物精灵图", "en": "Build Codex animated pet spritesheets from art"},
    "jupyter-notebook": {"zh": "创建/编辑 Jupyter notebook", "en": "Create or edit Jupyter notebooks from templates"},
    "drawio": {"zh": "可编辑 .drawio 图表 (架构图/流程图)", "en": "Editable .drawio diagrams — architecture / workflow / flowchart"},
    "pixel2motion": {"zh": "位图 logo → 极简 SVG → 动画 HTML", "en": "Raster logo → minimal SVG → animated HTML reveal"},
    "speech": {"zh": "OpenAI TTS 文本转语音/旁白", "en": "OpenAI TTS narration/voiceover via bundled CLI"},

    # ⚡ Action Executor (18)
    "figma-generate-design": {"zh": "从代码/描述在 Figma 里搭建整页/整屏", "en": "Build full pages/screens in Figma from code or spec"},
    "figma-use": {"zh": "每次 use_figma 写操作强制前置 skill", "en": "MANDATORY prereq skill before every use_figma call"},
    "figma-create-new-file": {"zh": "新建空白 Figma/FigJam 文件", "en": "Create a new blank Figma or FigJam file"},
    "notion-spec-to-implementation": {"zh": "Notion 规格 → 实施计划 + 任务", "en": "Turn Notion specs into implementation plans and tasks"},
    "codex-sessions-manager": {"zh": "本地 Codex 会话审计/清理/删除/恢复", "en": "Audit / clean / delete / restore local Codex sessions"},
    "enable-1m-context": {"zh": "Codex 打开 1M token 上下文", "en": "Enable/repair 1M token context for Codex Desktop/CLI"},
    "figma-generate-library": {"zh": "在 Figma 里从代码库反推建设计系统", "en": "Build Figma design system reverse-engineered from codebase"},
    "gh-fix-ci": {"zh": "修 GitHub Actions PR 检查失败", "en": "Debug and fix failing GitHub Actions PR checks"},
    "letta-fleet-management": {"zh": "kubectl 风格声明式管理 Letta 智能体舰队", "en": "Declarative kubectl-style Letta agent fleet management"},
    "notion-knowledge-capture": {"zh": "对话/决议沉淀到 Notion 结构化页面", "en": "Capture conversations/decisions into structured Notion pages"},
    "cloudflare-deploy": {"zh": "部署到 Cloudflare Workers/Pages", "en": "Deploy to Cloudflare Workers/Pages and related services"},
    "netlify-deploy": {"zh": "通过 Netlify CLI 部署站点", "en": "Deploy web projects to Netlify via CLI"},
    "render-deploy": {"zh": "部署到 Render 云", "en": "Deploy applications to Render via Blueprints"},
    "vercel-deploy": {"zh": "部署到 Vercel", "en": "Deploy applications and websites to Vercel"},
    "linear": {"zh": "在 Linear 里管理 issue/项目/工单", "en": "Manage Linear issues, projects and team workflows"},
    "gh-address-comments": {"zh": "处理当前分支 PR 的评审意见", "en": "Address GitHub PR review/issue comments via gh CLI"},
    "migrate-to-codex": {"zh": "把指令/skill/agent/MCP 迁到 Codex", "en": "Migrate instructions/skills/agents/MCP config into Codex"},
    "yeet": {"zh": "一条龙 stage→commit→push→开 PR", "en": "One-shot stage/commit/push and open PR via gh"},

    # 🛠 Integration Utility (23)
    "media-ai-routing": {"zh": "视频翻译/TTS/克隆/短视频流水线的路由决策", "en": "Route video translation/TTS/cloning/short-video pipelines"},
    "chatgpt-apps": {"zh": "构建 ChatGPT Apps SDK 应用 (MCP + widget UI)", "en": "Build ChatGPT Apps SDK apps — MCP server + widget UI"},
    "karpathy-guidelines": {"zh": "严谨编码行为准则 (明确假设/最小 diff/可验证)", "en": "Rigorous coding guidelines — assumptions, minimal diff, verifiable"},
    "security-best-practices": {"zh": "语言/框架的安全最佳实践审查", "en": "Language/framework-specific security best-practice review"},
    "creating-letta-code-channels": {"zh": "构建 Letta Code 渠道适配器 (TG/Slack/Discord)", "en": "Build Letta Code channel adapters — TG/Slack/Discord/etc"},
    "figma-create-design-system-rules": {"zh": "生成项目定制的设计系统规则", "en": "Generate custom design system rules for the codebase"},
    "letta-filesystem-to-memfs": {"zh": "把 Letta Filesystem 迁到 MemFS", "en": "Migrate deprecated Letta Filesystem to MemFS with lexical search"},
    "letta-conversations-api": {"zh": "Letta 会话 API 管理独立消息线程", "en": "Manage isolated message threads via Letta Conversations API"},
    "letta-development-guide": {"zh": "Letta 智能体开发全流程指南", "en": "Full guide for developing Letta agents end to end"},
    "my-auto-compact": {"zh": "会话压缩前保存 handoff 快照", "en": "Save handoff snapshot before session context compaction"},
    "aspnet-core": {"zh": "ASP.NET Core Web 应用开发/审查/重构", "en": "Build, review, refactor ASP.NET Core web applications"},
    "cli-creator": {"zh": "从 API/OpenAPI/curl/SDK 生成可组合 CLI", "en": "Generate composable CLI from API/OpenAPI/curl/SDK"},
    "compaction-prompts": {"zh": "配置 Letta 智能体压缩/摘要 prompt", "en": "Configure Letta agent compaction and summarization prompts"},
    "letta-configuration": {"zh": "配置 Letta 智能体的 LLM 提供方/模型", "en": "Configure Letta agent LLM models and providers"},
    "my-compact": {"zh": "从 handoff 生成运行时压缩摘要", "en": "Turn handoff file into concise runtime summary"},
    "winui-app": {"zh": "WinUI 3 桌面应用开发 (C# + Windows App SDK)", "en": "Build modern WinUI 3 desktop apps with C# and Windows App SDK"},
    "playwright": {"zh": "终端里驱动真实浏览器做自动化", "en": "Automate a real browser from terminal via playwright-cli"},
    "skillforge": {"zh": "跨 agent 技能闭环管理 (本工具自身)", "en": "Cross-agent skill discovery/install/manage (this tool)"},
    "design-taste-frontend": {"zh": "反 slop 的前端 skill,避免模板脸", "en": "Anti-slop frontend skill — avoids templated look"},
    "importing-chatgpt-memory": {"zh": "把 ChatGPT 记忆导入 Letta", "en": "Clone ChatGPT saved memory into Letta with enrichment"},
    "letta-api-client": {"zh": "用 Letta API 构建持久化智能体应用", "en": "Build persistent agent apps with the Letta API"},
    "define-goal": {"zh": "帮用户在开工前定义可量化目标", "en": "Define concrete measurable goals before starting work"},
    "playwright-interactive": {"zh": "Electron/浏览器交互式 UI 调试", "en": "Persistent browser/Electron interactive UI debugging"},

    # 🚫 Native Infra (1)
    "screenshot": {"zh": "OS 级桌面/窗口/区域截图", "en": "OS-level desktop/window/region screenshot capture"},
}


def brief_for(name: str, description: str, lang: str = "zh") -> str:
    """一句紧凑释义。字典命中直接返回,否则从原描述截首句。"""
    entry = _BRIEF_TRANSLATIONS.get(name)
    if entry and lang in entry:
        return entry[lang]
    # Fallback: 取原 description 首句(截断)
    text = (description or "").strip()
    if not text:
        return "(no description)" if lang == "en" else "(无描述)"
    for sep in ["。", ". ", "; ", "。 "]:
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    limit = 80 if lang == "zh" else 100
    return text[:limit] + ("…" if len(text) > limit else "")


# MECE 分类规则:name/description 中命中关键词 → 对应类
# 顺序 = 优先级(强特征在前,泛特征在后)。默认落 integration_utility。
_MECE_RULES = [
    # 分类采用 name==X 精确匹配 skill 名 + 少量安全的语义关键词
    # 顺序 = 优先级(隔离区先剥离,然后 Executor/Generator/Transformer/Fetcher,最后 Utility 兜底)

    # 1) 🚫 系统原生基建 → 隔离区
    ("native_infra", ["name==screenshot"]),

    # 2) ⚡ Action_Executor(改变外部状态的写操作)
    ("action_executor", [
        # 部署平台
        "name==vercel-deploy", "name==netlify-deploy",
        "name==cloudflare-deploy", "name==render-deploy",
        # GitHub PR/CI 写操作
        "name==yeet", "name==gh-address-comments", "name==gh-fix-ci",
        # issue tracker 写
        "name==linear",
        # Notion 写页面
        "name==notion-knowledge-capture", "name==notion-spec-to-implementation",
        # Figma 写文件
        "name==figma-create-new-file", "name==figma-use",
        "name==figma-generate-design", "name==figma-generate-library",
        # 系统配置 patch / 迁移 / 删除
        "name==enable-1m-context", "name==migrate-to-codex",
        "name==codex-sessions-manager",
        # Letta fleet 写操作
        "name==letta-fleet-management",
    ]),

    # 3) 🔥 Multi_Modal_Generator(算力生成新资产)
    ("multi_modal_generator", [
        "name==asset-forge", "name==hatch-pet", "name==speech",
        "name==drawio", "name==frontend-design", "name==pixel2motion",
        "name==notion-meeting-intelligence", "name==jupyter-notebook",
    ]),

    # 4) 🔵 Content_Transformer(纯本地数据 → 数据转换)
    ("content_transformer", [
        "name==markitdown-convert", "name==transcribe", "name==pdf",
        "name==knight-imagetopptx-skill", "name==security-threat-model",
        "name==impeccable", "name==redesign-existing-projects",
        "name==figma-implement-design", "name==figma-code-connect-components",
        "name==kami",
        # GSAP 系列:输入需求 → 输出代码片段
        "name==gsap-core", "name==gsap-scrolltrigger", "name==gsap-timeline",
        "name==gsap-react", "name==gsap-frameworks", "name==gsap-plugins",
        "name==gsap-performance", "name==gsap-utils",
    ]),

    # 5) 🟢 Data_Fetcher(只读外部)
    ("data_fetcher", [
        "name==figma",  # Figma MCP 主要是 read
        "name==sentry",  # 只读 Sentry
        "name==openai-docs",
        "name==navigating-chatgpt-history",
        "name==security-ownership-map",
        "name==notion-research-documentation",
    ]),

    # 6) 🛠 兜底 Integration_Utility
    # 未在上面命中的一律归 utility(skillforge/letta-*/karpathy/define-goal/
    # chatgpt-apps/cli-creator/aspnet/winui/playwright/design-taste-frontend/
    # my-compact/creating-letta-code-channels/media-ai-routing/importing-chatgpt-memory/
    # letta-filesystem-to-memfs/letta-configuration/letta-development-guide/
    # compaction-prompts/security-best-practices/figma-create-design-system-rules 等)
]


def _match_mece_rule(text: str, name: str) -> str:
    """按 _MECE_RULES 顺序匹配。返回 key,默认 'integration_utility'。"""
    tl = text.lower()
    nl = name.lower()
    for key, kws in _MECE_RULES:
        for kw in kws:
            if kw.startswith("name=="):
                if nl == kw[6:]:
                    return key
            elif kw in tl:
                return key
    return "integration_utility"


def mece_category(meta: dict) -> str:
    """根据 name + description 推断 MECE 5+1 类别 key。"""
    text = (meta.get("name", "") + " " + (meta.get("description") or "")).strip()
    return _match_mece_rule(text, meta.get("name", ""))


def mece_category_cached(name: str, meta: dict) -> str:
    """带缓存版本(md5 sig 失效)。"""
    import hashlib
    cache = _categories_cache_load()
    sig = hashlib.md5(((meta.get("description") or "") + name + "v9-mece").encode("utf-8")).hexdigest()[:12]
    entry = cache.get(name)
    if entry and entry.get("sig") == sig and "mece" in entry:
        return entry["mece"]
    key = mece_category(meta)
    cache[name] = {"sig": sig, "mece": key,
                   "category": entry.get("category") if entry else None}
    _categories_cache_save(cache)
    return key


# ---- 兼容旧接口:skill_category 仍可用,返回 MECE label(zh) ----
# 老的 27 类 _CATEGORY_RULES 已删除,skill_category 现在返回 MECE 标签。
# ============================================================================
# (旧 27 类分类规则,v9 已弃用)
# ============================================================================
# 注:v8 的 _CATEGORY_RULES / _category_for_text / categorize_skill 保留在下面
# 作为 fallback,防止老 slash 模板/外部调用触发。新代码统一走 mece_*。
_CATEGORY_RULES = [
    # 顺序原则:特征独占的强匹配在前,泛词类在后
    # 1) Letta 系列(8+ 个独立 skill)
    ("🤖 Letta agent",  ["letta"]),
    # 2) GSAP(只看 gsap 前缀,不被泛 animation 抢)
    ("🌊 GSAP/动效",     ["gsap-", "scrolltrigger", "framework-agnostic animation", "gsap.to(", "gsap.timeline"]),
    # 3) Figma 系列
    ("🖼 Figma",         ["figma"]),
    # 4) Notion(严:必须 notion- 前缀,避免被 markitdown 等 "import to notion" 抢)
    ("📓 Notion",        ["notion-"]),
    # 5) 路由/智能(本工具自身,放前面避免被 "skill"/"agent" 通配抢)
    ("🧭 路由/智能",     ["skillforge", "skill-creator", "anthropic-skill", "dispatch", "subagent", "superpowers", "brainstorm", "writing-plan", "writing-skill"]),
    # 6) GitHub PR/CI 单独
    ("🐙 GitHub PR/CI",  ["gh-address", "gh-fix-ci", " yeet ", "yeet:", "github pull request", "github ci", "github actions", "open a pull request"]),
    # 7) 前端审美(放在浏览器前,impeccable 应归审美而非浏览器)
    ("✨ 前端审美",      ["impeccable", "anti-slop", "taste-skill", "redesign-existing", "ui-ux-pro", "premium quality", "design-taste"]),
    # 8) 浏览器自动化
    ("🌐 浏览器",        ["browser ", "playwright", "claude in chrome", "chrome devtools", "puppeteer", "selenium"]),
    # 9) 排版/Typesetting — 放在 PPT 前,kami 不该归 PPT
    ("🎨 排版/Typeset",  ["kami", "typesetting", "one-pager", "白皮书", "landing page typography", "marp"]),
    # 10) 图像处理 — 放在视频音频前,asset-forge 主要是 image(虽然能转 webm)
    ("🖌 图像处理",      ["rembg", "asset-forge", "pixel2motion", "asset optimization", "background remov", "image background", "logo design", "raster", "imagegen", "spritesheet", "去背景", "hatch-pet"]),
    # 11) 视频音频
    ("🎬 视频音频",      ["transcribe", " tts ", "voiceover", "subtitle", "webm video", "video translation", "video composition", "audio file", "speech ", "音频", "视频翻译", "extract text from recording", "extract text from audio"]),
    # 12) 数据格式转换 — 放在 PPT/Word/Excel 前
    ("📦 数据转换",      ["markitdown", "convert files", "convert pdf", "rag ingestion", "to markdown", "doc to markdown"]),
    # 13) 截图(独立小类)
    ("🖼 截图",          ["screenshot"]),
    # 14) PPT 显式格式
    ("📊 PPT/幻灯",      ["pptx", "powerpoint", "knight-imagetopptx", "幻灯", "slide deck"]),
    # 15) PDF 显式格式
    ("📋 PDF",           ["pdf"]),
    # 16) Word 显式 docx
    ("📝 Word/DOCX",     ["docx", "word doc"]),
    # 17) Excel/表格 显式
    ("📈 表格",          ["xlsx", "csv ", "excel", "spreadsheet"]),
    # 18) 前端实现
    ("🎨 前端实现",     ["frontend-design", "implement design", "ui code", "production-grade", "tailwind", "create distinctive frontend"]),
    # 19) 部署平台
    ("🚀 部署平台",     ["vercel", "netlify", "cloudflare", "render-deploy", "deploy a", "deploy to "]),
    # 20) 迁移/扩展(放在 MCP 前 — migrate-to-codex 不该归 MCP)
    ("🔄 迁移/扩展",    ["enable-1m", "migrate-to-codex", "1m context"]),
    # 21) Session 管理(my-compact / codex-sessions)
    ("💾 Session 管理", ["codex-sessions", "my-compact", "my-auto-compact", "session compact"]),
    # 22) MCP/插件
    ("🔌 MCP/插件",     ["build-mcp", " mcp ", "model context protocol", "chatgpt apps sdk", "chatgpt-apps"]),
    # 23) 安全
    ("🛡 安全",         ["security-", "threat model", "ownership", "sentry", "vulnerab", "scorecard"]),
    # 24) 写作/Essay
    ("✍ 写作",         ["essay", "blog writ", "writing-helper", "ai writing"]),
    # 25) 绘图/可视化
    ("📐 绘图",         ["drawio", "diagram", "flowchart", "uml-mcp"]),
    # 26) Jupyter/数据科学
    ("🐍 Jupyter",      ["jupyter", "notebook"]),
    # 27) SDK 文档
    ("📚 SDK 文档",     ["openai-docs", "claude-api", "anthropic api"]),
    # 28) issue tracker
    ("📋 issue 管理",   ["linear", "jira", "issue tracker"]),
    # 29) CLI 工具创建
    ("🖥 CLI 工具",     ["cli-creator", "command-line tool", "winui", "aspnet"]),
    # 30) 配置类
    ("🔧 配置",         ["compaction-prompt", "configure llm", "configure model"]),
    # 31) 目标/任务
    ("🎯 目标/计划",    ["define-goal", "set objective"]),
    # 32) Karpathy 行为指南
    ("📖 行为指南",     ["karpathy", "behavioral guideline"]),
    # 33) ChatGPT 历史导航(navigating-chatgpt-history)
    ("📜 ChatGPT 历史", ["navigating-chatgpt", "chatgpt history", "chatgpt conversation"]),
]


def _category_for_text(text: str) -> str:
    """关键词命中第一个 category。'其它' 兜底。"""
    if not text:
        return "📦 其它"
    t = text.lower()
    for cat, kws in _CATEGORY_RULES:
        for kw in kws:
            if kw in t:
                return cat
    return "📦 其它"


def categorize_skill(meta: dict) -> str:
    """根据 SKILL.md frontmatter(name + description) 推断分类。"""
    text = (meta.get("name", "") + " " + (meta.get("description") or "")).strip()
    return _category_for_text(text)


def _categories_cache_load() -> dict:
    p = Path(CATEGORIES_CACHE).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _categories_cache_save(d: dict):
    p = Path(CATEGORIES_CACHE).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def skill_category(name: str, meta: dict) -> str:
    """带缓存的分类查询。SKILL.md description 改后自动失效(用 sha 校验)。"""
    import hashlib
    cache = _categories_cache_load()
    sig = hashlib.md5(((meta.get("description") or "") + name).encode("utf-8")).hexdigest()[:12]
    entry = cache.get(name)
    if entry and entry.get("sig") == sig:
        return entry["category"]
    cat = categorize_skill(meta)
    cache[name] = {"sig": sig, "category": cat}
    _categories_cache_save(cache)
    return cat


# ----------------------------------------------------------------------------- 多轴排序 + 使用频次
def skill_specificity(meta: dict) -> int:
    """专用程度评分:越高越"窄"。spec §reference profile §57-72 排序模型借鉴。
    - 含 "Use when" / "Only use" 段 = 边界明确
    - 含 "Do not use" / "Prefer X" = 反例边界明确
    - description 含触发词列表(顿号/分号分隔多项)
    - name 包含连字符(子项,通常更专用)
    """
    desc = (meta.get("description") or "")
    name = meta.get("name", "")
    score = 0
    if re.search(r"(?i)use when|use this skill when|trigger when|only use", desc):
        score += 3
    if re.search(r"(?i)do not use|don't use|prefer .+ skill|use .+ instead", desc):
        score += 3
    if re.search(r"触发[:: ]", desc) or re.search(r"trigger[s]?[:: ]", desc, re.I):
        score += 2
    # 触发词数量(顿号/分号分隔)
    trig_m = re.search(r"(?:触发|trigger[s]?)[:: ]([^.。\n]+)", desc, re.I)
    if trig_m:
        n = len(re.split(r"[,、,;；]", trig_m.group(1)))
        score += min(n, 3)
    # name 越长越具体
    if "-" in name:
        score += min(name.count("-"), 3)
    # 名字短而泛的通常更通用,扣点
    if len(name) < 6 and "-" not in name:
        score -= 1
    return score


def _usage_load() -> dict:
    p = Path(USAGE_STATS_FILE).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _usage_save(d: dict):
    p = Path(USAGE_STATS_FILE).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def usage_bump(name: str):
    """detail/intro/install 调用时 +1,作为 list/suggest 的历史偏好信号。"""
    d = _usage_load()
    d[name] = (d.get(name) or 0) + 1
    _usage_save(d)


def usage_count(name: str) -> int:
    return (_usage_load().get(name) or 0)





def generate_catalog(out_path=None, brief=None) -> Path:
    """生成/更新 CATALOG.md:全部已装 skill 的目录。
    brief=True 时输出紧凑格式(英文名 + 一句中/英释义,用户 v9.1 默认);
    brief=False 输出完整原描述;brief=None 走 SKILLFORGE_BRIEF env(默认 True)。
    自动在所有 mutation 操作(install/uninstall/modify/consolidate/self-install)末尾被调用。
    """
    import datetime as _dt
    skills, shadowed = scan_local()
    custom, regular = [], []
    for s in skills:
        (custom if _is_customized(s.name) else regular).append(s)

    out = Path(out_path or CATALOG_FILE).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lang_env = os.environ.get("SKILLFORGE_LANG", "").lower()
    if lang_env in ("zh", "en"):
        lang = lang_env
    else:
        lang = detect_lang()

    # brief 默认走 env(未设时 True);显式参数最高优先
    if brief is None:
        brief_env = os.environ.get("SKILLFORGE_BRIEF", "1").lower()
        brief = brief_env not in ("0", "false", "no", "off")

    mode_tag_zh = "紧凑模式" if brief else "完整模式"
    mode_tag_en = "brief mode" if brief else "full mode"
    header = {
        "zh": ("# SkillForge 本地技能目录",
               f"> 自动生成于 {now} · 按 MECE 5+1 分类 · {mode_tag_zh} · **不要手工编辑**(每次 install/uninstall/modify 都会重写)",
               f"🟢 普通 **{len(regular)}** · 🟡 已定制 **{len(custom)}** · ⚪ 被遮蔽 **{len(shadowed)}** · 合计 **{len(skills)}**"),
        "en": ("# SkillForge Local Skill Catalog",
               f"> Auto-generated at {now} · MECE 5+1 classification · {mode_tag_en} · **Do not edit** (rewritten on each install/uninstall/modify)",
               f"🟢 Regular **{len(regular)}** · 🟡 Customized **{len(custom)}** · ⚪ Shadowed **{len(shadowed)}** · Total **{len(skills)}**"),
    }[lang]
    lines = [header[0], "", header[1], "", header[2], "", "---", ""]

    from collections import defaultdict
    by_mece = defaultdict(list)
    for s in regular:
        key = mece_category_cached(s.name, {"name": s.name, "description": s.description})
        by_mece[key].append(s)

    def _ver_short(name: str) -> str:
        ver_root = Path(SKILLFORGE_VERSIONS).expanduser() / name
        bits = []
        if (ver_root / "pristine").exists(): bits.append("🟢")
        if (ver_root / "previous").exists(): bits.append("🟡")
        bits.append("🔵")
        return "".join(bits)

    def _render_brief(s, idx, customized=False):
        marker = "✨ " if customized else ""
        return [f"- `{idx:>2}.` **{marker}{s.name}** <sub>{_ver_short(s.name)}</sub> — {brief_for(s.name, s.description, lang)}"]

    def _render_full(s, idx, customized=False):
        marker = "✨ " if customized else ""
        return [
            f"#### {idx}. {marker}{s.name}  <sub>{_ver_short(s.name)}</sub>",
            "",
            f"{s.description or '_(无描述)_'}",
            "",
        ]

    _render_one = _render_brief if brief else _render_full

    # 全局编号从 1 开始,顺序 = CATALOG.md 里出现的顺序;末尾写 .last_list.json,
    # 让 /skill-详情 <编号> 快捷指令能直接命中用户看到的位置。
    mapping = {}
    idx = 1

    mece_order = ["data_fetcher", "content_transformer", "multi_modal_generator",
                  "action_executor", "integration_utility", "native_infra"]
    for key in mece_order:
        members = by_mece.get(key, [])
        if not members:
            continue
        label = mece_label(key, lang)
        contract = mece_contract(key, lang)
        hint = mece_hint(key, lang)
        lines.append(f"## {label}  ({len(members)})")
        lines.append("")
        if lang == "zh":
            lines.append(f"> **数据契约**:{contract}  ·  **编排建议**:{hint}")
        else:
            lines.append(f"> **Data contract**: {contract}  ·  **Orchestration hint**: {hint}")
        lines.append("")
        members = _sort_by_priority(members)
        for s in members:
            lines.extend(_render_one(s, idx, customized=False))
            mapping[idx] = s.name
            idx += 1
        lines.append("")
        lines.append("---")
        lines.append("")

    if custom:
        title = "## 🟡 已定制(改过源码)" if lang == "zh" else "## 🟡 Customized (modified source)"
        lines.append(f"{title}  ({len(custom)})")
        lines.append("")
        for s in _sort_by_priority(custom):
            lines.extend(_render_one(s, idx, customized=True))
            mapping[idx] = s.name
            idx += 1
        lines.append("")
        lines.append("---")
        lines.append("")

    if shadowed:
        title = "## ⚪ 被遮蔽副本" if lang == "zh" else "## ⚪ Shadowed duplicates"
        lines.append(f"{title}  ({len(shadowed)})")
        lines.append("")
        for s in shadowed:
            lines.append(f"- ✕ **{s.name}** — `{s.path}`")
        lines.append("")

    # 尾注:告诉用户/后续 agent 编号从这里来
    tail_zh = f"\n> 💡 共 **{idx-1}** 项。用 `/skill-详情 <编号>` 或 `/skill-info <编号>` 直接看某项详情;编号与 CATALOG 显示顺序一致,30 天有效。"
    tail_en = f"\n> 💡 Total **{idx-1}** items. Use `/skill-info <n>` or `/skill-详情 <n>` to inspect any item; numbers match CATALOG order, valid for 30 days."
    lines.append(tail_zh if lang == "zh" else tail_en)

    out.write_text("\n".join(lines), encoding="utf-8")
    if mapping:
        try:
            save_last_list(mapping)
        except Exception:
            pass  # 不影响 catalog 写入
    return out


def _is_customized(name: str) -> bool:
    """根据 SKILL.md description 是否含 ✨ 标识判断是否被客制化过。"""
    p = Path(CANONICAL_HOME).expanduser() / name / "SKILL.md"
    if not p.exists():
        return False
    meta = parse_frontmatter(p) or {}
    return "✨" in (meta.get("description") or "")


def _sort_by_priority(skills: list) -> list:
    """同段内排序:specificity(专用度) > usage(使用频次) > name 字典序。"""
    return sorted(skills, key=lambda s: (
        -skill_specificity({"name": s.name, "description": s.description}),
        -usage_count(s.name),
        s.name.lower(),
    ))


def cmd_list(args):
    import textwrap as _tw
    skills, shadowed = scan_local()
    if not skills:
        print("(没有找到已安装的技能)")
        return

    # 分类:客制化的 vs 普通的
    custom, regular = [], []
    for s in skills:
        (custom if _is_customized(s.name) else regular).append(s)

    # 语言:CLI --lang 优先,auto 走自动检测
    raw = getattr(args, "lang", None) or "auto"
    lang = detect_lang() if raw == "auto" else raw

    # 给 regular 按 MECE 5+1 分组
    from collections import defaultdict
    by_mece = defaultdict(list)
    for s in regular:
        key = mece_category_cached(s.name, {"name": s.name, "description": s.description})
        by_mece[key].append(s)
    for key in by_mece:
        by_mece[key] = _sort_by_priority(by_mece[key])

    def _print_desc(desc: str, mode: str):
        if not desc:
            return
        if mode == "brief":
            print(f"        {desc[:120]}")
        elif mode == "tight":
            # 紧凑:取首句(去触发段),最多 80 字
            first = re.split(r"[。.]\s*", desc, maxsplit=1)[0]
            if "触发" in first:
                first = re.split(r"\s*触发", first, maxsplit=1)[0]
            short = (first.strip() or desc)[:80]
            print(f"        {short}")
        else:  # full
            wrapped = _tw.fill(desc, width=72,
                               initial_indent="        ",
                               subsequent_indent="        ")
            print(wrapped)

    desc_mode = "full" if args.full else ("brief" if args.brief else "tight")

    mapping = {}
    n = 1
    total_reg = len(regular)

    # 双语头部
    head_zh = f"🟢 已装技能  {total_reg} 个,按 MECE 5+1 分类"
    head_en = f"🟢 Installed skills  {total_reg}  ·  MECE 5+1 classification"
    print((head_zh if lang == "zh" else head_en) + "\n")

    # ---- 输出:分类分段 / --flat 字母序 ----
    if args.flat:
        for s in sorted(regular, key=lambda x: x.name.lower()):
            mapping[n] = s.name
            print(f"  [{n:>3}] {s.name}")
            _print_desc(s.description, desc_mode)
            n += 1
    else:
        # MECE 5 类固定顺序
        mece_order = ["data_fetcher", "content_transformer", "multi_modal_generator",
                      "action_executor", "integration_utility", "native_infra"]
        for key in mece_order:
            members = by_mece.get(key, [])
            if not members:
                continue
            label = mece_label(key, lang)
            if args.cat and args.cat.lower() not in label.lower() and args.cat.lower() not in key:
                continue
            contract = mece_contract(key, lang)
            print(f"━━━ {label}  ({len(members)}) ━━━")
            print(f"    ↳ {contract}")
            for s in members:
                mapping[n] = s.name
                print(f"  [{n:>3}] {s.name}")
                _print_desc(s.description, desc_mode)
                n += 1
            print()

    if custom:
        title_zh = "━━━ 🟡 已定制(改过源码) "
        title_en = "━━━ 🟡 Customized (modified source) "
        print(f"{title_zh if lang == 'zh' else title_en} ({len(custom)}) ━━━")
        for s in _sort_by_priority(custom):
            mapping[n] = s.name
            print(f"  [{n:>3}] ✨ {s.name}")
            if s.description:
                d = s.description.lstrip("✨").lstrip("[已定制]").lstrip().lstrip(":").lstrip()
                _print_desc(d, desc_mode)
            n += 1
        print()

    if shadowed:
        title_zh = "━━━ ⚪ 被遮蔽副本 "
        title_en = "━━━ ⚪ Shadowed duplicates "
        print(f"{title_zh if lang == 'zh' else title_en} ({len(shadowed)}) ━━━")
        for s in shadowed:
            print(f"  ✕ {s.name}   {s.path}")
        tip_zh = "  可 `skillforge consolidate` 合并到 SKILLFORGE_HOME 后用软链统一。"
        tip_en = "  Use `skillforge consolidate` to unify duplicates via symlinks to SKILLFORGE_HOME."
        print(tip_zh if lang == "zh" else tip_en)
        print()

    save_last_list(mapping)

    # 顺手刷新 CATALOG.md
    try:
        cat_path = generate_catalog()
        print(f"📜 完整目录已自动写到 {cat_path}")
    except Exception as e:
        print(f"[warn] 生成 CATALOG.md 失败: {e}", file=sys.stderr)
    print(f"💡 提示:")
    print(f"   `skillforge list --full` 完整描述折行  `--brief` 紧凑 88 字")
    print(f"   `skillforge list --cat 部署` 只看某类  `--flat` 字母序")
    print(f"   `skillforge detail <编号>` 看详情  `skillforge install <编号|owner/repo>` 装")

    if args.json:
        print("\n" + json.dumps({
            "regular_by_cat": {cat: [asdict(s) for s in members] for cat, members in by_cat.items()},
            "custom": [asdict(s) for s in custom],
            "shadowed": [asdict(s) for s in shadowed],
            "numbering": {str(k): v for k, v in mapping.items()},
        }, ensure_ascii=False, indent=2))


def cmd_which(args):
    query = " ".join(args.query)
    skills, _ = scan_local()
    matches = match_local(query, skills)
    if matches:
        print(f'✅ 你已经装了能满足「{query}」的技能:\n')
        for s, score in matches[:3]:
            print(f"  ● {s.name}   (匹配度 {score:.2f})")
            if s.description:
                print(f"      {s.description[:88]}")
            print(f"      用法: Codex 里输 ${s.name};Claude Code 描述需求即自动触发")
        return True
    print(f'❌ 本地没有能满足「{query}」的技能。')
    # 中文 query 在无 LLM 时关键词匹配几乎必败 — 主动提示
    has_cjk = any("一" <= ch <= "鿿" for ch in query)
    if has_cjk and not os.environ.get("ANTHROPIC_API_KEY"):
        print(f'   ⚠️ 中文 query 在无 ANTHROPIC_API_KEY 时只能走关键词匹配,但已装 skill 的')
        print(f'   description 都是英文 → 几乎必然 0 命中。两个选择:')
        print(f'     a) 换英文 query 再试一次,如 "{_cjk_hint_en(query)}"')
        print(f'     b) export ANTHROPIC_API_KEY 后跑,LLM 会做语义匹配')
    print(f'   或运行  skillforge find "{query}"  去 GitHub 找一个并安装。')
    return False


def _cjk_hint_en(cn_query: str) -> str:
    """非常粗糙的中文 query 提示用英文关键词,只为给 cmd_which 失败时的一个 a) 建议方向。"""
    hints = {"视频": "video", "短片": "short", "图片": "image", "文档": "doc",
             "翻译": "translate", "压缩": "compress", "去背景": "remove background",
             "OCR": "ocr", "写作": "writing", "作文": "essay", "代码": "code"}
    out = []
    for k, v in hints.items():
        if k in cn_query:
            out.append(v)
    return " ".join(out) if out else "<英文关键词>"


_MODIFY_SKIP_DIRS = {".git", ".skillforge", "node_modules", "__pycache__",
                     ".venv", "venv", "dist", "build", ".next", ".cache"}


def _collect_skill_files(skill_dir: Path, max_bytes: int = 200_000) -> dict:
    """递归读 skill 目录所有文本文件,返回 {相对路径: 内容}。
    跳过常见无关目录、跳过二进制、跳过 > max_bytes 的大文件。
    """
    out = {}
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in _MODIFY_SKIP_DIRS and not d.startswith(".")]
        for f in files:
            full = Path(root) / f
            try:
                if full.stat().st_size > max_bytes:
                    continue
                rel = full.relative_to(skill_dir).as_posix()
                # 二进制保护:utf-8 解码失败就跳
                try:
                    out[rel] = full.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
            except OSError:
                continue
    return out


def _llm_modify(files: dict, user_request: str):
    """喂源码 + 需求,LLM 返回 changes 列表。无 key 或解析失败 → None。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    # 截断:每个文件最多 4000 字,最多 30 个文件
    file_items = list(files.items())[:30]
    dump = "\n\n".join(f"=== {p} ===\n{c[:4000]}" for p, c in file_items)
    prompt = (
        "用户想这样改一个 agent skill 的源码:\n\n"
        f"需求: {user_request}\n\n"
        f"下面是这个 skill 目录里的源文件(超 4000 字符已截断,共 {len(file_items)} 个):\n\n"
        f"{dump}\n\n"
        "请按需求做**最小**修改。规则:\n"
        "- 只动需要改的文件\n"
        "- SKILL.md 必须保留,可以微调 description / 触发词\n"
        "- 不要删 LICENSE / README.md\n"
        "- new_content 是完整的新文件内容(modify/create 必填,delete 不需要)\n\n"
        "输出严格 JSON 数组,不要任何其他文字:\n"
        '[{"path": "相对路径", "action": "modify"|"create"|"delete", "new_content": "完整新内容"}, ...]'
    )
    text = _llm_call(prompt, max_tokens=8000)
    if not text:
        return None
    try:
        arr = json.loads(_strip_code_fence(text))
        if isinstance(arr, list):
            return arr
    except Exception as e:
        print(f"  [warn] LLM 返回 JSON 解析失败: {e}", file=sys.stderr)
    return None


def _diff_changes(changes: list, skill_dir: Path) -> str:
    """diff 文本(unified format)。create/delete 也展示。"""
    import difflib
    out = []
    for ch in changes:
        path = ch.get("path", "?")
        action = ch.get("action", "modify")
        full = skill_dir / path
        if action == "delete":
            out.append(f"\n--- a/{path}\n+++ /dev/null\n[删除整个文件]")
            continue
        new = ch.get("new_content") or ""
        old = ""
        is_new = not full.exists()
        if not is_new:
            try:
                old = full.read_text(encoding="utf-8")
            except Exception:
                pass
        diff = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=("/dev/null" if is_new else f"a/{path}"),
            tofile=f"b/{path}",
            lineterm="",
        ))
        if diff:
            out.append("\n".join(diff))
        else:
            out.append(f"--- a/{path}\n+++ b/{path}\n[内容无变化]")
    return "\n".join(out)


def _apply_changes(changes: list, skill_dir: Path):
    """写盘。假设 save_previous 已经在外面调过了。"""
    for ch in changes:
        path = ch.get("path", "")
        if not path:
            continue
        action = ch.get("action", "modify")
        full = skill_dir / path
        if action == "delete":
            if full.exists():
                full.unlink()
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(ch.get("new_content") or "", encoding="utf-8")


def _update_customization_meta(name: str, request: str):
    """SKILL.md frontmatter:description 加 ✨[已定制] 前缀(如未加),append # customization-<ts>: <请求摘要>。"""
    md = Path(CANONICAL_HOME).expanduser() / name / "SKILL.md"
    if not md.exists():
        return
    text = md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return
    end = text.find("\n---", 3)
    if end < 0:
        return
    front_body = text[3:end]
    rest = text[end + 4:]

    new_lines = []
    desc_done = False
    for line in front_body.splitlines():
        m = re.match(r"(\s*description\s*:\s*)(.*)", line)
        if m and not desc_done:
            prefix, value = m.group(1), m.group(2)
            stripped = value.strip().strip('"').strip("'")
            if "✨" not in stripped and "[已定制]" not in stripped:
                new_value = f"✨[已定制] {stripped}"
                new_lines.append(f"{prefix}{new_value}")
            else:
                new_lines.append(line)
            desc_done = True
        else:
            new_lines.append(line)

    ts = _now()
    summary = (request or "").replace("\n", " ").strip()[:80]
    new_lines.append(f"# customization-{ts}: {summary}")

    new_front = "\n".join(new_lines)
    md.write_text(f"---\n{new_front.lstrip(chr(10))}\n---{rest}", encoding="utf-8")


def cmd_modify(args):
    name = resolve_skill(args.target) if args.target else None
    if not name:
        print("❌ 需要指定 skill。用法:`skillforge modify <name|编号> \"需求\"`", file=sys.stderr)
        return
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    if not skill_dir.exists():
        print(f"❌ skill '{name}' 不存在 ({skill_dir})", file=sys.stderr)
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ /skill-修改 需要 ANTHROPIC_API_KEY(此功能依赖 LLM 改源码)", file=sys.stderr)
        return
    request = " ".join(args.request or []).strip()
    if not request:
        print("❌ 需要说明改什么:`skillforge modify <name> \"你要的改动\"`", file=sys.stderr)
        return

    print(f"📂 读 {skill_dir} 源码…")
    files = _collect_skill_files(skill_dir)
    if not files:
        print("❌ 这个 skill 目录里没有可读源文件", file=sys.stderr)
        return
    total_chars = sum(len(c) for c in files.values())
    print(f"   {len(files)} 个文件,共 {total_chars} 字符")

    print(f"🤖 LLM 出修改方案…")
    changes = _llm_modify(files, request)
    if not changes:
        print("❌ LLM 没出可用方案", file=sys.stderr)
        return

    print(f"\n=== 提议的修改 ({len(changes)} 个文件) ===")
    diff_text = _diff_changes(changes, skill_dir)
    SHOW = 6000
    print(diff_text[:SHOW])
    if len(diff_text) > SHOW:
        print(f"\n[diff 太长,只显示前 {SHOW} 字符;应用前请确认你信任 LLM 输出]")

    if not confirm(f"\n应用以上修改?(改前自动快照到 versions/{name}/previous/)", args.yes):
        print("已取消。")
        return

    print(f"💾 快照当前到 versions/{name}/previous/")
    save_previous(name, skill_dir)
    print(f"✏️  应用修改…")
    _apply_changes(changes, skill_dir)
    _update_customization_meta(name, request)
    print(f"\n✅ 改完。回滚用:`skillforge rollback {name}`")


def cmd_rollback(args):
    name = resolve_skill(args.target) if args.target else None
    if not name:
        print("用法: skillforge rollback <name|编号> [--pristine]", file=sys.stderr)
        return
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    if not skill_dir.exists():
        print(f"❌ skill '{name}' 不存在", file=sys.stderr)
        return

    pristine = version_dir(name, "pristine")
    previous = version_dir(name, "previous")
    print(f"💾 当前快照状态:")
    print(f"   pristine (github 原版):  {'✓' if pristine.exists() else '✗ 缺失'}")
    print(f"   previous (上一版):        {'✓' if previous.exists() else '✗ 缺失'}")

    if args.pristine:
        if not pristine.exists():
            print("❌ 没有 pristine,无法回到原版(这个 skill 可能不是 skillforge 装的)", file=sys.stderr)
            return
        if not confirm(f"回到 GitHub 原版?当前 current 会保存为 previous(可再回滚一次)", args.yes):
            print("已取消。")
            return
        rollback_to_pristine(name, skill_dir)
        print(f"✅ {name} 已回到 GitHub 原版。再回滚(无 --pristine)会回到刚才被存的 previous。")
    else:
        if not previous.exists():
            print("❌ 没有 previous(还没修改过)。要回到原版用:`skillforge rollback {name} --pristine`", file=sys.stderr)
            return
        if not confirm(f"swap 形式:current ↔ previous 互换?", args.yes):
            print("已取消。")
            return
        rollback_to_previous(name, skill_dir)
        print(f"✅ {name} swap 完成。再 rollback 一次回到原状。")

    # 如果回滚后 current 里 description 已不含 ✨,我们不主动 unmark(用户回到原版仍知道改过)
    # 如果想精确反映,需要 reparse + 改 frontmatter,这里 KISS。


def cmd_suggest(args):
    """/skill-建议:纯本地路由 — 输入自然语言,在已装 skill 里挑 Top 3 推荐(不去 GitHub)。
    输出 Markdown 表格,带"适合场景/不适合场景"两列(借鉴 Codex profile 排序模型)。
    """
    query = " ".join(args.query).strip()
    if not query:
        print("用法: skillforge suggest <自然语言需求>", file=sys.stderr)
        sys.exit(2)

    skills, _ = scan_local()
    if not skills:
        print("(本地没有任何已装 skill)")
        return

    # 1) 关键词匹配(阈值 0.15 — 必须真有 keyword 命中才进候选,
    #    避免 0-base 高-spec 的 skill 因 composite 高而抢前排)
    matches = match_local(query, skills, threshold=0.15)
    has_cjk = any("一" <= ch <= "鿿" for ch in query)

    # 2) 关键词 0 命中 → 退到 category 名字模糊匹配
    if not matches:
        ql = query.lower()
        cat_hits = []
        for s in skills:
            cat = skill_category(s.name, {"name": s.name, "description": s.description})
            if any(part in cat or part in s.name.lower() for part in ql.split() if len(part) >= 2):
                cat_hits.append((s, 0.3))  # 给个 base=0.3,刚过阈值
        if cat_hits:
            matches = cat_hits[:10]

    # 3) 合 specificity / usage 权重排
    scored = []
    for s, base in matches[:20]:
        meta = {"name": s.name, "description": s.description}
        spec = skill_specificity(meta) / 12
        usage = min(1.0, usage_count(s.name) / 5)
        composite = 0.55 * base + 0.30 * spec + 0.15 * usage
        scored.append((s, composite, base, spec, usage))
    scored.sort(key=lambda x: -x[1])
    top = scored[:3]

    # 真没命中:matches 完全空(关键词 0.15 阈值都没过 + category 也没匹配)
    if not top:
        print(f"\n🎯 「{query}」 → 本地没匹配的 skill。\n")
        if has_cjk and not os.environ.get("ANTHROPIC_API_KEY"):
            print("   ⚠️ 中文 query + 无 ANTHROPIC_API_KEY → 关键词法必败")
            print("   先试英文 query,或 export ANTHROPIC_API_KEY 后让 LLM 做语义匹配")
        print(f'   去 GitHub 找新的:`skillforge find "{query}"`')
        if not args.no_browse:
            print(f"\n   或浏览已装的分类:`skillforge list`(按 MECE 5+1 分类)")
        return

    print(f"\n🎯 「{query}」 → 本地 Top {len(top)} (没去 GitHub,只看已装)\n")

    # 4) 输出表格(MECE 分类 + 双语)
    lang = detect_lang(query)
    if lang == "zh":
        print("| Rank | Skill | MECE 类 | 推荐级别 | 匹配度 | 适合场景 | 不适合场景 |")
        print("|------|-------|---------|----------|--------|----------|------------|")
        level_map = {"强推": "强推", "推荐": "推荐", "谨慎": "谨慎", "参考": "参考"}
    else:
        print("| Rank | Skill | MECE class | Level | Score | Fits | Avoid |")
        print("|------|-------|------------|-------|-------|------|-------|")
        level_map = {"强推": "STRONG", "推荐": "RECOMMEND", "谨慎": "CAUTION", "参考": "REFERENCE"}
    for i, (s, composite, base, spec, usage) in enumerate(top, 1):
        mkey = mece_category_cached(s.name, {"name": s.name, "description": s.description})
        mtag = mece_label(mkey, lang)
        if composite >= 0.7: lvl_key = "强推"
        elif composite >= 0.5: lvl_key = "推荐"
        elif composite >= 0.3: lvl_key = "谨慎"
        else: lvl_key = "参考"
        level = level_map[lvl_key]
        suit = _extract_clause(s.description, [r"Use when ([^.。\n]+)", r"使用[场]?景[:: ]([^.。\n]+)"])
        not_suit = _extract_clause(s.description, [r"Do not use (?:for |when )([^.。\n]+)", r"don't use ([^.。\n]+)", r"不[适]?用[于:: ]([^.。\n]+)"])
        suit_str = suit or ("(看描述)" if lang == "zh" else "(see desc)")
        not_suit_str = not_suit or ("(无明确反例)" if lang == "zh" else "(no explicit counter-case)")
        print(f"| {i} | **{s.name}** ({mtag}) | {mtag} | {level} | base={base:.2f} spec={spec:.2f} usage={usage:.2f} → **{composite:.2f}** | {suit_str} | {not_suit_str} |")

    if lang == "zh":
        print(f"\n💡 用法:`skillforge detail {top[0][0].name}` 看详情 / `skillforge intro {top[0][0].name}` 看简介")
        print(f"   都不合适?去 GitHub 找:`skillforge find \"{query}\"`")
    else:
        print(f"\n💡 Usage: `skillforge detail {top[0][0].name}` for details / `skillforge intro {top[0][0].name}` for a summary")
        print(f"   None fits? Find new on GitHub: `skillforge find \"{query}\"`")


def _extract_clause(desc: str, patterns: list) -> str:
    """从 description 抠出符合 pattern 的子句(短句)。返回 None 如果都不命中。"""
    if not desc:
        return None
    for p in patterns:
        m = re.search(p, desc, re.IGNORECASE)
        if m:
            s = m.group(1).strip().rstrip(",.;:。、")
            return s[:80]  # 限长
    return None


def cmd_uninstall(args):
    name = resolve_skill(args.target) if args.target else None
    if not name:
        print("用法: skillforge uninstall <name|编号>", file=sys.stderr)
        return
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    if not skill_dir.exists():
        print(f"❌ skill '{name}' 不存在", file=sys.stderr)
        return

    targets_to_remove = []
    for d in register_target_dirs():
        p = Path(d).expanduser() / name
        if p.exists() or p.is_symlink():
            targets_to_remove.append(p)

    versions_root = Path(SKILLFORGE_VERSIONS).expanduser() / name
    has_versions = versions_root.exists()

    print(f"将卸载 {name}:")
    print(f"  删 agent 软链 {len(targets_to_remove)} 个:")
    for p in targets_to_remove:
        print(f"    - {p}")
    print(f"  移 {skill_dir} → backups/")
    if has_versions:
        print(f"  移 {versions_root} → backups/(含 pristine/previous)")

    if not confirm("\n确认卸载?", args.yes):
        print("已取消。")
        return

    import shutil
    # 1) rm agent 软链 / 目录
    for p in targets_to_remove:
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p)
            print(f"  🗑️  删 {p}")
        except OSError as e:
            print(f"  ⚠️ 删 {p} 失败: {e}", file=sys.stderr)

    # 2) skills/<name> 搬到 backups/
    try:
        bak = backup_skill_dir(skill_dir)
        print(f"  📦 skill 搬到 {bak}")
    except Exception as e:
        print(f"  ⚠️ 搬 skill 失败: {e}", file=sys.stderr)

    # 3) versions/<name> 搬到 backups/
    if has_versions:
        try:
            bak2 = Path(BACKUP_HOME).expanduser() / f"versions-{name}-{_now()}"
            bak2.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(versions_root), str(bak2))
            print(f"  📦 versions 搬到 {bak2}")
        except Exception as e:
            print(f"  ⚠️ 搬 versions 失败: {e}", file=sys.stderr)

    # 4) 清理编号缓存里的引用
    ll = load_last_list()
    new_ll = {k: v for k, v in ll.items() if v != name}
    if len(new_ll) != len(ll):
        save_last_list(new_ll)

    # v6: 自动刷新 CATALOG.md
    try:
        generate_catalog()
    except Exception:
        pass

    print(f"\n✅ {name} 已卸载。所有数据搬到 backups/,误删可恢复。")


def cmd_help(args):
    print("""
/skill 命令体系 — 自然语言驱动的 agent skill 管理

  skillforge which <需求>          查本地装没装过
  skillforge find  <需求>          LLM 流水线找 Top 3 (含 Scorecard + OSV 安全审)
  skillforge install <编号|owner/repo>  装一个,自动 intro
  skillforge list                  看已装(普通 / 已定制 / 被遮蔽 三段 + 编号)
  skillforge detail <编号|name>    看来源/安装命令/版本状态/定制历史
  skillforge intro <name>          一段中文使用说明
  skillforge modify <name> <需求>  LLM 改源码,自动快照,显 diff,确认应用
  skillforge rollback <name> [--pristine]  回上一版或回 github 原版
  skillforge uninstall <name>      删软链 + 搬 backups
  skillforge trust list|add|remove <owner...>   信任白名单
  skillforge consolidate [--dry-run]  合并同名物理副本到 SKILLFORGE_HOME
  skillforge self-install          装自身 SKILL.md + 9 个 slash 命令到所有 agent
  skillforge help                  本帮助

数据布局:
  ~/.skillforge/skills/           当前在用 (current)
  ~/.skillforge/versions/<n>/     pristine + previous(三槽位)
  ~/.skillforge/backups/          adoption/consolidate/uninstall 的备份
  ~/.skillforge/trusted.txt       owner 白名单
  ~/.skillforge/.last_list.json   /skill-详情 <编号> 引用缓存(30 天过期)

通常用法: 在 agent 里说 /skill-查找 你的需求 或直接描述,agent 会自动路由到对应命令。
""")


def detect_host() -> str:
    """启发式推断当前 agent host。"""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude-code"
    if os.environ.get("CODEX_CLI") or os.environ.get("CODEX") or os.environ.get("OPENAI_CODEX"):
        return "codex"
    if os.environ.get("OPENCLAW"):
        return "openclaw"
    return "unknown"


# (host_dir, commands_dir) — slash command 文件应该装到 commands_dir
_HOST_LAYOUT = [
    ("claude-code", Path("~/.claude/skills").expanduser(), Path("~/.claude/commands").expanduser()),
    ("codex",       Path("~/.codex/skills").expanduser(),  Path("~/.codex/commands").expanduser()),
    ("openclaw",    Path("~/.openclaw/skills").expanduser(), Path("~/.openclaw/commands").expanduser()),
]


def _skillforge_own_skill_md(skillforge_path: str) -> str:
    """skillforge 自己的 SKILL.md frontmatter description 写成"自然语言触发型"。"""
    return f"""---
name: skillforge
description: 跨 agent 技能闭环管理(查找/安装/列表/详情/修改/回滚/卸载/介绍/建议)。触发:用户说"找一个/装一个/查一下/改一改/卸载.../查看 ... 的 skill / 技能 / 工具",或输入以 `/skill-` 或 `-skill` 开头的文本(如 `-skill列表`、`-skill查找 xxx`)。
---

# skillforge

跨 agent 技能闭环管理工具,把 GitHub 仓库 → SKILL.md → 三家 agent 同步装上。**所有命令的入口是**:

```bash
python {skillforge_path} <subcommand> [...]
```

## 前置要求(v9.4 强制)

任何需要联网访问 GitHub 的子命令(`find` / `find-data` / `deep-data` / `install <owner/repo>`)前,agent 必须先做 **Token 检查**:

1. 看 `GITHUB_TOKEN` 环境变量,或 `gh auth token` 是否返回 token
2. 都无 → **明确向用户索取**(不要偷偷探测 credential store,Claude Code 沙箱会拦):

   ```
   🔐 联网访问 GitHub 需要 token(匿名配额 60/小时,一次 -skill查找 就打满)。
      请给我一个 PAT,或告诉我"用 gh token"、"走 -skill建议"。
   ```

3. 用户提供 PAT 后,只写进本次会话的 env,**不写盘、不 commit、不横传**

**不需要 token** 的路径:`list` / `catalog` / `intro` / `detail` / `suggest` / `rollback` / `uninstall`(纯本地操作)。

## 何时用本技能

当用户说类似这些话时调用本技能:
- "帮我找一个能 X 的技能/工具/skill"
- "我们装过能 X 的东西吗?"
- "把刚装的 X 的源码改一下"
- "看一下都装了哪些 skill"
- "把 X 卸载了"

## 怎么用

详见 `python {skillforge_path} help`(列出所有 subcommand)。**三种入口都能触发同一套流程**:

| slash command (原生) | ASCII 别名 | 文本前缀(v9.3.2) | 语义 |
|---|---|---|---|
| /skill-查找 <需求> | /skill-find | `-skill查找 <需求>` | 去 GitHub 找 Top 3 |
| /skill-建议 <需求> | /skill-suggest | `-skill建议 <需求>` | 从已装里 Top 3(不联网) |
| /skill-列表 | /skill-list | `-skill列表` | MECE 5+1 紧凑目录 |
| /skill-详情 <编号\|name> | /skill-info | `-skill详情 <编号>` | 看某项详情 |
| /skill-安装 <target> | /skill-install | `-skill安装 <target>` | 装一个 |
| /skill-修改 <name> <需求> | /skill-modify | `-skill修改 <name> <需求>` | 改源码 |
| /skill-回滚 <name> | /skill-rollback | `-skill回滚 <name>` | 回上一版 |
| /skill-卸载 <name> | /skill-uninstall | `-skill卸载 <name>` | 卸载 |
| /skill-介绍 <name> | /skill-intro | `-skill介绍 <name>` | 出一段简介 |
| /skill-帮助 | /skill-help | `-skill帮助` | 命令表 |

## 文本前缀路由(重要,v9.3.2 新)

用户输入**以 `-skill` 开头的整行文本**时(不是 slash command,是普通消息),你要主动识别并路由到对应流程:

- 匹配规则:行首 `-skill` + 中文动词(查找/列表/详情/安装/修改/回滚/卸载/介绍/建议/帮助) + 空格 + 剩余参数
- 触发后:直接执行对应 slash 模板(`~/.claude/commands/skill-<动词>.md` 或本仓库 `slash_templates/skill-<动词>.md`)的完整流程,**不要再回问一遍**
- 示例:
  - 用户敲 `-skill列表` → 走 /skill-列表 流程
  - 用户敲 `-skill查找 能写作文的工具` → 走 /skill-查找 "能写作文的工具" 流程
  - 用户敲 `-skill详情 31` → 走 /skill-详情 31 流程
- 兜底:开头是 `-skill` 但动词不在表里 → 当作 `-skill查找 <剩余>` 处理

**为什么有两套**:`/` 是 Claude Code / Codex / OpenClaw 硬编码的 slash 前缀,`-` 是用户偏好的更"不冲突"的敲法。两种都保留,用户想敲哪种就哪种。

## 关键设计

- **三维评分透明**:每个推荐附 R(相关性) / U(真实使用证据) / T(治理透明度) 三维分 + 风险标签
- **OpenSSF Scorecard + OSV 漏洞库**:接入 Google + OpenSSF 安全评分 + 已知 CVE 库
- **版本三槽位**:pristine (github 原版,永不变) + previous (上一版) + current (在用)
- **trusted 白名单**:owner 在 `~/.skillforge/trusted.txt` 里的会自动允许 `--install`(运行 pip/npm install)
- **adoption**:发现 agent 目录里已有手写精品 SKILL.md 会自动采用,不覆盖

## 安全默认

- star / clone / 安装命令 / 修改源码 / 卸载 都先 confirm,加 `--yes` 才跳过
- 安装命令默认不跑,owner 进 trusted.txt 或加 --install 才跑
- 修改源码前自动快照到 versions/<name>/previous/
- 卸载时数据搬到 backups/ 不删
"""


def cmd_self_install(args):
    """检测各 agent 目录,装 skillforge 自身的 SKILL.md + 9 个 slash command 文件。"""
    script_path = Path(__file__).resolve()
    print(f"📍 skillforge 在: {script_path}")
    print(f"🔍 检测当前 host(env 启发式): {detect_host()}")

    # 1) 装 skillforge 自己的 SKILL.md 到 SKILLFORGE_HOME
    own_dir = Path(CANONICAL_HOME).expanduser() / "skillforge"
    own_dir.mkdir(parents=True, exist_ok=True)
    own_md = own_dir / "SKILL.md"
    own_md.write_text(_skillforge_own_skill_md(str(script_path)), encoding="utf-8")
    print(f"📝 写自身 SKILL.md: {own_md}")
    # 保存 pristine
    try:
        save_pristine("skillforge", own_dir)
    except Exception:
        pass

    # 2) 软链到各家 agent skills/
    skills_link_results = register_skill(own_dir, link=not args.copy)
    for tgt, how in skills_link_results:
        print(f"🔗 SKILL.md 注册: {tgt}  ({how})")

    # 3) 拷 slash 模板到各家 commands/。中文文件名同时部署一份 ASCII 别名,
    #    确保 Claude Code 自动补全下拉框能搜到(中文名 autocomplete 不友好)
    templates_dir = Path(__file__).resolve().parent / "slash_templates"
    if not templates_dir.is_dir():
        print(f"❌ slash 模板目录不存在: {templates_dir}", file=sys.stderr)
        return
    templates = sorted(templates_dir.glob("*.md"))
    # 中文 → ASCII 映射
    ASCII_ALIAS = {
        "skill-查找.md": "skill-find.md",
        "skill-列表.md": "skill-list.md",
        "skill-详情.md": "skill-info.md",
        "skill-安装.md": "skill-install.md",
        "skill-修改.md": "skill-modify.md",
        "skill-回滚.md": "skill-rollback.md",
        "skill-卸载.md": "skill-uninstall.md",
        "skill-介绍.md": "skill-intro.md",
        "skill-帮助.md": "skill-help.md",
        "skill-建议.md": "skill-suggest.md",
    }
    print(f"\n📂 准备 {len(templates)} 个 slash 模板 + 各自的 ASCII 别名:")
    for t in templates:
        alias = ASCII_ALIAS.get(t.name)
        if alias:
            print(f"   · {t.name}  +  {alias}")
        else:
            print(f"   · {t.name}")

    print(f"\n📤 部署到各 host 的 commands 目录(中文 + ASCII 别名都装,内容一样):")
    deployed = 0
    for host, skills_dir, commands_dir in _HOST_LAYOUT:
        if not skills_dir.exists():
            continue
        commands_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for t in templates:
            content = t.read_text(encoding="utf-8").replace("{SKILLFORGE_PATH}", str(script_path))
            for name in [t.name, ASCII_ALIAS.get(t.name)]:
                if not name:
                    continue
                dest = commands_dir / name
                try:
                    dest.write_text(content, encoding="utf-8")
                    deployed += 1
                    count += 1
                except OSError as e:
                    print(f"   ⚠️ {dest}: {e}", file=sys.stderr)
        print(f"   ✓ {host}: {count} 个 → {commands_dir}")

    # v6: 自动刷新 CATALOG.md(把 skillforge 自身也算进去)
    try:
        generate_catalog()
    except Exception:
        pass

    print(f"\n✅ 完成。共部署 {deployed} 个 slash 文件(每家中文+ASCII 双份)。")
    print(f"   在 agent 输 / 时,中文型 (/skill-帮助) 和 ASCII 型 (/skill-help) 都能用;")
    print(f"   ASCII 那套能被自动补全下拉框搜到,中文那套适合直接敲。")
    print(f"   ⚠ 裸 /skill 已废弃(易与 agent 内置命令冲突),改用 /skill-<X>。")


# ----------------------------------------------------------------------------- v4: agent-as-LLM API
# 5 个"无脑"子命令,只做数据采集 / 渲染 / 应用,所有 LLM 推理由调用 agent 完成。
# 流水线: agent 改写 query → find-data → agent 粗排 → deep-data → agent 终排 → render
# 修改:  modify-source → agent 写 changes → modify-apply


def cmd_find_data(args):
    """find-data <q1> [q2] [q3]: 多搜+元数据+T/U → JSON 到 stdout (进度去 stderr)。"""
    token = os.environ.get("GITHUB_TOKEN")
    queries = args.queries
    print(f"🔎 {len(queries)} 个 query 三角度搜", file=sys.stderr)
    seen, candidates = set(), []
    for q in queries:
        try:
            results = github_search(q, token, top=max(args.top * 2, 6))
        except GHError as e:
            print(f"   query '{q}' 搜索失败: {e}", file=sys.stderr)
            continue
        for c in results:
            if c["full_name"] in seen:
                continue
            seen.add(c["full_name"])
            candidates.append(c)
    print(f"   合并去重得 {len(candidates)} 候选", file=sys.stderr)
    if not candidates:
        print("[]")
        return

    print(f"   元数据体检中...", file=sys.stderr)
    out = []
    for c in candidates:
        try:
            m = fetch_metadata(c["full_name"], token)
        except GHError as e:
            print(f"   skip {c['full_name']}: {e}", file=sys.stderr)
            continue
        m["U"] = compute_u_score(
            stars=m.get("stargazers_count", 0),
            watchers=m.get("subscribers_count", 0),
            forks=m.get("forks_count", 0),
            downloads=None,
            release_count=m.get("release_count", 0),
            close_rate=None,
        )
        out.append({
            "full_name": m["full_name"],
            "description": m.get("description", "") or "",
            "language": m.get("language", "") or "",
            "stargazers_count": m.get("stargazers_count", 0),
            "subscribers_count": m.get("subscribers_count", 0),
            "forks_count": m.get("forks_count", 0),
            "default_branch": m.get("default_branch", "main"),
            "release_count": m.get("release_count", 0),
            "contributors_count": m.get("contributors_count", 0),
            "U": m["U"], "T": m["T"],
            "risk_flags": m.get("risk_flags", []),
            "clone_url": m.get("clone_url", ""),
            "html_url": m.get("html_url", ""),
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_deep_data(args):
    """deep-data <name...>: 抓 README + Scorecard + OSV + 下载量 + close_rate → JSON to stdout。"""
    token = os.environ.get("GITHUB_TOKEN")
    out = []
    for fn in args.names:
        print(f"🔍 deep {fn}", file=sys.stderr)
        try:
            m = fetch_metadata(fn, token)
        except GHError as e:
            print(f"   skip {fn}: {e}", file=sys.stderr)
            continue
        m["readme_excerpt"] = fetch_readme(fn, token)[:4000]
        m["close_rate"] = fetch_close_rate(fn, token)
        pkg = guess_package_name(fn, m.get("default_branch", "main"), m.get("language", ""))
        m["monthly_downloads"] = fetch_downloads(pkg["ecosystem"], pkg["name"]) if pkg else None
        m["scorecard"] = fetch_scorecard(fn)
        m["osv_vulns"] = fetch_osv_vulns(pkg["ecosystem"], pkg["name"]) if pkg else []
        m["T"] = compute_t_score(m, scorecard=m["scorecard"], osv_vulns=m["osv_vulns"])
        m["risk_flags"] = compute_risk_flags(m, scorecard=m["scorecard"], osv_vulns=m["osv_vulns"])
        m["U"] = compute_u_score(
            stars=m.get("stargazers_count", 0),
            watchers=m.get("subscribers_count", 0),
            forks=m.get("forks_count", 0),
            downloads=m["monthly_downloads"],
            release_count=m.get("release_count", 0),
            close_rate=m["close_rate"],
        )
        m["install_cmds"] = _guess_install(m.get("language", ""))
        sc_failed = []
        if m["scorecard"]:
            sc_failed = [c.get("name") for c in (m["scorecard"].get("checks") or [])
                         if (c.get("score") or 0) < 5][:8]
        out.append({
            "full_name": m["full_name"],
            "description": m.get("description", "") or "",
            "language": m.get("language", "") or "",
            "stargazers_count": m.get("stargazers_count", 0),
            "subscribers_count": m.get("subscribers_count", 0),
            "forks_count": m.get("forks_count", 0),
            "default_branch": m.get("default_branch", "main"),
            "release_count": m.get("release_count", 0),
            "close_rate": m["close_rate"],
            "monthly_downloads": m["monthly_downloads"],
            # v9.5: T 分明细需要下列原始字段
            "license": m.get("license"),
            "pushed_at": m.get("pushed_at", ""),
            "created_at": m.get("created_at", ""),
            "contributors_count": m.get("contributors_count", 0),
            "has_issues": m.get("has_issues"),
            "owner": m.get("owner") or {},
            "topics": m.get("topics") or [],
            "archived": m.get("archived", False),
            "scorecard": m["scorecard"],
            "scorecard_score": m["scorecard"].get("score") if m["scorecard"] else None,
            "scorecard_failed_checks": sc_failed,
            "osv_vulns": m["osv_vulns"],
            "U": m["U"], "T": m["T"],
            "risk_flags": m["risk_flags"],
            "install_cmds": m["install_cmds"],
            "readme_excerpt": m["readme_excerpt"],
            "clone_url": m.get("clone_url", ""),
            "html_url": m.get("html_url", ""),
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_render(args):
    """render --file <ranking.json>: 读 agent 的 ranking,渲染 Top 3。
    格式: {query: str, ranked: [...], meta_by_name: {full_name: {full meta dict}}}
    """
    path = Path(args.file)
    if not path.exists():
        print(f"❌ 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ JSON 解析失败: {e}", file=sys.stderr)
        sys.exit(1)
    query = data.get("query", "(未传)")
    ranked = data.get("ranked", [])
    meta_by_name = data.get("meta_by_name", {})
    trusted = load_trusted()
    print(render_top3(query, ranked, meta_by_name, trusted))


def cmd_modify_source(args):
    """modify-source <name|编号>: dump skill 所有源文件 → JSON to stdout。"""
    name = resolve_skill(args.target)
    if not name:
        print(f"❌ 找不到 '{args.target}'", file=sys.stderr)
        sys.exit(1)
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    if not skill_dir.exists():
        print(f"❌ skill 不存在: {skill_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"📂 读 {skill_dir} 源码", file=sys.stderr)
    files = _collect_skill_files(skill_dir)
    print(f"   {len(files)} 文件, {sum(len(c) for c in files.values())} 字符", file=sys.stderr)
    print(json.dumps({
        "name": name,
        "skill_dir": str(skill_dir),
        "files": files,
    }, ensure_ascii=False, indent=2))


def cmd_modify_apply(args):
    """modify-apply <name|编号> --file <changes.json>: 应用 agent 出的 changes。
    changes 格式: [{path, action: modify|create|delete, new_content}, ...]
    """
    name = resolve_skill(args.target)
    if not name:
        print(f"❌ 找不到 '{args.target}'", file=sys.stderr)
        sys.exit(1)
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    if not skill_dir.exists():
        print(f"❌ skill 不存在: {skill_dir}", file=sys.stderr)
        sys.exit(1)
    cfile = Path(args.file)
    if not cfile.exists():
        print(f"❌ changes 文件不存在: {cfile}", file=sys.stderr)
        sys.exit(1)
    try:
        changes = json.loads(cfile.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ changes JSON 解析失败: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(changes, list):
        print("❌ changes 必须是 JSON array", file=sys.stderr)
        sys.exit(1)

    request = args.summary or "(via modify-apply, agent-driven)"

    print(f"\n=== 提议的修改 ({len(changes)} 文件) ===")
    diff_text = _diff_changes(changes, skill_dir)
    SHOW = 6000
    print(diff_text[:SHOW])
    if len(diff_text) > SHOW:
        print(f"\n[diff 太长,只显示前 {SHOW} 字符]")

    if not confirm(f"\n应用以上修改?(改前自动快照到 versions/{name}/previous/)", args.yes):
        print("已取消。")
        return

    print(f"💾 快照 → versions/{name}/previous/")
    save_previous(name, skill_dir)
    _apply_changes(changes, skill_dir)
    _update_customization_meta(name, request)
    # v6: 自动刷新 CATALOG.md(✨[已定制] 标记会反映)
    try:
        generate_catalog()
    except Exception:
        pass

    print(f"\n✅ 改完。回滚: skillforge rollback {name}")


def cmd_install(args):
    """/skill-安装:把 target 装下来。target 可以是:
    - "owner/repo" → 先验仓库存在,再 find --repo --yes
    - "1" → 上次 list 的编号(数字)
    - 已装的 name → 提示"已装,看详情用 detail"
    - 不存在的 name → 明确报错,不要瞎跑安装流程
    """
    target = args.target
    if not target:
        print("用法: skillforge install <owner/repo|编号|name>", file=sys.stderr)
        sys.exit(2)

    if "/" in target:
        # owner/repo 直装路径:先验证仓库存在,避免双重错误污染
        token = os.environ.get("GITHUB_TOKEN")
        try:
            gh_request(f"/repos/{target}", token)
        except GHError as e:
            print(f"❌ install 失败:{e}", file=sys.stderr)
            return
        # 验证通过,走完整 install 流程
        from argparse import Namespace
        find_args = Namespace(
            query=[target.split("/")[-1]],
            repo=target,
            top=3, yes=True, force_new=True,
            no_star=args.no_star, install=args.install,
            no_register=False, copy=False,
            simple=True, no_readme=True,  # simple 路径避免 LLM 再跑
        )
        cmd_find(find_args)
        # 装完自动 intro(_install_chosen 末尾已经调过,这里再调一次会重复;_install_chosen 已有,此处不再额外调)
        return

    # 数字编号路径:必须真是数字才走 resolve
    if target.isdigit():
        resolved = resolve_skill(target)
        if not resolved:
            print(f"❌ 编号 {target} 越界或缓存过期。先 `skillforge list` 看现有编号。", file=sys.stderr)
            return
        skill_dir = Path(CANONICAL_HOME).expanduser() / resolved
        if skill_dir.exists():
            print(f"ℹ️  编号 {target} ({resolved}) 已经装过了。看详情用:`skillforge detail {target}`")
            return
        # 编号映射到了不存在的目录(理论上不该发生,除非 last_list.json 过期且 skill 已卸载)
        print(f"❌ 编号 {target} 映射到 '{resolved}' 但实际目录不存在 — last_list.json 缓存过期。", file=sys.stderr)
        print(f"   重跑 `skillforge list` 刷新编号缓存。", file=sys.stderr)
        return

    # 当作 name 处理(非数字、非 owner/repo)
    skill_dir = Path(CANONICAL_HOME).expanduser() / target
    if skill_dir.exists():
        print(f"ℹ️  '{target}' 已经装过了。看详情用:`skillforge detail {target}`")
        return
    print(f"❌ '{target}' 既不是已装的 skill 名,也不是数字编号,也不是 owner/repo 格式。", file=sys.stderr)
    print(f"   要装 GitHub 仓库请用完整路径(如 `octocat/Hello-World`);", file=sys.stderr)
    print(f"   要查已装请 `skillforge list`。", file=sys.stderr)
    """看一个 skill 的详情。args.target 可以是名字或编号。"""
    name = resolve_skill(args.target)
    if not name:
        print(f"❌ 找不到 '{args.target}'。先 `skillforge list` 看编号,或直接给名字。", file=sys.stderr)
        return
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    md = skill_dir / "SKILL.md"
    if not md.exists():
        print(f"❌ skill 不存在:{skill_dir}", file=sys.stderr)
        return
    meta = parse_frontmatter(md) or {"name": name, "description": ""}

    # 找 source URL
    body = md.read_text(encoding="utf-8", errors="replace")
    source_m = re.search(r"https://github\.com/[\w.\-]+/[\w.\-]+", body)
    source_url = source_m.group(0) if source_m else "(SKILL.md 里没记录)"

    # 找安装命令
    inst_m = re.search(r"```(?:bash|shell)?\s*\n(.+?)\n```", body, re.DOTALL)
    install_cmd = inst_m.group(1).strip() if inst_m else "(SKILL.md 里没记录)"

    # 三槽位是否存在
    has_pristine = version_dir(name, "pristine").exists()
    has_previous = version_dir(name, "previous").exists()

    # 客制化历史
    custom_history = meta.get("customizations") or "(无)"
    # frontmatter 的 customizations 字段如果是 list,parse_frontmatter 当前只拿 string,这里简化
    custom_marker = "✨ 已定制过" if "✨" in (meta.get("description") or "") else "(未定制)"

    print(f"━━━ {name} ━━━\n")
    print(f"📂 当前位置: {skill_dir}")
    print(f"🌐 来源: {source_url}")
    print(f"📥 安装命令: {install_cmd}")
    print(f"🏷  定制状态: {custom_marker}")
    print(f"💾 版本快照:")
    print(f"     pristine (github 原版):  {'✓ 存在' if has_pristine else '✗ 缺失(可能不是 skillforge 装的)'}")
    print(f"     previous (上次修改前):    {'✓ 存在(可 rollback)' if has_previous else '✗ 还没修改过'}")
    print(f"     current (在用):           ✓")
    print(f"\n📝 描述:\n{meta.get('description','(无)')}\n")
    print(f"调用例子: 跟 agent 说\"用 {name} 帮我 ...\" 自动触发,或用 /skill-介绍 {name} 看简介。")


def _specificity_label(meta: dict) -> str:
    """根据 specificity 给 skill 贴标签:专用 / 通用 / 极泛。"""
    score = skill_specificity(meta)
    if score >= 8: return "🎯 专用 (边界明确)"
    if score >= 4: return "⚙️ 中等专用"
    return "📦 通用 (适用面广,触发不强)"


def _template_intro(meta: dict, body: str) -> str:
    """无 LLM 的模板版 intro。提取 description + 第一个 install 命令 + 触发词。
    手写 / 英文 description 没有标准"触发:" / ```bash 块时,改用 description 前 200 字+
    "看 SKILL.md 学具体调用法" 这种朴实文案,不再瞎编。
    """
    desc = (meta.get("description") or "").strip()
    name = meta.get("name", "?")

    # 抠 install:任何 ```bash/shell/sh 代码块,或裸的 pip/npm/cargo 行
    install = None
    m = re.search(r"```(?:bash|shell|sh)?\s*\n(.+?)\n```", body, re.DOTALL)
    if m:
        install = m.group(1).strip().splitlines()[0]
    else:
        m2 = re.search(r"(?im)^\s*(pip install [^\n]+|npm install[^\n]*|cargo (?:build|install)[^\n]*|go install[^\n]+)", body)
        if m2:
            install = m2.group(1).strip()

    # 抠触发词:优先中文 "触发:..." 段;再尝试 description 第一句关键名词
    trig_m = re.search(r"触发[:：]([^;；。]+)", desc)
    if trig_m:
        triggers = trig_m.group(1).strip()
    else:
        # 退化:用 skill name 自身作为触发词("跟 agent 说 \"用 X 帮我...\"")
        triggers = None

    # 简短描述:取首句(中文 "。"或英文 ".")
    first_sent = re.split(r"[。.]\s*", desc, maxsplit=1)[0]
    if "触发" in first_sent:
        first_sent = re.split(r"\s*触发", first_sent, maxsplit=1)[0]
    desc_short = (first_sent.strip() or desc[:160]).rstrip("。.") + "。"

    cat = skill_category(name, meta)
    spec_label = _specificity_label(meta)
    not_for = _extract_clause(desc, [r"Do not use (?:for |when )([^.。\n]+)",
                                      r"don't use ([^.。\n]+)",
                                      r"不[适]?用[于:: ]([^.。\n]+)",
                                      r"Prefer .+ skill .* for ([^.。\n]+)"])

    lines = [f"✨ **{name}** 装好了  ·  {cat}  ·  {spec_label}", ""]
    lines.append(f"**做什么**:{desc_short}")
    lines.append("")
    if triggers:
        lines.append(f"**怎么触发**:跟 agent 说\"{triggers}\"相关的话")
    else:
        lines.append(f"**怎么触发**:跟 agent 说\"用 {name} 帮我 ...\",或描述任何 {name} 能解决的具体场景")
    lines.append("")
    if not_for:
        lines.append(f"**何时别用**:{not_for[:120]}")
        lines.append("")
    if install:
        lines.append(f"**装法**(已经替你装好了,这是参考):`{install}`")
    else:
        lines.append(f"**装法**:已经替你装好,无需额外步骤(skillforge 已注册到所有 agent 目录)")
    return "\n".join(lines)


def _llm_intro(meta: dict, body: str) -> str:
    """有 ANTHROPIC_API_KEY 时让模型把 intro 改成口语化。"""
    text = _llm_call(
        f"用一段 80-150 字的中文口语向用户介绍下面这个刚装好的 agent 技能,告诉他:\n"
        f"1) 它能帮你做什么(一句话)\n"
        f"2) 你说什么样的话会自动触发它\n"
        f"3) 一个最常用的调用例子\n\n"
        f"技能 frontmatter:\nname: {meta.get('name','')}\ndescription: {meta.get('description','')}\n\n"
        f"SKILL.md 正文摘录(前 1500 字):\n{(body or '')[:1500]}\n\n"
        f"只输出介绍正文,开头用 ✨ 作图标,不要其他元 markdown 头。",
        max_tokens=500,
    )
    return text


def cmd_detail(args):
    """看一个 skill 的详情。args.target 可以是名字或编号。"""
    name = resolve_skill(args.target)
    if not name:
        print(f"❌ 找不到 '{args.target}'。先 `skillforge list` 看编号,或直接给名字。", file=sys.stderr)
        return
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    md = skill_dir / "SKILL.md"
    if not md.exists():
        print(f"❌ skill 不存在: {skill_dir}", file=sys.stderr)
        return
    meta = parse_frontmatter(md) or {"name": name, "description": ""}

    body = md.read_text(encoding="utf-8", errors="replace")
    desc_text = meta.get("description") or ""

    # 来源:GitHub URL 优先;退到 description / body 里搜 "owner/repo" 模式
    source_m = re.search(r"https://github\.com/[\w.\-]+/[\w.\-]+", body + " " + desc_text)
    if source_m:
        source_url = source_m.group(0)
    else:
        # 再退:看 "仓库 owner/repo" 或 "底层项目: owner/repo"
        m2 = re.search(r"(?:仓库|底层项目|repo|repository)[: :]+([\w.\-]+/[\w.\-]+)", desc_text + " " + body, re.IGNORECASE)
        source_url = f"https://github.com/{m2.group(1)}" if m2 else "(SKILL.md 没记 source URL — 可能是手写或 adopted 版)"

    # 安装命令:```bash/shell 块优先;退到任何 pip/npm/cargo 行
    inst_m = re.search(r"```(?:bash|shell|sh)?\s*\n(.+?)\n```", body, re.DOTALL)
    if inst_m:
        install_cmd = inst_m.group(1).strip().splitlines()[0]
    else:
        m3 = re.search(r"(?im)^\s*(pip install [^\n]+|npm install[^\n]*|cargo (?:build|install)[^\n]+|go install[^\n]+)", body)
        install_cmd = m3.group(1).strip() if m3 else "(没有标准安装命令 — 看 SKILL.md 正文)"

    has_pristine = version_dir(name, "pristine").exists()
    has_previous = version_dir(name, "previous").exists()
    is_custom = "✨" in desc_text

    # pristine 缺失时,根据 description 长度 + .skillforge/ 痕迹,推测是 adopted / 手装 / 还是 skillforge install
    pristine_note = ""
    if not has_pristine:
        if len(desc_text) > 200:
            pristine_note = "  (大概率是 adopted 手写版或外部手装,不是 skillforge install)"
        else:
            pristine_note = "  (可能是 v3 之前装的,rollback --pristine 不可用)"

    print(f"━━━ {name} ━━━\n")
    print(f"📂 当前位置: {skill_dir}")
    print(f"🌐 来源: {source_url}")
    print(f"📥 安装命令: {install_cmd}")
    print(f"🏷  定制状态: {'✨ 已定制过' if is_custom else '(未定制)'}")
    print(f"💾 版本快照:")
    print(f"     pristine (github 原版): {'✓ 存在(可 rollback --pristine)' if has_pristine else '✗ 缺失' + pristine_note}")
    print(f"     previous (上次修改前):   {'✓ 存在(可 rollback)' if has_previous else '✗ 还没修改过'}")
    print(f"     current (在用):          ✓")
    print(f"\n📝 描述:\n{desc_text or '(无)'}\n")
    print(f"调用例子: 跟 agent 说\"用 {name} ...\" 即可触发,或 `skillforge intro {name}` 看简介。")
    usage_bump(name)  # v8: 记录用户对这个 skill 的关注度


def cmd_intro(args):
    name = args.name
    skill_dir = Path(CANONICAL_HOME).expanduser() / name
    md = skill_dir / "SKILL.md"
    if not md.exists():
        print(f"❌ 找不到 {md}", file=sys.stderr)
        return
    meta = parse_frontmatter(md) or {"name": name, "description": ""}
    body = md.read_text(encoding="utf-8", errors="replace")
    # 把 frontmatter 去掉
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end > 0:
            body = body[end + 4 :].lstrip()
    text = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        text = _llm_intro(meta, body)
    if not text:
        text = _template_intro(meta, body)
    print(text)
    usage_bump(name)  # v8: 关注度+1


def cmd_consolidate(args):
    """把所有同名物理副本合并到 SKILLFORGE_HOME,其它位置改为软链。
    挑权威版:已在 SKILLFORGE_HOME 的优先,否则 description 最长的。"""
    import shutil
    raw, _ = scan_local(name_dedup=False)
    from collections import defaultdict
    groups = defaultdict(list)
    for s in raw:
        groups[s.name].append(s)
    home = Path(CANONICAL_HOME).expanduser()

    plan = []  # (name, canonical_skill, canonical_dir, others=[(skill,dir)])
    for name, group in groups.items():
        physical = []
        for s in group:
            sd = Path(s.path).parent
            # 已经是软链的目录不要碰
            if sd.is_symlink() or Path(s.path).is_symlink():
                continue
            physical.append((s, sd))
        if len(physical) <= 1:
            continue
        # 已经在 SKILLFORGE_HOME 的优先;否则 description 最长
        canon = next(((s, sd) for s, sd in physical
                      if str(Path(s.source).expanduser()) == str(home)), None)
        if not canon:
            canon = max(physical, key=lambda x: len(x[0].description or ""))
        others = [x for x in physical if x is not canon]
        plan.append((name, canon[0], canon[1], others))

    if not plan:
        print("没有需要合并的同名物理副本。")
        return

    print(f"找到 {len(plan)} 组同名物理副本{'(预览,未执行)' if args.dry_run else ''}:\n")
    for name, c_skill, c_dir, others in plan:
        in_home = str(Path(c_skill.source).expanduser()) == str(home)
        tag = "(已在 SKILLFORGE_HOME)" if in_home else "(将迁移到 SKILLFORGE_HOME)"
        print(f"  ◆ {name}  desc {len(c_skill.description or '')} 字符")
        print(f"      权威版: {c_dir}  {tag}")
        for s, sd in others:
            print(f"      替换为软链: {sd}  (原目录改 .bak.<ts>)")

    if args.dry_run:
        print("\n(--dry-run。去掉 --dry-run 并加 --yes 才执行。)")
        return

    if not confirm("\n执行以上合并?", args.yes):
        print("已取消。")
        return

    done, fail = 0, 0
    for name, c_skill, c_dir, others in plan:
        try:
            home_dir = home / name
            home.mkdir(parents=True, exist_ok=True)
            # 1) 把权威版搬到 SKILLFORGE_HOME(如果不在)
            if c_dir.resolve() != home_dir.resolve():
                if home_dir.exists():
                    print(f"  ⚠️ {name}: SKILLFORGE_HOME/{name} 已存在但与当前权威版不同,跳过")
                    fail += 1
                    continue
                shutil.copytree(c_dir, home_dir, symlinks=True)
                bak = backup_skill_dir(c_dir)
                c_dir.symlink_to(home_dir, target_is_directory=True)
                print(f"  ✅ {name}: 权威版迁移到 {home_dir} (备份 {bak})")
            # 2) 其余位置:整目录搬到 BACKUP_HOME,原位置新建软链
            for s, sd in others:
                bak = backup_skill_dir(sd)
                sd.symlink_to(home_dir, target_is_directory=True)
                print(f"  🔗 {sd}  →  软链 (备份 {bak})")
            done += 1
        except OSError as e:
            print(f"  ❌ {name}: {e}")
            fail += 1

    # v6: 自动刷新 CATALOG.md
    try:
        generate_catalog()
    except Exception:
        pass

    print(f"\n完成 {done}/{len(plan)} 组(失败 {fail})。所有被替换的原目录已搬到 {Path(BACKUP_HOME).expanduser()}。")


def cmd_trust(args):
    trusted = load_trusted()
    if args.action == "list":
        if not trusted:
            print(f"(空。在 {TRUSTED_FILE} 加 owner 或 owner/repo,一行一条)")
            return
        for t in sorted(trusted):
            print(t)
        return
    items = {x.lower() for x in (args.items or [])}
    if not items:
        print("用法: skillforge trust add <owner>... | remove <owner>...", file=sys.stderr)
        sys.exit(2)
    if args.action == "add":
        new_set = trusted | items
        save_trusted(new_set)
        added = items - trusted
        for t in sorted(added):
            print(f"  + {t}")
        print(f"已写入 {Path(TRUSTED_FILE).expanduser()}({len(new_set)} 条)")
    elif args.action == "remove":
        new_set = trusted - items
        save_trusted(new_set)
        removed = items & trusted
        for t in sorted(removed):
            print(f"  - {t}")
        print(f"已写入 {Path(TRUSTED_FILE).expanduser()}({len(new_set)} 条)")


def _info_to_chosen(info: dict) -> dict:
    """把 /repos 响应 / fetch_metadata 输出格式化成 _install_chosen 期待的 chosen dict。"""
    return {
        "full_name": info["full_name"],
        "description": info.get("description") or "",
        "stars": info.get("stargazers_count", 0),
        "updated": (info.get("pushed_at") or "")[:10],
        "language": info.get("language") or "",
        "clone_url": info["clone_url"],
        "html_url": info["html_url"],
    }


def _install_chosen(args, chosen: dict, token):
    """选定一个仓库后的统一安装段:star → clone → 安装命令 → adoption → gen SKILL.md → register。
    供新老 cmd_find 共用。
    """
    print(f"\n选定: {chosen['full_name']}  {chosen['stars']}★")
    print(f"  {chosen['html_url']}")

    # star + 收藏
    if not args.no_star:
        if token:
            if confirm(f"给 {chosen['full_name']} 点 star?", args.yes):
                try:
                    ok = star_repo(chosen["full_name"], token)
                    print("  ⭐ 已 star" if ok else "  star 返回非预期状态")
                except GHError as e:
                    print(f"  ⚠️ star 失败:{e}")
                except Exception as e:
                    print(f"  ⚠️ star 失败:{e}")
        else:
            print("  (未设置 GITHUB_TOKEN,跳过 star;设置后即可自动点赞收藏)")

    # clone
    name = re.sub(r"[^a-z0-9-]+", "-", chosen["full_name"].split("/")[-1].lower()).strip("-")
    home = Path(CANONICAL_HOME).expanduser()
    skill_dir = home / name
    repo_dir = skill_dir / "repo"
    if skill_dir.exists():
        print(f"  技能目录已存在: {skill_dir}")
    else:
        if confirm(f"clone {chosen['full_name']} 到 {repo_dir}?", args.yes):
            try:
                clone_repo(chosen["clone_url"], repo_dir)
                print(f"  📥 已 clone 到 {repo_dir}")
            except subprocess.CalledProcessError as e:
                print(f"  clone 失败: {e.stderr}")
                return
        else:
            print("已取消。")
            return

    # 检测安装命令(白名单视作 --install)
    install_cmds = detect_install_cmds(repo_dir)
    if install_cmds:
        print(f"  检测到安装命令: {install_cmds}")
        trusted_hit = is_trusted(chosen["full_name"])
        allow = args.install or trusted_hit
        reason = "--install" if args.install else ("trusted.txt" if trusted_hit else None)
        if allow and confirm(f"现在执行安装?(来源:{reason},会运行仓库代码)", args.yes):
            for c in install_cmds:
                print(f"  $ {c}")
                subprocess.run(c, shell=True, cwd=repo_dir)
        elif allow:
            print("  (已取消安装)")
        else:
            print("  (默认不自动安装。把 owner 加入 ~/.skillforge/trusted.txt 或加 --install 即可自动)")
    else:
        print("  未检测到标准安装方式,仅保留源码。")

    # Adoption
    adopted = False
    found = find_handcrafted_skill_md(name, skill_dir)
    if found:
        existing_md, length, source_dir = found
        print(f"  📑 发现已有手写 SKILL.md:{existing_md} (description {length} 字符)")
        if confirm("  采用它为权威版,跳过新生成?", args.yes):
            adopt_handcrafted_skill_md(source_dir, skill_dir)
            print(f"  ✅ 已采用原作为权威版,旧目录已 .bak 备份")
            adopted = True

    # 生成 SKILL.md(如果没有 adopt)
    if not adopted:
        # 保护:如果 SKILL.md 已存在且 description ≥ 80 字符,默认不覆盖
        # (可能是手写版 / 之前用 LLM 生成的高质量版,不该被本次模板兜底冲掉)
        existing_md = skill_dir / "SKILL.md"
        if existing_md.exists():
            try:
                ex_meta = parse_frontmatter(existing_md)
            except Exception:
                ex_meta = None
            ex_desc_len = len((ex_meta or {}).get("description", ""))
            if ex_desc_len >= 80:
                print(f"  📑 SKILL.md 已存在 (description {ex_desc_len} 字符),保留;rm 后重跑可重新生成")
            else:
                readme = fetch_readme(chosen["full_name"], token)
                md = gen_skill_md(chosen, readme, install_cmds)
                existing_md.write_text(md, encoding="utf-8")
                print(f"  📝 已生成 {existing_md}")
        else:
            readme = fetch_readme(chosen["full_name"], token)
            md = gen_skill_md(chosen, readme, install_cmds)
            existing_md.write_text(md, encoding="utf-8")
            print(f"  📝 已生成 {existing_md}")

    # 注册到各 agent 目录
    if not args.no_register:
        for target, how in register_skill(skill_dir, link=not args.copy):
            print(f"  🔗 注册 {target}  ({how})")

    # v3: 保存 pristine 版本(只在第一次安装时写,/skill-修改 才有 baseline)
    try:
        save_pristine(name, skill_dir)
    except Exception as e:
        print(f"  [warn] 写 pristine 版本失败:{e}", file=sys.stderr)

    # v6: 自动刷新 CATALOG.md
    try:
        generate_catalog()
    except Exception as e:
        print(f"  [warn] 刷新 CATALOG.md 失败:{e}", file=sys.stderr)

    print(f"\n✅ 完成。下次再问类似需求,会直接命中本地技能 `{name}`。\n")
    # v3: 装完自动 intro
    try:
        from argparse import Namespace
        cmd_intro(Namespace(name=name))
    except Exception as e:
        print(f"[warn] 自动 intro 失败,可手动跑 `skillforge intro {name}`: {e}", file=sys.stderr)


def cmd_find_simple(args):
    """老路径:单次 keyword 搜 + stars 排序 + 用户挑序号。--simple 触发。"""
    query = " ".join(args.query)
    token = os.environ.get("GITHUB_TOKEN")

    # 1) 先查本地(闭环的前置闸门)
    skills, _ = scan_local()
    matches = match_local(query, skills)
    if matches and not args.force_new:
        print(f'✅ 本地已有能满足「{query}」的技能,无需重复安装:\n')
        for s, score in matches[:3]:
            print(f"  ● {s.name}  (匹配度 {score:.2f}) — {s.description[:60]}")
        print("\n如仍想另装新的,加 --force-new。")
        return

    # 2) 选仓库:可指定 --repo,否则搜索
    if args.repo:
        try:
            _, info = gh_request(f"/repos/{args.repo}", token)
        except GHError as e:
            print(f"❌ 取仓库元数据失败:{e}", file=sys.stderr)
            return
        chosen = _info_to_chosen(info)
    else:
        print(f'🔎 本地没有,去 GitHub 搜索「{query}」…')
        cands = github_search(query, token, top=args.top)
        if not cands:
            print("没搜到合适的仓库。换个描述试试。")
            return
        print("\n候选:")
        for i, c in enumerate(cands):
            print(f"  [{i}] {c['full_name']}  {c['stars']}★ {c['language']} (更新 {c['updated']})")
            print(f"       {c['description'][:80]}")
        if args.yes:
            idx = 0
            print("\n[自动选 0]")
        else:
            try:
                idx = int(input("\n选哪个? 输序号: ").strip())
            except (ValueError, EOFError):
                print("已取消。")
                return
        chosen = cands[idx]

    return _install_chosen(args, chosen, token)


def _guess_install(language: str):
    """根据语言粗推安装命令(真 clone 后还会精确检测)。"""
    lang = (language or "").lower()
    if lang == "python":
        return ["pip install -e ."]
    if lang in {"javascript", "typescript"}:
        return ["npm install"]
    if lang == "rust":
        return ["cargo build --release"]
    return []


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

    # --repo 直通
    if args.repo:
        try:
            _, info = gh_request(f"/repos/{args.repo}", token)
        except GHError as e:
            print(f"❌ 取仓库元数据失败:{e}", file=sys.stderr)
            return
        return _install_chosen(args, _info_to_chosen(info), token)

    # 1) LLM 改写
    print(f'🔎 「{query}」 改写中…')
    queries = llm_rewrite_query(query)
    print(f"   改写得到 {len(queries)} 个 query:{queries}")

    # 2) 多搜 + 合并去重
    seen, candidates = set(), []
    for q in queries:
        for c in github_search(q, token, top=max(args.top * 2, 6)):
            if c["full_name"] in seen:
                continue
            seen.add(c["full_name"])
            candidates.append(c)
    if not candidates:
        print("❌ 没搜到任何候选,换个描述试试。")
        return
    print(f"   合并去重得 {len(candidates)} 个候选")

    # 3) 元数据体检
    print("   体检中…")
    enriched = []
    for c in candidates:
        try:
            enriched.append(fetch_metadata(c["full_name"], token))
        except GHError as e:
            print(f"   skip {c['full_name']}: {e}", file=sys.stderr)
    if not enriched:
        print("❌ 体检后无可用候选。")
        return

    # 4) 临时 U 分(没有 downloads/close_rate)
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

    # 6) 深读 Top 5:README + close_rate + 包下载量 + Scorecard + OSV(可选跳过)
    if not args.no_readme:
        print("   深读 Top 5(README + close_rate + 下载量 + Scorecard + OSV)…")
        for m in top5:
            m["readme_excerpt"] = fetch_readme(m["full_name"], token)
            m["close_rate"] = fetch_close_rate(m["full_name"], token)
            pkg = guess_package_name(m["full_name"], m.get("default_branch", "main"), m.get("language", ""))
            m["monthly_downloads"] = fetch_downloads(pkg["ecosystem"], pkg["name"]) if pkg else None
            m["scorecard"] = fetch_scorecard(m["full_name"])
            m["osv_vulns"] = fetch_osv_vulns(pkg["ecosystem"], pkg["name"]) if pkg else []
            # 重算 U
            m["U"] = compute_u_score(
                stars=m.get("stargazers_count", 0),
                watchers=m.get("subscribers_count", 0),
                forks=m.get("forks_count", 0),
                downloads=m["monthly_downloads"],
                release_count=m.get("release_count", 0),
                close_rate=m["close_rate"],
            )
            # 重算 T 把 scorecard + osv 接进去,risk_flags 同样更新
            m["T"] = compute_t_score(m, scorecard=m["scorecard"], osv_vulns=m["osv_vulns"])
            m["risk_flags"] = compute_risk_flags(m, scorecard=m["scorecard"], osv_vulns=m["osv_vulns"])
            m["install_cmds"] = _guess_install(m.get("language", ""))
    else:
        for m in top5:
            m["readme_excerpt"] = ""
            m["close_rate"] = None
            m["monthly_downloads"] = None
            m["scorecard"] = None
            m["osv_vulns"] = []
            m["install_cmds"] = _guess_install(m.get("language", ""))

    # 7) LLM 终排 → Top 3
    print("   LLM 终排…")
    ranked = llm_final_rank(query, top5)

    # 8) 渲染
    trusted = load_trusted()
    meta_by_name = {m["full_name"]: m for m in top5}
    print("\n" + render_top3(query, ranked, meta_by_name, trusted))

    # 9) 让用户选
    if not ranked:
        print("❌ 终排无结果。")
        return
    if args.yes:
        idx = 0
        print("[自动选 0]")
    else:
        try:
            idx = int(input("选哪个? 输序号: ").strip())
        except (ValueError, EOFError):
            print("已取消。")
            return
        if idx < 0 or idx >= len(ranked):
            print(f"无效序号 {idx}。已取消。")
            return

    chosen_name = ranked[idx]["full_name"]
    return _install_chosen(args, _info_to_chosen(meta_by_name[chosen_name]), token)


# ----------------------------------------------------------------------------- 入口
def build_parser():
    p = argparse.ArgumentParser(prog="skillforge", description="跨 agent 技能闭环管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="列出已装技能(默认按 MECE 5+1 分类,双语)")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--brief", action="store_true", help="每条 120 字符简介(防输出过大)")
    pl.add_argument("--full", action="store_true", help="每条完整 description 折行(适合细看)")
    pl.add_argument("--flat", action="store_true", help="不分类,纯字母序")
    pl.add_argument("--cat", help="只看某分类(如 --cat executor 或 --cat 数据 模糊匹配)")
    pl.add_argument("--lang", choices=["zh", "en", "auto"], default="auto",
                    help="输出语言(zh/en/auto,auto 会看 CJK/LANG env)")
    pl.set_defaults(func=cmd_list)

    pcat = sub.add_parser("catalog", help="手动重生成 CATALOG.md (默认紧凑模式)")
    pcat.add_argument("--path", help="覆盖输出位置")
    pcat.add_argument("--lang", choices=["zh", "en"], help="输出语言(默认按 SKILLFORGE_LANG env 或 LANG)")
    pcat.add_argument("--brief", dest="brief", action="store_true", default=None,
                      help="紧凑格式:英文名 + 一句中/英释义(默认)")
    pcat.add_argument("--full", dest="brief", action="store_false",
                      help="完整格式:每 skill 输出原 description 全文")
    def _do_catalog(a):
        if a.lang:
            os.environ["SKILLFORGE_LANG"] = a.lang
        print(f"📜 写入 {generate_catalog(a.path, brief=a.brief)}")
    pcat.set_defaults(func=_do_catalog)

    pw = sub.add_parser("which", help="查本地有没有能满足需求的技能")
    pw.add_argument("query", nargs="+")
    pw.set_defaults(func=cmd_which)

    psg = sub.add_parser("suggest", help="自然语言路由 → 本地 Top 3 (markdown 表格,带'适合/不适合')")
    psg.add_argument("query", nargs="+")
    psg.add_argument("--no-browse", action="store_true", help="0 命中时不显示分类菜单提示")
    psg.set_defaults(func=cmd_suggest)

    pf = sub.add_parser("find", help="本地没有就去 GitHub 找并安装(LLM 增强,见 specs)")
    pf.add_argument("query", nargs="+")
    pf.add_argument("--repo", help="跳过搜索,直接指定 owner/repo")
    pf.add_argument("--top", type=int, default=3, help="最终展示几个候选(默认 3;多搜阶段按 top*2 拉)")
    pf.add_argument("--yes", action="store_true", help="非交互:自动确认+选第一个")
    pf.add_argument("--force-new", action="store_true", help="本地已有也强制装新的")
    pf.add_argument("--no-star", action="store_true")
    pf.add_argument("--install", action="store_true", help="允许执行安装命令")
    pf.add_argument("--no-register", action="store_true")
    pf.add_argument("--copy", action="store_true", help="注册用复制而非软链")
    pf.add_argument("--simple", action="store_true", help="跳过 LLM 流水线,走老 keyword 搜索路径")
    pf.add_argument("--no-readme", action="store_true", help="跳过 README 深读 + close-rate + 下载量")
    pf.set_defaults(func=cmd_find)

    pt = sub.add_parser("trust", help="管理可信 owner 白名单(命中则自动允许 --install)")
    pt.add_argument("action", choices=["list", "add", "remove"])
    pt.add_argument("items", nargs="*", help="owner 或 owner/repo,小写")
    pt.set_defaults(func=cmd_trust)

    pc = sub.add_parser("consolidate", help="把同名物理副本合并到 SKILLFORGE_HOME 并改软链")
    pc.add_argument("--dry-run", action="store_true", help="只显示计划不执行")
    pc.add_argument("--yes", action="store_true", help="跳过最后确认")
    pc.set_defaults(func=cmd_consolidate)

    # v3 子命令
    pi = sub.add_parser("intro", help="出一段中文使用说明")
    pi.add_argument("name")
    pi.set_defaults(func=cmd_intro)

    pd = sub.add_parser("detail", help="看某个已装 skill 的详情(名字或编号)")
    pd.add_argument("target", help="name 或 /skill-列表 里的编号")
    pd.set_defaults(func=cmd_detail)

    pin = sub.add_parser("install", help="装一个(编号|name|owner/repo)")
    pin.add_argument("target")
    pin.add_argument("--no-star", action="store_true")
    pin.add_argument("--install", action="store_true", help="允许执行安装命令")
    pin.set_defaults(func=cmd_install)

    pm = sub.add_parser("modify", help="LLM 改源码(需 ANTHROPIC_API_KEY)")
    pm.add_argument("target", help="name 或编号")
    pm.add_argument("request", nargs="+", help="改动需求(中文自然语言)")
    pm.add_argument("--yes", action="store_true", help="跳过应用前的确认")
    pm.set_defaults(func=cmd_modify)

    pr = sub.add_parser("rollback", help="回滚已修改的 skill")
    pr.add_argument("target")
    pr.add_argument("--pristine", action="store_true", help="回到 GitHub 原版(默认 swap previous)")
    pr.add_argument("--yes", action="store_true")
    pr.set_defaults(func=cmd_rollback)

    pu = sub.add_parser("uninstall", help="卸载一个 skill(数据搬 backups/ 不丢)")
    pu.add_argument("target")
    pu.add_argument("--yes", action="store_true")
    pu.set_defaults(func=cmd_uninstall)

    psi = sub.add_parser("self-install", help="装自身 SKILL.md + 9 个 slash 命令到所有 agent")
    psi.add_argument("--copy", action="store_true", help="注册用复制而非软链")
    psi.set_defaults(func=cmd_self_install)

    ph = sub.add_parser("help", help="列所有 skill 命令的用法")
    ph.set_defaults(func=cmd_help)

    # v4: agent-as-LLM 工具命令(无 LLM 调用,纯数据 / 渲染 / 应用)
    pfd = sub.add_parser("find-data", help="(无 LLM)多搜+元数据+T/U → JSON")
    pfd.add_argument("queries", nargs="+", help="1-3 个英文 query")
    pfd.add_argument("--top", type=int, default=3, help="每个 query 拉的候选数(总数会去重)")
    pfd.set_defaults(func=cmd_find_data)

    pdd = sub.add_parser("deep-data", help="(无 LLM)抓 README+Scorecard+OSV+下载量 → JSON")
    pdd.add_argument("names", nargs="+", help="一个或多个 owner/repo")
    pdd.set_defaults(func=cmd_deep_data)

    prn = sub.add_parser("render", help="(无 LLM)读 agent 的 ranking.json → 渲染 Top 3")
    prn.add_argument("--file", required=True, help="JSON 文件路径")
    prn.set_defaults(func=cmd_render)

    pms = sub.add_parser("modify-source", help="(无 LLM)dump skill 所有源文件 → JSON")
    pms.add_argument("target", help="name 或编号")
    pms.set_defaults(func=cmd_modify_source)

    pma = sub.add_parser("modify-apply", help="(无 LLM)读 agent 的 changes.json,显 diff,应用")
    pma.add_argument("target")
    pma.add_argument("--file", required=True, help="changes JSON 文件路径")
    pma.add_argument("--summary", help="一句话改动摘要(写进 SKILL.md 历史)")
    pma.add_argument("--yes", action="store_true")
    pma.set_defaults(func=cmd_modify_apply)
    return p


import urllib.parse  # 顶层导入,供 github_search 使用


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
