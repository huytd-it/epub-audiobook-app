import React from "react";
import { Route, Routes } from "react-router-dom";
import { Shell } from "@/components/layout/Shell";
import { Dashboard } from "@/pages/Dashboard";
import { Books } from "@/pages/Books";
import { Upload } from "@/pages/Upload";
import { BookDetail } from "@/pages/BookDetail";
import { Queue } from "@/pages/Queue";
import { Video } from "@/pages/Video";
import { MediaPage } from "@/pages/MediaPage";
import { MusicPage } from "@/pages/MusicPage";
import { PhotosPage } from "@/pages/PhotosPage";
import { VoicesPage } from "@/pages/VoicesPage";
import { YouTubePage } from "@/pages/YouTubePage";
import { DrivePage } from "@/pages/DrivePage";
import { DatabaseIoPage } from "@/pages/DatabaseIoPage";
import { FlowsPage } from "@/pages/FlowsPage";
import { LogsPage } from "@/pages/LogsPage";
import { EffectsPage } from "@/pages/EffectsPage";
import { Tools } from "@/pages/Tools";
import { ProductionSettingsPage } from "@/pages/ProductionSettingsPage";
import { GameplayPage } from "@/pages/GameplayPage";
import { NotFound } from "@/pages/NotFound";

function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/books" element={<Books />} />
        <Route path="/books/upload" element={<Upload />} />
        <Route path="/upload" element={<Upload />} />
        <Route path="/books/:id/*" element={<BookDetail />} />
        <Route path="/queue" element={<Queue />} />
        <Route path="/video" element={<Video />} />
        <Route path="/music" element={<MusicPage />} />
        <Route path="/photos" element={<PhotosPage />} />
        <Route path="/voices" element={<VoicesPage />} />
        <Route path="/media" element={<MediaPage />} />
        <Route path="/tools" element={<Tools />} />
        <Route path="/youtube" element={<YouTubePage />} />
        <Route path="/drive" element={<DrivePage />} />
        <Route path="/database-io" element={<DatabaseIoPage />} />
        <Route path="/flows" element={<FlowsPage />} />
        <Route path="/logs" element={<LogsPage />} />
        <Route path="/effects" element={<EffectsPage />} />
        <Route path="/production-defaults" element={<ProductionSettingsPage />} />
        <Route path="/gameplay" element={<GameplayPage />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Shell>
  );
}

export default App;
