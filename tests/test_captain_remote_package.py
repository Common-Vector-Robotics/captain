"""Verify the public Captain remote package and Nginx boundary."""

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "openclaw-plugin"
NGINX_EXAMPLE = PLUGIN / "examples/nginx-captain-remote.conf"


def npm_pack_paths(directory: Path) -> set[str]:
    """Return the paths npm would publish without creating a tarball."""

    result = subprocess.run(
        ["npm", "pack", "--dry-run", "--json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {entry["path"] for entry in json.loads(result.stdout)[0]["files"]}


def test_nginx_exposes_only_the_bounded_captain_https_route():
    config = NGINX_EXAMPLE.read_text(encoding="utf-8")

    for required in (
        "limit_req_zone $binary_remote_addr",
        "rate=5r/s",
        "burst=30",
        "limit_conn per_ip 20",
        "limit_conn per_server 100",
        "client_max_body_size 256k",
        "client_header_timeout 10s",
        "client_body_timeout 15s",
        "send_timeout 30s",
        "keepalive_timeout 30s",
        "large_client_header_buffers 2 8k",
        "location ^~ /captain/v1/",
        "proxy_pass http://127.0.0.1:18789",
        "proxy_set_header X-Captain-Client-IP $remote_addr",
        "location / { return 404; }",
    ):
        assert required in config

    assert re.search(r"listen\s+443\s+ssl\s*;", config)
    assert "ssl_certificate /etc/nginx/captain-remote.crt" in config
    assert "ssl_certificate_key /etc/nginx/captain-remote.key" in config
    assert config.count("proxy_pass ") == 1


def test_nginx_replaces_forwarding_headers_and_logs_no_credentials():
    config = NGINX_EXAMPLE.read_text(encoding="utf-8")

    for header in ("Forwarded", "X-Forwarded-For", "X-Real-IP"):
        assert f'proxy_set_header {header} "";' in config

    log_format = re.search(
        r"log_format\s+captain_remote\s+(.*?);", config, re.DOTALL
    )
    assert log_format is not None
    assert "$http_authorization" not in log_format.group(1)
    assert re.search(r"\$request(?![A-Za-z0-9_])", log_format.group(1)) is None
    assert (
        "access_log /var/log/nginx/captain-remote-access.log captain_remote;"
        in config
    )


def test_root_package_includes_the_native_remote_plugin():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    packaged = set(package["files"])
    assert {
        "openclaw-plugin/dist",
        "openclaw-plugin/examples",
        "openclaw-plugin/openclaw.plugin.json",
        "openclaw-plugin/package.json",
        "openclaw-plugin/README.md",
    } <= packaged

    paths = npm_pack_paths(ROOT)
    assert "openclaw-plugin/openclaw.plugin.json" in paths
    assert "openclaw-plugin/dist/index.js" in paths
    assert "openclaw-plugin/README.md" in paths
    assert "openclaw-plugin/examples/nginx-captain-remote.conf" in paths
    assert not any(
        path.startswith("openclaw-plugin/node_modules/") for path in paths
    )


def test_native_plugin_package_contains_operations_but_no_runtime_state():
    paths = npm_pack_paths(PLUGIN)
    assert {
        "package.json",
        "openclaw.plugin.json",
        "dist/index.js",
        "README.md",
        "examples/nginx-captain-remote.conf",
    } <= paths

    forbidden = re.compile(
        r"(^|/)(?:\.env(?:\..*)?|.*\.sqlite3(?:-wal|-shm)?|.*token.*)$",
        re.IGNORECASE,
    )
    assert sorted(path for path in paths if forbidden.search(path)) == []
