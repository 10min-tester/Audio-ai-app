import librosa
import soundfile as sf
import numpy as np
import os
from pedalboard import Pedalboard, Compressor, Distortion, HighpassFilter, Limiter, Gain

# Note: noisereduce relies on noisereduce module which we assume is installed
import noisereduce as nr

def process_audio(input_path: str, output_path: str, options: dict):
    """"
    사용자가 선택한 6가지 음질 개선(DSP) 옵션을 실제로 연산합니다.
    """""
    try:
        # 1. Load Audio (Convert to Stereo for spatial processing)
        y, sr = librosa.load(input_path, sr=44100, mono=False)
        
        # Ensure audio is 2D (Stereo)
        if y.ndim == 1:
            y = np.array([y, y])
            
        board = Pedalboard()
        
        # 2. Noise Reduction (Clean Background)
        # [수정] noisereduce는 1D 배열만 지원 → 채널별로 분리해서 처리 후 합치기
        if options.get("noise_reduction"):
            val = float(options.get("val_noise_reduction", 60)) / 100.0  # 0.0 ~ 1.0
            y_left  = nr.reduce_noise(y=y[0], sr=sr, prop_decrease=val)
            y_right = nr.reduce_noise(y=y[1], sr=sr, prop_decrease=val)
            y = np.array([y_left, y_right])
            
        # 3. Punchy Bass (Add Transient Energy)
        if options.get("punchy_bass"):
            val = float(options.get("val_punchy_bass", 50)) / 100.0
            ratio_val = 1.0 + (val * 7.0) # Ratio 1.0 to 8.0
            board.append(Compressor(threshold_db=-15, ratio=ratio_val, attack_ms=10.0, release_ms=100.0))
            
        # Apply Pedalboard plugins grouped so far
        if len(board) > 0:
            y = board(y, sr)
            board = Pedalboard() # Reset for next stage
        
        # 4. Super Resolution (Restore Highs / Bandwidth Extension)
        if options.get("super_res"):
            val = float(options.get("val_super_res", 50)) / 100.0
            mix_amt = val * 0.3 # 0 to 30% mix
            sr_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=10000), Distortion(drive_db=15)])
            y_highs = sr_board(y, sr)
            y = y + (y_highs * mix_amt)
            
        # 5. Brilliance (Harmonic Exciter)
        if options.get("brilliance"):
            val = float(options.get("val_brilliance", 40)) / 100.0
            mix_amt = val * 0.25 # 0 to 25% mix
            brilliance_board = Pedalboard([HighpassFilter(cutoff_frequency_hz=4000), Distortion(drive_db=8)])
            y_bright = brilliance_board(y, sr)
            y = y + (y_bright * mix_amt)
            
        # 6. Stereo Widener (Spatial Expansion)
        if options.get("stereo_widener"):
            val = float(options.get("val_stereo", 40)) / 100.0
            widen_amt = 1.0 + val # 1.0x to 2.0x widen
            mid = (y[0] + y[1]) / 2.0
            side = (y[0] - y[1]) / 2.0
            side = side * widen_amt
            y = np.array([mid + side, mid - side])
            
        # 7. Loudness Maximizer (Limiter)
        if options.get("limiter"):
            val = float(options.get("val_limiter", 80)) / 100.0
            gain_amt = val * 5.0 # Add 0 to 5.0 dB
            board.append(Gain(gain_db=gain_amt))
            board.append(Limiter(threshold_db=-0.5))
            y = board(y, sr)
            
        # Prevents any clipping artifacts
        y = np.clip(y, -1.0, 1.0)
        
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
