# Design decisions

1. **Indexes:** `employees.emp_code` and `(attendance_logs.emp_code, date)` are unique, preventing duplicate employees and daily attendance records. Other compound indexes support date-based lists, employee lookups, status filters, late totals, and department headcount. Extra indexes would increase write and storage costs, so each should serve a real query.

2. **Punch-in race:** Two requests can arrive together, but the unique attendance index allows only one insert. The successful request returns 201; the duplicate insert becomes 409. The index—not a preliminary lookup—is the concurrency guarantee.

3. **Leaderboard ties:** `$rank` gives equal totals the same rank and skips the next rank. The limit is applied to the rank, so ties at the cutoff are all included. `emp_code` makes the display order deterministic.

4. **Headcount:** The department summary starts from employees who joined by month-end, then looks up their attendance. Employees with no logs remain included in headcount.

5. **More data:** I would benchmark query plans and latency first, then consider precomputed summaries and partitioning if measurements justified them. The raw attendance history would remain available for audit and reconciliation.
