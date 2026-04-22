"""
=============================================================================
  SMART MOBILITY AID  —  Final Version
  "Eyes for the Blind"

  Team: Subash & Team
  Project: Embedded Edge-AI Based Smart Mobility Aid for
           Obstacle Recognition and Voice-Based Environmental Feedback
=============================================================================

  INSTALL
  -------
  pip install ultralytics opencv-python torch numpy scikit-learn

  For natural voice (strongly recommended — sounds human):
    pip install gtts pygame

  For offline voice fallback:
    pip install pyttsx3

  RUN
  ---
  python smart_mobility_aid_final.py

  CONTROLS (while window is open)
  --------------------------------
  Q  →  quit and print session metrics
  Y  →  mark last alert as CORRECT   (for F1 / accuracy tracking)
  N  →  mark last alert as FALSE ALARM

=============================================================================
"""

# ── stdlib ─────────────────────────────────────────────────────────────────
import os
import sys
import time
import platform
import threading
import tempfile
import hashlib
from   collections  import defaultdict, deque
from   dataclasses  import dataclass
from   typing       import Optional

# ── third-party ────────────────────────────────────────────────────────────
import cv2
import numpy as np
import torch
from   ultralytics     import YOLO
from   sklearn.metrics import accuracy_score, f1_score, classification_report


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

class CFG:
    # ── Model ──────────────────────────────────────────────────────────────
    # yolov8n = fastest / TinyML-like  |  yolov8s or yolov8m = more accurate
    YOLO_MODEL   = "yolov8n.pt"
    YOLO_CONF    = 0.38          # lower = catch more; raise if too many FP

    # ── Distance thresholds (metres) ───────────────────────────────────────
    EMERGENCY_M  = 0.8           # STOP
    DANGER_M     = 1.5           # Slow down
    WARNING_M    = 2.8           # Heads-up
    FLANK_MAX_M  = 1.0           # Side objects only warned if within this
    MIDAS_SCALE  = 7.0           # MiDaS depth → metres scale factor

    # ── Alert timing ───────────────────────────────────────────────────────
    # ONE global gap between any two alerts — the user needs time to react.
    GLOBAL_GAP_S = 2.8           # seconds between any alert
    LABEL_GAP_S  = 5.0           # seconds before same label repeats
    CLEAR_GAP_S  = 5.0           # seconds before "clear" repeats

    # ── Depth-grid (Layer 2 — wall/door/surface detection) ─────────────────
    GRID_ROWS    = 3
    GRID_COLS    = 3
    # Normalised MiDaS depth: 0 = far,  1 = very close
    DEPTH_CLOSE  = 0.52          # above this → something is close
    DEPTH_STD    = 0.035         # below this std → flat surface (wall/door)
    # Grid zone indices in path  (row×cols + col):
    #   0 1 2
    #   3 4 5   ← 3,4,5 = middle row (direct path)
    #   6 7 8   ← 7     = bottom centre (floor ahead)
    PATH_ZONES   = {3, 4, 5, 7}

    # ── Optical flow (Layer 3 — approach detection) ─────────────────────────
    OF_RADIAL_TH = 3.2           # px/frame outward flow = approaching danger
    OF_MIN_PTS   = 35
    OF_CTR_FRAC  = 0.50          # centre zone fraction of frame
    OF_HIST_LEN  = 5             # frames to smooth flow signal

    # ── Temporal gate ──────────────────────────────────────────────────────
    TRACK_WIN    = 7             # rolling window
    TRACK_CONF   = 2             # min hits to confirm (low = more responsive)

    # ── Camera ─────────────────────────────────────────────────────────────
    CAM_INDEX    = 0
    CAM_W        = 640
    CAM_H        = 480

    # ── Focal length (px) — standard webcam at 640 px width ────────────────
    FOCAL_PX     = 600


# ── Known real-world object heights (metres) ───────────────────────────────
KNOWN_H = {
    "person":       1.70,
    "chair":        0.90,
    "dining table": 0.75,
    "car":          1.50,
    "truck":        2.50,
    "bicycle":      1.00,
    "motorcycle":   1.10,
    "dog":          0.50,
    "cat":          0.30,
    "couch":        0.80,
    "bed":          0.60,
    "bottle":       0.25,
    "cup":          0.12,
    "suitcase":     0.65,
    "backpack":     0.50,
}

