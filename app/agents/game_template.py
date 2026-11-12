"""降级游戏模板：LLM 不可用时 Engineer 使用的确定性 Canvas 游戏。

实现 window.__game 自动化契约（QA / Player Simulation 以此驱动）：
  start() 进入 playing；action('collect') 加 1 分；action('miss') 漏接 +1；
  action('move_left'/'move_right') 移动；state() 返回
  {status, score, target, misses, loseLimit, winText, loseText}。
"""

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>__TITLE__</title>
<style>
  body { margin: 0; background: #101418; color: #eee; font-family: sans-serif;
         display: flex; flex-direction: column; align-items: center; }
  #stage { position: relative; }
  canvas { display: block; background: #1b2430; }
  .overlay { position: absolute; inset: 0; display: none; align-items: center;
             justify-content: center; font-size: 42px; font-weight: bold;
             text-shadow: 0 2px 8px #000; }
  #win-text { color: #ffd75e; }
  #lose-text { color: #ff6b6b; }
</style>
</head>
<body>
<button id="start-button" type="button">开始游戏</button>
<div id="stage">
  <canvas id="game" width="480" height="640"></canvas>
  <div id="win-text" class="overlay">__WIN_TEXT_HTML__</div>
  <div id="lose-text" class="overlay">__LOSE_TEXT_HTML__</div>
</div>
<script>
(function () {
  var canvas = document.getElementById('game');
  var ctx = canvas.getContext('2d');
  var W = canvas.width, H = canvas.height;

  var CONFIG = __CONFIG_JSON__;

  var state = {
    status: 'ready',
    score: 0,
    misses: 0,
    player: { x: W / 2, y: H - 48, w: 56, h: 16, speed: 24 },
    coins: [],
    spawnTimer: 0
  };

  var playerImg = new Image(), coinImg = new Image();
  var playerReady = false, coinReady = false;
  playerImg.onload = function () { playerReady = true; };
  coinImg.onload = function () { coinReady = true; };
  playerImg.onerror = function () { playerReady = false; };
  coinImg.onerror = function () { coinReady = false; };
  playerImg.src = 'assets/player.png';
  coinImg.src = 'assets/coin.png';

  function spawnCoin() {
    state.coins.push({ x: 24 + Math.random() * (W - 48), y: -16, vy: 2 + Math.random() * 2 });
  }

  function setStatus(next) {
    state.status = next;
    var winEl = document.getElementById('win-text');
    var loseEl = document.getElementById('lose-text');
    if (next === 'win') { winEl.style.display = 'flex'; loseEl.style.display = 'none'; }
    else if (next === 'lose') { loseEl.style.display = 'flex'; winEl.style.display = 'none'; }
    else { winEl.style.display = 'none'; loseEl.style.display = 'none'; }
  }

  function addScore() {
    if (state.status !== 'playing') return;
    state.score += 1;
    if (state.score >= CONFIG.target) setStatus('win');
  }

  function addMiss() {
    if (state.status !== 'playing') return;
    state.misses += 1;
    if (state.misses >= CONFIG.loseLimit) setStatus('lose');
  }

  function move(dir) {
    var p = state.player;
    if (dir === 'move_left') p.x = Math.max(p.w / 2, p.x - p.speed);
    if (dir === 'move_right') p.x = Math.min(W - p.w / 2, p.x + p.speed);
  }

  // --- 自动化契约 ---
  window.__game = {
    start: function () {
      if (state.status === 'ready' || state.status === 'playing') setStatus('playing');
      return state.status;
    },
    action: function (name) {
      if (name === 'collect') { addScore(); return true; }
      if (name === 'miss') { addMiss(); return true; }
      if (name === 'move_left' || name === 'move_right') { move(name); return true; }
      return false;
    },
    state: function () {
      return {
        status: state.status,
        score: state.score,
        target: CONFIG.target,
        misses: state.misses,
        loseLimit: CONFIG.loseLimit,
        playerX: state.player.x,
        winText: CONFIG.winText,
        loseText: CONFIG.loseText
      };
    }
  };

  document.getElementById('start-button').addEventListener('click', window.__game.start);
  document.addEventListener('keydown', function (event) {
    if (state.status !== 'playing') return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); move('move_left'); }
    if (event.key === 'ArrowRight') { event.preventDefault(); move('move_right'); }
  });

  function catchCheck() {
    var p = state.player;
    for (var i = state.coins.length - 1; i >= 0; i--) {
      var c = state.coins[i];
      if (Math.abs(c.x - p.x) < p.w / 2 + 12 && Math.abs(c.y - p.y) < 24) {
        state.coins.splice(i, 1);
        addScore();
      } else if (c.y > H + 16) {
        state.coins.splice(i, 1);
        addMiss();
      }
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    // 金币
    for (var i = 0; i < state.coins.length; i++) {
      var c = state.coins[i];
      if (coinReady) ctx.drawImage(coinImg, c.x - 12, c.y - 12, 24, 24);
      else {
        ctx.fillStyle = '#ffd75e';
        ctx.beginPath();
        ctx.arc(c.x, c.y, 12, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    // 玩家
    var p = state.player;
    if (playerReady) ctx.drawImage(playerImg, p.x - p.w / 2, p.y - p.h / 2, p.w, p.h);
    else {
      ctx.fillStyle = '#4da3ff';
      ctx.fillRect(p.x - p.w / 2, p.y - p.h / 2, p.w, p.h);
    }
    // 分数
    ctx.fillStyle = '#fff';
    ctx.font = '20px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('分数: ' + state.score + ' / ' + CONFIG.target, 12, 28);
    ctx.fillText('漏接: ' + state.misses + ' / ' + CONFIG.loseLimit, 12, 56);
  }

  var last = 0;
  function loop(ts) {
    if (state.status === 'playing') {
      var dt = Math.min(50, ts - last || 16);
      state.spawnTimer += dt;
      if (state.spawnTimer > 700) { state.spawnTimer = 0; spawnCoin(); }
      for (var i = 0; i < state.coins.length; i++) state.coins[i].y += state.coins[i].vy * dt / 16;
      catchCheck();
    }
    last = ts;
    draw();
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();
</script>
</body>
</html>
"""


def render_template(
    title: str,
    target: int,
    lose_limit: int,
    win_text: str,
    lose_text: str,
) -> str:
    import html
    import json

    title = str(title)[:80]
    win_text = str(win_text)[:40]
    lose_text = str(lose_text)[:40]
    config = json.dumps(
        {
            "target": max(1, min(100, int(target))),
            "loseLimit": max(1, min(20, int(lose_limit))),
            "winText": win_text,
            "loseText": lose_text,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return (
        TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__WIN_TEXT_HTML__", html.escape(win_text))
        .replace("__LOSE_TEXT_HTML__", html.escape(lose_text))
        .replace("__CONFIG_JSON__", config)
    )
