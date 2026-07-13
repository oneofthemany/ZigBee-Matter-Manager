import { useState, useEffect, useRef, useCallback } from "react";

// Languages covered by the Kokoro-82M voice pack
const REGIONS = [
  { code: "en-US", label: "English (US)" },
  { code: "en-GB", label: "English (UK)" },
  { code: "es-ES", label: "Spanish" },
  { code: "fr-FR", label: "French" },
  { code: "hi-IN", label: "Hindi" },
  { code: "it-IT", label: "Italian" },
  { code: "ja-JP", label: "Japanese" },
  { code: "pt-BR", label: "Portuguese (Brazil)" },
  { code: "zh-CN", label: "Chinese (Mandarin)" },
];

const DEFAULT_MESSAGES = {
  focus: ["Settle into this moment. Your attention is a powerful tool.","Let each breath anchor you deeper into focus.","Notice your thoughts without following them. Return to the task.","Your mind is sharp and clear. Stay with this feeling.","You are fully present. Everything else can wait.","Channel your energy into what matters most right now.","Distractions fade. Only what matters remains.","You are building something meaningful with each focused minute.","Clarity is your natural state. Return to it now.","One thing at a time. That is your superpower."],
  relaxation: ["Let the tension in your shoulders dissolve.","Each exhale carries away what you no longer need.","Your body knows how to rest. Trust it.","Soften your jaw. Relax your hands. Let go.","There is nowhere you need to be but here.","Feel gravity supporting you. You are held.","Warmth spreads through your body like sunlight.","Every muscle is releasing, softening, unwinding.","Peace is not something you find. It is something you allow.","You deserve this moment of stillness."],
  meditation: ["Observe your breath. In... and out.","You are awareness itself. Vast and open.","Let thoughts arise and pass like clouds in the sky.","Rest in the space between your thoughts.","Each moment is complete. Nothing is missing.","Return gently to the breath whenever you wander.","You are not your thoughts. You are the one watching.","Stillness lives beneath every wave of the mind.","In this silence, wisdom speaks.","Be here. Just this. Nothing more is needed."],
  sleep: ["Let your eyelids grow heavy.","Your body is sinking into deep comfort.","Release the day. It is complete.","Darkness is your friend tonight.","Drift... slowly... peacefully... into sleep.","You are safe. You are warm. Let go.","The world will wait for you until morning.","Each breath draws you deeper into rest.","Tomorrow holds its own light. For now, rest.","Sleep wraps around you like a soft blanket."],
  anxiety: ["You are safe in this moment. Right here. Right now.","Notice your feet on the ground. You are anchored.","This feeling is temporary. It will pass like a wave.","Breathe with the rhythm. Let it slow you down gently.","Name five things you can see. Ground yourself here.","Your body is learning to let go. Trust the process.","The storm inside is quieting. You are finding stillness.","Each slow breath sends a signal of safety to your body.","You have survived every anxious moment before this one.","Place your hand on your chest. Feel your own steady rhythm."],
  breathwork: ["Breathe in through your nose... slowly... deeply.","Hold gently. Feel the fullness in your lungs.","Now release. Let every last bit of air flow out.","Rest in the stillness between breaths.","Inhale calm. Exhale tension.","Your breath is your anchor to this moment.","Feel your belly rise with each inhale.","Let the exhale be longer than the inhale.","You are breathing yourself into balance.","Each cycle brings you deeper into peace."],
  pain: ["Bring gentle awareness to where you feel discomfort.","Breathe warmth and light into that area.","Your body is more than the sensation of pain.","Imagine the tension softening like wax near a flame.","With each breath, the sharp edges become smoother.","You are not fighting the pain. You are flowing around it.","Notice any part of your body that feels comfortable. Rest there.","Sound can carry away what the body holds. Let it.","Visualize healing light spreading slowly through your body.","You are strong. Your body knows how to heal."],
  emotional: ["Whatever you are feeling right now is valid.","You do not need to fix this feeling. Just let it be here.","Grief, sadness, anger — they all deserve space.","Let the music hold what words cannot.","It is brave to feel. You are being brave.","Tears are not weakness. They are release.","You are allowed to not be okay right now.","This feeling will not break you. You are larger than it.","Breathe into the emotion. Do not push it away.","After the rain, clarity comes. Be patient with yourself."],
  energy: ["A new energy is rising in you. Feel it building.","Your body is waking up. Every cell is alive.","Breathe in strength. Breathe out stagnation.","You are capable of extraordinary things today.","Feel the momentum building with each breath.","Light is pouring into you. You are recharging.","Stand tall in your power. This day is yours.","Your mind is clear. Your body is ready. Move forward.","Shake off the heaviness. Lightness is your natural state.","You are unstoppable when you believe in your own energy."],
  trauma: ["You are safe. Nothing here will harm you.","Feel the ground beneath you. It is solid and steady.","You are in the present moment. Not the past.","Your body is yours. You are in control.","Notice the temperature of the air on your skin.","Listen to the sounds around you. You are here.","You survived. You are still here. That matters.","There is no rush. Move at your own pace.","You deserve gentleness. Especially from yourself.","Safety lives in this moment. Rest here as long as you need."],
};

// Each mode has a "sweet spot" range that keeps speech therapeutic.
// Boundaries prevent unrealistic/harmful combinations.
const VOICE_PROFILE_DEFAULTS = {
  focus:      { speed: 0.9,  pitch: 1.0,  reverbMix: 0.15, tone: "steady" },
  relaxation: { speed: 0.75, pitch: 0.95, reverbMix: 0.25, tone: "warm" },
  meditation: { speed: 0.65, pitch: 0.9,  reverbMix: 0.35, tone: "ethereal" },
  sleep:      { speed: 0.6,  pitch: 0.85, reverbMix: 0.4,  tone: "whisper" },
  anxiety:    { speed: 0.78, pitch: 0.95, reverbMix: 0.3,  tone: "grounding" },
  breathwork: { speed: 0.7,  pitch: 0.95, reverbMix: 0.2,  tone: "rhythmic" },
  pain:       { speed: 0.7,  pitch: 0.9,  reverbMix: 0.35, tone: "soothing" },
  emotional:  { speed: 0.72, pitch: 0.95, reverbMix: 0.3,  tone: "gentle" },
  energy:     { speed: 1.0,  pitch: 1.05, reverbMix: 0.1,  tone: "uplifting" },
  trauma:     { speed: 0.65, pitch: 0.9,  reverbMix: 0.25, tone: "safe" },
};

// Boundaries keep each mode sounding therapeutic — not robotic or cartoon-like
const VOICE_PROFILE_BOUNDS = {
  focus:      { speed: [0.7, 1.1],  pitch: [0.85, 1.1],  reverbMix: [0.0, 0.35] },
  relaxation: { speed: [0.55, 0.9], pitch: [0.8, 1.05],  reverbMix: [0.1, 0.45] },
  meditation: { speed: [0.5, 0.8],  pitch: [0.75, 1.0],  reverbMix: [0.15, 0.55] },
  sleep:      { speed: [0.45, 0.75],pitch: [0.7, 0.95],  reverbMix: [0.2, 0.6]  },
  anxiety:    { speed: [0.6, 0.95], pitch: [0.8, 1.05],  reverbMix: [0.1, 0.45] },
  breathwork: { speed: [0.55, 0.85],pitch: [0.8, 1.05],  reverbMix: [0.05, 0.4] },
  pain:       { speed: [0.5, 0.85], pitch: [0.75, 1.0],  reverbMix: [0.15, 0.5] },
  emotional:  { speed: [0.55, 0.9], pitch: [0.8, 1.05],  reverbMix: [0.1, 0.5]  },
  energy:     { speed: [0.8, 1.2],  pitch: [0.9, 1.15],  reverbMix: [0.0, 0.25] },
  trauma:     { speed: [0.45, 0.8], pitch: [0.75, 1.0],  reverbMix: [0.1, 0.45] },
};

