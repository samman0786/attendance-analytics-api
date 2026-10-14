# DECISIONS.md

1. **Indexes:**
   - We created a unique index on `employees(emp_code)` for fast identity lookups and constraint enforcement.
   - A unique compound index on `attendance_logs(emp_code, date)` guarantees race safety for punch-ins and prevents duplicate daily logs.
   - Secondary indexes on `date`, `status`, and `department` ensure list and aggregation queries remain fast.
   - *Rejected:* We considered creating an index on individual historical nested fields inside the audit trail array, but rejected it because queries filter primarily by top-level collection fields, avoiding unnecessary write overhead.

2. **Punch-in race:**
   - When two identical punch-ins arrive simultaneously, both read the collection and find no log for that date. Both attempt an `insert_one`. Because we defined a unique compound index on `(emp_code, date)`, MongoDB's storage engine enforces uniqueness at the database level. The second write operation fails with a duplicate key error, which our code (or FastAPI/MongoDB driver) safely captures or handles to return a `409 Conflict`, ensuring only one punch-in succeeds.

3. **Ties:**
   - For the late leaderboard, we utilized MongoDB's `$setWindowFields` with `$denseRank` ordered descending by total late minutes and ascending by `emp_code`. If employees tie on late minutes, they receive the exact same rank, and the subsequent rank follows correctly (e.g., 1, 2, 2, 4). The `limit` applies cleanly without arbitrarily dropping tied candidates.

4. **Headcount:**
   - Our department summary aggregation starts from the `employees` collection rather than `attendance_logs`. We use a `$lookup` into attendance logs with `preserveNullAndEmptyArrays: true`. This ensures every employee assigned to a department is counted in the headcount regardless of whether they have absence, leave, or zero attendance records for that month.

5. **One thing you would change at 100x scale:**
   - If this had to scale to 100x the volume, we would introduce date-based sharding on the `attendance_logs` collection (partitioned by month/year) and add an in-memory caching layer (such as Redis) for heavy monthly departmental summaries and leaderboards to minimize frequent heavy aggregation hits on MongoDB.