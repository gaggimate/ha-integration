// --- IMPORT LIT ---
import {
  LitElement,
  html,
  css,
} from "https://unpkg.com/lit@3.1.2/index.js?module";

// --- HELPERS ---
// Discover GaggiMate devices by looking for mode select entities.
// Works with the official gaggimate/ha-integration.
const getGaggiMateDevices = (hass) => {
  if (!hass) return [];
  return Object.keys(hass.states)
    .filter(id => id.startsWith("select.") && id.includes("gaggimate") && id.endsWith("_mode"))
    .map(id => ({
      slug: id.split(".")[1].replace("_mode", ""),
      name: (() => {
        const n = hass.states[id].attributes.friendly_name || id;
        const lower = n.toLowerCase();
        if (lower.endsWith(" mode")) return n.slice(0, -5).trimEnd();
        if (lower.endsWith("mode"))  return n.slice(0, -4).trimEnd();
        return n;
      })()
    }));
};

// Lighten a hex color by blending with white at the given ratio (0=original, 1=white)
const lightenColor = (hex, ratio) => {
  const r = Number.parseInt(hex.slice(1, 3), 16);
  const g = Number.parseInt(hex.slice(3, 5), 16);
  const b = Number.parseInt(hex.slice(5, 7), 16);
  return `rgb(${Math.round(r + (255 - r) * ratio)},${Math.round(g + (255 - g) * ratio)},${Math.round(b + (255 - b) * ratio)})`;
};

// --- VISUAL EDITOR ---
class GaggiMateCardEditor extends LitElement {
  static get properties() { return { hass: {}, _config: {} }; }
  setConfig(config) { this._config = config; }

  _valueChanged(ev) {
    const target = ev.target;
    const config = { ...this._config };
    const value = target.tagName === "HA-SWITCH" ? target.checked : target.value;
    config[target.configValue] = value;
    if (target.configValue === "device_name") {
      const selected = getGaggiMateDevices(this.hass).find(d => d.slug === value);
      if (selected) config.name = selected.name;
    }
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }

  _resetColor() {
    const config = { ...this._config };
    delete config.color;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }

  render() {
    if (!this.hass || !this._config) return html``;
    const currentColor = this._config.color || "#ff9800";
    return html`
      <div class="card-config">
        <label class="label">Machine</label>
        <select class="std-input" .value="${this._config.device_name || ""}" .configValue="${"device_name"}" @change="${this._valueChanged}">
          <option value="" disabled>Select machine...</option>
          ${getGaggiMateDevices(this.hass).map(d => html`<option value="${d.slug}">${d.name}</option>`)}
        </select>

        <label class="label">Title</label>
        <input class="std-input" type="text" .value="${this._config.name || ""}" .configValue="${"name"}" @input="${this._valueChanged}" />

        <label class="label">Accent Color</label>
        <div class="color-row">
          <input type="color" class="color-input" .value="${currentColor}" .configValue="${"color"}" @input="${this._valueChanged}" />
          <span class="color-hint">${currentColor}</span>
          <button class="reset-btn" @click="${this._resetColor}">Reset to default</button>
        </div>

        <div class="sw-row"><span>Show Grinder</span><ha-switch .checked="${this._config.show_grinder !== false}" .configValue="${"show_grinder"}" @change="${this._valueChanged}"></ha-switch></div>
        <div class="sw-row"><span>Show Weight</span><ha-switch .checked="${this._config.show_weight !== false}" .configValue="${"show_weight"}" @change="${this._valueChanged}"></ha-switch></div>
      </div>`;
  }

