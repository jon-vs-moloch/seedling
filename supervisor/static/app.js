/*
 * Seedling supervisor dashboard.
 *
 * Live mode (default) polls the supervisor API. Demo mode (?demo in the URL)
 * reads /static/demo/fixture.json instead, shows a "Demo data" banner, and
 * disables every control, so the page can be previewed without models,
 * containers or a running stack.
 */
document.addEventListener('DOMContentLoaded', () => {
    const DEMO = new URLSearchParams(location.search).has('demo');
    const $ = (id) => document.getElementById(id);
    const logsBody = $('logsBody');
    const systemStatus = $('systemStatus');
    const updatesBody = $('updatesBody');
    const resetBtn = $('resetBtn');

    let logOffset = 0;
    let activeFilter = 'all';
    let lastUpdatesKey = '';
    let demoFixture = null;

    // ---------------------------------------------------------------
    // Theme: system by default; a manual choice is remembered per browser.
    // ---------------------------------------------------------------
    const root = document.documentElement;
    try { const saved = localStorage.getItem('seedling-theme'); if (saved) root.dataset.theme = saved; } catch {}
    const forcedTheme = new URLSearchParams(location.search).get('theme');
    if (forcedTheme === 'light' || forcedTheme === 'dark') root.dataset.theme = forcedTheme;
    $('themeBtn').addEventListener('click', () => {
        const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
        root.dataset.theme = dark ? 'light' : 'dark';
        try { localStorage.setItem('seedling-theme', root.dataset.theme); } catch {}
    });

    // ---------------------------------------------------------------
    // Data access: live API, or the demo fixture
    // ---------------------------------------------------------------
    async function getJson(path) {
        if (!DEMO) {
            const r = await fetch(path);
            return r.json();
        }
        if (!demoFixture) demoFixture = await (await fetch('/static/demo/fixture.json')).json();
        if (path.startsWith('/api/topology')) return demoFixture.topology;
        if (path.startsWith('/api/updates')) return { updates: demoFixture.updates };
        if (path.startsWith('/api/logs')) {
            const offset = Number(new URL(path, location.href).searchParams.get('offset') || 0);
            return { logs: demoFixture.logs.slice(offset), next_offset: demoFixture.logs.length };
        }
        throw new Error(`No demo data for ${path}`);
    }

    async function post(path) {
        if (DEMO) {
            toast('Demo data: controls are disabled.');
            return { ok: false, demo: true };
        }
        const r = await fetch(path, { method: 'POST' });
        let body = {};
        try { body = await r.json(); } catch {}
        return { ok: r.ok, ...body };
    }

    function imageUrl(name) {
        return DEMO ? `/static/demo/${name}.png` : `/api/operator/${name}/image?t=${Date.now()}`;
    }

    let toastTimer;
    function toast(message) {
        const el = $('toast');
        el.textContent = message;
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
    }

    function escapeHtml(value) {
        const div = document.createElement('div');
        div.textContent = String(value ?? '');
        return div.innerHTML;
    }

    function formatTime(iso) {
        const d = new Date(iso);
        return isNaN(d) ? '--:--:--' : d.toLocaleTimeString('en-GB', { hour12: false });
    }

    function formatBytes(n) {
        if (n == null) return '?';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / 1024 / 1024).toFixed(1)} MB`;
    }

    function statusClass(status) {
        const s = (status || '').split(' ')[0];
        return s === 'running' ? 'ok' : s === 'crashed' ? 'danger' : '';
    }

    // ---------------------------------------------------------------
    // Topology, safety posture and operator screenshots
    // ---------------------------------------------------------------
    function setValue(id, text, tone) {
        const el = $(id);
        el.textContent = text;
        el.className = `value ${tone || ''}`;
    }

    async function pollTopology() {
        try {
            const data = await getJson('/api/topology');
            const procs = data.processes || {};
            const safety = data.safety;
            if (safety) {
                const runtime = safety.container_runtime ? safety.container_runtime.split('/').pop() : null;
                if (safety.sandbox_mode === 'container') {
                    setValue('sandboxMode', runtime ? `Container · ${runtime}` : 'Container · runtime missing', runtime ? 'ok' : 'warn');
                } else {
                    setValue('sandboxMode', `${safety.sandbox_mode} (host shell)`, 'warn');
                }
                setValue('networkPolicy', safety.network === 'none' ? 'None (offline)' : safety.network, safety.network === 'none' ? 'ok' : 'warn');
                setValue('envPolicy', safety.env_policy === 'allowlist' ? 'Allowlisted' : safety.env_policy, safety.env_policy === 'allowlist' ? 'ok' : 'warn');
                setValue('artifactPolicy', safety.artifact_policy === 'quarantine-and-manual-apply' ? 'Quarantine + manual apply' : safety.artifact_policy, 'ok');
                const limits = safety.artifact_limits || {};
                setValue('artifactLimits', `${formatBytes(limits.archive_bytes)} · ${limits.members ?? '?'} files`, 'ok');
            }

            const names = Object.keys(procs);
            const running = names.filter((n) => procs[n].status === 'running').length;
            const dot = systemStatus.querySelector('.status-text');
            if (names.length && running === names.length) {
                systemStatus.className = 'chip ok';
                dot.textContent = `All ${names.length} processes running`;
            } else if (names.length) {
                systemStatus.className = running ? 'chip warn' : 'chip danger';
                dot.textContent = `Degraded · ${running}/${names.length} running`;
            } else {
                systemStatus.className = 'chip warn';
                dot.textContent = 'Starting…';
            }

            for (const [name, info] of Object.entries(procs)) {
                const topo = $(`topo_${name}`);
                if (topo) {
                    const short = (info.status || '').split(' ')[0];
                    topo.textContent = info.status;
                    topo.className = `node-status ${short}`;
                }
                const chip = $(`chip_${name}`);
                if (chip) {
                    chip.className = `chip ${statusClass(info.status)}`;
                    chip.innerHTML = `<span class="dot"></span>${escapeHtml(info.status)}`;
                }
            }

            for (const name of ['operator_a', 'operator_b']) {
                const img = $(`img_${name}`);
                const overlay = $(`overlay_${name}`);
                const info = procs[name];
                if (info && info.status === 'running') {
                    const probe = new Image();
                    probe.onload = () => { img.src = probe.src; overlay.hidden = true; };
                    probe.onerror = () => { overlay.hidden = false; overlay.textContent = 'No screenshot yet'; };
                    probe.src = imageUrl(name);
                } else {
                    img.removeAttribute('src');
                    overlay.hidden = false;
                    overlay.innerHTML = `<strong>Operator not running</strong><span class="faint">${escapeHtml(info ? info.status : 'unknown')}</span>`;
                }
            }
        } catch (e) {
            systemStatus.className = 'chip danger';
            systemStatus.querySelector('.status-text').textContent = 'Supervisor unreachable';
        }
    }

    // ---------------------------------------------------------------
    // Telemetry
    // ---------------------------------------------------------------
    async function pollLogs() {
        try {
            const data = await getJson(`/api/logs?offset=${logOffset}`);
            if (logOffset === 0 && data.logs.length) logsBody.innerHTML = '';
            const nearBottom = logsBody.scrollHeight - logsBody.scrollTop - logsBody.clientHeight < 40;
            for (const entry of data.logs) {
                const div = document.createElement('div');
                div.className = `log-entry log-${entry.source} ${entry.level === 'error' ? 'log-error' : ''}`;
                div.dataset.source = entry.source;
                div.innerHTML = `
                    <span class="log-ts">${formatTime(entry.ts)}</span>
                    <span class="log-source">${escapeHtml(entry.source)}</span>
                    <span class="log-msg">${escapeHtml(entry.msg)}</span>`;
                if (activeFilter !== 'all' && entry.source !== activeFilter) div.hidden = true;
                logsBody.appendChild(div);
            }
            if (data.logs.length && nearBottom) logsBody.scrollTop = logsBody.scrollHeight;
            logOffset = data.next_offset;
        } catch (e) {
            // The supervisor may still be starting.
        }
    }

    document.querySelectorAll('.filter-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach((b) => b.classList.remove('active'));
            btn.classList.add('active');
            activeFilter = btn.dataset.filter;
            logsBody.querySelectorAll('.log-entry').forEach((entry) => {
                entry.hidden = !(activeFilter === 'all' || entry.dataset.source === activeFilter);
            });
        });
    });

    // ---------------------------------------------------------------
    // Quarantine
    // ---------------------------------------------------------------
    const STATUS_TONE = { quarantined: 'warn', applying: 'info', applied: 'ok', failed: 'danger', rejected: 'danger' };

    // The candidate's parser score next to operator/'s current one
    // (scripts/measure_cycle.py, run by the supervisor in a container).
    // Read-only: it informs the Apply decision and never triggers it.
    function scoreLine(u) {
        const s = u.score;
        if (!s) return '';
        if (s.status === 'scoring') return '<div class="update-score">Scoring the candidate in a container…</div>';
        if (s.status !== 'done') return `<div class="update-score">Not scored: ${escapeHtml(s.reason || s.status)}</div>`;
        const c = s.candidate, b = s.baseline;
        const tone = c.passed < b.passed || s.regressed.length ? 'danger' : c.passed > b.passed ? 'ok' : 'warn';
        const delta = c.passed - b.passed;
        return `<div class="update-score ${tone}">
            <span>Parser score <strong>${c.passed}/${c.total}</strong> vs <span class="mono">operator/</span> now ${b.passed}/${b.total}
            (${delta > 0 ? '+' : ''}${delta})</span>
            <span>${s.fixed.length} fixed · ${s.regressed.length} regressed${s.parser_shipped ? '' : ' · parser not in artifact'}</span>
        </div>`;
    }

    function unexpectedLine(u) {
        const list = u.unexpected_files || [];
        if (!list.length) return '';
        return `<div class="update-unexpected">${list.length} ${list.length === 1 ? 'file is' : 'files are'} new to <span class="mono">${escapeHtml(u.target_codebase)}/</span>:
            <span class="mono">${list.map((f) => escapeHtml(f.path)).join(', ')}</span></div>`;
    }

    async function pollUpdates() {
        try {
            const data = await getJson('/api/updates');
            const updates = data.updates || [];
            const key = JSON.stringify(updates.map((u) => [u.id, u.status, u.score && u.score.status]));
            if (key === lastUpdatesKey) return;
            lastUpdatesKey = key;
            $('updatesCount').textContent = updates.length;
            if (!updates.length) return;
            updatesBody.innerHTML = updates.slice().reverse().map((u) => `
                <div class="update">
                    <div class="update-title">
                        <span class="mono">${escapeHtml(u.id)}</span>
                        <span class="chip ${STATUS_TONE[u.status] || ''}">${escapeHtml(u.status)}</span>
                        <span class="update-route">${escapeHtml(u.source)} → <span class="mono">${escapeHtml(u.target_codebase)}/</span></span>
                    </div>
                    <div class="update-meta">
                        <span>${formatBytes(u.archive_bytes)} archive</span>
                        <span>${formatBytes(u.expanded_bytes)} expanded</span>
                        <span title="${escapeHtml((u.files || []).join('\n'))}">${u.members ?? '?'} entries</span>
                        <span class="mono" title="${escapeHtml(u.sha256)}">sha256 ${escapeHtml((u.sha256 || '').slice(0, 12))}…</span>
                        <span>${formatTime(u.detected_at)}</span>
                    </div>
                    <div class="update-actions">
                        ${u.status === 'quarantined' ? `<button class="btn small primary apply-update-btn" data-update="${escapeHtml(u.id)}" data-target="${escapeHtml(u.target_codebase)}">Apply</button>` : ''}
                    </div>
                    ${scoreLine(u)}
                    ${unexpectedLine(u)}
                    ${u.error ? `<div class="update-error">${escapeHtml(u.error)}</div>` : ''}
                </div>`).join('');
        } catch (e) {}
    }

    updatesBody.addEventListener('click', async (e) => {
        const btn = e.target.closest('.apply-update-btn');
        if (!btn) return;
        if (DEMO) { toast('Demo data: controls are disabled.'); return; }
        const ok = confirm(`Apply artifact ${btn.dataset.update} to ${btn.dataset.target}/?\n\nThis overwrites files in the source tree in place and restarts the affected processes. There is no automatic rollback.`);
        if (!ok) return;
        btn.disabled = true;
        btn.textContent = 'Applying…';
        const result = await post(`/api/updates/${encodeURIComponent(btn.dataset.update)}/apply`);
        if (!result.ok && result.error) toast(result.error);
        lastUpdatesKey = '';
        await pollUpdates();
    });

    // ---------------------------------------------------------------
    // Process controls and reset
    // ---------------------------------------------------------------
    document.querySelectorAll('.restart-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            await post(`/api/process/${btn.dataset.process}/restart`);
            setTimeout(() => { btn.disabled = false; pollTopology(); }, 800);
        });
    });

    document.querySelectorAll('.stop-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
            await post(`/api/process/${btn.dataset.process}/stop`);
            pollTopology();
        });
    });

    resetBtn.addEventListener('click', async () => {
        if (DEMO) { toast('Demo data: controls are disabled.'); return; }
        if (!confirm('Reset stops every process, deletes the runtime sandboxes and quarantine, and starts fresh. Continue?')) return;
        resetBtn.disabled = true;
        resetBtn.textContent = 'Resetting…';
        await post('/api/reset');
        logOffset = 0;
        logsBody.innerHTML = '';
        lastUpdatesKey = '';
        $('updatesCount').textContent = '0';
        updatesBody.innerHTML = '<div class="empty"><strong>No artifacts yet.</strong><span>The runtime was reset.</span></div>';
        resetBtn.disabled = false;
        resetBtn.textContent = 'Reset';
    });

    // ---------------------------------------------------------------
    // Start
    // ---------------------------------------------------------------
    if (DEMO) {
        $('demoBanner').hidden = false;
        document.title = 'Seedling Supervisor · demo data';
    }
    pollTopology();
    pollLogs();
    pollUpdates();
    if (!DEMO) {
        setInterval(pollTopology, 2000);
        setInterval(pollLogs, 2000);
        setInterval(pollUpdates, 3000);
    }
});
