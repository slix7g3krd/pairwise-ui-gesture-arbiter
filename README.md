# pairwise-ui-gesture-arbiter

## 领域

游戏、图形与多媒体

## 主题

UI 手势仲裁

## 说明

手势裁决、阈值统一与坐标系重映射。

## 修复点与入口对应

| 问题 | 修复策略 | 入口 |
| --- | --- | --- |
| 触摸手势与滚动容器抢事件 | 按优先级仲裁，一次只产生一个赢家 | `gesture_arbiter`、`scroll_conflict` |
| 长按与点击判定窗口重叠，一次操作双触发 | 阈值窗口严格互斥，窗口之间为 `pending` 不提前触发 | `long_press_threshold` |
| 输入法弹出后输入框焦点丢失 | 弹键盘时挂起原焦点，收起后恢复 | `ime_focus`、`focus_restore` |
| 手柄导航与触摸焦点各走一套状态 | 统一焦点源，两条路径只写同一个 `focus` | `nav_touch_unify`、`gamepad_focus` |
| 横竖屏切换后多指坐标系未重映射 | 以竖屏为规范坐标系，旋转时重映射全部触点/锚点并交换视口 | `coord_remap`、`rotate` |
| 事件冒泡顺序不固定 | 固定 capture（根→目标）→ target → bubble（目标→根） | `bubble_order` |

配套入口：`validation`（事件结构校验）、`audit`（只追加审计日志）、
`fallback`（无人认领事件的兜底）、`metrics`（仲裁统计）、
`determinism`（同输入重复仲裁 + 跨 `PYTHONHASHSEED` 子进程回放校验）。

所有入口均可接受单个位置参数（会话状态字典；非字典输入使用临时状态），
并返回非 None 的结构化结果。

## 运行测试

```bash
python3 -m pytest -q
```

行为测试见 `tests/test_behavior.py`，契约测试见
`tests/test_ui_gesture_arbiter.py`。

> 注：原契约测试把参数写成 `(1)`，它是整数而非一元组，`fn(*1)` 会直接
> 抛 `TypeError`，任何实现都无法通过；已修正为 `(1,)`。
