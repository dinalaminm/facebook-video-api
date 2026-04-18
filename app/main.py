from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import logging
import sys
import asyncio
import aiohttp
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from app.config import settings
from app.models import (
    VideoDownloadRequest,
    VideoDownloadResponse,
    ErrorResponse,
    VideoQuality
)
from app.services.video_service import video_service
from app.utils.rate_limiter import check_rate_limit

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Facebook Video Downloader API starting up...")
    logger.info(f"Debug mode: {settings.DEBUG}")
    logger.info(
        f"Rate limiting: {settings.RATE_LIMIT_REQUESTS} req / {settings.RATE_LIMIT_WINDOW}s"
    )
    # Check ffmpeg availability at startup
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-version',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("✅ ffmpeg is available — DASH merge enabled")
    except FileNotFoundError:
        logger.warning(
            "⚠️  ffmpeg NOT found — DASH videos will stream video-only (no audio). "
            "Install ffmpeg to enable audio merging."
        )
    yield
    logger.info("📱 Facebook Video Downloader API shutting down...")

# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    description=settings.API_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────────────────────
# Global exception handler
# ──────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": "Internal server error occurred",
            "error_code": "INTERNAL_ERROR",
        },
    )

# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Serve the main HTML page"""
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": settings.API_VERSION,
        "service": "Facebook Video Downloader API",
    }


@app.post("/download", response_model=VideoDownloadResponse)
async def download_video(
    request: VideoDownloadRequest,
    _: None = Depends(check_rate_limit),
):
    """
    Download Facebook video — returns direct URL or /stream/ URL for DASH videos.

    - **url**: Facebook video URL (required)
    - **quality**: Preferred quality (optional, default: best)
    """
    try:
        logger.info(f"Processing download request: {request.url}")

        result = await video_service.get_video_info(str(request.url), request.quality)

        response = VideoDownloadResponse(
            status="success",
            video_info=result['video_info'],
            download_url=result['download_url'],
            available_formats=result['available_formats'],
        )

        logger.info(f"Successfully processed: {result['video_info'].title}")
        return response

    except ValueError as e:
        logger.warning(f"Invalid request: {e}")
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": str(e), "error_code": "INVALID_REQUEST"},
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "message": "Failed to process video", "error_code": "PROCESSING_ERROR"},
        )


# ──────────────────────────────────────────────────────────────────────────────
# Stream endpoint — handles both simple proxy and ffmpeg DASH merge
# ──────────────────────────────────────────────────────────────────────────────

def _is_safe_url(url: str) -> bool:
    """Basic URL safety check"""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in ('http', 'https') and parsed.netloc)
    except Exception:
        return False


async def _simple_proxy(url: str):
    """Stream a single URL through (progressive MP4 or video-only fallback)"""
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                )
            },
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=resp.status, detail="Failed to fetch video")
            async for chunk in resp.content.iter_chunked(8192):
                yield chunk


async def _download_to_temp(url: str, suffix: str) -> str:
    """
    Facebook CDN URL থেকে temp file এ download করো।
    ffmpeg direct URL এ ভালো কাজ করে না (403/redirect), তাই আগে download করি।
    """
    import tempfile
    import os

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.facebook.com/',
        'Origin': 'https://www.facebook.com',
    }

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise ValueError(f"Failed to download ({resp.status}): {url[:80]}")
                async for chunk in resp.content.iter_chunked(65536):
                    tmp.write(chunk)
        tmp.close()
        logger.info(f"Downloaded temp file: {tmp.name} ({os.path.getsize(tmp.name)} bytes)")
        return tmp.name
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


