import { useEffect, useState } from "react";
import { DownloadCloud, KeyRound, Save } from "lucide-react";
import { api, postJson, put, AccountSourceConfig, AccountSourceSyncSummary } from "@/api";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";

/** Kho tài khoản Kaggle/YouTube trên server 2 (gost file server có user/pass). */
export function AccountSourceCard() {
  const [config, setConfig] = useState<AccountSourceConfig | null>(null);
  const [urls, setUrls] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"save" | "sync" | null>(null);
  const [summary, setSummary] = useState<AccountSourceSyncSummary | null>(null);

  useEffect(() => {
    api<AccountSourceConfig>("/network/api/account-source")
      .then((next) => {
        setConfig(next);
        setUrls(next.urls.join("\n"));
        setUsername(next.username);
      })
      .catch((err) => showToast(`Không đọc được cấu hình kho tài khoản: ${err.message}`, "error"));
  }, []);

  const save = async () => {
    setBusy("save");
    try {
      const next = await put<AccountSourceConfig>("/network/api/account-source", {
        urls: urls.split(/\s+/).filter(Boolean),
        username,
        password: password || null,
      });
      setConfig(next);
      setPassword("");
      showToast("Đã lưu cấu hình kho tài khoản server 2", "success");
    } catch (err: any) {
      showToast(err.message, "error");
    } finally {
      setBusy(null);
    }
  };

  const sync = async () => {
    setBusy("sync");
    try {
      const result = await postJson<AccountSourceSyncSummary>("/network/api/account-source/sync", {});
      setSummary(result);
      showToast(
        `Đã nạp từ server 2 — Kaggle: +${result.kaggle.added} / cập nhật ${result.kaggle.updated} / gỡ ${result.kaggle.removed}; ` +
          `YouTube: +${result.youtube.added} / cập nhật ${result.youtube.updated} / gỡ ${result.youtube.removed}`,
        "success",
      );
    } catch (err: any) {
      showToast(err.message, "error");
    } finally {
      setBusy(null);
    }
  };

  const skipped = summary ? [...summary.kaggle.skipped, ...summary.youtube.skipped] : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm flex items-center justify-between gap-2">
          <span className="flex items-center gap-2"><KeyRound className="h-4 w-4" /> Kho tài khoản trên server 2</span>
          <Button size="sm" disabled={!config?.configured || busy !== null} onClick={sync}>
            <DownloadCloud className="h-4 w-4" /> {busy === "sync" ? "Đang nạp…" : "Nạp từ server 2"}
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <p className="text-muted-foreground leading-5">
          Server 2 phục vụ thư mục <span className="font-mono">accounts\kaggle\*.json</span> (kaggle.json gốc) và{" "}
          <span className="font-mono">accounts\youtube\*.json</span> (refresh token). "Nạp" chỉ ghi tên/username/kênh vào
          máy này để chọn ở trang sách, Drive và Socials; <b>key và token không lưu ở máy này</b>, mỗi lần chạy job mới đọc
          từ server 2. Server 2 tắt thì job đang cần được hoãn rồi tự chạy lại.
        </p>
        <label className="block space-y-1">
          <span className="font-medium">URL kho (mỗi dòng một URL, thử lần lượt)</span>
          <textarea
            className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs"
            rows={2}
            placeholder={"http://100.102.234.19:1081\nhttp://172.16.107.160:1081"}
            value={urls}
            onChange={(e) => setUrls(e.target.value)}
          />
        </label>
        <div className="grid gap-2 sm:grid-cols-2">
          <Input placeholder="User" value={username} onChange={(e) => setUsername(e.target.value)} />
          <Input
            type="password"
            placeholder={config?.has_password ? "Mật khẩu (để trống nếu giữ nguyên)" : "Mật khẩu"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-muted-foreground">
            Nên đặt URL Tailscale lên đầu: kết nối được mã hoá. Qua LAN, key đi dạng HTTP thường trong mạng nội bộ.
          </span>
          <Button size="sm" variant="outline" disabled={busy !== null} onClick={save}>
            <Save className="h-3.5 w-3.5" /> Lưu
          </Button>
        </div>
        {skipped.length > 0 && (
          <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-amber-900 space-y-0.5">
            <div className="font-semibold">Bỏ qua {skipped.length} file:</div>
            {skipped.map((item) => (
              <div key={item.file}><span className="font-mono">{item.file}</span>: {item.reason}</div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
