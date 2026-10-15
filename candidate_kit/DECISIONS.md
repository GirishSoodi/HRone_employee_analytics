# DECISIONS.md

1. **Indexes.**
We created unique index `employees(emp_code)` to enforce business key uniqueness and enable $O(1)$ lookups. Compound index `employees(department, emp_code)` accelerates filtered employee listings and department aggregations. On `attendance_logs`, compound unique index `(emp_code, date)` guarantees one record per employee daily and race-safe punch-ins. Index `(date: -1, emp_code: 1)` backs paginated attendance queries with zero in-memory sorting. Index `(emp_code: 1, date: -1)` serves punch-out and monthly analytics. Index `(date: 1, late_minutes: 1, emp_code: 1)` powers the late leaderboard without collection scans. We considered an index on `punch_in` alone, but rejected it because queries always constrain `emp_code`, making compound indexes significantly more selective.

2. **Punch-in race.**
When two identical punch-in requests arrive simultaneously, both compute the attendance date and attempt to insert into `attendance_logs`. Because of the database-level unique constraint on `(emp_code, date)`, MongoDB's document lock allows only one write to succeed (returning 201). The second request encounters a `DuplicateKeyError`, caught by our handler and translated into HTTP 409. No vulnerable check-then-insert window exists.

3. **Ties.**
Our leaderboard pipeline uses MongoDB's `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). The `limit` filter is applied *after* ranking (`rank <= limit`). If two employees tie on late minutes at rank 10, both share rank 10 and both are returned, resulting in 11 rows. Tied employees are deterministically sorted by `emp_code` ascending.

4. **Headcount.**
The department summary aggregation starts from the `employees` collection (matching `joined_on <= period_end_date`), grouping by department to establish true headcount first. It then `$lookup`s matching `attendance_logs`. Employees with zero logs are preserved and counted in headcount.

5. **One thing you would change for 100x data.**
At 100x scale (~10 million attendance records), we would shard `attendance_logs` on a hashed compound key `(emp_code, date)` and maintain pre-aggregated daily/monthly department rollup tables updated via change streams rather than computing on-the-fly cross-collection aggregations.