const MODES = {
  focus: { label: "Focus", icon: "◉", color: "#E8A838", bg: "linear-gradient(135deg, #1a1207 0%, #2d1f0a 40%, #1a1207 100%)", glow: "rgba(232,168,56,0.15)", baseFreq: 220, scale: [0,2,4,7,9,12,14,16], tempo: 0.5, filterFreq: 2200, filterQ: 0.7, reverbDecay: 1.5, padNotes: [220,277.18,329.63,440], padDetune: 5, binauralBase: 200, binauralBeat: 14 },
  relaxation: { label: "Relaxation", icon: "≈", color: "#5B9F6B", bg: "linear-gradient(135deg, #0a1a0d 0%, #0d2412 40%, #0a1a0d 100%)", glow: "rgba(91,159,107,0.15)", baseFreq: 174, scale: [0,2,4,5,7,9,11,12], tempo: 0.3, filterFreq: 1200, filterQ: 0.3, reverbDecay: 3, padNotes: [174,220,261,349], padDetune: 8, binauralBase: 174, binauralBeat: 10 },
  meditation: { label: "Meditation", icon: "◎", color: "#8B7EC8", bg: "linear-gradient(135deg, #100d1a 0%, #1a1428 40%, #100d1a 100%)", glow: "rgba(139,126,200,0.15)", baseFreq: 136.1, scale: [0,2,3,7,8,12,14,15], tempo: 0.2, filterFreq: 800, filterQ: 0.2, reverbDecay: 5, padNotes: [136.1,204.15,272.2,408.3], padDetune: 12, binauralBase: 136, binauralBeat: 7 },
  sleep: { label: "Sleep", icon: "☽", color: "#4A6FA5", bg: "linear-gradient(135deg, #070b14 0%, #0d1524 40%, #070b14 100%)", glow: "rgba(74,111,165,0.12)", baseFreq: 110, scale: [0,2,4,7,9,12], tempo: 0.12, filterFreq: 500, filterQ: 0.15, reverbDecay: 7, padNotes: [110,164.81,220,329.63], padDetune: 15, binauralBase: 110, binauralBeat: 3 },
  anxiety: { label: "Anxiety", icon: "⟡", color: "#C4856C", bg: "linear-gradient(135deg, #1a110d 0%, #241812 40%, #1a110d 100%)", glow: "rgba(196,133,108,0.14)", baseFreq: 196, scale: [0,2,4,5,7,9,11,12], tempo: 0.35, filterFreq: 1000, filterQ: 0.25, reverbDecay: 4, padNotes: [196,246.94,293.66,392], padDetune: 10, binauralBase: 180, binauralBeat: 10, entrainment: { startTempo: 0.5, endTempo: 0.18, durationSec: 600 } },
  breathwork: { label: "Breathwork", icon: "◠", color: "#6BAFB2", bg: "linear-gradient(135deg, #0a1616 0%, #0d2020 40%, #0a1616 100%)", glow: "rgba(107,175,178,0.14)", baseFreq: 160, scale: [0,4,7,12,16,19], tempo: 0.15, filterFreq: 700, filterQ: 0.2, reverbDecay: 4.5, padNotes: [160,213.33,240,320], padDetune: 10, binauralBase: 160, binauralBeat: 8 },
  pain: { label: "Pain Relief", icon: "✦", color: "#B8976B", bg: "linear-gradient(135deg, #141008 0%, #1e180c 40%, #141008 100%)", glow: "rgba(184,151,107,0.12)", baseFreq: 174, scale: [0,4,7,12,16,19,24], tempo: 0.18, filterFreq: 900, filterQ: 0.2, reverbDecay: 5.5, padNotes: [174,218.25,261,348], padDetune: 6, binauralBase: 174, binauralBeat: 6 },
  emotional: { label: "Emotional", icon: "◈", color: "#9B6B8A", bg: "linear-gradient(135deg, #140d12 0%, #1e141a 40%, #140d12 100%)", glow: "rgba(155,107,138,0.14)", baseFreq: 146.83, scale: [0,2,3,5,7,8,10,12], tempo: 0.2, filterFreq: 1100, filterQ: 0.3, reverbDecay: 6, padNotes: [146.83,174.61,220,293.66], padDetune: 10, binauralBase: 147, binauralBeat: 8 },
  energy: { label: "Energy", icon: "△", color: "#D4A843", bg: "linear-gradient(135deg, #1a1508 0%, #2a2010 40%, #1a1508 100%)", glow: "rgba(212,168,67,0.16)", baseFreq: 261.63, scale: [0,2,4,5,7,9,11,12,14,16], tempo: 0.7, filterFreq: 3000, filterQ: 0.8, reverbDecay: 1.2, padNotes: [261.63,329.63,392,523.25], padDetune: 3, binauralBase: 260, binauralBeat: 18 },
  trauma: { label: "Trauma Safe", icon: "⊹", color: "#7A9F8E", bg: "linear-gradient(135deg, #0b140f 0%, #101e16 40%, #0b140f 100%)", glow: "rgba(122,159,142,0.12)", baseFreq: 146.83, scale: [0,2,4,7,9,12,14,16], tempo: 0.14, filterFreq: 600, filterQ: 0.15, reverbDecay: 5, padNotes: [146.83,196,220,293.66], padDetune: 8, binauralBase: 147, binauralBeat: 9, traumaSafe: true },
};

const BREATH_PATTERNS = {
  "4-7-8":      { inhale: 4, hold: 7, exhale: 8, rest: 0, label: "4-7-8 Relaxing", desc: "Classic sleep & anxiety relief" },
  "box":        { inhale: 4, hold: 4, exhale: 4, rest: 4, label: "Box Breathing", desc: "Navy SEAL stress control" },
  "4-6":        { inhale: 4, hold: 0, exhale: 6, rest: 2, label: "4-6 Calming", desc: "Gentle parasympathetic activation" },
  "5-5":        { inhale: 5, hold: 0, exhale: 5, rest: 0, label: "5-5 Coherence", desc: "Heart rate variability training" },
  "3-3-6":      { inhale: 3, hold: 3, exhale: 6, rest: 0, label: "3-3-6 Gentle", desc: "Beginner-friendly calming" },
  "2-1-4":      { inhale: 2, hold: 1, exhale: 4, rest: 1, label: "2-1-4 Quick Calm", desc: "Fast anxiety reset" },
  "6-0-6":      { inhale: 6, hold: 0, exhale: 6, rest: 0, label: "6-6 Deep", desc: "Deep diaphragmatic breathing" },
  "4-4-8":      { inhale: 4, hold: 4, exhale: 8, rest: 0, label: "4-4-8 Extended", desc: "Extended exhale for deep calm" },
  "7-4-8":      { inhale: 7, hold: 4, exhale: 8, rest: 0, label: "7-4-8 Energising", desc: "Full lung expansion & release" },
  "4-0-8-4":    { inhale: 4, hold: 0, exhale: 8, rest: 4, label: "4-8-4 Moon", desc: "Long exhale with pause — sleep" },
  "triangle":   { inhale: 4, hold: 4, exhale: 4, rest: 0, label: "Triangle", desc: "Equal inhale-hold-exhale cycle" },
  "physiological": { inhale: 2, hold: 0, exhale: 6, rest: 2, label: "Physiological Sigh", desc: "Double inhale, long exhale — instant relief" },
};

const SESSION_DURATIONS = [
  { value: 0,    label: "∞",      desc: "Infinite" },
  { value: 300,  label: "5m",     desc: "5 minutes" },
  { value: 600,  label: "10m",    desc: "10 minutes" },
  { value: 900,  label: "15m",    desc: "15 minutes" },
  { value: 1200, label: "20m",    desc: "20 minutes" },
  { value: 1800, label: "30m",    desc: "30 minutes" },
  { value: 2700, label: "45m",    desc: "45 minutes" },
  { value: 3600, label: "1h",     desc: "1 hour" },
  { value: 5400, label: "1.5h",   desc: "1.5 hours" },
  { value: 7200, label: "2h",     desc: "2 hours" },
];

// Kokoro-82M voice ids: <lang><f|m>_<name> (af_heart = American female "Heart")
const DEFAULT_VOICES = {
  "en-US-female": "af_heart",
  "en-US-male":   "am_michael",
  "en-GB-female": "bf_emma",
  "en-GB-male":   "bm_george",
  "es-ES-female": "ef_dora",
  "es-ES-male":   "em_alex",
  "fr-FR-female": "ff_siwis",
  "fr-FR-male":   "ff_siwis",
  "hi-IN-female": "hf_alpha",
  "hi-IN-male":   "hm_omega",
  "it-IT-female": "if_sara",
  "it-IT-male":   "im_nicola",
  "ja-JP-female": "jf_alpha",
  "ja-JP-male":   "jm_kumo",
  "pt-BR-female": "pf_dora",
  "pt-BR-male":   "pm_alex",
  "zh-CN-female": "zf_xiaobei",
  "zh-CN-male":   "zm_yunjian",
  "default":      "af_heart",
};

function getDefaultVoice(region, gender, override) {
  if (override) return override;
  const key = `${region}-${gender}`;
  return DEFAULT_VOICES[key] || DEFAULT_VOICES[region.slice(0, 5) + `-${gender}`] || DEFAULT_VOICES.default;
}

