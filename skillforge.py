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
                return "GitHub 匿名 rate limit 超出 (403)。设置一个有效的 GITHUB_TOKEN 把配额提到 5000/小时。"
            return "GitHub rate limit 超出 (403)。等到 reset 窗口或换 token。"
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


def compute_t_score(meta: dict) -> int:
    """治理透明度 0-100。spec §5.1。"""
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


def compute_risk_flags(meta: dict) -> list:
    """风险标签 list。spec §5.3。"""
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
def cmd_list(args):
    skills, shadowed = scan_local()
    if not skills:
        print("(没有找到已安装的技能)")
        return
    print(f"已安装技能 {len(skills)} 个(按 name 去重;活跃版本):\n")
    for s in skills:
        print(f"  ● {s.name}")
        if s.description:
            print(f"      {s.description[:88]}")
        print(f"      来源: {s.source}")
    if shadowed:
        print(f"\n另有 {len(shadowed)} 份同名副本被遮蔽(同名时优先 SKILLFORGE_HOME > 用户级):")
        for s in shadowed:
            print(f"  ✕ {s.name}   {s.path}")
        print("  可以合并到 SKILLFORGE_HOME 后用软链统一,或删除冗余副本。")
    if args.json:
        print("\n" + json.dumps(
            {"active": [asdict(s) for s in skills], "shadowed": [asdict(s) for s in shadowed]},
            ensure_ascii=False, indent=2,
        ))


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
    print(f'   运行  skillforge find "{query}"  去 GitHub 找一个并安装。')
    return False


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


def cmd_find(args):
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
        chosen = {
            "full_name": info["full_name"],
            "description": info.get("description") or "",
            "stars": info.get("stargazers_count", 0),
            "updated": (info.get("pushed_at") or "")[:10],
            "language": info.get("language") or "",
            "clone_url": info["clone_url"],
            "html_url": info["html_url"],
        }
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

    print(f"\n选定: {chosen['full_name']}  {chosen['stars']}★")
    print(f"  {chosen['html_url']}")

    # 3) star + 收藏(改动账号,先确认)
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

    # 4) clone
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

    # 5) 检测安装命令(默认不自动执行;若 owner 在 trusted.txt 里,视作 --install)
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

    # 5.5) Adoption:发现已有手写的高质量 SKILL.md 就采用,而不是用模板/LLM 重新生成
    adopted = False
    found = find_handcrafted_skill_md(name, skill_dir)
    if found:
        existing_md, length, source_dir = found
        print(f"  📑 发现已有手写 SKILL.md:{existing_md} (description {length} 字符)")
        if confirm("  采用它为权威版,跳过新生成?", args.yes):
            adopt_handcrafted_skill_md(source_dir, skill_dir)
            print(f"  ✅ 已采用原作为权威版,旧目录已 .bak 备份")
            adopted = True

    # 6) 生成 SKILL.md(如果没有 adopt)
    if not adopted:
        readme = fetch_readme(chosen["full_name"], token)
        md = gen_skill_md(chosen, readme, install_cmds)
        (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
        print(f"  📝 已生成 {skill_dir / 'SKILL.md'}")

    # 7) 注册到各 agent 目录
    if not args.no_register:
        for target, how in register_skill(skill_dir, link=not args.copy):
            print(f"  🔗 注册 {target}  ({how})")

    print(f"\n✅ 完成。下次再问类似需求,会直接命中本地技能 `{name}`。")


# ----------------------------------------------------------------------------- 入口
def build_parser():
    p = argparse.ArgumentParser(prog="skillforge", description="跨 agent 技能闭环管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="列出已装技能(跨 agent 去重)")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pw = sub.add_parser("which", help="查本地有没有能满足需求的技能")
    pw.add_argument("query", nargs="+")
    pw.set_defaults(func=cmd_which)

    pf = sub.add_parser("find", help="本地没有就去 GitHub 找并安装")
    pf.add_argument("query", nargs="+")
    pf.add_argument("--repo", help="跳过搜索,直接指定 owner/repo")
    pf.add_argument("--top", type=int, default=5, help="搜索候选数")
    pf.add_argument("--yes", action="store_true", help="非交互:自动确认+选第一个")
    pf.add_argument("--force-new", action="store_true", help="本地已有也强制装新的")
    pf.add_argument("--no-star", action="store_true")
    pf.add_argument("--install", action="store_true", help="允许执行安装命令")
    pf.add_argument("--no-register", action="store_true")
    pf.add_argument("--copy", action="store_true", help="注册用复制而非软链")
    pf.set_defaults(func=cmd_find)

    pt = sub.add_parser("trust", help="管理可信 owner 白名单(命中则自动允许 --install)")
    pt.add_argument("action", choices=["list", "add", "remove"])
    pt.add_argument("items", nargs="*", help="owner 或 owner/repo,小写")
    pt.set_defaults(func=cmd_trust)

    pc = sub.add_parser("consolidate", help="把同名物理副本合并到 SKILLFORGE_HOME 并改软链")
    pc.add_argument("--dry-run", action="store_true", help="只显示计划不执行")
    pc.add_argument("--yes", action="store_true", help="跳过最后确认")
    pc.set_defaults(func=cmd_consolidate)
    return p


import urllib.parse  # 顶层导入,供 github_search 使用


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
