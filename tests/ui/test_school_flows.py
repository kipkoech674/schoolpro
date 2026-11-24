"""Optional Playwright smoke test across the main school workflows.

Install requirements-ui.txt and run ``python -m playwright install chromium``
before invoking this module with unittest.
"""
import os
import tempfile
import threading
import unittest


class SchoolFlowsBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="schoolpro-ui-")
        db_path = os.path.join(cls.temp_dir.name, "ui-test.sqlite").replace("\\", "/")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        os.environ["SECRET_KEY"] = "ui-test-secret-key"
        os.environ["APP_ENV"] = "development"
        os.environ["SESSION_COOKIE_SECURE"] = "false"

        from playwright.sync_api import sync_playwright
        from werkzeug.serving import make_server
        from app import (
            Enrollment,
            SchoolClass,
            SchoolYear,
            Student,
            TeacherClassAssignment,
            User,
            app,
            db,
        )

        cls.sync_playwright = staticmethod(sync_playwright)
        cls.app = app
        cls.db = db
        with app.app_context():
            db.create_all()
            users = {}
            for role in ("admin", "registrar", "teacher", "finance"):
                user = User(username=role, role=role)
                user.set_password("browser-test-password")
                db.session.add(user)
                users[role] = user
            year = SchoolYear(name="2026 - 2027")
            student = Student(student_number="UI-001", name="Browser Student")
            db.session.add_all([year, student])
            db.session.flush()
            school_class = SchoolClass(
                name="Browser Class",
                academic_year=year.name,
                school_year=year,
            )
            db.session.add(school_class)
            db.session.flush()
            db.session.add_all([
                TeacherClassAssignment(
                    teacher_id=users["teacher"].id,
                    class_id=school_class.id,
                ),
                Enrollment(student_id=student.id, class_id=school_class.id),
            ])
            db.session.commit()

        cls.server = make_server("127.0.0.1", 0, app, threaded=True)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server_thread.join(timeout=5)
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.temp_dir.cleanup()

    def setUp(self):
        self.playwright = self.sync_playwright().start()
        # Use the full Chromium build so tests also work when the optional
        # headless-shell download is unavailable.
        self.browser = self.playwright.chromium.launch(channel="chromium")
        self.page = self.browser.new_page()

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

    def login(self, username):
        self.page.goto(f"{self.base_url}/login")
        self.page.get_by_label("Username").fill(username)
        self.page.get_by_label("Password").fill("browser-test-password")
        self.page.get_by_role("button", name="Sign in").click()
        self.page.get_by_role("link", name="Students", exact=True).wait_for()

    def select_student(self, form, search_text, option_label):
        picker = form.locator("[data-student-picker]")
        search = picker.get_by_role("combobox")
        search.fill(search_text)
        option = picker.get_by_role("option", name=option_label, exact=True)
        option.wait_for()
        option.click()

    def test_mobile_navigation_is_keyboard_and_screen_reader_friendly(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.login("registrar")
        menu_button = self.page.get_by_role("button", name="Open menu")
        self.assertEqual(self.page.get_by_role("link", name="Dashboard", exact=True).get_attribute("aria-current"), "page")

        menu_button.click()
        self.assertEqual(self.page.get_by_role("button", name="Close menu").get_attribute("aria-expanded"), "true")
        self.page.keyboard.press("Escape")
        self.assertEqual(self.page.get_by_role("button", name="Open menu").get_attribute("aria-expanded"), "false")
        self.assertNotIn("open", self.page.locator("#sidebar").get_attribute("class"))

        self.page.get_by_role("button", name="Open menu").click()
        self.page.get_by_role("link", name="Classes", exact=True).click()
        self.page.wait_for_url("**/classes")
        self.assertEqual(self.page.locator("#menu-button").get_attribute("aria-expanded"), "false")

    def test_register_enroll_pay_attend_grade_and_manage_school_year(self):
        self.login("registrar")

        self.page.get_by_role("link", name="Students", exact=True).click()
        self.page.get_by_role("link", name="+ Register Student").click()
        self.page.get_by_label("Student number *").fill("UI-002")
        self.page.get_by_label("Full name *").fill("New Browser Student")
        self.page.get_by_role("button", name="Register").click()
        self.page.get_by_role("cell", name="New Browser Student", exact=True).wait_for()

        self.page.goto(f"{self.base_url}/enrollments")
        enrollment_form = self.page.locator('form[method="post"].grid-form').first
        self.select_student(enrollment_form, "New Browser", "UI-002 — New Browser Student")
        enrollment_form.locator('select[name="class_id"]').select_option(label="Browser Class")
        enrollment_form.get_by_role("button", name="Enroll Student").click()
        self.page.get_by_role("cell", name="New Browser Student", exact=True).wait_for()

        self.page.goto(f"{self.base_url}/school-years")
        self.page.get_by_label("School Year Name *").fill("2027 - 2028")
        self.page.get_by_role("button", name="Create School Year").click()
        self.page.get_by_text("2027 - 2028").wait_for()

        self.page = self.browser.new_page()
        self.login("finance")
        self.page.goto(f"{self.base_url}/fees")
        fee_form = self.page.locator('form[method="post"].grid-form').first
        self.select_student(fee_form, "New Browser", "UI-002 — New Browser Student")
        fee_form.get_by_label("Description").fill("Browser tuition")
        fee_form.get_by_label("Amount").fill("250")
        fee_form.get_by_role("button", name="Save Fee").click()
        payment_form = self.page.locator('form[action*="/payments"]').first
        payment_form.locator('input[name="amount"]').fill("100")
        payment_form.locator('input[name="payment_date"]').fill("2026-11-22")
        payment_form.get_by_role("button", name="Record payment").click()
        self.page.get_by_text("150.00").wait_for()

        self.page = self.browser.new_page()
        self.login("teacher")
        self.page.goto(f"{self.base_url}/attendance")
        attendance_form = self.page.locator('form[method="post"].grid-form').first
        attendance_form.locator('select[name="class_id"]').select_option(label="Browser Class")
        self.select_student(attendance_form, "New Browser", "UI-002 — New Browser Student")
        attendance_form.locator('input[name="date"]').fill("2026-11-22")
        attendance_form.locator('select[name="status"]').select_option(label="Present")
        attendance_form.get_by_role("button", name="Record Attendance").click()

        self.page.goto(f"{self.base_url}/grades")
        grade_form = self.page.locator('form[method="post"].grid-form').first
        grade_form.locator('select[name="class_id"]').select_option(label="Browser Class")
        self.select_student(grade_form, "New Browser", "UI-002 — New Browser Student")
        grade_form.locator('input[name="subject"]').fill("Mathematics")
        grade_form.locator('input[name="term"]').fill("Term 1")
        grade_form.locator('input[name="score"]').fill("88")
        grade_form.get_by_role("button", name="Record Grade").click()
        self.page.get_by_text("Mathematics").wait_for()


if __name__ == "__main__":
    unittest.main()
