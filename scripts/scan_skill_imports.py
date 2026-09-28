# -*- coding: utf-8 -*-
"""扫描 skill 目录下所有 Python 脚本的顶层 import，输出逗号分隔的模块名。

**为什么需要（E40）**：`skills/` 是**作为数据文件**打进包的（`--add-data`），
PyInstaller 的模块图分析**看不到它里面的 import**。官方 `memory_store.py` 需要 `sqlite3`，
而本应用自己的代码从头到尾没有 import 过 sqlite3 —— 于是 sqlite3 不进包，
打包后的长期记忆层直接 `ModuleNotFoundError: No module named 'sqlite3'`。
源码运行永远复现不了这个问题，因为源码环境里什么都有。

所以：**凡是「以数据文件形式随包分发、又要在运行时被 import 的 Python 代码」，
它们依赖的模块必须显式 --hidden-import**。与其手工维护一张清单（skill 一升级就漏），
不如每次构建现场扫一遍。

    python scripts/scan_skill_imports.py <skill_dir> [--all]

默认只输出标准库模块（官方 skill 是零第三方依赖的；`--all` 连第三方一起输出）。
以下划线开头的私有 C 扩展（如 `_sqlite3`）不输出 —— 它们由对应的公开模块 hook 负责。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def collect(root: Path, include_third_party: bool = False) -> list[str]:
    stdlib = set(sys.stdlib_module_names)
    mods: set[str] = set()
    for f in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue  # 坏文件不该让构建挂掉；PyInstaller 那边也不需要它
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mods.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                # level > 0 是包内相对 import，跟打包无关
                if node.level == 0 and node.module:
                    mods.add(node.module.split(".")[0])
    out = set()
    for m in mods:
        if m.startswith("_"):
            continue
        if include_third_party or m in stdlib:
            out.add(m)
    return sorted(out)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: scan_skill_imports.py <skill_dir> [--all]", file=sys.stderr)
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    mods = collect(root, include_third_party="--all" in argv)
    print(",".join(mods))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
