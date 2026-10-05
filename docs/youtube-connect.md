# Kết nối tài khoản YouTube

App upload video lên YouTube qua OAuth 2.0 của Google. Mỗi lần bấm **Kết nối YouTube** trên trang
`/youtube`, app mở `/youtube/connect` → FastAPI redirect sang màn hình đồng ý của Google → Google trả về
`/youtube/callback` → app lưu token vào database rồi quay lại `/youtube?connected=1`.

## 1. Tạo OAuth client trên Google Cloud

1. Vào [Google Cloud Console](https://console.cloud.google.com/), tạo (hoặc chọn) một project.
2. **APIs & Services → Library**: bật **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** (hoặc Internal nếu dùng Workspace).
   - Thêm scope `https://www.googleapis.com/auth/youtube` và `https://www.googleapis.com/auth/youtube.upload`.
   - Khi app còn ở trạng thái **Testing**, thêm email của kênh YouTube vào **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**.
   - **Authorized redirect URIs** — thêm đúng địa chỉ bạn mở app (Google so khớp từng ký tự):
     - `http://localhost:8000/youtube/callback`
     - `http://127.0.0.1:8000/youtube/callback` (app desktop Tauri dùng địa chỉ này)
     - Nếu chạy dev qua Vite (`npm run dev`), callback vẫn đi tới backend nên **không** cần thêm cổng 5173.
5. Copy **Client ID** và **Client secret**.

> Redirect URI được tạo từ địa chỉ của request (`request.base_url`), nên mở app bằng `localhost` hay
> `127.0.0.1` sẽ cho ra hai URI khác nhau. Lỗi `redirect_uri_mismatch` nghĩa là URI đó chưa được khai báo.

## 2. Cấu hình `.env`

```env
YOUTUBE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=GOCSPX-xxxxxxxx
```

Các biến tuỳ chọn khác (`YOUTUBE_DEFAULT_TAGS`, `YOUTUBE_DEFAULT_PRIVACY`, `YOUTUBE_AUTO_UPLOAD`,
`YOUTUBE_DECLARE_ALTERED_CONTENT`) xem trong `.env.example`. Khởi động lại backend sau khi sửa `.env`.

## 3. Kết nối

1. Chạy backend: `./.venv/Scripts/python.exe -m uvicorn app.main:app --reload` → `http://localhost:8000`.
2. Mở trang **YouTube** (`/youtube`), bấm **Kết nối YouTube**. Một tab mới mở `/youtube/connect`.
3. Chọn tài khoản Google sở hữu kênh, chọn kênh (nếu có nhiều kênh) và đồng ý các quyền.
4. Google chuyển về `/youtube/callback`; app lưu token và quay lại `/youtube?connected=1`. Tên kênh hiện
   ở đầu trang. Tab gốc có thể cần tải lại để cập nhật trạng thái.

Muốn đổi kênh: bấm **Ngắt kết nối** rồi kết nối lại.

## 4. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `/youtube/connect` hiện trang **Not found** của app | Service worker (PWA) bản build cũ trả `index.html` cho mọi điều hướng, nên request không tới FastAPI. Đã sửa bằng cách thêm `/(youtube\|drive)/(connect\|callback)` vào `navigateFallbackDenylist` trong `vite.config.ts`. | Chạy `npm run build`, rồi tải lại app (service worker tự cập nhật). Nếu vẫn lỗi: DevTools → **Application → Service Workers → Unregister**, hoặc hard reload (Ctrl+Shift+R). |
| `400 YouTube not configured` | Thiếu `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | Điền `.env` rồi khởi động lại backend. |
| Google báo `redirect_uri_mismatch` | Redirect URI chưa khai báo trong OAuth client | Thêm đúng URI ở bước 1.4 (chú ý `localhost` vs `127.0.0.1`). |
| Google báo `access_denied` / app chưa được xác minh | Tài khoản chưa nằm trong **Test users** | Thêm email vào Test users của OAuth consent screen. |
| Quay về `/youtube?error=...` | Đổi code lấy token hoặc lưu credentials thất bại | Xem log backend (`YouTube OAuth callback failed`), thử kết nối lại. |
| API báo `auth_required` sau một thời gian | Refresh token hết hạn/bị thu hồi (app ở chế độ Testing: token hết hạn sau 7 ngày) | Kết nối lại; publish OAuth consent screen sang **Production** để token không hết hạn sau 7 ngày. |

Lưu ý: luồng **Google Drive** (`/drive/connect`, `/drive/callback`) dùng cùng cơ chế và cũng được sửa
cùng lúc trong denylist của service worker.
