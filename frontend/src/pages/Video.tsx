import React, { useEffect, useState } from "react";
import { Film, Video as VideoIcon } from "lucide-react";
import { api } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/common/StatusBadge";

export function Video() {
  const [items, setItems] = useState<any[]>();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<any>("/video/api/videos")
      .then((x) => setItems(x.videos || x.items || x))
      .catch((err) => console.error(err))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="space-y-6">
      <Header
        title="Phòng dựng video thành phẩm"
        subtitle="Danh sách các video audiobook đã hoàn thành ghép hình ảnh, sóng âm và phụ đề."
      />

      {loading ? (
        <LoadingState text="Đang mở kho video..." />
      ) : !items || items.length === 0 ? (
        <EmptyState text="Chưa có video thành phẩm nào được dựng." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map((v) => (
            <Card key={v.id || v.filename} className="border-border hover:border-primary/40 transition-colors">
              <CardHeader className="p-4 pb-2">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <div className="h-8 w-8 rounded bg-primary/10 text-primary flex items-center justify-center">
                    <VideoIcon className="h-4 w-4" />
                  </div>
                  <StatusBadge value={v.status || "ready"} />
                </div>
                <CardTitle className="text-sm font-bold text-foreground truncate">
                  {v.title || v.filename}
                </CardTitle>
              </CardHeader>
              <CardContent className="p-4 pt-1">
                <div className="text-xs text-muted-foreground font-mono bg-muted/40 p-2 rounded border border-border">
                  {v.resolution || "Video mp4 thành phẩm"}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
