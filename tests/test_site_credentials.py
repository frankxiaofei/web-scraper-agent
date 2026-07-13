"""站点凭据存储与 API 单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.site_credentials import (
    delete_site_credentials,
    get_browser_auth,
    get_credentials_metadata,
    load_site_credentials,
    reset_site_auth_context,
    save_site_credentials,
    set_site_auth_context,
)
from src.web.app import app

SITE_ID = "zycg_national"
SECRET_VALUE = "super-secret-session-value-xyz"


@pytest.fixture
def credentials_dir(tmp_path: Path):
    with patch("src.core.site_credentials.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        settings.site_credentials_key = "test-credentials-key"
        yield tmp_path / "site_credentials"


def test_save_encrypts_and_load_roundtrip(credentials_dir: Path):
    result = save_site_credentials(
        SITE_ID,
        site_url="https://www.zycg.gov.cn/",
        cookie_header=f"session={SECRET_VALUE}; token=abc123",
        headers={"Authorization": "Bearer test-token"},
        notes="测试凭据",
    )
    assert result["ok"] is True
    assert result["cookie_count"] == 2

    stored_path = credentials_dir / f"{SITE_ID}.json"
    assert stored_path.is_file()
    raw_text = stored_path.read_text(encoding="utf-8")
    assert SECRET_VALUE not in raw_text
    assert "test-token" not in raw_text

    loaded = load_site_credentials(SITE_ID)
    assert loaded is not None
    assert loaded["cookies"][0]["value"] == SECRET_VALUE
    assert loaded["headers"]["Authorization"] == "Bearer test-token"


def test_get_credentials_metadata_masks_secrets(credentials_dir: Path):
    save_site_credentials(
        SITE_ID,
        site_url="https://www.zycg.gov.cn/",
        cookie_header=f"session={SECRET_VALUE}",
    )
    meta = get_credentials_metadata(SITE_ID)
    assert meta["has_credentials"] is True
    assert meta["cookie_names"] == ["session"]
    assert "cookies" not in meta
    assert SECRET_VALUE not in json.dumps(meta)


def test_delete_site_credentials(credentials_dir: Path):
    save_site_credentials(
        SITE_ID,
        site_url="https://www.zycg.gov.cn/",
        cookie_header="session=abc",
    )
    deleted = delete_site_credentials(SITE_ID)
    assert deleted["ok"] is True
    assert load_site_credentials(SITE_ID) is None

    missing = delete_site_credentials(SITE_ID)
    assert missing["ok"] is False


def test_save_requires_payload(credentials_dir: Path):
    with pytest.raises(ValueError, match="至少一项"):
        save_site_credentials(SITE_ID, site_url="https://example.com")


def test_get_browser_auth_builds_cookie_header(credentials_dir: Path):
    save_site_credentials(
        SITE_ID,
        site_url="https://www.zycg.gov.cn/",
        cookies=[{"name": "sid", "value": "v1", "domain": ".zycg.gov.cn", "path": "/"}],
    )
    auth = get_browser_auth(SITE_ID)
    assert auth["cookies"][0]["name"] == "sid"
    assert auth["extra_http_headers"]["Cookie"] == "sid=v1"


def test_site_auth_contextvar(credentials_dir: Path):
    save_site_credentials(
        SITE_ID,
        site_url="https://www.zycg.gov.cn/",
        cookie_header="session=ctx-value",
    )
    from src.core.site_credentials import get_current_browser_auth

    assert get_current_browser_auth()["cookies"] == []
    token = set_site_auth_context(SITE_ID)
    try:
        auth = get_current_browser_auth()
        assert auth["extra_http_headers"]["Cookie"] == "session=ctx-value"
    finally:
        reset_site_auth_context(token)
    assert get_current_browser_auth()["cookies"] == []


def test_api_save_get_delete(credentials_dir: Path):
    client = TestClient(app)

    missing = client.get("/api/sites/nonexistent_site_xyz/credentials")
    assert missing.status_code == 404

    response = client.post(
        f"/api/sites/{SITE_ID}/credentials",
        json={
            "credential_type": "cookie",
            "cookie_header": f"session={SECRET_VALUE}",
            "notes": "via api",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["cookie_count"] == 1

    meta = client.get(f"/api/sites/{SITE_ID}/credentials")
    assert meta.status_code == 200
    meta_body = meta.json()
    assert meta_body["has_credentials"] is True
    assert meta_body["cookie_names"] == ["session"]
    assert SECRET_VALUE not in meta.text

    deleted = client.delete(f"/api/sites/{SITE_ID}/credentials")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    empty = client.get(f"/api/sites/{SITE_ID}/credentials")
    assert empty.json()["has_credentials"] is False
