import React, { useEffect, useState } from "react";
import { Mic, Plus, Edit2, Trash2, Play, Pause, Save } from "lucide-react";
import { api, postForm, postJson, VoiceItem } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";

export function VoicesPage() {
  const [items, setItems] = useState<VoiceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [playingName, setPlayingName] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [search, setSearch] = useState("");
  const [editingItem, setEditingItem] = useState<VoiceItem | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");

  const loadData = () => {
    setLoading(true);
    api<{ voices: VoiceItem[] }>("/api/ui/media")
      .then((res) => setItems(res.voices || []))
      .catch((err) => console.error("Lỗi tải danh sách voice:", err))
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
      await postForm("/voices/upload", formData);
      loadData();
    } catch (err: any) {
      alert(`Tải file giọng mẫu thất bại: ${err.message}`);
    } finally {
      setIsUploading(false);
      e.target.value = "";
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`Bạn có chắc muốn xóa giọng mẫu "${name}"?`)) return;
    const form = new FormData();
    form.append("name", name);
    try {
      await postForm("/voices/delete", form);
      if (playingName === name) setPlayingName(null);
      loadData();
    } catch (err: any) {
      alert(`Xóa giọng mẫu thất bại: ${err.message}`);
    }
  };

  const openEdit = (item: VoiceItem) => {
    setEditingItem(item);
    setEditName(item.name);
    setEditDesc(item.description || "");
  };

  const handleSaveEdit = async () => {
    if (!editingItem) return;
    try {
      if (editName.trim() !== editingItem.name) {
        const renameForm = new FormData();
        renameForm.append("old_name", editingItem.name);
        renameForm.append("new_name", editName.trim());
        await postForm("/voices/rename", renameForm);
      }
      await postJson(`/voices/${encodeURIComponent(editName.trim())}/description`, {
        description: editDesc.trim(),
      });

      setEditingItem(null);
      loadData();
    } catch (err: any) {
      alert(`Lưu thông tin giọng mẫu thất bại: ${err.message}`);
    }
  };

  const filteredItems = items.filter(
    (i) =>
      i.name.toLowerCase().includes(search.toLowerCase()) ||
      (i.description && i.description.toLowerCase().includes(search.toLowerCase()))
  );

  return (
    <div className="space-y-6">
      <Header
        title="Thư viện giọng mẫu Voice Studio"
        subtitle="Quản lý các đoạn audio voice clip làm mẫu sinh giọng đọc AI (Cloning / TTS)."
        action={
          <label className="cursor-pointer">
            <input
              type="file"
              multiple
              accept="audio/wav,audio/mp3,audio/m4a,audio/ogg"
              className="hidden"
              onChange={handleUpload}
              disabled={isUploading}
            />
            <Button variant="default" size="sm" asChild disabled={isUploading}>
              <span>
                <Plus className="h-4 w-4" />
                {isUploading ? "Đang tải lên..." : "Tải voice mẫu lên"}
              </span>
            </Button>
          </label>
        }
      />

      <div className="flex items-center justify-between gap-4">
        <Input
          placeholder="Tìm kiếm giọng mẫu theo tên hoặc mô tả..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-md"
        />
        <span className="text-xs font-mono text-muted-foreground">Tổng số: {filteredItems.length} giọng</span>
      </div>

      {loading ? (
        <LoadingState text="Đang tải thư viện giọng mẫu..." />
      ) : filteredItems.length === 0 ? (
        <EmptyState text="Chưa có voice clip mẫu nào trong thư viện." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredItems.map((item) => {
            const isPlaying = playingName === item.name;
            return (
              <Card key={item.name} className="border-border flex flex-col justify-between hover:border-primary/40 transition-colors">
                <CardHeader className="p-4 pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <div className="h-9 w-9 rounded-md bg-lime/10 text-lime flex items-center justify-center shrink-0">
                        <Mic className="h-4 w-4" />
                      </div>
                      <div className="min-w-0">
                        <CardTitle className="text-sm font-bold text-foreground truncate" title={item.name}>
                          {item.name}
                        </CardTitle>
                        <span className="text-[11px] font-mono text-muted-foreground">
                          {(item.size / 1024 / 1024).toFixed(2)} MB
                        </span>
                      </div>
                    </div>

                    <Button
                      variant={isPlaying ? "destructive" : "outline"}
                      size="icon"
                      className="h-8 w-8 shrink-0"
                      onClick={() => setPlayingName(isPlaying ? null : item.name)}
                    >
                      {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
                    </Button>
                  </div>
                </CardHeader>

                <CardContent className="p-4 pt-2 space-y-3 flex-1 flex flex-col justify-between">
                  <div>
                    {item.description ? (
                      <p className="text-xs text-muted-foreground line-clamp-2">{item.description}</p>
                    ) : (
                      <span className="text-[11px] font-mono text-muted-foreground italic">Chưa có mô tả giọng</span>
                    )}
                  </div>

                  {isPlaying && (
                    <div className="bg-muted/40 p-2 rounded border border-border">
                      <audio
                        src={`/voices/file/${encodeURIComponent(item.name)}`}
                        controls
                        autoPlay
                        className="w-full h-8"
                        onEnded={() => setPlayingName(null)}
                      />
                    </div>
                  )}

                  <div className="flex items-center justify-end gap-1 pt-2 border-t border-border/50">
                    <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => openEdit(item)}>
                      <Edit2 className="h-3.5 w-3.5" />
                      Sửa
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs text-destructive hover:text-destructive"
                      onClick={() => handleDelete(item.name)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Xóa
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {editingItem && (
        <Dialog open={!!editingItem} onOpenChange={(open) => !open && setEditingItem(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Chỉnh sửa giọng mẫu</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tên file giọng mẫu</label>
                <Input value={editName} onChange={(e) => setEditName(e.target.value)} />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả đặc tính giọng đọc</label>
                <Textarea
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  placeholder="VD: Giọng nam Miền Nam ấm, truyền cảm, tốc độ vừa phải..."
                />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditingItem(null)}>
                Hủy
              </Button>
              <Button variant="default" onClick={handleSaveEdit}>
                <Save className="h-4 w-4" />
                Lưu thay đổi
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
