# OpenZone

## Source-Side Clock Discipline for Multi-Room Audio Synchronisation Across Closed Playback Ecosystems

**Target accuracy:** ±2 ms between any two devices in a group
**Scope:** Google Cast, AirPlay/RAOP, Sonos/UPnP, and generic HTTP renderers, without the use of any vendor grouping mechanism

---

### Abstract

Commercial multi-room audio systems achieve millisecond-scale playback synchronisation through a closed control loop terminating inside device firmware: a shared network timebase, presentation timestamps, and an asynchronous sample-rate converter that slews the local DAC by parts per million to hold lock. Because no vendor exposes this loop, playback groups cannot span ecosystems. OpenZone relocates the entire control system to the audio source. Endpoints are modelled as fixed-latency, free-running playback devices; all timing authority, measurement, and correction is applied server-side by synthesising an individually delayed and rate-adjusted stream for each device. Two complementary sensors close the loop: the device's own reported playback position, a noisy but continuously available signal, and acoustic time-of-arrival measurement using a microphone and matched-filter cross-correlation, which observes the only quantity that ultimately matters — the sound in the room. This document specifies the system model, the per-device signal path, the correction policy and drift estimator, the acoustic measurement subsystem, and the error budget, together with empirical results from a two-device Google Cast deployment.

---

### 1. Problem Statement

Vendor multi-room implementations (Google multizone, AirPlay 2, Sonos) share a three-part architecture: a **shared timebase** distributed over the local network, **presentation timestamps** attached to the audio, and **endpoint-side clock discipline** — an ASRC in firmware slewing the DAC rate a few ppm to track the reference. The third element is the barrier to interoperability: it runs inside closed firmware and is not exposed by any vendor. Consequently, synchronised groups cannot be formed across ecosystems through the devices themselves.

OpenZone inverts the placement of the control system. The endpoints are treated as dumb, fixed-latency, free-running playback pipes; all timing authority, measurement, and correction resides in a server under the operator's control.

A direct design consequence follows: because the endpoints cannot be disciplined, each must receive an **individually corrected stream** — individually delayed for static alignment and individually rate-adjusted for drift cancellation. One master timeline is rendered as N tailored renditions.

### 2. Reference Architecture in Proprietary Systems

Google multizone serves as the canonical example. mDNS (`_googlecast._tcp`) provides discovery only. On group playback, one device is elected leader; it alone fetches and decodes the media. Followers receive timestamped audio from the leader over a private channel (coordination namespace `urn:x-cast:com.google.cast.multizone`, port 8009). Leader and followers run a continuous NTP-style exchange over the LAN, giving each follower an estimate of the leader's clock offset and rate to tens of microseconds. Audio carries presentation timestamps in leader time; each follower converts to local time and schedules samples into its DAC while a firmware ASRC loop slews playback rate to hold lock.

OpenZone maps each function onto a source-side equivalent:

| Function | Google multizone | OpenZone |
|---|---|---|
| Timeline authority | Leader device clock | Server master PCM position |
| Clock/rate measurement | In-band NTP-style exchange, µs-grade, continuous | Reported playback position (~10 ms grade, continuous) and acoustic cross-correlation (~0.1 ms grade, on demand) |
| Correction actuator | ASRC in follower firmware | Per-device variable resampler at the source |
| Static alignment | Implicit in PTS scheduling | Per-device sample-accurate delay plus measured offset |
| Transport | Private leader→follower stream | Per-device native protocol; each endpoint pulls its own URL |

The control-theoretic structure is identical; only the placement of the loop and the choice of sensor differ.

### 3. System Model

The server defines a master timeline `T`, measured in samples at a master rate `Fs`. Every device `i` is modelled as playing the master timeline through an affine clock error:

```
audible_i(t_wall) = master( (t_wall − θ_i) · (1 + ε_i) )

θ_i : total static latency of device i (network, buffer, decode, DAC), seconds
ε_i : fractional rate error of device i's clock, dimensionless (±20–50 ppm)
```

