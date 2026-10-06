import React, { useEffect, useState } from "react";
import { RefreshCw, RotateCcw } from "lucide-react";
import { api, postJson, SocialPlatform } from "@/api";
import { EmptyState, LoadingState } from "@/components/common/Header";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { showToast } from "@/components/ui/toast";

type UploadRow = {
  short_id: number;
  platform_video_id: string | null;
  status: string;
  error_message: string | null;
  created_at: string;
  caption: string;
  book_id: number | null;
  book_title: string | null;
};

/** Khoá kênh mà /shorts/{id}/retry/{platform} nhận. */
const RETRY_CHANNEL: Record<SocialPlatform, string> = { youtube: "youtube", facebook: "fb", tiktok: "tiktok" };

/** Lịch sử đăng short của một tài khoản (Short Video Studio là nơi tạo và đăng). */
export function ShortUploadHistory({ platform, accountId }: { platform: SocialPlatform; accountId: number }) {
  const [rows, setRows] = useState<UploadRow[] | null>(null);
  const [retrying, setRetrying] = useState<number | null>(null);

  const load = () => {
    api<{ items: UploadRow[] }>(`/socials/api/${platform}/accounts/${accountId}/uploads`)
      .then((response) => setRows(response.items))
      .catch((error) => {
        setRows([]);
        showToast(error.message, "error");
      });
  };
  useEffect(() => {
    setRows(null);
    load();
  }, [platform, accountId]);

  const retry = async (row: UploadRow) => {
    setRetrying(row.short_id);
    try {
      await postJson(`/shorts/${row.short_id}/retry/${RETRY_CHANNEL[platform]}`, {});
      showToast("Đã đưa lại vào hàng đợi", "success");
      load();
    } catch (error: any) {
      showToast(error.message, "error");
    } finally {
      setRetrying(null);
    }
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Lịch sử đăng short</CardTitle>
        <Button size="sm" variant="ghost" onClick={load}>
          <RefreshCw className="h-3.5 w-3.5" />
          Tải lại
        </Button>
      </CardHeader>
      <CardContent>
        {rows === null ? (
          <LoadingState />
        ) : rows.length === 0 ? (
          <EmptyState text="Chưa có short nào đăng bằng tài khoản này. Tạo và đăng ở Short Video Studio." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-border text-[11px] font-mono uppercase text-muted-foreground">
                <tr>
                  <th className="p-2">Short</th>
                  <th className="p-2">Sách</th>
                  <th className="p-2">Caption</th>
                  <th className="p-2">Trạng thái</th>
                  <th className="p-2">ID trên mạng</th>
                  <th className="p-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.short_id} className="border-b border-border/60 align-top">
                    <td className="p-2 font-mono">#{row.short_id}</td>
                    <td className="p-2">{row.book_title || "—"}</td>
                    <td className="p-2 max-w-xs">
                      <span className="line-clamp-2">{row.caption || "—"}</span>
                      {row.error_message && <span className="mt-1 block text-red-600">{row.error_message}</span>}
                    </td>
                    <td className="p-2">
                      <StatusBadge value={row.status} />
                    </td>
                    <td className="p-2 font-mono">{row.platform_video_id || "—"}</td>
                    <td className="p-2 text-right">
                      {row.status === "failed" && (
                        <Button size="sm" variant="outline" disabled={retrying === row.short_id} onClick={() => retry(row)}>
                          <RotateCcw className="h-3.5 w-3.5" />
                          Thử lại
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
