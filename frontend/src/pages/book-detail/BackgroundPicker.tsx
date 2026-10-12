import React, { useEffect, useState } from "react";
import { Check, Film, Folder, FolderOpen, Search, X } from "lucide-react";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { BackgroundFolder, VideoConfig } from "./types";
import { Field, checkboxClass, selectClass } from "./parts";

type BackgroundSelection = Pick<VideoConfig, "backgrounds" | "background_folders" | "background_mode">;
type LibraryFile = { name: string; path: string; size: number; is_video: boolean };
type LibraryPage = { items: LibraryFile[]; total: number; root_count: number; folders: BackgroundFolder[] };

/** Tên đặc biệt trong background_folders: các file nằm ngay ở gốc thư viện. */
const ROOT_FOLDER = "__root__";
/** Thư viện có thể tới hàng nghìn file nên modal tải theo trang từ server. */
const PAGE_SIZE = 24;

const baseName = (path: string) => path.split(/[\\/]/).pop() || path;
const folderLabel = (name: string) => (name === ROOT_FOLDER ? "Thư mục gốc" : name);

/** Chọn background media cho video: tóm tắt lựa chọn hiện tại và nút mở modal
 * thư viện để chọn cả thư mục hoặc nhiều file. Thư mục chỉ lưu tên, nội dung
 * được đọc lúc dựng video nên file thêm vào sau vẫn được dùng. */
