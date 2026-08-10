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

| Function               | Google multizone                                 | OpenZone                                                                                                        |
|------------------------|--------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| Timeline authority     | Leader device clock                              | Server master PCM position                                                                                      |
| Clock/rate measurement | In-band NTP-style exchange, µs-grade, continuous | Reported playback position (~10 ms grade, continuous) and acoustic cross-correlation (~0.1 ms grade, on demand) |
| Correction actuator    | ASRC in follower firmware                        | Per-device variable resampler at the source                                                                     |
| Static alignment       | Implicit in PTS scheduling                       | Per-device sample-accurate delay plus measured offset                                                           |
| Transport              | Private leader→follower stream                   | Per-device native protocol; each endpoint pulls its own URL                                                     |

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
                         │  │   accurate)     frac. delay)        │     │
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

**Finite material and item boundaries.** A zone plays one timeline, so a queue cannot be walked by the endpoints — they receive an undifferentiated stream. It is walked by the decoder, and the seam between two items is the only point at which a zone can change what it is playing without disturbing anyone's alignment. The source therefore resolves its input immediately before every decoder start and is told *how the previous decode ended*, which is the entire mechanism: a clean exit means a finite item played out and the next one is due; a non-zero exit means the same item wants another attempt, usually because a signed URL expired mid-play (commercial services sign for minutes, not hours, so any session outliving one would otherwise decode to EOF and stay dead); no return code at all — the first start — means resolve what is current. A clean exit is not a fault and costs neither a restart count nor backoff, since a gap between items is audible; the fault path retains both. An item that produces no audio at all falls through to the fault path regardless of its exit code, so a dead URL cannot spin the loop.

Declining to supply a successor ends the material. The session is not torn down at that instant: at the moment the decoder stops, every device is still playing out of the delay line and its own buffer, an entire group latency behind the write head, so teardown waits until the tail has been *heard*. Cutting at the last decoded byte would truncate the end of the last item on every speaker.

#### 4.1a Crossfade at the Item Boundary

The seam between two items (§4.1) is a butt splice by default. Overlapping them instead is, in this architecture, a property of the delay line rather than a feature bolted beside it — and the placement is what makes it cheap.

**The overlap is paid for out of headroom that already exists.** Readers sit a group latency behind the write head, so the newest stretch of the ring is audio that has been decoded but served to nobody. It can still be reworked in place. The outgoing item's tail is faded down where it already lies, the incoming item's head is mixed over it, and no audio is held back and no latency is added. Systems that emit into a per-player stream cannot do this: once bytes leave, they are gone, and a crossfade there has to be built by buffering both sides ahead of the mix.

The budget is also the hard limit, and it is not knowable in advance — it depends on where the previous item happened to end relative to the write head. The length is therefore decided at the seam, re-checked at commit (collecting the incoming head consumes real time, during which readers advance), and **abandoned rather than forced** when it no longer fits. A seam that cannot afford a fade splices exactly as it did before. The practical ceiling is `source_delay_s` less a guard, so ~1.5 s at the default settings; longer overlaps need the incoming decoder started early against a known item duration, which is a separate mechanism.

Because the mix happens in the timeline domain, upstream of the per-device delay and resampler, the crossfade is sample-identical on every speaker and cannot perturb alignment — the same invariant that lets the calibration chirp be injected there (§6.2). Crossfade and multi-room synchronisation are not interacting features here; they are the same buffer.

**The budget must be measured against the readers, not against a play point, and this is where the first implementation went wrong.** Rework-in-place is safe only strictly ahead of every reader, and the headroom was estimated as `ring.end − (now − epoch)·Fs` — a single notional play point that no device actually occupies. A device reads at `(now − epoch − delay)·Fs` less its trim and its startup pre-compensation, plus every correction the ladder has since applied to it. None of those terms are visible from the delay line. The estimate was conservative in the nominal case, but only by the accident of two unrelated constants: the serve loop caps the furthest reader `source_delay_s − STREAM_AHEAD_S` behind the notional point, 0.8 s at the defaults. It does not follow the cumulative deliberate motion the serve loop carries per device (which grows with every jump and every drained slew, and passes 0.8 s within a session), and it inverts outright if the serve-ahead is raised to meet the delay. The source is now *told* the furthest-ahead reader position and keeps a half-second margin behind it, the margin covering the block a generator may be part-way through and the half-kernel the interpolator reaches past its position.

**The seam's own cost has to be charged to the budget that pays for it.** Two costs are spent with the write head parked while the readers keep walking towards it: resolving the next URL and starting a decoder against a commercial streaming service, and then receiving the overlap itself. Budgeting before the decoder starts charges the first to nobody, and waiting for the full overlap before re-testing charges the second too late — an item arriving at or below 1× is waited on long after the headroom that authorised it is gone. The overlap is therefore decided on the incoming item's *first decoded block*, and the budget re-tested every block, stopping as soon as what is already in hand is all the room still affords. A 1× source yields at least half the requested fade instead of a stall.

