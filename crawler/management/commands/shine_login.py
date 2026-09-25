import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from playwright.sync_api import sync_playwright

from crawler.browser import is_login_page

# TODO: unconfirmed. No real Shine login-page HTML has been inspected
# in this project — these are best-effort guesses at common
# login-form patterns, tried in order and abandoned (falling back to
# manual login) the moment any one of them doesn't match. Replace with
# confirmed selectors once the real login page's source has been
# checked.
_EMAIL_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "#username",
    "#email",
)
_PASSWORD_SELECTORS = ("input[type='password']", "#password")
_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "button:has-text('Sign in')",
    "button:has-text('Login')",
    "button:has-text('Log in')",
)


class Command(BaseCommand):
    help = (
        "One-time setup: opens a real (non-headless) browser to Shine's "
        "recruiter login page, logs in — automatically if SHINE_LOGIN_EMAIL/"
        "SHINE_LOGIN_PASSWORD are set and the login form matches a common "
        "pattern, otherwise by hand — then saves the resulting session "
        "cookies to SHINE_STORAGE_STATE_PATH so crawler/browser.py can reuse "
        "them without logging in again. Re-run this whenever the saved "
        "session expires."
    )

    def handle(self, *args, **options):
        storage_state_path = Path(settings.SHINE_STORAGE_STATE_PATH)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(settings.SHINE_BASE_URL, wait_until="networkidle")

            if settings.SHINE_LOGIN_EMAIL and settings.SHINE_LOGIN_PASSWORD:
                if self._try_auto_login(page):
                    self.stdout.write(
                        "Attempted auto-login with SHINE_LOGIN_EMAIL/PASSWORD. "
                        "Check the browser window — if it didn't work, finish "
                        "logging in by hand."
                    )
                else:
                    self.stdout.write(
                        "Auto-login didn't find a matching login form "
                        "(selectors are unconfirmed guesses) — please log in "
                        "by hand in the opened browser window."
                    )
            else:
                self.stdout.write(
                    "SHINE_LOGIN_EMAIL/PASSWORD not set — please log in by "
                    "hand in the opened browser window."
                )

            input(
                "\nPress Enter here once you're fully logged in and can see "
                "the recruiter dashboard/search page... "
            )

            if is_login_page(page):
                self.stderr.write(
                    self.style.ERROR(
                        "Still looks like a login page — session was NOT "
                        "saved. Make sure you're fully logged in, then "
                        "re-run this command."
                    )
                )
                browser.close()
                sys.exit(1)

            storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_path))
            self.stdout.write(
                self.style.SUCCESS(f"Saved Shine session to {storage_state_path}")
            )

            browser.close()

    def _try_auto_login(self, page) -> bool:
        email_field = self._first_match(page, _EMAIL_SELECTORS)
        password_field = self._first_match(page, _PASSWORD_SELECTORS)

        if email_field is None or password_field is None:
            return False

        try:
            email_field.fill(settings.SHINE_LOGIN_EMAIL)
            password_field.fill(settings.SHINE_LOGIN_PASSWORD)

            submit_button = self._first_match(page, _SUBMIT_SELECTORS)
            if submit_button is not None:
                submit_button.click()
            else:
                password_field.press("Enter")

            page.wait_for_load_state("networkidle")
            return True
        except Exception:
            # Best-effort only — any failure here just falls back to
            # manual login rather than crashing the command.
            return False

    @staticmethod
    def _first_match(page, selectors):
        for selector in selectors:
            element = page.query_selector(selector)
            if element is not None:
                return element
        return None
