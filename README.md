# 狗头军师 · 微信对话分析台（jev-chat-analyzer）

一个 Windows 桌面对话分析程序（PySide6 + PyFluentWidgets + PyInstaller one-dir 打包），核心能力由两部分拼接：

1. **微信对话采集**：窗口截图 + OCR（`windows_capture` + `rapidocr_onnxruntime`，依赖与 jev-chat-windows 一致），同时支持导入导出文件。
2. **狗头军师 skill 分析**：加载 `skills/goutoujunshi/` 的 `SKILL.md` + 按需 `references/` 知识文件，注入 OpenAI 兼容 LLM，输出五步分析 + 可复制话术卡。

> 关键前提：goutoujunshi 的回复文案必须由**生成式 LLM** 产出。
> jev-chat-windows 内置的 Laya 只会输出结构化判定/概率（不会生成文本），
> 所以本项目中 Laya 只做**可选辅助信号**，不作为回复引擎。

---

## 0. 下载使用

### 方式 A：直接下载程序包（推荐，无需 Python 环境）

1. 打开 [Releases 页面](https://github.com/MoringChen263/jev-chat-analyzer/releases/latest)。
2. 在 **Assets** 里下载 `jev-chat-analyzer-v0.1.0-win64-*.zip`（约 126 MB）。
3. **解压到任意目录**。压缩包根目录**就是**应用目录，解压后应直接看到 `jev-chat-analyzer.exe`，不要再套一层同名文件夹。
4. 双击 `jev-chat-analyzer.exe` 启动。
5. 首次使用需在界面设置里填入自己的 LLM API Key（DeepSeek / 任意 OpenAI 兼容端点）。

> **Windows 安全提示**：首次启动若弹出「Windows 已保护你的电脑」（SmartScreen），
> 点 **More info → Run anyway** 即可。该提示出现的原因是程序尚未取得微软签名证书，与程序本身无关。
> 打包内不含任何密钥与聊天数据，运行时数据（含 API Key）只写入你本机用户目录，不会上传。

**校验下载完整性**（建议在下载后做一次，可确认没有被下载工具截断）：

Windows 自带命令行里执行（无需装任何工具）：

```
Get-FileHash -Algorithm SHA256 .\jev-chat-analyzer-v0.1.0-win64-*.zip
```

Release 页面的 Assets 里会显示该附件的 SHA256，两者一致即为完整文件。
若解压后找不到 `jev-chat-analyzer.exe`，说明压缩包被多套了一层目录，重新解压一次即可。

### 方式 B：从源码运行 / 二次开发

```bash
git clone https://github.com/MoringChen263/jev-chat-analyzer.git
cd jev-chat-analyzer
git submodule update --init --recursive   # 必须：分析功能依赖 goutoujunshi skill
```

`skills/goutoujunshi` 以 git submodule 指向独立仓库，**跳过上面第二条命令会导致分析时找不到 skill 文件**。

依赖与打包：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

分析功能需要自备 LLM 接口（DeepSeek 等 OpenAI 兼容端点），在界面里填入 API Key 后使用；
仓库内不含任何密钥，`config.example.json` 只是示例模板。

---

## 1. 项目定位

- 目标用户：想用狗头军师的分析能力，但希望直接从微信对话拿素材、在桌面界面里看分析和话术的人。
- 不做什么：不读取/解密微信数据库，不自动回复微信，不绕过权限采集。只做「屏幕可见内容的 OCR」与「已导出文件的解析」。
- 分析侧完整沿用 goutoujunshi 的流程与安全边界，不做情绪之外的擅自升级。

## 2. 核心能力

| 能力 | 说明 |
| --- | --- |
| 微信窗口监控 | 定时/手动截取微信 PC 主窗口，OCR 出文字，识别气泡左右 → 说话人 |
| 说话人锁定 | 首轮让用户确认「我在左侧还是右侧」，锁定 `用户/对象` 映射 |
| 导入导出文件 | 支持 txt、html、ChatLab agent JSON 等格式，预处理后进入同一 transcript 管道 |
| Skill 分析 | 按用户选择的问题类型加载对应 references，构造 system prompt 调用 LLM |
| 话术卡 | 输出分析 + 首选回复 + 稳健/会撩/强势三个版本 + 发送时机 + 后续分支 + 观察窗口 |
| 活人感话术 | 反模板话术规则（禁排比/客套/总结腔）+ 用户口吻样本（模仿你自己消息的用词句长标点）+ 可选「说话风格」设置；解析时自动剥掉引号/编号/句尾句号等 AI 包装痕迹；起草温度 1.2（DeepSeek 闲聊档位） |
| 长期记忆 | 首次同意后写入精简档案与事件（对接 skill 的 `scripts/memory_store.py`，控制体积与封顶） |
| 打包 | PyInstaller one-dir，产出 `jev-chat-analyzer.exe` + `_internal/`，可脱离 Python 运行 |

## 3. 整体架构

```
┌─────────────────────────── Desktop App (PySide6 + qfluentwidgets) ─────────────────────────┐
│  main_window                                                                                │
│    ├─ capture_panel   监控/截图/导入 → transcript                                          │
│    ├─ analysis_panel  五步分析 + 话术卡(复制/算着发)                                        │
│    └─ profile_panel    档案/记忆 查看·暂停·撤销·清空                                         │
└───────────────┬────────────────────────────┬────────────────────────────────┘
                │                            │
        capture 层                     analysis 层
  window_capture → ocr → speaker_mapper │ skill_loader → prompt_builder → llm_client
                 → transcript          │ memory(记忆召回)        │
       importer → transcript            └─ laya_signal (可选概率信号)
                │                            │
                ▼                            ▼
          transcript.json  ──►   OpenAI 兼容 API（DeepSeek/Qwen/OpenAI…）
                                system = SKILL.md + 按需 references + 档案
                                user   = 对话 state + 用户目标/情绪
```

数据流（一轮分析）：

```
截屏/导入 → OCR/解析 → 说话人锁定 → transcript(带编号·间隔·发送人)
   → 问题类型路由 → 加载 skill 上下文(+档案召回) → LLM 调用
   → 结构化输出 → 分析面板渲染 → (可选)记忆自动更新
```

## 4. 模块设计

建议工程结构：

```
jev-chat-analyzer\
├── README.md
├── pyproject.toml / requirements.txt
├── config.example.json
├── app/
│   ├── main.py
│   ├── config.py
│   ├── ui/
│   │   ├── main_window.py
│   │   ├── capture_panel.py
│   │   ├── analysis_panel.py
│   │   └── profile_panel.py
│   ├── capture/
│   │   ├── window_capture.py     # 枚举微信窗口 + 按句柄截图(可滚动重截)
│   │   ├── ocr.py                # rapidocr_onnxruntime 包装，返回 text+box
│   │   ├── speaker_mapper.py     # 气泡左右→用户/对象，需用户首轮确认
│   │   ├── transcript.py         # 帧间去重、排序、编号、间隔计算、导出
│   │   └── importer.py           # 解析 txt/html/chatlab json
│   ├── analysis/
│   │   ├── skill_loader.py       # 读取 SKILL.md + 按需 references 并以JSON索引缓存
│   │   ├── prompt_builder.py     # 组装 system/user prompt（五步 + 边界）
│   │   ├── llm_client.py         # OpenAI 兼容 API（base/model/key 可配）
│   │   ├── parse_output.py       # 解析 LLM 结构化返回为 分析+话术卡
│   │   └── laya_signal.py        # 可选：调用 laya 模型出情绪/热度等概率信号
│   └── memory/
│       └── store.py              # 封装 skill 的 scripts/memory_store.py 或内置同 schema 存储
├── skills/goutoujunshi/          # 以符号链接/拷贝方式引用 skill 仓库(references 全量)
└── scripts/
    ├── dev.ps1
    └── build.ps1                # PyInstaller one-dir，时序对齐 jev-chat-windows
```

### 4.1 capture 层（微信采集）

- **窗口发现**：枚举顶层窗口，按窗口类名/标题匹配微信 PC
  （`WeChatMainWndForPC` 等；多个微信窗口时让用户选句柄）。
- **截图**：用 `windows_capture`（v2.0.1，与 jev-chat-windows 一致）按 HWND 截取，DIP 缩放按 1.0/1.25/1.5 适配，交由 OCR。
- **滚动补全**：可选「自动滚动 + 多次截取 + 帧间去重」，拼出比当前可视更长的记录；默认只分析当前可视内容，逐帧去重防重复。
- **OCR**：`rapidocr_onnxruntime`，输出每行 `(text, box)`。
- **说话人映射**：微信气泡左右两侧——`box 中心 x < 窗口中心` 归一档、`>=` 归另一档；**首轮必须由用户确认**「我＝右侧/左侧」，保存进配置，不按泡泡大小/颜色猜测（遵循 skill 输入证据边界）。

### 4.2 transcript（会话组装）

- 产出与 ChatLab `messages` 契约一致的带编号证据流，每行：`#id 时间 sender(me/obj-a) 原文`。
- 记录相邻消息间隔（秒级）与主动消息统计，供分析使用。
- 支持导出为纯文本 / ChatLab agent JSON，供外部助手（opencode/Claude）直接接手分析。

### 4.3 analysis 层（分析引擎）

- **skill_loader**：启动时读 `SKILL.md`；`references/` 生成索引（标题↔路径）。不批量全载，只按需 1–3 份。
- **问题类型路由**：UI 侧让用户选择本次场景，直接映射 SKILL.md 的「按需加载」表：

| 用户选择 | 加载 references |
| --- | --- |
| 这句怎么回 | `practical/实战话术编排器…` |
| 邀约/开场/第一次见面 | `practical/主动表达、第一次见面与自然接触.md`、`场景感…` |
| 怠慢/投入失衡/要不要退 | `practical/关系投入失衡…` |
| 冲突/吵架 | `knowledge/07-沟通冲突与修复.md` |
| 依恋/焦虑 | `knowledge/03-依恋理论与情绪调节.md` |
| 关系趋势/长记录 | `practical/ChatLab聊天记录分析适配.md` |
| 冷读/PUA 担心 | `knowledge/05-PUA操控与伦理替代.md`、`20-经典社交体系…` |
| 默认 | `practical/实战话术编排器…` |

- **prompt_builder**：system = `SKILL.md` 全文 + 选中的 1–3 份 references + 档案召回（压缩，≤4000 字）+ 紧急例外不适用时保留「先接情绪」。user = transcript 摘要（≤ token 预算，超出部分自动截断为最近 N 条并标注）+ 用户目标/情绪强度。
- **llm_client**：OpenAI 兼容 `POST {base}/chat/completions`；`base_url`、`model`、`api_key` 写入 `config.json`；支持 `response_format=json` 或提示词约束的结构化输出；带超时与重试。
- **parse_output**：把模型输出拆成结构化字段（见 §6）并渲染：分析正文、首选话术、可发送成品、时机、代价、后续分支、观察窗口/停止条件。
- **laya_signal（可选开关）**：对同一 transcript，用内置 laya 模型打 `sentiment`/`receiver_tone`/`risk` 等概率；只作为旁路信号展示，不进入回复文案。

### 4.4 memory 层（长期记忆）

- 优先复用 skill 仓库 `goutoujunshi/scripts/memory_store.py`（subprocess 调用，schema/封顶逻辑直接用官方实现）。
- 若需离线打包，则内置同 schema 的轻量存储（SQLite 或 JSON）：
  - 五类 scope：`user / object / relationship / event / hypothesis`
  - 体积控制：单条 ≤200 字、event ≤200 条/对象 ≤20、hypothesis ≤5、可撤销栈 ≤20
  - 用户控制：enable/revoke/undo/forget-object/clear 全走 UI
- 召回只注入当前对象：`user` 稳定档案 + `object` 快照 + 最近事件摘要（上限 ~4000 字）。

### 4.5 ui 层

- 仿 jev-chat-windows 视觉效果：QFluentWidgets 卡片式左右双栏。
- 左栏：会话监控区（目标窗口选择、截图预览、OCR 结果、说话人确认）。
- 右栏：分析结果（五步分段）+ 话术卡（三个版本 Tab，一键复制）。
- 顶栏：目标人档案按钮（MBTI/评分）、问题类型下拉、设置（API key/model）。
- 首次使用弹一次「允许保存本机档案？」同意窗口；撤销入口常驻。

## 5. 对话状态与说话人锁定

- 同一转录内保持固定映射「我＝obj 0/右侧 / 对象A＝obj 1/左侧」并在分析区头尾展示（契约与 ChatLab 对应）。
- 不根据内容/称呼/代词静默翻转；用户手动纠正后重新锁定。
- 图片/语音条：OCR 缺失时在 transcript 标 `[表情]`/`[图片]`/`[语音]`，不给虚构。

## 6. 分析输出格式（话术卡）

每次分析固定五步（复刻 SKILL.md「每次分析」）：

1. **情绪落地**：2–4 句指出感受、触发点与冲突点。
2. **事实拆分**：已知事实 / 合理推测 / 关键未知 分列，标证据编号。
3. **利益判断**：互惠、可靠、吸引、可行性、机会成本。
4. **明确建议**：一句首选 + 2–4 理由；随后 ≤3 个版本话术（稳健/会撩·策略/强势）。
5. **行动收束**：一个现在能做的小动作、观察窗口或停止条件、值得回来反馈的信号。

「这句怎么回」模式：第一屏先给一条可复制成品，再写时机、代价与积极/含糊/不回应的后续。

## 7. 与 goutoujunshi skill 的契约

- 本程序可导出/遵循 ChatLab 契约（`messages between --format agent` 等），让任何装了狗头军师 skill 的助手能直接驱动分析。
- 引用证据用 `[#id]` 编号；回答开头注明会话与时间范围、实际查询边界。
- 安全边界全部沿用：不协助性施压/威胁/跟踪；明显拒绝即停止推进；不诊断心理疾病；不保证话术真爱生效。

## 8. 配置（config.example.json）

```json
{
  "llm": { "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": "", "temperature": 1.2 },
  "capture": { "target_window_class": "WeChatMainWndForPC", "scroll_frames": 0, "poll_ms": 5000 },
  "transcript": { "max_messages": 60, "token_budget": 3500 },
  "analysis": { "question_type": "default", "style_note": "" },
  "memory": { "enabled": false, "max_context_chars": 4000 },
  "laya": { "enabled": false, "model_dir": "" }
}
```

`laya.model_dir` 留空即不加载（Laya 只是可选辅助信号，不参与回复生成）。

## 9. 打包（仿 jev-chat-windows-v0.1.9）

- PyInstaller **one-dir**：产出 `jev-chat-analyzer.exe` + `_internal/`（约定命名为 `_internal` 则直接默认同名）。
- 依赖清单与 jev-chat-windows 对齐：PySide6、qfluentwidgets、windows_capture、rapidocr_onnxruntime、onnxruntime、opencv-python、Pillow、hf 等（可选 laya/torch：若开启 laya 信号才打入）。
- `skills/goutoujunshi` 作为 data 目录整体打包进 `_internal`。
- `scripts/build.ps1` 封装上述流程，产物输出到 `dist/`。

## 10. 开发计划（里程碑）

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| M0 脚手架 | 工程结构、config、main_window 空壳、dev.ps1 跑通 | 可启动窗口 |
| M1 采集核心 | 窗口枚举+截图+OCR→transcript+说话人确认+导出 | 截一屏微信出带 #id 文本 |
| M2 导入解析 | txt/html/json 导入，统一进 transcript | 导入一份导出文件成功 |
| M3 分析闭环 | skill_loader+prompt+LLM 调用+话术卡渲染 | 输入 transcript 出五步+话术 |
| M4 记忆 | memory_store 封装+UI 同意流 | 建档→召回→撤销闭环 |
| M5 打包 | build.ps1 one-dir 产物、离线跑通 | exe 双击可用 |

## 11. 安全与边界（与 skill 一致）

- 不读解密微信库、不自动外发消息。
- OCR/导入素材只信可见原文；说话人未确认前不解释关系。
- 分析不强推：用户目标是退出/梳理时不催化行动；明确拒绝即停。
- 家暴/跟踪/胁迫等先确认安全并给法律与危机转介（load `knowledge/17`）。