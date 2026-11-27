import { useEffect, useState } from "react";
import { HardDrive } from "lucide-react";
import { api, put, DriveAccount } from "@/api";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { showToast } from "@/components/ui/toast";
import { selectClass } from "@/pages/socials/platforms";

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
