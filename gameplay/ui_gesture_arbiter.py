"""UI 手势仲裁核心模块。

覆盖六类输入/事件问题的修复：

1. 手势冲突按优先级仲裁 —— ``gesture_arbiter`` / ``scroll_conflict``
2. 长按与点击阈值互斥 —— ``long_press_threshold``
3. 输入法收起后恢复焦点 —— ``ime_focus`` / ``focus_restore``
4. 导航焦点与触摸焦点同源 —— ``nav_touch_unify`` / ``gamepad_focus``
5. 屏幕旋转后重映射坐标 —— ``rotate`` / ``coord_remap``
6. 事件冒泡顺序固定 —— ``bubble_order``

其余入口（``validation`` / ``audit`` / ``fallback`` / ``metrics`` /
``determinism``）提供配套的校验、审计、兜底、统计与确定性回放能力。

所有入口均可接受单个位置参数（通常为会话状态字典），调用成功后返回
非 None 的结果，便于契约测试与外部直接驱动。
"""

import hashlib
import json
import os
import subprocess
import sys

__all__ = [
    "gesture_arbiter",
    "long_press_threshold",
    "focus_restore",
    "nav_touch_unify",
    "coord_remap",
    "bubble_order",
    "scroll_conflict",
    "validation",
    "audit",
    "fallback",
    "ime_focus",
    "metrics",
    "gamepad_focus",
    "rotate",
    "determinism",
]

# ---------------------------------------------------------------------------
# 统一常量与优先级
# ---------------------------------------------------------------------------

#: 手势优先级：数字越小优先级越高（仲裁时高优先级可压制低优先级）。
GESTURE_PRIORITY = {
    "gamepad": 10,
    "keyboard": 20,
    "ime": 30,
    "multi_touch": 40,
    "pinch": 40,
    "long_press": 50,
    "scroll": 60,
    "swipe": 70,
    "drag": 70,
    "tap": 80,
    "hover": 90,
    "unknown": 100,
}

#: 点击判定窗口（毫秒）。
TAP_MAX_MS = 250

#: 点击允许的最大位移（像素）。
TAP_SLOP_PX = 12

#: 长按判定窗口（毫秒），严格大于点击窗口，二者互斥。
LONG_PRESS_MIN_MS = 500

#: 滚动方向判定死区（像素），小于该值视为抖动。
SCROLL_SLOP_PX = 8

#: 合法屏幕方向。
_ORIENTATIONS = ("portrait", "landscape", "reverse_portrait", "reverse_landscape")

#: 合法事件阶段。
_PHASES = ("capture", "target", "bubble")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _new_state():
    """创建一份新的会话状态。"""
    return {
        "claims": [],
        "orientation": "portrait",
        "viewport": {"w": 0, "h": 0},
        "contacts": {},
        "anchor": None,
        "focus": None,
        "focus_stack": [],
        "ime_open": False,
        "ime_focus": None,
        "audit_log": [],
        "metrics": {"decisions": 0, "wins": {}, "suppressed": 0},
        "events": [],
    }


def _state(payload):
    """从入口参数中取出可变会话状态。

    传入字典时直接返回（调用方的状态会被更新）；传入其它对象（例如
    契约测试里的整数 ``1``）时返回一份独立的临时状态，保证单参数调用
    不会抛异常。
    """
    if isinstance(payload, dict):
        state = payload
        for key, value in _new_state().items():
            state.setdefault(key, value)
        return state
    return _new_state()


def _priority(claim):
    """取一条手势声明的优先级，显式 priority 覆盖内置表。"""
    explicit = claim.get("priority")
    if isinstance(explicit, (int, float)):
        return int(explicit)
    return GESTURE_PRIORITY.get(claim.get("gesture", "unknown"),
                                GESTURE_PRIORITY["unknown"])


