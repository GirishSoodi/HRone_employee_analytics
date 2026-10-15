# PeoplePulse --- Employee Attendance & Workforce Analytics

PeoplePulse is a web-based employee attendance and workforce analytics
application. It combines a responsive dashboard with a FastAPI backend
and MongoDB database for attendance tracking, employee management,
attendance regularization, and workforce reporting.

## Features

-   **Dashboard:** Overview of attendance and workforce metrics.
-   **Employee directory:** Browse and manage employee records.
-   **Attendance management:** Record and review employee
    punch-in/punch-out activity.
-   **Regularization:** Support attendance correction workflows.
-   **Analytics:** Department and workforce summaries with date-based
    trends.
-   **Notifications:** A notification bell with a dropdown panel, unread
    blue-dot indicators, mark-all-as-read, clear-all, and toast messages
    for successful actions and errors.
-   **Health status:** Displays database/API health in the interface.
-   **API documentation:** Interactive Swagger UI and ReDoc pages.
-   **Performance:** MongoDB indexes and aggregation pipelines support
    efficient queries.
-   **IST handling:** Attendance calculations use Indian Standard Time
    (UTC+05:30).

## Tech Stack

  Layer             Technology
  ----------------- --------------------------------
  Backend           Python 3.10+, FastAPI, Uvicorn
  Database          MongoDB 6.0+ or MongoDB Atlas
  Database driver   PyMongo
  Validation        Pydantic v2
  Configuration     python-dotenv
  Frontend          HTML5, CSS, Vanilla JavaScript
  API contract      OpenAPI 3.0.3

## Project Structure

``` text
candidate_kit/
├── app/
│   ├── main.py              # FastAPI application and API endpoints
│   ├── __init__.py
│   └── static/
│       ├── index.html       # Dashboard markup
│       ├── style.css        # Responsive UI styles
│       └── app.js           # Frontend interactions and notifications
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── requirements.txt
├── sample_seed.py           # Load sample data
├── seed_users.py            # Seed user data
├── DATA_MODEL.md            # Data model documentation
├── DECISIONS.md             # Architecture decisions
├── REVIEW.md                # Review notes
├── openapi.yaml             # OpenAPI contract
└── README.md
```

## Requirements

-   Python 3.10 or later
-   MongoDB 6.0+ locally, or a MongoDB Atlas cluster
-   `pip`

## Setup and Run

### 1. Open the project directory

``` bash
cd candidate_kit
```

### 2. Create and activate a virtual environment

**Windows (PowerShell):**

``` powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux:**

``` bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

``` bash
pip install -r requirements.txt
```

### 4. Configure MongoDB

Set the following environment variables for your MongoDB connection:

``` env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For MongoDB Atlas, use your Atlas connection string as `MONGO_URI`. Keep
credentials private and do not commit them to source control.

If you use a `.env` file, place it in the project root:

``` env
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

### 5. Seed sample data

If you want to populate the database with the supplied sample records,
run:

``` bash
python sample_seed.py
```

Check the script before running it against any database containing real
data.

### 6. Start the API and dashboard

From the project root, run:

``` bash
uvicorn app.main:app --reload --port 8000
```

### 7. Open the application

-   **Dashboard:** http://localhost:8000/
-   **Swagger API docs:** http://localhost:8000/docs
-   **ReDoc:** http://localhost:8000/redoc
-   **Health endpoint:** http://localhost:8000/health

## Notifications UI

The dashboard includes a notification bell in the top navigation.

-   A small blue dot appears when at least one notification is unread.
-   Clicking the bell opens or closes the notification panel.
-   Opening the panel marks notifications as read in the current
    frontend implementation.
-   **Mark all as read** clears unread indicators.
-   **Clear all** removes notifications from the current in-memory list.
-   Successful actions and errors can create both a toast message and a
    notification entry.
-   The panel displays the notification title, message, date, and time
    in IST.

**Note:** Notifications are currently stored in browser memory, not
persisted in MongoDB. They reset when the page is reloaded. For
persistent or real-time notifications, connect the UI to a backend
notification endpoint or a WebSocket/SSE stream.

## API and Data Notes

-   The API is implemented in `app/main.py`.
-   The OpenAPI contract is available in `openapi.yaml`, and interactive
    documentation is served by FastAPI.
-   MongoDB indexes are initialized when the application starts.
-   The default database name is `attendance_db`, unless overridden with
    `MONGO_DB`.
-   The frontend is served by the FastAPI application from
    `app/static/`.

## Troubleshooting

### MongoDB connection errors

-   Confirm that MongoDB is running, or that your Atlas cluster is
    available.
-   Verify `MONGO_URI` and `MONGO_DB`.
-   For Atlas, check network access rules and database-user permissions.

### Port 8000 is already in use

Run the server on another port:

``` bash
uvicorn app.main:app --reload --port 8001
```

Then open http://localhost:8001/.

### Dependencies fail to install

Confirm that the active Python version is 3.10 or later, activate the
virtual environment, and retry:

``` bash
python --version
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Security Notes

-   Do not commit `.env` files, database credentials, or production
    secrets.
-   Configure CORS with trusted origins before deploying to production;
    the current application configuration allows all origins.
-   Replace development/demo credentials and review authentication,
    authorization, logging, and data-retention policies before
    production use.
-   Use HTTPS and restrict database access in deployed environments.

## Development

Run with automatic reload during development:

``` bash
uvicorn app.main:app --reload --port 8000
```

Before submitting changes, test the relevant API endpoints, dashboard
interactions, and MongoDB behavior in a development database.

## License

No license information was included in the project files. Add a
`LICENSE` file if you intend to distribute or open-source this project.
