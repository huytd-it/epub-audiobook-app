import React, { useEffect, useState } from "react";
import { Image, Plus, Edit2, Trash2, Film, Eye, Save, Folder, FolderInput, FolderPlus } from "lucide-react";
import { api, postForm, PhotoItem } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";

type PhotoFolder = { name: string; count: number };
type PhotoPage = { items: PhotoItem[]; total: number; root_count: number; folders: PhotoFolder[] };

/** Thư viện có thể tới hàng nghìn file nên tải theo trang từ server. */
const PAGE_SIZE = 48;

export function PhotosPage() {
  const [items, setItems] = useState<PhotoItem[]>([]);
  const [total, setTotal] = useState(0);
  const [rootCount, setRootCount] = useState(0);
  const [folders, setFolders] = useState<PhotoFolder[]>([]);
  const [page, setPage] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  // "" là thư mục gốc của thư viện; còn lại là tên sub folder đang mở.
  const [folder, setFolder] = useState("");
  const [loading, setLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [search, setSearch] = useState("");
  const [previewingName, setPreviewingName] = useState<string | null>(null);
  const [renamingItem, setRenamingItem] = useState<PhotoItem | null>(null);
  const [newName, setNewName] = useState("");
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [movingItem, setMovingItem] = useState<PhotoItem | null>(null);
  const [moveTarget, setMoveTarget] = useState("");

  const loadData = () => setReloadKey((key) => key + 1);

  // Debounce nhẹ để gõ tìm kiếm không gửi request theo từng ký tự.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({
        folder,
        q: search,
        offset: String(page * PAGE_SIZE),
        limit: String(PAGE_SIZE),
      });
      api<PhotoPage>(`/photos/list?${params}`)
        .then((res) => {
          if (cancelled) return;
          // Xóa/chuyển hết file của trang cuối thì lùi về trang còn dữ liệu.
          if (!res.items.length && res.total > 0 && page > 0) {
            setPage(Math.max(0, Math.ceil(res.total / PAGE_SIZE) - 1));
            return;
          }
          setItems(res.items || []);
          setTotal(res.total);
          setRootCount(res.root_count);
          setFolders(res.folders || []);
        })
        .catch((err) => console.error("Lỗi tải hình ảnh:", err))
        .finally(() => !cancelled && setLoading(false));
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [folder, search, page, reloadKey]);

  const openFolder = (name: string) => {
    setFolder(name);
    setPage(0);
  };

  const currentFolder = folders.find((item) => item.name === folder);
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const fileUrl = (name: string) =>
    `/photos/file/${encodeURIComponent(name)}${folder ? `?folder=${encodeURIComponent(folder)}` : ""}`;

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files || e.target.files.length === 0) return;
    setIsUploading(true);
    const formData = new FormData();
    Array.from(e.target.files).forEach((file) => formData.append("files", file));
    formData.append("folder", folder);
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
    form.append("folder", folder);
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
    form.append("folder", folder);
    try {
      await postForm("/photos/rename", form);
      setRenamingItem(null);
      loadData();
    } catch (err: any) {
      alert(`Đổi tên thất bại: ${err.message}`);
    }
  };

  const handleCreateFolder = async () => {
    if (!folderName.trim()) return;
    const form = new FormData();
    form.append("name", folderName.trim());
    try {
      const created = await postForm<{ name: string }>("/photos/folders/create", form);
      setCreatingFolder(false);
      setFolderName("");
      openFolder(created.name);
      loadData();
    } catch (err: any) {
      alert(`Tạo thư mục thất bại: ${err.message}`);
    }
  };

  const handleDeleteFolder = async () => {
    if (!currentFolder) return;
    if (
      !confirm(
        `Xóa thư mục "${currentFolder.name}" cùng ${currentFolder.count} file bên trong? Các sách đang chọn thư mục này sẽ bỏ chọn.`
      )
    )
      return;
    const form = new FormData();
    form.append("name", currentFolder.name);
    try {
      await postForm("/photos/folders/delete", form);
      openFolder("");
      loadData();
    } catch (err: any) {
      alert(`Xóa thư mục thất bại: ${err.message}`);
    }
  };

  const openMove = (item: PhotoItem) => {
    setMovingItem(item);
    setMoveTarget(folder ? "" : folders[0]?.name || "");
  };

  const handleMove = async () => {
    if (!movingItem) return;
    const form = new FormData();
    form.append("name", movingItem.name);
    form.append("folder", folder);
    form.append("target", moveTarget);
    try {
      await postForm("/photos/move", form);
      setMovingItem(null);
      loadData();
    } catch (err: any) {
      alert(`Chuyển thư mục thất bại: ${err.message}`);
    }
  };

  const moveTargets = ["", ...folders.map((item) => item.name)].filter((name) => name !== folder);

  return (
    <div className="space-y-6">
      <Header
        title="Thư viện hình ảnh & Video nền (Photos & Backgrounds)"
        subtitle="Quản lý kho ảnh minh họa và video lặp lại background dùng cho render video audiobook."
        action={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setCreatingFolder(true)}>
              <FolderPlus className="h-4 w-4" />
              Tạo thư mục
            </Button>
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
                  {isUploading ? "Đang tải lên..." : folder ? `Tải lên "${folder}"` : "Tải ảnh/video lên"}
                </span>
              </Button>
            </label>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        {[{ name: "", label: "Thư mục gốc", count: rootCount }, ...folders.map((item) => ({ name: item.name, label: item.name, count: item.count }))].map(
          (item) => (
            <Button
              key={item.name}
              variant={folder === item.name ? "default" : "outline"}
              size="sm"
              onClick={() => openFolder(item.name)}
            >
              <Folder className="h-3.5 w-3.5" />
              {item.label}
              <span className="font-mono text-[10px] opacity-70">{item.count}</span>
            </Button>
          )
        )}
        {currentFolder && (
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto text-destructive hover:text-destructive"
            onClick={handleDeleteFolder}
          >
            <Trash2 className="h-3.5 w-3.5" />
            Xóa thư mục
          </Button>
        )}
      </div>

      <div className="flex items-center justify-between gap-4">
        <Input
          placeholder="Tìm kiếm tư liệu theo tên..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          className="max-w-md"
        />
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-muted-foreground">Tổng số: {total} tư liệu</span>
          {pageCount > 1 && (
            <>
              <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Trước
              </Button>
              <span className="text-xs font-mono text-muted-foreground">
                {page + 1}/{pageCount}
              </span>
              <Button variant="outline" size="sm" disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>
                Sau
              </Button>
            </>
          )}
        </div>
      </div>

      {loading ? (
        <LoadingState text="Đang mở kho hình ảnh..." />
      ) : items.length === 0 ? (
        <EmptyState text={folder ? "Thư mục này chưa có hình ảnh hoặc video." : "Chưa có hình ảnh hoặc video background nào."} />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          {items.map((item) => {
            const isVideo = item.is_video;
            return (
              <Card key={item.name} className="border-border overflow-hidden flex flex-col group hover:border-primary/40 transition-colors">
                <div className="aspect-video bg-zinc-950 relative overflow-hidden flex items-center justify-center border-b border-border">
                  {isVideo ? (
                    // Chỉ phát khi rê chuột: cả trang video tự phát cùng lúc sẽ làm treo trình duyệt.
                    <video
                      src={fileUrl(item.name)}
                      className="w-full h-full object-cover"
                      muted
                      loop
                      preload="metadata"
                      onMouseEnter={(event) => void event.currentTarget.play().catch(() => undefined)}
                      onMouseLeave={(event) => event.currentTarget.pause()}
                    />
                  ) : (
                    <img src={fileUrl(item.name)} alt={item.name} className="w-full h-full object-cover" loading="lazy" />
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
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        title="Chuyển sang thư mục khác"
                        disabled={!moveTargets.length}
                        onClick={() => openMove(item)}
                      >
                        <FolderInput className="h-3.5 w-3.5" />
                      </Button>
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
                <video src={fileUrl(previewingName)} controls autoPlay className="max-h-[60vh] w-full" />
              ) : (
                <img src={fileUrl(previewingName)} alt={previewingName} className="max-h-[60vh] object-contain" />
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

      {/* Create Folder Modal */}
      {creatingFolder && (
        <Dialog open={creatingFolder} onOpenChange={(open) => !open && setCreatingFolder(false)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Tạo thư mục con</DialogTitle>
            </DialogHeader>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void handleCreateFolder();
              }}
            >
              <div className="py-2">
                <label className="text-xs font-semibold text-foreground mb-1 block">Tên thư mục</label>
                <Input autoFocus value={folderName} onChange={(e) => setFolderName(e.target.value)} />
                <p className="mt-1.5 text-[11px] text-muted-foreground">
                  Chọn thư mục này trong Cấu hình Video để dùng toàn bộ ảnh/video bên trong làm background.
                </p>
              </div>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setCreatingFolder(false)}>
                  Hủy
                </Button>
                <Button type="submit" variant="default" disabled={!folderName.trim()}>
                  <FolderPlus className="h-4 w-4" />
                  Tạo thư mục
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      )}

      {/* Move Modal */}
      {movingItem && (
        <Dialog open={!!movingItem} onOpenChange={(open) => !open && setMovingItem(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="truncate">Chuyển "{movingItem.name}"</DialogTitle>
            </DialogHeader>
            <div className="py-2">
              <label className="text-xs font-semibold text-foreground mb-1 block">Thư mục đích</label>
              <select
                className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm"
                value={moveTarget}
                onChange={(e) => setMoveTarget(e.target.value)}
              >
                {moveTargets.map((name) => (
                  <option key={name} value={name}>
                    {name || "Thư mục gốc"}
                  </option>
                ))}
              </select>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setMovingItem(null)}>
                Hủy
              </Button>
              <Button variant="default" onClick={handleMove}>
                <FolderInput className="h-4 w-4" />
                Chuyển
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
