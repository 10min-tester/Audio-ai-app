// PWA: Register Service Worker
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('./service-worker.js')
      .then(reg => console.log('Service Worker registered', reg))
      .catch(err => console.error('Service Worker registration failed', err));
}

const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('audio-file');
const fileNameDisplay = document.getElementById('file-name-display');
const submitBtn = document.getElementById('submit-btn');
const form = document.getElementById('upload-form');
const loader = document.getElementById('loader');
const statusText = document.querySelector('.status-text');
const resultArea = document.getElementById('result-area');
const originalPlayer = document.getElementById('original-player');
const restoredPlayer = document.getElementById('restored-player');
const downloadLink = document.getElementById('download-link');
const qualityPreset = document.getElementById('quality-preset');
const planSourceBadge = document.getElementById('plan-source-badge');
const processingModeBadge = document.getElementById('processing-mode-badge');
const feedbackStatus = document.getElementById('feedback-status');
const feedbackButtons = document.querySelectorAll('.feedback-btn');
let lastTaskId = null;

const PRESET_VALUES = {
    music_balanced: {
        "opt-super-res": true, "val-super-res": 45,
        "opt-noise-reduction": true, "val-noise-reduction": 50,
        "opt-highpass": true, "val-highpass": 70,
        "opt-punchy-bass": true, "val-punchy-bass": 45,
        "opt-brilliance": true, "val-brilliance": 35,
        "opt-stereo": true, "val-stereo": 35,
        "opt-limiter": true, "val-limiter": 65
    },
    voice_clean: {
        "opt-super-res": true, "val-super-res": 40,
        "opt-noise-reduction": true, "val-noise-reduction": 68,
        "opt-highpass": true, "val-highpass": 95,
        "opt-punchy-bass": false, "val-punchy-bass": 20,
        "opt-brilliance": true, "val-brilliance": 30,
        "opt-stereo": false, "val-stereo": 20,
        "opt-limiter": true, "val-limiter": 55
    },
    compressed_repair: {
        "opt-super-res": true, "val-super-res": 62,
        "opt-noise-reduction": true, "val-noise-reduction": 55,
        "opt-highpass": true, "val-highpass": 80,
        "opt-punchy-bass": true, "val-punchy-bass": 35,
        "opt-brilliance": true, "val-brilliance": 40,
        "opt-stereo": true, "val-stereo": 28,
        "opt-limiter": true, "val-limiter": 50
    }
};

function applyPreset(presetName) {
    const values = PRESET_VALUES[presetName];
    if (!values) return;

    Object.entries(values).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.type === 'checkbox') {
            el.checked = Boolean(value);
        } else {
            el.value = String(value);
        }
    });
    updateWarnings();
}

// UI Interactions
// [수정] 드롭존 클릭 시 fileInput이 이미 활성화되어 있으면 중복 실행 방지
dropZone.addEventListener('click', (e) => {
    if (e.target === fileInput) return; // fileInput 자체 클릭은 무시
    fileInput.click();
});

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        handleFileChange();
    }
});

fileInput.addEventListener('change', handleFileChange);

function handleFileChange() {
    if (fileInput.files.length > 0) {
        fileNameDisplay.textContent = fileInput.files[0].name;
        submitBtn.disabled = false;
        
        // Prepare original player
        const url = URL.createObjectURL(fileInput.files[0]);
        originalPlayer.src = url;
        
        // Reset result area
        resultArea.style.display = 'none';
        restoredPlayer.src = '';
        feedbackStatus.textContent = '';
        lastTaskId = null;
    } else {
        fileNameDisplay.textContent = "Drag & Drop or Click to Select File";
        submitBtn.disabled = true;
    }
}

