import React, { useEffect, useState } from "react";
import { AlertTriangle, Info } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type ConfirmKind = "info" | "warning";
type ConfirmOptions = {
  title: string;
  message?: string;
  kind?: ConfirmKind;
  confirmLabel?: string;
  cancelLabel?: string;
};
type ConfirmRequest = ConfirmOptions & {
  id: number;
  kind: ConfirmKind;
  resolve: (confirmed: boolean) => void;
};

const kindIcon: Record<ConfirmKind, React.ElementType> = {
  info: Info,
  warning: AlertTriangle,
};

const kindIconClass: Record<ConfirmKind, string> = {
  info: "text-primary",
  warning: "text-amber-600",
};

let nextId = 1;
let pending: ConfirmRequest | undefined;
const listeners = new Set<() => void>();

function publish() {
  listeners.forEach((listener) => listener());
}

/** Confirm kiểu SweetAlert2: popup nhỏ ở giữa màn hình, trên mọi modal.
 *  Trả Promise<boolean>. Chỉ hiển thị một popup tại một thời điểm — nếu gọi
 *  tiếp khi đang có popup mở thì popup cũ bị từ chối. */
export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  if (pending) pending.resolve(false);
  const request = { ...options, id: nextId++, kind: options.kind ?? "warning" } as ConfirmRequest;
  pending = request;
  publish();
  return new Promise<boolean>((resolve) => {
    request.resolve = (confirmed) => {
      pending = undefined;
      publish();
      resolve(confirmed);
    };
  });
}

/** Host toàn cục — mount một lần ở App. Popup nằm trên cả backdrop modal (z-50). */
export function ConfirmDialogHost() {
  const [request, setRequest] = useState<ConfirmRequest>();

  useEffect(() => {
    const update = () => setRequest(pending);
    listeners.add(update);
    update();
    return () => {
      listeners.delete(update);
    };
  }, []);

  const decide = (confirmed: boolean) => {
    if (pending) pending.resolve(confirmed);
  };

  const Icon = request ? kindIcon[request.kind] : Info;

  return (
    <div
      className={cn(
        "fixed inset-0 z-[100] flex items-center justify-center p-4 transition-colors",
        request ? "pointer-events-auto bg-black/40" : "pointer-events-none bg-transparent"
      )}
      onClick={() => decide(false)}
    >
      {request && (
        <div
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="confirm-dialog-title"
          className="pointer-events-auto w-full max-w-sm overflow-hidden rounded-md border border-border bg-card shadow-xl toast-in"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="flex items-start gap-2.5 border-b border-border px-5 py-4">
            <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", kindIconClass[request.kind])} />
            <div className="min-w-0">
              <h2 id="confirm-dialog-title" className="text-sm font-semibold leading-snug text-foreground">
                {request.title}
              </h2>
              {request.message && (
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{request.message}</p>
              )}
            </div>
          </div>
          <div className="flex flex-row-reverse items-center justify-start gap-2 px-5 py-4">
            <Button autoFocus onClick={() => decide(true)}>
              {request.confirmLabel ?? "Xác nhận"}
            </Button>
            <Button variant="outline" onClick={() => decide(false)}>
              {request.cancelLabel ?? "Hủy"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
