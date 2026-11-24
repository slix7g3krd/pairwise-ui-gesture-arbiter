"""六类输入/事件修复的行为测试。"""

import pytest

from gameplay import ui_gesture_arbiter as mod


# ---------------------------------------------------------------------------
# 1. 手势冲突按优先级仲裁
# ---------------------------------------------------------------------------
class TestGestureArbiter:
    def test_higher_priority_wins_and_lower_suppressed(self):
        result = mod.gesture_arbiter(
            {},
            [
                {"gesture": "tap", "id": 1},
                {"gesture": "scroll", "id": 2},
                {"gesture": "long_press", "id": 3},
            ],
        )
        assert result["winner"]["gesture"] == "long_press"
        assert {c["gesture"] for c in result["suppressed"]} == {
            "scroll",
            "tap",
        }
        # 一次仲裁只产生一个赢家，杜绝双触发。
        assert len(result["priority_order"]) == 3

    def test_tie_breaks_by_pointer_id_then_arrival_order(self):
        result = mod.gesture_arbiter(
            {},
            [
                {"gesture": "tap", "pointer_id": 9},
                {"gesture": "tap", "pointer_id": 2},
            ],
        )
        assert result["winner"]["pointer_id"] == 2

        result = mod.gesture_arbiter(
            {}, [{"gesture": "tap"}, {"gesture": "tap"}]
        )
        assert result["priority_order"][0] is result["winner"]

    def test_empty_claims_is_neutral(self):
        result = mod.gesture_arbiter({})
        assert result["winner"] is None
        assert result["reason"] == "no_claims"

    def test_explicit_priority_overrides_builtin_table(self):
        result = mod.gesture_arbiter(
            {},
            [
                {"gesture": "tap", "priority": 1},
                {"gesture": "gamepad"},
            ],
        )
        assert result["winner"]["gesture"] == "tap"

    def test_scroll_vs_gesture_axis_arbitration(self):
        # 纵向移动 + 容器只支持纵向滚动 => 滚动容器获胜。
        vertical = mod.scroll_conflict({}, 0, 40, ("vertical",))
        assert vertical["winner"] == "scroll"
        assert vertical["dominant_axis"] == "vertical"

        # 横向移动 + 容器只支持纵向 => 交给手势识别器。
        horizontal = mod.scroll_conflict({}, 40, 0, ("vertical",))
        assert horizontal["winner"] == "gesture"
        assert horizontal["dominant_axis"] == "horizontal"

        # 死区内不触发任何一方，避免滚动与手势抢事件。
        slop = mod.scroll_conflict({}, 2, 1)
        assert slop["winner"] == "none"
        assert slop["reason"] == "below_slop"


# ---------------------------------------------------------------------------
# 2. 长按与点击阈值互斥
# ---------------------------------------------------------------------------
class TestLongPressThreshold:
    def test_tap_within_window(self):
        result = mod.long_press_threshold({}, duration_ms=100)
        assert result["classification"] == "tap"

    def test_long_press_past_window(self):
        result = mod.long_press_threshold({}, duration_ms=600)
        assert result["classification"] == "long_press"

    def test_gap_between_windows_is_pending_not_double_fire(self):
        # 250 < 300 < 500：落在互斥窗口之间，只能 pending，
        # 不能既算 tap 又算 long_press。
        result = mod.long_press_threshold({}, duration_ms=300)
        assert result["classification"] == "pending"

    def test_movement_cancels_press(self):
        result = mod.long_press_threshold(
            {}, duration_ms=600, move_px=50
        )
        assert result["classification"] == "cancelled"

    def test_overlapping_windows_rejected(self):
        with pytest.raises(ValueError):
            mod.long_press_threshold(
                {},
                duration_ms=10,
                tap_max_ms=500,
                long_press_min_ms=400,
            )

    def test_default_config_is_exclusive(self):
        result = mod.long_press_threshold({})
        assert result["exclusive"] is True
        assert result["long_press_min_ms"] > result["tap_max_ms"]


# ---------------------------------------------------------------------------
# 3. 输入法收起后恢复焦点
# ---------------------------------------------------------------------------
class TestImeFocus:
    def test_show_suspends_focus_and_hide_restores_it(self):
        state = {"focus": "list_item_7"}
        shown = mod.ime_focus(state, "show", field="search_box")
        assert shown["ime_open"] is True
        assert shown["focus"] == "search_box"
        assert shown["suspended"] == "list_item_7"

        hidden = mod.ime_focus(state, "hide")
        assert hidden["ime_open"] is False
        assert hidden["restored"] == "list_item_7"
        assert state["focus"] == "list_item_7"

    def test_hide_without_suspended_focus_keeps_current(self):
        state = {"focus": "keep_me"}
        result = mod.ime_focus(state, "hide")
        assert result["restored"] is None
        assert state["focus"] == "keep_me"

    def test_focus_restore_uses_stack(self):
        state = {"focus": "a", "focus_stack": ["b", "c"]}
        result = mod.focus_restore(state)
        assert result["restored"] == "c"
        assert state["focus"] == "c"

    def test_focus_restore_explicit_target(self):
        state = {"focus": "a"}
        result = mod.focus_restore(state, "d")
        assert result["focus"] == "d"
        assert state["focus_stack"] == ["a"]

    def test_unknown_ime_action_rejected(self):
        with pytest.raises(ValueError):
            mod.ime_focus({}, "toggle")


