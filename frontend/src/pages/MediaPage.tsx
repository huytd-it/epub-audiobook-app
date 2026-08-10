import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Music, Image, Mic, FolderKanban, ArrowRight } from "lucide-react";
import { api, Media } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function MediaPage() {
  const [data, setData] = useState<Media>();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<Media>("/api/ui/media")
      .then(setData)
      .catch((err) => console.error(err))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingState text="Đang mở kho tài nguyên media..." />;

  return (
    <div className="space-y-6">
      <Header
        title="Kho tư liệu & Nguyên liệu Sản xuất"
        subtitle="Tổng quan thư viện nhạc nền background, hình ảnh minh họa và các file giọng mẫu sinh TTS."
      />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <MediaOverviewPanel
          title="Nhạc nền Background"
          icon={Music}
          items={data?.music || []}
          link="/music"
          linkLabel="Quản lý nhạc nền"
          kind="music"
        />
        <MediaOverviewPanel
          title="Hình ảnh & Video gốc"
          icon={Image}
          items={data?.photos || []}
          link="/photos"
          linkLabel="Quản lý hình ảnh"
          kind="photos"
        />
        <MediaOverviewPanel
          title="Giọng mẫu Voice Studio"
          icon={Mic}
          items={data?.voices || []}
          link="/voices"
          linkLabel="Quản lý giọng mẫu"
          kind="voices"
        />
      </div>
    </div>
  );
}

function MediaOverviewPanel({
  title,
  icon: Icon,
  items,
  link,
  linkLabel,
  kind,
}: {
  title: string;
  icon: React.ElementType;
  items: any[];
  link: string;
  linkLabel: string;
  kind: string;
}) {
  return (
    <Card className="border-border flex flex-col h-full hover:border-primary/40 transition-colors">
      <CardHeader className="pb-3 border-b border-border">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono flex items-center gap-2">
            <Icon className="h-4 w-4 text-primary" />
            {title}
          </CardTitle>
          <span className="text-xs font-mono font-bold px-2 py-0.5 rounded bg-muted text-muted-foreground">
            {items.length}
          </span>
        </div>
      </CardHeader>
      <CardContent className="p-0 flex-1 flex flex-col justify-between">
        <div className="divide-y divide-border">
          {items.length === 0 ? (
            <EmptyState text="Kho đang trống" />
          ) : (
            items.slice(0, 5).map((item, idx) => (
              <div key={item.id || item.name} className="p-3 flex items-center gap-3 text-xs hover:bg-muted/30 transition-colors">
                <span className="font-mono text-muted-foreground font-bold shrink-0 w-6 text-center">
                  #{idx + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold text-foreground truncate">{item.name}</div>
                  <div className="text-[11px] text-muted-foreground font-mono">
                    {item.duration_sec
                      ? `${Math.round(item.duration_sec)} giây`
                      : item.size
                      ? `${(item.size / 1024 / 1024).toFixed(2)} MB`
                      : kind}
                  </div>
                </div>
              </div>
            ))
          )}
          {items.length > 5 && (
            <div className="p-2 text-center text-xs font-mono text-muted-foreground">
              + {items.length - 5} file khác...
            </div>
          )}
        </div>

        <div className="p-3 border-t border-border bg-muted/20">
          <Button variant="outline" size="sm" className="w-full justify-between text-xs" asChild>
            <Link to={link}>
              <span>{linkLabel}</span>
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