export function BackgroundPicker({
  value,
  onChange,
}: {
  value: BackgroundSelection;
  onChange: (patch: Partial<BackgroundSelection>) => void;
}) {
  const [open, setOpen] = useState(false);
  const selectedFolders = value.background_folders || [];
  const empty = !selectedFolders.length && !value.backgrounds.length;
  const chipClass = "inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-muted/30 py-1 pl-2 pr-1 text-xs";
  const removeClass = "rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground";

  return (
    <div className="space-y-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Thứ tự background media">
          <select
            className={selectClass}
            value={value.background_mode}
            onChange={(event) => onChange({ background_mode: event.target.value as VideoConfig["background_mode"] })}
          >
            <option value="sequential">Theo thứ tự</option>
            <option value="random">Ngẫu nhiên</option>
          </select>
        </Field>
        <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
          <FolderOpen className="h-3.5 w-3.5" /> Chọn từ thư viện
        </Button>
        {!empty && (
          <Button type="button" variant="ghost" size="sm" onClick={() => onChange({ backgrounds: [], background_folders: [] })}>
            Bỏ chọn tất cả
          </Button>
        )}
        <span className="pb-2 text-[11px] text-muted-foreground">
          Đã chọn {selectedFolders.length} thư mục · {value.backgrounds.length} file lẻ
        </span>
      </div>

      {empty ? (
        <div className="text-xs text-muted-foreground">
          Chưa chọn background media — video dùng ảnh nền của sách hoặc ảnh mặc định.
        </div>
      ) : (
        <div className="flex max-h-28 flex-wrap gap-1.5 overflow-auto">
          {selectedFolders.map((name) => (
            <span key={`folder:${name}`} className={chipClass}>
              <Folder className="h-3.5 w-3.5 shrink-0 text-primary" />
              <span className="truncate">{folderLabel(name)}</span>
              <button
                type="button"
                className={removeClass}
                aria-label={`Bỏ thư mục ${folderLabel(name)}`}
                onClick={() => onChange({ background_folders: selectedFolders.filter((current) => current !== name) })}
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
          {value.backgrounds.map((path) => (
            <span key={path} className={chipClass} title={path}>
              <span className="truncate">{baseName(path)}</span>
              <button
                type="button"
                className={removeClass}
                aria-label={`Bỏ file ${baseName(path)}`}
                onClick={() => onChange({ backgrounds: value.backgrounds.filter((current) => current !== path) })}
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {open && <BackgroundLibraryDialog value={value} onChange={onChange} onClose={() => setOpen(false)} />}
    </div>
  );
}

/** Modal duyệt thư viện background: tick cả thư mục ở cột trái, hoặc mở thư
 * mục và chọn nhiều file ở lưới bên phải. Lựa chọn áp dụng ngay. */
function BackgroundLibraryDialog({
  value,
  onChange,
  onClose,
}: {
  value: BackgroundSelection;
  onChange: (patch: Partial<BackgroundSelection>) => void;
  onClose: () => void;
}) {
  // "" là gốc thư viện, còn lại là tên sub folder đang mở.
  const [folder, setFolder] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<LibraryPage>();
  const [error, setError] = useState("");

  // Debounce nhẹ để gõ tìm kiếm không gửi request theo từng ký tự.
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({
        folder,
        q: search,
        offset: String(page * PAGE_SIZE),
        limit: String(PAGE_SIZE),
      });
      api<LibraryPage>(`/photos/list?${params}`)
        .then((result) => {
          if (cancelled) return;
          setData(result);
          setError("");
        })
        .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)));
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [folder, search, page]);

  const selectedFolders = value.background_folders || [];
  const selectedPaths = new Set(value.backgrounds);
  const folderRows = [
    { open: "", name: ROOT_FOLDER, count: data?.root_count ?? 0 },
    ...(data?.folders || []).map((item) => ({ open: item.name, name: item.name, count: item.count })),
  ];
  const items = data?.items || [];
  const pageCount = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE));
  const wholeFolder = selectedFolders.includes(folder || ROOT_FOLDER);
  const pageSelected = items.length > 0 && items.every((item) => selectedPaths.has(item.path));
  const fileUrl = (name: string) =>
    `/photos/file/${encodeURIComponent(name)}${folder ? `?folder=${encodeURIComponent(folder)}` : ""}`;

  const openFolder = (name: string) => {
    setFolder(name);
    setPage(0);
  };
  const toggleFolder = (name: string) =>
    onChange({
      background_folders: selectedFolders.includes(name)
        ? selectedFolders.filter((current) => current !== name)
        : [...selectedFolders, name],
    });
  const toggleFile = (path: string) =>
    onChange({
      backgrounds: selectedPaths.has(path)
        ? value.backgrounds.filter((current) => current !== path)
        : [...value.backgrounds, path],
    });
  const togglePage = () => {
    const pagePaths = new Set(items.map((item) => item.path));
    onChange({
      backgrounds: pageSelected
        ? value.backgrounds.filter((path) => !pagePaths.has(path))
        : [...value.backgrounds, ...items.filter((item) => !selectedPaths.has(item.path)).map((item) => item.path)],
    });
  };

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>Chọn background media</DialogTitle>
          <DialogDescription>
            Tick thư mục để dùng toàn bộ ảnh/video bên trong, hoặc mở thư mục và chọn từng file.
          </DialogDescription>
        </DialogHeader>

        <div className="grid h-[60vh] min-h-0 grid-cols-1 gap-3 md:grid-cols-[210px_1fr]">
          <div className="flex max-h-40 flex-col gap-1 overflow-auto rounded-md border border-border p-1.5 md:max-h-none">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="justify-start"
              onClick={() =>
                onChange({
                  background_folders: folderRows.every((row) => selectedFolders.includes(row.name))
                    ? []
                    : folderRows.map((row) => row.name),
                })
              }
            >
              {folderRows.every((row) => selectedFolders.includes(row.name)) ? "Bỏ chọn mọi thư mục" : "Chọn tất cả thư mục"}
            </Button>
            {folderRows.map((row) => (
              <div
                key={row.name}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs",
                  folder === row.open ? "bg-primary/10 text-primary" : "hover:bg-muted/40"
                )}
              >
                <input
                  type="checkbox"
                  className={checkboxClass}
                  checked={selectedFolders.includes(row.name)}
                  aria-label={`Dùng cả thư mục ${folderLabel(row.name)}`}
                  onChange={() => toggleFolder(row.name)}
                />
                <button type="button" className="flex min-w-0 flex-1 items-center gap-1.5 text-left" onClick={() => openFolder(row.open)}>
                  <Folder className="h-3.5 w-3.5 shrink-0" />
                  <span className="min-w-0 flex-1 truncate">{folderLabel(row.name)}</span>
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{row.count}</span>
                </button>
              </div>
            ))}
          </div>

          <div className="flex min-h-0 flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-40 flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <input
                  className="h-8 w-full rounded-md border border-input bg-background pl-8 pr-2 text-xs outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
                  placeholder={`Tìm trong ${folderLabel(folder || ROOT_FOLDER)}...`}
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setPage(0);
                  }}
                />
              </div>
              <Button type="button" variant="outline" size="sm" disabled={!items.length} onClick={togglePage}>
                {pageSelected ? "Bỏ chọn trang này" : "Chọn cả trang"}
              </Button>
              <Button type="button" variant="outline" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Trước
              </Button>
              <span className="font-mono text-[11px] text-muted-foreground">
                {page + 1}/{pageCount}
              </span>
              <Button type="button" variant="outline" size="sm" disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>
                Sau
              </Button>
            </div>

            {wholeFolder && (
              <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-[11px]">
                Đang dùng cả thư mục này ({data?.total ?? 0} file) — không cần chọn từng file.
              </div>
            )}

            <div className="min-h-0 flex-1 overflow-auto">
              {error ? (
                <div className="text-xs text-red-700">{error}</div>
              ) : !data ? (
                <div className="text-xs text-muted-foreground">Đang tải thư viện...</div>
              ) : !items.length ? (
                <div className="text-xs text-muted-foreground">
                  {search.trim() ? "Không có file nào khớp." : "Thư mục này chưa có ảnh hoặc video."}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                  {items.map((item) => {
                    const checked = selectedPaths.has(item.path);
                    return (
                      <button
                        key={item.path}
                        type="button"
                        aria-pressed={checked}
                        onClick={() => toggleFile(item.path)}
                        className={cn(
                          "overflow-hidden rounded-md border text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          checked ? "border-primary ring-1 ring-primary" : "border-border hover:border-primary/50"
                        )}
                      >
                        <div className="relative aspect-video bg-zinc-950">
                          {item.is_video ? (
                            <video src={fileUrl(item.name)} className="h-full w-full object-cover" muted preload="metadata" />
                          ) : (
                            <img src={fileUrl(item.name)} alt="" className="h-full w-full object-cover" loading="lazy" />
                          )}
                          {item.is_video && (
                            <span className="absolute bottom-1 left-1 rounded bg-black/70 p-1 text-white">
                              <Film className="h-3 w-3" />
                            </span>
                          )}
                          {checked && (
                            <span className="absolute right-1 top-1 rounded-full bg-primary p-0.5 text-primary-foreground">
                              <Check className="h-3 w-3" />
                            </span>
                          )}
                        </div>
                        <div className="truncate px-2 py-1 text-[11px]" title={item.name}>
                          {item.name}
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </div>

        <DialogFooter className="items-center sm:justify-between">
          <span className="text-[11px] text-muted-foreground">
            Đã chọn {selectedFolders.length} thư mục · {value.backgrounds.length} file lẻ
          </span>
          <Button type="button" onClick={onClose}>
            Xong
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
