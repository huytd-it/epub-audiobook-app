import { useEffect, useState } from "react";
import { FolderSync, HardDrive, RefreshCw, Trash2 } from "lucide-react";
import { api, postJson, put, DriveAccount } from "@/api";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { showToast } from "@/components/ui/toast";
import { confirmDialog } from "@/components/ui/confirm";
import { selectClass } from "@/pages/socials/platforms";

type SyncFolder = {
  batch_id: string;
  package_id: number;
  book_title: string;
  folder_id: string;
  folder_name: string;
  size_bytes: number;
  modified_time: string | null;
  total: number;
  imported_count: number;
  imported: boolean;
  busy: boolean;
  can_clean: boolean;
};
type SyncFoldersState = {
  account: { id: number; email: string | null };
  quota: { limit: number | null; usage: number | null; usage_in_drive_trash: number | null };
  folders: SyncFolder[];
};

const formatBytes = (value: number | null | undefined) => {
  if (value == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
};

/** Thư mục sync của kernel Kaggle trên Drive dùng chung: dung lượng + dọn thủ công. */
export function KaggleSyncFoldersCard() {
  const [state, setState] = useState<SyncFoldersState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const load = () => {
    setLoading(true);
    setError(null);
    api<SyncFoldersState>("/drive/kaggle-sync-folders")
      .then((next) => {
        setState(next);
        setSelected(new Set(next.folders.filter((f) => f.can_clean).map((f) => f.batch_id)));
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  const clean = async () => {
    const chosen = (state?.folders ?? []).filter((f) => selected.has(f.batch_id) && f.can_clean);
    if (!chosen.length) return;
    const bytes = chosen.reduce((sum, f) => sum + f.size_bytes, 0);
    const ok = await confirmDialog({
      title: `Chuyển ${chosen.length} thư mục sync vào thùng rác Drive?`,
      message: `Khoảng ${formatBytes(bytes)}. Chỉ batch đã import xong mới được dọn; khôi phục được trong 30 ngày. Dung lượng chỉ được trả lại khi thùng rác được dọn.`,
      kind: "warning",
      confirmLabel: "Chuyển vào thùng rác",
    });
    if (!ok) return;
    try {
      const result = await postJson<{ trashed: string[]; skipped: { batch_id: string; reason: string }[] }>(
        "/drive/kaggle-sync-folders/cleanup", { batch_ids: chosen.map((f) => f.batch_id) },
      );
      showToast(`Đã chuyển ${result.trashed.length} thư mục vào thùng rác`, "success");
      result.skipped.forEach((s) => showToast(`${s.batch_id}: ${s.reason}`, "error"));
      load();
    } catch (err: any) {
      showToast(err.message, "error");
    }
  };

  const quota = state?.quota;
  const percent = quota?.limit && quota.usage != null ? Math.min(100, (quota.usage / quota.limit) * 100) : null;
  const cleanable = (state?.folders ?? []).filter((f) => selected.has(f.batch_id) && f.can_clean);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm flex items-center justify-between gap-2">
          <span className="flex items-center gap-2"><FolderSync className="h-4 w-4" /> Thư mục sync Kaggle</span>
          <span className="flex gap-2">
            <Button variant="outline" size="sm" disabled={loading} onClick={load}>
              <RefreshCw className="h-3.5 w-3.5" /> {state ? "Tải lại" : "Kiểm tra dung lượng"}
            </Button>
            <Button variant="destructive" size="sm" disabled={!cleanable.length} onClick={clean}>
              <Trash2 className="h-3.5 w-3.5" /> Dọn các batch đã xong ({cleanable.length})
            </Button>
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        {!state && !loading && !error && (
          <p className="text-muted-foreground">Bấm "Kiểm tra dung lượng" để quét Drive dùng chung (đi qua luồng drive).</p>
        )}
        {loading && <p className="text-muted-foreground">Đang quét Drive…</p>}
        {error && <p className="text-destructive">{error}</p>}
        {state && (
          <>
            <div className="space-y-1">
              <div className="flex justify-between">
                <span>Drive: <b>{state.account.email || `#${state.account.id}`}</b></span>
                <span>
                  {formatBytes(quota?.usage)} / {quota?.limit ? formatBytes(quota.limit) : "không giới hạn"}
                  {quota?.usage_in_drive_trash ? ` · thùng rác ${formatBytes(quota.usage_in_drive_trash)}` : ""}
                </span>
              </div>
              {percent !== null && (
                <div className="h-2 rounded bg-muted overflow-hidden">
                  <div className={`h-full ${percent > 90 ? "bg-red-500" : percent > 75 ? "bg-amber-500" : "bg-primary"}`} style={{ width: `${percent}%` }} />
                </div>
              )}
            </div>
            {state.folders.length === 0 ? (
              <p className="text-muted-foreground">Không thấy thư mục sync nào của batch Kaggle trên Drive này.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead className="text-left text-muted-foreground border-b border-border">
                    <tr><th className="p-2"></th><th className="p-2">Batch</th><th className="p-2">Sách</th><th className="p-2">Import</th><th className="p-2 text-right">Dung lượng</th><th className="p-2">Sửa lần cuối</th></tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {state.folders.map((folder) => (
                      <tr key={folder.batch_id}>
                        <td className="p-2">
                          <input
                            type="checkbox"
                            disabled={!folder.can_clean}
                            checked={selected.has(folder.batch_id)}
                            onChange={(e) => setSelected((current) => {
                              const next = new Set(current);
                              if (e.target.checked) next.add(folder.batch_id); else next.delete(folder.batch_id);
                              return next;
                            })}
                          />
                        </td>
                        <td className="p-2 font-mono break-all">{folder.folder_name}</td>
                        <td className="p-2">{folder.book_title}</td>
                        <td className="p-2">
                          {folder.busy ? <Badge variant="warning">Đang chạy/hoãn</Badge>
                            : folder.imported ? <Badge variant="success">Xong</Badge>
                            : <Badge variant="secondary">{folder.imported_count}/{folder.total}</Badge>}
                        </td>
                        <td className="p-2 text-right font-mono">{formatBytes(folder.size_bytes)}</td>
                        <td className="p-2 whitespace-nowrap">{folder.modified_time ? new Date(folder.modified_time).toLocaleString("vi-VN") : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="text-[11px] text-muted-foreground">
              Không tự xoá. Chỉ batch đã import đủ và không có job kaggle_tts đang chạy/hoãn mới được dọn; "dọn" là chuyển vào thùng rác Drive.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}

type KaggleDriveState = {
  account_id: number | null;
  effective_account_id: number | null;
  effective_account_email: string | null;
  error: string | null;
};

/** Drive dùng chung cho mọi kernel Kaggle (mọi tài khoản Kaggle đồng bộ vào đây). */
export function KaggleDriveAccountCard({ accounts }: { accounts: DriveAccount[] }) {
  const [state, setState] = useState<KaggleDriveState | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<KaggleDriveState>("/drive/kaggle-drive-account")
      .then(setState)
      .catch((err) => showToast(`Không đọc được Drive cho Kaggle: ${err.message}`, "error"));
  }, [accounts.length]);

  const choose = async (value: string) => {
    setSaving(true);
    try {
      const next = await put<KaggleDriveState>("/drive/kaggle-drive-account", {
        account_id: value === "" ? null : Number(value),
      });
      setState(next);
      showToast("Đã lưu Drive dùng chung cho kernel Kaggle", "success");
    } catch (err: any) {
      showToast(err.message, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm flex items-center gap-2">
          <HardDrive className="h-4 w-4" /> Drive dùng chung cho kernel Kaggle
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <select
          className={`${selectClass} w-full max-w-md`}
          value={state?.account_id ?? ""}
          disabled={saving || state === null}
          onChange={(e) => choose(e.target.value)}
        >
          <option value="">
            Tự động: tài khoản đầu tiên có refresh token
            {state?.account_id == null && state?.effective_account_email ? ` (${state.effective_account_email})` : ""}
          </option>
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.account_email || `Tài khoản #${account.id}`}
            </option>
          ))}
        </select>
        {state?.error && <p className="text-xs text-destructive">{state.error}</p>}
        <p className="text-[11px] text-muted-foreground leading-5">
          Mọi tài khoản Kaggle đồng bộ chunk/kết quả vào Drive này. Mỗi batch khoá Drive ngay lần chạy
          đầu, nên đổi Drive ở đây chỉ ảnh hưởng batch mới; batch đang chạy hoặc đang hoãn vẫn resume
          đúng Drive cũ.
        </p>
      </CardContent>
    </Card>
  );
}
