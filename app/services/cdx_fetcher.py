import logging
import httpx
from app.config import CDX_BASE_URL, CDX_USER_AGENT, CDX_PAGE_LIMIT

logger = logging.getLogger("wayvault.cdx")


def build_wayback_url(timestamp: str, original_url: str) -> str:
    return f"https://web.archive.org/web/{timestamp}/{original_url}"


async def check_domain_status(domain: str) -> dict:
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
            http2=False,
        ) as client:
            resp = await client.get(f"https://{domain}", headers=headers)
            result["status_code"] = resp.status_code
            result["final_url"] = str(resp.url)

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


async def _cdx_query(url: str, headers: dict) -> list:
    """
    Run a single CDX API query for a given URL and return the list of snapshot records.
    Used internally — callers should use fetch_homepage_snapshots instead.
    """
    params = {
        "url": url,
        "output": "json",
        "fl": "urlkey,timestamp,original,mimetype,statuscode,digest,length",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": CDX_PAGE_LIMIT,
    }

    async with httpx.AsyncClient(
        timeout=300.0,
        http2=False,
        follow_redirects=True,
        verify=False,
    ) as client:
        try:
            resp = await client.get(CDX_BASE_URL, params=params, headers=headers)
            resp.raise_for_status()

            raw = resp.text.strip()
            if not raw:
                logger.info(f"No CDX results for {url}")
                return []

            try:
                data = resp.json()
            except Exception:
                logger.warning(f"CDX non-JSON for {url}: {raw[:200]}")
                return []

            if not data or len(data) < 2:
                logger.info(f"No snapshots found for {url}")
                return []

            field_names = data[0]
            snapshots = []
            for row in data[1:]:
                record = dict(zip(field_names, row))
                ts = record.get("timestamp")
                orig = record.get("original")
                if ts and orig:
                    record["wayback_url"] = build_wayback_url(ts, orig)
                    snapshots.append(record)

            return snapshots

        except httpx.TimeoutException:
            raise Exception(f"CDX API timed out for {url}. Try again later.")
        except httpx.HTTPStatusError as e:
            logger.error(f"CDX API HTTP {e.response.status_code} for {url}")
            if e.response.status_code == 429:
                raise Exception("Rate limited by CDX API. Wait a few minutes.")
            raise Exception(f"CDX API returned HTTP {e.response.status_code}")
        except Exception as e:
            logger.error(f"CDX API error for {url}: {type(e).__name__}: {e}")
            raise


async def fetch_homepage_snapshots(domain: str, progress_callback=None) -> list:
    """
    Fetch all unique homepage snapshots from the Wayback CDX API.
    Tries the bare domain first; if that returns nothing, falls back to www.{domain}.
    """
    headers = {"User-Agent": CDX_USER_AGENT}

    if progress_callback:
        progress_callback(pages_so_far=0, message=f"Fetching Wayback snapshots for {domain}...")

    logger.info(f"Fetching homepage snapshots for {domain}...")

    snapshots = await _cdx_query(domain, headers)

    # Fallback: try www. prefix if no results and domain doesn't already have www.
    if not snapshots and not domain.startswith("www."):
        www_domain = f"www.{domain}"
        logger.info(f"No results for {domain}, trying {www_domain}...")
        snapshots = await _cdx_query(www_domain, headers)
        if snapshots:
            logger.info(f"Found {len(snapshots)} snapshots under {www_domain}")

    if progress_callback:
        progress_callback(
            pages_so_far=len(snapshots),
            message=f"Found {len(snapshots)} unique homepage snapshots",
        )

    logger.info(f"Domain {domain}: found {len(snapshots)} unique homepage snapshots")
    return snapshots
