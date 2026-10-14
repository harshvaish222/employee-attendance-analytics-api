# REVIEW.md

The starter has multiple correctness, validation, concurrency, and scalability defects. The replacement implementation is in `app/main.py`.

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late` | Uses the punch-in's clock date without reliably anchoring the shift to the attendance date; can miscalculate overnight records. | Punch in around midnight for a 22:00–06:00 shift. | Anchor shift start to the record's attendance date in IST. |
| 2 | `compute_late` | Does not truncate punch instants to whole seconds before calculations; `int()` also truncates negative values toward zero rather than applying floor consistently. | Punch at 09:40:00.900 or a timestamp just before shift start. | Truncate instants to seconds and use whole-minute floor arithmetic, applying the strict 10-minute grace rule. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even and binary floats, not the contract's half-up rounding. | Test a duration exactly at a half-centesimal boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Always uses the shift end on the same calendar date, which fails overnight shifts. | 22:00–06:00 shift ending at 06:40 the next day. | Anchor overnight shift end to the following day and apply the 30-minute minimum. |
| 5 | `compute_overtime` | Counts any positive overtime, even below the required 30 minutes. | Punch out 18:45 for an 18:30 shift. | Return zero unless elapsed overtime is at least 30 whole minutes. |
| 6 | `health` | Returns success without checking MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 when it is unavailable. |
| 7 | `create_employee` | Check-then-insert allows duplicate employee codes under concurrent requests. | Send parallel POSTs with the same `emp_code`. | Enforce a unique index and translate `DuplicateKeyError` to 409. |
| 8 | `create_employee` | Stores a naive local datetime and returns it without the required epoch-millisecond representation. | Compare `created_at` with the OpenAPI contract. | Store UTC BSON datetime and serialize to epoch milliseconds. |
| 9 | `create_employee` / model | Does not enforce the full field formats, date validity, and distinct shift start/end rule. | Submit malformed code/email/date or equal shift times. | Add Pydantic validation and explicit shift validation. |
| 10 | `list_employees` | `skip = page * page_size` skips the first page, and `total` ignores the department filter. | Request page 1 and filter by department. | Use `(page - 1) * page_size` and count the filtered query. |
| 11 | `list_employees` | Does not sort by `emp_code`, validate page bounds, or serialize `created_at`. | Compare response order/schema with OpenAPI. | Sort, validate pagination, and serialize each employee. |
| 12 | `punch_in` | Does not check whether the employee exists before indexing `emp`; unknown employee causes a 500. | Punch in with an unknown code. | Return 404 before using employee fields. |
| 13 | `punch_in` | Converts epoch milliseconds using local machine time and does not truncate/normalize to UTC. | Run with a different host timezone or pass fractional seconds. | Validate epoch milliseconds and normalize to UTC whole seconds. |
| 14 | `punch_in` | Uses the instant's UTC/local date rather than the contract's IST attendance date and overnight rule. | Punch in shortly after midnight IST. | Derive the natural attendance date in IST, accounting for overnight shifts. |
| 15 | `punch_in` | Check-then-insert is race-prone and the response leaks an `id` forbidden by the contract. | Send concurrent punch-ins; inspect the response keys. | Use a unique `(emp_code, date)` index, return 409 on duplicate, and never expose Mongo `_id`. |
| 16 | `punch_in` | Does not validate status or handle an unknown employee correctly. | Send `ABSENT` as punch-in status. | Restrict status to `PRESENT`, `WFH`, and `ON_DUTY`; return 422/404 as specified. |
| 17 | `list_attendance` | Loads every matching document into Python, sorts in memory, and only then paginates. | Use a large dataset; observe memory/time growth. | Apply indexed database sort, skip, limit, and filtered count. |
| 18 | `list_attendance` | Sorts only by date, omits the required `emp_code` tie-breaker, and leaks Mongo `_id` as `id`. | Create multiple records on the same date. | Sort date descending then employee code ascending; serialize contract fields only. |
| 19 | `list_attendance` | Does not validate date range ordering or status values. | Pass `date_from > date_to` or an unknown status. | Return 422 for invalid filters. |
| 20 | Application setup | Creates no indexes and does not implement punch-out, correction, analytics, or explain endpoints. | Inspect startup indexes and call each documented path. | Create idempotent indexes and implement every contracted route. |

### Reviewed and retained

Using synchronous PyMongo is permitted by the assignment (it explicitly allows PyMongo or Motor); synchronous FastAPI route functions are run in a worker thread. The two-collection model and `emp_code` natural key are intentional. Attendance history belongs only to manual corrections, not ordinary punch-in/out events.