The synchronisation error between devices i and j at wall time t is `Δ_ij(t) = (θ_i − θ_j) + (ε_i − ε_j)·t` — a constant offset plus linear drift. The server cancels it by pre-transforming each device's feed with the inverse: delaying device i's stream by `(θ_max − θ_i)` and time-stretching it by `−ε_i` relative to the master. Neither θ nor ε need be known a priori; both are estimated in operation (§6, §7).

**Budget arithmetic.** Worst-case relative drift between two consumer DACs is approximately 50 ppm, consuming a ±2 ms budget in 40 s if uncorrected. With the rate estimator converged to a residual of 2–3 ppm, the budget spans more than ten minutes between corrections.

**Empirical latency behaviour.** Measured static latencies vary by more than an order of magnitude across devices and, for some devices, across sessions: in the reference deployment one device exhibits θ ≈ 0.5–1.4 s with substantial session-to-session variance, while another holds θ ≈ 6.2–6.3 s with high repeatability. Static alignment therefore requires per-device measurement rather than per-model constants, and per-device models must weight recent observations over historical volume.

### 4. Architecture

```
                         ┌──────────────────────────────────────────────┐
                         │                 OpenZone Server              │
                         │                                              │
  Source (file /         │  ┌────────────┐                              │
  stream / pipe) ───────►│  │ Master PCM │  float32, native rate,       │
                         │  │  timeline  │  sample-indexed (t=0 origin) │
                         │  └─────┬──────┘                              │
                         │        │ fan-out (zero-copy ring buffer)     │
                         │  ┌─────┴───────────────────────────────┐     │
                         │  │  Per-device pipeline (×N)           │     │
                         │  │                                     │     │
                         │  │  Delay line ──► Variable resampler  │     │
                         │  │  (sample-      (ppm slew,           │     │
                         │  │   accurate)     soxr/ASRC)          │     │
                         │  │        │                            │     │
                         │  │        ▼                            │     │
                         │  │  Encoder (LPCM preferred,           │     │
                         │  │  else Opus/AAC/FLAC per device)     │     │
                         │  │        │                            │     │
                         │  │        ▼                            │     │
                         │  │  Transport sender                   │     │
                         │  │  (chunked HTTP / RTP / RAOP)        │     │
                         │  └─────────────────────────────────────┘     │
                         │                                              │
                         │  ┌──────────────┐   ┌────────────────────┐   │
                         │  │ Sync engine  │◄──│ Acoustic calibrator│◄──┼── mic
                         │  │ (per-device  │   │ (GCC-PHAT vs       │   │
                         │  │  offset+ppm  │   │  known emission)   │   │
                         │  │  estimator)  │   └────────────────────┘   │
                         │  └──────┬───────┘                            │
                         │         └──► writes delay + ppm targets      │
                         │              into each pipeline              │
                         └──────────────────────────────────────────────┘
                                          │
              ┌──────────────┬────────────┼──────────────┐
              ▼              ▼            ▼              ▼
        Chromecast      Nest Audio    Sonos One     AirPlay speaker
        (pulls URL A)  (pulls URL B) (UPnP, URL C)  (receives RTP D)
```

The governing invariant: **all timing manipulation occurs in the PCM domain, upstream of encoding.** Codecs are pure transport.

#### 4.1 Delay Line

A ring buffer over the master PCM feed, read at a per-device offset with single-sample resolution. Depth must cover the worst inter-vendor bulk-latency disparity; 4 s is provisioned (Cast devices buffer 0.5–2 s in buffered modes and considerably more on live streams; AirPlay ≈ 2 s; Sonos 75 ms–2 s depending on mode). Group playback consequently runs behind the source by approximately the largest device latency — acceptable for music, disqualifying for lip-synchronised video.

Implemented as `MediaSource` in `modules/media/sync_source.py`: an ffmpeg decoder, optionally through the EQ chain, writing float32 into a ring indexed by absolute timeline sample. Two sizing constraints, both load-bearing:

- **The ring must span the source delay plus the widest startup-lag pre-compensation, not merely the delay.** Pre-compensation offsets each device backwards by `θ_max − θ_i` (§3), so with the reference deployment's 0.5 s and 6.3 s devices the fastest reads ~5.8 s of history that the slowest passed long ago. Capacity is `media.cast.sync.ring_capacity_s`, default 20 s (≈7 MB at 44.1 kHz stereo float32) — sized for the spread, not for the delay.
- **A live source cannot be served ahead of its own arrival.** The per-device serve-ahead is cut from `source_delay_s` (default 2.0 s), which must therefore exceed `STREAM_AHEAD_S`; below that the stream loop chases the live edge and underruns every block. The generated timeline is closed-form and declares zero delay, which collapses these expressions to their original form.

The write head is throttled against the play point rather than left to run free: a live station paces itself, but a file, or a station bursting after reconnect, would otherwise overwrite the history the furthest-behind device is still reading. A decoder exit is restarted with backoff and decoding resumes at the current write head, so an outage costs a gap of silence rather than a permanent offset in everything downstream. Reads outside the buffered window are zero-filled and counted, never raised — a hiccup should cost a gap, not a dead session on every speaker.

#### 4.2 Variable Resampler

An arbitrary-ratio resampler per device, its ratio modulated around unity by a control signal expressed in ppm. Requirements: ratio resolution ≤ 0.1 ppm, continuous ratio changes without discontinuity, and negligible THD+N impact at ±200 ppm. `libsoxr` in variable-rate mode satisfies all three. Tempo-domain processors (`pitch`, `scaletempo`) are unsuitable.

For synthetic test material band-limited below ~1 kHz, sub-sample linear interpolation is a measured −84 dBFS approximation of the ideal resampler; programme material does not tolerate it, since linear interpolation's fraction-dependent high-frequency response is audible on wideband audio. That response is exactly `|cos(πf/Fs)|` at the interpolation midpoint — **−2.42 dB at 10 kHz** at 44.1 kHz, worsening to −6 dB by 14.7 kHz. (An earlier revision of this section quoted −5 dB at 10 kHz; the closed form and the measurement both give −2.42 dB.)

Three backends are implemented, selected by `media.cast.sync.resampler`:

| Backend | Mechanism | Notes |
|---|---|---|
| `soxr` (default) | `libsoxr` variable-rate via `ctypes` | Reference path. Measured **+498.87 ppm against a +500 ppm command**; internal delay 100 samples (2.27 ms) at 44.1 kHz |
| `sinc` | 32-tap Lanczos-16 fractional delay, 2048-phase table | Stateless. Bit-exact at integer positions, −0.01 dB at 10 kHz |
| `linear` | Sub-sample linear interpolation | Retained only to reproduce earlier measurements |

Two implementation notes that cost time to establish:

**`SOXR_VR` is the `flags` argument of `soxr_quality_spec`, not a recipe and not an OR into one.** Passing it as the recipe, or omitting it, yields a resampler that creates successfully and then fails the first `soxr_set_io_ratio` with *"varying O/I ratio is not supported with this quality level"*. VR is available at every quality level; the implementation requests VHQ. libsoxr is already linked by ffmpeg in the image, so the `ctypes` binding adds no build dependency and ships as a code patch rather than a wheel rebuild.

**The soxr path is stateful and the rest of the engine is not.** The delay line is addressed by absolute sample position, which is what makes a jump, a trim, or a late-joining device a matter of picking a different index. libsoxr instead holds filter memory and buffers internally, with three consequences the caller carries: the resampler consumes only asymptotically the commanded input per block, so position book-keeping must use the *actual* figure it reports; a deliberate jump must clear the instance, or filter memory smears the two sides of the discontinuity together; and its 2.27 ms delay is a constant addition to θ. That last one is uniform across devices and therefore cancels in Δ_ij (§3) — it shifts the group against the source, never the devices against each other.

