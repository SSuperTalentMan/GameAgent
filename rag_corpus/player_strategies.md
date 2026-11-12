# 玩家策略模板库（独立集合 player_strategy_templates）

## casual 休闲玩家
节奏放松的轻量玩家，移动缓慢，看到金币才有概率去接，经常中途切走。适合验证基础留存：session_timeout=25 move_interval=0.8 collect_prob=0.45。行为特征：动作间隔长、单会话分数中等、超时放弃比例高，是 D1 留存的主要来源。

## hardcore 硬核玩家
目标导向，几乎每帧都在优化操作，动作密集且持续收集。适合验证通关率上限：session_timeout=30 move_interval=0.35 collect_prob=0.85。行为特征：会话分数接近 target、几乎不超时，用于检查胜利判定的可达成性与难度曲线是否过缓。

## lurker 观望型玩家
先看不动，长时间只移动不收集，偶尔试一次。适合验证新手引导与首分钟体验：session_timeout=20 move_interval=0.6 collect_prob=0.15。行为特征：前 10 秒几乎无分数变化，是流失点分布里"开局流失"的典型样本。

## pay_tester 付费意愿测试者
通关意愿强、对结算页敏感，胜利后大概率点付费。用于付费漏斗验证：session_timeout=28 move_interval=0.5 collect_prob=0.7。行为特征：通关率高、pay_click 频繁，为付费转化率提供上界估计。
