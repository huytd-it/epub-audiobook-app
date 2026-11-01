import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Download, FileJson, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { TextBundleImportDialog } from "@/components/common/TextBundleImport";
import { downloadUrl, errorText } from "./types";
import { SectionHead } from "./parts";

/**
 * Di chuyển phần text của sách sang máy khác (hoặc nhận lại từ máy khác).
 * Chỉ text: không kèm audio/video/ảnh/EPUB nên file nhỏ và không làm hỏng
 * thành quả TTS đã có trên máy này.
 */
export function TextBundlePanel({
  bookId,
  bookTitle,
  onMessage,
  onRefresh,
  onBusyChange,
}: {
  bookId: string;
  bookTitle: string;
  onMessage: (message: string, kind?: "info" | "success" | "error") => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const navigate = useNavigate();
  const [downloading, setDownloading] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const download = async () => {
    setDownloading(true);
    onBusyChange(true);
    try {
      await downloadUrl(`/books/${bookId}/text-bundle`, `${bookId}.book-text.json`);
      onMessage("Đã tải file text của sách.", "success");
    } catch (error) {
      onMessage(errorText(error), "error");
    } finally {
      setDownloading(false);
      onBusyChange(false);
    }
  };

  return (
    <Card>
      <CardHeader className="border-b border-border bg-muted/20">
        <SectionHead
          icon={FileJson}
          title="Text sách · chuyển máy"
          detail="Xuất/nhập mục lục đầy đủ, nội dung chương, patch và luật thay thế dưới dạng JSON — không kèm audio, video, ảnh hay EPUB."
          action={
            <span className="shrink-0 rounded-md bg-primary/10 px-2 py-1 font-mono text-[11px] text-primary">
              .book-text.json
            </span>
          }
        />
      </CardHeader>

      <CardContent className="space-y-4 pt-5">
        <p className="text-xs text-muted-foreground">
          Dùng khi muốn sửa text ở máy khác rồi mang về. File xuất từ máy này khi nhập lại sẽ gộp vào đúng sách này;
          audio/video đã tạo vẫn được giữ. Patch nào đổi nội dung sẽ được đưa về trạng thái chờ để tạo lại audio.
        </p>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-2">
            <span className="block text-[11px] font-medium text-muted-foreground">Xuất text khỏi máy này</span>
            <Button className="w-full" size="sm" onClick={download} disabled={downloading}>
              <Download className="h-3.5 w-3.5" /> {downloading ? "Đang tải..." : "Tải file text"}
            </Button>
          </div>
          <div className="space-y-2">
            <span className="block text-[11px] font-medium text-muted-foreground">Nhập text từ máy khác</span>
            <Button
              className="w-full"
              size="sm"
              variant="outline"
              onClick={() => setImportOpen(true)}
              disabled={downloading}
            >
              <Upload className="h-3.5 w-3.5" /> Nhập file text
            </Button>
          </div>
        </div>

        <p className="flex items-center gap-1.5 border-t border-border pt-3 text-[11px] text-muted-foreground">
          Muốn chuyển cả sách sang máy mới? <ArrowRight className="h-3 w-3" /> nhập file này ở trang Thư viện để tạo
          sách mới, rồi chạy TTS tại đó.
        </p>
      </CardContent>

      <TextBundleImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        targetBookId={Number(bookId)}
        targetBookTitle={bookTitle}
        onMessage={onMessage}
        onImported={(summary) => {
          void onRefresh();
          if (summary.book_id !== Number(bookId)) {
            navigate(`/books/${summary.book_id}`);
          }
        }}
      />
    </Card>
  );
}
