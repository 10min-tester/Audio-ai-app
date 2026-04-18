if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .register("./service-worker.js")
    .then((reg) => console.log("Service Worker registered", reg))
    .catch((err) => console.error("Service Worker registration failed", err));
}

const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("audio-file");
const fileNameDisplay = document.getElementById("file-name-display");
const submitBtn = document.getElementById("submit-btn");
const form = document.getElementById("upload-form");
const loader = document.getElementById("loader");
const statusText = document.querySelector(".status-text");
const resultArea = document.getElementById("result-area");
const originalPlayer = document.getElementById("original-player");
const restoredPlayer = document.getElementById("restored-player");
const downloadLink = document.getElementById("download-link");
const qualityPreset = document.getElementById("quality-preset");
const planSourceBadge = document.getElementById("plan-source-badge");
const processingModeBadge = document.getElementById("processing-mode-badge");
const feedbackStatus = document.getElementById("feedback-status");
const feedbackButtons = document.querySelectorAll(".feedback-btn");
const analysisFeedback = document.getElementById("analysis-feedback");
let lastBatchId = "";
let lastBatchItems = [];

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
    if (el.type === "checkbox") {
      el.checked = Boolean(value);
    } else {
      el.value = String(value);
    }
  });
  updateWarnings();
}

dropZone.addEventListener("click", (e) => {
  if (e.target === fileInput) return;
  fileInput.click();
});

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("dragover");
});

dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("dragover");
});

dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("dragover");
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    handleFileChange();
  }
});

fileInput.addEventListener("change", handleFileChange);

function handleFileChange() {
  const files = fileInput.files;
  if (files.length > 0) {
    fileNameDisplay.textContent = files.length === 1
      ? files[0].name
      : `${files.length} files selected`;
    submitBtn.disabled = false;
    originalPlayer.src = URL.createObjectURL(files[0]);
    resultArea.style.display = "none";
    restoredPlayer.src = "";
    feedbackStatus.textContent = "";
    analysisFeedback.innerHTML = "";
    lastBatchId = "";
    lastBatchItems = [];
  } else {
    fileNameDisplay.textContent = "Drag & Drop or Click to Select File(s)";
    submitBtn.disabled = true;
  }
}

function updateWarnings() {
  const warningBox = document.getElementById("warning-box");
  const isSuperRes = document.getElementById("opt-super-res").checked;
  const isPunchy = document.getElementById("opt-punchy-bass").checked;
  const isBrilliance = document.getElementById("opt-brilliance").checked;
  const isLimiter = document.getElementById("opt-limiter").checked;
  const isStereo = document.getElementById("opt-stereo").checked;

  const valSuperRes = parseInt(document.getElementById("val-super-res").value, 10);
  const valPunchy = parseInt(document.getElementById("val-punchy-bass").value, 10);
  const valBrilliance = parseInt(document.getElementById("val-brilliance").value, 10);
  const valLimiter = parseInt(document.getElementById("val-limiter").value, 10);
  const valStereo = parseInt(document.getElementById("val-stereo").value, 10);

  let load = 0;
  if (isLimiter) load += valLimiter * 0.5;
  if (isBrilliance) load += valBrilliance * 0.25;
  if (isSuperRes) load += valSuperRes * 0.25;
  if (isPunchy) load += valPunchy * 0.2;

  const warnings = [];
  warningBox.className = "warning-box";

  if (load > 95) {
    warnings.push("🚨 경고: 마스터링 압력이 매우 높습니다.");
    warningBox.classList.add("danger");
  } else if (load > 70) {
    warnings.push("⚠️ 주의: 효과 강도가 높아 과처리될 수 있습니다.");
  }
  if (isBrilliance && valBrilliance > 75) {
    warnings.push("💡 Brilliance가 너무 높으면 고역이 거칠 수 있습니다.");
  }
  if (isStereo && valStereo > 85) {
    warnings.push("🔊 Stereo 확장이 너무 높으면 위상 문제가 날 수 있습니다.");
  }

  if (warnings.length > 0) {
    warningBox.innerHTML = warnings.join("<br><br>");
    warningBox.style.display = "block";
  } else {
    warningBox.style.display = "none";
    warningBox.innerHTML = "";
  }
}

