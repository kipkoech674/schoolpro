# School Registration System

A PostgreSQL-backed Flask application for managing:

- Student registration, editable profiles, guardian contacts, and archived records
- Classes, school years, class capacity and class teachers
- Class subjects and weekly timetable slots
- Academic terms with non-overlapping start/end dates
- Student enrollments
- Fee records
- Attendance
- Grades

## Setup

1. Make sure PostgreSQL 18 is installed and running. On this Windows setup, the service is `postgresql-x64-18`.

2. From PowerShell, create the application database, user, and local `.env` configuration:

   ```powershell
   .\setup_postgres.ps1
   ```

   Enter the PostgreSQL administrator username and password when prompted. The password prompt is hidden. The setup script generates a separate random password for the app's restricted database role and stores it in `.env`, which is excluded from Git.

3. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Initialize the database schema:

   ```powershell
   python -m flask --app app db upgrade
   ```

   For an existing database created by the earlier app version, back it up first. If its tables already match the current models, record that existing schema as the initial migration baseline instead:

   ```powershell
   python -m flask --app app db stamp head
   ```

   `stamp head` records the migration version but does not change the schema or data. Use it only for an existing database that already has the current tables; use `db upgrade` for an empty database.

5. Create the first staff account:

   ```powershell
   python -m flask --app app create-user
   ```

   Enter a username, choose `admin`, and provide a password of at least 12 characters. Password input is hidden. Use the same command to provision `registrar`, `teacher`, or `finance` accounts.

6. Start the app:

   ```powershell
   python run.py
   ```

Open `http://127.0.0.1:5000` and sign in. Roles are restricted to the areas needed for their work: registrars manage enrollment and classes, teachers manage attendance and grades, finance staff manage fees, and admins have access to all areas. After the first administrator is created, admins can invite staff from the Staff section; invitees set their own passwords from a one-time email link. The CLI command remains available for initial provisioning and recovery.

All pages require sign-in, and POST forms use CSRF protection. Registrar/admin users manage class assignments on the Classes page. Teachers only see classes assigned to them, and student, attendance, and grade lists are limited to active enrollments in those classes. Teachers cannot record attendance or grades for unassigned classes or students who are not actively enrolled in the selected class. Existing teacher accounts have no class assignments until a registrar assigns them.

## Database migrations

The schema is managed with Flask-Migrate/Alembic; the app no longer creates database tables automatically at startup.

- Apply checked-in migrations: `python -m flask --app app db upgrade`
- Check the current revision: `python -m flask --app app db current`
- After changing models, generate a migration: `python -m flask --app app db migrate -m "Describe the schema change"`. Review the generated file before applying it.
- Apply the new migration: `python -m flask --app app db upgrade`

Student profile details, guardian contacts, emergency contact indicators, and enrollment history are available to registrars and admins. Finance users can see student names and numbers needed for fee work, but the student directory and student CSV export do not expose email or phone. Archiving hides a student from the active list and enrollment options without deleting their existing records. Existing PostgreSQL databases should apply pending migrations with `python -m flask --app app db upgrade`.

Enrollment records can be marked active, completed, or withdrawn. Historical records are retained; re-enrolling creates a new record after the previous enrollment is completed or withdrawn. The database prevents more than one active enrollment for the same student and class, including concurrent submissions. Class capacity is optional; unset capacity remains unlimited.

Finance users create fee records separately from payments. Each payment records its date and can include a method, reference, and notes. Payments must be positive, have at most two decimal places, and cannot exceed the outstanding fee balance. The fee page shows payment history and remaining balances; dashboard payment totals are calculated from the ledger. The payment-ledger migration preserves any pre-existing paid balance as an opening payment with an unknown date rather than assigning a fabricated date.

Teachers can record attendance for a whole class and date at once, update existing entries, and filter attendance by class, date, and (when configured) term. Grade lists can be filtered by class, student, and grading period. Scores are limited to 0–100 with at most two decimal places. Grades can reference a managed term or preserve an older free-text grading period. Report cards show the selected period's average score per subject and the arithmetic average across subjects, plus attendance counts inside the managed term range when linked, or across all recorded dates for legacy periods. Teachers can access reports only for assigned classes.

Registrars manage open and archived school years from the School years page. Archiving retains historical classes and records but prevents new classes, active enrollments, attendance, and grades for that year; an archived year can be reopened. Class names may repeat across different years. Student promotion moves selected active enrollments to a class in another open year, marking the source enrollment completed and creating a new active enrollment without deleting prior history. The Reports page provides enrollment, attendance, and grade summaries by year to registrars and class-scoped teachers; finance users and admins can also review outstanding fee balances. Fees may be assigned to a school year; older/unassigned fees remain visible as unassigned when reporting across all years.

