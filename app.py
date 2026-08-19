#!/usr/bin/env python3
"""Cloudflare 优选 IPv4 -> 腾讯云 DNSPod 线路解析自动更新器。

程序流程：
1. 通过 FlareSolverr 获取目标页面；
2. 从页面表格中提取各线路的 IPv4，并保留页面排序；
3. 为每条线路选择排名前 MAX_IPS_PER_LINE（默认 3）的 IPv4，同步到 DNSPod 的 A 记录。

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
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_CONFIG_PARSE_ERRORS: list[str] = []


def _resolve_log_level(default: str = "INFO") -> str:
    """解析 LOG_LEVEL；非法值记录到配置错误并回退默认级别。"""
    raw = os.getenv("LOG_LEVEL", default).strip().upper()
    if isinstance(logging.getLevelName(raw), int):
        return raw
    _CONFIG_PARSE_ERRORS.append(f"LOG_LEVEL 必须是 DEBUG/INFO/WARNING/ERROR 之一，实际为 {raw!r}")
    return default


logging.basicConfig(
    level=_resolve_log_level(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cf-dns-updater")


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
FLARE_DELAY = _env_int("FLARE_DELAY", 2)

TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID", "").strip()
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "").strip()
DOMAIN = os.getenv("DOMAIN", "").strip()
SUBDOMAIN = os.getenv("SUBDOMAIN", "@").strip() or "@"
LINES = list(dict.fromkeys(x.strip() for x in os.getenv("LINES", "电信,联通,移动").split(",") if x.strip()))
MAX_IPS_PER_LINE = _env_int("MAX_IPS_PER_LINE", 3)
TTL = _env_int("TTL", 600)

DNSPOD_HOST = "dnspod.tencentcloudapi.com"
DNSPOD_SERVICE = "dnspod"
DNSPOD_VERSION = "2021-03-23"
DNSPOD_ENDPOINT = os.getenv("DNSPOD_ENDPOINT", f"https://{DNSPOD_HOST}").strip()
DNSPOD_ALLOW_HTTP = os.getenv("DNSPOD_ALLOW_HTTP", "false").strip().lower() in {"1", "true", "yes", "on"}
MUST_ADD_DEFAULT_LINE = "MustAddDefaultLineFirst"

# 线路名称通常是中文，也允许英文、数字、下划线和连字符；拒绝把表格中的普通文本当成线路。
LINE_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 _-]{0,63}$")

# DNS 标签/主机记录校验（IDN 请使用 punycode）。
_DNS_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
DOMAIN_LABEL_RE = re.compile(rf"^{_DNS_LABEL}$")
SUBDOMAIN_RE = re.compile(rf"^(?:@|\*|{_DNS_LABEL}(?:\.{_DNS_LABEL})*)$")


def _is_valid_domain(domain: str) -> bool:
    if len(domain) > 253:
        return False
    labels = domain.split(".")
    return bool(labels) and all(DOMAIN_LABEL_RE.fullmatch(label) for label in labels)


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
    bad_lines = [line for line in LINES if not LINE_NAME_RE.fullmatch(line)]
    if bad_lines:
        raise ConfigurationError(f"LINES 包含非法线路名: {', '.join(repr(line) for line in bad_lines)}")
    if MAX_IPS_PER_LINE <= 0:
        raise ConfigurationError("MAX_IPS_PER_LINE 必须大于 0")
    if MAX_IPS_PER_LINE > 20:
        raise ConfigurationError("MAX_IPS_PER_LINE 不能超过 20")
    if INTERVAL_MINUTES <= 0:
        raise ConfigurationError("INTERVAL_MINUTES 必须大于 0")
    if HTTP_TIMEOUT <= 0:
        raise ConfigurationError("HTTP_TIMEOUT 必须大于 0")
    if FLARE_MAX_TIMEOUT <= 0:
        raise ConfigurationError("FLARE_MAX_TIMEOUT 必须大于 0")
    if FLARE_DELAY < 0:
        raise ConfigurationError("FLARE_DELAY 不能小于 0")
    if FLARE_MAX_TIMEOUT > HTTP_TIMEOUT * 1000:
        raise ConfigurationError("FLARE_MAX_TIMEOUT(毫秒) 不能超过 HTTP_TIMEOUT(秒) * 1000")
    if not 1 <= TTL <= 604800:
        raise ConfigurationError("TTL 必须在 1 到 604800 秒之间")
    if not DOMAIN or any(ch.isspace() for ch in DOMAIN) or DOMAIN.endswith("."):
        raise ConfigurationError("DOMAIN 必须是非空域名，且不能以句点结尾")
    if not _is_valid_domain(DOMAIN):
        raise ConfigurationError(f"DOMAIN 不是合法域名: {DOMAIN!r}")
    if not SUBDOMAIN or any(ch.isspace() for ch in SUBDOMAIN):
        raise ConfigurationError("SUBDOMAIN 不能为空或包含空白字符")
    if not SUBDOMAIN_RE.fullmatch(SUBDOMAIN):
        raise ConfigurationError(f"SUBDOMAIN 不是合法的 DNS 主机记录: {SUBDOMAIN!r}")
    _validate_url(FLARESOLVERR_URL, "FLARESOLVERR_URL")
    _validate_url(TARGET_URL, "TARGET_URL")
    _validate_url(DNSPOD_ENDPOINT, "DNSPOD_ENDPOINT")

    endpoint = urlparse(DNSPOD_ENDPOINT)
    if endpoint.scheme != "https" and not (DNSPOD_ALLOW_HTTP and endpoint.scheme == "http"):
        raise ConfigurationError("DNSPOD_ENDPOINT 必须是 https:// 地址；仅本地测试可设 DNSPOD_ALLOW_HTTP=true")
    if endpoint.path not in ("", "/"):
        raise ConfigurationError("DNSPOD_ENDPOINT 不能包含路径（TC3 签名固定使用 /）")


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
    # delay：页面加载完成后额外等待的秒数。
    # 目标页面刚打开时显示旧数据占位，稍后才是最新数据，默认等 2 秒再抓取。
    payload = {"cmd": "request.get", "url": url, "maxTimeout": FLARE_MAX_TIMEOUT, "delay": FLARE_DELAY}
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
                raise TypeError("FlareSolverr 返回格式异常")

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


def _public_ipv4(value: str) -> ipaddress.IPv4Address | None:
    """把文本解析为公网 IPv4；内网/回环/保留/非 IPv4 返回 None。"""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if address.version != 4 or not address.is_global:
        return None
    return address


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
            address = _public_ipv4(ip_text)
            if address is None:
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
        raise TypeError(f"API {action} 返回格式异常")
    response_data = data.get("Response", data)
    if not isinstance(response_data, dict):
        raise TypeError(f"API {action} 的 Response 格式异常")
    if "Error" in response_data:
        error = response_data.get("Error") or {}
        if isinstance(error, dict):
            code = error.get("Code")
            message = error.get("Message")
        else:
            code = None
            message = str(error)
        raise TencentAPIError(action, code, message)
    return response_data


# ---------------------------------------------------------------------------
# DNSPod 业务逻辑
# ---------------------------------------------------------------------------
def describe_record_list(subdomain: str, line: str) -> list[dict[str, Any]]:
    """查询某线路下的全部 A 记录，自动处理分页。"""
    records: list[dict[str, Any]] = []
    offset = 0
    limit = 100
    seen_ids: set[Any] = set()
    pages = 0
    while True:
        pages += 1
        if pages > 200:
            raise RuntimeError("DescribeRecordList 分页超过 200 页，疑似 Offset 未生效，已中止")
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
            raise TypeError("API DescribeRecordList 返回的 RecordList 格式异常")
        page_ids = [record.get("RecordId") for record in page if isinstance(record, dict)]
        if page_ids and all(record_id in seen_ids for record_id in page_ids):
            raise RuntimeError("DescribeRecordList 返回重复页，疑似 Offset 未生效，已中止")
        seen_ids.update(record_id for record_id in page_ids if record_id is not None)
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


def delete_record(record_id: str | int) -> None:
    """删除 DNSPod 中的一条解析记录。"""
    tc3_request(
        "DeleteRecord",
        {"Domain": DOMAIN, "RecordId": record_id},
    )


def _record_sort_key(record: Mapping[str, Any]) -> tuple[int, int | str]:
    """按 RecordId 稳定排序，作为 DNSPod 多记录的位置顺序。"""
    record_id = record.get("RecordId")
    try:
        return (0, int(record_id))
    except (TypeError, ValueError):
        return (1, str(record_id))


def sync_line(subdomain: str, line: str, ips: list[str]) -> str:
    """让某条线路只保留目标 IP 列表，并按记录位置更新/创建/删除。"""
    target_ips = list(dict.fromkeys(ip.strip() for ip in ips if ip and ip.strip()))[:MAX_IPS_PER_LINE]
    if not target_ips:
        raise ValueError(f"线路「{line}」至少需要一个 IPv4")
    for ip in target_ips:
        if _public_ipv4(ip) is None:
            raise ValueError(f"无效或非公网 IPv4 地址: {ip!r}")

    records = describe_record_list(subdomain, line)
    matches = sorted(
        [
            record
            for record in records
            if str(record.get("Name", "")).lower() == subdomain.lower()
            and str(record.get("Line", "")) == line
        ],
        key=_record_sort_key,
    )
    for record in matches:
        if record.get("RecordId") is None:
            raise RuntimeError(f"线路「{line}」的记录缺少 RecordId")

    retained = matches[: len(target_ips)]
    extras = matches[len(target_ips) :]
    actions: list[str] = []

    for position, ip in enumerate(target_ips, start=1):
        if position <= len(retained):
            record = retained[position - 1]
            record_id = record["RecordId"]
            current = str(record.get("Value", "")).strip()
            if current == ip:
                continue
            if DRY_RUN:
                actions.append(
                    f"DRY-RUN: 将修改第{position}个位置 {current or '<空值>'} -> {ip}"
                )
            else:
                modify_record(record_id, subdomain, line, ip)
                actions.append(f"已更新第{position}个位置 {current or '<空值>'} -> {ip}")
        elif DRY_RUN:
            actions.append(f"DRY-RUN: 将创建第{position}个位置 -> {ip}")
        else:
            try:
                create_record(subdomain, line, ip)
            except TencentAPIError as exc:
                if exc.code == MUST_ADD_DEFAULT_LINE or MUST_ADD_DEFAULT_LINE in str(exc):
                    raise RuntimeError(
                        f"{exc}（提示：DNSPod 要求先存在「默认」线路的解析记录，才能添加分线路记录）"
                    ) from exc
                raise
            actions.append(f"已创建第{position}个位置 -> {ip}")

    for record in extras:
        record_id = record["RecordId"]
        current = str(record.get("Value", "")).strip()
        if DRY_RUN:
            actions.append(f"DRY-RUN: 将删除多余位置 {current or '<空值>'}")
        else:
            delete_record(record_id)
            actions.append(f"已删除多余位置 {current or '<空值>'}")

    if not actions:
        return f"{subdomain}/{line} 已有 {len(target_ips)} 个目标 IP，无需更新"
    return f"{subdomain}/{line}: " + "; ".join(actions)


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
        "各线路前%d名 IP：%s",
        MAX_IPS_PER_LINE,
        ", ".join(
            f"{line}={','.join(ips_by_line[line][:MAX_IPS_PER_LINE])}"
            for line in LINES
            if line in ips_by_line
        ),
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
            log.info(sync_line(SUBDOMAIN, line, ips[:MAX_IPS_PER_LINE]))
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
