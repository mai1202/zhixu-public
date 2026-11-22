import tempfile
import unittest
from pathlib import Path

from notification_app import create_app


class AppFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        # 使用系统临时目录，测试不向仓库 data 目录写入任何文件。
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="通知管理应用_",
            ignore_cleanup_errors=True,
        )
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.temp_dir.name,
                "DATABASE_URL": "sqlite:///:memory:",
                "FRONTEND_DIST": str(Path(self.temp_dir.name) / "frontend"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.app.extensions["notification_db_engine"].dispose()
        self.temp_dir.cleanup()

    def test_health_reports_service_and_database(self) -> None:
        response = self.client.get("/api/v1/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")
        self.assertEqual(response.json["service"], "notification-management-api")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.headers["X-Request-ID"])

    def test_unknown_api_route_uses_standard_error_shape(self) -> None:
        response = self.client.get("/api/v1/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"]["code"], "not_found")
        self.assertIn("request_id", response.json)


if __name__ == "__main__":
    unittest.main()
