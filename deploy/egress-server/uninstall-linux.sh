#!/usr/bin/env bash
# Gỡ gost-egress do install-linux.sh cài: service, binary, cấu hình, luật firewall.
#   sudo ./uninstall-linux.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Cần chạy bằng root (sudo)." >&2; exit 1; }

PORT=""
[[ -f /etc/gost/egress.env ]] && PORT="$(sed -n 's/^GOST_PORT=//p' /etc/gost/egress.env)"

systemctl disable --now gost-egress.service 2>/dev/null || true
rm -f /etc/systemd/system/gost-egress.service
systemctl daemon-reload

if command -v nft >/dev/null 2>&1; then
  nft delete table inet gost_egress 2>/dev/null || true
fi
if [[ -n "$PORT" ]] && command -v ufw >/dev/null 2>&1; then
  # Xoá đúng các luật install-linux.sh đã thêm (nhận diện qua comment).
  while read -r num; do
    yes | ufw delete "$num" >/dev/null 2>&1 || true
  done < <(ufw status numbered 2>/dev/null | grep "gost-egress" | sed -E 's/^\[ *([0-9]+)\].*/\1/' | sort -rn)
fi

rm -f /usr/local/bin/gost
rm -rf /etc/gost
echo "==> Đã gỡ gost-egress."
