import React, { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { UploadCloud, FileText, CheckCircle2, AlertCircle } from "lucide-react";
import { Header } from "@/components/common/Header";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function Upload() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError("");
    setBusy(true);

    const form = new FormData(e.currentTarget);
    try {
      const response = await fetch("/books/upload", {
        method: "POST",
        body: form,
      });

      if (!response.ok) {
        throw new Error(`Tải tệp thất bại (Mã lỗi ${response.status})`);
      }

      const match = response.url.match(/books\/(\d+)/);
      const id = match?.[1];
      navigate(id ? `/books/${id}` : "/books");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <Header
        title="Đưa một cuốn sách vào xưởng"
        subtitle="Chọn tệp EPUB từ máy tính. Dây chuyền tự động sẽ giải mã cấu trúc, trích xuất mục lục và phân đoạn văn bản."
      />

      <Card className="border-border bg-card">
        <CardContent className="p-6 sm:p-10">
          <form onSubmit={submit} className="space-y-6">
            <div className="border-2 border-dashed border-border hover:border-primary/60 transition-colors rounded-lg p-8 sm:p-12 text-center bg-background/50 flex flex-col items-center justify-center space-y-4">
              <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center text-primary">
                <UploadCloud className="h-8 w-8" />
              </div>

              <div className="space-y-1 max-w-md">
                <h3 className="text-base font-bold text-foreground">Kéo thả tệp EPUB vào đây</h3>
                <p className="text-xs text-muted-foreground">
                  Hỗ trợ định dạng .epub tiêu chuẩn. Dung lượng tối đa phụ thuộc cấu hình hệ thống.
                </p>
              </div>

              <div className="pt-2">
                <label className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-secondary hover:bg-secondary/80 text-secondary-foreground text-xs font-semibold cursor-pointer border border-border transition-colors">
                  <FileText className="h-4 w-4 text-primary" />
                  <span>{selectedFile ? selectedFile.name : "Duyệt tìm tệp EPUB..."}</span>
                  <input
                    name="epub_file"
                    type="file"
                    accept=".epub"
                    required
                    className="hidden"
                    onChange={(e) => {
                      if (e.target.files && e.target.files[0]) {
                        setSelectedFile(e.target.files[0]);
                      }
                    }}
                  />
                </label>
              </div>

              {selectedFile && (
                <div className="flex items-center gap-2 text-xs font-mono text-emerald-600 bg-emerald-50 px-3 py-1.5 rounded border border-emerald-200">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  <span>Đã chọn: {selectedFile.name} ({(selectedFile.size / 1024 / 1024).toFixed(2)} MB)</span>
                </div>
              )}
            </div>

            {error && (
              <div className="p-3 rounded-md bg-red-50 border border-red-200 text-red-700 text-xs flex items-center gap-2 font-mono">
                <AlertCircle className="h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <div className="flex items-center justify-end gap-3 pt-2">
              <Button type="button" variant="outline" onClick={() => navigate("/books")} disabled={busy}>
                Hủy bỏ
              </Button>
              <Button type="submit" variant="accent" disabled={busy || !selectedFile} className="min-w-[140px]">
                {busy ? (
                  <span className="flex items-center gap-2">
                    <span className="h-3.5 w-3.5 border-2 border-foreground border-t-transparent rounded-full animate-spin" />
                    Đang phân tích...
                  </span>
                ) : (
                  "Bắt đầu phân tích"
                )}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
