import React from "react";
import { useLocation } from "react-router-dom";
import { Wrench, Terminal, Info } from "lucide-react";
import { Header } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

const toolLabels: Record<string, { title: string; desc: string }> = {
  "/youtube": { title: "YouTube Studio", desc: "Đẩy video audiobook và đồng bộ danh sách phát" },
  "/drive": { title: "Google Drive Storage", desc: "Tải nguyên liệu và đồng bộ bản sao lưu" },
  "/database-io": { title: "Cơ sở dữ liệu", desc: "Trích xuất, khôi phục và dọn dẹp DB" },
  "/flows": { title: "Luồng tự động", desc: "Điều khiển chuỗi quy trình độc lập" },
  "/logs": { title: "Nhật ký hệ thống", desc: "Bảng ghi lại hoạt động FastAPI backend" },
  "/effects": { title: "Hiệu ứng âm thanh", desc: "Bộ xử lý hiệu ứng âm thanh và lọc nhiễu" },
};

export function LegacyTool() {
  const location = useLocation();
  const path = location.pathname;
  const info = toolLabels[path] || { title: "Bộ công cụ", desc: "Công cụ hệ thống" };

  return (
    <div className="space-y-6">
      <Header title={info.title} subtitle={info.desc} />

      <Card className="border-border">
        <CardHeader className="pb-3 border-b border-border">
          <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono flex items-center gap-2">
            <Wrench className="h-4 w-4 text-primary" />
            KHÔNG GIAN VẬN HÀNH TOOL
          </CardTitle>
        </CardHeader>
        <CardContent className="p-6 space-y-4">
          <div className="flex items-start gap-3 p-4 rounded-md bg-muted/40 border border-border text-xs text-muted-foreground">
            <Info className="h-4 w-4 text-primary shrink-0 mt-0.5" />
            <div>
              Khu vực này đã được hợp nhất vào app shell SPA. Tất cả các endpoint API backend bên dưới vẫn duy trì tương thích 100%.
            </div>
          </div>

          {path === "/logs" && (
            <div className="space-y-2 pt-2">
              <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground">
                <Terminal className="h-3.5 w-3.5 text-primary" />
                <span>NHẬT KÝ THỜI GIAN THỰC (LIVE LOGS RAW)</span>
              </div>
              <div className="rounded-md border border-border overflow-hidden bg-zinc-950">
                <iframe
                  title="Nhật ký"
                  src="/logs/raw"
                  className="w-full h-[60vh] border-0"
                />
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
