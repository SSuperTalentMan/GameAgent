# Phaser 引擎文档精选（Demo 语料：引擎 API）

## 游戏循环与场景
Phaser 3 的核心是 Scene。每个 Scene 有 init/preload/create/update 生命周期，update(time, delta) 每帧调用，delta 是帧间隔毫秒数。游戏状态机建议在 Scene 内维护 status 字段：ready → playing → win/lose，胜负切换集中在 setStatus() 一个入口，方便自动化测试钩子读取。Canvas 项目可仿照此结构：requestAnimationFrame 循环 + 每帧 update(delta)。

## Arcade 物理
Arcade Physics 提供轻量 AABB 碰撞：physics.add.overlap(player, coins, onCatch) 触发收集回调。速度用 setVelocity(x, y)，重力 gravity.y 作用于下落物体。Demo 级游戏可以不用完整物理引擎，用"每帧 y += vy * delta/16 + 矩形相交判断"近似实现，注意 delta 归一化避免高刷屏加速。

## 输入处理
keyboard.createCursorKeys() 返回 left/right/up/down/space 的 JustDown 状态。键盘输入要与自动化兼容：把"向左/向右/交互"封装成 action(name) 函数，键盘事件与测试钩子（window.__game.action）都调用同一入口，保证真实玩家与模拟玩家走同一条代码路径。

## 精灵与贴图
this.add.image(x, y, key) 创建贴图对象，setOrigin(0.5) 居中。贴图加载失败会显示绿框：务必监听 texture 加载错误并回退为纯色绘制（onerror → fallback flag），保证离线或资产缺失时游戏仍可运行。资产建议放相对路径 assets/ 目录。

## 文本与覆盖层
胜利/失败文案用 DOM 覆盖层（div.overlay + display:flex）比 Canvas 文本更易被 Playwright 断言（可见性检查 is_visible）。Canvas 文本作为补充，两者内容保持一致：状态对象里保存 winText/loseText 字符串，DOM 与绘制共用同一来源。

## 计时器与节奏
用 delta 累计实现节拍器：spawnTimer += dt，超过阈值生成下落物并重置。随机 x 与随机速度 2~4px/帧能让每局体验不同。节拍参数（生成间隔、下落速度）抽成 CONFIG 对象，是难度曲线调参的抓手。

## 碰撞与漏接判定
下落物 y > H + 半径 仍未被接住 → 漏接。判定放在对象移出屏幕时统一处理，避免在两处分别 +1 造成重复计数。接住判定用中心距离阈值（|c.x - p.x| < p.w/2 + r 且 |c.y - p.y| < 24），比精确 AABB 宽容，手感更好。

## 分数与状态暴露
自动化契约：window.__game.state() 返回 {status, score, target, misses, loseLimit, winText, loseText}。分数变化只在 addScore() 一处发生，胜利判定 score >= target 紧随其后。任何内部状态变化都必须同步反映到 state()，这是运行时验证与玩家模拟的数据来源。

## 离线约束
浏览器打开本地 HTML（file://）时不能引用任何 http/https 外部资源，CDN 的 Phaser 也不可用。Demo 阶段用原生 Canvas 实现等价玩法；若必须 Phaser，把 phaser.min.js 下载到本地同目录引用。所有资产必须本地相对路径。

## 游戏结束后的画面
胜利/失败后停止玩法推进（不再生成/移动下落物），但保留 requestAnimationFrame 继续绘制最终画面，让截图有内容。overlay 显示的同时把 state().status 固定为 win/lose，避免自动化读到中间态。