  static get styles() {
    return css`
      .card-config { display: flex; flex-direction: column; gap: 12px; padding: 10px; }
      .label { font-weight: 500; font-size: 14px; color: var(--primary-text-color); }
      .std-input { width: 100%; padding: 10px; border: 1px solid var(--divider-color); border-radius: 4px; background: var(--card-background-color); color: var(--primary-text-color); box-sizing: border-box; }
      .color-row { display: flex; align-items: center; gap: 10px; }
      .color-input { width: 48px; height: 36px; border: 1px solid var(--divider-color); border-radius: 4px; padding: 2px; cursor: pointer; background: none; }
      .color-hint { font-size: 13px; color: var(--secondary-text-color); font-family: monospace; flex: 1; }
      .reset-btn { padding: 4px 10px; border: 1px solid var(--divider-color); border-radius: 4px; background: var(--secondary-background-color); color: var(--primary-text-color); cursor: pointer; font-size: 12px; }
      .reset-btn:hover { background: var(--divider-color); }
      .sw-row { display: flex; justify-content: space-between; align-items: center; color: var(--primary-text-color); }
    `;
  }
}
customElements.define("gaggimate-card-editor", GaggiMateCardEditor);

// --- MAIN CARD ---
class GaggiMateCard extends LitElement {
  static get properties() {
    return {
      hass: {},
      config: {},
      _dragTgt: { type: Number },   // live drag value (null when not dragging)
    };
  }

  constructor() {
    super();
    this._dragTgt = null;
    this._dragMax = 110;
    this._dragBound = {
      move: this._onDragMove.bind(this),
      end:  this._onDragEnd.bind(this),
    };
  }

  static getConfigElement() { return document.createElement("gaggimate-card-editor"); }

  static getStubConfig(hass) {
    const dev = getGaggiMateDevices(hass)[0];
    return { device_name: dev?.slug || "", name: dev?.name || "GaggiMate", show_grinder: true, show_weight: true };
  }

  setConfig(config) { this.config = config; }

  _getVal(suffix) {
    if (!this.hass || !this.config) return 0;
    const stateObj = this.hass.states[`sensor.${this.config.device_name}_${suffix}`];
    return stateObj ? Number.parseFloat(stateObj.state) || 0 : 0;
  }

  _getUnit(suffix) {
    if (!this.hass || !this.config) return "";
    const stateObj = this.hass.states[`sensor.${this.config.device_name}_${suffix}`];
    return stateObj?.attributes?.unit_of_measurement || "";
  }

  // Get the current target temperature from the number entity (setpoint)
  _getTargetTempFromNumber() {
    if (!this.hass || !this.config) return 0;
    const slug = this.config.device_name;
    const stateObj = this.hass.states[`number.${slug}_target_temperature_setpoint`];
    return stateObj ? Number.parseFloat(stateObj.state) || 0 : 0;
  }

  // stroke-dasharray arc segment technique
  // Arc: radius=40, center=(50,50), 270° from -135° to +135° clockwise
  // Total arc circumference = 2*PI*40*(270/360) = 188.496
  _dashArc(fromPct, toPct) {
    const C = 2 * Math.PI * 40 * (270 / 360); // 188.496
    const segLen = Math.max((toPct - fromPct) * C, 0);
    return {
      dashArray: `${segLen} ${C - segLen + 1}`,
      dashOffset: -(fromPct * C - 0.5)
    };
  }

  // Convert a pointer event position to an arc percentage (0–1) for the temperature dial.
  // The arc runs 270° clockwise from -135° (bottom-left) to +135° (bottom-right),
  // with 0% at the start and 100% at the end.
  _pointerToArcPct(ev, svgEl) {
    const rect = svgEl.getBoundingClientRect();
    // Map pointer to SVG viewBox coordinates (viewBox is 0 0 100 100)
    const svgX = ((ev.clientX - rect.left) / rect.width) * 100;
    const svgY = ((ev.clientY - rect.top) / rect.height) * 100;
    // Angle from center (50,50), in degrees, 0° = up (12 o'clock)
    const dx = svgX - 50;
    const dy = svgY - 50;
    let angleDeg = Math.atan2(dy, dx) * 180 / Math.PI + 90; // 0° = top
    if (angleDeg < 0) angleDeg += 360;
    // Arc starts at 225° (bottom-left, -135° from top) and spans 270° clockwise
    const arcStart = 225;
    const arcSpan = 270;
    let arcAngle = angleDeg - arcStart;
    if (arcAngle < 0) arcAngle += 360;
    // Clamp to arc span
    arcAngle = Math.max(0, Math.min(arcSpan, arcAngle));
    return arcAngle / arcSpan;
  }

