document.addEventListener('DOMContentLoaded', () => {
    const setupScreen = document.getElementById('setupScreen');
    const workspaceScreen = document.getElementById('workspaceScreen');
    const probeBtn = document.getElementById('probeBtn');
    const loader = document.getElementById('probeLoader');
    const btnText = probeBtn.querySelector('.btn-text');
    const modelSelection = document.getElementById('modelSelection');
    const modelSelect = document.getElementById('modelSelect');
    const startBtn = document.getElementById('startBtn');
    const errorMsg = document.getElementById('errorMsg');
    const setupForm = document.getElementById('setupForm');
    
    // Probing logic
    probeBtn.addEventListener('click', async () => {
        const urlInput = document.getElementById('endpointUrl').value;
        if (!urlInput) {
            errorMsg.textContent = 'Please enter an endpoint URL';
            return;
        }

        errorMsg.textContent = '';
        btnText.classList.add('hidden');
        loader.classList.remove('hidden');

        try {
            // Usually /v1/models for OpenAI compatible endpoints
            const mUrl = urlInput.endsWith('/v1') ? `${urlInput}/models` : `${urlInput}/v1/models`;
            
            // Forwarding to our python backend to avoid CORS if needed
            // For now, attempting direct fetch. If CORS fails, it will error, so we proxy via backend:
            const response = await fetch('/api/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: mUrl })
            });

            if (!response.ok) throw new Error('Failed to fetch models');
            const data = await response.json();
            
            if (data.data && data.data.length > 0) {
                modelSelect.innerHTML = '';
                data.data.forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m.id;
                    opt.textContent = m.id;
                    modelSelect.appendChild(opt);
                });
                
                probeBtn.classList.add('hidden');
                modelSelection.classList.remove('hidden');
                startBtn.classList.remove('hidden');
            } else {
                throw new Error('No models found at endpoint');
            }
        } catch (e) {
            errorMsg.textContent = e.message;
            btnText.classList.remove('hidden');
            loader.classList.add('hidden');
        }
    });

    // Start Workspace
    setupForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const base_url = document.getElementById('endpointUrl').value;
        const model_id = modelSelect.value;
        
        // init agent backend
        fetch('/api/init', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ base_url, model_id })
        }).then(() => {
            setupScreen.style.opacity = '0';
            setTimeout(() => {
                setupScreen.classList.add('hidden');
                workspaceScreen.classList.remove('hidden');
                workspaceScreen.classList.add('fade-in');
            }, 500);
        });
    });

    // View Toggles
    const toggleBtns = document.querySelectorAll('.toggle-btn');
    const views = document.querySelectorAll('.view-content');

    toggleBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            toggleBtns.forEach(b => b.classList.remove('active'));
            views.forEach(v => v.style.display = 'none');
            
            btn.classList.add('active');
            document.getElementById(btn.dataset.view + 'View').style.display = 'flex';
        });
    });

    // Chat Logic
    const sendTaskBtn = document.getElementById('sendTaskBtn');
    const taskInput = document.getElementById('taskInput');
    const chatHistory = document.getElementById('chatHistory');

    function addMessage(role, text) {
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role}-message`;
        
        const avatar = document.createElement('div');
        avatar.className = `avatar ${role}-avatar`;
        if (role === 'user') avatar.textContent = 'U';
        if (role === 'agent') avatar.textContent = 'A';
        if (role === 'system') avatar.textContent = 'S';

        const content = document.createElement('div');
        content.className = 'msg-content';
        
        // basic parser for tool output
        let htmlText = text.replace(/```([\s\S]*?)```/g, '<pre>$1</pre>');
        htmlText = htmlText.split('\n').map(line => `<p>${line}</p>`).join('');
        content.innerHTML = htmlText;

        msgDiv.appendChild(avatar);
        msgDiv.appendChild(content);
        chatHistory.appendChild(msgDiv);
        chatHistory.scrollTop = chatHistory.scrollHeight;
    }

    taskInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
            e.preventDefault();
            sendTaskBtn.click();
        }
    });

    sendTaskBtn.addEventListener('click', async () => {
        const text = taskInput.value.trim();
        if(!text) return;

        addMessage('user', text);
        taskInput.value = '';

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ message: text })
            });
            const data = await res.json();
            addMessage('agent', data.response);
        } catch(e) {
            addMessage('system', 'Error communicating with supervisor sandbox.');
        }
    });

    // Polling sandbox state (mock logs for wow factor)
    const termOutput = document.getElementById('termOutput');
    function logTerm(text, type='log-out') {
        const div = document.createElement('div');
        div.className = `log-entry ${type}`;
        div.textContent = `> ${text}`;
        termOutput.appendChild(div);
        termOutput.scrollTop = termOutput.scrollHeight;
    }

    let logOffset = 0;
    setInterval(async () => {
        try {
            const r = await fetch(`/api/logs?offset=${logOffset}`);
            const data = await r.json();
            data.logs.forEach(l => {
                logTerm(l.msg, l.type);
            });
            logOffset = data.next_offset;
        } catch(e) {}
    }, 2000);

    const shipUpdateBtn = document.getElementById('shipUpdateBtn');
    if (shipUpdateBtn) {
        shipUpdateBtn.addEventListener('click', async () => {
            const r = await fetch('/api/ship_update', { method: 'POST' });
            if (r.ok) {
                logTerm('Artifact successfully zipped and pushed to /outbox!', 'log-cmd');
                shipUpdateBtn.textContent = '✅ Shipped!';
                setTimeout(()=> shipUpdateBtn.textContent = '📦 Ship Update', 3000);
            }
        });
    }
});
