"""
Tests for GET/PUT /api/positions.

Regression coverage for a bug found in production: PUT previously did a
naive yaml.dump() of the raw client body with zero validation, which (a)
destroyed all comments in config/positions.yaml on every save and (b) would
happily persist malformed payloads straight to the file the risk agent's
hard-constraint gate reads on every run. These tests point _POSITIONS_PATH at
a scratch file so the real config/positions.yaml is never touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import api.main as api_main
from api.main import app

_SAMPLE_YAML = """\
# Header comment describing the schema — must survive a PUT.
portfolio_nav:
  value: 1000000.0  # NAV comment
  currency: TWD
positions:
  - symbol: "2330.TW"
    instrument_type: stock
    quantity: 1000
    entry_price: 850.0
    currency: TWD
    multiplier: 1.0
"""

_VALID_BODY: dict[str, Any] = {
    "portfolio_nav": {"value": 2000000.0, "currency": "TWD"},
    "positions": [
        {
            "symbol": "2330.TW",
            "instrument_type": "stock",
            "quantity": 500,
            "entry_price": 900.0,
            "currency": "TWD",
            "multiplier": 1.0,
        },
    ],
}


@pytest.fixture
def positions_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "positions.yaml"
    path.write_text(_SAMPLE_YAML, encoding="utf-8")
    monkeypatch.setattr(api_main, "_POSITIONS_PATH", str(path))
    return path


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_positions_returns_file_contents(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["portfolio_nav"]["value"] == 1000000.0
    assert data["positions"][0]["symbol"] == "2330.TW"


async def test_put_valid_body_writes_file(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    resp = await client.put("/api/positions", json=_VALID_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    reloaded = await client.get("/api/positions")
    data = reloaded.json()
    assert data["portfolio_nav"]["value"] == 2000000.0
    assert data["positions"][0]["quantity"] == 500


async def test_put_preserves_header_comment(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    resp = await client.put("/api/positions", json=_VALID_BODY)
    assert resp.status_code == 200

    text = positions_file.read_text(encoding="utf-8")
    assert "Header comment describing the schema" in text


async def test_put_rejects_malformed_positions_type(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    before = positions_file.read_text(encoding="utf-8")
    bad_body = {"portfolio_nav": {"value": 1.0, "currency": "TWD"}, "positions": "not-a-list"}

    resp = await client.put("/api/positions", json=bad_body)

    assert resp.status_code == 422
    assert "positions must be a list" in str(resp.json()["detail"])
    # File must be untouched on validation failure.
    assert positions_file.read_text(encoding="utf-8") == before


async def test_put_rejects_option_missing_required_fields(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    bad_body = {
        "portfolio_nav": {"value": 1.0, "currency": "TWD"},
        "positions": [
            {
                "symbol": "TXO",
                "instrument_type": "option",
                "quantity": -5,
                "currency": "TWD",
                "multiplier": 50.0,
                # missing strike / expiry / option_type / style
            },
        ],
    }

    resp = await client.put("/api/positions", json=bad_body)

    assert resp.status_code == 422
    detail = " ".join(resp.json()["detail"])
    assert "strike" in detail
    assert "expiry" in detail


async def test_put_rejects_invalid_nav_currency(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    bad_body = {"portfolio_nav": {"value": 1.0, "currency": "XXX"}, "positions": []}

    resp = await client.put("/api/positions", json=bad_body)

    assert resp.status_code == 422
    assert "portfolio_nav.currency" in str(resp.json()["detail"])


async def test_put_does_not_leave_temp_file(
    client: httpx.AsyncClient, positions_file: Path
) -> None:
    resp = await client.put("/api/positions", json=_VALID_BODY)
    assert resp.status_code == 200
    assert not positions_file.with_suffix(".yaml.tmp").exists()
    assert not (positions_file.parent / f"{positions_file.name}.tmp").exists()
