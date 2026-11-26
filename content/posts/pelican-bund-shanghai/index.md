---
title: "鹈鹕骑行上海外滩：Canvas 动画与运动学实现"
date: 2026-11-26T20:32:00+08:00
summary: "使用 Google Gemini 4 Neon（Medium 档位 / Medium 思考深度）单轮生成的上海外滩鹈鹕骑行 Canvas 动画实测，记录完整评测参数、外滩地理视角与二连杆逆运动学约束。"
description: "鹈鹕测试（Pelican Benchmark）上海外滩动态版实测：Gemini 4 Neon（Medium 思考深度）生成参数、空间方位与自行车逆运动学拆解。"
tags:
  - pelican-test
  - evaluation
  - codegen
  - meta
---

这段「鹈鹕骑自行车」本土化动态测试由 Google Gemini 4 Neon 在 Medium 档位与 Medium 思考深度下单轮生成，使用单个原生 `<canvas>` 实现，不依赖外部动画库或图片素材。画面中的外滩方位、车轮转速、链条线速度与鹈鹕双腿关节由同一套距离变量驱动，避免常见的「脚踏悬空」「轮子打滑」「地标左右颠倒」问题。

## 在线演示

点击顶部按钮可以切换「黄昏蓝调 / 外滩夜景 / 晴朗午后」，拖动滑块调整骑行配速，点击「按车铃」或画面任意位置会触发车铃声效与点头动画。

