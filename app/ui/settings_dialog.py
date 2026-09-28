# -*- coding: utf-8 -*-
"""设置：模型后端、密钥、上下文预算（README.optimized §8.4 / §10）。

密钥规矩：
- `config.json` 里**永不出现明文**，只存引用（`env:NAME` / `dpapi:BLOB`）；
- 「保存到本机凭据」用 DPAPI 加密，密文只有当前 Windows 用户能解开；
- 界面上只显示「来源」与掩码预览，不回显完整密钥。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget,
)

from app import paths
from app.analysis.llm_client import LLMClient, LLMError
from app.config import resolve_key
from app.ui._qt import BodyLabel, CaptionLabel, ComboBox, PushButton, SubtitleLabel

MODES = [("云端（OpenAI 兼容 API）", "cloud"), ("本地（Ollama / LM Studio 等）", "local")]


def _mask(secret: str) -> str:
    if not secret:
        return "未设置"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}（{len(secret)} 位）"


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self._build()
        self._load()

    # ------------------------------ 构建 ------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # ---- 基础：模式 / 后端 / 密钥（大多数用户只需要看这里）----
        root.addWidget(SubtitleLabel("模型后端（基础）", self))

        self.form = QFormLayout()
        self.mode_box = ComboBox(self)
        for label, _key in MODES:
            self.mode_box.addItem(label)
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        self.form.addRow("模式", self.mode_box)

        self.base_url = QLineEdit(self)
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.form.addRow("base_url", self.base_url)

        model_row = QWidget(self)
        mh = QHBoxLayout(model_row)
        mh.setContentsMargins(0, 0, 0, 0)
        self.model = QLineEdit(model_row)
        self.model.setPlaceholderText("deepseek-chat")
        self.btn_models = PushButton("拉取模型列表", model_row)
        self.btn_models.clicked.connect(self.fetch_models)
        mh.addWidget(self.model, 1)
        mh.addWidget(self.btn_models)
        self.form.addRow("模型", model_row)

        key_row = QWidget(self)
        kh = QHBoxLayout(key_row)
        kh.setContentsMargins(0, 0, 0, 0)
        self.api_key = QLineEdit(key_row)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("只在保存时加密写入本机凭据，不落明文")
        self.btn_save_key = PushButton("保存到本机凭据", key_row)
        self.btn_save_key.clicked.connect(self.save_key)
        self.btn_clear_key = PushButton("清除", key_row)
        self.btn_clear_key.clicked.connect(self.clear_key)
        kh.addWidget(self.api_key, 1)
        kh.addWidget(self.btn_save_key)
        kh.addWidget(self.btn_clear_key)
        self.form.addRow("API Key", key_row)

        self.lbl_key_src = CaptionLabel("", self)
        self.form.addRow("", self.lbl_key_src)

        self.stream = QCheckBox("流式输出（边生成边显示进度）", self)
        self.form.addRow("", self.stream)
        root.addLayout(self.form)

        # ---- 高级：默认折叠，一般用户不需要动 ----
        self.adv_box = QGroupBox("高级设置（一般不用改，勾选展开）", self)
        self.adv_box.setCheckable(True)
        self.adv_inner = QWidget(self)
        form2 = QFormLayout(self.adv_inner)

        self.max_tokens = QSpinBox(self.adv_inner)
        self.max_tokens.setRange(256, 8192)
        self.max_tokens.setSingleStep(128)
        form2.addRow("max_output_tokens", self.max_tokens)

        self.timeout = QSpinBox(self.adv_inner)
        self.timeout.setRange(5, 600)
        self.timeout.setSuffix(" 秒")
        form2.addRow("request_timeout_s", self.timeout)

        self.cap = QDoubleSpinBox(self.adv_inner)
        self.cap.setRange(0.0, 1000.0)
        self.cap.setDecimals(2)
        self.cap.setPrefix("¥ ")
        self.cap.setSpecialValueText("不限制")
        form2.addRow("每日成本上限", self.cap)

        price_row = QWidget(self.adv_inner)
        ph = QHBoxLayout(price_row)
        ph.setContentsMargins(0, 0, 0, 0)
        self.price_in = QDoubleSpinBox(price_row)
        self.price_out = QDoubleSpinBox(price_row)
        for sp, tip in ((self.price_in, "输入 token 单价"), (self.price_out, "输出 token 单价")):
            sp.setRange(0.0, 1000.0)
            sp.setDecimals(2)
            sp.setSuffix(" 元/百万")
            sp.setToolTip(tip)
        ph.addWidget(self.price_in)
        ph.addWidget(self.price_out)
        form2.addRow("单价（与你自己账单对齐）", price_row)

        self.token_budget = QSpinBox(self.adv_inner)
        self.token_budget.setRange(500, 32000)
        self.token_budget.setSingleStep(500)
        self.token_budget.setSuffix(" tokens")
        self.token_budget.setToolTip("拼给模型的聊天记录上限；超出保留最近 N 条并标注截断。")
        form2.addRow("transcript.token_budget", self.token_budget)

        self.wide = QDoubleSpinBox(self.adv_inner)
        self.wide.setRange(1.0, 4.0)
        self.wide.setSingleStep(0.5)
        self.wide.setSuffix(" 倍")
        self.wide.setToolTip("「关系趋势/长记录」类型需要的更长历史（看趋势不能只看最近几条）。")
        form2.addRow("长记录放大倍数", self.wide)

        self.style_note = QLineEdit(self.adv_inner)
        self.style_note.setPlaceholderText(
            "例：说话直接，爱用「笑死」，几乎不打句号（留空则只靠口吻样本）")
        self.style_note.setToolTip("一句话描述自己的口吻，只影响话术的措辞，分析照旧。")
        form2.addRow("说话风格（可空）", self.style_note)

        self.poll_ms = QSpinBox(self.adv_inner)
        self.poll_ms.setRange(500, 60000)
        self.poll_ms.setSingleStep(500)
        self.poll_ms.setSuffix(" ms")
        form2.addRow("capture.poll_ms", self.poll_ms)

        self.ocr_min = QDoubleSpinBox(self.adv_inner)
        self.ocr_min.setRange(0.0, 1.0)
        self.ocr_min.setDecimals(2)
        self.ocr_min.setSingleStep(0.05)
        self.ocr_min.setToolTip("M-1 标定值 0.90：低于此置信度的 OCR 行会被丢弃。")
        form2.addRow("capture.ocr_min_score", self.ocr_min)

        av = QVBoxLayout(self.adv_box)
        av.addWidget(self.adv_inner)
        self.adv_box.toggled.connect(lambda v: self.adv_inner.setVisible(v))
        self.adv_box.setChecked(False)  # 放在连接之后：触发 setVisible(False)，默认折叠
        root.addWidget(self.adv_box)

        self.lbl_status = BodyLabel("", self)
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)

        row = QHBoxLayout()
        self.btn_test = PushButton("测试连接", self)
        self.btn_test.clicked.connect(self.test_connection)
        self.btn_open_dir = PushButton("打开数据目录", self)
        self.btn_open_dir.clicked.connect(self.open_data_dir)
        row.addWidget(self.btn_test)
        row.addWidget(self.btn_open_dir)
        row.addStretch(1)
        root.addLayout(row)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                               | QDialogButtonBox.StandardButton.Cancel, parent=self)
        box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        root.addWidget(box)

    # ------------------------------ 读写 ------------------------------
    def _mode(self) -> str:
        return MODES[self.mode_box.currentIndex()][1]

    @property
    def prefix(self) -> str:
        return "local_llm" if self._mode() == "local" else "llm"

    def _load(self) -> None:
        c = self.config
        mode = (c.get("llm.mode", "cloud") or "cloud").lower()
        idx = 1 if mode in ("local", "ollama") else 0
        self.mode_box.setCurrentIndex(idx)
        p = self.prefix
        self.base_url.setText(c.get(f"{p}.base_url", "") or "")
        self.model.setText(c.get(f"{p}.model", "") or "")
        self.stream.setChecked(bool(c.get("llm.stream", True)))
        self.max_tokens.setValue(int(c.get("llm.max_output_tokens", 1200)))
        self.timeout.setValue(int(c.get("llm.request_timeout_s", 60)))
        self.cap.setValue(float(c.get("llm.daily_cost_cap", 0) or 0))
        pricing = c.get("llm.pricing", {}) or {}
        star = pricing.get("*") if isinstance(pricing, dict) else None
        if isinstance(star, dict):
            self.price_in.setValue(float(star.get("in_cny_per_mtok", 0) or 0))
            self.price_out.setValue(float(star.get("out_cny_per_mtok", 0) or 0))
        self.token_budget.setValue(int(c.get("transcript.token_budget", 3500)))
        self.wide.setValue(float(c.get("analysis.wide_context_multiplier", 2.0)))
        self.poll_ms.setValue(int(c.get("capture.poll_ms", 5000)))
        self.ocr_min.setValue(float(c.get("capture.ocr_min_score", 0.9)))
        self.style_note.setText(c.get("analysis.style_note", "") or "")
        self._refresh_key_label()
        self._on_mode_changed()

    def _refresh_key_label(self) -> None:
        p = self.prefix
        ref = self.config.get(f"{p}.api_key_ref", "") or ""
        if not ref:
            env_name = "JEV_LLM_API_KEY" if p == "llm" else ""
            self.lbl_key_src.setText(
                f"当前来源：未设置（也可用环境变量 {env_name}）" if env_name
                else "当前来源：本地模式不需要 Key")
            return
        kind = ref.split(":", 1)[0]
        label = {"env": "环境变量", "dpapi": "本机凭据（DPAPI 加密）",
                 "keyring": "Windows 凭据管理器"}.get(kind, kind)
        self.lbl_key_src.setText(f"当前来源：{label}　{_mask(resolve_key(ref))}")

    def _on_mode_changed(self) -> None:
        local = self._mode() == "local"
        for w in (self.btn_save_key, self.btn_clear_key, self.api_key):
            w.setEnabled(not local)
        self.api_key.setPlaceholderText(
            "本地模型无需 Key" if local else "只在保存时加密写入本机凭据，不落明文")
        self.btn_models.setEnabled(True)
        if local and not self.base_url.text().strip():
            self.base_url.setText("http://127.0.0.1:11434/v1")
        self._refresh_key_label()

    # ------------------------------ 动作 ------------------------------
    def save_key(self) -> None:
        key = self.api_key.text().strip()
        if not key:
            self.lbl_status.setText("请先粘贴 Key，再点「保存到本机凭据」。")
            return
        try:
            self.config.store_api_key_dpapi(key, self.prefix)
            self.config.save()
        except Exception as e:
            self.lbl_status.setText(f"加密保存失败：{e}")
            return
        self.api_key.clear()
        self._refresh_key_label()
        self.lbl_status.setText("已用 DPAPI 加密写入本机凭据（config.json 里只有密文引用）。")

    def clear_key(self) -> None:
        self.config.set(f"{self.prefix}.api_key_ref", "")
        self.config.save()
        self._refresh_key_label()
        self.lbl_status.setText("已清除本机保存的 Key（环境变量若存在仍会生效）。")

    def fetch_models(self) -> None:
        self._apply_form_to_config(silent=True)
        client = LLMClient(self.config)
        models = client.list_models()
        if not models:
            self.lbl_status.setText("没能取到模型列表（服务端可能不提供 /models，"
                                    "或 base_url、Key 还不对）。手填也行。")
            return
        from PySide6.QtWidgets import QInputDialog

        picked, ok = QInputDialog.getItem(self, "选择模型", f"服务端可用 {len(models)} 个模型：",
                                          models, 0, False)
        if ok and picked:
            self.model.setText(picked)
            self.lbl_status.setText(f"已选择 {picked}")

    def test_connection(self) -> None:
        self._apply_form_to_config(silent=True)
        client = LLMClient(self.config)
        ready, why = client.ready()
        if not ready:
            self.lbl_status.setText("还不能测试：" + why)
            return
        self.lbl_status.setText("正在测试…")
        from PySide6.QtWidgets import QApplication

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            res = client.chat([{"role": "user", "content": "只回复两个字：可用"}],
                              stream=False, max_tokens=16)
            self.lbl_status.setText(f"连接成功：{res.model}　{res.elapsed_s}s　"
                                    f"{res.usage.total_tokens} tokens")
        except LLMError as e:
            self.lbl_status.setText(f"连接失败：{e}")
        finally:
            QApplication.restoreOverrideCursor()

    def open_data_dir(self) -> None:
        import os
        import subprocess

        d = paths.user_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # noqa: S606 - Windows 专用，打开资源管理器
        except Exception:
            subprocess.Popen(["explorer", str(d)])
        self.lbl_status.setText(str(d))

    # ------------------------------ 保存 ------------------------------
    def _apply_form_to_config(self, silent: bool = False) -> None:
        c = self.config
        p = self.prefix
        c.set("llm.mode", self._mode())
        c.set(f"{p}.base_url", self.base_url.text().strip())
        c.set(f"{p}.model", self.model.text().strip())
        c.set("llm.stream", self.stream.isChecked())
        c.set("llm.max_output_tokens", int(self.max_tokens.value()))
        c.set("llm.request_timeout_s", int(self.timeout.value()))
        c.set("llm.daily_cost_cap", float(self.cap.value()))
        pin, pout = float(self.price_in.value()), float(self.price_out.value())
        if pin or pout:
            c.set("llm.pricing", {"*": {"in_cny_per_mtok": pin, "out_cny_per_mtok": pout}})
        c.set("transcript.token_budget", int(self.token_budget.value()))
        c.set("analysis.wide_context_multiplier", float(self.wide.value()))
        c.set("capture.poll_ms", int(self.poll_ms.value()))
        c.set("capture.ocr_min_score", float(self.ocr_min.value()))
        c.set("analysis.style_note", self.style_note.text().strip())
        if not silent:
            c.save()

    def save(self) -> None:
        self._apply_form_to_config()
        self.accept()


__all__ = ["SettingsDialog"]
