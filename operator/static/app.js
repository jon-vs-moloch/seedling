document.addEventListener('DOMContentLoaded', () => {
    const liveView = document.getElementById('liveView');
    const statusBadge = document.getElementById('statusBadge');
    const roleBadge = document.getElementById('roleBadge');
    const toggleBtn = document.getElementById('toggleBtn');
    const goalInput = document.getElementById('goalInput');
    const telemetryOut = document.getElementById('telemetryOut');

    let currentStatus = 'paused';

    async function pushControl(status, goal) {
        await fetch('/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status, goal })
        });
    }

    toggleBtn.addEventListener('click', async () => {
        const newStatus = currentStatus === 'paused' ? 'running' : 'paused';
        const goal = goalInput.value;

        await pushControl(newStatus, goal);

        if (newStatus === 'running') {
            toggleBtn.textContent = 'Pause Operator';
            toggleBtn.className = 'btn pause';
            statusBadge.textContent = 'RUNNING';
            statusBadge.className = 'status-badge running';
        } else {
            toggleBtn.textContent = 'Start Operator Loop';
            toggleBtn.className = 'btn play';
            statusBadge.textContent = 'PAUSED';
            statusBadge.className = 'status-badge';
        }
        currentStatus = newStatus;
    });

    goalInput.addEventListener('change', async () => {
        await pushControl(currentStatus, goalInput.value);
    });

    setInterval(async () => {
        try {
            const r = await fetch('/api/state');
            const state = await r.json();

            // Display the operator role
            if (state.role && roleBadge) {
                roleBadge.textContent = state.role;
            }

            // Sync UI state if changed externally
            if (state.status !== currentStatus) {
                currentStatus = state.status;
                if (currentStatus === 'running') {
                    toggleBtn.textContent = 'Pause Operator';
                    toggleBtn.className = 'btn pause';
                    statusBadge.textContent = 'RUNNING';
                    statusBadge.className = 'status-badge running';
                } else {
                    toggleBtn.textContent = 'Start Operator Loop';
                    toggleBtn.className = 'btn play';
                    statusBadge.textContent = 'PAUSED';
                    statusBadge.className = 'status-badge';
                }
            }

            // Sync goal text if not actively focused
            if (document.activeElement !== goalInput && state.goal !== goalInput.value) {
                goalInput.value = state.goal;
            }

            // Force image refresh via cache-busting query
            liveView.src = `/api/image?t=${Date.now()}`;

            // Sync telemetry line
            telemetryOut.textContent = `> ${state.recent_action}`;

        } catch (e) {
            console.error('Failed to fetch state', e);
        }
    }, 1000);
});
