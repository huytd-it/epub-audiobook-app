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
  Download,
  FileUp,
  AlertTriangle,
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
  YouTubeImportSummary,
} from "@/api";
import { downloadTextFile, fileStamp, formatOf, parseSheet, toCsv } from "@/lib/tabular";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/** Columns of the playlist-order sheet; `position` drives the imported order. */
const PLAYLIST_COLUMNS = ["position", "playlist_item_id", "video_id", "title"];

const IMPORT_STATUS_LABELS: Record<string, string> = {
  updated: "Đã cập nhật",
  created: "Tạo mới",
  unchanged: "Không đổi",
  skipped: "Bỏ qua",
  error: "Lỗi",
};

const IMPORT_STATUS_CLASSES: Record<string, string> = {
  updated: "text-primary",
  created: "text-lime",
  unchanged: "text-muted-foreground",
  skipped: "text-amber-600",
  error: "text-destructive",
};

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
  const [manualOrders, setManualOrders] = useState<Record<string, string>>({});
  const [loadingPlaylistItems, setLoadingPlaylistItems] = useState(false);
  const [previewingSort, setPreviewingSort] = useState(false);
  const [savingOrder, setSavingOrder] = useState(false);
  const [hasPendingOrder, setHasPendingOrder] = useState(false);
  const [channelVideos, setChannelVideos] = useState<ChannelVideo[]>([]);
  const [showAddVideosModal, setShowAddVideosModal] = useState(false);
  const [selectedAddVideoIds, setSelectedAddVideoIds] = useState<string[]>([]);
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [sortMode, setSortMode] = useState<"natural" | "episode" | "manual">("natural");

  // Import / export of the upload queue (edit the sheet in Excel, push it back)
  const [ioFormat, setIoFormat] = useState<"csv" | "json">("csv");
  const [showImportModal, setShowImportModal] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importMode, setImportMode] = useState<"update" | "upsert">("update");
  const [importSummary, setImportSummary] = useState<YouTubeImportSummary | null>(null);
  const [importBusy, setImportBusy] = useState(false);

  // Bulk update modal
  const [showBulkUpdateModal, setShowBulkUpdateModal] = useState(false);
  const [bulkTitleTemplate, setBulkTitleTemplate] = useState("");
  const [bulkDescriptionTemplate, setBulkDescriptionTemplate] = useState("");
  const [bulkScheduledAt, setBulkScheduledAt] = useState("");
  const [bulkGenerateLabels, setBulkGenerateLabels] = useState(false);
  const [bulkUpdateBusy, setBulkUpdateBusy] = useState(false);

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
      .then((res) => {
        const items = res.items || [];
        setPlaylistItems(items);
        setManualOrders(Object.fromEntries(items.map((item, index) => [item.playlist_item_id, String(index + 1)])));
        setHasPendingOrder(false);
      })
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

  const handleBulkUpdate = () => {
    if (selectedIds.length === 0) return;
    setShowBulkUpdateModal(true);
  };

  const runBulkUpdate = async () => {
    if (selectedIds.length === 0) return;
    setBulkUpdateBusy(true);
    try {
      const result = await postJson<{ updated: number; total: number }>("/youtube/uploads/bulk-update", {
        ids: selectedIds,
        title_template: bulkTitleTemplate,
        description_template: bulkDescriptionTemplate,
        scheduled_publish_at: bulkScheduledAt || null,
        generate_ai_labels: bulkGenerateLabels,
      });
      setShowBulkUpdateModal(false);
      setSelectedIds([]);
      loadUploads();
      alert(`Đã cập nhật ${result.updated}/${result.total} mục.`);
    } catch (err: any) {
      alert(`Cập nhật hàng loạt thất bại: ${err.message}`);
    } finally {
      setBulkUpdateBusy(false);
    }
  };

  // --- Upload queue: export -> edit in a spreadsheet -> import back -------------

  const handleExportUploads = () => {
    const params = new URLSearchParams({ format: ioFormat });
    // Nothing selected means "the whole queue".
    if (selectedIds.length > 0) params.set("ids", selectedIds.join(","));
    window.location.href = `/youtube/uploads/export?${params}`;
  };

  const runImport = async (dryRun: boolean) => {
    if (!importFile) return alert("Vui lòng chọn file JSON hoặc CSV cần nhập");
    setImportBusy(true);
    try {
      const form = new FormData();
      form.append("file", importFile);
      form.append("mode", importMode);
      form.append("dry_run", dryRun ? "true" : "false");
      const summary = await postForm<YouTubeImportSummary>("/youtube/uploads/import", form);
      setImportSummary(summary);
      if (!dryRun) {
        loadUploads();
        setSelectedIds([]);
      }
    } catch (err: any) {
      alert(`Nhập dữ liệu thất bại: ${err.message}`);
    } finally {
      setImportBusy(false);
    }
  };

  const openImportModal = () => {
    setImportFile(null);
    setImportSummary(null);
    setImportMode("update");
    setShowImportModal(true);
  };

  // --- Playlist order: exported and re-imported entirely in the browser --------

  const handleExportPlaylist = () => {
    if (playlistItems.length === 0) return alert("Playlist này chưa có video nào để xuất");
    const playlistName = playlists.find((p) => p.id === selectedPlaylistId)?.title || selectedPlaylistId;
    const rows = playlistItems.map((item, index) => ({
      position: index + 1,
      playlist_item_id: item.playlist_item_id,
      video_id: item.video_id,
      title: item.title,
    }));
    const name = `youtube-playlist-${fileStamp()}.${ioFormat}`;
    if (ioFormat === "csv") {
      downloadTextFile(name, toCsv(rows, PLAYLIST_COLUMNS), "text/csv");
    } else {
      downloadTextFile(
        name,
        JSON.stringify(
          { kind: "youtube_playlist", playlist_id: selectedPlaylistId, playlist_title: playlistName, items: rows },
          null,
          2
        ),
        "application/json"
      );
    }
  };

  const handleImportPlaylistOrder = async (file: File) => {
    try {
      const rows = parseSheet(await file.text(), formatOf(file.name));
      const positioned = rows.every((row) => Number.isFinite(Number(row.position)))
        ? [...rows].sort((a, b) => Number(a.position) - Number(b.position))
        : rows;

      const byItemId = new Map(playlistItems.map((item) => [item.playlist_item_id, item]));
      const byVideoId = new Map(playlistItems.map((item) => [item.video_id, item]));
      const ordered: PlaylistItemDetail[] = [];
      const placed = new Set<string>();
      for (const row of positioned) {
        const key = String(row.playlist_item_id ?? "").trim();
        const videoKey = String(row.video_id ?? "").trim();
        const item = byItemId.get(key) || byVideoId.get(videoKey);
        if (!item || placed.has(item.playlist_item_id)) continue;
        placed.add(item.playlist_item_id);
        ordered.push(item);
      }
      if (ordered.length === 0) {
        return alert("File không khớp video nào trong playlist đang mở (cần cột playlist_item_id hoặc video_id).");
      }
      // Anything the file left out keeps its relative order at the end, so an
      // incomplete sheet can never silently drop videos from the playlist.
      const rest = playlistItems.filter((item) => !placed.has(item.playlist_item_id));
      const next = [...ordered, ...rest];
      setPlaylistItems(next);
      setManualOrders(Object.fromEntries(next.map((item, index) => [item.playlist_item_id, String(index + 1)])));
      setHasPendingOrder(true);
      alert(
        `Đã nạp thứ tự từ file: ${ordered.length} video khớp` +
          (rest.length > 0 ? `, ${rest.length} video không có trong file được xếp xuống cuối` : "") +
          '. Nhấn "Lưu thứ tự" để áp dụng lên YouTube.'
      );
    } catch (err: any) {
      alert(`Không đọc được file thứ tự: ${err.message}`);
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

  const handleManualOrderChange = (itemId: string, value: string) => {
    setManualOrders((prev) => ({ ...prev, [itemId]: value }));
  };

  const handlePreviewManualSort = (itemId: string) => {
    const enteredOrder = Number.parseInt(manualOrders[itemId], 10);
    const currentIndex = playlistItems.findIndex((item) => item.playlist_item_id === itemId);
    if (currentIndex < 0) return;
    if (Number.isNaN(enteredOrder)) {
      setManualOrders((prev) => ({
        ...prev,
        [itemId]: String(currentIndex + 1),
      }));
      return;
    }
    const normalizedOrder = Math.max(1, Math.min(enteredOrder, playlistItems.length));
    const reordered = [...playlistItems];
    const [movedItem] = reordered.splice(currentIndex, 1);
    reordered.splice(normalizedOrder - 1, 0, movedItem);
    setPlaylistItems(reordered);
    setManualOrders(Object.fromEntries(reordered.map((item, index) => [item.playlist_item_id, String(index + 1)])));
    setHasPendingOrder(true);
  };

  const handleSaveOrder = async () => {
    if (!selectedPlaylistId) return;
    setSavingOrder(true);
    try {
      await postJson(`/youtube/api/playlists/${selectedPlaylistId}/reorder-all`, {
        item_ids: playlistItems.map((item) => item.playlist_item_id),
      });
      alert("Đã lưu thứ tự danh sách phát!");
      const res = await api<{ items: PlaylistItemDetail[] }>(
        `/youtube/api/playlists/${selectedPlaylistId}/items?fetch_all=true`
      );
      const items = res.items || [];
      setPlaylistItems(items);
      setManualOrders(Object.fromEntries(items.map((item, index) => [item.playlist_item_id, String(index + 1)])));
      setHasPendingOrder(false);
    } catch (err: any) {
      alert(`Lưu thứ tự danh sách phát thất bại: ${err.message}`);
    } finally {
      setSavingOrder(false);
    }
  };

  const handlePreviewSort = async () => {
    if (!selectedPlaylistId) return;
    setPreviewingSort(true);
    try {
      const res = await postJson<{ items: Array<PlaylistItemDetail & { new_position: number }> }>(
        `/youtube/api/playlists/${selectedPlaylistId}/sort/preview`,
        { direction: sortDirection, mode: sortMode }
      );
      const items = [...(res.items || [])].sort((a, b) => a.new_position - b.new_position);
      setPlaylistItems(items);
      setManualOrders(Object.fromEntries(items.map((item, index) => [item.playlist_item_id, String(index + 1)])));
      setHasPendingOrder(true);
    } catch (err: any) {
      alert(`Xem trước sắp xếp thất bại: ${err.message}`);
    } finally {
      setPreviewingSort(false);
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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs font-mono text-muted-foreground">
              Đã chọn: {selectedIds.length} mục
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {selectedIds.length > 0 && (
                <>
                  <Button variant="outline" size="sm" onClick={handleBulkRetry}>
                    <RotateCcw className="h-3.5 w-3.5" />
                    Thử lại các mục thất bại
                  </Button>
                  <Button variant="destructive" size="sm" onClick={handleBulkDelete}>
                    <Trash2 className="h-3.5 w-3.5" />
                    Xóa lịch sử đã chọn
                  </Button>
                  <Button variant="default" size="sm" onClick={handleBulkUpdate}>
                    <Layers className="h-3.5 w-3.5" />
                    Cập nhật hàng loạt ({selectedIds.length})
                  </Button>
                </>
              )}
              <select
                value={ioFormat}
                onChange={(e) => setIoFormat(e.target.value as any)}
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                aria-label="Định dạng xuất dữ liệu"
              >
                <option value="csv">CSV (Excel)</option>
                <option value="json">JSON</option>
              </select>
              <Button variant="secondary" size="sm" onClick={handleExportUploads}>
                <Download className="h-3.5 w-3.5" />
                {selectedIds.length > 0 ? `Xuất ${selectedIds.length} mục đã chọn` : "Xuất toàn bộ dữ liệu"}
              </Button>
              <Button variant="outline" size="sm" onClick={openImportModal}>
                <FileUp className="h-3.5 w-3.5" />
                Nhập dữ liệu đã sửa
              </Button>
            </div>
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
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" onClick={openAddVideos}>
                    <Plus className="h-3.5 w-3.5" />
                    Thêm video
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleExportPlaylist}
                    disabled={playlistItems.length === 0}
                    title={`Xuất thứ tự playlist ra file ${ioFormat.toUpperCase()} để sửa`}
                  >
                    <Download className="h-3.5 w-3.5" />
                    Xuất thứ tự
                  </Button>
                  <label className="inline-flex items-center gap-1.5 h-8 px-3 rounded-md border border-input text-xs font-medium cursor-pointer hover:bg-muted transition-colors">
                    <FileUp className="h-3.5 w-3.5" />
                    Nhập thứ tự
                    <input
                      type="file"
                      accept=".json,.csv"
                      className="hidden"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file) handleImportPlaylistOrder(file);
                        e.target.value = "";
                      }}
                    />
                  </label>
                </div>
              )}
            </CardHeader>
            <CardContent className="p-4 space-y-4">
              {/* Sort Controls */}
              {selectedPlaylistId && (
                <div className="space-y-3 p-3 bg-muted/30 rounded border border-border text-xs">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <ArrowUpDown className="h-3.5 w-3.5 text-primary" />
                      <span className="font-semibold">Sắp xếp tự động:</span>
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
                        <option value="manual">Thủ công</option>
                        <option value="natural">Số tự nhiên</option>
                        <option value="episode">Theo tập (Episode)</option>
                      </select>
                    </div>
                    <Button
                      variant="secondary"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={handlePreviewSort}
                      disabled={previewingSort}
                    >
                      {previewingSort ? "Đang preview..." : "Xem trước sắp xếp"}
                    </Button>
                  </div>
                  <div className="flex items-center justify-between gap-3 pt-2 border-t border-border/50">
                    <span className="font-semibold">
                      {hasPendingOrder ? "Thứ tự preview chưa được lưu" : "Nhập vị trí rồi nhấn Enter để preview"}
                    </span>
                    <div className="flex gap-2">
                      <Button
                        variant="default"
                        size="sm"
                        className="h-7 text-xs"
                        onClick={handleSaveOrder}
                        disabled={!hasPendingOrder || savingOrder}
                      >
                        {savingOrder ? "Đang lưu..." : "Lưu thứ tự"}
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              {loadingPlaylistItems ? (
                <LoadingState text="Đang nạp các video trong playlist..." />
              ) : playlistItems.length === 0 ? (
                <EmptyState text="Playlist này chưa có video nào." />
              ) : (
                <div className="divide-y divide-border border border-border rounded-md overflow-hidden">
                  {playlistItems.map((item, idx) => {
                    const currentOrder = manualOrders[item.playlist_item_id] ?? (idx + 1);
                    return (
                        <div key={item.playlist_item_id} className="p-3 flex items-center justify-between gap-3 text-xs hover:bg-muted/20">
                          <input
                            type="text"
                            inputMode="numeric"
                            value={currentOrder}
                            onChange={(e) => handleManualOrderChange(item.playlist_item_id, e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault();
                                handlePreviewManualSort(item.playlist_item_id);
                              }
                            }}
                            aria-label={`Vị trí của ${item.title}`}
                            className="w-12 h-7 rounded border border-input bg-background/50 text-center text-xs font-mono font-bold"
                          />
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
                    );
                })}
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

      {/* Upload queue import modal */}
      {showImportModal && (
        <Dialog open={showImportModal} onOpenChange={setShowImportModal}>
          <DialogContent className="max-w-3xl">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <FileUp className="h-4 w-4 text-primary" />
                Nhập dữ liệu upload đã chỉnh sửa
              </DialogTitle>
            </DialogHeader>

            <div className="space-y-3 py-2">
              <p className="text-xs text-muted-foreground">
                Xuất dữ liệu ra JSON/CSV, sửa các cột{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">title</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">description</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">tags</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">privacy_status</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">playlist_id</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">video_path</code> rồi tải lại
                lên đây. Cột <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">id</code> là khóa
                đối chiếu — giữ nguyên, đừng xóa.
              </p>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">File dữ liệu (.json / .csv)</label>
                  <Input
                    type="file"
                    accept=".json,.csv"
                    onChange={(e) => {
                      setImportFile(e.target.files?.[0] || null);
                      setImportSummary(null);
                    }}
                  />
                </div>
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Chế độ nhập</label>
                  <select
                    className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                    value={importMode}
                    onChange={(e) => {
                      setImportMode(e.target.value as any);
                      setImportSummary(null);
                    }}
                  >
                    <option value="update">Chỉ cập nhật bản ghi có sẵn (theo id)</option>
                    <option value="upsert">Cập nhật + tạo mới dòng không có id</option>
                  </select>
                </div>
              </div>

              <div className="flex gap-2 p-2.5 rounded border border-amber-500/30 bg-amber-500/5 text-[11px] text-amber-700">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>
                  Bản ghi đang upload sẽ bị bỏ qua. Video đã lên YouTube chỉ được sửa dữ liệu trong ứng dụng, không đổi
                  metadata trên kênh. Chế độ "tạo mới" sẽ đưa dòng mới vào hàng đợi upload ngay.
                </span>
              </div>

              {importSummary && (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono">
                    <span className="font-semibold">
                      {importSummary.dry_run ? "Xem trước thay đổi" : "Kết quả áp dụng"} ({importSummary.total} dòng):
                    </span>
                    {Object.entries(importSummary.counts)
                      .filter(([, count]) => count > 0)
                      .map(([key, count]) => (
                        <span key={key} className="px-1.5 py-0.5 rounded bg-muted">
                          {IMPORT_STATUS_LABELS[key] || key}: {count}
                        </span>
                      ))}
                  </div>
                  <div className="max-h-[40vh] overflow-y-auto border border-border rounded">
                    <table className="w-full text-left text-[11px]">
                      <thead className="bg-muted/50 border-b border-border font-mono text-muted-foreground sticky top-0">
                        <tr>
                          <th className="p-2 w-12">Dòng</th>
                          <th className="p-2 w-16">ID</th>
                          <th className="p-2 w-28">Trạng thái</th>
                          <th className="p-2">Thay đổi / Ghi chú</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {importSummary.results.map((result) => (
                          <tr key={`${result.row}-${result.id ?? "new"}`} className="align-top">
                            <td className="p-2 font-mono text-muted-foreground">{result.row}</td>
                            <td className="p-2 font-mono">{result.id ?? "-"}</td>
                            <td className={`p-2 font-semibold ${IMPORT_STATUS_CLASSES[result.status] || ""}`}>
                              {IMPORT_STATUS_LABELS[result.status] || result.status}
                            </td>
                            <td className="p-2">
                              {Object.entries(result.changes).map(([field, value]) => (
                                <div key={field} className="font-mono truncate max-w-md">
                                  <span className="text-muted-foreground">{field}:</span>{" "}
                                  {typeof value === "string" ? value : JSON.stringify(value)}
                                </div>
                              ))}
                              {result.message && <div className="text-destructive">{result.message}</div>}
                              {result.warning && <div className="text-amber-600">{result.warning}</div>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>

            <div className="flex items-center justify-between pt-2 border-t border-border">
              <span className="text-[11px] font-mono text-muted-foreground">
                {importFile ? importFile.name : "Chưa chọn file"}
              </span>
              <div className="flex items-center gap-2">
                <Button variant="outline" onClick={() => setShowImportModal(false)}>
                  Đóng
                </Button>
                <Button variant="secondary" onClick={() => runImport(true)} disabled={!importFile || importBusy}>
                  {importBusy ? "Đang xử lý..." : "Xem trước thay đổi"}
                </Button>
                <Button variant="default" onClick={() => runImport(false)} disabled={!importFile || importBusy}>
                  <FileUp className="h-4 w-4" />
                  Áp dụng vào dữ liệu
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      )}

      {/* Bulk Update Modal */}
      {showBulkUpdateModal && (
        <Dialog open={showBulkUpdateModal} onOpenChange={setShowBulkUpdateModal}>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Layers className="h-4 w-4 text-primary" />
                Cập nhật hàng loạt {selectedIds.length} video
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-4 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">
                  Template tiêu đề
                </label>
                <Input
                  value={bulkTitleTemplate}
                  onChange={(e) => setBulkTitleTemplate(e.target.value)}
                  placeholder="VD: Truyện Kể Đêm Khuya - Tập {episode}"
                />
                <p className="text-[10px] text-muted-foreground mt-1">
                  Dùng <code className="bg-muted px-0.5 rounded">{"{episode}"}</code> để chèn số tập tự động.
                </p>
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">
                  Template mô tả
                </label>
                <Textarea
                  value={bulkDescriptionTemplate}
                  onChange={(e) => setBulkDescriptionTemplate(e.target.value)}
                  placeholder="VD: Tập {episode} - Truyện kể đêm khuya hay nhất..."
                  rows={3}
                />
                <p className="text-[10px] text-muted-foreground mt-1">
                  Dùng <code className="bg-muted px-0.5 rounded">{"{episode}"}</code> để chèn số tập tự động.
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">
                    Thời gian đăng (tùy chọn)
                  </label>
                  <Input
                    type="datetime-local"
                    value={bulkScheduledAt}
                    onChange={(e) => setBulkScheduledAt(e.target.value)}
                  />
                  <p className="text-[10px] text-muted-foreground mt-1">
                    Để trống = đăng ngay khi sẵn sàng.
                  </p>
                </div>
                <div className="flex items-end">
                  <label className="flex items-center gap-2 text-xs font-medium">
                    <input
                      type="checkbox"
                      checked={bulkGenerateLabels}
                      onChange={(e) => setBulkGenerateLabels(e.target.checked)}
                      className="rounded border-input"
                    />
                    Tự gán nhãn AI cho video
                  </label>
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-2 border-t border-border pt-3">
              <Button variant="outline" onClick={() => setShowBulkUpdateModal(false)}>
                Hủy
              </Button>
              <Button onClick={runBulkUpdate} disabled={bulkUpdateBusy}>
                {bulkUpdateBusy ? "Đang cập nhật..." : `Áp dụng cho ${selectedIds.length} video`}
              </Button>
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
