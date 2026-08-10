import React from "react";
import { Link } from "react-router-dom";
import { AlertOctagon, Home } from "lucide-react";
import { Header } from "@/components/common/Header";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function NotFound() {
  return (
    <div className="max-w-md mx-auto space-y-6 pt-12">
      <Card className="border-border text-center">
        <CardContent className="p-8 space-y-4">
          <div className="h-12 w-12 rounded-full bg-red-100 text-red-600 flex items-center justify-center mx-auto">
            <AlertOctagon className="h-6 w-6" />
          </div>
          <div className="space-y-1">
            <h2 className="text-xl font-bold text-foreground">404 - Không tìm thấy khu vực</h2>
            <p className="text-xs text-muted-foreground">
              Đường dẫn bạn yêu cầu không tồn tại hoặc đã được di chuyển trong xưởng.
            </p>
          </div>
          <div className="pt-2">
            <Button asChild variant="default" size="sm" className="gap-2">
              <Link to="/">
                <Home className="h-4 w-4" /> Về bàn làm việc
              </Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
