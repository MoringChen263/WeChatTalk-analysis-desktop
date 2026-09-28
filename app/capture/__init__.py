# -*- coding: utf-8 -*-
"""采集层：窗口发现 → 截屏 → OCR → transcript。"""
from app.capture.collector import CaptureClosed, Collector, FrameResult  # noqa: F401
from app.capture.transcript import Message, Transcript, TranscriptBuilder, validate_transcript  # noqa: F401

__all__ = ["Collector", "FrameResult", "CaptureClosed", "Transcript", "Message",
           "TranscriptBuilder", "validate_transcript"]