  // --- Drag handlers ---
  _onHandlePointerDown(ev, svgEl, max) {
    ev.preventDefault();
    this._dragMax = max;
    this._dragSvgEl = svgEl;
    this._dragTgt = this._getTargetTempFromNumber();
    globalThis.addEventListener("pointermove", this._dragBound.move);
    globalThis.addEventListener("pointerup",   this._dragBound.end);
    globalThis.addEventListener("pointercancel", this._dragBound.end);
  }

  _onDragMove(ev) {
    if (this._dragTgt === null) return;
    const pct = this._pointerToArcPct(ev, this._dragSvgEl);
    const raw = pct * this._dragMax;
    // Round to 0.5°C steps for a smooth but not jittery feel
    this._dragTgt = Math.round(raw * 2) / 2;
    this.requestUpdate();
  }

  _onDragEnd(ev) {
    globalThis.removeEventListener("pointermove", this._dragBound.move);
    globalThis.removeEventListener("pointerup",   this._dragBound.end);
    globalThis.removeEventListener("pointercancel", this._dragBound.end);
    if (this._dragTgt !== null) {
      const finalTemp = this._dragTgt;
      this._dragTgt = null;
      // Send the new target temperature via the number entity setpoint
      const slug = this.config.device_name;
      this.hass.callService("number", "set_value", {
        entity_id: `number.${slug}_target_temperature_setpoint`,
        value: finalTemp,
      });
    }
  }

  // Adjust target temperature by delta (+1 or -1) using the number entity
  _adjustTemp(delta) {
    const slug = this.config.device_name;
    const entityId = `number.${slug}_target_temperature_setpoint`;
    const stateObj = this.hass.states[entityId];
    if (!stateObj) return;
    const current = Number.parseFloat(stateObj.state) || 0;
    const min = Number.parseFloat(stateObj.attributes.min) ?? 0;
    const max = Number.parseFloat(stateObj.attributes.max) ?? 160;
    const newVal = Math.min(max, Math.max(min, current + delta));
    this.hass.callService("number", "set_value", {
      entity_id: entityId,
      value: newVal,
    });
  }

  // --- Dial rendering helpers ---

  // Compute the current and target values for a dial.
  _computeDialValues(curSuffix, tgtSuffix, showButtons) {
    const isDragging = showButtons && this._dragTgt !== null;
    const cur = this._getVal(curSuffix);
    let tgt;
    if (isDragging) {
      tgt = this._dragTgt;
    } else if (showButtons) {
      tgt = this._getTargetTempFromNumber();
    } else {
      tgt = this._getVal(tgtSuffix);
    }
    return { cur, tgt, isDragging };
  }

  // Compute arc geometry: percentages and SVG dot/handle positions.
  _computeArcGeometry(cur, tgt, max) {
    const curPct = Math.min(Math.max(cur, 0), max) / max;
    const tgtPct = Math.min(Math.max(tgt, 0), max) / max;
    // Dot/handle positions on the arc (radius=40, center=50,50)
    const toRad = (pct) => ((- 135 + pct * 270) - 90) * Math.PI / 180;
    const dotX = (50 + 40 * Math.cos(toRad(curPct))).toFixed(2);
    const dotY = (50 + 40 * Math.sin(toRad(curPct))).toFixed(2);
    const handleX = (50 + 40 * Math.cos(toRad(tgtPct))).toFixed(2);
    const handleY = (50 + 40 * Math.sin(toRad(tgtPct))).toFixed(2);
    return { curPct, tgtPct, dotX, dotY, handleX, handleY };
  }

