(function() {
  "use strict";

  // Telegram WebApp Setup
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      if (tg.enableClosingConfirmation) tg.enableClosingConfirmation();
    } catch(e) {}
  }

  // Sound Synthesizer (Zero External Audio Assets)
  const SoundFX = {
    ctx: null,
    enabled: localStorage.getItem("poker_sound") !== "0",
    init() {
      if (!this.ctx) {
        try {
          const AudioCtx = window.AudioContext || window.webkitAudioContext;
          if (AudioCtx) this.ctx = new AudioCtx();
        } catch(e) {}
      }
      if (this.ctx && this.ctx.state === "suspended") {
        try { this.ctx.resume(); } catch(e) {}
      }
    },
    toggle() {
      this.enabled = !this.enabled;
      localStorage.setItem("poker_sound", this.enabled ? "1" : "0");
      return this.enabled;
    },
    chip() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;
      try {
        const t = this.ctx.currentTime;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(2200, t);
        osc.frequency.exponentialRampToValueAtTime(1400, t + 0.05);
        gain.gain.setValueAtTime(0.25, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.06);
        osc.connect(gain);
        gain.connect(this.ctx.destination);
        osc.start(t);
        osc.stop(t + 0.07);
      } catch(e) {}
    },
    deal() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;
      try {
        const t = this.ctx.currentTime;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = "triangle";
        osc.frequency.setValueAtTime(400, t);
        osc.frequency.exponentialRampToValueAtTime(110, t + 0.08);
        gain.gain.setValueAtTime(0.2, t);
        gain.gain.exponentialRampToValueAtTime(0.01, t + 0.08);
        osc.connect(gain);
        gain.connect(this.ctx.destination);
        osc.start(t);
        osc.stop(t + 0.09);
      } catch(e) {}
    },
    knock() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;
      try {
        const t = this.ctx.currentTime;
        [0, 0.08].forEach((delay) => {
          try {
            const osc = this.ctx.createOscillator();
            const gain = this.ctx.createGain();
            osc.type = "sine";
            osc.frequency.setValueAtTime(140, t + delay);
            osc.frequency.exponentialRampToValueAtTime(50, t + delay + 0.05);
            gain.gain.setValueAtTime(0.35, t + delay);
            gain.gain.exponentialRampToValueAtTime(0.01, t + delay + 0.05);
            osc.connect(gain);
            gain.connect(this.ctx.destination);
            osc.start(t + delay);
            osc.stop(t + delay + 0.06);
          } catch(e) {}
        });
      } catch(e) {}
    },
    fanfare() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;
      try {
        const t = this.ctx.currentTime;
        const notes = [523.25, 659.25, 783.99, 1046.50];
        notes.forEach((freq, idx) => {
          try {
            const osc = this.ctx.createOscillator();
            const gain = this.ctx.createGain();
            osc.type = "triangle";
            osc.frequency.setValueAtTime(freq, t + idx * 0.09);
            gain.gain.setValueAtTime(0.22, t + idx * 0.09);
            gain.gain.exponentialRampToValueAtTime(0.001, t + idx * 0.09 + 0.3);
            osc.connect(gain);
            gain.connect(this.ctx.destination);
            osc.start(t + idx * 0.09);
            osc.stop(t + idx * 0.09 + 0.32);
          } catch(e) {}
        });
      } catch(e) {}
    },
    turnAlert() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;
      try {
        const t = this.ctx.currentTime;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(880, t);
        osc.frequency.setValueAtTime(1174, t + 0.08);
        gain.gain.setValueAtTime(0.25, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
        osc.connect(gain);
        gain.connect(this.ctx.destination);
        osc.start(t);
        osc.stop(t + 0.26);
      } catch(e) {}
    }
  };

  // Telegram Haptic Feedback
  function triggerHaptic(type = "light") {
    if (tg && tg.HapticFeedback) {
      try {
        if (type === "success" || type === "warning" || type === "error") {
          tg.HapticFeedback.notificationOccurred(type);
        } else {
          tg.HapticFeedback.impactOccurred(type);
        }
      } catch(e) {}
    }
  }

  // Room & WebSocket URL
  const params = new URLSearchParams(location.search);
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || "";
  const room = (params.get("room") || startParam || "main").slice(0, 48);
  const initData = (tg && tg.initData) || "";

  const $ = (id) => document.getElementById(id);

  let ws = null;
  let state = null;
  let myUid = null;
  let reconnectTimer = null;
  let lastEventMsg = "";
  let currentRaiseVal = 0;
  let lastTurnUid = null;
  let displayedHeroStack = 0;
  let turnTimerTicker = null;
  let currentTurnSec = 120;


  // Format chip numbers with spaces
  function fmtChips(n) {
    const num = Number(n || 0);
    return num.toLocaleString("ru-RU").replace(/\u00A0/g, " ");
  }

  // 4-Color Deck Parsing (Spades: White, Hearts: Red, Diamonds: Sky Cyan, Clubs: Emerald)
  function parseCard(c) {
    if (!c || c === "🂠") return { isBack: true };
    const clean = c.trim();
    const lastChar = clean.slice(-1);
    const rank = clean.slice(0, -1);
    let suitClass = "suit-spade";
    let icon = "♠";
    if (lastChar === "♥" || lastChar === "h") {
      suitClass = "suit-heart"; icon = "♥";
    } else if (lastChar === "♦" || lastChar === "d") {
      suitClass = "suit-diamond"; icon = "♦";
    } else if (lastChar === "♣" || lastChar === "c") {
      suitClass = "suit-club"; icon = "♣";
    } else {
      suitClass = "suit-spade"; icon = "♠";
    }
    return { rank, icon, suitClass, isBack: false };
  }

  function createCardEl(cardStr, isHero = false) {
    const info = parseCard(cardStr);
    const el = document.createElement("div");
    if (info.isBack) {
      el.className = `p-card card-back ${isHero ? "hero-pocket-card" : ""}`;
      return el;
    }
    el.className = `p-card ${info.suitClass} ${isHero ? "hero-pocket-card" : ""}`;
    el.innerHTML = `
      <div class="card-rank-top">${escapeHtml(info.rank)}</div>
      <div class="card-suit-mid">${info.icon}</div>
      <div class="card-rank-bot">${escapeHtml(info.rank)}</div>
    `;
    return el;
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function showToast(text) {
    if (!text) return;
    const t = $("eventToast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 3000);
  }

  // Render Full Screen
  function render() {
    if (!state) return;
    myUid = state.you ? state.you.uid : null;

    // 1. Zone 1: Header Bar & Pot
    const roundTxt = state.phase === "lobby" ? "ЛОББИ СТОЛА" : `РАЗДАЧА #${state.hand_no || 1} · ${(state.street || 'ИГРА').toUpperCase()}`;
    $("headerRoundText").textContent = roundTxt;
    $("headerPotVal").textContent = fmtChips(state.pot);
    $("centerPotSum").textContent = fmtChips(state.pot);

    // Hero Balance Counter Animation & Spectator Badge
    const meSeat = (state.seats || []).find(s => s.is_me);
    const newStack = meSeat ? meSeat.stack : 0;
    const balEl = $("heroHeaderBalance");
    if (displayedHeroStack !== newStack) {
      if (displayedHeroStack !== 0) {
        balEl.classList.remove("glow-inc", "glow-dec");
        void balEl.offsetWidth; // reflow
        balEl.classList.add(newStack > displayedHeroStack ? "glow-inc" : "glow-dec");
        setTimeout(() => balEl.classList.remove("glow-inc", "glow-dec"), 1200);
      }
      displayedHeroStack = newStack;
    }
    if (activeGameMode === "poker") {
      if (!state.you || !state.you.seated) {
        $("heroBalanceText").textContent = "Зритель";
      } else {
        $("heroBalanceText").textContent = fmtChips(displayedHeroStack);
      }
    }

    // Spectator Pill Update
    const specCount = Number(state.spectators_count || 0);
    const specPill = $("specPill");
    if (specPill) {
      if (specCount > 0 || (state.you && !state.you.seated)) {
        specPill.style.display = "flex";
        $("specCountVal").textContent = specCount;
      } else {
        specPill.style.display = "none";
      }
    }

    // 2. Zone 2: Community Cards on Board
    renderBoardCards();

    // 3. Zone 2: Player Pods
    renderSeats();

    // 4. Toast on Events
    if (state.last_event && state.last_event !== lastEventMsg) {
      lastEventMsg = state.last_event;
      showToast(lastEventMsg);
      if (lastEventMsg.includes("забрал") || lastEventMsg.includes("победил") || lastEventMsg.includes("Банк")) {
        SoundFX.fanfare();
        triggerHaptic("success");
      }
    }

    // 5. Sound on My Turn
    const isMyTurn = (state.current_turn === myUid) && (state.phase === "playing");
    if (isMyTurn && lastTurnUid !== myUid) {
      SoundFX.turnAlert();
      triggerHaptic("warning");
    }
    lastTurnUid = state.current_turn;

    // 6. Zone 3: Hero Cards & Hand Combo
    renderHeroCards();

    // 7. Zone 3: Bottom Action Panel
    renderActionPanel();
  }

  // Render Community Cards
  function renderBoardCards() {
    const row = $("boardCardsRow");
    row.innerHTML = "";
    const cards = state.board || [];
    for (let i = 0; i < 5; i++) {
      if (i < cards.length) {
        row.appendChild(createCardEl(cards[i]));
      } else {
        const slot = document.createElement("div");
        slot.className = "card-slot-placeholder";
        row.appendChild(slot);
      }
    }
  }

  // Balanced radial position mapping for any table size (2 to 8 players)
  const SEAT_LAYOUTS = {
    1: [4],
    2: [4, 0],
    3: [4, 1, 7],
    4: [4, 2, 0, 6],
    5: [4, 2, 1, 7, 6],
    6: [4, 3, 1, 0, 7, 5],
    7: [4, 3, 2, 1, 7, 6, 5],
    8: [4, 5, 6, 7, 0, 1, 2, 3]
  };

  function getVisualPosition(idx, total, myIdx) {
    const layout = SEAT_LAYOUTS[total] || SEAT_LAYOUTS[8];
    if (myIdx === -1) {
      // Spectator view: distribute evenly according to layout
      return layout[idx % layout.length];
    }
    // Rotate relative to Hero so Hero is always at layout[0] (Pos 4)
    const relIdx = (idx - myIdx + total) % total;
    return layout[relIdx % layout.length];
  }

  // Render Seats Around Table
  function renderSeats() {
    const container = $("seatsContainer");
    container.innerHTML = "";
    const seats = state.seats || [];
    const dealerUid = state.dealer_uid;
    const payouts = state.last_payouts || {};

    let myIdx = seats.findIndex(s => s.is_me);
    const totalSeats = seats.length;

    seats.forEach((s, idx) => {
      // Smart radial layout: perfectly spreads 2 to 8 players around the table
      const visualPos = getVisualPosition(idx, totalSeats, myIdx);

      const pod = document.createElement("div");
      const isTurn = s.is_turn && state.phase === "playing";
      const isFolded = s.folded;
      const isBusted = (!s.in_table && s.stack <= 0) || s.stack <= 0;

      pod.className = `player-pod ${isTurn ? "is-turn" : ""} ${s.is_me ? "is-me" : ""} ${isFolded ? "folded" : ""} ${isBusted ? "busted" : ""}`;
      pod.setAttribute("data-pos", visualPos);

      // Opponent Mini Pocket Cards
      if (s.cards && s.cards.length && !s.is_me && !isFolded && !isBusted) {
        const oppCardsWrap = document.createElement("div");
        oppCardsWrap.className = "opponent-mini-cards";
        s.cards.forEach(c => oppCardsWrap.appendChild(createCardEl(c)));
        pod.appendChild(oppCardsWrap);
      }

      // Avatar Capsule with Circular Timer
      const avWrap = document.createElement("div");
      avWrap.className = "avatar-wrapper";

      // Circular SVG Countdown Timer Ring
      if (isTurn) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "timer-ring-svg");
        svg.setAttribute("viewBox", "0 0 56 56");
        
        const circleBg = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circleBg.setAttribute("class", "timer-ring-bg");
        circleBg.setAttribute("cx", "28");
        circleBg.setAttribute("cy", "28");
        circleBg.setAttribute("r", "24");
        
        const circleProg = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circleProg.setAttribute("class", "timer-ring-prog");
        circleProg.setAttribute("id", "timerRingProg");
        circleProg.setAttribute("cx", "28");
        circleProg.setAttribute("cy", "28");
        circleProg.setAttribute("r", "24");
        
        svg.appendChild(circleBg);
        svg.appendChild(circleProg);
        avWrap.appendChild(svg);
      }

      const avInner = document.createElement("div");
      avInner.className = "avatar-inner";
      avInner.textContent = (s.name || "P").slice(0, 2).toUpperCase();

      const avatarSrc = s.avatar || (s.uid ? `/api/avatar/${s.uid}` : "");
      if (avatarSrc) {
        const img = document.createElement("img");
        img.className = "avatar-img";
        img.src = avatarSrc;
        img.alt = s.name || "P";
        img.onerror = () => {
          img.style.display = "none";
          avInner.style.display = "flex";
        };
        img.onload = () => {
          avInner.style.display = "none";
        };
        avWrap.appendChild(img);
      }
      avWrap.appendChild(avInner);

      // Dealer Chip 'D'
      if (dealerUid && s.uid === dealerUid) {
        const dChip = document.createElement("div");
        dChip.className = "dealer-chip";
        dChip.textContent = "D";
        avWrap.appendChild(dChip);
      }
      pod.appendChild(avWrap);

      // Info Capsule (Name + Stack)
      const capsule = document.createElement("div");
      capsule.className = "pod-info-capsule";

      let statusTxt = "";
      if (isBusted) statusTxt = "Выбыл";
      else if (s.disqualified) statusTxt = "⛔ Выбыл";
      else if (s.folded) statusTxt = "Пас";
      else if (s.all_in) statusTxt = "ALL-IN";
      else if (s.showdown) statusTxt = s.showdown;
      else if (s.last_action) statusTxt = s.last_action;

      capsule.innerHTML = `
        <div class="pod-nickname">${escapeHtml(s.name)}${s.is_me ? " (ты)" : ""}${s.is_host ? " 👑" : ""}</div>
        <div class="pod-stack">🪙 ${fmtChips(s.stack)}</div>
        ${statusTxt ? `<div class="pod-status-text">${escapeHtml(statusTxt)}</div>` : ""}
      `;
      pod.appendChild(capsule);

      // BUG FIX: Do NOT display street bet badge for busted players or when hand is over
      if (s.bet > 0 && !isBusted && state.phase === "playing") {
        const betBadge = document.createElement("div");
        betBadge.className = "pod-bet-badge";
        betBadge.textContent = `🪙 ${fmtChips(s.bet)}`;
        pod.appendChild(betBadge);
      }

      // Flying Win Label (+3,400) if player received payout in this hand
      if (payouts[s.uid] && payouts[s.uid] > 0) {
        const winTag = document.createElement("div");
        winTag.className = "pod-win-floater";
        winTag.textContent = `+${fmtChips(payouts[s.uid])}`;
        pod.appendChild(winTag);
      }

      container.appendChild(pod);
    });
  }

  // Render Hero Pocket Cards & Combo Banner
  function renderHeroCards() {
    const deck = $("heroCardsDeck");
    deck.innerHTML = "";
    const comboBadge = $("handComboBadge");

    if (!state.you || !state.you.seated) {
      deck.classList.add("spectator-hidden");
      deck.innerHTML = "";
      comboBadge.style.display = "none";
      return;
    }
    deck.classList.remove("spectator-hidden");

    // Read cards from state.you.hole with fallback to meSeat.cards for full compatibility
    const meSeat = (state.seats || []).find(s => s.is_me);
    let cards = (state.you && state.you.hole && state.you.hole.length) ? state.you.hole : [];
    if (!cards.length && meSeat && meSeat.cards && meSeat.cards.length && meSeat.cards[0] !== "🂠") {
      cards = meSeat.cards;
    }

    if (cards.length && cards[0] !== "🂠") {
      cards.forEach(c => {
        const cardEl = createCardEl(c, true);
        if (state.you && state.you.combo && (state.phase === "between_hands" || state.phase === "finished")) {
          cardEl.classList.add("winning-highlight");
        }
        deck.appendChild(cardEl);
      });
    } else if (state.phase === "playing") {
      deck.innerHTML = `
        <div class="card-slot-placeholder hero-slot"></div>
        <div class="card-slot-placeholder hero-slot"></div>
      `;
    }

    // Auto-combo hint is HIDDEN during betting rounds, only displayed at showdown!
    const combo = state.you && state.you.combo;
    const isShowdown = (state.phase === "between_hands" || state.phase === "finished");
    if (combo && cards.length && cards[0] !== "🂠" && isShowdown) {
      comboBadge.style.display = "flex";
      $("handComboText").textContent = combo;
    } else {
      comboBadge.style.display = "none";
    }
  }


  // Render Bottom Action Panel
  function renderActionPanel() {
    const panel = $("heroActionPanel");
    panel.innerHTML = "";
    const phase = state.phase;
    const you = state.you || {};
    const opts = you.options || {};
    const meSeat = (state.seats || []).find(s => s.is_me);

    // 1. Lobby Phase
    if (phase === "lobby") {
      const lobbyBox = document.createElement("div");
      lobbyBox.style.cssText = "display:flex; flex-direction:column; gap:8px;";
      
      const seatedCount = (state.seats || []).length;
      const canStart = seatedCount >= (state.min_players || 2);

      lobbyBox.innerHTML = `
        <div class="turn-wait-banner" style="padding:10px;">
          <span>Игроков за столом: <b>${seatedCount}</b> / 9 (мин. ${state.min_players || 2})</span>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn-act btn-call" id="lobbyJoinBtn" style="flex:1;">
            ${you.seated ? "✅ Ты за столом" : "🪑 Сесть за стол"}
          </button>
          <button class="btn-act btn-raise" id="lobbyStartBtn" style="flex:1.2;" ${canStart ? "" : "disabled"}>
            ▶️ Начать игру
          </button>
        </div>
      `;
      panel.appendChild(lobbyBox);

      const jBtn = $("lobbyJoinBtn");
      if (jBtn) {
        jBtn.disabled = you.seated;
        jBtn.onclick = () => {
          SoundFX.chip();
          triggerHaptic("medium");
          send("join");
        };
      }
      const sBtn = $("lobbyStartBtn");
      if (sBtn) {
        sBtn.onclick = () => {
          SoundFX.deal();
          triggerHaptic("heavy");
          send("start");
        };
      }
      return;
    }

    // 2. Between Hands
    if (phase === "between_hands") {
      panel.innerHTML = `
        <div class="turn-wait-banner" style="flex-direction:column; gap:8px;">
          <div style="display:flex; align-items:center; gap:8px;">
            <div class="turn-wait-spinner"></div>
            <span>Раздача завершена · Новая раздача начнётся через пару секунд...</span>
          </div>
          <button class="btn-act" id="forceNextHandBtn" style="display:none; height:34px; padding:0 18px; font-size:13px; font-weight:700; background:linear-gradient(135deg, #10b981, #059669); color:#fff; border-radius:8px; border:none; cursor:pointer; box-shadow: 0 2px 10px rgba(16,185,129,0.3); transition:all 0.2s;">
            ▶️ Раздать сейчас
          </button>
        </div>
      `;
      setTimeout(() => {
        const btn = $("forceNextHandBtn");
        if (btn && state && state.phase === "between_hands") {
          btn.style.display = "inline-flex";
          btn.onclick = () => {
            SoundFX.deal();
            triggerHaptic("medium");
            send("next_hand");
          };
        }
      }, 3500);
      return;
    }

    // 3. Finished / Tournament Over
    if (phase === "finished") {
      panel.innerHTML = `
        <div class="turn-wait-banner" style="flex-direction:column; gap:10px; padding:16px;">
          <span style="font-size:16px; font-weight:800; color:var(--cyber-gold);">👑 Турнир окончен!</span>
          <button class="btn-act btn-fold" id="closeFinTableBtn" style="height:44px; width:200px;">
            🛑 Закрыть стол
          </button>
        </div>
      `;
      const cBtn = $("closeFinTableBtn");
      if (cBtn) {
        cBtn.onclick = () => {
          send("close_table");
          showToast("Стол закрыт.");
        };
      }
      return;
    }

    // 4. Closed State
    if (phase === "closed") {
      panel.innerHTML = `
        <div class="turn-wait-banner" style="flex-direction:column; gap:6px;">
          <span style="font-size:15px; color:#ef4444; font-weight:800;">🛑 Стол закрыт</span>
          <span style="font-size:11px;">Игра завершена организатором.</span>
        </div>
      `;
      return;
    }

    // 5. Playing Phase: NOT My Turn (or Spectator)
    const isMyTurn = (state.current_turn === myUid) && Object.keys(opts).length > 0;
    if (!isMyTurn) {
      const curTurnSeat = (state.seats || []).find(s => s.uid === state.current_turn);
      const turnName = curTurnSeat ? curTurnSeat.name : "другого игрока";

      if (!you.seated) {
        panel.innerHTML = `
          <div class="turn-wait-banner spectator-banner" style="flex-direction: column; gap: 8px; padding: 12px 14px;">
            <div style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
              <span style="display: flex; align-items: center; gap: 6px; font-weight: 800; color: var(--neon-cyan); font-size: 13px;">
                <span class="pulse-dot"></span> 👀 РЕЖИМ ЗРИТЕЛЯ
              </span>
              <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-num);">Раздача #${state.hand_no || 1}</span>
            </div>
            <div style="font-size: 13px; color: #fff;">
              Ходит <b>${escapeHtml(turnName)}</b> · Банк: <b style="color: var(--cyber-gold);">🪙 ${fmtChips(state.pot)}</b>
            </div>
            <div style="font-size: 11px; color: var(--text-muted);">
              Карты участников откроются при вскрытии.
            </div>
          </div>
        `;
        return;
      }

      panel.innerHTML = `
        <div class="turn-wait-banner">
          <div class="turn-wait-spinner"></div>
          <span>Ходит <b>${escapeHtml(turnName)}</b> · Ждите свою очередь...</span>
        </div>
      `;
      return;
    }

    // 6. Playing Phase: ACTIVE MY TURN!
    const toCall = you.to_call || 0;
    const heroStack = (meSeat && meSeat.stack) || 0;
    const heroBet = (meSeat && meSeat.bet) || 0;
    const maxRaise = opts.max_raise_to || (heroStack + heroBet);
    const minRaise = opts.min_raise_to || (state.current_bet + state.big_blind);

    if (currentRaiseVal < minRaise || currentRaiseVal > maxRaise) {
      currentRaiseVal = Math.min(minRaise, maxRaise);
    }

    const actionBox = document.createElement("div");
    actionBox.style.cssText = "display:flex; flex-direction:column; gap:8px;";

    // Raise Slider Box (if raise is permitted)
    if (opts.can_raise && maxRaise >= minRaise) {
      const raiseBox = document.createElement("div");
      raiseBox.className = "raise-control-box";

      // 4 Multipliers Row (Min, 1/2 Pot, Pot, All-in, Custom)
      const multRow = document.createElement("div");
      multRow.className = "multipliers-row";

      const halfPot = Math.min(maxRaise, Math.max(minRaise, Math.floor(state.pot / 2) + state.current_bet));
      const fullPot = Math.min(maxRaise, Math.max(minRaise, state.pot + state.current_bet));

      const presets = [
        { label: "Мин", val: minRaise },
        { label: "½ Пота", val: halfPot },
        { label: "Пот", val: fullPot },
        { label: "All-in", val: maxRaise },
        { label: "✍️ Своя", isCustom: true }
      ];

      presets.forEach(pr => {
        const btn = document.createElement("button");
        btn.className = "multiplier-chip";
        btn.textContent = pr.label;
        if (pr.isCustom) {
          btn.style.color = "var(--cyber-gold)";
          btn.style.borderColor = "var(--cyber-gold)";
        }
        btn.onclick = () => {
          if (pr.isCustom) {
            openCustomBetModal(minRaise, maxRaise, currentRaiseVal);
            return;
          }
          currentRaiseVal = pr.val;
          updateRaiseSlider();
          SoundFX.chip();
          triggerHaptic("light");
        };
        multRow.appendChild(btn);
      });
      raiseBox.appendChild(multRow);

      // Slider Row: [-] [===●===] [+]
      const sRow = document.createElement("div");
      sRow.className = "slider-row";

      const minusBtn = document.createElement("button");
      minusBtn.className = "stepper-btn";
      minusBtn.textContent = "−";
      minusBtn.onclick = () => {
        currentRaiseVal = Math.max(minRaise, currentRaiseVal - (state.big_blind || 1000));
        updateRaiseSlider();
        SoundFX.chip();
        triggerHaptic("light");
      };

      const trackWrap = document.createElement("div");
      trackWrap.className = "slider-track-wrap";

      const tooltip = document.createElement("div");
      tooltip.className = "slider-tooltip";
      tooltip.id = "sliderTooltip";
      tooltip.textContent = fmtChips(currentRaiseVal);

      const slider = document.createElement("input");
      slider.type = "range";
      slider.className = "poker-range-slider";
      slider.id = "pokerSlider";
      slider.min = minRaise;
      slider.max = maxRaise;
      slider.step = state.big_blind || 1000;
      slider.value = currentRaiseVal;

      slider.oninput = (e) => {
        currentRaiseVal = Number(e.target.value);
        updateRaiseSlider();
        triggerHaptic("light");
      };

      trackWrap.appendChild(tooltip);
      trackWrap.appendChild(slider);

      const plusBtn = document.createElement("button");
      plusBtn.className = "stepper-btn";
      plusBtn.textContent = "+";
      plusBtn.onclick = () => {
        currentRaiseVal = Math.min(maxRaise, currentRaiseVal + (state.big_blind || 1000));
        updateRaiseSlider();
        SoundFX.chip();
        triggerHaptic("light");
      };

      sRow.appendChild(minusBtn);
      sRow.appendChild(trackWrap);
      sRow.appendChild(plusBtn);
      raiseBox.appendChild(sRow);

      actionBox.appendChild(raiseBox);
    }

    // Primary Action Buttons (Thumb Zone: Fold, Check/Call, Raise)
    const actGrid = document.createElement("div");
    actGrid.className = "actions-grid";

    // 1. Fold Button
    const foldBtn = document.createElement("button");
    foldBtn.className = "btn-act btn-fold";
    foldBtn.innerHTML = `<span>↩️ Пас</span><span class="btn-sub-label">Сбросить</span>`;
    foldBtn.onclick = () => {
      try { SoundFX.deal(); } catch(e) {}
      try { triggerHaptic("medium"); } catch(e) {}
      send("action", { action: "fold" });
    };
    actGrid.appendChild(foldBtn);

    // 2. Check / Call Button
    const callBtn = document.createElement("button");
    callBtn.className = "btn-act btn-call";
    if (toCall <= 0) {
      callBtn.innerHTML = `<span>✅ Чек</span><span class="btn-sub-label">Бесплатно</span>`;
      callBtn.onclick = () => {
        try { SoundFX.knock(); } catch(e) {}
        try { triggerHaptic("light"); } catch(e) {}
        send("action", { action: "check" });
      };
    } else {
      callBtn.innerHTML = `<span>💰 Колл</span><span class="btn-sub-label">${fmtChips(toCall)}</span>`;
      callBtn.onclick = () => {
        try { SoundFX.chip(); } catch(e) {}
        try { triggerHaptic("medium"); } catch(e) {}
        send("action", { action: "call" });
      };
    }
    actGrid.appendChild(callBtn);

    // 3. Raise / All-in Button
    if (opts.can_raise && maxRaise >= minRaise) {
      const raiseBtn = document.createElement("button");
      raiseBtn.className = "btn-act btn-raise";
      raiseBtn.id = "mainRaiseBtn";
      const isAllInRaise = (currentRaiseVal >= maxRaise);
      raiseBtn.innerHTML = isAllInRaise 
        ? `<span>🚨 ALL-IN</span><span class="btn-sub-label">${fmtChips(currentRaiseVal)}</span>`
        : `<span>📈 Рейз</span><span class="btn-sub-label">${fmtChips(currentRaiseVal)}</span>`;
      raiseBtn.onclick = () => {
        try { SoundFX.chip(); } catch(e) {}
        try { triggerHaptic("heavy"); } catch(e) {}
        if (currentRaiseVal >= maxRaise) {
          send("action", { action: "allin" });
        } else {
          send("action", { action: "raise", amount: currentRaiseVal });
        }
      };
      actGrid.appendChild(raiseBtn);
    } else if (opts.all_in) {
      const allInBtn = document.createElement("button");
      allInBtn.className = "btn-act btn-raise";
      allInBtn.innerHTML = `<span>🚨 ALL-IN</span><span class="btn-sub-label">${fmtChips(heroStack)}</span>`;
      allInBtn.onclick = () => {
        try { SoundFX.chip(); } catch(e) {}
        try { triggerHaptic("heavy"); } catch(e) {}
        send("action", { action: "allin" });
      };
      actGrid.appendChild(allInBtn);
    }

    actionBox.appendChild(actGrid);
    panel.appendChild(actionBox);

    // Initialize raise slider visual positions
    updateRaiseSlider();
  }

  // BUG FIX #2: Synchronize slider.value, tooltip and CSS gradient fill
  function updateRaiseSlider() {
    const slider = $("pokerSlider");
    const tooltip = $("sliderTooltip");
    const raiseBtn = $("mainRaiseBtn");
    if (!slider) return;

    slider.value = currentRaiseVal;

    const min = Number(slider.min) || 0;
    const max = Number(slider.max) || 100;
    const pct = max > min ? Math.max(0, Math.min(1, (currentRaiseVal - min) / (max - min))) : 0;
    
    slider.style.setProperty("--slider-pct", `${(pct * 100).toFixed(1)}%`);

    if (tooltip) {
      tooltip.textContent = fmtChips(currentRaiseVal);
      tooltip.style.left = `${(pct * 100).toFixed(1)}%`;
    }

    if (raiseBtn) {
      const isAllIn = (currentRaiseVal >= max);
      raiseBtn.innerHTML = isAllIn
        ? `<span>🚨 ALL-IN</span><span class="btn-sub-label">${fmtChips(currentRaiseVal)}</span>`
        : `<span>📈 Рейз</span><span class="btn-sub-label">${fmtChips(currentRaiseVal)}</span>`;
    }
  }

  // Turn Timer Countdown Visual & Circular Ring Ticker
  function updateTurnTimerVisual() {
    const progEl = $("timerRingProg");
    if (!progEl) return;
    
    const total = (state && state.turn_timeout_sec) ? state.turn_timeout_sec : 120;
    const pct = Math.max(0, Math.min(1, currentTurnSec / total));
    const circ = 157.0; // 2 * PI * 25
    const offset = circ * (1 - pct);
    
    progEl.style.strokeDashoffset = offset;
    if (currentTurnSec <= 5) {
      progEl.classList.add("danger");
      // Pulse haptic on <= 3 sec
      if (state.current_turn === myUid && currentTurnSec <= 3 && currentTurnSec > 0) {
        triggerHaptic("warning");
      }
    } else {
      progEl.classList.remove("danger");
    }
  }

  function initTurnTimerTicker() {
    if (turnTimerTicker) clearInterval(turnTimerTicker);
    turnTimerTicker = setInterval(() => {
      if (state && state.phase === "playing" && state.current_turn) {
        if (currentTurnSec > 0) {
          currentTurnSec -= 1;
        }
        updateTurnTimerVisual();
      }
    }, 1000);
  }

  // Custom Bet Modal Handlers
  function openCustomBetModal(minVal, maxVal, curVal) {
    const modal = $("customBetModal");
    if (!modal) return;
    $("cbetMinLabel").textContent = fmtChips(minVal);
    $("cbetMaxLabel").textContent = fmtChips(maxVal);
    const input = $("customBetInput");
    input.min = minVal;
    input.max = maxVal;
    input.value = curVal || minVal;
    modal.classList.add("open");
    setTimeout(() => { input.focus(); input.select(); }, 150);
  }

  function addCustomBet(amt) {
    const input = $("customBetInput");
    if (!input) return;
    const min = Number(input.min) || 0;
    const max = Number(input.max) || 100000;
    let cur = parseInt(input.value, 10) || min;
    cur = Math.min(max, cur + amt);
    input.value = cur;
    SoundFX.chip();
    triggerHaptic("light");
  }

  function setCustomBetRatio(type) {
    const input = $("customBetInput");
    if (!input) return;
    const min = Number(input.min) || 0;
    const max = Number(input.max) || 100000;
    let val = min;
    if (type === "half") {
      val = Math.min(max, Math.max(min, Math.floor((state.pot || 0) / 2) + (state.current_bet || 0)));
    } else if (type === "pot") {
      val = Math.min(max, Math.max(min, (state.pot || 0) + (state.current_bet || 0)));
    } else if (type === "allin") {
      val = max;
    }
    input.value = val;
    SoundFX.chip();
    triggerHaptic("light");
  }

  function initCustomBetModalHandlers() {
    const confirmBtn = $("confirmCustomBetBtn");
    if (confirmBtn) {
      confirmBtn.onclick = () => {
        const input = $("customBetInput");
        const min = Number(input.min) || 0;
        const max = Number(input.max) || 100000;
        let val = parseInt(input.value, 10);
        if (isNaN(val) || val < min) val = min;
        if (val > max) val = max;
        currentRaiseVal = val;
        updateRaiseSlider();
        $("customBetModal").classList.remove("open");
        SoundFX.chip();
        triggerHaptic("heavy");
        if (currentRaiseVal >= max) {
          send("action", { action: "allin" });
        } else {
          send("action", { action: "raise", amount: currentRaiseVal });
        }
      };
    }
    const closeBtn = $("closeCustomBetModalBtn");
    if (closeBtn) closeBtn.onclick = () => $("customBetModal").classList.remove("open");
    const cancelBtn = $("cancelCustomBetBtn");
    if (cancelBtn) cancelBtn.onclick = () => $("customBetModal").classList.remove("open");

    const b1k = $("cbAdd1k"); if (b1k) b1k.onclick = () => addCustomBet(1000);
    const b5k = $("cbAdd5k"); if (b5k) b5k.onclick = () => addCustomBet(5000);
    const b10k = $("cbAdd10k"); if (b10k) b10k.onclick = () => addCustomBet(10000);
    const bHalf = $("cbHalf"); if (bHalf) bHalf.onclick = () => setCustomBetRatio("half");
    const bPot = $("cbPot"); if (bPot) bPot.onclick = () => setCustomBetRatio("pot");
    const bAllIn = $("cbAllin"); if (bAllIn) bAllIn.onclick = () => setCustomBetRatio("allin");
  }

  // Active Rooms Modal
  async function loadRoomsModal() {
    $("roomsModal").classList.add("open");
    const list = $("roomsModalList");
    list.innerHTML = "<div style='text-align:center; color:var(--text-muted); padding:16px;'>Загрузка столов...</div>";
    try {
      const res = await fetch("/api/rooms");
      const data = await res.json();
      if (!data.rooms || !data.rooms.length) {
        list.innerHTML = `
          <div style="text-align:center; color:var(--text-muted); padding:20px;">
            Нет других активных столов.<br>
            <span style="font-size:12px;">Запустите игру в Telegram командой <b>/holdem</b>!</span>
          </div>
        `;
        return;
      }
      list.innerHTML = "";
      data.rooms.forEach(r => {
        const item = document.createElement("div");
        item.className = "room-item-card";
        const isCurrent = r.code === room;
        item.innerHTML = `
          <div>
            <div style="font-weight:700; color:var(--text-main); font-size:13px;">${escapeHtml(r.chat_title)} ${isCurrent ? "⭐" : ""}</div>
            <div style="font-size:11px; color:var(--text-muted);">Игроков: ${r.players_count} · Банк: 🪙 ${fmtChips(r.pot)} · ${r.phase}</div>
          </div>
          <div style="font-size:12px; font-weight:700; color:var(--cyber-gold);">
            ${isCurrent ? "Вы здесь" : "Войти →"}
          </div>
        `;
        item.onclick = () => {
          if (!isCurrent) {
            location.href = `/?room=${encodeURIComponent(r.code)}`;
          } else {
            $("roomsModal").classList.remove("open");
          }
        };
        list.appendChild(item);
      });
    } catch(e) {
      list.innerHTML = "<div style='text-align:center; color:#ef4444; padding:16px;'>Не удалось загрузить столы</div>";
    }
  }

  // Access Denied Screen (Private Group Chat Table)
  function showAccessDeniedScreen(msgText) {
    const root = document.querySelector(".app-root") || document.body;
    root.innerHTML = `
      <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:85vh; padding:24px; text-align:center;">
        <div style="font-size:56px; margin-bottom:16px;">🔒</div>
        <h2 style="font-family:var(--font-brand); color:var(--cyber-gold); margin-bottom:12px; font-size:22px;">Доступ ограничен</h2>
        <p style="color:var(--text-muted); max-width:320px; line-height:1.5; font-size:14px; margin-bottom:24px;">
          ${escapeHtml(msgText)}
        </p>
        <button onclick="if(window.Telegram&&Telegram.WebApp)Telegram.WebApp.close();else window.history.back();" 
                style="background:linear-gradient(135deg, #d97706, #b45309); color:#fff; border:none; padding:12px 28px; border-radius:12px; font-weight:700; font-size:15px; cursor:pointer;">
          Закрыть
        </button>
      </div>
    `;
  }

  // WebSocket Connection
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
      ws.send(JSON.stringify({
        type: "auth",
        initData: initData,
        room: room,
        dev_uid: params.get("dev_uid") || undefined,
        dev_name: params.get("dev_name") || undefined
      }));
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") {
          state = msg;
          if (state.turn_seconds_left !== undefined && state.turn_seconds_left !== null) {
            currentTurnSec = state.turn_seconds_left;
          } else {
            currentTurnSec = 120;
          }
          render();
          updateTurnTimerVisual();
        } else if (msg.type === "closed") {
          showToast("🛑 Стол закрыт.");
        } else if (msg.type === "error") {
          if (msg.error === "not_in_chat") {
            clearTimeout(reconnectTimer);
            showAccessDeniedScreen(msg.message || "Вы не состоите в этом чате.");
            return;
          }
          showToast("⚠️ " + (msg.error || "Ошибка"));
        }
      } catch(e) {}
    };

    ws.onclose = () => {
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 1500);
    };

    ws.onerror = () => {
      try { ws.close(); } catch(e) {}
    };
  }

  function send(type, extra) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(Object.assign({ type }, extra || {})));
    }
  }

  // Header Handlers
  $("soundBtn").onclick = () => {
    const isEnabled = SoundFX.toggle();
    $("soundBtn").textContent = isEnabled ? "🔊" : "🔇";
    triggerHaptic("light");
    if (isEnabled) SoundFX.chip();
  };
  $("soundBtn").textContent = SoundFX.enabled ? "🔊" : "🔇";

  $("roomsListBtn").onclick = () => {
    triggerHaptic("light");
    loadRoomsModal();
  };
  $("closeModalBtn").onclick = () => $("roomsModal").classList.remove("open");

  initCustomBetModalHandlers();
  initTurnTimerTicker();

  // Auto-check for active chat tables if opened on "main"
  async function checkActiveChatRooms() {
    if (room !== "main") return;
    try {
      const res = await fetch("/api/rooms");
      const data = await res.json();
      if (data && data.rooms) {
        const chatRooms = data.rooms.filter(r => r.code !== "main");
        if (chatRooms.length > 0) {
          setTimeout(() => {
            if (room === "main" && (!state || !state.seats || state.seats.length <= 1)) {
              loadRoomsModal();
            }
          }, 800);
        }
      }
    } catch(e) {}
  }


  // =========================================================================
  // MULTI-GAME ROUTING & BLACKJACK (21) ENGINE
  // =========================================================================
  let activeGameMode = "poker";

  window.switchGameMode = function(mode) {
    activeGameMode = mode;
    if (mode === "blackjack") {
      $("tabPoker").classList.remove("active");
      $("tabBlackjack").classList.add("active");
      $("pokerScreen").style.display = "none";
      $("blackjackScreen").style.display = "flex";
      if ($("roomsListBtn")) $("roomsListBtn").style.display = "none";
      $("specPill").style.display = "none";
      BJ.init();
    } else {
      $("tabBlackjack").classList.remove("active");
      $("tabPoker").classList.add("active");
      $("blackjackScreen").style.display = "none";
      $("pokerScreen").style.display = "flex";
      if ($("roomsListBtn")) $("roomsListBtn").style.display = "flex";
      render();
    }
  };

  const BJ = {
    deck: [],
    dealerHand: [],
    playerHand: [],
    bet: 500,
    chips: 50000,
    phase: "betting", // 'betting', 'playing', 'dealer_turn', 'round_over'
    
    init() {
      const saved = localStorage.getItem("chudo_bj_chips");
      if (saved !== null && !isNaN(Number(saved))) {
        this.chips = Math.max(0, Number(saved));
      } else {
        this.chips = 50000;
        this.saveChips();
      }
      if (this.bet > this.chips) {
        this.bet = Math.min(this.chips, 500);
      }
      this.updateUI();
    },

    saveChips() {
      localStorage.setItem("chudo_bj_chips", String(this.chips));
    },

    createDeck() {
      const ranks = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"];
      const suits = ["c","d","h","s"];
      const d = [];
      for (const r of ranks) {
        for (const s of suits) {
          d.push(r + s);
        }
      }
      for (let i = d.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [d[i], d[j]] = [d[j], d[i]];
      }
      return d;
    },

    evalHand(cards) {
      let sum = 0;
      let aces = 0;
      for (const c of cards) {
        if (!c || c === "🂠") continue;
        const r = c.slice(0, -1);
        if (r === "A") {
          sum += 11;
          aces += 1;
        } else if (["K","Q","J","T","10"].includes(r)) {
          sum += 10;
        } else {
          sum += Number(r);
        }
      }
      while (sum > 21 && aces > 0) {
        sum -= 10;
        aces -= 1;
      }
      const isSoft = aces > 0 && sum <= 21;
      const isBust = sum > 21;
      const isBlackjack = cards.length === 2 && sum === 21;
      return { score: sum, isSoft, isBust, isBlackjack };
    },

    addBet(amt) {
      if (this.phase !== "betting") return;
      if (this.bet + amt > this.chips) {
        this.bet = this.chips;
      } else {
        this.bet += amt;
      }
      SoundFX.chip();
      triggerHaptic("light");
      this.updateUI();
    },

    clearBet() {
      if (this.phase !== "betting") return;
      this.bet = 0;
      SoundFX.knock();
      triggerHaptic("light");
      this.updateUI();
    },

    allInBet() {
      if (this.phase !== "betting") return;
      this.bet = this.chips;
      SoundFX.chip();
      triggerHaptic("medium");
      this.updateUI();
    },

    refillChips() {
      if (this.chips > 0) return;
      this.chips = 50000;
      this.saveChips();
      this.bet = 500;
      SoundFX.fanfare();
      triggerHaptic("success");
      showToast("Баланс пополнен: 🪙 50 000!");
      this.updateUI();
    },

    startDeal() {
      if (this.phase !== "betting") return;
      if (this.bet <= 0) {
        showToast("Сделайте ставку перед раздачей!");
        return;
      }
      if (this.bet > this.chips) {
        this.bet = this.chips;
      }
      this.chips -= this.bet;
      this.saveChips();

      this.deck = this.createDeck();
      this.playerHand = [];
      this.dealerHand = [];
      this.phase = "playing";

      $("bjStatusBanner").style.display = "none";
      SoundFX.deal();
      triggerHaptic("medium");

      this.playerHand.push(this.deck.pop());
      this.dealerHand.push(this.deck.pop());
      this.playerHand.push(this.deck.pop());
      this.dealerHand.push(this.deck.pop());

      this.updateUI();

      const pEval = this.evalHand(this.playerHand);
      if (pEval.isBlackjack) {
        setTimeout(() => this.finishRoundNaturalBJ(), 600);
      }
    },

    hit() {
      if (this.phase !== "playing") return;
      if (!this.deck.length) this.deck = this.createDeck();
      const card = this.deck.pop();
      this.playerHand.push(card);
      SoundFX.deal();
      triggerHaptic("light");

      const pEval = this.evalHand(this.playerHand);
      this.updateUI();

      if (pEval.isBust) {
        this.phase = "round_over";
        SoundFX.knock();
        triggerHaptic("heavy");
        this.showStatus("💥 Перебор! (" + pEval.score + ") Проигрыш", "#ef4444");
        this.saveChips();
        setTimeout(() => this.resetToBetting(), 2200);
      } else if (pEval.score === 21) {
        setTimeout(() => this.stand(), 400);
      }
    },

    double() {
      if (this.phase !== "playing" || this.playerHand.length !== 2) return;
      if (this.chips < this.bet) {
        showToast("Недостаточно фишек для удвоения!");
        return;
      }
      this.chips -= this.bet;
      this.bet *= 2;
      this.saveChips();
      SoundFX.chip();
      triggerHaptic("medium");

      if (!this.deck.length) this.deck = this.createDeck();
      const card = this.deck.pop();
      this.playerHand.push(card);
      SoundFX.deal();

      const pEval = this.evalHand(this.playerHand);
      this.updateUI();

      if (pEval.isBust) {
        this.phase = "round_over";
        SoundFX.knock();
        triggerHaptic("heavy");
        this.showStatus("💥 Перебор! (" + pEval.score + ") Проигрыш", "#ef4444");
        setTimeout(() => this.resetToBetting(), 2200);
      } else {
        setTimeout(() => this.stand(), 600);
      }
    },

    stand() {
      if (this.phase !== "playing") return;
      this.phase = "dealer_turn";
      this.updateUI();
      this.playDealerTurn();
    },

    playDealerTurn() {
      SoundFX.deal();
      this.updateUI();

      const step = () => {
        const dEval = this.evalHand(this.dealerHand);
        if (dEval.score < 17) {
          if (!this.deck.length) this.deck = this.createDeck();
          this.dealerHand.push(this.deck.pop());
          SoundFX.deal();
          triggerHaptic("light");
          this.updateUI();
          setTimeout(step, 600);
        } else {
          this.evaluateWinner();
        }
      };
      setTimeout(step, 600);
    },

    finishRoundNaturalBJ() {
      this.phase = "round_over";
      const dEval = this.evalHand(this.dealerHand);
      this.updateUI();

      if (dEval.isBlackjack) {
        this.chips += this.bet;
        this.showStatus("🤝 Ничья! У обоих Блэкджек", "var(--neon-cyan)");
        SoundFX.knock();
      } else {
        const win = Math.floor(this.bet * 2.5);
        this.chips += win;
        this.showStatus("🏆 БЛЭКДЖЕК! +🪙 " + fmtChips(Math.floor(this.bet * 1.5)), "var(--cyber-gold)");
        SoundFX.fanfare();
        triggerHaptic("success");
      }
      this.saveChips();
      this.updateUI();
      setTimeout(() => this.resetToBetting(), 2600);
    },

    evaluateWinner() {
      this.phase = "round_over";
      const pScore = this.evalHand(this.playerHand).score;
      const dEval = this.evalHand(this.dealerHand);
      const dScore = dEval.score;

      if (dEval.isBust) {
        const win = this.bet * 2;
        this.chips += win;
        this.showStatus("🎉 Дилер перебрал! (" + dScore + ") Победа +🪙 " + fmtChips(this.bet), "var(--neon-emerald)");
        SoundFX.fanfare();
        triggerHaptic("success");
      } else if (pScore > dScore) {
        const win = this.bet * 2;
        this.chips += win;
        this.showStatus("🎉 Победа! (" + pScore + " против " + dScore + ") +🪙 " + fmtChips(this.bet), "var(--neon-emerald)");
        SoundFX.fanfare();
        triggerHaptic("success");
      } else if (pScore === dScore) {
        this.chips += this.bet;
        this.showStatus("🤝 Ничья! (" + pScore + " очков) Возврат ставки", "var(--neon-cyan)");
        SoundFX.knock();
        triggerHaptic("light");
      } else {
        this.showStatus("❌ Проигрыш (" + pScore + " против " + dScore + ")", "#ef4444");
        SoundFX.knock();
        triggerHaptic("medium");
      }

      this.saveChips();
      this.updateUI();
      setTimeout(() => this.resetToBetting(), 2600);
    },

    resetToBetting() {
      this.phase = "betting";
      if (this.bet > this.chips) {
        this.bet = Math.min(this.chips, 500);
      }
      $("bjStatusBanner").style.display = "none";
      this.updateUI();
    },

    showStatus(msg, color) {
      const banner = $("bjStatusBanner");
      banner.style.display = "block";
      banner.style.borderColor = color || "var(--cyber-gold)";
      banner.innerHTML = `<span style="color:${color || '#fff'}">${msg}</span>`;
    },

    updateUI() {
      $("bjPlayerChipsVal").textContent = fmtChips(this.chips);
      $("bjCurrentBetText").textContent = "🪙 " + fmtChips(this.bet);
      if (activeGameMode === "blackjack") {
        $("heroBalanceText").textContent = fmtChips(this.chips);
      }

      $("bjRefillBtn").style.display = (this.chips <= 0 && this.phase === "betting") ? "inline-block" : "none";

      const pRow = $("bjPlayerCards");
      pRow.innerHTML = "";
      this.playerHand.forEach(c => {
        const el = createCardEl(formatCardBJ(c));
        el.classList.add("card-deal-anim");
        pRow.appendChild(el);
      });
      const pEval = this.evalHand(this.playerHand);
      const pScoreEl = $("bjPlayerScore");
      if (this.playerHand.length === 0) {
        pScoreEl.textContent = "0";
        pScoreEl.className = "bj-score-badge";
      } else {
        pScoreEl.textContent = (pEval.isSoft && pEval.score < 21) ? `${pEval.score - 10}/${pEval.score}` : String(pEval.score);
        pScoreEl.className = "bj-score-badge" + (pEval.isBust ? " bust" : (pEval.isBlackjack ? " bj" : ""));
      }

      const dRow = $("bjDealerCards");
      dRow.innerHTML = "";
      const showHole = (this.phase === "dealer_turn" || this.phase === "round_over");
      this.dealerHand.forEach((c, idx) => {
        let cardStr = c;
        if (idx === 1 && !showHole) {
          cardStr = "🂠";
        } else {
          cardStr = formatCardBJ(c);
        }
        const el = createCardEl(cardStr);
        el.classList.add("card-deal-anim");
        dRow.appendChild(el);
      });

      const dScoreEl = $("bjDealerScore");
      if (this.dealerHand.length === 0) {
        dScoreEl.textContent = "0";
        dScoreEl.className = "bj-score-badge";
      } else if (!showHole) {
        const upCardEval = this.evalHand([this.dealerHand[0]]);
        dScoreEl.textContent = String(upCardEval.score);
        dScoreEl.className = "bj-score-badge";
      } else {
        const dEval = this.evalHand(this.dealerHand);
        dScoreEl.textContent = (dEval.isSoft && dEval.score < 21) ? `${dEval.score - 10}/${dEval.score}` : String(dEval.score);
        dScoreEl.className = "bj-score-badge" + (dEval.isBust ? " bust" : (dEval.isBlackjack ? " bj" : ""));
      }

      if (this.phase === "betting") {
        $("bjBettingControls").style.display = "flex";
        $("bjPlayControls").style.display = "none";
        $("bjDealBtn").disabled = this.bet <= 0 || this.chips <= 0;
      } else {
        $("bjBettingControls").style.display = "none";
        $("bjPlayControls").style.display = "flex";
        const isPlaying = this.phase === "playing";
        $("bjHitBtn").disabled = !isPlaying;
        $("bjStandBtn").disabled = !isPlaying;
        $("bjDoubleBtn").disabled = !isPlaying || this.playerHand.length !== 2 || this.chips < this.bet;
      }
    }
  };

  function formatCardBJ(c) {
    if (!c || c === "🂠") return "🂠";
    const r = c.slice(0, -1);
    const s = c.slice(-1);
    const rankMap = { "T": "10" };
    const suitIcon = { "c": "♣", "d": "♦", "h": "♥", "s": "♠" };
    return (rankMap[r] || r) + (suitIcon[s] || s);
  }

  window.bjAddBet = (amt) => BJ.addBet(amt);
  window.bjClearBet = () => BJ.clearBet();
  window.bjAllInBet = () => BJ.allInBet();
  window.bjStartDeal = () => BJ.startDeal();
  window.bjHit = () => BJ.hit();
  window.bjStand = () => BJ.stand();
  window.bjDouble = () => BJ.double();
  window.bjRefillChips = () => BJ.refillChips();

  // Desktop Monitor Keyboard Hotkeys
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    const key = e.key.toLowerCase();
    if (key === "m") {
      const menu = $("gameMenuModal");
      if (menu.classList.contains("open")) closeGameMenu();
      else openGameMenu();
      return;
    }
    if (key === "escape") {
      closeGameMenu();
      closeRanksModal();
      $("roomsModal").classList.remove("open");
      return;
    }
    // Only process table hotkeys if it's my turn
    if (activeGameMode !== "poker" || !state || state.current_turn !== myUid) return;
    const you = state.you || {};
    const opts = you.options || {};
    if (key === "f" && opts.can_fold) {
      triggerHaptic("medium");
      send("fold");
    } else if ((key === " " || key === "c")) {
      e.preventDefault();
      if (opts.can_check) {
        triggerHaptic("light");
        send("check");
      } else if (opts.can_call) {
        triggerHaptic("medium");
        send("call");
      }
    } else if (key === "r" && opts.can_raise) {
      triggerHaptic("heavy");
      send("raise", { amount: currentRaiseVal });
    }
  });

  // Game Menu & Hand Ranks Modal Controls
  window.openGameMenu = () => {
    $("gameMenuModal").classList.add("open");
    const isPoker = (activeGameMode === "poker");
    $("pokerActiveBadge").textContent = isPoker ? "Активно" : "";
    $("pokerActiveBadge").className = "menu-item-badge" + (isPoker ? " active" : "");
    $("menuSoundText").textContent = SoundFX.enabled ? "🔊 Звуковые эффекты: Включены" : "🔇 Звуковые эффекты: Выключены";
  };
  window.closeGameMenu = () => {
    $("gameMenuModal").classList.remove("open");
  };
  window.openRanksModal = () => {
    $("handRanksModal").classList.add("open");
  };
  window.closeRanksModal = () => {
    $("handRanksModal").classList.remove("open");
  };
  window.toggleSoundFromMenu = () => {
    const isEnabled = SoundFX.toggle();
    $("soundBtn").textContent = isEnabled ? "🔊" : "🔇";
    $("menuSoundText").textContent = isEnabled ? "🔊 Звуковые эффекты: Включены" : "🔇 Звуковые эффекты: Выключены";
    triggerHaptic("light");
    if (isEnabled) SoundFX.chip();
  };

  connect();
  checkActiveChatRooms();

  // URL Game Mode Check
  try {
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get("game") === "blackjack" || window.location.pathname.startsWith("/blackjack")) {
      switchGameMode("blackjack");
    }
  } catch(e) {}
})();
