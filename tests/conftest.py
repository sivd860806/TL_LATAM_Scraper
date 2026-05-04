"""Fixtures compartidos de pytest."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    """TestClient sincrono de FastAPI para tests rapidos."""
    return TestClient(app)
