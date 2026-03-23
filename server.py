"""
Norwegian Legislative Data MCP Server

Data sources:
- regjeringen.no: ministry consultation rounds (høringer) and public responses
- data.stortinget.no: parliamentary cases, votes, questions, hearings, and more

Run with: uvicorn server:app --host 0.0.0.0 --port $PORT
"""

from fastmcp import FastMCP
from mcp.server.fastmcp import Icon
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from _icon import FAVICON_B64
from tools import (
    # Regjeringen.no consultation tools
    search_høringer,
    get_høring_details,
    list_høringssvar,
    get_all_høringssvar,
    get_single_høringssvar,
    # Stortinget parliamentary tools
    search_stortinget,
    get_stortinget_horinger,
    get_vote_result,
    stortinget_lookup,
    get_case_details,
    get_hearing_submissions,
    get_parliamentary_questions,
    find_stortinget_hearings,
)

####### SERVER #######

icon = Icon(src=FAVICON_B64)

mcp = FastMCP(
    name="Norwegian Legislative Data",
    instructions=(
        "Server for Norwegian legislative data: ministry consultations (regjeringen.no) "
        "and parliamentary proceedings (Stortinget open data API).\n\n"
        "DECISION TREE:\n\n"
        "--- MINISTRY CONSULTATIONS (regjeringen.no) ---\n\n"
        "1. FIND A HORING by topic or ministry:\n"
        "   → search_horinger\n\n"
        "2. READ THE PROPOSAL — what the ministry proposed and what questions they asked:\n"
        "   → get_horing_details\n\n"
        "3. SEE WHO RESPONDED — fast metadata listing, no text fetching:\n"
        "   → list_horingssvar\n\n"
        "4. READ ALL RESPONSE TEXTS — for synthesis, comparison, question mapping:\n"
        "   → get_all_horingssvar\n"
        "   Use respondent_type for comparative analysis. max_chars_per_response=0 for full text.\n\n"
        "5. READ ONE SPECIFIC RESPONSE IN FULL:\n"
        "   → get_single_horingssvar\n\n"
        "--- STORTINGET PARLIAMENTARY DATA ---\n\n"
        "6. FIND A PARLIAMENTARY CASE by keyword:\n"
        "   → search_stortinget\n\n"
        "7. GET FULL CASE DETAILS — metadata, committee, documents, decision text:\n"
        "   → get_case_details(sak_id)\n\n"
        "8. FIND COMMITTEE HEARINGS BY TOPIC and read submissions in one step:\n"
        "   → find_stortinget_hearings(topic, sesjon)\n"
        "   This is the recommended entry point — no hearing IDs needed.\n\n"
        "9. BROWSE COMMITTEE HEARINGS for a session or specific case:\n"
        "   → get_stortinget_horinger(sesjon, sak_id)\n\n"
        "10. READ HEARING SUBMISSIONS by known hearing ID:\n"
        "    → get_hearing_submissions(hearing_id)\n"
        "    WARNING: hearing_id must be a Stortinget ID (small 4-6 digit number),\n"
        "    NOT a regjeringen.no URL/ID. Use find_stortinget_hearings instead\n"
        "    if you don't already have a Stortinget hearing ID.\n\n"
        "11. GET VOTE RESULT — party-by-party breakdown:\n"
        "    → get_vote_result(sak_id)\n\n"
        "12. SEARCH PARLIAMENTARY QUESTIONS — oral, written, interpellations:\n"
        "    → get_parliamentary_questions(sesjon, question_type, topic)\n\n"
        "13. EXPLORE ANY STORTINGET DATA — direct API access to all ~30 endpoints:\n"
        "    → stortinget_lookup(endpoint, params)\n"
        "    Covers: representatives, parties, committees, meetings, agendas, publications,\n"
        "    decisions, electoral districts, speaker lists, government cabinet, and more.\n"
        "    See tool docstring for the full endpoint table.\n\n"
        "TYPICAL WORKFLOWS:\n"
        "- Ministry consultation synthesis: search_horinger → get_horing_details → get_all_horingssvar\n"
        "- Full legislative lifecycle: search_horinger → get_all_horingssvar → search_stortinget\n"
        "  → get_case_details → get_vote_result\n"
        "- Committee hearing by topic: find_stortinget_hearings(topic='...') [single call, no IDs]\n"
        "- Committee hearing by case: search_stortinget → get_stortinget_horinger(sak_id=...) → get_hearing_submissions\n"
        "- Question accountability: get_parliamentary_questions(topic='...', answered_by='...')\n"
        "- Reference data: stortinget_lookup('dagensrepresentanter') for current MPs\n\n"
        "KEY DISTINCTION:\n"
        "- regjeringen.no høringer = ministry consultation rounds (propose regulations, ask public)\n"
        "- Stortinget høringer = parliamentary committee hearings (after bill submitted to parliament)\n"
        "These are separate systems with different IDs. Never mix their IDs across tools."
    ),
    version="2.0.0",
    website_url="https://www.regjeringen.no/no/dokument/hoyringar/id1763/",
    icons=[icon],
)

####### TOOLS #######
# MCP spec requires ASCII tool names — register with explicit names

# Regjeringen.no consultation tools
mcp.tool(name="search_horinger", meta={"requires_permission": False})(search_høringer)
mcp.tool(name="get_horing_details", meta={"requires_permission": False})(get_høring_details)
mcp.tool(name="list_horingssvar", meta={"requires_permission": False})(list_høringssvar)
mcp.tool(name="get_all_horingssvar", meta={"requires_permission": False})(get_all_høringssvar)
mcp.tool(name="get_single_horingssvar", meta={"requires_permission": False})(get_single_høringssvar)

# Stortinget parliamentary tools
mcp.tool(name="search_stortinget", meta={"requires_permission": False})(search_stortinget)
mcp.tool(name="get_stortinget_horinger", meta={"requires_permission": False})(get_stortinget_horinger)
mcp.tool(name="get_vote_result", meta={"requires_permission": False})(get_vote_result)
mcp.tool(name="get_case_details", meta={"requires_permission": False})(get_case_details)
mcp.tool(name="find_stortinget_hearings", meta={"requires_permission": False})(find_stortinget_hearings)
mcp.tool(name="get_hearing_submissions", meta={"requires_permission": False})(get_hearing_submissions)
mcp.tool(name="get_parliamentary_questions", meta={"requires_permission": False})(get_parliamentary_questions)
mcp.tool(name="stortinget_lookup", meta={"requires_permission": False})(stortinget_lookup)

####### ROUTES #######


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


####### APP #######

app = mcp.http_app()
