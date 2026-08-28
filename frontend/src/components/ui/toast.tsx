import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { cn } from "@/lib/utils";

type ToastKind = "info" | "success" | "error";
type ToastItem = { id: number; kind: ToastKind; message: string };
type ToastInput = { title?: string; message?: string; kind?: ToastKind };

const ToastContext = createContext<(toast: string | ToastInput, kind?: ToastKind) => void>(() => {});
export const useToast = () => useContext(ToastContext);

const kindStyle: Record<ToastKind, string> = {
  info: "border-border bg-background text-foreground",
  success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  error: "border-red-200 bg-red-50 text-red-900",
};

const kindIcon: Record<ToastKind, React.ElementType> = {
  info: Info,
  success: CheckCircle2,
  error: AlertTriangle,
};

const kindIconClass: Record<ToastKind, string> = {
  info: "text-primary",
  success: "text-emerald-600",
  error: "text-red-600",
};

let nextId = 1;

/** Toàn cục đơn giản: gọi từ bất cứ đâu, không cần Provider ở root. */
export function showToast(toast: string | ToastInput, kind?: ToastKind) {
  window.dispatchEvent(
    new CustomEvent<ToastItem>("studio:toast", {
      detail: {
        id: nextId++,
        kind:
          kind ??
          (typeof toast === "string" ? "info" : toast.kind ?? "info"),
        message: typeof toast === "string" ? toast : toast.message ?? toast.title ?? "",
      },
    })
  );
}

/** Viewport toàn cục — mount một lần ở App. Nằm trên cả backdrop modal (z-50). */
export function ToastViewport() {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    const onToast = (event: Event) => {
      const toast = (event as CustomEvent<ToastItem>).detail;
      setItems((current) => [...current, toast]);
      timers.current.set(
        toast.id,
        setTimeout(() => setItems((current) => current.filter((item) => item.id !== toast.id)), 4500)
      );
    };
    window.addEventListener("studio:toast", onToast);
    return () => {
      window.removeEventListener("studio:toast", onToast);
      timers.current.forEach((timer) => clearTimeout(timer));
      timers.current.clear();
    };
  }, []);

  const dismiss = (id: number) => {
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setItems((current) => current.filter((item) => item.id !== id));
  };

  return (
    <div className="pointer-events-none fixed inset-0 z-[100] flex flex-col items-center justify-end gap-2 p-4 sm:items-end sm:p-6">
      {items.map((item) => {
        const Icon = kindIcon[item.kind];
        return (
          <div
            key={item.id}
            role="status"
            className={cn(
              "pointer-events-auto flex w-full max-w-md items-start gap-2.5 rounded-md border px-3.5 py-3 text-xs shadow-lg toast-in",
              kindStyle[item.kind]
            )}
          >
            <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", kindIconClass[item.kind])} />
            <span className="min-w-0 flex-1 break-words leading-5">{item.message}</span>
            <button
              onClick={() => dismiss(item.id)}
              className="shrink-0 rounded-sm opacity-60 transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="Đóng"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const toast = useCallback((toast: string | ToastInput, kind?: ToastKind) => showToast(toast, kind), []);
  return <ToastContext.Provider value={toast}>{children}</ToastContext.Provider>;
}