NATIVE_CLS = set(KNOWN_H) | {
    "tv", "laptop", "toilet", "sink", "refrigerator", "oven",
    "microwave", "clock", "vase", "cell phone", "traffic light",
    "stop sign", "bench", "potted plant", "fire hydrant",
}


# ═══════════════════════════════════════════════════════════════════════════
#  VOICE ENGINE  —  natural-sounding, non-blocking
# ═══════════════════════════════════════════════════════════════════════════
#
#  Priority:
#    1. gTTS + pygame  →  natural Google voice, cached to disk
#    2. Windows SAPI   →  built-in, decent quality
#    3. pyttsx3        →  offline fallback
#    4. print only     →  last resort

class VoiceBackend:
    """Wraps whichever TTS backend is available.  All calls are non-blocking."""

    def __init__(self):
        self._backend = self._detect()
        self._lock    = threading.Lock()
        self._busy    = False
        self._cache   = {}          # phrase hash → temp audio file path

    # ── Backend detection ────────────────────────────────────────────────
    def _detect(self):
        # 1. gTTS + pygame
        try:
            from gtts  import gTTS
            import pygame
            pygame.mixer.init()
            self._gtts   = gTTS
            self._pygame = pygame
            print("[VOICE] Using gTTS + pygame  (natural voice)")
            return "gtts"
        except Exception:
            pass

        # 2. Windows SAPI
        if platform.system() == "Windows":
            try:
                import win32com.client
                v = win32com.client.Dispatch("SAPI.SpVoice")
                v.Rate = 0; v.Volume = 100
                self._sapi = v
                print("[VOICE] Using Windows SAPI")
                return "sapi"
            except Exception:
                pass

        # 3. pyttsx3
        try:
            import pyttsx3
            e = pyttsx3.init()
            e.setProperty("rate",   155)
            e.setProperty("volume", 1.0)
            # Try to pick a female voice (sounds softer / clearer)
            voices = e.getProperty("voices")
            for v in voices:
                if "female" in v.name.lower() or "zira" in v.name.lower():
                    e.setProperty("voice", v.id)
                    break
            self._pyttsx3 = e
            print("[VOICE] Using pyttsx3")
            return "pyttsx3"
        except Exception:
            pass

        print("[VOICE] No TTS backend found — printing alerts only")
        return "print"

    # ── Public API ───────────────────────────────────────────────────────
    def say(self, text: str):
        """Speak text in a background thread so detection is never blocked."""
        if self._busy:
            return   # skip if already speaking — never queue up
        t = threading.Thread(target=self._say_sync, args=(text,), daemon=True)
        t.start()

    def _say_sync(self, text: str):
        with self._lock:
            self._busy = True
            try:
                if   self._backend == "gtts":    self._say_gtts(text)
                elif self._backend == "sapi":    self._say_sapi(text)
                elif self._backend == "pyttsx3": self._say_pyttsx3(text)
                else:                            print(f"[VOICE] {text}")
            finally:
                self._busy = False

    def _say_gtts(self, text: str):
        """Cache audio files so repeated phrases play instantly."""
        key  = hashlib.md5(text.encode()).hexdigest()
        path = self._cache.get(key)
        if not path or not os.path.exists(path):
            from gtts import gTTS
            gTTS(text=text, lang="en", slow=False).save(
                path := tempfile.mktemp(suffix=".mp3")
            )
            self._cache[key] = path
        pg = self._pygame
        pg.mixer.music.load(path)
        pg.mixer.music.play()
        while pg.mixer.music.get_busy():
            time.sleep(0.05)

    def _say_sapi(self, text: str):
        self._sapi.Speak(text, 0)   # synchronous inside thread

    def _say_pyttsx3(self, text: str):
        self._pyttsx3.say(text)
        self._pyttsx3.runAndWait()


# ═══════════════════════════════════════════════════════════════════════════
#  ALERT PHRASE BANK  —  varied, natural, human-sounding
# ═══════════════════════════════════════════════════════════════════════════
#
#  Rules:
#  • Short sentences — blind users process audio fast.
#  • Start with ACTION word (Stop / Careful / Watch out / Clear).
#  • Rotate phrases so the user doesn't hear the exact same sentence twice.
#  • No metres in emergency/danger — the user needs action, not numbers.
#  • Only include distance for farther objects (warning level).

