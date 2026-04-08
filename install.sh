#!/bin/bash

# Kiểm tra quyền root
if [ "$EUID" -ne 0 ]; then 
  echo "Vui lòng chạy script với quyền root (sudo ./install.sh)"
  exit 1
fi

echo "======================================="
echo "   CÀI ĐẶT M3U8 TURBO PROXY PLAYER    "
echo "======================================="

# Đường dẫn cài đặt chuẩn
INSTALL_DIR="/var/www/m3u8player"
SERVICE_NAME="m3u8player"
PORT="6969"  #

# 1. Phát hiện hệ điều hành và trình quản lý gói
echo "[1/6] Đang kiểm tra hệ điều hành và cài đặt dependencies..."

if command -v apt-get >/dev/null; then
    PKG_MANAGER="apt-get"
    $PKG_MANAGER update
    $PKG_MANAGER install -y python3 python3-pip python3-venv curl
elif command -v dnf >/dev/null; then
    PKG_MANAGER="dnf"
    $PKG_MANAGER install -y python3 python3-pip curl
elif command -v yum >/dev/null; then
    PKG_MANAGER="yum"
    $PKG_MANAGER install -y python3 python3-pip curl
elif command -v pacman >/dev/null; then
    PKG_MANAGER="pacman"
    $PKG_MANAGER -Sy --noconfirm python python-pip curl
else
    echo "Không tìm thấy trình quản lý gói hỗ trợ (apt, dnf, yum, pacman). Vui lòng cài đặt Python 3 thủ công."
    exit 1
fi

# 2. Tạo thư mục và phân quyền
echo "[2/6] Khởi tạo thư mục $INSTALL_DIR và cache..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/templates"
mkdir -p "$INSTALL_DIR/cache"

# Sao chép mã nguồn
echo "[3/6] Đang copy mã nguồn..."
cp -r app.py requirements.txt "$INSTALL_DIR/" 2>/dev/null || cp app.py requirements.txt "$INSTALL_DIR/"
cp -r templates/index.html "$INSTALL_DIR/templates/" 2>/dev/null || cp templates/index.html "$INSTALL_DIR/templates/"

# Set quyền cho thư mục cache để app có thể ghi
chown -R root:root "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod -R 777 "$INSTALL_DIR/cache"

# 3. Môi trường ảo Python
echo "[4/6] Đang thiết lập Virtual Environment..."
cd "$INSTALL_DIR" || exit
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. Cấu hình Systemd
echo "[5/6] Cấu hình Systemd Service..."
cat <<EOF > /etc/systemd/system/${SERVICE_NAME}.service
[Unit]
Description=M3U8 Proxy Player FastAPI Service
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${INSTALL_DIR}/venv/bin"
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# 5. Tường lửa
echo "[6/6] Mở cổng ${PORT}..."
if command -v ufw > /dev/null; then
    ufw allow ${PORT}/tcp
elif command -v firewall-cmd > /dev/null; then
    firewall-cmd --permanent --add-port=${PORT}/tcp
    firewall-cmd --reload
fi

# Hoàn tất
IP_ADDR=$(curl -s ifconfig.me || echo "IP_CUA_SERVER")

echo "======================================="
echo " CÀI ĐẶT THÀNH CÔNG! (PORT: ${PORT})     "
echo "======================================="
echo "Truy cập ứng dụng: http://${IP_ADDR}:${PORT}"
echo "Thư mục Cache: ${INSTALL_DIR}/cache (Giới hạn: 10GB / Phiên xem 6H)"
echo "---------------------------------------"
echo "Lệnh quản lý:"
echo "  - Xem Logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo "  - Restart:  sudo systemctl restart ${SERVICE_NAME}"
echo "======================================="
