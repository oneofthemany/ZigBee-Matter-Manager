"""
TherapyStream — server-side soundscape synth, so therapy can cast to a player.

The SPA's Web Audio graph ported to numpy and served as an endless WAV, going
through the same /api/media/play path as radio and Tidal. Generation is paced to
real time so players buffer seconds, not minutes, and each listener has
independent state. See docs/speaker_sync.md.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import random
import time
import wave
from typing import AsyncIterator, Optional

import numpy as np

logger = logging.getLogger("modules.media.therapy_stream")

RATE = 44100
BLOCK = RATE // 4          # 0.25 s of frames per synthesis block
LEAD_SECONDS = 3.0         # how far ahead of real time we allow generation

# Mode tables (synthesis fields of the SPA's MODES)
MODES = {
    "focus":      dict(label="Focus", base=220.0, scale=[0, 2, 4, 7, 9, 12, 14, 16], tempo=0.5, filter=2200, decay=1.5, pads=[220.0, 277.18, 329.63, 440.0], detune=5, bin_base=200.0, bin_beat=14.0),
    "relaxation": dict(label="Relaxation", base=174.0, scale=[0, 2, 4, 5, 7, 9, 11, 12], tempo=0.3, filter=1200, decay=3.0, pads=[174.0, 220.0, 261.0, 349.0], detune=8, bin_base=174.0, bin_beat=10.0),
    "meditation": dict(label="Meditation", base=136.1, scale=[0, 2, 3, 7, 8, 12, 14, 15], tempo=0.2, filter=800, decay=5.0, pads=[136.1, 204.15, 272.2, 408.3], detune=12, bin_base=136.0, bin_beat=7.0),
    "sleep":      dict(label="Sleep", base=110.0, scale=[0, 2, 4, 7, 9, 12], tempo=0.12, filter=500, decay=7.0, pads=[110.0, 164.81, 220.0, 329.63], detune=15, bin_base=110.0, bin_beat=3.0),
    "anxiety":    dict(label="Anxiety", base=196.0, scale=[0, 2, 4, 5, 7, 9, 11, 12], tempo=0.35, filter=1000, decay=4.0, pads=[196.0, 246.94, 293.66, 392.0], detune=10, bin_base=180.0, bin_beat=10.0, entrain=(0.5, 0.18, 600.0)),
    "breathwork": dict(label="Breathwork", base=160.0, scale=[0, 4, 7, 12, 16, 19], tempo=0.15, filter=700, decay=4.5, pads=[160.0, 213.33, 240.0, 320.0], detune=10, bin_base=160.0, bin_beat=8.0),
    "pain":       dict(label="Pain Relief", base=174.0, scale=[0, 4, 7, 12, 16, 19, 24], tempo=0.18, filter=900, decay=5.5, pads=[174.0, 218.25, 261.0, 348.0], detune=6, bin_base=174.0, bin_beat=6.0),
    "emotional":  dict(label="Emotional", base=146.83, scale=[0, 2, 3, 5, 7, 8, 10, 12], tempo=0.2, filter=1100, decay=6.0, pads=[146.83, 174.61, 220.0, 293.66], detune=10, bin_base=147.0, bin_beat=8.0),
    "energy":     dict(label="Energy", base=261.63, scale=[0, 2, 4, 5, 7, 9, 11, 12, 14, 16], tempo=0.7, filter=3000, decay=1.2, pads=[261.63, 329.63, 392.0, 523.25], detune=3, bin_base=260.0, bin_beat=18.0),
    "trauma":     dict(label="Trauma Safe", base=146.83, scale=[0, 2, 4, 7, 9, 12, 14, 16], tempo=0.14, filter=600, decay=5.0, pads=[146.83, 196.0, 220.0, 293.66], detune=8, bin_base=147.0, bin_beat=9.0, trauma_safe=True),
}

# Default speech overlays (the SPA's DEFAULT_MESSAGES; custom edits stay local)
MESSAGES = {
    "focus": ["Settle into this moment. Your attention is a powerful tool.", "Let each breath anchor you deeper into focus.", "Notice your thoughts without following them. Return to the task.", "Your mind is sharp and clear. Stay with this feeling.", "You are fully present. Everything else can wait.", "Channel your energy into what matters most right now.", "Distractions fade. Only what matters remains.", "You are building something meaningful with each focused minute.", "Clarity is your natural state. Return to it now.", "One thing at a time. That is your superpower."],
    "relaxation": ["Let the tension in your shoulders dissolve.", "Each exhale carries away what you no longer need.", "Your body knows how to rest. Trust it.", "Soften your jaw. Relax your hands. Let go.", "There is nowhere you need to be but here.", "Feel gravity supporting you. You are held.", "Warmth spreads through your body like sunlight.", "Every muscle is releasing, softening, unwinding.", "Peace is not something you find. It is something you allow.", "You deserve this moment of stillness."],
    "meditation": ["Observe your breath. In... and out.", "You are awareness itself. Vast and open.", "Let thoughts arise and pass like clouds in the sky.", "Rest in the space between your thoughts.", "Each moment is complete. Nothing is missing.", "Return gently to the breath whenever you wander.", "You are not your thoughts. You are the one watching.", "Stillness lives beneath every wave of the mind.", "In this silence, wisdom speaks.", "Be here. Just this. Nothing more is needed."],
    "sleep": ["Let your eyelids grow heavy.", "Your body is sinking into deep comfort.", "Release the day. It is complete.", "Darkness is your friend tonight.", "Drift... slowly... peacefully... into sleep.", "You are safe. You are warm. Let go.", "The world will wait for you until morning.", "Each breath draws you deeper into rest.", "Tomorrow holds its own light. For now, rest.", "Sleep wraps around you like a soft blanket."],
    "anxiety": ["You are safe in this moment. Right here. Right now.", "Notice your feet on the ground. You are anchored.", "This feeling is temporary. It will pass like a wave.", "Breathe with the rhythm. Let it slow you down gently.", "Name five things you can see. Ground yourself here.", "Your body is learning to let go. Trust the process.", "The storm inside is quieting. You are finding stillness.", "Each slow breath sends a signal of safety to your body.", "You have survived every anxious moment before this one.", "Place your hand on your chest. Feel your own steady rhythm."],
    "breathwork": ["Breathe in through your nose... slowly... deeply.", "Hold gently. Feel the fullness in your lungs.", "Now release. Let every last bit of air flow out.", "Rest in the stillness between breaths.", "Inhale calm. Exhale tension.", "Your breath is your anchor to this moment.", "Feel your belly rise with each inhale.", "Let the exhale be longer than the inhale.", "You are breathing yourself into balance.", "Each cycle brings you deeper into peace."],
    "pain": ["Bring gentle awareness to where you feel discomfort.", "Breathe warmth and light into that area.", "Your body is more than the sensation of pain.", "Imagine the tension softening like wax near a flame.", "With each breath, the sharp edges become smoother.", "You are not fighting the pain. You are flowing around it.", "Notice any part of your body that feels comfortable. Rest there.", "Sound can carry away what the body holds. Let it.", "Visualize healing light spreading slowly through your body.", "You are strong. Your body knows how to heal."],
    "emotional": ["Whatever you are feeling right now is valid.", "You do not need to fix this feeling. Just let it be here.", "Grief, sadness, anger — they all deserve space.", "Let the music hold what words cannot.", "It is brave to feel. You are being brave.", "Tears are not weakness. They are release.", "You are allowed to not be okay right now.", "This feeling will not break you. You are larger than it.", "Breathe into the emotion. Do not push it away.", "After the rain, clarity comes. Be patient with yourself."],
    "energy": ["A new energy is rising in you. Feel it building.", "Your body is waking up. Every cell is alive.", "Breathe in strength. Breathe out stagnation.", "You are capable of extraordinary things today.", "Feel the momentum building with each breath.", "Light is pouring into you. You are recharging.", "Stand tall in your power. This day is yours.", "Your mind is clear. Your body is ready. Move forward.", "Shake off the heaviness. Lightness is your natural state.", "You are unstoppable when you believe in your own energy."],
    "trauma": ["You are safe. Nothing here will harm you.", "Feel the ground beneath you. It is solid and steady.", "You are in the present moment. Not the past.", "Your body is yours. You are in control.", "Notice the temperature of the air on your skin.", "Listen to the sounds around you. You are here.", "You survived. You are still here. That matters.", "There is no rush. Move at your own pace.", "You deserve gentleness. Especially from yourself.", "Safety lives in this moment. Rest here as long as you need."],
}


class _Biquad:
    """RBJ-cookbook biquad, stateful across blocks (mono float64)."""

    def __init__(self, kind: str, freq: float, q: float = 0.707):
        w0 = 2 * math.pi * min(freq, RATE * 0.45) / RATE
        alpha = math.sin(w0) / (2 * q)
        cw = math.cos(w0)
        if kind == "lowpass":
            b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        else:  # bandpass (constant peak gain)
            b0, b1, b2 = alpha, 0.0, -alpha
        a0 = 1 + alpha
        self.b = np.array([b0, b1, b2]) / a0
        self.a = np.array([(-2 * cw) / a0, (1 - alpha) / a0])
        self.zx = np.zeros(2)
        self.zy = np.zeros(2)

    def process(self, x: np.ndarray) -> np.ndarray:
        y = np.empty_like(x)
        x1, x2 = self.zx
        y1, y2 = self.zy
        b0, b1, b2 = self.b
        a1, a2 = self.a
        for i in range(len(x)):        # 11k iterations / block — fine
            y[i] = b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, x[i]
            y2, y1 = y1, y[i]
        self.zx[:] = (x1, x2)
        self.zy[:] = (y1, y2)
        return y


class TherapyStream:
    def __init__(self, tts, mode: str, speech: bool = True,
                 voice: str = "af_heart", speech_speed: float = 0.75,
                 pitch: float = 1.0, interval: int = 45,
                 breath: str = "4-7-8"):
        self.cfg = MODES.get(mode) or MODES["relaxation"]
        self.mode = mode if mode in MODES else "relaxation"
        self.tts = tts
        self.speech = speech and tts is not None
        self.voice = voice
        self.speech_speed = speech_speed
        self.pitch = pitch
        self.interval = max(15, min(300, interval))
        self.breath = self._parse_breath(breath) if self.mode == "breathwork" else None

        c = self.cfg
        self.n = 0                                     # frames generated
        self._pad_phase = np.zeros((len(c["pads"]), 2))
        self._sub_phase = 0.0
        self._bin_phase = np.zeros(2)
        self._lowpass = _Biquad("lowpass", c["filter"], 0.4)
        self._noise_bp = _Biquad("bandpass", c["filter"] * 0.3, 0.5)
        # Feedback echo standing in for the SPA's delay-network reverb
        self._echo = np.zeros(int(RATE * 0.31))
        self._echo_fb = min(0.55, 0.25 + c["decay"] * 0.05)
        # Generative notes: (buffer, offset) pairs currently sounding
        self._notes: list = []
        self._next_note = RATE                         # first note after 1 s
        # Speech overlay
        self._speech_buf: Optional[np.ndarray] = None
        self._speech_pos = 0
        self._speech_task: Optional[asyncio.Task] = None
        self._next_speech = RATE * 3                   # first message after 3 s
        self._msg_idx = 0

    # Public: endless WAV byte stream

    async def wav_stream(self) -> AsyncIterator[bytes]:
        yield self._wav_header()
        t0 = time.monotonic()
        try:
            while True:
                block = await self._render_block()
                yield block.tobytes()
                # Pace to real time with a small lead
                ahead = self.n / RATE - (time.monotonic() - t0)
                if ahead > LEAD_SECONDS:
                    await asyncio.sleep(ahead - LEAD_SECONDS)
        finally:
            if self._speech_task:
                self._speech_task.cancel()

    @staticmethod
    def _wav_header() -> bytes:
        # Endless stream: RIFF/data sizes pinned to 0xFFFFFFFF; players read
        # until the connection closes (same trick as icecast WAV streams).
        h = io.BytesIO()
        h.write(b"RIFF")
        h.write((0xFFFFFFFF).to_bytes(4, "little"))
        h.write(b"WAVEfmt ")
        h.write((16).to_bytes(4, "little"))
        h.write((1).to_bytes(2, "little"))             # PCM
        h.write((2).to_bytes(2, "little"))             # stereo
        h.write(RATE.to_bytes(4, "little"))
        h.write((RATE * 4).to_bytes(4, "little"))      # byte rate
        h.write((4).to_bytes(2, "little"))             # block align
        h.write((16).to_bytes(2, "little"))            # bits
        h.write(b"data")
        h.write((0xFFFFFFFF).to_bytes(4, "little"))
        return h.getvalue()

    # Synthesis

    async def _render_block(self) -> np.ndarray:
        c = self.cfg
        t = (self.n + np.arange(BLOCK)) / RATE         # absolute time axis
        now_s = self.n / RATE

        # Entrainment (anxiety): beat and tempo slide down over durationSec
        beat = c["bin_beat"]
        tempo = c["tempo"]
        if "entrain" in c:
            t_start, t_end, dur = c["entrain"]
            p = min(now_s / dur, 1.0)
            beat = c["bin_beat"] + (6.0 - c["bin_beat"]) * p
            tempo = t_start + (t_end - t_start) * p

        tonal = np.zeros(BLOCK)

        # Pads: two detuned sines per note, slow LFO on gain
        pad_gain = 0.14 / len(c["pads"])
        cents = c["detune"] / 1200.0
        for i, f in enumerate(c["pads"]):
            lfo_f = 0.02 + i * 0.008 if c.get("trauma_safe") else 0.05 + i * 0.02
            lfo = 0.75 + 0.25 * np.sin(2 * np.pi * lfo_f * t)
            for j, freq in enumerate((f * 2 ** cents, f * 1.002 * 2 ** -cents)):
                step = 2 * np.pi * freq / RATE
                ph = self._pad_phase[i, j] + step * np.arange(1, BLOCK + 1)
                tonal += pad_gain * lfo * np.sin(ph)
                self._pad_phase[i, j] = ph[-1] % (2 * np.pi)

        # Sub bass
        step = 2 * np.pi * (c["base"] / 2) / RATE
        ph = self._sub_phase + step * np.arange(1, BLOCK + 1)
        tonal += 0.09 * np.sin(ph)
        self._sub_phase = ph[-1] % (2 * np.pi)

        # Generative notes (triangle, attack/decay envelope)
        if self.n >= self._next_note:
            self._notes.append([self._make_note(tempo), 0])
            spacing = (1 / max(tempo, 0.05)) * (0.7 + random.random() * 0.6)
            self._next_note = self.n + int(spacing * RATE)
        for note in self._notes:
            buf, off = note
            take = min(BLOCK, len(buf) - off)
            tonal[:take] += buf[off:off + take]
            note[1] += take
        self._notes = [n for n in self._notes if n[1] < len(n[0])]

        # Texture noise through bandpass
        noise = self._noise_bp.process(np.random.uniform(-1, 1, BLOCK)) * 0.02

        # Voicing lowpass, then feedback echo for space
        bed = self._lowpass.process(tonal) + noise
        d = len(self._echo)
        out = np.empty(BLOCK)
        for i in range(BLOCK):                          # echo needs the loop
            e = self._echo[(self.n + i) % d]
            out[i] = bed[i] + 0.3 * e
            self._echo[(self.n + i) % d] = bed[i] + self._echo_fb * e
        bed = out

        # Breathwork amplitude envelope
        if self.breath:
            bed *= self._breath_env(t)

        # Stereo: bed both sides + binaural pair (L=base, R=base+beat)
        stereo = np.empty((BLOCK, 2))
        stereo[:, 0] = bed
        stereo[:, 1] = bed
        for ch, freq in enumerate((c["bin_base"], c["bin_base"] + beat)):
            step = 2 * np.pi * freq / RATE
            ph = self._bin_phase[ch] + step * np.arange(1, BLOCK + 1)
            stereo[:, ch] += 0.10 * np.sin(ph)
            self._bin_phase[ch] = ph[-1] % (2 * np.pi)

        # Speech overlay with bed ducking
        self._schedule_speech()
        if self._speech_buf is not None:
            take = min(BLOCK, len(self._speech_buf) - self._speech_pos)
            if take > 0:
                stereo[:take] *= 0.35
                seg = self._speech_buf[self._speech_pos:self._speech_pos + take]
                stereo[:take, 0] += seg
                stereo[:take, 1] += seg
                self._speech_pos += take
            if self._speech_pos >= len(self._speech_buf):
                self._speech_buf = None

        self.n += BLOCK
        pcm = np.clip(stereo, -0.98, 0.98)
        return (pcm * 32767).astype("<i2")

    def _make_note(self, tempo: float) -> np.ndarray:
        c = self.cfg
        semi = random.choice(c["scale"])
        freq = c["base"] * 2 ** (semi / 12)
        dur = (3 + random.random() * 4) if c.get("trauma_safe") else (1.5 + random.random() * 3)
        n = int(dur * RATE)
        tt = np.arange(n) / RATE
        tri = 2 / np.pi * np.arcsin(np.sin(2 * np.pi * freq * tt))
        attack = int(n * 0.35)
        env = np.empty(n)
        env[:attack] = np.linspace(0, 1, attack)
        env[attack:] = np.exp(np.linspace(0, math.log(0.03), n - attack))
        return 0.05 * tri * env

    @staticmethod
    def _parse_breath(spec: str):
        try:
            parts = [max(0, int(p)) for p in spec.split("-")]
            inhale, hold, exhale = parts[0], parts[1] if len(parts) > 2 else 0, parts[-1] if len(parts) < 3 else parts[2]
            rest = parts[3] if len(parts) > 3 else 0
            total = inhale + hold + exhale + rest
            return (inhale, hold, exhale, rest) if total > 0 else (4, 7, 8, 0)
        except (ValueError, IndexError):
            return (4, 7, 8, 0)

    def _breath_env(self, t: np.ndarray) -> np.ndarray:
        inhale, hold, exhale, rest = self.breath
        total = inhale + hold + exhale + rest
        ct = np.mod(t, total)
        env = np.full(len(t), 0.5)
        m = ct < inhale
        env[m] = 0.5 + 0.5 * (ct[m] / max(inhale, 1e-6))
        m = (ct >= inhale) & (ct < inhale + hold)
        env[m] = 1.0
        m = (ct >= inhale + hold) & (ct < inhale + hold + exhale)
        env[m] = 1.0 - 0.5 * ((ct[m] - inhale - hold) / max(exhale, 1e-6))
        return env

    # Speech

    def _schedule_speech(self):
        if not self.speech:
            return
        if (self.n >= self._next_speech and self._speech_task is None
                and self._speech_buf is None):
            msgs = MESSAGES[self.mode]
            text = msgs[self._msg_idx % len(msgs)]
            self._msg_idx += 1
            self._next_speech = self.n + self.interval * RATE
            self._speech_task = asyncio.create_task(self._synth_speech(text))

    async def _synth_speech(self, text: str):
        try:
            wav = await self.tts.synthesize(text, voice=self.voice,
                                            speed=self.speech_speed,
                                            pitch=self.pitch)
            with wave.open(io.BytesIO(wav)) as wf:
                rate = wf.getframerate()
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
            audio = pcm.astype(np.float64) / 32768.0
            if rate != RATE:   # kokoro is 24 kHz — linear resample
                idx = np.arange(0, len(audio), rate / RATE)
                audio = np.interp(idx, np.arange(len(audio)), audio)
            self._speech_buf = audio * 0.85
            self._speech_pos = 0
        except Exception as exc:
            logger.warning("Therapy stream speech failed: %s", exc)
        finally:
            self._speech_task = None
