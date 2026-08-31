import React, { useEffect, useMemo, useState } from "react";
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
  Layers,
  ArrowUpDown,
  FileVideo,
  Download,
  FileUp,
  AlertTriangle,
  Pencil,
  Search,
  Sparkles,
  ShieldCheck,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
  ListVideo,
  FolderInput,
  X,
  GripVertical,
  Undo2,
} from "lucide-react";
import {
  api,
  postForm,
  postJson,
  patchJson,
  del,
  YouTubeUploadItem,
  YouTubeUploadFilters,
  PlaylistItem,
  PlaylistItemDetail,
  ChannelVideo,
  YouTubeImportSummary,
  CachedChannelVideo,
  CachedChannelVideosResponse,
  ChannelVideoFilters,
  ChannelVideosSyncStatus,
  BatchResult,
} from "@/api";
import { downloadTextFile, fileStamp, formatOf, parseSheet, toCsv } from "@/lib/tabular";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";

/** Columns of the playlist-order sheet; `position` drives the imported order. */
const PLAYLIST_COLUMNS = ["position", "playlist_item_id", "video_id", "title", "description"];

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
  const [activeTab, setActiveTab] = useState<"uploads" | "playlists" | "upload_form" | "channel_videos">("uploads");
  const [uploads, setUploads] = useState<YouTubeUploadItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);

  // Uploads tab: advanced filters + inline title edit
  const [uploadFilters, setUploadFilters] = useState<YouTubeUploadFilters>({
    search: "", status: "", privacy_status: "", has_playlist: "", not_for_kids: "", ai_labels_enabled: "",
    date_from: "", date_to: "",
  });
  const [uploadSearchInput, setUploadSearchInput] = useState("");
  const [editingUploadId, setEditingUploadId] = useState<number | null>(null);
  const [editingUploadTitle, setEditingUploadTitle] = useState("");

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
  const [notForKids, setNotForKids] = useState(true);
  const [aiLabelsEnabled, setAiLabelsEnabled] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Playlists tab state (list-only; a playlist's items are managed on the
  // Videos-kênh tab - see channelFilters.playlist_id / setActivePlaylistId below).
  const [playlists, setPlaylists] = useState<PlaylistItem[]>([]);
  const [playlistSearch, setPlaylistSearch] = useState("");
  const [itemSearch, setItemSearch] = useState("");
  const [editingPlaylist, setEditingPlaylist] = useState<{ id: string; title: string; description: string } | null>(null);
  const [savingPlaylistEdit, setSavingPlaylistEdit] = useState(false);
  const [editingItem, setEditingItem] = useState<{ videoId: string; title: string } | null>(null);
  const [savingItemEdit, setSavingItemEdit] = useState(false);
  const [playlistItems, setPlaylistItems] = useState<PlaylistItemDetail[]>([]);
  const [manualOrders, setManualOrders] = useState<Record<string, string>>({});
  const [loadingPlaylistItems, setLoadingPlaylistItems] = useState(false);
  const [previewingSort, setPreviewingSort] = useState(false);
  const [savingOrder, setSavingOrder] = useState(false);
  const [hasPendingOrder, setHasPendingOrder] = useState(false);
  const [draggedItemId, setDraggedItemId] = useState<string | null>(null);
  /** Snapshot of the on-server order taken right before a local preview/reorder, so
   *  the user can undo and get the untouched playlist back without a reload. */
  const [orderSnapshot, setOrderSnapshot] = useState<PlaylistItemDetail[] | null>(null);
  const [channelVideos, setChannelVideos] = useState<ChannelVideo[]>([]);
  const [showAddVideosModal, setShowAddVideosModal] = useState(false);
  const [selectedAddVideoIds, setSelectedAddVideoIds] = useState<string[]>([]);
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [sortMode, setSortMode] = useState<"natural" | "episode" | "manual">("natural");

  // Playlist list: search/sort/pagination (left panel) + bulk selection + create/delete
  const [playlistSort, setPlaylistSort] = useState<"title" | "itemCount">("title");
  const [playlistSortDir, setPlaylistSortDir] = useState<"asc" | "desc">("asc");
  const [playlistPage, setPlaylistPage] = useState(1);
  const PLAYLIST_PAGE_SIZE = 10;
  const [selectedPlaylistIds, setSelectedPlaylistIds] = useState<string[]>([]);
  const [showCreatePlaylistModal, setShowCreatePlaylistModal] = useState(false);
  const [newPlaylistTitle, setNewPlaylistTitle] = useState("");
  const [newPlaylistDescription, setNewPlaylistDescription] = useState("");
  const [newPlaylistPrivacy, setNewPlaylistPrivacy] = useState("private");
  const [creatingPlaylist, setCreatingPlaylist] = useState(false);
  const [showPlaylistBulkUpdateModal, setShowPlaylistBulkUpdateModal] = useState(false);
  const [playlistBulkPrivacy, setPlaylistBulkPrivacy] = useState("");
  const [playlistBulkTitlePrefix, setPlaylistBulkTitlePrefix] = useState("");
  const [playlistBulkTitleSuffix, setPlaylistBulkTitleSuffix] = useState("");
  const [playlistBulkDescription, setPlaylistBulkDescription] = useState("");
  const [playlistBulkBusy, setPlaylistBulkBusy] = useState(false);

  // Playlist items: multi-select + pagination + copy/move to another playlist
  const [selectedItemIds, setSelectedItemIds] = useState<string[]>([]);
  const [itemsPage, setItemsPage] = useState(1);
  const ITEMS_PAGE_SIZE = 20;
  const [showCopyMoveModal, setShowCopyMoveModal] = useState<{ mode: "copy" | "move" } | null>(null);
  const [copyMoveDestId, setCopyMoveDestId] = useState("");
  const [copyMoveBusy, setCopyMoveBusy] = useState(false);

  // Videos-kênh tab: local cache of every video on the channel + bulk actions
  const [channelVideosCache, setChannelVideosCache] = useState<CachedChannelVideo[]>([]);
  const [channelTotal, setChannelTotal] = useState(0);
  const [channelLoading, setChannelLoading] = useState(false);
  const [channelSyncing, setChannelSyncing] = useState(false);
  const [channelSyncStatus, setChannelSyncStatus] = useState<ChannelVideosSyncStatus>({ count: 0, synced_at: null });
  const [channelPage, setChannelPage] = useState(1);
  const CHANNEL_PAGE_SIZE = 25;
  const [channelFilters, setChannelFilters] = useState<ChannelVideoFilters>({
    search: "", privacy_status: "", has_playlist: "", playlist_id: "", date_from: "", date_to: "",
    sort: "published_at", order: "desc",
  });
  /** The single source of truth for "which playlist's items are in view" - setting it
   *  switches the Videos-kênh tab from the whole-channel cache browser into the
   *  live playlist-items/reorder view for that one playlist. */
  const setActivePlaylistId = (id: string) => setChannelFilters((prev) => ({ ...prev, playlist_id: id }));
  const [channelSearchInput, setChannelSearchInput] = useState("");
  const [channelSelectedIds, setChannelSelectedIds] = useState<string[]>([]);
  const [showChannelBulkUpdateModal, setShowChannelBulkUpdateModal] = useState(false);
  const [channelBulkTitleTemplate, setChannelBulkTitleTemplate] = useState("");
  const [channelBulkDescriptionTemplate, setChannelBulkDescriptionTemplate] = useState("");
  const [channelBulkPrivacy, setChannelBulkPrivacy] = useState("");
  const [channelBulkAddTags, setChannelBulkAddTags] = useState("");
  const [channelBulkBusy, setChannelBulkBusy] = useState(false);
  // Removing from a playlist is handled on the playlist-items panel (its own bulk
  // toolbar); this one only ever adds, since the cache view spans the whole channel.
  const [showChannelAddToPlaylistModal, setShowChannelAddToPlaylistModal] = useState(false);
  const [channelPlaylistTargetId, setChannelPlaylistTargetId] = useState("");
  const [channelPlaylistBusy, setChannelPlaylistBusy] = useState(false);
  const [showChannelDeleteModal, setShowChannelDeleteModal] = useState(false);
  const [channelDeleteConfirmText, setChannelDeleteConfirmText] = useState("");
  const [channelDeleteBusy, setChannelDeleteBusy] = useState(false);

  // Import / export of the upload queue (edit the sheet in Excel, push it back)
  // JSON is the safe default: it round-trips emoji/Vietnamese text exactly, while a
  // CSV re-saved by Excel can silently mangle them (see ioFormat's use in the export
  // buttons and the import-modal hint below).
  const [ioFormat, setIoFormat] = useState<"csv" | "json">("json");
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

  const loadUploads = (filters: YouTubeUploadFilters = uploadFilters) => {
    setLoading(true);
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const qs = params.toString();
    api<{ uploads: YouTubeUploadItem[] }>(`/youtube/uploads${qs ? `?${qs}` : ""}`)
      .then((res) => setUploads(res.uploads || []))
      .catch((err) => console.error("Lỗi tải lịch sử upload YouTube:", err))
      .finally(() => setLoading(false));
  };

  const loadPlaylists = () => {
    api<{ items: PlaylistItem[] }>("/youtube/api/playlists")
      .then((res) => setPlaylists(res.items || []))
      .catch((err) => console.error("Lỗi tải danh sách playlist:", err));
  };

  useEffect(() => {
    loadPlaylists();
  }, []);

  useEffect(() => {
    loadUploads(uploadFilters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadFilters]);

  useEffect(() => {
    const handle = setTimeout(() => {
      setUploadFilters((prev) => (prev.search === uploadSearchInput ? prev : { ...prev, search: uploadSearchInput }));
    }, 300);
    return () => clearTimeout(handle);
  }, [uploadSearchInput]);

  useEffect(() => {
    if (!channelFilters.playlist_id) {
      setPlaylistItems([]);
      return;
    }
    setLoadingPlaylistItems(true);
    api<{ items: PlaylistItemDetail[] }>(`/youtube/api/playlists/${channelFilters.playlist_id}/items?fetch_all=true`)
      .then((res) => {
        const items = res.items || [];
    setPlaylistItems(items);
    setManualOrders(Object.fromEntries(items.map((item, index) => [item.playlist_item_id, String(index + 1)])));
    setHasPendingOrder(false);
    setOrderSnapshot(null);
  })
  .catch((err) => console.error("Lỗi tải mục trong playlist:", err))
  .finally(() => setLoadingPlaylistItems(false));
    setSelectedItemIds([]);
    setItemsPage(1);
  }, [channelFilters.playlist_id]);

  // --- Videos-kênh tab: local cache load + sync ---------------------------------

  const loadChannelVideos = (filters: ChannelVideoFilters = channelFilters, page: number = channelPage) => {
    setChannelLoading(true);
    const params = new URLSearchParams({ page: String(page), page_size: String(CHANNEL_PAGE_SIZE) });
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, String(value));
    });
    api<CachedChannelVideosResponse>(`/youtube/api/channel/videos/cached?${params}`)
      .then((res) => {
        setChannelVideosCache(res.items || []);
        setChannelTotal(res.total || 0);
        setChannelSyncStatus((prev) => ({ ...prev, synced_at: res.synced_at }));
      })
      .catch((err) => console.error("Lỗi tải danh sách video kênh:", err))
      .finally(() => setChannelLoading(false));
  };

  const loadChannelSyncStatus = () => {
    api<ChannelVideosSyncStatus>("/youtube/api/channel/videos/status")
      .then(setChannelSyncStatus)
      .catch((err) => console.error("Lỗi tải trạng thái đồng bộ:", err));
  };

  useEffect(() => {
    loadChannelSyncStatus();
  }, []);

  useEffect(() => {
    if (activeTab !== "channel_videos") return;
    loadChannelVideos(channelFilters, channelPage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, channelFilters, channelPage]);

  useEffect(() => {
    const handle = setTimeout(() => {
      setChannelFilters((prev) => (prev.search === channelSearchInput ? prev : { ...prev, search: channelSearchInput }));
      setChannelPage(1);
    }, 300);
    return () => clearTimeout(handle);
  }, [channelSearchInput]);

  const handleSyncChannelVideos = async () => {
    setChannelSyncing(true);
    try {
      const result = await postJson<{ synced: number; playlists_scanned: number }>(
        "/youtube/api/channel/videos/sync", {}
      );
      alert(`Đã đồng bộ ${result.synced} video trên kênh (quét ${result.playlists_scanned} playlist).`);
      loadChannelVideos();
      loadChannelSyncStatus();
    } catch (err: any) {
      alert(`Đồng bộ thất bại: ${err.message}`);
    } finally {
      setChannelSyncing(false);
    }
  };

  const handleChannelExport = () => {
    const params = new URLSearchParams({ format: ioFormat });
    if (channelSelectedIds.length > 0) params.set("ids", channelSelectedIds.join(","));
    window.location.href = `/youtube/api/channel/videos/export?${params}`;
  };

  const runChannelBulkUpdate = async () => {
    if (channelSelectedIds.length === 0) return;
    setChannelBulkBusy(true);
    try {
      const result = await postJson<BatchResult>("/youtube/api/channel/videos/bulk-update", {
        video_ids: channelSelectedIds,
        title_template: channelBulkTitleTemplate,
        description_template: channelBulkDescriptionTemplate,
        privacy_status: channelBulkPrivacy || null,
        add_tags: channelBulkAddTags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      setShowChannelBulkUpdateModal(false);
      setChannelSelectedIds([]);
      setChannelBulkTitleTemplate("");
      setChannelBulkDescriptionTemplate("");
      setChannelBulkPrivacy("");
      setChannelBulkAddTags("");
      loadChannelVideos();
      alert(`Đã cập nhật ${result.succeeded}/${result.requested} video` + (result.failed ? `, ${result.failed} lỗi` : "") + ".");
    } catch (err: any) {
      alert(`Cập nhật hàng loạt thất bại: ${err.message}`);
    } finally {
      setChannelBulkBusy(false);
    }
  };

  const runChannelAddToPlaylist = async () => {
    if (!channelPlaylistTargetId || channelSelectedIds.length === 0) return;
    setChannelPlaylistBusy(true);
    try {
      const result = await postJson<BatchResult>("/youtube/api/channel/videos/bulk-add-to-playlist", {
        playlist_id: channelPlaylistTargetId,
        video_ids: channelSelectedIds,
      });
      setShowChannelAddToPlaylistModal(false);
      setChannelSelectedIds([]);
      loadChannelVideos();
      loadPlaylists();
      alert(`Đã thêm ${result.succeeded}/${result.requested} video vào playlist` + (result.failed ? `, ${result.failed} lỗi` : "") + ".");
    } catch (err: any) {
      alert(`Thao tác playlist thất bại: ${err.message}`);
    } finally {
      setChannelPlaylistBusy(false);
    }
  };

  const runChannelDelete = async () => {
    if (channelDeleteConfirmText !== "XÓA" || channelSelectedIds.length === 0) return;
    setChannelDeleteBusy(true);
    try {
      const result = await postJson<BatchResult>("/youtube/api/channel/videos/bulk-delete", channelSelectedIds);
      setShowChannelDeleteModal(false);
      setChannelDeleteConfirmText("");
      setChannelSelectedIds([]);
      loadChannelVideos();
      loadChannelSyncStatus();
      loadPlaylists();
      alert(`Đã xóa vĩnh viễn ${result.succeeded}/${result.requested} video khỏi YouTube` +
        (result.failed ? `, ${result.failed} lỗi` : "") + ".");
    } catch (err: any) {
      alert(`Xóa video thất bại: ${err.message}`);
    } finally {
      setChannelDeleteBusy(false);
    }
  };

  /** Playlist tab -> Videos-kênh tab, pre-filtered to this playlist's items. */
  const viewPlaylistInChannelVideos = (playlistId: string) => {
    setActivePlaylistId(playlistId);
    setChannelPage(1);
    setActiveTab("channel_videos");
  };

  // --- Playlist tab: create / delete / bulk-update playlists ---------------------

  const handleCreatePlaylist = async () => {
    if (!newPlaylistTitle.trim()) return;
    setCreatingPlaylist(true);
    try {
      await postJson("/youtube/api/playlists", {
        title: newPlaylistTitle.trim(),
        description: newPlaylistDescription,
        privacy: newPlaylistPrivacy,
      });
      setShowCreatePlaylistModal(false);
      setNewPlaylistTitle("");
      setNewPlaylistDescription("");
      setNewPlaylistPrivacy("private");
      loadPlaylists();
    } catch (err: any) {
      alert(`Tạo playlist thất bại: ${err.message}`);
    } finally {
      setCreatingPlaylist(false);
    }
  };

  const handleDeletePlaylist = async (playlistId: string, title: string) => {
    if (!confirm(`Xóa playlist "${title}"? Video bên trong không bị xóa khỏi kênh.`)) return;
    try {
      await del(`/youtube/api/playlists/${playlistId}`);
      setSelectedPlaylistIds((prev) => prev.filter((id) => id !== playlistId));
      if (channelFilters.playlist_id === playlistId) setActivePlaylistId("");
      loadPlaylists();
    } catch (err: any) {
      alert(`Xóa playlist thất bại: ${err.message}`);
    }
  };

  const runPlaylistBulkUpdate = async () => {
    if (selectedPlaylistIds.length === 0) return;
    setPlaylistBulkBusy(true);
    try {
      const result = await postJson<BatchResult>("/youtube/api/playlists/bulk-update", {
        playlist_ids: selectedPlaylistIds,
        privacy_status: playlistBulkPrivacy || null,
        title_prefix: playlistBulkTitlePrefix,
        title_suffix: playlistBulkTitleSuffix,
        description_template: playlistBulkDescription || null,
      });
      setShowPlaylistBulkUpdateModal(false);
      setSelectedPlaylistIds([]);
      setPlaylistBulkPrivacy("");
      setPlaylistBulkTitlePrefix("");
      setPlaylistBulkTitleSuffix("");
      setPlaylistBulkDescription("");
      loadPlaylists();
      alert(`Đã cập nhật ${result.succeeded}/${result.requested} playlist` + (result.failed ? `, ${result.failed} lỗi` : "") + ".");
    } catch (err: any) {
      alert(`Cập nhật hàng loạt playlist thất bại: ${err.message}`);
    } finally {
      setPlaylistBulkBusy(false);
    }
  };

  // --- Playlist items: bulk remove / copy / move to another playlist -------------

  const handleRemoveSelectedItems = async () => {
    if (selectedItemIds.length === 0 || !channelFilters.playlist_id) return;
    if (!confirm(`Xóa ${selectedItemIds.length} video đã chọn khỏi playlist này?`)) return;
    try {
      await api(`/youtube/api/playlists/${channelFilters.playlist_id}/items`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_ids: selectedItemIds }),
      });
      setPlaylistItems((prev) => prev.filter((i) => !selectedItemIds.includes(i.playlist_item_id)));
      setSelectedItemIds([]);
      loadPlaylists();
    } catch (err: any) {
      alert(`Xóa hàng loạt thất bại: ${err.message}`);
    }
  };

  const runCopyMoveItems = async () => {
    if (!showCopyMoveModal || !copyMoveDestId || selectedItemIds.length === 0 || !channelFilters.playlist_id) return;
    setCopyMoveBusy(true);
    const endpoint = `/youtube/api/playlists/${channelFilters.playlist_id}/${showCopyMoveModal.mode}`;
    try {
      const result = await postJson<BatchResult>(endpoint, {
        dest_playlist_id: copyMoveDestId,
        item_ids: selectedItemIds,
      });
      alert(`Đã xử lý ${result.succeeded}/${result.requested} video` + (result.failed ? `, ${result.failed} lỗi` : "") + ".");
      setShowCopyMoveModal(null);
      setCopyMoveDestId("");
      if (showCopyMoveModal.mode === "move") {
        setPlaylistItems((prev) => prev.filter((i) => !selectedItemIds.includes(i.playlist_item_id)));
      }
      setSelectedItemIds([]);
      loadPlaylists();
    } catch (err: any) {
      alert(`${showCopyMoveModal.mode === "copy" ? "Sao chép" : "Di chuyển"} thất bại: ${err.message}`);
    } finally {
      setCopyMoveBusy(false);
    }
  };

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

  const startEditUploadTitle = (u: YouTubeUploadItem) => {
    setEditingUploadId(u.id);
    setEditingUploadTitle(u.title);
  };

  const saveUploadTitle = async (id: number) => {
    const nextTitle = editingUploadTitle.trim();
    setEditingUploadId(null);
    if (!nextTitle) return;
    const current = uploads.find((u) => u.id === id);
    if (current && current.title === nextTitle) return;
    try {
      await patchJson(`/youtube/uploads/${id}`, { title: nextTitle });
      setUploads((prev) => prev.map((u) => (u.id === id ? { ...u, title: nextTitle } : u)));
    } catch (err: any) {
      alert(`Sửa tiêu đề thất bại: ${err.message}`);
    }
  };

  const toggleUploadNotForKids = async (u: YouTubeUploadItem) => {
    const next = !u.not_for_kids;
    setUploads((prev) => prev.map((row) => (row.id === u.id ? { ...row, not_for_kids: next } : row)));
    try {
      await patchJson(`/youtube/uploads/${u.id}`, { not_for_kids: next });
    } catch (err: any) {
      alert(`Cập nhật thất bại: ${err.message}`);
      setUploads((prev) => prev.map((row) => (row.id === u.id ? { ...row, not_for_kids: u.not_for_kids } : row)));
    }
  };

  const toggleUploadAiLabels = async (u: YouTubeUploadItem) => {
    const next = !u.ai_labels_enabled;
    setUploads((prev) => prev.map((row) => (row.id === u.id ? { ...row, ai_labels_enabled: next } : row)));
    try {
      await patchJson(`/youtube/uploads/${u.id}`, { ai_labels_enabled: next });
      if (next) await postJson(`/youtube/uploads/${u.id}/ai-labels`, {});
    } catch (err: any) {
      alert(`Cập nhật nhãn AI thất bại: ${err.message}`);
      setUploads((prev) => prev.map((row) => (row.id === u.id ? { ...row, ai_labels_enabled: u.ai_labels_enabled } : row)));
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
    const playlistName = playlists.find((p) => p.id === channelFilters.playlist_id)?.title || channelFilters.playlist_id;
    const rows = playlistItems.map((item, index) => ({
      position: index + 1,
      playlist_item_id: item.playlist_item_id,
      video_id: item.video_id,
      title: item.title,
      description: item.description,
    }));
    const name = `youtube-playlist-${fileStamp()}.${ioFormat}`;
    if (ioFormat === "csv") {
      downloadTextFile(name, toCsv(rows, PLAYLIST_COLUMNS), "text/csv");
    } else {
      downloadTextFile(
        name,
        JSON.stringify(
          { kind: "youtube_playlist", playlist_id: channelFilters.playlist_id, playlist_title: playlistName, items: rows },
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
        form.append("not_for_kids", String(notForKids));
        form.append("ai_labels_enabled", String(aiLabelsEnabled));
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
        form.append("not_for_kids", String(notForKids));
        form.append("ai_labels_enabled", String(aiLabelsEnabled));
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
  const filteredPlaylists = useMemo(
    () => playlists.filter((p) => p.title.toLowerCase().includes(playlistSearch.toLowerCase())),
    [playlists, playlistSearch]
  );

  const sortedPlaylists = useMemo(() => {
    const dir = playlistSortDir === "asc" ? 1 : -1;
    return [...filteredPlaylists].sort((a, b) =>
      playlistSort === "itemCount" ? (a.itemCount - b.itemCount) * dir : a.title.localeCompare(b.title) * dir
    );
  }, [filteredPlaylists, playlistSort, playlistSortDir]);

  const playlistPageCount = Math.max(1, Math.ceil(sortedPlaylists.length / PLAYLIST_PAGE_SIZE));
  const paginatedPlaylists = useMemo(
    () => sortedPlaylists.slice((playlistPage - 1) * PLAYLIST_PAGE_SIZE, playlistPage * PLAYLIST_PAGE_SIZE),
    [sortedPlaylists, playlistPage]
  );
  useEffect(() => setPlaylistPage(1), [playlistSearch, playlistSort, playlistSortDir]);
  // Clamp back onto a valid page after a delete/filter shrinks the result set out from
  // under the current page (e.g. deleting the last playlist on the last page).
  useEffect(() => setPlaylistPage((p) => Math.min(p, playlistPageCount)), [playlistPageCount]);

  const filteredPlaylistItems = useMemo(() => {
    const q = itemSearch.trim().toLowerCase();
    if (!q) return playlistItems;
    return playlistItems.filter(
      (item) => item.title.toLowerCase().includes(q) || item.video_id.toLowerCase().includes(q)
    );
  }, [playlistItems, itemSearch]);

  const itemsPageCount = Math.max(1, Math.ceil(filteredPlaylistItems.length / ITEMS_PAGE_SIZE));
  const paginatedPlaylistItems = useMemo(
    () => filteredPlaylistItems.slice((itemsPage - 1) * ITEMS_PAGE_SIZE, itemsPage * ITEMS_PAGE_SIZE),
    [filteredPlaylistItems, itemsPage]
  );
  useEffect(() => setItemsPage(1), [itemSearch]);
  useEffect(() => setItemsPage((p) => Math.min(p, itemsPageCount)), [itemsPageCount]);

  const playlistTitleById = useMemo(
    () => Object.fromEntries(playlists.map((p) => [p.id, p.title])),
    [playlists]
  );

  const channelPageCount = Math.max(1, Math.ceil(channelTotal / CHANNEL_PAGE_SIZE));
  useEffect(() => setChannelPage((p) => Math.min(p, channelPageCount)), [channelPageCount]);

  const fmtDuration = (sec?: number | null) => {
    if (!sec && sec !== 0) return "-";
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  };

  const hasChannelFilters = !!(channelFilters.search || channelFilters.privacy_status || channelFilters.has_playlist ||
    channelFilters.playlist_id || channelFilters.date_from || channelFilters.date_to);

  const clearChannelFilters = () => {
    setChannelSearchInput("");
    setChannelFilters({ search: "", privacy_status: "", has_playlist: "", playlist_id: "", date_from: "", date_to: "",
      sort: channelFilters.sort, order: channelFilters.order });
    setChannelPage(1);
  };

  const saveEditedPlaylist = async () => {
    if (!editingPlaylist || !editingPlaylist.title.trim()) return;
    setSavingPlaylistEdit(true);
    try {
      await patchJson(`/youtube/api/playlists/${editingPlaylist.id}`, {
        title: editingPlaylist.title.trim(),
        description: editingPlaylist.description,
      });
      setEditingPlaylist(null);
      loadPlaylists();
    } catch (err: any) {
      alert(`Sửa playlist thất bại: ${err.message}`);
    } finally {
      setSavingPlaylistEdit(false);
    }
  };

  const saveEditedItemTitle = async () => {
    if (!editingItem || !editingItem.title.trim()) return;
    setSavingItemEdit(true);
    try {
      await patchJson(`/youtube/api/videos/${editingItem.videoId}`, { title: editingItem.title.trim() });
      setPlaylistItems((prev) =>
        prev.map((item) => (item.video_id === editingItem.videoId ? { ...item, title: editingItem.title.trim() } : item))
      );
      setEditingItem(null);
    } catch (err: any) {
      alert(`Sửa tiêu đề video thất bại: ${err.message}`);
    } finally {
      setSavingItemEdit(false);
    }
  };

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
    if (!channelFilters.playlist_id || selectedAddVideoIds.length === 0) return;
    try {
      await postJson(`/youtube/api/playlists/${channelFilters.playlist_id}/items`, {
        video_ids: selectedAddVideoIds,
      });
      setShowAddVideosModal(false);
      // reload playlist items
      const res = await api<{ items: PlaylistItemDetail[] }>(
        `/youtube/api/playlists/${channelFilters.playlist_id}/items?fetch_all=true`
      );
      setPlaylistItems(res.items || []);
      loadPlaylists();
    } catch (err: any) {
      alert(`Thêm video vào playlist thất bại: ${err.message}`);
    }
  };

  const handleRemovePlaylistItem = async (itemId: string) => {
    if (!confirm("Bạn có chắc muốn xóa video này khỏi danh sách phát?")) return;
    try {
      await del(`/youtube/api/playlist-items/${itemId}`);
      setPlaylistItems((prev) => prev.filter((i) => i.playlist_item_id !== itemId));
      loadPlaylists();
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
    applyLocalOrder(reordered);
  };

  const handleDragReorder = (targetItemId: string) => {
    if (!draggedItemId || draggedItemId === targetItemId) {
      setDraggedItemId(null);
      return;
    }
    const from = playlistItems.findIndex((item) => item.playlist_item_id === draggedItemId);
    const to = playlistItems.findIndex((item) => item.playlist_item_id === targetItemId);
    if (from < 0 || to < 0) return setDraggedItemId(null);
    const reordered = [...playlistItems];
    const [movedItem] = reordered.splice(from, 1);
    reordered.splice(to, 0, movedItem);
    applyLocalOrder(reordered);
  };

  /** Mutate the local order state and remember the previous order for Undo. */
  const applyLocalOrder = (reordered: PlaylistItemDetail[]) => {
    setOrderSnapshot((prev) => prev ?? playlistItems);
    setPlaylistItems(reordered);
    setManualOrders(Object.fromEntries(reordered.map((item, index) => [item.playlist_item_id, String(index + 1)])));
    setHasPendingOrder(true);
  };

  const handleUndoOrder = () => {
    if (!orderSnapshot) return;
    const restored = [...orderSnapshot];
    setPlaylistItems(restored);
    setManualOrders(Object.fromEntries(restored.map((item, index) => [item.playlist_item_id, String(index + 1)])));
    setOrderSnapshot(null);
    setHasPendingOrder(false);
  };

  const handleSaveOrder = async () => {
    if (!channelFilters.playlist_id) return;
    setSavingOrder(true);
    try {
      await postJson(`/youtube/api/playlists/${channelFilters.playlist_id}/reorder-all`, {
        item_ids: playlistItems.map((item) => item.playlist_item_id),
      });
      alert("Đã lưu thứ tự danh sách phát!");
      const res = await api<{ items: PlaylistItemDetail[] }>(
        `/youtube/api/playlists/${channelFilters.playlist_id}/items?fetch_all=true`
      );
      const items = res.items || [];
      setPlaylistItems(items);
      setManualOrders(Object.fromEntries(items.map((item, index) => [item.playlist_item_id, String(index + 1)])));
      setHasPendingOrder(false);
      setOrderSnapshot(null);
    } catch (err: any) {
      alert(`Lưu thứ tự danh sách phát thất bại: ${err.message}`);
    } finally {
      setSavingOrder(false);
    }
  };

  const handlePreviewSort = async () => {
    if (!channelFilters.playlist_id) return;
    setPreviewingSort(true);
    try {
      const res = await postJson<{ items: Array<PlaylistItemDetail & { new_position: number }> }>(
        `/youtube/api/playlists/${channelFilters.playlist_id}/sort/preview`,
        { direction: sortDirection, mode: sortMode }
      );
      const items = [...(res.items || [])].sort((a, b) => a.new_position - b.new_position);
      applyLocalOrder(items);
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
        <Button
          variant={activeTab === "channel_videos" ? "default" : "ghost"}
          size="sm"
          onClick={() => setActiveTab("channel_videos")}
        >
          <ListVideo className="h-4 w-4" />
          Video kênh ({channelSyncStatus.count})
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

          {/* Advanced filters */}
          <div className="flex flex-wrap items-end gap-2 p-3 bg-muted/30 rounded border border-border text-xs">
            <div className="relative min-w-56 flex-1">
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Tìm kiếm</label>
              <Search className="absolute left-2.5 top-[1.65rem] h-3.5 w-3.5 text-muted-foreground" />
              <Input
                className="h-8 pl-8 text-xs"
                placeholder="Tiêu đề hoặc mô tả..."
                value={uploadSearchInput}
                onChange={(e) => setUploadSearchInput(e.target.value)}
              />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Trạng thái</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={uploadFilters.status}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, status: e.target.value }))}
              >
                <option value="">Tất cả</option>
                <option value="pending">Chờ xử lý</option>
                <option value="uploading">Đang tải lên</option>
                <option value="done">Hoàn tất</option>
                <option value="failed">Thất bại</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Quyền riêng tư</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={uploadFilters.privacy_status}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, privacy_status: e.target.value }))}
              >
                <option value="">Tất cả</option>
                <option value="private">Riêng tư</option>
                <option value="unlisted">Không công khai</option>
                <option value="public">Công khai</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Playlist</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={uploadFilters.has_playlist}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, has_playlist: e.target.value }))}
              >
                <option value="">Tất cả</option>
                <option value="no">Chưa có playlist</option>
                <option value="yes">Đã có playlist</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Trẻ em</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={uploadFilters.not_for_kids}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, not_for_kids: e.target.value }))}
              >
                <option value="">Tất cả</option>
                <option value="1">Không dành cho trẻ em</option>
                <option value="0">Dành cho trẻ em</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Nhãn AI</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={uploadFilters.ai_labels_enabled}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, ai_labels_enabled: e.target.value }))}
              >
                <option value="">Tất cả</option>
                <option value="1">Đã bật</option>
                <option value="0">Chưa bật</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Từ ngày</label>
              <Input
                type="date"
                className="h-8 text-xs"
                value={uploadFilters.date_from}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, date_from: e.target.value }))}
              />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Đến ngày</label>
              <Input
                type="date"
                className="h-8 text-xs"
                value={uploadFilters.date_to}
                onChange={(e) => setUploadFilters((prev) => ({ ...prev, date_to: e.target.value }))}
              />
            </div>
            {(uploadFilters.search || uploadFilters.status || uploadFilters.privacy_status || uploadFilters.has_playlist ||
              uploadFilters.not_for_kids || uploadFilters.ai_labels_enabled || uploadFilters.date_from || uploadFilters.date_to) && (
              <Button
                variant="ghost"
                size="sm"
                className="h-8 text-xs"
                onClick={() => {
                  setUploadSearchInput("");
                  setUploadFilters({
                    search: "", status: "", privacy_status: "", has_playlist: "", not_for_kids: "",
                    ai_labels_enabled: "", date_from: "", date_to: "",
                  });
                }}
              >
                Xóa bộ lọc
              </Button>
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
                        <th className="p-3 font-semibold">Playlist</th>
                        <th className="p-3 font-semibold">Trẻ em</th>
                        <th className="p-3 font-semibold">Nhãn AI</th>
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
                              {editingUploadId === u.id ? (
                                <Input
                                  autoFocus
                                  className="h-7 text-xs font-bold"
                                  value={editingUploadTitle}
                                  onChange={(e) => setEditingUploadTitle(e.target.value)}
                                  onBlur={() => saveUploadTitle(u.id)}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") saveUploadTitle(u.id);
                                    if (e.key === "Escape") setEditingUploadId(null);
                                  }}
                                />
                              ) : (
                                <div
                                  className="font-bold truncate max-w-md cursor-text inline-flex items-center gap-1.5 group"
                                  onClick={() => startEditUploadTitle(u)}
                                  title="Nhấn để sửa tiêu đề"
                                >
                                  {u.title}
                                  <Pencil className="h-3 w-3 opacity-0 group-hover:opacity-60 shrink-0" />
                                </div>
                              )}
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
                            <td className="p-3 font-mono text-muted-foreground">
                              {u.playlist_id ? u.playlist_id : <span className="text-amber-600">Chưa có</span>}
                            </td>
                            <td className="p-3">
                              <button
                                type="button"
                                onClick={() => toggleUploadNotForKids(u)}
                                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                  u.not_for_kids ? "bg-muted text-muted-foreground" : "bg-amber-500/15 text-amber-700"
                                }`}
                                title="Nhấn để đổi"
                              >
                                <ShieldCheck className="h-3 w-3" />
                                {u.not_for_kids ? "Không dành cho trẻ em" : "Dành cho trẻ em"}
                              </button>
                            </td>
                            <td className="p-3">
                              <button
                                type="button"
                                onClick={() => toggleUploadAiLabels(u)}
                                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                  u.ai_labels_enabled ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground"
                                }`}
                                title="Nhấn để bật/tắt"
                              >
                                <Sparkles className="h-3 w-3" />
                                {u.ai_labels_enabled ? "Đã bật" : "Chưa bật"}
                              </button>
                            </td>
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

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <label className="flex items-center gap-2 text-xs font-semibold cursor-pointer">
                  <input
                    type="checkbox"
                    checked={notForKids}
                    onChange={(e) => setNotForKids(e.target.checked)}
                    className="rounded border-input"
                  />
                  Không dành cho trẻ em
                </label>
                <label className="flex items-center gap-2 text-xs font-semibold cursor-pointer">
                  <input
                    type="checkbox"
                    checked={aiLabelsEnabled}
                    onChange={(e) => setAiLabelsEnabled(e.target.checked)}
                    className="rounded border-input"
                  />
                  Tự bật nhãn AI (tự sinh tag từ tiêu đề/mô tả)
                </label>
              </div>

              <Button variant="default" className="w-full font-bold" type="submit" disabled={isSubmitting}>
                <Upload className="h-4 w-4" />
                {isSubmitting ? "Đang đưa vào hàng đợi..." : "Đưa vào hàng đợi Upload"}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      {/* TAB 3: PLAYLISTS MANAGEMENT - playlists only. A playlist's videos, order and
          episode re-sort live on the Videos-kênh tab (click a row, or the list icon,
          to open them there filtered to this playlist). */}
      {activeTab === "playlists" && (
        <div className="w-full">
          <Card className="border-border overflow-hidden">
            <CardHeader className="space-y-3 pb-3 border-b border-border bg-muted/20">
              {/* Tiêu đề + nút thao tác cùng hàng */}
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2.5 min-w-0">
                  <div className="h-8 w-8 rounded-md bg-primary/10 flex items-center justify-center shrink-0">
                    <PlaySquare className="h-4 w-4 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <CardTitle className="text-sm font-bold leading-none flex items-center gap-2">
                      DANH SÁCH PLAYLIST
                      <span className="inline-flex items-center justify-center min-w-7 h-5 px-1.5 rounded-full bg-primary text-primary-foreground text-[11px] font-mono font-bold">
                        {playlists.length}
                      </span>
                    </CardTitle>
                    <div className="text-[11px] font-mono text-muted-foreground mt-0.5">
                      {sortedPlaylists.length !== playlists.length
                        ? `Đang lọc ${sortedPlaylists.length}/${playlists.length} playlist`
                        : `${playlists.length} playlist trên kênh`}
                      {selectedPlaylistIds.length > 0 ? ` · Đã chọn ${selectedPlaylistIds.length}` : ""}
                    </div>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2 shrink-0">
                  <Button variant="outline" size="sm" className="h-8 text-xs font-semibold" onClick={() => setShowCreatePlaylistModal(true)}>
                    <Plus className="h-3.5 w-3.5" />
                    Tạo playlist
                  </Button>
                  {selectedPlaylistIds.length > 0 && (
                    <Button variant="default" size="sm" className="h-8 text-xs font-semibold" onClick={() => setShowPlaylistBulkUpdateModal(true)}>
                      <Layers className="h-3.5 w-3.5" />
                      Cập nhật hàng loạt ({selectedPlaylistIds.length})
                    </Button>
                  )}
                </div>
              </div>
              {/* Tìm kiếm + sắp xếp cùng hàng, full body */}
              <div className="flex flex-col sm:flex-row gap-2">
                <div className="relative flex-1 min-w-0">
                  <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
                  <Input
                    className="h-9 pl-9 text-sm"
                    placeholder="Tìm playlist theo tên, mô tả hoặc ID..."
                    value={playlistSearch}
                    onChange={(e) => setPlaylistSearch(e.target.value)}
                  />
                </div>
                <select
                  value={`${playlistSort}:${playlistSortDir}`}
                  onChange={(e) => {
                    const [s, d] = e.target.value.split(":");
                    setPlaylistSort(s as any);
                    setPlaylistSortDir(d as any);
                  }}
                  className="h-9 rounded-md border border-input bg-background px-3 text-xs font-medium shrink-0 sm:w-48"
                  aria-label="Sắp xếp playlist"
                >
                  <option value="title:asc">Tên A → Z</option>
                  <option value="title:desc">Tên Z → A</option>
                  <option value="itemCount:desc">Nhiều video nhất</option>
                  <option value="itemCount:asc">Ít video nhất</option>
                </select>
              </div>
            </CardHeader>
            <CardContent className="p-0">
              {playlists.length === 0 ? (
                <div className="p-6">
                  <EmptyState text="Chưa có playlist nào trên kênh." />
                </div>
              ) : paginatedPlaylists.length === 0 ? (
                <div className="p-8 text-center text-sm text-muted-foreground">Không tìm thấy playlist nào khớp với bộ lọc.</div>
              ) : (
                <>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <thead className="bg-muted/50 border-b border-border font-mono text-[11px] text-muted-foreground">
                        <tr>
                          <th className="p-3 w-9 text-center">
                            <input
                              type="checkbox"
                              aria-label="Chọn tất cả playlist trên trang"
                              checked={paginatedPlaylists.length > 0 && paginatedPlaylists.every((p) => selectedPlaylistIds.includes(p.id))}
                              onChange={(e) => {
                                const ids = paginatedPlaylists.map((p) => p.id);
                                setSelectedPlaylistIds((prev) =>
                                  e.target.checked ? Array.from(new Set([...prev, ...ids])) : prev.filter((id) => !ids.includes(id))
                                );
                              }}
                            />
                          </th>
                          <th className="p-3 w-20 font-semibold">Ảnh bìa</th>
                          <th className="p-3 font-semibold min-w-[320px]">Thông tin playlist</th>
                          <th className="p-3 font-semibold w-28">Quyền riêng tư</th>
                          <th className="p-3 font-semibold w-24 text-center">Video</th>
                          <th className="p-3 font-semibold w-[140px] text-right pr-4">Thao tác</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {paginatedPlaylists.map((pl) => (
                          <tr
                            key={pl.id}
                            onClick={() => viewPlaylistInChannelVideos(pl.id)}
                            title="Nhấn để mở video của playlist này ở tab Video kênh"
                            className={`group cursor-pointer transition-colors ${
                              channelFilters.playlist_id === pl.id ? "bg-primary/10 hover:bg-primary/15" : "hover:bg-muted/40"
                            }`}
                          >
                            <td className="p-3 text-center align-top pt-4" onClick={(e) => e.stopPropagation()}>
                              <input
                                type="checkbox"
                                checked={selectedPlaylistIds.includes(pl.id)}
                                onChange={(e) =>
                                  setSelectedPlaylistIds((prev) =>
                                    e.target.checked ? [...prev, pl.id] : prev.filter((id) => id !== pl.id)
                                  )
                                }
                              />
                            </td>
                            <td className="p-3 align-top" onClick={(e) => e.stopPropagation()}>
                              {pl.thumbnail ? (
                                <img
                                  src={pl.thumbnail}
                                  alt=""
                                  className="w-20 h-12 object-cover rounded border border-border bg-muted"
                                  loading="lazy"
                                />
                              ) : (
                                <div className="w-20 h-12 rounded border border-border bg-muted flex items-center justify-center text-muted-foreground">
                                  <PlaySquare className="h-5 w-5" />
                                </div>
                              )}
                            </td>
                            <td className="p-3 align-top min-w-0">
                              {/* Tiêu đề full, không truncate, cho phép xuống dòng */}
                              <div className="font-bold text-sm leading-snug text-foreground break-words whitespace-normal">
                                {pl.title}
                              </div>
                              {pl.description ? (
                                <div className="text-xs text-muted-foreground mt-1 line-clamp-2 break-words whitespace-normal leading-relaxed">
                                  {pl.description}
                                </div>
                              ) : (
                                <div className="text-xs text-muted-foreground/60 italic mt-1">Không có mô tả</div>
                              )}
                              <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                                <span className="text-[10px] font-mono text-muted-foreground bg-muted px-1.5 py-0.5 rounded border border-border break-all">
                                  {pl.id}
                                </span>
                                <a
                                  href={`https://www.youtube.com/playlist?list=${pl.id}`}
                                  target="_blank"
                                  rel="noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
                                >
                                  YouTube <ExternalLink className="h-3 w-3" />
                                </a>
                              </div>
                            </td>
                            <td className="p-3 align-top pt-3.5">
                              <span
                                className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-[11px] font-semibold border capitalize ${
                                  pl.privacy === "public"
                                    ? "bg-lime-500/15 text-lime-700 border-lime-500/20"
                                    : pl.privacy === "unlisted"
                                      ? "bg-amber-500/15 text-amber-700 border-amber-500/20"
                                      : "bg-muted text-muted-foreground border-border"
                                }`}
                              >
                                <ShieldCheck className="h-3 w-3" />
                                {pl.privacy === "public" ? "Công khai" : pl.privacy === "unlisted" ? "Không công khai" : "Riêng tư"}
                              </span>
                            </td>
                            <td className="p-3 align-top pt-3.5 text-center">
                              <span className="inline-flex items-center gap-1 justify-center px-2.5 py-1 rounded-full bg-primary/10 text-primary text-xs font-mono font-bold border border-primary/15">
                                <Layers className="h-3 w-3" />
                                {pl.itemCount}
                              </span>
                              <div className="text-[10px] text-muted-foreground mt-1 font-mono">video</div>
                            </td>
                            <td className="p-3 align-top pt-3 pr-4">
                              {/* Nút thao tác nằm cùng hàng, không xuống dòng */}
                              <div className="flex items-center justify-end gap-1 flex-nowrap">
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="h-7 px-2 text-xs font-medium"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    viewPlaylistInChannelVideos(pl.id);
                                  }}
                                  title="Xem video trong playlist"
                                >
                                  <ListVideo className="h-3.5 w-3.5" />
                                  Xem
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setEditingPlaylist({ id: pl.id, title: pl.title, description: pl.description || "" });
                                  }}
                                  title="Sửa tiêu đề/mô tả"
                                >
                                  <Pencil className="h-3.5 w-3.5" />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-destructive hover:text-destructive hover:bg-destructive/10"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleDeletePlaylist(pl.id, pl.title);
                                  }}
                                  title="Xóa playlist"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-t border-border bg-muted/20 text-xs text-muted-foreground">
                    <span className="font-mono">
                      Trang {playlistPage}/{playlistPageCount} · {sortedPlaylists.length} playlist
                      {playlistSearch ? ` · lọc từ "${playlistSearch}"` : ""}
                    </span>
                    <div className="flex items-center gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        disabled={playlistPage <= 1}
                        onClick={() => setPlaylistPage((p) => Math.max(1, p - 1))}
                      >
                        <ChevronLeft className="h-3.5 w-3.5" />
                        Trước
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        disabled={playlistPage >= playlistPageCount}
                        onClick={() => setPlaylistPage((p) => Math.min(playlistPageCount, p + 1))}
                      >
                        Sau
                        <ChevronRight className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* TAB 4: CHANNEL VIDEOS (Videos kênh) - every video on the channel, including
          ones in no playlist, browsed from the local cache built by "Đồng bộ". */}
      {activeTab === "channel_videos" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2 p-3 bg-muted/30 rounded border border-border text-xs">
            <div className="flex items-center gap-2">
              <Button variant="default" size="sm" onClick={handleSyncChannelVideos} disabled={channelSyncing}>
                <RefreshCw className={`h-3.5 w-3.5 ${channelSyncing ? "animate-spin" : ""}`} />
                {channelSyncing ? "Đang đồng bộ..." : "Đồng bộ từ YouTube"}
              </Button>
              <span className="text-[11px] text-muted-foreground font-mono">
                {channelSyncStatus.synced_at
                  ? `Đồng bộ lần cuối: ${new Date(channelSyncStatus.synced_at).toLocaleString("vi-VN")} (${channelSyncStatus.count} video)`
                  : "Chưa đồng bộ lần nào - nhấn Đồng bộ để tải danh sách video kênh."}
              </span>
            </div>
            <div className="flex items-center gap-2">
              {channelSelectedIds.length > 0 && (
                <span className="text-[11px] font-mono text-muted-foreground">Đã chọn: {channelSelectedIds.length}</span>
              )}
              <select
                value={ioFormat}
                onChange={(e) => setIoFormat(e.target.value as any)}
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                aria-label="Định dạng xuất dữ liệu"
              >
                <option value="json">JSON (khuyên dùng, giữ nguyên emoji)</option>
                <option value="csv">CSV (Excel)</option>
              </select>
              <Button variant="secondary" size="sm" onClick={handleChannelExport}>
                <Download className="h-3.5 w-3.5" />
                {channelSelectedIds.length > 0 ? `Xuất ${channelSelectedIds.length} mục` : "Xuất toàn bộ"}
              </Button>
            </div>
          </div>

          {/* Scope switch: whole channel (cache browser) vs. one playlist's live items
              (order/episode-resort). This is the only place that drives the switch. */}
          <div className="flex flex-wrap items-center gap-2 p-2 bg-muted/20 rounded border border-border text-xs">
            <span className="font-semibold text-muted-foreground">Xem theo:</span>
            <select
              value={channelFilters.playlist_id}
              onChange={(e) => { setActivePlaylistId(e.target.value); setChannelPage(1); }}
              className="h-8 rounded border border-input bg-background px-2 text-xs max-w-64"
            >
              <option value="">Toàn kênh (mọi video)</option>
              {playlists.map((pl) => (
                <option key={pl.id} value={pl.id}>{pl.title} ({pl.itemCount} video)</option>
              ))}
            </select>
            {channelFilters.playlist_id && (
              <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={() => setActivePlaylistId("")}>
                <X className="h-3.5 w-3.5" />
                Xem toàn kênh
              </Button>
            )}
          </div>

          {channelFilters.playlist_id ? (
            <Card className="border-border">
              <CardHeader className="space-y-3 pb-3 border-b border-border">
                <CardTitle className="text-sm font-bold flex items-center gap-2">
                  {playlists.find((p) => p.id === channelFilters.playlist_id)?.title || "Chi tiết Playlist"}
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-primary"
                    title="Sửa tiêu đề/mô tả playlist"
                    onClick={() => {
                      const pl = playlists.find((p) => p.id === channelFilters.playlist_id);
                      if (pl) setEditingPlaylist({ id: pl.id, title: pl.title, description: pl.description || "" });
                    }}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                </CardTitle>
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
                  {selectedItemIds.length > 0 && (
                    <>
                      <span className="text-[11px] font-mono text-muted-foreground">Đã chọn: {selectedItemIds.length}</span>
                      <Button variant="outline" size="sm" onClick={() => setShowCopyMoveModal({ mode: "copy" })}>
                        <Copy className="h-3.5 w-3.5" />
                        Sao chép sang...
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => setShowCopyMoveModal({ mode: "move" })}>
                        <FolderInput className="h-3.5 w-3.5" />
                        Di chuyển sang...
                      </Button>
                      <Button variant="destructive" size="sm" onClick={handleRemoveSelectedItems}>
                        <Trash2 className="h-3.5 w-3.5" />
                        Gỡ khỏi playlist
                      </Button>
                    </>
                  )}
                </div>
              </CardHeader>
              <CardContent className="p-4 space-y-4">
                {/* Sort Controls - the episode/natural re-sort tools, scoped to this playlist */}
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
                        variant="outline"
                        size="sm"
                        className="h-7 text-xs"
                        onClick={handleUndoOrder}
                        disabled={!orderSnapshot}
                        title="Khôi phục thứ tự như trên YouTube trước khi sắp xếp"
                      >
                        <Undo2 className="h-3.5 w-3.5" />
                        Hoàn tác
                      </Button>
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

                {playlistItems.length > 0 && (
                  <div className="relative">
                    <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
                    <Input
                      className="h-8 pl-8 text-xs"
                      placeholder="Tìm video trong playlist theo tiêu đề hoặc video ID..."
                      value={itemSearch}
                      onChange={(e) => setItemSearch(e.target.value)}
                    />
                  </div>
                )}

                {loadingPlaylistItems ? (
                  <LoadingState text="Đang nạp các video trong playlist..." />
                ) : playlistItems.length === 0 ? (
                  <EmptyState text="Playlist này chưa có video nào." />
                ) : filteredPlaylistItems.length === 0 ? (
                  <EmptyState text="Không tìm thấy video khớp với từ khóa tìm kiếm." />
                ) : (
                  <>
                    <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] font-semibold text-muted-foreground">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={paginatedPlaylistItems.length > 0 && paginatedPlaylistItems.every((i) => selectedItemIds.includes(i.playlist_item_id))}
                          onChange={(e) => {
                            const ids = paginatedPlaylistItems.map((i) => i.playlist_item_id);
                            setSelectedItemIds((prev) =>
                              e.target.checked ? Array.from(new Set([...prev, ...ids])) : prev.filter((id) => !ids.includes(id))
                            );
                          }}
                        />
                        Trang này
                      </label>
                      <span className="opacity-50">·</span>
                      <button
                        type="button"
                        className="text-primary hover:underline"
                        onClick={() => setSelectedItemIds(filteredPlaylistItems.map((i) => i.playlist_item_id))}
                      >
                        Chọn tất cả {filteredPlaylistItems.length} video đã lọc
                      </button>
                      {selectedItemIds.length > 0 && (
                        <>
                          <span className="opacity-50">·</span>
                          <button
                            type="button"
                            className="hover:underline"
                            onClick={() => setSelectedItemIds([])}
                          >
                            Bỏ chọn
                          </button>
                        </>
                      )}
                    </div>
                    <div className="divide-y divide-border border border-border rounded-md overflow-hidden">
                      {paginatedPlaylistItems.map((item) => {
                        const currentOrder = manualOrders[item.playlist_item_id] ?? "?";
                        const isSelected = selectedItemIds.includes(item.playlist_item_id);
                        const isDragging = draggedItemId === item.playlist_item_id;
                        const canDrag = !itemSearch.trim();
                        return (
                            <div
                              key={item.playlist_item_id}
                              onDragOver={(e) => {
                                if (!draggedItemId || isDragging) return;
                                e.preventDefault();
                                e.dataTransfer.dropEffect = "move";
                              }}
                              onDrop={(e) => {
                                e.preventDefault();
                                handleDragReorder(item.playlist_item_id);
                              }}
                              className={`p-3 flex items-center justify-between gap-3 text-xs transition-colors ${
                                isDragging ? "opacity-50 bg-muted/30" : isSelected ? "bg-primary/5 hover:bg-primary/10" : "hover:bg-muted/20"
                              }`}
                            >
                              <button
                                type="button"
                                draggable={canDrag}
                                onDragStart={(e) => {
                                  if (!canDrag) return;
                                  setDraggedItemId(item.playlist_item_id);
                                  e.dataTransfer.effectAllowed = "move";
                                  e.dataTransfer.setData("text/plain", item.playlist_item_id);
                                }}
                                onDragEnd={() => setDraggedItemId(null)}
                                title={canDrag ? "Kéo để sắp xếp lại vị trí trong playlist" : "Tắt tìm kiếm để kéo-thả"}
                                className={`shrink-0 text-muted-foreground ${canDrag ? "cursor-grab active:cursor-grabbing" : "cursor-not-allowed opacity-40"}`}
                                aria-label="Tay nắm kéo thả"
                              >
                                <GripVertical className="h-4 w-4" />
                              </button>
                              <input
                                type="checkbox"
                                checked={isSelected}
                                onChange={(e) =>
                                  setSelectedItemIds((prev) =>
                                    e.target.checked ? [...prev, item.playlist_item_id] : prev.filter((id) => id !== item.playlist_item_id)
                                  )
                                }
                              />
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
                                className="h-7 w-7 shrink-0"
                                title="Sửa tiêu đề video"
                                onClick={() => setEditingItem({ videoId: item.video_id, title: item.title })}
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </Button>
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
                    <div className="flex items-center justify-between text-[11px] text-muted-foreground px-1">
                      <span>Trang {itemsPage}/{itemsPageCount} ({filteredPlaylistItems.length} video)</span>
                      <div className="flex gap-1">
                        <Button variant="ghost" size="icon" className="h-6 w-6" disabled={itemsPage <= 1}
                          onClick={() => setItemsPage((p) => Math.max(1, p - 1))}>
                          <ChevronLeft className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-6 w-6" disabled={itemsPage >= itemsPageCount}
                          onClick={() => setItemsPage((p) => Math.min(itemsPageCount, p + 1))}>
                          <ChevronRight className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          ) : (
          <>
          {channelSelectedIds.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 p-2.5 bg-primary/5 rounded border border-primary/20 text-xs">
              <Button variant="default" size="sm" onClick={() => setShowChannelBulkUpdateModal(true)}>
                <Layers className="h-3.5 w-3.5" />
                Sửa metadata hàng loạt
              </Button>
              <Button variant="outline" size="sm" onClick={() => { setChannelPlaylistTargetId(""); setShowChannelAddToPlaylistModal(true); }}>
                <Plus className="h-3.5 w-3.5" />
                Thêm vào playlist
              </Button>
              <Button variant="destructive" size="sm" onClick={() => setShowChannelDeleteModal(true)}>
                <Trash2 className="h-3.5 w-3.5" />
                Xóa khỏi YouTube
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setChannelSelectedIds([])}>
                Bỏ chọn
              </Button>
            </div>
          )}

          {/* Filters */}
          <div className="flex flex-wrap items-end gap-2 p-3 bg-muted/30 rounded border border-border text-xs">
            <div className="relative min-w-56 flex-1">
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Tìm kiếm</label>
              <Search className="absolute left-2.5 top-[1.65rem] h-3.5 w-3.5 text-muted-foreground" />
              <Input
                className="h-8 pl-8 text-xs"
                placeholder="Tiêu đề hoặc mô tả..."
                value={channelSearchInput}
                onChange={(e) => setChannelSearchInput(e.target.value)}
              />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Quyền riêng tư</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={channelFilters.privacy_status}
                onChange={(e) => { setChannelFilters((prev) => ({ ...prev, privacy_status: e.target.value })); setChannelPage(1); }}
              >
                <option value="">Tất cả</option>
                <option value="private">Riêng tư</option>
                <option value="unlisted">Không công khai</option>
                <option value="public">Công khai</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Trạng thái playlist</label>
              <select
                className="h-8 rounded border border-input bg-background px-2 text-xs"
                value={channelFilters.has_playlist}
                onChange={(e) => { setChannelFilters((prev) => ({ ...prev, has_playlist: e.target.value })); setChannelPage(1); }}
              >
                <option value="">Tất cả</option>
                <option value="no">Chưa có playlist</option>
                <option value="yes">Đã có playlist</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Từ ngày</label>
              <Input
                type="date"
                className="h-8 text-xs"
                value={channelFilters.date_from}
                onChange={(e) => { setChannelFilters((prev) => ({ ...prev, date_from: e.target.value })); setChannelPage(1); }}
              />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Đến ngày</label>
              <Input
                type="date"
                className="h-8 text-xs"
                value={channelFilters.date_to}
                onChange={(e) => { setChannelFilters((prev) => ({ ...prev, date_to: e.target.value })); setChannelPage(1); }}
              />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground mb-1 block">Sắp xếp</label>
              <div className="flex gap-1">
                <select
                  className="h-8 rounded border border-input bg-background px-2 text-xs"
                  value={channelFilters.sort}
                  onChange={(e) => { setChannelFilters((prev) => ({ ...prev, sort: e.target.value as any })); setChannelPage(1); }}
                >
                  <option value="published_at">Ngày đăng</option>
                  <option value="title">Tiêu đề</option>
                  <option value="view_count">Lượt xem</option>
                  <option value="duration_sec">Thời lượng</option>
                </select>
                <select
                  className="h-8 rounded border border-input bg-background px-2 text-xs"
                  value={channelFilters.order}
                  onChange={(e) => { setChannelFilters((prev) => ({ ...prev, order: e.target.value as any })); setChannelPage(1); }}
                >
                  <option value="desc">Giảm dần</option>
                  <option value="asc">Tăng dần</option>
                </select>
              </div>
            </div>
            {hasChannelFilters && (
              <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={clearChannelFilters}>
                Xóa bộ lọc
              </Button>
            )}
          </div>

          {channelLoading ? (
            <LoadingState text="Đang nạp danh sách video kênh..." />
          ) : channelVideosCache.length === 0 ? (
            <EmptyState
              text={
                channelSyncStatus.count === 0
                  ? "Chưa có dữ liệu - nhấn \"Đồng bộ từ YouTube\" để tải toàn bộ video trên kênh."
                  : "Không tìm thấy video khớp với bộ lọc hiện tại."
              }
            />
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
                            checked={channelVideosCache.length > 0 && channelVideosCache.every((v) => channelSelectedIds.includes(v.video_id))}
                            onChange={(e) => {
                              const ids = channelVideosCache.map((v) => v.video_id);
                              setChannelSelectedIds((prev) =>
                                e.target.checked ? Array.from(new Set([...prev, ...ids])) : prev.filter((id) => !ids.includes(id))
                              );
                            }}
                          />
                        </th>
                        <th className="p-3 font-semibold">Video</th>
                        <th className="p-3 font-semibold">Playlist</th>
                        <th className="p-3 font-semibold">Trạng thái</th>
                        <th className="p-3 font-semibold text-right">Lượt xem</th>
                        <th className="p-3 font-semibold text-right">Thời lượng</th>
                        <th className="p-3 font-semibold">Ngày đăng</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {channelVideosCache.map((v) => {
                        const isSelected = channelSelectedIds.includes(v.video_id);
                        return (
                          <tr key={v.video_id} className={`hover:bg-muted/30 transition-colors ${isSelected ? "bg-primary/5" : ""}`}>
                            <td className="p-3 text-center">
                              <input
                                type="checkbox"
                                checked={isSelected}
                                onChange={(e) =>
                                  setChannelSelectedIds((prev) =>
                                    e.target.checked ? [...prev, v.video_id] : prev.filter((id) => id !== v.video_id)
                                  )
                                }
                              />
                            </td>
                            <td className="p-3">
                              <div className="flex items-start gap-2">
                                {v.thumbnail ? (
                                  <img src={v.thumbnail} alt="" className="w-16 h-10 object-cover rounded shrink-0 border border-border" />
                                ) : (
                                  <div className="w-16 h-10 bg-zinc-900 rounded shrink-0 flex items-center justify-center text-muted-foreground">
                                    <FileVideo className="h-4 w-4" />
                                  </div>
                                )}
                                <div className="min-w-0">
                                  <div className="font-bold truncate max-w-sm">{v.title}</div>
                                  <div className="text-[10px] text-muted-foreground truncate max-w-sm" title={v.description}>
                                    {v.description ? v.description.slice(0, 90) : ""}
                                  </div>
                                  <a
                                    href={`https://youtu.be/${v.video_id}`}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-[10px] text-primary hover:underline inline-flex items-center gap-1"
                                  >
                                    {v.video_id}
                                    <ExternalLink className="h-2.5 w-2.5" />
                                  </a>
                                </div>
                              </div>
                            </td>
                            <td className="p-3">
                              {v.playlist_ids.length === 0 ? (
                                <span className="text-amber-600 text-[11px]">Chưa có playlist</span>
                              ) : (
                                <div className="flex flex-wrap gap-1 max-w-40">
                                  {v.playlist_ids.map((pid) => (
                                    <span key={pid} className="px-1.5 py-0.5 rounded bg-muted text-[10px] truncate max-w-40" title={playlistTitleById[pid] || pid}>
                                      {playlistTitleById[pid] || pid}
                                    </span>
                                  ))}
                                </div>
                              )}
                            </td>
                            <td className="p-3 font-mono text-muted-foreground uppercase">{v.privacy_status}</td>
                            <td className="p-3 text-right font-mono text-muted-foreground">
                              {v.view_count != null ? v.view_count.toLocaleString("vi-VN") : "-"}
                            </td>
                            <td className="p-3 text-right font-mono text-muted-foreground">{fmtDuration(v.duration_sec)}</td>
                            <td className="p-3 font-mono text-muted-foreground whitespace-nowrap">
                              {v.published_at ? new Date(v.published_at).toLocaleDateString("vi-VN") : "-"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center justify-between text-[11px] text-muted-foreground px-3 py-2 border-t border-border">
                  <span>Trang {channelPage}/{channelPageCount} ({channelTotal} video)</span>
                  <div className="flex gap-1">
                    <Button variant="ghost" size="icon" className="h-6 w-6" disabled={channelPage <= 1}
                      onClick={() => setChannelPage((p) => Math.max(1, p - 1))}>
                      <ChevronLeft className="h-3.5 w-3.5" />
                    </Button>
                    <Button variant="ghost" size="icon" className="h-6 w-6" disabled={channelPage >= channelPageCount}
                      onClick={() => setChannelPage((p) => Math.min(channelPageCount, p + 1))}>
                      <ChevronRight className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}
          </>
          )}
        </div>
      )}

      {/* Edit playlist title/description */}
      {editingPlaylist && (
        <Dialog open={!!editingPlaylist} onOpenChange={(open) => !open && setEditingPlaylist(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Pencil className="h-4 w-4 text-primary" />
                Sửa Playlist
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tiêu đề playlist *</label>
                <Input
                  value={editingPlaylist.title}
                  onChange={(e) => setEditingPlaylist({ ...editingPlaylist, title: e.target.value })}
                />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả playlist</label>
                <Textarea
                  rows={4}
                  value={editingPlaylist.description}
                  onChange={(e) => setEditingPlaylist({ ...editingPlaylist, description: e.target.value })}
                />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditingPlaylist(null)}>
                Hủy
              </Button>
              <Button onClick={saveEditedPlaylist} disabled={savingPlaylistEdit || !editingPlaylist.title.trim()}>
                {savingPlaylistEdit ? "Đang lưu..." : "Lưu thay đổi"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Edit video title (already live in a playlist) */}
      {editingItem && (
        <Dialog open={!!editingItem} onOpenChange={(open) => !open && setEditingItem(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Pencil className="h-4 w-4 text-primary" />
                Sửa tiêu đề video
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tiêu đề video *</label>
                <Input
                  value={editingItem.title}
                  onChange={(e) => setEditingItem({ ...editingItem, title: e.target.value })}
                />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditingItem(null)}>
                Hủy
              </Button>
              <Button onClick={saveEditedItemTitle} disabled={savingItemEdit || !editingItem.title.trim()}>
                {savingItemEdit ? "Đang lưu..." : "Lưu thay đổi"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
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
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">video_path</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">not_for_kids</code>,{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">ai_labels_enabled</code> (giá
                trị true/false) rồi tải lại lên đây. Cột{" "}
                <code className="bg-muted px-1 py-0.5 rounded font-mono text-foreground">id</code> là khóa đối chiếu —
                giữ nguyên, đừng xóa.
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

      {/* Create Playlist Modal */}
      {showCreatePlaylistModal && (
        <Dialog open={showCreatePlaylistModal} onOpenChange={setShowCreatePlaylistModal}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Plus className="h-4 w-4 text-primary" />
                Tạo Playlist mới
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tiêu đề playlist *</label>
                <Input value={newPlaylistTitle} onChange={(e) => setNewPlaylistTitle(e.target.value)} autoFocus />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả</label>
                <Textarea rows={3} value={newPlaylistDescription} onChange={(e) => setNewPlaylistDescription(e.target.value)} />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Quyền riêng tư</label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                  value={newPlaylistPrivacy}
                  onChange={(e) => setNewPlaylistPrivacy(e.target.value)}
                >
                  <option value="private">Riêng tư</option>
                  <option value="unlisted">Không công khai</option>
                  <option value="public">Công khai</option>
                </select>
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowCreatePlaylistModal(false)}>Hủy</Button>
              <Button onClick={handleCreatePlaylist} disabled={creatingPlaylist || !newPlaylistTitle.trim()}>
                {creatingPlaylist ? "Đang tạo..." : "Tạo playlist"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Playlist Bulk Update Modal */}
      {showPlaylistBulkUpdateModal && (
        <Dialog open={showPlaylistBulkUpdateModal} onOpenChange={setShowPlaylistBulkUpdateModal}>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Layers className="h-4 w-4 text-primary" />
                Cập nhật hàng loạt {selectedPlaylistIds.length} playlist
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Tiền tố tiêu đề</label>
                  <Input value={playlistBulkTitlePrefix} onChange={(e) => setPlaylistBulkTitlePrefix(e.target.value)} placeholder="VD: [2026] " />
                </div>
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Hậu tố tiêu đề</label>
                  <Input value={playlistBulkTitleSuffix} onChange={(e) => setPlaylistBulkTitleSuffix(e.target.value)} placeholder="VD: (Full)" />
                </div>
              </div>
              <p className="text-[10px] text-muted-foreground">Tiền tố/hậu tố được thêm vào tiêu đề hiện có của từng playlist, không thay thế.</p>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả mới (để trống = giữ nguyên)</label>
                <Textarea rows={3} value={playlistBulkDescription} onChange={(e) => setPlaylistBulkDescription(e.target.value)} />
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Quyền riêng tư (để trống = giữ nguyên)</label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                  value={playlistBulkPrivacy}
                  onChange={(e) => setPlaylistBulkPrivacy(e.target.value)}
                >
                  <option value="">Giữ nguyên</option>
                  <option value="private">Riêng tư</option>
                  <option value="unlisted">Không công khai</option>
                  <option value="public">Công khai</option>
                </select>
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowPlaylistBulkUpdateModal(false)}>Hủy</Button>
              <Button onClick={runPlaylistBulkUpdate} disabled={playlistBulkBusy}>
                {playlistBulkBusy ? "Đang cập nhật..." : `Áp dụng cho ${selectedPlaylistIds.length} playlist`}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Copy / Move selected playlist items to another playlist */}
      {showCopyMoveModal && (
        <Dialog open={!!showCopyMoveModal} onOpenChange={(open) => !open && setShowCopyMoveModal(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                {showCopyMoveModal.mode === "copy" ? <Copy className="h-4 w-4 text-primary" /> : <FolderInput className="h-4 w-4 text-primary" />}
                {showCopyMoveModal.mode === "copy" ? "Sao chép" : "Di chuyển"} {selectedItemIds.length} video sang playlist khác
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <label className="text-xs font-semibold text-foreground mb-1 block">Playlist đích *</label>
              <select
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                value={copyMoveDestId}
                onChange={(e) => setCopyMoveDestId(e.target.value)}
              >
                <option value="">-- Chọn playlist --</option>
                {playlists.filter((p) => p.id !== channelFilters.playlist_id).map((pl) => (
                  <option key={pl.id} value={pl.id}>{pl.title} ({pl.itemCount} video)</option>
                ))}
              </select>
              {showCopyMoveModal.mode === "move" && (
                <p className="text-[11px] text-amber-600">Video sẽ bị gỡ khỏi playlist hiện tại sau khi thêm vào playlist đích thành công.</p>
              )}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowCopyMoveModal(null)}>Hủy</Button>
              <Button onClick={runCopyMoveItems} disabled={copyMoveBusy || !copyMoveDestId}>
                {copyMoveBusy ? "Đang xử lý..." : showCopyMoveModal.mode === "copy" ? "Sao chép" : "Di chuyển"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Channel-videos: bulk metadata update */}
      {showChannelBulkUpdateModal && (
        <Dialog open={showChannelBulkUpdateModal} onOpenChange={setShowChannelBulkUpdateModal}>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Layers className="h-4 w-4 text-primary" />
                Sửa metadata hàng loạt {channelSelectedIds.length} video
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-4 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Template tiêu đề (để trống = giữ nguyên)</label>
                <Input value={channelBulkTitleTemplate} onChange={(e) => setChannelBulkTitleTemplate(e.target.value)} placeholder="VD: Truyện Kể Đêm Khuya - Tập {episode}" />
                <p className="text-[10px] text-muted-foreground mt-1">
                  Dùng <code className="bg-muted px-0.5 rounded">{"{episode}"}</code> để tự tách số tập từ tiêu đề hiện có.
                </p>
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Template mô tả (để trống = giữ nguyên)</label>
                <Textarea rows={3} value={channelBulkDescriptionTemplate} onChange={(e) => setChannelBulkDescriptionTemplate(e.target.value)} placeholder="VD: Tập {episode} - Truyện kể đêm khuya hay nhất..." />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Quyền riêng tư (để trống = giữ nguyên)</label>
                  <select
                    className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                    value={channelBulkPrivacy}
                    onChange={(e) => setChannelBulkPrivacy(e.target.value)}
                  >
                    <option value="">Giữ nguyên</option>
                    <option value="private">Riêng tư</option>
                    <option value="unlisted">Không công khai</option>
                    <option value="public">Công khai</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs font-semibold text-foreground mb-1 block">Thêm tags (phân cách dấu phẩy)</label>
                  <Input value={channelBulkAddTags} onChange={(e) => setChannelBulkAddTags(e.target.value)} placeholder="sach noi, audiobooks" />
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-2 border-t border-border pt-3">
              <Button variant="outline" onClick={() => setShowChannelBulkUpdateModal(false)}>Hủy</Button>
              <Button onClick={runChannelBulkUpdate} disabled={channelBulkBusy}>
                {channelBulkBusy ? "Đang cập nhật..." : `Áp dụng cho ${channelSelectedIds.length} video`}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}

      {/* Channel-videos: add selected videos (from anywhere on the channel) to a playlist */}
      {showChannelAddToPlaylistModal && (
        <Dialog open={showChannelAddToPlaylistModal} onOpenChange={setShowChannelAddToPlaylistModal}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <PlaySquare className="h-4 w-4 text-primary" />
                Thêm {channelSelectedIds.length} video vào playlist
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <label className="text-xs font-semibold text-foreground mb-1 block">Playlist *</label>
              <select
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                value={channelPlaylistTargetId}
                onChange={(e) => setChannelPlaylistTargetId(e.target.value)}
              >
                <option value="">-- Chọn playlist --</option>
                {playlists.map((pl) => (
                  <option key={pl.id} value={pl.id}>{pl.title} ({pl.itemCount} video)</option>
                ))}
              </select>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowChannelAddToPlaylistModal(false)}>Hủy</Button>
              <Button onClick={runChannelAddToPlaylist} disabled={channelPlaylistBusy || !channelPlaylistTargetId}>
                {channelPlaylistBusy ? "Đang xử lý..." : "Thêm vào playlist"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Channel-videos: irreversible delete confirmation */}
      {showChannelDeleteModal && (
        <Dialog open={showChannelDeleteModal} onOpenChange={setShowChannelDeleteModal}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 text-destructive">
                <AlertTriangle className="h-4 w-4" />
                Xóa vĩnh viễn {channelSelectedIds.length} video khỏi YouTube
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div className="flex gap-2 p-2.5 rounded border border-destructive/30 bg-destructive/5 text-[11px] text-destructive">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>
                  Hành động này KHÔNG THỂ HOÀN TÁC. {channelSelectedIds.length} video sẽ bị xóa vĩnh viễn khỏi kênh YouTube,
                  kể cả lượt xem, bình luận và vị trí trong mọi playlist.
                </span>
              </div>
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">
                  Gõ <code className="bg-muted px-1 rounded">XÓA</code> để xác nhận
                </label>
                <Input value={channelDeleteConfirmText} onChange={(e) => setChannelDeleteConfirmText(e.target.value)} autoFocus />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => { setShowChannelDeleteModal(false); setChannelDeleteConfirmText(""); }}>Hủy</Button>
              <Button variant="destructive" onClick={runChannelDelete} disabled={channelDeleteBusy || channelDeleteConfirmText !== "XÓA"}>
                {channelDeleteBusy ? "Đang xóa..." : "Xóa vĩnh viễn"}
              </Button>
            </DialogFooter>
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
