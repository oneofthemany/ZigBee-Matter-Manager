/**
 * deploy-animation.js
 * ZigBee Matter Manager — Back to the Future deploy animation
 *
 * Phase 1 (0–45%):  Bee cruises across left half with gentle particles
 * Phase 2 (45–50%): Bee accelerates violently, screen shakes, sparks intensify
 * Phase 3 (50%):    FLASH — bee vanishes at midpoint (88 MPH!)
 * Phase 4 (50–95%): Twin fire trails streak across the rest of the screen
 * Phase 5 (95–100%):Trails fade, "DEPLOYING... 88 MPH" text
 */

(function () {

  /* ------------------------------------------------------------------ */
  /*  Inject overlay HTML once                                            */
  /* ------------------------------------------------------------------ */
  function ensureOverlay() {
    if (document.getElementById('bee-overlay')) return;

    const style = document.createElement('style');
    style.textContent = `
      #bee-overlay {
        position:fixed;top:0;left:0;width:100vw;height:100vh;
        pointer-events:none;z-index:99999;overflow:hidden;display:none;
      }
      #bee-flash {
        position:absolute;top:0;left:0;width:100%;height:100%;
        background:white;opacity:0;
      }
      #bee-msg {
        display:none;
        position:absolute;top:42%;left:50%;
        //transform:translate(-50%,-50%) scale(0);
        //font-family:'Courier New',monospace;font-size:2rem;font-weight:bold;
        //color:#f5a623;white-space:nowrap;opacity:0;letter-spacing:4px;
        //text-shadow:0 0 30px #ff8800, 0 0 60px #ff4400;
      }
      #bee-speed {
        display:none;
        position:absolute;top:52%;left:50%;
        //transform:translate(-50%,-50%);
        //font-family:'Courier New',monospace;font-size:1rem;
        //color:#4af;white-space:nowrap;opacity:0;letter-spacing:2px;
        //text-shadow:0 0 15px #4af;
      }
    `;
    document.head.appendChild(style);

    const div = document.createElement('div');
    div.id = 'bee-overlay';
    div.innerHTML = `
      <canvas id="bee-canvas"></canvas>
      <div id="bee-flash"></div>
      <div id="bee-msg">DEPLOYING... 88 MPH</div>
      <div id="bee-speed"></div>
    `;
    document.body.appendChild(div);
  }

  /* ------------------------------------------------------------------ */
  /*  Particle system                                                     */
  /* ------------------------------------------------------------------ */
  const particles = [];

  function spawnCruiseParticles(x, y, count) {
    for (let i = 0; i < count; i++) {
      particles.push({
        x: x - 20 - Math.random() * 15,
        y: y + (Math.random() - 0.5) * 20,
        vx: -(1 + Math.random() * 2),
        vy: (Math.random() - 0.5) * 1,
        life: 1,
        decay: 0.02 + Math.random() * 0.02,
        size: 3 + Math.random() * 5,
        type: Math.random() < 0.6 ? 'ember' : 'spark'
      });
    }
  }

  function spawnAccelParticles(x, y, count) {
    for (let i = 0; i < count; i++) {
      particles.push({
        x: x - 30 - Math.random() * 10,
        y: y + (Math.random() - 0.5) * 16,
        vx: -(4 + Math.random() * 8),
        vy: (Math.random() - 0.5) * 3,
        life: 1,
        decay: 0.03 + Math.random() * 0.04,
        size: 4 + Math.random() * 10,
        type: Math.random() < 0.4 ? 'spark' : 'flame'
      });
    }
  }

  function spawnFlashBurst(x, y) {
    for (let i = 0; i < 80; i++) {
      const angle = Math.random() * Math.PI * 2;
      const speed = 2 + Math.random() * 12;
      particles.push({
        x: x,
        y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        life: 1,
        decay: 0.015 + Math.random() * 0.025,
        size: 2 + Math.random() * 8,
        type: Math.random() < 0.3 ? 'spark' : Math.random() < 0.5 ? 'blue' : 'flame'
      });
    }
  }

  function updateAndDrawParticles(ctx) {
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.x += p.vx;
      p.y += p.vy;
      p.vy += (Math.random() - 0.52) * 0.2;
      p.vx *= 0.99;
      p.life -= p.decay;
      if (p.life <= 0) { particles.splice(i, 1); continue; }

      const a = p.life;

      if (p.type === 'spark') {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size * 0.3 * a, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,240,100,${a})`;
        ctx.fill();
      } else if (p.type === 'blue') {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size * 0.35 * a, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(100,180,255,${a * 0.9})`;
        ctx.fill();
      } else if (p.type === 'ember') {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size * 0.2 * a, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,180,50,${a * 0.7})`;
        ctx.fill();
      } else {
        const sz = p.size * (0.4 + 0.6 * a);
        const g = p.life > 0.6 ? Math.floor(180 * a) : Math.floor(80 * a);
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(Math.PI * 0.5 + (Math.random() - 0.5) * 0.3);
        ctx.beginPath();
        ctx.moveTo(0, -sz);
        ctx.bezierCurveTo(sz * 0.6, -sz * 0.3, sz * 0.5, sz * 0.4, 0, sz * 0.5);
        ctx.bezierCurveTo(-sz * 0.5, sz * 0.4, -sz * 0.6, -sz * 0.3, 0, -sz);
        ctx.closePath();
        ctx.fillStyle = `rgba(255,${g},0,${a * 0.85})`;
        ctx.fill();
        ctx.restore();
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Twin fire trails (post-vanish)                                      */
  /* ------------------------------------------------------------------ */
  function drawFireTrails(ctx, startX, progress, t, W, H) {
    // Two parallel trails like DeLorean tyre marks
    const trailProgress = (progress - 0.5) / 0.45;
    const trailEndX = startX + trailProgress * (W - startX + 100);
    const cy = H * 0.48;
    const trailAlpha = trailProgress < 0.8 ? 1 : 1 - (trailProgress - 0.8) / 0.2;

    const trails = [
      { y: cy - 8, width: 6 },
      { y: cy + 18, width: 6 },
    ];

    trails.forEach(function (trail) {
      // Main flame trail
      const grad = ctx.createLinearGradient(startX, trail.y, trailEndX, trail.y);
      grad.addColorStop(0, `rgba(255,100,0,0)`);
      grad.addColorStop(0.3, `rgba(255,140,0,${0.4 * trailAlpha})`);
      grad.addColorStop(0.7, `rgba(255,200,50,${0.7 * trailAlpha})`);
      grad.addColorStop(0.9, `rgba(255,255,200,${0.9 * trailAlpha})`);
      grad.addColorStop(1, `rgba(255,255,255,${trailAlpha})`);

      // Wavy trail with turbulence
      ctx.beginPath();
      const steps = 60;
      for (let s = 0; s <= steps; s++) {
        const frac = s / steps;
        const px = startX + frac * (trailEndX - startX);
        const wave = Math.sin(frac * 8 + t * 0.008) * 2 * (1 - frac);
        const py = trail.y + wave - trail.width * 0.5;
        if (s === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      for (let s = steps; s >= 0; s--) {
        const frac = s / steps;
        const px = startX + frac * (trailEndX - startX);
        const wave = Math.sin(frac * 8 + t * 0.008 + 1) * 2 * (1 - frac);
        const py = trail.y + wave + trail.width * 0.5;
        ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // White-hot tip glow
      if (trailAlpha > 0.3) {
        const tipGlow = ctx.createRadialGradient(
          trailEndX, trail.y, 0,
          trailEndX, trail.y, 25
        );
        tipGlow.addColorStop(0, `rgba(255,255,255,${0.8 * trailAlpha})`);
        tipGlow.addColorStop(0.3, `rgba(255,200,50,${0.5 * trailAlpha})`);
        tipGlow.addColorStop(1, `rgba(255,100,0,0)`);
        ctx.beginPath();
        ctx.arc(trailEndX, trail.y, 25, 0, Math.PI * 2);
        ctx.fillStyle = tipGlow;
        ctx.fill();
      }

      // Spawn trail particles at the tip
      if (trailAlpha > 0.2 && Math.random() < 0.6) {
        particles.push({
          x: trailEndX + (Math.random() - 0.5) * 10,
          y: trail.y + (Math.random() - 0.5) * 12,
          vx: -(1 + Math.random() * 3),
          vy: (Math.random() - 0.5) * 2,
          life: 1,
          decay: 0.04 + Math.random() * 0.04,
          size: 3 + Math.random() * 6,
          type: Math.random() < 0.3 ? 'spark' : 'flame'
        });
      }
    });

    // Blue temporal glow between the trails (fading)
    if (trailAlpha > 0.1) {
      const blueGrad = ctx.createLinearGradient(startX, cy, trailEndX, cy);
      blueGrad.addColorStop(0, `rgba(100,180,255,0)`);
      blueGrad.addColorStop(0.5, `rgba(100,180,255,${0.15 * trailAlpha})`);
      blueGrad.addColorStop(1, `rgba(100,180,255,${0.25 * trailAlpha})`);
      ctx.beginPath();
      ctx.rect(startX, cy - 6, trailEndX - startX, 22);
      ctx.fillStyle = blueGrad;
      ctx.fill();
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Exhaust flames (pre-vanish, behind bee)                             */
  /* ------------------------------------------------------------------ */
  function drawExhaust(ctx, beeX, beeY, t, intensity) {
    const ox = beeX - 32;
    const oy = beeY + 12;
    const maxLen = Math.min(ox, 120 * intensity);
    if (maxLen < 5) return;

    const tongues = [
      { spread: 0,   lenMul: 1,    w: 14 * intensity, r: 255, g: 240, b: 80  },
      { spread: -6,  lenMul: 0.7,  w: 9 * intensity,  r: 255, g: 180, b: 0   },
      { spread: 6,   lenMul: 0.7,  w: 9 * intensity,  r: 255, g: 180, b: 0   },
      { spread: -12, lenMul: 0.4,  w: 5 * intensity,  r: 255, g: 120, b: 0   },
      { spread: 12,  lenMul: 0.4,  w: 5 * intensity,  r: 255, g: 120, b: 0   },
    ];

    tongues.forEach(function (tng) {
      const len = maxLen * tng.lenMul;
      const tipX = ox - len;
      const tipY = oy + tng.spread;
      const wave = Math.sin(t * 0.01 + tng.spread * 0.2) * 6 * intensity;
      const midY = oy + tng.spread * 0.5 + wave;
      const steps = 30;

      const grad = ctx.createLinearGradient(ox, oy, tipX, tipY);
      grad.addColorStop(0, `rgba(${tng.r},${tng.g},${tng.b},${0.9 * intensity})`);
      grad.addColorStop(0.5, `rgba(${tng.r},${Math.floor(tng.g * 0.5)},0,${0.5 * intensity})`);
      grad.addColorStop(1, `rgba(${tng.r},${Math.floor(tng.g * 0.3)},0,0)`);

      ctx.beginPath();
      for (let s = 0; s <= steps; s++) {
        const frac = s / steps;
        const px = ox - frac * len;
        const py = midY + (oy - midY) * (1 - frac)
          + Math.sin(frac * Math.PI * 3 + t * 0.012) * 3 * (1 - frac);
        const halfW = (tng.w * 0.5) * (1 - frac);
        if (s === 0) ctx.moveTo(px, py - halfW);
        else ctx.lineTo(px, py - halfW);
      }
      for (let s = steps; s >= 0; s--) {
        const frac = s / steps;
        const px = ox - frac * len;
        const py = midY + (oy - midY) * (1 - frac)
          + Math.sin(frac * Math.PI * 3 + t * 0.012) * 3 * (1 - frac);
        const halfW = (tng.w * 0.5) * (1 - frac);
        ctx.lineTo(px, py + halfW);
      }
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    });

    // White-hot core
    if (intensity > 0.3) {
      const coreGrad = ctx.createLinearGradient(ox, oy, ox - maxLen * 0.15, oy);
      coreGrad.addColorStop(0, `rgba(255,255,220,${0.95 * intensity})`);
      coreGrad.addColorStop(0.5, `rgba(255,240,100,${0.5 * intensity})`);
      coreGrad.addColorStop(1, 'rgba(255,200,0,0)');
      ctx.beginPath();
      ctx.ellipse(ox - maxLen * 0.06, oy, maxLen * 0.12, 4, 0, 0, Math.PI * 2);
      ctx.fillStyle = coreGrad;
      ctx.fill();
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Bee drawing                                                         */
  /* ------------------------------------------------------------------ */
  function drawBee(ctx, cx, cy, t, alpha) {
    if (alpha <= 0) return;
    const wingBeat = Math.sin(t * 0.25) * 0.3;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(cx, cy);

    // Top wing
    ctx.save();
    ctx.rotate(-0.2 + wingBeat);
    ctx.beginPath();
    ctx.moveTo(0, -10);
    ctx.bezierCurveTo(-10, -55, -65, -60, -80, -25);
    ctx.bezierCurveTo(-75, -5, -30, 0, 0, -10);
    ctx.closePath();
    ctx.fillStyle = 'rgba(230,220,200,0.55)';
    ctx.strokeStyle = 'rgba(160,120,80,0.5)';
    ctx.lineWidth = 0.8;
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-5, -12);
    ctx.bezierCurveTo(-25, -40, -55, -48, -75, -28);
    ctx.moveTo(-5, -14);
    ctx.bezierCurveTo(-20, -30, -45, -32, -70, -20);
    ctx.moveTo(-15, -22);
    ctx.lineTo(-55, -38);
    ctx.moveTo(-30, -28);
    ctx.lineTo(-60, -28);
    ctx.strokeStyle = 'rgba(140,100,60,0.35)';
    ctx.lineWidth = 0.6;
    ctx.stroke();
    ctx.restore();

    // Bottom wing
    ctx.save();
    ctx.rotate(0.15 + wingBeat * 0.7);
    ctx.beginPath();
    ctx.moveTo(-5, 10);
    ctx.bezierCurveTo(-15, -20, -60, -22, -68, 5);
    ctx.bezierCurveTo(-62, 22, -25, 20, -5, 10);
    ctx.closePath();
    ctx.fillStyle = 'rgba(220,210,185,0.45)';
    ctx.strokeStyle = 'rgba(150,110,70,0.4)';
    ctx.lineWidth = 0.7;
    ctx.fill();
    ctx.stroke();
    ctx.restore();

    // Abdomen
    ctx.beginPath();
    ctx.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
    ctx.fillStyle = '#e8920a';
    ctx.fill();

    var stripeClip = new Path2D();
    stripeClip.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
    ctx.save();
    ctx.clip(stripeClip);
    [[-66, 12], [-50, 12], [-34, 12], [-18, 12], [-2, 12]].forEach(function (pos, idx) {
      ctx.beginPath();
      ctx.rect(pos[0], 2, 10, 24);
      ctx.fillStyle = idx % 2 === 0 ? '#1a1005' : '#e8920a';
      ctx.fill();
    });
    ctx.beginPath();
    ctx.ellipse(-28, 8, 30, 10, -0.15, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,200,80,0.12)';
    ctx.fill();
    ctx.restore();

    ctx.beginPath();
    ctx.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
    ctx.strokeStyle = '#3a2000';
    ctx.lineWidth = 1;
    ctx.stroke();

    // Stinger
    ctx.beginPath();
    ctx.moveTo(-64, 14);
    ctx.lineTo(-76, 12);
    ctx.lineTo(-66, 20);
    ctx.closePath();
    ctx.fillStyle = '#2a1800';
    ctx.fill();

    // Thorax
    ctx.beginPath();
    ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
    ctx.fillStyle = '#c07808';
    ctx.fill();
    for (var f = 0; f < 10; f++) {
      var fx = 8 + Math.cos(f * 0.63) * 14;
      var fy = 4 + Math.sin(f * 0.63) * 11;
      ctx.beginPath();
      ctx.moveTo(fx, fy);
      ctx.lineTo(fx + Math.cos(f * 0.63) * 4, fy + Math.sin(f * 0.63) * 4);
      ctx.strokeStyle = 'rgba(240,160,20,0.4)';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
    ctx.strokeStyle = '#3a2000';
    ctx.lineWidth = 0.8;
    ctx.stroke();

    // Head
    ctx.beginPath();
    ctx.ellipse(28, -2, 17, 15, 0, 0, Math.PI * 2);
    ctx.fillStyle = '#d08010';
    ctx.fill();
    ctx.strokeStyle = '#3a2000';
    ctx.lineWidth = 0.8;
    ctx.stroke();

    // Compound eye
    ctx.beginPath();
    ctx.ellipse(36, -4, 10, 12, 0.3, 0, Math.PI * 2);
    ctx.fillStyle = '#0a0a0a';
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(33, -8, 3, 4, 0.5, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(80,120,180,0.4)';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(32, -9, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(200,220,255,0.3)';
    ctx.fill();

    // Antennae
    ctx.beginPath();
    ctx.moveTo(34, -14);
    ctx.bezierCurveTo(38, -30, 50, -35, 52, -28);
    ctx.strokeStyle = '#1a0a00';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(52, -27, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#1a0a00';
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(30, -16);
    ctx.bezierCurveTo(32, -28, 40, -30, 38, -22);
    ctx.strokeStyle = '#1a0a00';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(38, -21, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#1a0a00';
    ctx.fill();

    // Legs
    [[10, 16, 20, 30], [0, 18, 8, 32], [-10, 16, -4, 28]].forEach(function (leg) {
      ctx.beginPath();
      ctx.moveTo(leg[0], leg[1]);
      ctx.lineTo(leg[2], leg[3]);
      ctx.strokeStyle = '#1a0a00';
      ctx.lineWidth = 1.2;
      ctx.stroke();
    });

    ctx.restore();
  }

  /* ------------------------------------------------------------------ */
  /*  Bee skeleton — the electrocution X-ray gag                          */
  /*                                                                      */
  /*  Same geometry as drawBee (abdomen/thorax/head/wings/legs in the     */
  /*  same places) but rendered as a dark translucent silhouette with     */
  /*  glowing bones: spine + ribs where the stripes were, a skull with    */
  /*  an empty eye socket, jointed leg bones and wing veins.              */
  /* ------------------------------------------------------------------ */
  function drawBeeSkeleton(ctx, cx, cy, t, alpha) {
    if (alpha <= 0) return;
    var wingBeat = Math.sin(t * 0.25) * 0.3;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(cx, cy);

    var BODY = 'rgba(8,12,24,0.88)';
    var RIM = 'rgba(160,220,255,0.7)';
    var BONE = 'rgba(230,250,255,0.95)';

    // Top wing — faint membrane, veins as glowing wing bones
    ctx.save();
    ctx.rotate(-0.2 + wingBeat);
    ctx.beginPath();
    ctx.moveTo(0, -10);
    ctx.bezierCurveTo(-10, -55, -65, -60, -80, -25);
    ctx.bezierCurveTo(-75, -5, -30, 0, 0, -10);
    ctx.closePath();
    ctx.fillStyle = 'rgba(140,200,255,0.10)';
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(-5, -12);
    ctx.bezierCurveTo(-25, -40, -55, -48, -75, -28);
    ctx.moveTo(-5, -14);
    ctx.bezierCurveTo(-20, -30, -45, -32, -70, -20);
    ctx.moveTo(-15, -22);
    ctx.lineTo(-55, -38);
    ctx.strokeStyle = BONE;
    ctx.lineWidth = 1.2;
    ctx.shadowColor = '#aef';
    ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.restore();

    ctx.shadowColor = '#aef';
    ctx.shadowBlur = 10;

    // Abdomen silhouette + spine + ribs (where the stripes were)
    ctx.beginPath();
    ctx.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
    ctx.fillStyle = BODY;
    ctx.fill();
    ctx.strokeStyle = RIM;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-64, 17);
    ctx.quadraticCurveTo(-28, 6, 6, 8);
    ctx.strokeStyle = BONE;
    ctx.lineWidth = 2.2;
    ctx.stroke();
    [[-58, 15], [-44, 14], [-30, 13], [-16, 12], [-3, 11]].forEach(function (r, i) {
      var rl = 14 - Math.abs(i - 2) * 2.5;
      ctx.beginPath();
      ctx.moveTo(r[0], r[1] - rl);
      ctx.quadraticCurveTo(r[0] - 5, r[1], r[0], r[1] + rl);
      ctx.lineWidth = 1.6;
      ctx.stroke();
    });

    // Stinger bone
    ctx.beginPath();
    ctx.moveTo(-64, 14);
    ctx.lineTo(-76, 12);
    ctx.lineTo(-66, 20);
    ctx.closePath();
    ctx.strokeStyle = BONE;
    ctx.lineWidth = 1.2;
    ctx.stroke();

    // Thorax silhouette + crossed strut bones
    ctx.beginPath();
    ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
    ctx.fillStyle = BODY;
    ctx.fill();
    ctx.strokeStyle = RIM;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-4, -8);
    ctx.lineTo(20, 14);
    ctx.moveTo(20, -8);
    ctx.lineTo(-4, 14);
    ctx.strokeStyle = BONE;
    ctx.lineWidth = 1.6;
    ctx.stroke();

    // Skull: head silhouette, empty eye socket, mandible line
    ctx.beginPath();
    ctx.ellipse(28, -2, 17, 15, 0, 0, Math.PI * 2);
    ctx.fillStyle = BODY;
    ctx.fill();
    ctx.strokeStyle = BONE;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.beginPath();
    ctx.ellipse(36, -4, 9, 11, 0.3, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(0,0,0,0.92)';
    ctx.fill();
    ctx.strokeStyle = RIM;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(16, 8);
    ctx.quadraticCurveTo(28, 13, 40, 8);
    ctx.strokeStyle = BONE;
    ctx.lineWidth = 1.4;
    ctx.stroke();

    // Antenna bones with joint knobs
    [[34, -14, 38, -30, 50, -35, 52, -28], [30, -16, 32, -28, 40, -30, 38, -22]]
      .forEach(function (a) {
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.bezierCurveTo(a[2], a[3], a[4], a[5], a[6], a[7]);
        ctx.strokeStyle = BONE;
        ctx.lineWidth = 1.3;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(a[6], a[7] + 1, 2.2, 0, Math.PI * 2);
        ctx.stroke();
      });

    // Leg bones with joints
    [[10, 16, 20, 30], [0, 18, 8, 32], [-10, 16, -4, 28]].forEach(function (leg) {
      ctx.beginPath();
      ctx.moveTo(leg[0], leg[1]);
      ctx.lineTo(leg[2], leg[3]);
      ctx.strokeStyle = BONE;
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(leg[2], leg[3], 1.8, 0, Math.PI * 2);
      ctx.stroke();
    });

    ctx.restore();
  }

  /* ------------------------------------------------------------------ */
  /*  Speedometer HUD                                                     */
  /* ------------------------------------------------------------------ */
  function drawSpeedometer(ctx, speed, W, H) {
    var mph = Math.floor(speed);
    var text = mph + ' MPH';

    ctx.save();
    ctx.font = 'bold 18px "Courier New", monospace';
    ctx.textAlign = 'right';

    // Glow gets more intense near 88
    var intensity = Math.min(speed / 88, 1);
    var r = Math.floor(100 + 155 * intensity);
    var g = Math.floor(255 - 75 * intensity);
    var b = Math.floor(255 - 200 * intensity);

    ctx.shadowColor = speed >= 85 ? '#ff4400' : '#4af';
    ctx.shadowBlur = 10 + intensity * 20;
    ctx.fillStyle = 'rgba(' + r + ',' + g + ',' + b + ',' + (0.7 + intensity * 0.3) + ')';
    ctx.fillText(text, W - 30, H * 0.48 - 40);

    // Flash red at 88
    if (speed >= 86 && speed <= 88) {
      ctx.shadowColor = '#ff0000';
      ctx.shadowBlur = 30;
      ctx.fillStyle = 'rgba(255,50,50,0.9)';
      ctx.fillText(text, W - 30, H * 0.48 - 40);
    }

    ctx.restore();
  }

  /* ------------------------------------------------------------------ */
  /*  Main animation                                                      */
  /* ------------------------------------------------------------------ */
  window.showDeloreanAnimation = function () {
    ensureOverlay();

    var overlay = document.getElementById('bee-overlay');
    var canvas = document.getElementById('bee-canvas');
    var flash = document.getElementById('bee-flash');
    var msg = document.getElementById('bee-msg');
    var speedEl = document.getElementById('bee-speed');
    var ctx = canvas.getContext('2d');

    var W = canvas.width = window.innerWidth;
    var H = canvas.height = window.innerHeight;
    overlay.style.display = 'block';
    particles.length = 0;

    var startTime = null;
    var duration = 4200;      // Slightly longer for dramatic effect
    var vanishX = W * 0.5;    // Midpoint
    var vanishY = H * 0.48;
    var hasFlashed = false;

    function animate(ts) {
      if (!startTime) startTime = ts;
      var elapsed = ts - startTime;
      var progress = Math.min(elapsed / duration, 1);

      ctx.clearRect(0, 0, W, H);

      // ── Phase timing ──
      var beeVisible = progress < 0.52;
      var accelPhase = progress > 0.35 && progress < 0.52;
      var trailPhase = progress >= 0.50;

      // ── Bee position (eased — slow cruise then violent acceleration) ──
      var beeX, beeY, speed;

      if (progress < 0.35) {
        // Gentle cruise — linear across left portion
        var cruiseFrac = progress / 0.35;
        beeX = -80 + cruiseFrac * (vanishX * 0.7);
        speed = 25 + cruiseFrac * 15;   // 25–40 mph
      } else if (progress < 0.52) {
        // Acceleration phase — exponential ramp to vanish point
        var accelFrac = (progress - 0.35) / 0.17;
        var eased = accelFrac * accelFrac * accelFrac;  // Cubic ease-in
        beeX = -80 + 0.35 / 0.35 * (vanishX * 0.7) + eased * (vanishX * 0.3 + 80);
        speed = 40 + eased * 48;         // 40–88 mph
      } else {
        beeX = vanishX;
        speed = 88;
      }

      beeY = H * 0.48 + Math.sin(elapsed * 0.003) * (beeVisible ? 8 : 0);

      // ── Screen shake during acceleration ──
      if (accelPhase) {
        var shakeIntensity = ((progress - 0.35) / 0.17) * 4;
        ctx.save();
        ctx.translate(
          (Math.random() - 0.5) * shakeIntensity,
          (Math.random() - 0.5) * shakeIntensity
        );
      }

      // ── Blue temporal streaks (visible during cruise + accel) ──
      if (beeVisible && progress > 0.1) {
        var streakAlpha = Math.min((progress - 0.1) / 0.2, 1) * 0.4;
        if (accelPhase) streakAlpha = 0.6;
        for (var si = 0; si < 6; si++) {
          var sy = H * 0.48 + si * 7 - 18;
          var gr = ctx.createLinearGradient(0, sy, beeX - 40, sy);
          gr.addColorStop(0, 'rgba(100,180,255,0)');
          gr.addColorStop(0.7, 'rgba(100,180,255,' + (streakAlpha * 0.3) + ')');
          gr.addColorStop(1, 'rgba(100,180,255,' + streakAlpha + ')');
          ctx.beginPath();
          ctx.strokeStyle = gr;
          ctx.lineWidth = si < 2 ? 1.5 : 0.8;
          ctx.moveTo(0, sy);
          ctx.lineTo(beeX - 40, sy);
          ctx.stroke();
        }
      }

      // ── Exhaust flames (behind bee, intensity grows with speed) ──
      if (beeVisible) {
        var exhaustIntensity = speed < 40 ? 0.3 : Math.min((speed - 40) / 48, 1);
        drawExhaust(ctx, beeX, beeY, elapsed, exhaustIntensity);
      }

      // ── Particles ──
      if (beeVisible && !accelPhase) {
        spawnCruiseParticles(beeX, beeY, 2);
      }
      if (accelPhase) {
        spawnAccelParticles(beeX, beeY, 6);
      }

      // ── Fire trails (post-vanish) ──
      if (trailPhase) {
        drawFireTrails(ctx, vanishX, progress, elapsed, W, H);
      }

      // ── Draw particles (always, they persist across phases) ──
      updateAndDrawParticles(ctx);

      // ── Draw bee ──
      if (beeVisible) {
        var beeAlpha = progress < 0.48 ? 1 : Math.max(0, 1 - (progress - 0.48) / 0.04);
        drawBee(ctx, beeX, beeY, elapsed, beeAlpha);

        // Speed lines get more intense during acceleration
        if (accelPhase) {
          var lineCount = 6;
          for (var li = 0; li < lineCount; li++) {
            var ly = beeY - 15 + li * 6;
            var lineLen = 30 + li * 15 + ((progress - 0.35) / 0.17) * 80;
            var lgr = ctx.createLinearGradient(beeX - lineLen - 20, ly, beeX - 20, ly);
            lgr.addColorStop(0, 'rgba(245,166,35,0)');
            lgr.addColorStop(1, 'rgba(245,166,35,' + (0.5 - li * 0.06) + ')');
            ctx.beginPath();
            ctx.strokeStyle = lgr;
            ctx.lineWidth = 1.5 - li * 0.15;
            ctx.moveTo(beeX - lineLen - 20, ly);
            ctx.lineTo(beeX - 20, ly);
            ctx.stroke();
          }
        }
      }

      // ── Speed readout ──
      if (progress > 0.05 && progress < 0.55) {
        //drawSpeedometer(ctx, speed, W, H);
      }

      // ── THE FLASH (at vanish point) ──
      if (progress >= 0.49 && progress <= 0.56) {
        if (!hasFlashed && progress >= 0.50) {
          hasFlashed = true;
          spawnFlashBurst(vanishX, vanishY);
        }
        var flashProgress = (progress - 0.49) / 0.07;
        var flashAlpha = flashProgress < 0.15
          ? flashProgress / 0.15
          : Math.max(0, 1 - (flashProgress - 0.15) / 0.85);
        flash.style.opacity = (flashAlpha * 0.9).toFixed(3);
      } else {
        flash.style.opacity = '0';
      }

      // ── Undo screen shake ──
      if (accelPhase) {
        ctx.restore();
      }

      // ── Message text ──
      if (progress > 0.58 && progress < 0.92) {
        var mp = (progress - 0.58) / 0.34;
        var sc = mp < 0.15 ? mp / 0.15 : mp > 0.85 ? 1 - (mp - 0.85) / 0.15 : 1;
        msg.style.opacity = sc.toString();
        msg.style.transform = 'translate(-50%,-50%) scale(' + (0.4 + sc * 0.6) + ')';
        speedEl.style.opacity = (sc * 0.7).toString();
        speedEl.textContent = 'TEMPORAL DEPLOYMENT SUCCESSFUL';
      } else {
        msg.style.opacity = '0';
        speedEl.style.opacity = '0';
      }

      if (progress < 1) {
        requestAnimationFrame(animate);
      } else {
        overlay.style.display = 'none';
        particles.length = 0;
      }
    }

    requestAnimationFrame(animate);
  };

  /* ------------------------------------------------------------------ */
  /*  Fork lightning (time-travel overlay)                                */
  /*                                                                      */
  /*  makeForkBolt() generates the geometry once — a jagged main channel  */
  /*  with recursive thinner branches. The overlay keeps each bolt alive  */
  /*  for ~150-280ms and redraws it with a flicker envelope, which is     */
  /*  what makes it read as lightning instead of a one-frame scribble.    */
  /* ------------------------------------------------------------------ */
  function makeForkBolt(x, y, angle, len, width, depth) {
    var paths = [];
    (function walk(x0, y0, ang, l, w, d) {
      var steps = Math.max(4, Math.floor(l / 16));
      var seg = l / steps;
      var pts = [[x0, y0]];
      var px = x0, py = y0;
      for (var i = 0; i < steps; i++) {
        // Mostly small jitter with the occasional hard kink
        ang += (Math.random() - 0.5) * (Math.random() < 0.2 ? 1.4 : 0.35);
        px += Math.cos(ang) * seg;
        py += Math.sin(ang) * seg;
        pts.push([px, py]);
        if (d > 0 && i > 0 && Math.random() < 0.3) {
          walk(px, py,
            ang + (Math.random() < 0.5 ? -0.7 : 0.7) + (Math.random() - 0.5) * 0.4,
            l * (0.3 + Math.random() * 0.3), w * 0.5, d - 1);
        }
      }
      paths.push({ pts: pts, width: w });
    })(x, y, angle, len, width, depth);
    return paths;
  }

  // Return-stroke envelope — real strikes re-flash 2-3 times as the channel
  // discharges again (peaks at 0 / 130 / 280 ms, each with a sharp attack
  // and exponential decay). dt in milliseconds.
  function restrikeEnv(dt) {
    var pulses = [[0, 1], [130, 0.6], [280, 0.45]];
    var v = 0;
    for (var i = 0; i < pulses.length; i++) {
      var d = dt - pulses[i][0];
      if (d >= 0) {
        var a = d < 25 ? d / 25 : Math.exp(-(d - 25) / 90);
        v = Math.max(v, pulses[i][1] * a);
      }
    }
    return v;
  }

  // Brightness of a live bolt at time ts — restrike bolts pulse, ordinary
  // bolts get the simple fast-attack/slow-decay envelope.
  function boltAlpha(bolt, ts) {
    var dt = ts - bolt.born;
    var age = dt / bolt.life;
    if (age >= 1) return 0;
    if (bolt.restrike) return restrikeEnv(dt);
    return age < 0.15 ? age / 0.15 : 1 - (age - 0.15) / 0.85;
  }

  function drawBoltPaths(ctx, paths, alpha) {
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    paths.forEach(function (p) {
      ctx.beginPath();
      ctx.moveTo(p.pts[0][0], p.pts[0][1]);
      for (var j = 1; j < p.pts.length; j++) ctx.lineTo(p.pts[j][0], p.pts[j][1]);
      // Halo pass — wide, dim, blue
      ctx.lineWidth = p.width * 3.5;
      ctx.strokeStyle = 'rgba(110,170,255,' + (0.22 * alpha).toFixed(3) + ')';
      ctx.shadowColor = '#5af';
      ctx.shadowBlur = 18;
      ctx.stroke();
      // Core pass — thin, white-hot (same path, restroked)
      ctx.lineWidth = Math.max(0.6, p.width * 0.9);
      ctx.strokeStyle = 'rgba(240,248,255,' + (0.95 * alpha).toFixed(3) + ')';
      ctx.shadowBlur = 6;
      ctx.stroke();
    });
    ctx.restore();
  }

  /* ------------------------------------------------------------------ */
  /*  Time-travel wait overlay (image swap / upgrade)                     */
  /*                                                                      */
  /*  Unlike showDeloreanAnimation (a 4s one-shot), this runs for as      */
  /*  long as the swap takes: the bee cruises with flame exhaust while a  */
  /*  BTTF time-circuits panel shows the version jump; complete() then    */
  /*  accelerates it to 88 MPH and fires the callback at the white-flash  */
  /*  peak — the natural moment to reload into the new timeline.          */
  /*                                                                      */
  /*  Returns { setStatus(text), complete(onFlash), abort() }.            */
  /* ------------------------------------------------------------------ */
  window.showTimeTravelWait = function (opts) {
    opts = opts || {};
    // Values land in innerHTML — keep them to version-shaped characters.
    var clean = function (v) { return String(v || '?').replace(/[^\w.+-]/g, ''); };
    var present = clean(opts.present);
    var destination = clean(opts.destination);
    var reduced = window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    var old = document.getElementById('tt-overlay');
    if (old) old.remove();

    var style = document.createElement('style');
    style.id = 'tt-style';
    style.textContent = `
      #tt-overlay {
        position:fixed;inset:0;z-index:100000;overflow:hidden;
        background:radial-gradient(ellipse at 50% 60%, #0b1226 0%, #05070f 70%);
        opacity:0;transition:opacity .5s ease;pointer-events:auto;
      }
      #tt-overlay.tt-in { opacity:1; }
      #tt-flash {
        position:absolute;inset:0;background:white;opacity:0;pointer-events:none;
      }
      #tt-circuits {
        position:absolute;left:50%;top:64%;transform:translateX(-50%);
        font-family:'Courier New',monospace;color:#ddd;
        background:rgba(8,10,16,.82);border:1px solid #2a3550;border-radius:8px;
        padding:14px 22px 12px;min-width:min(420px,86vw);
        box-shadow:0 0 40px rgba(80,140,255,.25), inset 0 0 25px rgba(0,0,0,.6);
      }
      .tt-row {
        display:flex;justify-content:space-between;align-items:baseline;
        gap:18px;padding:3px 0;
      }
      .tt-row .lbl { font-size:.62rem;letter-spacing:2px;color:#7a86a0; }
      .tt-row .val {
        font-size:1.15rem;font-weight:bold;letter-spacing:3px;white-space:nowrap;
      }
      .tt-red   .val { color:#ff5030;text-shadow:0 0 12px #ff3000,0 0 30px #a00; }
      .tt-green .val { color:#3aff70;text-shadow:0 0 12px #0f8,0 0 30px #063; }
    `;
    document.head.appendChild(style);

    var div = document.createElement('div');
    div.id = 'tt-overlay';
    div.innerHTML = `
      <canvas id="tt-canvas"></canvas>
      <div id="tt-flash"></div>
      <div id="tt-circuits">
        <div class="tt-row tt-red"><span class="lbl">DESTINATION VERSION</span><span class="val">v${destination}</span></div>
        <div class="tt-row tt-green"><span class="lbl">PRESENT VERSION</span><span class="val">v${present}</span></div>
      </div>
    `;
    document.body.appendChild(div);
    requestAnimationFrame(function () { div.classList.add('tt-in'); });

    var canvas = div.querySelector('#tt-canvas');
    var flash = div.querySelector('#tt-flash');
    var ctx = canvas.getContext('2d');

    // Offscreen scratch for the struck-bee whiteout: draw the bee there,
    // tint it in place with source-atop, then blit — so the flash follows
    // the bee's exact silhouette without touching drawBee itself.
    var beeScratch = document.createElement('canvas');
    beeScratch.width = 220;
    beeScratch.height = 160;
    var beeSctx = beeScratch.getContext('2d');

    function drawBeeTinted(x, y, t, tint) {
      if (tint <= 0.02) { drawBee(ctx, x, y, t, 1); return; }
      beeSctx.clearRect(0, 0, 220, 160);
      drawBee(beeSctx, 110, 80, t, 1);
      beeSctx.save();
      beeSctx.globalCompositeOperation = 'source-atop';
      beeSctx.fillStyle = 'rgba(235,245,255,' + Math.min(tint, 0.92).toFixed(3) + ')';
      beeSctx.fillRect(0, 0, 220, 160);
      beeSctx.restore();
      ctx.drawImage(beeScratch, x - 110, y - 80);
    }

    var W = canvas.width = window.innerWidth;
    var H = canvas.height = window.innerHeight;
    function onResize() {
      W = canvas.width = window.innerWidth;
      H = canvas.height = window.innerHeight;
    }
    window.addEventListener('resize', onResize);

    particles.length = 0;
    var bolts = [];   // live lightning: {paths, born, life}

    var start = performance.now();
    // Timeline:
    //   enter  — bee flies in from the left with small cruise particles
    //   strike — a giant fork bolt hits it dead-centre
    //   blast  — it accelerates off-screen right in a storm of particles
    //   storm  — black sky with lightning only, for as long as the swap
    //            takes; complete() then fires the barrage + white flash.
    var T_ENTER = 2800, T_STRIKE = 420, T_BLAST = 1300;
    var strikeSpawned = false;
    var completing = false;
    var barrageStart = 0;
    var onFlashCb = null;
    var flashFired = false;
    var done = false;
    var raf = null;

    function frame(ts) {
      if (done) return;
      var elapsed = ts - start;
      ctx.clearRect(0, 0, W, H);

      // Ambient flash: live bolts light up the whole sky for their duration,
      // pulsing with the return strokes — the big realism cue.
      var wash = 0;
      for (var wi = 0; wi < bolts.length; wi++) {
        wash += boltAlpha(bolts[wi], ts) * (bolts[wi].big ? 0.14 : 0.035);
      }
      if (wash > 0.005) {
        ctx.fillStyle = 'rgba(140,180,255,' + Math.min(wash, 0.20).toFixed(3) + ')';
        ctx.fillRect(0, 0, W, H);
      }

      var cy = H * 0.34;
      var cx = W * 0.5;
      var beeX = null, beeY = null;   // null → bee has left the timeline

      var phase;
      if (elapsed < T_ENTER) phase = 'enter';
      else if (elapsed < T_ENTER + T_STRIKE) phase = 'strike';
      else if (elapsed < T_ENTER + T_STRIKE + T_BLAST) phase = 'blast';
      else phase = 'storm';

      if (phase === 'enter') {
        var ep = elapsed / T_ENTER;
        var easeOut = 1 - Math.pow(1 - ep, 3);
        beeX = -100 + easeOut * (cx + 100);
        beeY = cy + Math.sin(elapsed * 0.003) * 8;
      } else if (phase === 'strike') {
        beeX = cx;
        beeY = cy + Math.sin(elapsed * 0.003) * 3;
        if (!strikeSpawned) {
          strikeSpawned = true;
          if (!reduced) {
            // Return strokes: the main channel re-flashes as it discharges
            bolts.push({ paths: makeForkBolt(cx + (Math.random() - 0.5) * 30, 0,
              Math.PI / 2, cy * 1.15, 6, 3),
              born: ts, life: 620, restrike: true, big: true });
            bolts.push({ paths: makeForkBolt(cx - 70, 0,
              Math.PI / 2 + 0.15, cy * 1.05, 3, 2),
              born: ts, life: 620, restrike: true });
            bolts.push({ paths: makeForkBolt(cx + 70, 0,
              Math.PI / 2 - 0.15, cy * 1.05, 3, 2),
              born: ts, life: 620, restrike: true });
          }
        }
      } else if (phase === 'blast') {
        var bp = (elapsed - T_ENTER - T_STRIKE) / T_BLAST;
        var easeIn = bp * bp * bp;
        beeX = cx + easeIn * (W - cx + 160);
        beeY = cy + Math.sin(elapsed * 0.003) * 4;
      }
      // storm: no bee — black sky, lightning only

      // Screen shake while struck and blasting off
      var shaking = !reduced && !flashFired
        && (phase === 'strike' || phase === 'blast');
      if (shaking) {
        var sh = phase === 'strike' ? 6 : 4;
        ctx.save();
        ctx.translate((Math.random() - 0.5) * sh, (Math.random() - 0.5) * sh);
      }

      // Temporal streaks behind the bee while it's on screen
      if (beeX !== null && beeX > 60) {
        var streakAlpha = phase === 'blast' ? 0.6 : 0.3;
        for (var si = 0; si < 6; si++) {
          var sy = cy + si * 7 - 18;
          var gr = ctx.createLinearGradient(0, sy, beeX - 40, sy);
          gr.addColorStop(0, 'rgba(100,180,255,0)');
          gr.addColorStop(1, 'rgba(100,180,255,' + streakAlpha + ')');
          ctx.beginPath();
          ctx.strokeStyle = gr;
          ctx.lineWidth = si < 2 ? 1.5 : 0.8;
          ctx.moveTo(0, sy);
          ctx.lineTo(beeX - 40, sy);
          ctx.stroke();
        }
      }

      // Sky lightning from the top edge — sparse while the bee enters,
      // heavier as it blasts off, constant through the storm.
      var boltChance = phase === 'storm' ? 0.08
        : phase === 'blast' ? 0.10 : 0.03;
      if (!reduced && Math.random() < boltChance) {
        // Roughly a third of storm strikes get return strokes too
        var doubles = Math.random() < 0.35;
        bolts.push({
          paths: makeForkBolt(Math.random() * W, 0,
            Math.PI / 2 + (Math.random() - 0.5) * 0.6,
            H * (0.25 + Math.random() * 0.35), 2 + Math.random(), 2),
          born: ts,
          life: doubles ? 620 : 150 + Math.random() * 130,
          restrike: doubles,
        });
      }

      // Swap finished: one last giant barrage, then the white flash
      if (completing && phase === 'storm' && !barrageStart) {
        barrageStart = ts;
        if (!reduced) {
          for (var k = 0; k < 3; k++) {
            bolts.push({ paths: makeForkBolt(W * (0.25 + Math.random() * 0.5), 0,
              Math.PI / 2 + (Math.random() - 0.5) * 0.3,
              H * (0.5 + Math.random() * 0.3), 4 + Math.random() * 3, 3),
              born: ts, life: 620, restrike: true, big: true });
          }
        }
      }
      if (barrageStart && !flashFired && (reduced || ts - barrageStart >= 320)) {
        flashFired = true;
        flash.style.transition = 'opacity .18s ease';
        flash.style.opacity = '1';
        setTimeout(function () {
          if (onFlashCb) onFlashCb();
        }, 260);
      }

      for (var bi = bolts.length - 1; bi >= 0; bi--) {
        var bolt = bolts[bi];
        if (ts - bolt.born >= bolt.life) { bolts.splice(bi, 1); continue; }
        var env = boltAlpha(bolt, ts);
        if (env > 0.01) {
          drawBoltPaths(ctx, bolt.paths, env * (0.8 + Math.random() * 0.2));
        }
      }

      // Charge level: ramps up as the strike lands, stays lit through blast
      var charge = phase === 'strike'
        ? Math.min((elapsed - T_ENTER) / 200, 1)
        : phase === 'blast' ? 1 : 0;

      // Struck-bee whiteout: the bee's body flashes white-hot in sync with
      // the strike's return strokes, fading as the channel dies (~600ms).
      var beeTint = (phase === 'strike' || phase === 'blast')
        ? 0.9 * restrikeEnv(elapsed - T_ENTER)
        : 0;

      if (beeX !== null && !flashFired) {
        // Electric halo around the struck bee — flickers like held charge
        if (charge > 0) {
          var flick = 0.75 + Math.random() * 0.25;
          var gR = 70 + 25 * charge;
          var glow = ctx.createRadialGradient(beeX, beeY, 0, beeX, beeY, gR);
          glow.addColorStop(0, 'rgba(210,235,255,' + (0.5 * charge * flick).toFixed(3) + ')');
          glow.addColorStop(0.4, 'rgba(130,190,255,' + (0.28 * charge * flick).toFixed(3) + ')');
          glow.addColorStop(1, 'rgba(100,180,255,0)');
          ctx.beginPath();
          ctx.arc(beeX, beeY, gR, 0, Math.PI * 2);
          ctx.fillStyle = glow;
          ctx.fill();
        }

        var intensity = phase === 'blast' ? 1
          : phase === 'strike' ? 0.6
          : 0.35 + 0.1 * Math.sin(elapsed * 0.002);
        drawExhaust(ctx, beeX, beeY, elapsed, intensity);
        if (!reduced) {
          if (phase === 'blast') spawnAccelParticles(beeX, beeY, 12);
          else if (phase === 'enter' && Math.random() < 0.7) {
            spawnCruiseParticles(beeX, beeY, 2);
          }
        }
      }

      updateAndDrawParticles(ctx);
      if (beeX !== null && !flashFired) {
        // X-ray gag: at each return-stroke peak the bee strobes to its
        // skeleton, dropping back to the (whitening) bee between pulses.
        var skel = beeTint > 0.4 ? Math.min(1, (beeTint - 0.4) / 0.25) : 0;
        drawBeeTinted(beeX, beeY, elapsed, beeTint * (1 - skel * 0.7));
        if (skel > 0) drawBeeSkeleton(ctx, beeX, beeY, elapsed, skel);
        // Residual arcs crackling over the charged bee
        if (charge > 0.3 && !reduced && Math.random() < 0.5) {
          var arcA = Math.random() * Math.PI * 2;
          drawBoltPaths(ctx,
            makeForkBolt(beeX + Math.cos(arcA) * 20, beeY + Math.sin(arcA) * 14,
              arcA, 22 + Math.random() * 26, 0.8, 1),
            0.5 + Math.random() * 0.4);
        }
      }

      if (shaking) ctx.restore();

      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);

    function teardown() {
      done = true;
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
      particles.length = 0;
      div.classList.remove('tt-in');
      setTimeout(function () { div.remove(); style.remove(); }, 550);
    }

    return {
      // Kept for API compatibility — the pared-down overlay has no text row.
      setStatus: function () {},
      complete: function (onFlash) {
        if (completing) return;
        completing = true;
        onFlashCb = onFlash;
        // The barrage fires once the intro reaches the storm phase (at most
        // ~4.5s after the overlay opened). Safety net: if the callback
        // doesn't navigate away, clean up.
        setTimeout(teardown, 10000);
      },
      abort: teardown,
    };
  };

})();