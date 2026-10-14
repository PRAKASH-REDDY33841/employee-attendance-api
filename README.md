# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for managing employee attendance (punch-in, punch-out, regularization audit trails) and computing attendance analytics across departments.

## Prerequisites

- **Python**: 3.11 or newer
- **MongoDB**: 6.0 or newer (Local Docker or MongoDB Atlas)
- **Git**

## Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd employee_attendance_manager
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Configuration:**
   Configure connection details using environment variables or a local `.env` file (never commit `.env`):
   ```bash
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

5. **(Optional) Seed sample data:**
   ```bash
   python sample_seed.py
   ```

## Running the Application

Start the API server from the repository root:
```bash
uvicorn app.main:app --port 8000 --reload
```
The application initializes all necessary MongoDB indexes idempotently at startup.

## API Documentation

Interactive Swagger documentation is available once the server is running:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **OpenAPI Schema**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

## Running Automated Tests

Run the test suite using `pytest`:
```bash
pytest -v
```

## Implemented Features

- **Health Probe**: `GET /health` verifying live MongoDB ping.
- **Employee Management**:
  - `POST /employees` with unique customer `emp_code`, input validation, and `shift_start != shift_end` enforcement.
  - `GET /employees` with department filtering, ascending sort by `emp_code`, and offset-based pagination.
- **Attendance Punch Operations**:
  - `POST /attendance/punch-in` implementing IST calendar dates, overnight shift boundary rules (R1), late-minute grace rules (R2), and atomic race condition prevention.
  - `POST /attendance/punch-out` computing work hours half-up (R4), overtime thresholding (R3), half-day status (R5), and concurrency-safe record closure.
  - `GET /attendance` with date range filtering, compound sorting, and direct database pagination.
  - `PATCH /attendance/{emp_code}/{date}` manual correction with validation and an append-only `history` audit trail capturing before/after states.
- **MongoDB Aggregation Analytics**:
  - `GET /analytics/employees/{emp_code}/monthly`: Mid-month joiner working days (R7), Mon–Fri present days (R5, R6), late/overtime totals, and attendance percentage.
  - `GET /analytics/departments/summary`: Department-level summary preserving zero-log employees (R9) and record-level average work hours.
  - `GET /analytics/leaderboard/late`: Competition ranking (1, 2, 2, 4) using MongoDB `$setWindowFields` `$rank`, applying limit after ties.
  - `GET /analytics/departments/{department}/trend`: Daily time-series with gap filling and 7-day moving averages.
- **Admin & Index Inspection**:
  - `GET /admin/explain/{endpoint}` returning raw MongoDB `executionStats` confirming index scans (`IXSCAN`).

## Known Limitations

- Real-time aggregation pipelines on unindexed full collection scans would degrade at 100x scale (~10M+ records); for that volume, pre-aggregated rollups or change stream consumers would be recommended.