function createReverb(ctx, decay) {
  // Delay-network reverb instead of convolver — predictable gain, no noise accumulation
  const input = ctx.createGain();
  const output = ctx.createGain();
  const predelay = ctx.createDelay(0.1);
  predelay.delayTime.value = 0.02;

  const delays = [0.029, 0.037, 0.053, 0.067]; // prime-spaced taps in seconds
  const feedbackLevel = Math.min(0.55, 0.3 + decay * 0.04); // longer decay = more feedback, capped

  delays.forEach(t => {
    const delay = ctx.createDelay(0.1);
    delay.delayTime.value = t;
    const fb = ctx.createGain();
    fb.gain.value = feedbackLevel;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 2000 + (1000 / decay); // darker for longer decays
    lp.Q.value = 0.1;
    const tapGain = ctx.createGain();
    tapGain.gain.value = 0.2; // each tap contributes 0.2 of its signal

    input.connect(predelay);
    predelay.connect(delay);
    delay.connect(lp);
    lp.connect(fb);
    fb.connect(delay); // feedback loop
    lp.connect(tapGain);
    tapGain.connect(output);
  });

  // Return the output node — callers connect input and read from output
  output._input = input;
  return output;
}

function createSpeechReverb(ctx, mix) {
  const input = ctx.createGain();
  const output = ctx.createGain();

  // Simple stereo delay for speech
  const delayL = ctx.createDelay(0.1);
  delayL.delayTime.value = 0.035;
  const delayR = ctx.createDelay(0.1);
  delayR.delayTime.value = 0.051;
  const fbL = ctx.createGain(); fbL.gain.value = 0.3;
  const fbR = ctx.createGain(); fbR.gain.value = 0.3;
  const lp = ctx.createBiquadFilter();
  lp.type = "lowpass"; lp.frequency.value = 3000; lp.Q.value = 0.1;

  input.connect(delayL);
  input.connect(delayR);
  delayL.connect(lp);
  lp.connect(fbL);
  fbL.connect(delayL);
  delayR.connect(fbR);
  fbR.connect(delayR);

  const wet = ctx.createGain(); wet.gain.value = mix * 0.3;
  const dry = ctx.createGain(); dry.gain.value = 1.0;

  delayL.connect(wet);
  delayR.connect(wet);
  wet.connect(output);
  dry.connect(output);

  return { input: dry, reverbInput: input, output: output };
}

// Hard clipper waveshaper — absolute safety net
function createHardLimiter(ctx) {
  const shaper = ctx.createWaveShaper();
  const samples = 8192;
  const curve = new Float32Array(samples);
  for (let i = 0; i < samples; i++) {
    const x = (i * 2) / samples - 1;
    // Soft-clip with tanh, then hard-limit at ±0.95
    curve[i] = Math.max(-0.95, Math.min(0.95, Math.tanh(x * 1.5)));
  }
  shaper.curve = curve;
  shaper.oversample = '2x';
  return shaper;
}

function BreathGuide({ playing, pattern, color }) {
  const canvasRef = useRef(null), animRef = useRef(null), startRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return;
    const ctx = canvas.getContext("2d"), dpr = window.devicePixelRatio || 1;
    canvas.width = 400 * dpr; canvas.height = 170 * dpr; ctx.scale(dpr, dpr);
    const total = pattern.inhale + pattern.hold + pattern.exhale + (pattern.rest || 0);
    const draw = (ts) => {
      if (!startRef.current) startRef.current = ts;
      const el = (ts - startRef.current) / 1000; ctx.clearRect(0, 0, 400, 170);
      let phase = "rest", pp = 0, op = 0;
      if (playing) {
        const ct = el % total;
        if (ct < pattern.inhale) { phase = "inhale"; pp = ct / pattern.inhale; }
        else if (pattern.hold > 0 && ct < pattern.inhale + pattern.hold) { phase = "hold"; pp = (ct - pattern.inhale) / pattern.hold; }
        else if (ct < pattern.inhale + (pattern.hold || 0) + pattern.exhale) { phase = "exhale"; pp = (ct - pattern.inhale - (pattern.hold || 0)) / pattern.exhale; }
        else { phase = "rest"; pp = pattern.rest > 0 ? (ct - pattern.inhale - (pattern.hold || 0) - pattern.exhale) / pattern.rest : 0; }
        op = phase === "inhale" ? pp : phase === "hold" ? 1 : phase === "exhale" ? 1 - pp : 0;
      }
      const cx = 200, cy = 85, r = 22 + 38 * op;
      for (let i = 3; i >= 0; i--) { ctx.beginPath(); ctx.arc(cx, cy, r + i * 12 + 8, 0, Math.PI * 2); ctx.fillStyle = color + Math.floor(0.03 * (4 - i) * 255).toString(16).padStart(2, "0"); ctx.fill(); }
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r); g.addColorStop(0, color + "50"); g.addColorStop(1, color + "18");
      ctx.fillStyle = g; ctx.fill(); ctx.strokeStyle = color + "80"; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.fillStyle = color; ctx.font = "500 11px 'JetBrains Mono', monospace"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.globalAlpha = playing ? 0.9 : 0.3;
      ctx.fillText(playing ? { inhale: "BREATHE IN", hold: "HOLD", exhale: "BREATHE OUT", rest: "REST" }[phase] : "READY", cx, cy); ctx.globalAlpha = 1;
      if (playing) { ctx.beginPath(); ctx.arc(cx, cy, r + 5, -Math.PI / 2, -Math.PI / 2 + pp * Math.PI * 2); ctx.strokeStyle = color + "90"; ctx.lineWidth = 2.5; ctx.lineCap = "round"; ctx.stroke(); }
      animRef.current = requestAnimationFrame(draw);
    };
    animRef.current = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(animRef.current);
  }, [playing, pattern, color]);
  return <canvas ref={canvasRef} style={{ width: "100%", maxWidth: 400, height: 170, borderRadius: 12, opacity: 0.9 }} />;
}

function NeuralVisualizer({ mode, playing }) {
  const canvasRef = useRef(null), animRef = useRef(null), timeRef = useRef(0);
  const config = MODES[mode];
  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return;
    const ctx = canvas.getContext("2d"), dpr = window.devicePixelRatio || 1;
    canvas.width = 400 * dpr; canvas.height = 170 * dpr; ctx.scale(dpr, dpr);
    const draw = () => {
      if (playing) timeRef.current += 0.008 * (config.tempo + 0.2);
      const t = timeRef.current; ctx.clearRect(0, 0, 400, 170);
      for (let l = 0; l < 4; l++) {
        ctx.beginPath(); ctx.strokeStyle = config.color + Math.floor((0.12 + l * 0.08) * 255).toString(16).padStart(2, "0"); ctx.lineWidth = 1.5 - l * 0.2;
        for (let x = 0; x <= 400; x += 2) { const n = x / 400, env = Math.sin(n * Math.PI); const y = 85 + (Math.sin(n * 4 * Math.PI + t * (1 + l * 0.3)) * (18 + l * 7) + Math.sin(n * 7 * Math.PI + t * 0.7 + l) * (8 + l * 4) + Math.sin(n * 2 * Math.PI + t * 0.3) * 12) * env * (playing ? 1 : 0.15); x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }
        ctx.stroke();
      }
      if (playing) for (let i = 0; i < 10; i++) { const px = ((i / 10) * 400 + t * 30) % 400; ctx.beginPath(); ctx.arc(px, 85 + Math.sin(px / 50 + t + i) * 35 * Math.sin(t * 0.2 + i), 1.5 + Math.sin(t + i * 2) * 0.8, 0, Math.PI * 2); ctx.fillStyle = config.color + "60"; ctx.fill(); }
      animRef.current = requestAnimationFrame(draw);
    };
    draw(); return () => cancelAnimationFrame(animRef.current);
  }, [mode, playing]);
  return <canvas ref={canvasRef} style={{ width: "100%", maxWidth: 400, height: 170, borderRadius: 12, opacity: 0.9 }} />;
}

const SS = {
  mono: { fontFamily: "'JetBrains Mono', monospace" },
  label: { fontSize: 10, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 2, opacity: 0.5, textTransform: "uppercase" },
};

