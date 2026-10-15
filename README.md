# Employee Attendance & Analytics API

FastAPI and MongoDB backend implementation for employee attendance tracking, regularization, and analytics, conforming strictly to `openapi.yaml`, `DATA_MODEL.md`, and `PROBLEM_STATEMENT.docx`.

## Prerequisites & Stack
- Python 3.11+
- MongoDB 6.0+ (Tested on MongoDB 8.0)
- FastAPI, PyMongo, Pydantic, Uvicorn

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Configuration:**
   Configure `MONGO_URI` and `MONGO_DB` (defaults to `mongodb://localhost:27017` and `attendance_db`):
   ```bash
   # Optional: set via environment or .env
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

4. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. **Verify Readiness:**
   ```bash
   curl http://localhost:8000/health
   # Returns: {"status": "ok"}
   ```

## Architecture & Implementation Highlights
- **Single File Architecture:** All application code resides in `app/main.py`.
- **Startup Index Initialization:** Indexes are created idempotently on startup within FastAPI's `lifespan` handler.
- **Strict Business Rule Compliance:**
  - **R1:** IST calendar date mapping and overnight shift resolution; instants truncated to whole seconds.
  - **R2:** Grace period logic evaluated strictly; floor minutes from shift start.
  - **R3:** Overtime minutes calculated with minimum 30-minute threshold.
  - **R4:** Work hours calculated using half-up rounding (`ROUND_HALF_UP`).
  - **R5:** Half-day flag based on rounded work hours (< 4.50).
  - **R6–R9:** Accurate working days (Mon–Fri), headcount accounting for zero-log employees, and standard competition ranking.
- **MongoDB Aggregation Pipelines:** Analytics endpoints (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) and `/admin/explain/{endpoint}` run entirely in the database engine using index-backed operations, window functions (`$rank`, `$setWindowFields`), and gap-filling (`$densify`).

## Completed Artifacts
- `app/main.py`: Full implementation of all endpoints and business rules.
- `REVIEW.md`: Detailed defect analysis and fixes from the starter code.
- `DECISIONS.md`: Architectural decisions, race condition mechanics, and scaling strategies.
