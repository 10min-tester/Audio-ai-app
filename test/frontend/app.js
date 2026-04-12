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
const resultArea = document.getElementById('result-area');
const originalPlayer = document.getElementById('original-player');
const restoredPlayer = document.getElementById('restored-player');
const downloadLink = document.getElementById('download-link');

// UI Interactions
dropZone.addEventListener('click', () => fileInput.click());

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
    } else {
        fileNameDisplay.textContent = "Drag & Drop or Click to Select File";
        submitBtn.disabled = true;
    }
}

function updateWarnings() {
    const warningBox = document.getElementById('warning-box');
    
    // Check toggle statuses
    const isSuperRes = document.getElementById('opt-super-res').checked;
    const isPunchy = document.getElementById('opt-punchy-bass').checked;
    const isBrilliance = document.getElementById('opt-brilliance').checked;
    const isLimiter = document.getElementById('opt-limiter').checked;
    const isStereo = document.getElementById('opt-stereo').checked;

    // Get slider values
    const valSuperRes = parseInt(document.getElementById('val-super-res').value, 10);
    const valPunchy = parseInt(document.getElementById('val-punchy-bass').value, 10);
    const valBrilliance = parseInt(document.getElementById('val-brilliance').value, 10);
    const valLimiter = parseInt(document.getElementById('val-limiter').value, 10);
    const valStereo = parseInt(document.getElementById('val-stereo').value, 10);

    let load = 0;
    if (isLimiter) load += valLimiter * 0.5; // Max 50
    if (isBrilliance) load += valBrilliance * 0.25; // Max 25
    if (isSuperRes) load += valSuperRes * 0.25; // Max 25
    if (isPunchy) load += valPunchy * 0.2; // Max 20

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

    if (warnings.length > 0) {
        warningBox.innerHTML = warnings.join("<br><br>");
        warningBox.style.display = 'block';
    } else {
        warningBox.style.display = 'none';
        warningBox.innerHTML = "";
    }
}

// Add event listeners to all inputs to trigger warnings
document.querySelectorAll('.options-grid input').forEach(input => {
    input.addEventListener('input', updateWarnings);
    input.addEventListener('change', updateWarnings);
});

// Run once on load
updateWarnings();

// Form Submission
form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!fileInput.files[0]) return;

    submitBtn.disabled = true;
    loader.style.display = 'block';
    resultArea.style.display = 'none';

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('super_res', document.getElementById('opt-super-res').checked);
    formData.append('val_super_res', document.getElementById('val-super-res').value);
    formData.append('noise_reduction', document.getElementById('opt-noise-reduction').checked);
    formData.append('val_noise_reduction', document.getElementById('val-noise-reduction').value);
    formData.append('punchy_bass', document.getElementById('opt-punchy-bass').checked);
    formData.append('val_punchy_bass', document.getElementById('val-punchy-bass').value);
    formData.append('brilliance', document.getElementById('opt-brilliance').checked);
    formData.append('val_brilliance', document.getElementById('val-brilliance').value);
    formData.append('stereo_widener', document.getElementById('opt-stereo').checked);
    formData.append('val_stereo', document.getElementById('val-stereo').value);
    formData.append('limiter', document.getElementById('opt-limiter').checked);
    formData.append('val_limiter', document.getElementById('val-limiter').value);

    try {
        // API URL (adjust for production)
        const response = await fetch('/api/restore', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Restoration failed');
        }

        // Extract Audio Analysis from Headers
        const lufs = parseFloat(response.headers.get("X-Analysis-LUFS") || "0");
        const crest = parseFloat(response.headers.get("X-Analysis-Crest") || "0");
        const phase = parseFloat(response.headers.get("X-Analysis-Phase") || "0");
        
        // Update Report UI
        const elLufs = document.getElementById("metric-lufs");
        const elCrest = document.getElementById("metric-crest");
        const elPhase = document.getElementById("metric-phase");
        const elFeedback = document.getElementById("analysis-feedback");
        
        let feedbacks = [];
        
        // 1. LUFS Logic
        elLufs.textContent = lufs + " LUFS";
        if (lufs > -8.0) {
            elLufs.className = "value danger";
            feedbacks.push("🚨 [경고] 볼륨이 너무 큽니다 (-8 LUFS 초과). 라우드니스 리미터 강도를 줄이세요.");
        } else if (lufs > -12.0) {
            elLufs.className = "value warning";
            feedbacks.push("⚠️ [주의] 상업용 음반 수준의 큰 볼륨입니다. 귀에 피로감을 줄 수 있습니다.");
        } else {
            elLufs.className = "value good";
        }

        // 2. Crest Factor Logic
        elCrest.textContent = crest + " dB";
        if (crest < 8.0) {
            elCrest.className = "value danger";
            feedbacks.push("🚨 [경고] 다이내믹 레인지가 너무 좁습니다! (과압축 상태/소시지 파형). 베이스 리드나 리미터를 낮추세요.");
        } else {
            elCrest.className = "value good";
        }

        // 3. Phase Correlation Logic
        elPhase.textContent = phase;
        if (phase < 0.3) {
            elPhase.className = "value danger";
            feedbacks.push("🚨 [경고] 위상 계수가 너무 낮습니다. 모노 환경에서 소리가 사라질(Phase Cancellation) 위험이 큽니다! 스테레오 확장을 줄이세요.");
        } else {
            elPhase.className = "value good";
        }

        if (feedbacks.length > 0) {
            elFeedback.innerHTML = feedbacks.join("<br><br>");
            elFeedback.style.color = "#b91c1c";
        } else {
            elFeedback.innerHTML = "✅ 전문 분석 결과: 다이내믹스와 위상 모두 안정적이고 건강한 마스터링 상태입니다!";
            elFeedback.style.color = "#15803d";
        }

        const blob = await response.blob();
        const outputUrl = URL.createObjectURL(blob);
        
        // Update UI
        restoredPlayer.src = outputUrl;
        downloadLink.href = outputUrl;
        downloadLink.download = `restored_${fileInput.files[0].name}.wav`;
        
        loader.style.display = 'none';
        resultArea.style.display = 'block';

    } catch (error) {
        alert("An error occurred during audio processing: " + error.message);
        loader.style.display = 'none';
    } finally {
        submitBtn.disabled = false;
    }
});
