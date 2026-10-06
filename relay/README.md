# Relay (Vercel Edge)

Một Edge Function chuyển tiếp request từ app tới các provider AI bị giới hạn theo vùng
(Gemini, OpenAI...). Đây là project Vercel độc lập: không nằm trong build của app và
không dùng chung `node_modules` với thư mục gốc.

## Khi nào dùng relay, khi nào dùng proxy

| | Relay (thư mục này) | Outbound proxy |
|---|---|---|
| Dùng cho scope | `ai` | `ai`, `youtube`, `facebook`, `tiktok` |
| Upload video | Không — Vercel giới hạn body ~4,5 MB | Được |
| Chi phí | Miễn phí ở mức dùng cá nhân | Tuỳ nhà cung cấp proxy |

App từ chối đặt relay cho scope mạng xã hội; muốn đổi IP cho YouTube/Facebook/TikTok
thì thêm outbound proxy ở trang **Mạng & Proxy**.

## Deploy

```bash
cd relay
npx vercel login
npx vercel link            # tạo một project mới, ví dụ epub-relay-sin
npx vercel env add RELAY_SECRET production    # dán một chuỗi ngẫu nhiên dài
npx vercel deploy --prod
```

Tạo secret: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Sau khi deploy, vào app → **OPERATIONS → Mạng & Proxy → Thêm endpoint**:

- Loại: `relay`
- URL: địa chỉ production của project, ví dụ `https://epub-relay-sin.vercel.app`
- Secret: đúng giá trị `RELAY_SECRET` vừa đặt

Bấm **Kiểm tra** để thấy IP thoát, rồi đặt scope `ai` sang `relay`.

## Nhiều region / nhiều IP

Một project chạy ở một region: hằng `regions` trong `config` ở đầu `api/relay.ts`,
mặc định `sin1`. Phải ghim tường minh vì Edge Function mặc định chạy ở vùng gần người
gọi nhất, tức là ngay trong vùng đang bị chặn. Muốn xoay vòng giữa nhiều vùng thì
deploy thư mục này thành nhiều project, mỗi project sửa `regions` sang một vùng khác
(`hnd1`, `iad1`, `fra1`...) trước khi deploy, rồi thêm từng URL vào app. App xoay vòng giữa các relay của scope `ai` và tự tạm nghỉ relay nào
lỗi hoặc bị provider chặn theo vùng.

Chọn vùng mà provider có phục vụ — đặt relay ở vùng bị chặn thì vẫn bị chặn.

## Biến môi trường

| Biến | Bắt buộc | Ý nghĩa |
|---|---|---|
| `RELAY_SECRET` | Có | Secret dùng chung; request thiếu/sai header `x-relay-key` bị từ chối 401. |
| `RELAY_ALLOWED_HOSTS` | Không | Danh sách host đích, cách nhau dấu phẩy. Đặt biến này sẽ **thay** danh sách mặc định trong `api/relay.ts`. |

Thêm provider OpenAI-compatible khác (`AI_BASE_URL`, `AI_CONTENT_BASE_URL`) thì phải
thêm host của nó vào `RELAY_ALLOWED_HOSTS`, nếu không relay trả 403 `host_not_allowed`.
Giữ `api.ipify.org` trong danh sách nếu muốn nút **Kiểm tra** hoạt động.

## An toàn

- Không có `RELAY_SECRET` thì function trả 500 chứ không chạy ở chế độ mở.
- Chỉ chuyển tiếp tới host trong allowlist, chỉ `https`, không tự đi theo redirect.
- API key của provider đi qua relay trong header của request. Chỉ deploy relay trên
  tài khoản Vercel của chính bạn và đừng chia sẻ URL + secret.
- Đổi secret: cập nhật env trên Vercel, deploy lại, rồi sửa secret của endpoint trong app.
