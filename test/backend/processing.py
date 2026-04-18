import librosa
import soundfile as sf
import numpy as np
import gc
import os
import tempfile
import traceback
from pedalboard import Pedalboard, Compressor, Distortion, HighpassFilter, Limiter, Gain
from scipy.signal import butter, sosfiltfilt, resample_poly
import noisereduce as nr

# --- 상기 설정: Render 512MB RAM 환경 최적화 ---
_CHUNK_SEC = 10          # 10초 단위 분할 (RAM 점유 최소화)
_LONG_AUDIO_SEC = 15     # 15초 이상이면 즉시 분할 처리

def _ensure_stereo(y: np.ndarray) -> np.ndarray:
    if y.ndim == 1:
        return np.array([y, y], dtype=np.float32)
    return y.astype(np.float32)

def _normalize_headroom(y: np.ndarray, headroom_db: float = 3.0) -> np.ndarray:
    peak = float(np.max(np.abs(y)))
    if peak <= 1e-9: return y
    target_peak = 10 ** (-headroom_db / 20.0)
    if peak <= target_peak: return y
    return y * (target_peak / peak)

def _high_band_deficit_ratio(y: np.ndarray, sr: int, split_hz: float = 6000.0) -> float:
    stft = np.abs(librosa.stft(librosa.to_mono(y), n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low_mask = (freqs >= 150.0) & (freqs < split_hz)
    high_mask = freqs >= split_hz
    low_energy = float(np.mean(stft[low_mask])) + 1e-9
    high_energy = float(np.mean(stft[high_mask])) + 1e-9
    return float(np.clip((0.22 - (high_energy / low_energy)) / 0.22, 0.0, 1.0))

def _adaptive_noise_reduce(ch: np.ndarray, sr: int, intensity: float, fast_mode: bool = False) -> np.ndarray:
    frame, hop = 2048, 512
    rms = librosa.feature.rms(y=ch, frame_length=frame, hop_length=hop)[0]
    if rms.size == 0: return ch
    q = np.percentile(rms, 20)
    quiet_idx = np.where(rms <= q)[0]
    noise_segments = [ch[int(idx*hop):min(int(idx*hop)+frame, ch.shape[0])] for idx in quiet_idx[:40]]
    
    if noise_segments:
        y_noise = np.concatenate(noise_segments)
        return nr.reduce_noise(y=ch, sr=sr, y_noise=y_noise, prop_decrease=float(np.clip(intensity, 0.15, 0.72)), stationary=True)
    return nr.reduce_noise(y=ch, sr=sr, prop_decrease=0.3, stationary=True)

def _classify_content(y: np.ndarray, sr: int) -> str:
    y_mono = librosa.to_mono(y)
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y_mono, sr=sr)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y_mono)))
    return "voice" if centroid > 2100.0 and zcr > 0.075 else "music"

def _apply_deesser(y: np.ndarray, sr: int, strength: float, fast_mode: bool = False) -> np.ndarray:
    def _deess_ch(ch):
        n_fft, hop = (1024, 256) if fast_mode else (2048, 512)
        stft = librosa.stft(ch, n_fft=n_fft, hop_length=hop)
        mag, phase = np.abs(stft), np.angle(stft)
        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        s_band = (freqs >= 4500.0) & (freqs <= 9800.0)
        if not np.any(s_band): return ch
        s_energy = np.mean(mag[s_band, :], axis=0)
        thr = np.percentile(s_energy, 78)
        atten = 1.0 - (np.maximum((s_energy - thr) / (thr + 1e-9), 0.0) * (0.35 * strength))
        mag[s_band, :] *= atten[np.newaxis, :]
        return librosa.istft(mag * np.exp(1j * phase), hop_length=hop, length=ch.shape[0])
    return np.array([_deess_ch(y[0]), _deess_ch(y[1])], dtype=np.float32)