<div class="pelican-embed" translate="no">
  <style>
    .pelican-embed {
      margin: 20px 0 28px;
      padding: 14px 16px 12px;
      background: var(--paper-2, #ececec);
      border: 1px solid var(--rule, rgba(45, 49, 66, 0.12));
      border-radius: 10px;
    }
    .pelican-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding-bottom: 10px;
      margin-bottom: 12px;
      border-bottom: 1px solid var(--rule, rgba(45, 49, 66, 0.12));
      font-size: 12px;
      line-height: 1.4;
    }
    .pelican-group {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
    }
    .pelican-btn {
      appearance: none;
      border: 1px solid var(--rule, rgba(45, 49, 66, 0.18));
      background: var(--paper, #f5f5f5);
      color: var(--ink, #2d3142);
      border-radius: 6px;
      padding: 4px 10px;
      font: inherit;
      font-size: 12px;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
    }
    .pelican-btn:hover {
      border-color: var(--accent, #bf4520);
      color: var(--accent, #bf4520);
    }
    .pelican-btn[data-active="true"] {
      background: var(--accent, #bf4520);
      border-color: var(--accent, #bf4520);
      color: #ffffff;
    }
    .pelican-speed {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted, #4f5d75);
    }
    .pelican-speed input[type="range"] {
      width: 84px;
      accent-color: var(--accent, #bf4520);
      cursor: pointer;
    }
    .pelican-mono {
      font-family: var(--mono, ui-monospace, monospace);
      font-variant-numeric: tabular-nums;
      color: var(--ink, #2d3142);
    }
    .pelican-canvas {
      display: block;
      width: 100%;
      height: 360px;
      border-radius: 8px;
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
    }
    .pelican-foot {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted, #4f5d75);
    }
  </style>

  <div class="pelican-bar">
    <div class="pelican-group">
      <button type="button" class="pelican-btn pelican-mode" data-mode="twilight" data-active="true">黄昏蓝调</button>
      <button type="button" class="pelican-btn pelican-mode" data-mode="night" data-active="false">外滩夜景</button>
      <button type="button" class="pelican-btn pelican-mode" data-mode="day" data-active="false">晴朗午后</button>
    </div>
    <div class="pelican-group">
      <label class="pelican-speed">
        <span>配速</span>
        <input id="pelican-speed" type="range" min="0.4" max="2.2" step="0.1" value="1.0" />
        <span id="pelican-speed-label" class="pelican-mono" style="min-width:52px;">16 km/h</span>
      </label>
      <button type="button" id="pelican-bell" class="pelican-btn" title="点击画面也可以按铃">按车铃</button>
      <button type="button" id="pelican-pause" class="pelican-btn">暂停</button>
    </div>
  </div>

  <canvas id="pelican-scene" class="pelican-canvas" aria-label="鹈鹕骑着自行车在上海外滩骑行动画"></canvas>

  <div class="pelican-foot">
    <span>Gemini 4 Neon (Medium) · 中山东一路非机动车道（自北向南骑行）</span>
    <span id="pelican-telemetry" class="pelican-mono">踏频 58 RPM · 点击画面按铃</span>
  </div>

  <script>
    (() => {
      const canvas = document.getElementById('pelican-scene');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');

      const state = {
        paused: false,
        speedMul: 1.0,
        targetMode: 'twilight',
        modeBlend: { twilight: 1, night: 0, day: 0 },
        distance: 0,
        time: 0,
        bellTimer: 0,
        blinkTimer: 2.4,
        blinkDur: 0,
        boatX: 520,
        boat2X: 110,
        clouds: [
          { x: 120, y: 54, w: 96, s: 0.14 },
          { x: 420, y: 38, w: 120, s: 0.10 },
          { x: 740, y: 62, w: 84, s: 0.16 }
        ],
        stars: Array.from({ length: 42 }, (_, i) => ({
          x: (i * 97 + 31) % 1000 / 1000,
          y: (i * 53 + 17) % 1000 / 1000 * 0.42,
          r: 0.7 + (i % 3) * 0.45,
          phase: i * 1.3
        })),
        windLines: Array.from({ length: 10 }, (_, i) => ({
          x: i * 110,
          y: 200 + (i * 37) % 130,
          len: 28 + (i % 4) * 14,
          speed: 1.35 + (i % 3) * 0.25
        }))
      };

      const PALETTES = {
        day: {
          skyTop: [76, 152, 224], skyMid: [148, 202, 244], skyBot: [216, 236, 248],
          riverTop: [78, 138, 178], riverBot: [52, 102, 142],
          cityFar: [146, 168, 192], cityMid: [112, 136, 164],
          road: [84, 90, 98], sidewalk: [188, 184, 176], wall: [176, 170, 158],
          neonAlpha: 0.08, lampGlow: 0.0, starAlpha: 0.0
        },
        twilight: {
          skyTop: [22, 34, 74], skyMid: [76, 62, 114], skyBot: [232, 132, 92],
          riverTop: [36, 44, 78], riverBot: [22, 28, 52],
          cityFar: [48, 52, 84], cityMid: [34, 38, 66],
          road: [50, 54, 64], sidewalk: [114, 110, 118], wall: [106, 100, 108],
          neonAlpha: 0.92, lampGlow: 0.85, starAlpha: 0.45
        },
        night: {
          skyTop: [8, 14, 32], skyMid: [16, 26, 56], skyBot: [34, 48, 86],
          riverTop: [14, 22, 46], riverBot: [8, 14, 30],
          cityFar: [24, 32, 58], cityMid: [16, 22, 42],
          road: [34, 38, 46], sidewalk: [74, 76, 84], wall: [68, 70, 78],
          neonAlpha: 1.0, lampGlow: 1.0, starAlpha: 0.92
        }
      };

      function blendColor(key) {
        const w = state.modeBlend;
        const d = PALETTES.day[key], t = PALETTES.twilight[key], n = PALETTES.night[key];
        const r = Math.round(d[0] * w.day + t[0] * w.twilight + n[0] * w.night);
        const g = Math.round(d[1] * w.day + t[1] * w.twilight + n[1] * w.night);
        const b = Math.round(d[2] * w.day + t[2] * w.twilight + n[2] * w.night);
        return `rgb(${r},${g},${b})`;
      }

      function blendScalar(key) {
        const w = state.modeBlend;
        return PALETTES.day[key] * w.day + PALETTES.twilight[key] * w.twilight + PALETTES.night[key] * w.night;
      }

      let audioCtx = null;
      function playBellSound() {
        try {
          if (!audioCtx) {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            audioCtx = new AudioContext();
          }
          if (audioCtx.state === 'suspended') audioCtx.resume();
          const now = audioCtx.currentTime;
          [0, 0.11].forEach((offset, idx) => {
            const osc = audioCtx.createOscillator();
            const osc2 = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = 'sine';
            osc2.type = 'triangle';
            const baseFreq = idx === 0 ? 1760 : 2093;
            osc.frequency.setValueAtTime(baseFreq, now + offset);
            osc2.frequency.setValueAtTime(baseFreq * 2.01, now + offset);
            gain.gain.setValueAtTime(0.001, now + offset);
            gain.gain.exponentialRampToValueAtTime(0.09, now + offset + 0.012);
            gain.gain.exponentialRampToValueAtTime(0.0008, now + offset + 0.36);
            osc.connect(gain);
            osc2.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start(now + offset);
            osc2.start(now + offset);
            osc.stop(now + offset + 0.38);
            osc2.stop(now + offset + 0.38);
          });
        } catch (_) {}
      }

      function triggerBell() {
        state.bellTimer = 0.75;
        playBellSound();
      }

      const modeBtns = document.querySelectorAll('.pelican-mode');
      modeBtns.forEach(btn => {
        btn.addEventListener('click', () => {
          state.targetMode = btn.dataset.mode;
          modeBtns.forEach(b => b.dataset.active = String(b === btn));
        });
      });

      const speedSlider = document.getElementById('pelican-speed');
      const speedLabel = document.getElementById('pelican-speed-label');
      const telemetry = document.getElementById('pelican-telemetry');
      const btnPause = document.getElementById('pelican-pause');
      const btnBell = document.getElementById('pelican-bell');

      function updateReadout() {
        const kmh = Math.round(16 * state.speedMul);
        const rpm = Math.round(58 * state.speedMul);
        speedLabel.textContent = `${kmh} km/h`;
        telemetry.textContent = state.paused
          ? '已暂停 · 踏频 0 RPM'
          : `踏频 ${rpm} RPM · 点击画面按铃`;
      }

      speedSlider.addEventListener('input', () => {
        state.speedMul = parseFloat(speedSlider.value);
        updateReadout();
      });
      btnPause.addEventListener('click', () => {
        state.paused = !state.paused;
        btnPause.textContent = state.paused ? '继续' : '暂停';
        updateReadout();
      });
      btnBell.addEventListener('click', triggerBell);
      canvas.addEventListener('click', triggerBell);

      let W = 800;
      const H = 360;
      function resize() {
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const rect = canvas.getBoundingClientRect();
        W = Math.max(320, Math.floor(rect.width || 720));
        canvas.width = W * dpr;
        canvas.height = H * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      window.addEventListener('resize', resize);
      resize();

      function solveIK2Bone(hx, hy, tx, ty, L1, L2, bendDir) {
        const dx = tx - hx;
        const dy = ty - hy;
        let d = Math.hypot(dx, dy);
        const maxD = L1 + L2 - 0.5;
        const minD = Math.abs(L1 - L2) + 0.5;
        if (d > maxD) d = maxD;
        if (d < minD) d = minD;
        const a = (L1 * L1 - L2 * L2 + d * d) / (2 * d);
        const h = Math.sqrt(Math.max(0, L1 * L1 - a * a));
        const len = Math.hypot(dx, dy) || 1;
        const ux = dx / len;
        const uy = dy / len;
        const mx = hx + a * ux;
        const my = hy + a * uy;
        return {
          kx: mx + bendDir * h * (-uy),
          ky: my + bendDir * h * (ux)
        };
      }

      function drawSky() {
        const grad = ctx.createLinearGradient(0, 0, 0, 195);
        grad.addColorStop(0, blendColor('skyTop'));
        grad.addColorStop(0.55, blendColor('skyMid'));
        grad.addColorStop(1, blendColor('skyBot'));
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, W, 195);

        const starAlpha = blendScalar('starAlpha');
        if (starAlpha > 0.02) {
          for (const s of state.stars) {
            const twinkle = 0.55 + 0.45 * Math.sin(state.time * 2.2 + s.phase);
            ctx.fillStyle = `rgba(255, 250, 235, ${(starAlpha * twinkle).toFixed(3)})`;
            ctx.beginPath();
            ctx.arc(s.x * W, s.y * H, s.r, 0, Math.PI * 2);
            ctx.fill();
          }
        }

        const w = state.modeBlend;
        if (w.day > 0.05) {
          ctx.fillStyle = `rgba(255, 248, 210, ${(0.85 * w.day).toFixed(3)})`;
          ctx.beginPath();
          ctx.arc(W * 0.82, 48, 18, 0, Math.PI * 2);
          ctx.fill();
        }
        if (w.night > 0.05) {
          ctx.fillStyle = `rgba(248, 242, 210, ${(0.9 * w.night).toFixed(3)})`;
          ctx.beginPath();
          ctx.arc(W * 0.84, 46, 12, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = blendColor('skyTop');
          ctx.beginPath();
          ctx.arc(W * 0.84 + 5, 43, 11, 0, Math.PI * 2);
          ctx.fill();
        }

        const cloudAlpha = 0.55 * w.day + 0.22 * w.twilight + 0.06 * w.night;
        ctx.fillStyle = w.twilight > 0.5
          ? `rgba(255, 186, 150, ${cloudAlpha.toFixed(3)})`
          : `rgba(255, 255, 255, ${cloudAlpha.toFixed(3)})`;
        for (const c of state.clouds) {
          const cx = ((c.x - state.distance * 0.025 * c.s) % (W + 220) + (W + 220)) % (W + 220) - 110;
          ctx.beginPath();
          ctx.ellipse(cx, c.y, c.w * 0.5, 11, 0, 0, Math.PI * 2);
          ctx.ellipse(cx - c.w * 0.25, c.y + 3, c.w * 0.3, 8, 0, 0, Math.PI * 2);
          ctx.ellipse(cx + c.w * 0.26, c.y + 4, c.w * 0.28, 8, 0, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      function drawLujiazuiTile(ox, baseY, neonAlpha) {
        ctx.save();
        ctx.translate(ox, baseY);

        const farCol = blendColor('cityFar');
        const midCol = blendColor('cityMid');

        const fillers = [
          { x: 20, w: 28, h: 68 }, { x: 54, w: 22, h: 86 }, { x: 82, w: 34, h: 60 },
          { x: 185, w: 30, h: 78 }, { x: 222, w: 26, h: 94 }, { x: 256, w: 36, h: 66 },
          { x: 356, w: 26, h: 76 }, { x: 472, w: 34, h: 82 }, { x: 560, w: 28, h: 96 },
          { x: 596, w: 38, h: 72 }, { x: 642, w: 26, h: 88 }, { x: 676, w: 32, h: 64 },
          { x: 720, w: 28, h: 74 }, { x: 756, w: 36, h: 58 }, { x: 800, w: 30, h: 80 },
          { x: 842, w: 34, h: 62 }
        ];

        for (const b of fillers) {
          ctx.fillStyle = farCol;
          ctx.fillRect(b.x, -b.h, b.w, b.h);
          if (neonAlpha > 0.12) {
            ctx.fillStyle = `rgba(255, 224, 150, ${(neonAlpha * 0.45).toFixed(3)})`;
            for (let wy = -b.h + 8; wy < -8; wy += 9) {
              for (let wx = b.x + 4; wx < b.x + b.w - 5; wx += 7) {
                if (((wx * 13 + wy * 7) & 3) !== 0) ctx.fillRect(wx, wy, 3, 4);
              }
            }
          }
        }

        // 东方明珠
        const px = 140;
        ctx.strokeStyle = midCol;
        ctx.fillStyle = midCol;
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(px - 22, 0); ctx.lineTo(px - 6, -42);
        ctx.moveTo(px + 22, 0); ctx.lineTo(px + 6, -42);
        ctx.moveTo(px, 0); ctx.lineTo(px, -138);
        ctx.stroke();
        ctx.lineWidth = 2.2;
        ctx.beginPath();
        ctx.moveTo(px - 5, -42); ctx.lineTo(px - 4, -98);
        ctx.moveTo(px + 5, -42); ctx.lineTo(px + 4, -98);
        ctx.stroke();
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(px, -124); ctx.lineTo(px, -158);
        ctx.stroke();

        ctx.fillStyle = midCol;
        ctx.beginPath(); ctx.arc(px, -42, 13, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(px, -98, 9.5, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(px, -121, 4.5, 0, Math.PI * 2); ctx.fill();

        if (neonAlpha > 0.15) {
          ctx.fillStyle = `rgba(244, 92, 168, ${(0.55 + 0.45 * neonAlpha).toFixed(3)})`;
          ctx.beginPath(); ctx.arc(px, -42, 10.5, 0, Math.PI * 2); ctx.fill();
          ctx.beginPath(); ctx.arc(px, -98, 7.5, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = `rgba(120, 235, 255, ${(neonAlpha * 0.9).toFixed(3)})`;
          ctx.beginPath(); ctx.arc(px, -121, 3.2, 0, Math.PI * 2); ctx.fill();
          const pulse = 0.5 + 0.5 * Math.sin(state.time * 4);
          ctx.fillStyle = `rgba(255, 70, 90, ${(neonAlpha * pulse).toFixed(3)})`;
          ctx.beginPath(); ctx.arc(px, -158, 2.2, 0, Math.PI * 2); ctx.fill();
        }

        // 金茂大厦
        const jx = 320;
        ctx.fillStyle = midCol;
        const tiers = [
          { w: 28, y0: 0, y1: -34 }, { w: 24, y0: -34, y1: -62 },
          { w: 20, y0: -62, y1: -86 }, { w: 16, y0: -86, y1: -106 },
          { w: 11, y0: -106, y1: -120 }
        ];
        for (const t of tiers) ctx.fillRect(jx - t.w / 2, t.y1, t.w, t.y0 - t.y1);
        ctx.beginPath();
        ctx.moveTo(jx - 5, -120); ctx.lineTo(jx, -138); ctx.lineTo(jx + 5, -120);
        ctx.closePath();
        ctx.fill();
        if (neonAlpha > 0.15) {
          ctx.strokeStyle = `rgba(255, 206, 112, ${(neonAlpha * 0.85).toFixed(3)})`;
          ctx.lineWidth = 1.4;
          for (const t of tiers) {
            ctx.beginPath();
            ctx.moveTo(jx - t.w / 2 + 1, t.y1 + 1);
            ctx.lineTo(jx + t.w / 2 - 1, t.y1 + 1);
            ctx.stroke();
          }
          ctx.fillStyle = `rgba(255, 224, 140, ${(neonAlpha * 0.9).toFixed(3)})`;
          ctx.fillRect(jx - 2, -134, 4, 14);
        }

        // 上海中心大厦
        const sx = 418;
        ctx.fillStyle = midCol;
        ctx.beginPath();
        ctx.moveTo(sx - 19, 0);
        ctx.quadraticCurveTo(sx - 21, -85, sx - 10, -172);
        ctx.lineTo(sx + 8, -162);
        ctx.quadraticCurveTo(sx + 21, -82, sx + 18, 0);
        ctx.closePath();
        ctx.fill();
        if (neonAlpha > 0.15) {
          ctx.strokeStyle = `rgba(96, 214, 255, ${(neonAlpha * 0.88).toFixed(3)})`;
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          ctx.moveTo(sx - 15, -6);
          ctx.bezierCurveTo(sx + 16, -54, sx - 16, -110, sx + 5, -162);
          ctx.stroke();
          ctx.strokeStyle = `rgba(180, 240, 255, ${(neonAlpha * 0.5).toFixed(3)})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(sx + 12, -10);
          ctx.bezierCurveTo(sx - 14, -68, sx + 12, -120, sx - 8, -170);
          ctx.stroke();
        }

        // 上海环球金融中心
        const wx = 520;
        ctx.fillStyle = midCol;
        ctx.beginPath();
        ctx.moveTo(wx - 17, 0); ctx.lineTo(wx - 12, -150);
        ctx.lineTo(wx + 12, -150); ctx.lineTo(wx + 17, 0);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = blendColor('skyMid');
        ctx.beginPath();
        ctx.moveTo(wx - 6.5, -138); ctx.lineTo(wx + 6.5, -138);
        ctx.lineTo(wx + 8.5, -121); ctx.lineTo(wx - 8.5, -121);
        ctx.closePath();
        ctx.fill();
        if (neonAlpha > 0.15) {
          ctx.strokeStyle = `rgba(125, 224, 255, ${(neonAlpha * 0.92).toFixed(3)})`;
          ctx.lineWidth = 1.6;
          ctx.beginPath();
          ctx.moveTo(wx - 6.5, -138); ctx.lineTo(wx + 6.5, -138);
          ctx.lineTo(wx + 8.5, -121); ctx.lineTo(wx - 8.5, -121);
          ctx.closePath();
          ctx.stroke();
          ctx.strokeStyle = `rgba(165, 210, 255, ${(neonAlpha * 0.45).toFixed(3)})`;
          ctx.beginPath();
          ctx.moveTo(wx - 12, -149); ctx.lineTo(wx - 16, -2);
          ctx.moveTo(wx + 12, -149); ctx.lineTo(wx + 16, -2);
          ctx.stroke();
        }

        if (neonAlpha > 0.2) {
          ctx.fillStyle = `rgba(255, 86, 120, ${(neonAlpha * 0.85).toFixed(3)})`;
          ctx.fillRect(262, -22, 24, 11);
          ctx.fillStyle = `rgba(255, 248, 235, ${(neonAlpha * 0.95).toFixed(3)})`;
          ctx.font = 'bold 7px sans-serif';
          ctx.fillText('SH', 269, -14);
        }

        ctx.restore();
      }

      function drawCruiseBoat(x, y, scale, neonAlpha) {
        ctx.save();
        ctx.translate(x, y);
        ctx.scale(scale, scale);
        ctx.strokeStyle = 'rgba(220, 238, 255, 0.28)';
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(26, 6); ctx.lineTo(58, 7); ctx.stroke();

        ctx.fillStyle = '#263042';
        ctx.beginPath();
        ctx.moveTo(-34, 0); ctx.lineTo(30, 0); ctx.lineTo(24, 7); ctx.lineTo(-28, 7);
        ctx.closePath();
        ctx.fill();

        ctx.fillStyle = '#e8ecf2';
        ctx.fillRect(-24, -8, 44, 8);
        ctx.fillRect(-14, -14, 24, 6);

        ctx.fillStyle = neonAlpha > 0.2 ? '#ffd670' : '#4a607a';
        for (let wx = -20; wx <= 14; wx += 7) ctx.fillRect(wx, -6, 4, 3);
        if (neonAlpha > 0.25) {
          ctx.strokeStyle = `rgba(255, 198, 92, ${(neonAlpha * 0.9).toFixed(3)})`;
          ctx.lineWidth = 1.2;
          ctx.beginPath(); ctx.moveTo(-32, 0); ctx.lineTo(28, 0); ctx.stroke();
        }
        ctx.restore();
      }

      function drawSkylineAndRiver() {
        const horizonY = 186;
        const riverBotY = 238;
        const neonAlpha = blendScalar('neonAlpha');
        const tileW = 900;
        const scroll = (state.distance * 0.065) % tileW;

        for (let x = -scroll; x < W + tileW; x += tileW) {
          drawLujiazuiTile(x, horizonY, neonAlpha);
        }

        const rGrad = ctx.createLinearGradient(0, horizonY, 0, riverBotY);
        rGrad.addColorStop(0, blendColor('riverTop'));
        rGrad.addColorStop(1, blendColor('riverBot'));
        ctx.fillStyle = rGrad;
        ctx.fillRect(0, horizonY, W, riverBotY - horizonY);

        if (neonAlpha > 0.12) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(0, horizonY, W, riverBotY - horizonY);
          ctx.clip();
          const refColumns = [
            { localX: 140, color: [244, 92, 168], w: 18 },
            { localX: 320, color: [255, 206, 112], w: 16 },
            { localX: 418, color: [96, 214, 255], w: 20 },
            { localX: 520, color: [125, 224, 255], w: 18 }
          ];
          for (let tx = -scroll; tx < W + tileW; tx += tileW) {
            for (const rc of refColumns) {
              const rx = tx + rc.localX;
              for (let row = 0; row < 12; row++) {
                const ry = horizonY + 3 + row * 4.2;
                const sway = Math.sin(state.time * 3.1 + row * 0.9 + rc.localX) * (2.5 + row * 0.4);
                const alpha = neonAlpha * (0.36 - row * 0.025);
                ctx.fillStyle = `rgba(${rc.color[0]},${rc.color[1]},${rc.color[2]},${Math.max(0, alpha).toFixed(3)})`;
                ctx.fillRect(rx - rc.w / 2 + sway, ry, rc.w + row * 0.8, 2.2);
              }
            }
          }
          ctx.restore();
        }

        drawCruiseBoat(state.boatX, 212, 1.0, neonAlpha);
        drawCruiseBoat(state.boat2X, 200, 0.65, neonAlpha);

        ctx.strokeStyle = `rgba(255, 255, 255, ${(0.12 + 0.08 * state.modeBlend.day).toFixed(3)})`;
        ctx.lineWidth = 1;
        for (let i = 0; i < 14; i++) {
          const rx = (((i * 83 - state.distance * 0.16) % (W + 80)) + (W + 80)) % (W + 80) - 40;
          const ry = horizonY + 8 + (i * 19) % 40;
          ctx.beginPath();
          ctx.moveTo(rx, ry);
          ctx.lineTo(rx + 18 + (i % 3) * 8, ry);
          ctx.stroke();
        }
      }

      function drawBundLamp(x, baseY, glow) {
        ctx.save();
        ctx.translate(x, baseY);
        ctx.fillStyle = '#252b34';
        ctx.fillRect(-3.5, -14, 7, 14);
        ctx.fillRect(-2, -96, 4, 84);
        ctx.strokeStyle = '#252b34';
        ctx.lineWidth = 2.4;
        ctx.beginPath();
        ctx.moveTo(-14, -90);
        ctx.quadraticCurveTo(0, -84, 14, -90);
        ctx.stroke();

        if (glow > 0.05) {
          [-14, 14].forEach(gx => {
            const rad = ctx.createRadialGradient(gx, -94, 2, gx, -94, 34);
            rad.addColorStop(0, `rgba(255, 224, 145, ${(0.55 * glow).toFixed(3)})`);
            rad.addColorStop(0.5, `rgba(255, 192, 96, ${(0.18 * glow).toFixed(3)})`);
            rad.addColorStop(1, 'rgba(255, 180, 80, 0)');
            ctx.fillStyle = rad;
            ctx.beginPath();
            ctx.arc(gx, -94, 34, 0, Math.PI * 2);
            ctx.fill();
          });
        }

        ctx.fillStyle = glow > 0.2 ? '#fff4cf' : '#e6ebf0';
        ctx.beginPath(); ctx.arc(-14, -94, 4.5, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(14, -94, 4.5, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
      }

      function drawShanghaiStreetSign(x, baseY) {
        ctx.save();
        ctx.translate(x, baseY);
        ctx.fillStyle = '#4a525e';
        ctx.fillRect(-1.8, -68, 3.6, 68);
        const sw = 58, sh = 24;
        ctx.fillStyle = '#f5f7fa';
        ctx.fillRect(-sw / 2, -74, sw, 6);
        ctx.fillStyle = '#1f8a5b';
        ctx.fillRect(-sw / 2 + 2, -72.5, 12, 3);
        ctx.fillStyle = '#144fa0';
        ctx.fillRect(-sw / 2, -68, sw, sh - 6);
        ctx.strokeStyle = 'rgba(255,255,255,0.65)';
        ctx.lineWidth = 0.8;
        ctx.strokeRect(-sw / 2 + 1, -67, sw - 2, sh - 8);

        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 8.5px -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('中山东一路', 0, -56);
        ctx.font = '5.5px sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.fillText('ZHONGSHAN E.1ST RD', 0, -51);
        ctx.restore();
      }

      function drawBundPromenadeAndRoad() {
        const wallTopY = 232;
        const sidewalkTopY = 258;
        const roadTopY = 278;
        const lampGlow = blendScalar('lampGlow');

        ctx.fillStyle = blendColor('wall');
        ctx.fillRect(0, wallTopY, W, sidewalkTopY - wallTopY);

        const pillarStep = 44;
        const pOff = (state.distance * 0.55) % pillarStep;
        ctx.fillStyle = 'rgba(255,255,255,0.14)';
        ctx.fillRect(0, wallTopY, W, 3);
        ctx.fillStyle = 'rgba(0,0,0,0.18)';
        for (let px = -pOff; px < W + pillarStep; px += pillarStep) {
          ctx.fillRect(px, wallTopY + 3, 6, sidewalkTopY - wallTopY - 3);
        }

        ctx.fillStyle = blendColor('sidewalk');
        ctx.fillRect(0, sidewalkTopY, W, roadTopY - sidewalkTopY);
        ctx.fillStyle = 'rgba(0,0,0,0.28)';
        ctx.fillRect(0, roadTopY - 3, W, 4);

        const furnitureSpan = 560;
        const fOff = (state.distance * 0.85) % furnitureSpan;
        for (let fx = -fOff; fx < W + furnitureSpan; fx += furnitureSpan) {
          drawBundLamp(fx + 110, sidewalkTopY + 4, lampGlow);
          drawShanghaiStreetSign(fx + 310, sidewalkTopY + 5);
          drawBundLamp(fx + 430, sidewalkTopY + 4, lampGlow);
        }

        ctx.fillStyle = blendColor('road');
        ctx.fillRect(0, roadTopY, W, H - roadTopY);
        ctx.fillStyle = 'rgba(255,255,255,0.08)';
        ctx.fillRect(0, roadTopY + 1, W, 1.5);

        const dashCycle = 90;
        const dashOff = state.distance % dashCycle;
        ctx.fillStyle = 'rgba(240, 242, 245, 0.55)';
        for (let dx = -dashOff; dx < W + dashCycle; dx += dashCycle) {
          ctx.fillRect(dx, roadTopY + 42, 42, 3.5);
        }

        const iconCycle = 480;
        const iconOff = state.distance % iconCycle;
        ctx.strokeStyle = 'rgba(240, 242, 245, 0.22)';
        ctx.lineWidth = 2;
        for (let ix = -iconOff + 220; ix < W + iconCycle; ix += iconCycle) {
          ctx.save();
          ctx.translate(ix, roadTopY + 62);
          ctx.scale(1, 0.42);
          ctx.beginPath();
          ctx.arc(-14, 0, 9, 0, Math.PI * 2);
          ctx.arc(14, 0, 9, 0, Math.PI * 2);
          ctx.moveTo(-14, 0); ctx.lineTo(-2, -12); ctx.lineTo(10, -12); ctx.lineTo(14, 0);
          ctx.moveTo(-2, -12); ctx.lineTo(1, 0);
          ctx.stroke();
          ctx.restore();
        }
      }

      function drawWheel(cx, cy, R, angle) {
        ctx.save();
        ctx.translate(cx, cy);
        ctx.strokeStyle = '#1e2228';
        ctx.lineWidth = 5.5;
        ctx.beginPath(); ctx.arc(0, 0, R - 2.5, 0, Math.PI * 2); ctx.stroke();

        ctx.strokeStyle = '#c8beaa';
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(0, 0, R - 5.5, 0, Math.PI * 2); ctx.stroke();

        ctx.strokeStyle = '#cfd6de';
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(0, 0, R - 7, 0, Math.PI * 2); ctx.stroke();

        ctx.strokeStyle = 'rgba(220, 228, 236, 0.72)';
        ctx.lineWidth = 1;
        for (let i = 0; i < 12; i++) {
          const a = angle + (i * Math.PI) / 6;
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.lineTo(Math.cos(a) * (R - 7.5), Math.sin(a) * (R - 7.5));
          ctx.stroke();
        }

        const refA = angle + 0.4;
        ctx.fillStyle = '#ff9824';
        ctx.beginPath();
        ctx.arc(Math.cos(refA) * (R - 12), Math.sin(refA) * (R - 12), 2.3, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = '#adb6c2';
        ctx.beginPath(); ctx.arc(0, 0, 4, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
      }

      function drawDrivetrain(rx, ry, bbx, bby, crankAngle) {
        const rCog = 5.5;
        const rChainring = 11.5;

        ctx.strokeStyle = '#3c434d';
        ctx.lineWidth = 2;
        ctx.setLineDash([3, 2.5]);
        ctx.lineDashOffset = -(crankAngle * rChainring);
        ctx.beginPath();
        ctx.moveTo(rx, ry - rCog); ctx.lineTo(bbx, bby - rChainring);
        ctx.moveTo(rx, ry + rCog); ctx.lineTo(bbx, bby + rChainring);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = '#8b95a2';
        ctx.beginPath(); ctx.arc(rx, ry, rCog, 0, Math.PI * 2); ctx.fill();

        ctx.fillStyle = '#b4beca';
        ctx.beginPath(); ctx.arc(bbx, bby, rChainring, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = '#5a6370';
        ctx.beginPath(); ctx.arc(bbx, bby, rChainring - 3.5, 0, Math.PI * 2); ctx.fill();
      }

      function drawCrankAndPedal(bbx, bby, px, py, isFar) {
        ctx.strokeStyle = isFar ? '#8a94a0' : '#d6dde6';
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(bbx, bby); ctx.lineTo(px, py);
        ctx.stroke();

        ctx.fillStyle = isFar ? '#2a3038' : '#1b1f26';
        ctx.fillRect(px - 6, py - 2, 12, 4);
      }

      function drawFrontBasket(bx, by, fx, fy, bob) {
        ctx.save();
        ctx.strokeStyle = '#9aa4b0';
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(bx + 12, by + 18); ctx.lineTo(fx, fy); ctx.stroke();

        ctx.fillStyle = '#d4a36a';
        ctx.beginPath(); ctx.roundRect(bx + 2, by - 8 + bob, 11, 14, 1.5); ctx.fill();
        ctx.fillStyle = '#b8864c';
        ctx.fillRect(bx + 2, by - 8 + bob, 11, 3);

        ctx.fillStyle = '#f5f0e6';
        ctx.beginPath();
        ctx.moveTo(bx + 14, by - 7 + bob); ctx.lineTo(bx + 22, by - 7 + bob);
        ctx.lineTo(bx + 20.5, by + 6 + bob); ctx.lineTo(bx + 15.5, by + 6 + bob);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = '#c97848';
        ctx.fillRect(bx + 14.6, by - 2 + bob, 6.8, 4.5);
        ctx.fillStyle = '#2c2a28';
        ctx.fillRect(bx + 13.5, by - 9.2 + bob, 9, 2.4);

        ctx.fillStyle = '#c48f52';
        ctx.strokeStyle = '#8c5d2c';
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(bx, by); ctx.lineTo(bx + 25, by);
        ctx.lineTo(bx + 22, by + 18); ctx.lineTo(bx + 3, by + 18);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

        ctx.strokeStyle = 'rgba(110, 68, 28, 0.45)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(bx + 1, by + 6); ctx.lineTo(bx + 24, by + 6);
        ctx.moveTo(bx + 2, by + 12); ctx.lineTo(bx + 23, by + 12);
        ctx.moveTo(bx + 9, by); ctx.lineTo(bx + 9.5, by + 18);
        ctx.moveTo(bx + 16, by); ctx.lineTo(bx + 15.5, by + 18);
        ctx.stroke();
        ctx.restore();
      }

      function drawPelicanLeg(hipX, hipY, pedalX, pedalY, isFar) {
        const { kx, ky } = solveIK2Bone(hipX, hipY, pedalX, pedalY - 2, 35, 36, -1);

        ctx.strokeStyle = isFar ? '#c46c22' : '#ee8b2e';
        ctx.lineWidth = 4.2;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.beginPath();
        ctx.moveTo(hipX, hipY); ctx.lineTo(kx, ky); ctx.lineTo(pedalX, pedalY - 2);
        ctx.stroke();

        ctx.fillStyle = isFar ? '#bf641c' : '#f28b29';
        ctx.beginPath();
        ctx.moveTo(pedalX - 4, pedalY - 2);
        ctx.lineTo(pedalX + 9, pedalY - 1);
        ctx.lineTo(pedalX + 7, pedalY + 2);
        ctx.lineTo(pedalX - 5, pedalY + 1.5);
        ctx.closePath();
        ctx.fill();
      }

      function drawPelicanBodyAndHead(hipX, hipY, shoulderX, shoulderY, bob) {
        ctx.save();

        const scarfBaseX = shoulderX + 4;
        const scarfBaseY = shoulderY - 10;
        ctx.strokeStyle = '#d93848';
        ctx.lineWidth = 5.5;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(scarfBaseX, scarfBaseY);
        const flutter = 3 + state.speedMul * 3.5;
        for (let i = 1; i <= 5; i++) {
          const sx = scarfBaseX - i * 8.5;
          const sy = scarfBaseY + 2 + Math.sin(state.time * (7 + state.speedMul * 4) - i * 0.9) * (flutter * i * 0.16);
          ctx.lineTo(sx, sy);
        }
        ctx.stroke();
        ctx.strokeStyle = '#f5c451';
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(scarfBaseX - 30, scarfBaseY + 2 + Math.sin(state.time * (7 + state.speedMul * 4) - 3.2) * (flutter * 0.55));
        ctx.lineTo(scarfBaseX - 38, scarfBaseY + 2 + Math.sin(state.time * (7 + state.speedMul * 4) - 4.1) * (flutter * 0.72));
        ctx.stroke();

        ctx.fillStyle = '#e5e9ef';
        ctx.beginPath();
        ctx.moveTo(hipX - 12, hipY - 4);
        ctx.lineTo(hipX - 28, hipY - 11 + bob * 0.5);
        ctx.lineTo(hipX - 24, hipY - 2 + bob * 0.5);
        ctx.lineTo(hipX - 29, hipY + 3 + bob * 0.5);
        ctx.lineTo(hipX - 10, hipY + 5);
        ctx.closePath();
        ctx.fill();

        const bodyGrad = ctx.createLinearGradient(hipX - 12, hipY - 30, hipX + 20, hipY + 8);
        bodyGrad.addColorStop(0, '#ffffff');
        bodyGrad.addColorStop(0.75, '#f0f3f7');
        bodyGrad.addColorStop(1, '#d6dde6');
        ctx.fillStyle = bodyGrad;
        ctx.beginPath();
        ctx.ellipse(hipX + 5, hipY - 11, 24, 15.5, -0.32, 0, Math.PI * 2);
        ctx.fill();

        const headNod = state.bellTimer > 0 ? Math.sin(state.bellTimer * 18) * 3 : 0;
        const headX = shoulderX + 8;
        const headY = shoulderY - 24 + bob * 1.1 + headNod;

        ctx.strokeStyle = '#f8fafc';
        ctx.lineWidth = 11.5;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(shoulderX - 1, shoulderY - 3);
        ctx.bezierCurveTo(shoulderX + 8, shoulderY - 12, headX - 9, headY + 12, headX, headY + 2);
        ctx.stroke();

        ctx.strokeStyle = '#d93848';
        ctx.lineWidth = 5;
        ctx.beginPath();
        ctx.moveTo(shoulderX - 4, shoulderY - 10);
        ctx.lineTo(shoulderX + 8, shoulderY - 7);
        ctx.stroke();

        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.ellipse(headX + 1, headY, 10.5, 8.5, -0.1, 0, Math.PI * 2);
        ctx.fill();

        ctx.strokeStyle = '#f2f5f9';
        ctx.lineWidth = 2.2;
        const tuftWave = Math.sin(state.time * 12) * 1.8;
        ctx.beginPath();
        ctx.moveTo(headX - 6, headY - 4);
        ctx.quadraticCurveTo(headX - 13, headY - 8 + tuftWave, headX - 16, headY - 3 + tuftWave);
        ctx.moveTo(headX - 5, headY - 1);
        ctx.quadraticCurveTo(headX - 12, headY - 3 + tuftWave, headX - 14, headY + 1 + tuftWave);
        ctx.stroke();

        const pouchJiggle = Math.sin(state.time * 9) * 0.9;
        const pouchGrad = ctx.createLinearGradient(headX + 6, headY + 1, headX + 32, headY + 14);
        pouchGrad.addColorStop(0, '#ffa938');
        pouchGrad.addColorStop(1, '#e8761c');
        ctx.fillStyle = pouchGrad;
        ctx.beginPath();
        ctx.moveTo(headX + 7, headY + 2);
        ctx.lineTo(headX + 41, headY + 4);
        ctx.quadraticCurveTo(headX + 28, headY + 18 + pouchJiggle, headX + 6, headY + 8);
        ctx.closePath();
        ctx.fill();

        ctx.fillStyle = '#f5be6b';
        ctx.beginPath();
        ctx.moveTo(headX + 7, headY - 2.5);
        ctx.lineTo(headX + 43, headY + 2);
        ctx.quadraticCurveTo(headX + 45, headY + 5, headX + 41, headY + 5.5);
        ctx.lineTo(headX + 7, headY + 2.5);
        ctx.closePath();
        ctx.fill();

        ctx.strokeStyle = 'rgba(160, 88, 20, 0.45)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(headX + 9, headY + 2.2); ctx.lineTo(headX + 40, headY + 3.8);
        ctx.stroke();

        const eyeX = headX + 4;
        const eyeY = headY - 1.5;
        if (state.blinkDur > 0) {
          ctx.strokeStyle = '#1e242c';
          ctx.lineWidth = 1.6;
          ctx.beginPath(); ctx.moveTo(eyeX - 2.5, eyeY); ctx.lineTo(eyeX + 2.5, eyeY); ctx.stroke();
        } else {
          ctx.fillStyle = '#ffd56b';
          ctx.beginPath(); ctx.arc(eyeX, eyeY, 3.1, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = '#1a2028';
          ctx.beginPath(); ctx.arc(eyeX + 0.4, eyeY, 1.9, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = '#ffffff';
          ctx.beginPath(); ctx.arc(eyeX + 1, eyeY - 0.7, 0.7, 0, Math.PI * 2); ctx.fill();
        }

        ctx.restore();
      }

      function drawPelicanWing(shoulderX, shoulderY, gripX, gripY, bob) {
        ctx.save();
        const midX = (shoulderX + gripX) * 0.5 - 4;
        const midY = (shoulderY + gripY) * 0.5 + 9 + bob * 0.5;

        ctx.fillStyle = '#dfe5ed';
        ctx.beginPath();
        ctx.moveTo(shoulderX - 10, shoulderY + 3);
        ctx.quadraticCurveTo(midX - 6, midY + 9, gripX - 2, gripY + 3);
        ctx.lineTo(gripX + 2, gripY - 2);
        ctx.quadraticCurveTo(midX + 2, midY - 4, shoulderX + 2, shoulderY - 4);
        ctx.closePath();
        ctx.fill();

        ctx.fillStyle = '#f8fafd';
        ctx.strokeStyle = '#cfd7e2';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(shoulderX - 8, shoulderY + 1);
        ctx.quadraticCurveTo(midX - 3, midY + 5, gripX, gripY + 1);
        ctx.lineTo(gripX + 1, gripY - 3);
        ctx.quadraticCurveTo(midX + 3, midY - 6, shoulderX + 1, shoulderY - 4);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

        ctx.strokeStyle = '#c5cfdc';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(shoulderX - 4, shoulderY + 2);
        ctx.quadraticCurveTo(midX - 4, midY + 3, midX + 5, midY + 2);
        ctx.stroke();
        ctx.restore();
      }

      function drawPelicanAndBike() {
        const bikeX = Math.min(W * 0.42, 320);
        const groundY = 324;
        const R = 31;
        const gearRatio = 2.15;

        const wheelAngle = state.distance / R;
        const crankAngle = wheelAngle / gearRatio;
        const cadenceBob = Math.sin(crankAngle * 2) * 1.4;

        const rx = bikeX - 46, ry = groundY - R;
        const fx = bikeX + 52, fy = groundY - R;
        const bbx = bikeX - 8, bby = groundY - R + 3;
        const sx = bikeX - 24, sy = groundY - R - 34;
        const saddleX = sx - 4, saddleY = sy - 10;
        const htbx = bikeX + 36, htby = groundY - R - 28;
        const httx = bikeX + 32, htty = groundY - R - 45;
        const stemX = httx - 2, stemY = htty - 12;
        const gripX = stemX - 11, gripY = stemY + 2;

        const crankLen = 14;
        const rPedalX = bbx + Math.cos(crankAngle) * crankLen;
        const rPedalY = bby + Math.sin(crankAngle) * crankLen;
        const lPedalX = bbx + Math.cos(crankAngle + Math.PI) * crankLen;
        const lPedalY = bby + Math.sin(crankAngle + Math.PI) * crankLen;

        const hipX = saddleX + 3;
        const hipY = saddleY - 3 + cadenceBob * 0.7;
        const shoulderX = hipX + 18;
        const shoulderY = hipY - 24 + cadenceBob;

        ctx.fillStyle = 'rgba(0, 0, 0, 0.34)';
        ctx.beginPath();
        ctx.ellipse(bikeX + 3, groundY + 2, 74, 6.5, 0, 0, Math.PI * 2);
        ctx.fill();

        const lampGlow = blendScalar('lampGlow');
        if (lampGlow > 0.1) {
          const beamGrad = ctx.createLinearGradient(httx + 12, htty + 2, httx + 165, groundY);
          beamGrad.addColorStop(0, `rgba(255, 240, 175, ${(0.42 * lampGlow).toFixed(3)})`);
          beamGrad.addColorStop(1, 'rgba(255, 240, 175, 0)');
          ctx.fillStyle = beamGrad;
          ctx.beginPath();
          ctx.moveTo(httx + 10, htty + 2);
          ctx.lineTo(httx + 165, groundY - 26);
          ctx.lineTo(httx + 165, groundY + 4);
          ctx.closePath();
          ctx.fill();
        }

        drawPelicanLeg(hipX - 2, hipY, lPedalX, lPedalY, true);
        drawCrankAndPedal(bbx, bby, lPedalX, lPedalY, true);

        drawWheel(rx, ry, R, wheelAngle);
        drawWheel(fx, fy, R, wheelAngle);
        drawDrivetrain(rx, ry, bbx, bby, crankAngle);

        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';

        ctx.strokeStyle = '#175e55';
        ctx.lineWidth = 3.5;
        ctx.beginPath(); ctx.arc(rx, ry, R + 4.5, Math.PI * 0.92, Math.PI * 1.68); ctx.stroke();
        ctx.beginPath(); ctx.arc(fx, fy, R + 4.5, Math.PI * 1.12, Math.PI * 1.75); ctx.stroke();

        ctx.strokeStyle = '#1e7a6e';
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.moveTo(rx, ry); ctx.lineTo(bbx, bby);
        ctx.moveTo(rx, ry); ctx.lineTo(sx, sy);
        ctx.moveTo(bbx, bby); ctx.lineTo(sx, sy);
        ctx.moveTo(bbx, bby); ctx.lineTo(htbx, htby);
        ctx.moveTo(sx, sy); ctx.lineTo(httx, htty);
        ctx.moveTo(htbx, htby); ctx.lineTo(httx, htty);
        ctx.moveTo(htbx, htby);
        ctx.quadraticCurveTo(htbx + 6, htby + 16, fx, fy);
        ctx.stroke();

        ctx.strokeStyle = '#38a898';
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(sx + 2, sy - 1); ctx.lineTo(httx - 2, htty - 1); ctx.stroke();

        ctx.strokeStyle = '#c8d0d8';
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(saddleX + 2, saddleY + 2); ctx.stroke();

        ctx.fillStyle = '#6b3f24';
        ctx.beginPath(); ctx.ellipse(saddleX, saddleY, 13, 4.5, -0.06, 0, Math.PI * 2); ctx.fill();

        ctx.fillStyle = '#ff3b4a';
        ctx.beginPath(); ctx.arc(rx - 22, ry - 26, 2.8, 0, Math.PI * 2); ctx.fill();
        if (lampGlow > 0.1) {
          ctx.fillStyle = `rgba(255, 60, 75, ${(0.35 * lampGlow).toFixed(3)})`;
          ctx.beginPath(); ctx.arc(rx - 22, ry - 26, 8, 0, Math.PI * 2); ctx.fill();
        }

        drawFrontBasket(httx + 5, htty - 6, fx, fy, cadenceBob * 0.4);

        ctx.strokeStyle = '#d4dce4';
        ctx.lineWidth = 3.2;
        ctx.beginPath();
        ctx.moveTo(httx, htty);
        ctx.lineTo(stemX, stemY);
        ctx.quadraticCurveTo(stemX - 4, stemY - 2, gripX, gripY);
        ctx.stroke();

        ctx.strokeStyle = '#3b2b22';
        ctx.lineWidth = 4.6;
        ctx.beginPath(); ctx.moveTo(gripX + 3, gripY); ctx.lineTo(gripX - 5, gripY + 1); ctx.stroke();

        ctx.fillStyle = '#d4dce4';
        ctx.beginPath(); ctx.roundRect(httx + 4, htty - 1, 8, 6, 2); ctx.fill();
        ctx.fillStyle = lampGlow > 0.15 ? '#fff3b8' : '#e6eef5';
        ctx.beginPath(); ctx.arc(httx + 12, htty + 2, 2.6, 0, Math.PI * 2); ctx.fill();

        ctx.fillStyle = '#f2c94c';
        ctx.beginPath(); ctx.arc(stemX - 3, stemY - 3.5, 3.2, Math.PI, 0); ctx.fill();

        if (state.bellTimer > 0) {
          const prog = 1 - state.bellTimer / 0.75;
          ctx.strokeStyle = `rgba(255, 224, 110, ${(1 - prog).toFixed(3)})`;
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          ctx.arc(stemX - 3, stemY - 5, 5 + prog * 24, -Math.PI * 0.85, -Math.PI * 0.15);
          ctx.stroke();
          ctx.fillStyle = `rgba(255, 240, 175, ${(1 - prog).toFixed(3)})`;
          ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif';
          ctx.fillText('叮铃~', stemX - 10, stemY - 14 - prog * 18);
        }

        drawPelicanBodyAndHead(hipX, hipY, shoulderX, shoulderY, cadenceBob);
        drawCrankAndPedal(bbx, bby, rPedalX, rPedalY, false);
        drawPelicanLeg(hipX + 1, hipY + 1, rPedalX, rPedalY, false);
        drawPelicanWing(shoulderX, shoulderY, gripX - 1, gripY, cadenceBob);
      }

      function drawWindStreaks() {
        const alpha = Math.min(0.32, 0.08 + state.speedMul * 0.11);
        ctx.strokeStyle = `rgba(255, 255, 255, ${alpha.toFixed(3)})`;
        ctx.lineWidth = 1.1;
        for (const wl of state.windLines) {
          ctx.beginPath();
          ctx.moveTo(wl.x, wl.y);
          ctx.lineTo(wl.x + wl.len * state.speedMul, wl.y);
          ctx.stroke();
        }
      }

      function update(dt) {
        const lerpRate = Math.min(1, dt * 4.5);
        ['day', 'twilight', 'night'].forEach(m => {
          const target = state.targetMode === m ? 1 : 0;
          state.modeBlend[m] += (target - state.modeBlend[m]) * lerpRate;
        });

        if (state.paused) return;

        state.time += dt;
        const v = 155 * state.speedMul;
        state.distance += v * dt;

        state.boatX -= (14 + 4 * state.speedMul) * dt;
        if (state.boatX < -80) state.boatX = W + 80;

        state.boat2X += 9 * dt;
        if (state.boat2X > W + 70) state.boat2X = -70;

        if (state.bellTimer > 0) state.bellTimer = Math.max(0, state.bellTimer - dt);

        if (state.blinkDur > 0) {
          state.blinkDur = Math.max(0, state.blinkDur - dt);
        } else {
          state.blinkTimer -= dt;
          if (state.blinkTimer <= 0) {
            state.blinkDur = 0.14;
            state.blinkTimer = 2.6 + Math.random() * 2.8;
          }
        }

        for (const wl of state.windLines) {
          wl.x -= v * wl.speed * dt;
          if (wl.x < -80) {
            wl.x = W + 20 + Math.random() * 90;
            wl.y = 195 + Math.random() * 145;
          }
        }
      }

      function draw() {
        ctx.clearRect(0, 0, W, H);
        drawSky();
        drawSkylineAndRiver();
        drawBundPromenadeAndRoad();
        drawPelicanAndBike();
        drawWindStreaks();
      }

      let lastTs = performance.now();
      function frame(ts) {
        const dt = Math.min(0.05, Math.max(0, (ts - lastTs) / 1000));
        lastTs = ts;
        update(dt);
        draw();
        requestAnimationFrame(frame);
      }

      updateReadout();
      requestAnimationFrame(frame);
    })();
  </script>
</div>

## 评测参数与生成记录

相比静态 SVG 版的经典鹈鹕测试（Pelican riding a bicycle），连续帧 Canvas 动画额外考察模型对多帧几何闭合、齿轮线速度同步与真实城市空间方位的联合约束能力。本次实测的完整运行参数如下：

| 参数项 | 配置与实测记录 |
|---|---|
| 评测基准 | 鹈鹕测试（Pelican Benchmark）上海外滩动态交互版（60fps Canvas） |
| 生成模型 | Google Gemini 4 Neon（Gemini 4 系列 Tier S 主力工程模型） |
| 模型档位（Mode / Tier） | Medium |
| 思考深度（Thinking Depth） | Medium（中等思考深度，1M 原生上下文窗口） |
| 运行宿主（Harness） | Google Antigravity（Agentic 模式，启用文件编辑与 `node --check` 语法校验） |
| 原始提示词（Prompt） | `帮我画一张鹈鹕骑着自行车在上海外滩骑行的代码动画，要求符合上海本地特色，要求动画流畅，要求逻辑合理。` |
| 交互与返工轮次 | 单轮直出（Zero-shot，0 次人工追问返工） |
| 端到端耗时 | 4 分 15 秒（20:12:54 提交提示词 → 20:17:09 完成自检交付） |
| 产物规格 | 单文件原生 Canvas 2D + Web Audio API，零第三方图形库、零外部图片与音频 |

1. **中等思考深度已够覆盖空间与传动约束。** Gemini 4 Neon 在 Medium 思考深度下一次性对齐了浦西东望陆家嘴的北南地标顺序、钻石车架闭合拓扑、分层视差与齿比联动，无需切到最高思考档消耗额外输出 token。
2. **同轮静态几何自检消除了下止点脱节。** 模型在写出首版代码后同一轮内复算髋关节到五通与脚踏的距离，发现脚踏处于 6 点钟下止点时跨度为 66.8px，而初版大腿加小腿仅 46px（22px + 24px），当场将二连杆臂长修补为 71px（35px + 36px）并通过 `node --check` 校验。

## 地理方位与外滩元素

视角定在中山东一路西侧、面向正东，鹈鹕从左向右骑行即自北向南沿非机动车道前进。外滩亲水观景平台禁行自行车，因此画面把骑行道放在防汛墙外侧的沥青路面，并按真实地理方位排列背景：

1. **陆家嘴天际线按北到南排序。** 从左到右依次为东方明珠广播电视塔、金茂大厦、上海中心大厦与上海环球金融中心，与从浦西外滩东望浦东的实景一致。
2. **中景与近景分层视差。** 天际线滚动系数 0.065x、江面水波 0.16x、防汛墙石栏杆 0.55x、外滩双头铸铁路灯与蓝底白字「中山东一路」路牌 0.85x，路面标线保持 1.0x 与轮胎接触面同步。
3. **上海本地细节落在道具层。** 车架采用复古墨绿城市车涂装并配前后挡泥板与车灯，前车筐放置外带咖啡杯和牛皮纸袋，江面加入双向缓慢错行的浦江游船。

## 机械传动与二连杆逆运动学

多数代码绘制的自行车动画容易出现轮速与地面脱节、腿部穿模或脚掌脱离脚踏。这段实现把整套运动链收口到单一世界位移 `distance`：

```text
wheelAngle = distance / R                  # 轮胎无滑移滚动角
crankAngle = wheelAngle / 2.15             # 2.15:1 齿比换算曲柄角
chainOffset = -(crankAngle * rChainring)   # 链条虚线相位与牙盘线速度一致
knee = solveIK2Bone(hip, pedal, 35, 36, -1) # 二连杆逆解，膝盖朝行进方向弯曲
```

这段关系保证了任意配速下地面虚线位移等于车轮滚动弧长。大腿 35px 加小腿 36px 的总臂长 71px 略大于髋关节到脚踏最低点的最大距离 66.8px，踩到 6 点钟方向时膝盖保留自然微屈，不会拉直或脱节。

## 渲染与交互控制

主循环采用 `requestAnimationFrame` 配合限幅 `dt`（上限 50ms），避免切后台回来后画面跳变。时段切换通过三组色板权重线性插值完成，天空渐变、江面倒影、建筑霓虹与路灯光晕同步过渡；车铃声效由 Web Audio API 在点击时按双音正弦波实时合成，不加载外部音频文件。
