import React, { useEffect, useState } from "react";
import { Image, Plus, Edit2, Trash2, Film, Eye, Save } from "lucide-react";
import { api, postForm, PhotoItem } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";

export function PhotosPage() {
  const [items, setItems] = useState<PhotoItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [search, setSearch] = useState("");
  const [previewingName, setPreviewingName] = useState<string | null>(null);
  const [renamingItem, setRenamingItem] = useState<PhotoItem | null>(null);
  const [newName, setNewName] = useState("");

  const loadData = () => {
    setLoading(true);
    api<{ photos: PhotoItem[] }>("/api/ui/media")
      .then((res) => setItems(res.photos || []))
      .catch((err) => console.error("Lỗi tải hình ảnh:", err))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files || e.target.files.length === 0) return;
    setIsUploading(true);
    const formData = new FormData();
    Array.from(e.target.files).forEach((file) => formData.append("files", file));
    try {
      await postForm("/photos/upload", formData);
      loadData();
    } catch (err: any) {
      alert(`Tải file ảnh/video lên thất bại: ${err.message}`);
    } finally {
      setIsUploading(false);
      e.target.value = "";
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`Bạn có chắc muốn xóa tư liệu hình ảnh/video "${name}"?`)) return;
    const form = new FormData();
    form.append("name", name);
    try {
      await postForm("/photos/delete", form);
      if (previewingName === name) setPreviewingName(null);
      loadData();
    } catch (err: any) {
      alert(`Xóa thất bại: ${err.message}`);
    }
  };

  const openRename = (item: PhotoItem) => {
    setRenamingItem(item);
    setNewName(item.name);
  };

  const handleSaveRename = async () => {
    if (!renamingItem) return;
    if (!newName.trim() || newName.trim() === renamingItem.name) {
      setRenamingItem(null);
      return;
    }
    const form = new FormData();
    form.append("old_name", renamingItem.name);
    form.append("new_name", newName.trim());
    try {
      await postForm("/photos/rename", form);
      setRenamingItem(null);
      loadData();
    } catch (err: any) {
      alert(`Đổi tên thất bại: ${err.message}`);
    }
  };

  const filteredItems = items.filter((i) => i.name.toLowerCase().includes(search.toLowerCase()));

  return (
    <div className="space-y-6">
      <Header
        title="Thư viện hình ảnh & Video nền (Photos & Backgrounds)"
        subtitle="Quản lý kho ảnh minh họa và video lặp lại background dùng cho render video audiobook."
        action={
          <label className="cursor-pointer">
            <input
              type="file"
              multiple
              accept="image/jpeg,image/png,image/webp,video/mp4,video/webm,video/quicktime"
              className="hidden"
              onChange={handleUpload}
              disabled={isUploading}
            />
            <Button variant="default" size="sm" asChild disabled={isUploading}>
              <span>
                <Plus className="h-4 w-4" />
                {isUploading ? "Đang tải lên..." : "Tải ảnh/video lên"}
              </span>
            </Button>
          </label>
        }
      />

      <div className="flex items-center justify-between gap-4">
        <Input
          placeholder="Tìm kiếm tư liệu theo tên..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-md"
        />
        <span className="text-xs font-mono text-muted-foreground">Tổng số: {filteredItems.length} tư liệu</span>
      </div>

      {loading ? (
        <LoadingState text="Đang mở kho hình ảnh..." />
      ) : filteredItems.length === 0 ? (
        <EmptyState text="Chưa có hình ảnh hoặc video background nào." />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          {filteredItems.map((item) => {
            const fileUrl = `/photos/file/${encodeURIComponent(item.name)}`;
            const isVideo = item.is_video;
            return (
              <Card key={item.name} className="border-border overflow-hidden flex flex-col group hover:border-primary/40 transition-colors">
                <div className="aspect-video bg-zinc-950 relative overflow-hidden flex items-center justify-center border-b border-border">
                  {isVideo ? (
                    <video src={fileUrl} className="w-full h-full object-cover" muted loop autoPlay />
                  ) : (
                    <img src={fileUrl} alt={item.name} className="w-full h-full object-cover" loading="lazy" />
                  )}
                  <div className="absolute top-2 right-2 flex items-center gap-1">
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-black/70 text-white backdrop-blur-xs flex items-center gap-1">
                      {isVideo ? <Film className="h-3 w-3 text-lime" /> : <Image className="h-3 w-3 text-primary" />}
                      {isVideo ? "VIDEO" : "ẢNH"}
                    </span>
                  </div>
                </div>

                <CardContent className="p-3 flex-1 flex flex-col justify-between space-y-2">
                  <div>
                    <div className="font-semibold text-xs text-foreground truncate" title={item.name}>
                      {item.name}
                    </div>
                    <div className="text-[10px] font-mono text-muted-foreground">
                      {(item.size / 1024 / 1024).toFixed(2)} MB
                    </div>
                  </div>

                  <div className="flex items-center justify-between gap-1 pt-2 border-t border-border/50">
                    <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => setPreviewingName(item.name)}>
                      <Eye className="h-3.5 w-3.5" />
                      Xem
                    </Button>
                    <div className="flex items-center gap-1">
                      <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openRename(item)}>
                        <Edit2 className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-destructive hover:text-destructive"
                        onClick={() => handleDelete(item.name)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Preview Modal */}
      {previewingName && (
        <Dialog open={!!previewingName} onOpenChange={(open) => !open && setPreviewingName(null)}>
          <DialogContent className="max-w-3xl">
            <DialogHeader>
              <DialogTitle className="truncate">{previewingName}</DialogTitle>
            </DialogHeader>
            <div className="flex items-center justify-center p-2 bg-black rounded border border-border">
              {items.find((i) => i.name === previewingName)?.is_video ? (
                <video src={`/photos/file/${encodeURIComponent(previewingName)}`} controls autoPlay className="max-h-[60vh] w-full" />
              ) : (
                <img src={`/photos/file/${encodeURIComponent(previewingName)}`} alt={previewingName} className="max-h-[60vh] object-contain" />
              )}
            </div>
          </DialogContent>
        </Dialog>
      )}

      {/* Rename Modal */}
      {renamingItem && (
        <Dialog open={!!renamingItem} onOpenChange={(open) => !open && setRenamingItem(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Đổi tên file tư liệu</DialogTitle>
            </DialogHeader>
            <div className="py-2">
              <label className="text-xs font-semibold text-foreground mb-1 block">Tên mới</label>
              <Input value={newName} onChange={(e) => setNewName(e.target.value)} />
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setRenamingItem(null)}>
                Hủy
              </Button>
              <Button variant="default" onClick={handleSaveRename}>
                <Save className="h-4 w-4" />
                Lưu tên mới
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
