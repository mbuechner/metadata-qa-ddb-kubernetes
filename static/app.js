'use strict';

const MAX_LIVE_LOG_LINES = 5000;
const applicationBasePath = document.querySelector('meta[name="application-base-path"]').content;
const applicationUrl = (path) => `${applicationBasePath}${path}`;
const socket = io({ path: applicationUrl('/socket.io') });

const alertContainer = document.getElementById('alertContainer');
const jobsTableBody = document.getElementById('jobsTableBody');
const liveLogsButton = document.getElementById('liveLogsButton');
const logsElement = document.getElementById('logs');
const modalBody = document.getElementById('modalBody');
const modalConfirmButton = document.getElementById('modalConfirmButton');
const modalElement = document.getElementById('confirmationModal');
const modalTitle = document.getElementById('modalTitle');
const spinner = document.getElementById('spinner');
const startButton = document.getElementById('startButton');
const stopButton = document.getElementById('stopButton');

const modalInstance = new bootstrap.Modal(modalElement);
let activeJobCache = null;
let jobsCache = [];
let logMode = 'live';
let sortConfig = { key: 'startTime', dir: 'desc' };

function setUiState(state) {
    switch (state) {
        case 'Running':
        case 'Starting':
        case 'Pending':
            startButton.disabled = true;
            stopButton.disabled = false;
            spinner.style.animationIterationCount = 'infinite';
            break;
        case 'Stopping':
            startButton.disabled = true;
            stopButton.disabled = true;
            spinner.style.animationIterationCount = 'infinite';
            break;
        default:
            startButton.disabled = false;
            stopButton.disabled = true;
            spinner.style.animationIterationCount = '0';
            break;
    }
}

function showAlert(message, type = 'success', timeoutMilliseconds = 0) {
    const alert = document.createElement('div');
    alert.classList.add('alert', `alert-${type}`, 'alert-dismissible', 'fade', 'show');
    alert.setAttribute('role', 'alert');

    const messageElement = document.createElement('div');
    messageElement.classList.add('text-break');
    messageElement.textContent = String(message);
    alert.append(messageElement);

    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.dataset.bsDismiss = 'alert';
    closeButton.setAttribute('aria-label', 'Schließen');
    closeButton.classList.add('btn-close');
    alert.append(closeButton);
    alertContainer.append(alert);

    if (timeoutMilliseconds > 0) {
        window.setTimeout(() => alert.remove(), timeoutMilliseconds);
    }
}

function appendLiveLog(message) {
    logsElement.append(document.createTextNode(`${String(message)}\n`));
    while (logsElement.childNodes.length > MAX_LIVE_LOG_LINES) {
        logsElement.firstChild.remove();
    }
}

socket.on('status_update', (data) => {
    const status = data.status || 'Stopped';
    const alertType = (status === 'Error' || status === 'Failed') ? 'danger' : 'success';
    showAlert(data.message, alertType);
    setUiState(status);
});

socket.on('log_update', (data) => {
    if (logMode !== 'live') return;
    const isAtBottom = logsElement.scrollHeight - logsElement.scrollTop <= logsElement.clientHeight + 1;
    appendLiveLog(data.message);
    if (isAtBottom) logsElement.scrollTop = logsElement.scrollHeight;
});

function pad2(number) {
    return String(number).padStart(2, '0');
}