  // Compute arc zone segments and colors.
  // When heating: zone1 = arc-start → cur (full brand), zone2 = cur → tgt (mid brand)
  // When at temp: zone1 = arc-start → tgt (full brand), zone2 = empty
  _computeArcColors(curPct, tgtPct, isHeating, isDragging, brand) {
    const brandMid = lightenColor(brand, 0.5);
    const zone1End = isHeating ? curPct : tgtPct;
    const zone2Start = isHeating ? curPct : tgtPct;
    const zone1 = this._dashArc(0, zone1End);
    const zone2 = this._dashArc(zone2Start, tgtPct);
    const zone1Color = (isHeating || tgtPct > 0) ? brand : "transparent";
    const zone2Color = isHeating ? brandMid : "transparent";
    const dotColor = isHeating ? brand : "#aaa";
    const handleStroke = isDragging ? brand : (isHeating ? brandMid : "#aaa");
    const handleFill = isDragging ? brand : "white";
    const handleR = isDragging ? 5 : 3;
    return { zone1, zone2, zone1Color, zone2Color, dotColor, handleStroke, handleFill, handleR };
  }

  // Render the SVG arc dial.
  _renderDialSvg(geometry, colors, showButtons, onHandleDown) {
    const { dotX, dotY, handleX, handleY } = geometry;
    const { zone1, zone2, zone1Color, zone2Color, dotColor, handleStroke, handleFill, handleR } = colors;
    const FULL_ARC = "M 22 78 A 40 40 0 1 1 78 78";
    return html`
      <svg viewBox="0 0 100 100" style="overflow:visible; ${showButtons ? 'touch-action:none;' : ''}">
        <!-- Grey background track -->
        <path fill="none" stroke="#ccc" stroke-width="8" stroke-linecap="round" d="${FULL_ARC}" />
        <!-- Zone 1: achieved (full brand) -->
        <path fill="none" stroke="${zone1Color}" stroke-width="8" stroke-linecap="round"
          d="${FULL_ARC}"
          stroke-dasharray="${zone1.dashArray}"
          stroke-dashoffset="${zone1.dashOffset}" />
        <!-- Zone 2: heating (mid brand) -->
        <path fill="none" stroke="${zone2Color}" stroke-width="8" stroke-linecap="round"
          d="${FULL_ARC}"
          stroke-dasharray="${zone2.dashArray}"
          stroke-dashoffset="${zone2.dashOffset}" />
        <!-- Target handle: draggable circle on track -->
        <circle
          fill="${handleFill}" stroke="${handleStroke}" stroke-width="1.5"
          cx="${handleX}" cy="${handleY}" r="${handleR}"
          style="${showButtons ? 'cursor:grab; touch-action:none;' : ''}"
          @pointerdown="${showButtons ? onHandleDown : null}" />
        <!-- Current dot: small filled circle on track -->
        <circle fill="${dotColor}" cx="${dotX}" cy="${dotY}" r="2.5" />
      </svg>`;
  }

  // Render the data overlay (target value, separator, current value).
  _renderDialData(tgt, cur, unit, decimals, isDragging, isHeating, buttonsEnabled, brand) {
    const minusDisabled = !buttonsEnabled;
    const plusDisabled = !buttonsEnabled;
    return html`
      <div class="d-data">
        <div class="v-tgt-row">
          ${buttonsEnabled !== undefined ? html`
            <button class="temp-btn ${minusDisabled ? 'disabled' : ''}"
              @click="${() => { if (!minusDisabled) this._adjustTemp(-1); }}"
              ?disabled="${minusDisabled}">−</button>
          ` : ''}
          <div class="v-tgt ${isDragging ? 'dragging' : ''}">${tgt.toFixed(decimals)}<span class="v-unit">${unit}</span></div>
          ${buttonsEnabled !== undefined ? html`
            <button class="temp-btn ${plusDisabled ? 'disabled' : ''}"
              @click="${() => { if (!plusDisabled) this._adjustTemp(1); }}"
              ?disabled="${plusDisabled}">+</button>
          ` : ''}
        </div>
        <div class="v-sep"></div>
        <div class="v-cur-row">
          <span class="v-cur">${cur.toFixed(decimals)}<span class="v-unit">${unit}</span></span>
          ${isHeating ? html`
            <ha-icon class="heat-icon active" icon="mdi:heat-wave"
              style="color:${brand}"></ha-icon>
          ` : (buttonsEnabled !== undefined ? html`
            <ha-icon class="heat-icon" icon="mdi:heat-wave"></ha-icon>
          ` : '')}
        </div>
      </div>`;
  }

