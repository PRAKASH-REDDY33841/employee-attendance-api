# DECISIONS.md

1. **Indexes.**
   We created a unique index on `employees.emp_code` for business identity and fast lookups, and `employees.(department, joined_on)` for department filtering and headcount queries. On `attendance_logs`, we created a compound unique index on `(emp_code, date)` to enforce natural key uniqueness and prevent race conditions; `(date DESC, emp_code ASC)` to power paginated attendance queries with zero in-memory sort; `(emp_code, punch_out, punch_in)` for fast punch-out open-shift lookups; and `(date, late_minutes, emp_code)` for analytics. We considered and rejected a standalone index on `status`, as status has low cardinality and is always queried alongside `date` or `emp_code`.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both compute the same attendance date and attempt `insert_one`. The database-level unique compound index on `(emp_code, date)` ensures atomic concurrency control: exactly one insert succeeds and commits (returning HTTP 201), while the concurrent insert raises a `DuplicateKeyError`. Our code catches this error and returns HTTP 409 Conflict, eliminating check-then-insert race windows.

3. **Ties.**
   We use standard competition ranking (`$rank` in `$setWindowFields`), where tied employees share the same rank and subsequent ranks are skipped (e.g. 1, 2, 2, 4). Because the contract specifies `limit` is applied *after* ranking (`rank <= limit`), if two employees tie at rank 2 with `limit=2`, both are returned (3 rows total), sorted by `total_late_minutes` descending and then `emp_code` ascending.

4. **Headcount.**
   The department summary aggregation starts from the `employees` collection (matching `joined_on <= month_end`), grouping by department to establish authoritative headcount via `{"$sum": 1}`. It then joins `attendance_logs` using `$lookup`. Employees with zero attendance logs are preserved in the department headcount, ensuring no undercounting.

5. **One thing you would change for 100x the data.**
   At 100x scale (~10 million logs), running ad-hoc cross-collection aggregations for trend and monthly analytics becomes expensive. We would shard `attendance_logs` by a compound key `(date, emp_code)` and maintain pre-aggregated daily/monthly summary collections updated asynchronously via MongoDB Change Streams.
