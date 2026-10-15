# DECISIONS.md

1. **Indexes.**
   - `employees.emp_code` (unique): Enforces strict uniqueness for employee business identifiers and accelerates single-record lookups.
   - `employees.(department, joined_on)`: Supports department filtering and headcount aggregations, avoiding collection scans.
   - `attendance_logs.(emp_code, date)` (unique): Prevents duplicate daily punch-ins under concurrency and provides index-backed lookup by natural key.
   - `attendance_logs.(date, emp_code)`: Backs attendance list queries and sorts (`date DESC, emp_code ASC`).
   - `attendance_logs.(emp_code, punch_in)`: Accelerates punch-out lookups (`punch_in <= punched_at`).
   - `attendance_logs.(date, late_minutes)`: Speeds up late-leaderboard aggregation filtering.
   - *Rejected*: A single index on `status`. With only 5 distinct values, its low cardinality makes it inefficient compared to compound indexes prefixed by `date`.

2. **Punch-in race.**
   Both requests validate the employee, truncate the timestamp to whole seconds, compute the IST attendance date, and execute `insert_one`. Because of the unique compound index on `(emp_code, date)`, MongoDB's storage engine atomically accepts the first insert and rejects the second with a duplicate key error. Our handler catches PyMongo's `DuplicateKeyError` and returns HTTP 409 Conflict.

3. **Ties.**
   Our leaderboard aggregation uses MongoDB's standard competition ranking window function (`$rank: {}`), where tied employees share the same rank and subsequent ranks are skipped (e.g., 1, 2, 2, 4). The limit is applied as `$match: { rank: { $lte: limit } }` followed by sorting on `total_late_minutes` descending and `emp_code` ascending. If multiple employees tie at or below the cutoff rank, all of them are returned.

4. **Headcount.**
   The department summary aggregation starts on the `employees` collection (matching `joined_on <= month_end`), groups by department to establish baseline `headcount` and employee codes, and performs a `$lookup` into `attendance_logs`. Employees with zero attendance logs still contribute to the headcount group.

5. **One thing you would change for 100x data.**
   At 100x data (~10M attendance records), real-time cross-collection `$lookup` joins and dense trend calculations would increase latency. I would maintain pre-computed daily department rollups (via MongoDB Change Streams or background cron jobs writing to a summary collection), turning heavy aggregations into single-document index lookups.