  _renderDial(label, curSuffix, tgtSuffix, max, showButtons = false) {
    const brand = this.config.color || "#ff9800";
    const unit = this._getUnit(curSuffix);
    const decimals = label === "PRESSURE" ? 2 : 1;

    const { cur, tgt, isDragging } = this._computeDialValues(curSuffix, tgtSuffix, showButtons);
    const geometry = this._computeArcGeometry(cur, tgt, max);
    const { curPct, tgtPct } = geometry;

    // Heating: current is more than 0.5 below target (only for temp dial)
    const isHeating = showButtons && !isDragging && cur < tgt - 0.5;

    const slug = this.config.device_name;
    const mode = this.hass.states[`select.${slug}_mode`]?.state || "Standby";
    const buttonsEnabled = showButtons ? (mode === "Brew") : undefined;

    const colors = this._computeArcColors(curPct, tgtPct, isHeating, isDragging, brand);

    const onHandleDown = showButtons
      ? (ev) => {
          const svgEl = ev.currentTarget.closest("svg");
          this._onHandlePointerDown(ev, svgEl, max);
        }
      : null;

    return html`
      <div class="dial">
        <div class="d-label">${label}</div>
        <div class="d-rel">
          ${this._renderDialSvg(geometry, colors, showButtons, onHandleDown)}
          ${this._renderDialData(tgt, cur, unit, decimals, isDragging, isHeating, buttonsEnabled, brand)}
        </div>
      </div>`;
  }

