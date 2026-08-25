import React, { useState } from "react";
import { FolderKanban } from "lucide-react";
import { Header } from "@/components/common/Header";
import { MediaBrowser, type MediaEntry } from "@/components/media-browser/MediaBrowser";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

const CATEGORY_OPTIONS = [
  { value: "", label: "Tất cả" },
  { value: "thumbnails", label: "Thumbnail" },
  { value: "videos", label: "Video" },
  { value: "audio", label: "Âm thanh" },
  { value: "backgrounds", label: "Ảnh nền" },
  { value: "voices", label: "Giọng mẫu" },
  { value: "music", label: "Nhạc nền" },
  { value: "logos", label: "Logo" },
  { value: "uploads", label: "Tải lên" },
  { value: "effects", label: "Hiệu ứng" },
];

export function MediaBrowserPage() {
  const [category, setCategory] = useState("");
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<MediaEntry | null>(null);

  const handleSelect = (entry: MediaEntry) => {
    setSelected(entry);
    setSelectMode(false);
  };

  return (
    <div className="space-y-6">
      <Header
        title="Quản lý Media"
        subtitle="Duyệt, xem trước và chọn file media từ tất cả thư mục trong dự án."
        action={
          <div className="flex items-center gap-2">
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="h-8 text-xs font-mono rounded-md border border-border bg-background px-2 text-foreground"
              aria-label="Lọc theo loại"
            >
              {CATEGORY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <Button
              variant={selectMode ? "default" : "outline"}
              size="sm"
              onClick={() => setSelectMode(!selectMode)}
            >
              <FolderKanban className="h-4 w-4" />
              {selectMode ? "Đang chọn..." : "Chế độ chọn"}
            </Button>
          </div>
        }
      />

      <MediaBrowser
        category={category}
        onSelect={selectMode ? handleSelect : undefined}
        selectedPath={selected?.path ?? null}
        height="calc(100vh - 260px)"
      />

      {/* Selection result dialog */}
      {selected && !selectMode && (
        <Dialog open={!!selected} onOpenChange={(open) => !open && setSelected(null)}>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle className="truncate">Đã chọn: {selected.name}</DialogTitle>
            </DialogHeader>
            <div className="space-y-2 text-xs font-mono text-muted-foreground">
              <div>Đường dẫn: <span className="text-foreground">{selected.path}</span></div>
              <div>Kích thước: {selected.size > 0 ? `${(selected.size / 1024).toFixed(1)} KB` : "N/A"}</div>
              <div>Loại: {selected.kind}</div>
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="outline" size="sm" onClick={() => setSelected(null)}>
                Đóng
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  navigator.clipboard.writeText(selected.path);
                }}
              >
                Sao chép đường dẫn
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
