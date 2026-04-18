import yt_dlp
import asyncio
import logging
from typing import Dict, List, Optional, Any
from app.models import VideoInfo, VideoFormat, VideoQuality
from app.utils.validators import URLValidator
from app.config import settings

logger = logging.getLogger(__name__)

class VideoDownloadService:
    
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
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'format': 'best[ext=mp4]/best',
            'merge_output_format': 'mp4',
        }
    
    async def get_video_info(self, url: str, quality: VideoQuality = VideoQuality.BEST) -> Dict[str, Any]:
        if not URLValidator.is_valid_facebook_url(url):
            raise ValueError("Invalid Facebook URL provided")
        normalized_url = URLValidator.normalize_url(url)
        if 'fb.watch' in normalized_url:
            normalized_url = await self._resolve_fb_watch_url(normalized_url)
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._extract_info, normalized_url, quality)
            return result
        except Exception as e:
            logger.error(f"Error extracting video info: {str(e)}")
            raise ValueError(f"Failed to extract video information: {str(e)}")
    
    async def _resolve_fb_watch_url(self, url: str) -> str:
        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                current_url = url
                for _ in range(5):
                    async with session.get(current_url, allow_redirects=False, headers=headers) as response:
                        if response.status in [301, 302, 303, 307, 308]:
                            redirect_url = response.headers.get('Location', '')
                            if 'facebook.com' in redirect_url:
                                return redirect_url
                            current_url = redirect_url
                        else:
                            break
        except Exception as e:
            logger.warning(f"Could not resolve fb.watch URL: {str(e)}")
        return url
    
    def _extract_info(self, url: str, quality: VideoQuality) -> Dict[str, Any]:
        opts = self.ydl_opts.copy()
        if quality == VideoQuality.BEST:
            opts['format'] = 'best[ext=mp4]/best'
        elif quality == VideoQuality.WORST:
            opts['format'] = 'worst[ext=mp4]/worst'
        elif quality == VideoQuality.P360:
            opts['format'] = 'best[height<=360][ext=mp4]/best[height<=360]'
        elif quality == VideoQuality.P720:
            opts['format'] = 'best[height<=720][ext=mp4]/best[height<=720]'
        elif quality == VideoQuality.P1080:
            opts['format'] = 'best[height<=1080][ext=mp4]/best[height<=1080]'
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("No video information found")
                return self._process_video_info(info)
        except Exception as e:
            raise ValueError(f"Could not extract video: {str(e)}")
    
    def _process_video_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        video_info = VideoInfo(
            title=info.get('title', 'Unknown Title'),
            duration=info.get('duration'),
            thumbnail=info.get('thumbnail'),
            uploader=info.get('uploader'),
            view_count=info.get('view_count'),
            upload_date=info.get('upload_date')
        )
        download_url = info.get('url')
        available_formats = []
        for fmt in info.get('formats', []):
            if fmt.get('url') and fmt.get('height'):
                available_formats.append(VideoFormat(
                    quality=f"{fmt.get('height')}p",
                    format_id=fmt.get('format_id', ''),
                    ext=fmt.get('ext', 'mp4'),
                    filesize=fmt.get('filesize'),
                    url=fmt.get('url')
                ))
        available_formats.sort(
            key=lambda x: int(x.quality.replace('p', '')) if x.quality.replace('p', '').isdigit() else 0,
            reverse=True
        )
        return {'video_info': video_info, 'download_url': download_url, 'available_formats': available_formats[:10]}

video_service = VideoDownloadService()