# ---------------------------------------------------------------------------
# 4. 导航焦点与触摸焦点同源
# ---------------------------------------------------------------------------
class TestUnifiedFocus:
    def test_touch_and_gamepad_share_one_focus_owner(self):
        state = {}
        mod.nav_touch_unify(state, target="btn_a", source="touch")
        assert state["focus"] == "btn_a"

        mod.gamepad_focus(
            state, direction="right", candidates=["btn_a", "btn_b", "btn_c"]
        )
        # 手柄移动后的焦点与触摸焦点写在同一个字段，不存在第二套状态。
        assert state["focus"] == "btn_b"
        view = mod.nav_touch_unify(state)
        assert view["focus"] == "btn_b"
        assert view["owner"] == "unified_focus_state"

    def test_gamepad_wrap_around(self):
        state = {"focus": "btn_c"}
        result = mod.gamepad_focus(
            state, direction="right",
            candidates=["btn_a", "btn_b", "btn_c"], wrap=True,
        )
        assert result["focus"] == "btn_a"

    def test_gamepad_no_wrap_clamps(self):
        state = {"focus": "btn_c"}
        result = mod.gamepad_focus(
            state, direction="right",
            candidates=["btn_a", "btn_b", "btn_c"], wrap=False,
        )
        assert result["focus"] == "btn_c"

    def test_gamepad_without_focus_starts_at_first_for_down(self):
        state = {}
        result = mod.gamepad_focus(
            state, direction="down", candidates=["a", "b"]
        )
        assert result["focus"] == "a"

    def test_focus_source_recorded(self):
        state = {}
        mod.nav_touch_unify(state, target="x", source="touch")
        mod.nav_touch_unify(state, target="y", source="gamepad")
        assert state["last_focus_source"] == "gamepad"
        assert state["focus_stack"] == ["x"]


# ---------------------------------------------------------------------------
# 5. 屏幕旋转后的坐标重映射
# ---------------------------------------------------------------------------
class TestCoordRemap:
    def test_portrait_is_identity(self):
        result = mod.coord_remap(
            {}, [10, 20], orientation="portrait",
            viewport={"w": 100, "h": 200},
        )
        assert result["mapped"] == [10, 20]

    def test_landscape_roundtrip_is_identity(self):
        viewport = {"w": 200, "h": 100}
        point = [30, 70]
        to_canonical = mod.coord_remap(
            {}, point, orientation="landscape", viewport=viewport,
            direction="to_canonical",
        )
        back = mod.coord_remap(
            {}, to_canonical["mapped"], orientation="landscape",
            viewport=viewport, direction="to_device",
        )
        assert back["mapped"] == point

    def test_known_landscape_inverse_mapping(self):
        # 横屏设备 200x100，规范（竖屏）尺寸 100x200。
        # landscape 正向: (x, y) -> (ch - y, x)；逆: (x, y) -> (y, ch - x)
        result = mod.coord_remap(
            {}, [100, 30], orientation="landscape",
            viewport={"w": 200, "h": 100},
        )
        assert result["mapped"] == [30, 100]
        assert result["canonical_size"] == {"w": 100, "h": 200}

    def test_reverse_portrait_180_rotation(self):
        result = mod.coord_remap(
            {}, [10, 20], orientation="reverse_portrait",
            viewport={"w": 100, "h": 200},
        )
        assert result["mapped"] == [90, 180]

    def test_multi_pointer_dict_remapped(self):
        result = mod.coord_remap(
            {},
            {0: [100, 30], 1: [50, 40]},
            orientation="landscape",
            viewport={"w": 200, "h": 100},
        )
        assert result["mapped"][0] == [30, 100]
        assert result["mapped"][1] == [40, 150]

    def test_unknown_orientation_reported_not_raised(self):
        result = mod.coord_remap({}, [1, 1], orientation="sideways")
        assert result["ok"] is False

    def test_rotate_updates_orientation_viewport_and_contacts(self):
        state = {
            "orientation": "portrait",
            "viewport": {"w": 100, "h": 200},
            "contacts": {0: [10, 20]},
            "anchor": [5, 5],
        }
        result = mod.rotate(state, "landscape")
        assert result["ok"] is True
        assert state["orientation"] == "landscape"
        # 横竖屏切换后视口宽高交换。
        assert state["viewport"] == {"w": 200, "h": 100}
        # 触点被重映射进规范坐标系，旋转后多指手势坐标系连续。
        assert state["contacts"][0] == [10, 20]
        assert state["anchor"] == [5, 5]

    def test_rotate_relative_direction(self):
        state = {"orientation": "portrait", "viewport": {"w": 10, "h": 20}}
        mod.rotate(state, direction="cw")
        assert state["orientation"] == "landscape"
        mod.rotate(state, direction="cw")
        assert state["orientation"] == "reverse_portrait"
        mod.rotate(state, direction="ccw")
        assert state["orientation"] == "landscape"


