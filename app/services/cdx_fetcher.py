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
    Each unique digest = a different version of the homepage.
    """
    headers = {"User-Agent": CDX_USER_AGENT}
    all_snapshots = []

    async with httpx.AsyncClient(timeout=300.0) as client:
        # Fetch homepage snapshots with collapse=digest for unique content
        params = {
            "url": domain,
            "output": "json",
            "fl": "urlkey,timestamp,original,mimetype,statuscode,digest,length",
            "filter": "statuscode:200",
            "collapse": "digest",
            "limit": CDX_PAGE_LIMIT,
        }

        if progress_callback:
            progress_callback(
                pages_so_far=0,
                message=f"Fetching unique homepage snapshots for {domain}...",
            )

        try:
            logger.info(f"Fetching homepage snapshots for {domain}...")
            resp = await client.get(CDX_BASE_URL, params=params, headers=headers)
            resp.raise_for_status()

            text = resp.text.strip()
            if not text:
                logger.info(f"No CDX results for {domain}")
                return []

            try:
                data = resp.json()
            except Exception:
                logger.warning(f"CDX returned non-JSON for {domain}: {text[:200]}")
                return []

            if not data or len(data) < 2:
                logger.info(f"No snapshots found for {domain}")
                return []

            field_names = data[0]
            for row in data[1:]:
                record = dict(zip(field_names, row))
                record["wayback_url"] = build_wayback_url(
                    record["timestamp"], record["original"]
                )
                all_snapshots.append(record)

            if progress_callback:
                progress_callback(
                    pages_so_far=len(all_snapshots),
                    message=f"Found {len(all_snapshots)} unique homepage snapshots",
                )

            logger.info(f"Domain {domain}: found {len(all_snapshots)} unique homepage snapshots")

        except httpx.TimeoutException:
            raise Exception(f"CDX API timed out for {domain}. Try again later.")
        except httpx.HTTPStatusError as e:
            logger.error(f"CDX API HTTP {e.response.status_code} for {domain}")
            if e.response.status_code == 429:
                raise Exception("Rate limited by CDX API. Wait a few minutes.")
            raise Exception(f"CDX API returned HTTP {e.response.status_code}")
        except Exception as e:
            logger.error(f"CDX API error for {domain}: {type(e).__name__}: {e}")
            raise

    return all_snapshots
