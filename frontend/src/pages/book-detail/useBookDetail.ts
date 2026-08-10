import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, VoiceItem } from "@/api";
import { BookStatus, Detail, ExportContext, OnlineVoice, TtsModel, VoiceOption, errorText } from "./types";

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

const EMPTY_EXPORTS: ExportContext = { exports: [], sync_targets: [], accounts: [] };

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

  useEffect(() => {
    if (!selectedModel || selectedModel.supports_reference) {
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
  }, [modelId, selectedModel]);

  const voiceOptions = useMemo<VoiceOption[]>(() => {
    if (selectedModel && !selectedModel.supports_reference) {
      return onlineVoices.map((voice) => ({ value: voice.id, label: voice.label || voice.id }));
    }
    const names = new Set([...localVoices.map((voice) => voice.name), currentVoiceName]);
    return Array.from(names)
      .filter(Boolean)
      .map((name) => ({ value: name, label: name }));
  }, [selectedModel, onlineVoices, localVoices, currentVoiceName]);

  return { ttsModels, voiceOptions, currentVoiceName };
}