_PHRASES = {
    "emergency": [
        "Stop! {label} right in front of you.",
        "Stop now! {label} is blocking your way.",
        "Halt! Very close {label} ahead.",
        "Stop! You are about to hit a {label}.",
    ],
    "danger": [
        "Careful, {label} ahead.",
        "{label} ahead. Slow down.",
        "Watch out, {label} in your path.",
        "Slow down. {label} is close.",
    ],
    "warn_center": [
        "{label} ahead of you.",
        "There is a {label} in front of you.",
        "{label} detected in your path.",
    ],
    "warn_left": [
        "{label} on your left.",
        "Watch the {label} to your left.",
        "Mind the {label} on your left side.",
    ],
    "warn_right": [
        "{label} on your right.",
        "Watch the {label} to your right.",
        "Mind the {label} on your right side.",
    ],
    "clear": [
        "Path is clear.",
        "All clear. Walk forward.",
        "Way is clear ahead.",
    ],
}

class PhraseBank:
    def __init__(self):
        self._idx = defaultdict(int)

    def get(self, category: str, label: str = "", pos: str = "") -> str:
        phrases = _PHRASES[category]
        i       = self._idx[category] % len(phrases)
        self._idx[category] += 1
        return phrases[i].format(label=label, pos=pos)


# ═══════════════════════════════════════════════════════════════════════════
#  DETECTION DATA CLASS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Det:
    label:  str
    dist_m: float
    pos:    str      # "left" | "center" | "right"
    conf:   float
    source: str      # "yolo" | "depth_grid" | "optic_flow"


# ═══════════════════════════════════════════════════════════════════════════
#  LAYER 1 — YOLO OBJECT DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def _infer_class(raw, x1, y1, x2, y2, fh, fw) -> str:
    if raw in NATIVE_CLS:
        return raw
    w = x2-x1; h = y2-y1; area = w*h
    ar = h/w if w > 0 else 0
    if area > 200_000:                       return "wall"
    if ar > 3.5 and w < 70:                  return "pole"
    if 1.8 < ar < 3.5 and 120 < w < 250:    return "door"
    if y2 > fh*0.75 and w > 250:             return "stairs"
    return raw


def _size_dist(label, pixel_h) -> Optional[float]:
    rh = KNOWN_H.get(label)
    return float(np.clip((rh * CFG.FOCAL_PX) / pixel_h, 0.3, 8.0)) \
        if rh and pixel_h > 10 else None


