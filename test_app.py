import hashlib
import hmac
import json
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


class Tc3RequestTests(unittest.TestCase):
    def test_api_error_is_typed(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"Response": {"Error": {"Code": "Bad", "Message": "no"}}}
        with patch.object(app.requests, "post", return_value=response):
            with self.assertRaises(app.TencentAPIError) as context:
                app.tc3_request("Test", {})
        self.assertEqual(context.exception.code, "Bad")


if __name__ == "__main__":
    unittest.main()
