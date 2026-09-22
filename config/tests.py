from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from tenants.models import Tenant, TenantAllowedOrigin

from config import settings as project_settings
from config.database import build_database_config, is_running_tests, parse_database_conn_max_age


class DeploymentSettingsHelperTests(SimpleTestCase):
    def test_csv_env_parses_comma_separated_values(self):
        with patch.dict("os.environ", {"DJANGO_ALLOWED_HOSTS": "livia.example, localhost,127.0.0.1"}):
            self.assertEqual(
                project_settings.csv_env("DJANGO_ALLOWED_HOSTS"),
                ["livia.example", "localhost", "127.0.0.1"],
            )

    def test_csv_env_uses_default_when_missing(self):
        self.assertEqual(
            project_settings.csv_env("LIVIA_MISSING_TEST_VALUE", default="localhost,127.0.0.1"),
            ["localhost", "127.0.0.1"],
        )

    @override_settings(DEBUG=True)
    def test_debug_true_settings_remain_valid(self):
        self.assertTrue(project_settings.DEBUG in {True, False})
        self.assertIn("django.contrib.staticfiles", project_settings.INSTALLED_APPS)


    def test_database_uses_sqlite_when_debug_true_without_database_url(self):
        databases = build_database_config(debug=True, base_dir=project_settings.BASE_DIR, database_url="")

        self.assertEqual(databases["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertIn("db.sqlite3", str(databases["default"]["NAME"]))

    def test_database_uses_postgresql_when_debug_true_with_database_url(self):
        databases = build_database_config(
            debug=True,
            base_dir=project_settings.BASE_DIR,
            database_url="postgresql://livia@localhost:55432/livia_platform?sslmode=disable",
        )

        self.assertEqual(databases["default"]["ENGINE"], "django.db.backends.postgresql")

    def test_database_uses_postgresql_from_database_url(self):
        databases = build_database_config(
            debug=False,
            base_dir=project_settings.BASE_DIR,
            database_url="postgresql://livia@localhost:55432/livia_platform?sslmode=disable",
            conn_max_age=123,
        )

        default = databases["default"]
        self.assertEqual(default["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(default["NAME"], "livia_platform")
        self.assertEqual(default["CONN_MAX_AGE"], 123)
        self.assertTrue(default["CONN_HEALTH_CHECKS"])
        self.assertEqual(default["OPTIONS"]["sslmode"], "disable")
        self.assertEqual(default["TEST"]["NAME"], "test_livia_platform")

    def test_database_fails_closed_in_production_without_database_url(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "DATABASE_URL is required"):
            build_database_config(debug=False, base_dir=project_settings.BASE_DIR, database_url="")

    def test_database_invalid_url_error_does_not_reveal_secret(self):
        password = "sensitive-value-not-real"
        invalid_url = f"mysql://user:{password}@unsupported-host/db"

        with self.assertRaises(ImproperlyConfigured) as captured:
            build_database_config(debug=False, base_dir=project_settings.BASE_DIR, database_url=invalid_url)

        message = str(captured.exception)
        self.assertNotIn(password, message)
        self.assertNotIn(invalid_url, message)

    def test_database_rejects_external_database_url_during_tests_by_default(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "Refusing to run tests"):
            build_database_config(
                debug=False,
                base_dir=project_settings.BASE_DIR,
                database_url="postgresql://livia@db.prod.example/livia_platform",
                running_tests=True,
            )

    def test_database_allows_local_database_url_during_tests(self):
        databases = build_database_config(
            debug=False,
            base_dir=project_settings.BASE_DIR,
            database_url="postgresql://livia@127.0.0.1:55432/livia_platform",
            running_tests=True,
        )

        self.assertEqual(databases["default"]["TEST"]["NAME"], "test_livia_platform")

    def test_database_allows_external_database_url_when_explicitly_enabled(self):
        databases = build_database_config(
            debug=False,
            base_dir=project_settings.BASE_DIR,
            database_url="postgresql://livia@db.prod.example/livia_platform",
            running_tests=True,
            allow_external_test_database_url=True,
        )

        self.assertEqual(databases["default"]["TEST"]["NAME"], "test_livia_platform")

    def test_database_conn_max_age_invalid_fails_safely(self):
        with self.assertRaises(ImproperlyConfigured) as captured:
            parse_database_conn_max_age("not-a-number")

        self.assertEqual(str(captured.exception), "DATABASE_CONN_MAX_AGE must be a non-negative integer.")

    def test_database_conn_max_age_negative_fails_safely(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "non-negative integer"):
            parse_database_conn_max_age("-1")

    def test_running_tests_detection_uses_command_or_explicit_env(self):
        self.assertTrue(is_running_tests(["manage.py", "test"], ""))
        self.assertTrue(is_running_tests(["manage.py", "check"], "true"))
        self.assertFalse(is_running_tests(["manage.py", "contest"], ""))

    def test_ssl_redirect_default_is_disabled_while_running_test_command(self):
        self.assertTrue(project_settings.RUNNING_TESTS)
        self.assertFalse(project_settings.SECURE_SSL_REDIRECT)



class HealthcheckTests(SimpleTestCase):
    def test_healthcheck_returns_ok(self):
        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "livia-platform"})


class LiviaWidgetCorsMiddlewareTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://www.smartcontrolbrasil.com.br")

    @override_settings(DEBUG=False, LIVIA_ALLOW_ORIGINLESS_PUBLIC_API=False)
    def test_allowed_origin_receives_cors_headers(self):
        response = self.client.options(
            "/api/chat/",
            HTTP_ORIGIN="https://www.smartcontrolbrasil.com.br",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_X_LIVIA_TENANT="smart-control-brasil",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.smartcontrolbrasil.com.br")

    @override_settings(DEBUG=False, LIVIA_ALLOW_ORIGINLESS_PUBLIC_API=False)
    def test_blocked_origin_does_not_receive_cors_headers(self):
        response = self.client.options(
            "/api/chat/",
            HTTP_ORIGIN="https://evil.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_X_LIVIA_TENANT="smart-control-brasil",
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response)

    @override_settings(DEBUG=False, LIVIA_ALLOW_ORIGINLESS_PUBLIC_API=False)
    def test_request_without_origin_does_not_break(self):
        response = self.client.options("/api/chat/")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response)

    @override_settings(DEBUG=False, LIVIA_ALLOWED_WIDGET_ORIGINS=[])
    def test_empty_allowed_origins_is_not_permissive(self):
        response = self.client.options(
            "/api/chat/",
            HTTP_ORIGIN="https://www.canecadegaragem.com.br",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response)

    def test_executor_api_allows_chrome_extension_preflight(self):
        response = self.client.options(
            "/api/v1/executors/pairing/request/",
            HTTP_ORIGIN="chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "chrome-extension://abcdefghijklmnopabcdefghijklmnop")
        self.assertIn("POST", response["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", response["Access-Control-Allow-Headers"])

    def test_executor_api_rejects_non_extension_preflight(self):
        response = self.client.options(
            "/api/v1/executors/pairing/request/",
            HTTP_ORIGIN="https://evil.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response)