The `sinc` backend exists because at ±1000 ppm this is not really sample-rate conversion — it is fractional delay, and a delay filter needs no history. It sidesteps all three consequences above, and is the automatic fallback where `libsoxr` is unavailable.

The ratio command has two components:

```
ratio_i = 1 + ε̂_i + s_i(t)

ε̂_i    : estimated steady-state rate error of device i (drift cancellation)
s_i(t) : bounded slew term used to remove residual offset gradually
```

Slew policy is sensor-dependent and specified in §7.

#### 4.3 Encoder

One encoder instance per device: streams differ in content phase, so no encode is shared. Preference order: (1) LPCM/WAV over chunked HTTP — zero codec latency variance, sample-transparent, ~1.5 Mb/s per device; (2) FLAC; (3) Opus/AAC-LC for constrained devices. Codec frame quantisation (20 ms Opus, ~21.3 ms AAC) does not limit synchronisation accuracy because all alignment is applied before encoding (§5). Every device is served at its native pipeline rate to prevent device-side resampling, which adds nondeterministic latency.

#### 4.4 Transport

| Ecosystem | Mechanism | Notes |
|---|---|---|
| Google Cast | Default Media Receiver pointed at a per-device URL; device pulls chunked HTTP | Media status used for state and position reporting; never as a timing authority |
| AirPlay / RAOP | Server acts as RAOP sender (RTP with timing channel) | AirPlay 2 PTP mode not required; correction is upstream |
| Sonos / UPnP-DLNA | `SetAVTransportURI` + `Play` | Stream mode preferred over buffered radio mode |
| Generic (browsers, Squeezebox, VLC) | Plain HTTP stream URL | Browsers serve as zero-cost test endpoints |

Bluetooth endpoints are excluded: their latency is large *and time-varying* (jitter rather than fixed offset), so no static model fits and the correction loop cannot converge.

The HTTP server issues unbounded chunked responses (no `Content-Length`), one per device, reading from that device's post-encoder FIFO. On reconnect, the stream resumes from the live edge and the device re-enters acquisition (§7.3).

### 5. Correction Domain

A lossy codec quantises the *cutting points* of a stream to its frame size (AAC-LC 1024 samples ≈ 21.3 ms at 48 kHz; MP3 24 ms; Opus typically 20 ms; FLAC blocks ~85 ms). Any compressed-domain correction — frame drop, stream restart, seek — therefore moves the timeline in steps of one frame, one to two orders of magnitude larger than the ±2 ms budget. Frame boundaries between two independent streams need never coincide, however: only the audio *within* the frames must be simultaneous. Shifting the PCM by an exact sample count before encoding achieves arbitrary alignment while the encoder packages the shifted audio obliviously. This yields the invariant of §4 and the requirement for per-device encoder instances.

### 6. Measurement

Two sensors of very different quality close the loop.

#### 6.1 Reported Playback Position

Closed endpoints expose a playback position through their control protocol (for Cast, the media status channel with client-side extrapolation between reports). Comparing the reported position against the server's bookkeeping of what was served yields a per-device lag measurement with no additional hardware and no user interaction.

The signal is poor by network-time standards. Measured characteristics on Google Cast hardware: per-observation noise σ ≈ 10–25 ms, occasional isolated outliers in the hundreds of milliseconds, and systematically unreliable readings in the first seconds after a stream connects, while the device's buffer fills. Three mitigations apply:

- **Within-poll aggregation.** Each poll takes the median of 3–5 consecutive reads. Consecutive reads partially share the device's underlying status report, so returns diminish beyond ~5 reads; the constraint is information, not bandwidth.
- **Across-poll gating.** All correction decisions operate on the median of the last three poll errors; no correction acts on a single reading. A post-connect grace period discards the initial transient entirely.
- **Adaptive cadence.** Polling runs at ~1 s intervals while a device's drift baseline is building and relaxes to 2.5 s at steady state. All devices are measured concurrently.

The effective noise floor after aggregation is approximately ±7 ms per poll — sufficient for coarse alignment and drift estimation, and two orders of magnitude short of the accuracy target. It bounds what §6.2 must contribute.

