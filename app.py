import os
import re
import csv
import io
import hashlib
import hmac
import uuid
import smtplib
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from functools import wraps
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, time

import click
from flask import (
    Flask,
    abort,
    flash,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    Response,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from flask_wtf.csrf import CSRFProtect


load_dotenv()


def read_bool_setting(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be set to true or false.")


app = Flask(__name__)
secret_key = os.getenv("SECRET_KEY")
database_url = os.getenv("DATABASE_URL")
app_environment = os.getenv("APP_ENV", "development").strip().lower()
debug_enabled = read_bool_setting("FLASK_DEBUG", False)
secure_cookies = read_bool_setting(
    "SESSION_COOKIE_SECURE",
    app_environment == "production",
)
if not secret_key or not database_url:
    raise RuntimeError(
        "Set SECRET_KEY and DATABASE_URL in .env. "
        "On Windows, run setup_postgres.ps1 to configure them."
    )
if app_environment not in {"development", "production"}:
    raise RuntimeError("APP_ENV must be 'development' or 'production'.")
if app_environment == "production" and debug_enabled:
    raise RuntimeError("FLASK_DEBUG must be false when APP_ENV is production.")
if app_environment == "production" and not secure_cookies:
    raise RuntimeError(
        "SESSION_COOKIE_SECURE must be true when APP_ENV is production."
    )
app.config["SECRET_KEY"] = secret_key
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["DEBUG"] = debug_enabled
app.config["SESSION_COOKIE_SECURE"] = secure_cookies
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

db = SQLAlchemy(app)
Migrate(app, db)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to access the school system."
login_manager.login_message_category = "warning"

ROLES = ("admin", "registrar", "teacher", "finance")
ENROLLMENT_STATUSES = ("active", "completed", "withdrawn")
RECORDS_PER_PAGE = 25
EXPORT_BATCH_SIZE = 500
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCHOOL_YEAR_PATTERN = re.compile(r"^(\d{4})\s*-\s*(\d{2}|\d{4})$")


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(120))
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(254), unique=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    reset_token_hash = db.Column(db.String(64))
    reset_requested_at = db.Column(db.DateTime)
    invitation_token_hash = db.Column(db.String(64))
    invitation_sent_at = db.Column(db.DateTime)
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'registrar', 'teacher', 'finance')",
            name="valid_user_role",
        ),
    )

    @property
    def is_active(self):
        return self.enabled

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AuditEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer)
    summary = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(),
        nullable=False,
        index=True,
    )
    actor = db.relationship("User")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def roles_required(*allowed_roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != "admin" and current_user.role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def requested_page():
    """Return a safe, positive page number for list views."""
    return max(request.args.get("page", 1, type=int) or 1, 1)


def record_audit(action, entity_type, entity_id, summary):
    """Queue a concise record of a sensitive user action for the audit log."""
    db.session.add(
        AuditEvent(
            actor_id=(
                current_user.id
                if has_request_context() and current_user.is_authenticated
                else None
            ),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary[:255],
        )
    )


def validate_school_year_name(value):
    name = value.strip()
    match = SCHOOL_YEAR_PATTERN.fullmatch(name)
    if not match:
        raise ValueError(
            "Enter a school year name such as 2027 - 2028 or 2027-28."
        )
    start_year = int(match.group(1))
    end_text = match.group(2)
    if len(end_text) == 4:
        end_year = int(end_text)
    else:
        end_year = (start_year // 100) * 100 + int(end_text)
        if end_year <= start_year:
            end_year += 100
    if end_year != start_year + 1:
        raise ValueError("A school year must cover consecutive calendar years.")
    return name


def same_school_year(left, right):
    try:
        left_start = validate_school_year_name(left).replace(" ", "").split("-")[0]
        right_start = validate_school_year_name(right).replace(" ", "").split("-")[0]
        return left_start == right_start
    except (AttributeError, ValueError):
        return False


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        now = datetime.now()
        if user and user.locked_until and user.locked_until > now:
            flash("Invalid username or password.", "danger")
            return render_template("login.html")
        if user and user.locked_until and user.locked_until <= now:
            user.failed_login_attempts = 0
            user.locked_until = None
        if user and user.enabled and user.check_password(password):
            user.failed_login_attempts = 0
            user.locked_until = None
            session.clear()
            session.permanent = True
            login_user(user)
            record_audit(
                "login_succeeded",
                "user",
                user.id,
                f"Successful sign-in for {user.username}.",
            )
            db.session.commit()
            next_url = request.args.get("next", "")
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("dashboard"))
        if user and user.enabled:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.failed_login_attempts = 0
                user.locked_until = now + timedelta(minutes=15)
                record_audit(
                    "account_temporarily_locked",
                    "user",
                    user.id,
                    f"Temporarily locked {user.username} after repeated failed sign-in attempts.",
                )
            db.session.commit()
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


def send_password_reset_email(user, token):
    base_url = os.getenv("PUBLIC_BASE_URL", request.url_root.rstrip("/"))
    reset_url = f"{base_url.rstrip('/')}{url_for('password_reset', token=token)}"
    return send_staff_email(
        user.email,
        "Reset your SchoolPro password",
        "A password reset was requested for your staff account. "
        f"Use this one-time link within one hour: {reset_url}\n\n"
        "If you did not request this, you can ignore this message.",
    )


def send_staff_invitation_email(user, token):
    base_url = os.getenv("PUBLIC_BASE_URL", request.url_root.rstrip("/"))
    invitation_url = f"{base_url.rstrip('/')}{url_for('accept_staff_invitation', token=token)}"
    return send_staff_email(
        user.email,
        "Invitation to SchoolPro",
        f"Hello {user.display_name or user.username},\n\n"
        f"An administrator created a SchoolPro staff account for you. "
        f"Your username is {user.username}. Use this one-time link within "
        f"48 hours to choose your password and activate your account: {invitation_url}\n\n"
        "If you were not expecting this invitation, contact your school administrator.",
    )


def send_staff_email(recipient, subject, body):
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    if not host or not sender or not recipient:
        return False
    port = int(os.getenv("SMTP_PORT", "587"))
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo()
        if read_bool_setting("SMTP_USE_TLS", True):
            smtp.starttls()
            smtp.ehlo()
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
    return True


def make_staff_invitation(user):
    token = URLSafeTimedSerializer(app.config["SECRET_KEY"]).dumps(
        {"user_id": user.id, "nonce": os.urandom(16).hex()},
        salt="staff-invitation",
    )
    user.invitation_token_hash = hashlib.sha256(token.encode()).hexdigest()
    user.invitation_sent_at = datetime.now()
    return token


@app.route("/password-reset", methods=["GET", "POST"])
def password_reset_request():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter(db.func.lower(User.email) == email).first() if email else None
        # The response is intentionally the same whether or not the address exists.
        if user and user.enabled and user.email:
            now = datetime.now()
            if not user.reset_requested_at or user.reset_requested_at < now - timedelta(minutes=1):
                token = URLSafeTimedSerializer(app.config["SECRET_KEY"]).dumps(
                    {"user_id": user.id, "nonce": os.urandom(16).hex()},
                    salt="staff-password-reset",
                )
                user.reset_token_hash = hashlib.sha256(token.encode()).hexdigest()
                user.reset_requested_at = now
                db.session.commit()
                try:
                    sent = send_password_reset_email(user, token)
                except (OSError, smtplib.SMTPException, ValueError) as error:
                    app.logger.warning("Password reset email could not be sent: %s", error)
                    sent = False
                if not sent:
                    app.logger.warning("Password reset is configured for %s but SMTP delivery is unavailable.", user.username)
        flash("If an enabled account uses that email address, a reset link will be sent.", "info")
        return redirect(url_for("password_reset_request"))
    return render_template("password_reset_request.html")


@app.route("/password-reset/<token>", methods=["GET", "POST"])
def password_reset(token):
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    try:
        data = serializer.loads(token, salt="staff-password-reset", max_age=3600)
        user = db.session.get(User, int(data["user_id"]))
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        user = None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not user or not user.enabled or not user.reset_token_hash or not hmac.compare_digest(user.reset_token_hash, token_hash):
        flash("That password-reset link is invalid or expired. Request a new one.", "danger")
        return redirect(url_for("password_reset_request"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if len(password) < 12:
            flash("The new password must contain at least 12 characters.", "danger")
        elif password != confirmation:
            flash("The password confirmation does not match.", "danger")
        else:
            user.set_password(password)
            user.reset_token_hash = None
            user.reset_requested_at = None
            user.failed_login_attempts = 0
            user.locked_until = None
            record_audit("password_reset", "user", user.id, f"Completed self-service password reset for {user.username}.")
            db.session.commit()
            flash("Your password has been reset. You can now sign in.", "success")
            return redirect(url_for("login"))
    return render_template("password_reset_form.html")


@app.route("/staff-invitation/<token>", methods=["GET", "POST"])
def accept_staff_invitation(token):
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    try:
        data = serializer.loads(
            token,
            salt="staff-invitation",
            max_age=48 * 60 * 60,
        )
        user = db.session.get(User, int(data["user_id"]))
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        user = None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if (
        not user
        or user.enabled
        or not user.invitation_token_hash
        or not hmac.compare_digest(user.invitation_token_hash, token_hash)
    ):
        flash("That staff invitation is invalid or expired. Contact your administrator for a new link.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if len(password) < 12:
            flash("Your password must contain at least 12 characters.", "danger")
        elif password != confirmation:
            flash("The password confirmation does not match.", "danger")
        else:
            user.set_password(password)
            user.enabled = True
            user.invitation_token_hash = None
            user.invitation_sent_at = None
            user.failed_login_attempts = 0
            user.locked_until = None
            record_audit(
                "staff_invitation_accepted",
                "user",
                user.id,
                f"{user.username} activated their staff account.",
            )
            db.session.commit()
            flash("Your staff account is active. Sign in with your username and new password.", "success")
            return redirect(url_for("login"))
    return render_template("staff_invitation.html", user=user)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirmation = request.form.get("confirm_password", "")
        if not current_user.check_password(current_password):
            flash("Your current password is incorrect.", "danger")
        elif len(new_password) < 12:
            flash("Your new password must contain at least 12 characters.", "danger")
        elif new_password != confirmation:
            flash("The new-password confirmation does not match.", "danger")
        else:
            current_user.set_password(new_password)
            current_user.failed_login_attempts = 0
            current_user.locked_until = None
            record_audit(
                "password_changed",
                "user",
                current_user.id,
                f"Changed the password for {current_user.username}.",
            )
            db.session.commit()
            flash("Your password has been updated.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html")


@app.route("/search")
@roles_required("registrar", "teacher", "finance")
def search():
    query = request.args.get("q", "").strip()[:100]
    students = []
    classes = []
    school_years = []
    fees = []
    if query:
        pattern = f"%{query}%"
        students = (
            active_students_visible_to_current_user()
            .filter(
                db.or_(
                    Student.name.ilike(pattern),
                    Student.student_number.ilike(pattern),
                )
            )
            .order_by(Student.name)
            .limit(10)
            .all()
        )
        if current_user.role in {"admin", "registrar", "teacher"}:
            classes = (
                classes_visible_to_current_user()
                .filter(
                    db.or_(
                        SchoolClass.name.ilike(pattern),
                        SchoolClass.academic_year.ilike(pattern),
                        SchoolClass.room.ilike(pattern),
                    )
                )
                .order_by(SchoolClass.academic_year.desc(), SchoolClass.name)
                .limit(10)
                .all()
            )
        if current_user.role in {"admin", "registrar"}:
            school_years = (
                SchoolYear.query.filter(SchoolYear.name.ilike(pattern))
                .order_by(SchoolYear.name.desc())
                .limit(10)
                .all()
            )
        if current_user.role in {"admin", "finance"}:
            fees = (
                Fee.query.join(Student)
                .filter(
                    db.or_(
                        Fee.description.ilike(pattern),
                        Fee.academic_year.ilike(pattern),
                        Student.name.ilike(pattern),
                        Student.student_number.ilike(pattern),
                    )
                )
                .order_by(Fee.id.desc())
                .limit(10)
                .all()
            )
    return render_template(
        "search.html",
        query=query,
        students=students,
        classes=classes,
        school_years=school_years,
        fees=fees,
    )


@app.route("/notifications")
@login_required
def notifications():
    return render_template("notifications.html")


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


@app.route("/audit-log")
@roles_required("admin")
def audit_log():
    events = AuditEvent.query.order_by(AuditEvent.created_at.desc()).limit(200).all()
    return render_template("audit_log.html", events=events)


@app.route("/staff", methods=["GET", "POST"])
@roles_required("admin")
def staff():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        username = request.form.get("username", "").strip()
        role = request.form.get("role", "").strip().lower()
        email = request.form.get("email", "").strip().lower()
        if not display_name or len(display_name) > 120:
            flash("Enter the staff member's name (up to 120 characters).", "danger")
        elif not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", username):
            flash(
                "Username must be 3–80 characters and use only letters, numbers, dots, hyphens, or underscores.",
                "danger",
            )
        elif role not in ROLES:
            flash("Choose a valid staff role.", "danger")
        elif not email or len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
            flash("Enter a valid work email address; invitations are sent by email.", "danger")
        elif User.query.filter(db.func.lower(User.email) == email).first():
            flash("That email is already assigned to a staff account.", "warning")
        elif User.query.filter_by(username=username).first():
            flash("That username is already in use.", "warning")
        else:
            user = User(
                username=username,
                display_name=display_name,
                role=role,
                email=email,
                enabled=False,
            )
            user.set_password(os.urandom(32).hex())
            db.session.add(user)
            db.session.flush()
            invitation_token = make_staff_invitation(user)
            record_audit(
                "staff_invitation_created",
                "user",
                user.id,
                f"Created a pending {role} invitation for {username}.",
            )
            db.session.commit()
            try:
                sent = send_staff_invitation_email(user, invitation_token)
            except (OSError, smtplib.SMTPException, ValueError) as error:
                app.logger.warning("Staff invitation email could not be sent: %s", error)
                sent = False
            if sent:
                flash(f"Invitation sent to {email}.", "success")
            else:
                flash("The account is pending, but the invitation could not be sent. Check SMTP settings and resend it below.", "warning")
            return redirect(url_for("staff"))
    return render_template("staff.html", users=User.query.order_by(User.username).all())


@app.route("/staff/<int:user_id>/invitation", methods=["POST"])
@roles_required("admin")
def resend_staff_invitation(user_id):
    user = db.get_or_404(User, user_id)
    if user.enabled or not user.invitation_token_hash:
        flash("This account does not have a pending invitation.", "warning")
        return redirect(url_for("staff"))
    if not user.email:
        flash("Add a work email before sending an invitation.", "warning")
        return redirect(url_for("staff"))

    invitation_token = make_staff_invitation(user)
    record_audit(
        "staff_invitation_resent",
        "user",
        user.id,
        f"Resent the pending staff invitation to {user.username}.",
    )
    db.session.commit()
    try:
        sent = send_staff_invitation_email(user, invitation_token)
    except (OSError, smtplib.SMTPException, ValueError) as error:
        app.logger.warning("Staff invitation email could not be resent: %s", error)
        sent = False
    if sent:
        flash(f"A new invitation was sent to {user.email}.", "success")
    else:
        flash("The invitation could not be sent. Check SMTP settings and try again.", "danger")
    return redirect(url_for("staff"))


def would_remove_last_enabled_admin(user, role, enabled):
    if user.role != "admin" or not user.enabled:
        return False
    if role == "admin" and enabled:
        return False
    return not User.query.filter(
        User.role == "admin",
        User.enabled.is_(True),
        User.id != user.id,
    ).first()


@app.route("/staff/<int:user_id>/update", methods=["POST"])
@roles_required("admin")
def update_staff(user_id):
    user = db.get_or_404(User, user_id)
    old_display_name = user.display_name
    old_email = user.email
    pending_invitation = bool(user.invitation_token_hash)
    display_name = request.form.get("display_name", "").strip()
    role = request.form.get("role", "").strip().lower()
    email = request.form.get("email", "").strip().lower()
    enabled = request.form.get("enabled") == "on"
    if not display_name or len(display_name) > 120:
        flash("Enter a staff name of up to 120 characters.", "danger")
    elif pending_invitation and enabled:
        flash("A pending account can only be enabled by accepting its invitation.", "warning")
    elif pending_invitation and not email:
        flash("A pending invitation must have a work email address.", "danger")
    elif role not in ROLES:
        flash("Choose a valid staff role.", "danger")
    elif email and (len(email) > 254 or not EMAIL_PATTERN.fullmatch(email)):
        flash("Enter a valid staff email address.", "danger")
    elif email and User.query.filter(
        db.func.lower(User.email) == email, User.id != user.id
    ).first():
        flash("That email is already assigned to another staff account.", "warning")
    elif user.id == current_user.id and (
        not enabled or role != current_user.role
    ):
        flash("You cannot change your own role or disable your own account.", "danger")
    elif would_remove_last_enabled_admin(user, role, enabled):
        flash("At least one enabled administrator account must remain.", "danger")
    else:
        user.display_name = display_name
        user.role = role
        user.enabled = enabled
        user.email = email or None
        record_audit(
            "staff_updated",
            "user",
            user.id,
            f"Updated {user.username}: role {role}, {'enabled' if enabled else 'disabled'}.",
        )
        invitation_token = None
        if pending_invitation and (
            display_name != old_display_name or (email or None) != old_email
        ):
            invitation_token = make_staff_invitation(user)
        db.session.commit()
        if invitation_token:
            try:
                sent = send_staff_invitation_email(user, invitation_token)
            except (OSError, smtplib.SMTPException, ValueError) as error:
                app.logger.warning("Updated staff invitation could not be sent: %s", error)
                sent = False
            if sent:
                flash(f"Updated the account and sent a fresh invitation to {user.email}.", "success")
            else:
                flash("The account was updated, but its fresh invitation could not be sent. Resend it after checking SMTP settings.", "warning")
        else:
            flash(f"Updated {user.username}'s account.", "success")
    return redirect(url_for("staff"))


@app.route("/staff/<int:user_id>/password", methods=["POST"])
@roles_required("admin")
def reset_staff_password(user_id):
    user = db.get_or_404(User, user_id)
    if not user.enabled:
        flash("Only an active staff member can receive a password reset link.", "warning")
    elif not user.email:
        flash("Add a work email before sending a password reset link.", "warning")
    else:
        token = URLSafeTimedSerializer(app.config["SECRET_KEY"]).dumps(
            {"user_id": user.id, "nonce": os.urandom(16).hex()},
            salt="staff-password-reset",
        )
        user.reset_token_hash = hashlib.sha256(token.encode()).hexdigest()
        user.reset_requested_at = datetime.now()
        record_audit(
            "password_reset_requested_by_admin",
            "user",
            user.id,
            f"An administrator requested a password reset for {user.username}.",
        )
        db.session.commit()
        try:
            sent = send_password_reset_email(user, token)
        except (OSError, smtplib.SMTPException, ValueError) as error:
            app.logger.warning("Staff password reset email could not be sent: %s", error)
            sent = False
        if sent:
            flash(f"A password reset link was sent to {user.email}.", "success")
        else:
            flash("The reset link could not be sent. Check SMTP settings and try again.", "danger")
    return redirect(url_for("staff"))


@app.cli.command("create-user")
@click.option("--username", prompt=True)
@click.option("--role", type=click.Choice(ROLES, case_sensitive=False), prompt=True)
def create_user(username, role):
    """Create a staff login account."""
    username = username.strip()
    if not username:
        raise click.ClickException("Username cannot be empty.")
    if User.query.filter_by(username=username).first():
        raise click.ClickException(f"User '{username}' already exists.")

    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 12:
        raise click.ClickException("Password must contain at least 12 characters.")

    user = User(username=username, role=role.lower())
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    record_audit(
        "staff_created",
        "user",
        user.id,
        f"Created {role.lower()} account for {username} through the CLI.",
    )
    db.session.commit()
    click.echo(f"Created {role.lower()} user '{username}'.")


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_number = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    date_of_birth = db.Column(db.Date)
    gender = db.Column(db.String(20))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    address = db.Column(db.Text)
    photo_filename = db.Column(db.String(80))
    active = db.Column(db.Boolean, default=True, nullable=False)
    guardians = db.relationship(
        "Guardian",
        backref="student",
        cascade="all, delete-orphan",
        order_by="Guardian.id",
    )
    __table_args__ = (
        Index("ix_student_active_name", "active", "name", "id"),
    )


class Guardian(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    relationship = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(120))
    is_emergency_contact = db.Column(db.Boolean, default=False, nullable=False)


class SchoolYear(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), unique=True, nullable=False)
    archived = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(), nullable=False)


def school_year_for_name(name):
    if not name:
        return None
    pending_year = next(
        (
            item
            for item in db.session.new
            if isinstance(item, SchoolYear) and item.name == name
        ),
        None,
    )
    return pending_year or SchoolYear.query.filter_by(name=name).first()


class SchoolClass(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    academic_year = db.Column(db.String(20), nullable=False)
    school_year_id = db.Column(
        db.Integer,
        db.ForeignKey("school_year.id"),
        nullable=False,
        index=True,
    )
    room = db.Column(db.String(30))
    capacity = db.Column(db.Integer)
    class_teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    school_year = db.relationship("SchoolYear", backref="classes")
    class_teacher = db.relationship("User", foreign_keys=[class_teacher_id])
    teacher_assignments = db.relationship(
        "TeacherClassAssignment",
        backref="school_class",
        cascade="all, delete-orphan",
        order_by="TeacherClassAssignment.id",
    )
    __table_args__ = (
        UniqueConstraint("name", "school_year_id", name="unique_class_name_year"),
    )

    def __init__(self, **kwargs):
        if "school_year" not in kwargs and "school_year_id" not in kwargs:
            year = school_year_for_name(kwargs.get("academic_year"))
            if year:
                kwargs["school_year"] = year
        super().__init__(**kwargs)


class SchoolTerm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    school_year_id = db.Column(db.Integer, db.ForeignKey("school_year.id"), nullable=False)
    name = db.Column(db.String(40), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    school_year = db.relationship("SchoolYear", backref="terms")
    __table_args__ = (
        UniqueConstraint("school_year_id", "name", name="unique_term_name_year"),
        CheckConstraint("start_date <= end_date", name="valid_term_date_range"),
    )


class ClassSubject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey("school_class.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    school_class = db.relationship(
        "SchoolClass", backref=db.backref("subjects", cascade="all, delete-orphan")
    )
    teacher = db.relationship("User", foreign_keys=[teacher_id])
    __table_args__ = (
        UniqueConstraint("class_id", "name", name="unique_subject_per_class"),
    )


class TimetableSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey("school_class.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("class_subject.id"), nullable=False)
    weekday = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    room = db.Column(db.String(30))
    school_class = db.relationship(
        "SchoolClass", backref=db.backref("timetable", cascade="all, delete-orphan")
    )
    subject = db.relationship("ClassSubject")
    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="valid_timetable_weekday"),
        CheckConstraint("start_time < end_time", name="valid_timetable_time_range"),
    )


class TeacherClassAssignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_class.id"), nullable=False)
    teacher = db.relationship("User", backref="class_assignments")
    __table_args__ = (
        UniqueConstraint("teacher_id", "class_id", name="unique_teacher_class"),
    )


class Enrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_class.id"), nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)
    student = db.relationship("Student", backref="enrollments")
    school_class = db.relationship("SchoolClass", backref="enrollments")
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'withdrawn')",
            name="valid_enrollment_status",
        ),
        Index(
            "unique_active_enrollment",
            "student_id",
            "class_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ix_enrollment_class_status_student",
            "class_id",
            "status",
            "student_id",
        ),
        Index(
            "ix_enrollment_student_status_class",
            "student_id",
            "status",
            "class_id",
        ),
    )


