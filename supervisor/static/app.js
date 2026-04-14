document.addEventListener('DOMContentLoaded', () => {
    const logsBody = document.getElementById('logsBody');
    const systemStatus = document.getElementById('systemStatus');
    const updatesSection = document.getElementById('updatesSection');
    const updatesBody = document.getElementById('updatesBody');

    let logOffset = 0;
    let activeFilter = 'all';
    let knownUpdates = 0;

    // -------------------------------------------------------
    // Topology + screenshot polling
    // -------------------------------------------------------
    async function pollTopology() {
        try {
            const r = await fetch('/api/topology');
            const data = await r.json();
            const procs = data.processes;

            let allRunning = true;
            for (const [name, info] of Object.entries(procs)) {
                const topoEl = document.getElementById(`topo_${name}`);
                if (topoEl) {
                    const shortStatus = info.status.split(' ')[0];
                    topoEl.textContent = shortStatus;
                    topoEl.className = `topo-status ${shortStatus}`;
                }
                if (info.status !== 'running') allRunning = false;
            }

            // System status indicator
            const dot = systemStatus.querySelector('.status-dot');
            const text = systemStatus.querySelector('.status-text');
            if (allRunning) {
                text.textContent = 'All systems nominal';
                systemStatus.style.background = 'var(--success-dim)';
                dot.style.background = 'var(--success)';
                dot.style.boxShadow = '0 0 8px var(--success)';
            } else {
                text.textContent = 'Degraded';
                systemStatus.style.background = 'var(--warning-dim)';
                dot.style.background = 'var(--warning)';
                dot.style.boxShadow = '0 0 8px var(--warning)';
            }

            // Refresh operator screenshots
            for (const name of ['operator_a', 'operator_b']) {
                const img = document.getElementById(`img_${name}`);
                const overlay = document.getElementById(`overlay_${name}`);
                if (procs[name] && procs[name].status === 'running') {
                    img.src = `/api/operator/${name}/image?t=${Date.now()}`;
                    img.onload = () => { overlay.style.display = 'none'; };
                    img.onerror = () => { overlay.style.display = 'flex'; overlay.textContent = 'No screenshot yet'; };
                } else {
                    overlay.style.display = 'flex';
                    overlay.textContent = `${name} is not running`;
                }
            }
        } catch (e) {
            const text = systemStatus.querySelector('.status-text');
            text.textContent = 'Supervisor unreachable';
            systemStatus.style.background = 'var(--error-dim)';
        }
    }

    // -------------------------------------------------------
    // Log polling
    // -------------------------------------------------------
    function formatTime(isoStr) {
        try {
            const d = new Date(isoStr);
            return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
        } catch {
            return '--:--:--';
        }
    }

    async function pollLogs() {
        try {
            const r = await fetch(`/api/logs?offset=${logOffset}`);
            const data = await r.json();

            for (const entry of data.logs) {
                const div = document.createElement('div');
                const sourceClass = `log-${entry.source}`;
                const levelClass = entry.level === 'error' ? 'log-error' : '';
                div.className = `log-entry ${sourceClass} ${levelClass}`;
                div.dataset.source = entry.source;

                div.innerHTML = `
                    <span class="log-ts">${formatTime(entry.ts)}</span>
                    <span class="log-source">${entry.source}</span>
                    <span class="log-msg">${escapeHtml(entry.msg)}</span>
                `;

                // Apply current filter
                if (activeFilter !== 'all' && entry.source !== activeFilter) {
                    div.style.display = 'none';
                }

                logsBody.appendChild(div);
            }

            if (data.logs.length > 0) {
                logsBody.scrollTop = logsBody.scrollHeight;
            }

            logOffset = data.next_offset;
        } catch (e) {
            // Supervisor might not be ready yet
        }
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // -------------------------------------------------------
    // Update polling
    // -------------------------------------------------------
    async function pollUpdates() {
        try {
            const r = await fetch('/api/updates');
            const data = await r.json();

            if (data.updates.length > knownUpdates) {
                updatesSection.style.display = 'block';
                updatesBody.innerHTML = '';

                for (const u of data.updates) {
                    const div = document.createElement('div');
                    div.className = 'update-entry';
                    div.innerHTML = `
                        <span class="update-icon">📦</span>
                        <span class="update-label">
                            <strong>${u.source}</strong> produced an artifact targeting
                            <strong>${u.target_codebase}/</strong>
                        </span>
                    `;
                    updatesBody.appendChild(div);
                }
                knownUpdates = data.updates.length;
            }
        } catch (e) {}
    }

    // -------------------------------------------------------
    // Process controls
    // -------------------------------------------------------
    document.querySelectorAll('.restart-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const name = btn.dataset.process;
            btn.textContent = '…';
            await fetch(`/api/process/${name}/restart`, { method: 'POST' });
            setTimeout(() => { btn.textContent = '↻'; }, 1000);
        });
    });

    document.querySelectorAll('.stop-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const name = btn.dataset.process;
            await fetch(`/api/process/${name}/stop`, { method: 'POST' });
        });
    });

    // -------------------------------------------------------
    // Log filters
    // -------------------------------------------------------
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeFilter = btn.dataset.filter;

            document.querySelectorAll('.log-entry').forEach(entry => {
                if (activeFilter === 'all' || entry.dataset.source === activeFilter) {
                    entry.style.display = '';
                } else {
                    entry.style.display = 'none';
                }
            });
        });
    });

    // -------------------------------------------------------
    // Polling intervals
    // -------------------------------------------------------
    pollTopology();
    pollLogs();

    setInterval(pollTopology, 2000);
    setInterval(pollLogs, 2000);
    setInterval(pollUpdates, 5000);
});
