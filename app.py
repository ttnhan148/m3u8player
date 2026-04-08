import m3u8
import urllib.parse
from urllib.parse import urljoin, quote
import hashlib
import os
import aiofiles
import shutil
import time
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks, Response
from fastapi.responses import StreamingResponse, PlainTextResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import httpx
import logging
from urllib.parse import urljoin, quote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Quản lý các tiến trình đang tải (Single Flight) để chống tải trùng
download_locks = {} 

# Đảm bảo đường dẫn tuyệt đối để chạy ổn định trên Linux/Systemd
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
MAX_CACHE_SIZE = 10 * 1024 * 1024 * 1024  # 10 GB
MAX_CACHE_AGE = 30 * 24 * 60 * 60        # 30 ngày (giây)

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR, exist_ok=True)

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

# Phục vụ các file tĩnh (manifest, icons)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

@app.on_event("startup")
async def startup_event():
    # Khởi động tác vụ dọn dẹp cache ngầm
    asyncio.create_task(prune_cache())


# Khởi tạo Jinja2 templates với đường dẫn tuyệt đối
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Cho phép CORS để Player JS có thể truy cập streams từ bất kì đâu
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTTP client dùng để proxy
client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True)

def make_proxy_url(request: Request, path: str, target_url: str) -> str:
    """Tạo URL đi qua proxy của chúng ta"""
    proxy_base = str(request.base_url).rstrip("/")
    encoded_target = urllib.parse.quote(target_url, safe="")
    return f"{proxy_base}{path}?url={encoded_target}"

@app.get("/")
async def root(request: Request):
    """Trang chủ - giao diện Player"""
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/proxy/m3u8")
async def proxy_m3u8(request: Request, url: str):
    """Proxy phân tích m3u8 và viết lại các URL bên trong"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Phân tích nội dung M3U8 với thư viện m3u8
        playlist = m3u8.loads(response.text, uri=url)
        
        # Phân nhánh 1: Nếu là luồng MASTER
        if playlist.is_variant:
            for item in playlist.playlists:
                abs_uri = item.absolute_uri
                item.uri = make_proxy_url(request, "/proxy/m3u8", abs_uri)
            if playlist.iframe_playlists:
                for iframe in playlist.iframe_playlists:
                    abs_uri = iframe.absolute_uri
                    iframe.uri = make_proxy_url(request, "/proxy/m3u8", abs_uri)
            if playlist.media:
                for media in playlist.media:
                    if media.uri:
                        abs_uri = media.absolute_uri
                        media.uri = make_proxy_url(request, "/proxy/m3u8", abs_uri)
                        
        # Phân nhánh 2: Nếu là luồng MEDIA
        else:
            for segment in playlist.segments:
                abs_uri = segment.absolute_uri
                segment.uri = make_proxy_url(request, "/proxy/ts", abs_uri)
                
            for key in playlist.keys:
                if key and key.uri:
                    abs_uri = key.absolute_uri
                    key.uri = make_proxy_url(request, "/proxy/ts", abs_uri)
                    
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
    """Proxy và cache .ts với cơ chế Single Flight chống tải trùng"""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_path = os.path.join(CACHE_DIR, f"{url_hash}.ts")
    
    # 1. Kiểm tra cache trên đĩa
    if os.path.exists(cache_path):
        return FileResponse(cache_path, media_type="video/MP2T", headers={"X-Cache": "HIT"})
    
    # 2. Cơ chế Single Flight: Kiểm tra xem có ai đang tải đoạn này chưa
    if url_hash in download_locks:
        # Đang có người tải, đợi họ tải xong
        await download_locks[url_hash].wait()
        # Sau khi đợi xong, file chắc chắn đã có trên đĩa
        if os.path.exists(cache_path):
            return FileResponse(cache_path, media_type="video/MP2T", headers={"X-Cache": "HIT-QUEUED"})

    # 3. Nếu chưa ai tải, tạo Lock và bắt đầu tải
    event = asyncio.Event()
    download_locks[url_hash] = event
    
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            async with aiofiles.open(cache_path, "wb") as f:
                await f.write(resp.content)
            
            # Giải phóng cho những yêu cầu đang đợi
            event.set()
            return Response(content=resp.content, media_type="video/MP2T", headers={"X-Cache": "MISS"})
        else:
            event.set() # Vẫn phải set để giải phóng hàng chờ cho dù lỗi
            return PlainTextResponse("Error fetching segment", status_code=resp.status_code)
    except Exception as e:
        event.set()
        logger.error(f"Lỗi tải segment: {e}")
        return PlainTextResponse(f"Unknown Error: {str(e)}", status_code=500)
    finally:
        # Xóa lock sau khi hoàn tất
        if url_hash in download_locks:
            del download_locks[url_hash]

@app.get("/api/cache/status")
async def get_cache_status():
    """Lấy thông tin dung lượng cache hiện tại"""
    try:
        total_size = 0
        for file in os.listdir(CACHE_DIR):
            file_path = os.path.join(CACHE_DIR, file)
            if os.path.isfile(file_path):
                total_size += os.path.getsize(file_path)
        
        percent = (total_size / MAX_CACHE_SIZE) * 100
        total_gb = total_size / (1024 * 1024 * 1024)
        
        return {
            "size_gb": round(total_gb, 2),
            "percent": round(percent, 1),
            "max_gb": 10
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/cache/clear")
async def clear_cache_endpoint():
    """Xóa sạch bộ nhớ đêm ngay lập tức"""
    try:
        for file in os.listdir(CACHE_DIR):
            file_path = os.path.join(CACHE_DIR, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        return {"status": "success", "message": "Đã dọn sạch cache."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.on_event("shutdown")
async def shutdown_event():
    await client.aclose()
