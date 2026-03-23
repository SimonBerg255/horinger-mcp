"""
Tools for processing Norwegian government consultation responses (høringssvar)
from regjeringen.no, and parliamentary data from Stortinget's open data API.

Data sources:
- regjeringen.no: ministry consultation rounds (høringer) and responses (høringssvar)
- data.stortinget.no: parliamentary cases (saker), committee hearings, and vote results

No authentication required. Both sources are fully public.
"""

import asyncio
import httpx
import pdfplumber
import io
import re
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlencode, urlparse, urlunparse

BASE_URL = "https://www.regjeringen.no"
HØRINGSLIST_URL = f"{BASE_URL}/no/dokument/hoyringar/id1763/"
REQUEST_DELAY = 1.5  # Polite delay between requests (increased to avoid Cloudflare 429s)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────

def classify_respondent(name: str) -> str:
    """Classify a respondent by their name into a category."""
    name_lower = name.lower()

    if any(kw in name_lower for kw in ["fylkeskommune"]):
        return "fylkeskommune"
    if any(kw in name_lower for kw in ["kommune", "kommunene"]):
        return "kommune"
    if any(kw in name_lower for kw in [
        "departement", "direktorat", "tilsyn", "statsforvalter",
        "riksrevisjonen", "sysselmannen", "ombudsman", "datatilsynet",
        "domstoladministrasjonen", "nasjonal", "statens", "sametinget",
        "sivilombudsmannen", "politidirektoratet", "utdanningsdirektoratet",
        "helsedirektoratet", "skattedirektoratet", "arbeidstilsynet",
        "mattilsynet", "miljødirektoratet", "fiskeridirektoratet",
    ]):
        return "stat"
    if any(kw in name_lower for kw in [
        "forbund", "forening", "lag", "organisasjon", "sammenslutning",
        "stiftelse", "forum", "nettverk", "landsforening", "samvirke",
        "rådet", "råd", "interesseorganisasjon", "interesseorg",
        "næringsorganisasjon", "bransjeorganisasjon", "fagorganisasjon",
        "yrkesorganisasjon", "paraplyorganisasjon", "ideell", "interesseforening",
    ]):
        return "organisasjon"
    return "naringsliv"


def classify_respondent_from_instans(instans: str) -> str:
    """
    Classify respondent using the data-instans attribute from regjeringen.no.

    The instans values are standardised category names used by the site.
    """
    instans_lower = instans.lower()

    if "fylkeskommune" in instans_lower:
        return "fylkeskommune"
    if "kommune" in instans_lower:
        return "kommune"
    if any(kw in instans_lower for kw in [
        "departement", "direktorat", "offentlig etat", "offentlig", "statsforvalter",
        "domstol", "riksrevisjonen", "forvaltning", "politi", "forsvar",
    ]):
        return "stat"
    if any(kw in instans_lower for kw in [
        "organisasjon", "forening", "forbund", "lag", "interesseorg",
        "arbeidsgiverorganisasjon", "arbeidstakerorganisasjon",
        "bruker", "frivillig", "næringslivs", "bransje", "fagorg",
        "ideell", "stiftelse",
    ]):
        return "organisasjon"
    if any(kw in instans_lower for kw in [
        "bedrift", "foretak", "næring", "virksomhet", "selskap",
        "konsern", "annen virksomhet",
    ]):
        return "naringsliv"
    # Fall back to name-based classification
    return "naringsliv"  # Default


def extract_pdf_text(content: bytes) -> str:
    """Extract all text from PDF bytes using pdfplumber."""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            texts = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    texts.append(text)
            return "\n".join(texts)
    except Exception as e:
        print(f"[PDF] Error extracting text: {e}")
        return ""


def extract_specific_questions(text: str) -> list:
    """
    Extract explicit questions from høringsnotat text.

    Looks for sections asking for respondent input, numbered questions,
    and explicit question markers.
    """
    questions = []

    # Look for key phrases that introduce questions
    trigger_patterns = [
        r"vi ber høringsinstansene særlig uttale seg om[:\s]+(.{10,500}?)(?=\n\n|\Z)",
        r"departementet ber om synspunkter på[:\s]+(.{10,500}?)(?=\n\n|\Z)",
        r"spørsmål til høringsinstansene[:\s]*\n(.{10,1000}?)(?=\n\n[A-ZÆØÅ]|\Z)",
        r"høringsinstansene bes særlig uttale seg om[:\s]+(.{10,500}?)(?=\n\n|\Z)",
        r"vi ber særlig om[:\s]+(.{10,500}?)(?=\n\n|\Z)",
    ]

    for pattern in trigger_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        for m in matches:
            clean = m.strip()
            if len(clean) > 10:
                questions.append(clean[:500])

    # Look for numbered question items (1. ..., 2. ..., etc.)
    numbered = re.findall(r'^\s*(\d+[\.\)]\s+[A-ZÆØÅ].{20,300})', text, re.MULTILINE)
    for q in numbered[:15]:
        q = q.strip()
        if q not in questions:
            questions.append(q)

    # Look for explicit question marks
    question_sentences = re.findall(r'[A-ZÆØÅ][^?]{20,200}\?', text)
    for q in question_sentences[:10]:
        q = q.strip()
        if q not in questions:
            questions.append(q)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for q in questions:
        key = q[:50]
        if key not in seen:
            seen.add(key)
            unique.append(q)

    return unique[:25]


def make_absolute_url(url: str, base: str = BASE_URL) -> str:
    """Convert a relative URL to absolute."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return urljoin(base, url)


def get_høring_base_url(url: str) -> str:
    """Strip query params from URL."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


async def fetch_html(client: httpx.AsyncClient, url: str, delay: float = 1.5, max_retries: int = 3) -> Optional[str]:
    """Fetch HTML from URL with polite delay and retry on 429."""
    await asyncio.sleep(delay)
    for attempt in range(max_retries):
        try:
            resp = await client.get(url, headers=DEFAULT_HEADERS, follow_redirects=True, timeout=30)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                retry_after = max(retry_after, 30)  # Wait at least 30s for Cloudflare
                print(f"[fetch_html] 429 rate limit on {url[:60]}, waiting {retry_after}s (attempt {attempt+1})")
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 30))
                retry_after = max(retry_after, 30)
                print(f"[fetch_html] 429 rate limit, waiting {retry_after}s (attempt {attempt+1})")
                await asyncio.sleep(retry_after)
                continue
            print(f"[fetch_html] Error fetching {url}: {e}")
            return None
        except Exception as e:
            print(f"[fetch_html] Error fetching {url}: {e}")
            return None
    print(f"[fetch_html] Max retries exceeded for {url[:60]}")
    return None


async def fetch_binary(client: httpx.AsyncClient, url: str, delay: float = 1.0) -> Optional[bytes]:
    """Fetch binary content (e.g. PDF) from URL with polite delay."""
    await asyncio.sleep(delay)
    try:
        resp = await client.get(url, headers=DEFAULT_HEADERS, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"[fetch_binary] Error fetching {url}: {e}")
        return None


