import asyncio
import logging
import httpx
from app.config import CDX_BASE_URL, CDX_RATE_LIMIT_SECONDS, CDX_USER_AGENT, CDX_PAGE_LIMIT

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


async def fetch_homepage_snapshots(domain: str, progress_callback=None) -> list:
    """
    Fetch all unique snapshots of the domain HOMEPAGE only from Wayback CDX API.
    Uses collapse=digest to get only snapshots with different content.
    Retries up to 3 times with backoff on connection errors.
    """
    headers = {"User-Agent": CDX_USER_AGENT}
    params = {
        "url": domain,
        "output": "json",
        "fl": "urlkey,timestamp,original,mimetype,statuscode,digest,length",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": CDX_PAGE_LIMIT,
    }

    # Separate connect vs read timeout: fail fast on connect (30s),
    # allow long read (5 min) for large result sets.
    # http2=False forces HTTP/1.1 — avoids connectivity issues on some VPS
    # setups where HTTP/2 is blocked or causes ConnectError.
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=10.0)
    max_retries = 3
    last_error = None

    for attempt in range(1, max_retries + 1):
        if progress_callback:
            msg = f"Fetching Wayback snapshots for {domain}..." if attempt == 1 \
                  else f"Retry {attempt-1}/{max_retries-1}: fetching snapshots for {domain}..."
            progress_callback(pages_so_far=0, message=msg)

        try:
            logger.info(f"CDX request for {domain} (attempt {attempt}/{max_retries})")
            async with httpx.AsyncClient(
                timeout=timeout,
                http2=False,         # HTTP/1.1 only — more compatible with VPS firewalls
                follow_redirects=True,
            ) as client:
                resp = await client.get(CDX_BASE_URL, params=params, headers=headers)
                resp.raise_for_status()

            text = resp.text.strip()
            if not text:
                logger.info(f"No CDX results for {domain}")
                return []

            try:
                data = resp.json()
            except Exception:
                logger.warning(f"CDX non-JSON for {domain}: {text[:200]}")
                return []

            if not data or len(data) < 2:
                logger.info(f"No snapshots found for {domain}")
                return []

            all_snapshots = []
            field_names = data[0]
            for row in data[1:]:
                record = dict(zip(field_names, row))
                record["wayback_url"] = build_wayback_url(record["timestamp"], record["original"])
                all_snapshots.append(record)

            if progress_callback:
                progress_callback(
                    pages_so_far=len(all_snapshots),
                    message=f"Found {len(all_snapshots)} unique homepage snapshots",
                )

            logger.info(f"Domain {domain}: {len(all_snapshots)} unique snapshots (attempt {attempt})")
            return all_snapshots

        except httpx.ConnectError as e:
            last_error = e
            logger.warning(f"CDX connect error for {domain} (attempt {attempt}): {e}")
            if attempt < max_retries:
                wait = attempt * 10  # 10s, 20s between retries
                logger.info(f"Waiting {wait}s before retry...")
                if progress_callback:
                    progress_callback(pages_so_far=0, message=f"Connection failed, retrying in {wait}s... (attempt {attempt}/{max_retries})")
                await asyncio.sleep(wait)

        except httpx.TimeoutException as e:
            last_error = e
            logger.warning(f"CDX timeout for {domain} (attempt {attempt}): {e}")
            if attempt < max_retries:
                wait = attempt * 15
                logger.info(f"Waiting {wait}s before retry...")
                if progress_callback:
                    progress_callback(pages_so_far=0, message=f"Request timed out, retrying in {wait}s... (attempt {attempt}/{max_retries})")
                await asyncio.sleep(wait)

        except httpx.HTTPStatusError as e:
            logger.error(f"CDX HTTP {e.response.status_code} for {domain}")
            if e.response.status_code == 429:
                raise Exception("Rate limited by Wayback CDX API. Wait a few minutes and try again.")
            raise Exception(f"CDX API returned HTTP {e.response.status_code}")

        except Exception as e:
            logger.error(f"CDX error for {domain}: {type(e).__name__}: {e}")
            raise

    raise Exception(f"CDX API unreachable after {max_retries} attempts. Last error: {last_error}")
