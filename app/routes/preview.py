from fastapi import APIRouter, Query

router = APIRouter(prefix="/api", tags=["preview"])


@router.get("/preview")
async def get_preview_url(url: str = Query(...), timestamp: str = Query(...)):
    """
    Returns the Wayback iframe-friendly URL (with id_ to disable the toolbar).
    Frontend uses this to embed previews.
    """
    preview_url = f"https://web.archive.org/web/{timestamp}id_/{url}"
    return {"preview_url": preview_url, "original_url": url, "timestamp": timestamp}