  render() {
    if (!this.hass || !this.config?.device_name) return html`<ha-card style="padding:16px">Check configuration.</ha-card>`;
    const slug = this.config.device_name;
    const mode = this.hass.states[`select.${slug}_mode`]?.state || "Standby";
    const profile = this.hass.states[`select.${slug}_profile`];
    const brand = this.config.color || "#ff9800";
    // Official integration uses "Hot Water" for the Water mode option value,
    // but we display it as "Water" to keep the 5 buttons fitting in the card.
    // modeOptions maps display label → actual HA option value.
    const modeOptions = [
      { label: "Standby",  value: "Standby" },
      { label: "Brew",     value: "Brew" },
      { label: "Steam",    value: "Steam" },
      { label: "Water",    value: "Hot Water" },
      ...(this.config.show_grinder !== false ? [{ label: "Grind", value: "Grind" }] : []),
    ];

    return html`
      <ha-card>
        <div class="card-header">${this.config.name}</div>
        ${profile ? html`
          <div class="pad">
            <ha-select label="Profile" .value="${profile.state}"
              @selected="${(e) => {
                // Only act on user-initiated changes, not initial render
                if (e.target.value && e.target.value !== profile.state) {
                  this.hass.callService('select', 'select_option', {entity_id: `select.${slug}_profile`, option: e.target.value});
                }
              }}">
              ${profile.attributes.options.map(opt => html`<mwc-list-item .value="${opt}">${opt}</mwc-list-item>`)}
            </ha-select>
          </div>` : ''}
        <div class="modes">
          ${modeOptions.map(m => html`
            <div class="m-btn ${mode === m.value ? 'active' : ''}"
              style="${mode === m.value ? `background:${brand};` : ''}"
              @click="${() => this.hass.callService('select', 'select_option', {entity_id: `select.${slug}_mode`, option: m.value})}">
              ${m.label.toUpperCase()}
            </div>`)}
        </div>
        <div class="dials">
          ${this._renderDial("TEMPERATURE", "current_temperature", "target_temperature", 110, true)}
          ${this._renderDial("PRESSURE", "current_pressure", "target_pressure", 15)}
        </div>
        ${this.config.show_weight !== false ? html`
          <div class="weight">
            <div class="w-sec"><div class="w-l">CURRENT</div><div>${this._getVal("current_weight").toFixed(1)}g</div></div>
            <div class="divider"></div>
            <div class="w-sec"><div class="w-l">TARGET</div><div>${this._getVal("target_shot_volume").toFixed(1)}g</div></div>
          </div>` : ''}
      </ha-card>`;
  }

  static get styles() {
    return css`
      .card-header { padding: 24px 16px 16px; font-size: var(--ha-card-header-font-size, 24px); line-height: var(--ha-card-header-line-height, 32px); color: var(--ha-card-header-color, var(--primary-text-color)); }
      .pad { padding: 0 16px 16px; }
      ha-select { width: 100%; }
      .modes { display: grid; grid-template-columns: repeat(auto-fit, minmax(65px, 1fr)); gap: 8px; padding: 0 16px 16px; }
      .m-btn { background: var(--secondary-background-color); padding: 10px; border-radius: 8px; text-align: center; cursor: pointer; font-size: 10px; font-weight: bold; color: var(--primary-text-color); }
      .m-btn.active { color: white; }
      .dials { display: flex; justify-content: space-around; padding: 0 8px 16px; }
      .dial { width: 46%; text-align: center; }
      .d-label { font-size: 10px; font-weight: bold; opacity: 0.6; margin-bottom: 4px; color: var(--primary-text-color); }
      .d-rel { position: relative; }
      svg { width: 100%; display: block; }
      /* Data overlay: centered at 56% so target temp sits at visual center */
      .d-data { position: absolute; top: 56%; width: 100%; transform: translateY(-50%); text-align: center; pointer-events: none; }
      /* Target temp row: flex row with buttons flanking the value */
      .v-tgt-row { display: flex; align-items: center; justify-content: center; gap: 6px; }
      .v-tgt { font-size: 22px; font-weight: bold; color: var(--primary-text-color); line-height: 1.1; }
      .v-tgt.dragging { color: var(--accent-color, #ff9800); }
      .v-unit { font-size: 12px; font-weight: normal; opacity: 0.7; }
      .v-sep { height: 1px; background: var(--divider-color); margin: 4px 20%; }
      .v-cur-row { display: flex; align-items: center; justify-content: center; gap: 4px; }
      .v-cur { font-size: 13px; color: var(--secondary-text-color); }
      .heat-icon { --mdc-icon-size: 16px; color: var(--disabled-text-color); opacity: 0.4; }
      .heat-icon.active { opacity: 1; }
      /* +/- buttons: inline siblings of target temp, vertically centred by flexbox */
      .temp-btn {
        width: 26px; height: 26px;
        border-radius: 50%;
        background: var(--card-background-color);
        border: 2px solid var(--primary-text-color);
        color: var(--primary-text-color);
        cursor: pointer;
        font-size: 18px; font-weight: bold; line-height: 1;
        display: flex; align-items: center; justify-content: center;
        padding: 0;
        pointer-events: auto;
        flex-shrink: 0;
      }
      .temp-btn:hover:not(.disabled) { background: var(--primary-text-color); color: var(--card-background-color); }
      .temp-btn.disabled { opacity: 0.3; cursor: not-allowed; }
      .weight { margin: 0 16px 16px; padding: 12px; background: var(--secondary-background-color); border-radius: 12px; display: flex; justify-content: space-around; text-align: center; color: var(--primary-text-color); }
      .w-l { font-size: 9px; opacity: 0.5; font-weight: bold; }
      .divider { width: 1px; background: var(--divider-color); }
    `;
  }
}
customElements.define("gaggimate-card", GaggiMateCard);

globalThis.customCards = globalThis.customCards || [];
globalThis.customCards.push({ type: "gaggimate-card", name: "GaggiMate", description: "GaggiMate Dashboard Card", preview: true });