function formatIso(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return `${pad2(date.getDate())}.${pad2(date.getMonth() + 1)}.${date.getFullYear()} `
        + `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

function parseIsoToMillis(iso) {
    if (!iso) return null;
    const time = new Date(iso).getTime();
    return Number.isNaN(time) ? null : time;
}

function getSortValue(job, key) {
    switch (key) {
        case 'name':
        case 'status':
            return (job[key] || '').toLowerCase();
        case 'startTime':
        case 'completionTime':
            return parseIsoToMillis(job[key]);
        default:
            return null;
    }
}

function updateSortIndicators() {
    for (const key of ['name', 'status', 'startTime', 'completionTime']) {
        const indicator = document.getElementById(`sortIndicator-${key}`);
        indicator.textContent = sortConfig.key === key ? (sortConfig.dir === 'asc' ? '▲' : '▼') : '';
    }
}

function setSort(key) {
    if (sortConfig.key === key) {
        sortConfig.dir = sortConfig.dir === 'asc' ? 'desc' : 'asc';
    } else {
        sortConfig = {
            key,
            dir: (key === 'startTime' || key === 'completionTime') ? 'desc' : 'asc',
        };
    }
    updateSortIndicators();
    renderJobs();
}

function applyClusterStateToUi(apiData) {
    const activeJob = apiData?.activeJob || null;
    if (!activeJob) {
        setUiState('Stopped');
        return;
    }
    if (apiData.activeJobTerminating) {
        setUiState('Stopping');
        return;
    }
    const status = apiData.activeJobStatus
        || jobsCache.find((job) => job.name === activeJob)?.status
        || 'Pending';
    setUiState(status);
}

function createCell(value = '') {
    const cell = document.createElement('td');
    cell.textContent = value;
    return cell;
}

function renderJobs() {
    jobsTableBody.replaceChildren();
    const decorated = jobsCache.map((job, index) => ({ job, index }));
    decorated.sort((left, right) => {
        const leftValue = getSortValue(left.job, sortConfig.key);
        const rightValue = getSortValue(right.job, sortConfig.key);
        if (leftValue == null && rightValue == null) return left.index - right.index;
        if (leftValue == null) return 1;
        if (rightValue == null) return -1;
        const comparison = (typeof leftValue === 'number' && typeof rightValue === 'number')
            ? leftValue - rightValue
            : String(leftValue).localeCompare(String(rightValue));
        if (comparison === 0) return left.index - right.index;
        return sortConfig.dir === 'asc' ? comparison : -comparison;
    });

    for (const { job } of decorated) {
        const row = document.createElement('tr');
        if (job.status?.toLowerCase() === 'running' || job.name === activeJobCache) {
            row.classList.add('table-info');
        }
        row.append(
            createCell(job.name),
            createCell(job.status),
            createCell(formatIso(job.startTime)),
            createCell(formatIso(job.completionTime)),
        );

        const actions = document.createElement('td');
        const viewButton = document.createElement('button');
        viewButton.className = 'btn btn-outline-primary btn-sm';
        viewButton.type = 'button';
        viewButton.textContent = 'Logs ansehen';
        viewButton.addEventListener('click', () => viewJobLogs(job.name));
        actions.append(viewButton);

        const activeStatuses = new Set(['running', 'pending', 'starting']);
        const isActive = job.name === activeJobCache || activeStatuses.has(job.status?.toLowerCase());
        const deleteButton = document.createElement('button');
        deleteButton.className = 'btn btn-outline-danger btn-sm ms-2';
        deleteButton.type = 'button';
        deleteButton.textContent = 'Löschen';
        deleteButton.disabled = Boolean(job.isTerminating);
        deleteButton.addEventListener('click', () => confirmDeleteJob(job.name, isActive));
        actions.append(deleteButton);
        row.append(actions);
        jobsTableBody.append(row);
    }
}

async function refreshJobs() {
    try {
        const response = await fetch(applicationUrl('/api/jobs'), { cache: 'no-store' });
        if (!response.ok) return;
        const data = await response.json();
        jobsCache = Array.isArray(data.jobs) ? data.jobs : [];
        activeJobCache = data.activeJob || null;
        applyClusterStateToUi(data);
        renderJobs();
    } catch {
        // A later polling cycle retries transient browser/network failures.
    }
}

async function viewJobLogs(jobName) {
    try {
        logMode = 'history';
        liveLogsButton.disabled = false;
        showAlert(`Logs für ${jobName} werden angezeigt`, 'success', 2000);
        const response = await fetch(
            applicationUrl(`/api/jobs/${encodeURIComponent(jobName)}/logs?tailLines=2000`),
            { cache: 'no-store' },
        );
        const data = await response.json();
        if (!response.ok || data.error) {
            showAlert(data.error || `Fehler beim Laden der Logs (${response.status})`, 'danger');
            return;
        }
        logsElement.textContent = data.logs || '';
        logsElement.scrollTop = logsElement.scrollHeight;
    } catch (error) {
        showAlert(String(error), 'danger');
    }
}

function showLiveLogs() {
    logMode = 'live';
    liveLogsButton.disabled = true;
    logsElement.replaceChildren();
    showAlert('Auf Live-Logs umgeschaltet', 'success', 1500);
}

function confirmAction(action) {
    if (action === 'start') {
        modalTitle.textContent = 'Job starten';
        modalBody.textContent = 'Möchtest du den Job wirklich starten?';
        modalConfirmButton.onclick = () => {
            logsElement.replaceChildren();
            setUiState('Starting');
            socket.emit('start_job');
            modalInstance.hide();
        };
    } else {
        modalTitle.textContent = 'Job stoppen';
        modalBody.textContent = 'Möchtest du den Job wirklich stoppen?';
        modalConfirmButton.onclick = () => {
            setUiState('Stopping');
            socket.emit('cancel_job');
            modalInstance.hide();
        };
    }
    modalInstance.show();
}

function confirmDeleteJob(jobName, isActive) {
    modalTitle.textContent = 'Job löschen';
    modalBody.textContent = isActive
        ? `Der Job ${jobName} ist aktuell aktiv. Wirklich löschen und terminieren?`
        : `Möchtest du den Job ${jobName} wirklich löschen?`;
    modalConfirmButton.onclick = async () => {
        try {
            const response = await fetch(applicationUrl(`/api/jobs/${encodeURIComponent(jobName)}`), {
                method: 'DELETE',
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                showAlert(data.error || `Fehler beim Löschen (${response.status})`, 'danger');
                return;
            }
            showAlert(`Job ${jobName} wurde gelöscht`, 'success', 2000);
            await refreshJobs();
        } catch (error) {
            showAlert(String(error), 'danger');
        } finally {
            modalInstance.hide();
        }
    };
    modalInstance.show();
}

startButton.addEventListener('click', () => confirmAction('start'));
stopButton.addEventListener('click', () => confirmAction('stop'));
liveLogsButton.addEventListener('click', showLiveLogs);
for (const button of document.querySelectorAll('.sort-button')) {
    button.addEventListener('click', () => setSort(button.dataset.sortKey));
}

spinner.style.animationIterationCount = '0';
updateSortIndicators();
refreshJobs();
window.setInterval(refreshJobs, 5000);
