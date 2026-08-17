#!/usr/bin/env python3
"""仅用于本地 Docker 集成测试的 FlareSolverr/DNSPod mock 服务。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HTML = """
<!doctype html>
<html><body><table>
<tr><th>排名</th><th>线路</th><th>IP 地址</th></tr>
<tr><td>1</td><td>电信</td><td>1.2.3.4</td></tr>
<tr><td>2</td><td>联通</td><td>5.6.7.8</td></tr>
<tr><td>3</td><td>移动</td><td>9.8.7.6</td></tr>
</table></body></html>
"""

# 电信已有旧 IP，联通没有记录，移动已经是目标 IP；三种同步路径都会被覆盖。
RECORDS: dict[str, dict] = {
    "电信": {"RecordId": 101, "Name": "@", "Line": "电信", "Value": "1.1.1.1"},
    "移动": {"RecordId": 103, "Name": "@", "Line": "移动", "Value": "9.8.7.6"},
}
NEXT_RECORD_ID = 200
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "cloudflareyes-test-mock/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[mock] {self.address_string()} - {fmt % args}")

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        if urlparse(self.path).path == "/v1":
            self._json({"status": "ok", "solution": {"response": HTML}})
            return

        action = self.headers.get("X-TC-Action", "")
        print(f"[mock] action={action} request={request}")
        with LOCK:
            if action == "DescribeRecordList":
                line = request.get("RecordLine")
                records = [record.copy() for record in RECORDS.values() if record["Line"] == line]
                self._json({"Response": {"RecordList": records}})
            elif action == "ModifyRecord":
                record_id = request.get("RecordId")
                for record in RECORDS.values():
                    if record["RecordId"] == record_id:
                        record["Value"] = request.get("Value", "")
                        break
                self._json({"Response": {}})
            elif action == "CreateRecord":
                global NEXT_RECORD_ID
                NEXT_RECORD_ID += 1
                record = {
                    "RecordId": NEXT_RECORD_ID,
                    "Name": request.get("SubDomain", "@"),
                    "Line": request.get("RecordLine", ""),
                    "Value": request.get("Value", ""),
                }
                RECORDS[record["Line"]] = record
                self._json({"Response": {"RecordId": NEXT_RECORD_ID}})
            else:
                self._json({"Response": {"Error": {"Code": "UnsupportedAction", "Message": action}}}, 400)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8192)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock services listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
