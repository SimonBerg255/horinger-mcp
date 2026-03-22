"""
Norwegian Consultation Processing (Høringer) MCP Server

Processes Norwegian government consultation rounds (høringer) from regjeringen.no.
Run with: uvicorn server:app --host 0.0.0.0 --port $PORT
"""

from fastmcp import FastMCP
from mcp.server.fastmcp import Icon
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from _icon import FAVICON_B64
from tools import (
    search_høringer,
    get_høring_details,
    list_høringssvar,
    get_all_høringssvar,
    get_single_høringssvar,
)

####### SERVER #######

icon = Icon(src=FAVICON_B64)

mcp = FastMCP(
    name="Norwegian Consultation Processing (Høringer)",
    instructions=(
        "Use this server to process Norwegian government consultation rounds (høringer) from regjeringen.no.\n\n"
        "DECISION TREE — pick the right tool based on what is needed:\n\n"
        "1. FIND A HØRING by topic or ministry:\n"
        "   → search_horinger\n"
        "   Example: 'finn høringen om kommunelov' → search_horinger(query='kommunelov')\n\n"
        "2. READ THE PROPOSAL — what was the ministry proposing, what questions did they ask:\n"
        "   → get_horing_details\n"
        "   Returns full høringsnotat text and a list of specific questions asked.\n\n"
        "3. SEE WHO RESPONDED — fast overview of all respondents without reading content:\n"
        "   → list_horingssvar\n"
        "   Use this first on large høringer (50+ responses) to see the full list of\n"
        "   respondents, their types, and how many there are before fetching full texts.\n\n"
        "4. READ ALL RESPONSE TEXTS — for synthesis, comparison, or question mapping:\n"
        "   → get_all_horingssvar\n"
        "   Set max_chars_per_response=0 for full untruncated text.\n"
        "   Use respondent_type filter ('kommune', 'stat', 'organisasjon', 'naringsliv')\n"
        "   for comparative analysis between groups.\n\n"
        "5. READ ONE SPECIFIC RESPONSE IN FULL:\n"
        "   → get_single_horingssvar(url=response_url)\n"
        "   Use when get_all_horingssvar truncated a response you need in full,\n"
        "   or to verify a specific respondent's exact position.\n\n"
        "TYPICAL WORKFLOWS:\n"
        "- Full synthesis: search → get_horing_details → get_all_horingssvar → summarise\n"
        "- Large høring (50+ responses): search → list_horingssvar → get_all_horingssvar\n"
        "- Comparative: get_all_horingssvar(type='kommune') + get_all_horingssvar(type='naringsliv')\n"
        "- Question mapping: get_horing_details (get questions) → get_all_horingssvar\n"
        "- Deep read: get_single_horingssvar with response_url from list_horingssvar"
    ),
    version="1.0.0",
    website_url="https://www.regjeringen.no/no/dokument/hoyringar/id1763/",
    icons=[icon],
)

####### TOOLS #######
# MCP spec requires ASCII tool names — register with explicit names

mcp.tool(name="search_horinger", meta={"requires_permission": False})(search_høringer)
mcp.tool(name="get_horing_details", meta={"requires_permission": False})(get_høring_details)
mcp.tool(name="list_horingssvar", meta={"requires_permission": False})(list_høringssvar)
mcp.tool(name="get_all_horingssvar", meta={"requires_permission": False})(get_all_høringssvar)
mcp.tool(name="get_single_horingssvar", meta={"requires_permission": False})(get_single_høringssvar)

####### ROUTES #######


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


####### APP #######

app = mcp.http_app()