def _split_bands(ch: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    low_sos = butter(4, 180.0, btype="low", fs=sr, output="sos")
    high_sos = butter(4, 3500.0, btype="high", fs=sr, output="sos")
    low, high = sosfiltfilt(low_sos, ch), sosfiltfilt(high_sos, ch)
    return low.astype(np.float32), (ch - low - high).astype(np.float32), high.astype(np.float32)

def _multiband_glue(y: np.ndarray, sr: int, intensity: float, content_type: str) -> np.ndarray:
    if intensity <= 0.01: return y
    out = []
    for ch in y:
        l, m, h = _split_bands(ch, sr)
        l_c = Pedalboard([Compressor(threshold_db=-22.0, ratio=2.0, attack_ms=25, release_ms=180)])(l, sr)
        m_c = Pedalboard([Compressor(threshold_db=-20.0, ratio=2.2, attack_ms=12, release_ms=120)])(m, sr)
        h_c = Pedalboard([Compressor(threshold_db=-24.0, ratio=1.8, attack_ms=5, release_ms=80)])(h, sr)
        out.append((l_c + m_c + h_c).astype(np.float32))
    return np.array(out, dtype=np.float32)

def _true_peak_db(y: np.ndarray) -> float:
    peaks = [np.max(np.abs(resample_poly(ch, 4, 1))) for ch in y]
    return float(20.0 * np.log10(max(peaks) + 1e-12))

def _find_split_point(y_mono: np.ndarray, sr: int, target_sample: int) -> int:
    search = int(3.0 * sr)
    start, end = max(0, target_sample - search), min(len(y_mono), target_sample + search)
    rms = librosa.feature.rms(y=y_mono[start:end], frame_length=1024, hop_length=256)[0]
    return int(np.clip(start + np.argmin(rms) * 256, 0, len(y_mono)))

def _split_audio_at_silence(y: np.ndarray, sr: int) -> list:
    y_mono = librosa.to_mono(y)
    total, chunk_s, pos, chunks = y.shape[1], int(_CHUNK_SEC * sr), 0, []
    while pos < total:
        target = pos + chunk_s
        if target >= total:
            chunks.append(y[:, pos:]); break
        split = _find_split_point(y_mono, sr, target)
        chunks.append(y[:, pos:split])
        pos = split
    return chunks

def process_audio(input_path: str, output_path: str, options: dict):
    try:
        y_full, sr = librosa.load(input_path, sr=None, mono=False)
        y_full = _ensure_stereo(y_full)
        duration = y_full.shape[1] / sr

        if duration > _LONG_AUDIO_SEC:
            chunks = _split_audio_at_silence(y_full, sr)
            del y_full; gc.collect()
            
            processed_chunks = []
            for i, chunk in enumerate(chunks):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t_in, \
                     tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t_out:
                    sf.write(t_in.name, chunk.T, sr, subtype="PCM_16")
                    success, _, _ = _process_audio_core(t_in.name, t_out.name, options)
                    if success:
                        res, _ = librosa.load(t_out.name, sr=sr, mono=False)
                        processed_chunks.append(_ensure_stereo(res))
                    else:
                        processed_chunks.append(chunk)
                for p in [t_in.name, t_out.name]:
                    if os.path.exists(p): os.remove(p)
                del chunk; gc.collect()

            y_out = np.concatenate(processed_chunks, axis=1)
            sf.write(output_path, y_out.T, sr, subtype="PCM_16")
            return True, "Success (10s Chunked)", {"Mode": "Chunked"}

        return _process_audio_core(input_path, output_path, options)
    except Exception as e:
        return False, f"{str(e)}\n{traceback.format_exc()}", {}

def _process_audio_core(input_path: str, output_path: str, options: dict):
    try:
        y, sr = librosa.load(input_path, sr=None, mono=False)
        y = _normalize_headroom(_ensure_stereo(y), 3.0)
        y_ref = y.copy()
        content = _classify_content(y, sr)
        
        # 1. Noise Reduction
        if options.get("noise_reduction"):
            val = float(options.get("val_noise_reduction", 60)) / 100.0
            y = np.array([_adaptive_noise_reduce(y[0], sr, val), _adaptive_noise_reduce(y[1], sr, val)])

        # 2. Highpass
        if options.get("highpass"):
            y = Pedalboard([HighpassFilter(cutoff_frequency_hz=float(options.get("val_highpass", 80)))])(y, sr)

        # 3. Super Res (Highs Restore)
        if options.get("super_res"):
            val = float(options.get("val_super_res", 50)) / 100.0
            mix = val * _high_band_deficit_ratio(y, sr) * 0.25
            y_h = Pedalboard([HighpassFilter(6200), Distortion(5.0)])(y, sr)
            y = (y * (1.0 - mix)) + (y_h * mix)

        # 4. De-esser & Multiband
        y = _apply_deesser(y, sr, 0.5, fast_mode=True)
        y = _multiband_glue(y, sr, 0.3, content)

        # 5. Gain & Limiter
        target_lufs = -16.0 if content == "voice" else -14.0
        # (Loudness 정규화 로직 생략/간소화 - RAM 절약)
        if options.get("limiter"):
            y = Pedalboard([Gain(2.0), Limiter(threshold_db=-1.0)])(y, sr)

        sf.write(output_path, y.T, sr, subtype="PCM_16")
        del y, y_ref; gc.collect()
        return True, "Success", {}
    except Exception as e:
        return False, str(e), {}