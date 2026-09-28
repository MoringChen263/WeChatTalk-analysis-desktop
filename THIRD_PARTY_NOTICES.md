# 第三方许可与署名 / Third-Party Notices

本程序（jev-chat-analyzer）在源码与发布包中复用/打包了下列第三方作品。
按各自许可要求，随包保留其许可全文与版权声明。

---

## 1. goutoujunshi（狗头军师 skill）

- **用途**：内置为 `skills/goutoujunshi`（data 目录，随 one-dir 包打入 `_internal`）。
  仅**读取**其 `SKILL.md`、`references/` 与调用 `scripts/memory_store.py`，不做修改。
- **来源**：`D:\Jev\goutoujunshi`（唯一权威源）；应用运行时**只读**，不回写。
- **许可**：MIT License

```
MIT License

Copyright (c) 2026 powerycy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. jev-chat-src（采集与引擎代码来源）

- **用途**：`app/capture/*`、`app/analysis/*`、`app/worker.py`、`app/ui/*` 由该项目的
  `app/{capture,ocr,worker,settings,overlay,update,version}.py` 与 `core/{engine,jev_client,questions,draft}.py`
  **改造复用**；`probe/*` 原样保留作冒烟脚本。
- **来源**：`D:\Jev\jev-chat-src`
- **许可**：MIT License（已核对 `LICENSE` 文件）

```
MIT License

Copyright (c) 2026 rezoch340

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 3. Python 依赖（pip 包）

以下依赖不复制其源码，仅在打包时按各自许可随包分发二进制/字节码，
`scripts/build.ps1` 构建时会自动导出完整清单到 `dist/THIRD_PARTY_LICENSES.txt`。

| 包 | 许可 |
| --- | --- |
| PySide6 / PySide6-Fluent-Widgets (qfluentwidgets) | LGPL-3.0 / GPL-3.0 · MIT |
| windows-capture | MIT |
| rapidocr-onnxruntime | Apache-2.0 |
| onnxruntime | MIT |
| opencv-python | Apache-2.0 |
| numpy | BSD-3-Clause |
| Pillow | MIT-CMU |

> 注：Laya 相关依赖（torch 等）**默认不打包**，仅在用户显式安装
> `requirements-laya.txt` 时额外引入，届时由构建脚本追加其许可清单。