#### 6.2 Acoustic Time-of-Arrival

The quantity of interest is not what a device reports but when its output reaches the air; the gap between the two — output-pipeline latency in the DAC chain and speaker DSP — is invisible to §6.1 by construction. Acoustic measurement observes it directly, applying the physical principle of per-speaker delay measurement in room-correction systems (Trueplay, Audyssey) as a synchronisation sensor.

**Chirp calibration.** During normal playback, each device is assigned a time slot ~1.5 s apart in which a 100 ms Hann-windowed logarithmic chirp (2–8 kHz, spectrally disjoint from typical programme energy) is mixed into its stream *in the timeline domain, upstream of the resampler*, so it is subject to precisely the corrections applied to programme audio. A single microphone recording spans all slots. Generalised cross-correlation with phase transform (GCC-PHAT) between the recording and the known chirp waveform, with parabolic sub-sample peak interpolation and a peak-to-floor acceptance threshold, yields each chirp's arrival time.

Because all arrivals are extracted from one recording, every common-mode term — capture start latency, microphone clock offset, shared acoustic path — cancels when arrivals are differenced across devices. Microphone clock *drift* contributes < 0.5 ms over a 10 s recording at 50 ppm, within budget. The residual systematic error is microphone path asymmetry: ~1 ms per 34 cm of difference in mic-to-speaker distance, mitigated by placing the microphone near the listening position.

Each device's expected arrival is computed from its slot position, the group latency target, and its current static offset; the deviation, differenced against the group mean, is the true in-air misalignment and is folded directly into the device's static offset. Under synthetic but adversarial conditions — programme material, broadband transients, noise floor, and a −10 dB reflection at 8 ms — the estimator recovers arrival to within ±0.001 ms of ground truth and rejects windows containing no chirp.

