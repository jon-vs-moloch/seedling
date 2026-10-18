/*
 * ZenCode UI.
 *
 * Live mode talks to the ZenCode server (/api/probe, /api/init, /api/chat,
 * /api/logs, /api/files, /api/ship_update). Demo mode (?demo) skips setup and
 * renders demo.json with a "Demo data" banner; nothing is sent anywhere.
 */
document.addEventListener('DOMContentLoaded', () => {
    const DEMO = new URLSearchParams(location.search).has('demo');
    const $ = (id) => document.getElementById(id);
    const setupScreen = $('setupScreen');
    const workspaceScreen = $('workspaceScreen');
    const probeBtn = $('probeBtn');
    const loader = $('probeLoader');
    const btnText = probeBtn.querySelector('.btn-text');
    const modelSelection = $('modelSelection');
    const modelSelect = $('modelSelect');
    const startBtn = $('startBtn');
    const errorMsg = $('errorMsg');
    const setupForm = $('setupForm');
    const chatHistory = $('chatHistory');
    const taskInput = $('taskInput');
    const sendTaskBtn = $('sendTaskBtn');
    const termOutput = $('termOutput');
    const fileTree = $('fileTree');
    const shipUpdateBtn = $('shipUpdateBtn');

    const forcedTheme = new URLSearchParams(location.search).get('theme');
    if (forcedTheme === 'light' || forcedTheme === 'dark') document.documentElement.dataset.theme = forcedTheme;

    function escapeHtml(value) {
        const div = document.createElement('div');
        div.textContent = String(value ?? '');
        return div.innerHTML;
    }

    // Model output is untrusted: escape everything, then render ``` fences as
    // <pre> and `inline` as <code>.
    function renderText(text) {
        const parts = String(text ?? '').split(/```[a-zA-Z0-9_-]*\n?/);
        return parts.map((part, i) => {
            if (i % 2 === 1) return `<pre>${escapeHtml(part.replace(/\n$/, ''))}</pre>`;
            return part.split('\n').filter((line, idx, all) => line.trim() || (idx > 0 && idx < all.length - 1))
                .map((line) => `<p>${escapeHtml(line).replace(/`([^`]+)`/g, '<code>$1</code>')}</p>`).join('');
        }).join('');
    }

    function formatBytes(n) {
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / 1024 / 1024).toFixed(1)} MB`;
    }

    // -----------------------------------------------------------
    // Status facts (sandbox mode, network) for setup and workspace
    // -----------------------------------------------------------
    function showStatus(status) {
        const sandbox = status.sandbox_mode === 'container'
            ? (status.container_runtime ? `container (${status.container_runtime})` : 'container: runtime missing, commands blocked')
            : `${status.sandbox_mode} (host shell, development only)`;
        $('factSandbox').textContent = sandbox;
        $('factSandbox').className = status.sandbox_mode === 'container' && status.container_runtime ? '' : 'warn';
        $('factNetwork').textContent = status.network === 'none' ? 'none (offline)' : status.network;
        $('sandboxChip').textContent = status.sandbox_mode === 'container'
            ? (status.container_runtime ? `container · network ${status.network}` : 'container runtime missing')
            : `${status.sandbox_mode} sandbox`;
        $('sandboxChip').className = `chip ${status.sandbox_mode === 'container' && status.container_runtime ? 'ok' : 'warn'}`;
        if (status.model_id) {
            $('modelChip').textContent = status.model_id;
            $('modelChip').className = 'chip info';
        }
    }

    // -----------------------------------------------------------
    // Setup: probe the endpoint, pick a model, initialize
    // -----------------------------------------------------------
    probeBtn.addEventListener('click', async () => {
        const urlInput = $('endpointUrl').value.trim().replace(/\/+$/, '');
        if (!urlInput) {
            errorMsg.textContent = 'Enter an endpoint URL first.';
            return;
        }
        errorMsg.textContent = '';
        btnText.classList.add('hidden');
        loader.classList.remove('hidden');
        try {
            const modelsUrl = urlInput.endsWith('/v1') ? `${urlInput}/models` : `${urlInput}/v1/models`;
            // Proxied through the ZenCode server to avoid CORS.
            const response = await fetch('/api/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: modelsUrl }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Could not list models at that endpoint.');
            if (!data.data || !data.data.length) throw new Error('The endpoint answered but lists no models.');
            modelSelect.innerHTML = '';
            data.data.forEach((m) => {
                const opt = document.createElement('option');
                opt.value = m.id;
                opt.textContent = m.id;
                modelSelect.appendChild(opt);
            });
            probeBtn.classList.add('hidden');
            modelSelection.classList.remove('hidden');
            startBtn.classList.remove('hidden');
        } catch (e) {
            errorMsg.textContent = e.message;
            btnText.classList.remove('hidden');
            loader.classList.add('hidden');
        }
    });

    setupForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const base_url = $('endpointUrl').value.trim().replace(/\/+$/, '');
        const model_id = modelSelect.value;
        startBtn.disabled = true;
        try {
            await fetch('/api/init', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ base_url, model_id }),
            });
            openWorkspace();
            refreshStatus();
        } catch (err) {
            errorMsg.textContent = 'Could not initialize the workspace.';
            startBtn.disabled = false;
        }
    });

    function openWorkspace() {
        setupScreen.classList.add('hidden');
        workspaceScreen.classList.remove('hidden');
        taskInput.focus();
    }

    // -----------------------------------------------------------
    // Sandbox views
    // -----------------------------------------------------------
    document.querySelectorAll('.toggle-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.toggle-btn').forEach((b) => b.classList.toggle('active', b === btn));
            $('terminalView').classList.toggle('hidden', btn.dataset.view !== 'terminal');
            $('filesView').classList.toggle('hidden', btn.dataset.view !== 'files');
            if (btn.dataset.view === 'files') refreshFiles();
        });
    });

    function logTerm(text, type = 'log-out') {
        const div = document.createElement('div');
        div.className = `log-entry ${type}`;
        div.textContent = type === 'log-cmd' ? text : text.replace(/\n$/, '');
        const nearBottom = termOutput.scrollHeight - termOutput.scrollTop - termOutput.clientHeight < 40;
        termOutput.appendChild(div);
        if (nearBottom) termOutput.scrollTop = termOutput.scrollHeight;
    }

    function renderFiles(files) {
        if (!files.length) {
            fileTree.innerHTML = '<div class="file-empty">The sandbox is empty.</div>';
            return;
        }
        fileTree.innerHTML = files.map((f) => {
            const slash = f.path.lastIndexOf('/');
            const dir = slash >= 0 ? `<span class="dir">${escapeHtml(f.path.slice(0, slash + 1))}</span>` : '';
            return `<div class="file-row"><span>${dir}${escapeHtml(f.path.slice(slash + 1))}</span><span class="size">${formatBytes(f.bytes)}</span></div>`;
        }).join('');
    }

    async function refreshFiles() {
        if (DEMO) return;
        try {
            const r = await fetch('/api/files');
            const data = await r.json();
            renderFiles(data.files || []);
        } catch {
            fileTree.innerHTML = '<div class="file-empty">Could not list sandbox files.</div>';
        }
    }

    async function refreshStatus() {
        if (DEMO) return;
        try { showStatus(await (await fetch('/api/status')).json()); } catch {}
    }

    // -----------------------------------------------------------
    // Chat
    // -----------------------------------------------------------
    function addMessage(role, text) {
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role}-message`;
        const avatar = { user: 'U', agent: 'Z', system: 'S' }[role] || '?';
        msgDiv.innerHTML = `<div class="avatar">${avatar}</div><div class="msg-content">${renderText(text)}</div>`;
        chatHistory.appendChild(msgDiv);
        chatHistory.scrollTop = chatHistory.scrollHeight;
        return msgDiv;
    }

    taskInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
            e.preventDefault();
            sendTaskBtn.click();
        }
    });

    sendTaskBtn.addEventListener('click', async () => {
        const text = taskInput.value.trim();
        if (!text) return;
        if (DEMO) { addMessage('system', 'Demo data: nothing is sent to a model.'); return; }
        addMessage('user', text);
        taskInput.value = '';
        sendTaskBtn.disabled = true;
        const pending = addMessage('agent', 'Working… tool calls appear in the terminal.');
        pending.classList.add('pending');
        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text }),
            });
            const data = await res.json();
            pending.remove();
            addMessage('agent', data.response || '(no reply)');
        } catch (e) {
            pending.remove();
            addMessage('system', 'Could not reach the ZenCode server.');
        } finally {
            sendTaskBtn.disabled = false;
            refreshFiles();
        }
    });

    // -----------------------------------------------------------
    // Ship: pack the sandbox into the outbox for the supervisor
    // -----------------------------------------------------------
    const shipLabel = shipUpdateBtn.innerHTML;
    shipUpdateBtn.addEventListener('click', async () => {
        if (DEMO) { logTerm('Demo data: nothing was shipped.', 'log-err'); return; }
        shipUpdateBtn.disabled = true;
        try {
            const r = await fetch('/api/ship_update', { method: 'POST' });
            const data = await r.json();
            if (r.ok) {
                logTerm(`Shipped ${data.artifact} (${formatBytes(data.bytes)}) to the outbox. The supervisor will quarantine it.`, 'log-cmd');
                shipUpdateBtn.textContent = 'Shipped';
            } else {
                logTerm(`Ship failed: ${data.error}`, 'log-err');
            }
        } finally {
            setTimeout(() => { shipUpdateBtn.innerHTML = shipLabel; shipUpdateBtn.disabled = false; }, 2500);
        }
    });

    // -----------------------------------------------------------
    // Start
    // -----------------------------------------------------------
    if (DEMO) {
        $('demoBanner').hidden = false;
        document.title = 'ZenCode · demo data';
        fetch('demo.json').then((r) => r.json()).then((demo) => {
            showStatus(demo.status);
            openWorkspace();
            chatHistory.innerHTML = '';
            demo.messages.forEach((m) => addMessage(m.role, m.text));
            demo.logs.forEach((l) => logTerm(l.msg, l.type));
            renderFiles(demo.files);
            if (new URLSearchParams(location.search).get('view') === 'files') document.querySelector('[data-view="files"]').click();
            taskInput.blur();
        });
        return;
    }

    refreshStatus();
    let logOffset = 0;
    setInterval(async () => {
        try {
            const r = await fetch(`/api/logs?offset=${logOffset}`);
            const data = await r.json();
            data.logs.forEach((l) => logTerm(l.msg, l.type));
            logOffset = data.next_offset;
        } catch {}
    }, 1500);
});
