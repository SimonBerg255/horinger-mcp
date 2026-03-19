"""
Norwegian Consultation Processing (Høringer) MCP Server

Processes Norwegian government consultation rounds (høringer) from regjeringen.no.
Run with: uvicorn server:app --host 0.0.0.0 --port $PORT
"""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from tools import (
    search_høringer,
    get_høring_details,
    get_all_høringssvar,
    get_single_høringssvar,
)

####### SERVER #######

mcp = FastMCP(
    name="Norwegian Consultation Processing (Høringer)",
    instructions=(
        "Use this server to process Norwegian government consultation rounds (høringer) from regjeringen.no.\n\n"
        "Primary use case: a ministry has received hundreds of høringssvar and needs to analyse, "
        "compare, and synthesise them.\n\n"
        "Tool usage:\n"
        "- search_horinger: find a høring by topic or ministry\n"
        "- get_horing_details: read the proposal document and extract the specific questions "
        "that were posed to respondents\n"
        "- get_all_horingssvar: retrieve the full set of published responses — use this as the "
        "main data source for analysis. Set max_results high (200+) when doing comprehensive analysis\n"
        "- get_single_horingssvar: read one specific response in depth\n\n"
        "Typical workflow: search → get details → get all responses → synthesise.\n"
        "For comparative analysis: get all responses twice with different respondent_type filters."
    ),
    version="1.0.0",
    website_url="https://www.regjeringen.no/no/dokument/hoyringar/id1763/",
)

####### TOOLS #######
# MCP spec requires ASCII tool names — register with explicit names

mcp.tool(name="search_horinger", meta={"requires_permission": False})(search_høringer)
mcp.tool(name="get_horing_details", meta={"requires_permission": False})(get_høring_details)
mcp.tool(name="get_all_horingssvar", meta={"requires_permission": False})(get_all_høringssvar)
mcp.tool(name="get_single_horingssvar", meta={"requires_permission": False})(get_single_høringssvar)

####### ROUTES #######


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


####### APP #######

app = mcp.http_app()