class Fee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    description = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    due_date = db.Column(db.Date)
    academic_year = db.Column(db.String(20))
    school_year_id = db.Column(
        db.Integer,
        db.ForeignKey("school_year.id"),
        index=True,
    )
    student = db.relationship("Student", backref="fees")
    school_year = db.relationship("SchoolYear")
    payments = db.relationship(
        "Payment",
        backref="fee",
        cascade="all, delete-orphan",
        order_by="Payment.id",
    )

    @property
    def paid_amount(self):
        return sum((payment.amount for payment in self.payments), Decimal("0"))

    def __init__(self, **kwargs):
        if "school_year" not in kwargs and "school_year_id" not in kwargs:
            year = school_year_for_name(kwargs.get("academic_year"))
            if year:
                kwargs["school_year"] = year
        super().__init__(**kwargs)

    __table_args__ = (
        Index("ix_fee_student_id_id", "student_id", "id"),
    )


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fee_id = db.Column(
        db.Integer,
        db.ForeignKey("fee.id"),
        nullable=False,
        index=True,
    )
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.Date)
    payment_method = db.Column(db.String(40))
    reference = db.Column(db.String(120))
    notes = db.Column(db.Text)
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_payment_amount"),
    )


class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_class.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False)
    student = db.relationship("Student", backref="attendance")
    school_class = db.relationship("SchoolClass", backref="attendance")
    __table_args__ = (
        UniqueConstraint("student_id", "class_id", "date", name="unique_attendance"),
        CheckConstraint(
            "status IN ('Present', 'Absent', 'Late', 'Excused')",
            name="valid_attendance_status",
        ),
        Index("ix_attendance_class_date_student", "class_id", "date", "student_id"),
    )


