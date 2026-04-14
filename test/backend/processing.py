import librosa
import soundfile as sf
import numpy as np
from pedalboard import Pedalboard, Compressor, Distortion, HighpassFilter, Limiter, Gain
from scipy.signal import butter, sosfiltfilt, resample_poly

# Note: noisereduce relies on noisereduce module which we assume is installed
import noisereduce as nr


def analyze_audio(input_path: str) -> dict:
    y, sr = librosa.load(input_path, sr=None, mono=False)
    y = _ensure_stereo(y)
    y_mono = librosa.to_mono(y)

    duration_sec = float(y_mono.shape[-1] / sr) if sr > 0 else 0.0
    peak = float(np.max(np.abs(y_mono))) if y_mono.size else 0.0
    rms = float(np.sqrt(np.mean(y_mono**2))) if y_mono.size else 0.0
    crest_db = float(20 * np.log10((peak + 1e-10) / (rms + 1e-10))) if peak > 0 else 0.0

    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(sr)
        lufs = float(meter.integrated_loudness(y.T))
    except Exception:
        lufs = 0.0

    high_deficit = _high_band_deficit_ratio(y, sr, split_hz=6000.0)
    y_mono_stft = np.abs(librosa.stft(y_mono, n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    sib_mask = (freqs >= 4500.0) & (freqs <= 9800.0)
    low_mask = (freqs >= 40.0) & (freqs <= 140.0)
    mid_mask = (freqs >= 500.0) & (freqs <= 2000.0)
    sib_energy = float(np.mean(y_mono_stft[sib_mask])) if np.any(sib_mask) else 0.0
    low_energy = float(np.mean(y_mono_stft[low_mask])) if np.any(low_mask) else 0.0
    mid_energy = float(np.mean(y_mono_stft[mid_mask])) if np.any(mid_mask) else 1e-9
    sibilance_index = sib_energy / (mid_energy + 1e-9)
    low_bloom_index = low_energy / (mid_energy + 1e-9)

    frame = 2048
    hop = 512
    rms_frames = librosa.feature.rms(y=y_mono, frame_length=frame, hop_length=hop)[0]
    noise_floor_db = float(20 * np.log10(np.percentile(rms_frames, 20) + 1e-10)) if rms_frames.size else -90.0

    try:
        from scipy.stats import pearsonr
        phase_corr, _ = pearsonr(y[0] + 1e-10, y[1] + 1e-10)
        phase_corr = float(phase_corr)
    except Exception:
        phase_corr = 1.0

    return {
        "sample_rate": int(sr),
        "duration_sec": round(duration_sec, 2),
        "peak": round(peak, 4),
        "rms": round(rms, 4),
        "crest_db": round(crest_db, 2),
        "lufs": round(lufs, 2),
        "high_band_deficit": round(float(high_deficit), 3),
        "sibilance_index": round(float(sibilance_index), 3),
        "low_bloom_index": round(float(low_bloom_index), 3),
        "noise_floor_db": round(noise_floor_db, 2),
        "phase_corr": round(phase_corr, 3)
    }


def build_processing_plan(analysis: dict, preferred_preset: str = "music_balanced") -> dict:
    plan = {
        "preset": preferred_preset,
        "super_res_mix_mul": 1.0,
        "noise_reduction_mul": 1.0,
        "brilliance_mul": 1.0,
        "stereo_mul": 1.0,
        "limiter_mul": 1.0,
        "target_lufs_offset": 0.0,
        "deesser_mul": 1.0,
        "highpass_offset_hz": 0.0
    }

    if analysis.get("high_band_deficit", 0.0) > 0.45:
        plan["super_res_mix_mul"] = 1.25
        plan["brilliance_mul"] = 1.1

    if analysis.get("noise_floor_db", -90.0) > -42.0:
        plan["noise_reduction_mul"] = 1.2

    if analysis.get("phase_corr", 1.0) < 0.2:
        plan["stereo_mul"] = 0.75

    if analysis.get("crest_db", 12.0) < 7.5:
        plan["limiter_mul"] = 0.7
        plan["target_lufs_offset"] = -0.6

    if analysis.get("sibilance_index", 0.0) > 0.42:
        plan["deesser_mul"] = 1.22
        plan["brilliance_mul"] *= 0.92

    if analysis.get("low_bloom_index", 0.0) > 1.35:
        plan["highpass_offset_hz"] = 18.0
    elif analysis.get("low_bloom_index", 0.0) < 0.75:
        plan["highpass_offset_hz"] = -10.0

    return plan


def _get_preset_profile(preset_name: str) -> dict:
    profiles = {
        "music_balanced": {
            "target_lufs": -14.0,
            "sr_mix_mul": 1.0,
            "nr_mul": 1.0,
            "brilliance_mul": 1.0,
            "stereo_mul": 1.0,
            "limiter_mul": 1.0
        },
        "voice_clean": {
            "target_lufs": -15.0,
            "sr_mix_mul": 0.85,
            "nr_mul": 1.15,
            "brilliance_mul": 0.9,
            "stereo_mul": 0.7,
            "limiter_mul": 0.85
        },
        "compressed_repair": {
            "target_lufs": -14.5,
            "sr_mix_mul": 1.2,
            "nr_mul": 1.0,
            "brilliance_mul": 1.1,
            "stereo_mul": 0.9,
            "limiter_mul": 0.8
        }
    }
    return profiles.get(preset_name, profiles["music_balanced"])


def _ensure_stereo(y: np.ndarray) -> np.ndarray:
    if y.ndim == 1:
        return np.array([y, y], dtype=np.float32)
    return y.astype(np.float32)


def _normalize_headroom(y: np.ndarray, headroom_db: float = 3.0) -> np.ndarray:
    peak = float(np.max(np.abs(y)))
    if peak <= 1e-9:
        return y
    target_peak = 10 ** (-headroom_db / 20.0)
    if peak <= target_peak:
        return y
    return y * (target_peak / peak)


def _high_band_deficit_ratio(y: np.ndarray, sr: int, split_hz: float = 6000.0) -> float:
    # High-band energy가 과도하게 부족한 소스만 더 보강하도록 비율 계산
    stft = np.abs(librosa.stft(librosa.to_mono(y), n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low_mask = (freqs >= 150.0) & (freqs < split_hz)
    high_mask = freqs >= split_hz

    low_energy = float(np.mean(stft[low_mask])) + 1e-9
    high_energy = float(np.mean(stft[high_mask])) + 1e-9
    ratio = high_energy / low_energy
    # ratio가 너무 낮으면(고역 부족) 1.0에 가깝게, 충분하면 0.0에 가깝게
    return float(np.clip((0.22 - ratio) / 0.22, 0.0, 1.0))


def _safe_phase_widen(y: np.ndarray, width_strength: float) -> np.ndarray:
    # 위상 안정성을 위해 side gain 상한을 제한
    from scipy.stats import pearsonr

    mid = (y[0] + y[1]) / 2.0
    side = (y[0] - y[1]) / 2.0
    side_gain = 1.0 + (width_strength * 0.6)  # 기존 대비 과확장 억제

    for _ in range(3):
        y_try = np.array([mid + side * side_gain, mid - side * side_gain], dtype=np.float32)
        try:
            corr, _ = pearsonr(y_try[0] + 1e-10, y_try[1] + 1e-10)
        except Exception:
            corr = 1.0
        if corr >= 0.15:
            return y_try
        side_gain *= 0.85
    return np.array([mid + side, mid - side], dtype=np.float32)


def _safe_phase_widen_highband(y: np.ndarray, sr: int, width_strength: float) -> np.ndarray:
    # 저역 위상 안정성을 위해 고역 밴드 중심으로만 스테레오 확장
    low_l, mid_l, high_l = _split_bands(y[0], sr)
    low_r, mid_r, high_r = _split_bands(y[1], sr)

    high_pair = np.array([high_l, high_r], dtype=np.float32)
    widened_high = _safe_phase_widen(high_pair, width_strength=width_strength)

    out_l = low_l + mid_l + widened_high[0]
    out_r = low_r + mid_r + widened_high[1]
    return np.array([out_l, out_r], dtype=np.float32)


def _adaptive_noise_reduce(ch: np.ndarray, sr: int, intensity: float) -> np.ndarray:
    # 무음/저레벨 구간을 노이즈 프로파일로 사용해 먹먹함을 줄임
    frame = 2048
    hop = 512
    rms = librosa.feature.rms(y=ch, frame_length=frame, hop_length=hop)[0]
    if rms.size == 0:
        return ch

    q = np.percentile(rms, 20)
    quiet_idx = np.where(rms <= q)[0]

    noise_segments = []
    for idx in quiet_idx[:80]:
        start = int(idx * hop)
        end = min(start + frame, ch.shape[0])
        if end - start > 256:
            noise_segments.append(ch[start:end])

    # 프레임별 에너지 차이로 신호-노이즈 분리 정도 추정
    p20 = float(np.percentile(rms, 20))
    p80 = float(np.percentile(rms, 80))
    contrast = (p80 + 1e-9) / (p20 + 1e-9)
    is_music_like = contrast > 2.1

    if noise_segments:
        y_noise = np.concatenate(noise_segments)
        if is_music_like:
            # 음악성 신호는 과감쇠 방지(질감 보존)
            return nr.reduce_noise(
                y=ch,
                sr=sr,
                y_noise=y_noise,
                prop_decrease=float(np.clip(intensity * 0.85, 0.12, 0.58)),
                stationary=False
            )
        return nr.reduce_noise(
            y=ch,
            sr=sr,
            y_noise=y_noise,
            prop_decrease=float(np.clip(intensity, 0.15, 0.72)),
            stationary=True
        )
    if is_music_like:
        return nr.reduce_noise(
            y=ch,
            sr=sr,
            prop_decrease=float(np.clip(intensity * 0.82, 0.1, 0.52)),
            stationary=False
        )
    return nr.reduce_noise(y=ch, sr=sr, prop_decrease=float(np.clip(intensity, 0.15, 0.62)))


def _classify_content(y: np.ndarray, sr: int) -> str:
    y_mono = librosa.to_mono(y)
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y_mono, sr=sr)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y_mono)))
    # 음성은 평균 centroid가 중고역에 치우치고, zcr이 상대적으로 높은 경향
    if centroid > 2100.0 and zcr > 0.075:
        return "voice"
    return "music"