Registrars can set class capacity and class teacher, then manage class subjects and weekly timetable slots from each class page. Active enrollment and promotions respect capacity. Terms are managed separately by school year; date ranges within one year cannot overlap. Grades may use a managed term or retain the legacy free-text period. Report cards use a matching managed term's dates to summarize attendance for that term; unlinked legacy periods continue to show all recorded attendance. Student photos accept JPEG, PNG, or WebP uploads up to the app's 5 MB request limit; images are validated, resized, re-encoded, and stored in the private instance folder rather than public static files. Registrar-only access protects photo retrieval.

Finance users can open each student's fee statement for printing or CSV/PDF download. The attendance page supports filtered printouts and CSV downloads; report cards are printable from the browser.

For staff invitations and password recovery, configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_FROM`, and `PUBLIC_BASE_URL` in `.env` (or the deployment environment). The public base URL must be the URL staff can reach. Admins invite staff using a full name, unique username, work email, and role; invite links expire after 48 hours and are single-use. Pending invites can be resent from the Staff page. Admin-triggered and self-service password-reset links expire after one hour and are single-use; the request screen intentionally gives the same response for known and unknown addresses. SMTP credentials must remain secret and should be supplied through the environment in production.

School year names must describe consecutive years, such as `2027 - 2028` or `2027-28`. Classes and assigned fees reference the school-year record by its stable database ID; the stored year name is retained as a synchronized display value. School-year names can be changed without detaching linked classes or fees. The admin-only Audit log records staff account changes, student and guardian record changes, school-year changes, enrollment and promotion activity, fee payments, attendance, and grades. It retains the latest 200 events in the interface while keeping event records in the database.

## Tests

Run the automated tests with Python's built-in unittest runner:

```powershell
python -m unittest discover -v
```

The tests use an isolated in-memory SQLite database and do not read or modify the configured PostgreSQL database.

Optional real-browser smoke tests cover student registration, enrollment, payments, attendance, grades, and school-year creation. They use a temporary SQLite database and require Chromium:

```powershell
python -m pip install -r requirements-ui.txt
python -m playwright install chromium
python -m unittest discover -s tests/ui -v
```

The regular unittest command does not require Playwright or a browser.

## Runtime security and deployment

- `FLASK_DEBUG` defaults to `false`; do not enable it outside local development. `APP_ENV=production` refuses to start if debug is enabled.
- For a production deployment over HTTPS, set `APP_ENV=production` and `SESSION_COOKIE_SECURE=true` in the deployment environment. Production mode defaults the secure-cookie setting to true and refuses to start if it is explicitly disabled. Secure cookies require HTTPS; they will not be sent over plain HTTP.
- Keep `.env` private and out of source control. Use a unique, randomly generated `SECRET_KEY`; do not reuse passwords or commit environment files.
- Browser sessions expire after 30 minutes of inactivity. Five consecutive failed sign-ins temporarily lock an account for 15 minutes; successful sign-in and password reset clear the counter. Apply the pending database migration before deploying this change.
- `python run.py` uses Flask's development server and is for local development only. The project includes Waitress and a `wsgi:app` entry point for production. On Windows, set production environment variables and start it with `waitress-serve --listen=127.0.0.1:8000 wsgi:app`. Put it behind a maintained HTTPS reverse proxy; keep Waitress bound to localhost so it is not exposed directly to the internet. Configure the proxy, domain, TLS certificates, service supervision, and firewall rules for the actual hosting environment before deployment.
- Restrict access to PostgreSQL and the machine holding school records. Backups contain sensitive student and guardian information; keep them on an access-controlled, encrypted storage location and follow your school's retention policy.

## Database backup and recovery

On this Windows setup, create a custom-format PostgreSQL backup with:

```powershell
.\backup_database.ps1
```

The script reads the PostgreSQL URL from `.env`, passes the password to `pg_dump` through a temporary process environment variable (not a command-line argument), and stores timestamped DPAPI-encrypted backups under `%LOCALAPPDATA%\SchoolRegistration\backups` rather than the project/OneDrive folder. DPAPI uses the current Windows user profile: restore requires the same Windows account/profile on the same machine, so this is not a portable off-machine backup format. Copy encrypted files to approved protected storage and separately maintain a documented recovery plan if that Windows profile or machine is lost. Run backups regularly and test recovery periodically in a separate empty recovery database, never over the live database.

To restore, first create a new, empty recovery database and grant the database role access. Then run the helper with that explicit database name. It refuses to target the database name configured in `.env`, decrypts to a temporary backup file, runs a single-transaction restore, and removes the temporary plaintext file afterward:

```powershell
.\restore_database.ps1 -BackupPath "$env:LOCALAPPDATA\SchoolRegistration\backups\school_registration_YYYYMMDD_HHMMSS.backup.dpapi" -DatabaseName school_registration_recovery
```

The restore helper reads PostgreSQL credentials from `.env` through a temporary process environment variable, not a command-line argument. Creating the empty recovery database may require a PostgreSQL administrator. If PostgreSQL command-line tools are not on `PATH`, the helper checks `C:\Program Files\PostgreSQL\18\bin`. Validate the restored database and its migration revision before using it. The application database backup does not include PostgreSQL roles; recreate/configure the app database role separately if recovering onto another server.
