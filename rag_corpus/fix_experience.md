# 历史修复经验（Demo 语料：修复经验 chunk）

## 自动化钩子契约缺失
症状：QA 报"页面加载/契约失败"。原因：生成的游戏没暴露 window.__game。修复：Engineer 的 system prompt 加入硬性契约（start/action/state 三方法 + state 字段清单），并在静态审查中检查 __game 关键字。修复要声称解决的契约条目 ID 回填到 contract_claims，编排器验证闭环。

## 分数不更新
症状：action('collect') 后 state().score 不变。原因：addScore 里状态判断写错（status 不是 playing 时直接 return，但 QA 在 start 前调用了 collect）。修复：动作入口统一检查，自动化路径先 start() 再动作；把"分数变化"断言的执行顺序固化为 start → collect → read state。

## 胜利判定不触发
症状：collect 到 target 仍不显示胜利。原因：判定写的是 score > target（差一）或判定后没有调用 setStatus('win')。修复：胜利判定与分数更新写在同一处 addScore() 内，用 >= 比较；判定后同步 DOM 覆盖层 display。

## 胜利文案不可见
症状：状态是 win 但 Playwright 断言文案不可见。原因：overlay 的 z-index 被canvas 覆盖，或 display 未从 none 切换。修复：覆盖层用 position:absolute + inset:0 + z-index:10，父容器 position:relative；setStatus 内统一设置 display。

## 资产 404 导致白屏
症状：file:// 打开时贴图裂图甚至脚本中断。原因：img.src 加载失败抛错未捕获，或路径用了绝对路径。修复：onerror 回退纯色绘制；资产一律相对路径 assets/xxx.png；加载失败不阻断游戏循环。

## requestAnimationFrame 时序
症状：自动化 wait_for_timeout 后状态仍未更新。原因：页面用 rAF 且后台标签页被浏览器节流。修复：无头浏览器用 page.bring_to_page 或在动作后轮询 state() 至多 N 次；模拟玩家用真实事件循环 sleep 而不是压满 CPU。

## 高刷屏移动加速
症状：120Hz 屏幕上下落物速度翻倍。原因：每帧位移没有按 delta 归一化。修复：y += vy * delta/16。玩家模拟的多会话分数分布异常时优先排查此项。

## 漏接重复计数
症状：一次漏接 misses +2。原因：移出屏幕判定和碰撞判定都在遍历中 splice 同一数组，边界对象被处理两次。修复：遍历中先标记，遍历后统一删除；漏接只在其唯一的移出点计数。
