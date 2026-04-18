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

# --- Render Free Tier (512MB RAM) 최적화 설정 ---
_CHUNK_SEC = 10          
_LONG_AUDIO_SEC = 15     

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
    # 메모리 절약을 위해 mono로 변환 후 분석
    y_mono = librosa.to_mono(y)
    stft = np.abs(librosa.stft(y_mono, n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low_mask = (freqs >= 150.0) & (freqs < split_hz)
    high_mask = freqs >= split_hz
    low_energy = float(np.mean(stft[low_mask])) + 1e-9
    high_energy = float(np.mean(stft[high_mask])) + 1e-9
    return float(np.clip((0.22 - (high_energy / low_energy)) / 0.22, 0.0, 1.0))

def _classify_content(y: np.ndarray, sr: int) -> str:
    y_mono = librosa.to_mono(y)
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y_mono, sr=sr)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y_mono)))
    return "voice" if centroid > 2100.0 and zcr > 0.075 else "music"

# --- main.py에서 호출하는 필수 함수 1: analyze_audio ---
def analyze_audio(input_path: str) -> dict:
    y, sr = librosa.load(input_path, sr=None, mono=False)
    y = _ensure_stereo(y)
    y_mono = librosa.to_mono(y)
    
    duration = float(y.shape[1] / sr)
    peak = float(np.max(np.abs(y_mono)))
    
    # 512MB 환경을 위해 분석 후 즉시 메모리 해제 시도
    high_deficit = _high_band_deficit_ratio(y, sr)
    content = _classify_content(y, sr)
    
    res = {
        "duration_sec": round(duration, 2),
        "peak_db": round(float(20 * np.log10(peak + 1e-10)), 2),
        "high_band_loss": round(high_deficit * 100, 1),
        "content_type": content
    }
    del y, y_mono; gc.collect()
    return res

# --- main.py에서 호출하는 필수 함수 2: build_processing_plan ---
def build_processing_plan(analysis: dict) -> dict:
    # 분석 결과에 따라 자동으로 옵션 제안
    plan = {
        "noise_reduction": True if analysis["content_type"] == "voice" else False,
        "super_res": True if analysis["high_band_loss"] > 30 else False,
        "highpass": True,
        "limiter": True
    }
    return plan

def _adaptive_noise_reduce(ch: np.ndarray, sr: int, intensity: float) -> np.ndarray:
    return nr.reduce_noise(y=ch, sr=sr, prop_decrease=float(np.clip(intensity, 0.1, 0.8)), stationary=True)

def _apply_deesser(y: np.ndarray, sr: int, strength: float) -> np.ndarray:
    def _deess_ch(ch):
        stft = librosa.stft(ch, n_fft=2048, hop_length=512)
        mag, phase = np.abs(stft), np.angle(stft)
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        s_band = (freqs >= 4500.0) & (freqs <= 9800.0)
        if not np.any(s_band): return ch
        mag[s_band, :] *= 0.8 # 단순화된 감쇄
        return librosa.istft(mag * np.exp(1j * phase), hop_length=512, length=ch.shape[0])
    return np.array([_deess_ch(y[0]), _deess_ch(y[1])], dtype=np.float32)

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

# --- main.py에서 호출하는 필수 함수 3: process_audio ---
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
                    _process_audio_core(t_in.name, t_out.name, options)
                    res, _ = librosa.load(t_out.name, sr=sr, mono=False)
                    processed_chunks.append(_ensure_stereo(res))
                
                if os.path.exists(t_in.name): os.remove(t_in.name)
                if os.path.exists(t_out.name): os.remove(t_out.name)
                del chunk; gc.collect()

            y_out = np.concatenate(processed_chunks, axis=1)
            sf.write(output_path, y_out.T, sr, subtype="PCM_16")
            return True, "Success (10s Chunked)", {}

        return _process_audio_core(input_path, output_path, options)
    except Exception as e:
        return False, f"{str(e)}\n{traceback.format_exc()}", {}

def _process_audio_core(input_path: str, output_path: str, options: dict):
    try:
        y, sr = librosa.load(input_path, sr=None, mono=False)
        y = _normalize_headroom(_ensure_stereo(y), 3.0)
        
        if options.get("noise_reduction"):
            y = np.array([_adaptive_noise_reduce(y[0], sr, 0.5), _adaptive_noise_reduce(y[1], sr, 0.5)])

        if options.get("highpass"):
            y = Pedalboard([HighpassFilter(cutoff_frequency_hz=80.0)])(y, sr)

        if options.get("super_res"):
            y_h = Pedalboard([HighpassFilter(6000), Distortion(drive_db=10)])(y, sr)
            y = (y * 0.8) + (y_h * 0.2)

        if options.get("limiter"):
            y = Pedalboard([Gain(gain_db=2.0), Limiter(threshold_db=-1.0)])(y, sr)

        sf.write(output_path, y.T, sr, subtype="PCM_16")
        del y; gc.collect()
        return True, "Success", {}
    except Exception as e:
        return False, str(e), {}