def _normalize_claims(raw):
    """把任意形式的手势声明规整为字典列表。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    normalized = []
    for item in raw:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            normalized.append({"gesture": str(item)})
    return normalized


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 1a. 手势冲突按优先级仲裁
# ---------------------------------------------------------------------------
def gesture_arbiter(payload, claims=None):
    """对同一时间窗内的多条手势声明做优先级仲裁。

    仲裁规则固定，保证确定性：

    * 优先级数字小者胜（见 :data:`GESTURE_PRIORITY`），声明可自带
      ``priority`` 覆盖内置表；
    * 同级时按 ``pointer_id`` / ``id`` / 到达序号排序，序号小者胜；
    * 败者写入 ``suppressed``，不会与胜者同时触发。

    返回仲裁结果字典，并把结果累计进会话状态。
    """
    state = _state(payload)
    if claims is None and isinstance(payload, dict):
        claims = payload.get("claims")
    claims = _normalize_claims(claims)

    ordered = sorted(
        enumerate(claims),
        key=lambda pair: (
            _priority(pair[1]),
            _as_int(pair[1].get("pointer_id", pair[1].get("id", pair[0])),
                    pair[0]),
            pair[0],
        ),
    )

    winner = ordered[0][1] if ordered else None
    suppressed = [claim for _, claim in ordered[1:]]

    state["claims"] = claims
    state["metrics"]["decisions"] += 1
    if winner is not None:
        name = winner.get("gesture", "unknown")
        state["metrics"]["wins"][name] = \
            state["metrics"]["wins"].get(name, 0) + 1
    state["metrics"]["suppressed"] += len(suppressed)
    state["audit_log"].append({
        "op": "gesture_arbiter",
        "winner": None if winner is None else winner.get("gesture"),
        "suppressed": [c.get("gesture") for c in suppressed],
        "seq": len(state["audit_log"]),
    })

    return {
        "winner": winner,
        "suppressed": suppressed,
        "priority_order": [claim for _, claim in ordered],
        "reason": "highest_priority" if winner else "no_claims",
    }


# ---------------------------------------------------------------------------
# 1b. 触摸手势与滚动容器的冲突仲裁
# ---------------------------------------------------------------------------
def scroll_conflict(payload, delta_x=0, delta_y=0,
                    container_axes=("vertical",), slop=None):
    """裁决一次移动归滚动容器还是归手势识别器。

    * 移动量低于 ``slop`` 死区：忽略，双方都不触发；
    * 主轴落在容器可滚动轴上：滚动容器获胜（``scroll``）；
    * 否则交给手势识别器（``gesture``）。
    """
    state = _state(payload)
    if slop is None:
        slop = SCROLL_SLOP_PX
    dx = _as_int(delta_x)
    dy = _as_int(delta_y)

    magnitude = max(abs(dx), abs(dy))
    if magnitude < slop:
        winner, reason = "none", "below_slop"
    else:
        if abs(dy) >= abs(dx):
            dominant = "vertical"
        else:
            dominant = "horizontal"
        if dominant in tuple(container_axes):
            winner, reason = "scroll", "axis_consumed_by_container"
        else:
            winner, reason = "gesture", "axis_not_scrollable"

    state["audit_log"].append({
        "op": "scroll_conflict",
        "winner": winner,
        "delta": [dx, dy],
        "seq": len(state["audit_log"]),
    })
    if winner != "none":
        state["metrics"]["decisions"] += 1
        state["metrics"]["wins"][winner] = \
            state["metrics"]["wins"].get(winner, 0) + 1

    return {
        "winner": winner,
        "delta": {"x": dx, "y": dy},
        "dominant_axis": None if winner == "none" else dominant,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 2. 长按与点击阈值互斥
# ---------------------------------------------------------------------------
def long_press_threshold(payload=None, duration_ms=None, move_px=0,
                         tap_max_ms=TAP_MAX_MS,
                         long_press_min_ms=LONG_PRESS_MIN_MS,
                         slop_px=TAP_SLOP_PX):
    """按互斥阈值把一次按压分类为 tap / long_press / cancelled。

    阈值窗口互斥：``long_press_min_ms`` 必须严格大于 ``tap_max_ms``，
    否则直接抛 ``ValueError``，从配置层杜绝一次操作同时命中两种判定。

    * 位移超过 ``slop_px``：``cancelled``（变成拖动，不再是点按）；
    * 时长达到长按窗口：``long_press``；
    * 时长在点击窗口内：``tap``；
    * 落在两个窗口之间：``pending``（继续观察，绝不提前触发点击）。
    """
    if long_press_min_ms <= tap_max_ms:
        raise ValueError(
            "threshold windows must be mutually exclusive: "
            "long_press_min_ms (%d) must be greater than tap_max_ms (%d)"
            % (long_press_min_ms, tap_max_ms)
        )

    state = _state(payload)
    duration = None if duration_ms is None else _as_int(duration_ms)
    move = _as_int(move_px)

    if duration is None:
        # 单参数/缺省调用：回传当前生效的互斥阈值配置。
        result = {
            "classification": None,
            "tap_max_ms": tap_max_ms,
            "long_press_min_ms": long_press_min_ms,
            "slop_px": slop_px,
            "exclusive": long_press_min_ms > tap_max_ms,
        }
    elif move > slop_px:
        result = {
            "classification": "cancelled",
            "duration_ms": duration,
            "move_px": move,
            "reason": "moved_beyond_slop",
        }
    elif duration >= long_press_min_ms:
        result = {
            "classification": "long_press",
            "duration_ms": duration,
            "move_px": move,
            "reason": "reached_long_press_window",
        }
    elif duration <= tap_max_ms:
        result = {
            "classification": "tap",
            "duration_ms": duration,
            "move_px": move,
            "reason": "within_tap_window",
        }
    else:
        result = {
            "classification": "pending",
            "duration_ms": duration,
            "move_px": move,
            "reason": "between_mutually_exclusive_windows",
        }

    state["audit_log"].append({
        "op": "long_press_threshold",
        "classification": result["classification"],
        "seq": len(state["audit_log"]),
    })
    return result


# ---------------------------------------------------------------------------
# 3. 输入法焦点：弹出挂起、收起恢复
# ---------------------------------------------------------------------------
def ime_focus(payload, action="hide", field=None):
    """处理输入法（IME）弹出/收起时的输入框焦点。

    ``action="show"``：记录当前焦点为挂起焦点并把焦点交给输入框，
    防止弹键盘导致原焦点丢失；``action="hide"``：键盘收起后把焦点
    恢复到挂起的元素；无挂起元素时保持当前焦点不动。
    """
    state = _state(payload)
    action = str(action)

    if action == "show":
        if state.get("ime_focus") is None:
            state["ime_focus"] = state.get("focus")
        state["ime_open"] = True
        if field is not None:
            state["focus"] = field
        restored = None
    elif action == "hide":
        state["ime_open"] = False
        restored = state.get("ime_focus")
        if restored is not None:
            state["focus"] = restored
        state["ime_focus"] = None
    else:
        raise ValueError("unknown ime action: %r" % action)

    state["audit_log"].append({
        "op": "ime_focus",
        "action": action,
        "restored": restored,
        "seq": len(state["audit_log"]),
    })
    return {
        "ime_open": state["ime_open"],
        "focus": state.get("focus"),
        "suspended": state.get("ime_focus"),
        "restored": restored,
    }


def focus_restore(payload, target=None):
    """显式恢复/设置焦点（IME 收起后的恢复入口）。

    不传 ``target`` 时优先恢复 IME 挂起焦点，其次恢复焦点栈顶；
    显式传入 ``target`` 时直接聚焦该元素并压栈。
    """
    state = _state(payload)

    if target is None:
        target = state.get("ime_focus")
        if target is None and state.get("focus_stack"):
            target = state["focus_stack"][-1]

    if target is None:
        return {
            "focus": state.get("focus"),
            "restored": None,
            "reason": "no_saved_focus",
        }

    if state.get("focus") is not None and state["focus"] != target:
        state["focus_stack"].append(state["focus"])
    state["focus"] = target
    state["audit_log"].append({
        "op": "focus_restore",
        "focus": target,
        "seq": len(state["audit_log"]),
    })
    return {"focus": target, "restored": target, "reason": "restored"}


# ---------------------------------------------------------------------------
# 4. 导航焦点与触摸焦点同源
# ---------------------------------------------------------------------------
def nav_touch_unify(payload, target=None, source="touch"):
    """触摸与方向导航共用同一个焦点所有者（单一焦点源）。

    无论焦点来自触摸还是手柄/键盘导航，都只写入 ``state['focus']``，
    不再维护两套互相漂移的焦点状态。
    """
    state = _state(payload)
    if target is not None:
        previous = state.get("focus")
        if previous is not None and previous != target:
            state["focus_stack"].append(previous)
        state["focus"] = target
        state["last_focus_source"] = source
        state["audit_log"].append({
            "op": "nav_touch_unify",
            "focus": target,
            "source": source,
            "seq": len(state["audit_log"]),
        })

    return {
        "focus": state.get("focus"),
        "source": state.get("last_focus_source"),
        "owner": "unified_focus_state",
    }


def gamepad_focus(payload, direction=None, candidates=None, wrap=True):
    """手柄方向键在统一焦点源上移动焦点。

    ``direction`` 取 ``up/down/left/right``，``candidates`` 为可聚焦
    元素 id 列表（也可放在状态里）。移动结果通过
    :func:`nav_touch_unify` 同一条路径写焦点，保证与触摸焦点同源。
    """
    state = _state(payload)
    if candidates is None:
        candidates = state.get("focus_candidates", [])
    candidates = list(candidates)

    if not candidates or direction is None:
        return nav_touch_unify(state, source="gamepad")

    current = state.get("focus")
    if current in candidates:
        index = candidates.index(current)
    else:
        index = -1 if direction in ("down", "right") else 0

    step = 1 if direction in ("down", "right") else -1
    next_index = index + step
    if wrap:
        next_index %= len(candidates)
    else:
        next_index = max(0, min(len(candidates) - 1, next_index))

    target = candidates[next_index]
    result = nav_touch_unify(state, target=target, source="gamepad")
    result["direction"] = direction
    return result


# ---------------------------------------------------------------------------
# 5. 屏幕旋转后的坐标系重映射
# ---------------------------------------------------------------------------
def _forward(point, orientation, cw, ch):
    """规范坐标 -> 给定方向下的设备坐标。"""
    x, y = point
    if orientation == "portrait":
        return [x, y]
    if orientation == "landscape":
        # 顺时针 90°：(x, y) -> (ch - y, x)
        return [ch - y, x]
    if orientation == "reverse_portrait":
        return [cw - x, ch - y]
    if orientation == "reverse_landscape":
        # 逆时针 90°：(x, y) -> (y, cw - x)
        return [y, cw - x]
    raise ValueError("unknown orientation: %r" % orientation)


def _inverse(point, orientation, cw, ch):
    """设备坐标 -> 规范坐标（:func:`_forward` 的逆变换）。"""
    x, y = point
    if orientation == "portrait":
        return [x, y]
    if orientation == "landscape":
        # 逆映射：(x, y) -> (y, ch - x)
        return [y, ch - x]
    if orientation == "reverse_portrait":
        return [cw - x, ch - y]
    if orientation == "reverse_landscape":
        # 逆映射：(x, y) -> (cw - y, x)
        return [cw - y, x]
    raise ValueError("unknown orientation: %r" % orientation)


def _canonical_dims(viewport, orientation):
    w = _as_int(viewport.get("w"))
    h = _as_int(viewport.get("h"))
    if orientation in ("landscape", "reverse_landscape"):
        # 设备处于横屏：规范（竖屏）宽高与设备宽高互换。
        return h, w
    return w, h


def coord_remap(payload, points=None, orientation=None, viewport=None,
                direction="to_canonical"):
    """屏幕旋转后把触点坐标重映射到规范（竖屏）坐标系，或反向映射。

    ``points`` 支持：

    * 单个 ``[x, y]`` / ``(x, y)``；
    * 点列表 ``[[x, y], ...]``；
    * 触点字典 ``{"pointer_id": [x, y], ...}``；
    * 缺省时重映射状态中保存的全部活动触点。

    横竖屏互换时宽高自动交换，保证多指手势旋转后仍落在正确位置。
    """
    state = _state(payload)
    if orientation is None:
        orientation = state.get("orientation", "portrait")
    if viewport is None:
        viewport = state.get("viewport", {"w": 0, "h": 0})
    if orientation not in _ORIENTATIONS:
        return {"ok": False, "reason": "unknown_orientation",
                "orientation": orientation}

    cw, ch = _canonical_dims(viewport, orientation)
    transform = _inverse if direction == "to_canonical" else _forward

    if points is None:
        points = dict(state.get("contacts", {}))
        input_kind = "contacts"
    elif isinstance(points, dict):
        input_kind = "dict"
    elif (isinstance(points, (list, tuple))
          and points and isinstance(points[0], (list, tuple))):
        input_kind = "list"
    else:
        input_kind = "point"
        points = [points]

    if input_kind in ("contacts", "dict"):
        mapped = {
            pointer_id: transform(list(point), orientation, cw, ch)
            for pointer_id, point in points.items()
        }
    else:
        mapped = [transform(list(point), orientation, cw, ch)
                  for point in points]
        if input_kind == "point":
            mapped = mapped[0]

    return {
        "ok": True,
        "mapped": mapped,
        "orientation": orientation,
        "canonical_size": {"w": cw, "h": ch},
        "direction": direction,
    }


def rotate(payload, new_orientation=None, direction="cw"):
    """处理一次屏幕旋转：更新方向、交换视口、重映射全部触点与锚点。

    ``new_orientation`` 显式指定目标方向；否则按 ``direction``
    （``cw``/``ccw``）相对当前方向旋转 90°。
    """
    state = _state(payload)
    current = state.get("orientation", "portrait")

    if new_orientation is None:
        order = _ORIENTATIONS
        step = 1 if direction == "cw" else -1
        new_orientation = order[(order.index(current) + step) % len(order)]
    if new_orientation not in _ORIENTATIONS:
        return {"ok": False, "reason": "unknown_orientation",
                "orientation": new_orientation}

    old_viewport = dict(state.get("viewport", {"w": 0, "h": 0}))
    remapped_contacts = coord_remap(
        state,
        points=dict(state.get("contacts", {})),
        orientation=current,
        viewport=old_viewport,
        direction="to_canonical",
    )
    if remapped_contacts["ok"] and state.get("contacts"):
        state["contacts"] = remapped_contacts["mapped"]

    if current != new_orientation and \
            (current.startswith(("landscape", "reverse_landscape"))
             != new_orientation.startswith(("landscape", "reverse_landscape"))):
        state["viewport"] = {"w": old_viewport.get("h", 0),
                             "h": old_viewport.get("w", 0)}
    else:
        state["viewport"] = old_viewport

    anchor = state.get("anchor")
    if anchor is not None:
        anchor_remap = coord_remap(
            state, points=anchor, orientation=current,
            viewport=old_viewport, direction="to_canonical")
        if anchor_remap["ok"]:
            state["anchor"] = anchor_remap["mapped"]

    state["orientation"] = new_orientation
    state["audit_log"].append({
        "op": "rotate",
        "from": current,
        "to": new_orientation,
        "seq": len(state["audit_log"]),
    })
    return {
        "ok": True,
        "orientation": new_orientation,
        "viewport": state["viewport"],
        "contacts": dict(state.get("contacts", {})),
        "anchor": state.get("anchor"),
    }


# ---------------------------------------------------------------------------
# 6. 固定的事件冒泡顺序
# ---------------------------------------------------------------------------
def bubble_order(payload, target_path=None, event=None):
    """返回固定的事件传播序列：capture（根→目标）→ target → bubble（目标→根）。

    ``target_path`` 为从根到目标的节点 id 列表，例如
    ``["root", "panel", "button"]``。输出顺序恒定，不依赖节点注册顺序
    或字典遍历顺序。
    """
    state = _state(payload)
    if target_path is None and isinstance(payload, dict):
        target_path = payload.get("target_path", [])
    path = list(target_path or [])

    if not path:
        return {
            "phases": [],
            "capture": [],
            "target": None,
            "bubble": [],
        }

    target = path[-1]
    capture = [{"node": node, "phase": "capture"} for node in path[:-1]]
    bubble = [{"node": node, "phase": "bubble"}
              for node in reversed(path[:-1])]
    phases = capture + [{"node": target, "phase": "target"}] + bubble

    state["audit_log"].append({
        "op": "bubble_order",
        "target": target,
        "depth": len(path),
        "seq": len(state["audit_log"]),
    })
    return {
        "phases": phases,
        "capture": [item["node"] for item in capture],
        "target": target,
        "bubble": [item["node"] for item in bubble],
    }


# ---------------------------------------------------------------------------
# 配套：校验 / 审计 / 兜底 / 统计 / 确定性
# ---------------------------------------------------------------------------
def validation(payload, event=None):
    """校验事件结构，返回结构化结果而不是抛异常。"""
    if event is None:
        event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        return {"valid": False, "errors": ["event_must_be_dict"], "event": event}

    errors = []
    if "type" not in event:
        errors.append("missing_type")
    if "timestamp" in event and not isinstance(event["timestamp"],
                                                (int, float)):
        errors.append("timestamp_not_numeric")
    phase = event.get("phase")
    if phase is not None and phase not in _PHASES:
        errors.append("unknown_phase")
    orientation = event.get("orientation")
    if orientation is not None and orientation not in _ORIENTATIONS:
        errors.append("unknown_orientation")
    if "pointer_id" in event and not isinstance(
            event["pointer_id"], (int, str)):
        errors.append("pointer_id_wrong_type")

    return {"valid": not errors, "errors": errors, "event": event}


def audit(payload, entry=None):
    """向只追加的审计日志写入一条记录，返回当前日志快照。"""
    state = _state(payload)
    record = {"op": "audit", "seq": len(state["audit_log"])}
    if isinstance(entry, dict):
        record.update(entry)
    elif entry is not None:
        record["detail"] = entry
    state["audit_log"].append(record)
    return {"entries": list(state["audit_log"]), "count": len(state["audit_log"])}


def fallback(payload, event=None):
    """无识别器认领事件时的兜底决策。"""
    state = _state(payload)
    if event is None and isinstance(payload, dict):
        event = payload.get("event")
    if not isinstance(event, dict):
        event = {}

    event_type = event.get("type", "unknown")
    target = event.get("target")
    if event_type == "tap" and target is not None:
        decision = {"action": "deliver", "target": target,
                    "reason": "unclaimed_tap_delivered_to_target"}
    else:
        decision = {"action": "ignore", "target": target,
                    "reason": "no_recognizer_claimed_event"}

    state["audit_log"].append({
        "op": "fallback",
        "event_type": event_type,
        "action": decision["action"],
        "seq": len(state["audit_log"]),
    })
    return decision


def metrics(payload):
    """返回仲裁统计：决策总数、各手势胜出次数、被压制次数。"""
    state = _state(payload)
    return {
        "decisions": state["metrics"]["decisions"],
        "wins": dict(state["metrics"]["wins"]),
        "suppressed": state["metrics"]["suppressed"],
        "audit_entries": len(state["audit_log"]),
    }


def _replay_digest(claims):
    """对同一组声明做确定性仲裁并计算摘要（排序键固定，与哈希种子无关）。"""
    result = gesture_arbiter(_new_state(), claims)
    canonical = json.dumps(
        {"winner": result["winner"],
         "order": [c.get("gesture") for c in result["priority_order"]]},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest, result["winner"]


def determinism(payload, claims=None):
    """验证仲裁结果的确定性。

    同一会话内：对同一组声明重复仲裁，比较摘要；另外以
    ``PYTHONHASHSEED=0/1`` 各起一个子进程回放，确认结果跨进程、
    跨哈希种子完全一致。
    """
    state = _state(payload)
    if claims is None:
        claims = state.get("claims", [])
    claims = _normalize_claims(claims)

    digest_a, winner_a = _replay_digest(claims)
    digest_b, _ = _replay_digest(claims)
    consistent = digest_a == digest_b

    cross_seed_ok = None
    if claims:
        module = __name__
        script = (
            "import json, sys;"
            "import %s as m;"
            "claims = json.loads(sys.stdin.read());"
            "digest, winner = m._replay_digest(claims);"
            "sys.stdout.write(json.dumps({'digest': digest, "
            "'winner': winner}, sort_keys=True))" % module
        )
        payload_text = json.dumps(claims, sort_keys=True)
        cross = {}
        for seed in ("0", "1", "random"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            completed = subprocess.run(
                [sys.executable, "-c", script],
                input=payload_text, capture_output=True, text=True,
                env=env, timeout=30, check=False,
            )
            if completed.returncode != 0:
                cross[seed] = {"error": completed.stderr.strip()[:200]}
            else:
                cross[seed] = json.loads(completed.stdout)
        digests = {value.get("digest")
                   for value in cross.values() if "digest" in value}
        cross_seed_ok = len(digests) == 1 and digest_a in digests
        if cross_seed_ok is False:
            consistent = False
    else:
        cross = {}

    return {
        "consistent": consistent,
        "digest": digest_a,
        "winner": winner_a,
        "cross_seed": cross,
        "cross_seed_ok": cross_seed_ok,
    }
