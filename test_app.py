import base64
import hashlib
import hmac
import itertools
import os
import unittest
from unittest.mock import Mock, call, patch

import app


class ParseIpsTests(unittest.TestCase):
    def test_parse_headered_table_and_skip_ipv6(self):
        html = """
        <html><table>
          <tr><th>排名</th><th>线路</th><th>IP 地址</th></tr>
          <tr><td>1</td><td>电信</td><td>1.2.3.4</td></tr>
          <tr><td>2</td><td>电信</td><td>1.2.3.4</td></tr>
          <tr><td>3</td><td>联通</td><td>2001:db8::1</td></tr>
          <tr><td>4</td><td>联通</td><td>5.6.7.8</td></tr>
        </table></html>
        """
        self.assertEqual(
            app.parse_ips(html),
            {"电信": ["1.2.3.4"], "联通": ["5.6.7.8"]},
        )

    def test_parse_without_header_keeps_legacy_column_positions(self):
        html = "<table><tr><td>1</td><td>移动</td><td>9.8.7.6</td></tr></table>"
        self.assertEqual(app.parse_ips(html), {"移动": ["9.8.7.6"]})

    def test_skips_non_public_ipv4(self):
        html = """
        <table>
          <tr><td>1</td><td>电信</td><td>10.0.0.1</td></tr>
          <tr><td>2</td><td>电信</td><td>127.0.0.1</td></tr>
          <tr><td>3</td><td>电信</td><td>192.168.1.1</td></tr>
          <tr><td>4</td><td>电信</td><td>169.254.1.1</td></tr>
          <tr><td>5</td><td>电信</td><td>0.0.0.0</td></tr>
          <tr><td>6</td><td>电信</td><td>8.8.8.8</td></tr>
        </table>
        """
        self.assertEqual(app.parse_ips(html), {"电信": ["8.8.8.8"]})


class PublicIpv4Tests(unittest.TestCase):
    def test_accepts_public_ipv4(self):
        address = app._public_ipv4("8.8.8.8")
        self.assertIsNotNone(address)
        self.assertEqual(str(address), "8.8.8.8")

    def test_rejects_private_loopback_link_local_and_unspecified(self):
        for value in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1", "0.0.0.0"):
            self.assertIsNone(app._public_ipv4(value), value)

    def test_rejects_ipv6_and_garbage(self):
        self.assertIsNone(app._public_ipv4("2001:db8::1"))
        self.assertIsNone(app._public_ipv4("not-an-ip"))
        self.assertIsNone(app._public_ipv4(""))


class FetchViaFlareSolverrTests(unittest.TestCase):
    def test_success_returns_html(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html><body><table></table></body></html>"},
        }
        with patch.object(app.requests, "post", return_value=response) as post:
            html = app.fetch_via_flaresolverr("http://example.com", retries=1)
        self.assertIn("<html>", html)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["delay"], app.FLARE_DELAY)

    def test_retries_then_raises(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "error", "message": "nope"}
        with patch.object(app.requests, "post", return_value=response), patch.object(app.time, "sleep"):
            with self.assertRaises(RuntimeError):
                app.fetch_via_flaresolverr("http://example.com", retries=2)

    def test_decode_base64_html(self):
        encoded = base64.b64encode(b"<html><table></table></html>").decode("ascii")
        self.assertIn("<html>", app._decode_flaresolverr_response(encoded))

    def test_decode_plain_text_returns_as_is(self):
        self.assertEqual(app._decode_flaresolverr_response("plain text"), "plain text")