async def _ffmpeg_merge(video_url: str, audio_url: str):
    """
    DASH video + audio আলাদাভাবে temp file এ download করে
    ffmpeg দিয়ে merge করে stream করো।

    Strategy:
    1. video + audio আলাদা temp file এ download
    2. ffmpeg দিয়ে merge → stdout pipe
    3. chunk করে client এ stream
    4. temp files cleanup
    """
    import os

    video_tmp = None
    audio_tmp = None
    proc = None

    try:
        # Step 1: Download both streams to temp files
        logger.info("Downloading video stream to temp file...")
        video_tmp = await _download_to_temp(video_url, '.mp4')

        logger.info("Downloading audio stream to temp file...")
        audio_tmp = await _download_to_temp(audio_url, '.m4a')

        # Step 2: ffmpeg merge from temp files
        cmd = [
            'ffmpeg',
            '-loglevel', 'warning',
            '-i', video_tmp,
            '-i', audio_tmp,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-shortest',
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov+faststart',
            'pipe:1',
        ]

        logger.info("Starting ffmpeg merge from temp files")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Step 3: Stream output chunks
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk

        await proc.wait()

        stderr_out = await proc.stderr.read()
        if proc.returncode != 0:
            logger.error(f"ffmpeg error (code {proc.returncode}): {stderr_out.decode()}")
        else:
            logger.info("ffmpeg merge completed successfully")
            if stderr_out:
                logger.debug(f"ffmpeg warnings: {stderr_out.decode()}")

    except Exception as e:
        logger.error(f"ffmpeg merge error: {e}")
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        raise

    finally:
        # Step 4: Cleanup temp files
        for tmp_path in (video_tmp, audio_tmp):
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                    logger.debug(f"Cleaned up temp file: {tmp_path}")
                except Exception:
                    pass


@app.get("/stream/{video_id}")
async def stream_video(video_id: str, url: str, audio_url: str = None):
    """
    Stream video through the server.

    - If only **url** is provided → simple proxy (progressive MP4)
    - If **audio_url** is also provided → ffmpeg merges DASH video+audio on the fly
    """
    # Validate URLs
    if not _is_safe_url(url):
        raise HTTPException(status_code=400, detail="Invalid video URL")
    if audio_url and not _is_safe_url(audio_url):
        raise HTTPException(status_code=400, detail="Invalid audio URL")

    filename = f"{video_id}.mp4"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "video/mp4",
        "Cache-Control": "no-cache",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }

    try:
        if audio_url:
            # DASH: merge with ffmpeg
            logger.info(f"DASH merge stream: video_id={video_id}")
            return StreamingResponse(
                _ffmpeg_merge(url, audio_url),
                media_type="video/mp4",
                headers=headers,
            )
        else:
            # Progressive: simple proxy
            logger.info(f"Simple proxy stream: video_id={video_id}")
            return StreamingResponse(
                _simple_proxy(url),
                media_type="video/mp4",
                headers=headers,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stream error: {e}")
        raise HTTPException(status_code=500, detail="Failed to stream video")


# ──────────────────────────────────────────────────────────────────────────────
# Info-only endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/info", response_model=VideoDownloadResponse)
async def get_video_info(
    request: VideoDownloadRequest,
    _: None = Depends(check_rate_limit),
):
    """Get Facebook video metadata without a download URL"""
    try:
        logger.info(f"Processing info request: {request.url}")

        result = await video_service.get_video_info(str(request.url), request.quality)

        response = VideoDownloadResponse(
            status="success",
            video_info=result['video_info'],
            available_formats=result['available_formats'],
        )

        logger.info(f"Info retrieved: {result['video_info'].title}")
        return response

    except ValueError as e:
        logger.warning(f"Invalid request: {e}")
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": str(e), "error_code": "INVALID_REQUEST"},
        )


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/qualities")
async def get_supported_qualities():
    """Get list of supported video qualities"""
    return {
        "status": "success",
        "qualities": [q.value for q in VideoQuality],
        "descriptions": {
            "best":  "Best available quality",
            "worst": "Worst available quality",
            "360p":  "360p resolution",
            "720p":  "720p resolution",
            "1080p": "1080p resolution",
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Dev runner
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
