#!/usr/bin/env bash
# Cài gost (SOCKS5 có user/pass) làm điểm thoát mạng cho app trên máy 1.
# Chỉ cho dải LAN (tham số) + Tailscale 100.64.0.0/10 vào cổng proxy; chặn phần còn lại.
#
#   sudo ./install-linux.sh --lan 192.168.1.0/24 [--user egress] [--pass <mật khẩu>] [--port 1080]
#                           [--accounts [--accounts-dir /var/lib/gost-egress/accounts] [--accounts-port 1081]]
#
# Bỏ --pass thì script tự sinh mật khẩu ngẫu nhiên và in ra cuối cùng.
# --accounts: bật thêm kho tài khoản Kaggle/YouTube (gost file server, cùng user/pass).
set -euo pipefail

GOST_VERSION="3.3.3"
# SHA-256 do GitHub công bố cho từng asset của release v3.3.3 (go-gost/gost).
declare -A GOST_SHA256=(
  [amd64]="f4c1255dc892a7014c0ad6e5f1c3573108f7bea4403f04f21873406ab58aa666"
  [arm64]="4c704c92df6eeea8a3b08d0de4fd3c44343095a20136b3b6cf334f98d2f174ab"
  [armv7]="721343061accae0003c380eec61188dda75f5d324ce9ffdd733c4251cb9d2210"
)
TAILSCALE_V4="100.64.0.0/10"
TAILSCALE_V6="fd7a:115c:a1e0::/48"

LAN=""
USER_NAME="egress"
PASS=""
PORT="1080"
ACCOUNTS=""
ACCOUNTS_DIR="/var/lib/gost-egress/accounts"
ACCOUNTS_PORT="1081"

usage() { sed -n '2,8p' "$0"; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lan) LAN="${2:-}"; shift 2 ;;
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --pass) PASS="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --accounts) ACCOUNTS=1; shift ;;
    --accounts-dir) ACCOUNTS_DIR="${2:-}"; shift 2 ;;
    --accounts-port) ACCOUNTS_PORT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Tham số lạ: $1" >&2; usage ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Cần chạy bằng root (sudo)." >&2; exit 1; }
[[ -n "$LAN" ]] || { echo "Thiếu --lan (vd. 192.168.1.0/24)." >&2; usage; }
[[ "$LAN" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { echo "--lan phải là CIDR IPv4, vd. 192.168.1.0/24" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT > 0 && PORT < 65536 )) || { echo "--port không hợp lệ" >&2; exit 1; }
if [[ -z "$PASS" ]]; then
  PASS="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  GENERATED=1
fi
# User/pass nằm trong URL socks5://user:pass@... nên chỉ nhận ký tự an toàn cho URL.
[[ "$USER_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--user chỉ gồm A-Z a-z 0-9 . _ -" >&2; exit 1; }
[[ "$PASS" =~ ^[A-Za-z0-9._-]{12,}$ ]] || { echo "--pass tối thiểu 12 ký tự, chỉ gồm A-Z a-z 0-9 . _ -" >&2; exit 1; }

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  armv7l|armv7) ARCH=armv7 ;;
  *) echo "Kiến trúc $(uname -m) chưa hỗ trợ trong script này." >&2; exit 1 ;;
esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ASSET="gost_${GOST_VERSION}_linux_${ARCH}.tar.gz"
URL="https://github.com/go-gost/gost/releases/download/v${GOST_VERSION}/${ASSET}"
echo "==> Tải $URL"
curl -fsSL -o "$TMP/$ASSET" "$URL"
echo "${GOST_SHA256[$ARCH]}  $TMP/$ASSET" | sha256sum -c - >/dev/null \
  || { echo "Checksum KHÔNG khớp — dừng, không cài." >&2; exit 1; }
tar -xzf "$TMP/$ASSET" -C "$TMP" gost
install -m 0755 "$TMP/gost" /usr/local/bin/gost
echo "==> Đã cài /usr/local/bin/gost ($(/usr/local/bin/gost -V 2>/dev/null || echo v$GOST_VERSION))"

install -d -m 0750 /etc/gost
umask 077
cat > /etc/gost/egress.env <<EOF
GOST_USER=${USER_NAME}
GOST_PASS=${PASS}
GOST_PORT=${PORT}
EOF
chmod 600 /etc/gost/egress.env

