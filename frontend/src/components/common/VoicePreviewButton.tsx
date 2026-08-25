import React, { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Loader2, Pause, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type Props = {
  modelId: string;
  voiceId: string;
  ttsOptions?: Record<string, string | number>;
  className?: string;
};

export function VoicePreviewButton({ modelId, voiceId, ttsOptions, className }: Props) {
  const [state, setState] = useState<"idle" | "loading" | "playing" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  const cleanup = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
  }, []);

  useEffect(() => cleanup, [cleanup]);

  const stop = useCallback(() => {
    cleanup();
    setState("idle");
  }, [cleanup]);

  const play = useCallback(async () => {
    if (state === "playing") {
      stop();
      return;
    }
    cleanup();
    setState("loading");
    setErrorMsg("");
    try {
      const response = await fetch("/api/ui/voice-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId, voice_id: voiceId || "", tts_options: ttsOptions || {} }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({ detail: `Lỗi ${response.status}` }));
        throw new Error(body.detail || `Lỗi ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      objectUrlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.onended = () => {
        setState("idle");
        cleanup();
      };
      audio.onerror = () => {
        setErrorMsg("Không thể phát audio");
        setState("error");
        cleanup();
      };
      await audio.play();
      setState("playing");
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Lỗi không xác định");
      setState("error");
      cleanup();
    }
  }, [modelId, voiceId, ttsOptions, state, stop, cleanup]);

  const hasVoice = Boolean(modelId);
  const label = state === "playing" ? "Dừng nghe" : state === "loading" ? "Đang tổng hợp..." : "Nghe mẫu giọng";
  const title = state === "error" ? errorMsg : label;

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={cn(
        "h-8 w-8 shrink-0",
        state === "error" && "text-destructive",
        state === "playing" && "text-primary",
        className
      )}
      disabled={!hasVoice || state === "loading"}
      onClick={play}
      title={title}
      aria-label={label}
      aria-busy={state === "loading"}
    >
      {state === "loading" ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : state === "playing" ? (
        <Pause className="h-3.5 w-3.5" />
      ) : state === "error" ? (
        <AlertCircle className="h-3.5 w-3.5" />
      ) : (
        <Play className="h-3.5 w-3.5" />
      )}
    </Button>
  );
}