**Programme-correlated tracking** (continuous, non-intrusive measurement against the device's individually time-shifted programme rendition, with occasional low-level per-device watermarks for disambiguation) extends the same primitive to closed-loop operation without user interaction; it is specified but not yet implemented.

### 7. Control

#### 7.1 Correction Ladder

All corrections are expressed through the resampler; the playback buffer is stepped only in circumstances where continuity has already been lost. Decisions are made on the median of the last three poll errors, net of any correction already scheduled (the *residual*), so work in flight is never re-corrected:

| Condition | Action |
|---|---|
| Residual beyond ±100 ms (±30 ms before first lock) | Buffer step (hard resync); cooldown while the device flushes |
| Median error beyond ±30 ms (≈2σ of the sensor) | Fast slew: 1000 ppm ≈ 1.7 cents, inaudible |
| Within ±30 ms | Gentle slew, ≤ 20 ppm, continuous |

The pre-lock threshold reduction reflects a boundary condition rather than a policy change: before a device first locks, it is not yet audibly part of the group, and stepping a 30–100 ms startup offset instantly is strictly preferable to grinding it out over tens of seconds.

With the acoustic sensor active, the slew thresholds contract toward the original ±20 ppm design point; the wide ladder is a property of the coarse sensor, not of the architecture.

#### 7.2 Drift Estimation

**Significance gating.** A span threshold is a proxy for "is this fit informative", and a poor one, because sensor noise differs per device: in one reference group the two members measured p90 |error| of 8.8 ms and 22.8 ms, so an identical baseline buys them very different confidence. The fit is therefore gated on its own standard error, `σ_slope = √(s²/Σ(t−t̄)²)`, rather than on elapsed time alone. At ~12 ms poll noise and a 2.5 s cadence that error is on the order of ±140 ppm at a 60 s baseline and still ±18 ppm at 240 s — far larger than the few ppm of real crystal drift being sought. A slope that cannot be distinguished from zero carries no information, and applying it injects precisely the differential rate error the loop exists to remove. Weight ramps from zero at 2σ to full at 3σ.

**Anchoring.** The weighted estimate is blended against the *model prior*, not against the estimator's own previous output. Feeding the previous output back — `rate += g·(slope − rate)` — is a random walk when the slope is noise: each poll steps toward a fresh noisy target with no restoring force, and because consecutive fits share almost all their points a chance excursion persists across many polls and walks the estimate into the clamp. Anchoring to the prior makes an uninformative fit decay to what the model has learned across sessions, and only a fit clearly better than the prior displaces it.

The asymmetry justifying conservatism: the rate term is feed-forward, with the slew loop as the fast actuator behind it. Under-tracking a real drift costs a slow-draining slew; over-trusting a noisy fit costs permanent differential wander.

Simulated against ground truth at the measured noise levels (two devices whose true drifts differ by 0.011 ppm, 5-minute sessions, 300 trials), the injected differential misalignment accumulated over a session falls from a median of 2.28 ms to 0.02 ms, and at p90 from 5.97 ms to 1.98 ms. The extreme tail is only modestly improved (15.5 ms → 11.1 ms): with a noisy sensor a short session will occasionally produce a convincingly linear spurious slope, and no gate distinguishes it from drift within the window. A genuine 40 ppm offset is still recovered to ~37 ppm.

The rate term ε̂ is obtained by linear regression over the device's *free-running* lag: each lag observation is decompensated by adding back the cumulative deliberate timeline motion (rate correction, slews, and steps) applied up to that instant. The regression therefore observes the device clock alone and cannot chase its own actuator — a closed-loop integrator formulation, evaluated first, fed correction-induced motion and sensor noise back into itself and oscillated between its clamps.

Resolving ppm-grade slopes through ±10 ms observation noise is a baseline problem: slope noise falls as T^1.5 for observation span T. The window accordingly grows to ~20 min (bounded to permit slow thermal re-tracking), the fit is applied only beyond a 60 s span with gain scaled up to full weight at 240 s, one 3σ outlier-rejection pass guards isolated bad readings, and any fitted slope beyond 200 ppm — unreachable by any crystal — is classified as a disturbance (host scheduling stall, device pipeline event) and restarts the baseline rather than updating the estimate. The estimate is clamped to the physical ±50 ppm bound.

Per-device estimates persist in a model keyed by device identity, seeding subsequent sessions. Model updates require a fit span sufficient for the estimator to have genuinely converged, preventing seed values from re-recording themselves as fresh evidence.

#### 7.3 Acquisition and Lock

Per device: `UNLOCKED → ACQUIRING → LOCKED → HOLDING`.

- **UNLOCKED** — no valid estimate (start, rebuffer, reconnect, seek). Playback proceeds; measurement is dense.
- **ACQUIRING** — estimator converging; ~1 s measurement cadence; reduced step threshold (§7.1).
- **LOCKED** — offset within threshold and rate residual small; relaxed cadence.
- **HOLDING** — sensor unavailable; open-loop on the last rate estimate (budget-safe for ~10 min at 3 ppm residual).

Any device-side pipeline event demotes that device to UNLOCKED; the group continues playing and recovery is automatic. A desynchronisation window of a few seconds during recovery is the irreducible cost of not controlling endpoint firmware; the design goal is automatic recovery, not the pretence that interruptions do not occur.

#### 7.4 Static Offset and Manual Adjustment

Each device carries a persistent static offset (resolution 1 ms, ±2 s range) representing output-pipeline latency and listener preference. The acoustic calibrator writes it; a manual control exposes it. Manual adjustments during playback apply as an immediate buffer step, deliberately trading the no-step rule for feedback latency: an offset change slewed at inaudible rates takes `|Δ|/rate` — over a minute for a 100 ms change — to become audible, which renders adjustment-by-ear impossible in practice. The step is decompensated in the drift estimator's bookkeeping, so the rate baseline survives it.

### 8. Error Budget

| Error source | Magnitude | Mitigation |
|---|---|---|
| Reported-position noise (per poll, after aggregation) | ~±7 ms | median filtering; drives coarse ladder only |
| GCC-PHAT peak resolution at 44.1/48 kHz | ~0.1–0.3 ms | parabolic peak interpolation |
| Microphone path asymmetry | ~1 ms per 34 cm | placement at listening position; two-position calibration |
| Room reflections | peak smearing | PHAT weighting; windowed chirps; acceptance threshold |
| Estimator residual drift | ≤ 3 ppm | periodic re-measurement; HOLDING budget ~10 min |
| Microphone clock drift over one calibration | < 0.5 ms | single-recording differencing |

Net achievable with the acoustic sensor: **±0.5–1 ms** inter-device at the calibration point, inside the ±2 ms target with margin. The target denotes multi-room echo-free reproduction; sub-millisecond phase-coherent stereo pairing across vendors within one room lies at the edge of feasibility and is not claimed.

### 9. Empirical Results

Reference deployment: two Google Cast devices (Chromecast-class and Nest Audio), LPCM/WAV over chunked HTTP at 44.1 kHz, reported-position sensor only, five-minute fixed observation windows.

- **Steady-state alignment (reported-position domain):** per-device median |error| 3–13 ms, 90th percentile 7–29 ms; inter-device spread median 8–9 ms. These figures sit at the aggregated sensor's noise floor — the loop extracts essentially all the information the sensor contains.
- **Drift estimation:** live estimates converge to 0–5 ppm and remain stable across sessions; in simulation against a known ground truth with realistic sensor noise, the estimator settles within ±2–4 ppm of the true rate, where the superseded integral controller oscillated between ±50 ppm clamps.
- **Acquisition:** one buffer step per device per session (frequently zero for the high-latency device, whose model-based pre-alignment lands within the slew window), completing within ~10 s. The earlier single-reading policy produced a characteristic overshoot-and-return step pair at every start.
- **Static latency:** θ session-to-session repeatability differs sharply between devices (§3), confirming recency-weighted per-device modelling.
- **Chirp extraction:** ±0.001 ms recovery against ground truth under synthetic room conditions; in-room accuracy is expected to be dominated by the §8 acoustic terms rather than by the estimator.

### 10. Limitations

1. **Endpoint pipeline events break lock.** Rebuffering and reconnects cause seconds-long desynchronisation until re-acquisition; unavoidable without firmware access.
2. **The group plays late** by approximately the largest device latency (observed up to ~6.5 s). Music is unaffected; lip-synchronised video is out of scope.
3. **Bluetooth endpoints are excluded** (time-varying latency; no stable model).
4. **Full automation requires a microphone**; without one the system is manual static offset plus open-loop drift hold.
5. **Device behaviours require per-model accommodation** (stream-format probing, buffered-radio modes, firmware changes to latency); an ongoing compatibility matrix, not a one-off implementation.
6. **Same-room phase-coherent pairing** across vendors is not a goal; the target is echo-free multi-room listening.

### 11. Future Work

Programme-correlated acoustic tracking (§6.2) for continuous closed-loop operation; RAOP and UPnP transport senders alongside the Cast implementation; multi-microphone operation and cached per-model latency profiles; migration of the fan-out, resampling, and transport hot path to a compiled daemon; concurrent independent sessions, the engine still being single-session by construction.

Ingest of arbitrary programme material (§4.1) and the soxr resampler path (§4.2) are implemented and no longer future work. The empirical figures in §9 predate them and were taken on the generated timeline; they have not yet been re-measured against programme material, where the sensor is unchanged but the correction domain now carries real spectral content.

---

### Appendix A. Glossary

- **PTS** — presentation timestamp: "play this sample at time T on the reference clock."
- **ASRC** — asynchronous sample-rate converter; a resampler with a continuously adjustable ratio, used for ppm-level rate discipline.
- **ppm** — parts per million of rate error; 50 ppm ≈ 3 ms of drift per minute.
- **GCC-PHAT** — generalised cross-correlation with phase transform; time-delay estimation robust to reverberation.
- **Multizone** — Google's private leader/follower group-playback protocol, coordination namespace `urn:x-cast:com.google.cast.multizone`.
