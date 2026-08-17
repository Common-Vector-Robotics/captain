import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_IDS = {"U0123456789", "C0123456789"}
TEXT_SUFFIXES = {".md", ".json", ".py", ".sh", ".yml", ".yaml", ".plist"}
LAUNCHER = ROOT / "agent-plugin/bin/captain-agent-mcp"


def product_text_paths():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    paths = []
    for name in package["files"]:
        path = ROOT / name
        if path.is_dir():
            paths.extend(
                child for child in path.rglob("*")
                if child.is_file()
                and (child.suffix in TEXT_SUFFIXES or child == LAUNCHER)
            )
        elif path.is_file() and path.suffix in TEXT_SUFFIXES:
            paths.append(path)
    paths.extend((ROOT / "README.md", ROOT / "BOOTSTRAP.md"))
    return sorted(set(paths))


def test_product_text_paths_include_extensionless_plugin_launcher():
    assert LAUNCHER in product_text_paths()


def test_product_files_contain_no_private_deployment_literals():
    failures = []
    for path in product_text_paths():
        text = path.read_text(encoding="utf-8")
        if re.search(r"/Users/(?!example(?:/|\b))[^/\s]+/", text):
            failures.append(f"{path.relative_to(ROOT)}:private-user-path")
        if "com" + ".intermode" in text:
            failures.append(f"{path.relative_to(ROOT)}:private-launchd-label")
        ids = set(re.findall(r"\b[UC][A-Z0-9]{8,}\b", text)) - PLACEHOLDER_IDS
        if ids:
            failures.append(f"{path.relative_to(ROOT)}:configured-slack-id")
        if re.search(r"\b(?:Gavin|Arnold)\b", text):
            failures.append(f"{path.relative_to(ROOT)}:private-person-name")
    assert failures == []


def test_guidance_does_not_require_unshipped_private_files():
    combined = "\n".join(path.read_text(encoding="utf-8") for path in product_text_paths())
    assert "docs/daily-loop.md" not in combined
    assert "Work from `/Users/" not in combined
    assert "Read `MEMORY.md`." not in combined


def test_product_text_describes_only_the_rendered_service_scheduler():
    combined = "\n".join(path.read_text(encoding="utf-8") for path in product_text_paths())
    assert "scripts/render_sentry_service.py" in combined
    assert "ai.openclaw.captain-sentry-bridge.plist" in combined
    assert "launchd/com" + ".intermode.captain-sentry-bridge.plist" not in combined


def test_tools_references_only_shipped_scripts():
    tools = (ROOT / "TOOLS.md").read_text(encoding="utf-8")
    references = set(re.findall(r"`(scripts/[A-Za-z0-9_.-]+\.py)", tools))
    missing = sorted(path for path in references if not (ROOT / path).is_file())
    assert missing == []


def test_setup_fails_closed_when_daily_reporting_routing_is_missing():
    for name in ("README.md", "BOOTSTRAP.md"):
        document = (ROOT / name).read_text(encoding="utf-8")
        assert 'required = ("activity_digest_channel", "slack_account")' in document
        assert "Captain Slack routing configuration missing" in document
        assert "Captain Slack routing verified" in document


def test_bootstrap_creates_local_memory_and_user_files_without_overwriting():
    bootstrap = (ROOT / "BOOTSTRAP.md").read_text(encoding="utf-8")
    assert "for file in MEMORY.md USER.md" in bootstrap
    assert 'if [ ! -e "$file" ]' in bootstrap
    assert 'install -m 600 /dev/null "$file"' in bootstrap


def test_claw_sources_are_in_the_package_file_list():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    packaged = set(package["files"])
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    sources = set(re.findall(r"^\s+- source: ([^\n]+)$", manifest, re.MULTILINE))
    sources.update(re.findall(r"^\s+source: ([^\n]+)$", manifest, re.MULTILINE))
    assert sorted(sources - packaged) == []


def test_readme_google_auth_is_least_privilege_and_fails_closed():
    """Catch setup guidance that authorizes broad or unverified Google access."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = re.split(
        r"(?m)^### \d+\. Configure meeting ingestion$",
        readme,
        maxsplit=1,
    )[1]
    section = re.split(
        r"(?m)^### \d+\. Connect Captain to Slack$",
        section,
        maxsplit=1,
    )[0]
    normalized = " ".join(section.split())

    assert (
        "gog auth add captain@example.com --services gmail,drive,docs "
        "--readonly --drive-scope readonly"
        in normalized
    )
    assert (
        "gog auth list --check --account captain@example.com --no-input --json"
        in normalized
    )
    assert re.search(r"(?m)^gog auth add captain@example\.com\s*$", readme) is None

    allowed_scopes = {
        "email",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
    }
    for scope in allowed_scopes:
        assert f"`{scope}`" in normalized
    assert "GOG_KEYRING_BACKEND=file" in normalized
    assert "GOG_KEYRING_PASSWORD" in normalized


def test_release_plan_contains_no_private_host_paths():
    plan_root = ROOT / "docs/superpowers"
    private_path_pattern = r"/" + r"Users/(?!example(?:/|\b))[^/\s]+/"
    failures = [
        str(path.relative_to(ROOT))
        for path in plan_root.rglob("*.md")
        if re.search(private_path_pattern, path.read_text(encoding="utf-8"))
    ]
    assert failures == []
