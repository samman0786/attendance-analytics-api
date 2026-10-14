# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (Helper) | Mixed naive/aware datetimes and flaw in grace period evaluation. | Test with specific timestamps near the 10-minute grace window; yields wrong late minutes under different time zones. | Standardized input instants to UTC, converted cleanly to IST, and enforced strict `> 10` minutes rule per Rule R2. |
| 2 | `compute_overtime` (Helper) | Overtime was calculated without checking the mandatory 30-minute minimum threshold. | Symptom: Overtime credited for minor overstays under 30 minutes (e.g., 15 mins). | Added check to count overtime only if whole minutes $\ge$ 30, else return 0 per Rule R3. |
| 3 | `punch_in` endpoint | Lack of unique database index and race safety check for concurrent requests. | Symptom: Simultaneous parallel punch-in requests created duplicate logs for the same employee and date. | Created a unique compound index on `(emp_code, date)` at startup and handled duplicate key exceptions to return `409 Conflict`. |
| 4 | `list_employees` / `list_attendance` | Unbounded `page_size` parameter allowing excessively large pages. | Test with `page_size=50000`, causing high memory usage and latency at 100k scale (violating Rule R10). | Enforced strict pagination boundaries: `page_size = min(max(1, page_size), 100)`. |
| 5 | `compute_work_hours` (Helper) | Floating-point precision issues during hourly calculations. | Symptom: Minor decimal drift in work hours summation over monthly aggregates. | Implemented proper half-up rounding logic (`round(hours + 1e-9, 2)`) to comply with Rule R4 & R8. |

Also note anything you looked at and decided was **not** a defect, and why:
- **`/health` endpoint structure:** We examined the health check implementation using `client.admin.command('ping')`. This is clean, safe, and correctly returns `503` if MongoDB is down, adhering to the environment contract.
- **`create_employee` duplicate check:** The check for existing `emp_code` using `db.employees.find_one` combined with the unique index works properly to catch duplicates and return `409 Conflict`.