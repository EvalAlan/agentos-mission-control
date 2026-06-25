export const GlassCard = ({ children, className = '', style = '' }) => `
  <div class="glass-card ${className}" style="
    background: var(--bg-glass);
    backdrop-filter: var(--blur-heavy);
    border: 1px solid var(--border-glass);
    border-radius: var(--radius-lg);
    ${style}
  ">${children}</div>
`;

export const Badge = ({ text, color = 'var(--text-muted)', variant = 'subtle' }) => {
  const bg = variant === 'solid' ? color : `${color}20`;
  const border = variant === 'solid' ? 'none' : `1px solid ${color}40`;
  return `<span class="badge" style="
    background: ${bg};
    color: ${color};
    border: ${border};
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-full);
    font: 500 var(--font-size-xs) var(--font-mono);
    letter-spacing: var(--letter-spacing-wide);
    text-transform: uppercase;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    white-space: nowrap;
  ">${text}</span>`;
};

export const StatCard = ({ label, value, accent = 'var(--brand-cyan)', subtext, barWidth = '100%' }) => `
  <div class="stat-card" style="
    background: var(--bg-glass);
    backdrop-filter: var(--blur-heavy);
    border: 1px solid var(--border-glass);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    position: relative;
    overflow: hidden;
  ">
    <div style="font: 400 var(--font-size-xs) var(--font-mono); color: var(--text-muted); letter-spacing: var(--letter-spacing-wide); text-transform: uppercase;">${label}</div>
    <div style="font: 700 var(--font-size-2xl) var(--font-display); color: ${accent}; margin: var(--space-1) 0;">${value}</div>
    ${subtext ? `<div style="font: 400 var(--font-size-sm) var(--font-mono); color: var(--text-muted);">${subtext}</div>` : ''}
    <div style="position: absolute; bottom: 0; left: 0; height: 2px; width: ${barWidth}; background: ${accent}; border-radius: var(--radius-full);"></div>
  </div>
`;

export const ProgressBar = ({ pct, color = 'var(--brand-cyan)', height = '6px' }) => `
  <div style="background: rgba(255,255,255,0.05); border-radius: var(--radius-full); height: ${height}; overflow: hidden; width: 100%;">
    <div style="height: 100%; width: ${Math.min(100, Math.max(0, pct))}%; background: ${color}; border-radius: var(--radius-full); transition: width var(--transition-normal);"></div>
  </div>
`;

export const ThinBar = ({ values, color }) => {
  const max = Math.max(...values, 1);
  const bars = values.slice(-7).reverse().map((v, i) => {
    const h = Math.max(2, (v / max) * 32);
    return `<div style="width: calc(100% / 7 - 2px); height: ${h}px; background: ${color}; border-radius: var(--radius-sm); opacity: ${v > 0 ? 0.85 : 0.15};"></div>`;
  }).join('');
  return `<div style="display: flex; gap: 2px; align-items: flex-end; height: 32px;">${bars}</div>`;
};

export const DonutChart = ({ slices, total, size = 130 }) => {
  if (total === 0) return `<canvas width="${size}" height="${size}" style="width:${size}px;height:${size}px"></canvas>`;
  return `<canvas width="${size}" height="${size}" style="width:${size}px;height:${size}px"
    data-slices='${JSON.stringify(slices)}'
    data-total='${total}'
  ></canvas>`;
};

export function drawDonut(canvas) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const slices = JSON.parse(canvas.dataset.slices || '[]');
  const total = Number(canvas.dataset.total || '0');
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2, r = w * 0.42, lw = w * 0.12;

  const resolveColor = (color) => {
    if (!color || !color.startsWith('var(')) return color || '#7DD3FC';
    const name = color.slice(4, -1).trim();
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#7DD3FC';
  };

  ctx.clearRect(0, 0, w, h);
  ctx.lineCap = 'round';

  if (!total || slices.every(s => !s.value)) {
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.lineWidth = lw;
    ctx.strokeStyle = 'rgba(125,211,252,0.18)';
    ctx.stroke();
    return;
  }

  let angle = -Math.PI / 2;
  slices.filter(s => s.value > 0).forEach(s => {
    const sliceAngle = (s.value / total) * 2 * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, r, angle, angle + sliceAngle);
    ctx.lineWidth = lw;
    ctx.strokeStyle = resolveColor(s.color);
    ctx.shadowColor = resolveColor(s.color);
    ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.shadowBlur = 0;
    angle += sliceAngle;
  });
}
