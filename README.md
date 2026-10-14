# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, attendance punches, manual corrections, and monthly analytics.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your environment, or create a local `.env` file:

```env
MONGO_URI=<your Atlas connection string>
MONGO_DB=attendance_db
```

Do not commit `.env`. From this directory, load the included sample records and start the API:

```powershell
python sample_seed.py
uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/docs` to try the endpoints. `GET /health` checks the MongoDB connection. Environment variables take precedence over `.env` values.

## Endpoints

- `GET /health`
- `POST /employees` and `GET /employees`
- `POST /attendance/punch-in` and `POST /attendance/punch-out`
- `GET /attendance` and `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

Indexes are created at startup. Instants are stored as BSON UTC dates and returned as epoch milliseconds; MongoDB's internal `_id` is not exposed.

The sample dataset is only a smoke test. Before submission, test edge cases and run every explain option against MongoDB 6.0+; the hidden dataset and concurrency tests have not been run here.
