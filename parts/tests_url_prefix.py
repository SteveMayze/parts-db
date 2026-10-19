"""The app behind a reverse proxy that strips a URL prefix such as /parts-db."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import clear_script_prefix, set_script_prefix

PREFIX = "/parts-db"


def _settings_with_env(**env):
    """Evaluate config.settings in a fresh interpreter with the given environment."""
    code = (
        "import sys, json; sys.path.insert(0, sys.argv[1]); import config.settings as s;"
        "print(json.dumps({k: getattr(s, k, None) for k in "
        "['FORCE_SCRIPT_NAME','SECURE_PROXY_SSL_HEADER','CSRF_TRUSTED_ORIGINS','ALLOWED_HOSTS',"
        "'SESSION_COOKIE_NAME','CSRF_COOKIE_NAME','SESSION_COOKIE_PATH','CSRF_COOKIE_PATH']}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(settings.BASE_DIR)],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class SettingsFromEnvironmentTests(TestCase):
    def test_default_is_unchanged_without_prefix(self):
        values = _settings_with_env()
        self.assertIsNone(values["FORCE_SCRIPT_NAME"])
        self.assertIsNone(values["SECURE_PROXY_SSL_HEADER"])
        self.assertEqual(values["CSRF_TRUSTED_ORIGINS"], [])
        self.assertEqual(values["SESSION_COOKIE_PATH"], "/")
        self.assertEqual(values["CSRF_COOKIE_PATH"], "/")

    def test_prefix_proxy_header_origins_and_cookies(self):
        values = _settings_with_env(
            DJANGO_URL_PREFIX="/parts-db/",
            DJANGO_BEHIND_TLS_PROXY="1",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://example.test, http://example.test:8888",
            DJANGO_DEBUG_FALSE="1",
            DJANGO_SECRET_KEY="x" * 40,
            DJANGO_ALLOWED_HOSTS="example.test,other.test",
        )
        self.assertEqual(values["FORCE_SCRIPT_NAME"], PREFIX)
        self.assertEqual(values["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertEqual(
            values["CSRF_TRUSTED_ORIGINS"], ["https://example.test", "http://example.test:8888"]
        )
        self.assertEqual(values["ALLOWED_HOSTS"], ["example.test", "other.test"])
        self.assertEqual(values["SESSION_COOKIE_NAME"], "partsdb_sessionid")
        self.assertEqual(values["CSRF_COOKIE_NAME"], "partsdb_csrftoken")
        self.assertEqual(values["SESSION_COOKIE_PATH"], PREFIX)
        self.assertEqual(values["CSRF_COOKIE_PATH"], PREFIX)


@override_settings(
    FORCE_SCRIPT_NAME=PREFIX,
    STATIC_URL=f"{PREFIX}/static/",  # also resets the cached static files storage
    SESSION_COOKIE_PATH=PREFIX,
    CSRF_COOKIE_PATH=PREFIX,
    SESSION_COOKIE_NAME="partsdb_sessionid",
    CSRF_COOKIE_NAME="partsdb_csrftoken",
)
class PrefixedRoutingTests(TestCase):
    """The proxy strips the prefix, so requests arrive without it."""

    def setUp(self):
        # WSGIHandler does this per request; the test client does not.
        set_script_prefix(PREFIX + "/")
        self.addCleanup(clear_script_prefix)
        self.user = get_user_model().objects.create_superuser("admin", password="pw-for-tests")

    def test_root_redirect_carries_prefix(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{PREFIX}/parts/")

    def test_parts_list_links_and_static_carry_prefix(self):
        response = self.client.get("/parts/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(f'href="{PREFIX}/admin/"', html)
        self.assertIn(f"{PREFIX}/static/parts/theme.css", html)
        self.assertNotIn('href="/admin/"', html)

    def test_forms_post_to_the_prefixed_page_and_redirect_with_prefix(self):
        response = self.client.get("/parts/add/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # No action attribute: the browser posts to the current (prefixed) address.
        self.assertNotRegex(html, r'<form[^>]*action="/(?!parts-db/)')
        self.assertIn(f'href="{PREFIX}/parts/"', html)

        response = self.client.post("/parts/add/", {"part_type": "R", "value": "1k", "quantity": 1})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(f"{PREFIX}/parts/"))

    def test_admin_login_logout_and_cookies(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{PREFIX}/admin/login/?next={PREFIX}/admin/")

        login_page = self.client.get("/admin/login/")
        self.assertIn("partsdb_csrftoken", login_page.cookies)
        self.assertEqual(login_page.cookies["partsdb_csrftoken"]["path"], PREFIX)

        response = self.client.post(
            "/admin/login/",
            {"username": "admin", "password": "pw-for-tests", "next": f"{PREFIX}/admin/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{PREFIX}/admin/")
        self.assertIn("partsdb_sessionid", response.cookies)
        self.assertEqual(response.cookies["partsdb_sessionid"]["path"], PREFIX)
        self.assertNotIn("sessionid", response.cookies)
        self.assertNotIn("csrftoken", response.cookies)

        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/admin/parts/part/").status_code, 200)

        response = self.client.post("/admin/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{PREFIX}/admin/login/")
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_static_files_are_served_at_the_stripped_path(self):
        from django.test import RequestFactory
        from whitenoise.middleware import WhiteNoiseMiddleware

        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "probe.css").write_text("body{}")
            with override_settings(STATIC_ROOT=root):
                middleware = WhiteNoiseMiddleware(lambda request: None)
                # The browser asks for /parts-db/static/...; the proxy hands over /static/...
                response = middleware(RequestFactory().get("/static/probe.css"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(settings.STATIC_URL, f"{PREFIX}/static/")

    def test_prefixed_path_also_works_for_direct_access(self):
        self.assertEqual(self.client.get("/parts-db/parts/").status_code, 200)
        response = self.client.get("/parts-db/")
        self.assertEqual(response["Location"], f"{PREFIX}/parts/")

    def test_named_login_urls_carry_prefix(self):
        from django.shortcuts import resolve_url

        self.assertEqual(resolve_url(settings.LOGIN_URL), f"{PREFIX}/admin/login/")
        self.assertEqual(resolve_url(settings.LOGIN_REDIRECT_URL), f"{PREFIX}/parts/")


class UnprefixedRoutingTests(TestCase):
    def test_default_urls_have_no_prefix(self):
        response = self.client.get("/")
        self.assertEqual(response["Location"], "/parts/")
        self.assertIn('href="/admin/"', self.client.get("/parts/").content.decode())
