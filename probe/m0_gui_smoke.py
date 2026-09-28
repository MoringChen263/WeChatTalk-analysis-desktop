# -*- coding: utf-8 -*-
"""M0 GUI 冒烟：真建主窗口、跑几秒、自动退出。用于确认控件树不炸。

    python probe/m0_gui_smoke.py

记忆库指向临时目录（`GOUTOUJUNSHI_MEMORY_DIR`），只读地跑一遍档案页刷新，
确认「后台线程 + 事件循环」这条路径在真窗口里也走得通，而且不碰用户真实数据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在 import app.* 之前
_TMP = tempfile.mkdtemp(prefix="jev-m0-smoke-")
os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(os.path.join(_TMP, "memory"))
os.environ["JEV_DATA_DIR"] = os.path.join(_TMP, "data")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import load_config  # noqa: E402
from app.ui import _qt  # noqa: E402


def main() -> int:
    cfg = load_config()
    app = QApplication(sys.argv)
    _qt.apply_theme()

    from app.ui.main_window import MainWindow
    win = MainWindow(cfg)
    win.show()

    # 真扫一次窗口，确认左栏下拉能填上内容
    win.page.capture_panel.refresh_windows()
    n = win.page.capture_panel.window_box.count()
    first = win.page.capture_panel.window_box.itemText(0) if n else "(空)"

    print(f"HAS_FLUENT={_qt.HAS_FLUENT}  导航页数={getattr(win, 'navigationInterface', None) and 'n/a'}")
    print(f"窗口尺寸={win.width()}x{win.height()} 标题={win.windowTitle()}")
    print(f"左栏窗口下拉项数={n} 首项={first[:60]}")
    print(f"问题类型={win.page.analysis_panel.current_question_type()}")
    print(f"档案页控件：状态行={len(win.profile.lbl_status.text())} 字　"
          f"表格列数={win.profile.table.columnCount()}　"
          f"按钮数={len(win.profile.findChildren(type(win.profile.btn_refresh)))}")
    print(f"记忆库指向（临时）：{os.environ['GOUTOUJUNSHI_MEMORY_DIR']}")

    # 档案页刷新走后台线程 —— 只有真跑过事件循环才知道这条路通不通
    win.profile.refresh()
    report: dict = {}

    def peek() -> None:
        report["job"] = win.profile._job
        report["st"] = win.profile._st
        report["status"] = win.profile.lbl_status.text()

    QTimer.singleShot(1500, peek)
    QTimer.singleShot(2200, app.quit)
    rc = app.exec()

    print(f"档案页刷新：{'完成' if report.get('job') is None and report.get('st') else '未完成'}"
          f"　{report.get('status', '')}")
    print(f"GUI 冒烟结束 rc={rc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
