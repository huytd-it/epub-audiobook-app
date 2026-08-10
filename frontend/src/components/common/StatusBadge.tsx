import React from "react";
import { Badge } from "@/components/ui/badge";

export function StatusBadge({ value }: { value?: string }) {
  if (!value) return null;
  const val = value.toLowerCase();

  if (["done", "completed", "ready", "finished", "success"].includes(val)) {
    return <Badge variant="success">HOÀN THÀNH</Badge>;
  }
  if (["running", "active", "processing", "in_progress"].includes(val)) {
    return (
      <Badge variant="lime" className="gap-1">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-600 animate-pulse" />
        ĐANG CHẠY
      </Badge>
    );
  }
  if (["pending", "queued", "waiting"].includes(val)) {
    return <Badge variant="warning">CHỜ XỬ LÝ</Badge>;
  }
  if (["failed", "error", "cancelled"].includes(val)) {
    return <Badge variant="destructive">LỖI / HỦY</Badge>;
  }
  if (["excluded"].includes(val)) {
    return <Badge variant="outline" className="text-muted-foreground">BỎ QUA</Badge>;
  }

  return <Badge variant="outline">{value}</Badge>;
}