def parse_pagination(html: str) -> dict:
    """
    Extract pagination info from HTML.

    Returns dict with total, total_pages, current_page.
    Handles multiple patterns from regjeringen.no.
    """
    result = {"total": 0, "total_pages": 1, "current_page": 1}

    # "Viser X-Y av Z treff" (large høringer)
    m = re.search(r'Viser\s+\d+[-–]\d+\s+av\s+([\d\s\xa0]+)\s+treff', html)
    if m:
        total_str = m.group(1).replace(" ", "").replace("\xa0", "")
        try:
            result["total"] = int(total_str)
        except ValueError:
            pass

    # "Søket ditt gav N treff." (smaller høringer, response listing page)
    if result["total"] == 0:
        m = re.search(r'S[øo]ket\s+ditt\s+gav\s+([\d\s\xa0]+)\s+treff', html, re.IGNORECASE)
        if m:
            total_str = m.group(1).replace(" ", "").replace("\xa0", "")
            try:
                result["total"] = int(total_str)
            except ValueError:
                pass

    # "Side N av M"
    m = re.search(r'Side\s+(\d+)\s+av\s+(\d+)', html)
    if m:
        try:
            result["current_page"] = int(m.group(1))
            result["total_pages"] = int(m.group(2))
        except ValueError:
            pass

    # If total found but no pagination, infer total_pages from 100-per-page
    if result["total"] > 0 and result["total_pages"] == 1:
        result["total_pages"] = max(1, (result["total"] + 99) // 100)

    return result


def parse_høringssvar_entries(html: str, høring_base_url: str) -> list:
    """
    Parse response listing HTML to extract individual response entries.

    Looks for <ul data-horingssvar-list> containing <li> elements with:
    - <a href="?uid=UUID"> for inline HTML responses
    - <a href="/contentassets/...pdf?uid=Name"> for PDF responses

    Returns list of dicts: {"name": str, "url": str, "type": "html"|"pdf", "instans": str}
    """
    soup = BeautifulSoup(html, "lxml")
    entries = []

    # Primary: find the <ul data-horingssvar-list> element
    svar_ul = soup.find("ul", attrs={"data-horingssvar-list": True})
    if not svar_ul:
        # Fallback: find any <ul> that contains ?uid= links
        for ul in soup.find_all("ul"):
            if ul.find("a", href=lambda h: h and "?uid=" in h):
                svar_ul = ul
                break

    if not svar_ul:
        # Last resort: find individual ?uid= and pdf links anywhere
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if href.startswith("?uid="):
                name = a.get_text(strip=True)
                name = re.sub(r'\s+', ' ', name).strip()
                if name:
                    entries.append({
                        "name": name,
                        "url": høring_base_url + href,
                        "type": "html",
                        "instans": "",
                    })
            elif ".pdf" in href.lower() and "/contentassets/" in href:
                raw_name = a.get_text(strip=True)
                name = re.sub(r'\s*\(PDF[^)]*\)\s*$', '', raw_name, flags=re.IGNORECASE).strip()
                if name:
                    entries.append({
                        "name": name,
                        "url": make_absolute_url(href.split("?")[0]),  # Remove ?uid= from PDF URL
                        "type": "pdf",
                        "instans": "",
                    })
        return entries

    # Parse the list items from the found <ul>
    for li in svar_ul.find_all("li", recursive=False):
        a = li.find("a", href=True)
        if not a:
            continue

        href = a.get("href", "").strip()
        if not href:
            continue

        raw_name = a.get_text(strip=True)
        raw_name = re.sub(r'\s+', ' ', raw_name).strip()

        # Get data-instans attribute for respondent type classification
        instans = li.get("data-instans", "")

        # Clean name: remove "(PDF, XXkB)" suffix
        name = re.sub(r'\s*\(PDF[^)]*\)\s*$', '', raw_name, flags=re.IGNORECASE).strip()
        name = re.sub(r'\s*\(\d+\s*[KM]B\)\s*$', '', name, flags=re.IGNORECASE).strip()
        name = re.sub(r'\s+', ' ', name).strip()

        if not name:
            continue

        if href.startswith("?uid="):
            # Inline HTML response
            full_url = høring_base_url + href
            entries.append({
                "name": name,
                "url": full_url,
                "type": "html",
                "instans": instans,
            })
        elif ".pdf" in href.lower() and "/contentassets/" in href:
            # PDF response — strip the ?uid= param from the URL
            pdf_url = href.split("?")[0]
            full_url = make_absolute_url(pdf_url)
            entries.append({
                "name": name,
                "url": full_url,
                "type": "pdf",
                "instans": instans,
            })

    return entries


async def fetch_html_response_text(client: httpx.AsyncClient, url: str) -> tuple:
    """
    Fetch and parse an inline HTML høringssvar page.

    Returns (respondent_name, date, response_text)
    """
    html = await fetch_html(client, url, delay=0.3)
    if not html:
        return None, None, None

    soup = BeautifulSoup(html, "lxml")

    # Respondent name from h1
    h1 = soup.find("h1")
    respondent = h1.get_text(strip=True) if h1 else ""
    # Remove "Høringssvar fra " prefix
    respondent = re.sub(r'^Høringssvar\s+fra\s+', '', respondent, flags=re.IGNORECASE).strip()

    # Date — search raw HTML first (reliable)
    date = ""
    m = re.search(r'Dato[:\s]*(?:<[^>]+>)*\s*(\d{2}\.\d{2}\.\d{4})', html)
    if m:
        date = m.group(1)
    else:
        # Fallback: any date in content area
        m2 = re.search(r'(\d{2}\.\d{2}\.\d{4})', html)
        if m2:
            date = m2.group(1)

    # Response text — use div.article-body (primary), fallback to content-col-2 or mainContentArea
    response_text = ""

    # Try div.article-body first (regjeringen.no response pages)
    body_div = soup.find("div", class_="article-body")
    if body_div:
        response_text = body_div.get_text(separator="\n", strip=True)
    else:
        # Try other content containers
        for cls_name in ["article-body", "mainContentArea", "content-col-2"]:
            found = soup.find("div", class_=lambda x: x and cls_name in (
                " ".join(x) if isinstance(x, list) else (x or "")
            ))
            if found:
                response_text = found.get_text(separator="\n", strip=True)
                break

    if not response_text:
        # Last resort: try article tag, then main
        article = soup.find("article")
        if article:
            response_text = article.get_text(separator="\n", strip=True)
        else:
            main = soup.find("main")
            if main:
                for tag in main.find_all(["nav", "header", "footer"]):
                    tag.decompose()
                response_text = main.get_text(separator="\n", strip=True)

    return respondent, date, response_text.strip()


# ─────────────────────────────────────────────
# Main tools
# ─────────────────────────────────────────────

async def search_høringer(
    query: str,
    status: str = "all",
    ministry: Optional[str] = None,
    max_results: int = 10,
) -> dict:
    """
    Search for consultation rounds (høringer) on regjeringen.no.

    Use this to find the høring whose responses need to be processed.
    Searches by keyword in title. Returns matching høringer with metadata.

    For analysis work, use status="all" or status="closed" since the published
    responses exist for completed consultations.

    Args:
        query: Keywords in Norwegian or English, e.g. "kommunelov", "helselov",
               "offentlige anskaffelser", "digitalisering"
        status: "open" (active), "closed" (completed), "all" (default)
        ministry: Optional ministry name filter, e.g. "Helse", "Kommunal"
        max_results: Maximum results to return, default 10

    Returns:
        dict with "results" list and "total_found" count. Each result has:
        title, url, høring_id, published_date, deadline, status, ministry,
        short_description, response_count (None — requires individual page fetch)
    """
    results = []
    query_words = [w.lower() for w in query.split() if w]
    # Scan up to 100 pages to find matches — each page has 20 items (2000 total candidates)
    # At 0.2s delay this takes up to ~20s but ensures good coverage
    max_pages = 100
    scan_delay = 0.2  # Faster scan since we're just reading titles

    async with httpx.AsyncClient() as client:
        for page_num in range(1, max_pages + 1):
            if len(results) >= max_results:
                break

            page_url = f"{HØRINGSLIST_URL}?sortby=1&page={page_num}"
            html = await fetch_html(client, page_url, delay=scan_delay)
            if not html:
                break

            soup = BeautifulSoup(html, "lxml")

            # Find list items with høring links
            # Structure: <li class="listItem"><h2 class="title"><a href=...>Title</a></h2>...
            found_any = False

            # Primary: look for <li class="listItem"> with <h2 class="title"> inside
            list_items = soup.find_all("li", class_="listItem")
            if not list_items:
                # Fallback: any <li> containing a /dokumenter/ link
                list_items = [
                    li for li in soup.find_all("li")
                    if li.find("a", href=lambda h: h and "/dokumenter/" in h)
                ]

            for li in list_items:
                # Find the title link — prefer the one in <h2 class="title">
                h2 = li.find("h2", class_="title")
                a = h2.find("a", href=True) if h2 else li.find("a", href=True)
                if not a:
                    continue
                href = a.get("href", "")
                if "/dokumenter/" not in href:
                    continue

                found_any = True
                title = a.get_text(strip=True)

                # Client-side keyword filter — match ANY query word
                title_lower = title.lower()
                if query_words and not any(w in title_lower for w in query_words):
                    continue

                full_url = make_absolute_url(href)

                # Extract høring_id from URL
                id_match = re.search(r'/id(\d+)/?', href)
                høring_id = id_match.group(1) if id_match else None

                # Extract metadata from li text
                li_text = li.get_text(separator=" ", strip=True)

                # Date pattern DD.MM.YYYY
                dates = re.findall(r'\d{2}\.\d{2}\.\d{4}', li_text)
                published_date = dates[0] if dates else None
                deadline = None
                deadline_match = re.search(r'Høringsfrist:\s*(\d{2}\.\d{2}\.\d{4})', li_text)
                if deadline_match:
                    deadline = deadline_match.group(1)
                elif len(dates) > 1:
                    deadline = dates[-1]

                # Status
                item_status = "unknown"
                li_text_lower = li_text.lower()
                if "på høring" in li_text_lower:
                    item_status = "open"
                elif "ferdigbehandlet" in li_text_lower or "avsluttet" in li_text_lower:
                    item_status = "closed"

                # Apply status filter
                if status == "open" and item_status != "open":
                    continue
                if status == "closed" and item_status != "closed":
                    continue

                # Ministry — look for "departementet" or "direktorat" in li text
                item_ministry = ""
                ministry_match = re.search(
                    r'([\w\-]+(?:departementet|direktoratet|ministeriet))',
                    li_text, re.IGNORECASE
                )
                if ministry_match:
                    item_ministry = ministry_match.group(1)
                else:
                    # Try to find ministry name from list item context
                    # Often appears as: "17.03.2026 Høring Kunnskapsdepartementet"
                    dept_match = re.search(
                        r'Høring\s+(\w+(?:departementet|direktoratet))',
                        li_text, re.IGNORECASE
                    )
                    if dept_match:
                        item_ministry = dept_match.group(1)

                # Apply ministry filter
                if ministry and ministry.lower() not in item_ministry.lower():
                    continue

                # Short description — text after the link
                desc = li_text
                # Remove the title itself
                desc = desc.replace(title, "").strip()
                # Remove dates
                desc = re.sub(r'\d{2}\.\d{2}\.\d{4}', '', desc).strip()
                # Truncate
                short_desc = desc[:200] if desc else ""

                results.append({
                    "title": title,
                    "url": full_url,
                    "høring_id": høring_id,
                    "published_date": published_date,
                    "deadline": deadline,
                    "status": item_status,
                    "ministry": item_ministry,
                    "short_description": short_desc,
                    "response_count": None,
                })

                if len(results) >= max_results:
                    break

            if not found_any:
                break

    return {"results": results[:max_results], "total_found": len(results)}


async def get_høring_details(url: str) -> dict:
    """
    Get full details about a specific høring including the proposal document.

    Fetches the høring page, downloads the høringsnotat PDF, and extracts
    the full text and any explicitly stated consultation questions. Use this
    to understand what was being proposed and what specific questions were
    asked of respondents before analysing the responses.

    Args:
        url: URL to the høring page (e.g. from search_høringer results)
             or the høring_id number (will construct URL)

    Returns:
        dict with: title, ministry, published_date, deadline, status,
        reference, høringsnotat_text (full PDF text), specific_questions
        (list of explicit questions found in the document), attachment_urls
        (dict of PDF links by type), response_count, høringssvar_url
    """
    # If just a number, construct URL
    if str(url).isdigit():
        url = f"{BASE_URL}/no/dokumenter/id{url}/"

    async with httpx.AsyncClient() as client:
        html = await fetch_html(client, url, delay=REQUEST_DELAY)
        if not html:
            return {"error": f"Could not fetch page: {url}"}

        soup = BeautifulSoup(html, "lxml")

        # Title
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

        # Parse metadata from <strong> tags
        metadata = {}
        for strong in soup.find_all("strong"):
            key = strong.get_text(strip=True).rstrip(":").lower()
            parent = strong.parent
            if parent:
                parent_text = parent.get_text(separator=" ", strip=True)
                # Remove the key itself
                value = parent_text.replace(strong.get_text(strip=True), "").strip()
                value = value.lstrip(":").strip()
                if value:
                    metadata[key] = value

        published_date = metadata.get("dato", metadata.get("publisert", ""))
        deadline = metadata.get("høringsfrist", metadata.get("frist", ""))
        ministry = metadata.get("ansvarlig", metadata.get("departement", ""))
        reference = metadata.get("saksnr", metadata.get("saksnummer", metadata.get("journalnr", "")))

        # Status
        status_val = metadata.get("status", "")
        if "på høring" in status_val.lower():
            status = "open"
        elif "ferdigbehandlet" in status_val.lower():
            status = "closed"
        else:
            status = status_val or "unknown"

        # Find all PDF attachment links
        attachment_urls = {}
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/contentassets/" in href and ".pdf" in href.lower():
                link_text = a.get_text(strip=True).lower()
                full_pdf_url = make_absolute_url(href)
                pdf_links.append((link_text, full_pdf_url))

                if "notat" in link_text or "høringsnotat" in link_text:
                    attachment_urls["høringsnotat"] = full_pdf_url
                elif "brev" in link_text or "høringsbrev" in link_text:
                    attachment_urls["høringsbrev"] = full_pdf_url
                elif "instans" in link_text or "liste" in link_text:
                    attachment_urls["høringsinstanser"] = full_pdf_url
                else:
                    attachment_urls[f"vedlegg_{len(attachment_urls)+1}"] = full_pdf_url

        # Find høringsnotat PDF — prefer one with "notat" in link text
        notat_pdf_url = attachment_urls.get("høringsnotat")
        if not notat_pdf_url and pdf_links:
            # Fall back to first PDF
            notat_pdf_url = pdf_links[0][1]

        # Download and extract høringsnotat text
        høringsnotat_text = ""
        if notat_pdf_url:
            print(f"[get_høring_details] Downloading høringsnotat: {notat_pdf_url}")
            pdf_bytes = await fetch_binary(client, notat_pdf_url, delay=REQUEST_DELAY)
            if pdf_bytes:
                høringsnotat_text = extract_pdf_text(pdf_bytes)
                print(f"[get_høring_details] Extracted {len(høringsnotat_text)} chars from PDF")

        # Extract specific questions
        specific_questions = extract_specific_questions(høringsnotat_text) if høringsnotat_text else []

        # Find "Se publiserte høringssvar" link
        høringssvar_url = None
        for a in soup.find_all("a", href=True):
            a_text = a.get_text(strip=True).lower()
            if "publiserte" in a_text and ("høringssvar" in a_text or "svar" in a_text):
                href = a.get("href", "")
                if href:
                    høringssvar_url = make_absolute_url(href)
                    break

        # Try to find response count
        response_count = None
        page_text = soup.get_text()

        # Look for response count patterns
        rc_match = re.search(r'(\d+)\s+(?:publiserte\s+)?høringssvar', page_text, re.IGNORECASE)
        if rc_match:
            response_count = int(rc_match.group(1))

        # If we found a høringssvar URL, try to get count from the response listing
        base_url = get_høring_base_url(url)
        if response_count is None:
            responses_list_url = f"{base_url}?showSvar=true&consterm=&page=1&isFilterOpen=true"
            resp_html = await fetch_html(client, responses_list_url, delay=REQUEST_DELAY)
            if resp_html:
                pag = parse_pagination(resp_html)
                if pag["total"] > 0:
                    response_count = pag["total"]

        return {
            "title": title,
            "ministry": ministry,
            "published_date": published_date,
            "deadline": deadline,
            "status": status,
            "reference": reference,
            "høringsnotat_text": høringsnotat_text,
            "specific_questions": specific_questions,
            "attachment_urls": attachment_urls,
            "response_count": response_count,
            "høringssvar_url": høringssvar_url or f"{base_url}?showSvar=true&consterm=&page=1&isFilterOpen=true",
            "url": url,
        }


async def list_høringssvar(
    url: str,
    respondent_type: str = "alle",
) -> dict:
    """
    List all published responses to a høring — fast, metadata only.

    Use this FIRST to see who has responded before deciding whether to read
    the full texts. Returns respondent names, types, and URLs without
    downloading any response bodies. Typically completes in 2-5 seconds
    regardless of how many responses exist.

    After listing, use get_all_horingssvar to fetch full texts, or
    get_single_horingssvar to read one specific response.

    Args:
        url: URL to the høring page
        respondent_type: Filter by type — "kommune", "fylkeskommune", "stat",
                        "organisasjon", "naringsliv", "alle" (default)

    Returns:
        dict with:
        - "responses": list of metadata objects, each with:
          respondent (str), respondent_type (str), response_url (str),
          format ("html" or "pdf")
        - "total_listed": number of responses returned
        - "total_available": total published responses on regjeringen.no
        - "respondent_type_breakdown": {"kommune": N, "stat": N, ...}
    """
    base_url = get_høring_base_url(url)
    all_entries = []
    total_available = 0

    async with httpx.AsyncClient() as client:
        page1_url = f"{base_url}?showSvar=true&consterm=&page=1&isFilterOpen=true"
        page1_html = await fetch_html(client, page1_url, delay=REQUEST_DELAY)
        if not page1_html:
            return {"responses": [], "total_listed": 0, "total_available": 0, "respondent_type_breakdown": {}}

        pag = parse_pagination(page1_html)
        total_available = pag["total"]
        total_pages = pag["total_pages"]

        all_entries.extend(parse_høringssvar_entries(page1_html, base_url))

        for page_num in range(2, total_pages + 1):
            page_url = f"{base_url}?showSvar=true&consterm=&page={page_num}&isFilterOpen=true"
            page_html = await fetch_html(client, page_url, delay=REQUEST_DELAY)
            if not page_html:
                break
            all_entries.extend(parse_høringssvar_entries(page_html, base_url))

    breakdown = {"kommune": 0, "fylkeskommune": 0, "stat": 0, "organisasjon": 0, "naringsliv": 0}
    responses = []
    for entry in all_entries:
        instans = entry.get("instans", "")
        resp_type = classify_respondent_from_instans(instans) if instans else classify_respondent(entry["name"])
        if respondent_type != "alle" and resp_type != respondent_type:
            continue
        breakdown[resp_type] = breakdown.get(resp_type, 0) + 1
        responses.append({
            "respondent": entry["name"],
            "respondent_type": resp_type,
            "response_url": entry["url"],
            "format": entry["type"],
        })

    return {
        "responses": responses,
        "total_listed": len(responses),
        "total_available": total_available,
        "respondent_type_breakdown": breakdown,
    }


async def get_all_høringssvar(
    url: str,
    respondent_type: str = "alle",
    max_results: int = 200,
    max_chars_per_response: int = 5000,
) -> dict:
    """
    Retrieve full text of all published responses to a høring.

    Use this for synthesis and analysis — it downloads and returns the full
    text of each response. For large høringer (100+ responses), first use
    list_horingssvar to see who responded, then call this tool.

    Fetches responses concurrently (5 at a time) to stay within MCP timeouts.

    For comparative analysis by respondent type, call this twice with different
    respondent_type values: once for "kommune" and once for "naringsliv".

    Args:
        url: URL to the høring page (same URL used in get_horing_details)
        respondent_type: Filter by type — "kommune" (municipalities),
                        "fylkeskommune" (county authorities), "stat"
                        (state agencies/directorates), "organisasjon"
                        (NGOs/organisations), "naringsliv" (businesses),
                        "alle" (all, default)
        max_results: Maximum responses to retrieve, default 200.
        max_chars_per_response: Cap text per response to avoid context overflow.
                               Default 5000 chars. Set to 0 for no limit (full text).

    Returns:
        dict with:
        - "responses": list of response objects, each with:
          respondent (str), respondent_type (str), date (str),
          response_text (str), response_url (str), word_count (int)
        - "total_retrieved": number of responses in the returned list
        - "total_available": total published responses on regjeringen.no
        - "respondent_type_breakdown": {"kommune": N, "stat": N, ...}
    """
    base_url = get_høring_base_url(url)

    # Step 1: Collect all entry metadata (name + URL) from listing pages
    all_entries = []
    total_available = 0

    async with httpx.AsyncClient() as client:
        # Fetch page 1 to get total count
        page1_url = f"{base_url}?showSvar=true&consterm=&page=1&isFilterOpen=true"
        print(f"[get_all_høringssvar] Fetching responses listing: {page1_url}")

        page1_html = await fetch_html(client, page1_url, delay=REQUEST_DELAY)
        if not page1_html:
            return {
                "responses": [],
                "total_retrieved": 0,
                "total_available": 0,
                "respondent_type_breakdown": {},
                "error": "Could not fetch response listing",
            }

        pag = parse_pagination(page1_html)
        total_available = pag["total"]
        total_pages = pag["total_pages"]
        print(f"[get_all_høringssvar] Total available: {total_available}, pages: {total_pages}")

        # Parse page 1 entries
        entries = parse_høringssvar_entries(page1_html, base_url)
        all_entries.extend(entries)
        print(f"[get_all_høringssvar] Page 1: {len(entries)} entries")

        # Fetch additional pages until we have enough entries
        for page_num in range(2, total_pages + 1):
            if len(all_entries) >= max_results:
                break

            page_url = f"{base_url}?showSvar=true&consterm=&page={page_num}&isFilterOpen=true"
            page_html = await fetch_html(client, page_url, delay=REQUEST_DELAY)
            if not page_html:
                break

            page_entries = parse_høringssvar_entries(page_html, base_url)
            all_entries.extend(page_entries)
            print(f"[get_all_høringssvar] Page {page_num}: {len(page_entries)} entries, total collected: {len(all_entries)}")

        # Limit to max_results
        all_entries = all_entries[:max_results]

        # Step 2: Fetch each response — concurrently with a semaphore to stay polite
        # 5 concurrent workers keeps total time ~20s for 50 responses vs 75s+ sequential
        CONCURRENCY = 5
        FETCH_DELAY = 0.3  # Per-worker delay; effective site rate = CONCURRENCY × FETCH_DELAY
        semaphore = asyncio.Semaphore(CONCURRENCY)

        breakdown = {"kommune": 0, "fylkeskommune": 0, "stat": 0, "organisasjon": 0, "naringsliv": 0}
        results_list = [None] * len(all_entries)  # Pre-sized to preserve order

        async def fetch_one(idx: int, entry: dict):
            async with semaphore:
                response_text = ""
                date = ""
                respondent_name = entry["name"]

                if entry["type"] == "html":
                    name, dt, text = await fetch_html_response_text(client, entry["url"])
                    if name:
                        respondent_name = name
                    if dt:
                        date = dt
                    if text:
                        response_text = text
                elif entry["type"] == "pdf":
                    pdf_bytes = await fetch_binary(client, entry["url"], delay=FETCH_DELAY)
                    if pdf_bytes:
                        response_text = extract_pdf_text(pdf_bytes)

                instans = entry.get("instans", "")
                resp_type = classify_respondent_from_instans(instans) if instans else classify_respondent(respondent_name)

                # Apply per-response text cap (0 = no limit)
                if max_chars_per_response and len(response_text) > max_chars_per_response:
                    response_text = response_text[:max_chars_per_response] + "... [truncated]"

                results_list[idx] = {
                    "respondent": respondent_name,
                    "respondent_type": resp_type,
                    "date": date,
                    "response_text": response_text,
                    "response_url": entry["url"],
                    "word_count": len(response_text.split()) if response_text else 0,
                }

        print(f"[get_all_høringssvar] Fetching {len(all_entries)} responses with {CONCURRENCY} concurrent workers...")
        await asyncio.gather(*[fetch_one(i, e) for i, e in enumerate(all_entries)])

        # Apply respondent_type filter and build breakdown
        responses = []
        for item in results_list:
            if item is None:
                continue
            resp_type = item["respondent_type"]
            if respondent_type != "alle" and resp_type != respondent_type:
                continue
            breakdown[resp_type] = breakdown.get(resp_type, 0) + 1
            responses.append(item)

        print(f"[get_all_høringssvar] Done. Retrieved {len(responses)} responses (type filter: {respondent_type})")

        return {
            "responses": responses,
            "total_retrieved": len(responses),
            "total_available": total_available,
            "respondent_type_breakdown": breakdown,
        }


async def get_single_høringssvar(url: str) -> dict:
    """
    Retrieve and read one specific response in full.

    Use this when you need to go deeper on a particular respondent's position,
    read a specific argument in detail, or verify an extract from get_all_høringssvar.

    Args:
        url: URL to an individual høringssvar page. Can be:
             - An inline HTML response URL (contains ?uid=)
             - A PDF response URL (contains .pdf)
             Both formats are handled automatically.

    Returns:
        dict with: respondent (str), date (str), response_text (str, full text),
        word_count (int), source_url (str)
    """
    async with httpx.AsyncClient() as client:
        if ".pdf" in url.lower():
            # PDF response
            pdf_bytes = await fetch_binary(client, url, delay=REQUEST_DELAY)
            if not pdf_bytes:
                return {"error": f"Could not fetch PDF: {url}"}

            text = extract_pdf_text(pdf_bytes)

            # Try to extract respondent name from URL
            filename = url.split("/")[-1].split("?")[0].replace(".pdf", "").replace("-", " ").title()
            respondent = filename

            return {
                "respondent": respondent,
                "date": "",
                "response_text": text,
                "word_count": len(text.split()),
                "source_url": url,
            }

        else:
            # HTML response (with ?uid=)
            respondent, date, text = await fetch_html_response_text(client, url)

            if respondent is None:
                return {"error": f"Could not fetch response: {url}"}

            return {
                "respondent": respondent or "",
                "date": date or "",
                "response_text": text or "",
                "word_count": len(text.split()) if text else 0,
                "source_url": url,
            }


# ─────────────────────────────────────────────
# Stortinget open data API tools
# API base: https://data.stortinget.no/eksport/
# JSON, no auth required, returns Norwegian parliamentary data
# ─────────────────────────────────────────────

STORTINGET_API = "https://data.stortinget.no/eksport"
CURRENT_SESJON = "2024-2025"

# Registry of ALL Stortinget API endpoints.
# Adding a new endpoint = adding one dict entry. No code changes needed.
# "params" = documented parameters, "list_key" = JSON field with result list (None = single object),
# "desc" = one-line description for the stortinget_lookup docstring.
STORTINGET_ENDPOINTS = {
    # Structure & reference
    "stortingsperioder": {"params": [], "list_key": "stortingsperioder_liste", "desc": "All parliamentary periods (e.g. 2021-2025)"},
    "sesjoner": {"params": [], "list_key": "sesjoner_liste", "desc": "All sessions from 1986-87"},
    "emner": {"params": [], "list_key": "emner_liste", "desc": "Topic/subject taxonomy for tagging cases"},
    "partier": {"params": ["sesjonid"], "list_key": "partier_liste", "desc": "Parties represented in a session"},
    "allepartier": {"params": [], "list_key": "partier_liste", "desc": "All parties historically represented"},
    "komiteer": {"params": ["sesjonid"], "list_key": "komiteer_liste", "desc": "Active committees in a session"},
    "allekomiteer": {"params": [], "list_key": "komiteer_liste", "desc": "All committees historically"},
    "valgdistrikter": {"params": [], "list_key": "fylker_liste", "desc": "Electoral districts (fylker)"},
    # People
    "representanter": {"params": ["stortingsperiodeid"], "list_key": "representanter_liste", "desc": "MPs elected for a parliamentary period"},
    "dagensrepresentanter": {"params": [], "list_key": "dagensrepresentanter_liste", "desc": "Current sitting MPs with party and committee"},
    "person": {"params": ["personid"], "list_key": None, "desc": "Detailed info for one person by ID"},
    "regjering": {"params": [], "list_key": "regjeringsmedlemmer_liste", "desc": "Current government cabinet members"},
    # Cases & legislation
    "saker": {"params": ["sesjonid"], "list_key": "saker_liste", "desc": "All parliamentary cases in a session"},
    "sak": {"params": ["sakid"], "list_key": None, "desc": "Detailed info for one case by sak_id"},
    # Voting
    "voteringer": {"params": ["sakid"], "list_key": "sak_votering_liste", "desc": "Voting events for a case (from 2011-2012)"},
    "voteringsforslag": {"params": ["voteringid"], "list_key": "voteringsforslag_liste", "desc": "Proposals put to a specific vote"},
    "voteringsvedtak": {"params": ["voteringid"], "list_key": "voteringsvedtak_liste", "desc": "Formal decisions from a specific vote"},
    "voteringsresultat": {"params": ["voteringid"], "list_key": "voteringsresultat_liste", "desc": "Per-representative result for a vote"},
    "stortingsvedtak": {"params": ["sesjonid"], "list_key": "stortingsvedtak_liste", "desc": "All formal decisions in a session"},
    # Questions
    "sporretimesporsmal": {"params": ["sesjonid"], "list_key": "sporsmal_liste", "desc": "Question Time oral questions"},
    "interpellasjoner": {"params": ["sesjonid"], "list_key": "sporsmal_liste", "desc": "Interpellations (formal debate questions)"},
    "skriftligesporsmal": {"params": ["sesjonid"], "list_key": "sporsmal_liste", "desc": "Written questions to ministers"},
    "enkeltsporsmal": {"params": ["sporsmalid"], "list_key": None, "desc": "Single question with full text and answer"},
    # Hearings
    "horinger": {"params": ["sesjonid"], "list_key": "horinger_liste", "desc": "Committee hearings in a session"},
    "horingsprogram": {"params": ["horingid"], "list_key": None, "desc": "Hearing schedule/program"},
    "horingsinnspill": {"params": ["horingid"], "list_key": "horingsinnspill_liste", "desc": "Written submissions to a hearing"},
    # Meetings
    "moter": {"params": ["sesjonid"], "list_key": "moter_liste", "desc": "Plenary meetings in a session"},
    "dagsorden": {"params": ["moteid"], "list_key": "dagsordensak_liste", "desc": "Agenda items for a meeting"},
    "talerliste": {"params": [], "list_key": None, "desc": "Real-time speaker list from current meeting"},
    # Publications
    "publikasjoner": {"params": ["publikasjontype", "sesjonid"], "list_key": "publikasjoner_liste", "desc": "Publications by type and session (types: referat, innstilling, lovvedtak, dok8, dok12)"},
    "publikasjon": {"params": ["publikasjonid"], "list_key": None, "desc": "Single publication by ID"},
}


def _parse_stortinget_date(ms_date: str) -> str:
    """Convert /Date(1234567890000+0100)/ to YYYY-MM-DD string."""
    if not ms_date:
        return ""
    m = re.search(r'/Date\((-?\d+)', ms_date)
    if not m:
        return ""
    try:
        from datetime import datetime, timezone
        ts = int(m.group(1)) / 1000
        if ts < 0 or ts > 9999999999:
            return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _recursive_parse_dates(obj):
    """Walk a dict/list and convert all /Date()/ strings to YYYY-MM-DD in place."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and "/Date(" in v:
                obj[k] = _parse_stortinget_date(v) or v
            elif isinstance(v, (dict, list)):
                _recursive_parse_dates(v)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str) and "/Date(" in item:
                obj[i] = _parse_stortinget_date(item) or item
            elif isinstance(item, (dict, list)):
                _recursive_parse_dates(item)
    return obj


async def search_stortinget(
    query: str,
    sesjon: str = CURRENT_SESJON,
    max_results: int = 10,
) -> dict:
    """
    Search Stortinget's parliamentary cases (saker) by keyword.

    Use this to find the parliamentary case corresponding to a ministry
    consultation — to see what happened after the høring, whether a law
    was passed, which committee handled it, and whether there were votes.

    This covers the second half of the legislative process: after a ministry
    consultation closes, the proposal goes to Stortinget as a case (sak).

    Args:
        query: Keywords to search in Norwegian, e.g. "kommunelov",
               "markedsføringsloven", "anskaffelser", "Ukraina"
        sesjon: Parliamentary session, e.g. "2024-2025", "2023-2024".
               Default is current session.
        max_results: Max results to return, default 10.

    Returns:
        dict with "saker" list, each containing:
        title, korttittel, id (sak_id), sesjon, type, status,
        committee (komite), url (stortinget.no link)
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{STORTINGET_API}/saker",
            params={"sesjonid": sesjon, "format": "json"},
            timeout=15,
        )
        r.raise_for_status()
        saker = r.json().get("saker_liste", [])

    query_words = [w.lower() for w in query.split() if w]
    matches = []
    for sak in saker:
        title = sak.get("tittel", "")
        kort = sak.get("korttittel", "")
        combined = (title + " " + kort).lower()
        if any(w in combined for w in query_words):
            komite = sak.get("komite", {})
            matches.append({
                "id": sak.get("id"),
                "title": title,
                "korttittel": kort,
                "sesjon": sak.get("behandlet_sesjon_id", sesjon),
                "type": sak.get("type"),
                "status": sak.get("status"),
                "committee": komite.get("navn", "") if isinstance(komite, dict) else "",
                "url": f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak.get('id')}",
            })
        if len(matches) >= max_results:
            break

    return {"saker": matches, "total_found": len(matches), "sesjon": sesjon}


