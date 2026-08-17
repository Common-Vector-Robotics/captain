"""MCP stdio entrypoint for local Captain session reports."""

from typing import Any

from mcp.server import MCPServer

from .reporting import CaptainReportResult, handle_session_report

mcp = MCPServer(
    "Captain",
    instructions=(
        "Report completed coding-agent work to the user's local Captain agent."
    ),
)


@mcp.tool()
def captain_session_report(
    report_id: str,
    report: dict[str, Any],
    metadata: dict[str, Any],
) -> CaptainReportResult:
    """Send one idempotent, redacted session report to local Captain."""

    return handle_session_report(report_id, report, metadata)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
