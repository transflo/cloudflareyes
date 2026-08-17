#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare 优选 IPv4 -> 腾讯云 DNSPod 线路解析自动更新器。

程序流程：
1. 通过 FlareSolverr 获取目标页面；
2. 从页面表格中提取各线路的 IPv4，并保留页面排序；
3. 为每条线路选择第一个 IP，同步到 DNSPod 的 A 记录。

所有运行参数通过环境变量注入，适合本地运行或 Docker 部署。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cf-dns-updater")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_CONFIG_PARSE_ERRORS: list[str] = []


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        _CONFIG_PARSE_ERRORS.append(f"{name} 必须是整数，实际为 {raw!r}")
        return default


FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191").strip()
TARGET_URL = os.getenv("TARGET_URL", "https://api.uouin.com/cloudflare.html").strip()
INTERVAL_MINUTES = _env_int("INTERVAL_MINUTES", 10)
RUN_ONCE = os.getenv("RUN_ONCE", "false").strip().lower() in {"1", "true", "yes", "on"}
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"}
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 90)
FLARE_MAX_TIMEOUT = _env_int("FLARE_MAX_TIMEOUT", 60000)

TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID", "").strip()
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "").strip()
DOMAIN = os.getenv("DOMAIN", "").strip()
SUBDOMAIN = os.getenv("SUBDOMAIN", "@").strip() or "@"
LINES = list(dict.fromkeys(x.strip() for x in os.getenv("LINES", "电信,联通,移动").split(",") if x.strip()))
TTL = _env_int("TTL", 600)

DNSPOD_HOST = "dnspod.tencentcloudapi.com"
DNSPOD_SERVICE = "dnspod"
DNSPOD_VERSION = "2021-03-23"
DNSPOD_ENDPOINT = os.getenv("DNSPOD_ENDPOINT", f"https://{DNSPOD_HOST}").strip()
MUST_ADD_DEFAULT_LINE = "MustAddDefaultLineFirst"

# 线路名称通常是中文，也允许英文、数字、下划线和连字符；拒绝把表格中的普通文本当成线路。
LINE_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 _-]{0,63}$")


class ConfigurationError(ValueError):
    """运行配置不合法。"""


class TencentAPIError(RuntimeError):
    """腾讯云 API 返回业务错误。"""

    def __init__(self, action: str, code: str | None, message: str | None):
        self.action = action
        self.code = code or "UnknownError"
        self.message = message or "未知错误"
        super().__init__(f"API {action} 失败: [{self.code}] {self.message}")


def _validate_url(value: str, name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} 必须是完整的 http(s) URL: {value!r}")