export default function MusicTherapyServer() {
  const [mode, setMode] = useState("focus");
  const [playing, setPlaying] = useState(false);
  const [volume, setVolume] = useState(0.6);
  const [speechEnabled, setSpeechEnabled] = useState(true);
  const [speechVol, setSpeechVol] = useState(0.7);
  const [speechInterval, setSpeechInterval] = useState(45);
  const [currentMsg, setCurrentMsg] = useState("");
  const [elapsed, setElapsed] = useState(0);
  // Voice preferences persist across sessions (localStorage); a stored region
  // from an older build that we no longer offer falls back to en-US.
  const [voiceGender, setVoiceGender] = useState(() =>
    localStorage.getItem("zmm-tts-gender") === "male" ? "male" : "female");
  const [region, setRegion] = useState(() => {
    const saved = localStorage.getItem("zmm-tts-region");
    return REGIONS.some(r => r.code === saved) ? saved : "en-US";
  });
  const [customMessages, setCustomMessages] = useState(DEFAULT_MESSAGES);
  const [newLine, setNewLine] = useState("");
  const [tab, setTab] = useState("controls");
  const [breathPattern, setBreathPattern] = useState("4-7-8");
  const [sessionDuration, setSessionDuration] = useState(0); // 0 = infinite
  const [ttsLoading, setTtsLoading] = useState(false);
  const [piperConnected, setPiperConnected] = useState(false);
  const [piperVoices, setPiperVoices] = useState([]);
  const [piperVoiceOverride, setPiperVoiceOverride] = useState(() =>
    localStorage.getItem("zmm-tts-voice") || "");
  const [ttsSetup, setTtsSetup] = useState(null);   // /api/tts/setup/status
  const [setupBusy, setSetupBusy] = useState(false);
  const [setupMsg, setSetupMsg] = useState("");
  const setupPollRef = useRef(null);

  // Cast-to-player (server-side stream via /api/media/play, like radio).
  // The target is the media tab's selected player, pushed in by the parent
  // frame — same select-then-play flow as radio/Tidal.
  const [selPlayer, setSelPlayer] = useState(null);   // {id, name} | null
  const [castingOn, setCastingOn] = useState(null);   // player_id while casting
  const [castMsg, setCastMsg] = useState("");

  // ── Editable voice profiles (per mode) ──
  const [voiceProfiles, setVoiceProfiles] = useState(() => JSON.parse(JSON.stringify(VOICE_PROFILE_DEFAULTS)));

  const ctxRef = useRef(null), masterGain = useRef(null), nodesRef = useRef([]), modeGainRef = useRef(1);
  const speechTimer = useRef(null), elapsedTimer = useRef(null), msgIndexRef = useRef(0);
  const entrainmentRef = useRef(null), speechAudioRef = useRef(null), ttsAbortRef = useRef(null);
  const sessionTimerRef = useRef(null);

  // Refs so speak() always reads the latest values mid-session
  const voiceProfilesRef = useRef(voiceProfiles);
  const speechVolRef = useRef(speechVol);
  const modeRef = useRef(mode);
  const regionRef = useRef(region);
  const voiceGenderRef = useRef(voiceGender);
  const piperVoiceOverrideRef = useRef(piperVoiceOverride);
  useEffect(() => { voiceProfilesRef.current = voiceProfiles; }, [voiceProfiles]);
  useEffect(() => { speechVolRef.current = speechVol; }, [speechVol]);
  useEffect(() => { modeRef.current = mode; }, [mode]);
  useEffect(() => { regionRef.current = region; }, [region]);
  useEffect(() => { voiceGenderRef.current = voiceGender; }, [voiceGender]);
  useEffect(() => { piperVoiceOverrideRef.current = piperVoiceOverride; }, [piperVoiceOverride]);

  // Retain voice preferences
  useEffect(() => { localStorage.setItem("zmm-tts-region", region); }, [region]);
  useEffect(() => { localStorage.setItem("zmm-tts-gender", voiceGender); }, [voiceGender]);
  useEffect(() => { localStorage.setItem("zmm-tts-voice", piperVoiceOverride); }, [piperVoiceOverride]);

  // Check the TTS engine on mount (and again after a model download)
  const checkTts = useCallback(() => {
    fetch('/api/tts/status').then(r => r.json()).then(d => {
      setPiperConnected(d.connected);
      if (d.connected) {
        fetch('/api/tts/voices').then(r => r.json()).then(v => {
          const list = v.voices || v.downloaded || (Array.isArray(v) ? v : []);
          setPiperVoices(Array.isArray(list) ? list : Object.keys(list));
        }).catch(() => {});
      } else {
        fetch('/api/tts/setup/status').then(r => r.json()).then(setTtsSetup).catch(() => {});
      }
    }).catch(() => setPiperConnected(false));
  }, []);
  useEffect(() => { checkTts(); }, [checkTts]);
  useEffect(() => () => clearInterval(setupPollRef.current), []);

  // One-off Kokoro voice-model download (kokoro engine only), with progress
  const downloadModel = useCallback(async () => {
    setSetupBusy(true);
    setSetupMsg("Starting download…");
    try {
      const r = await fetch('/api/tts/setup/start', { method: 'POST' }).then(x => x.json());
      if (!r.success) { setSetupMsg(r.error || "Download failed"); setSetupBusy(false); return; }
      clearInterval(setupPollRef.current);
      setupPollRef.current = setInterval(async () => {
        const j = await fetch('/api/tts/setup/job').then(x => x.json()).catch(() => null);
        if (!j) return;
        setSetupMsg((j.log || []).slice(-1)[0] || j.status);
        if (j.status === "done" || j.status === "error") {
          clearInterval(setupPollRef.current);
          setSetupBusy(false);
          if (j.status === "done") { setSetupMsg(""); checkTts(); }
        }
      }, 2000);
    } catch {
      setSetupMsg("Download request failed");
      setSetupBusy(false);
    }
  }, [checkTts]);

  // ── Cast to the media tab's selected player (like radio/Tidal) ──
  useEffect(() => {
    const onMsg = (e) => {
      if (e.origin !== location.origin || !e.data) return;
      if (e.data.type === 'zmm-selected-player') {
        setSelPlayer(e.data.id ? { id: e.data.id, name: e.data.name } : null);
      }
    };
    window.addEventListener('message', onMsg);
    // Ask the parent for the current selection (we may load after it was made)
    if (window.parent !== window) {
      window.parent.postMessage({ type: 'zmm-get-selected-player' }, location.origin);
    }
    return () => window.removeEventListener('message', onMsg);
  }, []);

  const castStart = useCallback(async () => {
    if (!selPlayer) return;
    const vp = voiceProfiles[mode] || VOICE_PROFILE_DEFAULTS[mode];
    const params = new URLSearchParams({
      mode,
      speech: speechEnabled && piperConnected ? "1" : "0",
      voice: getDefaultVoice(region, voiceGender, piperVoiceOverride),
      speed: String(vp.speed),
      pitch: String(vp.pitch),
      interval: String(speechInterval),
    });
    if (mode === "breathwork") params.set("breath", breathPattern);
    setCastMsg("");
    try {
      const r = await fetch('/api/media/play', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          player_id: selPlayer.id,
          url: `${location.origin}/api/therapy/stream?${params}`,
          title: `Neural Therapy — ${MODES[mode].label}`,
          artist: "Neural Therapy",
          content_type: "audio/wav",
        }),
      }).then(x => x.json());
      if (r.success) setCastingOn(selPlayer.id);
      else setCastMsg(r.error || "Cast failed");
    } catch { setCastMsg("Cast request failed"); }
  }, [selPlayer, mode, speechEnabled, piperConnected, region, voiceGender, piperVoiceOverride, voiceProfiles, speechInterval, breathPattern]);

  const castStop = useCallback(async () => {
    if (!castingOn) return;
    try {
      await fetch('/api/media/control', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ player_id: castingOn, action: 'stop' }),
      });
    } catch {}
    setCastingOn(null);
  }, [castingOn]);

  const vp = voiceProfiles[mode] || VOICE_PROFILE_DEFAULTS[mode];
  const bounds = VOICE_PROFILE_BOUNDS[mode] || VOICE_PROFILE_BOUNDS.focus;

  const updateProfile = (key, val) => {
    setVoiceProfiles(p => ({ ...p, [mode]: { ...p[mode], [key]: val } }));
  };
  const resetProfile = () => {
    setVoiceProfiles(p => ({ ...p, [mode]: { ...VOICE_PROFILE_DEFAULTS[mode] } }));
  };

  const stopAll = useCallback(() => {
    nodesRef.current.forEach(n => { try { n.stop?.(); } catch {} try { n.disconnect?.(); } catch {} });
    nodesRef.current = [];
    clearInterval(speechTimer.current); clearInterval(elapsedTimer.current); clearInterval(entrainmentRef.current);
    clearTimeout(sessionTimerRef.current);
    if (speechAudioRef.current) { try { speechAudioRef.current.stop(); } catch {} speechAudioRef.current = null; }
    if (ttsAbortRef.current) { try { ttsAbortRef.current.abort(); } catch {} }
    setCurrentMsg("");
  }, []);

  // ── Neural TTS speak (POST /api/tts → WAV) ──
  const speak = useCallback(async (text, audioCtx, destNode) => {
    const currentMode = modeRef.current;
    const profile = voiceProfilesRef.current[currentMode] || VOICE_PROFILE_DEFAULTS[currentMode];
    const voice = getDefaultVoice(regionRef.current, voiceGenderRef.current, piperVoiceOverrideRef.current);
    const controller = new AbortController();
    ttsAbortRef.current = controller;

    try {
      setTtsLoading(true);
      const res = await fetch('/api/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice, speed: profile.speed, pitch: profile.pitch }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`TTS ${res.status}`);
      const arrayBuf = await res.arrayBuffer();
      const ctx = audioCtx || ctxRef.current;
      if (!ctx) return;

      const audioBuf = await ctx.decodeAudioData(arrayBuf);
      const source = ctx.createBufferSource();
      source.buffer = audioBuf;
      // Apply pitch via playbackRate (the engine applies speed, we shift pitch here)
      source.playbackRate.value = profile.pitch;

      const speechReverb = createSpeechReverb(ctx, profile.reverbMix);
      const speechGain = ctx.createGain();
      speechGain.gain.value = speechVolRef.current;
      source.connect(speechReverb.input);
      source.connect(speechReverb.reverbInput);
      speechReverb.output.connect(speechGain);
      speechGain.connect(destNode || ctx.destination);

      source.start();
      speechAudioRef.current = source;
      setCurrentMsg(text);
      source.onended = () => setTimeout(() => setCurrentMsg(""), 2000);
    } catch (err) {
      if (err.name !== 'AbortError') console.warn('TTS failed:', err.message);
    } finally {
      setTtsLoading(false);
    }
  }, []);

  const startAudio = useCallback(() => {
    const config = MODES[mode];
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    ctxRef.current = ctx;

    // ── Simple safety limiter — should never engage with correct gains ──
    const limiter = createHardLimiter(ctx);
    limiter.connect(ctx.destination);

    const master = ctx.createGain(); master.gain.value = volume; master.connect(limiter); masterGain.current = master;
    modeGainRef.current = 1;

    // ── Reverb bus ──
    const reverbNode = createReverb(ctx, config.reverbDecay);
    const rvG = ctx.createGain(); rvG.gain.value = 0.3; reverbNode.connect(rvG); rvG.connect(master);

    // ── Dry bus ──
    const dG = ctx.createGain(); dG.gain.value = 0.5; dG.connect(master);

    // ── Filter — Q capped low to prevent resonant peaks ──
    const filter = ctx.createBiquadFilter(); filter.type = "lowpass";
    filter.frequency.value = config.filterFreq;
    filter.Q.value = Math.min(config.filterQ, 0.4);
    filter.connect(reverbNode._input); filter.connect(dG);
    const nodes = [];

    // ── Pads: total output ~0.02 ──
    const padTotal = 0.02;
    config.padNotes.forEach((freq, i) => {
      const o1 = ctx.createOscillator(); o1.type = "sine"; o1.frequency.value = freq; o1.detune.value = config.padDetune;
      const o2 = ctx.createOscillator(); o2.type = "sine"; o2.frequency.value = freq * 1.002; o2.detune.value = -config.padDetune;
      const g = ctx.createGain(); g.gain.value = padTotal / config.padNotes.length;
      const lfo = ctx.createOscillator(); lfo.frequency.value = config.traumaSafe ? 0.02 + i * 0.008 : 0.05 + i * 0.02;
      const lg = ctx.createGain(); lg.gain.value = 0.005; // very subtle modulation
      lfo.connect(lg); lg.connect(g.gain); lfo.start(); o1.connect(g); o2.connect(g); g.connect(filter); o1.start(); o2.start(); nodes.push(o1, o2, lfo);
    });

    // ── Sub bass: 0.015 ──
    const sub = ctx.createOscillator(); sub.type = "sine"; sub.frequency.value = config.baseFreq / 2;
    const sg = ctx.createGain(); sg.gain.value = 0.015; sub.connect(sg); sg.connect(filter); sub.start(); nodes.push(sub);

    // ── Binaural: 0.02 (direct to master, not through filter) ──
    const bL = ctx.createOscillator(), bR = ctx.createOscillator();
    bL.frequency.value = config.binauralBase; bR.frequency.value = config.binauralBase + config.binauralBeat;
    const mg = ctx.createChannelMerger(2), bG = ctx.createGain(); bG.gain.value = 0.02;
    bL.connect(mg, 0, 0); bR.connect(mg, 0, 1); mg.connect(bG); bG.connect(master); bL.start(); bR.start(); nodes.push(bL, bR);

    // ── Generative notes: peak 0.01 each, max ~3 overlap = 0.03 ──
    const playNote = () => {
      if (!ctxRef.current) return;
      const c = MODES[mode]; let t = c.tempo;
      if (c.entrainment) { const p = Math.min(ctx.currentTime / c.entrainment.durationSec, 1); t = c.entrainment.startTempo + (c.entrainment.endTempo - c.entrainment.startTempo) * p; }
      const freq = c.baseFreq * Math.pow(2, c.scale[Math.floor(Math.random() * c.scale.length)] / 12);
      const osc = ctx.createOscillator(); osc.type = "triangle"; osc.frequency.value = freq;
      const ng = ctx.createGain(); const now = ctx.currentTime;
      const dur = c.traumaSafe ? 3 + Math.random() * 4 : 1.5 + Math.random() * 3;
      ng.gain.setValueAtTime(0, now);
      ng.gain.linearRampToValueAtTime(0.01, now + dur * 0.35);
      ng.gain.exponentialRampToValueAtTime(0.0003, now + dur);
      osc.connect(ng); ng.connect(filter); osc.start(now); osc.stop(now + dur);
      const spacing = (1 / t) * (0.7 + Math.random() * 0.6) * 1000;
      setTimeout(playNote, spacing);
    };
    setTimeout(playNote, 1000);

    // ── Texture noise: 0.003 ──
    const nb = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
    const nd = nb.getChannelData(0); for (let i = 0; i < nd.length; i++) nd[i] = Math.random() * 2 - 1;
    const noise = ctx.createBufferSource(); noise.buffer = nb; noise.loop = true;
    const nf = ctx.createBiquadFilter(); nf.type = "bandpass"; nf.frequency.value = config.filterFreq * 0.3; nf.Q.value = 0.5;
    const nG = ctx.createGain(); nG.gain.value = 0.003;
    noise.connect(nf); nf.connect(nG); nG.connect(reverbNode._input); noise.start(); nodes.push(noise);

    // ── GAIN BUDGET ──────────────────────────────────────
    // Pads through filter:    0.02 × (0.3 + 0.5) = 0.016
    // Sub through filter:     0.015 × 0.8         = 0.012
    // Notes (3 overlap):      0.03 × 0.8          = 0.024
    // Binaural (direct):      0.02
    // Noise through reverb:   0.003 × 0.3         = 0.001
    // ─────────────────────────────────────────────────────
    // Total at master input:                        ~0.073
    // × volume (0.6):                               ~0.044
    // Headroom to 1.0:                              ~26 dB ✓
    // ─────────────────────────────────────────────────────

    if (mode === "breathwork") {
      const bp = BREATH_PATTERNS[breathPattern] || BREATH_PATTERNS["4-7-8"];
      const total = bp.inhale + (bp.hold || 0) + bp.exhale + (bp.rest || 0);
      const breathGain = ctx.createGain(); breathGain.gain.value = 1.0;
      master.disconnect(); master.connect(breathGain); breathGain.connect(limiter);
      const sched = (t0) => { if (!ctxRef.current) return; const g = breathGain.gain; g.setValueAtTime(0.5, t0); g.linearRampToValueAtTime(1.0, t0 + bp.inhale); if (bp.hold > 0) g.setValueAtTime(1.0, t0 + bp.inhale); g.linearRampToValueAtTime(0.5, t0 + bp.inhale + (bp.hold || 0) + bp.exhale); setTimeout(() => sched(ctx.currentTime), total * 1000); };
      sched(ctx.currentTime);
    }
    if (config.entrainment) {
      const sb = config.binauralBeat, st = ctx.currentTime;
      entrainmentRef.current = setInterval(() => { if (!ctxRef.current) return; const p = Math.min((ctx.currentTime - st) / config.entrainment.durationSec, 1); bR.frequency.value = config.binauralBase + sb + (6 - sb) * p; }, 2000);
    }

    nodesRef.current = nodes;
    const msgs = customMessages[mode];
    if (speechEnabled && msgs.length && piperConnected) {
      msgIndexRef.current = 0;
      setTimeout(() => speak(msgs[0], ctx, master), 3000);
      speechTimer.current = setInterval(() => { msgIndexRef.current = (msgIndexRef.current + 1) % msgs.length; speak(msgs[msgIndexRef.current], ctx, master); }, speechInterval * 1000);
    }
    setElapsed(0); elapsedTimer.current = setInterval(() => setElapsed(e => e + 1), 1000);

    // Session timer auto-stop with graceful fade-out
    if (sessionDuration > 0) {
      const fadeTime = Math.min(10, sessionDuration * 0.05); // 5% of session, max 10s
      const fadeStart = (sessionDuration - fadeTime) * 1000;
      sessionTimerRef.current = setTimeout(() => {
        // Fade master volume to 0 over fadeTime seconds
        if (masterGain.current && ctxRef.current) {
          const now = ctxRef.current.currentTime;
          masterGain.current.gain.setValueAtTime(masterGain.current.gain.value, now);
          masterGain.current.gain.linearRampToValueAtTime(0, now + fadeTime);
        }
        // Then stop after fade completes
        setTimeout(() => {
          stopAll(); ctxRef.current?.close(); ctxRef.current = null; setPlaying(false);
        }, fadeTime * 1000);
      }, fadeStart);
    }
  }, [mode, volume, speechEnabled, speechInterval, speak, customMessages, breathPattern, piperConnected, sessionDuration, stopAll]);

  const togglePlay = () => {
    if (playing) { stopAll(); ctxRef.current?.close(); ctxRef.current = null; setPlaying(false); }
    else { startAudio(); setPlaying(true); }
  };
  useEffect(() => { if (masterGain.current) masterGain.current.gain.value = volume; }, [volume]);
  useEffect(() => () => { stopAll(); ctxRef.current?.close(); }, [stopAll]);

  const switchMode = (m) => {
    if (playing) { stopAll(); ctxRef.current?.close(); ctxRef.current = null; setPlaying(false); }
    setMode(m);
    if (tab === "breath" && m !== "breathwork") setTab("controls");
  };
  const addMessage = () => { if (!newLine.trim()) return; setCustomMessages(p => ({ ...p, [mode]: [...p[mode], newLine.trim()] })); setNewLine(""); };
  const removeMessage = (idx) => setCustomMessages(p => ({ ...p, [mode]: p[mode].filter((_, i) => i !== idx) }));
  const resetMessages = () => setCustomMessages(p => ({ ...p, [mode]: [...DEFAULT_MESSAGES[mode]] }));

  const testVoice = () => {
    let ctx = ctxRef.current;
    if (!ctx) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      ctxRef.current = ctx;
    }
    speak(customMessages[mode]?.[0] || "Testing voice. This is your selected voice and language.", ctx, null);
  };
  const fmt = (s) => {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${sec.toString().padStart(2, "0")}`;
    return `${m.toString().padStart(2, "0")}:${sec.toString().padStart(2, "0")}`;
  };
  const remaining = sessionDuration > 0 ? Math.max(0, sessionDuration - elapsed) : null;
  const config = MODES[mode];
  const btnStyle = (active) => ({
    padding: "6px 14px", borderRadius: 8, border: `1px solid ${active ? config.color : "rgba(255,255,255,0.08)"}`,
    background: active ? config.color + "20" : "rgba(255,255,255,0.02)",
    color: active ? config.color : "rgba(255,255,255,0.35)", fontSize: 10, ...SS.mono, letterSpacing: 1,
    cursor: "pointer", transition: "all 0.3s", textTransform: "uppercase",
  });
  const currentBreathPattern = BREATH_PATTERNS[breathPattern] || BREATH_PATTERNS["4-7-8"];
  const row1 = Object.entries(MODES).slice(0, 5), row2 = Object.entries(MODES).slice(5);

  // Check if profile has been modified from default
  const dflt = VOICE_PROFILE_DEFAULTS[mode];
  const isModified = vp.speed !== dflt.speed || vp.pitch !== dflt.pitch || vp.reverbMix !== dflt.reverbMix;

  // Voice-model download prompt (shown wherever the engine is reported offline)
  const ttsSetupUi = !piperConnected && (
    <div style={{ marginTop: 8 }}>
      {ttsSetup?.installable && !setupBusy && (
        <button onClick={downloadModel} style={{ ...btnStyle(true), width: "100%", padding: "8px 0", textAlign: "center" }}>
          ⬇ Download voice model ({ttsSetup.download_mb || 340} MB, one-off)
        </button>
      )}
      {ttsSetup && ttsSetup.installable === false && ttsSetup.hint && (
        <div style={{ ...SS.mono, fontSize: 8, opacity: 0.45 }}>{ttsSetup.hint}</div>
      )}
      {setupMsg && <div style={{ marginTop: 6, ...SS.mono, fontSize: 8, opacity: 0.55, textAlign: "center", animation: setupBusy ? "pulse 1.5s infinite" : "none" }}>{setupMsg}</div>}
    </div>
  );

  return (
    <div style={{ minHeight: "100vh", background: config.bg, color: "#e0ddd5", fontFamily: "'Cormorant Garamond','Georgia',serif", transition: "background 1.5s ease", display: "flex", flexDirection: "column", alignItems: "center", padding: "28px 16px" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        input[type=range]{-webkit-appearance:none;width:100%;height:4px;border-radius:2px;background:rgba(255,255,255,0.1);outline:none;cursor:pointer}
        input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:${config.color};border:none;box-shadow:0 0 8px ${config.color}40}
        select{-webkit-appearance:none;appearance:none}
        textarea:focus,input:focus,select:focus{outline:1px solid ${config.color}50}
        @keyframes breathe{0%,100%{opacity:.5}50%{opacity:1}}
        @keyframes fadeMsg{0%{opacity:0;transform:translateY(6px)}15%{opacity:1;transform:translateY(0)}85%{opacity:1}100%{opacity:0}}
        @keyframes pulse{0%,100%{opacity:.4}50%{opacity:.8}}
        .st::-webkit-scrollbar{width:4px}.st::-webkit-scrollbar-track{background:transparent}.st::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:2px}
      `}</style>

      <div style={{ textAlign: "center", marginBottom: 20 }}>
        <h1 style={{ fontSize: 26, fontWeight: 300, letterSpacing: 6, textTransform: "uppercase", color: config.color, transition: "color 1s", marginBottom: 3 }}>Neural Therapy</h1>
        <p style={{ fontSize: 10, ...SS.mono, letterSpacing: 3, opacity: 0.4 }}>STREAMING · {playing ? "ACTIVE" : "IDLE"}{piperConnected ? "" : " · TTS OFFLINE"}</p>
      </div>

      {[row1, row2].map((row, ri) => (
        <div key={ri} style={{ display: "flex", gap: 5, marginBottom: ri === 0 ? 6 : 20, flexWrap: "wrap", justifyContent: "center", maxWidth: 480 }}>
          {row.map(([k, m]) => (
            <button key={k} onClick={() => switchMode(k)} style={{ padding: "7px 11px", borderRadius: 20, border: `1px solid ${mode === k ? m.color : "rgba(255,255,255,0.08)"}`, background: mode === k ? m.color + "18" : "rgba(255,255,255,0.02)", color: mode === k ? m.color : "rgba(255,255,255,0.4)", fontSize: 11, fontFamily: "'Cormorant Garamond',serif", fontWeight: 500, letterSpacing: 1.5, cursor: "pointer", transition: "all 0.4s", textTransform: "uppercase", whiteSpace: "nowrap" }}>
              <span style={{ marginRight: 4 }}>{m.icon}</span>{m.label}
            </button>
          ))}
        </div>
      ))}

      <div style={{ background: "rgba(0,0,0,0.3)", borderRadius: 14, padding: 14, marginBottom: 18, border: `1px solid ${config.color}12`, boxShadow: `0 0 40px ${config.glow}`, width: "100%", maxWidth: 432, display: "flex", flexDirection: "column", alignItems: "center" }}>
        {mode === "breathwork" ? <BreathGuide playing={playing} pattern={currentBreathPattern} color={config.color} /> : <NeuralVisualizer mode={mode} playing={playing} />}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8, width: "100%" }}>
          <span style={{ ...SS.mono, fontSize: 10, opacity: 0.5 }}>{fmt(elapsed)}</span>
          <div style={{ flex: 1, textAlign: "center" }}>
            {mode === "breathwork" ? <span style={{ fontSize: 10, letterSpacing: 2, opacity: 0.35, textTransform: "uppercase" }}>{currentBreathPattern.inhale}-{currentBreathPattern.hold || 0}-{currentBreathPattern.exhale}{currentBreathPattern.rest > 0 ? `-${currentBreathPattern.rest}` : ""} · {config.binauralBeat}Hz</span>
            : mode === "anxiety" ? <span style={{ fontSize: 10, letterSpacing: 2, opacity: 0.35, textTransform: "uppercase" }}>Entrainment · {config.binauralBeat}→6Hz · {config.baseFreq}Hz</span>
            : <span style={{ fontSize: 10, letterSpacing: 2, opacity: 0.35, textTransform: "uppercase" }}>{config.binauralBeat}Hz Binaural · {config.baseFreq}Hz Root</span>}
          </div>
          <span style={{ ...SS.mono, fontSize: 10, opacity: remaining !== null ? 0.5 : 0.25 }}>{remaining !== null ? `-${fmt(remaining)}` : "∞"}</span>
        </div>
        {/* Session progress bar */}
        {sessionDuration > 0 && playing && (
          <div style={{ width: "100%", height: 3, borderRadius: 2, background: "rgba(255,255,255,0.06)", marginTop: 6, overflow: "hidden" }}>
            <div style={{ height: "100%", borderRadius: 2, background: config.color + "60", transition: "width 1s linear", width: `${Math.min(100, (elapsed / sessionDuration) * 100)}%` }} />
          </div>
        )}
      </div>

      <button onClick={togglePlay} style={{ width: 64, height: 64, borderRadius: "50%", border: `2px solid ${config.color}`, background: playing ? config.color + "20" : "transparent", color: config.color, fontSize: 22, cursor: "pointer", marginBottom: 16, transition: "all 0.4s", boxShadow: playing ? `0 0 30px ${config.color}30` : "none", display: "flex", alignItems: "center", justifyContent: "center", animation: playing ? "breathe 4s ease-in-out infinite" : "none" }}>{playing ? "⏸" : "▶"}</button>

      {currentMsg && <div key={currentMsg} style={{ textAlign: "center", fontSize: 15, fontWeight: 300, fontStyle: "italic", color: config.color, opacity: 0.8, marginBottom: 14, maxWidth: 340, lineHeight: 1.6, animation: "fadeMsg 8s ease forwards" }}>
        "{currentMsg}"
        {ttsLoading && <span style={{ display: "block", ...SS.mono, fontSize: 8, opacity: 0.4, marginTop: 4, animation: "pulse 1.5s infinite" }}>synthesizing...</span>}
      </div>}

      {mode === "anxiety" && <div style={{ marginBottom: 12, padding: "8px 14px", borderRadius: 8, background: config.color + "10", border: `1px solid ${config.color}20`, maxWidth: 420, width: "100%", textAlign: "center" }}><span style={{ ...SS.mono, fontSize: 9, color: config.color, opacity: 0.7 }}>ENTRAINMENT — tempo and binaural frequency gradually slow over 10 minutes</span></div>}
      {mode === "trauma" && <div style={{ marginBottom: 12, padding: "8px 14px", borderRadius: 8, background: config.color + "10", border: `1px solid ${config.color}20`, maxWidth: 420, width: "100%", textAlign: "center" }}><span style={{ ...SS.mono, fontSize: 9, color: config.color, opacity: 0.7 }}>TRAUMA-SAFE — pentatonic scale, no sudden changes, gentle dynamics</span></div>}

      <div style={{ display: "flex", gap: 4, marginBottom: 10, width: "100%", maxWidth: 420 }}>
        {[["controls","Audio"],["speech","Voice"],["editor","Messages"], ...(mode === "breathwork" ? [["breath","Breath"]] : [])].map(([k,l]) => (
          <button key={k} onClick={() => setTab(k)} style={{ flex: 1, padding: "8px 0", borderRadius: 8, border: "none", background: tab === k ? config.color + "18" : "rgba(255,255,255,0.03)", color: tab === k ? config.color : "rgba(255,255,255,0.3)", fontSize: 10, ...SS.mono, letterSpacing: 1.5, cursor: "pointer", textTransform: "uppercase", borderBottom: tab === k ? `2px solid ${config.color}` : "2px solid transparent", transition: "all 0.3s" }}>{l}</button>
        ))}
      </div>

      <div style={{ width: "100%", maxWidth: 420, background: "rgba(0,0,0,0.25)", borderRadius: 14, padding: 18, border: "1px solid rgba(255,255,255,0.04)", minHeight: 180 }}>

        {tab === "controls" && <>
          {/* Play on the media tab's selected player (same flow as radio/Tidal) */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
              <span style={SS.label}>PLAY ON SPEAKER</span>
              {castingOn && <span style={{ ...SS.mono, fontSize: 9, color: config.color, animation: "pulse 1.5s infinite" }}>● CASTING</span>}
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "stretch" }}>
              <div style={{ flex: 1, minWidth: 0, padding: "8px 12px", borderRadius: 8, background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", ...SS.mono, fontSize: 10, opacity: selPlayer ? 0.85 : 0.4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {selPlayer ? `→ ${selPlayer.name}` : "select a player in the Players list"}
              </div>
              {!castingOn
                ? <button onClick={castStart} disabled={!selPlayer} style={{ ...btnStyle(!!selPlayer), padding: "8px 14px", opacity: selPlayer ? 1 : 0.4 }}>▶ Play</button>
                : <button onClick={castStop} style={{ ...btnStyle(true), padding: "8px 14px", borderColor: "rgba(255,100,100,0.4)", color: "rgba(255,100,100,0.7)", background: "rgba(255,100,100,0.08)" }}>⏹ Stop</button>}
            </div>
            {castMsg && <div style={{ marginTop: 6, ...SS.mono, fontSize: 8, color: "#e87838" }}>{castMsg}</div>}
            {castingOn && <div style={{ marginTop: 6, ...SS.mono, fontSize: 8, opacity: 0.4 }}>
              Streaming {config.label} to the player — press Play again after changing mode or voice settings.
              Binaural beats need headphones; on speakers you still get the ambient bed and voice.
            </div>}
          </div>

          {/* Session Timer */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
              <span style={SS.label}>SESSION TIMER</span>
              <span style={{ ...SS.label, opacity: 0.4 }}>{sessionDuration === 0 ? "Infinite" : SESSION_DURATIONS.find(d => d.value === sessionDuration)?.desc || fmt(sessionDuration)}</span>
            </div>
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
              {SESSION_DURATIONS.map(d => (
                <button key={d.value} onClick={() => setSessionDuration(d.value)} style={{
                  padding: "5px 0", flex: "1 1 auto", minWidth: 36, borderRadius: 6,
                  border: `1px solid ${sessionDuration === d.value ? config.color : "rgba(255,255,255,0.06)"}`,
                  background: sessionDuration === d.value ? config.color + "20" : "rgba(255,255,255,0.02)",
                  color: sessionDuration === d.value ? config.color : "rgba(255,255,255,0.35)",
                  fontSize: 10, ...SS.mono, cursor: "pointer", transition: "all 0.3s",
                }}>{d.label}</button>
              ))}
            </div>
          </div>
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}><span style={SS.label}>VOLUME</span><span style={{ ...SS.label, opacity: 0.4 }}>{Math.round(volume * 100)}%</span></div>
            <input type="range" min={0} max={1} step={0.01} value={volume} onChange={e => setVolume(+e.target.value)} />
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={SS.label}>SPEECH OVERLAY</span>
            <button onClick={() => { setSpeechEnabled(!speechEnabled); if (speechEnabled) { clearInterval(speechTimer.current); setCurrentMsg(""); } }} style={btnStyle(speechEnabled)}>{speechEnabled ? "ON" : "OFF"}</button>
          </div>
          {speechEnabled && <div style={{ marginTop: 14 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}><span style={SS.label}>SPEECH VOLUME</span><span style={{ ...SS.label, opacity: 0.4 }}>{Math.round(speechVol * 100)}%</span></div>
            <input type="range" min={0} max={1} step={0.01} value={speechVol} onChange={e => setSpeechVol(+e.target.value)} />
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6, marginTop: 14 }}><span style={SS.label}>INTERVAL</span><span style={{ ...SS.label, opacity: 0.4 }}>{speechInterval}s</span></div>
            <input type="range" min={15} max={120} step={5} value={speechInterval} onChange={e => setSpeechInterval(+e.target.value)} />
          </div>}
          {!piperConnected && speechEnabled && <div style={{ marginTop: 12, padding: "8px 12px", borderRadius: 8, background: "rgba(255,100,100,0.08)", border: "1px solid rgba(255,100,100,0.15)" }}>
            <span style={{ ...SS.mono, fontSize: 9, color: "#e87838" }}>Neural TTS not ready — download the voice model to enable speech</span>
            {ttsSetupUi}
          </div>}
        </>}

        {tab === "speech" && <>
          {/* Engine status */}
          <div style={{ marginBottom: 14, padding: "6px 10px", borderRadius: 6, background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.05)" }}>
            <span style={{ ...SS.mono, fontSize: 9, opacity: 0.5 }}>
              {piperConnected ? <><span style={{ color: "#5B9F6B" }}>●</span> Neural TTS ready (Kokoro-82M)</> : <><span style={{ color: "#e87838" }}>●</span> Neural TTS not ready — download the voice model</>}
            </span>
            {ttsSetupUi}
          </div>

          {/* Default language */}
          <div style={{ marginBottom: 14 }}>
            <span style={{ ...SS.label, display: "block", marginBottom: 8 }}>DEFAULT LANGUAGE</span>
            <select value={region} onChange={e => { setRegion(e.target.value); setPiperVoiceOverride(""); }} style={{ width: "100%", padding: "10px 12px", borderRadius: 8, background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", color: "#e0ddd5", ...SS.mono, fontSize: 11, cursor: "pointer" }}>
              {REGIONS.map(r => <option key={r.code} value={r.code} style={{ background: "#1a1a1a" }}>{r.label}</option>)}
            </select>
          </div>

          {/* Gender */}
          <div style={{ marginBottom: 14 }}>
            <span style={{ ...SS.label, display: "block", marginBottom: 8 }}>VOICE GENDER</span>
            <div style={{ display: "flex", gap: 6 }}>
              {["female","male"].map(g => (
                <button key={g} onClick={() => { setVoiceGender(g); setPiperVoiceOverride(""); }} style={{ ...btnStyle(voiceGender === g), flex: 1, padding: "10px 0" }}>
                  {g === "female" ? "♀ Female" : "♂ Male"}
                </button>
              ))}
            </div>
          </div>

          {/* Voice picker — filtered to the chosen language + gender (falls
              back to any gender when the language has no match, e.g. French) */}
          {piperVoices.length > 0 && (() => {
            const catalog = piperVoices.filter(v => typeof v !== 'string');
            const inLang = catalog.filter(v => v.lang === region);
            const matched = inLang.filter(v => v.gender === voiceGender);
            const shown = matched.length ? matched : inLang.length ? inLang : catalog;
            const relaxed = !matched.length && inLang.length > 0;
            return <div style={{ marginBottom: 14 }}>
              <span style={{ ...SS.label, display: "block", marginBottom: 8 }}>
                VOICE — {REGIONS.find(r => r.code === region)?.label || region} · {voiceGender}
              </span>
              <select value={piperVoiceOverride} onChange={e => setPiperVoiceOverride(e.target.value)} style={{ width: "100%", padding: "10px 12px", borderRadius: 8, background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", color: "#e0ddd5", ...SS.mono, fontSize: 10, cursor: "pointer" }}>
                <option value="" style={{ background: "#1a1a1a" }}>Auto ({getDefaultVoice(region, voiceGender, "")})</option>
                {shown.map((v, i) => <option key={i} value={v.id} style={{ background: "#1a1a1a" }}>{v.label || v.id}</option>)}
              </select>
              {relaxed && <div style={{ ...SS.mono, fontSize: 8, opacity: 0.4, marginTop: 4 }}>No {voiceGender} voice for this language — showing all genders</div>}
            </div>;
          })()}

          {/* ── Per-mode voice profile sliders ── */}
          <div style={{ marginBottom: 10, padding: "10px 12px", borderRadius: 10, background: config.color + "08", border: `1px solid ${config.color}12` }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <span style={{ ...SS.label, opacity: 0.7 }}>{config.label.toUpperCase()} VOICE PROFILE — {vp.tone.toUpperCase()}</span>
              {isModified && <button onClick={resetProfile} style={{ ...btnStyle(false), fontSize: 8, padding: "3px 8px" }}>Reset</button>}
            </div>

            {/* Speed */}
            <div style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.5 }}>SPEED</span>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.4 }}>{vp.speed.toFixed(2)}x</span>
              </div>
              <input type="range" min={bounds.speed[0]} max={bounds.speed[1]} step={0.01} value={vp.speed} onChange={e => updateProfile("speed", +e.target.value)} />
              <div style={{ display: "flex", justifyContent: "space-between", ...SS.mono, fontSize: 8, opacity: 0.25, marginTop: 2 }}>
                <span>{bounds.speed[0]}x</span><span>{bounds.speed[1]}x</span>
              </div>
            </div>

            {/* Pitch */}
            <div style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.5 }}>PITCH</span>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.4 }}>{vp.pitch.toFixed(2)}</span>
              </div>
              <input type="range" min={bounds.pitch[0]} max={bounds.pitch[1]} step={0.01} value={vp.pitch} onChange={e => updateProfile("pitch", +e.target.value)} />
              <div style={{ display: "flex", justifyContent: "space-between", ...SS.mono, fontSize: 8, opacity: 0.25, marginTop: 2 }}>
                <span>{bounds.pitch[0]}</span><span>{bounds.pitch[1]}</span>
              </div>
            </div>

            {/* Reverb */}
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.5 }}>REVERB</span>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.4 }}>{Math.round(vp.reverbMix * 100)}%</span>
              </div>
              <input type="range" min={bounds.reverbMix[0]} max={bounds.reverbMix[1]} step={0.01} value={vp.reverbMix} onChange={e => updateProfile("reverbMix", +e.target.value)} />
              <div style={{ display: "flex", justifyContent: "space-between", ...SS.mono, fontSize: 8, opacity: 0.25, marginTop: 2 }}>
                <span>{Math.round(bounds.reverbMix[0] * 100)}%</span><span>{Math.round(bounds.reverbMix[1] * 100)}%</span>
              </div>
            </div>
          </div>

          {/* Active voice display */}
          <div style={{ marginBottom: 14 }}>
            <span style={{ ...SS.label, display: "block", marginBottom: 6 }}>ACTIVE VOICE</span>
            <div style={{ padding: "8px 12px", borderRadius: 8, background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)", ...SS.mono, fontSize: 10, color: piperConnected ? config.color : "#e87838", opacity: 0.8 }}>
              {piperConnected ? `${piperVoiceOverride || getDefaultVoice(region, voiceGender, "")}` : "TTS offline"}
            </div>
          </div>

          <button onClick={testVoice} disabled={!piperConnected} style={{ ...btnStyle(piperConnected), width: "100%", padding: "10px 0", textAlign: "center", opacity: piperConnected ? 1 : 0.4 }}>
            {ttsLoading ? "Synthesizing..." : "▶ Test Voice"}
          </button>
        </>}

        {tab === "editor" && <>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={SS.label}>{config.label.toUpperCase()} MESSAGES ({customMessages[mode].length})</span>
            <button onClick={resetMessages} style={{ ...btnStyle(false), fontSize: 9, padding: "4px 10px" }}>Reset</button>
          </div>
          <div className="st" style={{ maxHeight: 170, overflowY: "auto", marginBottom: 10, display: "flex", flexDirection: "column", gap: 3 }}>
            {customMessages[mode].map((msg, i) => (
              <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 6, padding: "5px 7px", borderRadius: 6, background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.04)" }}>
                <span style={{ ...SS.mono, fontSize: 9, opacity: 0.3, marginTop: 2, flexShrink: 0 }}>{i + 1}</span>
                <span style={{ flex: 1, fontSize: 12, lineHeight: 1.5, opacity: 0.7 }}>{msg}</span>
                <button onClick={() => removeMessage(i)} style={{ background: "none", border: "none", color: "rgba(255,100,100,0.5)", cursor: "pointer", fontSize: 14, padding: "0 3px", flexShrink: 0 }}>×</button>
              </div>
            ))}
            {!customMessages[mode].length && <div style={{ textAlign: "center", padding: 14, opacity: 0.3, fontSize: 12 }}>No messages. Add below or reset.</div>}
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <input value={newLine} onChange={e => setNewLine(e.target.value)} onKeyDown={e => e.key === "Enter" && addMessage()} placeholder="Type a new message..." style={{ flex: 1, padding: "8px 12px", borderRadius: 8, background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", color: "#e0ddd5", fontSize: 12, fontFamily: "'Cormorant Garamond',serif" }} />
            <button onClick={addMessage} style={{ ...btnStyle(true), padding: "8px 14px" }}>Add</button>
          </div>
        </>}

        {tab === "breath" && mode === "breathwork" && <>
          <div style={{ marginBottom: 14 }}>
            <span style={{ ...SS.label, display: "block", marginBottom: 10 }}>BREATHING PATTERN ({Object.keys(BREATH_PATTERNS).length} patterns)</span>
            <div className="st" style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 320, overflowY: "auto" }}>
              {Object.entries(BREATH_PATTERNS).map(([key, bp]) => (
                <button key={key} onClick={() => { setBreathPattern(key); if (playing) { stopAll(); ctxRef.current?.close(); ctxRef.current = null; setPlaying(false); } }} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 14px", borderRadius: 10, border: `1px solid ${breathPattern === key ? config.color : "rgba(255,255,255,0.06)"}`, background: breathPattern === key ? config.color + "15" : "rgba(255,255,255,0.02)", color: breathPattern === key ? config.color : "rgba(255,255,255,0.5)", cursor: "pointer", transition: "all 0.3s", textAlign: "left" }}>
                  <div>
                    <div style={{ fontSize: 13, fontFamily: "'Cormorant Garamond',serif", fontWeight: 500, letterSpacing: 1 }}>{bp.label}</div>
                    <div style={{ ...SS.mono, fontSize: 8, opacity: 0.4, marginTop: 2 }}>{bp.desc}</div>
                    <div style={{ ...SS.mono, fontSize: 9, opacity: 0.5, marginTop: 3 }}>IN {bp.inhale}s{bp.hold > 0 ? ` · HOLD ${bp.hold}s` : ""} · OUT {bp.exhale}s{bp.rest > 0 ? ` · REST ${bp.rest}s` : ""}</div>
                  </div>
                  <div style={{ ...SS.mono, fontSize: 10, opacity: 0.4, flexShrink: 0, marginLeft: 10 }}>{bp.inhale + (bp.hold || 0) + bp.exhale + (bp.rest || 0)}s</div>
                </button>
              ))}
            </div>
          </div>
        </>}

      </div>

      <div style={{ marginTop: 18, textAlign: "center", opacity: 0.25, fontSize: 9, ...SS.mono, letterSpacing: 2, lineHeight: 2 }}>
        USE HEADPHONES FOR BINAURAL BEATS<br/>KOKORO NEURAL TTS · PROCEDURAL AUDIO · WEB AUDIO API
      </div>
    </div>
  );
}
