"""
Tools for processing Norwegian government consultation responses (høringssvar)
from regjeringen.no.

All tools access regjeringen.no which is fully server-side rendered.
No authentication required for public consultation data.
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