**Both faults present as a skew between the speakers rather than as a bad crossfade**, which is what makes them hard to read from the symptom. The delay line is a pure function of sample index — that property is what makes every device's rendition the same audio — and both faults break it in the same way, by making the content at index `X` depend on *when* `X` was read. Devices read the same index seconds apart (§3: 0.5 s and 6.3 s of static latency in the reference deployment, so ~5.8 s of pre-compensation between their read heads). A splice landing behind a reader is heard as the blend by the speakers still approaching it and as a hard cut by those past it. A stall outlasting the headroom is heard as silence on whichever device is furthest ahead — the *high*-latency one, whose pre-compensation is zero and whose margin is therefore the smallest — while the others, reaching those samples later, get real audio. Simulated at the reference geometry, a 1.65 s overlap behind a 1.5 s decoder start on a 0.7× feed stalled 3.93 s, abandoned the fade anyway, and left the 6.3 s device with 1.13 s of silence the 0.5 s device never heard.

**Fade law.** A crossfade sounds like a *dip* when the law does not match the material, and the two standard laws are each wrong for half of it: uncorrelated signals add as power, so equal-amplitude gains dip 3 dB at the midpoint; correlated ones add as amplitude, so equal-power *gains* 3 dB. One exponent spans both, exactly rather than approximately:

```
g_out = cos(πt/2)^p        g_in = sin(πt/2)^p

p = 1  ->  g_out² + g_in² == 1     constant power
p = 2  ->  g_out  + g_in  == 1     constant amplitude
```

`p = 1 + |r|`, with `r` the normalised cross-correlation of the two overlap segments measured on the level-normalised mono sum — what decides the law is whether the signals reinforce or add in quadrature, which is a property of their shapes and not of which is louder. Measured across correlations, the envelope holds within +0.26 dB of unity, against a 3 dB error for either fixed law at the wrong extreme:

| material | fixed power | fixed amplitude | adaptive |
|---|---|---|---|
| uncorrelated | −0.25 dB | **−3.06 dB** | −0.28 dB |
| correlated | **+2.95 dB** | −0.00 dB | −0.00 dB |

**The mix belongs in the compiled crate, and for an algorithmic reason rather than a language one.** The overlap runs once per item, so throughput is irrelevant; what matters is that it runs *synchronously on the event loop*, between two ring operations that cannot leave it (§A.1), at the one moment in a session when every stream generator is already contending. The numpy formulation of the peak guard is `sliding_window_view(...).min(axis=1)` followed by `np.convolve`, and both are **O(n·w) where the algorithm is O(n)** — at a 1.65 s overlap, 16M operations per stage over a strided view, tens of milliseconds of blocked loop. In `zmm_eq.xfade_mix` the running minimum is a monotonic deque, and the Hann-weighted average exploits `k[d] = (0.5 − 0.5cos(ωd))/S` splitting into a box term and two quadrature terms, each a prefix-sum difference; both stages become single passes. The numpy path is retained as the fallback where the wheel is absent, as it is for the resampler (§4.2), and the two agree to float32 epsilon.

**Level is not peak, and the distinction is load-bearing.** A flat envelope does not bound instantaneous samples: two uncorrelated signals hold a constant RMS through the fade while their peaks still add, measured at +3.01 dB, and the serve path clips hard. A gain envelope over the overlap removes that excess — per-sample requirement, running minimum for look-ahead and release, smoothed so the reduction is itself inaudible. Its ceiling is the louder *source's* own peak rather than an absolute number: at the edges of the fade the mix **is** one source at unity gain, so an absolute ceiling below that source's peak would demand a reduction that cannot be applied without stepping the level against the audio either side. The guard removes only the excess the sum creates; material that was already hot stays as hot as it was and meets the serve path on the same terms as the rest of the track.

#### 4.2 Variable Resampler

An arbitrary-ratio resampler per device, its ratio modulated around unity by a control signal expressed in ppm. Requirements: ratio resolution ≤ 0.1 ppm, continuous ratio changes without discontinuity, and negligible THD+N impact at ±200 ppm. A stateless fractional-delay filter satisfies all three trivially, because it has no state for a ratio change to discontinue. Tempo-domain processors (`pitch`, `scaletempo`) are unsuitable.

For synthetic test material band-limited below ~1 kHz, sub-sample linear interpolation is a measured −84 dBFS approximation of the ideal resampler; programme material does not tolerate it, since linear interpolation's fraction-dependent high-frequency response is audible on wideband audio. That response is exactly `|cos(πf/Fs)|` at the interpolation midpoint — **−2.42 dB at 10 kHz** at 44.1 kHz, worsening to −6 dB by 14.7 kHz. (An earlier revision of this section quoted −5 dB at 10 kHz; the closed form and the measurement both give −2.42 dB.)

Three backends are implemented, selected by `media.cast.sync.resampler`:

| Backend | Mechanism | Notes |
|---|---|---|
| `rust` (default) | 32-tap Lanczos-16 fractional delay, 2048-phase table, in `zmm_eq.interp_block` | Stateless. Bit-exact at integer positions, −0.01 dB at 10 kHz. ~290× realtime per core |
| `sinc` | The same filter in numpy | Automatic fallback where the `zmm_eq` wheel is absent. Agrees with `rust` to float32 epsilon; ~70× realtime per core |
| `linear` | Sub-sample linear interpolation | Retained only to reproduce earlier measurements |

`soxr` is still accepted in config as a deprecated alias for `rust`.

