import React, { useState } from "react";
import { Lock, Plus } from "lucide-react";
import { postJson, SocialAccount } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { ShortUploadHistory } from "../ShortUploadHistory";
import type { WorkspaceProps } from "../platforms";

/** Form dán access token. open_id là thứ phân biệt các tài khoản: trùng open_id = đổi token. */
function AddAccountForm({ onAdded }: { onAdded: (account: SocialAccount) => void }) {
  const [openId, setOpenId] = useState("");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const account = await postJson<SocialAccount>("/socials/api/tiktok/accounts", {
        open_id: openId.trim(),
        access_token: token.trim(),
        display_name: name.trim() || null,
      });
      setOpenId("");
      setToken("");
      setName("");
      showToast("Đã lưu tài khoản TikTok", "success");
      onAdded(account);
    } catch (error: any) {
      showToast(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Thêm tài khoản</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_2fr_1fr_auto] sm:items-end">
          <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
            Open ID
            <Input value={openId} onChange={(event) => setOpenId(event.target.value)} required className="text-xs" />
          </label>
          <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
            Access token
            <Input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              required
              className="text-xs"
            />
          </label>
          <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
            Tên hiển thị (tuỳ chọn)
            <Input value={name} onChange={(event) => setName(event.target.value)} className="text-xs" />
          </label>
          <Button type="submit" size="sm" disabled={busy}>
            <Plus className="h-4 w-4" />
            Lưu
          </Button>
        </form>
        <p className="mt-3 text-[11px] text-muted-foreground">
          Token của app TikTok đã được duyệt scope <code>video.upload</code>. Open ID phân biệt các tài khoản:
          nhập lại Open ID đã có để thay token hết hạn. App chưa tự làm mới token TikTok.
        </p>
      </CardContent>
    </Card>
  );
}

export function TikTokWorkspace({ account, onChanged }: WorkspaceProps) {
  return (
    <div className="space-y-4">
      <AddAccountForm onAdded={(added) => onChanged(added.id)} />
      {account && (
        <>
          <Card>
            <CardContent className="flex items-start gap-3 p-4 text-xs text-muted-foreground">
              <Lock className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <p>
                Short đăng lên TikTok ở mức riêng tư <code>SELF_ONLY</code> (chỉ mình bạn thấy). Vào app TikTok để
                đổi sang công khai sau khi kiểm tra. Link trong caption của TikTok không bấm được.
              </p>
            </CardContent>
          </Card>
          <ShortUploadHistory platform="tiktok" accountId={account.id} />
        </>
      )}
    </div>
  );
}
