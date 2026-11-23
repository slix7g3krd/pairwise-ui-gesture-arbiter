# pairwise-ui-gesture-arbiter

## 领域

游戏、图形与多媒体

## 主题

UI 手势仲裁

## 说明

手势裁决、阈值统一与坐标系重映射。

初始状态下 `gameplay/ui_gesture_arbiter.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