**This is fractional delay, not sample-rate conversion, and that is why it can be stateless.** At ±1000 ppm the ratio never leaves the neighbourhood of unity, so no material is being rate-converted — the timeline is simply read at a different position. A delay filter can be evaluated at an arbitrary position with no history, which is what lets the whole engine keep its central property: the delay line is addressed by absolute sample position, so a jump, a trim, or a late-joining device is a matter of picking a different index. Output length is exactly what was asked for, the commanded advance is consumed exactly, and there is no filter memory to clear across a discontinuity.

**On the removal of the `libsoxr` backend.** Until v31.04.07.2026 the default was `libsoxr` in variable-rate mode, bound through `ctypes`, and it was the reference path: measured **+498.87 ppm against a +500 ppm command**, internal delay 100 samples (2.27 ms) at 44.1 kHz. It was removed after it aborted the process with `double free or corruption (out)` during a stream session. The binding passed raw numpy heap pointers (`ndarray.ctypes.data`) across a hand-declared ABI and freed a `soxr_t` by hand from a `close()` that session teardown could race against a live stream generator — while a Cast device re-fetching its URL could put two generators on one instance at the same time. The failure was in the ownership model, not in libsoxr.

What the port gives up is genuine but small: a true variable-rate SRC tracked the commanded ratio to 1.13 ppm, where a fractional-delay filter is exact by construction because it never resamples. What it gains is that there is no handle, no C ABI, no filter state and nothing to free, so concurrent callers cannot corrupt each other and teardown cannot race playback. The 2.27 ms constant delay also disappears from θ; it was uniform across devices and cancelled in Δ_ij (§3) anyway, so this shifts the group against the source by that much and does not affect inter-device alignment.

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
- **Staleness rejection.** The reported position is extrapolated from the last status report to read time, which is a measurement while the report is fresh and a guess once it is not: a device that rebuffered is credited with having played through the gap, and the correction arrives as a step of hundreds of milliseconds the moment the next report lands. Within-poll aggregation cannot defend against this — the reads in one poll extrapolate the *same* report, so the median reproduces it with false confidence rather than rejecting it. Reads whose underlying report is older than two poll intervals are discarded outright. A device that never reports inside that bound goes uncorrected, so the discards are counted into its stats; a sensor that has gone dark must not look like one that is merely quiet.

The effective noise floor after aggregation is approximately ±7 ms per poll — sufficient for coarse alignment and drift estimation, and two orders of magnitude short of the accuracy target. It bounds what §6.2 must contribute.

**The measurement is taken net of the device's static offset (§7.4), and this is load-bearing.** The offset shifts the timeline the device is served from, and the same quantity is subtracted from the resulting lag, so the two cancel exactly and the reported-position loop is blind to it. Without that cancellation the loop would treat a deliberate offset as error and slew it back out — the manual control and the acoustic calibrator would both be fighting the correction loop, and neither could win. The cancellation holds only while the value subtracted is the value the served timeline was actually built from: the offset is therefore *latched* per stream at the moment it is applied, and the timeline step and the subtracted term change together, in one operation, for one device. Re-reading the effective offset per poll — which is what a per-model default (§7.4) makes possible, since it can change while a session runs — breaks the identity on any device that did not move, and the loop reads the difference as a real error of the full offset and corrects audio that was already where it belonged.

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
| Reload cannot converge either (below) | Group re-alignment: re-LOAD everyone, re-derive the target |
| Step clamped by the reader ceiling | Forced receiver re-LOAD (§A.2), rate-limited per device |
| Residual beyond ±100 ms (±30 ms before first lock) | Buffer step (hard resync); cooldown while the device flushes |
| Median error beyond ±30 ms (≈2σ of the sensor) | Fast slew: 1000 ppm ≈ 1.7 cents, inaudible |
| Within ±30 ms | Gentle slew, ≤ 20 ppm, continuous |

**The step rung is bounded in one direction only, and the bound is narrow.** The serve loop paces each reader to sit `source_delay_s − STREAM_AHEAD_S` below the play point, and the play point is the reader ceiling (§A.2), so *forward* motion — the correction a device that has fallen behind needs — can never exceed that difference. Backwards motion is bounded only by the ring. At the original `source_delay_s` of 2.0 s the forward authority was 800 ms against group latencies of 8 s and more, which makes every disturbance larger than a fifth of a second unreachable by the rung nominally responsible for it. The default is now 4.0 s, buying 2.8 s. The cost is a group that plays 2 s later (§10.2) and a longer prime; the benefit is that an event-loop stall of the size actually observed is absorbed by a step instead of escalating.

**A per-device re-LOAD only converges inside a budget the group's geometry fixes, not the size of the error.** A reload re-seats the reader at `play_now − delay_s − trim − precomp` and the device plays that sample once it has refilled, so it lands on target only if it resumes within `target_lag − (delay_s + trim + precomp)` — which reduces to `max_precomp − precomp_i − trim_i`. For the *most* pre-compensated speaker in a wide group that is a couple of hundred milliseconds, against Cast refills measured in seconds; for the least pre-compensated it is seconds and reloads work. Raising `source_delay_s` does not help here, because `target_lag` grows with it and the term cancels.

