# Server 2 — điểm thoát mạng (egress) cho app

Server 2 **chỉ chạy một SOCKS5 proxy** ([gost](https://github.com/go-gost/gost) v3). App vẫn
chạy trên máy 1 và làm mọi việc. Các luồng YouTube, Kaggle API và Google Drive API đi ra
Internet qua IP public của server 2, nên luôn ra **một IP ổn định**.

Máy 1 nối tới server 2 qua **LAN** hoặc **Tailscale**. Trong app, server 2 là **2 endpoint**
(một qua IP LAN, một qua IP Tailscale 100.x). Cả hai ra cùng một IP public, nên app chuyển
qua lại giữa chúng (kiểm tra TCP ~2s trước khi chọn) là an toàn.

> Mục tiêu "IP ổn định" chỉ đạt khi server 2 có IP public ổn định (không đổi liên tục).

## 1. Chuẩn bị

- Một mật khẩu dài (≥ 12 ký tự, chỉ gồm `A-Z a-z 0-9 . _ -` vì nó nằm trong URL proxy).
  Bỏ trống thì script tự sinh rồi in ra.
- Dải LAN của bạn, ví dụ `192.168.1.0/24`. Script **không** hard-code dải này.
- IP LAN của server 2: `ip -4 addr` (Linux) hoặc `ipconfig` (Windows).
- IP Tailscale của server 2: `tailscale ip -4` (dạng `100.x.y.z`).

Phiên bản gost được pin là **v3.3.3**. Script tải từ GitHub Releases và **dừng nếu SHA-256
không khớp** (checksum ghi sẵn trong script, lấy từ digest GitHub công bố cho từng asset).

## 2. Cài

### Linux (systemd)

```bash
sudo ./install-linux.sh --lan 192.168.1.0/24            # mật khẩu tự sinh
sudo ./install-linux.sh --lan 192.168.1.0/24 --user egress --pass 'mat-khau-dai' --port 1080
```

- Binary: `/usr/local/bin/gost`. Cấu hình: `/etc/gost/egress.env` (chmod 600).
- Service: `gost-egress.service` chạy `gost -L "socks5://USER:PASS@:PORT"`, tự khởi động lại.
- Firewall: nếu **ufw** đang bật thì thêm luật allow cho dải LAN và `100.64.0.0/10`, deny
  phần còn lại của cổng. Nếu không thì dùng **nftables**: bảng riêng `inet gost_egress`,
  nạp lại mỗi lần service khởi động.
- Xem log: `journalctl -u gost-egress -f`.

### Windows

PowerShell **Run as Administrator**:

```powershell
.\install-windows.ps1 -LanCidr 192.168.1.0/24
.\install-windows.ps1 -LanCidr 192.168.1.0/24 -User egress -Pass "mat-khau-dai" -Port 1080
```

- Cài vào `%ProgramData%\gost-egress` (chỉ SYSTEM và Administrators đọc được).
- Chạy lúc khởi động bằng **Task Scheduler** (tài khoản SYSTEM), tự chạy lại khi chết.
- Windows Firewall: một luật inbound cho phép `RemoteAddress` = dải LAN và `100.64.0.0/10`.
  Script gỡ các luật cũ trỏ tới `gost.exe`, kể cả luật "Allow" Windows tự tạo, để không còn
  luật nào mở cổng ra Internet.

> **Microsoft Defender** hay chặn gost (xếp loại HackTool/PUA vì là công cụ proxy). Khi đó
> script dừng và in hướng dẫn. Kiểm tra tên mối đe doạ bằng `Get-MpThreatDetection`; nếu đúng
> là gost và bạn chấp nhận thì thêm ngoại lệ **chỉ** cho thư mục cài:
> `Add-MpPreference -ExclusionPath "$env:ProgramData\gost-egress"`, rồi chạy lại script.
> Checksum SHA-256 vẫn được kiểm tra.

> Gost nghe trên mọi interface. Firewall là lớp chặn chính, user/pass là lớp thứ hai.
> **Không** mở/forward cổng này trên router.

## 3. Thêm vào app

Vào trang **Mạng & Server** (`/network`) → **Thêm endpoint**, loại *Outbound proxy*:

| Tên | URL |
|---|---|
| Server 2 – LAN | `socks5h://egress:<mật khẩu>@<IP-LAN>:1080` |
| Server 2 – Tailscale | `socks5h://egress:<mật khẩu>@<100.x.y.z>:1080` |

Dùng `socks5h://` (không phải `socks5://`) để DNS được phân giải ở server 2.

Bấm **Kiểm tra** trên từng endpoint: cả hai phải hiện **cùng một IP thoát** (IP public của
server 2).

Sau đó ở phần **Định tuyến theo luồng**, với `youtube`, `kaggle`, `drive`: chọn *Qua proxy*,
tick cả 2 endpoint, *Xoay vòng*, rồi **Lưu**. Luồng nào để *Đi thẳng* thì chạy như cũ.

## 4. Khi server 2 không vào được

App **không bao giờ tự đi thẳng**. Job cần luồng đó được hoãn với backoff 2 → 10 phút,
không tính vào số lần thử, và chờ vô hạn. Muốn dừng thì bấm **Huỷ** job. Kernel Kaggle đã
push thì handler ở lại, đợi rồi poll tiếp, không push kernel thứ hai.

## 5. Gỡ cài

```bash
sudo ./uninstall-linux.sh
```

```powershell
.\uninstall-windows.ps1
```
