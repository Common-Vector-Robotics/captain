"""MCP stdio entrypoint for Captain session reports."""

from typing import Any

from mcp.server import MCPServer

from .dispatch import handle_captain_turn
from .reporting import CaptainReportResult

mcp = MCPServer(
    "Captain",
    instructions=(
        "Report completed coding-agent work to the user's configured "
        "Captain agent."
    ),
)


@mcp.tool()
def captain_session_report(
    report_id: str,
    report: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    reply: str | None = None,
    cancel_pending: bool = False,
) -> CaptainReportResult:
    """Send a report/reply or clear one local pending continuation."""

    return handle_captain_turn(
        report_id,
        report,
        metadata,
        reply,
        cancel_pending=cancel_pending,
    )


def main() -> None:
    """Start the Captain MCP server over standard input and output."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