Observed on 2026-08-10: a 10.5 s event-loop stall (a DuckDB query on the loop thread, from an unrelated subsystem — §A.1) left five speakers 2.4–6.4 s behind. Every one was past the step ceiling. The two low-latency speakers then reloaded to a stable `lag ≈ 2 × target` — 17.06 s against an 8.34 s target, flat across dozens of polls, so the device was playing correctly and simply one whole group-latency late — and each reload reproduced it exactly. One took seven reloads over 4 m 25 s and escaped only when a refill happened to take 0.6 s.

**The rung above is therefore group re-alignment, not a further reload.** Session start is the only path that reliably converges, and the reason is that it does not aim at a fixed target: every device is LOADed together and `target_lag` is *derived* from the lags that result, so whatever the receivers actually did becomes the definition of aligned. A re-alignment re-LOADs every receiver, re-seats every reader from the model, and clears the target so it is re-derived the same way. The source is untouched — the timeline was never the problem, only the devices' relationship to it. Two constraints make it safe: nothing may be derived until the re-acquisition cooldown has expired, or "aligned" gets defined from the middle of the disturbance; and the re-measured lags must not reach the learned model, because they are re-acquisition figures taken while every receiver refills at once, not the natural startup latency the model holds (§7.2).

**A clamped step must not be charged as a step.** The cooldown exists to let a discontinuity reach the device's output before the sensor is believed again; a step that moved nothing produced no discontinuity, so the ~11 s blackout buys nothing and delays the only correction still available. Clearing the error history compounds it — the next decision then needs three fresh readings *after* the blackout. Together these spaced reload attempts at ~42 s against their own 30 s floor. The clamped path now leaves cooldown, history, slew and the resync counter alone and re-decides on the next poll; its warning is rate-limited instead, since the decision now repeats.

The pre-lock threshold reduction reflects a boundary condition rather than a policy change: before a device first locks, it is not yet audibly part of the group, and stepping a 30–100 ms startup offset instantly is strictly preferable to grinding it out over tens of seconds.

**A slew may never own more than the step threshold, and the ladder is only monotonic because of it.** Deciding on the residual is what stops work in flight being re-corrected, but it also means a slew that absorbs an offset outright drives the residual to zero — and a zero residual cannot authorise a step. An unbounded slew therefore disarms the rung above it: the ladder stops being a ladder and the 1000 ppm actuator is left alone with an offset it drains at 1 ms/s, which for a multi-second offset is tens of minutes of audible misalignment with no escalation path and no further events logged. Clamping the scheduled slew at the step threshold makes the two rungs complementary by construction — the slew owns everything up to ±100 ms, the excess stays in the residual where the three-reading vote can still see it — so a discontinuity is re-detected on the next polls and escalates on its own. This costs nothing in the steady state, where the slew is never asked to carry more than a few tens of milliseconds.

The distinction matters most where the two failure modes look alike from a single poll: drift arrives continuously and never leaves the slew's range, while a source-side stall arrives as a step of whatever the stall lasted. Only the second needs the rung above, and only the ceiling lets it get there.

**A step needs the whole window, not a majority of it.** The step is the one correction a listener hears as a discontinuity, so past first lock it is authorised only by three readings that are each beyond the threshold and all of one sign — not by the median alone. The distinction is not academic, because a step also *clears* the error history: permitting a decision on the two readings that follow left every step having lowered the bar for the next one. Against a two-poll sensor transient the loop would then step several hundred milliseconds into a correctly aligned device, spend the cooldown blind to what it had done, and step back by the same amount on the far side — two audible jumps that cancel, and a drift baseline discarded at each one. In one reference group 19% of consecutive steps were an immediate undo of their predecessor, at a median 371 ms and a p90 of 3.6 s, clustered at cooldown-plus-one-poll after the step they were undoing.

A related detail carried the same fault: `sorted(xs)[len(xs) // 2]` is the upper of the two middle values on an even-length list, so on two samples it selects the more positive rather than their midpoint. The selector was therefore biased toward reporting a device as behind, and the evidence is in the asymmetry — self-undoing steps began with a positive step 81% of the time against a 50% base rate across all steps. Genuine misalignment persists across three polls and is unaffected by either requirement.

**The cooldown after a step is not a constant.** A step moves the reader, but the device reveals it only after playing out what it had already buffered — the group's target lag, seconds rather than milliseconds. A cooldown shorter than that does not merely delay the recovery: it guarantees at least one poll of pre-step data, and since the decision is a median over recent readings, the stale readings re-authorise the step that produced them. A flat 7 s against a measured 8.4 s target lag stepped one device +4.2 s and then +4.4 s again 27 s later, having already corrected it; the second step is what put the reader on the write head. The cooldown is therefore `max(floor, target_lag + poll_interval)`, and it applies to every path that moves a reader — step, trim, and re-LOAD. The re-LOAD is the strictest case, because it also demotes the device to unacquired, where two readings suffice to authorise a step.

With the acoustic sensor active, the slew thresholds contract toward the original ±20 ppm design point; the wide ladder is a property of the coarse sensor, not of the architecture.

#### 7.2 Drift Estimation