class Grade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_class.id"), nullable=False)
    subject = db.Column(db.String(80), nullable=False)
    term = db.Column(db.String(40), nullable=False)
    term_id = db.Column(db.Integer, db.ForeignKey("school_term.id"))
    score = db.Column(db.Numeric(5, 2), nullable=False)
    student = db.relationship("Student", backref="grades")
    school_class = db.relationship("SchoolClass", backref="grades")
    school_term = db.relationship("SchoolTerm")
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="valid_grade_score"),
        Index("ix_grade_class_id_id", "class_id", "id"),
        Index("ix_grade_class_student_term", "class_id", "student_id", "term"),
    )


def parse_date(value):
    from datetime import date

    return date.fromisoformat(value) if value else None


def teacher_class_ids():
    if current_user.role != "teacher":
        return None
    return [
        row.class_id
        for row in TeacherClassAssignment.query.filter_by(
            teacher_id=current_user.id
        ).all()
    ]


def classes_visible_to_current_user():
    query = SchoolClass.query
    class_ids = teacher_class_ids()
    if class_ids is not None:
        query = query.filter(SchoolClass.id.in_(class_ids))
    return query


def active_students_visible_to_current_user():
    query = Student.query.filter_by(active=True)
    class_ids = teacher_class_ids()
    if class_ids is not None:
        query = (
            query.join(Enrollment, Enrollment.student_id == Student.id)
            .filter(
                Enrollment.status == "active",
                Enrollment.class_id.in_(class_ids),
            )
            .distinct()
        )
    return query


def enrolled_students_for_classes(class_ids):
    query = (
        Student.query.join(Enrollment, Enrollment.student_id == Student.id)
        .filter(Student.active.is_(True), Enrollment.status == "active")
    )
    if class_ids is not None:
        query = query.filter(Enrollment.class_id.in_(class_ids))
    return query.distinct().order_by(Student.name)


def ensure_teacher_class_access(class_id):
    class_ids = teacher_class_ids()
    if class_ids is not None and class_id not in class_ids:
        abort(403)


@app.route("/api/student-options")
@roles_required("registrar", "teacher", "finance")
def student_options():
    """Return a small, permission-scoped result set for student pickers."""
    search = request.args.get("q", "").strip()[:120]
    if len(search) < 2:
        return jsonify(results=[])

    class_id = request.args.get("class_id", "").strip()
    if class_id:
        try:
            class_id = int(class_id)
        except ValueError:
            return jsonify(results=[]), 400
        if not classes_visible_to_current_user().filter_by(id=class_id).first():
            abort(403)
        statuses = ("active", "completed", "withdrawn") if request.args.get("include_past") == "1" else ("active",)
        enrolled_ids = db.session.query(Enrollment.student_id).filter(
            Enrollment.class_id == class_id,
            Enrollment.status.in_(statuses),
        )
        query = Student.query.filter(
            Student.active.is_(True),
            Student.id.in_(enrolled_ids),
        )
    else:
        query = active_students_visible_to_current_user()
    if not class_id and request.args.get("enrolled") == "1":
        enrolled_ids = db.session.query(Enrollment.student_id).filter(
            Enrollment.status == "active",
        )
        query = query.filter(Student.id.in_(enrolled_ids))

    escaped_search = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    prefix = f"{escaped_search}%"
    students = (
        query.filter(
            db.or_(
                db.func.lower(Student.name).like(prefix, escape="\\"),
                db.func.lower(Student.student_number).like(prefix, escape="\\"),
            )
        )
        .distinct()
        .order_by(db.func.lower(Student.name), Student.id)
        .limit(20)
        .all()
    )
    return jsonify(results=[
        {"id": student.id, "label": f"{student.student_number} — {student.name}"}
        for student in students
    ])


def class_year_is_open(class_id):
    return (
        db.session.query(SchoolClass.id)
        .join(SchoolYear, SchoolClass.school_year_id == SchoolYear.id)
        .filter(SchoolClass.id == class_id, SchoolYear.archived.is_(False))
        .first()
        is not None
    )


@app.route("/")
@roles_required("registrar", "teacher", "finance")
def dashboard():
    if current_user.role == "teacher":
        class_ids = teacher_class_ids()
        student_count = active_students_visible_to_current_user().count()
        class_count = len(class_ids)
        enrollment_count = Enrollment.query.filter(
            Enrollment.status == "active",
            Enrollment.class_id.in_(class_ids),
        ).count()
        fee_total = fee_paid = None
    else:
        student_count = Student.query.filter_by(active=True).count()
        class_count = SchoolClass.query.count()
        enrollment_count = Enrollment.query.filter_by(status="active").count()
        fee_total = db.session.query(
            db.func.coalesce(db.func.sum(Fee.amount), 0)
        ).scalar()
        fee_paid = db.session.query(
            db.func.coalesce(db.func.sum(Payment.amount), 0)
        ).scalar()
    return render_template(
        "dashboard.html",
        student_count=student_count,
        class_count=class_count,
        enrollment_count=enrollment_count,
        fee_total=fee_total,
        fee_paid=fee_paid,
    )


@app.route("/students")
@roles_required("registrar", "teacher", "finance")
def students():
    search = request.args.get("q", "").strip()
    include_archived = (
        request.args.get("archived") == "1"
        and current_user.role in ("admin", "registrar")
    )
    if current_user.role == "teacher":
        include_archived = False
        query = active_students_visible_to_current_user()
    else:
        query = Student.query
    if not include_archived:
        query = query.filter(Student.active.is_(True))
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            db.or_(Student.name.ilike(pattern), Student.student_number.ilike(pattern))
        )
    student_pagination = query.order_by(Student.name).paginate(
        page=requested_page(),
        per_page=RECORDS_PER_PAGE,
        error_out=False,
    )
    return render_template(
        "students.html",
        students=student_pagination.items,
        pagination=student_pagination,
        include_archived=include_archived,
    )