function updateWarnings() {
    const warningBox = document.getElementById('warning-box');
    
    const isSuperRes = document.getElementById('opt-super-res').checked;
    const isPunchy = document.getElementById('opt-punchy-bass').checked;
    const isBrilliance = document.getElementById('opt-brilliance').checked;
    const isLimiter = document.getElementById('opt-limiter').checked;
    const isStereo = document.getElementById('opt-stereo').checked;

    const valSuperRes = parseInt(document.getElementById('val-super-res').value, 10);
    const valPunchy = parseInt(document.getElementById('val-punchy-bass').value, 10);
    const valBrilliance = parseInt(document.getElementById('val-brilliance').value, 10);
    const valLimiter = parseInt(document.getElementById('val-limiter').value, 10);
    const valStereo = parseInt(document.getElementById('val-stereo').value, 10);

    let load = 0;
    if (isLimiter) load += valLimiter * 0.5;
    if (isBrilliance) load += valBrilliance * 0.25;
    if (isSuperRes) load += valSuperRes * 0.25;
    if (isPunchy) load += valPunchy * 0.2;

    let warnings = [];
    warningBox.className = 'warning-box';

    if (load > 95) {
        warnings.push("🚨 경고: 마스터링 압력이 매우 높아 사운드가 심하게 찌그러질 (Squashed & Overcompressed) 위험이 있습니다!");
        warningBox.classList.add('danger');
    } else if (load > 70) {
        warnings.push("⚠️ 주의: 여러 효과 강도가 높아 음원에 따라 강한 펌핑(눌림) 현상이 발생할 수 있습니다.");
    }

    if (isBrilliance && valBrilliance > 75) {
        warnings.push("💡 하모닉 익사이터(Brilliance) 강도가 너무 높으면 보컬/심벌이 쇳소리처럼 찢어질 수 있습니다.");
    }

    if (isStereo && valStereo > 85) {
        warnings.push("🔊 스테레오 확장을 너무 많이 주면 모노(Mono) 환경에서 가운데 소리가 사라지는 위상 캔슬링이 발생할 수 있습니다.");
    }

    if (document.getElementById('opt-highpass').checked) {
        const valHP = parseInt(document.getElementById('val-highpass').value, 10);
        if (valHP > 120) {
            warnings.push("⚠️ Low Cut이 너무 높으면 저음이 사라져 소리가 얇아질 수 있습니다.");
        }
    }

    if (warnings.length > 0) {
        warningBox.innerHTML = warnings.join("<br><br>");
        warningBox.style.display = 'block';
    } else {
        warningBox.style.display = 'none';
        warningBox.innerHTML = "";
    }
}

document.querySelectorAll('.options-grid input').forEach(input => {
    input.addEventListener('input', updateWarnings);
    input.addEventListener('change', updateWarnings);
});

updateWarnings();
qualityPreset.addEventListener('change', () => applyPreset(qualityPreset.value));
applyPreset(qualityPreset.value);

async function postForm(url, formData) {
    const res = await fetch(url, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
        throw new Error(data?.detail || `Request failed: ${url}`);
    }
    return data;
}

