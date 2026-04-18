import yt_dlp
import asyncio
import logging
from typing import Dict, List, Optional, Any
from app.models import VideoInfo, VideoFormat, VideoQuality
from app.utils.validators import URLValidator
from app.config import settings

logger = logging.getLogger(__name__)


class VideoDownloadService:
    """Service for downloading Facebook videos using yt-dlp"""

    def __init__(self):
        self.ydl_opts = {
            'quiet': True,
            'no_warnings': False,
            'extractaudio': False,
            'outtmpl': '/tmp/%(title)s.%(ext)s',
            'retries': 5,
            'fragment_retries': 5,
            'ignoreerrors': False,
            'no_check_certificate': True,
            'cookiefile': None,
            'extract_flat': False,
            'user_agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def get_video_info(
        self, url: str, quality: VideoQuality = VideoQuality.BEST
    ) -> Dict[str, Any]:
        """Extract video information and download URLs"""

        if not URLValidator.is_valid_facebook_url(url):
            raise ValueError("Invalid Facebook URL provided")

        normalized_url = URLValidator.normalize_url(url)

        if 'fb.watch' in normalized_url:
            normalized_url = await self._resolve_fb_watch_url(normalized_url)

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                self._extract_info,
                normalized_url,
                quality,
            )
            return result

        except Exception as e:
            logger.error(f"Error extracting video info: {str(e)}")
            raise ValueError(f"Failed to extract video information: {str(e)}")

    # ──────────────────────────────────────────────────────────────────────────
    # fb.watch resolver
    # ──────────────────────────────────────────────────────────────────────────

    async def _resolve_fb_watch_url(self, url: str) -> str:
        """Resolve fb.watch short URLs to full Facebook URLs"""
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                ),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }

            async with aiohttp.ClientSession(timeout=timeout) as session:
                current_url = url
                for _ in range(5):
                    try:
                        async with session.get(
                            current_url,
                            allow_redirects=False,
                            headers=headers,
                        ) as response:
                            if response.status in (301, 302, 303, 307, 308):
                                location = response.headers.get('Location', '')
                                if not location:
                                    break
                                if location.startswith('/'):
                                    from urllib.parse import urljoin
                                    location = urljoin(current_url, location)
                                if 'facebook.com' in location:
                                    logger.info(f"Resolved fb.watch: {url} → {location}")
                                    return location
                                current_url = location
                            else:
                                if 'facebook.com' in current_url:
                                    return current_url
                                break
                    except Exception as e:
                        logger.warning(f"Redirect error: {e}")
                        break

            logger.warning(f"Could not resolve fb.watch URL: {url}")
            return url

        except Exception as e:
            logger.warning(f"fb.watch resolve failed: {e}, using original")
            return url

    # ──────────────────────────────────────────────────────────────────────────
    # Core extraction (runs in thread-pool executor)
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_info(self, url: str, quality: VideoQuality) -> Dict[str, Any]:
        """Extract video information using yt-dlp"""

        opts = self.ydl_opts.copy()

        # Quality-specific format selection
        # bestvideo+bestaudio → yt-dlp merges locally before returning info
        quality_map = {
            VideoQuality.BEST:  'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            VideoQuality.WORST: 'worstvideo[ext=mp4]+worstaudio[ext=m4a]/worstvideo+worstaudio/worst[ext=mp4]/worst',
            VideoQuality.P360:  'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360][ext=mp4]/best[height<=360]',
            VideoQuality.P720:  'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720][ext=mp4]/best[height<=720]',
            VideoQuality.P1080: 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080][ext=mp4]/best[height<=1080]',
        }
        opts['format'] = quality_map.get(quality, quality_map[VideoQuality.BEST])

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("No video information found")
                return self._process_video_info(info)

        except yt_dlp.DownloadError as e:
            msg = str(e).lower()
            if 'redirect' in msg:
                raise ValueError(
                    "fb.watch URL couldn't be processed. Please use the full facebook.com URL."
                    if 'fb.watch' in url
                    else "Video URL has redirect issues. Try the direct Facebook video URL."
                )
            elif 'private' in msg or 'not available' in msg:
                raise ValueError("This video is private or not available for download.")
            elif 'age' in msg:
                raise ValueError("This video has age restrictions and cannot be downloaded.")
            else:
                raise ValueError(f"Could not extract video: {e}")
        except Exception as e:
            msg = str(e)
            if '302' in msg or 'redirect' in msg.lower():
                raise ValueError("URL redirect issue. Please use the direct Facebook video URL.")
            raise ValueError(f"Unexpected error: {msg}")

    # ──────────────────────────────────────────────────────────────────────────
    # Video info processing
    # ──────────────────────────────────────────────────────────────────────────

    def _process_video_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """Process and structure video information"""

        video_info = VideoInfo(
            title=info.get('title', 'Unknown Title'),
            duration=info.get('duration'),
            thumbnail=info.get('thumbnail'),
            uploader=info.get('uploader'),
            view_count=info.get('view_count'),
            upload_date=info.get('upload_date'),
        )

        # Try to get a progressive (audio+video) URL directly
        progressive_url = self._get_progressive_url(info)

        if progressive_url:
            # ✅ Progressive MP4 — direct download link দেওয়া যাবে
            logger.info("Progressive MP4 found — using direct URL")
            download_url = progressive_url
            is_dash = False
        else:
            # ❌ DASH stream — server-side ffmpeg merge দরকার
            logger.warning("DASH stream detected — will use /stream/ endpoint for merge")
            download_url = self._build_stream_url(info)
            is_dash = True

        available_formats = self._get_available_formats(info, is_dash)

        return {
            'video_info': video_info,
            'download_url': download_url,
            'available_formats': available_formats,
            'is_dash': is_dash,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # URL helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_progressive_url(self, info: Dict[str, Any]) -> Optional[str]:
        """
        Return URL of the best progressive MP4 (audio + video in one stream).
        Returns None if only DASH streams are available.
        """
        formats = info.get('formats', [])

        progressive = [
            f for f in formats
            if f.get('url')
            and f.get('acodec') not in (None, 'none')
            and f.get('vcodec') not in (None, 'none')
        ]

        if not progressive:
            # Also check top-level info dict (some extractors put it there)
            direct_url = info.get('url')
            direct_acodec = info.get('acodec', 'none')
            if direct_url and direct_acodec not in (None, 'none'):
                logger.info("Using top-level URL with audio")
                return direct_url
            return None

        # Best quality first
        progressive.sort(
            key=lambda f: (f.get('height') or 0, f.get('filesize') or 0),
            reverse=True,
        )
        best = progressive[0]
        logger.info(
            f"Progressive format: {best.get('format_id')} "
            f"height={best.get('height')} acodec={best.get('acodec')}"
        )
        return best['url']

    def _get_dash_urls(self, info: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """
        Extract separate video-only and audio-only URLs from DASH stream info.
        Returns {'video': url, 'audio': url}
        """
        requested = info.get('requested_formats', [])
        formats = info.get('formats', [])

        # Prefer requested_formats (yt-dlp puts the selected pair here)
        if requested:
            video_fmts = sorted(
                [f for f in requested if f.get('vcodec') not in (None, 'none')],
                key=lambda f: f.get('height') or 0,
                reverse=True,
            )
            audio_fmts = [
                f for f in requested
                if f.get('acodec') not in (None, 'none')
                and f.get('vcodec') in (None, 'none')
            ]
        else:
            # Fallback: search all formats
            video_fmts = sorted(
                [
                    f for f in formats
                    if f.get('url')
                    and f.get('vcodec') not in (None, 'none')
                    and f.get('acodec') in (None, 'none')
                ],
                key=lambda f: f.get('height') or 0,
                reverse=True,
            )
            audio_fmts = sorted(
                [
                    f for f in formats
                    if f.get('url')
                    and f.get('acodec') not in (None, 'none')
                    and f.get('vcodec') in (None, 'none')
                ],
                key=lambda f: f.get('abr') or 0,
                reverse=True,
            )

        video_url = video_fmts[0].get('url') if video_fmts else None
        audio_url = audio_fmts[0].get('url') if audio_fmts else None

        logger.info(
            f"DASH URLs — video: {'found' if video_url else 'MISSING'}, "
            f"audio: {'found' if audio_url else 'MISSING'}"
        )
        return {'video': video_url, 'audio': audio_url}

    def _build_stream_url(self, info: Dict[str, Any]) -> Optional[str]:
        """
        Build the /stream/ endpoint URL for DASH videos.
        The stream endpoint will use ffmpeg to merge video+audio on the fly.

        IMPORTANT: Facebook CDN URLs contain '&' characters, so we must
        use quote() (not urlencode) to properly percent-encode each URL
        before embedding it as a query parameter value.
        """
        from urllib.parse import urlencode, quote

        dash = self._get_dash_urls(info)
        video_url = dash.get('video')
        audio_url = dash.get('audio')

        if not video_url:
            # Last resort: return whatever URL yt-dlp gave us
            return info.get('url')

        video_id = info.get('id', 'video')

        # Use quote() to encode each URL value — this handles '&', '?', '=' etc.
        params = [('url', video_url)]
        if audio_url:
            params.append(('audio_url', audio_url))

        query_string = urlencode(params, quote_via=quote)
        stream_path = f"/stream/{video_id}?{query_string}"
        logger.info(f"Built stream URL: /stream/{video_id} (audio={'yes' if audio_url else 'no'})")
        return stream_path

    # ──────────────────────────────────────────────────────────────────────────
    # Available formats list
    # ──────────────────────────────────────────────────────────────────────────

    def _get_available_formats(
        self, info: Dict[str, Any], is_dash: bool
    ) -> List[VideoFormat]:
        """
        Build list of available formats for the response.
        For progressive: list real progressive formats.
        For DASH: expose quality options via /stream/ endpoint.
        """
        formats = info.get('formats', [])
        video_id = info.get('id', 'video')
        result: List[VideoFormat] = []

        if not is_dash:
            # ── Progressive: list audio+video formats ─────────────────────────
            progressive = [
                f for f in formats
                if f.get('url')
                and f.get('acodec') not in (None, 'none')
                and f.get('vcodec') not in (None, 'none')
            ]
            progressive.sort(
                key=lambda f: (f.get('height') or 0, f.get('filesize') or 0),
                reverse=True,
            )
            seen_heights = set()
            for fmt in progressive:
                height = fmt.get('height')
                if height in seen_heights:
                    continue
                seen_heights.add(height)
                quality_label = f"{height}p" if height else fmt.get('format_note', 'Standard')
                result.append(VideoFormat(
                    quality=quality_label,
                    format_id=fmt.get('format_id', ''),
                    ext=fmt.get('ext', 'mp4'),
                    filesize=fmt.get('filesize') or fmt.get('filesize_approx'),
                    url=fmt.get('url'),
                ))
                if len(result) >= 5:
                    break

        else:
            # ── DASH: expose height options via /stream/ endpoint ─────────────
            from urllib.parse import urlencode, quote

            # Collect distinct video-only heights
            video_fmts = sorted(
                [
                    f for f in formats
                    if f.get('url')
                    and f.get('vcodec') not in (None, 'none')
                    and f.get('acodec') in (None, 'none')
                    and f.get('height')
                ],
                key=lambda f: f.get('height') or 0,
                reverse=True,
            )

            # Best audio URL (reuse across qualities)
            audio_fmts = sorted(
                [
                    f for f in formats
                    if f.get('url')
                    and f.get('acodec') not in (None, 'none')
                    and f.get('vcodec') in (None, 'none')
                ],
                key=lambda f: f.get('abr') or 0,
                reverse=True,
            )
            best_audio_url = audio_fmts[0]['url'] if audio_fmts else None

            seen_heights = set()
            for vfmt in video_fmts:
                height = vfmt.get('height')
                if height in seen_heights:
                    continue
                seen_heights.add(height)

                # Use quote_via=quote to properly encode Facebook CDN URLs
                params = [('url', vfmt['url'])]
                if best_audio_url:
                    params.append(('audio_url', best_audio_url))

                stream_url = f"/stream/{video_id}?{urlencode(params, quote_via=quote)}"
                result.append(VideoFormat(
                    quality=f"{height}p",
                    format_id=vfmt.get('format_id', ''),
                    ext='mp4',
                    filesize=vfmt.get('filesize') or vfmt.get('filesize_approx'),
                    url=stream_url,
                ))
                if len(result) >= 5:
                    break

        # Fallback: at least one entry
        if not result:
            fallback_url = self._get_progressive_url(info) or info.get('url')
            if fallback_url:
                result.append(VideoFormat(
                    quality='Best',
                    format_id='best',
                    ext='mp4',
                    filesize=None,
                    url=fallback_url,
                ))

        return result


# Global service instance
video_service = VideoDownloadService()
