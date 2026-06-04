#!/usr/bin/env python3

import base64
import hashlib
import ipaddress
import os
import random
import re
import socket
import subprocess
import threading
from datetime import datetime, timezone
from itertools import count
from typing import Dict, List, Optional, Set, Tuple

import uvicorn
from fastapi import FastAPI, Response

# Edit these values directly if you do not want to use environment variables.
# Use "0.0.0.0:9143" to listen on every local network interface.
API_BIND = os.getenv("API_BIND", "0.0.0.0:9143")
ONLINE_EGRESS_IPS_CONFIG = os.getenv("ONLINE_EGRESS_IPS", "")
ONLINE_EGRESS_IFACE = os.getenv("ONLINE_EGRESS_IFACE", "")
DH_MAIN_SERVER = os.getenv("DH_MAIN_SERVER", "www.easy4ipcloud.com")
DH_MAIN_PORT = int(os.getenv("DH_MAIN_PORT", "8800"))
DH_MAIN_SERVER_IPS_CONFIG = os.getenv(
    "DH_MAIN_SERVER_IPS",
    ",".join(
        [
            "152.32.199.10",
            "152.32.199.218",
            "152.32.199.251",
            "152.32.197.243",
            "152.32.199.183",
            "152.32.200.8",
            "152.32.200.17",
            "152.32.200.14",
            "152.32.197.191",
            "152.32.200.105",
            "152.32.199.2",
            "152.32.200.253",
            "146.235.211.50",
            "192.9.243.233",
            "159.54.166.208",
            "155.248.199.231",
            "146.235.223.187",
            "159.54.167.231",
            "165.154.165.79",
            "165.154.165.110",
            "165.154.165.33",
            "165.154.165.21",
            "165.154.165.40",
            "165.154.165.154",
            "165.154.165.27",
            "165.154.165.8",
            "165.154.165.19",
            "165.154.165.48",
            "165.154.165.37",
            "165.154.165.15",
            "165.154.165.231",
            "165.154.165.47",
            "165.154.165.43",
            "165.154.165.53",
            "165.154.165.41",
            "165.154.165.252",
            "165.154.165.35",
            "165.154.165.42",
            "165.154.198.11"
        ]
    ),
)
DH_UDP_TIMEOUT_SECS = float(os.getenv("DH_UDP_TIMEOUT_SECS", "5.0"))
ONLINE_MAX_CONCURRENT_CONFIG = int(os.getenv("ONLINE_MAX_CONCURRENT", "20"))
ONLINE_WAIT_TIMEOUT_SECS_CONFIG = float(os.getenv("ONLINE_WAIT_TIMEOUT_SECS", "30.0"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
LOG_UPSTREAM_ERRORS = os.getenv("LOG_UPSTREAM_ERRORS", "1").lower() not in {"0", "false", "no"}
BIND_EGRESS_FALLBACK = os.getenv("BIND_EGRESS_FALLBACK", "1").lower() not in {"0", "false", "no"}


MAIN_SERVER = DH_MAIN_SERVER
MAIN_PORT = DH_MAIN_PORT
UDP_TIMEOUT_SECS = DH_UDP_TIMEOUT_SECS

DEFAULT_USERNAME = "cba1b29e32cb17aa46b8ff9e73c7f40b"
DEFAULT_USERKEY = "996103384cdf19179e19243e959bbf8b"
USERNAME = os.getenv("DH_USERNAME", DEFAULT_USERNAME)
USERKEY = os.getenv("DH_USERKEY", DEFAULT_USERKEY)

ONLINE_MAX_CONCURRENT = ONLINE_MAX_CONCURRENT_CONFIG
ONLINE_WAIT_TIMEOUT_SECS = ONLINE_WAIT_TIMEOUT_SECS_CONFIG

_cseq = count(1)
_cseq_lock = threading.Lock()
_egress_lock = threading.Lock()
_egress_idx = 0
_main_server_lock = threading.Lock()
_main_server_idx = 0
_online_gate = threading.BoundedSemaphore(max(ONLINE_MAX_CONCURRENT, 1))
_bind_state = threading.local()

app = FastAPI(title="Dahua Online API", docs_url=None, redoc_url=None)


class DhResponse:
    def __init__(self, code, status, headers, body):
        self.code = code
        self.status = status
        self.headers = headers
        self.body = body


def next_cseq() -> int:
    with _cseq_lock:
        return next(_cseq)


def build_request(path: str, body: str = "", with_auth: bool = True) -> bytes:
    method = "DHPOST" if body else "DHGET"
    nonce = random.randrange(0, 2**31)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    password = f"{nonce}{created}DHP2P:{USERNAME}:{USERKEY}"
    digest = base64.b64encode(hashlib.sha1(password.encode("utf-8")).digest()).decode("ascii")

    lines = [
        f"{method} {path} HTTP/1.1",
        f"CSeq: {next_cseq()}",
    ]
    if with_auth:
        lines.append('Authorization: WSSE profile="UsernameToken"')
        lines.append(
            f'X-WSSE: UsernameToken Username="{USERNAME}", '
            f'PasswordDigest="{digest}", Nonce="{nonce}", Created="{created}"'
        )
    if body:
        lines.append("Content-Type: ")
        lines.append(f"Content-Length: {len(body.encode('utf-8'))}")

    raw = "\r\n".join(lines) + "\r\n\r\n" + body
    return raw.encode("utf-8")


def parse_response(raw: bytes) -> DhResponse:
    text = raw.decode("utf-8", errors="replace")
    if "\r\n\r\n" not in text:
        raise ValueError("invalid response format")

    head, body = text.split("\r\n\r\n", 1)
    lines = head.splitlines()
    if not lines:
        raise ValueError("empty response")

    parts = lines[0].split(" ", 2)
    if len(parts) < 2:
        raise ValueError("missing status code")

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key] = value

    return DhResponse(
        code=int(parts[1]),
        status=parts[2] if len(parts) > 2 else "UNKNOWN",
        headers=headers,
        body=body,
    )


def bind_udp(egress_ip: Optional[str]) -> socket.socket:
    family = socket.AF_INET6 if egress_ip and ":" in egress_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(UDP_TIMEOUT_SECS)
    bind_host = egress_ip or ("::" if family == socket.AF_INET6 else "0.0.0.0")
    try:
        sock.bind((bind_host, 0))
    except PermissionError as exc:
        sock.close()
        if not egress_ip or not BIND_EGRESS_FALLBACK:
            raise
        print(
            "egress bind failed ip={} error={}; falling back to OS routing".format(
                egress_ip,
                exc,
            ),
            flush=True,
        )
        _bind_state.fell_back = True
        fallback = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fallback.settimeout(UDP_TIMEOUT_SECS)
        fallback.bind(("0.0.0.0", 0))
        return fallback
    return sock


def host_is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def parse_ip_pool(value: str, family: Optional[int] = None) -> List[str]:
    ips: List[str] = []
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if ip.is_loopback:
            continue
        if family == socket.AF_INET and ip.version != 4:
            continue
        if family == socket.AF_INET6 and ip.version != 6:
            continue
        ips.append(str(ip))
    return ips


def pick_main_server_ip(family: int) -> Optional[str]:
    global _main_server_idx
    pool = MAIN_SERVER_IPS_BY_FAMILY.get(family, [])
    if not pool:
        return None
    with _main_server_lock:
        ip = pool[_main_server_idx % len(pool)]
        _main_server_idx += 1
        return ip


def resolve_one(host: str, port: int, family: int) -> tuple:
    if host_is_ip(host):
        return (host, port)

    if host == MAIN_SERVER:
        preconfigured_ip = pick_main_server_ip(family)
        if preconfigured_ip:
            return (preconfigured_ip, port)

    infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    if not infos:
        raise RuntimeError(f"no address resolved for {host}:{port}")
    return infos[0][4]


def send_dh_request(
    sock: socket.socket,
    host: str,
    port: int,
    path: str,
    body: Optional[str] = None,
    with_auth: bool = True,
    should_read: bool = True,
) -> Optional[DhResponse]:
    remote = resolve_one(host, port, sock.family)
    sock.sendto(build_request(path, body or "", with_auth), remote)

    if not should_read:
        return None

    try:
        data, _addr = sock.recvfrom(8192)
    except socket.timeout as exc:
        raise TimeoutError(f"timeout waiting response from {host}:{port}") from exc

    return parse_response(data)


def request_required(
    sock: socket.socket,
    host: str,
    port: int,
    path: str,
    body: Optional[str] = None,
    with_auth: bool = True,
    should_read: bool = True,
) -> DhResponse:
    response = send_dh_request(sock, host, port, path, body, with_auth, should_read)
    if response is None:
        raise RuntimeError(f"empty response for {path}")
    return response


def tag_value(xml: str, tag: str) -> Optional[str]:
    match = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", xml, re.DOTALL)
    return match.group(1) if match else None


def split_host_port(value: str) -> Tuple[str, int]:
    if ":" not in value:
        raise ValueError(f"invalid endpoint: {value}")
    host, port = value.rsplit(":", 1)
    return host, int(port)


def check_online_impl(serial: str, egress_ip: Optional[str]) -> bool:
    with bind_udp(egress_ip) as main_remote:
        p2p_info = request_required(
            main_remote,
            MAIN_SERVER,
            MAIN_PORT,
            f"/online/p2psrv/{serial}",
        )

    if p2p_info.code >= 400:
        return False

    us = tag_value(p2p_info.body, "US")
    if not us:
        return False

    p2p_host, p2p_port = split_host_port(us)

    def query_probe_info(with_auth: bool) -> Tuple[DhResponse, DhResponse]:
        with bind_udp(egress_ip) as p2p_remote:
            probe_response = request_required(
                p2p_remote,
                p2p_host,
                p2p_port,
                f"/probe/device/{serial}",
                with_auth=with_auth,
            )
            info_response = request_required(
                p2p_remote,
                p2p_host,
                p2p_port,
                f"/info/device/{serial}",
                with_auth=with_auth,
            )
        return probe_response, info_response

    probe, info = query_probe_info(with_auth=True)
    key_error_auth = probe.code == 401 and "KeyError" in probe.body
    key_error_auth = key_error_auth or (info.code == 401 and "KeyError" in info.body)
    if key_error_auth:
        probe, info = query_probe_info(with_auth=False)

    if probe.code >= 400 or info.code >= 400:
        return False

    return bool(info.body.strip())


def parse_egress_ips(value: str) -> List[str]:
    return parse_ip_pool(value)


def run_command(args: List[str]) -> str:
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=4,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def discover_ips_from_ip_command(iface: Optional[str]) -> List[str]:
    args = ["ip", "-o", "address", "show"]
    if iface:
        args.extend(["dev", iface])
    text = run_command(args)
    ips: List[str] = []
    for line in text.splitlines():
        match = re.search(r"\sinet\s+([0-9.]+)/\d+", line)
        if match:
            ip = ipaddress.ip_address(match.group(1))
            if not ip.is_loopback:
                ips.append(str(ip))
    return ips


def discover_ips_from_ifconfig(iface: Optional[str]) -> List[str]:
    args = ["ifconfig", iface] if iface else ["ifconfig"]
    text = run_command(args)
    ips: List[str] = []
    for match in re.finditer(r"\binet(?: addr:)?\s*([0-9.]+)", text):
        ip = ipaddress.ip_address(match.group(1))
        if not ip.is_loopback:
            ips.append(str(ip))
    return ips


def discover_egress_ips() -> List[str]:
    configured = ONLINE_EGRESS_IPS_CONFIG
    if configured.strip():
        return parse_egress_ips(configured)

    iface = ONLINE_EGRESS_IFACE.strip() or None
    ips = discover_ips_from_ip_command(iface)
    if not ips:
        ips = discover_ips_from_ifconfig(iface)

    seen: Set[str] = set()
    unique: List[str] = []
    for ip in ips:
        if ip not in seen:
            unique.append(ip)
            seen.add(ip)
    return unique


EGRESS_IPS = discover_egress_ips()
MAIN_SERVER_IPS_BY_FAMILY = {
    socket.AF_INET: parse_ip_pool(DH_MAIN_SERVER_IPS_CONFIG, socket.AF_INET),
    socket.AF_INET6: parse_ip_pool(DH_MAIN_SERVER_IPS_CONFIG, socket.AF_INET6),
}


def pick_egress_ip() -> Optional[str]:
    global _egress_idx
    if not EGRESS_IPS:
        return None
    with _egress_lock:
        ip = EGRESS_IPS[_egress_idx % len(EGRESS_IPS)]
        _egress_idx += 1
        return ip


@app.get("/online/{serial}")
def online(serial: str, response: Response):
    acquired = _online_gate.acquire(timeout=ONLINE_WAIT_TIMEOUT_SECS)
    if not acquired:
        response.status_code = 502
        return {
            "ok": False,
            "serial": serial,
            "online": False,
            "egress_ip": None,
            "error": "timeout waiting for online concurrency slot",
        }

    egress_ip = pick_egress_ip()
    _bind_state.fell_back = False
    try:
        online_result = check_online_impl(serial, egress_ip)
        response_egress_ip = None if getattr(_bind_state, "fell_back", False) else egress_ip
        return {
            "ok": True,
            "serial": serial,
            "online": online_result,
            "egress_ip": response_egress_ip,
            "error": None,
        }
    except Exception as exc:
        response_egress_ip = None if getattr(_bind_state, "fell_back", False) else egress_ip
        if LOG_UPSTREAM_ERRORS:
            print(
                "online error serial={} egress_ip={} error_type={} error={}".format(
                    serial,
                    response_egress_ip,
                    type(exc).__name__,
                    exc,
                ),
                flush=True,
            )
        response.status_code = 502
        return {
            "ok": False,
            "serial": serial,
            "online": False,
            "egress_ip": response_egress_ip,
            "error": str(exc),
        }
    finally:
        _online_gate.release()


def parse_bind(value: str) -> Tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host, int(port)


def main() -> None:
    bind = API_BIND
    host, port = parse_bind(bind)
    print(f"API_BIND={bind}")
    print(f"DH_MAIN_SERVER={MAIN_SERVER} DH_MAIN_PORT={MAIN_PORT}")
    print(f"DH_MAIN_SERVER_IPS={MAIN_SERVER_IPS_BY_FAMILY[socket.AF_INET] or 'DNS resolver'}")
    print(f"EGRESS_IPS={EGRESS_IPS or 'OS routing'}")
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL)


if __name__ == "__main__":
    main()
