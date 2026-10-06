import React, { useEffect, useState } from "react";
import { ExternalLink, Plus, RefreshCw } from "lucide-react";
import { api, postJson, SocialAccount } from "@/api";
import { EmptyState, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { ShortUploadHistory } from "../ShortUploadHistory";
import type { WorkspaceProps } from "../platforms";

type PageVideo = {
  id: string;
  title?: string;
  description?: string;
  created_time?: string;
  length?: number;
  permalink_url?: string;
  picture?: string;
};

const formatLength = (seconds?: number) => {
  if (!seconds) return "—";
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

/** Form dán Page Access Token. Thêm Page mới, hoặc đổi token của Page đã có (trùng Page ID). */
function AddPageForm({ onAdded }: { onAdded: (account: SocialAccount) => void }) {
  const [pageId, setPageId] = useState("");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const account = await postJson<SocialAccount>("/socials/api/facebook/accounts", {
        page_id: pageId.trim(),
        page_access_token: token.trim(),
        page_name: name.trim() || null,
      });
      setPageId("");
      setToken("");
      setName("");
      showToast(`Đã kết nối Page ${account.label}`, "success");
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
        <CardTitle>Thêm Page</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_2fr_1fr_auto] sm:items-end">
          <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
            Page ID
            <Input value={pageId} onChange={(event) => setPageId(event.target.value)} required className="text-xs" />
          </label>
          <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
            Page Access Token
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
            {busy ? "Đang kiểm tra..." : "Kết nối"}
          </Button>
        </form>
        <p className="mt-3 text-[11px] text-muted-foreground">
          Token lấy từ Meta App đã được duyệt quyền <code>pages_manage_posts</code> và{" "}
          <code>pages_read_engagement</code>. App gọi Graph API để xác nhận token đọc được Page trước khi lưu;
          nhập lại Page ID đã có để thay token hết hạn.
        </p>
      </CardContent>
    </Card>
  );
}

function PageVideos({ accountId }: { accountId: number }) {
  const [videos, setVideos] = useState<PageVideo[] | null>(null);
  const [error, setError] = useState("");

  const load = () => {
    setVideos(null);
    setError("");
    api<{ items: PageVideo[] }>(`/socials/api/facebook/accounts/${accountId}/videos`)
      .then((response) => setVideos(response.items))
      .catch((failure) => {
        setVideos([]);
        setError(failure.message);
      });
  };
  useEffect(load, [accountId]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Video & Reels trên Page</CardTitle>
        <Button size="sm" variant="ghost" onClick={load}>
          <RefreshCw className="h-3.5 w-3.5" />
          Tải lại
        </Button>
      </CardHeader>
      <CardContent>
        {videos === null ? (
          <LoadingState text="Đang hỏi Graph API..." />
        ) : error ? (
          <p className="text-xs text-red-600">{error}</p>
        ) : videos.length === 0 ? (
          <EmptyState text="Page chưa có video nào." />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {videos.map((video) => (
              <a
                key={video.id}
                href={video.permalink_url ? new URL(video.permalink_url, "https://www.facebook.com").href : undefined}
                target="_blank"
                rel="noreferrer"
                className="flex gap-3 rounded-md border border-border p-2 hover:bg-muted"
              >
                {video.picture ? (
                  <img src={video.picture} alt="" className="h-16 w-12 shrink-0 rounded object-cover" />
                ) : (
                  <div className="h-16 w-12 shrink-0 rounded bg-muted" />
                )}
                <div className="min-w-0 text-xs">
                  <p className="line-clamp-2 font-medium text-foreground">
                    {video.title || video.description || `Video ${video.id}`}
                  </p>
                  <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                    {formatLength(video.length)} · {video.created_time?.slice(0, 10) || "—"}
                    <ExternalLink className="ml-1 inline h-3 w-3" />
                  </p>
                </div>
              </a>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function FacebookWorkspace({ account, onChanged }: WorkspaceProps) {
  return (
    <div className="space-y-4">
      <AddPageForm onAdded={(added) => onChanged(added.id)} />
      {account && (
        <>
          <PageVideos key={`videos-${account.id}`} accountId={account.id} />
          <ShortUploadHistory platform="facebook" accountId={account.id} />
        </>
      )}
    </div>
  );
}