# ---------------------------------------------------------------------------
# 6. 固定的事件冒泡顺序
# ---------------------------------------------------------------------------
class TestBubbleOrder:
    def test_capture_target_bubble_order(self):
        result = mod.bubble_order({}, ["root", "panel", "button"])
        assert result["capture"] == ["root", "panel"]
        assert result["target"] == "button"
        assert result["bubble"] == ["panel", "root"]
        assert [item["phase"] for item in result["phases"]] == [
            "capture",
            "capture",
            "target",
            "bubble",
            "bubble",
        ]
        nodes = [item["node"] for item in result["phases"]]
        assert nodes == ["root", "panel", "button", "panel", "root"]

    def test_order_is_stable_across_repeated_calls(self):
        first = mod.bubble_order({}, ["a", "b", "c", "d"])
        second = mod.bubble_order({}, ["a", "b", "c", "d"])
        assert first["phases"] == second["phases"]

    def test_empty_path_is_neutral(self):
        result = mod.bubble_order({})
        assert result["phases"] == []
        assert result["target"] is None


# ---------------------------------------------------------------------------
# 配套能力
# ---------------------------------------------------------------------------
class TestSupporting:
    def test_validation_accepts_well_formed_event(self):
        result = mod.validation(
            {},
            {
                "type": "tap",
                "timestamp": 12,
                "phase": "bubble",
                "pointer_id": 0,
            },
        )
        assert result["valid"] is True
        assert result["errors"] == []

    def test_validation_collects_all_errors(self):
        result = mod.validation(
            {},
            {"timestamp": "now", "phase": "explode",
             "orientation": "tilted", "pointer_id": [1]},
        )
        assert result["valid"] is False
        assert set(result["errors"]) == {
            "missing_type",
            "timestamp_not_numeric",
            "unknown_phase",
            "unknown_orientation",
            "pointer_id_wrong_type",
        }

    def test_audit_is_append_only_and_sequenced(self):
        state = {}
        mod.audit(state, {"op": "one"})
        snapshot = mod.audit(state, {"op": "two"})
        assert snapshot["count"] == 2
        assert [entry["seq"] for entry in snapshot["entries"]] == [0, 1]
        # 后续追加不会改变历史记录。
        assert state["audit_log"][0]["op"] == "one"

    def test_fallback_delivers_unclaimed_tap_ignores_others(self):
        tap = mod.fallback({}, {"type": "tap", "target": "btn"})
        assert tap["action"] == "deliver"
        assert tap["target"] == "btn"

        hover = mod.fallback({}, {"type": "hover"})
        assert hover["action"] == "ignore"

    def test_metrics_counts_decisions_wins_and_suppressions(self):
        state = {}
        mod.gesture_arbiter(
            state,
            [{"gesture": "scroll"}, {"gesture": "tap"}, {"gesture": "hover"}],
        )
        mod.scroll_conflict(state, 5, 0, ("vertical",))  # 死区，不计决策
        stats = mod.metrics(state)
        assert stats["decisions"] == 1
        assert stats["wins"]["scroll"] == 1
        assert stats["suppressed"] == 2

    def test_determinism_replay_and_cross_seed(self):
        claims = [
            {"gesture": "tap", "id": 1},
            {"gesture": "scroll", "id": 2},
            {"gesture": "long_press", "id": 3},
        ]
        result = mod.determinism({}, claims)
        assert result["consistent"] is True
        assert result["cross_seed_ok"] is True
        assert result["winner"]["gesture"] == "long_press"
        # 摘要是 64 位 sha256 十六进制串。
        assert len(result["digest"]) == 64

    def test_determinism_without_claims(self):
        result = mod.determinism({})
        assert result["consistent"] is True
        assert result["cross_seed_ok"] is None


# ---------------------------------------------------------------------------
# 单参数契约：每个入口直接吃一个整数也必须返回非 None
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fn_name",
    [
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
    ],
)
def test_single_positional_arg_contract(fn_name):
    fn = getattr(mod, fn_name)
    assert fn(1) is not None
