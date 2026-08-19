"""MCP stdio entrypoint for Captain session reports."""

from typing import Any

from mcp.server import MCPServer

from .dispatch import handle_captain_turn
from .reporting import CaptainReportResult

mcp = MCPServer(
    "Captain",
    instructions=(
        "Report completed coding-agent work to the user's configured Captain agent."
    ),
)


@mcp.tool()
def captain_session_report(
    report_id: str,
    report: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    reply: str | None = None,
) -> CaptainReportResult:
    """Send a report or an exact user-authored follow-up to Captain."""

    return handle_captain_turn(report_id, report, metadata, reply)


def main() -> None:
    """Start the Captain MCP server over standard input and output."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
