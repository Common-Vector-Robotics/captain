import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_IDS = {"U0123456789", "C0123456789"}
TEXT_SUFFIXES = {".md", ".json", ".py", ".yml", ".yaml", ".plist"}


def product_text_paths():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    paths = {ROOT / name for name in package["files"]}
    paths.update({ROOT / "README.md", ROOT / "BOOTSTRAP.md"})
    return sorted(path for path in paths if path.is_file() and path.suffix in TEXT_SUFFIXES)


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


def test_setup_uses_the_complete_install_checker():
    for name in ("README.md", "BOOTSTRAP.md"):
        document = (ROOT / name).read_text(encoding="utf-8")
        assert "python3 scripts/check_install.py" in document
        assert "Captain is ready for shadow mode." in document


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


def test_beginner_install_checker_is_shipped_with_the_claw():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    manifest = (ROOT / "CLAW.md").read_text(encoding="utf-8")
    assert "scripts/check_install.py" in package["files"]
    assert "source: scripts/check_install.py" in manifest


def test_readme_has_one_safe_copy_paste_install_path():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "git clone https://github.com/Common-Vector-Robotics/captain.git" in readme
    assert "brew install openclaw/tap/gogcli" in readme
    assert "https://gogcli.sh/install.html" in readme
    assert "--version 2026.7.2-beta.5" in normalized
    assert "openclaw gateway status" in readme
    assert "openclaw agents bind" in readme
    assert "--agent captain" in normalized
    assert "--bind slack:captain" in normalized
    assert "python3 scripts/check_install.py" in readme
    assert "python3 scripts/install_heartbeat_policy.py --enable" in readme
    assert "openclaw cron list --agent captain --all" in readme
    assert readme.index("cd ~/.openclaw/workspace-captain") < readme.index(
        "mkdir -p .secrets"
    )
    assert "Keep the Gateway and Captain's scheduler stopped" not in readme
    assert readme.index("Test every Captain workflow") < readme.index(
        "## Switch to live mode"
    )


def test_core_install_does_not_require_optional_sentry_dependency():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install_section = readme.split("## Optional integrations", maxsplit=1)[0]
    assert "pip install" not in install_section


def test_readme_google_auth_is_least_privilege_and_fails_closed():
    """Catch setup guidance that authorizes broad or unverified Google access."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = re.split(
        r"(?m)^### Configure meeting ingestion$",
        readme,
        maxsplit=1,
    )[1]
    section = re.split(
        r"(?m)^### Connect Captain to Slack$",
        section,
        maxsplit=1,
    )[0]
    normalized = " ".join(section.replace("\\\n", "").split())

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