// Form Submission
form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!fileInput.files[0]) return;

    submitBtn.disabled = true;
    loader.style.display = 'block';
    resultArea.style.display = 'none';

    try {
        const selectedFile = fileInput.files[0];
        const preset = qualityPreset.value;

        statusText.textContent = 'Analyzing source audio...';
        const analyzeForm = new FormData();
        analyzeForm.append('file', selectedFile);
        const analyzeData = await postForm('/api/analyze', analyzeForm);

        statusText.textContent = 'Building AI processing plan...';
        const planForm = new FormData();
        planForm.append('analysis_json', JSON.stringify(analyzeData.analysis || {}));
        planForm.append('preferred_preset', preset);
        const planData = await postForm('/api/plan', planForm);
        const planSource = String(planData.plan_source || "rule_based");
        const planError = planData.plan_error ? String(planData.plan_error) : "";
        planSourceBadge.classList.remove('external', 'fallback');
        if (planSource === 'external_ai' || planSource === 'external_ai_openai' || planSource === 'external_ai_google') {
            planSourceBadge.textContent = 'Plan: external AI';
            planSourceBadge.classList.add('external');
        } else if (planSource === 'rule_based_fallback') {
            planSourceBadge.textContent = `Plan: fallback (rule-based)${planError ? ` - ${planError}` : ''}`;
            planSourceBadge.classList.add('fallback');
        } else {
            planSourceBadge.textContent = 'Plan: local rule-based';
        }

        statusText.textContent = 'Applying restoration chain...';
        const durationSec = Number((analyzeData.analysis || {}).duration_sec || 0);
        const estimatedLong = durationSec >= 170 || selectedFile.size >= (20 * 1024 * 1024);
        const host = window.location.hostname || "";
        const isLocalHost = host === "localhost" || host === "127.0.0.1" || host.startsWith("192.168.") || host.startsWith("10.") || host.startsWith("172.");
        const processingMode = isLocalHost ? 'full' : (estimatedLong ? 'fast_cloud' : 'full');
        processingModeBadge.classList.remove('mode-fast');
        processingModeBadge.textContent = `Mode: ${processingMode}`;
        if (processingMode === 'fast_cloud') {
            processingModeBadge.classList.add('mode-fast');
        }
        const restoreForm = new FormData();
        restoreForm.append('file', selectedFile);
        restoreForm.append('preset', preset);
        restoreForm.append('processing_mode', processingMode);
        restoreForm.append('processing_plan_json', JSON.stringify(planData.processing_plan || {}));
        restoreForm.append('plan_source', planSource);
        restoreForm.append('plan_error', planError);
        restoreForm.append('analysis_json', JSON.stringify(analyzeData.analysis || {}));
        restoreForm.append('super_res', document.getElementById('opt-super-res').checked);
        restoreForm.append('val_super_res', document.getElementById('val-super-res').value);
        restoreForm.append('noise_reduction', document.getElementById('opt-noise-reduction').checked);
        restoreForm.append('val_noise_reduction', document.getElementById('val-noise-reduction').value);
        restoreForm.append('punchy_bass', document.getElementById('opt-punchy-bass').checked);
        restoreForm.append('val_punchy_bass', document.getElementById('val-punchy-bass').value);
        restoreForm.append('brilliance', document.getElementById('opt-brilliance').checked);
        restoreForm.append('val_brilliance', document.getElementById('val-brilliance').value);
        restoreForm.append('stereo_widener', document.getElementById('opt-stereo').checked);
        restoreForm.append('val_stereo', document.getElementById('val-stereo').value);
        restoreForm.append('limiter', document.getElementById('opt-limiter').checked);
        restoreForm.append('val_limiter', document.getElementById('val-limiter').value);
        restoreForm.append('highpass', document.getElementById('opt-highpass').checked);
        restoreForm.append('val_highpass', document.getElementById('val-highpass').value);

        const data = await postForm('/api/restore', restoreForm);
        const taskId = data.task_id;

        let done = false;
        let pollCount = 0;
        const maxPollCount = 180; // about 6 minutes
        let lastProgress = 5;

        while (!done) {
            await new Promise(r => setTimeout(r, 2000));
            pollCount += 1;
            if (pollCount > maxPollCount) {
                throw new Error("처리 시간이 너무 길어 타임아웃되었습니다. 파일 길이나 옵션을 줄여 다시 시도해 주세요.");
            }

            const statusRes = await fetch(`/api/status/${taskId}`);
            const statusData = await statusRes.json();
            if (statusData.status === "processing") {
                const p = Number(statusData.progress || lastProgress || 5);
                lastProgress = Math.max(lastProgress, Math.min(99, p));
                const stage = String(statusData.stage || "processing");
                statusText.textContent = `Processing Audio with AI... ${lastProgress}% (${stage})`;
            }

            if (statusData.status === "done") {
                done = true;

                const downloadUrl = `/api/download/${taskId}`;
                restoredPlayer.src = downloadUrl;
                downloadLink.href = downloadUrl;
                lastTaskId = taskId;
                feedbackStatus.textContent = '결과를 들어보고 피드백을 남겨주세요.';

                const metrics = statusData.metrics;

                const elLufs = document.getElementById("metric-lufs");
                const elCrest = document.getElementById("metric-crest");
                const elPhase = document.getElementById("metric-phase");

                if (metrics) {
                    elLufs.textContent = metrics.LUFS + " LUFS";
                    elCrest.textContent = metrics.Crest_dB + " dB";
                    elPhase.textContent = metrics.Phase_Corr;
                }

                statusText.textContent = 'Processing Audio with AI... 100% (done)';
                loader.style.display = 'none';
                resultArea.style.display = 'block';
            }
            if (statusData.status === "error") {
                done = true;
                alert("처리 실패: " + statusData.message);
                statusText.textContent = 'Processing Audio with AI...';
                loader.style.display = 'none';
            }
        }

    } catch (error) {
        alert("An error occurred during audio processing: " + error.message);
        statusText.textContent = 'Processing Audio with AI...';
        loader.style.display = 'none';
    } finally {
        submitBtn.disabled = false;
    }
});

feedbackButtons.forEach((btn) => {
    btn.addEventListener('click', async () => {
        if (!lastTaskId) {
            feedbackStatus.textContent = '먼저 처리 결과를 생성해 주세요.';
            return;
        }
        const feedback = btn.getAttribute('data-feedback');
        if (!feedback) return;

        try {
            const formData = new FormData();
            formData.append('task_id', lastTaskId);
            formData.append('feedback', feedback);
            await postForm('/api/feedback', formData);
            feedbackStatus.textContent = `피드백 저장됨: ${feedback}`;
        } catch (error) {
            feedbackStatus.textContent = `피드백 저장 실패: ${error.message}`;
        }
    });
});
