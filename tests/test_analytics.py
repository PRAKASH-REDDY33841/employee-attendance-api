"""
Unit and integration tests for analytics endpoints and explain endpoint.
"""
from datetime import datetime, timezone
from unittest.mock import patch
import mongomock
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def mock_db_client():
    mock_client = mongomock.MongoClient()
    mock_database = mock_client["test_attendance_db"]
    mock_client.admin.command = lambda cmd, **kwargs: {"ok": 1.0}

    with patch("app.main.client", mock_client), patch("app.main.db", mock_database):
        yield mock_database, mock_client


@pytest.fixture
def test_client(mock_db_client):
    return TestClient(app)


def test_department_trend_validation(test_client):
    # 404 for unknown department
    res = test_client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 404

    # Seed an employee for Engineering
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    # 422 for to < from
    res_inv_dates = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res_inv_dates.status_code == 422

    # 422 for range > 92 days
    res_large_range = test_client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res_large_range.status_code == 422

    # 200 for valid 7-day range
    res_ok = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res_ok.status_code == 200
    data = res_ok.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7  # Exactly 7 days
    # Verify each day has required fields
    for day_item in data["items"]:
        assert "date" in day_item
        assert "is_working_day" in day_item
        assert "headcount" in day_item
        assert "present_count" in day_item
        assert "late_count" in day_item


def test_employee_monthly_not_found(test_client):
    res = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404


def test_admin_explain_validation(test_client):
    # Invalid endpoint name -> 422
    res = test_client.get("/admin/explain/nonexistent_endpoint")
    assert res.status_code == 422

    # Missing required query params -> 422
    res = test_client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    res = test_client.get("/admin/explain/department_trend")
    assert res.status_code == 422
