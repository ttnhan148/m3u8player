import m3u8
import urllib.parse
import hashlib
import os
import aiofiles
import shutil
import time
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, PlainTextResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CACHE_DIR = "cache"
MAX_CACHE_SIZE = 10 * 1024 * 1024 * 1024  # 10 GB
MAX_CACHE_AGE = 30 * 24 * 60 * 60        # 30 ngày (giây)

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

async def prune_cache():
    """Tự động dọn dẹp cache: xóa file cũ > 30 ngày và đảm bảo tổng dung lượng < 10GB"""
    while True:
        try:
            now = time.time()
            all_files = []
            total_size = 0

            # 1. Xóa file quá hạn 30 ngày
            for file in os.listdir(CACHE_DIR):
                file_path = os.path.join(CACHE_DIR, file)
                if not os.path.isfile(file_path): continue
                
                stats = os.stat(file_path)
                if now - stats.st_mtime > MAX_CACHE_AGE:
                    os.remove(file_path)
                    logger.info(f"Đã xóa cache hết hạn: {file}")
                else:
                    all_files.append((file_path, stats.st_mtime, stats.st_size))
                    total_size += stats.st_size

            # 2. Nếu vẫn vượt quá 10GB, xóa các file cũ nhất
            if total_size > MAX_CACHE_SIZE:
                # Sắp xếp theo thời gian sửa đổi (cũ nhất lên trước)
                all_files.sort(key=lambda x: x[1])
                for file_path, _, size in all_files:
                    if total_size <= MAX_CACHE_SIZE:
                        break
                    os.remove(file_path)
                    total_size -= size
                    logger.info(f"Đã xóa cache cũ nhất (Vượt 10GB): {file_path}")

        except Exception as e:
            logger.error(f"Lỗi dọn dẹp cache: {e}")
            
        await asyncio.sleep(3600)  # Chạy mỗi giờ một lần

app = FastAPI(title="M3U8 Proxy Player")

@app.on_event("startup")
async def startup_event():
    # Khởi động tác vụ dọn dẹp cache ngầm
    asyncio.create_task(prune_cache())


# Khởi tạo Jinja2 templates (thư mục chứa giao diện web)
templates = Jinja2Templates(directory="templates")

# Cho phép CORS để Player JS có thể truy cập streams từ bất kì đâu (quan trọng khi share link)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTTP client dùng để proxy
# Thiết lập limits và timeout tối ưu cho proxy video
limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
client = httpx.AsyncClient(limits=limits)

def make_proxy_url(request: Request, path: str, target_url: str) -> str:
    """Tạo URL đi qua proxy của chúng ta"""
    proxy_base = str(request.base_url).rstrip("/")
    encoded_target = urllib.parse.quote(target_url, safe="")
    return f"{proxy_base}{path}?url={encoded_target}"

@app.get("/")
async def root(request: Request):
    """Trang chủ - giao diện Player"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/proxy/m3u8")
async def proxy_m3u8(request: Request, url: str):
    """Proxy phân tích m3u8 và viết lại các URL bên trong"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Phân tích nội dung M3U8 với thư viện m3u8
        # uri=url giúp m3u8 tự động hiểu các đường dẫn relative bên trong
        playlist = m3u8.loads(response.text, uri=url)
        
        # Phân nhánh 1: Nếu là luồng MASTER (chứa độ phân giải khác nhau)
        if playlist.is_variant:
            # Sửa các variant playlist
            for item in playlist.playlists:
                abs_uri = item.absolute_uri
                item.uri = make_proxy_url(request, "/proxy/m3u8", abs_uri)
                
            # Sửa iframe playlists (nếu có)
            if playlist.iframe_playlists:
                for iframe in playlist.iframe_playlists:
                    abs_uri = iframe.absolute_uri
                    iframe.uri = make_proxy_url(request, "/proxy/m3u8", abs_uri)
                    
            # Sửa các thẻ media (âm thanh, phụ đề)
            if playlist.media:
                for media in playlist.media:
                    if media.uri:
                        abs_uri = media.absolute_uri
                        media.uri = make_proxy_url(request, "/proxy/m3u8", abs_uri)
                        
        # Phân nhánh 2: Nếu là luồng MEDIA (chứa file .TS Video)
        else:
            for segment in playlist.segments:
                abs_uri = segment.absolute_uri
                # Các file ts sẽ được proxy tải qua nhánh /proxy/ts
                segment.uri = make_proxy_url(request, "/proxy/ts", abs_uri)
                
            # Hỗ trợ proxy luôn cho file Khóa giải mã (hỗ trợ luồng mã hóa bảo mật)
            for key in playlist.keys:
                if key and key.uri:
                    abs_uri = key.absolute_uri
                    key.uri = make_proxy_url(request, "/proxy/ts", abs_uri)
                    
        # Trả về M3U8 đúng chuẩn để Player đọc
        return PlainTextResponse(
            playlist.dumps(), 
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"}
        )
        
    except Exception as e:
        logger.error(f"Error fetching proxy m3u8 '{url}': {e}")
        return PlainTextResponse(f"Proxy Error: {str(e)}", status_code=500)


@app.get("/proxy/ts")
async def proxy_ts(request: Request, url: str):
    """Proxy stream và Cache các luồng video .TS"""
    try:
        # Tạo tên file cache từ mã băm URL
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_path = os.path.join(CACHE_DIR, f"{url_hash}.ts")
        
        # Nếu đã có trong cache, trả về file ngay lập tức
        if os.path.exists(cache_path):
            return FileResponse(
                cache_path, 
                media_type="video/mp2t",
                headers={"X-Cache": "HIT"}
            )

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        
        async def stream_and_cache_generator():
            temp_path = f"{cache_path}.tmp"
            try:
                async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
                    response.raise_for_status()
                    async with aiofiles.open(temp_path, mode='wb') as cache_file:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            await cache_file.write(chunk)
                            yield chunk
                
                # Sau khi tải xong, đổi tên từ .tmp thành .ts chính thức
                os.rename(temp_path, cache_path)
            except Exception as stream_err:
                logger.error(f"Lỗi khi đang stream và cache: {stream_err}")
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        return StreamingResponse(
            stream_and_cache_generator(),
            media_type="video/mp2t",
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Cache": "MISS"
            }
        )
        
    except httpx.HTTPError as he:
        return PlainTextResponse(f"HTTP Error: {str(he)}", status_code=502)
    except Exception as e:
        return PlainTextResponse(f"Unknown Error: {str(e)}", status_code=500)

@app.on_event("shutdown")
async def shutdown_event():
    await client.aclose()