def validate_config() -> None:
    """在启动前校验配置，避免循环运行到一半才因参数错误失败。"""
    if _CONFIG_PARSE_ERRORS:
        raise ConfigurationError("；".join(_CONFIG_PARSE_ERRORS))
    missing = [
        name
        for name, value in (
            ("TENCENT_SECRET_ID", TENCENT_SECRET_ID),
            ("TENCENT_SECRET_KEY", TENCENT_SECRET_KEY),
            ("DOMAIN", DOMAIN),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(f"缺少必要环境变量: {', '.join(missing)}")
    if not LINES:
        raise ConfigurationError("LINES 至少需要包含一条线路")
    if INTERVAL_MINUTES <= 0:
        raise ConfigurationError("INTERVAL_MINUTES 必须大于 0")
    if HTTP_TIMEOUT <= 0:
        raise ConfigurationError("HTTP_TIMEOUT 必须大于 0")
    if FLARE_MAX_TIMEOUT <= 0:
        raise ConfigurationError("FLARE_MAX_TIMEOUT 必须大于 0")
    if not 1 <= TTL <= 604800:
        raise ConfigurationError("TTL 必须在 1 到 604800 秒之间")
    if not DOMAIN or any(ch.isspace() for ch in DOMAIN) or DOMAIN.endswith("."):
        raise ConfigurationError("DOMAIN 必须是非空域名，且不能以句点结尾")
    if not SUBDOMAIN or any(ch.isspace() for ch in SUBDOMAIN):
        raise ConfigurationError("SUBDOMAIN 不能为空或包含空白字符")
    _validate_url(FLARESOLVERR_URL, "FLARESOLVERR_URL")
    _validate_url(TARGET_URL, "TARGET_URL")
    _validate_url(DNSPOD_ENDPOINT, "DNSPOD_ENDPOINT")


# ---------------------------------------------------------------------------
# HTTP/FlareSolverr 抓取
# ---------------------------------------------------------------------------
def _decode_flaresolverr_response(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    if "<" in value[:2000]:
        return value
    # FlareSolverr 通常直接返回 HTML；这里只在明显不是 HTML 时尝试 base64，
    # 避免对普通错误文本进行无意义解码。
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return value
    return decoded if "<" in decoded[:2000] else value


def fetch_via_flaresolverr(url: str, retries: int = 3) -> str:
    """通过 FlareSolverr 的 request.get 抓取页面，返回 HTML 文本。"""
    if retries < 1:
        raise ValueError("retries 必须大于 0")
    endpoint = FLARESOLVERR_URL.rstrip("/") + "/v1"
    payload = {"cmd": "request.get", "url": url, "maxTimeout": FLARE_MAX_TIMEOUT}
    last_err = "未知错误"

    for attempt in range(1, retries + 1):
        try:
            log.info("通过 FlareSolverr 抓取 %s（第 %d/%d 次）", url, attempt, retries)
            response = requests.post(
                endpoint,
                json=payload,
                timeout=HTTP_TIMEOUT,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f"FlareSolverr 返回了无效 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise RuntimeError("FlareSolverr 返回格式异常")

            if data.get("status") != "ok":
                last_err = f"FlareSolverr 返回错误: {data.get('message', data)}"
            else:
                solution = data.get("solution") or {}
                if not isinstance(solution, dict):
                    raise RuntimeError("FlareSolverr 的 solution 格式异常")
                html = _decode_flaresolverr_response(solution.get("response", ""))
                if _looks_like_html(html):
                    log.info("抓取成功，页面大小 %d 字节", len(html.encode("utf-8")))
                    return html
                last_err = "页面内容里没有找到 HTML，可能未通过人机校验"
            log.warning(last_err)
        except (requests.RequestException, RuntimeError) as exc:
            last_err = f"请求 FlareSolverr 失败: {exc}"
            log.warning(last_err)
        if attempt < retries:
            time.sleep(min(5 * attempt, 30))

    raise RuntimeError(f"抓取失败: {last_err}")


def _looks_like_html(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return "<html" in lowered or "<table" in lowered or "<tbody" in lowered


# ---------------------------------------------------------------------------
# 页面表格解析
# ---------------------------------------------------------------------------
def _column_indexes(header_cells: list[str]) -> tuple[int, int]:
    normalized = [re.sub(r"\s+", "", cell).lower() for cell in header_cells]
    line_index = next(
        (i for i, cell in enumerate(normalized) if cell in {"线路", "线路名称", "运营商", "isp"}),
        1,
    )
    ip_index = next(
        (i for i, cell in enumerate(normalized) if cell in {"ip", "ip地址", "ipv4"}),
        2,
    )
    return line_index, ip_index


def parse_ips(html: str) -> dict[str, list[str]]:
    """解析页面，返回 ``{线路: [IP, ...]}``，顺序即页面排序。"""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, list[str]] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_cells = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["td", "th"])]
        line_index, ip_index = _column_indexes(first_cells)
        for row in rows:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
            if len(cells) <= max(line_index, ip_index):
                continue
            line = re.sub(r"\s+", " ", cells[line_index]).strip()
            ip_text = cells[ip_index].strip()
            if not LINE_NAME_RE.fullmatch(line):
                continue
            try:
                address = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if address.version != 4:
                continue
            ip = str(address)
            if ip not in result.setdefault(line, []):
                result[line].append(ip)

    if not result:
        raise ValueError("未能从页面中解析出任何线路/IP")
    return result


# ---------------------------------------------------------------------------
# 腾讯云 API v3（TC3-HMAC-SHA256）签名与调用
# ---------------------------------------------------------------------------
def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign_tc3(action: str, params: Mapping[str, Any]) -> tuple[dict[str, str], str]:
    """计算 TC3-HMAC-SHA256 签名，返回 ``(headers, body)``。"""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    # body 同时用于签名和发送，确保签名内容与实际请求完全一致。
    payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    canonical_headers = f"content-type:{content_type}\nhost:{DNSPOD_HOST}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            _sha256_hex(payload),
        ]
    )
    credential_scope = f"{date}/{DNSPOD_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        ["TC3-HMAC-SHA256", str(timestamp), credential_scope, _sha256_hex(canonical_request)]
    )

    secret_date = _hmac_sha256(("TC3" + TENCENT_SECRET_KEY).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, DNSPOD_SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={TENCENT_SECRET_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": DNSPOD_HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": DNSPOD_VERSION,
    }
    return headers, payload


def tc3_request(action: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """调用腾讯云 DNSPod API，返回响应中的 ``Response`` 对象。"""
    headers, payload = sign_tc3(action, params)
    try:
        response = requests.post(
            DNSPOD_ENDPOINT,
            data=payload.encode("utf-8"),
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"API {action} 网络请求失败: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"API {action} 返回了无效 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"API {action} 返回格式异常")
    response_data = data.get("Response", data)
    if not isinstance(response_data, dict):
        raise RuntimeError(f"API {action} 的 Response 格式异常")
    if "Error" in response_data:
        error = response_data.get("Error") or {}
        raise TencentAPIError(action, error.get("Code"), error.get("Message"))
    return response_data


# ---------------------------------------------------------------------------
# DNSPod 业务逻辑
# ---------------------------------------------------------------------------
def describe_record_list(subdomain: str, line: str) -> list[dict[str, Any]]:
    """查询某线路下的全部 A 记录，自动处理分页。"""
    records: list[dict[str, Any]] = []
    offset = 0
    limit = 100
    while True:
        response = tc3_request(
            "DescribeRecordList",
            {
                "Domain": DOMAIN,
                "SubDomain": subdomain,
                "RecordType": "A",
                "RecordLine": line,
                "Offset": offset,
                "Limit": limit,
                "ErrorOnEmpty": "no",
            },
        )
        page = response.get("RecordList") or []
        if not isinstance(page, list):
            raise RuntimeError("API DescribeRecordList 返回的 RecordList 格式异常")
        records.extend(record for record in page if isinstance(record, dict))
        if len(page) < limit:
            return records
        offset += limit


def modify_record(record_id: str | int, subdomain: str, line: str, value: str) -> None:
    tc3_request(
        "ModifyRecord",
        {
            "Domain": DOMAIN,
            "SubDomain": subdomain,
            "RecordType": "A",
            "RecordLine": line,
            "Value": value,
            "RecordId": record_id,
            "TTL": TTL,
        },
    )


def create_record(subdomain: str, line: str, value: str) -> None:
    tc3_request(
        "CreateRecord",
        {
            "Domain": DOMAIN,
            "SubDomain": subdomain,
            "RecordType": "A",
            "RecordLine": line,
            "Value": value,
            "TTL": TTL,
        },
    )


def sync_line(subdomain: str, line: str, ip: str) -> str:
    """把某条线路的 A 记录更新到指定 IP，返回状态描述。"""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"无效 IP 地址: {ip!r}") from exc
    if address.version != 4:
        raise ValueError(f"仅支持 IPv4 A 记录: {ip!r}")

    records = describe_record_list(subdomain, line)
    matches = [
        record
        for record in records
        if str(record.get("Name", "")).lower() == subdomain.lower()
        and str(record.get("Line", "")) == line
    ]
    if len(matches) > 1:
        raise RuntimeError(f"线路「{line}」找到多个同名 A 记录，拒绝自动选择以避免误改")
    record = matches[0] if matches else None

    if record is None:
        if DRY_RUN:
            return f"DRY-RUN: 将创建 {subdomain}/{line} -> {ip}"
        try:
            create_record(subdomain, line, ip)
        except TencentAPIError as exc:
            if exc.code == MUST_ADD_DEFAULT_LINE or MUST_ADD_DEFAULT_LINE in str(exc):
                raise RuntimeError(
                    f"{exc}（提示：DNSPod 要求先存在「默认」线路的解析记录，才能添加分线路记录）"
                ) from exc
            raise
        return f"已创建 {subdomain}/{line} -> {ip}"

    record_id = record.get("RecordId")
    if record_id is None:
        raise RuntimeError(f"线路「{line}」的记录缺少 RecordId")
    current = str(record.get("Value", "")).strip()
    if current == ip:
        return f"{subdomain}/{line} 已是 {ip}，无需更新"
    if DRY_RUN:
        return f"DRY-RUN: 将修改 {subdomain}/{line} {current or '<空值'} -> {ip}"
    modify_record(record_id, subdomain, line, ip)
    return f"已更新 {subdomain}/{line} {current or '<空值'} -> {ip}"


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def run_once() -> None:
    log.info("=" * 60)
    log.info("开始同步：域名=%s 子域=%s 线路=%s", DOMAIN, SUBDOMAIN, ",".join(LINES))
    if DRY_RUN:
        log.info("DRY_RUN=true，仅打印将要执行的变更，不会修改解析记录")

    html = fetch_via_flaresolverr(TARGET_URL)
    ips_by_line = parse_ips(html)
    log.info("页面解析结果：%s", ", ".join(f"{line}({len(ips)}个)" for line, ips in ips_by_line.items()))
    log.info(
        "各线路最优 IP：%s",
        ", ".join(f"{line}={ips_by_line[line][0]}" for line in LINES if line in ips_by_line),
    )

    errors: list[str] = []
    for line in LINES:
        ips = ips_by_line.get(line)
        if not ips:
            message = f"页面中没有找到线路「{line}」的 IP"
            errors.append(message)
            log.warning("%s，跳过", message)
            continue
        try:
            log.info(sync_line(SUBDOMAIN, line, ips[0]))
        except Exception as exc:  # 单条线路失败不影响其它线路，但最终要让本轮可观测为失败。
            errors.append(f"{line}: {exc}")
            log.error("线路「%s」更新失败：%s", line, exc)
    if errors:
        raise RuntimeError("部分线路更新失败：" + "; ".join(errors))


def main() -> int:
    try:
        validate_config()
    except ConfigurationError as exc:
        log.error("配置错误：%s", exc)
        return 1

    while True:
        succeeded = True
        try:
            run_once()
        except Exception as exc:
            succeeded = False
            log.error("本次运行失败：%s", exc)

        if RUN_ONCE:
            log.info("RUN_ONCE=true，执行完毕退出")
            return 0 if succeeded else 1
        log.info("%d 分钟后进行下一次同步...", INTERVAL_MINUTES)
        try:
            time.sleep(INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            log.info("收到退出信号，程序结束")
            return 0


if __name__ == "__main__":
    sys.exit(main())