def _apply_deesser_channel(ch: np.ndarray, sr: int, strength: float) -> np.ndarray:
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.01:
        return ch

    n_fft = 2048
    hop = 512
    stft = librosa.stft(ch, n_fft=n_fft, hop_length=hop)
    mag = np.abs(stft)
    phase = np.angle(stft)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    s_band = (freqs >= 4500.0) & (freqs <= 9800.0)
    if not np.any(s_band):
        return ch

    s_energy = np.mean(mag[s_band, :], axis=0)
    thr = np.percentile(s_energy, 78)
    over = np.maximum((s_energy - thr) / (thr + 1e-9), 0.0)
    atten = 1.0 - (np.clip(over, 0.0, 1.0) * (0.35 * strength))
    mag[s_band, :] *= atten[np.newaxis, :]

    out = librosa.istft(mag * np.exp(1j * phase), hop_length=hop, length=ch.shape[0])
    return out.astype(np.float32)


def _apply_deesser(y: np.ndarray, sr: int, strength: float) -> np.ndarray:
    return np.array([
        _apply_deesser_channel(y[0], sr, strength),
        _apply_deesser_channel(y[1], sr, strength)
    ], dtype=np.float32)


def _split_bands(ch: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    low_sos = butter(4, 180.0, btype="low", fs=sr, output="sos")
    high_sos = butter(4, 3500.0, btype="high", fs=sr, output="sos")
    low = sosfiltfilt(low_sos, ch)
    high = sosfiltfilt(high_sos, ch)
    mid = ch - low - high
    return low.astype(np.float32), mid.astype(np.float32), high.astype(np.float32)


def _adaptive_multiband_params(content_type: str, intensity: float, analysis_hint: dict) -> dict:
    intensity = float(np.clip(intensity, 0.0, 1.0))
    crest_hint = float(analysis_hint.get("crest_db", 10.0))
    noise_floor_hint = float(analysis_hint.get("noise_floor_db", -60.0))

    if content_type == "voice":
        low_ratio = 1.2 + (0.6 * intensity)
        mid_ratio = 1.4 + (1.0 * intensity)
        high_ratio = 1.1 + (0.6 * intensity)
        low_attack, low_release = 28.0, 190.0
        mid_attack, mid_release = 14.0, 130.0
        high_attack, high_release = 5.0, 85.0
    else:
        low_ratio = 1.45 + (1.25 * intensity)
        mid_ratio = 1.25 + (0.95 * intensity)
        high_ratio = 1.2 + (0.75 * intensity)
        low_attack, low_release = 22.0, 170.0
        mid_attack, mid_release = 11.0, 115.0
        high_attack, high_release = 3.0, 68.0

    # 다이내믹이 이미 좁으면(낮은 crest) 더 보수적으로
    if crest_hint < 8.0:
        low_ratio *= 0.9
        mid_ratio *= 0.88
        high_ratio *= 0.9
        low_release += 25.0
        mid_release += 20.0
        high_release += 15.0

    # 노이즈 바닥이 높으면(덜 음수) 고역 release를 늘려 거친 pumping 완화
    if noise_floor_hint > -45.0:
        high_release += 22.0
        high_attack += 1.5

    return {
        "low_ratio": float(np.clip(low_ratio, 1.05, 3.8)),
        "mid_ratio": float(np.clip(mid_ratio, 1.05, 3.8)),
        "high_ratio": float(np.clip(high_ratio, 1.05, 3.8)),
        "low_attack": low_attack,
        "low_release": low_release,
        "mid_attack": mid_attack,
        "mid_release": mid_release,
        "high_attack": high_attack,
        "high_release": high_release
    }


def _multiband_glue(y: np.ndarray, sr: int, intensity: float, content_type: str, analysis_hint: dict | None = None) -> np.ndarray:
    intensity = float(np.clip(intensity, 0.0, 1.0))
    if intensity <= 0.01:
        return y

    params = _adaptive_multiband_params(
        content_type=content_type,
        intensity=intensity,
        analysis_hint=(analysis_hint or {})
    )

    out = []
    for ch in y:
        low, mid, high = _split_bands(ch, sr)
        low_c = Pedalboard([
            Compressor(
                threshold_db=-22.0,
                ratio=params["low_ratio"],
                attack_ms=params["low_attack"],
                release_ms=params["low_release"]
            )
        ])(low, sr)
        mid_c = Pedalboard([
            Compressor(
                threshold_db=-20.0,
                ratio=params["mid_ratio"],
                attack_ms=params["mid_attack"],
                release_ms=params["mid_release"]
            )
        ])(mid, sr)
        high_c = Pedalboard([
            Compressor(
                threshold_db=-24.0,
                ratio=params["high_ratio"],
                attack_ms=params["high_attack"],
                release_ms=params["high_release"]
            )
        ])(high, sr)
        out.append((low_c + mid_c + high_c).astype(np.float32))
    return np.array(out, dtype=np.float32)


def _preserve_transients(y_processed: np.ndarray, y_reference: np.ndarray, amount: float) -> np.ndarray:
    amount = float(np.clip(amount, 0.0, 1.0))
    if amount <= 0.01:
        return y_processed

    out = []
    kernel = np.ones(96, dtype=np.float32) / 96.0
    for ch_ref, ch_proc in zip(y_reference, y_processed):
        smooth_ref = np.convolve(ch_ref, kernel, mode="same")
        transient_ref = ch_ref - smooth_ref
        out.append(ch_proc + (transient_ref * (0.35 * amount)))
    return np.array(out, dtype=np.float32)


def _get_output_profile(content_type: str, preset_name: str) -> dict:
    if content_type == "voice":
        return {"target_lufs": -16.0, "true_peak_dbtp": -1.2}
    if preset_name == "compressed_repair":
        return {"target_lufs": -14.5, "true_peak_dbtp": -1.0}
    return {"target_lufs": -14.0, "true_peak_dbtp": -1.0}


def _true_peak_db(y: np.ndarray, upsample: int = 4) -> float:
    # 간단한 오버샘플링 기반 true peak 근사
    peaks = []
    for ch in y:
        up = resample_poly(ch, upsample, 1)
        peaks.append(float(np.max(np.abs(up))))
    peak = max(peaks) if peaks else 0.0
    return float(20.0 * np.log10(peak + 1e-12))


def _apply_true_peak_guard(y: np.ndarray, target_dbtp: float) -> np.ndarray:
    tp_db = _true_peak_db(y, upsample=4)
    if tp_db <= target_dbtp:
        return y
    gain_db = target_dbtp - tp_db
    gain_lin = float(10 ** (gain_db / 20.0))
    return (y * gain_lin).astype(np.float32)


def _overprocessing_guard(y_processed: np.ndarray, y_reference: np.ndarray, sr: int) -> np.ndarray:
    # 과도한 고역/다이내믹 손실이 감지되면 원본을 소량 블렌딩
    def _crest(signal: np.ndarray) -> float:
        peak = float(np.max(np.abs(signal)))
        rms = float(np.sqrt(np.mean(signal**2)) + 1e-12)
        return 20.0 * np.log10((peak + 1e-12) / rms)

    proc_mono = librosa.to_mono(y_processed)
    ref_mono = librosa.to_mono(y_reference)

    proc_crest = _crest(proc_mono)
    ref_crest = _crest(ref_mono)
    crest_drop = max((ref_crest - proc_crest), 0.0)

    proc_hdef = _high_band_deficit_ratio(y_processed, sr, split_hz=6000.0)
    ref_hdef = _high_band_deficit_ratio(y_reference, sr, split_hz=6000.0)
    brighten_jump = max((ref_hdef - proc_hdef), 0.0)

    blend = 0.0
    if crest_drop > 3.0:
        blend += min((crest_drop - 3.0) * 0.06, 0.18)
    if brighten_jump > 0.35:
        blend += min((brighten_jump - 0.35) * 0.25, 0.16)

    blend = float(np.clip(blend, 0.0, 0.24))
    if blend <= 0.0:
        return y_processed
    return (y_processed * (1.0 - blend) + y_reference * blend).astype(np.float32)


def _dynamic_super_res_mix(y: np.ndarray, sr: int, base_mix: float) -> float:
    # 고역 에너지와 centroid를 함께 봐서 복원량을 자동 보정
    base_mix = float(np.clip(base_mix, 0.0, 0.35))
    y_mono = librosa.to_mono(y)
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y_mono, sr=sr)))
    rolloff = float(np.mean(librosa.feature.spectral_rolloff(y=y_mono, sr=sr, roll_percent=0.90)))

    # 저품질/저대역 입력일수록 mix를 키우고, 이미 밝은 소스는 줄임
    centroid_mul = 1.18 if centroid < 1700 else (0.88 if centroid > 2800 else 1.0)
    rolloff_mul = 1.16 if rolloff < 4200 else (0.9 if rolloff > 7600 else 1.0)
    return float(np.clip(base_mix * centroid_mul * rolloff_mul, 0.0, 0.35))