**Significance gating.** A span threshold is a proxy for "is this fit informative", and a poor one, because sensor noise differs per device: in one reference group the two members measured p90 |error| of 8.8 ms and 22.8 ms, so an identical baseline buys them very different confidence. The fit is therefore gated on its own standard error, `σ_slope = √(s²/Σ(t−t̄)²)`, rather than on elapsed time alone. At ~12 ms poll noise and a 2.5 s cadence that error is on the order of ±140 ppm at a 60 s baseline and still ±18 ppm at 240 s — far larger than the few ppm of real crystal drift being sought. A slope that cannot be distinguished from zero carries no information, and applying it injects precisely the differential rate error the loop exists to remove. Weight ramps from zero at 2σ to full at 3σ.

**Anchoring.** The weighted estimate is blended against the *model prior*, not against the estimator's own previous output. Feeding the previous output back — `rate += g·(slope − rate)` — is a random walk when the slope is noise: each poll steps toward a fresh noisy target with no restoring force, and because consecutive fits share almost all their points a chance excursion persists across many polls and walks the estimate into the clamp. Anchoring to the prior makes an uninformative fit decay to what the model has learned across sessions, and only a fit clearly better than the prior displaces it.

The asymmetry justifying conservatism: the rate term is feed-forward, with the slew loop as the fast actuator behind it. Under-tracking a real drift costs a slow-draining slew; over-trusting a noisy fit costs permanent differential wander.

Simulated against ground truth at the measured noise levels (two devices whose true drifts differ by 0.011 ppm, 5-minute sessions, 300 trials), the injected differential misalignment accumulated over a session falls from a median of 2.28 ms to 0.02 ms, and at p90 from 5.97 ms to 1.98 ms. The extreme tail is only modestly improved (15.5 ms → 11.1 ms): with a noisy sensor a short session will occasionally produce a convincingly linear spurious slope, and no gate distinguishes it from drift within the window. A genuine 40 ppm offset is still recovered to ~37 ppm.

The rate term ε̂ is obtained by linear regression over the device's *free-running* lag: each lag observation is decompensated by adding back the cumulative deliberate timeline motion (rate correction, slews, and steps) applied up to that instant. The regression therefore observes the device clock alone and cannot chase its own actuator — a closed-loop integrator formulation, evaluated first, fed correction-induced motion and sensor noise back into itself and oscillated between its clamps.

Resolving ppm-grade slopes through ±10 ms observation noise is a baseline problem: slope noise falls as T^1.5 for observation span T. The window accordingly grows to ~20 min (bounded to permit slow thermal re-tracking), the fit is applied only beyond a 60 s span with gain scaled up to full weight at 240 s, one 3σ outlier-rejection pass guards isolated bad readings, and any fitted slope beyond 200 ppm — unreachable by any crystal — is classified as a disturbance (host scheduling stall, device pipeline event) and restarts the baseline rather than updating the estimate.

**Reaching the ±50 ppm bound is a rejection, not a value to clip.** Clamping a blended estimate into range records the clamp *as* the estimate, and since the window that produced it is discarded immediately afterwards, the clamp is what the device then runs on — the maximum differential drift the loop exists to remove, applied as though it had been measured. A result at or beyond the bound therefore restarts the baseline and reverts to the model prior. The 200 ppm disturbance gate does not cover this: it catches the gross cases, and everything between the bound and four times it lands here.

**An estimate with no live evidence decays to the prior.** A step clears the baseline, which must then rebuild past the 60 s apply threshold before any fit runs again; steps arriving faster than that leave the last fit standing indefinitely, since nothing else can revise or retire it. Whatever value happened to be current when the disturbance began becomes permanent for the session. Observed directly: through the final quarter-hour of one 2.6-hour session, four of five devices held a fixed rate across ~300 polls each — two of them at exactly +50.0 ppm — while the fifth, spiking less and keeping its baseline, updated normally over 215 distinct values. The rate term is feed-forward, so an unsupported estimate now relaxes toward the cross-session model value rather than toward whichever fit ran last.

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

**The transient handling is scaled to the step.** A step large enough to disturb the endpoint's buffer is followed by a cooldown and a reset of the poll-median history, since readings taken across the discontinuity describe neither side of it. Applying that treatment to *every* step is a mistake that only shows up in use: alignment by ear is not one adjustment but a run of dozens of 1 ms taps, and at one blackout per tap the correction loop is blind for the entire tuning session — accumulating exactly the error the listener is straining to hear. Steps at or below the sensor's own noise floor (10 ms) therefore apply without disturbing the loop's state at all. They are invisible to the sensor by construction (§6.1) and too small to perturb the buffer, so there is nothing for the blackout to protect.

**The model identity must not flicker.** A default keyed on something the network reports inconsistently is worse than no default, because the value keyed on it changes with the key. The key was `cast_type/model_name`, on the reasoning that Google reports the form factor separately and the screened devices are where the long pipelines live — but mDNS does not always carry `cast_type`, and a Pixel Tablet enumerates with it absent on some discoveries and present on others. One physical device wrote under two keys, and the live store was found holding `cast/Pixel Tablet: 0` beside `/Pixel Tablet: 219`, one of them silently authoritative depending on which discovery ran last. The prefix bought nothing the name did not already carry — a "Google Nest Hub" is screened and a "Google Home" is not, whatever the discovery says — so the key is now the model name alone, and legacy entries are folded onto it once at session start. Where two folded values disagree, an explicit per-device trim on a unit of that model decides; failing that the default is dropped, per the paragraph below.