def _region_depth(dmap, x1, y1, x2, y2) -> float:
    """20th-percentile of inner 50% of box = closest sub-region."""
    s = 0.25
    dh, dw = y2-y1, x2-x1
    ry1, ry2 = int(y1+dh*s), int(y2-dh*s)
    rx1, rx2 = int(x1+dw*s), int(x2-dw*s)
    if ry2 <= ry1 or rx2 <= rx1:
        return float(dmap[(y1+y2)//2, (x1+x2)//2])
    return float(np.percentile(dmap[ry1:ry2, rx1:rx2], 20))


def _to_m(v: float) -> float:
    return float(np.clip((1.0 - v) * CFG.MIDAS_SCALE, 0.4, CFG.MIDAS_SCALE))


def run_yolo(frame, model, dmap) -> list[Det]:
    fh, fw = frame.shape[:2]
    out    = []

    for r in model(frame, verbose=False):
        for box in r.boxes:
            conf = float(box.conf[0])
            if conf < CFG.YOLO_CONF:
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            raw   = model.names[int(box.cls[0])]
            label = _infer_class(raw, x1, y1, x2, y2, fh, fw)

            md    = _to_m(_region_depth(dmap, x1, y1, x2, y2))
            sd    = _size_dist(label, y2-y1)
            # 60% geometry + 40% MiDaS when geometry known; else pure MiDaS
            dist  = round(0.6*sd + 0.4*md if sd else md, 1)
            dist  = float(np.clip(dist, 0.4, 7.0))

            cx  = (x1+x2)//2
            pos = "left" if cx < fw/3 else ("right" if cx > 2*fw/3 else "center")

            out.append(Det(label, dist, pos, conf, "yolo"))

            # Overlay
            c = (0, 255, 80) if pos == "center" else (0, 210, 230)
            cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
            cv2.putText(frame, f"{label}  {dist}m",
                        (x1, max(y1-8, 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, c, 2)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  LAYER 2 — DEPTH-GRID  (wall / door / surface detector)
# ═══════════════════════════════════════════════════════════════════════════
#
#  WHY THIS EXISTS:
#  YOLO only fires on objects it recognises from training data.
#  A plain wall, glass door, or dark corridor end = YOLO returns nothing.
#  MiDaS always produces depth though.  We scan the depth map in a grid:
#    • Zone has HIGH mean depth  → something is close in that zone
#    • Zone has LOW std-dev      → flat uniform surface = wall or door
#    • Zone has HIGH std-dev     → textured / edged = furniture / object
#  This is the layer that fixes "Clear when wall is 0.5m away."

def run_depth_grid(frame, dmap) -> list[Det]:
    fh, fw = frame.shape[:2]
    rows, cols = CFG.GRID_ROWS, CFG.GRID_COLS
    ch, cw     = fh // rows, fw // cols
    out        = []
    seen_pos   = set()   # only one detection per position per frame

    for r in range(rows):
        for c in range(cols):
            if r*cols + c not in CFG.PATH_ZONES:
                continue
            y1, y2 = r*ch, r*ch + ch
            x1, x2 = c*cw, c*cw + cw
            patch  = dmap[y1:y2, x1:x2]
            mean_d = float(np.mean(patch))
            std_d  = float(np.std(patch))

            if mean_d < CFG.DEPTH_CLOSE:
                continue   # zone is far — nothing to warn about

            dist_m = _to_m(mean_d)
            label  = "wall" if std_d < CFG.DEPTH_STD else "obstacle"
            pos    = "left" if c == 0 else ("right" if c == cols-1 else "center")

            if pos in seen_pos:
                continue
            seen_pos.add(pos)
            out.append(Det(label, round(dist_m, 1), pos, 0.82, "depth_grid"))

            # Subtle overlay — don't clutter the frame
            intensity = int(np.clip((mean_d - CFG.DEPTH_CLOSE) / 0.48 * 180, 0, 180))
            cv2.rectangle(frame, (x1+2, y1+2), (x2-2, y2-2),
                          (0, intensity, 220), 1)
            cv2.putText(frame, f"{label} ~{dist_m:.1f}m",
                        (x1+4, y1+18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, intensity, 220), 1)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  LAYER 3 — OPTICAL FLOW APPROACH DETECTOR
# ═══════════════════════════════════════════════════════════════════════════
#
#  WHY THIS EXISTS:
#  If the user is WALKING TOWARD any surface — textured or plain — feature
#  points in the centre of the frame show OUTWARD (expanding) motion.
#  Large smooth outward flow = the world is looming = danger.
#  This catches glass doors, plain white walls, and fast approach where
#  depth changes faster than MiDaS can react.

class OpticalFlowLayer:

    _LK = dict(winSize=(15,15), maxLevel=2,
               criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT, 10, 0.03))
    _FT = dict(maxCorners=100, qualityLevel=0.22, minDistance=8, blockSize=7)

    def __init__(self):
        self._prev_gray : Optional[np.ndarray] = None
        self._prev_pts  : Optional[np.ndarray] = None
        self._hist      : deque = deque(maxlen=CFG.OF_HIST_LEN)

    def update(self, gray: np.ndarray, frame: np.ndarray) -> list[Det]:
        out = []

        # First frame — just store
        if self._prev_gray is None:
            self._prev_gray = gray
            self._prev_pts  = cv2.goodFeaturesToTrack(gray, mask=None, **self._FT)
            return out

        # Need enough points
        if self._prev_pts is None or len(self._prev_pts) < CFG.OF_MIN_PTS:
            self._prev_gray = gray
            self._prev_pts  = cv2.goodFeaturesToTrack(gray, mask=None, **self._FT)
            return out

        curr, status, _ = cv2.calcOpticalFlowPyrLK(
            self._prev_gray, gray, self._prev_pts, None, **self._LK
        )
        if curr is None:
            self._prev_gray = gray; self._prev_pts = None
            return out

        good_p = self._prev_pts[status == 1]
        good_c = curr[status == 1]
        if len(good_c) < CFG.OF_MIN_PTS:
            self._prev_gray = gray
            self._prev_pts  = cv2.goodFeaturesToTrack(gray, mask=None, **self._FT)
            return out

        fh, fw = gray.shape
        cx, cy = fw/2, fh/2
        f      = CFG.OF_CTR_FRAC
        mask   = ((good_p[:,0] > cx*(1-f)) & (good_p[:,0] < cx*(1+f)) &
                  (good_p[:,1] > cy*(1-f)) & (good_p[:,1] < cy*(1+f)))
        cp, cc = good_p[mask], good_c[mask]

        if len(cp) >= 12:
            flow     = cc - cp
            centres  = cp - np.array([cx, cy])
            norms    = np.linalg.norm(centres, axis=1, keepdims=True) + 1e-6
            radial   = np.sum(flow * (centres/norms), axis=1)
            self._hist.append(float(np.mean(radial)))
            smooth   = float(np.mean(self._hist))

            if smooth > CFG.OF_RADIAL_TH:
                mag   = float(np.mean(np.linalg.norm(flow, axis=1)))
                dist  = float(np.clip(4.0 - mag * 0.25, 0.5, 3.0))
                out.append(Det("surface", round(dist, 1), "center", 0.72, "optic_flow"))
                cv2.putText(frame, f"Approaching! flow={smooth:.1f}",
                            (10, fh-40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 60, 255), 2)

        self._prev_gray = gray
        self._prev_pts  = cv2.goodFeaturesToTrack(gray, mask=None, **self._FT)
        return out


# ═══════════════════════════════════════════════════════════════════════════
#  TEMPORAL SMOOTHER
# ═══════════════════════════════════════════════════════════════════════════

class Smoother:
    """
    Object must appear in >= TRACK_CONF of last TRACK_WIN frames.
    Distance averaged for stability — stops jittery metre readings.
    """

    def __init__(self):
        self._h: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=CFG.TRACK_WIN)
        )

    def update(self, dets: list[Det]):
        seen = {d.label for d in dets}
        for d in dets:
            self._h[d.label].append(d)
        for lbl in list(self._h):
            if lbl not in seen:
                self._h[lbl].append(None)

    def confirmed(self) -> list[Det]:
        out = []
        for lbl, frames in self._h.items():
            valid = [f for f in frames if f is not None]
            if len(valid) >= CFG.TRACK_CONF:
                avg_d = round(float(np.mean([f.dist_m for f in valid])), 1)
                last  = valid[-1]
                out.append(Det(lbl, avg_d, last.pos, last.conf, last.source))
        return out


# ═══════════════════════════════════════════════════════════════════════════
#  ALERT ENGINE  —  the brain
# ═══════════════════════════════════════════════════════════════════════════
#
#  KEY RULES (designed for a blind user):
#
#  1. EXACTLY ONE alert per cycle.  Never two at once.
#  2. Global gap:  no alert within 2.8 s of the previous one.
#     (User needs time to react and process.)
#  3. Label gap:   same label silent for 5 s after being spoken.
#  4. Priority:    closest centre obstacle first.
#                  Side obstacles only if within FLANK_MAX_M.
#  5. "Clear":     spoken ONLY when ALL THREE layers are completely silent
#                  AND global gap has passed.  Never prematurely.
#  6. Phrases vary so the user doesn't habituate and stop listening.

class AlertEngine:

    def __init__(self, voice: VoiceBackend):
        self._voice      = voice
        self._phrases    = PhraseBank()
        self._last_any   = 0.0
        self._last_label : dict[str, float] = defaultdict(float)
        self._last_clear = 0.0
        self._last_det   : Optional[Det] = None   # for Y/N metrics

    def _can_speak(self, label: str) -> bool:
        now = time.time()
        return (now - self._last_any   >= CFG.GLOBAL_GAP_S and
                now - self._last_label[label] >= CFG.LABEL_GAP_S)

    def _speak(self, text: str, label: str):
        now = time.time()
        self._last_any        = now
        self._last_label[label] = now
        self._voice.say(text)

    def process(self, dets: list[Det]) -> Optional[Det]:
        """
        Process confirmed detections, pick ONE to speak, return it.
        Returns None if nothing was spoken.
        """
        # Separate centre-path and flanking
        center = [d for d in dets
                  if d.pos == "center" and d.dist_m <= CFG.WARNING_M]
        flanks = [d for d in dets
                  if d.pos != "center" and d.dist_m <= CFG.FLANK_MAX_M]

        # Sort by distance — closest danger first
        center.sort(key=lambda x: x.dist_m)
        flanks.sort(key=lambda x: x.dist_m)

        # Priority list: centre first, then very-close flanks
        priority = center + flanks

        for det in priority:
            if not self._can_speak(det.label):
                continue

            phrase = self._build_phrase(det)
            self._speak(phrase, det.label)
            self._last_det = det
            return det      # ONE alert only — stop here

        # Nothing in priority → only then consider "Clear"
        if not priority:
            now = time.time()
            if (now - self._last_any   >= CFG.GLOBAL_GAP_S and
                now - self._last_clear >= CFG.CLEAR_GAP_S):
                phrase = self._phrases.get("clear")
                self._last_any   = now
                self._last_clear = now
                self._voice.say(phrase)

        return None

    def _build_phrase(self, det: Det) -> str:
        lbl, dist, pos = det.label, det.dist_m, det.pos

        if dist <= CFG.EMERGENCY_M:
            return self._phrases.get("emergency", label=lbl)

        if dist <= CFG.DANGER_M:
            return self._phrases.get("danger", label=lbl)

        # Warning level — include direction
        cat = "warn_center" if pos == "center" else f"warn_{pos}"
        return self._phrases.get(cat, label=lbl, pos=pos)

    @property
    def last_det(self) -> Optional[Det]:
        return self._last_det


# ═══════════════════════════════════════════════════════════════════════════
#  METRICS TRACKER
# ═══════════════════════════════════════════════════════════════════════════

class Metrics:

    def __init__(self):
        self.y_true    : list[str]   = []
        self.y_pred    : list[str]   = []
        self.false_a   : int         = 0
        self.total_a   : int         = 0
        self.latencies : list[float] = []

    def log_lat(self, ms: float):      self.latencies.append(ms)
    def log_ok(self, label: str):
        self.total_a  += 1
        self.y_pred.append(label); self.y_true.append(label)
    def log_fa(self, label: str):
        self.total_a  += 1; self.false_a += 1
        self.y_pred.append(label); self.y_true.append("__FA__")

    def report(self) -> str:
        sep = "═" * 60
        L   = ["\n" + sep, "  SESSION METRICS  —  Smart Mobility Aid", sep]
        if self.latencies:
            L.append(f"  Inference latency  :  "
                     f"mean {np.mean(self.latencies):.0f} ms   "
                     f"p95 {np.percentile(self.latencies,95):.0f} ms")
        if self.total_a:
            L.append(f"  Alerts issued      :  {self.total_a}")
            L.append(f"  False alarms       :  {self.false_a}  "
                     f"({self.false_a/self.total_a*100:.1f} %)")
        if len(set(self.y_true)) >= 2:
            acc = accuracy_score(self.y_true, self.y_pred)
            f1w = f1_score(self.y_true, self.y_pred,
                           average="weighted", zero_division=0)
            L += [f"  Accuracy           :  {acc*100:.1f} %",
                  f"  Weighted F1-score  :  {f1w:.3f}",
                  "\n  Per-class report:\n",
                  classification_report(self.y_true, self.y_pred,
                                        zero_division=0)]
        else:
            L.append("  Press Y / N while running to log classification data.")
        L.append(sep + "\n")
        return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
#  DEPTH MINI-MAP HUD
# ═══════════════════════════════════════════════════════════════════════════

def draw_hud(frame, dmap, lat_ms, sources):
    fh, fw = frame.shape[:2]

    # Depth colour map — top-right corner
    vis = cv2.applyColorMap((dmap*255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    vis = cv2.resize(vis, (150, 112))
    frame[6:118, fw-156:fw-6] = vis
    cv2.putText(frame, "Depth map", (fw-148, 130),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180,180,180), 1)

    # Bottom status bar
    src_str = " | ".join(sorted(sources)) if sources else "—"
    cv2.putText(frame,
                f"Latency {lat_ms:.0f}ms   Active: {src_str}   Q=quit Y=ok N=miss",
                (8, fh-8), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180,180,180), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(__doc__)
    print("[INFO] Loading AI models …\n")

    # ── Models ──────────────────────────────────────────────────────────
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[INFO] Torch device : {device}")

    yolo     = YOLO(CFG.YOLO_MODEL)
    midas    = torch.hub.load("intel-isl/MiDaS", "MiDaS_small")
    midas.eval().to(device)
    midas_tx = torch.hub.load("intel-isl/MiDaS", "transforms").small_transform

    # ── Components ──────────────────────────────────────────────────────
    voice    = VoiceBackend()
    of_layer = OpticalFlowLayer()
    smoother = Smoother()
    alerts   = AlertEngine(voice)
    metrics  = Metrics()

    # ── Camera ──────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(CFG.CAM_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CFG.CAM_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CFG.CAM_H)
    if not cap.isOpened():
        sys.exit("[ERROR] Cannot open camera. Check CAM_INDEX in CFG.")

    print("\n[INFO] System ready.\n"
          "  Q = quit & show metrics\n"
          "  Y = last alert was CORRECT\n"
          "  N = last alert was a FALSE ALARM\n")

    # Warm-up speak
    voice.say("Smart mobility aid is ready.")

    # ── Main loop ────────────────────────────────────────────────────────
    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        t0 = time.perf_counter()

        # ── Depth (MiDaS) ────────────────────────────────────────────────
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        inp = midas_tx(rgb).to(device)
        with torch.no_grad():
            raw = midas(inp)
            raw = torch.nn.functional.interpolate(
                raw.unsqueeze(1), size=rgb.shape[:2],
                mode="bicubic", align_corners=False
            ).squeeze()
        dmap = cv2.normalize(raw.cpu().numpy(), None, 0, 1, cv2.NORM_MINMAX)

        # ── Layer 1: YOLO ─────────────────────────────────────────────────
        l1 = run_yolo(frame, yolo, dmap)

        # ── Layer 2: Depth grid ───────────────────────────────────────────
        l2 = run_depth_grid(frame, dmap)

        # ── Layer 3: Optical flow ─────────────────────────────────────────
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        l3   = of_layer.update(gray, frame)

        # ── Merge + Deduplicate ───────────────────────────────────────────
        # If YOLO already detected an object at a position, skip depth-grid
        # hit for the same position to avoid doubling up the same obstacle.
        yolo_positions = {d.pos for d in l1 if d.dist_m <= CFG.WARNING_M}
        l2_filtered    = [d for d in l2 if d.pos not in yolo_positions]

        all_dets = l1 + l2_filtered + l3

        # ── Temporal smoother ─────────────────────────────────────────────
        smoother.update(all_dets)
        stable = smoother.confirmed()

        # ── Alert engine ──────────────────────────────────────────────────
        spoken = alerts.process(stable)

        # ── Metrics ───────────────────────────────────────────────────────
        lat_ms = (time.perf_counter() - t0) * 1000
        metrics.log_lat(lat_ms)

        # ── HUD ───────────────────────────────────────────────────────────
        sources = {d.source for d in stable}
        draw_hud(frame, dmap, lat_ms, sources)
        cv2.imshow("Smart Mobility Aid  —  Eyes for the Blind", frame)

        # ── Keyboard ─────────────────────────────────────────────────────
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        elif key == ord("y") and alerts.last_det:
            metrics.log_ok(alerts.last_det.label)
            print(f"[METRIC] ✓  {alerts.last_det.label}  {alerts.last_det.dist_m}m")
        elif key == ord("n") and alerts.last_det:
            metrics.log_fa(alerts.last_det.label)
            print(f"[METRIC] ✗  {alerts.last_det.label}  (false alarm)")

    # ── Shutdown ─────────────────────────────────────────────────────────
    cap.release()
    cv2.destroyAllWindows()
    print(metrics.report())
    print("[INFO] System stopped.")


if __name__ == "__main__":
    main()