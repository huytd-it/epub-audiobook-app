import React, { useEffect, useState } from "react";
import { AudioWaveform, Music, Plus, Edit2, Trash2, Play, Pause, FileAudio, Save } from "lucide-react";
import { api, postForm, AudioProcessResult, MusicItem } from "@/api";
import { AudioStudioDialog } from "@/components/common/AudioStudioDialog";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";

export function MusicPage() {
  const [items, setItems] = useState<MusicItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [playingId, setPlayingId] = useState<number | null>(null);
  const [editingItem, setEditingItem] = useState<MusicItem | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editLicense, setEditLicense] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [studioItem, setStudioItem] = useState<MusicItem | null>(null);
  const [search, setSearch] = useState("");

  const loadData = () => {
    setLoading(true);
    api<{ music: MusicItem[] }>("/music/list")
      .then((res) => setItems(res.music || []))
      .catch((err) => console.error("Lỗi khi tải danh sách nhạc:", err))
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
      await postForm("/music/upload", formData);
      loadData();
    } catch (err: any) {
      alert(`Tải file nhạc lên thất bại: ${err.message}`);
    } finally {
      setIsUploading(false);
      e.target.value = "";
    }
  };

  const handleProcessed = (_result: AudioProcessResult) => {
    // Reload rather than patch locally: a "save as copy" adds a row and an
    // overwrite changes the duration, both of which come from the list.
    loadData();
    setPlayingId(null);
  };

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`Bạn có chắc muốn xóa nhạc "${name}"?`)) return;
    try {
      await postForm(`/music/${id}/delete`, new FormData());
      if (playingId === id) setPlayingId(null);
      loadData();
    } catch (err: any) {
      alert(`Xóa thất bại: ${err.message}`);
    }
  };

  const openEdit = (item: MusicItem) => {
    setEditingItem(item);
    setEditName(item.name);
    setEditDesc(item.description || "");
    setEditLicense(item.license || "");
  };

  const handleSaveEdit = async () => {
    if (!editingItem) return;
    try {
      if (editName.trim() !== editingItem.name) {
        const form = new FormData();
        form.append("name", editName.trim());
        await postForm(`/music/${editingItem.id}/rename`, form);
      }
      const metaForm = new FormData();
      metaForm.append("description", editDesc.trim());
      metaForm.append("license", editLicense.trim());
      await postForm(`/music/${editingItem.id}/metadata`, metaForm);

      setEditingItem(null);
      loadData();
    } catch (err: any) {
      alert(`Lưu thông tin thất bại: ${err.message}`);
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
        title="Thư viện nhạc nền (Music Library)"
        subtitle="Quản lý các bản nhạc nền hòa tấu background audio ghép cùng video audiobook."
        action={
          <label className="cursor-pointer">
            <input
              type="file"
              multiple
              accept="audio/mp3,audio/wav,audio/ogg,audio/m4a"
              className="hidden"
              onChange={handleUpload}
              disabled={isUploading}
            />
            <Button variant="default" size="sm" asChild disabled={isUploading}>
              <span>
                <Plus className="h-4 w-4" />
                {isUploading ? "Đang tải lên..." : "Tải nhạc lên"}
              </span>
            </Button>
          </label>
        }
      />

      <div className="flex items-center justify-between gap-4">
        <Input
          placeholder="Tìm kiếm nhạc nền theo tên hoặc mô tả..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-md"
        />
        <span className="text-xs font-mono text-muted-foreground">Tổng số: {filteredItems.length} file</span>
      </div>

      {loading ? (
        <LoadingState text="Đang tải thư viện nhạc nền..." />
      ) : filteredItems.length === 0 ? (
        <EmptyState text="Chưa có bản nhạc nền nào trong hệ thống." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredItems.map((item) => {
            const isPlaying = playingId === item.id;
            return (
              <Card key={item.id} className="border-border flex flex-col justify-between hover:border-primary/40 transition-colors">
                <CardHeader className="p-4 pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <div className="h-9 w-9 rounded-md bg-primary/10 text-primary flex items-center justify-center shrink-0">
                        <Music className="h-4 w-4" />
                      </div>
                      <div className="min-w-0">
                        <CardTitle className="text-sm font-bold text-foreground truncate" title={item.name}>
                          {item.name}
                        </CardTitle>
                        <span className="text-[11px] font-mono text-muted-foreground">
                          {item.duration_sec ? `${Math.round(item.duration_sec)} giây` : "Chưa rõ độ dài"}
                        </span>
                      </div>
                    </div>

                    <Button
                      variant={isPlaying ? "destructive" : "outline"}
                      size="icon"
                      className="h-8 w-8 shrink-0"
                      onClick={() => setPlayingId(isPlaying ? null : item.id)}
                    >
                      {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
                    </Button>
                  </div>
                </CardHeader>

                <CardContent className="p-4 pt-2 space-y-3 flex-1 flex flex-col justify-between">
                  <div>
                    {item.description && (
                      <p className="text-xs text-muted-foreground line-clamp-2 mb-1">{item.description}</p>
                    )}
                    {item.license && (
                      <span className="inline-block text-[10px] font-mono px-2 py-0.5 rounded bg-muted text-muted-foreground">
                        Bản quyền: {item.license}
                      </span>
                    )}
                  </div>

                  {isPlaying && (
                    <div className="bg-muted/40 p-2 rounded border border-border">
                      <audio
                        src={`/music/${item.id}/file`}
                        controls
                        autoPlay
                        className="w-full h-8"
                        onEnded={() => setPlayingId(null)}
                      />
                    </div>
                  )}

                  <div className="flex items-center justify-end gap-1 pt-2 border-t border-border/50">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => setStudioItem(item)}
                      title="Cắt sửa, chỉnh âm lượng và làm sạch bản nhạc"
                    >
                      <AudioWaveform className="h-3.5 w-3.5" />
                      Xử lý
                    </Button>
                    <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => openEdit(item)}>
                      <Edit2 className="h-3.5 w-3.5" />
                      Sửa
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs text-destructive hover:text-destructive"
                      onClick={() => handleDelete(item.id, item.name)}
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

      {studioItem && (
        <AudioStudioDialog
          target={{
            name: studioItem.name,
            fileUrl: `/music/${studioItem.id}/file`,
            infoUrl: `/music/${studioItem.id}/info`,
            processUrl: `/music/${studioItem.id}/process`,
            hint: "Cắt lấy đoạn nhạc cần dùng, chỉnh âm lượng và fade trong một lượt.",
            overwriteHint: "Các sách đang mix bản nhạc này sẽ nhận bản đã xử lý ngay.",
          }}
          onClose={() => setStudioItem(null)}
          onSaved={handleProcessed}
        />
      )}

      {editingItem && (
        <Dialog open={!!editingItem} onOpenChange={(open) => !open && setEditingItem(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Chỉnh sửa thông tin nhạc</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tên hiển thị</label>
                <Input value={editName} onChange={(e) => setEditName(e.target.value)} />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả</label>
                <Textarea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} placeholder="Nhập ghi chú hoặc thông tin bản nhạc..." />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Giấy phép / License</label>
                <Input value={editLicense} onChange={(e) => setEditLicense(e.target.value)} placeholder="VD: CC-BY, Public Domain..." />
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