**Per-model defaults, and when to abandon them.** Output-pipeline latency is a property of the hardware, not of the individual unit, so an offset established on one device is a defensible starting point for the next device of the same model — a second screened device joining a zone should not have to be measured from zero. The premise is falsifiable, and it must be tested rather than assumed: two units of one model deliberately set more than 25 ms apart are evidence that in this installation the offset is also absorbing something unit-specific — placement, distance to the listening position, room — at which point extrapolating from either of them applies a wrong number to a device nobody touched. The default is then discarded rather than overwritten, and untrimmed units of that model revert to where the correction loop puts them. No default is better than a confident wrong one. A default is in any case only a starting value: it is latched at session start (§6.1), never mid-session, and an explicit per-device offset always outranks it.

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
7. **Endpoint displays show the session, not the item.** Track metadata and artwork ride on the load message that starts a device's stream, and closed endpoints offer no way to revise them afterwards; re-issuing the load would restart the device's buffering and cost the zone its alignment (§7.3). A zone playing a multi-item queue therefore shows the set on its screens while the controlling application tracks the individual item, where doing so is free. Per-item display updates would need a custom receiver carrying its own metadata channel.

### 11. Future Work

Programme-correlated acoustic tracking (§6.2) for continuous closed-loop operation; RAOP and UPnP transport senders alongside the Cast implementation; multi-microphone operation and cached per-model latency profiles; migration of the fan-out, resampling, and transport hot path to a compiled daemon; concurrent independent sessions, the engine still being single-session by construction.

Ingest of arbitrary programme material (§4.1) and the per-device resampler (§4.2) are implemented and no longer future work. The empirical figures in §9 predate them and were taken on the generated timeline; they have not yet been re-measured against programme material, where the sensor is unchanged but the correction domain now carries real spectral content.

---

### Appendix A. Implementation Invariants

Rules the implementation depends on, kept here rather than in the source. Each one is a constraint whose violation has a specific consequence; the code carries a section reference where the hazard is not visible from the line itself.

#### A.1 Threading

The engine is single-threaded by construction: the delay line, the correction ladder, and every stream generator run on the asyncio event loop, and their mutual exclusion is the loop itself, not a lock.

- **No DuckDB write may be issued from the loop thread.** Every `telemetry_db` and `sync_db` write helper is synchronous and takes a lock held for as long as DuckDB needs it — on first open after a restart, that is a full open/migration/WAL replay. Anything blocking the loop stops every stream generator with it, so the speakers drain their buffers and the group falls out of alignment by the length of the stall. Route writes through `asyncio.to_thread`, or buffer in memory and drain from a background task.

- **Nor any DuckDB *read*, and the callers that matter are in other subsystems.** This engine does not own its event loop; it shares one with every other module in the application, and none of them know that a scan of their own telemetry is an audio fault. `heating_controller._last_temperature_ts` ran a freshness query on the loop once per room per tick — its cache TTL was under the tick interval, so every tick missed — and on 2026-08-10 that query took **10.5 s**, against a `telemetry.duckdb` sitting behind a 9.6 MB un-checkpointed WAL. Five speakers went 2.4–6.4 s out and the group did not recover for four and a half minutes (§7.1). The route handlers serving heating diagnostics, thermal fits and sizing were doing the same thing over multi-day scans. The rule generalises: **anything reaching DuckDB from a coroutine belongs in `asyncio.to_thread`**, and a synchronous helper that might be called from the loop should refuse to query there rather than block — `heating_controller` now pre-warms its cache off-thread and its loop-side path serves the cache or reports unknown. A stall of this kind is invisible from the audio code, and no correction the ladder possesses distinguishes it from a device fault.
- **`sync_db` statements run on a `cursor()`, never on the cached connection.** A `DuckDBPyConnection` holds one result set; two threads issuing statements on the same connection object hand each other their results, silently and without raising. A cursor is a separate connection over the same database instance, so the file lock stays where it belongs.
- **`_Ring` has no lock.** Reads and writes are safe only because both happen on the loop. Moving either off-thread requires adding one first.
- **Cast connection callbacks arrive on pychromecast's socket thread.** `_ConnWatch` therefore only assigns plain fields; it records the reconnect and lets the monitor decide what to do about it.

- **The resampler is split across the thread boundary, and the split is where it is for a reason.** `window()` reads the delay line and must stay on the loop, because the ring is unlocked. `render()` is pure arithmetic over the private copy `window()` returns, and runs on a worker via `asyncio.to_thread` — the Rust filter releases the GIL, so this genuinely parallelises rather than merely moving the block.

  The await is also the loop's only *guaranteed* suspension point per block. The generator sleeps only when it is more than `STREAM_AHEAD_S` ahead, so a device refilling hard after a reconnect holds `ahead` under that threshold indefinitely and, without an await in the body, produces block after block while every other task on the loop starves. Observed before the split: a 20 s stall with the sampled stack inside `interp_block`, taking the whole application down with it — the health endpoint stopped answering and the tunnel reported TLS handshake timeouts against the origin.

  Of a 0.2 s block at 44.1 kHz stereo, the window read is 0.21 ms and the filter 0.68 ms on the Rust backend, so 77% of the work leaves the loop — 95% on the numpy fallback, where the filter is 3.60 ms. Simulating five devices producing flat out, worst-case loop scheduling delay falls from 136 ms to 1.8 ms (Rust) and from 702 ms to 0.9 ms (numpy). Wall-clock time for the same work also falls, roughly 2–3×, because the Rust filter releases the GIL and the renders genuinely run in parallel rather than merely being deferred.

  Because the loop can now run between the window read and the filter, a correction may land mid-render. The generator compares the position it started from against the position on return and discards the block if they differ: rendering from a superseded position would play a block of pre-jump audio *after* the jump. Dropping it costs nothing, since the next block is rendered from the corrected position.

