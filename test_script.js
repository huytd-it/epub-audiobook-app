
(function() {
    const API = {
        uploadBatch: '/video/upload-batch',
        generateBatch: '/video/generate-batch',
        backgrounds: '/video/backgrounds',
        uploadBg: '/video/upload-background',
        previewBg: '/video/backgrounds/preview',
    };

    let batchId = null;
    let batchFiles = [];
    let backgrounds = [];

    // Dropzone for audio files
    (function() {
        const dz = document.getElementById('dropzone-audio');
        const input = document.getElementById('audio-files');
        const preview = document.getElementById('audio-preview');
        if (!dz || !input) return;
        ['dragenter','dragover'].forEach(e => dz.addEventListener(e, ev => { ev.preventDefault(); dz.classList.add('dragover'); }));
        ['dragleave','drop'].forEach(e => dz.addEventListener(e, ev => { ev.preventDefault(); dz.classList.remove('dragover'); }));
        dz.addEventListener('drop', ev => {
            const files = ev.dataTransfer.files;
            if (files.length) { input.files = files; input.dispatchEvent(new Event('change')); }
        });
        input.addEventListener('change', function() {
            if (!this.files.length) return;
            const names = Array.from(this.files).map(f => f.name).join(', ');
            preview.innerHTML = '<span style="font-size:var(--font-size-sm);color:var(--text-secondary)">&#127925; ' + names + '</span>';
            preview.style.display = 'inline-block';
        });
    })();

    async function loadBackgrounds() {
        try {
            const res = await fetch(API.backgrounds);
            const data = await res.json();
            backgrounds = data.backgrounds || [];
            refreshBgSelects();
            refreshAllPreviews();
        } catch(e) { console.error('Failed to load backgrounds', e); }
    }

    function refreshBgSelects() {
        document.querySelectorAll('.bg-select').forEach(sel => {
            const current = sel.value;
            sel.innerHTML = '<option value="">-- Default --</option>';
            backgrounds.forEach(bg => {
                const opt = document.createElement('option');
                opt.value = bg.path;
                opt.textContent = bg.is_default ? '(Default) ' + bg.name.split('/').pop() : bg.name;
                sel.appendChild(opt);
            });
            if (current) sel.value = current;
        });
    }

    function updateSelectedCount() {
        const checks = document.querySelectorAll('.row-check');
        const checked = [...checks].filter(c => c.checked).length;
        document.getElementById('selected-count').textContent = checked + ' selected';
    }

    function renderTable(files) {
        const tbody = document.getElementById('file-table-body');
        tbody.innerHTML = '';
        files.forEach((f, i) => {
            const tr = document.createElement('tr');
            tr.dataset.index = f.index;
            tr.innerHTML = `
                <td class="col-check"><input type="checkbox" class="row-check" data-index="${f.index}" checked></td>
                <td>${i + 1}</td>
                <td>${escHtml(f.name)}</td>
                <td>${f.size_mb} MB</td>
                <td>
                    <div class="bg-controls">
                        <select class="bg-select" data-index="${f.index}">
                            <option value="">-- Default --</option>
                        </select>
                        <input type="file" class="bg-upload-inline" data-index="${f.index}" accept=".jpg,.jpeg,.png,.webp">
                    </div>
                </td>
                <td class="col-preview">
                    <img class="bg-preview-img" data-index="${f.index}"
                         src="" alt="background preview" loading="lazy">
                    <div class="bg-preview-name" data-index="${f.index}">Default</div>
                </td>
                <td class="status-pending" data-status="${f.index}">Ready</td>
            `;
            tbody.appendChild(tr);
        });
        refreshBgSelects();
        refreshAllPreviews();
        updateSelectedCount();

        tbody.querySelectorAll('.row-check').forEach(cb => {
            cb.addEventListener('change', updateSelectedCount);
        });

        document.getElementById('select-all').addEventListener('change', function() {
            tbody.querySelectorAll('.row-check').forEach(c => { c.checked = this.checked; });
            updateSelectedCount();
        });

        tbody.querySelectorAll('.bg-select').forEach(sel => {
            sel.addEventListener('change', function() {
                updatePreview(this.dataset.index);
            });
        });

        tbody.querySelectorAll('.bg-upload-inline').forEach(input => {
            input.addEventListener('change', async function() {
                const idx = this.dataset.index;
                const file = this.files[0];
                if (!file) return;
                const fd = new FormData();
                fd.append('file', file);
                const statusEl = document.querySelector(`[data-status="${idx}"]`);
                statusEl.textContent = 'Uploading bg...';
                statusEl.className = 'status-pending';
                try {
                    const res = await fetch(API.uploadBg, { method: 'POST', body: fd });
                    const data = await res.json();
                    backgrounds.push({ name: data.name, path: data.path, is_default: false });
                    refreshBgSelects();
                    const sel = document.querySelector(`.bg-select[data-index="${idx}"]`);
                    sel.value = data.path;
                    updatePreview(idx);
                    statusEl.textContent = 'Ready';
                } catch(e) {
                    statusEl.textContent = 'BG upload failed';
                    statusEl.className = 'status-err';
                }
            });
        });
    }

    function defaultBackgroundPath() {
        const def = backgrounds.find(b => b.is_default);
        return def ? def.path : '';
    }

    function bgNameForPath(p) {
        if (!p) return 'Default';
        const found = backgrounds.find(b => b.path === p);
        if (found) return found.is_default ? 'Default' : found.name;
        try {
            return p.split(/[\\/]/).pop() || p;
        } catch (_) { return p; }
    }

    function previewUrlFor(p) {
        if (!p) return '';
        return API.previewBg + '?path=' + encodeURIComponent(p);
    }

    function updatePreview(idx) {
        const sel = document.querySelector(`.bg-select[data-index="${idx}"]`);
        const img = document.querySelector(`.bg-preview-img[data-index="${idx}"]`);
        const name = document.querySelector(`.bg-preview-name[data-index="${idx}"]`);
        if (!sel || !img || !name) return;
        const p = sel.value || defaultBackgroundPath();
        img.src = previewUrlFor(p);
        img.alt = 'Background: ' + bgNameForPath(p);
        name.textContent = bgNameForPath(p);
    }

    function refreshAllPreviews() {
        document.querySelectorAll('.bg-select').forEach(sel => updatePreview(sel.dataset.index));
    }

    function escHtml(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    document.getElementById('upload-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        const input = document.getElementById('audio-files');
        if (!input.files.length) return;

        const fd = new FormData();
        for (const f of input.files) fd.append('files', f);

        const btn = document.getElementById('btn-upload');
        btn.disabled = true;
        btn.textContent = 'Uploading...';

        try {
            const res = await fetch(API.uploadBatch, { method: 'POST', body: fd });
            const data = await res.json();
            batchId = data.batch_id;
            batchFiles = data.files;
            if (data.errors.length) {
                alert('Some files skipped:\n' + data.errors.join('\n'));
            }
            document.getElementById('step-table').style.display = '';
            renderTable(batchFiles);
        } catch(err) {
            alert('Upload failed: ' + err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Upload';
        }
    });

    document.getElementById('btn-upload-bg').addEventListener('click', async function() {
        const input = document.getElementById('new-bg-file');
        if (!input.files.length) return;
        const fd = new FormData();
        fd.append('file', input.files[0]);
        this.disabled = true;
        try {
            const res = await fetch(API.uploadBg, { method: 'POST', body: fd });
            const data = await res.json();
            backgrounds.push({ name: data.name, path: data.path, is_default: false });
            refreshBgSelects();
            input.value = '';
        } catch(e) {
            alert('Upload failed');
        } finally {
            this.disabled = false;
        }
    });

    document.getElementById('btn-generate').addEventListener('click', async function() {
        if (!batchId) return;
        const checks = document.querySelectorAll('.row-check');
        const selected = [...checks].filter(c => c.checked).map(c => parseInt(c.dataset.index));
        if (!selected.length) { alert('Chọn ít nhất 1 file'); return; }

        const backgrounds_map = {};
        document.querySelectorAll('.bg-select').forEach(sel => {
            backgrounds_map[sel.dataset.index] = sel.value || null;
        });

        const config = {
            resolution: document.getElementById('cfg-resolution').value,
            fps: parseInt(document.getElementById('cfg-fps').value),
            codec: document.getElementById('cfg-codec').value,
            audio_bitrate: document.getElementById('cfg-audio-bitrate').value,
            image_type: document.getElementById('cfg-image-type').value,
            crf: parseInt(document.getElementById('cfg-crf').value),
        };

        const btn = this;
        btn.disabled = true;
        btn.textContent = 'Generating...';

        document.getElementById('step-results').style.display = '';
        const resultsList = document.getElementById('results-list');
        resultsList.innerHTML = '<p>Processing...</p>';

        try {
            const res = await fetch(API.generateBatch, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ batch_id: batchId, selected, backgrounds: backgrounds_map, config }),
            });
            const data = await res.json();
            resultsList.innerHTML = '';
            data.results.forEach(r => {
                const div = document.createElement('div');
                div.className = 'result-item';
                if (r.status === 'done') {
                    div.innerHTML = `<span class="status-ok">Done</span> <span>${escHtml(r.name)}</span> <a href="${r.video_url}" download>Download (${r.size_mb} MB)</a> <button class="btn-yt-mini" onclick="uploadToYouTube('${r.video_url}', '${escHtml(r.name)}')">YouTube</button>`;
                    const statusEl = document.querySelector(`[data-status="${r.index}"]`);
                    if (statusEl) { statusEl.textContent = 'Done'; statusEl.className = 'status-ok'; }
                } else {
                    div.innerHTML = `<span class="status-err">Failed</span> <span>#${r.index}</span> <span class="status-err">${escHtml(r.message)}</span>`;
                    const statusEl = document.querySelector(`[data-status="${r.index}"]`);
                    if (statusEl) { statusEl.textContent = 'Error'; statusEl.className = 'status-err'; }
                }
                resultsList.appendChild(div);
            });
        } catch(err) {
            resultsList.innerHTML = `<div class="error-block"><p style="margin:0">${escHtml(err.message)}</p></div>`;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Generate Selected Videos';
        }
    });

    loadBackgrounds();
})();

async function uploadToYouTube(videoUrl, videoName) {
    const title = prompt('YouTube title:', videoName.replace(/\.[^.]+$/, ''));
    if (!title) return;
    const tags = prompt('Tags (comma separated):', 'audiobook,epub,video') || '';
    const privacy = prompt('Privacy (private/unlisted/public):', 'private') || 'private';

    const fd = new FormData();
    try {
        const resp = await fetch(videoUrl);
        const blob = await resp.blob();
        fd.append('file', blob, videoName);
        fd.append('title', title);
        fd.append('description', '');
        fd.append('tags', tags);
        fd.append('privacy_status', privacy);

        const res = await fetch('/youtube/upload-file', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.status === 'done') {
            alert('Upload thành công! Video ID: ' + data.youtube_video_id);
        } else {
            alert('Upload failed: ' + (data.error || 'Unknown error'));
        }
    } catch(err) {
        alert('Upload failed: ' + err.message);
    }
}
