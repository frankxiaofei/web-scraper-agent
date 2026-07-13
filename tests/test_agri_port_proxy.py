"""迁移后农业/股票 UI 端口代理测试。"""

from __future__ import annotations

from pathlib import Path

from src.web.agri_insights_service import get_agri_ui_port, get_main_ui_port
from src.web.stock_insights_service import get_stock_ui_port


def test_get_agri_ui_port_from_file(tmp_path, monkeypatch):
    port_file = tmp_path / ".agri_ui_port"
    port_file.write_text("8191", encoding="utf-8")
    monkeypatch.setattr(
        "src.web.agri_insights_service.AGRI_UI_PORT_PATH",
        port_file,
    )
    assert get_agri_ui_port() == 8191


def test_get_main_ui_port_default():
    assert get_main_ui_port(default=8090) == 8090


def test_get_stock_ui_port_from_file(tmp_path, monkeypatch):
    port_file = tmp_path / ".stock_ui_port"
    port_file.write_text("8192", encoding="utf-8")
    monkeypatch.setattr(
        "src.web.stock_insights_service.STOCK_UI_PORT_PATH",
        port_file,
    )
    assert get_stock_ui_port() == 8192