#### A.2 The Delay Line

- **The delay line is addressed by absolute sample position.** This is the property the whole engine rests on: a jump, a trim, or a late-joining device is a matter of choosing a different index, with no state to unwind.
- **Resamplers must be stateless.** A stateful backend holds filter memory and buffered output, which means position book-keeping has to use the figure it *reports* consuming rather than the figure commanded, a deliberate jump has to clear the instance or filter memory smears both sides of the discontinuity together, and its internal delay is a constant addition to θ. The `libsoxr` backend was removed for the ownership problems this caused, not for its accuracy (§4.2).
- **`read_grid` must return a fresh array, never a view.** The calibration chirp is mixed in with `+=`; a view would write the chirp permanently into the shared timeline and every other device would play it too.
- **No reader may be moved past the write head.** The delay line is bounded on both sides: `earliest_sample()` is the floor, `latest_sample()` the ceiling, and a jump must be clamped to both. On a live source the write head is the live edge, so a device knocked far behind — an assistant notification or an incoming call takes the receiver for ten or fifteen seconds — produces an error larger than the buffer ahead of it. Jumping by the measured error then lands the reader past the head, and because the source commits silence up to the furthest-ahead reader so every device hears the same thing, **one runaway reader silences the entire zone** until the session is restarted. The ceiling is the only thing standing between a single interrupted speaker and a dead group.

  **The ceiling is not the write head; it is the head less the margin the source keeps above its furthest reader.** Clamping to `latest_sample()` itself satisfies the letter of the rule and still produces the failure it exists to prevent, because the source reworks and gap-fills against `reader + XFADE_READER_MARGIN_S`: a reader seated on the head is *inside* that window and overtakes it on the next block. The silence committed to cover the overtake then pushes `ring.end` past the write-head throttle's ceiling, which stalls the decoder — and the two form a latch, the head dragged forward by the reader while the reader keeps pace with the head. Measured in a live five-device session on 2026-08-05: three separate episodes, the longest an unbroken hour at 93% silence, each beginning at a jump clamped to the head and ending only when an unrelated re-LOAD happened to re-seat the reader. The second bound is the play point, since a reader ahead of it has no decoded headroom left and the next decoder hiccup puts it past the head regardless of the guard.

  **A clamped jump must move nothing at all.** The partial move was long taken to be a free down payment on the correction — it is not, because it is precisely the motion that walks the reader into the guard band, and it cannot close the offset anyway. Moving nothing leaves one device out of alignment for another cycle, which is a fault local to that device and audible only in that room; the alternative silences every room. The shortfall is therefore the trigger for the ladder's last rung — a forced **receiver re-LOAD**, the one correction that drops the device's own buffer instead of moving ours. It is rate-limited per device (`STREAM_RELOAD_MIN_INTERVAL_S`), because a LOAD costs several seconds of re-acquisition and one that did not take will not take three times over; losing a reload to that limit is now survivable, where before it left the reader stranded on the head with no way back.

  **This failure is invisible to the correction ladder, which is why it ran for hours.** The reader is what runs away, and the device's own buffer absorbs the step that put it there, so the offending device goes on reporting a healthy on-target lag — ±10 ms, locked, for the entire episode — while every speaker in the zone plays silence. No error signal the loop possesses distinguishes this state from a working one. The health of a zone is a property of the *source*: the gap counter in `stats()`, not the per-device error.

  **A re-LOAD restarts the device's reported media time at zero, so the reader must be re-seated with it.** The lag reading is `(start_pos + shift) / RATE + reported_time`; leaving either term at its mid-session value makes every subsequent measurement wrong by however far the session had already moved, and the ladder then corrects confidently against a false error. Seating zeroes `shift` and re-bases `start_pos`, which is why the seat is one function shared with session start rather than two that can drift apart.
- **The content at a sample index must be final before any reader reaches it.** Rework in place (the crossfade splice, §4.1a) is the only thing that rewrites resident samples, and it is bounded by the *furthest-ahead reader's actual position*, which the engine supplies — not by a notional play point, which no device occupies and which does not track the per-device correction motion. Rewriting an index after some devices have read it and before others have splits the group across two different timelines, and is heard as a skew between the speakers rather than as a fault in the fade.
- **The write head may not park for longer than the headroom.** A reader that passes `ring.end` is zero-filled, and because readers reach a given index seconds apart, a stall at the seam silences the device with the least pre-compensation — the highest-latency one — while the rest hear the audio normally. Anything that holds audio back at a seam (URL resolve, decoder start, overlap collection) spends that budget and must be re-tested against it per block.

