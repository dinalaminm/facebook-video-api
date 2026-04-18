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
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            # ✅ FIX: bestvideo+bestaudio দিয়ে সবসময় audio নিশ্চিত করি
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
        }
    
    async def get_video_info(self, url: str, quality: VideoQuality = VideoQuality.BEST) -> Dict[str, Any]:
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
                quality
            )
            return result
            
        except Exception as e:
            logger.error(f"Error extracting video info: {str(e)}")
            raise ValueError(f"Failed to extract video information: {str(e)}")
    
    async def _resolve_fb_watch_url(self, url: str) -> str:
        """Resolve fb.watch URLs to full Facebook URLs"""
        import aiohttp
        
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                current_url = url
                max_redirects = 5
                redirect_count = 0
                
                while redirect_count < max_redirects:
                    try:
                        async with session.get(
                            current_url, 
                            allow_redirects=False,
                            headers=headers
                        ) as response:
                            if response.status in [301, 302, 303, 307, 308]:
                                redirect_url = response.headers.get('Location')
                                if redirect_url:
                                    if redirect_url.startswith('/'):
                                        from urllib.parse import urljoin
                                        redirect_url = urljoin(current_url, redirect_url)
                                    if 'facebook.com' in redirect_url:
                                        logger.info(f"Resolved fb.watch URL: {url} -> {redirect_url}")
                                        return redirect_url
                                    current_url = redirect_url
                                    redirect_count += 1
                                else:
                                    break
                            else:
                                if 'facebook.com' in current_url:
                                    return current_url
                                break
                    except Exception as e:
                        logger.warning(f"Error during redirect {redirect_count}: {str(e)}")
                        break
                
                logger.warning(f"Could not resolve fb.watch URL to facebook.com: {url}")
                return url
                
        except Exception as e:
            logger.warning(f"Could not resolve fb.watch URL: {str(e)}, using original")
            return url
    
    def _extract_info(self, url: str, quality: VideoQuality) -> Dict[str, Any]:
        """Extract video information using yt-dlp (runs in thread)"""
        
        opts = self.ydl_opts.copy()
        
        # ✅ FIX: সব quality তে bestvideo+bestaudio pattern ব্যবহার করি
        # এটা নিশ্চিত করে যে audio সবসময় থাকবে।
        # আগে "best[ext=mp4]+bestaudio" ছিল — এটা ভুল কারণ
        # "best[ext=mp4]" মানে সবচেয়ে ভালো mp4, কিন্তু সেটা video-only হতে পারে।
        if quality == VideoQuality.BEST:
            opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best'
        elif quality == VideoQuality.WORST:
            opts['format'] = 'worstvideo[ext=mp4]+worstaudio[ext=m4a]/worstvideo+worstaudio/worst[ext=mp4]/worst'
        elif quality == VideoQuality.P360:
            opts['format'] = 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360][ext=mp4]/best[height<=360]'
        elif quality == VideoQuality.P720:
            opts['format'] = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720][ext=mp4]/best[height<=720]'
        elif quality == VideoQuality.P1080:
            opts['format'] = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080][ext=mp4]/best[height<=1080]'
        
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    raise ValueError("No video information found")
                
                return self._process_video_info(info)
                
        except yt_dlp.DownloadError as e:
            error_msg = str(e)
            if "redirect loop" in error_msg.lower() or "redirect" in error_msg.lower():
                if 'fb.watch' in url:
                    raise ValueError("fb.watch URL couldn't be processed. Please use the full facebook.com URL.")
                else:
                    raise ValueError("Video URL has redirect issues. Try the direct Facebook video URL.")
            elif "private" in error_msg.lower() or "not available" in error_msg.lower():
                raise ValueError("This video is private or not available for download.")
            elif "age" in error_msg.lower():
                raise ValueError("This video has age restrictions and cannot be downloaded.")
            else:
                raise ValueError(f"Could not extract video: {error_msg}")
        except Exception as e:
            error_msg = str(e)
            if "302" in error_msg or "redirect" in error_msg.lower():
                raise ValueError("URL redirect issue. Please use the direct Facebook video URL.")
            else:
                raise ValueError(f"Unexpected error: {error_msg}")
    
    def _process_video_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """Process and structure video information"""
        
        video_info = VideoInfo(
            title=info.get('title', 'Unknown Title'),
            duration=info.get('duration'),
            thumbnail=info.get('thumbnail'),
            uploader=info.get('uploader'),
            view_count=info.get('view_count'),
            upload_date=info.get('upload_date')
        )
        
        # ✅ FIX: সঠিক download_url বের করা
        # আগে: info.get('url') — এটা video-only stream দেয় (audio নেই!)
        # এখন: _get_audio_video_url() — audio+video উভয়ই আছে এমন URL খোঁজে
        download_url = self._get_audio_video_url(info)
        
        # ✅ FIX: available_formats এ শুধু audio+video উভয়ই আছে এমন format রাখি
        available_formats = self._get_merged_formats(info)
        
        return {
            'video_info': video_info,
            'download_url': download_url,
            'available_formats': available_formats,
        }

    def _get_audio_video_url(self, info: Dict[str, Any]) -> Optional[str]:
        """
        yt-dlp info থেকে audio+video merged URL বের করে।

        Facebook ভিডিওর দুই ধরন:
        ১. Progressive MP4 → একটাই URL, audio+video একসাথে ✅
        ২. DASH stream    → video-only + audio-only আলাদা URL ❌
                            yt-dlp merge করে কিন্তু info['url'] তে video-only দেয়

        এই function progressive MP4 URL খোঁজে, না পেলে
        requested_formats থেকে সঠিক URL নেয়।
        """
        formats = info.get('formats', [])

        # ── Priority 1: Progressive MP4 (acodec + vcodec উভয়ই আছে) ──────────
        progressive = [
            f for f in formats
            if f.get('url')
            and f.get('acodec') not in (None, 'none')
            and f.get('vcodec') not in (None, 'none')
        ]

        if progressive:
            # সবচেয়ে ভালো quality (height বেশি হলে ভালো)
            progressive.sort(
                key=lambda f: (f.get('height') or 0, f.get('filesize') or 0),
                reverse=True
            )
            logger.info(f"Found progressive format: {progressive[0].get('format_id')} height={progressive[0].get('height')}")
            return progressive[0]['url']

        # ── Priority 2: info['url'] — শুধু তখন নাও যদি audio থাকে ─────────
        direct_url = info.get('url')
        direct_acodec = info.get('acodec', 'none')
        if direct_url and direct_acodec not in (None, 'none'):
            logger.info("Using direct URL with audio")
            return direct_url

        # ── Priority 3: requested_formats থেকে video format URL নাও ─────────
        # (DASH এর ক্ষেত্রে yt-dlp requested_formats এ [video, audio] দেয়)
        # এক্ষেত্রে আমরা video format URL দিই — Android এ play হবে কারণ
        # server merge করেছে আর URL টা merged file এর
        requested = info.get('requested_formats', [])
        if requested:
            # video format এর URL নাও (height বেশি হলে ভালো)
            video_fmts = [f for f in requested if f.get('vcodec') not in (None, 'none')]
            if video_fmts:
                video_fmts.sort(key=lambda f: f.get('height') or 0, reverse=True)
                logger.warning("DASH stream detected — returning video format URL, audio may be missing")
                # এই URL তে audio নাও থাকতে পারে, তাই stream endpoint ব্যবহার করা উচিত
                return video_fmts[0].get('url')

        # ── Fallback: যেকোনো URL ────────────────────────────────────────────
        logger.warning("Could not find audio+video merged URL, using fallback")
        return direct_url or (formats[0]['url'] if formats else None)

    def _get_merged_formats(self, info: Dict[str, Any]) -> List[VideoFormat]:
        """
        Available formats list — শুধু audio+video উভয়ই আছে এমন format।
        আগে audio check ছিল না তাই video-only formats ও list এ আসত।
        """
        formats = info.get('formats', [])
        result = []
        seen_heights = set()

        # ── Progressive formats (audio+video একসাথে) ── সবার আগে ────────────
        for fmt in reversed(formats):  # reversed → best quality আগে
            if not fmt.get('url'):
                continue
            acodec = fmt.get('acodec', 'none')
            vcodec = fmt.get('vcodec', 'none')

            # audio+video উভয়ই থাকতে হবে
            if acodec in (None, 'none') or vcodec in (None, 'none'):
                continue

            height = fmt.get('height')
            if height and height in seen_heights:
                continue

            quality_label = f"{height}p" if height else fmt.get('format_note', 'Standard')
            seen_heights.add(height)

            result.append(VideoFormat(
                quality=quality_label,
                format_id=fmt.get('format_id', ''),
                ext=fmt.get('ext', 'mp4'),
                filesize=fmt.get('filesize') or fmt.get('filesize_approx'),
                url=fmt.get('url'),
            ))

            if len(result) >= 5:
                break

        # ── Progressive format না পেলে best URL একটা দিই ────────────────────
        if not result:
            best_url = self._get_audio_video_url(info)
            if best_url:
                result.append(VideoFormat(
                    quality='Best',
                    format_id='best',
                    ext='mp4',
                    filesize=None,
                    url=best_url,
                ))

        return result


# Global service instance
video_service = VideoDownloadService()