async def get_stortinget_horinger(
    sesjon: str = CURRENT_SESJON,
    komite: Optional[str] = None,
    sak_id: Optional[int] = None,
) -> dict:
    """
    Get Stortinget committee hearings (høringer) for a session or specific case.

    NOTE: These are parliamentary committee hearings — different from the
    ministry consultation rounds on regjeringen.no. These happen AFTER the
    ministry has processed consultation responses and submitted a bill to
    Stortinget. The committee then holds its own hearings before voting.

    Use sak_id to find hearings linked to a specific parliamentary case
    (from search_stortinget). Use komite to filter by committee name.

    Args:
        sesjon: Parliamentary session, e.g. "2024-2025". Default: current.
        komite: Optional committee filter, e.g. "justis", "helse", "finans"
        sak_id: Optional — filter hearings linked to a specific sak

    Returns:
        dict with "horinger" list, each containing:
        id, status, type (skriftlig/muntlig), committee, start_date,
        deadline, linked_saker (list of related sak IDs and titles)
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{STORTINGET_API}/horinger",
            params={"sesjonid": sesjon, "format": "json"},
            timeout=15,
        )
        r.raise_for_status()
        horings = r.json().get("horinger_liste", [])

    results = []
    for h in horings:
        komite_info = h.get("komite", {})
        komite_navn = komite_info.get("navn", "") if isinstance(komite_info, dict) else ""

        # Filter by komite if specified
        if komite and komite.lower() not in komite_navn.lower():
            continue

        # Filter by sak_id if specified
        linked_saker = h.get("horing_sak_info_liste", [])
        if sak_id:
            linked_ids = [s.get("sak_id") for s in linked_saker]
            if sak_id not in linked_ids:
                continue

        results.append({
            "id": h.get("id"),
            "status": h.get("horing_status"),
            "type": "skriftlig" if h.get("skriftlig") else "muntlig",
            "committee": komite_navn,
            "start_date": _parse_stortinget_date(h.get("start_dato", "")),
            "deadline": _parse_stortinget_date(h.get("innspillsfrist", "")),
            "linked_saker": [
                {"sak_id": s.get("sak_id"), "title": s.get("sak_korttittel")}
                for s in linked_saker
            ],
        })

    return {
        "horinger": results,
        "total": len(results),
        "sesjon": sesjon,
        "note": "These are Stortinget committee hearings, not ministry consultations.",
    }


async def get_vote_result(
    sak_id: int,
    sesjon: str = CURRENT_SESJON,
) -> dict:
    """
    Get the Stortinget vote result for a parliamentary case.

    Returns how each party voted on the case — for, against, or absent.
    Use this to see whether a law passed and which parties supported or
    opposed it, completing the picture from ministry consultation to
    final parliamentary decision.

    To find the sak_id, use search_stortinget first.

    Args:
        sak_id: Stortinget case ID (from search_stortinget results)
        sesjon: Parliamentary session, e.g. "2024-2025". Default: current.

    Returns:
        dict with:
        - sak_id, title
        - votes: list of voting events, each with:
          vote_id, for_count, against_count, absent_count, passed (bool),
          party_breakdown: {"Arbeiderpartiet": {"for": N, "mot": N, "ikke_tilstede": N}, ...}
        - overall_result: "passed" or "rejected" (based on first/main vote)
    """
    async with httpx.AsyncClient() as client:
        # Step 1: get voting events for this sak
        r = await client.get(
            f"{STORTINGET_API}/voteringer",
            params={"sakid": sak_id, "format": "json"},
            timeout=15,
        )
        r.raise_for_status()
        votering_liste = r.json().get("sak_votering_liste", [])

        if not votering_liste:
            return {
                "sak_id": sak_id,
                "error": "No vote records found for this case. It may not have been voted on yet.",
            }

        # Step 2: for each vote event, fetch per-representative results
        votes = []
        for v in votering_liste:
            vote_id = v.get("votering_id")  # field is votering_id, not id
            if not vote_id:
                continue

            r2 = await client.get(
                f"{STORTINGET_API}/voteringsresultat",
                params={"voteringId": vote_id, "format": "json"},
                timeout=15,
            )
            if r2.status_code != 200:
                continue

            rep_results = r2.json().get("voteringsresultat_liste", [])

            # Aggregate by party
            party_votes: dict = {}
            for rep in rep_results:
                parti = rep.get("representant", {}).get("parti", {})
                parti_navn = parti.get("navn", "Ukjent") if isinstance(parti, dict) else "Ukjent"
                vote_val = rep.get("votering")  # 1=for, 2=mot, 3=ikke_tilstede

                if parti_navn not in party_votes:
                    party_votes[parti_navn] = {"for": 0, "mot": 0, "ikke_tilstede": 0}
                if vote_val == 1:
                    party_votes[parti_navn]["for"] += 1
                elif vote_val == 2:
                    party_votes[parti_navn]["mot"] += 1
                elif vote_val == 3:
                    party_votes[parti_navn]["ikke_tilstede"] += 1

            total_for = v.get("antall_for", 0)
            total_mot = v.get("antall_mot", 0)
            votes.append({
                "vote_id": vote_id,
                "topic": v.get("votering_tema", ""),
                "for_count": total_for,
                "against_count": total_mot,
                "absent_count": v.get("antall_ikke_tilstede", 0),
                "passed": v.get("vedtatt", total_for > total_mot),
                "party_breakdown": party_votes,
            })

            await asyncio.sleep(0.3)  # polite delay between vote fetches

    overall = "passed" if (votes and votes[0]["passed"]) else "rejected"
    return {
        "sak_id": sak_id,
        "votes": votes,
        "overall_result": overall,
        "total_voting_events": len(votes),
    }


# ─────────────────────────────────────────────
# Generic Stortinget API lookup
# ─────────────────────────────────────────────

def _build_endpoint_table() -> str:
    """Build a formatted table of all endpoints for the docstring."""
    lines = []
    for name, info in STORTINGET_ENDPOINTS.items():
        params = ", ".join(info["params"]) if info["params"] else "(none)"
        lines.append(f"  {name:25s} params: {params:35s} — {info['desc']}")
    return "\n".join(lines)


_ENDPOINT_TABLE = _build_endpoint_table()


async def stortinget_lookup(
    endpoint: str,
    params: Optional[dict] = None,
    max_results: int = 50,
) -> dict:
    f"""
    Direct access to any Stortinget open data API endpoint.

    Use this for any parliamentary data not covered by the purpose-built tools
    (search_stortinget, get_vote_result, get_case_details, etc.). Covers all
    ~30 endpoints: representatives, parties, committees, meetings, agendas,
    publications, decisions, electoral districts, speaker lists, and more.

    Available endpoints and their parameters:

{_ENDPOINT_TABLE}

    Args:
        endpoint: Endpoint name from the table above, e.g. "dagensrepresentanter",
                 "komiteer", "stortingsvedtak", "publikasjoner"
        params: Dict of query parameters, e.g. {{"sesjonid": "2024-2025"}}.
               The "format" param is added automatically. See table above for
               required params per endpoint.
        max_results: Cap on returned items (default 50, 0=no limit). Prevents
                    context overflow on large endpoints like saker or spørsmål.

    Returns:
        dict with "endpoint", "params", "results" (list or single object),
        "total" (full count before cap), "returned" (count after cap)
    """
    if endpoint not in STORTINGET_ENDPOINTS:
        valid = ", ".join(sorted(STORTINGET_ENDPOINTS.keys()))
        return {"error": f"Unknown endpoint '{endpoint}'. Valid endpoints: {valid}"}

    # Detect regjeringen.no IDs passed to horingsinnspill by mistake
    if endpoint == "horingsinnspill" and params:
        hid = params.get("horingid", 0)
        if isinstance(hid, int) and hid > 999999:
            return {
                "error": (
                    f"horingid {hid} looks like a regjeringen.no consultation ID, "
                    "not a Stortinget hearing ID. Stortinget hearing IDs are small numbers (4-6 digits)."
                ),
                "how_to_fix": (
                    "Use find_stortinget_hearings(topic='your topic') to search by topic "
                    "and get submissions in one call — no ID knowledge needed. "
                    "Or use get_stortinget_horinger() to browse hearings and find valid IDs."
                ),
            }

    ep_info = STORTINGET_ENDPOINTS[endpoint]
    merged_params = {"format": "json"}
    if params:
        merged_params.update(params)

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{STORTINGET_API}/{endpoint}",
                params=merged_params,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return {"error": f"API request failed: {e}", "endpoint": endpoint}

    # Extract results using the registry's list_key
    list_key = ep_info["list_key"]
    if list_key is None:
        # Single-object endpoint (sak, person, etc.)
        _recursive_parse_dates(data)
        # Strip metadata fields
        for k in ("respons_dato_tid", "versjon"):
            data.pop(k, None)
        return {"endpoint": endpoint, "params": params, "results": data, "total": 1, "returned": 1}

    results = data.get(list_key, [])
    total = len(results)

    if max_results and total > max_results:
        results = results[:max_results]

    _recursive_parse_dates(results)

    return {
        "endpoint": endpoint,
        "params": params,
        "results": results,
        "total": total,
        "returned": len(results),
    }


# ─────────────────────────────────────────────
# Purpose-built Stortinget tools
# ─────────────────────────────────────────────

async def get_case_details(sak_id: int) -> dict:
    """
    Get comprehensive details about a single Stortinget parliamentary case.

    Returns everything about a case: title, type, status, committee, proposers,
    related documents, decision text, and whether votes exist. Use this after
    search_stortinget to get the full picture of a specific case.

    For vote breakdown by party, follow up with get_vote_result(sak_id).

    Args:
        sak_id: Stortinget case ID (from search_stortinget results)

    Returns:
        dict with: sak_id, title, korttittel, type, status, committee,
        proposers, session, decision_text, documents, has_votes, vote_count, url
    """
    async with httpx.AsyncClient() as client:
        # Fetch detailed case info
        r = await client.get(
            f"{STORTINGET_API}/sak",
            params={"sakid": sak_id, "format": "json"},
            timeout=15,
        )
        r.raise_for_status()
        sak = r.json()

        # Extract key fields
        komite = sak.get("komite", {})
        komite_navn = komite.get("navn", "") if isinstance(komite, dict) else ""

        # Proposers
        forslagstillere = []
        for f in sak.get("forslagstiller_liste", []):
            name = f"{f.get('fornavn', '')} {f.get('etternavn', '')}".strip()
            parti = f.get("parti", {})
            parti_navn = parti.get("navn", "") if isinstance(parti, dict) else ""
            if name:
                forslagstillere.append({"name": name, "party": parti_navn})

        # Documents
        docs = []
        for pub in sak.get("publikasjon_referanse_liste", []):
            docs.append({
                "title": pub.get("tittel", ""),
                "type": pub.get("type", ""),
                "url": pub.get("lenke_url", ""),
            })

        # Check vote count
        r2 = await client.get(
            f"{STORTINGET_API}/voteringer",
            params={"sakid": sak_id, "format": "json"},
            timeout=15,
        )
        vote_list = r2.json().get("sak_votering_liste", []) if r2.status_code == 200 else []

        return {
            "sak_id": sak_id,
            "title": sak.get("tittel", ""),
            "korttittel": sak.get("korttittel", ""),
            "type": sak.get("type", ""),
            "status": sak.get("status", ""),
            "committee": komite_navn,
            "session": sak.get("behandlet_sesjon_id", ""),
            "decision_text": sak.get("kortvedtak", "") or sak.get("vedtakstekst", ""),
            "proposers": forslagstillere,
            "documents": docs,
            "has_votes": len(vote_list) > 0,
            "vote_count": len(vote_list),
            "url": f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak_id}",
        }


async def get_hearing_submissions(
    hearing_id: int,
    max_results: int = 50,
) -> dict:
    """
    Get all written submissions to a Stortinget committee hearing.

    This is the parliamentary equivalent of get_all_horingssvar — it retrieves
    what organisations and individuals submitted to a Stortinget committee
    hearing (not a ministry consultation). Use hearing IDs from
    get_stortinget_horinger or find_stortinget_hearings.

    IMPORTANT: hearing_id must be a Stortinget hearing ID (typically a small
    4-6 digit number), NOT a regjeringen.no consultation ID (which are large
    7-8 digit numbers like 10005622). To find valid hearing IDs, use
    find_stortinget_hearings(topic='...') — it searches and fetches submissions
    in a single call with no ID knowledge required.

    Args:
        hearing_id: Stortinget hearing ID (from get_stortinget_horinger or
                   find_stortinget_hearings results)
        max_results: Max submissions to return, default 50. Set to 0 for all.

    Returns:
        dict with "submissions" list (organization, date, text, id),
        "total", "hearing_id"
    """
    # Detect if a regjeringen.no ID was passed by mistake (they're typically > 1M)
    if hearing_id > 999999:
        return {
            "error": (
                f"hearing_id {hearing_id} looks like a regjeringen.no consultation ID, "
                "not a Stortinget hearing ID. Stortinget hearing IDs are small numbers (4-6 digits). "
            ),
            "hearing_id": hearing_id,
            "how_to_fix": (
                "Option A (recommended): Use find_stortinget_hearings(topic='your topic') — "
                "it searches by topic and fetches submissions in one call. "
                "Option B: Use get_stortinget_horinger() to browse hearings and find valid IDs. "
                "Option C: If you want ministry consultation responses (regjeringen.no), "
                "use get_all_horingssvar with the regjeringen.no URL instead."
            ),
        }

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{STORTINGET_API}/horingsinnspill",
            params={"horingid": hearing_id, "format": "json"},
            timeout=20,
        )
        if r.status_code != 200:
            return {"error": f"Could not fetch submissions for hearing {hearing_id}: HTTP {r.status_code}", "hearing_id": hearing_id}

        data = r.json()
        raw_list = data.get("horingsinnspill_liste", [])

        submissions = []
        for item in raw_list:
            submissions.append({
                "id": item.get("id"),
                "organization": item.get("organisasjon", ""),
                "title": item.get("tittel", ""),
                "date": _parse_stortinget_date(item.get("dato", "")),
                "text": item.get("tekst", ""),
            })

        total = len(submissions)
        if max_results and total > max_results:
            submissions = submissions[:max_results]

        # Also try to get program for context
        program = []
        try:
            r2 = await client.get(
                f"{STORTINGET_API}/horingsprogram",
                params={"horingid": hearing_id, "format": "json"},
                timeout=10,
            )
            if r2.status_code == 200:
                prog_data = r2.json()
                _recursive_parse_dates(prog_data)
                program_items = prog_data.get("horingsdag_liste", [])
                for day in program_items:
                    for innslag in day.get("horing_program_innslag_liste", []):
                        program.append({
                            "time": innslag.get("tidspunkt", ""),
                            "organization": innslag.get("organisasjon", ""),
                            "topic": innslag.get("tema", ""),
                        })
        except Exception:
            pass

        return {
            "hearing_id": hearing_id,
            "submissions": submissions,
            "total": total,
            "returned": len(submissions),
            "program": program if program else None,
        }


async def get_parliamentary_questions(
    sesjon: str = CURRENT_SESJON,
    question_type: str = "alle",
    topic: Optional[str] = None,
    asked_by: Optional[str] = None,
    answered_by: Optional[str] = None,
    max_results: int = 20,
) -> dict:
    """
    Search parliamentary questions across all types: oral Question Time,
    written questions to ministers, and interpellations.

    Use to find what MPs have asked ministers about specific topics, or to
    track ministerial accountability on a subject.

    Args:
        sesjon: Parliamentary session, e.g. "2024-2025". Default: current.
        question_type: "muntlig" (oral question time), "skriftlig" (written),
                      "interpellasjon" (formal debate), "alle" (all types, default)
        topic: Keywords to search in question titles, e.g. "Ukraina", "klima"
        asked_by: Filter by MP name (substring match), e.g. "Listhaug"
        answered_by: Filter by minister name (substring), e.g. "Brenna"
        max_results: Max results, default 20.

    Returns:
        dict with "questions" list (id, type, title, asked_by, answered_by,
        date_asked, date_answered, status), "total_found", "session"
    """
    type_map = {
        "muntlig": ["sporretimesporsmal"],
        "skriftlig": ["skriftligesporsmal"],
        "interpellasjon": ["interpellasjoner"],
        "alle": ["sporretimesporsmal", "interpellasjoner", "skriftligesporsmal"],
    }
    endpoints = type_map.get(question_type, type_map["alle"])

    all_questions = []
    async with httpx.AsyncClient() as client:
        for ep in endpoints:
            try:
                r = await client.get(
                    f"{STORTINGET_API}/{ep}",
                    params={"sesjonid": sesjon, "format": "json"},
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                items = r.json().get("sporsmal_liste", [])

                q_type = {"sporretimesporsmal": "muntlig", "skriftligesporsmal": "skriftlig", "interpellasjoner": "interpellasjon"}.get(ep, ep)

                for q in items:
                    title = q.get("tittel", "")

                    # Topic filter
                    if topic:
                        words = [w.lower() for w in topic.split()]
                        if not any(w in title.lower() for w in words):
                            continue

                    # Extract who asked/answered
                    fra = q.get("sporsmal_fra", {})
                    fra_name = f"{fra.get('fornavn', '')} {fra.get('etternavn', '')}".strip() if isinstance(fra, dict) else ""
                    fra_party = fra.get("parti", {}).get("navn", "") if isinstance(fra, dict) and isinstance(fra.get("parti"), dict) else ""

                    til = q.get("sporsmal_til", {})
                    til_name = f"{til.get('fornavn', '')} {til.get('etternavn', '')}".strip() if isinstance(til, dict) else ""
                    til_dept = til.get("departement", "") if isinstance(til, dict) else ""

                    besvart = q.get("besvart_av", {})
                    besvart_name = f"{besvart.get('fornavn', '')} {besvart.get('etternavn', '')}".strip() if isinstance(besvart, dict) else ""

                    # Name filters
                    if asked_by and asked_by.lower() not in fra_name.lower():
                        continue
                    if answered_by:
                        answerer = besvart_name or til_name
                        if answered_by.lower() not in answerer.lower():
                            continue

                    all_questions.append({
                        "id": q.get("id", ""),
                        "type": q_type,
                        "title": title,
                        "asked_by": {"name": fra_name, "party": fra_party},
                        "answered_by": {"name": besvart_name or til_name, "department": til_dept},
                        "date_asked": _parse_stortinget_date(q.get("datert_dato", "")),
                        "date_answered": _parse_stortinget_date(q.get("besvart_dato", "")),
                        "status": q.get("status", ""),
                    })
            except Exception as e:
                print(f"[get_parliamentary_questions] Error fetching {ep}: {e}")

    # Sort by date descending
    all_questions.sort(key=lambda x: x.get("date_asked", ""), reverse=True)
    total = len(all_questions)
    if max_results and total > max_results:
        all_questions = all_questions[:max_results]

    return {
        "questions": all_questions,
        "total_found": total,
        "returned": len(all_questions),
        "session": sesjon,
    }


async def find_stortinget_hearings(
    topic: str,
    sesjon: str = CURRENT_SESJON,
    include_submissions: bool = True,
    max_hearings: int = 5,
    max_submissions: int = 30,
) -> dict:
    """
    Find Stortinget committee hearings by topic and retrieve their submissions
    in a single call. No hearing IDs needed.

    This is the recommended starting point for any Stortinget hearing query.
    It combines the search + submission fetching that would otherwise require
    knowing hearing IDs and making multiple separate calls.

    NOTE: These are parliamentary COMMITTEE hearings (after a bill reaches
    Stortinget). They are NOT the same as ministry consultation rounds on
    regjeringen.no. For ministry consultations, use search_horinger +
    get_all_horingssvar instead.

    Args:
        topic: Natural language topic in Norwegian or English, e.g. "helse",
               "klima", "kommunelov", "pensjon", "immigration", "skatt".
               Matched against committee names and linked case titles.
        sesjon: Parliamentary session, e.g. "2024-2025", "2023-2024".
               Default: current session.
        include_submissions: Fetch written submissions for each hearing.
               Default True. Set False to get just hearing metadata quickly.
        max_hearings: Max hearings to return. Default 5.
        max_submissions: Max submissions per hearing. Default 30.

    Returns:
        dict with "hearings" list. Each hearing contains:
        - id, committee, type (skriftlig/muntlig), status, start_date, deadline
        - linked_cases: list of related parliamentary cases with sak_id and title
        - submissions (if include_submissions=True): list of:
            organization, title, date, text, id
        - submissions_total: total count before max_submissions cap
    """
    # Step 1: fetch all hearings for the session
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{STORTINGET_API}/horinger",
                params={"sesjonid": sesjon, "format": "json"},
                timeout=15,
            )
            r.raise_for_status()
            all_hearings = r.json().get("horinger_liste", [])
        except Exception as e:
            return {"error": f"Could not fetch hearings: {e}", "topic": topic, "sesjon": sesjon}

    # Step 2: match hearings against topic keywords
    query_words = [w.lower() for w in topic.split() if len(w) > 1]
    matched = []
    for h in all_hearings:
        komite_info = h.get("komite", {})
        komite_navn = komite_info.get("navn", "") if isinstance(komite_info, dict) else ""

        linked_saker = h.get("horing_sak_info_liste", [])
        linked_text = " ".join(
            (s.get("sak_tittel", "") + " " + s.get("sak_korttittel", ""))
            for s in linked_saker
        )
        searchable = (komite_navn + " " + linked_text).lower()

        if any(w in searchable for w in query_words):
            matched.append(h)
        if len(matched) >= max_hearings:
            break

    if not matched:
        return {
            "topic": topic,
            "sesjon": sesjon,
            "hearings": [],
            "total": 0,
            "message": (
                f"No committee hearings found for '{topic}' in session {sesjon}. "
                f"Try a broader keyword or a different session (e.g. '2023-2024'). "
                f"Total hearings in this session: {len(all_hearings)}."
            ),
        }

    # Step 3: optionally fetch submissions for each matched hearing
    results = []
    async with httpx.AsyncClient() as client:
        for h in matched:
            hearing_id = h.get("id")
            komite_info = h.get("komite", {})
            komite_navn = komite_info.get("navn", "") if isinstance(komite_info, dict) else ""
            linked_saker = h.get("horing_sak_info_liste", [])

            entry = {
                "id": hearing_id,
                "committee": komite_navn,
                "type": "skriftlig" if h.get("skriftlig") else "muntlig",
                "status": h.get("horing_status"),
                "start_date": _parse_stortinget_date(h.get("start_dato", "")),
                "deadline": _parse_stortinget_date(h.get("innspillsfrist", "")),
                "linked_cases": [
                    {
                        "sak_id": s.get("sak_id"),
                        "title": s.get("sak_korttittel") or s.get("sak_tittel", ""),
                    }
                    for s in linked_saker
                ],
            }

            if include_submissions and hearing_id:
                try:
                    r = await client.get(
                        f"{STORTINGET_API}/horingsinnspill",
                        params={"horingid": hearing_id, "format": "json"},
                        timeout=20,
                    )
                    if r.status_code == 200:
                        raw = r.json().get("horingsinnspill_liste", [])
                        capped = raw[:max_submissions] if max_submissions else raw
                        entry["submissions"] = [
                            {
                                "id": item.get("id"),
                                "organization": item.get("organisasjon", ""),
                                "title": item.get("tittel", ""),
                                "date": _parse_stortinget_date(item.get("dato", "")),
                                "text": item.get("tekst", ""),
                            }
                            for item in capped
                        ]
                        entry["submissions_total"] = len(raw)
                        entry["submissions_returned"] = len(entry["submissions"])
                    else:
                        entry["submissions"] = []
                        entry["submissions_error"] = f"HTTP {r.status_code}"
                except Exception as e:
                    entry["submissions"] = []
                    entry["submissions_error"] = str(e)

                await asyncio.sleep(0.3)

            results.append(entry)

    return {
        "topic": topic,
        "sesjon": sesjon,
        "hearings": results,
        "total": len(results),
        "note": (
            "These are Stortinget committee hearings. "
            "For ministry consultation responses, use find_horinger_og_svar instead."
        ),
    }


async def find_horinger_og_svar(
    topic: str,
    include_responses: bool = True,
    max_responses: int = 20,
    max_chars_per_response: int = 3000,
    respondent_type: str = "alle",
) -> dict:
    """
    Find ministry consultations (høringer) by topic and retrieve responses in one call.
    No URLs or IDs needed — just describe what you're looking for.

    This is the recommended starting point for any regjeringen.no consultation query.
    It combines search + response fetching that would otherwise require knowing URLs
    and making multiple separate calls.

    NOTE: These are ministry consultation rounds (government proposes regulations and
    asks the public to comment). They are NOT the same as Stortinget committee hearings.
    For parliamentary committee hearings, use find_stortinget_hearings instead.

    Args:
        topic: Natural language topic in Norwegian or English, e.g. "klima",
               "pensjon", "helse", "kommunelov", "immigrasjon", "skatt".
               Matched against høring titles.
        include_responses: Also fetch response texts for the best match. Default True.
        max_responses: Max responses to fetch if include_responses=True. Default 20.
        max_chars_per_response: Character cap per response text. Default 3000.
                                Set to 0 for full text (warning: large).
        respondent_type: Filter responses by type — "kommune", "fylkeskommune",
                         "stat", "organisasjon", "naringsliv", "alle" (default).

    Returns:
        dict with:
        - matches: list of all matching høringer with title, url, ministry, deadline, status
        - total_matches: number of matches found
        - If include_responses=True:
          - best_match: the first/most relevant match with full response data
          - best_match.responses: list of response objects with respondent, text, type
          - best_match.total_available: total published responses on regjeringen.no
          - best_match.respondent_type_breakdown: counts by respondent category
        - note: guidance if multiple matches found
    """
    # Step 1: Search for matching høringer
    search_result = await search_høringer(query=topic, status="all", max_results=10)
    matches = search_result.get("results", [])

    if not matches:
        return {
            "topic": topic,
            "matches": [],
            "total_matches": 0,
            "message": (
                f"No høringer found for '{topic}'. "
                "Try a broader keyword or Norwegian equivalent "
                "(e.g. 'klima' instead of 'climate', 'helse' instead of 'health')."
            ),
        }

    result: dict = {
        "topic": topic,
        "matches": [
            {
                "title": m["title"],
                "url": m["url"],
                "ministry": m.get("ministry", ""),
                "deadline": m.get("deadline"),
                "status": m.get("status"),
            }
            for m in matches
        ],
        "total_matches": len(matches),
    }

    if not include_responses:
        if len(matches) > 1:
            result["note"] = (
                f"Found {len(matches)} matches. Call again with the specific title "
                "or use get_all_horingssvar with the URL of the desired match."
            )
        return result

    # Step 2: Fetch responses for the best (first) match
    best = matches[0]
    responses_result = await get_all_høringssvar(
        url=best["url"],
        respondent_type=respondent_type,
        max_results=max_responses,
        max_chars_per_response=max_chars_per_response,
    )

    result["best_match"] = {
        "title": best["title"],
        "url": best["url"],
        "ministry": best.get("ministry", ""),
        "deadline": best.get("deadline"),
        "status": best.get("status"),
        "responses": responses_result.get("responses", []),
        "total_retrieved": responses_result.get("total_retrieved", 0),
        "total_available": responses_result.get("total_available", 0),
        "respondent_type_breakdown": responses_result.get("respondent_type_breakdown", {}),
    }

    if responses_result.get("error"):
        result["best_match"]["error"] = responses_result["error"]

    if len(matches) > 1:
        result["note"] = (
            f"Responses shown for best match: '{best['title']}'. "
            f"{len(matches) - 1} other match(es) found — see 'matches' list. "
            "To get responses for a different match, call get_all_horingssvar "
            "with its URL, or call find_horinger_og_svar with a more specific topic."
        )

    return result