class SignTc3Tests(unittest.TestCase):
    def test_signature_uses_real_newlines_in_canonical_headers(self):
        with patch.object(app, "TENCENT_SECRET_ID", "sid"), patch.object(
            app, "TENCENT_SECRET_KEY", "skey"
        ), patch.object(app.time, "time", return_value=1700000000):
            headers, payload = app.sign_tc3("DescribeRecordList", {"b": 2, "a": 1})

        self.assertEqual(payload, '{"a":1,"b":2}')
        timestamp = 1700000000
        date = "2023-11-14"
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{app.DNSPOD_HOST}\n"
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                canonical_headers,
                "content-type;host",
                hashlib.sha256(payload.encode()).hexdigest(),
            ]
        )
        scope = f"{date}/{app.DNSPOD_SERVICE}/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        secret_date = hmac.new(b"TC3skey", date.encode(), hashlib.sha256).digest()
        secret_service = hmac.new(secret_date, b"dnspod", hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
        expected_signature = hmac.new(
            secret_signing, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        self.assertIn(f"Signature={expected_signature}", headers["Authorization"])


class SyncLineTests(unittest.TestCase):
    def test_creates_only_the_first_three_ips(self):
        with patch.object(app, "describe_record_list", return_value=[{"Name": "other"}]), patch.object(
            app, "create_record"
        ) as create:
            result = app.sync_line("www", "电信", ["1.2.3.4", "1.2.3.5", "1.2.3.6", "1.2.3.7"])
        self.assertEqual(create.call_count, 3)
        create.assert_has_calls(
            [
                call("www", "电信", "1.2.3.4"),
                call("www", "电信", "1.2.3.5"),
                call("www", "电信", "1.2.3.6"),
            ]
        )
        self.assertIn("已创建第3个位置", result)

    def test_updates_positions_and_deletes_extras(self):
        records = [
            {"Name": "www", "Line": "电信", "RecordId": 13, "Value": "3.3.3.0"},
            {"Name": "www", "Line": "电信", "RecordId": 11, "Value": "1.1.1.0"},
            {"Name": "www", "Line": "电信", "RecordId": 12, "Value": "2.2.2.2"},
            {"Name": "www", "Line": "电信", "RecordId": 14, "Value": "9.9.9.9"},
            {"Name": "other", "Line": "电信", "RecordId": 99, "Value": "8.8.8.8"},
        ]
        with patch.object(app, "describe_record_list", return_value=records), patch.object(
            app, "modify_record"
        ) as modify, patch.object(app, "delete_record") as delete:
            result = app.sync_line("www", "电信", ["1.1.1.1", "2.2.2.2", "3.3.3.3"])
        modify.assert_has_calls(
            [
                call(11, "www", "电信", "1.1.1.1"),
                call(13, "www", "电信", "3.3.3.3"),
            ]
        )
        delete.assert_called_once_with(14)
        self.assertIn("已删除多余位置", result)

    def test_dry_run_does_not_modify(self):
        with patch.object(app, "DRY_RUN", True), patch.object(
            app,
            "describe_record_list",
            return_value=[
                {"Name": "www", "Line": "电信", "RecordId": 1, "Value": "1.1.1.1"},
                {"Name": "www", "Line": "电信", "RecordId": 2, "Value": "2.2.2.2"},
                {"Name": "www", "Line": "电信", "RecordId": 3, "Value": "old"},
                {"Name": "www", "Line": "电信", "RecordId": 4, "Value": "extra"},
            ],
        ), patch.object(app, "modify_record") as modify, patch.object(
            app, "create_record"
        ) as create, patch.object(app, "delete_record") as delete:
            result = app.sync_line("www", "电信", ["1.1.1.1", "2.2.2.2", "3.3.3.3"])
        self.assertIn("DRY-RUN", result)
        modify.assert_not_called()
        create.assert_not_called()
        delete.assert_not_called()

    def test_rejects_non_public_ip(self):
        with patch.object(app, "describe_record_list", return_value=[]):
            with self.assertRaises(ValueError):
                app.sync_line("www", "电信", ["10.0.0.1"])


class DescribeRecordListTests(unittest.TestCase):
    def _page(self, start, count):
        return [
            {"RecordId": i, "Name": "@", "Line": "电信", "Value": "1.1.1.1"}
            for i in range(start, start + count)
        ]

    def test_paginates_until_short_page(self):
        pages = [{"RecordList": self._page(1, 100)}, {"RecordList": self._page(101, 5)}]
        with patch.object(app, "tc3_request", side_effect=pages):
            records = app.describe_record_list("@", "电信")
        self.assertEqual(len(records), 105)
        self.assertEqual(records[0]["RecordId"], 1)
        self.assertEqual(records[-1]["RecordId"], 105)

    def test_repeated_page_aborts(self):
        page = {"RecordList": self._page(1, 100)}
        with patch.object(app, "tc3_request", return_value=page):
            with self.assertRaises(RuntimeError):
                app.describe_record_list("@", "电信")

    def test_pages_cap_aborts(self):
        counter = itertools.count(1)

        def fake_request(*args, **kwargs):
            return {"RecordList": [{"RecordId": next(counter)} for _ in range(100)]}

        with patch.object(app, "tc3_request", side_effect=fake_request):
            with self.assertRaises(RuntimeError):
                app.describe_record_list("@", "电信")


class Tc3RequestTests(unittest.TestCase):
    def test_api_error_is_typed(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"Response": {"Error": {"Code": "Bad", "Message": "no"}}}
        with patch.object(app.requests, "post", return_value=response):
            with self.assertRaises(app.TencentAPIError) as context:
                app.tc3_request("Test", {})
        self.assertEqual(context.exception.code, "Bad")

    def test_error_not_dict_is_tolerated(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"Response": {"Error": "boom"}}
        with patch.object(app.requests, "post", return_value=response):
            with self.assertRaises(app.TencentAPIError) as context:
                app.tc3_request("Test", {})
        self.assertEqual(context.exception.code, "UnknownError")
        self.assertIn("boom", context.exception.message)


class ConfigValidationTests(unittest.TestCase):
    BASE = (
        ("TENCENT_SECRET_ID", "sid"),
        ("TENCENT_SECRET_KEY", "skey"),
        ("DOMAIN", "example.com"),
        ("SUBDOMAIN", "@"),
        ("LINES", ["电信", "联通", "移动"]),
        ("MAX_IPS_PER_LINE", 3),
        ("INTERVAL_MINUTES", 10),
        ("HTTP_TIMEOUT", 90),
        ("FLARE_MAX_TIMEOUT", 60000),
        ("FLARE_DELAY", 2),
        ("TTL", 600),
        ("FLARESOLVERR_URL", "http://flaresolverr:8191"),
        ("TARGET_URL", "https://api.uouin.com/cloudflare.html"),
        ("DNSPOD_ENDPOINT", "https://dnspod.tencentcloudapi.com"),
        ("DNSPOD_ALLOW_HTTP", False),
    )

    def setUp(self):
        self.patchers = []
        for name, value in dict(self.BASE).items():
            patcher = patch.object(app, name, value)
            patcher.start()
            self.patchers.append(patcher)
        app._CONFIG_PARSE_ERRORS.clear()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        app._CONFIG_PARSE_ERRORS.clear()

    def test_valid_config_passes(self):
        app.validate_config()

    def test_invalid_line_rejected(self):
        with patch.object(app, "LINES", ["电信\n联通"]):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_http_dnspod_endpoint_rejected_by_default(self):
        with patch.object(app, "DNSPOD_ENDPOINT", "http://127.0.0.1:8192"):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_http_dnspod_endpoint_allowed_when_explicit(self):
        with patch.object(app, "DNSPOD_ENDPOINT", "http://127.0.0.1:8192"), patch.object(
            app, "DNSPOD_ALLOW_HTTP", True
        ):
            app.validate_config()

    def test_endpoint_with_path_rejected(self):
        with patch.object(app, "DNSPOD_ENDPOINT", "https://example.com/v2"):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_max_ips_cap_rejected(self):
        with patch.object(app, "MAX_IPS_PER_LINE", 21):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_invalid_domain_rejected(self):
        with patch.object(app, "DOMAIN", "-bad.example.com"):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_invalid_subdomain_rejected(self):
        with patch.object(app, "SUBDOMAIN", "a..b"):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_flare_timeout_cross_check(self):
        with patch.object(app, "FLARE_MAX_TIMEOUT", 120000):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_flare_delay_negative_rejected(self):
        with patch.object(app, "FLARE_DELAY", -1):
            with self.assertRaises(app.ConfigurationError):
                app.validate_config()

    def test_invalid_log_level_breaks_validation(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "BOGUS"}):
            app._resolve_log_level()
        with self.assertRaises(app.ConfigurationError):
            app.validate_config()

    def test_resolve_log_level_falls_back_to_default(self):
        app._CONFIG_PARSE_ERRORS.clear()
        with patch.dict(os.environ, {"LOG_LEVEL": "BOGUS"}):
            self.assertEqual(app._resolve_log_level(), "INFO")
        self.assertTrue(any("LOG_LEVEL" in error for error in app._CONFIG_PARSE_ERRORS))
        app._CONFIG_PARSE_ERRORS.clear()


if __name__ == "__main__":
    unittest.main()
