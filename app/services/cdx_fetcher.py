import asyncio
import logging
import httpx
from app.config import CDX_USER_AGENT, CDX_PAGE_LIMIT

logger = logging.getLogger("wayvault.cdx")


def build_wayback_url(timestamp: str, original_url: str) -> str:
    return f"https://web.archive.org/web/{timestamp}/{original_url}"


async def check_domain_status(domain: str) -> dict:
    """
    Check the live status of a domain's homepage.
    Returns status info: status_code, redirect chain, final_url, availability.
    """
    headers = {"User-Agent": CDX_USER_AGENT}
    result = {
        "live_status": "unknown",
        "status_code": None,
        "final_url": None,
        "redirect_chain": [],
        "error": None,
    }

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            max_redirects=10,
        ) as client:
            resp = await client.get(f"https://{domain}", headers=headers)
            result["status_code"] = resp.status_code
            result["final_url"] = str(resp.url)

            # Build redirect chain
            if resp.history:
                result["redirect_chain"] = [
                    {"url": str(r.url), "status": r.status_code}
                    for r in resp.history
                ]

            if resp.status_code == 200:
                result["live_status"] = "ok"
            elif 300 <= resp.status_code < 400:
                result["live_status"] = "redirect"
            elif resp.status_code == 403:
                result["live_status"] = "forbidden"
            elif resp.status_code == 404:
                result["live_status"] = "not_found"
            elif resp.status_code >= 500:
                result["live_status"] = "server_error"
            else:
                result["live_status"] = "other"

            # Check if domain redirected to a different domain
            if resp.history and result["final_url"]:
                from urllib.parse import urlparse
                final_domain = urlparse(result["final_url"]).netloc.replace("www.", "")
                if final_domain != domain and final_domain != f"www.{domain}":
                    result["live_status"] = "redirected_away"

    except httpx.ConnectError:
        result["live_status"] = "unreachable"
        result["error"] = "Could not connect to domain"
    except httpx.TimeoutException:
        result["live_status"] = "timeout"
        result["error"] = "Connection timed out"
    except Exception as e:
        result["live_status"] = "error"
        result["error"] = str(e)

    logger.info(f"Live status for {domain}: {result['live_status']} (HTTP {result['status_code']})")
    return result


async def _try_cdx_request(url: str, params: dict, headers: dict):
    """
    Single CDX request attempt. Returns parsed snapshot list on success,
    None on connection/timeout failure, raises on HTTP errors.
    """
    timeout = httpx.Timeout(connect=20.0, read=300.0, write=10.0, pool=5.0)

    async with httpx.AsyncClient(
        timeout=timeout,
        http2=False,
        follow_redirects=True,
        verify=False,  # skip SSL verify — avoids cert chain issues on some VPS
    ) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        raw = resp.text.strip()

    if not raw:
        return []

    try:
        data = resp.json()
    except Exception:
        logger.warning(f"CDX non-JSON response: {raw[:200]}")
        return []

    if not data or len(data) < 2:
        return []

    field_names = data[0]
    snapshots = []
    for row in data[1:]:
        record = dict(zip(field_names, row))
        record["wayback_url"] = build_wayback_url(record["timestamp"], record["original"])
        snapshots.append(record)
    return snapshots


async def fetch_homepage_snapshots(domain: str, progress_callback=None) -> list:
    """
    Fetch all unique homepage snapshots from the Wayback CDX API.
    Tries http:// first (avoids SSL issues on VPS), falls back to https://.
    Retries each URL up to 2 times before moving to the next.
    """
    params = {
        "url": domain,
        "output": "json",
        "fl": "urlkey,timestamp,original,mimetype,statuscode,digest,length",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": CDX_PAGE_LIMIT,
    }
    headers = {"User-Agent": CDX_USER_AGENT}

    # Try HTTP first (bypasses SSL issues), then HTTPS as fallback
    cdx_urls = [
        "http://web.archive.org/cdx/search/cdx",
        "https://web.archive.org/cdx/search/cdx",
    ]

    last_error = None

    for url_idx, url in enumerate(cdx_urls):
        for attempt in range(1, 3):  # 2 attempts per URL
            attempt_label = f"url {url_idx+1}/{len(cdx_urls)}, attempt {attempt}/2"
            if progress_callback:
                progress_callback(
                    pages_so_far=0,
                    message=f"Fetching Wayback snapshots for {domain}... ({attempt_label})",
                )
            logger.info(f"CDX fetch: {domain} via {url} ({attempt_label})")

            try:
                snapshots = await _try_cdx_request(url, params, headers)
                if snapshots is not None:
                    logger.info(f"CDX success: {domain} got {len(snapshots)} snapshots via {url}")
                    if progress_callback:
                        progress_callback(
                            pages_so_far=len(snapshots),
                            message=f"Found {len(snapshots)} unique homepage snapshots",
                        )
                    return snapshots

            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                logger.error(f"CDX HTTP {code} for {domain} via {url}")
                if code == 429:
                    raise Exception("Rate limited by Wayback CDX API. Wait a few minutes.")
                raise Exception(f"CDX API returned HTTP {code}")

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning(f"CDX {type(e).__name__} for {domain} via {url} ({attempt_label}): {e}")
                if attempt < 2:
                    wait = 5
                    logger.info(f"Retrying in {wait}s...")
                    if progress_callback:
                        progress_callback(
                            pages_so_far=0,
                            message=f"Connection failed, retrying in {wait}s... ({attempt_label})",
                        )
                    await asyncio.sleep(wait)

            except Exception as e:
                logger.error(f"CDX unexpected error for {domain}: {type(e).__name__}: {e}")
                raise

    raise Exception(
        f"Wayback CDX API unreachable for {domain}. "
        f"Tried {len(cdx_urls)} URLs × 2 attempts. Last error: {last_error}"
    )