PORTS="$PORT"
if [[ -n "$ACCOUNTS" ]]; then
  [[ "$ACCOUNTS_PORT" =~ ^[0-9]+$ ]] && (( ACCOUNTS_PORT > 0 && ACCOUNTS_PORT < 65536 )) || { echo "--accounts-port không hợp lệ" >&2; exit 1; }
  [[ "$ACCOUNTS_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "--accounts-dir phải là đường dẫn tuyệt đối, không khoảng trắng" >&2; exit 1; }
  install -d -m 0700 "$ACCOUNTS_DIR" "$ACCOUNTS_DIR/kaggle" "$ACCOUNTS_DIR/youtube"
  echo "GOST_ACCOUNTS_DIR=${ACCOUNTS_DIR}" >> /etc/gost/egress.env
  echo "GOST_ACCOUNTS_PORT=${ACCOUNTS_PORT}" >> /etc/gost/egress.env
  PORTS="$PORT, $ACCOUNTS_PORT"
fi

# ------------------------------------------------------------------ firewall
FIREWALL=""
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  FIREWALL=ufw
  ufw allow from "$LAN" to any port "$PORT" proto tcp comment "gost-egress LAN" >/dev/null
  ufw allow from "$TAILSCALE_V4" to any port "$PORT" proto tcp comment "gost-egress Tailscale" >/dev/null
  ufw deny "$PORT"/tcp comment "gost-egress chặn phần còn lại" >/dev/null
  if [[ -n "$ACCOUNTS" ]]; then
    ufw allow from "$LAN" to any port "$ACCOUNTS_PORT" proto tcp comment "gost-egress accounts LAN" >/dev/null
    ufw allow from "$TAILSCALE_V4" to any port "$ACCOUNTS_PORT" proto tcp comment "gost-egress accounts Tailscale" >/dev/null
    ufw deny "$ACCOUNTS_PORT"/tcp comment "gost-egress accounts chặn phần còn lại" >/dev/null
  fi
  echo "==> ufw: chỉ $LAN + $TAILSCALE_V4 vào được cổng $PORTS"
elif command -v nft >/dev/null 2>&1; then
  FIREWALL=nft
  # Bảng riêng, nạp lại mỗi lần service khởi động (ExecStartPre) nên không phụ thuộc
  # cấu hình nftables của distro. "add + delete" ở đầu để nạp lại không bị trùng.
  cat > /etc/gost/egress.nft <<EOF
table inet gost_egress
delete table inet gost_egress
table inet gost_egress {
  chain input {
    type filter hook input priority 0; policy accept;
    tcp dport { ${PORTS} } ip saddr { ${LAN}, ${TAILSCALE_V4}, 127.0.0.0/8 } accept
    tcp dport { ${PORTS} } ip6 saddr { ${TAILSCALE_V6}, ::1 } accept
    tcp dport { ${PORTS} } drop
  }
}
EOF
  nft -f /etc/gost/egress.nft
  echo "==> nftables: chỉ $LAN + $TAILSCALE_V4 vào được cổng $PORTS"
else
  echo "!! Không thấy ufw (đang bật) hay nft. Tự chặn cổng $PORT, chỉ cho $LAN và $TAILSCALE_V4." >&2
fi

# ------------------------------------------------------------------ systemd
NFT_PRE=""
FILE_ARG=""
[[ -n "$ACCOUNTS" ]] && FILE_ARG=' -L "file://${GOST_USER}:${GOST_PASS}@:${GOST_ACCOUNTS_PORT}?dir=${GOST_ACCOUNTS_DIR}"'
[[ "$FIREWALL" == nft ]] && NFT_PRE="ExecStartPre=$(command -v nft) -f /etc/gost/egress.nft"
cat > /etc/systemd/system/gost-egress.service <<EOF
[Unit]
Description=gost SOCKS5 egress cho epub-audiobook-app (LAN + Tailscale)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
EnvironmentFile=/etc/gost/egress.env
${NFT_PRE}
ExecStart=/usr/local/bin/gost -L "socks5://\${GOST_USER}:\${GOST_PASS}@:\${GOST_PORT}"${FILE_ARG}
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now gost-egress.service
sleep 1
systemctl is-active --quiet gost-egress.service || { journalctl -u gost-egress -n 30 --no-pager; exit 1; }

LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -v '^100\.' | head -n1 || true)"
TS_IP="$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -n1 || true)"
echo
echo "==> gost-egress đang chạy trên cổng $PORT."
[[ -n "${GENERATED:-}" ]] && echo "    Mật khẩu sinh ngẫu nhiên (lưu lại): $PASS"
echo "    Thêm 2 endpoint trong app (Mạng & Server → Thêm endpoint):"
echo "      socks5h://${USER_NAME}:<mật khẩu>@${LAN_IP:-<IP-LAN>}:${PORT}"
echo "      socks5h://${USER_NAME}:<mật khẩu>@${TS_IP:-<IP-Tailscale-100.x>}:${PORT}"
if [[ -n "$ACCOUNTS" ]]; then
  echo
  echo "==> Kho tài khoản: $ACCOUNTS_DIR (cổng $ACCOUNTS_PORT, cùng user/pass)."
  echo "    $ACCOUNTS_DIR/kaggle/<tên>.json   {\"username\": \"...\", \"key\": \"...\"}"
  echo "    $ACCOUNTS_DIR/youtube/<tên>.json  {\"refresh_token\": \"...\", \"channel_id\": \"UC...\"}"
  echo "    URL trong app: http://${TS_IP:-<IP-Tailscale-100.x>}:${ACCOUNTS_PORT}  và  http://${LAN_IP:-<IP-LAN>}:${ACCOUNTS_PORT}"
fi
