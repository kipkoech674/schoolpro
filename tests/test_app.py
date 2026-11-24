import os
import re
import unittest
from unittest.mock import patch
from datetime import date
from decimal import Decimal
from sqlalchemy import event, inspect


os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-secret-key"

from app import (
    Attendance,
    Enrollment,
    Fee,
    Grade,
    Guardian,
    Payment,
    SchoolClass,
    SchoolTerm,
    SchoolYear,
    Student,
    TeacherClassAssignment,
    User,
    app,
    db,
)
from werkzeug.security import generate_password_hash


TEST_PASSWORD_HASH = generate_password_hash("test-password-123")


class SchoolAppTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        with app.app_context():
            db.create_all()
            for role in ("admin", "registrar", "teacher", "finance"):
                db.session.add(
                    User(
                        username=role,
                        role=role,
                        password_hash=TEST_PASSWORD_HASH,
                    )
                )
            school_year = SchoolYear(name="2026-27")
            student = Student(student_number="S-001", name="Test Student")
            school_class = SchoolClass(
                name="Grade 1",
                academic_year="2026-27",
                school_year=school_year,
            )
            db.session.add_all([school_year, student, school_class])
            db.session.commit()
            self.student_id = student.id
            self.class_id = school_class.id

    def tearDown(self):
        with app.app_context():
            db.drop_all()

    def csrf_token(self, client, path="/login"):
        response = client.get(path)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match, f"No CSRF token rendered at {path}")
        return match.group(1).decode()

    def login(self, role, client=None):
        client = client or self.client
        token = self.csrf_token(client)
        return client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": role,
                "password": "test-password-123",
            },
        )

    def assign_teacher(self, class_id=None):
        with app.app_context():
            db.session.add(
                TeacherClassAssignment(
                    teacher_id=User.query.filter_by(username="teacher").one().id,
                    class_id=class_id or self.class_id,
                )
            )
            db.session.commit()

    def enroll_student(self, student_id=None, class_id=None):
        with app.app_context():
            db.session.add(
                Enrollment(
                    student_id=student_id or self.student_id,
                    class_id=class_id or self.class_id,
                )
            )
            db.session.commit()

    def test_guest_is_redirected_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_debug_is_off_by_default_and_session_cookie_is_hardened(self):
        self.assertFalse(app.config["DEBUG"])
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_login_accepts_valid_credentials(self):
        response = self.login("registrar")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/students").status_code, 200)

    def test_successful_login_uses_permanent_session(self):
        self.login("registrar")
        with self.client.session_transaction() as session:
            self.assertTrue(session.permanent)

    def test_five_failed_logins_temporarily_lock_account(self):
        token = self.csrf_token(self.client)
        for _ in range(5):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "username": "registrar",
                    "password": "incorrect-password",
                },
            )
            self.assertEqual(response.status_code, 200)
        with app.app_context():
            user = User.query.filter_by(username="registrar").one()
            self.assertIsNotNone(user.locked_until)
            self.assertEqual(user.failed_login_attempts, 0)
        response = self.login("registrar")
        self.assertEqual(response.status_code, 200)

    def test_login_rejects_invalid_password(self):
        token = self.csrf_token(self.client)
        response = self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "registrar",
                "password": "incorrect",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password.", response.data)

    def test_disabled_account_cannot_log_in(self):
        with app.app_context():
            User.query.filter_by(username="registrar").one().enabled = False
            db.session.commit()
        response = self.login("registrar")
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertFalse(User.query.filter_by(username="registrar").one().is_active)

    def test_post_requests_require_csrf_token(self):
        response = self.client.post(
            "/login",
            data={"username": "registrar", "password": "test-password-123"},
        )
        self.assertEqual(response.status_code, 400)

    def test_registrar_can_manage_class_capacity_subject_and_timetable(self):
        self.login("registrar")
        response = self.client.get(f"/classes/{self.class_id}")
        self.assertEqual(response.status_code, 200)
        token = self.csrf_token(self.client, f"/classes/{self.class_id}")
        response = self.client.post(
            f"/classes/{self.class_id}",
            data={"csrf_token": token, "action": "update-class", "capacity": "2", "room": "A1"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            f"/classes/{self.class_id}",
            data={"csrf_token": token, "action": "add-subject", "name": "Mathematics"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            from app import ClassSubject
            subject = ClassSubject.query.filter_by(class_id=self.class_id).one()
            subject_id = subject.id
        data = {"csrf_token": token, "action": "add-slot", "subject_id": str(subject_id), "weekday": "0", "start_time": "08:00", "end_time": "09:00", "room": "A1"}
        self.assertEqual(self.client.post(f"/classes/{self.class_id}", data=data).status_code, 302)
        data.update(start_time="08:30", end_time="09:30")
        response = self.client.post(f"/classes/{self.class_id}", data=data, follow_redirects=True)
        self.assertIn(b"overlaps another class slot", response.data)

    def test_term_dates_cannot_overlap_within_a_school_year(self):
        self.login("registrar")
        token = self.csrf_token(self.client, "/terms")
        response = self.client.post("/terms", data={
            "csrf_token": token,
            "school_year_id": "1",
            "name": "Term 1",
            "start_date": "2026-01-01",
            "end_date": "2026-03-31",
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/terms", data={
            "csrf_token": token,
            "school_year_id": "1",
            "name": "Term 2",
            "start_date": "2026-03-31",
            "end_date": "2026-06-30",
        }, follow_redirects=True)
        self.assertIn(b"cannot overlap", response.data)
        with app.app_context():
            self.assertEqual(SchoolTerm.query.count(), 1)

    def test_password_reset_request_is_generic_for_unknown_email(self):
        token = self.csrf_token(self.client, "/password-reset")
        response = self.client.post("/password-reset", data={"csrf_token": token, "email": "unknown@example.com"}, follow_redirects=True)
        self.assertIn(b"If an enabled account uses that email address", response.data)

    def test_admin_invites_staff_and_invitee_sets_their_own_password(self):
        self.login("admin")
        csrf = self.csrf_token(self.client, "/staff")
        with patch("app.send_staff_invitation_email", return_value=True) as send_email:
            response = self.client.post("/staff", data={
                "csrf_token": csrf,
                "display_name": "Jane Kimani",
                "username": "new.teacher",
                "role": "teacher",
                "email": "new.teacher@example.com",
            })
        self.assertEqual(response.status_code, 302)
        send_email.assert_called_once()
        _user, invitation = send_email.call_args.args
        self.assertTrue(invitation)
        with app.app_context():
            pending_user = User.query.filter_by(username="new.teacher").one()
            self.assertFalse(pending_user.enabled)
            self.assertEqual(pending_user.display_name, "Jane Kimani")

        invite_client = app.test_client()
        invite_page = invite_client.get(f"/staff-invitation/{invitation}")
        self.assertEqual(invite_page.status_code, 200)
        invite_csrf = self.csrf_token(invite_client, f"/staff-invitation/{invitation}")
        activated = invite_client.post(
            f"/staff-invitation/{invitation}",
            data={
                "csrf_token": invite_csrf,
                "password": "teacher-password-123",
                "confirm_password": "teacher-password-123",
            },
        )
        self.assertEqual(activated.status_code, 302)
        with app.app_context():
            invited_user = User.query.filter_by(username="new.teacher").one()
            self.assertTrue(invited_user.enabled)
            self.assertIsNone(invited_user.invitation_token_hash)
            self.assertTrue(invited_user.check_password("teacher-password-123"))
        reused = invite_client.get(f"/staff-invitation/{invitation}", follow_redirects=True)
        self.assertIn(b"invalid or expired", reused.data)

    def test_admin_can_resend_a_pending_staff_invitation(self):
        self.login("admin")
        csrf = self.csrf_token(self.client, "/staff")
        with patch("app.send_staff_invitation_email", return_value=False):
            self.client.post("/staff", data={
                "csrf_token": csrf,
                "display_name": "Pending Staff",
                "username": "pending.staff",
                "role": "finance",
                "email": "pending@example.com",
            })
        with app.app_context():
            invited_user = User.query.filter_by(username="pending.staff").one()
            invited_user_id = invited_user.id
            old_hash = invited_user.invitation_token_hash
        resend_csrf = self.csrf_token(self.client, "/staff")
        with patch("app.send_staff_invitation_email", return_value=True) as send_email:
            response = self.client.post(
                f"/staff/{invited_user_id}/invitation",
                data={"csrf_token": resend_csrf},
            )
        self.assertEqual(response.status_code, 302)
        send_email.assert_called_once()
        with app.app_context():
            invited_user = db.session.get(User, invited_user_id)
            self.assertNotEqual(invited_user.invitation_token_hash, old_hash)
            self.assertFalse(invited_user.enabled)

    def test_changing_pending_invitation_email_reissues_a_fresh_link(self):
        self.login("admin")
        csrf = self.csrf_token(self.client, "/staff")
        with patch("app.send_staff_invitation_email", return_value=False) as initial_email:
            self.client.post("/staff", data={
                "csrf_token": csrf,
                "display_name": "Pat Pending",
                "username": "pat.pending",
                "role": "teacher",
                "email": "old@example.com",
            })
        old_token = initial_email.call_args.args[1]
        with app.app_context():
            invited_user = User.query.filter_by(username="pat.pending").one()
            invited_user_id = invited_user.id
        update_csrf = self.csrf_token(self.client, "/staff")
        with patch("app.send_staff_invitation_email", return_value=True) as updated_email:
            response = self.client.post(
                f"/staff/{invited_user_id}/update",
                data={
                    "csrf_token": update_csrf,
                    "display_name": "Patricia Pending",
                    "role": "teacher",
                    "email": "new@example.com",
                },
            )
        self.assertEqual(response.status_code, 302)
        updated_email.assert_called_once()
        with app.app_context():
            invited_user = db.session.get(User, invited_user_id)
            self.assertEqual(invited_user.email, "new@example.com")
            self.assertEqual(invited_user.display_name, "Patricia Pending")
            self.assertFalse(invited_user.enabled)
        old_link = app.test_client().get(
            f"/staff-invitation/{old_token}", follow_redirects=True
        )
        self.assertIn(b"invalid or expired", old_link.data)

    def test_admin_password_reset_sends_link_without_handling_password(self):
        self.login("admin")
        with app.app_context():
            registrar = User.query.filter_by(username="registrar").one()
            registrar.email = "registrar@example.com"
            original_password_hash = registrar.password_hash
            registrar_id = registrar.id
            db.session.commit()
        csrf = self.csrf_token(self.client, "/staff")
        with patch("app.send_password_reset_email", return_value=True) as send_email:
            response = self.client.post(
                f"/staff/{registrar_id}/password",
                data={"csrf_token": csrf},
            )
        self.assertEqual(response.status_code, 302)
        send_email.assert_called_once()
        with app.app_context():
            registrar = db.session.get(User, registrar_id)
            self.assertEqual(registrar.password_hash, original_password_hash)
            self.assertIsNotNone(registrar.reset_token_hash)

    def test_staff_password_reset_link_is_single_use(self):
        with app.app_context():
            registrar = User.query.filter_by(username="registrar").one()
            registrar.email = "registrar@example.com"
            db.session.commit()
        csrf = self.csrf_token(self.client, "/password-reset")
        with patch("app.send_password_reset_email", return_value=True) as send_email:
            response = self.client.post("/password-reset", data={"csrf_token": csrf, "email": "registrar@example.com"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        reset_token = send_email.call_args.args[1]
        reset_csrf = self.csrf_token(self.client, f"/password-reset/{reset_token}")
        response = self.client.post(
            f"/password-reset/{reset_token}",
            data={"csrf_token": reset_csrf, "password": "new-test-password-123", "confirm_password": "new-test-password-123"},
            follow_redirects=True,
        )
        self.assertIn(b"Your password has been reset", response.data)
        response = self.client.get(f"/password-reset/{reset_token}", follow_redirects=True)
        self.assertIn(b"invalid or expired", response.data)
        with app.app_context():
            self.assertTrue(User.query.filter_by(username="registrar").one().check_password("new-test-password-123"))

    def test_role_permissions_restrict_school_sections(self):
        self.assign_teacher()
        cases = {
            "registrar": {"/students": 200, "/classes": 200, "/fees": 403, "/grades": 403, "/reports": 403},
            "teacher": {"/students": 200, "/attendance": 200, "/grades": 200, "/reports": 200, "/fees": 403},
            "finance": {"/students": 200, "/fees": 200, "/attendance": 403, "/classes": 403, "/reports": 403},
            "admin": {"/students": 200, "/classes": 200, "/fees": 200, "/grades": 200, "/reports": 200},
        }
        for role, expected_statuses in cases.items():
            with self.subTest(role=role):
                client = app.test_client()
                self.login(role, client)
                for path, expected_status in expected_statuses.items():
                    with self.subTest(path=path):
                        self.assertEqual(client.get(path).status_code, expected_status)

    def test_main_record_lists_are_paginated_and_preserve_filters(self):
        with app.app_context():
            for number in range(1, 31):
                student = Student(
                    student_number=f"PAGE-{number:03d}",
                    name=f"Page Student {number:02d}",
                )
                db.session.add(student)
                db.session.flush()
                db.session.add_all([
                    Enrollment(student_id=student.id, class_id=self.class_id),
                    Fee(
                        student_id=student.id,
                        description=f"Page fee {number:02d}",
                        amount=Decimal("10.00"),
                    ),
                    Grade(
                        student_id=student.id,
                        class_id=self.class_id,
                        subject=f"Page subject {number:02d}",
                        term="Term 1",
                        score=Decimal("80.00"),
                    ),
                ])
            db.session.commit()

        self.login("admin")
        response = self.client.get("/students?q=Page&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Page Student 26", response.data)
        self.assertNotIn(b"Page Student 01", response.data)
        self.assertIn(b"q=Page", response.data)
        self.assertIn(b"of 30", response.data)

        response = self.client.get("/enrollments?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<td>Page Student 05</td>", response.data)
        self.assertNotIn(b"<td>Page Student 30</td>", response.data)

        response = self.client.get("/fees?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Page fee 05", response.data)
        self.assertNotIn(b"Page fee 30", response.data)

        response = self.client.get("/grades?term=Term+1&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Page subject 05", response.data)
        self.assertNotIn(b"Page subject 30", response.data)
        self.assertIn(b"term=Term+1", response.data)

    def test_registrar_can_create_student(self):
        self.login("registrar")
        token = self.csrf_token(self.client, "/students/new")
        response = self.client.post(
            "/students/new",
            data={
                "csrf_token": token,
                "student_number": "S-002",
                "name": "New Student",
                "email": "new@example.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(
                Student.query.filter_by(student_number="S-002").first()
            )

    def test_student_form_rejects_invalid_email_and_future_date(self):
        self.login("registrar")
        token = self.csrf_token(self.client, "/students/new")
        response = self.client.post(
            "/students/new",
            data={
                "csrf_token": token,
                "student_number": "S-INVALID",
                "name": "Invalid Student",
                "email": "not-an-email",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Enter a valid email address", response.data)
        token = self.csrf_token(self.client, "/students/new")
        response = self.client.post(
            "/students/new",
            data={
                "csrf_token": token,
                "student_number": "S-FUTURE",
                "name": "Future Student",
                "date_of_birth": "2999-01-01",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Date of birth cannot be in the future.", response.data)
        with app.app_context():
            self.assertIsNone(Student.query.filter_by(student_number="S-INVALID").first())
            self.assertIsNone(Student.query.filter_by(student_number="S-FUTURE").first())

    def test_registrar_can_edit_student_profile(self):
        self.login("registrar")
        token = self.csrf_token(
            self.client, f"/students/{self.student_id}/edit"
        )
        response = self.client.post(
            f"/students/{self.student_id}/edit",
            data={
                "csrf_token": token,
                "student_number": "S-001",
                "name": "Updated Student",
                "date_of_birth": "2012-04-03",
                "gender": "Other",
                "email": "updated@example.com",
                "phone": "555-0100",
                "address": "New address",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            student = db.session.get(Student, self.student_id)
            self.assertEqual(student.name, "Updated Student")
            self.assertEqual(student.email, "updated@example.com")
            self.assertEqual(student.date_of_birth.isoformat(), "2012-04-03")

    def test_registrar_can_add_and_remove_guardian_contact(self):
        self.login("registrar")
        token = self.csrf_token(self.client, f"/students/{self.student_id}")
        response = self.client.post(
            f"/students/{self.student_id}/guardians",
            data={
                "csrf_token": token,
                "name": "Parent Name",
                "relationship": "Parent",
                "phone": "555-0111",
                "email": "parent@example.com",
                "is_emergency_contact": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            guardian = Guardian.query.filter_by(student_id=self.student_id).one()
            guardian_id = guardian.id
            self.assertTrue(guardian.is_emergency_contact)
            self.assertEqual(guardian.email, "parent@example.com")
        token = self.csrf_token(self.client, f"/students/{self.student_id}")
        response = self.client.post(
            f"/students/{self.student_id}/guardians/{guardian_id}/delete",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Guardian.query.filter_by(student_id=self.student_id).count(), 0)

    def test_archiving_preserves_records_and_hides_student_by_default(self):
        with app.app_context():
            db.session.add(
                Enrollment(student_id=self.student_id, class_id=self.class_id)
            )
            db.session.commit()
        self.login("registrar")
        token = self.csrf_token(
            self.client, f"/students/{self.student_id}"
        )
        response = self.client.post(
            f"/students/{self.student_id}/archive",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"Test Student", self.client.get("/students").data)
        self.assertIn(
            b"Test Student",
            self.client.get("/students?archived=1").data,
        )
        self.assertIn(
            b"Grade 1",
            self.client.get(f"/students/{self.student_id}").data,
        )
        with app.app_context():
            student = db.session.get(Student, self.student_id)
            self.assertFalse(student.active)
            self.assertEqual(Enrollment.query.filter_by(student_id=self.student_id).count(), 1)

    def test_archived_student_cannot_be_enrolled(self):
        with app.app_context():
            db.session.get(Student, self.student_id).active = False
            db.session.commit()
        self.login("registrar")
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            "/enrollments",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": self.class_id,
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Enrollment.query.count(), 0)

    def test_duplicate_active_enrollment_is_rejected(self):
        self.enroll_student()
        self.login("registrar")
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            "/enrollments",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": self.class_id,
            },
            follow_redirects=True,
        )
        self.assertIn(b"already actively enrolled", response.data)
        with app.app_context():
            self.assertEqual(Enrollment.query.count(), 1)

    def test_status_change_preserves_history_and_allows_reenrollment(self):
        self.enroll_student()
        with app.app_context():
            enrollment_id = Enrollment.query.one().id
        self.login("registrar")
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            f"/enrollments/{enrollment_id}/status",
            data={"csrf_token": token, "status": "completed"},
            follow_redirects=True,
        )
        self.assertIn(b"Enrollment status updated.", response.data)
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            "/enrollments",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": self.class_id,
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            records = Enrollment.query.order_by(Enrollment.id).all()
            self.assertEqual([record.status for record in records], ["completed", "active"])

    def test_invalid_enrollment_status_is_rejected(self):
        self.enroll_student()
        with app.app_context():
            enrollment_id = Enrollment.query.one().id
        self.login("registrar")
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            f"/enrollments/{enrollment_id}/status",
            data={"csrf_token": token, "status": "expelled"},
            follow_redirects=True,
        )
        self.assertIn(b"Select a valid enrollment status.", response.data)
        with app.app_context():
            self.assertEqual(Enrollment.query.one().status, "active")

    def test_school_year_can_be_archived_and_reopened_without_losing_records(self):
        self.login("registrar")
        token = self.csrf_token(self.client, "/school-years")
        response = self.client.post(
            "/school-years",
            data={"csrf_token": token, "name": "2027-28"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            year_id = SchoolYear.query.filter_by(name="2027-28").one().id
            same_name_class = SchoolClass(
                name="Grade 1",
                academic_year="2027-28",
            )
            db.session.add(same_name_class)
            db.session.commit()
            class_id = same_name_class.id

        with app.app_context():
            teacher_id = User.query.filter_by(username="teacher").one().id
        token = self.csrf_token(self.client, "/classes")
        self.client.post(
            f"/classes/{class_id}/teachers",
            data={
                "csrf_token": token,
                "teacher_id": teacher_id,
                "action": "assign",
            },
        )
        token = self.csrf_token(self.client, "/enrollments")
        self.client.post(
            "/enrollments",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": class_id,
            },
        )
        token = self.csrf_token(self.client, "/school-years")
        response = self.client.post(
            f"/school-years/{year_id}/archive",
            data={"csrf_token": token, "action": "archive"},
        )
        self.assertEqual(response.status_code, 302)
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            "/enrollments",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": class_id,
            },
            follow_redirects=True,
        )
        self.assertIn(b"valid class", response.data)
        with app.app_context():
            self.assertTrue(db.session.get(SchoolYear, year_id).archived)
            self.assertEqual(SchoolClass.query.filter_by(name="Grade 1").count(), 2)
            self.assertEqual(Enrollment.query.count(), 1)

        teacher_client = app.test_client()
        self.login("teacher", teacher_client)
        token = self.csrf_token(teacher_client, "/attendance")
        response = teacher_client.post(
            "/attendance",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": class_id,
                "date": "2027-09-01",
                "status": "Present",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Attendance.query.count(), 0)

        token = self.csrf_token(self.client, "/school-years")
        self.client.post(
            f"/school-years/{year_id}/archive",
            data={"csrf_token": token, "action": "reopen"},
        )
        with app.app_context():
            self.assertFalse(db.session.get(SchoolYear, year_id).archived)

    def test_school_year_rejects_empty_long_and_duplicate_names(self):
        self.login("registrar")
        for name in ("", "x" * 21, "2026-27"):
            with self.subTest(name=name):
                token = self.csrf_token(self.client, "/school-years")
                response = self.client.post(
                    "/school-years",
                    data={"csrf_token": token, "name": name},
                    follow_redirects=True,
                )
                if name == "2026-27":
                    self.assertIn(b"already exists", response.data)
                else:
                    self.assertIn(b"school year name", response.data)
        with app.app_context():
            self.assertEqual(SchoolYear.query.count(), 1)

    def test_registrar_can_promote_students_and_complete_prior_enrollment(self):
        self.enroll_student()
        with app.app_context():
            next_year = SchoolYear(name="2027-28")
            destination = SchoolClass(
                name="Grade 2",
                academic_year="2027-28",
                school_year=next_year,
            )
            db.session.add_all([next_year, destination])
            db.session.commit()
            destination_id = destination.id
        self.login("registrar")
        response = self.client.get(f"/promotions?source_class_id={self.class_id}")
        self.assertIn(b"Test Student", response.data)
        token = self.csrf_token(self.client, f"/promotions?source_class_id={self.class_id}")
        response = self.client.post(
            "/promotions",
            data={
                "csrf_token": token,
                "source_class_id": self.class_id,
                "destination_class_id": destination_id,
                "student_ids": [str(self.student_id)],
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            enrollments = Enrollment.query.order_by(Enrollment.id).all()
            self.assertEqual(len(enrollments), 2)
            self.assertEqual(enrollments[0].status, "completed")
            self.assertEqual(enrollments[0].class_id, self.class_id)
            self.assertEqual(enrollments[1].status, "active")
            self.assertEqual(enrollments[1].class_id, destination_id)

    def test_promotion_rejects_already_enrolled_destination_without_partial_changes(self):
        self.enroll_student()
        with app.app_context():
            second_student = Student(
                student_number="S-004",
                name="Another Student",
            )
            target_year = SchoolYear(name="2027-28")
            target_class = SchoolClass(
                name="Grade 2",
                academic_year="2027-28",
                school_year=target_year,
            )
            db.session.add_all([second_student, target_year, target_class])
            db.session.flush()
            db.session.add_all([
                Enrollment(
                    student_id=second_student.id,
                    class_id=self.class_id,
                ),
                Enrollment(
                    student_id=second_student.id,
                    class_id=target_class.id,
                ),
            ])
            second_student_id = second_student.id
            target_class_id = target_class.id
            db.session.commit()
        self.login("registrar")
        token = self.csrf_token(self.client, "/promotions")
        response = self.client.post(
            "/promotions",
            data={
                "csrf_token": token,
                "source_class_id": self.class_id,
                "destination_class_id": target_class_id,
                "student_ids": [str(self.student_id), str(second_student_id)],
            },
            follow_redirects=True,
        )
        self.assertIn(b"already active in the destination class", response.data)
        with app.app_context():
            source_records = Enrollment.query.filter_by(
                class_id=self.class_id,
            ).order_by(Enrollment.student_id).all()
            self.assertEqual(
                [record.status for record in source_records],
                ["active", "active"],
            )
            self.assertEqual(
                Enrollment.query.filter_by(class_id=target_class_id).count(),
                1,
            )

    def test_completed_enrollment_cannot_be_reactivated_in_archived_year(self):
        self.enroll_student()
        with app.app_context():
            enrollment = Enrollment.query.one()
            enrollment.status = "completed"
            school_year = SchoolYear.query.filter_by(name="2026-27").one()
            school_year.archived = True
            enrollment_id = enrollment.id
            db.session.commit()
        self.login("registrar")
        token = self.csrf_token(self.client, "/enrollments")
        response = self.client.post(
            f"/enrollments/{enrollment_id}/status",
            data={"csrf_token": token, "status": "active"},
            follow_redirects=True,
        )
        self.assertIn(b"Archived school years cannot have active enrollments.", response.data)
        with app.app_context():
            self.assertEqual(db.session.get(Enrollment, enrollment_id).status, "completed")

    def test_teacher_can_record_and_update_attendance(self):
        self.assign_teacher()
        self.enroll_student()
        self.login("teacher")
        token = self.csrf_token(self.client, "/attendance")
        data = {
            "csrf_token": token,
            "student_id": self.student_id,
            "class_id": self.class_id,
            "date": "2026-09-01",
            "status": "Present",
        }
        self.assertEqual(self.client.post("/attendance", data=data).status_code, 302)
        data["status"] = "Late"
        self.assertEqual(self.client.post("/attendance", data=data).status_code, 302)
        with app.app_context():
            self.assertEqual(Attendance.query.count(), 1)
            self.assertEqual(Attendance.query.first().status, "Late")

    def test_teacher_can_bulk_record_and_filter_attendance(self):
        self.assign_teacher()
        self.enroll_student()
        with app.app_context():
            second_student = Student(student_number="S-003", name="Second Student")
            db.session.add(second_student)
            db.session.flush()
            db.session.add(Enrollment(
                student_id=second_student.id,
                class_id=self.class_id,
            ))
            second_student_id = second_student.id
            db.session.commit()

        self.login("teacher")
        query = f"/attendance?class_id={self.class_id}&date=2026-09-01"
        response = self.client.get(query)
        self.assertIn(b"Bulk attendance", response.data)
        self.assertIn(b"Second Student", response.data)
        token = self.csrf_token(self.client, query)
        bulk_data = {
            "csrf_token": token,
            "class_id": str(self.class_id),
            "date": "2026-09-01",
            f"status_{self.student_id}": "Present",
            f"status_{second_student_id}": "Absent",
        }
        response = self.client.post("/attendance/bulk", data=bulk_data)
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Attendance.query.count(), 2)
            statuses = {
                record.student_id: record.status
                for record in Attendance.query.all()
            }
            self.assertEqual(statuses[self.student_id], "Present")
            self.assertEqual(statuses[second_student_id], "Absent")

        bulk_data[f"status_{self.student_id}"] = "Late"
        self.client.post("/attendance/bulk", data=bulk_data)
        with app.app_context():
            self.assertEqual(Attendance.query.count(), 2)
            self.assertEqual(
                Attendance.query.filter_by(student_id=self.student_id).one().status,
                "Late",
            )
        filtered = self.client.get(query)
        self.assertIn(b"Late", filtered.data)
        self.assertIn(b"Absent", filtered.data)

    def test_bulk_attendance_rejects_missing_or_invalid_status(self):
        self.assign_teacher()
        self.enroll_student()
        self.login("teacher")
        token = self.csrf_token(self.client, "/attendance")
        response = self.client.post(
            "/attendance/bulk",
            data={
                "csrf_token": token,
                "class_id": str(self.class_id),
                "date": "2026-09-01",
                f"status_{self.student_id}": "On vacation",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Attendance.query.count(), 0)

    def test_bulk_attendance_and_report_card_respect_teacher_class_scope(self):
        self.assign_teacher()
        self.enroll_student()
        with app.app_context():
            other_class = SchoolClass(name="Grade 4", academic_year="2026-27")
            db.session.add(other_class)
            db.session.commit()
            other_class_id = other_class.id
        self.login("teacher")
        token = self.csrf_token(self.client, "/attendance")
        response = self.client.post(
            "/attendance/bulk",
            data={
                "csrf_token": token,
                "class_id": str(other_class_id),
                "date": "2026-09-01",
                f"status_{self.student_id}": "Present",
            },
        )
        self.assertEqual(response.status_code, 403)
        report_response = self.client.get(
            f"/reports?class_id={other_class_id}"
            f"&student_id={self.student_id}&term=Term%201"
        )
        self.assertEqual(report_response.status_code, 403)

    def test_finance_user_can_record_fee(self):
        self.login("finance")
        token = self.csrf_token(self.client, "/fees")
        response = self.client.post(
            "/fees",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "description": "Tuition",
                "amount": "500.00",
                "due_date": "2026-09-30",
                "academic_year": "2026-27",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Fee.query.count(), 1)
            fee_id = Fee.query.first().id
            self.assertEqual(Fee.query.first().paid_amount, 0)
            self.assertEqual(Fee.query.first().academic_year, "2026-27")

        token = self.csrf_token(self.client, "/fees")
        response = self.client.post(
            f"/fees/{fee_id}/payments",
            data={
                "csrf_token": token,
                "amount": "125.00",
                "payment_date": "2026-09-15",
                "payment_method": "Bank transfer",
                "reference": "TX-123",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            fee = db.session.get(Fee, fee_id)
            self.assertEqual(str(fee.paid_amount), "125.00")
            self.assertEqual(str(fee.amount - fee.paid_amount), "375.00")
            payment = Payment.query.one()
            self.assertEqual(payment.payment_date, date(2026, 9, 15))
            self.assertEqual(payment.payment_method, "Bank transfer")
            self.assertEqual(payment.reference, "TX-123")
        response = self.client.get("/fees")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Bank transfer", response.data)
        self.assertIn(b"375.00", response.data)
        self.assertIn(b"125.00", self.client.get("/").data)

    def test_finance_student_directory_and_export_hide_contact_details(self):
        with app.app_context():
            student = db.session.get(Student, self.student_id)
            student.email = "private@example.test"
            student.phone = "555-0100"
            db.session.commit()
        self.login("finance")
        response = self.client.get("/students")
        self.assertNotIn(b"private@example.test", response.data)
        self.assertNotIn(b"555-0100", response.data)
        response = self.client.get("/exports/students.csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_streamed)
        self.assertNotIn(b"Email", response.data)
        self.assertNotIn(b"Phone", response.data)
        self.assertNotIn(b"private@example.test", response.data)
        self.assertNotIn(b"555-0100", response.data)

    def test_csv_export_streaming_keeps_formula_injection_protection(self):
        with app.app_context():
            student = db.session.get(Student, self.student_id)
            student.name = "=HYPERLINK(\"https://example.test\")"
            db.session.commit()
        self.login("registrar")

        response = self.client.get("/exports/students.csv")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_streamed)
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"'=HYPERLINK", response.data)

    def test_payment_cannot_exceed_outstanding_balance(self):
        self.login("finance")
        with app.app_context():
            fee = Fee(
                student_id=self.student_id,
                description="Tuition",
                amount="100.00",
            )
            db.session.add(fee)
            db.session.commit()
            fee_id = fee.id

        token = self.csrf_token(self.client, "/fees")
        response = self.client.post(
            f"/fees/{fee_id}/payments",
            data={
                "csrf_token": token,
                "amount": "100.01",
                "payment_date": "2026-09-15",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Payment.query.count(), 0)

    def test_payment_rejects_non_positive_or_invalid_precision(self):
        self.login("finance")
        with app.app_context():
            fee = Fee(
                student_id=self.student_id,
                description="Tuition",
                amount="100.00",
            )
            db.session.add(fee)
            db.session.commit()
            fee_id = fee.id

        for amount in ("0", "-1.00", "1.001", "NaN"):
            with self.subTest(amount=amount):
                token = self.csrf_token(self.client, "/fees")
                response = self.client.post(
                    f"/fees/{fee_id}/payments",
                    data={
                        "csrf_token": token,
                        "amount": amount,
                        "payment_date": "2026-09-15",
                    },
                )
                self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Payment.query.count(), 0)

    def test_fee_creation_rejects_invalid_amount_date_student_and_school_year(self):
        self.login("finance")
        with app.app_context():
            inactive_student = Student(
                student_number="S-INACTIVE",
                name="Inactive Student",
                active=False,
            )
            archived_year = SchoolYear(name="2025-26", archived=True)
            db.session.add_all([inactive_student, archived_year])
            db.session.commit()
            inactive_student_id = inactive_student.id

        invalid_fees = [
            {"amount": "0"},
            {"amount": "-1.00"},
            {"amount": "1.001"},
            {"amount": "NaN"},
            {"amount": "10000000000.00"},
            {"amount": "10.00", "due_date": "not-a-date"},
            {"amount": "10.00", "student_id": "999999"},
            {
                "amount": "10.00",
                "student_id": str(inactive_student_id),
            },
            {"amount": "10.00", "academic_year": "2025-26"},
        ]
        for invalid_fee in invalid_fees:
            with self.subTest(invalid_fee=invalid_fee):
                token = self.csrf_token(self.client, "/fees")
                data = {
                    "csrf_token": token,
                    "student_id": self.student_id,
                    "description": "Tuition",
                    "amount": invalid_fee.get("amount", "10.00"),
                    "due_date": invalid_fee.get("due_date", "2026-09-01"),
                    "academic_year": invalid_fee.get("academic_year", "2026-27"),
                }
                data["student_id"] = invalid_fee.get("student_id", str(self.student_id))
                response = self.client.post("/fees", data=data)
                self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Fee.query.count(), 0)

    def test_payment_rejects_invalid_date_and_overlong_reference(self):
        self.login("finance")
        with app.app_context():
            fee = Fee(
                student_id=self.student_id,
                description="Tuition",
                amount="100.00",
            )
            db.session.add(fee)
            db.session.commit()
            fee_id = fee.id

        invalid_payments = [
            {"amount": "10.00", "payment_date": "not-a-date"},
            {
                "amount": "10.00",
                "payment_date": "2026-09-01",
                "reference": "R" * 121,
            },
            {"amount": "10.00"},
        ]
        for invalid_payment in invalid_payments:
            with self.subTest(invalid_payment=invalid_payment):
                token = self.csrf_token(self.client, "/fees")
                response = self.client.post(
                    f"/fees/{fee_id}/payments",
                    data={"csrf_token": token, **invalid_payment},
                )
                self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Payment.query.count(), 0)

    def test_teacher_can_record_grade(self):
        self.assign_teacher()
        self.enroll_student()
        self.login("teacher")
        token = self.csrf_token(self.client, "/grades")
        response = self.client.post(
            "/grades",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": self.class_id,
                "subject": "Mathematics",
                "term": "Term 1",
                "score": "87.50",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Grade.query.count(), 1)
            self.assertEqual(str(Grade.query.first().score), "87.50")

    def test_grade_rejects_invalid_range_and_precision(self):
        self.assign_teacher()
        self.enroll_student()
        self.login("teacher")
        token = self.csrf_token(self.client, "/grades")
        for score in ("-0.01", "100.01", "87.501", "NaN"):
            with self.subTest(score=score):
                response = self.client.post(
                    "/grades",
                    data={
                        "csrf_token": token,
                        "student_id": self.student_id,
                        "class_id": self.class_id,
                        "subject": "Mathematics",
                        "term": "Term 1",
                        "score": score,
                    },
                )
                self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(Grade.query.count(), 0)

    def test_report_card_filters_grades_and_summarizes_attendance(self):
        self.assign_teacher()
        self.enroll_student()
        with app.app_context():
            db.session.add_all([
                Grade(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    subject="Mathematics",
                    term="Term 1",
                    score="80.00",
                ),
                Grade(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    subject="Mathematics",
                    term="Term 1",
                    score="100.00",
                ),
                Grade(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    subject="English",
                    term="Term 1",
                    score="70.00",
                ),
                Grade(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    subject="Mathematics",
                    term="Term 2",
                    score="70.00",
                ),
                Attendance(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    date=date(2026, 9, 1),
                    status="Present",
                ),
                Attendance(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    date=date(2026, 9, 2),
                    status="Late",
                ),
            ])
            db.session.commit()
        self.login("teacher")
        class_only_response = self.client.get(f"/reports?class_id={self.class_id}")
        self.assertEqual(class_only_response.status_code, 200)
        self.assertIn(b'data-student-picker', class_only_response.data)
        self.assertNotIn(b'Test Student', class_only_response.data)
        self.assertIn(b"Term 1", class_only_response.data)
        response = self.client.get(
            f"/reports?class_id={self.class_id}"
            f"&student_id={self.student_id}&term=Term%201"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<td>Mathematics</td><td>90.00</td>", response.data)
        self.assertIn(b"<td>English</td><td>70.00</td>", response.data)
        self.assertIn(b"<strong>80.00</strong>", response.data)
        self.assertNotIn(b"<strong>83.33</strong>", response.data)
        self.assertIn(b"Present", response.data)
        self.assertIn(b"Late", response.data)
        self.assertNotIn(b"<td>Mathematics</td><td>70.00</td>", response.data)

    def test_school_reports_summarize_year_and_outstanding_fee_balance(self):
        self.enroll_student()
        with app.app_context():
            fee = Fee(
                student_id=self.student_id,
                description="Tuition",
                amount="100.00",
                academic_year="2026-27",
            )
            db.session.add_all([
                fee,
                Attendance(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    date=date(2026, 9, 1),
                    status="Present",
                ),
                Grade(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    subject="Mathematics",
                    term="Term 1",
                    score="80.00",
                ),
            ])
            db.session.flush()
            db.session.add(Payment(
                fee_id=fee.id,
                amount="25.00",
                payment_date=date(2026, 9, 1),
            ))
            db.session.commit()

        self.login("registrar")
        response = self.client.get("/school-reports?year=2026-27")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Active", response.data)
        self.assertIn(b"Present", response.data)
        self.assertIn(b"Term 1", response.data)
        self.assertIn(b"<td>2026-27</td><td>Active</td><td>1</td>", response.data)
        self.assertIn(b"<td>2026-27</td><td>Present</td><td>1</td>", response.data)
        self.assertIn(
            b"<td>2026-27</td><td>Term 1</td><td>1</td><td>80.00</td>",
            response.data,
        )
        self.assertNotIn(b"Outstanding fee balances", response.data)

        finance_client = app.test_client()
        self.login("finance", finance_client)
        finance_report = finance_client.get("/school-reports?year=2026-27")
        self.assertEqual(finance_report.status_code, 200)
        self.assertIn(b"Outstanding fee balances", finance_report.data)
        self.assertIn(b"75.00", finance_report.data)
        self.assertIn(b"Total outstanding: <strong>75.00</strong>", finance_report.data)
        self.assertNotIn(b"Grades by year", finance_report.data)

    def test_registrar_can_assign_and_remove_teacher_from_class(self):
        self.login("registrar")
        token = self.csrf_token(self.client, "/classes")
        with app.app_context():
            teacher_id = User.query.filter_by(username="teacher").one().id
        response = self.client.post(
            f"/classes/{self.class_id}/teachers",
            data={
                "csrf_token": token,
                "teacher_id": teacher_id,
                "action": "assign",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                TeacherClassAssignment.query.filter_by(
                    teacher_id=teacher_id, class_id=self.class_id
                ).count(),
                1,
            )
        token = self.csrf_token(self.client, "/classes")
        response = self.client.post(
            f"/classes/{self.class_id}/teachers",
            data={
                "csrf_token": token,
                "teacher_id": teacher_id,
                "action": "remove",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(TeacherClassAssignment.query.count(), 0)

    def test_teacher_only_sees_assigned_classes_and_enrolled_students(self):
        self.assign_teacher()
        self.enroll_student()
        with app.app_context():
            other_class = SchoolClass(
                name="Grade 2",
                academic_year="2026-27",
            )
            other_student = Student(
                student_number="S-002",
                name="Other Student",
            )
            db.session.add_all([other_class, other_student])
            db.session.flush()
            db.session.add(
                Enrollment(student_id=other_student.id, class_id=other_class.id)
            )
            db.session.add_all([
                Attendance(
                    student_id=other_student.id,
                    class_id=other_class.id,
                    date=date(2026, 9, 1),
                    status="Present",
                ),
                Grade(
                    student_id=other_student.id,
                    class_id=other_class.id,
                    subject="Mathematics",
                    term="Term 1",
                    score="90.00",
                ),
            ])
            other_class_id = other_class.id
            db.session.commit()

        self.login("teacher")
        students_response = self.client.get("/students")
        attendance_response = self.client.get("/attendance")
        grades_response = self.client.get("/grades")
        self.assertIn(b"Test Student", students_response.data)
        self.assertNotIn(b"Other Student", students_response.data)
        self.assertNotIn(b"Grade 2", attendance_response.data)
        self.assertNotIn(b"Other Student", attendance_response.data)
        self.assertNotIn(b"Grade 2", grades_response.data)
        self.assertNotIn(b"Other Student", grades_response.data)
        school_report = self.client.get("/school-reports?year=2026-27")
        self.assertIn(b"<td>2026-27</td><td>Active</td><td>1</td>", school_report.data)
        self.assertNotIn(b"<td>2026-27</td><td>Present</td>", school_report.data)
        self.assertNotIn(b"Other Student", school_report.data)

        token = self.csrf_token(self.client, "/attendance")
        response = self.client.post(
            "/attendance",
            data={
                "csrf_token": token,
                "student_id": self.student_id,
                "class_id": other_class_id,
                "date": "2026-09-02",
                "status": "Present",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_student_search_is_bounded_minimal_and_excludes_inactive_students(self):
        with app.app_context():
            db.session.add_all([
                Student(student_number=f"LARGE-{index:03}", name=f"Search Student {index:03}")
                for index in range(25)
            ])
            db.session.add(Student(
                student_number="OLD-001", name="Archived Search Student", active=False
            ))
            db.session.commit()

        self.login("finance")
        short_query = self.client.get("/api/student-options?q=S")
        self.assertEqual(short_query.status_code, 200)
        self.assertEqual(short_query.json["results"], [])
        response = self.client.get("/api/student-options?q=Search%20Student")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["results"]), 20)
        self.assertEqual(
            set(response.json["results"][0]),
            {"id", "label"},
        )
        self.assertNotIn("Archived Search Student", str(response.json))

    def test_student_search_obeys_teacher_class_assignments(self):
        self.assign_teacher()
        self.enroll_student()
        with app.app_context():
            other_class = SchoolClass(name="Grade 2", academic_year="2026-27")
            other_student = Student(student_number="S-002", name="Other Student")
            db.session.add_all([other_class, other_student])
            db.session.flush()
            db.session.add(Enrollment(student_id=other_student.id, class_id=other_class.id))
            other_class_id = other_class.id
            db.session.commit()

        self.login("teacher")
        response = self.client.get("/api/student-options?q=Test&enrolled=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["label"] for item in response.json["results"]], ["S-001 — Test Student"])
        forbidden = self.client.get(
            f"/api/student-options?q=Other&class_id={other_class_id}"
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_high_volume_record_indexes_exist(self):
        expected = {
            "student": {"ix_student_active_name"},
            "enrollment": {
                "ix_enrollment_class_status_student",
                "ix_enrollment_student_status_class",
            },
            "fee": {"ix_fee_student_id_id"},
            "attendance": {"ix_attendance_class_date_student"},
            "grade": {"ix_grade_class_id_id", "ix_grade_class_student_term"},
        }
        with app.app_context():
            inspector = inspect(db.engine)
            for table, index_names in expected.items():
                actual = {index["name"] for index in inspector.get_indexes(table)}
                self.assertTrue(index_names.issubset(actual), f"Missing indexes on {table}: {index_names - actual}")

    def test_fee_list_uses_bounded_queries_for_related_payment_data(self):
        with app.app_context():
            student = db.session.get(Student, self.student_id)
            for index in range(30):
                fee = Fee(
                    student=student,
                    description=f"Test charge {index}",
                    amount=Decimal("100.00"),
                )
                db.session.add(fee)
                db.session.flush()
                db.session.add(Payment(fee=fee, amount=Decimal("10.00")))
            db.session.commit()
            engine = db.engine

        self.login("finance")
        select_statements = []

        def capture_select(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                select_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            response = self.client.get("/fees")
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(select_statements), 12)
        fees_export = self.client.get("/exports/fees.csv")
        self.assertEqual(fees_export.status_code, 200)
        self.assertTrue(fees_export.is_streamed)
        self.assertIn(b"Test charge 0", fees_export.data)
        statement_export = self.client.get(
            f"/fees/student/{self.student_id}/statement.csv"
        )
        self.assertEqual(statement_export.status_code, 200)
        self.assertTrue(statement_export.is_streamed)

    def test_teacher_csv_exports_stream_related_records(self):
        self.assign_teacher()
        self.enroll_student()
        with app.app_context():
            db.session.add_all([
                Attendance(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    date=date(2026, 9, 1),
                    status="Present",
                ),
                Grade(
                    student_id=self.student_id,
                    class_id=self.class_id,
                    subject="Mathematics",
                    term="Term 1",
                    score=Decimal("88.00"),
                ),
            ])
            db.session.commit()
        self.login("teacher")

        attendance_export = self.client.get("/exports/attendance.csv")
        self.assertEqual(attendance_export.status_code, 200)
        self.assertTrue(attendance_export.is_streamed)
        self.assertIn(b"Test Student", attendance_export.get_data())
        grades_export = self.client.get("/exports/grades.csv")

        self.assertEqual(grades_export.status_code, 200)
        self.assertTrue(grades_export.is_streamed)
        self.assertIn(b"Mathematics", grades_export.data)

    def test_create_user_command_hashes_password(self):
        result = app.test_cli_runner().invoke(
            args=["create-user", "--username", "new-admin", "--role", "admin"],
            input="a-long-password-456\na-long-password-456\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        with app.app_context():
            user = User.query.filter_by(username="new-admin").one()
            self.assertTrue(user.check_password("a-long-password-456"))
            self.assertFalse(user.check_password("wrong-password"))


if __name__ == "__main__":
    unittest.main()
