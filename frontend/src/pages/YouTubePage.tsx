import React, { useEffect, useState } from "react";
import {
  Video,
  Upload,
  PlaySquare,
  Key,
  Trash2,
  RotateCcw,
  Plus,
  ExternalLink,
  Copy,
  Check,
  ListVideo,
  Layers,
  ArrowUpDown,
  FileVideo,
} from "lucide-react";
import {
  api,
  postForm,
  postJson,
  del,
  YouTubeUploadItem,
  PlaylistItem,
  PlaylistItemDetail,
  ChannelVideo,
} from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

export function YouTubePage() {
  const [activeTab, setActiveTab] = useState<"uploads" | "playlists" | "upload_form">("uploads");
  const [uploads, setUploads] = useState<YouTubeUploadItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  
  // Kaggle credentials modal
  const [showKaggleCreds, setShowKaggleCreds] = useState(false);
  const [kaggleCreds, setKaggleCreds] = useState<string>("");
  const [copiedCreds, setCopiedCreds] = useState(false);

  // Manual upload form
  const [uploadMode, setUploadMode] = useState<"path" | "file">("path");
  const [videoPath, setVideoPath] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [privacyStatus, setPrivacyStatus] = useState("private");
  const [playlistId, setPlaylistId] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Playlists tab state
  const [playlists, setPlaylists] = useState<PlaylistItem[]>([]);
  const [selectedPlaylistId, setSelectedPlaylistId] = useState<string>("");
  const [playlistItems, setPlaylistItems] = useState<PlaylistItemDetail[]>([]);
  const [loadingPlaylistItems, setLoadingPlaylistItems] = useState(false);
  const [channelVideos, setChannelVideos] = useState<ChannelVideo[]>([]);
  const [showAddVideosModal, setShowAddVideosModal] = useState(false);
  const [selectedAddVideoIds, setSelectedAddVideoIds] = useState<string[]>([]);
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [sortMode, setSortMode] = useState<"natural" | "episode">("natural");

  const loadUploads = () => {
    setLoading(true);
    api<{ uploads: YouTubeUploadItem[] }>("/youtube/uploads")
      .then((res) => setUploads(res.uploads || []))
      .catch((err) => console.error("Lỗi tải lịch sử upload YouTube:", err))
      .finally(() => setLoading(false));
  };

  const loadPlaylists = () => {
    api<{ items: PlaylistItem[] }>("/youtube/api/playlists")
      .then((res) => {
        setPlaylists(res.items || []);
        if (res.items && res.items.length > 0 && !selectedPlaylistId) {
          setSelectedPlaylistId(res.items[0].id);
        }
      })
      .catch((err) => console.error("Lỗi tải danh sách playlist:", err));
  };

  useEffect(() => {
    loadUploads();
    loadPlaylists();
  }, []);

  useEffect(() => {
    if (!selectedPlaylistId) return;
    setLoadingPlaylistItems(true);
    api<{ items: PlaylistItemDetail[] }>(`/youtube/api/playlists/${selectedPlaylistId}/items?fetch_all=true`)
      .then((res) => setPlaylistItems(res.items || []))
      .catch((err) => console.error("Lỗi tải mục trong playlist:", err))
      .finally(() => setLoadingPlaylistItems(false));
  }, [selectedPlaylistId]);

  const handleFetchKaggleCreds = async () => {
    try {
      const res = await api<any>("/youtube/kaggle-credentials");
      setKaggleCreds(JSON.stringify(res, null, 2));
      setShowKaggleCreds(true);
    } catch (err: any) {
      alert(`Không thể lấy credentials Kaggle: ${err.message}`);
    }
  };

  const handleCopyCreds = () => {
    navigator.clipboard.writeText(kaggleCreds);
    setCopiedCreds(true);
    setTimeout(() => setCopiedCreds(false), 2000);
  };

  const handleSingleDelete = async (id: number) => {
    if (!confirm("Bạn có chắc muốn xóa lịch sử upload này?")) return;
    try {
      await del(`/youtube/uploads/${id}`);
      loadUploads();
    } catch (err: any) {
      alert(`Xóa thất bại: ${err.message}`);
    }
  };

  const handleBulkDelete = async () => {
    if (selectedIds.length === 0) return;
    if (!confirm(`Xóa ${selectedIds.length} lịch sử upload đã chọn?`)) return;
    try {
      await postJson("/youtube/uploads/bulk-delete", selectedIds);
      setSelectedIds([]);
      loadUploads();
    } catch (err: any) {
      alert(`Xóa hàng loạt thất bại: ${err.message}`);
    }
  };

  const handleBulkRetry = async () => {
    if (selectedIds.length === 0) return;
    try {
      await postJson("/youtube/uploads/bulk-retry", selectedIds);
      setSelectedIds([]);
      loadUploads();
    } catch (err: any) {
      alert(`Thử lại hàng loạt thất bại: ${err.message}`);
    }
  };

  const handleManualUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return alert("Vui lòng nhập tiêu đề video");
    setIsSubmitting(true);
    try {
      if (uploadMode === "path") {
        if (!videoPath.trim()) return alert("Vui lòng nhập đường dẫn file video trên đĩa");
        const form = new FormData();
        form.append("video_path", videoPath.trim());
        form.append("title", title.trim());
        form.append("description", description);
        form.append("tags", tags);
        form.append("privacy_status", privacyStatus);
        form.append("playlist_id", playlistId);
        await postForm("/youtube/upload", form);
      } else {
        if (!uploadFile) return alert("Vui lòng chọn file video từ máy tính");
        const form = new FormData();
        form.append("file", uploadFile);
        form.append("title", title.trim());
        form.append("description", description);
        form.append("tags", tags);
        form.append("privacy_status", privacyStatus);
        form.append("playlist_id", playlistId);
        await postForm("/youtube/upload-file", form);
      }

      alert("Đã thêm video vào hàng đợi upload YouTube!");
      setTitle("");
      setDescription("");
      setVideoPath("");
      setUploadFile(null);
      setActiveTab("uploads");
      loadUploads();
    } catch (err: any) {
      alert(`Lỗi upload video: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  // Playlist action handlers
  const openAddVideos = async () => {
    try {
      const res = await api<{ items: ChannelVideo[] }>("/youtube/api/channel/videos?max_results=50");
      setChannelVideos(res.items || []);
      setSelectedAddVideoIds([]);
      setShowAddVideosModal(true);
    } catch (err: any) {
      alert(`Lỗi tải danh sách video trên kênh: ${err.message}`);
    }
  };

  const handleAddVideosToPlaylist = async () => {
    if (!selectedPlaylistId || selectedAddVideoIds.length === 0) return;
    try {
      await postJson(`/youtube/api/playlists/${selectedPlaylistId}/items`, {
        video_ids: selectedAddVideoIds,
      });
      setShowAddVideosModal(false);
      // reload playlist items
      const res = await api<{ items: PlaylistItemDetail[] }>(
        `/youtube/api/playlists/${selectedPlaylistId}/items?fetch_all=true`
      );
      setPlaylistItems(res.items || []);
    } catch (err: any) {
      alert(`Thêm video vào playlist thất bại: ${err.message}`);
    }
  };

  const handleRemovePlaylistItem = async (itemId: string) => {
    if (!confirm("Bạn có chắc muốn xóa video này khỏi danh sách phát?")) return;
    try {
      await del(`/youtube/api/playlist-items/${itemId}`);
      setPlaylistItems((prev) => prev.filter((i) => i.playlist_item_id !== itemId));
    } catch (err: any) {
      alert(`Xóa khỏi playlist thất bại: ${err.message}`);
    }
  };

  const handleApplySort = async () => {
    if (!selectedPlaylistId) return;
    try {
      await postJson(`/youtube/api/playlists/${selectedPlaylistId}/sort/apply`, {
        direction: sortDirection,
        mode: sortMode,
      });
      alert("Đã sắp xếp lại danh sách phát!");
      const res = await api<{ items: PlaylistItemDetail[] }>(
        `/youtube/api/playlists/${selectedPlaylistId}/items?fetch_all=true`
      );
      setPlaylistItems(res.items || []);
    } catch (err: any) {
      alert(`Sắp xếp danh sách phát thất bại: ${err.message}`);
    }
  };

  return (
    <div className="space-y-6">
      <Header
        title="YouTube Studio & Tải tự động"
        subtitle="Đẩy video audiobook thành phẩm lên kênh YouTube, quản lý tiến trình và đồng bộ danh sách phát (Playlists)."
        action={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => window.open("/youtube/connect", "_blank")}>
              <ExternalLink className="h-4 w-4" />
              Kết nối Tài khoản YouTube
            </Button>
            <Button variant="secondary" size="sm" onClick={handleFetchKaggleCreds}>
              <Key className="h-4 w-4" />
              Kaggle Secret
            </Button>
          </div>
        }
      />

      {/* Control Room Navigation Tabs */}
      <div className="flex items-center gap-2 border-b border-border pb-2">
        <Button
          variant={activeTab === "uploads" ? "default" : "ghost"}
          size="sm"
          onClick={() => setActiveTab("uploads")}
        >
          <Video className="h-4 w-4" />
          Lịch sử & Tiến trình Upload ({uploads.length})
        </Button>
        <Button
          variant={activeTab === "upload_form" ? "default" : "ghost"}
          size="sm"
          onClick={() => setActiveTab("upload_form")}
        >
          <Upload className="h-4 w-4" />
          Tải video mới lên
        </Button>
        <Button
          variant={activeTab === "playlists" ? "default" : "ghost"}
          size="sm"
          onClick={() => setActiveTab("playlists")}
        >
          <PlaySquare className="h-4 w-4" />
          Quản lý Playlist ({playlists.length})
        </Button>
      </div>

      {/* TAB 1: UPLOADS HISTORY */}
      {activeTab === "uploads" && (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs font-mono text-muted-foreground">
              Đã chọn: {selectedIds.length} mục
            </div>
            {selectedIds.length > 0 && (
              <div className="flex items-center gap-2">
                <Button variant="outline" size="sm" onClick={handleBulkRetry}>
                  <RotateCcw className="h-3.5 w-3.5" />
                  Thử lại các mục thất bại
                </Button>
                <Button variant="destructive" size="sm" onClick={handleBulkDelete}>
                  <Trash2 className="h-3.5 w-3.5" />
                  Xóa lịch sử đã chọn
                </Button>
              </div>
            )}
          </div>

          {loading ? (
            <LoadingState text="Đang nạp danh sách upload YouTube..." />
          ) : uploads.length === 0 ? (
            <EmptyState text="Chưa có video nào trong lịch sử upload YouTube." />
          ) : (
            <Card className="border-border">
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-muted/50 border-b border-border font-mono text-muted-foreground">
                      <tr>
                        <th className="p-3 w-10 text-center">
                          <input
                            type="checkbox"
                            checked={selectedIds.length === uploads.length && uploads.length > 0}
                            onChange={(e) =>
                              setSelectedIds(e.target.checked ? uploads.map((u) => u.id) : [])
                            }
                          />
                        </th>
                        <th className="p-3 font-semibold">Tiêu đề video</th>
                        <th className="p-3 font-semibold">Trạng thái</th>
                        <th className="p-3 font-semibold">Chế độ</th>
                        <th className="p-3 font-semibold">Thời gian tạo</th>
                        <th className="p-3 font-semibold text-right">Thao tác</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {uploads.map((u) => {
                        const isSelected = selectedIds.includes(u.id);
                        return (
                          <tr key={u.id} className="hover:bg-muted/30 transition-colors">
                            <td className="p-3 text-center">
                              <input
                                type="checkbox"
                                checked={isSelected}
                                onChange={(e) =>
                                  setSelectedIds((prev) =>
                                    e.target.checked ? [...prev, u.id] : prev.filter((id) => id !== u.id)
                                  )
                                }
                              />
                            </td>
                            <td className="p-3 font-medium text-foreground">
                              <div className="font-bold truncate max-w-md">{u.title}</div>
                              <div className="text-[10px] font-mono text-muted-foreground truncate max-w-md">
                                {u.video_path}
                              </div>
                              {u.youtube_video_id && (
                                <a
                                  href={`https://youtu.be/${u.youtube_video_id}`}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-[11px] text-primary hover:underline inline-flex items-center gap-1 mt-0.5"
                                >
                                  Xem trên YouTube ({u.youtube_video_id})
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              )}
                              {u.error_message && (
                                <div className="text-[11px] text-destructive mt-0.5">{u.error_message}</div>
                              )}
                            </td>
                            <td className="p-3">
                              <StatusBadge value={u.status} />
                            </td>
                            <td className="p-3 font-mono text-muted-foreground uppercase">{u.privacy_status}</td>
                            <td className="p-3 font-mono text-muted-foreground whitespace-nowrap">
                              {new Date(u.created_at).toLocaleString("vi-VN")}
                            </td>
                            <td className="p-3 text-right">
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7 text-destructive hover:text-destructive"
                                onClick={() => handleSingleDelete(u.id)}
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* TAB 2: MANUAL UPLOAD FORM */}
      {activeTab === "upload_form" && (
        <Card className="border-border max-w-2xl mx-auto">
          <CardHeader>
            <CardTitle className="text-base font-bold flex items-center gap-2">
              <Upload className="h-4 w-4 text-primary" />
              Đẩy Video lên YouTube
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-4 border-b border-border pb-3">
              <label className="flex items-center gap-2 text-xs font-semibold cursor-pointer">
                <input
                  type="radio"
                  name="uploadMode"
                  checked={uploadMode === "path"}
                  onChange={() => setUploadMode("path")}
                />
                Nhập đường dẫn file MP4 trên đĩa server
              </label>
              <label className="flex items-center gap-2 text-xs font-semibold cursor-pointer">
                <input
                  type="radio"
                  name="uploadMode"
                  checked={uploadMode === "file"}
                  onChange={() => setUploadMode("file")}
                />
                Tải file MP4 trực tiếp từ máy tính
              </label>
            </div>

            <form onSubmit={handleManualUpload} className="space-y-4">
              {uploadMode === "path" ? (
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">
                    Đường dẫn file video trên đĩa (Path) *
                  </label>
                  <Input
                    placeholder="VD: D:\epub-audiobook-app\data\videos\sample.mp4"
                    value={videoPath}
                    onChange={(e) => setVideoPath(e.target.value)}
                    required
                  />
                </div>
              ) : (
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Chọn file MP4 *</label>
                  <Input
                    type="file"
                    accept="video/mp4,video/x-m4v,video/*"
                    onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
                    required
                  />
                </div>
              )}

              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tiêu đề Video *</label>
                <Input
                  placeholder="Nhập tiêu đề phát sóng trên YouTube..."
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  required
                />
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả video (Description)</label>
                <Textarea
                  placeholder="Nhập nội dung mô tả, danh sách chương, credits..."
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Thẻ Tags (phân cách dấu phẩy)</label>
                  <Input
                    placeholder="sach noi, audiobooks, truyen doc..."
                    value={tags}
                    onChange={(e) => setTags(e.target.value)}
                  />
                </div>

                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Quyền riêng tư (Privacy)</label>
                  <select
                    className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-xs"
                    value={privacyStatus}
                    onChange={(e) => setPrivacyStatus(e.target.value)}
                  >
                    <option value="private">Riêng tư (Private)</option>
                    <option value="unlisted">Không công khai (Unlisted)</option>
                    <option value="public">Công khai (Public)</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Danh sách phát (Playlist ID)</label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-xs"
                  value={playlistId}
                  onChange={(e) => setPlaylistId(e.target.value)}
                >
                  <option value="">-- Không đưa vào Playlist --</option>
                  {playlists.map((pl) => (
                    <option key={pl.id} value={pl.id}>
                      {pl.title} ({pl.itemCount} item)
                    </option>
                  ))}
                </select>
              </div>

              <Button variant="default" className="w-full font-bold" type="submit" disabled={isSubmitting}>
                <Upload className="h-4 w-4" />
                {isSubmitting ? "Đang đưa vào hàng đợi..." : "Đưa vào hàng đợi Upload"}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      {/* TAB 3: PLAYLISTS MANAGEMENT */}
      {activeTab === "playlists" && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {/* Playlist selector */}
          <Card className="border-border md:col-span-1">
            <CardHeader>
              <CardTitle className="text-sm font-bold flex items-center justify-between">
                <span>DANH SÁCH PLAYLIST</span>
                <span className="text-xs font-mono text-muted-foreground">{playlists.length}</span>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-2 space-y-1">
              {playlists.length === 0 ? (
                <EmptyState text="Chưa có playlist nào trên kênh." />
              ) : (
                playlists.map((pl) => (
                  <button
                    key={pl.id}
                    onClick={() => setSelectedPlaylistId(pl.id)}
                    className={`w-full text-left p-2.5 rounded-md text-xs transition-colors flex items-center justify-between ${
                      selectedPlaylistId === pl.id
                        ? "bg-primary text-primary-foreground font-semibold"
                        : "hover:bg-muted text-foreground"
                    }`}
                  >
                    <div className="truncate pr-2">
                      <div className="truncate font-bold">{pl.title}</div>
                      <div className="text-[10px] opacity-80 font-mono">{pl.privacy}</div>
                    </div>
                    <span className="text-[11px] font-mono px-1.5 py-0.5 rounded bg-black/20 shrink-0">
                      {pl.itemCount} video
                    </span>
                  </button>
                ))
              )}
            </CardContent>
          </Card>

          {/* Playlist Items */}
          <Card className="border-border md:col-span-2">
            <CardHeader className="flex flex-row items-center justify-between pb-3 border-b border-border">
              <CardTitle className="text-sm font-bold flex items-center gap-2">
                <ListVideo className="h-4 w-4 text-primary" />
                {playlists.find((p) => p.id === selectedPlaylistId)?.title || "Chi tiết Playlist"}
              </CardTitle>
              {selectedPlaylistId && (
                <div className="flex items-center gap-2">
                  <Button variant="outline" size="sm" onClick={openAddVideos}>
                    <Plus className="h-3.5 w-3.5" />
                    Thêm video
                  </Button>
                </div>
              )}
            </CardHeader>
            <CardContent className="p-4 space-y-4">
              {/* Sort Controls */}
              {selectedPlaylistId && (
                <div className="flex items-center justify-between gap-3 p-3 bg-muted/30 rounded border border-border text-xs">
                  <div className="flex items-center gap-2">
                    <ArrowUpDown className="h-3.5 w-3.5 text-primary" />
                    <span className="font-semibold">Sắp xếp:</span>
                    <select
                      value={sortDirection}
                      onChange={(e) => setSortDirection(e.target.value as any)}
                      className="h-7 rounded border border-input bg-background px-2 text-xs"
                    >
                      <option value="asc">Tăng dần (A-Z)</option>
                      <option value="desc">Giảm dần (Z-A)</option>
                    </select>
                    <select
                      value={sortMode}
                      onChange={(e) => setSortMode(e.target.value as any)}
                      className="h-7 rounded border border-input bg-background px-2 text-xs"
                    >
                      <option value="natural">Số tự nhiên</option>
                      <option value="episode">Theo tập (Episode)</option>
                    </select>
                  </div>
                  <Button variant="secondary" size="sm" className="h-7 text-xs" onClick={handleApplySort}>
                    Áp dụng sắp xếp
                  </Button>
                </div>
              )}

              {loadingPlaylistItems ? (
                <LoadingState text="Đang nạp các video trong playlist..." />
              ) : playlistItems.length === 0 ? (
                <EmptyState text="Playlist này chưa có video nào." />
              ) : (
                <div className="divide-y divide-border border border-border rounded-md overflow-hidden">
                  {playlistItems.map((item, idx) => (
                    <div key={item.playlist_item_id} className="p-3 flex items-center justify-between gap-3 text-xs hover:bg-muted/20">
                      <span className="font-mono text-muted-foreground font-bold w-6 shrink-0 text-center">
                        #{idx + 1}
                      </span>
                      {item.thumbnail ? (
                        <img src={item.thumbnail} alt="" className="w-16 h-10 object-cover rounded shrink-0 border border-border" />
                      ) : (
                        <div className="w-16 h-10 bg-zinc-900 rounded shrink-0 flex items-center justify-center text-muted-foreground">
                          <FileVideo className="h-4 w-4" />
                        </div>
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="font-semibold text-foreground truncate">{item.title}</div>
                        <div className="text-[10px] font-mono text-muted-foreground">
                          ID: {item.video_id}
                        </div>
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-destructive hover:text-destructive shrink-0"
                        onClick={() => handleRemovePlaylistItem(item.playlist_item_id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Add Videos Modal */}
      {showAddVideosModal && (
        <Dialog open={showAddVideosModal} onOpenChange={setShowAddVideosModal}>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle>Thêm Video trên Kênh vào Playlist</DialogTitle>
            </DialogHeader>
            <div className="max-h-[50vh] overflow-y-auto space-y-2 py-2">
              {channelVideos.length === 0 ? (
                <EmptyState text="Không tìm thấy video nào trên kênh." />
              ) : (
                channelVideos.map((v) => {
                  const isChecked = selectedAddVideoIds.includes(v.video_id);
                  return (
                    <label
                      key={v.video_id}
                      className={`flex items-center gap-3 p-2.5 rounded border text-xs cursor-pointer transition-colors ${
                        isChecked ? "bg-primary/10 border-primary" : "border-border hover:bg-muted/30"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={(e) =>
                          setSelectedAddVideoIds((prev) =>
                            e.target.checked ? [...prev, v.video_id] : prev.filter((id) => id !== v.video_id)
                          )
                        }
                      />
                      {v.thumbnail && (
                        <img src={v.thumbnail} alt="" className="w-14 h-9 object-cover rounded border border-border shrink-0" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="font-bold truncate">{v.title}</div>
                        <div className="text-[10px] font-mono text-muted-foreground">{v.video_id}</div>
                      </div>
                    </label>
                  );
                })
              )}
            </div>
            <div className="flex items-center justify-between pt-2 border-t border-border">
              <span className="text-xs font-mono text-muted-foreground">Đã chọn: {selectedAddVideoIds.length} video</span>
              <div className="flex items-center gap-2">
                <Button variant="outline" onClick={() => setShowAddVideosModal(false)}>
                  Hủy
                </Button>
                <Button variant="default" onClick={handleAddVideosToPlaylist} disabled={selectedAddVideoIds.length === 0}>
                  Thêm vào Playlist
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      )}

      {/* Kaggle Credentials Modal */}
      {showKaggleCreds && (
        <Dialog open={showKaggleCreds} onOpenChange={setShowKaggleCreds}>
          <DialogContent className="max-w-xl">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Key className="h-4 w-4 text-primary" />
                Mã Credentials cho Kaggle Secret (YOUTUBE_CREDS)
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <p className="text-xs text-muted-foreground">
                Sao chép chuỗi JSON bên dưới và dán vào Kaggle Secret có tên <code className="bg-muted px-1.5 py-0.5 rounded font-mono text-foreground">YOUTUBE_CREDS</code> để Notebook tự động đẩy MP4 lên YouTube.
              </p>
              <pre className="p-3 bg-zinc-950 text-lime font-mono text-xs rounded border border-border overflow-x-auto max-h-60">
                {kaggleCreds}
              </pre>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setShowKaggleCreds(false)}>
                Đóng
              </Button>
              <Button variant="default" onClick={handleCopyCreds}>
                {copiedCreds ? <Check className="h-4 w-4 text-lime" /> : <Copy className="h-4 w-4" />}
                {copiedCreds ? "Đã sao chép!" : "Sao chép JSON"}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