@app.route("/students/new", methods=["GET", "POST"])
@roles_required("registrar")
def new_student():
    if request.method == "POST":
        try:
            student = student_from_form()
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("new_student"))
        if Student.query.filter_by(student_number=student.student_number).first():
            flash("That student number already exists.", "danger")
            return redirect(url_for("new_student"))
        try:
            photo_filename = save_student_photo(request.files.get("photo"))
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("new_student"))
        student.photo_filename = photo_filename
        db.session.add(student)
        try:
            db.session.flush()
            record_audit(
                "student_created",
                "student",
                student.id,
                f"Registered student {student.name} ({student.student_number}).",
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            delete_student_photo(photo_filename)
            flash("That student number already exists.", "danger")
            return redirect(url_for("new_student"))
        flash("Student registered successfully.", "success")
        return redirect(url_for("students"))
    return render_template(
        "student_form.html",
        student=None,
        today=date.today().isoformat(),
    )


def student_from_form(student=None):
    data = validate_student_form()
    if student is None:
        student = Student()
    for field, value in data.items():
        setattr(student, field, value)
    return student


def student_photo_directory():
    directory = os.path.join(app.instance_path, "student_photos")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


def save_student_photo(upload):
    if not upload or not upload.filename:
        return None
    try:
        from PIL import Image, UnidentifiedImageError
        upload.stream.seek(0)
        image = Image.open(upload.stream)
        if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 20_000_000:
            raise ValueError("Upload a JPEG, PNG, or WebP student photo under 20 megapixels.")
        image.verify()
        upload.stream.seek(0)
        image = Image.open(upload.stream).convert("RGB")
        image.thumbnail((1200, 1200))
        filename = f"{uuid.uuid4().hex}.jpg"
        image.save(os.path.join(student_photo_directory(), filename), "JPEG", quality=86, optimize=True)
        return filename
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("That file is not a valid JPEG, PNG, or WebP image.") from error


def delete_student_photo(filename):
    if not filename:
        return
    path = os.path.join(student_photo_directory(), filename)
    if os.path.isfile(path):
        os.remove(path)


@app.route("/students/<int:student_id>/photo")
@roles_required("registrar")
def student_photo(student_id):
    student = db.get_or_404(Student, student_id)
    if not student.photo_filename:
        abort(404)
    return send_from_directory(student_photo_directory(), student.photo_filename, max_age=3600)


def validate_student_form():
    student_number = request.form.get("student_number", "").strip()
    name = request.form.get("name", "").strip()
    if not student_number or not name:
        raise ValueError("Student number and name are required.")
    if len(student_number) > 30:
        raise ValueError("Student number must be 30 characters or fewer.")
    if len(name) > 120:
        raise ValueError("Student name must be 120 characters or fewer.")

    date_value = request.form.get("date_of_birth", "").strip()
    try:
        date_of_birth = parse_date(date_value)
    except ValueError as error:
        raise ValueError("Enter a valid date of birth.") from error
    if date_of_birth and date_of_birth > date.today():
        raise ValueError("Date of birth cannot be in the future.")

    gender = request.form.get("gender", "").strip()
    if gender and gender not in {"Female", "Male", "Other"}:
        raise ValueError("Select a valid gender option.")

    email = request.form.get("email", "").strip()
    if email and (len(email) > 120 or not EMAIL_PATTERN.fullmatch(email)):
        raise ValueError("Enter a valid email address (maximum 120 characters).")

    phone = request.form.get("phone", "").strip()
    if len(phone) > 30:
        raise ValueError("Phone number must be 30 characters or fewer.")

    return {
        "student_number": student_number,
        "name": name,
        "date_of_birth": date_of_birth,
        "gender": gender or None,
        "email": email or None,
        "phone": phone or None,
        "address": request.form.get("address", "").strip() or None,
    }


@app.route("/students/<int:student_id>")
@roles_required("registrar")
def student_detail(student_id):
    student = db.get_or_404(Student, student_id)
    enrollment_history = (
        Enrollment.query.options(joinedload(Enrollment.school_class))
        .filter(Enrollment.student_id == student.id)
        .join(SchoolClass)
        .order_by(SchoolClass.academic_year.desc(), SchoolClass.name)
        .all()
    )
    return render_template(
        "student_detail.html",
        student=student,
        enrollment_history=enrollment_history,
    )


@app.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@roles_required("registrar")
def edit_student(student_id):
    student = db.get_or_404(Student, student_id)
    if request.method == "POST":
        try:
            data = validate_student_form()
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("edit_student", student_id=student.id))
        duplicate = Student.query.filter(
            Student.student_number == data["student_number"],
            Student.id != student.id,
        ).first()
        if duplicate:
            flash("That student number already exists.", "danger")
            return redirect(url_for("edit_student", student_id=student.id))
        try:
            new_photo_filename = save_student_photo(request.files.get("photo"))
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("edit_student", student_id=student.id))
        previous_photo = student.photo_filename
        for field, value in data.items():
            setattr(student, field, value)
        if new_photo_filename:
            student.photo_filename = new_photo_filename
        try:
            record_audit(
                "student_updated",
                "student",
                student.id,
                f"Updated student profile for {student.name} ({student.student_number}).",
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            delete_student_photo(new_photo_filename)
            flash("That student number already exists.", "danger")
            return redirect(url_for("edit_student", student_id=student.id))
        if new_photo_filename:
            delete_student_photo(previous_photo)
        flash("Student profile updated.", "success")
        return redirect(url_for("student_detail", student_id=student.id))
    return render_template(
        "student_form.html",
        student=student,
        today=date.today().isoformat(),
    )


@app.route("/students/<int:student_id>/guardians", methods=["POST"])
@roles_required("registrar")
def add_guardian(student_id):
    student = db.get_or_404(Student, student_id)
    name = request.form.get("name", "").strip()
    relationship = request.form.get("relationship", "").strip()
    phone = request.form.get("phone", "").strip()
    if not name or not relationship or not phone:
        flash("Guardian name, relationship, and phone are required.", "danger")
        return redirect(url_for("student_detail", student_id=student.id))
    guardian = Guardian(
        student=student,
        name=name,
        relationship=relationship,
        phone=phone,
        email=request.form.get("email", "").strip() or None,
        is_emergency_contact=request.form.get("is_emergency_contact") == "on",
    )
    db.session.add(guardian)
    db.session.flush()
    record_audit(
        "guardian_added",
        "student",
        student.id,
        f"Added a guardian contact for {student.name} ({student.student_number}).",
    )
    db.session.commit()
    flash("Guardian contact added.", "success")
    return redirect(url_for("student_detail", student_id=student.id))


@app.route(
    "/students/<int:student_id>/guardians/<int:guardian_id>/delete",
    methods=["POST"],
)
@roles_required("registrar")
def delete_guardian(student_id, guardian_id):
    student = db.get_or_404(Student, student_id)
    guardian = Guardian.query.filter_by(
        id=guardian_id, student_id=student.id
    ).first_or_404()
    db.session.delete(guardian)
    record_audit(
        "guardian_removed",
        "student",
        student.id,
        f"Removed a guardian contact for {student.name} ({student.student_number}).",
    )
    db.session.commit()
    flash("Guardian contact removed.", "success")
    return redirect(url_for("student_detail", student_id=student.id))


@app.route("/students/<int:student_id>/archive", methods=["POST"])
@roles_required("registrar")
def archive_student(student_id):
    student = db.get_or_404(Student, student_id)
    student.active = False
    record_audit(
        "student_archived",
        "student",
        student.id,
        f"Archived student {student.name} ({student.student_number}).",
    )
    db.session.commit()
    flash("Student archived. Their enrollment and academic records were retained.", "success")
    return redirect(url_for("student_detail", student_id=student.id))


@app.route("/students/<int:student_id>/restore", methods=["POST"])
@roles_required("registrar")
def restore_student(student_id):
    student = db.get_or_404(Student, student_id)
    student.active = True
    record_audit(
        "student_restored",
        "student",
        student.id,
        f"Restored student {student.name} ({student.student_number}).",
    )
    db.session.commit()
    flash("Student restored.", "success")
    return redirect(url_for("student_detail", student_id=student.id))


@app.route("/classes", methods=["GET", "POST"])
@roles_required("registrar")
def classes():
    if request.method == "POST":
        name = request.form["name"].strip()
        academic_year = request.form["academic_year"].strip()
        try:
            capacity = int(request.form["capacity"]) if request.form.get("capacity") else None
            class_teacher_id = (
                int(request.form["class_teacher_id"])
                if request.form.get("class_teacher_id") else None
            )
        except ValueError:
            flash("Enter a valid class capacity and class teacher.", "danger")
            return redirect(url_for("classes"))
        school_year = SchoolYear.query.filter_by(
            name=academic_year,
            archived=False,
        ).first()
        class_teacher = (
            User.query.filter_by(id=class_teacher_id, role="teacher", enabled=True).first()
            if class_teacher_id else None
        )
        if (
            not name or len(name) > 50 or not school_year
            or (capacity is not None and capacity < 1)
            or (class_teacher_id is not None and not class_teacher)
        ):
            flash("Enter a class name, open school year, valid capacity, and enabled teacher.", "danger")
            return redirect(url_for("classes"))
        db.session.add(SchoolClass(
            name=name,
            academic_year=academic_year,
            school_year=school_year,
            room=request.form.get("room", "").strip() or None,
            capacity=capacity,
            class_teacher=class_teacher,
        ))
        try:
            db.session.flush()
            school_class = SchoolClass.query.filter_by(
                name=name,
                academic_year=academic_year,
            ).one()
            record_audit(
                "class_created",
                "school_class",
                school_class.id,
                f"Created class {name} for school year {academic_year}.",
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That class already exists in this school year.", "warning")
            return redirect(url_for("classes"))
        flash("Class created.", "success")
        return redirect(url_for("classes"))
    return render_template(
        "classes.html",
        classes=SchoolClass.query.order_by(
            SchoolClass.academic_year.desc(),
            SchoolClass.name,
        ).all(),
        school_years=SchoolYear.query.order_by(SchoolYear.name.desc()).all(),
        teachers=User.query.filter_by(role="teacher", enabled=True)
        .order_by(User.username)
        .all(),
        subjects=ClassSubject.query.order_by(ClassSubject.name).all(),
    )


@app.route("/classes/<int:class_id>", methods=["GET", "POST"])
@roles_required("registrar")
def class_detail(class_id):
    school_class = db.get_or_404(SchoolClass, class_id)
    action = request.form.get("action") if request.method == "POST" else None
    if request.method == "POST" and action == "update-class":
        try:
            capacity = int(request.form["capacity"]) if request.form.get("capacity") else None
            teacher_id = int(request.form["class_teacher_id"]) if request.form.get("class_teacher_id") else None
        except (KeyError, ValueError):
            flash("Enter a valid class capacity and class teacher.", "danger")
            return redirect(url_for("class_detail", class_id=class_id))
        teacher = (
            User.query.filter_by(id=teacher_id, role="teacher", enabled=True).first()
            if teacher_id else None
        )
        active_count = Enrollment.query.filter_by(class_id=class_id, status="active").count()
        if (capacity is not None and capacity < max(active_count, 1)) or (teacher_id and not teacher):
            flash("Capacity cannot be below current enrollment; choose an enabled teacher.", "danger")
            return redirect(url_for("class_detail", class_id=class_id))
        school_class.capacity = capacity
        school_class.class_teacher = teacher
        school_class.room = request.form.get("room", "").strip() or None
        record_audit("class_updated", "school_class", class_id, f"Updated class settings for {school_class.name}.")
        db.session.commit()
        flash("Class settings updated.", "success")
        return redirect(url_for("class_detail", class_id=class_id))

    if request.method == "POST" and action == "add-subject":
        name = request.form.get("name", "").strip()
        try:
            teacher_id = int(request.form["teacher_id"]) if request.form.get("teacher_id") else None
        except ValueError:
            teacher_id = -1
        teacher = (
            User.query.filter_by(id=teacher_id, role="teacher", enabled=True).first()
            if teacher_id and teacher_id > 0 else None
        )
        if not name or len(name) > 80 or (teacher_id and not teacher):
            flash("Enter a subject name and optionally select an enabled teacher.", "danger")
            return redirect(url_for("class_detail", class_id=class_id))
        db.session.add(ClassSubject(class_id=class_id, name=name, teacher=teacher))
        try:
            db.session.flush()
            record_audit("class_subject_added", "school_class", class_id, f"Added subject {name} to {school_class.name}.")
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That subject already exists for this class.", "warning")
            return redirect(url_for("class_detail", class_id=class_id))
        flash("Subject added.", "success")
        return redirect(url_for("class_detail", class_id=class_id))

    if request.method == "POST" and action == "add-slot":
        try:
            subject_id = int(request.form["subject_id"])
            weekday = int(request.form["weekday"])
            start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
            end_time = datetime.strptime(request.form["end_time"], "%H:%M").time()
        except (KeyError, ValueError):
            flash("Enter a valid subject, weekday, and time range.", "danger")
            return redirect(url_for("class_detail", class_id=class_id))
        subject = ClassSubject.query.filter_by(id=subject_id, class_id=class_id).first()
        if not subject or weekday not in range(7) or start_time >= end_time:
            flash("Select a subject and valid timetable time range.", "danger")
            return redirect(url_for("class_detail", class_id=class_id))
        overlap = TimetableSlot.query.filter(
            TimetableSlot.class_id == class_id,
            TimetableSlot.weekday == weekday,
            TimetableSlot.start_time < end_time,
            TimetableSlot.end_time > start_time,
        ).first()
        if overlap:
            flash("That time overlaps another class slot.", "warning")
            return redirect(url_for("class_detail", class_id=class_id))
        slot = TimetableSlot(
            class_id=class_id,
            subject_id=subject.id,
            weekday=weekday,
            start_time=start_time,
            end_time=end_time,
            room=request.form.get("room", "").strip() or None,
        )
        db.session.add(slot)
        db.session.flush()
        record_audit("timetable_slot_added", "school_class", class_id, f"Added a {subject.name} timetable slot for {school_class.name}.")
        db.session.commit()
        flash("Timetable slot added.", "success")
        return redirect(url_for("class_detail", class_id=class_id))

    return render_template(
        "class_detail.html",
        school_class=school_class,
        teachers=User.query.filter_by(role="teacher", enabled=True).order_by(User.username).all(),
        weekdays=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        slots=TimetableSlot.query.filter_by(class_id=class_id).order_by(TimetableSlot.weekday, TimetableSlot.start_time).all(),
    )


@app.route("/timetable/<int:slot_id>/delete", methods=["POST"])
@roles_required("registrar")
def delete_timetable_slot(slot_id):
    slot = db.get_or_404(TimetableSlot, slot_id)
    class_id = slot.class_id
    record_audit("timetable_slot_deleted", "school_class", class_id, f"Deleted a {slot.subject.name} timetable slot.")
    db.session.delete(slot)
    db.session.commit()
    flash("Timetable slot deleted.", "success")
    return redirect(url_for("class_detail", class_id=class_id))


@app.route("/school-years", methods=["GET", "POST"])
@roles_required("registrar")
def school_years():
    if request.method == "POST":
        try:
            name = validate_school_year_name(request.form.get("name", ""))
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("school_years"))
        if len(name) > 20:
            flash("Enter a school year name of 20 characters or fewer.", "danger")
            return redirect(url_for("school_years"))
        if any(same_school_year(year.name, name) for year in SchoolYear.query.all()):
            flash("That school year already exists.", "warning")
            return redirect(url_for("school_years"))
        school_year = SchoolYear(name=name)
        db.session.add(school_year)
        db.session.flush()
        record_audit(
            "school_year_created",
            "school_year",
            school_year.id,
            f"Created school year {school_year.name}.",
        )
        db.session.commit()
        flash("School year created.", "success")
        return redirect(url_for("school_years"))
    return render_template(
        "school_years.html",
        school_years=SchoolYear.query.order_by(SchoolYear.name.desc()).all(),
    )


@app.route("/terms", methods=["GET", "POST"])
@roles_required("registrar")
def terms():
    if request.method == "POST":
        try:
            year_id = int(request.form.get("school_year_id", ""))
            term_id = int(request.form["term_id"]) if request.form.get("term_id") else None
            start_date = date.fromisoformat(request.form.get("start_date", ""))
            end_date = date.fromisoformat(request.form.get("end_date", ""))
        except ValueError:
            flash("Select a school year and valid start/end dates.", "danger")
            return redirect(url_for("terms"))
        name = request.form.get("name", "").strip()
        year = db.session.get(SchoolYear, year_id)
        term = db.session.get(SchoolTerm, term_id) if term_id else SchoolTerm()
        if not year or year.archived or not name or len(name) > 40 or start_date > end_date:
            flash("Choose an open school year, a term name, and a valid date range.", "danger")
            return redirect(url_for("terms"))
        if term_id and not term:
            abort(404)
        if term_id and Grade.query.join(SchoolClass, Grade.class_id == SchoolClass.id).filter(
            Grade.term_id == term.id,
            SchoolClass.school_year_id != year.id,
        ).first():
            flash("This term is linked to grades in its current school year and cannot be moved.", "warning")
            return redirect(url_for("terms", edit=term.id))
        overlap = SchoolTerm.query.filter(
            SchoolTerm.school_year_id == year.id,
            SchoolTerm.id != (term.id if term_id else -1),
            SchoolTerm.start_date <= end_date,
            SchoolTerm.end_date >= start_date,
        ).first()
        duplicate = SchoolTerm.query.filter(
            SchoolTerm.school_year_id == year.id,
            SchoolTerm.name.ilike(name),
            SchoolTerm.id != (term.id if term_id else -1),
        ).first()
        if overlap:
            flash("Term date ranges in one school year cannot overlap.", "danger")
            return redirect(url_for("terms"))
        if duplicate:
            flash("A term with that name already exists in this school year.", "warning")
            return redirect(url_for("terms"))
        term.name = name
        term.school_year = year
        term.start_date = start_date
        term.end_date = end_date
        if term_id:
            for linked_grade in Grade.query.filter_by(term_id=term.id).all():
                linked_grade.term = name
        db.session.add(term)
        db.session.flush()
        record_audit("school_term_saved", "school_term", term.id, f"Saved {name} for {year.name}.")
        db.session.commit()
        flash("Term saved.", "success")
        return redirect(url_for("terms"))
    term_rows = SchoolTerm.query.join(SchoolYear).order_by(SchoolYear.name.desc(), SchoolTerm.start_date).all()
    grade_counts = {
        term.id: Grade.query.filter_by(term_id=term.id).count()
        for term in term_rows
    }
    return render_template(
        "terms.html",
        years=SchoolYear.query.order_by(SchoolYear.name.desc()).all(),
        terms=term_rows,
        grade_counts=grade_counts,
        editing=db.session.get(SchoolTerm, request.args.get("edit", type=int)) if request.args.get("edit", type=int) else None,
    )


@app.route("/terms/<int:term_id>/delete", methods=["POST"])
@roles_required("registrar")
def delete_term(term_id):
    term = db.get_or_404(SchoolTerm, term_id)
    if Grade.query.filter_by(term_id=term.id).first():
        flash("This term is linked to grades and cannot be deleted.", "warning")
    else:
        record_audit("school_term_deleted", "school_term", term.id, f"Deleted term {term.name}.")
        db.session.delete(term)
        db.session.commit()
        flash("Term deleted.", "success")
    return redirect(url_for("terms"))


@app.route("/school-years/<int:year_id>/archive", methods=["POST"])
@roles_required("registrar")
def archive_school_year(year_id):
    school_year = db.get_or_404(SchoolYear, year_id)
    action = request.form.get("action")
    if action not in {"archive", "reopen"}:
        flash("Choose whether to archive or reopen the school year.", "danger")
        return redirect(url_for("school_years"))
    school_year.archived = action == "archive"
    record_audit(
        "school_year_archived" if school_year.archived else "school_year_reopened",
        "school_year",
        school_year.id,
        f"{'Archived' if school_year.archived else 'Reopened'} school year {school_year.name}.",
    )
    db.session.commit()
    flash(
        "School year archived; its records are retained."
        if school_year.archived
        else "School year reopened.",
        "success",
    )
    return redirect(url_for("school_years"))


@app.route("/school-years/<int:year_id>/delete", methods=["POST"])
@roles_required("registrar")
def delete_school_year(year_id):
    school_year = db.get_or_404(SchoolYear, year_id)
    if school_year.classes:
        flash("A school year with classes cannot be deleted.", "warning")
        return redirect(url_for("school_years"))
    if Fee.query.filter_by(school_year_id=school_year.id).first():
        flash("A school year with fees cannot be deleted.", "warning")
        return redirect(url_for("school_years"))
    record_audit(
        "school_year_deleted",
        "school_year",
        school_year.id,
        f"Deleted school year {school_year.name}.",
    )
    db.session.delete(school_year)
    db.session.commit()
    flash("School year deleted.", "success")
    return redirect(url_for("school_years"))


@app.route("/school-years/<int:year_id>/edit", methods=["POST"])
@roles_required("registrar")
def edit_school_year(year_id):
    school_year = db.get_or_404(SchoolYear, year_id)
    try:
        name = validate_school_year_name(request.form.get("name", ""))
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("school_years"))
    if len(name) > 20:
        flash("Enter a school year name of 20 characters or fewer.", "danger")
        return redirect(url_for("school_years"))
    existing = SchoolYear.query.filter(
        SchoolYear.name == name,
        SchoolYear.id != school_year.id,
    ).first()
    semantic_duplicate = any(
        year.id != school_year.id and same_school_year(year.name, name)
        for year in SchoolYear.query.all()
    )
    if existing or semantic_duplicate:
        flash("That school year name is already in use.", "warning")
        return redirect(url_for("school_years"))
    previous_name = school_year.name
    school_year.name = name
    for school_class in school_year.classes:
        school_class.academic_year = name
    for fee in Fee.query.filter_by(school_year_id=school_year.id).all():
        fee.academic_year = name
    try:
        record_audit(
            "school_year_renamed",
            "school_year",
            school_year.id,
            f"Renamed school year {previous_name} to {name}.",
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("That school year name is already in use.", "warning")
    else:
        flash("School year updated.", "success")
    return redirect(url_for("school_years"))


@app.route("/promotions", methods=["GET", "POST"])
@roles_required("registrar")
def promotions():
    if request.method == "POST":
        try:
            source_class_id = int(request.form["source_class_id"])
            destination_class_id = int(request.form["destination_class_id"])
            student_ids = [int(value) for value in request.form.getlist("student_ids")]
        except (KeyError, TypeError, ValueError):
            flash("Select valid source and destination classes and students.", "danger")
            return redirect(url_for("promotions"))
        source_class = db.session.get(SchoolClass, source_class_id)
        destination_class = (
            SchoolClass.query.filter_by(id=destination_class_id)
            .with_for_update()
            .first()
        )
        if (
            not source_class
            or not destination_class
            or source_class.id == destination_class.id
            or source_class.school_year_id == destination_class.school_year_id
            or not class_year_is_open(source_class.id)
            or not class_year_is_open(destination_class.id)
        ):
            flash("Choose classes in different open school years.", "danger")
            return redirect(url_for("promotions"))
        if not student_ids or len(student_ids) != len(set(student_ids)):
            flash("Select one or more students to promote.", "danger")
            return redirect(url_for("promotions"))

        source_enrollments = Enrollment.query.join(Student).filter(
            Enrollment.class_id == source_class.id,
            Enrollment.student_id.in_(student_ids),
            Enrollment.status == "active",
            Student.active.is_(True),
        ).all()
        if len(source_enrollments) != len(student_ids):
            flash("Every selected student must be actively enrolled in the source class.", "danger")
            return redirect(url_for("promotions", source_class_id=source_class.id))
        existing_target_ids = {
            enrollment.student_id
            for enrollment in Enrollment.query.filter(
                Enrollment.class_id == destination_class.id,
                Enrollment.student_id.in_(student_ids),
                Enrollment.status == "active",
            ).all()
        }
        if existing_target_ids:
            flash("One or more selected students are already active in the destination class.", "warning")
            return redirect(url_for("promotions", source_class_id=source_class.id))
        if (
            destination_class.capacity is not None
            and Enrollment.query.filter_by(
                class_id=destination_class.id, status="active"
            ).count() + len(student_ids) > destination_class.capacity
        ):
            flash("The destination class does not have enough capacity for this promotion.", "warning")
            return redirect(url_for("promotions", source_class_id=source_class.id))

        for enrollment in source_enrollments:
            enrollment.status = "completed"
            db.session.add(Enrollment(
                student_id=enrollment.student_id,
                class_id=destination_class.id,
                status="active",
            ))
        try:
            record_audit(
                "students_promoted",
                "school_class",
                destination_class.id,
                f"Promoted {len(source_enrollments)} student(s) from {source_class.name} ({source_class.academic_year}) to {destination_class.name} ({destination_class.academic_year}).",
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Promotion could not be completed because an enrollment changed.", "warning")
            return redirect(url_for("promotions", source_class_id=source_class.id))
        flash(f"Promoted {len(source_enrollments)} student(s).", "success")
        return redirect(url_for("promotions"))

    source_class_id = request.args.get("source_class_id", type=int)
    source_class = db.session.get(SchoolClass, source_class_id) if source_class_id else None
    candidates = []
    if source_class:
        candidates = (
            Student.query.join(Enrollment, Enrollment.student_id == Student.id)
            .filter(
                Student.active.is_(True),
                Enrollment.status == "active",
                Enrollment.class_id == source_class.id,
            )
            .order_by(Student.name)
            .all()
        )
    open_classes = (
        SchoolClass.query.join(SchoolYear)
        .filter(SchoolYear.archived.is_(False))
        .order_by(SchoolClass.academic_year, SchoolClass.name)
        .all()
    )
    return render_template(
        "promotions.html",
        source_classes=open_classes,
        destination_classes=[
            item for item in open_classes
            if item.academic_year != (source_class.academic_year if source_class else None)
        ],
        all_source_classes=open_classes,
        source_class=source_class,
        candidates=candidates,
    )


@app.route("/classes/<int:class_id>/teachers", methods=["POST"])
@roles_required("registrar")
def assign_teacher(class_id):
    school_class = db.get_or_404(SchoolClass, class_id)
    if not class_year_is_open(school_class.id):
        flash("Archived school years are read-only.", "warning")
        return redirect(url_for("classes"))
    action = request.form.get("action")
    try:
        teacher_id = int(request.form.get("teacher_id", ""))
    except ValueError:
        flash("Select a valid teacher.", "danger")
        return redirect(url_for("classes"))

    teacher = User.query.filter_by(
        id=teacher_id, role="teacher", enabled=True
    ).first()
    if not teacher:
        flash("Select an enabled teacher account.", "danger")
        return redirect(url_for("classes"))

    assignment = TeacherClassAssignment.query.filter_by(
        teacher_id=teacher.id, class_id=school_class.id
    ).first()
    if action == "assign":
        if assignment:
            flash("That teacher is already assigned to this class.", "warning")
        else:
            db.session.add(
                TeacherClassAssignment(
                    teacher_id=teacher.id,
                    class_id=school_class.id,
                )
            )
            record_audit(
                "teacher_assigned",
                "school_class",
                school_class.id,
                f"Assigned teacher {teacher.username} to {school_class.name} ({school_class.academic_year}).",
            )
            db.session.commit()
            flash("Teacher assigned to class.", "success")
    elif action == "remove":
        if assignment:
            db.session.delete(assignment)
            record_audit(
                "teacher_unassigned",
                "school_class",
                school_class.id,
                f"Removed teacher {teacher.username} from {school_class.name} ({school_class.academic_year}).",
            )
            db.session.commit()
            flash("Teacher assignment removed.", "success")
        else:
            flash("That teacher is not assigned to this class.", "warning")
    else:
        flash("Choose whether to assign or remove the teacher.", "danger")
    return redirect(url_for("classes"))


@app.route("/enrollments", methods=["GET", "POST"])
@roles_required("registrar")
def enrollments():
    if request.method == "POST":
        try:
            student_id = int(request.form["student_id"])
            class_id = int(request.form["class_id"])
        except (KeyError, ValueError):
            flash("Select a valid student and class.", "danger")
            return redirect(url_for("enrollments"))
        student = Student.query.filter_by(id=student_id, active=True).first()
        school_class = SchoolClass.query.filter_by(id=class_id).with_for_update().first()
        if not student or not school_class or not class_year_is_open(class_id):
            flash("Select an active student and a valid class.", "danger")
            return redirect(url_for("enrollments"))
        if (
            school_class.capacity is not None
            and Enrollment.query.filter_by(class_id=class_id, status="active").count()
            >= school_class.capacity
        ):
            flash("This class has reached its enrollment capacity.", "warning")
            return redirect(url_for("enrollments"))
        existing_active = Enrollment.query.filter_by(
            student_id=student_id,
            class_id=class_id,
            status="active",
        ).first()
        if existing_active:
            flash("Student is already actively enrolled in this class.", "warning")
            return redirect(url_for("enrollments"))
        enrollment = Enrollment(student_id=student_id, class_id=class_id)
        db.session.add(enrollment)
        try:
            db.session.flush()
            record_audit(
                "student_enrolled",
                "enrollment",
                enrollment.id,
                f"Enrolled {student.name} in {school_class.name} ({school_class.academic_year}).",
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Student is already actively enrolled in this class.", "warning")
            return redirect(url_for("enrollments"))
        flash("Student enrolled.", "success")
        return redirect(url_for("enrollments"))
    enrollment_pagination = (
        Enrollment.query.options(
            joinedload(Enrollment.student),
            joinedload(Enrollment.school_class),
        )
        .order_by(Enrollment.id.desc())
        .paginate(
        page=requested_page(),
        per_page=RECORDS_PER_PAGE,
        error_out=False,
        )
    )
    return render_template(
        "enrollments.html",
        enrollments=enrollment_pagination.items,
        pagination=enrollment_pagination,
        classes=SchoolClass.query.join(SchoolYear)
        .filter(SchoolYear.archived.is_(False))
        .order_by(SchoolClass.academic_year.desc(), SchoolClass.name)
        .all(),
        enrollment_statuses=ENROLLMENT_STATUSES,
    )


@app.route("/enrollments/<int:enrollment_id>/status", methods=["POST"])
@roles_required("registrar")
def update_enrollment_status(enrollment_id):
    enrollment = db.get_or_404(Enrollment, enrollment_id)
    status = request.form.get("status", "")
    if status not in ENROLLMENT_STATUSES:
        flash("Select a valid enrollment status.", "danger")
        return redirect(url_for("enrollments"))
    if status == "active" and not enrollment.student.active:
        flash("Archived students cannot have active enrollments.", "danger")
        return redirect(url_for("enrollments"))
    if status == "active" and not class_year_is_open(enrollment.class_id):
        flash("Archived school years cannot have active enrollments.", "danger")
        return redirect(url_for("enrollments"))
    if status == "active":
        school_class = SchoolClass.query.filter_by(
            id=enrollment.class_id
        ).with_for_update().first_or_404()
        active_count = Enrollment.query.filter_by(
            class_id=enrollment.class_id,
            status="active",
        ).count()
        if (
            enrollment.status != "active"
            and school_class.capacity is not None
            and active_count >= school_class.capacity
        ):
            flash("This class has reached its enrollment capacity.", "warning")
            return redirect(url_for("enrollments"))
        other_active = Enrollment.query.filter(
            Enrollment.student_id == enrollment.student_id,
            Enrollment.class_id == enrollment.class_id,
            Enrollment.status == "active",
            Enrollment.id != enrollment.id,
        ).first()
        if other_active:
            flash(
                "This student already has an active enrollment in this class.",
                "warning",
            )
            return redirect(url_for("enrollments"))
    previous_status = enrollment.status
    enrollment.status = status
    try:
        record_audit(
            "enrollment_status_changed",
            "enrollment",
            enrollment.id,
            f"Changed enrollment for {enrollment.student.name} in {enrollment.school_class.name} from {previous_status} to {status}.",
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            "This student already has an active enrollment in this class.",
            "warning",
        )
        return redirect(url_for("enrollments"))
    flash("Enrollment status updated.", "success")
    return redirect(url_for("enrollments"))


@app.route("/fees", methods=["GET", "POST"])
@roles_required("finance")
def fees():
    if request.method == "POST":
        try:
            student_id = int(request.form["student_id"])
            amount = Decimal(request.form["amount"])
            due_date = parse_date(request.form.get("due_date"))
            academic_year = request.form.get("academic_year", "").strip() or None
        except (KeyError, TypeError, ValueError, InvalidOperation):
            flash("Enter a valid student, fee amount, and due date.", "danger")
            return redirect(url_for("fees"))
        description = request.form.get("description", "").strip()
        school_year = (
            SchoolYear.query.filter_by(name=academic_year, archived=False).first()
            if academic_year
            else None
        )
        if (
            not description
            or not amount.is_finite()
            or amount <= 0
            or amount > Decimal("9999999999.99")
            or amount.as_tuple().exponent < -2
            or not Student.query.filter_by(id=student_id, active=True).first()
            or (academic_year is not None and not school_year)
        ):
            flash("Enter a valid active student, open school year, description, and positive fee amount.", "danger")
            return redirect(url_for("fees"))
        fee = Fee(
            student_id=student_id,
            description=description,
            amount=amount,
            due_date=due_date,
            academic_year=academic_year,
            school_year=school_year,
        )
        db.session.add(fee)
        db.session.flush()
        record_audit(
            "fee_created",
            "fee",
            fee.id,
            f"Created fee '{description}' for {fee.student.name}: {amount:.2f}.",
        )
        db.session.commit()
        flash("Fee record saved.", "success")
        return redirect(url_for("fees"))
    fee_pagination = (
        Fee.query.options(
            joinedload(Fee.student),
            selectinload(Fee.payments),
        )
        .order_by(Fee.id.desc())
        .paginate(
        page=requested_page(),
        per_page=RECORDS_PER_PAGE,
        error_out=False,
        )
    )
    return render_template(
        "fees.html",
        fees=fee_pagination.items,
        pagination=fee_pagination,
        school_years=SchoolYear.query.filter_by(archived=False)
        .order_by(SchoolYear.name.desc())
        .all(),
    )


@app.route("/fees/student/<int:student_id>/statement")
@roles_required("finance")
def fee_statement(student_id):
    student = db.get_or_404(Student, student_id)
    fees = (
        Fee.query.options(selectinload(Fee.payments))
        .filter_by(student_id=student.id)
        .order_by(Fee.id)
        .all()
    )
    return render_template("fee_statement.html", student=student, fees=fees, generated_on=date.today())


@app.route("/fees/student/<int:student_id>/statement.csv")
@roles_required("finance")
def fee_statement_csv(student_id):
    student = db.get_or_404(Student, student_id)
    fees = (
        Fee.query.options(selectinload(Fee.payments))
        .filter_by(student_id=student.id)
        .order_by(Fee.id)
        .yield_per(EXPORT_BATCH_SIZE)
    )

    def rows():
        for fee in fees:
            yield (student.student_number, student.name, fee.description, fee.academic_year,
                   fee.amount, fee.paid_amount, fee.amount - fee.paid_amount, fee.due_date, "Fee")
            for payment in fee.payments:
                yield (student.student_number, student.name, fee.description, fee.academic_year,
                       "", payment.amount, "", payment.payment_date, payment.payment_method or "Payment")

    return csv_download(
        f"fee_statement_{student.id}.csv",
        ("Student number", "Student", "Fee", "School year", "Amount", "Paid", "Balance", "Date", "Entry"),
        rows(),
    )


@app.route("/fees/student/<int:student_id>/statement.pdf")
@roles_required("finance")
def fee_statement_pdf(student_id):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    student = db.get_or_404(Student, student_id)
    fees = (
        Fee.query.options(selectinload(Fee.payments))
        .filter_by(student_id=student.id)
        .order_by(Fee.id)
        .all()
    )
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    width, height = A4
    y = height - 55
    pdf.setTitle(f"Fee statement - {student.student_number}".encode("latin-1", "replace").decode("latin-1"))
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(48, y, "Fee statement")
    y -= 25
    pdf.setFont("Helvetica", 10)
    pdf.drawString(48, y, f"{student.name} ({student.student_number})".encode("latin-1", "replace").decode("latin-1"))
    y -= 28
    for fee in fees:
        lines = [f"{fee.description} | {fee.academic_year or 'Unassigned'} | Amount {fee.amount:.2f} | Paid {fee.paid_amount:.2f} | Balance {fee.amount - fee.paid_amount:.2f}"]
        lines.extend(f"  Payment {p.payment_date or 'Date unknown'}: {p.amount:.2f} {p.payment_method or ''} {p.reference or ''}" for p in fee.payments)
        for line in lines:
            if y < 55:
                pdf.showPage(); y = height - 55; pdf.setFont("Helvetica", 10)
            pdf.drawString(48, y, line[:125].encode("latin-1", "replace").decode("latin-1"))
            y -= 17
    if not fees:
        pdf.drawString(48, y, "No fee records.")
    pdf.save()
    output.seek(0)
    return Response(output.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="fee_statement_{student.id}.pdf"'})


@app.route("/fees/<int:fee_id>/payments", methods=["POST"])
@roles_required("finance")
def record_payment(fee_id):
    try:
        amount = Decimal(request.form["amount"])
        payment_date = parse_date(request.form.get("payment_date"))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        flash("Enter a valid payment amount and date.", "danger")
        return redirect(url_for("fees"))
    payment_method = request.form.get("payment_method", "").strip()
    reference = request.form.get("reference", "").strip()
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > Decimal("9999999999.99")
        or amount.as_tuple().exponent < -2
        or payment_date is None
        or len(payment_method) > 40
        or len(reference) > 120
    ):
        flash("Enter a positive payment amount with no more than two decimals and a date.", "danger")
        return redirect(url_for("fees"))

    fee = Fee.query.filter_by(id=fee_id).with_for_update().first_or_404()
    if amount > fee.amount - fee.paid_amount:
        flash("Payment exceeds the outstanding fee balance.", "warning")
        return redirect(url_for("fees"))
    payment = Payment(
        fee_id=fee.id,
        amount=amount,
        payment_date=payment_date,
        payment_method=payment_method or None,
        reference=reference or None,
        notes=request.form.get("notes", "").strip() or None,
    )
    db.session.add(payment)
    db.session.flush()
    record_audit(
        "payment_recorded",
        "payment",
        payment.id,
        f"Recorded payment of {amount:.2f} for {fee.student.name}, fee '{fee.description}'.",
    )
    db.session.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("fees"))


@app.route("/attendance", methods=["GET", "POST"])
@roles_required("teacher")
def attendance():
    if request.method == "POST":
        try:
            student_id = int(request.form["student_id"])
            class_id = int(request.form["class_id"])
            date = parse_date(request.form.get("date"))
        except (KeyError, TypeError, ValueError):
            flash("Enter a valid student, class, and date.", "danger")
            return redirect(url_for("attendance"))
        ensure_teacher_class_access(class_id)
        if not class_year_is_open(class_id):
            flash("Archived school years are read-only.", "warning")
            return redirect(url_for("attendance"))
        if date is None:
            flash("Enter a valid student, class, and date.", "danger")
            return redirect(url_for("attendance"))
        status = request.form.get("status", "")
        if status not in {"Present", "Absent", "Late", "Excused"}:
            flash("Select a valid attendance status.", "danger")
            return redirect(url_for("attendance"))
        enrollment = Enrollment.query.join(Student).filter(
            Enrollment.student_id == student_id,
            Enrollment.class_id == class_id,
            Enrollment.status == "active",
            Student.active.is_(True),
        ).first()
        if not enrollment:
            flash("Select an active student enrolled in that class.", "danger")
            return redirect(url_for("attendance"))
        record = Attendance.query.filter_by(
            student_id=student_id, class_id=class_id, date=date
        ).first()
        if record:
            record.status = status
        else:
            db.session.add(Attendance(
                student_id=student_id,
                class_id=class_id,
                date=date,
                status=status,
            ))
        record_audit(
            "attendance_recorded",
            "school_class",
            class_id,
            f"Recorded {status.lower()} attendance for {enrollment.student.name} in {enrollment.school_class.name} on {date.isoformat()}.",
        )
        db.session.commit()
        flash("Attendance recorded.", "success")
        return redirect(url_for("attendance"))
    class_ids = teacher_class_ids()
    selected_class_id = None
    selected_date = None
    try:
        selected_class_id = (
            int(request.args["class_id"]) if request.args.get("class_id") else None
        )
        selected_date = parse_date(request.args.get("date"))
        selected_term_id = int(request.args["term_id"]) if request.args.get("term_id") else None
    except (TypeError, ValueError):
        flash("Invalid attendance filter.", "danger")
        return redirect(url_for("attendance"))
    if selected_class_id is not None:
        ensure_teacher_class_access(selected_class_id)
    selected_class = db.session.get(SchoolClass, selected_class_id) if selected_class_id else None
    selected_term = None
    if selected_term_id is not None:
        selected_term = db.session.get(SchoolTerm, selected_term_id)
        if not selected_term or not selected_class or selected_term.school_year_id != selected_class.school_year_id:
            flash("Select a term for the chosen class.", "danger")
            return redirect(url_for("attendance"))
    records_query = Attendance.query.options(
        joinedload(Attendance.student),
        joinedload(Attendance.school_class),
    )
    if class_ids is not None:
        records_query = records_query.filter(Attendance.class_id.in_(class_ids))
    if selected_class_id is not None:
        records_query = records_query.filter(Attendance.class_id == selected_class_id)
    if selected_date is not None:
        records_query = records_query.filter(Attendance.date == selected_date)
    if selected_term is not None:
        records_query = records_query.filter(
            Attendance.date >= selected_term.start_date,
            Attendance.date <= selected_term.end_date,
        )
    bulk_students = []
    current_statuses = {}
    if selected_class_id is not None and selected_date is not None:
        bulk_students = (
            Student.query.join(Enrollment, Enrollment.student_id == Student.id)
            .filter(
                Student.active.is_(True),
                Enrollment.status == "active",
                Enrollment.class_id == selected_class_id,
            )
            .order_by(Student.name)
            .all()
        )
        current_statuses = {
            record.student_id: record.status
            for record in Attendance.query.filter_by(
                class_id=selected_class_id,
                date=selected_date,
            ).all()
        }
    return render_template(
        "attendance.html",
        records=records_query.order_by(Attendance.date.desc()).limit(100).all(),
        classes=classes_visible_to_current_user().order_by(SchoolClass.name).all(),
        selected_class_id=selected_class_id,
        selected_date=request.args.get("date", ""),
        selected_term=selected_term,
        selected_term_id=selected_term_id,
        terms=(SchoolTerm.query.filter_by(school_year_id=selected_class.school_year_id).order_by(SchoolTerm.start_date).all() if selected_class_id else []),
        bulk_students=bulk_students,
        current_statuses=current_statuses,
    )


@app.route("/attendance/bulk", methods=["POST"])
@roles_required("teacher")
def bulk_attendance():
    try:
        class_id = int(request.form["class_id"])
        attendance_date = parse_date(request.form.get("date"))
    except (KeyError, TypeError, ValueError):
        flash("Select a valid class and attendance date.", "danger")
        return redirect(url_for("attendance"))
    ensure_teacher_class_access(class_id)
    if not class_year_is_open(class_id):
        flash("Archived school years are read-only.", "warning")
        return redirect(url_for("attendance"))
    if attendance_date is None:
        flash("Select a valid class and attendance date.", "danger")
        return redirect(url_for("attendance"))
    if request.form.get("term_id"):
        try:
            term_id = int(request.form["term_id"])
        except ValueError:
            flash("Select a valid academic term.", "danger")
            return redirect(url_for("attendance"))
        term = db.session.get(SchoolTerm, term_id)
        school_class = db.get_or_404(SchoolClass, class_id)
        if not term or term.school_year_id != school_class.school_year_id or not (term.start_date <= attendance_date <= term.end_date):
            flash("Attendance date must fall within the selected class term.", "danger")
            return redirect(url_for("attendance", class_id=class_id, date=attendance_date.isoformat()))

    students = (
        Student.query.join(Enrollment, Enrollment.student_id == Student.id)
        .filter(
            Student.active.is_(True),
            Enrollment.status == "active",
            Enrollment.class_id == class_id,
        )
        .all()
    )
    if not students:
        flash("There are no active students enrolled in that class.", "warning")
        return redirect(url_for("attendance"))
    submitted = {}
    for key, status in request.form.items():
        if key.startswith("status_"):
            student_id = key[len("status_"):]
            if not student_id.isdigit():
                flash("Invalid attendance submission.", "danger")
                return redirect(url_for("attendance"))
            submitted[int(student_id)] = status
    active_student_ids = {student.id for student in students}
    if any(
        student.id not in submitted
        or submitted[student.id] not in {"Present", "Absent", "Late", "Excused"}
        for student in students
    ) or any(student_id not in active_student_ids for student_id in submitted):
        flash("Submit a valid attendance status for every active student.", "danger")
        return redirect(url_for(
            "attendance",
            class_id=class_id,
            date=attendance_date.isoformat(),
        ))

    existing = {
        record.student_id: record
        for record in Attendance.query.filter_by(
            class_id=class_id,
            date=attendance_date,
        ).all()
    }
    for student in students:
        record = existing.get(student.id)
        if record:
            record.status = submitted[student.id]
        else:
            db.session.add(Attendance(
                student_id=student.id,
                class_id=class_id,
                date=attendance_date,
                status=submitted[student.id],
            ))
    school_class = db.session.get(SchoolClass, class_id)
    record_audit(
        "bulk_attendance_recorded",
        "school_class",
        class_id,
        f"Saved attendance for {len(students)} students in {school_class.name} on {attendance_date.isoformat()}.",
    )
    db.session.commit()
    flash(f"Attendance saved for {len(students)} students.", "success")
    return redirect(url_for(
        "attendance",
        class_id=class_id,
        date=attendance_date.isoformat(),
        term_id=request.form.get("term_id", ""),
    ))


@app.route("/grades", methods=["GET", "POST"])
@roles_required("teacher")
def grades():
    if request.method == "POST":
        try:
            student_id = int(request.form["student_id"])
            class_id = int(request.form["class_id"])
            score = Decimal(request.form["score"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            flash("Enter a valid student, class, and grade.", "danger")
            return redirect(url_for("grades"))
        ensure_teacher_class_access(class_id)
        if not class_year_is_open(class_id):
            flash("Archived school years are read-only.", "warning")
            return redirect(url_for("grades"))
        if (
            not score.is_finite()
            or score < 0
            or score > 100
            or score.as_tuple().exponent < -2
        ):
            flash("Grade must be between 0 and 100 with no more than two decimals.", "danger")
            return redirect(url_for("grades"))
        enrollment = Enrollment.query.join(Student).filter(
            Enrollment.student_id == student_id,
            Enrollment.class_id == class_id,
            Enrollment.status == "active",
            Student.active.is_(True),
        ).first()
        if not enrollment:
            flash("Select an active student enrolled in that class.", "danger")
            return redirect(url_for("grades"))
        subject = request.form.get("subject", "").strip()
        term = request.form.get("term", "").strip()
        managed_term = None
        if request.form.get("term_id"):
            try:
                managed_term = db.session.get(SchoolTerm, int(request.form["term_id"]))
            except ValueError:
                managed_term = None
            class_record = db.session.get(SchoolClass, class_id)
            if not managed_term or managed_term.school_year_id != class_record.school_year_id:
                flash("Select a term belonging to the class school year.", "danger")
                return redirect(url_for("grades"))
            term = managed_term.name
        if not subject or not term or len(subject) > 80 or len(term) > 40:
            flash("Enter a subject (up to 80 characters) and grading period (up to 40 characters).", "danger")
            return redirect(url_for("grades"))
        grade = Grade(
            student_id=student_id,
            class_id=class_id,
            subject=subject,
            term=term,
            school_term=managed_term,
            score=score,
        )
        db.session.add(grade)
        db.session.flush()
        record_audit(
            "grade_recorded",
            "grade",
            grade.id,
            f"Recorded {subject} grade {score:.2f} for {enrollment.student.name} in {term}.",
        )
        db.session.commit()
        flash("Grade recorded.", "success")
        return redirect(url_for("grades"))
    class_ids = teacher_class_ids()
    records_query = Grade.query.options(
        joinedload(Grade.student),
        joinedload(Grade.school_class),
    )
    if class_ids is not None:
        records_query = records_query.filter(Grade.class_id.in_(class_ids))
    selected_class_id = None
    selected_student_id = None
    selected_term = request.args.get("term", "").strip()
    try:
        selected_class_id = (
            int(request.args["class_id"]) if request.args.get("class_id") else None
        )
        selected_student_id = (
            int(request.args["student_id"]) if request.args.get("student_id") else None
        )
    except (TypeError, ValueError):
        flash("Invalid grade filter.", "danger")
        return redirect(url_for("grades"))
    if selected_class_id is not None:
        ensure_teacher_class_access(selected_class_id)
        records_query = records_query.filter(Grade.class_id == selected_class_id)
    if selected_student_id is not None:
        records_query = records_query.filter(Grade.student_id == selected_student_id)
    if selected_term:
        records_query = records_query.filter(Grade.term == selected_term)
    period_query = db.session.query(Grade.term).distinct()
    if class_ids is not None:
        period_query = period_query.filter(Grade.class_id.in_(class_ids))
    if selected_class_id is not None:
        period_query = period_query.filter(Grade.class_id == selected_class_id)
    periods = [row[0] for row in period_query.order_by(Grade.term).all()]
    managed_terms = SchoolTerm.query.join(SchoolYear).filter(SchoolYear.archived.is_(False))
    if selected_class_id is not None:
        selected_class = db.session.get(SchoolClass, selected_class_id)
        managed_terms = managed_terms.filter(SchoolTerm.school_year_id == selected_class.school_year_id)
    grade_students = enrolled_students_for_classes(class_ids)
    if selected_class_id is not None:
        grade_students = (
            Student.query.join(Enrollment, Enrollment.student_id == Student.id)
            .filter(
                Student.active.is_(True),
                Enrollment.status == "active",
                Enrollment.class_id == selected_class_id,
            )
            .distinct()
            .order_by(Student.name)
        )
    selected_student = (
        grade_students.filter(Student.id == selected_student_id).first()
        if selected_student_id is not None else None
    )
    grade_pagination = records_query.order_by(Grade.id.desc()).paginate(
        page=requested_page(),
        per_page=RECORDS_PER_PAGE,
        error_out=False,
    )
    return render_template(
        "grades.html",
        records=grade_pagination.items,
        pagination=grade_pagination,
        selected_student=selected_student,
        classes=classes_visible_to_current_user().order_by(SchoolClass.name).all(),
        selected_class_id=selected_class_id,
        selected_student_id=selected_student_id,
        selected_term=selected_term,
        periods=periods,
        managed_terms=managed_terms.order_by(SchoolTerm.start_date).all(),
    )


@app.route("/reports", methods=["GET"])
@roles_required("teacher")
def report_card():
    class_ids = teacher_class_ids()
    classes = classes_visible_to_current_user().order_by(SchoolClass.name).all()
    selected_class_id = None
    selected_student_id = None
    selected_term = request.args.get("term", "").strip()
    selected_student = None
    grades = []
    report_subjects = []
    attendance_counts = {}
    selected_period_range = None
    average_score = None
    try:
        selected_class_id = (
            int(request.args["class_id"]) if request.args.get("class_id") else None
        )
        selected_student_id = (
            int(request.args["student_id"]) if request.args.get("student_id") else None
        )
    except (TypeError, ValueError):
        flash("Invalid report-card filter.", "danger")
        return redirect(url_for("report_card"))

    if selected_class_id is not None:
        ensure_teacher_class_access(selected_class_id)
        if selected_student_id is not None:
            selected_student = (
            Student.query.join(Enrollment, Enrollment.student_id == Student.id)
            .filter(
                Student.active.is_(True),
                Student.id == selected_student_id,
                Enrollment.class_id == selected_class_id,
                Enrollment.status.in_(("active", "completed", "withdrawn")),
            )
            .first()
            )
    if selected_class_id is not None and selected_student_id is not None and selected_term:
        if not selected_student:
            abort(404)
        grades = Grade.query.filter_by(
            class_id=selected_class_id,
            student_id=selected_student_id,
            term=selected_term,
        ).order_by(Grade.subject, Grade.id).all()
        school_class = db.session.get(SchoolClass, selected_class_id)
        selected_period_range = SchoolTerm.query.filter_by(
            school_year_id=school_class.school_year_id,
            name=selected_term,
        ).first()
        subject_scores = {}
        for grade in grades:
            subject_scores.setdefault(grade.subject, []).append(grade.score)
        report_subjects = [
            {
                "subject": subject,
                "score": sum(scores, Decimal("0")) / len(scores),
            }
            for subject, scores in sorted(subject_scores.items())
        ]
        attendance_query = Attendance.query.filter_by(
            class_id=selected_class_id,
            student_id=selected_student_id,
        )
        if selected_period_range:
            attendance_query = attendance_query.filter(
                Attendance.date >= selected_period_range.start_date,
                Attendance.date <= selected_period_range.end_date,
            )
        attendance_rows = attendance_query.all()
        for record in attendance_rows:
            attendance_counts[record.status] = attendance_counts.get(record.status, 0) + 1
        if report_subjects:
            average_score = (
                sum((item["score"] for item in report_subjects), Decimal("0"))
                / len(report_subjects)
            )

    period_query = db.session.query(Grade.term).distinct()
    if class_ids is not None:
        period_query = period_query.filter(Grade.class_id.in_(class_ids))
    if selected_class_id is not None:
        period_query = period_query.filter(Grade.class_id == selected_class_id)
    periods = [row[0] for row in period_query.order_by(Grade.term).all()]
    return render_template(
        "report_card.html",
        classes=classes,
        selected_class=(
            next((item for item in classes if item.id == selected_class_id), None)
            if selected_class_id is not None else None
        ),
        selected_student=selected_student,
        periods=periods,
        selected_class_id=selected_class_id,
        selected_student_id=selected_student_id,
        selected_term=selected_term,
        grades=grades,
        report_subjects=report_subjects,
        average_score=average_score,
        attendance_counts=attendance_counts,
        selected_period_range=selected_period_range,
    )


def csv_download(filename, headers, rows):
    """Stream spreadsheet-safe CSV rows instead of buffering the whole export."""
    def generate():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        yield "\ufeff" + output.getvalue()
        output.seek(0)
        output.truncate(0)
        for index, row in enumerate(rows, start=1):
            safe_row = []
            for value in row:
                text_value = "" if value is None else str(value)
                if text_value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    text_value = "'" + text_value
                safe_row.append(text_value)
            output.seek(0)
            writer.writerow(safe_row)
            if index % EXPORT_BATCH_SIZE == 0:
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
        if output.tell():
            yield output.getvalue()

    return Response(
        stream_with_context(generate()),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/exports/students.csv")
@roles_required("registrar", "teacher", "finance")
def export_students_csv():
    search = request.args.get("q", "").strip()
    include_archived = (
        request.args.get("archived") == "1"
        and current_user.role in ("admin", "registrar")
    )
    query = (
        active_students_visible_to_current_user()
        if current_user.role == "teacher"
        else Student.query
    )
    if not include_archived:
        query = query.filter(Student.active.is_(True))
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            db.or_(Student.name.ilike(pattern), Student.student_number.ilike(pattern))
        )
    students = query.order_by(Student.name).yield_per(EXPORT_BATCH_SIZE)
    if current_user.role == "finance":
        headers = ("Student number", "Name", "Status")
        rows = (
            (item.student_number, item.name, "Active" if item.active else "Archived")
            for item in students
        )
    else:
        headers = ("Student number", "Name", "Email", "Phone", "Status")
        rows = (
            (item.student_number, item.name, item.email, item.phone,
             "Active" if item.active else "Archived")
            for item in students
        )
    return csv_download("students.csv", headers, rows)


@app.route("/exports/fees.csv")
@roles_required("finance")
def export_fees_csv():
    fees = (
        Fee.query.options(
            joinedload(Fee.student),
            selectinload(Fee.payments),
        )
        .order_by(Fee.id.desc())
        .yield_per(EXPORT_BATCH_SIZE)
    )
    return csv_download(
        "fees.csv",
        ("Student number", "Student", "Description", "School year", "Amount",
         "Paid", "Balance", "Due date"),
        ((item.student.student_number, item.student.name, item.description,
          item.academic_year or "Unassigned", item.amount, item.paid_amount,
          item.amount - item.paid_amount, item.due_date) for item in fees),
    )


@app.route("/exports/attendance.csv")
@roles_required("teacher")
def export_attendance_csv():
    class_ids = teacher_class_ids()
    try:
        selected_class_id = int(request.args["class_id"]) if request.args.get("class_id") else None
        selected_date = parse_date(request.args.get("date"))
        selected_term_id = int(request.args["term_id"]) if request.args.get("term_id") else None
    except (TypeError, ValueError):
        abort(400)
    if selected_class_id is not None:
        ensure_teacher_class_access(selected_class_id)
    if selected_term_id is not None:
        term = db.session.get(SchoolTerm, selected_term_id)
        selected_class = db.session.get(SchoolClass, selected_class_id) if selected_class_id else None
        if not term or not selected_class or term.school_year_id != selected_class.school_year_id:
            abort(400)
    else:
        term = None
    query = (
        Attendance.query.options(
            joinedload(Attendance.student),
            joinedload(Attendance.school_class),
        )
        .join(SchoolClass, Attendance.class_id == SchoolClass.id)
    )
    if class_ids is not None:
        query = query.filter(Attendance.class_id.in_(class_ids))
    if selected_class_id is not None:
        query = query.filter(Attendance.class_id == selected_class_id)
    if selected_date is not None:
        query = query.filter(Attendance.date == selected_date)
    if term:
        query = query.filter(Attendance.date >= term.start_date, Attendance.date <= term.end_date)
    records = query.order_by(Attendance.date.desc(), Attendance.id).yield_per(EXPORT_BATCH_SIZE)
    return csv_download(
        "attendance.csv",
        ("Date", "Student number", "Student", "Class", "School year", "Status"),
        ((item.date, item.student.student_number, item.student.name,
          item.school_class.name, item.school_class.academic_year, item.status)
         for item in records),
    )


@app.route("/exports/grades.csv")
@roles_required("teacher")
def export_grades_csv():
    class_ids = teacher_class_ids()
    try:
        selected_class_id = int(request.args["class_id"]) if request.args.get("class_id") else None
        selected_student_id = int(request.args["student_id"]) if request.args.get("student_id") else None
    except (TypeError, ValueError):
        abort(400)
    if selected_class_id is not None:
        ensure_teacher_class_access(selected_class_id)
    query = Grade.query.options(
        joinedload(Grade.student),
        joinedload(Grade.school_class),
    )
    if class_ids is not None:
        query = query.filter(Grade.class_id.in_(class_ids))
    if selected_class_id is not None:
        query = query.filter(Grade.class_id == selected_class_id)
    if selected_student_id is not None:
        query = query.filter(Grade.student_id == selected_student_id)
    selected_term = request.args.get("term", "").strip()
    if selected_term:
        query = query.filter(Grade.term == selected_term)
    records = query.order_by(Grade.id.desc()).yield_per(EXPORT_BATCH_SIZE)
    return csv_download(
        "grades.csv",
        ("Student number", "Student", "Class", "School year", "Subject", "Term", "Score"),
        ((item.student.student_number, item.student.name, item.school_class.name,
          item.school_class.academic_year, item.subject, item.term, item.score)
         for item in records),
    )


@app.route("/school-reports", methods=["GET"])
@roles_required("registrar", "teacher", "finance")
def school_reports():
    selected_year = request.args.get("year", "").strip()
    years = SchoolYear.query.order_by(SchoolYear.name.desc()).all()
    if selected_year and not SchoolYear.query.filter_by(name=selected_year).first():
        flash("Select a valid school year.", "danger")
        return redirect(url_for("school_reports"))

    class_ids = teacher_class_ids()
    enrollment_rows = []
    attendance_rows = []
    grade_rows = []
    outstanding_fees = []
    outstanding_total = Decimal("0")
    if current_user.role != "finance":
        enrollment_query = (
            db.session.query(
                SchoolClass.academic_year,
                Enrollment.status,
                db.func.count(Enrollment.id),
            )
            .join(SchoolClass, Enrollment.class_id == SchoolClass.id)
            .group_by(SchoolClass.academic_year, Enrollment.status)
        )
        attendance_query = (
            db.session.query(
                SchoolClass.academic_year,
                Attendance.status,
                db.func.count(Attendance.id),
            )
            .join(SchoolClass, Attendance.class_id == SchoolClass.id)
            .group_by(SchoolClass.academic_year, Attendance.status)
        )
        grades_query = (
            db.session.query(
                SchoolClass.academic_year,
                Grade.term,
                db.func.count(Grade.id),
                db.func.avg(Grade.score),
            )
            .join(SchoolClass, Grade.class_id == SchoolClass.id)
            .group_by(SchoolClass.academic_year, Grade.term)
        )
        if selected_year:
            enrollment_query = enrollment_query.filter(
                SchoolClass.school_year_id
                == SchoolYear.query.filter_by(name=selected_year).one().id
            )
            attendance_query = attendance_query.filter(
                SchoolClass.school_year_id
                == SchoolYear.query.filter_by(name=selected_year).one().id
            )
            grades_query = grades_query.filter(
                SchoolClass.school_year_id
                == SchoolYear.query.filter_by(name=selected_year).one().id
            )
        if class_ids is not None:
            enrollment_query = enrollment_query.filter(
                Enrollment.class_id.in_(class_ids)
            )
            attendance_query = attendance_query.filter(
                Attendance.class_id.in_(class_ids)
            )
            grades_query = grades_query.filter(Grade.class_id.in_(class_ids))
        enrollment_rows = enrollment_query.order_by(
            SchoolClass.academic_year.desc(),
            Enrollment.status,
        ).all()
        attendance_rows = attendance_query.order_by(
            SchoolClass.academic_year.desc(),
            Attendance.status,
        ).all()
        grade_rows = grades_query.order_by(
            SchoolClass.academic_year.desc(),
            Grade.term,
        ).all()

    if current_user.role in {"admin", "finance"}:
        payment_totals = (
            db.session.query(
                Payment.fee_id.label("fee_id"),
                db.func.sum(Payment.amount).label("paid_amount"),
            )
            .group_by(Payment.fee_id)
            .subquery()
        )
        fee_query = (
            db.session.query(
                Fee,
                Student.name,
                db.func.coalesce(payment_totals.c.paid_amount, 0),
            )
            .join(Student, Fee.student_id == Student.id)
            .outerjoin(payment_totals, payment_totals.c.fee_id == Fee.id)
        )
        if selected_year:
            fee_query = fee_query.filter(
                Fee.school_year_id
                == SchoolYear.query.filter_by(name=selected_year).one().id
            )
        for fee, student_name, paid_amount in fee_query.order_by(
            Fee.academic_year.desc(),
            Student.name,
            Fee.description,
        ).all():
            balance = fee.amount - Decimal(str(paid_amount))
            if balance > 0:
                outstanding_fees.append({
                    "student": student_name,
                    "description": fee.description,
                    "year": fee.academic_year or "Unassigned",
                    "balance": balance,
                })
                outstanding_total += balance

    return render_template(
        "school_reports.html",
        years=years,
        selected_year=selected_year,
        enrollment_rows=enrollment_rows,
        attendance_rows=attendance_rows,
        grade_rows=grade_rows,
        outstanding_fees=outstanding_fees,
        outstanding_total=outstanding_total,
    )
