import React from "react";
import { Link } from "react-router-dom";
import { Video, HardDrive, Database, Workflow, FileText, Sparkles, ArrowRight } from "lucide-react";
import { Header } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

const toolList = [
  { to: "/youtube", title: "YouTube Publishing", desc: "Tự động đăng tải video & quản lý danh sách phát", icon: Video },
  { to: "/drive", title: "Google Drive Sync", desc: "Đồng bộ tư liệu & xuất bản tập tin lên mây", icon: HardDrive },
  { to: "/database-io", title: "Quản trị dữ liệu", desc: "Sao lưu, khôi phục và kiểm tra SQLite DB", icon: Database },
  { to: "/flows", title: "Luồng tự động", desc: "Cấu hình pipeline xử lý batch chuỗi tác vụ", icon: Workflow },
  { to: "/logs", title: "Nhật ký hệ thống", desc: "Xem dòng log trực tiếp từ backend FastAPI", icon: FileText },
  { to: "/effects", title: "Bộ hiệu ứng sound", desc: "Tùy chỉnh audio filter, pitch và equalizer", icon: Sparkles },
];

export function Tools() {
  return (
    <div className="space-y-6">
      <Header
        title="Bộ công cụ vận hành"
        subtitle="Mở rộng tích hợp các dịch vụ đám mây, nhật ký hệ thống và công cụ quản trị dữ liệu."
      />

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {toolList.map((tool) => {
          const Icon = tool.icon;
          return (
            <Card key={tool.to} className="border-border hover:border-primary/50 transition-all group">
              <CardHeader className="p-5">
                <div className="flex items-center justify-between mb-3">
                  <div className="h-9 w-9 rounded bg-primary/10 text-primary flex items-center justify-center">
                    <Icon className="h-5 w-5" />
                  </div>
                  <ArrowRight className="h-4 w-4 text-muted-foreground group-hover:text-primary group-hover:translate-x-0.5 transition-all" />
                </div>
                <CardTitle className="text-base font-bold text-foreground group-hover:text-primary transition-colors">
                  <Link to={tool.to} className="after:absolute after:inset-0">
                    {tool.title}
                  </Link>
                </CardTitle>
                <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{tool.desc}</p>
              </CardHeader>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
