import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, VoiceItem } from "@/api";
import {
  BookStatus,
  ChaptersValidation,
  Detail,
  ExportContext,
  OnlineVoice,
  TtsModel,
  VoiceOption,
  errorText,
  presetVoiceOptions,
} from "./types";

const POLL_MS = 5000;

/**
 * State chỉ đổi khi payload thực sự khác lần trước.
 * Nhờ vậy vòng polling không tạo object mới mỗi nhịp -> bảng không render lại,
 * không mất lựa chọn checkbox, không nhảy vị trí cuộn.
 */
function useSettledState<T>(initial: T) {
  const [value, setValue] = useState<T>(initial);
  const snapshot = useRef<string | undefined>(undefined);

  const commit = useCallback((next: T) => {
    const serialized = JSON.stringify(next);
    if (serialized === snapshot.current) return;
    snapshot.current = serialized;
    setValue(next);
  }, []);

  return [value, commit] as const;
}

export type BookDetailData = {
  data?: Detail;
  exports: ExportContext;
  pipeline?: BookStatus;
  loading: boolean;
  error: string;
  live: boolean;
  setLive: (value: boolean) => void;
  updatedAt?: number;
  refreshing: boolean;
  refresh: () => Promise<void>;
};

const EMPTY_EXPORTS: ExportContext = { exports: [], sync_targets: [], accounts: [], kaggle_accounts: [] };

/**
 * Tải hồ sơ sách + trạng thái pipeline và tự làm mới nền.
 * Polling tự dừng khi tab ẩn hoặc khi `paused` (đang mở dialog / đang chạy thao tác).
 */
export function useBookDetail(id: string | undefined, paused: boolean): BookDetailData {
  const [data, commitData] = useSettledState<Detail | undefined>(undefined);
  const [exports, commitExports] = useSettledState<ExportContext>(EMPTY_EXPORTS);
  const [pipeline, commitPipeline] = useSettledState<BookStatus | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [live, setLive] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number>();

  const inFlight = useRef(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const refresh = useCallback(async () => {
    if (!id || inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const [book, exportContext, status] = await Promise.all([
        api<Detail>(`/api/ui/books/${id}`),
        api<ExportContext>(`/api/ui/books/${id}/exports`),
        api<BookStatus>(`/books/${id}/status`).catch(() => undefined),
      ]);
      commitData(book);
      commitExports(exportContext);
      if (status) commitPipeline(status);
      setUpdatedAt(Date.now());
      setError("");
    } catch (err) {
      setError(errorText(err));
    } finally {
      inFlight.current = false;
      setRefreshing(false);
      setLoading(false);
    }
  }, [id, commitData, commitExports, commitPipeline]);

  useEffect(() => {
    if (!id) return;
    let stopped = false;
    let timer = 0;

    const schedule = () => {
      if (stopped) return;
      timer = window.setTimeout(async () => {
        if (live && !document.hidden && !pausedRef.current) await refresh();
        schedule();
      }, POLL_MS);
    };

    refresh();
    schedule();

    const onVisibility = () => {
      if (!document.hidden && live && !pausedRef.current) refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [id, live, refresh]);

  return { data, exports, pipeline, loading, error, live, setLive, updatedAt, refreshing, refresh };
}

/**
 * Kiểm tra chương (định dạng tiêu đề + đánh số + lỗi TTS) cho toàn sách.
 * Không polling — chỉ tải khi mount và khi `reload()` được gọi sau một thao tác ghi,
 * vì soát toàn bộ chương là chi phí không nhỏ trên sách nhiều chương và dữ liệu chỉ
 * đổi khi có ghi, không đổi theo nhịp 5s như phần còn lại của trang.
 */
export function useChapterValidation(id: string | undefined) {
  const [report, setReport] = useState<ChaptersValidation>();
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    try {
      setReport(await api<ChaptersValidation>(`/books/${id}/chapters/validation`));
    } catch {
      // im lặng: phần này chỉ bổ trợ, không nên chặn phần còn lại của trang
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { report, loading, reload };
}

export type TtsOptions = {
  ttsModels: TtsModel[];
  voiceOptions: VoiceOption[];
  currentVoiceName: string;
};

/** Danh sách model TTS + voice khả dụng theo model đang chọn. */
export function useTtsOptions(data: Detail | undefined, modelId: string): TtsOptions {
  const [localVoices, setLocalVoices] = useState<VoiceItem[]>([]);
  const [onlineVoices, setOnlineVoices] = useState<OnlineVoice[]>([]);

  const ttsModels = useMemo(() => data?.tts_models || [], [data?.tts_models]);
  const selectedModel = ttsModels.find((model) => model.id === modelId) || null;
  const currentVoiceName = data?.book.voice_clip_path
    ? data.book.voice_clip_path.split(/[/\\]/).pop() || ""
    : "";

  useEffect(() => {
    api<{ voices: VoiceItem[] }>("/api/ui/media")
      .then((res) => setLocalVoices(res.voices || []))
      .catch(() => {});
  }, []);

  // Model mang sẵn danh sách giọng (ZeroTTS) thì dùng luôn, khỏi gọi mạng.
  const builtInVoices = useMemo(() => selectedModel?.voices || [], [selectedModel]);

  useEffect(() => {
    if (!selectedModel || selectedModel.supports_reference || builtInVoices.length) {
      setOnlineVoices([]);
      return;
    }
    let cancelled = false;
    api<{ voices: OnlineVoice[] }>(`/text-studio/light-tts/voices?backend=${encodeURIComponent(modelId)}`)
      .then((res) => !cancelled && setOnlineVoices(res.voices || []))
      .catch(() => !cancelled && setOnlineVoices([]));
    return () => {
      cancelled = true;
    };
  }, [modelId, selectedModel, builtInVoices]);

  const voiceOptions = useMemo<VoiceOption[]>(() => {
    if (builtInVoices.length) {
      return builtInVoices.map((voice) => ({ value: voice.id, label: voice.label || voice.id }));
    }
    if (selectedModel && !selectedModel.supports_reference) {
      if (onlineVoices.length) {
        return onlineVoices.map((voice) => ({ value: voice.id, label: voice.label || voice.id }));
      }
      // Model chọn giọng nhưng chưa liệt kê được (chưa tải weights / chưa cài package):
      // ít nhất vẫn đưa ra giọng mặc định thay vì một dropdown trống không lời giải thích.
      return selectedModel.default_voice
        ? [{ value: selectedModel.default_voice, label: selectedModel.default_voice }]
        : [];
    }
    // Model clone: audio mẫu đã upload trong thư viện, cộng thêm giọng preset của
    // VieNeu/ZeroTTS — chọn preset thì app tự sinh clip mẫu để clone, khỏi cần thu âm.
    const names = new Set([...localVoices.map((voice) => voice.name), currentVoiceName]);
    return [
      ...Array.from(names)
        .filter(Boolean)
        .map((name) => ({ value: name, label: name })),
      ...presetVoiceOptions(ttsModels),
    ];
  }, [selectedModel, builtInVoices, onlineVoices, localVoices, currentVoiceName, ttsModels]);

  return { ttsModels, voiceOptions, currentVoiceName };
}
