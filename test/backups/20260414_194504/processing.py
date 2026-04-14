import librosa
import soundfile as sf
import numpy as np
from pedalboard import Pedalboard, Compressor, Distortion, HighpassFilter, Limiter, Gain

# Note: noisereduce relies on noisereduce module which we assume is installed
import noisereduce as nr


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

    if noise_segments:
        y_noise = np.concatenate(noise_segments)
        return nr.reduce_noise(
            y=ch,
            sr=sr,
            y_noise=y_noise,
            prop_decrease=float(np.clip(intensity, 0.15, 0.75)),
            stationary=True
        )
    return nr.reduce_noise(y=ch, sr=sr, prop_decrease=float(np.clip(intensity, 0.15, 0.65)))


def process_audio(input_path: str, output_path: str, options: dict):
    """"
    사용자가 선택한 6가지 음질 개선(DSP) 옵션을 실제로 연산합니다.
    """""
    try:
        # 1. Load Audio (Convert to Stereo for spatial processing)
        y, sr = librosa.load(input_path, sr=None, mono=False)
        y = _ensure_stereo(y)
        y = _normalize_headroom(y, headroom_db=3.0)
        preset_profile = _get_preset_profile(str(options.get("preset", "music_balanced")))

        board = Pedalboard()

        # 2. Noise Reduction (Clean Background)
        if options.get("noise_reduction"):
            val = float(options.get("val_noise_reduction", 60)) / 100.0
            val = float(np.clip(val * preset_profile["nr_mul"], 0.0, 1.0))
            y_left = _adaptive_noise_reduce(y[0], sr, val)
            y_right = _adaptive_noise_reduce(y[1], sr, val)
            y = np.array([y_left, y_right])

        # High-pass filter
        if options.get("highpass"):
            cutoff = float(options.get("val_highpass", 80))
            cutoff = float(np.clip(cutoff, 20.0, 180.0))
            hp_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=cutoff)])
            y = hp_board(y, sr)

        # 3. Punchy Bass (Add Transient Energy)
        if options.get("punchy_bass"):
            val = float(options.get("val_punchy_bass", 50)) / 100.0
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
            mix_amt = float(np.clip(val * deficit * 0.28 * preset_profile["sr_mix_mul"], 0.0, 0.3))
            sr_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=6200), Distortion(drive_db=5.0)])
            y_highs = sr_board(y, sr)
            y = (y * (1.0 - mix_amt)) + (y_highs * mix_amt) + (y * 0.03 * deficit)

        # 5. Brilliance (Harmonic Exciter)
        if options.get("brilliance"):
            val = float(options.get("val_brilliance", 40)) / 100.0
            mix_amt = float(np.clip(val * 0.18 * preset_profile["brilliance_mul"], 0.0, 0.22))
            brilliance_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=4300), Distortion(drive_db=4.0)])
            y_bright = brilliance_board(y, sr)
            y = (y * (1.0 - mix_amt)) + (y_bright * mix_amt)

        # 6. Stereo Widener (Spatial Expansion)
        if y.shape[0] == 2:  # stereo일 때만
            if options.get("stereo_widener"):
                val = float(options.get("val_stereo", 40)) / 100.0
                val *= float(preset_profile["stereo_mul"])
                y = _safe_phase_widen(y, width_strength=val)

        # Auto Gain based on LUFS
        try:
            import pyloudnorm as pyln
            meter = pyln.Meter(sr)
            loudness = meter.integrated_loudness(y.T)

            # limiter 강도가 높으면 더 보수적 타겟을 사용해 과압축 방지
            limiter_val = float(options.get("val_limiter", 80)) / 100.0
            target_lufs = float(preset_profile["target_lufs"])
            if limiter_val > 0.7:
                target_lufs -= 0.3

            gain_db = target_lufs - loudness

            # 과도한 증폭 방지
            gain_db = max(min(gain_db, 4.0), -5.0)

            gain_board = Pedalboard([Gain(gain_db=gain_db)])
            y = gain_board(y, sr)

        except Exception as e:
            print("Auto Gain Error:", e)

        # 7. Loudness Maximizer (Limiter)
        if options.get("limiter"):
            val = float(options.get("val_limiter", 80)) / 100.0
            val *= float(preset_profile["limiter_mul"])
            gain_amt = val * 1.6
            board.append(Gain(gain_db=gain_amt))
            board.append(Limiter(threshold_db=-1.0))
            y = board(y, sr)

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
        except Exception as e:
            metrics["LUFS"] = 0
            metrics["Crest_dB"] = 0
            metrics["Phase_Corr"] = 0
            print("Analysis Error:", e)

        # 8. Save Audio Output
        sf.write(output_path, y.T, sr, subtype='PCM_16')
        
        return True, "Success", metrics
    except Exception as e:
        import traceback
        return False, str(e) + "\n" + traceback.format_exc(), {}