document.querySelectorAll(".options-grid input").forEach((input) => {
  input.addEventListener("input", updateWarnings);
  input.addEventListener("change", updateWarnings);
});

updateWarnings();
qualityPreset.addEventListener("change", () => applyPreset(qualityPreset.value));
applyPreset(qualityPreset.value);

async function postForm(url, formData) {
  const res = await fetch(url, { method: "POST", body: formData });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.detail || `Request failed: ${url}`);
  return data;
}

async function getJsonOrThrow(url) {
  const res = await fetch(url);
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    data = {};
  }
  if (!res.ok) throw new Error(data?.detail || `Request failed: ${url}`);
  return data;
}

async function uploadFileToStorage(file) {
  const initForm = new FormData();
  initForm.append("filename", file.name);
  initForm.append("content_type", file.type || "application/octet-stream");
  const init = await postForm("/api/v2/storage/init-upload", initForm);
  const upload = init.upload || {};
  const method = upload.method || "PUT";
  const headers = upload.headers || {};
  const res = await fetch(upload.url, {
    method,
    headers,
    body: file,
  });
  if (!res.ok) {
    let errText = "";
    try {
      const errJson = await res.json();
      errText = errJson?.detail || "";
    } catch (_) {
      errText = "";
    }
    throw new Error(errText || `Upload failed for ${file.name}`);
  }
  return {
    filename: file.name,
    input_uri: init.input_uri,
  };
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!fileInput.files || fileInput.files.length === 0) return;

  submitBtn.disabled = true;
  loader.style.display = "block";
  resultArea.style.display = "none";
  analysisFeedback.innerHTML = "";
  planSourceBadge.classList.remove("external", "fallback");
  planSourceBadge.textContent = "Plan: checking...";
  processingModeBadge.textContent = "Mode: v2 batch";

  try {
    const files = Array.from(fileInput.files);
    const refs = [];
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i];
      statusText.textContent = `Uploading source files... (${i + 1}/${files.length})`;
      const ref = await uploadFileToStorage(file);
      refs.push(ref);
    }
    statusText.textContent = "Starting batch restoration...";
    const restoreForm = new FormData();
    restoreForm.append("refs_json", JSON.stringify(refs));

    const preset = qualityPreset.value;
    restoreForm.append("preset", preset);
    restoreForm.append("processing_mode", "full");
    restoreForm.append("super_res", document.getElementById("opt-super-res").checked);
    restoreForm.append("val_super_res", document.getElementById("val-super-res").value);
    restoreForm.append("noise_reduction", document.getElementById("opt-noise-reduction").checked);
    restoreForm.append("val_noise_reduction", document.getElementById("val-noise-reduction").value);
    restoreForm.append("punchy_bass", document.getElementById("opt-punchy-bass").checked);
    restoreForm.append("val_punchy_bass", document.getElementById("val-punchy-bass").value);
    restoreForm.append("brilliance", document.getElementById("opt-brilliance").checked);
    restoreForm.append("val_brilliance", document.getElementById("val-brilliance").value);
    restoreForm.append("stereo_widener", document.getElementById("opt-stereo").checked);
    restoreForm.append("val_stereo", document.getElementById("val-stereo").value);
    restoreForm.append("limiter", document.getElementById("opt-limiter").checked);
    restoreForm.append("val_limiter", document.getElementById("val-limiter").value);
    restoreForm.append("highpass", document.getElementById("opt-highpass").checked);
    restoreForm.append("val_highpass", document.getElementById("val-highpass").value);

    const data = await postForm("/api/v2/restore-batch-by-ref", restoreForm);
    const batchId = data.batch_id;
    let done = false;
    const maxPollCount = 300; // ~10 minutes with 2s interval
    let pollCount = 0;

    while (!done) {
      if (pollCount >= maxPollCount) {
        throw new Error("Batch polling timed out. Please retry with fewer files.");
      }
      pollCount += 1;

      await new Promise((r) => setTimeout(r, 2000));
      const statusData = await getJsonOrThrow(`/api/v2/status-batch/${batchId}`);

      const progress = Number(statusData.progress || 0);
      const doneCount = Number(statusData.count_done || 0);
      const errorCount = Number(statusData.count_error || 0);
      const totalCount = Number(statusData.count_total || files.length);
      statusText.textContent = `Batch processing... ${progress}% (${doneCount}/${totalCount}, errors: ${errorCount})`;

      if (statusData.status === "done" || statusData.status === "done_with_errors") {
        done = true;
        loader.style.display = "none";
        resultArea.style.display = "block";

        const items = Array.isArray(statusData.items) ? statusData.items : [];
        lastBatchId = batchId;
        lastBatchItems = items;
        const doneItems = items.filter((item) => item.status === "done");
        const hasExternalAI = doneItems.some((item) => {
          const src = String(item.plan_source || "");
          return src === "external_ai" || src === "external_ai_openai" || src === "external_ai_google";
        });
        const hasFallback = doneItems.some((item) => String(item.plan_source || "").startsWith("rule_based_fallback"));
        const fallbackErrors = doneItems
          .map((item) => String(item.plan_error || ""))
          .filter(Boolean);

        planSourceBadge.classList.remove("external", "fallback");
        if (hasExternalAI) {
          planSourceBadge.textContent = "Plan: external AI (partial/full)";
          planSourceBadge.classList.add("external");
        } else if (hasFallback) {
          const errText = fallbackErrors.length ? ` - ${fallbackErrors[0]}` : "";
          planSourceBadge.textContent = `Plan: fallback (rule-based)${errText}`;
          planSourceBadge.classList.add("fallback");
        } else {
          planSourceBadge.textContent = "Plan: local rule-based";
        }

        const playable = items.find((item) => item.status === "done");
        if (playable) {
          restoredPlayer.src = `/api/v2/download-item/${batchId}/${playable.index}`;
        }

        downloadLink.href = `/api/v2/download-batch/${batchId}`;
        downloadLink.textContent = "📦 Download Restored Batch (.zip)";

        const rows = items.map((item) => {
          const icon = item.status === "done" ? "✅" : item.status === "error" ? "❌" : "⏳";
          const msg = item.message ? ` - ${item.message}` : "";
          const itemHref = item.status === "done"
            ? `/api/v2/download-item/${batchId}/${item.index}`
            : "";
          return item.status === "done"
            ? `${icon} <a href="${itemHref}" target="_blank" rel="noopener">${item.filename}</a>${msg}`
            : `${icon} ${item.filename}${msg}`;
        });
        analysisFeedback.innerHTML = rows.join("<br>");
        feedbackStatus.textContent = statusData.status === "done_with_errors"
          ? "일부 파일 처리 실패. 목록을 확인해 주세요."
          : "배치 처리 완료.";

        try {
          const insights = await getJsonOrThrow("/api/v2/insights?limit=300");
          const recommendation = insights?.summary?.recommendation;
          if (recommendation) {
            feedbackStatus.textContent += ` 추천 프리셋: ${recommendation}`;
          }
        } catch (_) {
          // Keep UI resilient even when insights endpoint fails.
        }
      } else if (statusData.status === "error" || statusData.status === "not_found") {
        done = true;
        if (statusData.status === "not_found") {
          throw new Error("Batch was not found on server.");
        }
        throw new Error("Batch processing failed.");
      }
    }
  } catch (error) {
    loader.style.display = "none";
    statusText.textContent = "Processing Audio with AI...";
    alert(`Batch restore error: ${error.message}`);
  } finally {
    submitBtn.disabled = false;
  }
});

feedbackButtons.forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!lastBatchId) {
      feedbackStatus.textContent = "먼저 배치 처리 결과를 생성해 주세요.";
      return;
    }
    const hasDone = lastBatchItems.some((item) => item.status === "done");
    if (!hasDone) {
      feedbackStatus.textContent = "피드백을 저장할 완료 파일이 없습니다.";
      return;
    }

    const feedback = btn.getAttribute("data-feedback");
    if (!feedback) return;

    try {
      const formData = new FormData();
      formData.append("batch_id", lastBatchId);
      formData.append("feedback", feedback);
      formData.append("item_index", "-1");
      const res = await postForm("/api/v2/feedback", formData);
      feedbackStatus.textContent = `피드백 저장됨: ${feedback} (${res.updated_count}개 항목 반영)`;
    } catch (error) {
      feedbackStatus.textContent = `피드백 저장 실패: ${error.message}`;
    }
  });
});