def _chain_intensity_scale(analysis_hint: dict, content_type: str) -> float:
    crest = float(analysis_hint.get("crest_db", 10.0))
    noise_floor = float(analysis_hint.get("noise_floor_db", -60.0))
    sibilance = float(analysis_hint.get("sibilance_index", 0.25))

    scale = 1.0
    # 이미 눌린 음원은 강도 보수화
    if crest < 7.5:
        scale *= 0.82
    elif crest > 13.0:
        scale *= 1.06

    # 노이즈가 높으면 노이즈/디에서 중심으로 강도 소폭 상승
    if noise_floor > -42.0:
        scale *= 1.05

    if content_type == "voice" and sibilance > 0.42:
        scale *= 0.93

    return float(np.clip(scale, 0.72, 1.12))


def _safe_wet_mix(y_processed: np.ndarray, y_reference: np.ndarray, wet: float) -> np.ndarray:
    wet = float(np.clip(wet, 0.0, 1.0))
    if wet >= 0.999:
        return y_processed
    return (y_processed * wet + y_reference * (1.0 - wet)).astype(np.float32)


def process_audio(input_path: str, output_path: str, options: dict):
    """"
    사용자가 선택한 6가지 음질 개선(DSP) 옵션을 실제로 연산합니다.
    """""
    try:
        # 1. Load Audio (Convert to Stereo for spatial processing)
        y, sr = librosa.load(input_path, sr=None, mono=False)
        y = _ensure_stereo(y)
        y = _normalize_headroom(y, headroom_db=3.0)
        y_reference = y.copy()
        preset_name = str(options.get("preset", "music_balanced"))
        preset_profile = _get_preset_profile(preset_name)
        plan = options.get("processing_plan") or {}
        content_type = _classify_content(y, sr)
        output_profile = _get_output_profile(content_type=content_type, preset_name=preset_name)

        plan_sr_mul = float(np.clip(plan.get("super_res_mix_mul", 1.0), 0.5, 1.6))
        plan_nr_mul = float(np.clip(plan.get("noise_reduction_mul", 1.0), 0.6, 1.5))
        plan_br_mul = float(np.clip(plan.get("brilliance_mul", 1.0), 0.6, 1.5))
        plan_st_mul = float(np.clip(plan.get("stereo_mul", 1.0), 0.5, 1.3))
        plan_lim_mul = float(np.clip(plan.get("limiter_mul", 1.0), 0.5, 1.3))
        plan_deesser_mul = float(np.clip(plan.get("deesser_mul", 1.0), 0.7, 1.5))
        plan_highpass_offset = float(np.clip(plan.get("highpass_offset_hz", 0.0), -30.0, 40.0))
        plan_lufs_offset = float(np.clip(plan.get("target_lufs_offset", 0.0), -2.0, 2.0))
        analysis_hint = options.get("analysis_hint") or {}
        chain_scale = _chain_intensity_scale(analysis_hint=analysis_hint, content_type=content_type)

        board = Pedalboard()

        # 2. Noise Reduction (Clean Background)
        if options.get("noise_reduction"):
            val = float(options.get("val_noise_reduction", 60)) / 100.0
            val = float(np.clip(val * preset_profile["nr_mul"] * plan_nr_mul, 0.0, 1.0))
            val = float(np.clip(val * (1.03 if chain_scale > 1.0 else 0.98), 0.0, 1.0))
            y_left = _adaptive_noise_reduce(y[0], sr, val)
            y_right = _adaptive_noise_reduce(y[1], sr, val)
            y = np.array([y_left, y_right])

        # High-pass filter
        if options.get("highpass"):
            cutoff = float(options.get("val_highpass", 80))
            cutoff += plan_highpass_offset
            cutoff = float(np.clip(cutoff, 20.0, 180.0))
            hp_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=cutoff)])
            y = hp_board(y, sr)

        # 3. Punchy Bass (Add Transient Energy)
        if options.get("punchy_bass"):
            val = float(options.get("val_punchy_bass", 50)) / 100.0
            if content_type == "voice":
                ratio_val = 1.15 + (val * 1.0)
                board.append(Compressor(threshold_db=-21.0, ratio=ratio_val, attack_ms=16.0, release_ms=130.0))
            else:
                ratio_val = 1.4 + (val * 2.2)
                board.append(Compressor(threshold_db=-18.0, ratio=ratio_val, attack_ms=14.0, release_ms=110.0))

        # Apply Pedalboard plugins grouped so far
        if len(board) > 0:
            y = board(y, sr)
            board = Pedalboard()

        # 4. Super Resolution (Restore Highs / Bandwidth Extension)
        if options.get("super_res"):
            val = float(options.get("val_super_res", 50)) / 100.0
            deficit = _high_band_deficit_ratio(y, sr, split_hz=6000.0)
            base_mix = float(np.clip(val * deficit * 0.28 * preset_profile["sr_mix_mul"] * plan_sr_mul, 0.0, 0.3))
            mix_amt = _dynamic_super_res_mix(y, sr, base_mix=base_mix)
            mix_amt = float(np.clip(mix_amt * chain_scale, 0.0, 0.35))
            sr_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=6200), Distortion(drive_db=5.0)])
            y_highs = sr_board(y, sr)
            y = (y * (1.0 - mix_amt)) + (y_highs * mix_amt) + (y * 0.03 * deficit)

        # 5. Brilliance (Harmonic Exciter)
        if options.get("brilliance"):
            val = float(options.get("val_brilliance", 40)) / 100.0
            mix_amt = float(np.clip(val * 0.18 * preset_profile["brilliance_mul"] * plan_br_mul, 0.0, 0.22))
            mix_amt = float(np.clip(mix_amt * chain_scale, 0.0, 0.24))
            brilliance_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=4300), Distortion(drive_db=4.0)])
            y_bright = brilliance_board(y, sr)
            y = (y * (1.0 - mix_amt)) + (y_bright * mix_amt)

        # 6. De-esser (high harshness control)
        deesser_strength = float(np.clip((float(options.get("val_brilliance", 40)) / 100.0) * 0.9, 0.0, 1.0))
        if content_type == "voice":
            deesser_strength = float(np.clip(deesser_strength + 0.18, 0.0, 1.0))
        deesser_strength = float(np.clip(deesser_strength * plan_deesser_mul, 0.0, 1.0))
        if chain_scale < 0.9:
            deesser_strength = float(np.clip(deesser_strength * 1.05, 0.0, 1.0))
        y = _apply_deesser(y, sr, deesser_strength)

        # 7. Multiband glue compression
        mb_intensity = 0.35 if content_type == "music" else 0.28
        if options.get("punchy_bass"):
            mb_intensity += (float(options.get("val_punchy_bass", 50)) / 100.0) * 0.15
        mb_intensity *= chain_scale
        y = _multiband_glue(
            y,
            sr,
            intensity=float(np.clip(mb_intensity, 0.15, 0.65)),
            content_type=content_type,
            analysis_hint=analysis_hint
        )

        # 8.5 Transient preserve
        transient_amount = 0.26 if content_type == "music" else 0.2
        y = _preserve_transients(y, y_reference=y_reference, amount=transient_amount)

        # 9. Stereo Widener (Spatial Expansion)
        if y.shape[0] == 2:  # stereo일 때만
            if options.get("stereo_widener"):
                val = float(options.get("val_stereo", 40)) / 100.0
                if content_type == "voice":
                    val *= 0.75
                val *= float(preset_profile["stereo_mul"]) * plan_st_mul
                y = _safe_phase_widen_highband(y, sr=sr, width_strength=val)

        # Auto Gain based on LUFS
        try:
            import pyloudnorm as pyln
            meter = pyln.Meter(sr)
            loudness = meter.integrated_loudness(y.T)

            # limiter 강도가 높으면 더 보수적 타겟을 사용해 과압축 방지
            limiter_val = float(options.get("val_limiter", 80)) / 100.0
            target_lufs = float(output_profile["target_lufs"])
            if limiter_val > 0.7:
                target_lufs -= 0.3
            target_lufs += plan_lufs_offset

            gain_db = target_lufs - loudness

            # 과도한 증폭 방지
            gain_db = max(min(gain_db, 4.0), -5.0)

            gain_board = Pedalboard([Gain(gain_db=gain_db)])
            y = gain_board(y, sr)

        except Exception as e:
            print("Auto Gain Error:", e)

        # 10. Loudness Maximizer (Limiter)
        if options.get("limiter"):
            val = float(options.get("val_limiter", 80)) / 100.0
            val *= float(preset_profile["limiter_mul"]) * plan_lim_mul
            val *= chain_scale
            gain_amt = val * 1.6
            board.append(Gain(gain_db=gain_amt))
            board.append(Limiter(threshold_db=-1.0))
            y = board(y, sr)

        # 분석 힌트 기반 안전 wet/dry (소스 손상 방지)
        wet = 0.97
        if float(analysis_hint.get("crest_db", 10.0)) < 7.0:
            wet = 0.9
        elif float(analysis_hint.get("crest_db", 10.0)) < 8.0:
            wet = 0.94
        y = _safe_wet_mix(y, y_reference=y_reference, wet=wet)

        y = _overprocessing_guard(y, y_reference=y_reference, sr=sr)
        y = _apply_true_peak_guard(y, target_dbtp=float(output_profile["true_peak_dbtp"]))
        y = _normalize_headroom(y, headroom_db=1.0)

        # --- Stage 5: Audio Analysis Report ---
        metrics = {}
        try:
            # 1. LUFS (Using pyloudnorm requires (N, 2) shape)
            import pyloudnorm as pyln
            meter = pyln.Meter(sr)
            metrics["LUFS"] = round(meter.integrated_loudness(y.T), 2)
            
            # 2. Crest Factor (Dynamic Range: Peak to RMS)
            peak = np.max(np.abs(y))
            rms = np.sqrt(np.mean(y**2))
            # avoid log(0)
            crest_factor = 20 * np.log10(peak / (rms + 1e-10)) if peak > 0 else 0
            metrics["Crest_dB"] = round(float(crest_factor), 2)
            
            # 3. Phase Correlation (Pearson r)
            from scipy.stats import pearsonr
            if y.shape[0] == 2:
                # Add tiny noise to avoid variance=0 errors in pure silence
                r, p = pearsonr(y[0] + 1e-10, y[1] + 1e-10)
                metrics["Phase_Corr"] = round(float(r), 2)
            else:
                metrics["Phase_Corr"] = 1.0 # Mono
            metrics["TruePeak_dBTP"] = round(_true_peak_db(y, upsample=4), 2)
            metrics["Content"] = content_type
        except Exception as e:
            metrics["LUFS"] = 0
            metrics["Crest_dB"] = 0
            metrics["Phase_Corr"] = 0
            metrics["TruePeak_dBTP"] = 0
            metrics["Content"] = content_type
            print("Analysis Error:", e)

        # 8. Save Audio Output
        sf.write(output_path, y.T, sr, subtype='PCM_16')
        
        return True, "Success", metrics
    except Exception as e:
        import traceback
        return False, str(e) + "\n" + traceback.format_exc(), {}