#### A.3 Measurement

- **A reported position is used only if the device says `PLAYING` and the report is younger than `STREAM_STATUS_MAX_AGE_S`.** `adjusted_current_time` credits the device with having played continuously since `last_updated`, which holds while the report is fresh and fails exactly when it matters: a rebuffering device is reported as still playing, and when the next report lands the position snaps back by the whole gap — arriving as a step of hundreds of milliseconds that looks like a measurement. Refusing to extrapolate past the bound turns that into a missing sample, which the ladder already handles.
- **An unusable timestamp means the age is unknown, so the reading is used.** Blinding the sensor on a timestamp the code failed to parse would stop correction on every device at once and look merely quiet.
- **The median of the per-poll reads must be the true median**, not `sorted(xs)[len(xs) // 2]`, which returns the upper of the two middle values on an even-length list and biases the sensor toward reporting devices as behind.

#### A.4 The Correction Ladder

Beyond the ladder itself (§7.1):

- **Decisions are made on the residual** — the 3-poll median minus the slew already scheduled — so work in flight is never re-corrected.
- **A slew may never own more than the step threshold.** An unbounded slew drives the residual to zero and permanently disarms the rung above it (§7.1).
- **A step past first lock needs the whole window**: three readings, each beyond the threshold, all of one sign. A step also clears the error history, so allowing a decision on the two readings that follow makes each step lower the bar for the next.
- **A clamped step is not a step.** Cooldown, history reset, slew cancellation and the resync counter all belong to a discontinuity that reached the device; a step the reader ceiling reduced to zero produced none of that, and charging it anyway blinds the sensor for a target-lag while the rungs above wait on readings that cannot arrive (§7.1).
- **Escalation is about capability, not severity.** A reload converges only inside `max_precomp − precomp_i − trim_i`; past a couple of attempts it is re-creating the fault, and the group must be re-aligned instead. A re-align denied by its own floor waits — it must never fall back to another reload.
- **A re-align must not teach the model.** Lags measured while every receiver refills at once are re-acquisition figures, and writing them to `lag_s` seeds every future session's pre-compensation with the incident (§7.2).
- **The drift fit must never see our own corrections.** Deliberate timeline motion — rate term, drained slew, jumps — is accumulated and added back onto the measured lag before fitting, so the fit sees the device's free-running clock. Feeding corrections back into the fit is what railed the previous PLL.
- **A settled rate only counts as evidence if the session ran long enough for the fit to update.** Otherwise the recorded rate is the seed it was given, re-recording old junk as fresh evidence.
- **The trim is latched for the session.** The measurement subtracts the trim so the loop cannot fight it, which holds only while the value subtracted is the value baked into the timeline. Re-reading it means a model-trim write learned from another device of the same model silently changes this device's subtracted term with no matching move of its timeline — the next poll reads a step of the full delta and the monitor hard-resyncs real audio to "correct" it.
- **After a control-socket reset, hold corrections for `STREAM_RECONNECT_GRACE_S` and clear the history.** The control socket is a separate connection from the audio fetch and drops on its own; across the reset the device keeps answering, but the positions it reports alternate between correct and seconds-adrift until it settles. The median filter must not straddle the reset, or it manufactures a step that was never played.

#### A.5 Stream Lifecycle

- **The newest fetch of a device's URL wins.** A Cast device re-fetches on buffer restarts and seeks — observed twice within 300 ms at session start — and without a generation counter both generators run, both advance the position, and the device is served two interleaved halves of the timeline.
- **Only the live fetch owns `connected`.** A superseded generator finishing after its replacement opened must not mark the device disconnected underneath it.
- **A display feed must never be able to take the session down with it.**

---

### Appendix B. Glossary

- **PTS** — presentation timestamp: "play this sample at time T on the reference clock."
- **ASRC** — asynchronous sample-rate converter; a resampler with a continuously adjustable ratio, used for ppm-level rate discipline.
- **ppm** — parts per million of rate error; 50 ppm ≈ 3 ms of drift per minute.
- **GCC-PHAT** — generalised cross-correlation with phase transform; time-delay estimation robust to reverberation.
- **Multizone** — Google's private leader/follower group-playback protocol, coordination namespace `urn:x-cast:com.google.cast.multizone`.

## RSSI presence zones

`modules/zones.py` does per-device RSSI-to-coordinator presence detection. This
design supersedes the earlier pair-link RSSI model.

- Each zone holds a set of device IEEEs.
- For every frame the coordinator receives from a zone device, one `(rssi, lqi)`
  sample is recorded against that device.
- **Calibration is explicit**: the user triggers it once, with the room empty. A
  baseline (trimmed mean + σ) is computed per device from that window.
- Evaluation compares smoothed current RSSI against baseline in σ units.
- **Aggressiveness** — the per-device σ threshold multiplier — is only exposed
  for mains-fed (Router role) devices. End devices contribute weak "evidence"
  weight at the default threshold, because their sample cadence is dictated by
  their own wake cycle.
- A zone is OCCUPIED when the weighted sum of triggered devices crosses
  `min_devices_triggered`, and clears after `clear_delay` of stability.

> `zones` / `config/zones.yaml` are RSSI presence-detection zones, **not** rooms.
> See [chambers](frames.md#chambers).
