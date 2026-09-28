# -*- coding: utf-8 -*-
"""skill_loader：读 `goutoujunshi` skill，按需取 1–3 份参考（README.optimized §5.3 / §9）。

硬规矩：
- **只读**：`skill_source` 指向哪就读哪，不回写、不就地改（§9）。
- **按需**：默认只给当前问题直接需要的 1–3 份，不批量加载整个知识库（SKILL.md 原话）。
- **不静默降级**：`route()` 拿到的路径必须在磁盘上真的存在；不存在就进 `missing`
  并给出可读告警，绝不悄悄变成「没有参考」。
- **有预算**：单份与总量都有字符上限，超了就 head 截断并在正文里留可见标记，
  避免一次请求把整本知识库塞进上下文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.analysis import questions as q

_FRONTMATTER = re.compile(r"\A\ufeff?---\s*\n.*?\n---\s*\n", re.S)
_HEADING = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.M)

DEFAULT_REF_CHARS = 9000      # 单份参考的字符上限
DEFAULT_TOTAL_CHARS = 24000   # 本次加载的全部参考合计上限
TRUNC_MARK = "\n\n…（本文件因上下文预算被截断，需要细节时请直接查 skill 原文）\n"


def read_text(path: Path) -> str:
    """按 utf-8-sig → gbk 顺序读；skill 里有个别文件是 Windows 编辑过的。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1).strip()


def title_of(path: Path, text: str | None = None) -> str:
    """标题 = 文件里第一个 `# ` 一级标题，取不到就用文件名（去掉序号前缀）。"""
    if text is None:
        text = read_text(path)
    m = _HEADING.search(text[:4000])
    title = m.group(1).strip() if m else ""
    return title or path.stem


@dataclass
class Ref:
    rel: str        # 相对 skill 根目录的路径，与 SKILL.md 里的写法一致
    path: Path
    title: str
    group: str      # knowledge / practical / other


@dataclass
class LoadedSkill:
    """一次加载的结果。`text` 直接拼进 system prompt。"""

    text: str = ""
    files: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SkillLoader:
    def __init__(self, root: Path | str, ref_chars: int = DEFAULT_REF_CHARS,
                 total_chars: int = DEFAULT_TOTAL_CHARS):
        self.root = Path(root)
        self.ref_chars = int(ref_chars)
        self.total_chars = int(total_chars)
        self._skill_md: str | None = None
        self._index: dict[str, Ref] | None = None

    # --------------------------- 基础件 ---------------------------
    @property
    def skill_md_path(self) -> Path:
        return self.root / "SKILL.md"

    def exists(self) -> bool:
        return self.skill_md_path.is_file()

    def skill_md(self) -> str:
        """SKILL.md 正文（去掉 YAML frontmatter）。读不到返回空串，由上层提示。"""
        if self._skill_md is None:
            if not self.exists():
                self._skill_md = ""
            else:
                self._skill_md = strip_frontmatter(read_text(self.skill_md_path))
        return self._skill_md

    def index(self) -> dict[str, Ref]:
        """references/ 下所有文件的 相对路径 → Ref（标题、分组）。只扫一次。"""
        if self._index is not None:
            return self._index
        idx: dict[str, Ref] = {}
        base = self.root / "references"
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.suffix.lower() in (".md", ".txt"):
                    rel = p.relative_to(self.root).as_posix()
                    parts = rel.split("/")
                    group = parts[1] if len(parts) > 2 else "other"
                    idx[rel] = Ref(rel=rel, path=p, title=title_of(p), group=group)
        self._index = idx
        return idx

    def titles(self) -> list[str]:
        return [f"{r.title}（{r.rel}）" for r in self.index().values()]

    # --------------------------- 路由与加载 ---------------------------
    def route(self, key: str | None, extra: list[str] | None = None) -> tuple[list[Ref], list[str]]:
        """问题类型 → 实际要读的参考（≤3 份）。返回 (refs, missing)。"""
        want = q.pick_refs(key, extra)
        idx = self.index()
        refs: list[Ref] = []
        missing: list[str] = []
        for rel in want:
            hit = idx.get(rel)
            if hit is None:
                missing.append(rel)
            elif hit not in refs:
                refs.append(hit)
        return refs[: q.MAX_REFS], missing

    def load(self, key: str | None, extra: list[str] | None = None,
             skill_text: bool = True) -> LoadedSkill:
        """组装本次 system prompt 的技能部分：SKILL.md + 命中的参考（带预算）。"""
        out = LoadedSkill()
        if not self.exists():
            out.warnings.append(f"找不到 SKILL.md：{self.skill_md_path}")
            return out

        refs, missing = self.route(key, extra)
        out.missing = missing
        if missing:
            out.warnings.append(
                "以下参考文件在 skill 里不存在（SKILL.md 的按需加载表可能与文件不同步）："
                + "、".join(missing))

        budget = self.total_chars
        chunks: list[str] = []
        if skill_text:
            body = self.skill_md()
            if body:
                chunks.append("# SKILL.md（技能总纲，必须遵守）\n\n" + body)
                budget -= min(len(body), budget)

        for ref in refs:
            if budget <= 0:
                out.warnings.append(f"上下文预算已用尽，未加载：{ref.title}")
                continue
            try:
                body = strip_frontmatter(read_text(ref.path))
            except OSError as e:
                out.missing.append(ref.rel)
                out.warnings.append(f"读取失败 {ref.rel}：{e}")
                continue
            cap = min(self.ref_chars, budget)
            if len(body) > cap:
                body = body[:cap] + TRUNC_MARK
                out.truncated.append(ref.rel)
            budget -= len(body)
            chunks.append(f"# 参考：{ref.title}\n> 来源：{ref.rel}\n\n{body}")
            out.files.append(ref.rel)
            out.titles.append(ref.title)

        out.text = "\n\n---\n\n".join(chunks)
        if out.truncated:
            out.warnings.append("以下参考因单份上限被截断：" + "、".join(out.truncated))
        return out

    # --------------------------- 漂移自检 ---------------------------
    def validate_catalog(self) -> list[str]:
        """核对问题类型目录里每条路径都真的存在——文档写错、skill 改名都当场抓出来。"""
        idx = self.index()
        bad: list[str] = []
        for key, _label, refs in q.route_table():
            for rel in refs:
                if rel not in idx:
                    bad.append(f"{key} → {rel}")
        return bad


def default_loader(config) -> SkillLoader:
    return SkillLoader(
        config.skill_path(),
        ref_chars=int(config.get("analysis.ref_chars", DEFAULT_REF_CHARS)),
        total_chars=int(config.get("analysis.total_ref_chars", DEFAULT_TOTAL_CHARS)),
    )


__all__ = ["SkillLoader", "LoadedSkill", "Ref", "default_loader", "strip_frontmatter",
           "title_of", "read_text", "DEFAULT_REF_CHARS", "DEFAULT_TOTAL_CHARS"]
