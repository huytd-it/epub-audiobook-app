import React, { useMemo, useState } from "react";
import { Cloud, Download, FolderSync, Gauge, HardDrive, Save, Layers } from "lucide-react";
import { DriveAccount, DriveTarget, KaggleAccount, Patch, post, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Combobox } from "@/components/ui/combobox";
import { cn } from "@/lib/utils";
import { AudioSettings, TtsModel, VoiceOption, downloadForm, errorText } from "./types";
import { CheckField, fieldClass, selectClass } from "./parts";

/**
 * Không giữ danh sách chọn riêng: patch để export lấy thẳng từ lựa chọn trong bảng Patches.
 * Không chọn gì = export mọi patch đang sẵn sàng.
 * Layout gọn: cấu hình + 3 điểm đến trong một Card thấp, khối Kaggle cố định ở bottom nav.
 */
export function ExportPanel({
  bookId,
  patches,
  selectedIds,
  accounts,
  syncTargets,
  kaggleAccounts,
  settings,
  onSettingsChange,
  ttsModels,
  voiceOptions,
  onMessage,
  onRefresh,
  onBusyChange,
}: {
  bookId: string;
  patches: Patch[];
  selectedIds: number[];
  accounts: DriveAccount[];
  syncTargets: DriveTarget[];
  kaggleAccounts: KaggleAccount[];
  settings: AudioSettings;
  onSettingsChange: (patch: Partial<AudioSettings>) => void;
  ttsModels: TtsModel[];
  voiceOptions: VoiceOption[];
  onMessage: (message: string) => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [syncTargetId, setSyncTargetId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [exporting, setExporting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [kaggleAutoVideo, setKaggleAutoVideo] = useState(true);
  const [kaggleAutoUpload, setKaggleAutoUpload] = useState(false);

  const { targetIds, skipped, usingSelection } = useMemo(() => {
    const exportable = patches.filter((patch) => patch.status !== "processing");
    if (!selectedIds.length) {
      return { targetIds: exportable.map((patch) => patch.id), skipped: 0, usingSelection: false };
    }
    const chosen = exportable.filter((patch) => selectedIds.includes(patch.id));
    return {
      targetIds: chosen.map((patch) => patch.id),
      skipped: selectedIds.length - chosen.length,
      usingSelection: true,
    };
  }, [patches, selectedIds]);

  const buildForm = () => {
    const form = new FormData();
    targetIds.forEach((id) => form.append("patch_ids", String(id)));
    form.set("model_id", settings.modelId);
    form.set("voice_id", settings.voiceId);
    form.set("max_chars", settings.maxChars || "0");
    form.set("with_effects", settings.withEffects ? "1" : "0");
    return form;
  };

  const runExport = async (action: (form: FormData) => Promise<unknown>, done: string, refresh = true) => {
    if (!targetIds.length) {
      onMessage("Không có patch sẵn sàng để export.");
      return;
    }
    setExporting(true);
    onBusyChange(true);
    try {
      await action(buildForm());
      onMessage(done);
      if (refresh) await onRefresh();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setExporting(false);
      onBusyChange(false);
    }
  };

  const saveSettings = async () => {
    setSaving(true);
    onBusyChange(true);
    try {
      await postJson(`/books/${bookId}/export-audio-settings`, {
        model_id: settings.modelId,
        voice_id: settings.voiceId,
        max_chars: settings.maxChars ? Number(settings.maxChars) : 1200,
        with_effects: settings.withEffects,
      });
      onMessage("Đã lưu cấu hình Export Colab / Kaggle.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
      onBusyChange(false);
    }
  };

  const downloadZip = () =>
    runExport(
      (form) => downloadForm(`/books/${bookId}/patches/export-batch/download`, form),
      `Đã tải gói ZIP (${targetIds.length} patch).`,
      false
    );

  const exportToDesktop = () => {
    if (!syncTargetId) {
      onMessage("Chọn Drive Desktop target.");
      return;
    }
    return runExport((form) => {
      form.set("sync_target_id", syncTargetId);
      return post(`/books/${bookId}/patches/export-batch`, form);
    }, `Đã export ${targetIds.length} patch sang Drive Desktop.`);
  };

  const exportToDriveApi = () => {
    if (!accountId) {
      onMessage("Chọn Google Drive account.");
      return;
    }
    return runExport((form) => {
      form.set("account_id", accountId);
      return post(`/books/${bookId}/patches/export-batch-api`, form);
    }, `Đã export ${targetIds.length} patch qua Google Drive API.`);
  };

  const exportToKaggle = () => {
    const video = kaggleAutoUpload || kaggleAutoVideo;
    const chain = kaggleAutoUpload
      ? " → tự động dựng video và upload YouTube"
      : video
        ? " → tự động dựng video"
        : "";
    // Luôn per_patch — backend vẫn nhận automation_mode nhưng mặc định chuỗi từng patch.
    return runExport(
      (form) => {
        form.set("auto_create_video", video ? "1" : "0");
        form.set("auto_upload_youtube", kaggleAutoUpload ? "1" : "0");
        form.set("automation_mode", "per_patch");
        return post(`/books/${bookId}/patches/export-batch-kaggle`, form);
      },
      `Đã đưa ${targetIds.length} patch vào hàng đợi Kaggle${chain} (chuỗi từng patch). Theo dõi tiến độ ở trang Queue.`
    );
  };

  const hasTargets = targetIds.length > 0;
  const hasSelectionBar = selectedIds.length > 0;

  return (
    <>
      <Card className="overflow-hidden border-border shadow-sm">
        {/* Compact header — một dòng */}
        <CardHeader className="flex flex-row items-center justify-between gap-3 border-b border-border bg-muted/30 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <HardDrive className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0 leading-none">
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-bold tracking-tight">Export Colab / Kaggle</span>
                <span className="hidden rounded bg-foreground px-1.5 py-0.5 font-mono text-[9px] font-bold tracking-widest text-background sm:inline">
                  BATCH
                </span>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
                    hasTargets ? "border-primary/20 bg-primary/10 text-primary" : "border-border bg-card text-muted-foreground"
                  )}
                >
                  <span className={cn("h-1.5 w-1.5 rounded-full", hasTargets ? "animate-pulse bg-primary" : "bg-muted-foreground/40")} />
                  {targetIds.length} patch
                </span>
              </div>
              <div className="mt-1 hidden text-[11px] leading-none text-muted-foreground sm:block">
                {usingSelection ? (
                  <>
                    Dùng <b className="font-semibold text-foreground">{targetIds.length} patch đã chọn</b>
                    {skipped > 0 && ` · bỏ qua ${skipped} đang xử lý`} — text + cấu hình giọng
                  </>
                ) : (
                  <>
                    Chưa chọn patch nào — sẽ export <b className="font-semibold text-foreground">{targetIds.length} patch sẵn sàng</b>
                  </>
                )}
              </div>
            </div>
          </div>

          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={saveSettings}
            disabled={saving || exporting}
            className="hidden h-7 shrink-0 gap-1 rounded-md px-2.5 text-xs sm:inline-flex"
            title="Lưu cấu hình giọng cho lần export sau"
          >
            <Save className="h-3 w-3" /> {saving ? "Đang lưu..." : "Lưu"}
          </Button>
        </CardHeader>

        <CardContent className="space-y-3 p-3">
          {/* Mobile selection hint */}
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground sm:hidden">
            <Layers className="h-3 w-3" />
            {usingSelection ? `${targetIds.length} patch đã chọn${skipped ? ` · bỏ qua ${skipped}` : ""}` : `${targetIds.length} patch sẵn sàng`}
          </div>

          {/* Dòng cấu hình gọn: 3 field + checkbox + nút lưu (mobile) */}
          <div className="flex flex-col gap-2 rounded-lg border border-border bg-card px-3 py-2.5">
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium leading-none text-muted-foreground">TTS model</span>
                <select
                  className={cn(selectClass, "h-7 bg-background py-0 text-xs")}
                  value={settings.modelId}
                  onChange={(event) => onSettingsChange({ modelId: event.target.value })}
                >
                  {ttsModels.length ? (
                    ttsModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                      </option>
                    ))
                  ) : (
                    <option value={settings.modelId}>{settings.modelId}</option>
                  )}
                </select>
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium leading-none text-muted-foreground">Voice</span>
                <Combobox
                  value={settings.voiceId}
                  options={voiceOptions.map((option) => ({ ...option, description: option.value }))}
                  onChange={(voiceId) => onSettingsChange({ voiceId })}
                  placeholder="Tìm voice..."
                  aria-label="Voice ID"
                  inputClassName="h-7 py-0 text-xs"
                  className="[&>div>input]:h-7"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="flex items-center justify-between text-[11px] font-medium leading-none text-muted-foreground">
                  Max chars <span className="font-normal opacity-70">0 = mặc định</span>
                </span>
                <input
                  className={cn(fieldClass, "h-7 bg-background py-0 text-xs")}
                  type="number"
                  min="0"
                  value={settings.maxChars}
                  onChange={(event) => onSettingsChange({ maxChars: event.target.value })}
                  placeholder="1200"
                />
              </label>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2">
              <CheckField
                checked={settings.withEffects}
                onChange={(value) => onSettingsChange({ withEffects: value })}
                label="Chèn hiệu ứng"
              />
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={saveSettings}
                disabled={saving || exporting}
                className="h-7 gap-1 rounded-md px-2.5 text-xs sm:hidden"
              >
                <Save className="h-3 w-3" /> {saving ? "Đang lưu..." : "Lưu cấu hình"}
              </Button>
              <span className="hidden text-[11px] text-muted-foreground sm:inline">
                Áp dụng cho {targetIds.length} patch · hiệu ứng trộn vào batch
              </span>
            </div>
          </div>

          {/* 3 điểm đến — một hàng, gọn */}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {/* ZIP */}
            <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-600">
                  <Download className="h-3.5 w-3.5" />
                </span>
                <div className="min-w-0">
                  <div className="text-xs font-semibold leading-none">Tải về máy</div>
                  <div className="text-[11px] leading-none text-muted-foreground">ZIP · {targetIds.length} patch</div>
                </div>
                <span className="ml-auto rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] font-semibold text-muted-foreground">ZIP</span>
              </div>
              <Button size="sm" className="mt-3 h-7 w-full gap-1 rounded-md text-xs" onClick={downloadZip} disabled={exporting || !hasTargets}>
                <Download className="h-3 w-3" /> Tải gói ZIP
              </Button>
            </div>

            {/* Drive Desktop */}
            <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-md bg-blue-500/10 text-blue-600">
                  <FolderSync className="h-3.5 w-3.5" />
                </span>
                <div className="min-w-0">
                  <div className="text-xs font-semibold leading-none">Drive Desktop</div>
                  <div className="text-[11px] leading-none text-muted-foreground">Thư mục đồng bộ</div>
                </div>
                <span className="ml-auto rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] font-semibold text-muted-foreground">DESKTOP</span>
              </div>
              <div className="mt-3 space-y-1.5">
                <select
                  className={cn(selectClass, "h-7 bg-background py-0 text-xs")}
                  value={syncTargetId}
                  onChange={(event) => setSyncTargetId(event.target.value)}
                >
                  <option value="">Chọn target</option>
                  {syncTargets.map((target) => (
                    <option key={target.id} value={target.id}>
                      {target.name}
                    </option>
                  ))}
                </select>
                <Button size="sm" variant="outline" className="h-7 w-full rounded-md bg-card text-xs" onClick={exportToDesktop} disabled={exporting || !hasTargets}>
                  Export thư mục
                </Button>
              </div>
              {!syncTargets.length && <p className="pt-1 text-[11px] text-amber-700">Chưa có target — tạo ở Đồng bộ.</p>}
            </div>

            {/* Drive API */}
            <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-md bg-sky-500/10 text-sky-600">
                  <Cloud className="h-3.5 w-3.5" />
                </span>
                <div className="min-w-0">
                  <div className="text-xs font-semibold leading-none">Drive API</div>
                  <div className="text-[11px] leading-none text-muted-foreground">Không cần Desktop</div>
                </div>
                <span className="ml-auto rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] font-semibold text-muted-foreground">API</span>
              </div>
              <div className="mt-3 space-y-1.5">
                <select
                  className={cn(selectClass, "h-7 bg-background py-0 text-xs")}
                  value={accountId}
                  onChange={(event) => setAccountId(event.target.value)}
                >
                  <option value="">Chọn account</option>
                  {accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.account_email}
                    </option>
                  ))}
                </select>
                <Button size="sm" variant="outline" className="h-7 w-full rounded-md bg-card text-xs" onClick={exportToDriveApi} disabled={exporting || !hasTargets}>
                  Export qua API
                </Button>
              </div>
              {!accounts.length && <p className="pt-1 text-[11px] text-amber-700">Chưa có account — thêm ở Đồng bộ.</p>}
            </div>
          </div>

          {!hasTargets && (
            <div className="rounded-md border border-dashed bg-muted/20 px-3 py-2 text-center text-xs text-muted-foreground">
              Không có patch sẵn sàng — kiểm tra bộ lọc ở bảng trên.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Kaggle — bottom fixed nav (khoảng trống cuối trang do BookDetail padding giữ) */}
      <div
        className={cn(
          "fixed inset-x-0 z-30 border-t border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80",
          hasSelectionBar ? "bottom-[52px] sm:bottom-[48px]" : "bottom-0",
          "md:left-64"
        )}
        role="toolbar"
        aria-label="Kaggle automation"
      >
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-2 px-3 py-2 sm:flex-nowrap sm:gap-3 sm:px-4 lg:px-6">
          {/* Left: identity */}
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-amber-500 text-white shadow-sm">
              <Gauge className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0 leading-none">
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-bold tracking-tight">Kaggle</span>
                <span className="hidden rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-widest text-amber-700 sm:inline">AUTO</span>
                <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[11px] font-medium text-muted-foreground">
                  {targetIds.length} patch
                </span>
              </div>
              <div className="hidden text-[11px] leading-none text-muted-foreground sm:block">
                {kaggleAccounts.length ? `${kaggleAccounts.length} account · Queue` : "Chưa có tài khoản Kaggle"}
              </div>
            </div>
          </div>

          {/* Middle: automation controls — luôn per_patch, bỏ select after_all */}
          <div className="flex w-full items-center gap-2 sm:w-auto sm:flex-1 sm:justify-center">
            <div className="flex flex-wrap items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2 py-1 sm:flex-nowrap sm:gap-2 sm:px-3">
              <CheckField
                checked={kaggleAutoVideo}
                disabled={kaggleAutoUpload}
                onChange={setKaggleAutoVideo}
                label="Dựng video"
              />
              <span className="hidden h-3 w-px bg-border sm:block" aria-hidden />
              <CheckField
                checked={kaggleAutoUpload}
                onChange={(value) => {
                  setKaggleAutoUpload(value);
                  if (value) setKaggleAutoVideo(true);
                }}
                label="Up YouTube"
              />
              <span className="hidden h-3 w-px bg-border sm:block" aria-hidden />
              <span
                className="inline-flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700"
                title="Mỗi patch xong audio sẽ chuỗi ngay sang video/YouTube"
              >
                Chuỗi từng patch
              </span>
            </div>
            {!kaggleAccounts.length && (
              <span className="text-[11px] leading-tight text-amber-700 sm:hidden">Thêm ở Drive & Đồng bộ</span>
            )}
          </div>

          {/* Right: action */}
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {!kaggleAccounts.length && (
              <span className="hidden max-w-[22ch] text-right text-[11px] leading-tight text-muted-foreground sm:block">
                Thêm tài khoản ở <b className="font-semibold text-foreground">Drive & Đồng bộ</b> (tab Kaggle)
              </span>
            )}
            <Button
              size="sm"
              onClick={exportToKaggle}
              disabled={exporting || !hasTargets || !kaggleAccounts.length}
              className="h-8 gap-1.5 rounded-full bg-amber-500 px-4 text-xs font-semibold text-white shadow-sm hover:bg-amber-600"
            >
              <Gauge className="h-3.5 w-3.5" /> {exporting ? "Đang gửi..." : `Chạy Kaggle · ${targetIds.length}`}
            </Button>
          </div>
        </div>
      </div>
    </>
  );
}
