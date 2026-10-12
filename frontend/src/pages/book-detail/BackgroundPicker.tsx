import React from "react";
import { Folder } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BackgroundFolder, BackgroundItem, VideoConfig } from "./types";
import { Field, checkboxClass, selectClass } from "./parts";

type BackgroundSelection = Pick<VideoConfig, "backgrounds" | "background_folders" | "background_mode">;

/** Chọn background media cho video: file lẻ và/hoặc cả sub folder của thư viện.
 * Folder được đọc lúc dựng video nên file thêm vào sau vẫn được dùng. */
export function BackgroundPicker({
  items,
  folders,
  value,
  onChange,
}: {
  items: BackgroundItem[];
  folders: BackgroundFolder[];
  value: BackgroundSelection;
  onChange: (patch: Partial<BackgroundSelection>) => void;
}) {
  const selectedFolders = value.background_folders || [];
  const allPaths = items.map((item) => item.path);
  const allFolders = folders.map((folder) => folder.name);
  const allSelected =
    allPaths.length + allFolders.length > 0 &&
    allPaths.every((path) => value.backgrounds.includes(path)) &&
    allFolders.every((name) => selectedFolders.includes(name));
  const folderFileCount = folders
    .filter((folder) => selectedFolders.includes(folder.name))
    .reduce((total, folder) => total + folder.files.length, 0);
  const rowClass =
    "flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-muted/40";

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
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!allPaths.length && !allFolders.length}
          onClick={() =>
            onChange(
              allSelected
                ? { backgrounds: [], background_folders: [] }
                : { backgrounds: allPaths, background_folders: allFolders }
            )
          }
        >
          {allSelected ? "Bỏ chọn tất cả" : "Chọn tất cả"}
        </Button>
        <span className="pb-2 text-[11px] text-muted-foreground">
          Đã chọn {value.backgrounds.length} file ảnh/video
          {selectedFolders.length > 0 && ` · ${selectedFolders.length} thư mục (${folderFileCount} file)`}
        </span>
      </div>
      {folders.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-[11px] font-medium text-muted-foreground">
            Thư mục — dùng toàn bộ ảnh/video bên trong, lặp lại cho đủ thời lượng audio
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {folders.map((folder) => {
              const checked = selectedFolders.includes(folder.name);
              return (
                <label key={folder.name} className={rowClass}>
                  <input
                    type="checkbox"
                    className={checkboxClass}
                    checked={checked}
                    onChange={() =>
                      onChange({
                        background_folders: checked
                          ? selectedFolders.filter((name) => name !== folder.name)
                          : [...selectedFolders, folder.name],
                      })
                    }
                  />
                  <Folder className="h-3.5 w-3.5 shrink-0 text-primary" />
                  <span className="min-w-0 flex-1 truncate">{folder.name}</span>
                  <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    {folder.files.length} file
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      )}
      {items.length ? (
        <div className="grid max-h-52 grid-cols-1 gap-2 overflow-auto sm:grid-cols-2">
          {items.map((item) => {
            const checked = value.backgrounds.includes(item.path);
            return (
              <label key={item.path} className={rowClass}>
                <input
                  type="checkbox"
                  className={checkboxClass}
                  checked={checked}
                  onChange={() =>
                    onChange({
                      backgrounds: checked
                        ? value.backgrounds.filter((path) => path !== item.path)
                        : [...value.backgrounds, item.path],
                    })
                  }
                />
                <span className="min-w-0 flex-1 truncate">{item.is_default ? "Mặc định" : item.name}</span>
                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {item.is_video ? "Video" : "Ảnh"}
                </span>
              </label>
            );
          })}
        </div>
      ) : (
        !folders.length && <div className="text-xs text-muted-foreground">Thư viện chưa có background media.</div>
      )}
    </div>
  );
}
