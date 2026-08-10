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
import { Tools } from "@/pages/Tools";
import { LegacyTool } from "@/pages/LegacyTool";
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
        <Route path="/music" element={<MediaPage />} />
        <Route path="/photos" element={<MediaPage />} />
        <Route path="/voices" element={<MediaPage />} />
        <Route path="/media" element={<MediaPage />} />
        <Route path="/tools" element={<Tools />} />
        <Route path="/youtube" element={<LegacyTool />} />
        <Route path="/drive" element={<LegacyTool />} />
        <Route path="/database-io" element={<LegacyTool />} />
        <Route path="/flows" element={<LegacyTool />} />
        <Route path="/logs" element={<LegacyTool />} />
        <Route path="/effects" element={<LegacyTool />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Shell>
  );
}

export default App;
