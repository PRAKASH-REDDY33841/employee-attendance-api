"""
API Endpoint tests using FastAPI TestClient and mongomock.
"""
from datetime import datetime, timezone
from unittest.mock import patch
import mongomock
import pytest
from fastapi.testclient import TestClient

from app.main import app, db


@pytest.fixture
def mock_db_client():
    mock_client = mongomock.MongoClient()
    mock_database = mock_client["test_attendance_db"]
    # Mock ping command
    mock_client.admin.command = lambda cmd, **kwargs: {"ok": 1.0}

    with patch("app.main.client", mock_client), patch("app.main.db", mock_database):
        yield mock_database, mock_client


@pytest.fixture
def test_client(mock_db_client):
    return TestClient(app)


def test_health_check(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_list_employees(test_client):
    # Validation error: shift_start == shift_end
    res_invalid = test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Invalid Shift",
            "email": "invalid@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-05",
        },
    )
    assert res_invalid.status_code == 422

    # Success
    res = test_client.post(
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
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)

    # Duplicate emp_code -> 409
    res_dup = test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Duplicate",
            "email": "dup@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    assert res_dup.status_code == 409

    # List employees
    list_res = test_client.get("/employees?department=Engineering")
    assert list_res.status_code == 200
    page_data = list_res.json()
    assert page_data["total"] == 1
    assert len(page_data["items"]) == 1
    assert page_data["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_and_punch_out_flow(test_client):
    # Setup employee
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-13",
        },
    )

    # Punch in for unknown employee -> 404
    p_err = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert p_err.status_code == 404

    # Punch in 09:28 IST on 2026-07-14 (03:58 UTC) = 1783999080000 ms
    # 2026-07-14 03:58:00 UTC = 1783999080000
    punch_in_ms = 1783999080000
    p_in = test_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP0002",
            "punched_at": punch_in_ms,
            "status": "PRESENT",
        },
    )
    assert p_in.status_code == 201
    in_data = p_in.json()
    assert in_data["emp_code"] == "EMP0002"
    assert in_data["date"] == "2026-07-14"
    assert in_data["punch_in"] == punch_in_ms
    assert in_data["punch_out"] is None
    assert in_data["late_minutes"] == 0
    assert in_data["half_day"] is False
    assert in_data["history"] == []

    # Punch out at 19:10 IST on 2026-07-14 (13:40 UTC) = 1784036400000 ms
    punch_out_ms = 1784036400000
    p_out = test_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0002",
            "punched_at": punch_out_ms,
        },
    )
    assert p_out.status_code == 200
    out_data = p_out.json()
    assert out_data["punch_out"] == punch_out_ms
    assert out_data["work_hours"] > 0
    assert out_data["overtime_minutes"] == 40  # 40 min past 18:30

    # Second punch out -> 409 already punched out
    p_out_again = test_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0002",
            "punched_at": punch_out_ms + 1000,
        },
    )
    assert p_out_again.status_code == 409


def test_regularize_attendance(test_client):
    # Setup employee and punch
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
        },
    )

    punch_in_ms = 1783312500000  # 2026-07-06 10:05 IST (late by 35 min)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0003", "punched_at": punch_in_ms, "status": "PRESENT"},
    )

    # Regularize: correct punch_in to 09:28 IST (1783310280000 ms)
    corr_in_ms = 1783310280000
    patch_res = test_client.patch(
        "/attendance/EMP0003/2026-07-06",
        json={
            "punch_in": corr_in_ms,
            "reason": "Biometric scanner delay",
            "regularized_by": "hr.manager",
        },
    )
    assert patch_res.status_code == 200
    patched_data = patch_res.json()
    assert patched_data["punch_in"] == corr_in_ms
    assert patched_data["late_minutes"] == 0  # Recomputed!
    assert len(patched_data["history"]) == 1
    h_entry = patched_data["history"][0]
    assert h_entry["by"] == "hr.manager"
    assert "late_minutes" in h_entry["changes"]
    assert h_entry["changes"]["late_minutes"]["from"] == 35
    assert h_entry["changes"]["late_minutes"]["to"] == 0

    # Changing nothing -> 422
    patch_noop = test_client.patch(
        "/attendance/EMP0003/2026-07-06",
        json={
            "punch_in": corr_in_ms,
            "reason": "Trying same value",
            "regularized_by": "hr.manager",
        },
    )
    assert patch_noop.status_code == 422


def test_attendance_list_validation(test_client):
    # date_from > date_to -> 422
    res = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422
